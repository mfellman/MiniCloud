"""Orchestrator: health + geladen workflows (startup)."""
from __future__ import annotations

from starlette.testclient import TestClient


def test_healthz_readyz(orchestrator_app):
    with TestClient(orchestrator_app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.get("/readyz").json()
    assert r["status"] == "ready"
    assert r["workflows"] >= 1


def test_list_workflows(orchestrator_app):
    with TestClient(orchestrator_app) as c:
        data = c.get("/workflows").json()
    names = {w["name"] for w in data["workflows"]}
    assert "minimal" in names
