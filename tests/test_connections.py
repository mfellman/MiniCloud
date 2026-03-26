"""Unit tests: connections.resolve_http_url + load_connections."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import REPO_ROOT, load_fastapi_app


def test_resolve_http_url_paths():
    load_fastapi_app("orchestrator")
    from app.connections import resolve_http_url

    assert (
        resolve_http_url(
            base_url="https://httpbin.org",
            path_or_url=None,
            path="/post",
        )
        == "https://httpbin.org/post"
    )
    assert (
        resolve_http_url(
            base_url="https://httpbin.org/",
            path_or_url="get",
            path=None,
        )
        == "https://httpbin.org/get"
    )
    assert (
        resolve_http_url(
            base_url="https://a.example",
            path_or_url="https://other.example/x",
            path=None,
        )
        == "https://other.example/x"
    )


def test_load_connections_example():
    load_fastapi_app("orchestrator")
    from app.connections import load_connections

    d = REPO_ROOT / "services" / "orchestrator" / "connections"
    reg = load_connections(d)
    assert "httpbin_example" in reg
    assert getattr(reg["httpbin_example"], "base_url") == "https://httpbin.org"
    assert "rabbitmq_events" in reg
    assert getattr(reg["rabbitmq_events"], "exchange") == "minicloud.events"
