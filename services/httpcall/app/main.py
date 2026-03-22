import logging
import os
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl, field_validator

LOG = logging.getLogger("httpcall")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_TIMEOUT = float(os.environ.get("HTTP_CALL_TIMEOUT_SECONDS", "60"))
MAX_RESPONSE_BYTES = int(os.environ.get("HTTP_CALL_MAX_RESPONSE_BYTES", str(10 * 1024 * 1024)))
# Leeg = geen restrictie; anders comma-gescheiden hostnamen (geen SSRF naar willekeurige hosts)
_ALLOWED_HOSTS_RAW = os.environ.get("HTTP_ALLOWED_HOSTS", "").strip()


def _allowed_hosts() -> set[str] | None:
    if not _ALLOWED_HOSTS_RAW:
        return None
    return {h.strip().lower() for h in _ALLOWED_HOSTS_RAW.split(",") if h.strip()}


def _check_host_allowed(url: str) -> None:
    allowed = _allowed_hosts()
    if allowed is None:
        return
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Host not allowed: {host!r} (configure HTTP_ALLOWED_HOSTS)",
        )


app = FastAPI(title="MiniCloud HTTP call", version="0.1.0")


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class CallBody(BaseModel):
    method: str = "GET"
    url: HttpUrl
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT, ge=1, le=300)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, v: str) -> str:
        u = v.upper()
        if u not in _METHODS:
            raise ValueError(f"Unsupported HTTP method: {v!r}")
        return u


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/call")
async def do_call(
    body: CallBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict:
    rid = x_request_id or "-"
    url_s = str(body.url)
    _check_host_allowed(url_s)
    method = body.method
    LOG.info("http %s %s request_id=%s", method, url_s, rid)
    try:
        async with httpx.AsyncClient(timeout=body.timeout_seconds) as client:
            r = await client.request(
                method,
                url_s,
                headers=body.headers,
                content=body.body.encode("utf-8") if body.body is not None else None,
            )
    except httpx.TimeoutException as e:
        LOG.warning("timeout request_id=%s: %s", rid, e)
        raise HTTPException(status_code=504, detail="Downstream HTTP timeout") from e
    except httpx.RequestError as e:
        LOG.warning("request error request_id=%s: %s", rid, e)
        raise HTTPException(status_code=502, detail=f"HTTP request failed: {e}") from e

    raw = r.content
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=502,
            detail=f"Response too large ({len(raw)} bytes, max {MAX_RESPONSE_BYTES})",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in ("transfer-encoding",)}
    return {
        "status_code": r.status_code,
        "headers": out_headers,
        "body": text,
    }
