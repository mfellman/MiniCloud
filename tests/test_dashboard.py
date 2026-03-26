from __future__ import annotations

import base64
import hashlib
import importlib
import pytest

from fastapi.testclient import TestClient

@pytest.fixture(autouse=True)
def mock_identity(dashboard_app, monkeypatch):
    """Bypass the identity middleware for tests so they don't fail with 401."""
    dashboard_main = importlib.import_module("app.main")

    async def fake_identity(method, path, **kwargs):
        if path == "/auth/me":
            return {"username": "admin", "scopes": ["minicloud:*"], "groups": []}
        return {}

    monkeypatch.setattr(dashboard_main, "_identity_request", fake_identity)
    monkeypatch.setattr(dashboard_main, "_extract_identity_token", lambda r: "test-token")


def test_rabbitmq_status_reports_disabled_by_default(dashboard_app):
    client = TestClient(dashboard_app)
    r = client.get("/api/rabbitmq/status")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False


def test_rabbitmq_overview_returns_503_when_disabled(dashboard_app):
    client = TestClient(dashboard_app)
    r = client.get("/api/rabbitmq/overview")
    assert r.status_code == 503


def test_rabbitmq_overview_proxy_when_enabled(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")

    async def fake_get(path: str, params: dict | None = None):
        assert path == "/overview"
        return {"rabbitmq_version": "3.13"}

    monkeypatch.setattr(dashboard_main, "RABBITMQ_INSPECT_ENABLED", True)
    monkeypatch.setattr(dashboard_main, "_rabbitmq_get", fake_get)

    client = TestClient(dashboard_app)
    r = client.get("/api/rabbitmq/overview")
    assert r.status_code == 200
    assert r.json()["rabbitmq_version"] == "3.13"


def test_rabbitmq_peek_uses_safe_requeue_payload(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")

    captured: dict = {}

    async def fake_post(path: str, payload: dict):
        captured["path"] = path
        captured["payload"] = payload
        return [{"payload": "hello", "payload_bytes": 5}]

    monkeypatch.setattr(dashboard_main, "RABBITMQ_INSPECT_ENABLED", True)
    monkeypatch.setattr(dashboard_main, "RABBITMQ_MANAGEMENT_VHOST", "/")
    monkeypatch.setattr(dashboard_main, "_rabbitmq_post", fake_post)

    client = TestClient(dashboard_app)
    r = client.get("/api/rabbitmq/messages/peek", params={"queue": "orders.queue", "count": 5})
    assert r.status_code == 200
    assert r.json()[0]["payload"] == "hello"

    assert captured["path"] == "/queues/%2F/orders.queue/get"
    assert captured["payload"]["ackmode"] == "ack_requeue_true"
    assert captured["payload"]["requeue"] is True
    assert captured["payload"]["count"] == 5


def test_dashboard_basic_auth_when_enabled(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_USERNAME", "admin")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_PASSWORD", "secret")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_PASSWORD_SHA256", "")

    client = TestClient(dashboard_app)
    unauth = client.get("/")
    assert unauth.status_code == 401

    token = base64.b64encode(b"admin:secret").decode("ascii")
    auth = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert auth.status_code == 200


def test_storage_proxy_buckets(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")

    async def fake_storage_get(path: str, params: dict | None = None):
        assert path == "/v1/storage"
        return {"buckets": ["demo", "events"]}

    monkeypatch.setattr(dashboard_main, "_storage_get", fake_storage_get)

    client = TestClient(dashboard_app)
    r = client.get("/api/storage/buckets")
    assert r.status_code == 200
    assert r.json()["buckets"] == ["demo", "events"]


def test_dashboard_basic_auth_with_sha256_hash(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_USERNAME", "admin")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_PASSWORD", "")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_PASSWORD_SHA256", hashlib.sha256(b"secret").hexdigest())

    client = TestClient(dashboard_app)
    token = base64.b64encode(b"admin:secret").decode("ascii")
    r = client.get("/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200


def test_dashboard_sets_security_headers(dashboard_app):
    client = TestClient(dashboard_app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_auth_session_endpoint(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dashboard_main, "DASH_AUTH_USERNAME", "admin")

    client = TestClient(dashboard_app)
    r = client.get("/auth/session")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is True
    assert r.json()["username"] == "admin"


def test_auth_logout_returns_401_challenge(dashboard_app):
    client = TestClient(dashboard_app)
    r = client.get("/auth/logout", params={"nonce": "123"})
    assert r.status_code == 401
    assert "MiniCloud Dashboard Logout 123" in r.headers["www-authenticate"]


def test_workflow_run_proxy(dashboard_app, monkeypatch):
    dashboard_main = importlib.import_module("app.main")

    async def fake_require_scope(_request, _scope: str):
        return {"username": "tester", "_token": "token-123"}

    async def fake_run(workflow_name: str, payload: dict[str, object], bearer_token: str = ""):
        assert workflow_name == "minimal"
        assert payload == {"xml": "<root/>"}
        assert bearer_token == "token-123"
        return {
            "status": "ok",
            "workflow": workflow_name,
            "request_id": "rid-123",
            "output": "<ok/>",
            "content_type": "application/xml",
        }

    monkeypatch.setattr(dashboard_main, "_require_scope", fake_require_scope)
    monkeypatch.setattr(dashboard_main, "_proxy_run_workflow", fake_run)

    client = TestClient(dashboard_app)
    r = client.post("/api/run/minimal", json={"xml": "<root/>"})
    assert r.status_code == 200
    assert r.json()["request_id"] == "rid-123"
