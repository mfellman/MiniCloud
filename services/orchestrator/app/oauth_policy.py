"""
OAuth2 / OIDC access tokens (JWT) and scope checks for workflow runs.

Scopes are plain strings carried in the JWT (typically the standard `scope` claim,
space-separated, and/or `scp` as an array). Configure your IdP (Keycloak, Azure AD,
Auth0, …) to issue these strings to clients or map roles to scopes.

When OAUTH2_ENABLED is false, run_workflow receives granted_scopes=None and skips checks.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

LOG = logging.getLogger("orchestrator.oauth")

OAUTH2_ENABLED = os.environ.get("OAUTH2_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
# When OAuth2 is on: also protect POST /invoke/scheduled with JWT (default true). If false, scheduled uses SCHEDULE_INVOCATION_TOKEN only and scope checks are skipped for that route.
OAUTH2_APPLY_TO_SCHEDULED = os.environ.get("OAUTH2_APPLY_TO_SCHEDULED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
OAUTH2_JWKS_URI = os.environ.get("OAUTH2_JWKS_URI", "").strip()
OAUTH2_ISSUER = os.environ.get("OAUTH2_ISSUER", "").strip()
OAUTH2_AUDIENCE = os.environ.get("OAUTH2_AUDIENCE", "").strip() or None
OAUTH2_SCOPE_PREFIX = os.environ.get("OAUTH2_SCOPE_PREFIX", "minicloud").strip().rstrip(":")

_jwks_client: PyJWKClient | None = None


class OAuthScopeDenied(RuntimeError):
    """Raised when the caller lacks a required scope; mapped to HTTP 403 in main."""


def _jwks() -> PyJWKClient:
    global _jwks_client  # noqa: PLW0603
    if _jwks_client is None:
        if not OAUTH2_JWKS_URI:
            raise RuntimeError("OAUTH2_JWKS_URI is required when OAUTH2_ENABLED")
        _jwks_client = PyJWKClient(OAUTH2_JWKS_URI)
    return _jwks_client


def validate_oauth_config_at_startup() -> None:
    if not OAUTH2_ENABLED:
        return
    if not OAUTH2_JWKS_URI:
        raise RuntimeError("OAUTH2_ENABLED requires OAUTH2_JWKS_URI")
    if not OAUTH2_ISSUER:
        LOG.warning(
            "OAUTH2_ISSUER is empty; JWT issuer verification is disabled (not recommended in production)",
        )


def scopes_from_payload(payload: dict[str, Any]) -> frozenset[str]:
    """Collect scopes from common claim shapes."""
    out: list[str] = []
    sc = payload.get("scope")
    if isinstance(sc, str):
        out.extend(sc.split())
    scp = payload.get("scp")
    if isinstance(scp, list):
        out.extend(str(x) for x in scp)
    # Some providers use permissions as JSON array
    perms = payload.get("permissions")
    if isinstance(perms, list):
        out.extend(str(x) for x in perms)
    return frozenset(s for s in out if s)


def decode_access_token_jwt(bearer_token: str) -> dict[str, Any]:
    """Verify signature (JWKS), optional iss/aud, return claims."""
    try:
        signing_key = _jwks().get_signing_key_from_jwt(bearer_token)
    except Exception as e:
        LOG.warning("JWKS / key resolve failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid access token") from e

    decode_kw: dict[str, Any] = {
        "algorithms": ["RS256", "ES256"],
        "options": {
            "verify_aud": bool(OAUTH2_AUDIENCE),
            "verify_iss": bool(OAUTH2_ISSUER),
        },
    }
    if OAUTH2_AUDIENCE:
        decode_kw["audience"] = OAUTH2_AUDIENCE
    if OAUTH2_ISSUER:
        decode_kw["issuer"] = OAUTH2_ISSUER

    try:
        return jwt.decode(bearer_token, signing_key.key, **decode_kw)
    except jwt.PyJWTError as e:
        LOG.warning("JWT decode failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired access token") from e


def bearer_scopes_from_request(authorization: str | None) -> frozenset[str]:
    """Read Authorization Bearer, validate JWT, return scopes (OAuth2 mode only)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    raw = authorization.removeprefix("Bearer ").strip()
    payload = decode_access_token_jwt(raw)
    return scopes_from_payload(payload)


def scope_allowed(granted: frozenset[str], required: str) -> bool:
    """
    required examples: minicloud:workflow:run:demo, minicloud:egress:http
    Wildcards: minicloud:*, minicloud:egress:*, minicloud:workflow:run:*
    """
    p = OAUTH2_SCOPE_PREFIX
    if f"{p}:*" in granted:
        return True
    if required in granted:
        return True
    parts = required.split(":")
    for i in range(len(parts), 1, -1):
        candidate = ":".join(parts[: i - 1]) + ":*"
        if candidate in granted:
            return True
    return False


def workflow_run_scope(workflow_name: str) -> str:
    return f"{OAUTH2_SCOPE_PREFIX}:workflow:run:{workflow_name}"


def egress_scope(kind: str) -> str:
    return f"{OAUTH2_SCOPE_PREFIX}:egress:{kind}"


def enforce_workflow_invocation(
    granted: frozenset[str] | None,
    workflow_name: str,
) -> None:
    if granted is None:
        return
    req = workflow_run_scope(workflow_name)
    if scope_allowed(granted, req):
        return
    raise OAuthScopeDenied(
        f"Missing scope to run this workflow: {req!r} (or a matching wildcard). "
        f"Granted scopes: {sorted(granted)}",
    )


def enforce_egress(
    granted: frozenset[str] | None,
    kind: str,
    *,
    step_id: str,
) -> None:
    if granted is None:
        return
    req = egress_scope(kind)
    if scope_allowed(granted, req):
        return
    raise OAuthScopeDenied(
        f"Step {step_id!r} requires egress scope {req!r} (or {OAUTH2_SCOPE_PREFIX}:egress:*). "
        f"Granted scopes: {sorted(granted)}",
    )


def enforce_connection_oauth(
    granted: frozenset[str] | None,
    oauth_scope: str | None,
    *,
    step_id: str,
    connection_name: str,
) -> None:
    """If the connection defines oauth_scope, require it in addition to egress:*."""
    if granted is None or not oauth_scope:
        return
    if scope_allowed(granted, oauth_scope):
        return
    raise OAuthScopeDenied(
        f"Step {step_id!r} connection {connection_name!r} requires scope {oauth_scope!r} "
        f"(or a matching wildcard). Granted scopes: {sorted(granted)}",
    )
