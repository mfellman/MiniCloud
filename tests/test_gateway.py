"""Gateway: health, /v1/status aggregate, /v1/transform with mocked upstream."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from starlette.testclient import TestClient


def test_healthz_readyz(gateway_app):
    c = TestClient(gateway_app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}


def test_v1_transform_forwards_to_transformers(gateway_app, monkeypatch):
    monkeypatch.setenv("TRANSFORMERS_URL", "http://upstream-transformers:8080")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.content = b"<result/>"
    mock_resp.text = "<result/>"
    mock_resp.headers = httpx.Headers({"content-type": "application/xml; charset=utf-8"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", return_value=mock_client):
        c = TestClient(gateway_app)
        r = c.post(
            "/v1/transform",
            json={"xml": "<a/>", "xslt": "<?xml version='1.0'?><xsl:stylesheet version='1.0' xmlns:xsl='http://www.w3.org/1999/XSL/Transform'><xsl:template match='/'><x/></xsl:template></xsl:stylesheet>"},
        )
    assert r.status_code == 200
    assert r.content == b"<result/>"
    mock_client.post.assert_called_once()
    call_kw = mock_client.post.call_args
    assert "/applyXSLT" in str(call_kw[0][0])


def test_v1_status_aggregate(gateway_app, monkeypatch):
    import app.main as gw

    monkeypatch.setattr(gw, "TRANSFORMERS_BASE_URL", "http://tf:8080")
    monkeypatch.setattr(gw, "ORCHESTRATOR_URL", "http://orch:8080")
    monkeypatch.setattr(gw, "EGRESS_HTTP_URL", "")
    monkeypatch.setattr(gw, "EGRESS_FTP_URL", "")
    monkeypatch.setattr(gw, "EGRESS_SSH_URL", "")

    async def mock_get(url: str, **kwargs):
        return httpx.Response(200, json={"status": "ready"})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", return_value=mock_client):
        c = TestClient(gateway_app)
        data = c.get("/v1/status").json()

    assert data["overall_ok"] is True
    assert data["gateway"]["ok"] is True
    assert data["services"]["transformers"]["ok"] is True
    assert data["services"]["orchestrator"]["ok"] is True
    assert data["services"]["egress_http"]["skipped"] is True
    assert data["tests"]["suite"] == "pytest"
    assert "hint" in data["tests"]
    assert mock_client.get.call_count == 2


def test_orchestration_only_hides_transform_and_status(gateway_app, monkeypatch):
    import app.main as gw

    monkeypatch.setattr(gw, "GATEWAY_ORCHESTRATION_ONLY", True)
    c = TestClient(gateway_app)
    r = c.post(
        "/v1/transform",
        json={"xml": "<a/>", "xslt": "<xsl:stylesheet version='1.0' xmlns:xsl='http://www.w3.org/1999/XSL/Transform'/>"},
    )
    assert r.status_code == 404
    assert c.get("/v1/status").status_code == 404


def test_v1_run_forwards_authorization_header(gateway_app, monkeypatch):
    import app.main as gw

    monkeypatch.setattr(gw, "ORCHESTRATOR_URL", "http://orch:8080")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.content = b"<ok/>"
    mock_resp.text = "<ok/>"
    mock_resp.headers = httpx.Headers({"content-type": "application/xml; charset=utf-8"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.httpx.AsyncClient", return_value=mock_client):
        c = TestClient(gateway_app)
        c.post(
            "/v1/run/minimal",
            json={"xml": "<r/>"},
            headers={"Authorization": "Bearer tok"},
        )

    sent_headers = mock_client.post.call_args.kwargs.get("headers", {})
    assert sent_headers.get("Authorization") == "Bearer tok"


def test_v1_run_without_orchestrator_returns_503(gateway_app, monkeypatch):
    import app.main as gw

    monkeypatch.setattr(gw, "ORCHESTRATOR_URL", "")
    c = TestClient(gateway_app)
    r = c.post(
        "/v1/run/minimal",
        json={"xml": "<r/>"},
    )
    assert r.status_code == 503
