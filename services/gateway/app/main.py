import asyncio
import logging
import os
import time
import uuid
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

LOG = logging.getLogger("gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

TRANSFORMERS_BASE_URL = os.environ.get("TRANSFORMERS_URL", "http://localhost:8081").rstrip("/")
TRANSFORMERS_APPLY_PATH = os.environ.get("TRANSFORMERS_APPLY_PATH", "/applyXSLT")
TRANSFORMERS_TIMEOUT = float(os.environ.get("TRANSFORMERS_TIMEOUT_SECONDS", "60"))

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
ORCHESTRATOR_RUN_PATH = os.environ.get("ORCHESTRATOR_RUN_PATH", "/run")
ORCH_TIMEOUT = float(os.environ.get("ORCHESTRATOR_TIMEOUT_SECONDS", "120"))

# Aggregate status (/v1/status): probe peer services (optional egress URLs for full stack view).
STATUS_PROBE_TIMEOUT = float(os.environ.get("STATUS_PROBE_TIMEOUT_SECONDS", "3"))
EGRESS_HTTP_URL = os.environ.get("EGRESS_HTTP_URL", "").rstrip("/")
EGRESS_FTP_URL = os.environ.get("EGRESS_FTP_URL", "").rstrip("/")
EGRESS_SSH_URL = os.environ.get("EGRESS_SSH_URL", "").rstrip("/")
GITLAB_PIPELINE_URL = os.environ.get("GITLAB_PIPELINE_URL", "").strip()
GITLAB_PROJECT_URL = os.environ.get("GITLAB_PROJECT_URL", "").strip()

# Production: expose only workflow triggers (/v1/run*). Disables /v1/transform and /v1/status
# so clients cannot reach transformers or enumerate internals without going through workflows.
_ORCH_ONLY_RAW = os.environ.get("GATEWAY_ORCHESTRATION_ONLY", "").strip().lower()
GATEWAY_ORCHESTRATION_ONLY = _ORCH_ONLY_RAW in ("1", "true", "yes", "on")

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


async def _probe_service(
    client: httpx.AsyncClient,
    logical_name: str,
    base_url: str,
    *,
    path: str = "/readyz",
) -> tuple[str, dict[str, Any]]:
    """GET base_url+path; treat 2xx/3xx as ok for readiness-style endpoints."""
    url = f"{base_url}{path}"
    t0 = time.perf_counter()
    try:
        r = await client.get(url)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        ok = r.status_code < 400
        return logical_name, {
            "ok": ok,
            "status_code": r.status_code,
            "latency_ms": elapsed_ms,
            "url": url,
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        LOG.warning("status probe failed %s %s: %s", logical_name, url, e)
        return logical_name, {
            "ok": False,
            "error": str(e),
            "latency_ms": elapsed_ms,
            "url": url,
        }


@app.get("/v1/status")
async def aggregate_status() -> dict[str, Any]:
    """
    Aggregated health of configured downstream services (parallel probes).
    The **tests** section does not execute pytest; it points to CI / local runs.
    """
    if GATEWAY_ORCHESTRATION_ONLY:
        raise HTTPException(status_code=404, detail="Not found")
    services: dict[str, Any] = {}
    tasks: list[Any] = []

    timeout = httpx.Timeout(STATUS_PROBE_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if TRANSFORMERS_BASE_URL:
            tasks.append(
                _probe_service(client, "transformers", TRANSFORMERS_BASE_URL),
            )
        if ORCHESTRATOR_URL:
            tasks.append(
                _probe_service(client, "orchestrator", ORCHESTRATOR_URL),
            )
        if EGRESS_HTTP_URL:
            tasks.append(
                _probe_service(client, "egress_http", EGRESS_HTTP_URL),
            )
        if EGRESS_FTP_URL:
            tasks.append(_probe_service(client, "egress_ftp", EGRESS_FTP_URL))
        if EGRESS_SSH_URL:
            tasks.append(_probe_service(client, "egress_ssh", EGRESS_SSH_URL))

        if tasks:
            results = await asyncio.gather(*tasks)
            for name, detail in results:
                services[name] = detail

    if not TRANSFORMERS_BASE_URL:
        services["transformers"] = {
            "skipped": True,
            "reason": "TRANSFORMERS_URL not set",
        }
    if not ORCHESTRATOR_URL:
        services["orchestrator"] = {
            "skipped": True,
            "reason": "ORCHESTRATOR_URL not set",
        }
    if not EGRESS_HTTP_URL:
        services.setdefault(
            "egress_http",
            {"skipped": True, "reason": "EGRESS_HTTP_URL not set on gateway"},
        )
    if not EGRESS_FTP_URL:
        services.setdefault(
            "egress_ftp",
            {"skipped": True, "reason": "EGRESS_FTP_URL not set on gateway"},
        )
    if not EGRESS_SSH_URL:
        services.setdefault(
            "egress_ssh",
            {"skipped": True, "reason": "EGRESS_SSH_URL not set on gateway"},
        )

    active = [s for s in services.values() if not s.get("skipped")]
    overall_ok = bool(active) and all(bool(s.get("ok")) for s in active)

    tests: dict[str, Any] = {
        "execution": "ci_and_local",
        "suite": "pytest",
        "path": "tests/",
        "hint": "Pytest is not run by this endpoint. Run ./scripts/run-tests.sh locally or rely on GitLab CI (.gitlab-ci.yml).",
    }
    if GITLAB_PIPELINE_URL:
        tests["gitlab_pipeline_url"] = GITLAB_PIPELINE_URL
    if GITLAB_PROJECT_URL:
        tests["gitlab_project_url"] = GITLAB_PROJECT_URL

    return {
        "overall_ok": overall_ok,
        "gateway": {"ok": True, "role": "entry"},
        "services": services,
        "tests": tests,
    }


@app.post("/v1/transform")
async def transform(
    body: TransformBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    if GATEWAY_ORCHESTRATION_ONLY:
        raise HTTPException(status_code=404, detail="Not found")
    rid = x_request_id or str(uuid.uuid4())
    url = f"{TRANSFORMERS_BASE_URL}{TRANSFORMERS_APPLY_PATH}"
    headers = {"X-Request-ID": rid, "Content-Type": "application/json"}
    payload = {"xml": body.xml, "xslt": body.xslt}
    LOG.info("forwarding transformers/applyXSLT request_id=%s -> %s", rid, url)
    try:
        async with httpx.AsyncClient(timeout=TRANSFORMERS_TIMEOUT) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as e:
        LOG.error("transformers timeout request_id=%s: %s", rid, e)
        raise HTTPException(status_code=504, detail="Transformers service timeout") from e
    except httpx.RequestError as e:
        LOG.error("transformers unreachable request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail="Transformers service unavailable") from e

    if r.status_code >= 400:
        try:
            err_json = r.json()
            detail = err_json.get("detail", r.text)
        except Exception:
            detail = r.text
        LOG.warning(
            "transformers error request_id=%s status=%s detail=%s",
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
    authorization: str | None = None,
) -> Response:
    headers = {"X-Request-ID": rid, "Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
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
    authorization: Annotated[str | None, Header()] = None,
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
    return await _forward_orchestrator(
        url,
        json_body={"xml": body.xml},
        rid=rid,
        authorization=authorization,
    )


@app.post("/v1/run")
async def run_workflow(
    body: RunWorkflowBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    authorization: Annotated[str | None, Header()] = None,
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
    return await _forward_orchestrator(
        url,
        json_body=payload,
        rid=rid,
        authorization=authorization,
    )


# ---------------------------------------------------------------------------
# Trace API (proxy to orchestrator /api/traces)
# ---------------------------------------------------------------------------

@app.get("/v1/traces")
async def list_traces(limit: int = 50) -> Response:
    """Proxy trace list from orchestrator."""
    if not ORCHESTRATOR_URL:
        raise HTTPException(status_code=503, detail="Orchestrator not configured")
    if GATEWAY_ORCHESTRATION_ONLY:
        raise HTTPException(status_code=403, detail="Trace API disabled in orchestration-only mode")
    url = f"{ORCHESTRATOR_URL}/api/traces?limit={min(limit, 500)}"
    async with httpx.AsyncClient(timeout=ORCH_TIMEOUT) as client:
        r = await client.get(url)
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/v1/traces/{request_id}")
async def get_trace(request_id: str) -> Response:
    """Proxy single trace from orchestrator."""
    if not ORCHESTRATOR_URL:
        raise HTTPException(status_code=503, detail="Orchestrator not configured")
    if GATEWAY_ORCHESTRATION_ONLY:
        raise HTTPException(status_code=403, detail="Trace API disabled in orchestration-only mode")
    url = f"{ORCHESTRATOR_URL}/api/traces/{quote(request_id)}"
    async with httpx.AsyncClient(timeout=ORCH_TIMEOUT) as client:
        r = await client.get(url)
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/v1/traces/{request_id}/steps/{step_path:path}/{kind}")
async def get_step_data(request_id: str, step_path: str, kind: str) -> Response:
    """Proxy step input/output data from orchestrator."""
    if not ORCHESTRATOR_URL:
        raise HTTPException(status_code=503, detail="Orchestrator not configured")
    if GATEWAY_ORCHESTRATION_ONLY:
        raise HTTPException(status_code=403, detail="Trace API disabled in orchestration-only mode")
    if kind not in ("input", "output"):
        raise HTTPException(status_code=400, detail="kind must be 'input' or 'output'")
    url = f"{ORCHESTRATOR_URL}/api/traces/{quote(request_id)}/steps/{quote(step_path)}/{kind}"
    async with httpx.AsyncClient(timeout=ORCH_TIMEOUT) as client:
        r = await client.get(url)
    return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "text/plain"))
