"""Unit tests for OAuth scope matching (orchestrator app on path)."""
from __future__ import annotations

import pytest

from tests.conftest import load_fastapi_app


def _orch_oauth():
    load_fastapi_app("orchestrator")
    from app import oauth_policy

    return oauth_policy


@pytest.mark.parametrize(
    ("granted", "required", "expect"),
    [
        (frozenset(), "minicloud:workflow:run:x", False),
        (
            frozenset({"minicloud:workflow:run:x"}),
            "minicloud:workflow:run:x",
            True,
        ),
        (
            frozenset({"minicloud:workflow:run:*"}),
            "minicloud:workflow:run:demo",
            True,
        ),
        (
            frozenset({"minicloud:egress:*"}),
            "minicloud:egress:http",
            True,
        ),
        (
            frozenset({"minicloud:*"}),
            "minicloud:egress:http",
            True,
        ),
    ],
)
def test_scope_allowed(granted, required, expect):
    op = _orch_oauth()
    assert op.scope_allowed(granted, required) is expect


def test_workflow_and_egress_scope_strings():
    op = _orch_oauth()
    assert op.workflow_run_scope("demo") == "minicloud:workflow:run:demo"
    assert op.egress_scope("http") == "minicloud:egress:http"


def test_enforce_connection_oauth_skips_when_oauth_disabled():
    op = _orch_oauth()
    op.enforce_connection_oauth(
        None,
        "minicloud:connection:secret",
        step_id="s1",
        connection_name="c1",
    )


def test_enforce_connection_oauth_raises_when_missing_scope():
    op = _orch_oauth()
    from app.oauth_policy import OAuthScopeDenied

    with pytest.raises(OAuthScopeDenied):
        op.enforce_connection_oauth(
            frozenset({"minicloud:egress:http"}),
            "minicloud:connection:secret",
            step_id="s1",
            connection_name="c1",
        )


def test_enforce_connection_oauth_ok_when_scope_present():
    op = _orch_oauth()
    op.enforce_connection_oauth(
        frozenset({"minicloud:egress:http", "minicloud:connection:secret"}),
        "minicloud:connection:secret",
        step_id="s1",
        connection_name="c1",
    )
