"""Egress SSH/SFTP: smoke (health). Exec/SFTP need a real server or heavy mocks."""
from __future__ import annotations

from starlette.testclient import TestClient


def test_healthz(egress_ssh_app):
    c = TestClient(egress_ssh_app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}
