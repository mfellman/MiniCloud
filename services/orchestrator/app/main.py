import logging
import os
import threading
import uuid
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated

import asyncio
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.oauth_policy import (
    OAUTH2_APPLY_TO_SCHEDULED,
    OAUTH2_ENABLED,
    OAuthScopeDenied,
    bearer_scopes_from_request,
    validate_oauth_config_at_startup,
)
from app.runtime_store import build_runtime_store
from app.trace_store import begin_run_trace, get_step_data, get_trace, list_traces
from app.workflow_runner import WorkflowDoc, run_workflow

EGRESS_RABBITMQ_BASE = os.environ.get("EGRESS_RABBITMQ_URL", "http://localhost:8087").rstrip("/")

RABBITMQ_TRIGGER_ENABLED = os.environ.get("RABBITMQ_TRIGGER_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
RABBITMQ_TRIGGER_URL = os.environ.get("RABBITMQ_TRIGGER_URL", "amqp://guest:guest@rabbitmq:5672/").strip()
RABBITMQ_TRIGGER_EXCHANGE = os.environ.get("RABBITMQ_TRIGGER_EXCHANGE", "minicloud.events").strip()
RABBITMQ_TRIGGER_EXCHANGE_TYPE = os.environ.get("RABBITMQ_TRIGGER_EXCHANGE_TYPE", "topic").strip()
RABBITMQ_TRIGGER_QUEUE = os.environ.get("RABBITMQ_TRIGGER_QUEUE", "orchestrator-trigger").strip()
RABBITMQ_TRIGGER_BINDING_KEY = os.environ.get("RABBITMQ_TRIGGER_BINDING_KEY", "#").strip() or "#"
RABBITMQ_TRIGGER_WORKFLOW = os.environ.get("RABBITMQ_TRIGGER_WORKFLOW", "").strip()
RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW = os.environ.get(
    "RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW",
    "",
).strip()
RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW = os.environ.get(
    "RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW",
    "",
).strip()
RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW = os.environ.get(
    "RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW",
    "",
).strip()
RABBITMQ_TRIGGER_REQUEUE_ON_ERROR = os.environ.get("RABBITMQ_TRIGGER_REQUEUE_ON_ERROR", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_RABBITMQ_TRIGGER_TASK: asyncio.Task | None = None

LOG = logging.getLogger("orchestrator")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR", "/app/workflows"))
CONNECTIONS_DIR = Path(os.environ.get("CONNECTIONS_DIR", "/app/connections"))
TRANSFORMERS_BASE_URL = os.environ.get("TRANSFORMERS_URL", "http://localhost:8081").rstrip("/")
_EGRESS_HTTP_BASE = os.environ.get(
    "EGRESS_HTTP_URL",
    os.environ.get("HTTP_CALL_URL", "http://localhost:8082"),
).rstrip("/")
_EGRESS_HTTP_PATH = os.environ.get(
    "EGRESS_HTTP_PATH",
    os.environ.get("HTTP_CALL_PATH", "/call"),
)
EGRESS_FTP_BASE = os.environ.get("EGRESS_FTP_URL", "http://localhost:8084").rstrip("/")
EGRESS_SSH_BASE = os.environ.get("EGRESS_SSH_URL", "http://localhost:8085").rstrip("/")
STORAGE_SERVICE_URL = os.environ.get("STORAGE_SERVICE_URL", "").rstrip("/")
STORAGE_SERVICE_BEARER_TOKEN = os.environ.get("STORAGE_SERVICE_BEARER_TOKEN", "").strip()
STORAGE_SERVICE_ROLES = os.environ.get("STORAGE_SERVICE_ROLES", "orchestrator").strip()
REQUEST_TIMEOUT = float(os.environ.get("ORCH_TIMEOUT_SECONDS", "120"))
RELOAD_TOKEN = os.environ.get("ORCH_RELOAD_TOKEN", "").strip()

# Optioneel: Bearer voor POST /run en POST /run/{name} (HTTP-triggers, o.a. via gateway).
# Genegeerd wanneer OAUTH2_ENABLED (JWT + scopes i.p.v. shared secret).
HTTP_INVOCATION_TOKEN = os.environ.get("HTTP_INVOCATION_TOKEN", "").strip()
# Optioneel: Bearer voor POST /invoke/scheduled (CronJob / interne caller).
SCHEDULE_INVOCATION_TOKEN = os.environ.get("SCHEDULE_INVOCATION_TOKEN", "").strip()

app = FastAPI(title="MiniCloud Orchestrator", version="0.1.0")
_WORKFLOWS: dict[str, WorkflowDoc] = {}
_CONNECTIONS: dict[str, object] = {}
_RUNTIME_STORE = build_runtime_store(
    workflows_dir=WORKFLOWS_DIR,
    connections_dir=CONNECTIONS_DIR,
)
_RELOAD_LOCK = threading.Lock()


def _optional_bearer_or_raise(authorization: str | None, secret: str) -> None:
    if not secret:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    token = authorization.removeprefix("Bearer ").strip()
    if token != secret:
        raise HTTPException(status_code=403, detail="Invalid Bearer token")


async def _http_entry_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> frozenset[str] | None:
    """JWT + scopes (OAuth2) of gedeeld geheim (HTTP_INVOCATION_TOKEN)."""
    if OAUTH2_ENABLED:
        return bearer_scopes_from_request(authorization)
    _optional_bearer_or_raise(authorization, HTTP_INVOCATION_TOKEN)
    return None


async def _schedule_entry_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> frozenset[str] | None:
    if OAUTH2_ENABLED and OAUTH2_APPLY_TO_SCHEDULED:
        return bearer_scopes_from_request(authorization)
    _optional_bearer_or_raise(authorization, SCHEDULE_INVOCATION_TOKEN)
    return None


def _reload_runtime_data() -> tuple[int, int]:
    global _WORKFLOWS, _CONNECTIONS  # noqa: PLW0603
    with _RELOAD_LOCK:
        workflows = _RUNTIME_STORE.load_workflows()
        connections = _RUNTIME_STORE.load_connections()
        _WORKFLOWS = workflows
        _CONNECTIONS = connections
    return len(_WORKFLOWS), len(_CONNECTIONS)


def _workflows_snapshot() -> dict[str, WorkflowDoc]:
    with _RELOAD_LOCK:
        return dict(_WORKFLOWS)


def _connections_snapshot() -> dict[str, object]:
    with _RELOAD_LOCK:
        return dict(_CONNECTIONS)


def _get_workflow_or_404(workflow_name: str) -> WorkflowDoc:
    with _RELOAD_LOCK:
        doc = _WORKFLOWS.get(workflow_name)
        known = sorted(_WORKFLOWS.keys())
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow: {workflow_name!r}. Available: {known}",
        )
    return doc


def _resolve_trigger_workflow(headers: dict[str, str]) -> str | None:
    def _h(name: str) -> str:
        return (headers.get(name) or headers.get(name.lower()) or "").strip()

    def _patterns(raw: str) -> list[str]:
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _matches_any(value: str, pats: list[str]) -> bool:
        if not pats:
            return True
        return any(fnmatchcase(value, p) for p in pats)

    explicit = (headers.get("Workflow") or headers.get("workflow") or "").strip()
    if explicit:
        return explicit

    domain = _h("Domain")
    service = _h("Service")
    action = _h("Action")
    version = _h("Version")

    # Dedicated route for storage.changed events with optional bucket/key filtering.
    if RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW:
        is_storage_changed = (
            domain.lower() == "storage"
            and service.lower() == "kv"
            and action.lower() == "updated"
        )
        if is_storage_changed:
            bucket = _h("Bucket")
            key = _h("Key")
            if _matches_any(bucket, _patterns(RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW)) and _matches_any(
                key,
                _patterns(RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW),
            ):
                return RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW
            LOG.info(
                "storage.changed event skipped by filters bucket=%r key=%r",
                bucket,
                key,
            )
            return None

    if RABBITMQ_TRIGGER_WORKFLOW:
        return RABBITMQ_TRIGGER_WORKFLOW

    if not (domain and service and action and version):
        return None

    candidates = [
        f"{domain}.{service}.{action}.{version}",
        f"{domain}-{service}-{action}-{version}",
    ]
    workflows = _workflows_snapshot()
    lower_map = {name.lower(): name for name in workflows}
    for candidate in candidates:
        if candidate in workflows:
            return candidate
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


async def _rabbitmq_trigger_loop() -> None:
    try:
        import importlib

        aio_pika = importlib.import_module("aio_pika")
        ExchangeType = aio_pika.ExchangeType
        connect_robust = aio_pika.connect_robust
    except Exception as e:
        LOG.error("RabbitMQ trigger enabled but aio_pika is unavailable: %s", e)
        return

    LOG.info(
        "starting RabbitMQ trigger loop queue=%s exchange=%s binding=%s",
        RABBITMQ_TRIGGER_QUEUE,
        RABBITMQ_TRIGGER_EXCHANGE,
        RABBITMQ_TRIGGER_BINDING_KEY,
    )
    while True:
        try:
            connection = await connect_robust(RABBITMQ_TRIGGER_URL)
            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=10)
                exchange = await channel.declare_exchange(
                    RABBITMQ_TRIGGER_EXCHANGE,
                    ExchangeType(RABBITMQ_TRIGGER_EXCHANGE_TYPE),
                    durable=True,
                )
                queue = await channel.declare_queue(RABBITMQ_TRIGGER_QUEUE, durable=True)
                await queue.bind(exchange, routing_key=RABBITMQ_TRIGGER_BINDING_KEY)

                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        headers = {
                            str(k): str(v)
                            for k, v in (message.headers or {}).items()
                            if k is not None and v is not None
                        }
                        workflow_name = _resolve_trigger_workflow(headers)
                        if not workflow_name:
                            LOG.warning(
                                "rabbitmq trigger skipped message: unable to resolve workflow from headers %s",
                                sorted(headers.keys()),
                            )
                            await message.ack()
                            continue

                        doc = _workflows_snapshot().get(workflow_name)
                        if doc is None:
                            LOG.warning("rabbitmq trigger workflow not found: %s", workflow_name)
                            await message.ack()
                            continue
                        if not doc.invocation.allow_schedule:
                            LOG.warning("rabbitmq trigger workflow does not allow schedule: %s", workflow_name)
                            await message.ack()
                            continue

                        try:
                            payload = message.body.decode("utf-8")
                            rid = message.correlation_id or str(uuid.uuid4())
                            await _execute(
                                doc,
                                payload,
                                rid=rid,
                                workflow_label=workflow_name,
                                granted_scopes=None,
                            )
                            await message.ack()
                        except Exception as e:
                            LOG.error("rabbitmq trigger failed for workflow=%s: %s", workflow_name, e)
                            await message.nack(requeue=RABBITMQ_TRIGGER_REQUEUE_ON_ERROR)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.error("rabbitmq trigger connection loop error: %s", e)
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup() -> None:
    global _RABBITMQ_TRIGGER_TASK  # noqa: PLW0603
    validate_oauth_config_at_startup()
    wf_count, conn_count = _reload_runtime_data()
    LOG.info(
        "orchestrator loaded %d workflow(s), %d connection(s) using runtime store %s",
        wf_count,
        conn_count,
        type(_RUNTIME_STORE).__name__,
    )
    if RABBITMQ_TRIGGER_ENABLED:
        _RABBITMQ_TRIGGER_TASK = asyncio.create_task(_rabbitmq_trigger_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global _RABBITMQ_TRIGGER_TASK  # noqa: PLW0603
    if _RABBITMQ_TRIGGER_TASK is not None:
        _RABBITMQ_TRIGGER_TASK.cancel()
        try:
            await _RABBITMQ_TRIGGER_TASK
        except asyncio.CancelledError:
            pass
        _RABBITMQ_TRIGGER_TASK = None


@app.post("/admin/reload")
def admin_reload(
    x_reload_token: Annotated[str | None, Header(alias="X-Reload-Token")] = None,
) -> dict:
    if RELOAD_TOKEN and x_reload_token != RELOAD_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid reload token")
    wf_count, conn_count = _reload_runtime_data()
    LOG.info(
        "runtime reload complete: workflows=%d connections=%d",
        wf_count,
        conn_count,
    )
    return {
        "status": "reloaded",
        "workflows": wf_count,
        "connections": conn_count,
    }


def _require_http(doc: WorkflowDoc) -> None:
    if not doc.invocation.allow_http:
        raise HTTPException(
            status_code=403,
            detail="This workflow is not invokable via HTTP triggers",
        )


def _require_schedule(doc: WorkflowDoc) -> None:
    if not doc.invocation.allow_schedule:
        raise HTTPException(
            status_code=403,
            detail="This workflow is not invokable via scheduled invocation",
        )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    with _RELOAD_LOCK:
        wf_count = len(_WORKFLOWS)
        conn_count = len(_CONNECTIONS)
    return {
        "status": "ready",
        "workflows": wf_count,
        "connections": conn_count,
    }


@app.get("/workflows")
def list_workflows() -> dict:
    workflows = _workflows_snapshot()
    return {
        "workflows": [
            {
                "name": n,
                "group": doc.group,
                "invocation": doc.invocation.model_dump(),
                "step_count": len(doc.steps),
                "step_types": sorted({getattr(s, "type", "unknown") for s in doc.steps}),
            }
            for n, doc in sorted(workflows.items())
        ]
    }


@app.get("/workflows/http")
def list_http_workflows() -> dict:
    """Alleen workflows die via HTTP (gateway/URL) gestart mogen worden."""
    workflows = _workflows_snapshot()
    return {
        "workflows": [
            {"name": n, "invocation": doc.invocation.model_dump()}
            for n, doc in sorted(workflows.items())
            if doc.invocation.allow_http
        ]
    }


class XmlBody(BaseModel):
    xml: str = Field(..., min_length=1)


class RunBody(BaseModel):
    workflow: str = Field(..., min_length=1)
    xml: str = Field(..., min_length=1)


async def _execute(
    doc: WorkflowDoc,
    xml: str,
    *,
    rid: str,
    workflow_label: str,
    granted_scopes: frozenset[str] | None = None,
) -> PlainTextResponse:
    egress_http_url = f"{_EGRESS_HTTP_BASE}{_EGRESS_HTTP_PATH}"
    egress_ftp_url = f"{EGRESS_FTP_BASE}/ftp"
    egress_ssh_url = f"{EGRESS_SSH_BASE}/exec"
    egress_sftp_url = f"{EGRESS_SSH_BASE}/sftp"
    egress_rabbitmq_url = EGRESS_RABBITMQ_BASE
    wf_def = [s.model_dump(mode="json") for s in doc.steps]
    connections = _connections_snapshot()
    rt = begin_run_trace(rid, workflow_label, workflow_definition=wf_def)
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            final_body, _outputs, trace, _ctx = await run_workflow(
                doc,
                xml,
                transformers_base_url=TRANSFORMERS_BASE_URL,
                egress_http_url=egress_http_url,
                egress_ftp_url=egress_ftp_url,
                egress_ssh_url=egress_ssh_url,
                egress_sftp_url=egress_sftp_url,
                egress_rabbitmq_url=egress_rabbitmq_url,
                request_id=rid,
                httpx_client=client,
                granted_scopes=granted_scopes,
                connections=connections,
                run_trace=rt,
                storage_base_url=STORAGE_SERVICE_URL,
                storage_bearer_token=STORAGE_SERVICE_BEARER_TOKEN,
                storage_roles_header=STORAGE_SERVICE_ROLES,
            )
    except OAuthScopeDenied as e:
        rt.finish(status="failed", error=str(e))
        raise HTTPException(status_code=403, detail=str(e)) from e
    except RuntimeError as e:
        LOG.warning("workflow failed request_id=%s: %s", rid, e)
        rt.finish(status="failed", error=str(e))
        raise HTTPException(status_code=502, detail=str(e)) from e
    except httpx.TimeoutException as e:
        LOG.error("timeout request_id=%s: %s", rid, e)
        rt.finish(status="failed", error=f"Timeout: {e}")
        raise HTTPException(status_code=504, detail="Workflow step timeout") from e
    except httpx.RequestError as e:
        LOG.error("upstream error request_id=%s: %s", rid, e)
        rt.finish(status="failed", error=f"Upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    rt.finish(status="succeeded", final_output=final_body, context=_ctx)
    LOG.info(
        "workflow ok request_id=%s workflow=%s trace=%s",
        rid,
        workflow_label,
        trace,
    )
    return PlainTextResponse(
        content=final_body,
        media_type="text/plain; charset=utf-8",
        headers={"X-Request-ID": rid},
    )


@app.post("/run/{workflow_name}")
async def run_by_path(
    workflow_name: str,
    body: XmlBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    granted_scopes: frozenset[str] | None = Depends(_http_entry_auth),
) -> PlainTextResponse:
    """HTTP-entry: workflow wordt gekozen via URL-pad; vereist allow_http op de workflow."""
    rid = x_request_id or str(uuid.uuid4())
    doc = _get_workflow_or_404(workflow_name)
    _require_http(doc)
    return await _execute(
        doc,
        body.xml,
        rid=rid,
        workflow_label=workflow_name,
        granted_scopes=granted_scopes,
    )


@app.post("/run")
async def run_by_body(
    body: RunBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    granted_scopes: frozenset[str] | None = Depends(_http_entry_auth),
) -> PlainTextResponse:
    """Legacy: workflow in body. Zelfde HTTP-policy als pad-variant."""
    rid = x_request_id or str(uuid.uuid4())
    doc = _get_workflow_or_404(body.workflow)
    _require_http(doc)
    return await _execute(
        doc,
        body.xml,
        rid=rid,
        workflow_label=body.workflow,
        granted_scopes=granted_scopes,
    )


@app.post("/invoke/scheduled")
async def invoke_scheduled(
    body: RunBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    granted_scopes: frozenset[str] | None = Depends(_schedule_entry_auth),
) -> PlainTextResponse:
    """
    Aanroep voor CronJobs / interne scheduler: alleen workflows met allow_schedule: true.
    Zonder OAuth2: optioneel SCHEDULE_INVOCATION_TOKEN. Met OAuth2: zelfde JWT+scopes als /run.
    """
    rid = x_request_id or str(uuid.uuid4())
    doc = _get_workflow_or_404(body.workflow)
    _require_schedule(doc)
    return await _execute(
        doc,
        body.xml,
        rid=rid,
        workflow_label=body.workflow,
        granted_scopes=granted_scopes,
    )


# ---------------------------------------------------------------------------
# Trace & workflow detail API (read-only, used by MiniCloud Dashboard)
# ---------------------------------------------------------------------------

@app.get("/workflows/{workflow_name}")
def get_workflow_detail(workflow_name: str) -> dict:
    """Return full workflow definition with steps for visualisation."""
    doc = _get_workflow_or_404(workflow_name)
    return {
        "name": doc.name,
        "group": doc.group,
        "invocation": doc.invocation.model_dump(),
        "step_count": len(doc.steps),
        "step_types": sorted({getattr(s, "type", "unknown") for s in doc.steps}),
        "steps": [s.model_dump(mode="json") for s in doc.steps],
    }


@app.get("/api/traces")
def api_list_traces(limit: int = 50, workflow: str | None = None) -> dict:
    return {"traces": list_traces(limit=min(limit, 500), workflow=workflow)}


@app.get("/api/traces/{request_id}")
def api_get_trace(request_id: str) -> dict:
    doc = get_trace(request_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return doc


@app.get("/api/traces/{request_id}/steps/{step_path}/{kind}")
def api_get_step_data(request_id: str, step_path: str, kind: str) -> PlainTextResponse:
    data = get_step_data(request_id, step_path, kind)
    if data is None:
        raise HTTPException(status_code=404, detail="Step data not found")
    return PlainTextResponse(content=data)
