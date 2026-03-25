"""MiniCloud Dashboard — lightweight FastAPI backend.

Proxies workflow + trace data from the orchestrator and serves the
single-page dashboard frontend.
"""
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

LOG = logging.getLogger("dashboard")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8083").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("DASH_TIMEOUT_SECONDS", "30"))

app = FastAPI(title="MiniCloud Dashboard", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


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


# ---------------------------------------------------------------------------
# Serve SPA
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# Mount static assets (CSS/JS) — keep this AFTER specific routes
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
