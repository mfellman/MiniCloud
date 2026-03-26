from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import load_fastapi_app


def _login(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()


def test_identity_default_users_and_scope_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_DATA_DIR", str(tmp_path / "identity-data"))
    app = load_fastapi_app("identity")

    with TestClient(app) as c:
        admin = _login(c, "admin", "admin")
        operator = _login(c, "operator", "operator")
        viewer = _login(c, "viewer", "viewer")

    assert "minicloud:*" in admin["scopes"]
    assert "minicloud:workflow:run:*" in operator["scopes"]
    assert "minicloud:workflow:retrigger:*" in operator["scopes"]
    assert viewer["scopes"] == []


def test_identity_admin_can_set_user_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_DATA_DIR", str(tmp_path / "identity-data"))
    app = load_fastapi_app("identity")

    with TestClient(app) as c:
        admin = _login(c, "admin", "admin")
        token = admin["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        create = c.post(
            "/permissions",
            json={"name": "minicloud:workflow:run:minimal", "description": "Run minimal"},
            headers=headers,
        )
        assert create.status_code in (200, 409)

        set_resp = c.put(
            "/users/viewer/permissions",
            json={"permissions": ["minicloud:workflow:run:minimal"]},
            headers=headers,
        )
        assert set_resp.status_code == 200
        assert "minicloud:workflow:run:minimal" in set_resp.json()

        viewer = _login(c, "viewer", "viewer")
        assert "minicloud:workflow:run:minimal" in viewer["scopes"]
