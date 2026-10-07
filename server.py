import json
import os
from urllib.parse import urljoin

from flask import Flask, Response, jsonify, request
import requests

app = Flask(__name__)

GATEWAY_VERSION = "1.2.0"
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("SAGEWIRE_GATEWAY_TIMEOUT", "20")
)
GOOGLE_USERINFO_URL = os.getenv(
    "SAGEWIRE_GOOGLE_USERINFO_URL",
    "https://openidconnect.googleapis.com/v1/userinfo",
)
SCOUT_TENANTS_ENV = "SAGEWIRE_SCOUT_TENANTS_JSON"

SERVICES = {
    "tts": os.getenv(
        "SAGEWIRE_TTS_URL",
        "https://tts.sagewire.dev",
    ).rstrip("/"),
    "stt": os.getenv(
        "SAGEWIRE_STT_URL",
        "https://stt.sagewire.dev",
    ).rstrip("/"),
    "loc": os.getenv(
        "SAGEWIRE_LOC_URL",
        "https://loc.sagewire.dev",
    ).rstrip("/"),
    "wx": os.getenv(
        "SAGEWIRE_WEATHER_URL",
        "https://weather.herdmate.ag",
    ).rstrip("/"),
    "onboarding": os.getenv(
        "SAGEWIRE_ONBOARDING_URL",
        "https://onboarding.sagewire.dev",
    ).rstrip("/"),
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

FORWARDED_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "authorization",
    "content-type",
    "user-agent",
    "x-correlation-id",
    "x-request-id",
}


@app.after_request
def add_cors_headers(response):
    response.headers.setdefault(
        "Access-Control-Allow-Origin",
        "*",
    )
    response.headers.setdefault(
        "Access-Control-Allow-Headers",
        (
            "Content-Type, Authorization, "
            "X-Correlation-ID, X-Request-ID"
        ),
    )
    response.headers.setdefault(
        "Access-Control-Allow-Methods",
        "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    )
    return response


def _gateway_error(
    service_name,
    message,
    status_code=502,
):
    return jsonify(
        {
            "error": {
                "code": "UPSTREAM_SERVICE_ERROR",
                "message": message,
                "service": service_name,
            }
        }
    ), status_code


def _forward_headers():
    headers = {}

    for name, value in request.headers.items():
        if name.lower() in FORWARDED_REQUEST_HEADERS:
            headers[name] = value

    return headers


def _proxy_response(upstream_response):
    excluded_headers = HOP_BY_HOP_HEADERS | {
        "content-encoding"
    }

    headers = [
        (name, value)
        for name, value in upstream_response.headers.items()
        if name.lower() not in excluded_headers
    ]

    return Response(
        upstream_response.content,
        status=upstream_response.status_code,
        headers=headers,
    )


def _proxy_request(
    service_name,
    upstream_path,
):
    base_url = SERVICES[service_name] + "/"
    target_url = urljoin(
        base_url,
        upstream_path.lstrip("/"),
    )

    try:
        upstream_response = requests.request(
            method=request.method,
            url=target_url,
            params=list(
                request.args.items(multi=True)
            ),
            headers=_forward_headers(),
            data=request.get_data(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )

    except requests.Timeout:
        return _gateway_error(
            service_name,
            "The upstream service timed out.",
            504,
        )

    except requests.RequestException:
        return _gateway_error(
            service_name,
            "The upstream service could not be reached.",
            502,
        )

    return _proxy_response(
        upstream_response
    )


class ScoutRegistryError(ValueError):
    """Raised when the server-side Scout registry is unsafe to use."""


def _load_scout_registry():
    raw_registry = os.getenv(SCOUT_TENANTS_ENV, "").strip()

    if not raw_registry:
        return []

    try:
        payload = json.loads(raw_registry)
    except json.JSONDecodeError as exc:
        raise ScoutRegistryError(
            "Scout tenant registry is not valid JSON."
        ) from exc

    operations = payload.get("operations")

    if not isinstance(operations, list):
        raise ScoutRegistryError(
            "Scout tenant registry must contain an operations list."
        )

    normalized = []
    claimed_emails = set()

    for operation in operations:
        if not isinstance(operation, dict):
            raise ScoutRegistryError(
                "Every Scout operation must be an object."
            )

        operation_id = str(operation.get("id", "")).strip()
        name = str(operation.get("name", "")).strip()
        sheet_id = str(operation.get("sheet_id", "")).strip()
        schema_version = str(
            operation.get("schema_version", "1")
        ).strip()
        status = str(
            operation.get("status", "ACTIVE")
        ).strip().upper()
        members = operation.get("members", [])
        features = operation.get("features", {})

        if not operation_id or not name or not sheet_id:
            raise ScoutRegistryError(
                "Every Scout operation requires id, name, and sheet_id."
            )

        if not isinstance(members, list) or not isinstance(features, dict):
            raise ScoutRegistryError(
                "Scout members must be a list and features must be an object."
            )

        normalized_members = set()

        for member in members:
            if isinstance(member, str):
                email = member
                enabled = True
            elif isinstance(member, dict):
                email = member.get("email", "")
                enabled = member.get("enabled", True) is True
            else:
                raise ScoutRegistryError(
                    "Scout members must be email strings or member objects."
                )

            email = str(email).strip().lower()

            if not email or not enabled:
                continue

            if email in claimed_emails:
                raise ScoutRegistryError(
                    "A Scout identity may belong to only one active operation."
                )

            normalized_members.add(email)
            claimed_emails.add(email)

        normalized.append(
            {
                "id": operation_id,
                "name": name,
                "sheet_id": sheet_id,
                "schema_version": schema_version,
                "status": status,
                "members": normalized_members,
                "features": {
                    "field_head_count": (
                        features.get("field_head_count", False) is True
                    ),
                    "animal_lookup": (
                        features.get("animal_lookup", False) is True
                    ),
                    "bovine_beacon": (
                        features.get("bovine_beacon", False) is True
                    ),
                },
            }
        )

    return normalized


def _verified_google_identity():
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
    ):
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "AUTHENTICATION_REQUIRED",
                        "message": "A Google bearer token is required.",
                    }
                }
            ),
            401,
        )

    try:
        response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={
                "Authorization": "Bearer " + token.strip(),
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "IDENTITY_TIMEOUT",
                        "message": "Google identity verification timed out.",
                    }
                }
            ),
            504,
        )
    except requests.RequestException:
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "IDENTITY_UNAVAILABLE",
                        "message": "Google identity verification is unavailable.",
                    }
                }
            ),
            502,
        )

    if response.status_code != 200:
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "INVALID_GOOGLE_TOKEN",
                        "message": "The Google bearer token was rejected.",
                    }
                }
            ),
            401,
        )

    try:
        identity = response.json()
    except ValueError:
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "INVALID_IDENTITY_RESPONSE",
                        "message": "Google returned an invalid identity response.",
                    }
                }
            ),
            502,
        )

    email = str(identity.get("email", "")).strip().lower()
    email_verified = identity.get("email_verified")

    if not email or email_verified is not True:
        return None, (
            jsonify(
                {
                    "error": {
                        "code": "UNVERIFIED_GOOGLE_IDENTITY",
                        "message": "A verified Google email is required.",
                    }
                }
            ),
            403,
        )

    return {"email": email, "sub": identity.get("sub")}, None


