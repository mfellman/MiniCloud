from __future__ import annotations

from starlette.testclient import TestClient

from tests.conftest import load_fastapi_app


def test_storage_health_and_rw(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    app = load_fastapi_app("storage")
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/readyz").json() == {"status": "ready"}

        w = c.put(
            "/v1/storage/demo/path/to/key",
            json={"value": "abc", "content_type": "text/plain"},
        )
        assert w.status_code == 200
        assert w.json()["status"] == "stored"

        r = c.get("/v1/storage/demo/path/to/key")
        assert r.status_code == 200
        assert r.json()["value"] == "abc"


def test_storage_runtime_internal_docs(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    app = load_fastapi_app("storage")
    with TestClient(app) as c:
        wf = c.put(
            "/internal/workflows/wf-a",
            json={"document": {"name": "wf-a", "steps": []}},
        )
        assert wf.status_code == 200

        conn = c.put(
            "/internal/connections/c-http",
            json={"document": {"name": "c-http", "type": "http", "base_url": "https://example.org"}},
        )
        assert conn.status_code == 200

        wf_list = c.get("/internal/workflows")
        assert wf_list.status_code == 200
        assert wf_list.json()["workflows"][0]["name"] == "wf-a"

        conn_list = c.get("/internal/connections")
        assert conn_list.status_code == 200
        assert conn_list.json()["connections"][0]["name"] == "c-http"


def test_storage_acl_allows_and_denies_by_role(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_ACL_ENABLED", "true")
    monkeypatch.setenv(
        "STORAGE_ACL_POLICY",
        '{"default":{"read_roles":["admin"],"write_roles":["admin"]},"buckets":{"demo":{"read_roles":["reader","writer"],"write_roles":["writer"]}}}',
    )
    app = load_fastapi_app("storage")

    with TestClient(app) as c:
        denied_write = c.put(
            "/v1/storage/demo/a/key",
            json={"value": "x", "content_type": "text/plain"},
            headers={"X-Storage-Roles": "reader"},
        )
        assert denied_write.status_code == 403

        allowed_write = c.put(
            "/v1/storage/demo/a/key",
            json={"value": "x", "content_type": "text/plain"},
            headers={"X-Storage-Roles": "writer"},
        )
        assert allowed_write.status_code == 200

        allowed_read = c.get(
            "/v1/storage/demo/a/key",
            headers={"X-Storage-Roles": "reader"},
        )
        assert allowed_read.status_code == 200
        assert allowed_read.json()["value"] == "x"


def test_storage_acl_default_role_is_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_ACL_ENABLED", "true")
    monkeypatch.setenv("STORAGE_DEFAULT_ROLE", "orchestrator")
    monkeypatch.setenv(
        "STORAGE_ACL_POLICY",
        '{"default":{"read_roles":["admin"],"write_roles":["admin"]},"buckets":{"runtime":{"read_roles":["orchestrator"],"write_roles":["orchestrator"]}}}',
    )
    app = load_fastapi_app("storage")

    with TestClient(app) as c:
        w = c.put(
            "/v1/storage/runtime/key1",
            json={"value": "ok", "content_type": "text/plain"},
        )
        assert w.status_code == 200
        r = c.get("/v1/storage/runtime/key1")
        assert r.status_code == 200


def test_storage_acl_simple_env_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_ACL_ENABLED", "true")
    monkeypatch.setenv("STORAGE_DEFAULT_ROLE", "")
    monkeypatch.setenv("STORAGE_ACL_READ_ROLES", "reader, writer")
    monkeypatch.setenv("STORAGE_ACL_WRITE_ROLES", "writer")
    monkeypatch.delenv("STORAGE_ACL_POLICY", raising=False)
    monkeypatch.delenv("STORAGE_ACL_BUCKET_OVERRIDES", raising=False)
    app = load_fastapi_app("storage")

    with TestClient(app) as c:
        denied = c.put(
            "/v1/storage/demo/env/key",
            json={"value": "v", "content_type": "text/plain"},
            headers={"X-Storage-Roles": "reader"},
        )
        assert denied.status_code == 403

        ok_write = c.put(
            "/v1/storage/demo/env/key",
            json={"value": "v", "content_type": "text/plain"},
            headers={"X-Storage-Roles": "writer"},
        )
        assert ok_write.status_code == 200

        ok_read = c.get(
            "/v1/storage/demo/env/key",
            headers={"X-Storage-Roles": "reader"},
        )
        assert ok_read.status_code == 200


def test_storage_acl_secure_default_denies_unknown_role(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_ACL_ENABLED", "true")
    monkeypatch.setenv("STORAGE_DEFAULT_ROLE", "")
    monkeypatch.setenv("STORAGE_ACL_READ_ROLES", "orchestrator")
    monkeypatch.setenv("STORAGE_ACL_WRITE_ROLES", "orchestrator")
    monkeypatch.delenv("STORAGE_ACL_POLICY", raising=False)
    monkeypatch.delenv("STORAGE_ACL_BUCKET_OVERRIDES", raising=False)
    app = load_fastapi_app("storage")

    with TestClient(app) as c:
        denied_no_roles = c.put(
            "/v1/storage/demo/secure/key",
            json={"value": "v", "content_type": "text/plain"},
        )
        assert denied_no_roles.status_code == 403

        denied_unknown = c.put(
            "/v1/storage/demo/secure/key",
            json={"value": "v", "content_type": "text/plain"},
            headers={"X-Storage-Roles": "reader"},
        )
        assert denied_unknown.status_code == 403


def test_storage_write_publishes_event_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_EVENT_ENABLED", "true")
    app = load_fastapi_app("storage")

    import importlib

    sm = importlib.import_module("app.main")

    captured: list[dict] = []

    def _fake_publish(payload: dict[str, object]) -> bool:
        captured.append(dict(payload))
        return True

    monkeypatch.setattr(sm, "_publish_storage_event", _fake_publish)

    with TestClient(app) as c:
        w = c.put(
            "/v1/storage/demo/publish/key",
            json={"value": "abc", "content_type": "text/plain"},
        )

    assert w.status_code == 200
    assert w.json()["event_published"] is True
    assert len(captured) == 1
    assert captured[0]["bucket"] == "demo"
    assert captured[0]["key"] == "publish/key"


def test_storage_write_returns_502_when_event_required_and_publish_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_EVENT_ENABLED", "true")
    monkeypatch.setenv("STORAGE_EVENT_REQUIRED", "true")
    app = load_fastapi_app("storage")

    import importlib

    sm = importlib.import_module("app.main")

    def _fake_publish(_payload: dict[str, object]) -> bool:
        raise RuntimeError("failed to publish storage event: boom")

    monkeypatch.setattr(sm, "_publish_storage_event", _fake_publish)

    with TestClient(app) as c:
        w = c.put(
            "/v1/storage/demo/publish/key2",
            json={"value": "abc", "content_type": "text/plain"},
        )

    assert w.status_code == 502
    assert "failed to publish storage event" in w.json()["detail"]


def test_storage_lists_buckets_and_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    app = load_fastapi_app("storage")

    with TestClient(app) as c:
        c.put("/v1/storage/demo/a/one", json={"value": "1", "content_type": "text/plain"})
        c.put("/v1/storage/demo/a/two", json={"value": "2", "content_type": "text/plain"})
        c.put("/v1/storage/events/x", json={"value": "3", "content_type": "text/plain"})

        buckets = c.get("/v1/storage")
        assert buckets.status_code == 200
        assert buckets.json()["buckets"] == ["demo", "events"]

        keys = c.get("/v1/storage/demo", params={"prefix": "a/"})
        assert keys.status_code == 200
        assert keys.json()["keys"] == ["a/one", "a/two"]
