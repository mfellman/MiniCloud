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
