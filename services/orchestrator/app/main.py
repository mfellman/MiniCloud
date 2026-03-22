import logging
import os
import uuid
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.workflow_runner import WorkflowDoc, load_workflows, run_workflow

LOG = logging.getLogger("orchestrator")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR", "/app/workflows"))
XSLT_BASE_URL = os.environ.get("XSLT_URL", "http://localhost:8081").rstrip("/")
XSLT_APPLY_PATH = os.environ.get("XSLT_APPLY_PATH", "/apply")
HTTP_CALL_BASE_URL = os.environ.get("HTTP_CALL_URL", "http://localhost:8082").rstrip("/")
HTTP_CALL_PATH = os.environ.get("HTTP_CALL_PATH", "/call")
REQUEST_TIMEOUT = float(os.environ.get("ORCH_TIMEOUT_SECONDS", "120"))

# Optioneel: vereist Authorization: Bearer <token> voor POST /invoke/scheduled (CronJob / interne caller).
SCHEDULE_INVOCATION_TOKEN = os.environ.get("SCHEDULE_INVOCATION_TOKEN", "").strip()

app = FastAPI(title="MiniCloud Orchestrator", version="0.1.0")
_WORKFLOWS: dict[str, WorkflowDoc] = {}


@app.on_event("startup")
def startup() -> None:
    global _WORKFLOWS  # noqa: PLW0603
    _WORKFLOWS = load_workflows(WORKFLOWS_DIR)
    LOG.info("orchestrator loaded %d workflow(s) from %s", len(_WORKFLOWS), WORKFLOWS_DIR)


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


async def _verify_schedule_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> bool:
    if not SCHEDULE_INVOCATION_TOKEN:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    token = authorization.removeprefix("Bearer ").strip()
    if token != SCHEDULE_INVOCATION_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid schedule invocation token")
    return True


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready", "workflows": len(_WORKFLOWS)}


@app.get("/workflows")
def list_workflows() -> dict:
    return {
        "workflows": [
            {"name": n, "invocation": doc.invocation.model_dump()}
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
) -> PlainTextResponse:
    xslt_url = f"{XSLT_BASE_URL}{XSLT_APPLY_PATH}"
    http_url = f"{HTTP_CALL_BASE_URL}{HTTP_CALL_PATH}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            final_body, _outputs, trace = await run_workflow(
                doc,
                xml,
                xslt_apply_url=xslt_url,
                http_call_url=http_url,
                request_id=rid,
                httpx_client=client,
            )
    except RuntimeError as e:
        LOG.warning("workflow failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    except httpx.TimeoutException as e:
        LOG.error("timeout request_id=%s: %s", rid, e)
        raise HTTPException(status_code=504, detail="Workflow step timeout") from e
    except httpx.RequestError as e:
        LOG.error("upstream error request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    LOG.info(
        "workflow ok request_id=%s workflow=%s trace=%s",
        rid,
        workflow_label,
        trace,
    )
    return PlainTextResponse(
        content=final_body,
        media_type="application/xml; charset=utf-8",
        headers={"X-Request-ID": rid},
    )


@app.post("/run/{workflow_name}")
async def run_by_path(
    workflow_name: str,
    body: XmlBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
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
    return await _execute(doc, body.xml, rid=rid, workflow_label=workflow_name)


@app.post("/run")
async def run_by_body(
    body: RunBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
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
    return await _execute(doc, body.xml, rid=rid, workflow_label=body.workflow)


@app.post("/invoke/scheduled")
async def invoke_scheduled(
    body: RunBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    _: bool = Depends(_verify_schedule_auth),
) -> PlainTextResponse:
    """
    Aanroep voor CronJobs / interne scheduler: alleen workflows met allow_schedule: true.
    Optioneel beveiligen met SCHEDULE_INVOCATION_TOKEN (Bearer).
    """
    rid = x_request_id or str(uuid.uuid4())
    doc = _WORKFLOWS.get(body.workflow)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown workflow: {body.workflow!r}. Available: {sorted(_WORKFLOWS.keys())}",
        )
    _require_schedule(doc)
    return await _execute(doc, body.xml, rid=rid, workflow_label=body.workflow)
