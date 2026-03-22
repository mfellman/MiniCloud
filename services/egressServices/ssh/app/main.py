"""Uitgaande SSH (exec) en SFTP (bestanden over SSH)."""
from __future__ import annotations

import base64
import logging
import os
from io import StringIO
from typing import Annotated, Any, Literal

import paramiko
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

LOG = logging.getLogger("egress.ssh")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_DEFAULT_TIMEOUT = float(os.environ.get("SSH_EGRESS_TIMEOUT_SECONDS", "60"))
_ALLOWED_HOSTS_RAW = os.environ.get("SSH_EGRESS_ALLOWED_HOSTS", "").strip()


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
            detail=f"SSH host not allowed: {h!r} (SSH_EGRESS_ALLOWED_HOSTS)",
        )


def _load_private_key(pem: str) -> paramiko.PKey | None:
    for key_cls in (
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
    ):
        try:
            return key_cls.from_private_key(StringIO(pem))
        except Exception:
            continue
    return None


def _connect_ssh(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    private_key_pem: str | None,
    timeout: int,
) -> paramiko.SSHClient:
    _check_host(host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    pkey = _load_private_key(private_key_pem) if private_key_pem else None
    if private_key_pem and pkey is None:
        raise ValueError("private_key_pem kon niet worden gelezen")
    client.connect(
        host,
        port=port,
        username=username,
        password=password if pkey is None else None,
        pkey=pkey,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


app = FastAPI(title="MiniCloud egress SSH / SFTP", version="0.1.0")


class SshBody(BaseModel):
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_pem: str | None = Field(
        default=None,
        description="Optioneel PEM private key (RSA/Ed25519). Als gezet, heeft dit voorrang op password.",
    )
    command: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT, ge=5, le=600)


class SftpBody(BaseModel):
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_pem: str | None = None
    action: Literal["list", "retrieve", "fetch", "store", "delete"] = "list"
    remote_path: str = Field(
        default=".",
        description="Bestand of map: voor list een map, voor retrieve/store/delete een pad.",
    )
    data: str | None = Field(default=None, description="Voor store: UTF-8-tekst.")
    data_base64: str | None = Field(
        default=None,
        description="Voor store: binaire inhoud als base64.",
    )
    timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT, ge=5, le=600)


def _do_ssh(body: SshBody) -> dict:
    timeout = int(body.timeout_seconds)
    client = _connect_ssh(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        private_key_pem=body.private_key_pem,
        timeout=timeout,
    )
    try:
        _stdin, stdout, stderr = client.exec_command(
            body.command, timeout=timeout
        )
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_status = stdout.channel.recv_exit_status()
        return {
            "ok": exit_status == 0,
            "exit_status": exit_status,
            "stdout": out,
            "stderr": err,
        }
    finally:
        client.close()


def _do_sftp(body: SftpBody) -> dict:
    timeout = int(body.timeout_seconds)
    client = _connect_ssh(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        private_key_pem=body.private_key_pem,
        timeout=timeout,
    )
    try:
        sftp = client.open_sftp()
        try:
            if body.action == "list":
                path = body.remote_path or "."
                entries: list[dict[str, Any]] = []
                for attr in sftp.listdir_attr(path):
                    entries.append(
                        {
                            "filename": attr.filename,
                            "size": int(attr.st_size),
                            "mode": int(attr.st_mode),
                        }
                    )
                return {"ok": True, "action": "list", "path": path, "entries": entries}
            if body.action in ("retrieve", "fetch"):
                with sftp.open(body.remote_path, "rb") as rf:
                    raw = rf.read()
                act = "fetch" if body.action == "fetch" else "retrieve"
                return {
                    "ok": True,
                    "action": act,
                    "remote_path": body.remote_path,
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                    "size": len(raw),
                }
            if body.action == "store":
                if body.data_base64 is not None:
                    raw = base64.b64decode(body.data_base64)
                elif body.data is not None:
                    raw = body.data.encode("utf-8")
                else:
                    raise ValueError("store vereist data of data_base64")
                with sftp.open(body.remote_path, "wb") as wf:
                    wf.write(raw)
                return {
                    "ok": True,
                    "action": "store",
                    "remote_path": body.remote_path,
                    "size": len(raw),
                }
            if body.action == "delete":
                sftp.remove(body.remote_path)
                return {
                    "ok": True,
                    "action": "delete",
                    "remote_path": body.remote_path,
                }
            raise ValueError(f"Onbekende action: {body.action!r}")
        finally:
            sftp.close()
    finally:
        client.close()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/exec")
async def ssh_exec(
    body: SshBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict:
    rid = x_request_id or "-"
    LOG.info("ssh exec host=%s port=%s request_id=%s", body.host, body.port, rid)
    try:
        return await run_in_threadpool(_do_ssh, body)
    except HTTPException:
        raise
    except paramiko.SSHException as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        LOG.exception("ssh failed request_id=%s", rid)
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/sftp")
async def sftp_op(
    body: SftpBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict:
    rid = x_request_id or "-"
    LOG.info(
        "sftp %s %s:%s %s request_id=%s",
        body.action,
        body.host,
        body.port,
        body.remote_path,
        rid,
    )
    try:
        return await run_in_threadpool(_do_sftp, body)
    except HTTPException:
        raise
    except (paramiko.SSHException, OSError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        LOG.exception("sftp failed request_id=%s", rid)
        raise HTTPException(status_code=502, detail=str(e)) from e