@app.route(
    "/scout/api/v1/config",
    methods=["GET"],
)
def scout_config():
    identity, error_response = _verified_google_identity()

    if error_response is not None:
        return error_response

    try:
        operations = _load_scout_registry()
    except ScoutRegistryError:
        app.logger.exception(
            "Scout tenant registry is invalid; failing closed."
        )
        return jsonify(
            {
                "error": {
                    "code": "SCOUT_REGISTRY_INVALID",
                    "message": "Scout configuration is temporarily unavailable.",
                }
            }
        ), 503

    for operation in operations:
        if identity["email"] not in operation["members"]:
            continue

        if operation["status"] != "ACTIVE":
            return jsonify(
                {
                    "status": "ACCESS_DISABLED",
                }
            ), 403

        return jsonify(
            {
                "status": "ACTIVE",
                "operation": {
                    "id": operation["id"],
                    "name": operation["name"],
                    "sheet_id": operation["sheet_id"],
                    "schema_version": operation["schema_version"],
                },
                "features": operation["features"],
            }
        )

    return jsonify(
        {
            "status": "ONBOARDING_REQUIRED",
        }
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "service": "sagewire-api-gateway",
            "status": "ok",
            "version": GATEWAY_VERSION,
        }
    )


@app.route("/services")
def services():
    return jsonify(SERVICES)


@app.route(
    "/tts/speak",
    methods=["POST"],
)
def tts():
    try:
        upstream_response = requests.post(
            SERVICES["tts"] + "/speak",
            json=request.get_json(force=True),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.Timeout:
        return _gateway_error(
            "tts",
            "The upstream service timed out.",
            504,
        )

    except requests.RequestException:
        return _gateway_error(
            "tts",
            "The upstream service could not be reached.",
            502,
        )

    return _proxy_response(
        upstream_response
    )


@app.route(
    "/stt/transcribe",
    methods=["POST"],
)
def stt():
    if "audio" not in request.files:
        return jsonify(
            {
                "error": "audio file required"
            }
        ), 400

    audio = request.files["audio"]

    try:
        upstream_response = requests.post(
            SERVICES["stt"] + "/transcribe",
            files={
                "audio": (
                    audio.filename,
                    audio.stream,
                    audio.content_type,
                )
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.Timeout:
        return _gateway_error(
            "stt",
            "The upstream service timed out.",
            504,
        )

    except requests.RequestException:
        return _gateway_error(
            "stt",
            "The upstream service could not be reached.",
            502,
        )

    return _proxy_response(
        upstream_response
    )


@app.route(
    "/loc/stamp",
    methods=["POST"],
)
def loc():
    try:
        upstream_response = requests.post(
            SERVICES["loc"] + "/stamp",
            json=request.get_json(force=True),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.Timeout:
        return _gateway_error(
            "loc",
            "The upstream service timed out.",
            504,
        )

    except requests.RequestException:
        return _gateway_error(
            "loc",
            "The upstream service could not be reached.",
            502,
        )

    return _proxy_response(
        upstream_response
    )


@app.route(
    "/onboarding",
    methods=["GET"],
)
def onboarding_root():
    return _proxy_request(
        "onboarding",
        "/",
    )


@app.route(
    "/onboarding/health",
    methods=["GET"],
)
def onboarding_health():
    return _proxy_request(
        "onboarding",
        "/health",
    )


@app.route(
    "/onboarding/ready",
    methods=["GET"],
)
def onboarding_ready():
    return _proxy_request(
        "onboarding",
        "/ready",
    )


@app.route(
    "/onboarding/api/v1",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ],
)
def onboarding_api_root():
    return _proxy_request(
        "onboarding",
        "/api/v1",
    )


@app.route(
    "/onboarding/api/v1/<path:upstream_path>",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ],
)
def onboarding_proxy(
    upstream_path,
):
    return _proxy_request(
        "onboarding",
        f"/api/v1/{upstream_path}",
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5010,
    )
