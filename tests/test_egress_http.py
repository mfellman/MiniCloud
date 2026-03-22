"""Egress HTTP: health + /call met gemockte downstream."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from starlette.testclient import TestClient


def test_healthz(egress_http_app):
    c = TestClient(egress_http_app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}


def test_call_get_mocked(egress_http_app, monkeypatch):
    monkeypatch.delenv("HTTP_EGRESS_ALLOWED_HOSTS", raising=False)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.content = b"hello"
    mock_resp.text = "hello"
    mock_resp.headers = httpx.Headers({"content-type": "text/plain"})

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", return_value=mock_client):
        c = TestClient(egress_http_app)
        r = c.post(
            "/call",
            json={
                "method": "GET",
                "url": "https://example.com/x",
                "headers": {},
                "timeout_seconds": 30,
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status_code"] == 200
    assert data["body"] == "hello"
