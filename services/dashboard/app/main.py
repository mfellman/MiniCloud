"""MiniCloud Dashboard — lightweight FastAPI backend.

Proxies workflow + trace data from the orchestrator and serves the
single-page dashboard frontend.
"""
import base64
import binascii
import hashlib
import hmac
import logging
import os
from pathlib import Path
from urllib.parse import quote

from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

LOG = logging.getLogger("dashboard")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8083").rstrip("/")
SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "http://localhost:8089").rstrip("/")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080").rstrip("/")
GATEWAY_RUN_BEARER_TOKEN = os.environ.get("GATEWAY_RUN_BEARER_TOKEN", "").strip()
STORAGE_SERVICE_URL = os.environ.get("STORAGE_SERVICE_URL", "http://localhost:8086").rstrip("/")
STORAGE_READ_TOKEN = os.environ.get("STORAGE_READ_TOKEN", "").strip()
STORAGE_ROLES = os.environ.get("STORAGE_ROLES", "orchestrator").strip()
IDENTITY_URL = os.environ.get("IDENTITY_URL", "http://localhost:8088").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("DASH_TIMEOUT_SECONDS", "30"))
DASH_AUTH_ENABLED = os.environ.get("DASH_AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
DASH_AUTH_USERNAME = os.environ.get("DASH_AUTH_USERNAME", "").strip()
DASH_AUTH_PASSWORD = os.environ.get("DASH_AUTH_PASSWORD", "")
DASH_AUTH_PASSWORD_SHA256 = os.environ.get("DASH_AUTH_PASSWORD_SHA256", "").strip().lower()
RABBITMQ_INSPECT_ENABLED = os.environ.get("DASH_RABBITMQ_INSPECT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
RABBITMQ_MANAGEMENT_URL = os.environ.get("RABBITMQ_MANAGEMENT_URL", "http://localhost:15672/api").rstrip("/")
RABBITMQ_MANAGEMENT_USER = os.environ.get("RABBITMQ_MANAGEMENT_USER", "")
RABBITMQ_MANAGEMENT_PASSWORD = os.environ.get("RABBITMQ_MANAGEMENT_PASSWORD", "")
RABBITMQ_MANAGEMENT_VHOST = os.environ.get("RABBITMQ_MANAGEMENT_VHOST", "/")
IDENTITY_COOKIE_NAME = "mc_identity_token"

app = FastAPI(title="MiniCloud Dashboard", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Hash of CSS+JS content — used as cache-busting query param."""
    h = hashlib.md5(usedforsecurity=False)
    for name in ("style.css", "app.js"):
        p = STATIC_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:8]

_ASSET_VERSION = _asset_version()


def _basic_auth_unauthorized() -> Response:
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={
            "WWW-Authenticate": 'Basic realm="MiniCloud Dashboard"',
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        },
    )


def _password_matches(password: str) -> bool:
    if DASH_AUTH_PASSWORD_SHA256:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest().lower()
        return hmac.compare_digest(digest, DASH_AUTH_PASSWORD_SHA256)
    return hmac.compare_digest(password, DASH_AUTH_PASSWORD)


@app.middleware("http")
async def _dashboard_auth_middleware(request: Request, call_next):
    if request.url.path in {"/healthz", "/auth/session", "/auth/logout"}:
        return await call_next(request)
    if not DASH_AUTH_ENABLED:
        return await call_next(request)
    if not DASH_AUTH_USERNAME or (not DASH_AUTH_PASSWORD and not DASH_AUTH_PASSWORD_SHA256):
        return _basic_auth_unauthorized()

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return _basic_auth_unauthorized()

    b64 = auth_header.removeprefix("Basic ").strip()
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return _basic_auth_unauthorized()

    if ":" not in decoded:
        return _basic_auth_unauthorized()
    username, password = decoded.split(":", 1)
    if not hmac.compare_digest(username, DASH_AUTH_USERNAME):
        return _basic_auth_unauthorized()
    if not _password_matches(password):
        return _basic_auth_unauthorized()

    return await call_next(request)


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.middleware("http")
async def _identity_api_auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in {"/api/auth/login", "/api/auth/logout"}:
        return await call_next(request)

    token = _extract_identity_token(request)
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    try:
        await _identity_request("GET", "/auth/me", token=token)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": "Not authenticated"})

    return await call_next(request)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/auth/session")
def auth_session() -> dict[str, Any]:
    return {
        "auth_enabled": DASH_AUTH_ENABLED,
        "username": DASH_AUTH_USERNAME if DASH_AUTH_ENABLED and DASH_AUTH_USERNAME else None,
    }


@app.get("/auth/logout")
def auth_logout(nonce: str | None = None) -> Response:
    realm_suffix = f" {nonce}" if nonce else ""
    return Response(
        content="Signed out",
        status_code=401,
        headers={
            "WWW-Authenticate": f'Basic realm="MiniCloud Dashboard Logout{realm_suffix}"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        },
    )


class IdentityLoginBody(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


@app.post("/api/auth/login")
async def api_auth_login(body: IdentityLoginBody) -> JSONResponse:
    token_resp = await _identity_request(
        "POST",
        "/auth/login",
        json_body={"username": body.username, "password": body.password},
    )
    response = JSONResponse(
        content={
            "username": token_resp.get("username", ""),
            "groups": token_resp.get("groups", []),
            "scopes": token_resp.get("scopes", []),
        },
    )
    response.set_cookie(
        key=IDENTITY_COOKIE_NAME,
        value=str(token_resp.get("access_token", "")),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=int(token_resp.get("expires_in", 3600)),
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout() -> JSONResponse:
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie(IDENTITY_COOKIE_NAME)
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request) -> dict[str, Any]:
    me = await _require_identity_user(request)
    return {
        "username": me.get("username"),
        "groups": me.get("groups", []),
        "scopes": me.get("scopes", []),
    }


# ---------------------------------------------------------------------------
# Proxy endpoints — avoid CORS issues by keeping browser ↔ dashboard only
# ---------------------------------------------------------------------------

async def _proxy_get(path: str, params: dict | None = None) -> dict:
    url = f"{ORCHESTRATOR_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(url, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


async def _proxy_get_text(path: str) -> str:
    url = f"{ORCHESTRATOR_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(url)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.text


async def _proxy_run_workflow(workflow_name: str, payload: dict[str, Any], bearer_token: str = "") -> dict[str, Any]:
    url = f"{GATEWAY_URL}/v1/run/{quote(workflow_name, safe='')}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = bearer_token or GATEWAY_RUN_BEARER_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, json=payload, headers=headers)

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return {
        "status": "ok",
        "workflow": workflow_name,
        "request_id": r.headers.get("X-Request-ID", ""),
        "content_type": r.headers.get("content-type", "text/plain"),
        "output": r.text,
    }


async def _identity_request(method: str, path: str, *, token: str = "", json_body: dict | None = None) -> Any:
    url = f"{IDENTITY_URL}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.request(method, url, headers=headers, json=json_body)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def _extract_identity_token(request: Request) -> str:
    return request.cookies.get(IDENTITY_COOKIE_NAME, "").strip()


def _scope_allowed(granted: set[str], required: str) -> bool:
    if "minicloud:*" in granted:
        return True
    if required in granted:
        return True
    parts = required.split(":")
    for i in range(len(parts), 1, -1):
        candidate = ":".join(parts[: i - 1]) + ":*"
        if candidate in granted:
            return True
    return False


def _run_scope_for(workflow_name: str) -> str:
    return f"minicloud:workflow:run:{workflow_name}"


def _retrigger_scope_for(workflow_name: str) -> str:
    return f"minicloud:workflow:retrigger:{workflow_name}"


async def _require_identity_user(request: Request) -> dict[str, Any]:
    token = _extract_identity_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    me = await _identity_request("GET", "/auth/me", token=token)
    me["_token"] = token
    return me


async def _require_scope(request: Request, required_scope: str) -> dict[str, Any]:
    me = await _require_identity_user(request)
    granted = set(me.get("scopes") or [])
    if _scope_allowed(granted, required_scope):
        return me
    raise HTTPException(status_code=403, detail=f"Missing required permission: {required_scope}")


async def _require_admin(request: Request) -> dict[str, Any]:
    me = await _require_identity_user(request)
    groups = set(me.get("groups") or [])
    if "admins" in groups:
        return me
    raise HTTPException(status_code=403, detail="Admin access required")


def _storage_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if STORAGE_READ_TOKEN:
        headers["Authorization"] = f"Bearer {STORAGE_READ_TOKEN}"
    if STORAGE_ROLES:
        headers["X-Storage-Roles"] = STORAGE_ROLES
    return headers


async def _storage_get(path: str, params: dict | None = None) -> Any:
    url = f"{STORAGE_SERVICE_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=_storage_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def _rabbitmq_auth() -> tuple[str, str] | None:
    if RABBITMQ_MANAGEMENT_USER:
        return (RABBITMQ_MANAGEMENT_USER, RABBITMQ_MANAGEMENT_PASSWORD)
    return None


def _require_rabbitmq_inspect_enabled() -> None:
    if not RABBITMQ_INSPECT_ENABLED:
        raise HTTPException(status_code=503, detail="RabbitMQ inspect API is disabled")


async def _rabbitmq_get(path: str, params: dict | None = None) -> Any:
    _require_rabbitmq_inspect_enabled()
    url = f"{RABBITMQ_MANAGEMENT_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, auth=_rabbitmq_auth()) as client:
        r = await client.get(url, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


async def _rabbitmq_post(path: str, payload: dict) -> Any:
    _require_rabbitmq_inspect_enabled()
    url = f"{RABBITMQ_MANAGEMENT_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, auth=_rabbitmq_auth()) as client:
        r = await client.post(url, json=payload)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.get("/api/workflows")
async def list_workflows() -> dict:
    return await _proxy_get("/workflows")


@app.get("/api/workflows/{name}")
async def get_workflow(name: str) -> dict:
    return await _proxy_get(f"/workflows/{name}")


@app.get("/api/traces")
async def list_traces(
    limit: int = Query(default=50, ge=1, le=500),
    workflow: str | None = Query(default=None),
) -> dict:
    params: dict = {"limit": limit}
    if workflow:
        params["workflow"] = workflow
    return await _proxy_get("/api/traces", params=params)


@app.get("/api/traces/{request_id}")
async def get_trace(request_id: str) -> dict:
    return await _proxy_get(f"/api/traces/{request_id}")


@app.get("/api/traces/{request_id}/steps/{step_path}/{kind}")
async def get_step_data(request_id: str, step_path: str, kind: str) -> PlainTextResponse:
    text = await _proxy_get_text(f"/api/traces/{request_id}/steps/{step_path}/{kind}")
    return PlainTextResponse(content=text)


class WorkflowRunBody(BaseModel):
    xml: str = Field(..., min_length=1)


@app.post("/api/run/{workflow_name}")
async def run_workflow_from_dashboard(workflow_name: str, body: WorkflowRunBody, request: Request) -> dict[str, Any]:
    me = await _require_scope(request, _run_scope_for(workflow_name))
    LOG.info("dashboard run authorized user=%s workflow=%s", me.get("username"), workflow_name)
    return await _proxy_run_workflow(
        workflow_name,
        payload={"xml": body.xml},
        bearer_token=str(me.get("_token", "")),
    )


@app.post("/api/retrigger/{request_id}")
async def retrigger_run_from_dashboard(request_id: str, request: Request) -> dict[str, Any]:
    trace = await _proxy_get(f"/api/traces/{request_id}")
    workflow_name = str(trace.get("workflow") or "")
    if not workflow_name:
        raise HTTPException(status_code=400, detail="Trace does not contain workflow")
    me = await _require_scope(request, _retrigger_scope_for(workflow_name))
    return {
        "status": "not_implemented",
        "message": "Re-trigger flow is not implemented yet",
        "workflow": workflow_name,
        "request_id": request_id,
        "authorized_user": me.get("username"),
    }


@app.get("/api/iam/users")
async def iam_users(request: Request) -> Any:
    me = await _require_admin(request)
    return await _identity_request("GET", "/users", token=me["_token"])


# ---------------------------------------------------------------------------
# Scheduler proxy routes
# ---------------------------------------------------------------------------

async def _scheduler_proxy(method: str, path: str, *, body: Any = None, user: str = "anonymous") -> Any:
    url = f"{SCHEDULER_URL}{path}"
    headers = {"X-User": user}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        if method == "GET":
            r = await client.get(url, headers=headers)
        elif method == "POST":
            if body is None:
                r = await client.post(url, headers=headers)
            else:
                r = await client.post(url, json=body, headers=headers)
        elif method == "PUT":
            r = await client.put(url, json=body, headers=headers)
        elif method == "DELETE":
            r = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)
    return r.json()


@app.get("/api/scheduler/named-schedules")
async def scheduler_list_named_schedules() -> Any:
    return await _scheduler_proxy("GET", "/named-schedules")


@app.post("/api/scheduler/named-schedules")
async def scheduler_create_named_schedule(body: dict, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("POST", "/named-schedules", body=body, user=me.get("username", "anonymous"))


@app.put("/api/scheduler/named-schedules/{schedule_id}")
async def scheduler_update_named_schedule(schedule_id: str, body: dict, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("PUT", f"/named-schedules/{schedule_id}", body=body, user=me.get("username", "anonymous"))


@app.delete("/api/scheduler/named-schedules/{schedule_id}")
async def scheduler_delete_named_schedule(schedule_id: str, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("DELETE", f"/named-schedules/{schedule_id}", user=me.get("username", "anonymous"))


@app.get("/api/scheduler/schedules")
async def scheduler_list_schedules() -> Any:
    return await _scheduler_proxy("GET", "/schedules")


@app.post("/api/scheduler/schedules")
async def scheduler_create_schedule(body: dict, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("POST", "/schedules", body=body, user=me.get("username", "anonymous"))


@app.delete("/api/scheduler/schedules/{job_id}")
async def scheduler_delete_schedule(job_id: str, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("DELETE", f"/schedules/{job_id}", user=me.get("username", "anonymous"))


@app.post("/api/scheduler/schedules/{job_id}/run")
async def scheduler_run_schedule(job_id: str, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("POST", f"/schedules/{job_id}/run", user=me.get("username", "anonymous"))


@app.post("/api/scheduler/workflows/{workflow_name}/run")
async def scheduler_run_workflow(workflow_name: str, body: dict, request: Request) -> Any:
    me = await _require_identity_user(request)
    return await _scheduler_proxy("POST", f"/workflows/{workflow_name}/run", body=body, user=me.get("username", "anonymous"))


@app.get("/api/iam/permissions")
async def iam_permissions(request: Request) -> Any:
    me = await _require_admin(request)
    return await _identity_request("GET", "/permissions", token=me["_token"])


class UpdateUserPermissionsBody(BaseModel):
    permissions: list[str]


@app.get("/api/iam/users/{username}/permissions")
async def iam_get_user_permissions(username: str, request: Request) -> Any:
    me = await _require_admin(request)
    return await _identity_request(
        "GET",
        f"/users/{quote(username, safe='')}/permissions",
        token=me["_token"],
    )


@app.put("/api/iam/users/{username}/permissions")
async def iam_set_user_permissions(username: str, body: UpdateUserPermissionsBody, request: Request) -> Any:
    me = await _require_admin(request)
    return await _identity_request(
        "PUT",
        f"/users/{quote(username, safe='')}/permissions",
        token=me["_token"],
        json_body={"permissions": body.permissions},
    )


@app.get("/api/rabbitmq/status")
async def rabbitmq_status() -> dict:
    return {
        "enabled": RABBITMQ_INSPECT_ENABLED,
        "management_url_configured": bool(RABBITMQ_MANAGEMENT_URL),
        "auth_configured": bool(RABBITMQ_MANAGEMENT_USER),
        "vhost": RABBITMQ_MANAGEMENT_VHOST,
    }


@app.get("/api/rabbitmq/overview")
async def rabbitmq_overview() -> Any:
    return await _rabbitmq_get("/overview")


@app.get("/api/rabbitmq/queues")
async def rabbitmq_queues() -> Any:
    vhost = quote(RABBITMQ_MANAGEMENT_VHOST, safe="")
    return await _rabbitmq_get(f"/queues/{vhost}")


@app.get("/api/rabbitmq/exchanges")
async def rabbitmq_exchanges() -> Any:
    vhost = quote(RABBITMQ_MANAGEMENT_VHOST, safe="")
    return await _rabbitmq_get(f"/exchanges/{vhost}")


@app.get("/api/rabbitmq/messages/peek")
async def rabbitmq_peek_messages(
    queue: str = Query(..., min_length=1),
    count: int = Query(default=20, ge=1, le=100),
    encoding: str = Query(default="auto", pattern="^(auto|base64)$"),
) -> Any:
    vhost = quote(RABBITMQ_MANAGEMENT_VHOST, safe="")
    queue_name = quote(queue, safe="")
    payload = {
        "count": count,
        "ackmode": "ack_requeue_true",
        "encoding": encoding,
        "requeue": True,
        "truncate": 50000,
    }
    return await _rabbitmq_post(f"/queues/{vhost}/{queue_name}/get", payload)


@app.get("/api/storage/status")
async def storage_status() -> dict:
    return {
        "storage_url_configured": bool(STORAGE_SERVICE_URL),
        "read_token_configured": bool(STORAGE_READ_TOKEN),
        "roles": STORAGE_ROLES,
    }


@app.get("/api/storage/buckets")
async def storage_buckets(limit: int = Query(default=200, ge=1, le=1000)) -> Any:
    return await _storage_get("/v1/storage", params={"limit": limit})


@app.get("/api/storage/keys")
async def storage_keys(
    bucket: str = Query(..., min_length=1),
    prefix: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> Any:
    params: dict[str, Any] = {"limit": limit}
    if prefix:
        params["prefix"] = prefix
    b = quote(bucket, safe="")
    return await _storage_get(f"/v1/storage/{b}", params=params)


@app.get("/api/storage/object")
async def storage_object(
    bucket: str = Query(..., min_length=1),
    key: str = Query(..., min_length=1),
) -> Any:
    b = quote(bucket, safe="")
    k = quote(key, safe="/")
    return await _storage_get(f"/v1/storage/{b}/{k}")


# ---------------------------------------------------------------------------
# Serve SPA
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    html = html_path.read_text(encoding="utf-8")
    # Cache-busting: append version hash to asset URLs
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={_ASSET_VERSION}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={_ASSET_VERSION}"')
    return HTMLResponse(content=html)


# Mount static assets (CSS/JS) — keep this AFTER specific routes
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
