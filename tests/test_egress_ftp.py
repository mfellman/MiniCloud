"""Egress FTP: smoke (health). No real FTP server required."""
from __future__ import annotations

from starlette.testclient import TestClient


def test_healthz(egress_ftp_app):
    c = TestClient(egress_ftp_app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}
