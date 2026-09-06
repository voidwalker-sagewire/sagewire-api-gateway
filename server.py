import os
from urllib.parse import urljoin

from flask import Flask, Response, jsonify, request
import requests

app = Flask(__name__)

GATEWAY_VERSION = "1.1.0"
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("SAGEWIRE_GATEWAY_TIMEOUT", "20")
)

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
