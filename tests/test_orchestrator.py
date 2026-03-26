"""Orchestrator: health + geladen workflows (startup)."""
from __future__ import annotations

from starlette.testclient import TestClient


def test_healthz_readyz(orchestrator_app):
    with TestClient(orchestrator_app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.get("/readyz").json()
    assert r["status"] == "ready"
    assert r["workflows"] >= 1
    assert r.get("connections", 0) >= 1


def test_list_workflows(orchestrator_app):
    with TestClient(orchestrator_app) as c:
        data = c.get("/workflows").json()
    names = {w["name"] for w in data["workflows"]}
    assert "minimal" in names


def test_list_workflows_includes_storage_changed_trigger(orchestrator_app):
    with TestClient(orchestrator_app) as c:
        data = c.get("/workflows").json()
    names = {w["name"] for w in data["workflows"]}
    assert "storage_changed_trigger" in names


def test_admin_reload_refreshes_workflows(orchestrator_app, monkeypatch):
    import app.main as om

    with TestClient(orchestrator_app) as c:
        before = {w["name"] for w in c.get("/workflows").json()["workflows"]}
        assert "minimal" in before

        base = om._WORKFLOWS["minimal"].model_dump(mode="python")
        base["name"] = "minimal_reloaded"
        reloaded = om.WorkflowDoc.model_validate(base)

        class _FakeStore:
            def load_workflows(self):
                return {"minimal_reloaded": reloaded}

            def load_connections(self):
                return dict(om._CONNECTIONS)

        monkeypatch.setattr(om, "_RUNTIME_STORE", _FakeStore())
        rr = c.post("/admin/reload")
        assert rr.status_code == 200
        assert rr.json()["status"] == "reloaded"

        after = {w["name"] for w in c.get("/workflows").json()["workflows"]}

    assert "minimal_reloaded" in after
    assert "minimal" not in after


async def _fake_run_workflow(*args, **kwargs):
    return "ok", {}, [], {}


def test_http_invocation_token_when_set(orchestrator_app, monkeypatch):
    import app.main as om

    monkeypatch.setattr(om, "HTTP_INVOCATION_TOKEN", "http-secret")
    monkeypatch.setattr(om, "run_workflow", _fake_run_workflow)
    with TestClient(orchestrator_app) as c:
        r = c.post(
            "/run/minimal",
            json={"xml": '<?xml version="1.0"?><doc><item/></doc>'},
        )
        assert r.status_code == 401
        r2 = c.post(
            "/run/minimal",
            json={"xml": '<?xml version="1.0"?><doc><item/></doc>'},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r2.status_code == 403
        r3 = c.post(
            "/run/minimal",
            json={"xml": '<?xml version="1.0"?><doc><item/></doc>'},
            headers={"Authorization": "Bearer http-secret"},
        )
        assert r3.status_code == 200
        assert r3.text == "ok"


def test_resolve_trigger_workflow_storage_changed_route(orchestrator_app, monkeypatch):
    import app.main as om

    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW", "storage_demo")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW", "demo,*-events")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW", "payloads/*")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_WORKFLOW", "")

    with om._RELOAD_LOCK:
        om._WORKFLOWS = {"storage_demo": om._WORKFLOWS.get("minimal")}

    wf = om._resolve_trigger_workflow(
        {
            "Domain": "Storage",
            "Service": "KV",
            "Action": "Updated",
            "Version": "1",
            "Bucket": "demo",
            "Key": "payloads/last",
        },
    )
    assert wf == "storage_demo"


def test_resolve_trigger_workflow_storage_changed_filter_miss(orchestrator_app, monkeypatch):
    import app.main as om

    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW", "storage_demo")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW", "demo")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW", "payloads/*")
    monkeypatch.setattr(om, "RABBITMQ_TRIGGER_WORKFLOW", "")

    wf = om._resolve_trigger_workflow(
        {
            "Domain": "Storage",
            "Service": "KV",
            "Action": "Updated",
            "Version": "1",
            "Bucket": "other",
            "Key": "payloads/last",
        },
    )
    assert wf is None
