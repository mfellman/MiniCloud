import logging
import os
import uuid
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.connections import load_connections
from app.oauth_policy import (
    OAUTH2_APPLY_TO_SCHEDULED,
    OAUTH2_ENABLED,
    OAuthScopeDenied,
    bearer_scopes_from_request,
    validate_oauth_config_at_startup,
)
from app.trace_store import begin_run_trace, get_step_data, get_trace, list_traces
from app.workflow_runner import WorkflowDoc, load_workflows, run_workflow

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
REQUEST_TIMEOUT = float(os.environ.get("ORCH_TIMEOUT_SECONDS", "120"))

# Optioneel: Bearer voor POST /run en POST /run/{name} (HTTP-triggers, o.a. via gateway).
# Genegeerd wanneer OAUTH2_ENABLED (JWT + scopes i.p.v. shared secret).
HTTP_INVOCATION_TOKEN = os.environ.get("HTTP_INVOCATION_TOKEN", "").strip()
# Optioneel: Bearer voor POST /invoke/scheduled (CronJob / interne caller).
SCHEDULE_INVOCATION_TOKEN = os.environ.get("SCHEDULE_INVOCATION_TOKEN", "").strip()


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


app = FastAPI(title="MiniCloud Orchestrator", version="0.1.0")
_WORKFLOWS: dict[str, WorkflowDoc] = {}
_CONNECTIONS: dict[str, object] = {}


@app.on_event("startup")
def startup() -> None:
    global _WORKFLOWS, _CONNECTIONS  # noqa: PLW0603
    validate_oauth_config_at_startup()
    _WORKFLOWS = load_workflows(WORKFLOWS_DIR)
    _CONNECTIONS = load_connections(CONNECTIONS_DIR)
    LOG.info(
        "orchestrator loaded %d workflow(s) from %s, %d connection(s) from %s",
        len(_WORKFLOWS),
        WORKFLOWS_DIR,
        len(_CONNECTIONS),
        CONNECTIONS_DIR,
    )


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
    return {
        "status": "ready",
        "workflows": len(_WORKFLOWS),
        "connections": len(_CONNECTIONS),
    }


@app.get("/workflows")
def list_workflows() -> dict:
    return {
        "workflows": [
            {
                "name": n,
                "group": doc.group,
                "invocation": doc.invocation.model_dump(),
                "step_count": len(doc.steps),
                "step_types": sorted({getattr(s, "type", "unknown") for s in doc.steps}),
            }
            for n, doc in sorted(_WORKFLOWS.items())
        ]
    }


@app.get("/workflows/http")
def list_http_workflows() -> dict:
    """Alleen workflows die via HTTP (gateway/URL) gestart mogen worden."""
    return {
        "workflows": [
            {"name": n, "invocation": doc.invocation.model_dump()}
            for n, doc in sorted(_WORKFLOWS.items())
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
    wf_def = [s.model_dump(mode="json") for s in doc.steps]
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
                request_id=rid,
                httpx_client=client,
                granted_scopes=granted_scopes,
                connections=_CONNECTIONS,
                run_trace=rt,
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
    doc = _WORKFLOWS.get(workflow_name)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow: {workflow_name!r}. Available: {sorted(_WORKFLOWS.keys())}",
        )
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
    doc = _WORKFLOWS.get(body.workflow)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow: {body.workflow!r}. Available: {sorted(_WORKFLOWS.keys())}",
        )
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
    doc = _WORKFLOWS.get(body.workflow)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow: {body.workflow!r}. Available: {sorted(_WORKFLOWS.keys())}",
        )
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
    doc = _WORKFLOWS.get(workflow_name)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_name!r}")
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
