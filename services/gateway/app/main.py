import logging
import os
import uuid
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

LOG = logging.getLogger("gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

XSLT_BASE_URL = os.environ.get("XSLT_URL", "http://localhost:8081").rstrip("/")
XSLT_APPLY_PATH = os.environ.get("XSLT_APPLY_PATH", "/apply")
XSLT_TIMEOUT = float(os.environ.get("XSLT_TIMEOUT_SECONDS", "60"))

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
ORCHESTRATOR_RUN_PATH = os.environ.get("ORCHESTRATOR_RUN_PATH", "/run")
ORCH_TIMEOUT = float(os.environ.get("ORCHESTRATOR_TIMEOUT_SECONDS", "120"))

app = FastAPI(title="MiniCloud Gateway", version="0.1.0")


class TransformBody(BaseModel):
    xml: str = Field(..., min_length=1)
    xslt: str = Field(..., min_length=1)


class RunWorkflowBody(BaseModel):
    workflow: str = Field(..., min_length=1)
    xml: str = Field(..., min_length=1)


class XmlOnlyBody(BaseModel):
    """Body voor URL-entry: workflow staat in het pad, niet in JSON."""

    xml: str = Field(..., min_length=1)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/v1/transform")
async def transform(
    body: TransformBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    rid = x_request_id or str(uuid.uuid4())
    url = f"{XSLT_BASE_URL}{XSLT_APPLY_PATH}"
    headers = {"X-Request-ID": rid, "Content-Type": "application/json"}
    payload = {"xml": body.xml, "xslt": body.xslt}
    LOG.info("forwarding transform request_id=%s -> %s", rid, url)
    try:
        async with httpx.AsyncClient(timeout=XSLT_TIMEOUT) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as e:
        LOG.error("xslt timeout request_id=%s: %s", rid, e)
        raise HTTPException(status_code=504, detail="XSLT service timeout") from e
    except httpx.RequestError as e:
        LOG.error("xslt unreachable request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail="XSLT service unavailable") from e

    if r.status_code >= 400:
        try:
            err_json = r.json()
            detail = err_json.get("detail", r.text)
        except Exception:
            detail = r.text
        LOG.warning(
            "xslt error request_id=%s status=%s detail=%s",
            rid,
            r.status_code,
            detail,
        )
        raise HTTPException(status_code=r.status_code, detail=detail)

    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "application/xml; charset=utf-8"),
        headers={"X-Request-ID": rid},
    )


async def _forward_orchestrator(
    url: str,
    *,
    json_body: dict,
    rid: str,
) -> Response:
    headers = {"X-Request-ID": rid, "Content-Type": "application/json"}
    LOG.info("forwarding workflow request_id=%s -> %s", rid, url)
    try:
        async with httpx.AsyncClient(timeout=ORCH_TIMEOUT) as client:
            r = await client.post(url, json=json_body, headers=headers)
    except httpx.TimeoutException as e:
        LOG.error("orchestrator timeout request_id=%s: %s", rid, e)
        raise HTTPException(status_code=504, detail="Orchestrator timeout") from e
    except httpx.RequestError as e:
        LOG.error("orchestrator unreachable request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail="Orchestrator unavailable") from e

    if r.status_code >= 400:
        try:
            err_json = r.json()
            detail = err_json.get("detail", r.text)
        except Exception:
            detail = r.text
        LOG.warning(
            "orchestrator error request_id=%s status=%s detail=%s",
            rid,
            r.status_code,
            detail,
        )
        raise HTTPException(status_code=r.status_code, detail=detail)

    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "application/xml; charset=utf-8"),
        headers={"X-Request-ID": r.headers.get("X-Request-ID", rid)},
    )


@app.post("/v1/run/{workflow_name}")
async def run_workflow_by_url(
    workflow_name: str,
    body: XmlOnlyBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    """HTTP-trigger: workflow wordt gekozen via pad (bv. /v1/run/demo)."""
    if not ORCHESTRATOR_URL:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator not configured (set ORCHESTRATOR_URL)",
        )
    rid = x_request_id or str(uuid.uuid4())
    path = f"/run/{quote(workflow_name, safe='')}"
    url = f"{ORCHESTRATOR_URL}{path}"
    return await _forward_orchestrator(url, json_body={"xml": body.xml}, rid=rid)


@app.post("/v1/run")
async def run_workflow(
    body: RunWorkflowBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    """Legacy: workflownaam in JSON-body."""
    if not ORCHESTRATOR_URL:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator not configured (set ORCHESTRATOR_URL)",
        )
    rid = x_request_id or str(uuid.uuid4())
    url = f"{ORCHESTRATOR_URL}{ORCHESTRATOR_RUN_PATH}"
    payload = {"workflow": body.workflow, "xml": body.xml}
    return await _forward_orchestrator(url, json_body=payload, rid=rid)
