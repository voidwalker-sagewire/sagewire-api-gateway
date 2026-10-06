import json

import pytest
import requests

import server


DCC_EMAIL = "dcc@example.com"
TEST_EMAIL = "test@example.com"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    registry = {
        "operations": [
            {
                "id": "dcc",
                "name": "DCC",
                "sheet_id": "dcc-sheet",
                "schema_version": "1",
                "status": "ACTIVE",
                "members": [DCC_EMAIL],
                "features": {
                    "field_head_count": True,
                    "animal_lookup": True,
                    "bovine_beacon": False,
                },
            },
            {
                "id": "test-ranch",
                "name": "Test Ranch",
                "sheet_id": "test-sheet",
                "schema_version": "1",
                "status": "ACTIVE",
                "members": [{"email": TEST_EMAIL, "enabled": True}],
                "features": {
                    "field_head_count": True,
                    "animal_lookup": False,
                    "bovine_beacon": False,
                },
            },
        ]
    }
    monkeypatch.setenv(
        "SAGEWIRE_SCOUT_TENANTS_JSON",
        json.dumps(registry),
    )
    server.app.config.update(TESTING=True)
    return server.app.test_client()


def authorize(monkeypatch, email, verified=True, status=200):
    def fake_get(url, headers, timeout):
        assert url == server.GOOGLE_USERINFO_URL
        assert headers["Authorization"].startswith("Bearer ")
        return FakeResponse(
            status,
            {
                "sub": "google-subject",
                "email": email,
                "email_verified": verified,
            },
        )

    monkeypatch.setattr(server.requests, "get", fake_get)


def test_requires_bearer_token(client):
    response = client.get("/scout/api/v1/config")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_rejects_invalid_google_token(client, monkeypatch):
    authorize(monkeypatch, DCC_EMAIL, status=401)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer rejected"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "INVALID_GOOGLE_TOKEN"


def test_rejects_unverified_google_identity(client, monkeypatch):
    authorize(monkeypatch, DCC_EMAIL, verified=False)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "UNVERIFIED_GOOGLE_IDENTITY"


def test_dcc_identity_resolves_only_dcc(client, monkeypatch):
    authorize(monkeypatch, DCC_EMAIL)

    response = client.get(
        "/scout/api/v1/config?operation=test-ranch&sheet_id=test-sheet",
        headers={"Authorization": "Bearer token"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ACTIVE"
    assert payload["operation"]["id"] == "dcc"
    assert payload["operation"]["sheet_id"] == "dcc-sheet"
    assert payload["features"]["animal_lookup"] is True
    assert payload["features"]["bovine_beacon"] is False


def test_test_identity_resolves_only_test_operation(client, monkeypatch):
    authorize(monkeypatch, TEST_EMAIL)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["operation"]["id"] == "test-ranch"
    assert payload["operation"]["sheet_id"] == "test-sheet"
    assert payload["features"]["animal_lookup"] is False


def test_unknown_identity_requires_onboarding(client, monkeypatch):
    authorize(monkeypatch, "unknown@example.com")

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ONBOARDING_REQUIRED"}


def test_duplicate_membership_fails_closed(client, monkeypatch):
    registry = {
        "operations": [
            {
                "id": "one",
                "name": "One",
                "sheet_id": "sheet-one",
                "members": [DCC_EMAIL],
            },
            {
                "id": "two",
                "name": "Two",
                "sheet_id": "sheet-two",
                "members": [DCC_EMAIL],
            },
        ]
    }
    monkeypatch.setenv(
        "SAGEWIRE_SCOUT_TENANTS_JSON",
        json.dumps(registry),
    )
    authorize(monkeypatch, DCC_EMAIL)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "SCOUT_REGISTRY_INVALID"


def test_disabled_operation_is_denied(client, monkeypatch):
    registry = {
        "operations": [
            {
                "id": "dcc",
                "name": "DCC",
                "sheet_id": "dcc-sheet",
                "status": "DISABLED",
                "members": [DCC_EMAIL],
            }
        ]
    }
    monkeypatch.setenv(
        "SAGEWIRE_SCOUT_TENANTS_JSON",
        json.dumps(registry),
    )
    authorize(monkeypatch, DCC_EMAIL)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 403
    assert response.get_json() == {"status": "ACCESS_DISABLED"}


def test_google_timeout_returns_gateway_timeout(client, monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(server.requests, "get", timeout)

    response = client.get(
        "/scout/api/v1/config",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 504
    assert response.get_json()["error"]["code"] == "IDENTITY_TIMEOUT"
