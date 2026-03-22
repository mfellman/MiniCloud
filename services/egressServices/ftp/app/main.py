"""Uitgaande FTP en FTPS (expliciet TLS)."""
from __future__ import annotations

import base64
import io
import logging
import os
from ftplib import FTP, FTP_TLS, error_perm
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

LOG = logging.getLogger("egress.ftp")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_DEFAULT_TIMEOUT = float(os.environ.get("FTP_EGRESS_TIMEOUT_SECONDS", "60"))
_ALLOWED_HOSTS_RAW = os.environ.get("FTP_EGRESS_ALLOWED_HOSTS", "").strip()


def _allowed_hosts() -> set[str] | None:
    if not _ALLOWED_HOSTS_RAW:
        return None
    return {h.strip().lower() for h in _ALLOWED_HOSTS_RAW.split(",") if h.strip()}


def _check_host(host: str) -> None:
    allowed = _allowed_hosts()
    if allowed is None:
        return
    h = host.strip().lower()
    if h not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"FTP host not allowed: {h!r} (FTP_EGRESS_ALLOWED_HOSTS)",
        )


app = FastAPI(title="MiniCloud egress FTP/FTPS", version="0.1.0")


class FtpBody(BaseModel):
    protocol: Literal["ftp", "ftps"] = "ftp"
    host: str = Field(..., min_length=1)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = ""
    password: str = ""
    action: Literal[
        "list", "retrieve", "fetch", "store", "delete", "nlst"
    ] = "list"
    remote_path: str = "/"
    data: str | None = Field(
        default=None,
        description="Voor store: tekstinhoud (UTF-8).",
    )
    data_base64: str | None = Field(
        default=None,
        description="Voor store: binaire inhoud als base64 (alternatief voor data).",
    )
    timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT, ge=5, le=600)


def _ftp_connect(body: FtpBody):
    _check_host(body.host)
    timeout = int(body.timeout_seconds)
    if body.protocol == "ftps":
        ft = FTP_TLS()
        ft.connect(body.host, body.port, timeout=timeout)
        ft.login(body.username, body.password)
        ft.prot_p()
    else:
        ft = FTP()
        ft.connect(body.host, body.port, timeout=timeout)
        ft.login(body.username, body.password)
    return ft


def _do_ftp(body: FtpBody) -> dict:
    ft = _ftp_connect(body)
    try:
        if body.action == "list":
            lines: list[str] = []
            ft.retrlines(f"LIST {body.remote_path}", lines.append)
            return {"ok": True, "action": "list", "lines": lines}
        if body.action == "nlst":
            names = ft.nlst(body.remote_path)
            return {"ok": True, "action": "nlst", "names": names}
        if body.action in ("retrieve", "fetch"):
            buf = io.BytesIO()

            def _cb(chunk: bytes) -> None:
                buf.write(chunk)

            ft.retrbinary(f"RETR {body.remote_path}", _cb)
            raw = buf.getvalue()
            act = "fetch" if body.action == "fetch" else "retrieve"
            return {
                "ok": True,
                "action": act,
                "content_base64": base64.b64encode(raw).decode("ascii"),
                "size": len(raw),
            }
        if body.action == "store":
            if body.data_base64 is not None:
                raw = base64.b64decode(body.data_base64)
                bio = io.BytesIO(raw)
            elif body.data is not None:
                bio = io.BytesIO(body.data.encode("utf-8"))
            else:
                raise ValueError("store vereist data of data_base64")
            ft.storbinary(f"STOR {body.remote_path}", bio)
            return {"ok": True, "action": "store", "remote_path": body.remote_path}
        if body.action == "delete":
            ft.delete(body.remote_path)
            return {"ok": True, "action": "delete", "remote_path": body.remote_path}
        raise ValueError(f"Onbekende action: {body.action!r}")
    except error_perm as e:
        raise HTTPException(status_code=502, detail=f"FTP: {e}") from e
    finally:
        try:
            ft.quit()
        except Exception:
            try:
                ft.close()
            except Exception:
                pass


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/ftp")
async def ftp_op(
    body: FtpBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict:
    rid = x_request_id or "-"
    LOG.info(
        "ftp %s %s:%s %s request_id=%s",
        body.protocol,
        body.host,
        body.port,
        body.action,
        rid,
    )
    try:
        return await run_in_threadpool(_do_ftp, body)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        LOG.exception("ftp failed request_id=%s", rid)
        raise HTTPException(status_code=502, detail=str(e)) from e
