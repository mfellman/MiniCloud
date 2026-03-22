"""
Named egress connections: credentials and endpoints live here; workflow steps only
reference connection id + method-specific fields.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

LOG = logging.getLogger("orchestrator.connections")


class HttpConnection(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="http")
    base_url: str = Field(..., min_length=1, description="Prefix for relative paths in steps")
    default_headers: dict[str, str] = Field(default_factory=dict)
    oauth_scope: str | None = Field(
        default=None,
        description="Extra OAuth scope required to use this connection (besides egress:http)",
    )

    @field_validator("type")
    @classmethod
    def type_http(cls, v: str) -> str:
        if v != "http":
            raise ValueError("HttpConnection type must be http")
        return v


class FtpConnection(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="ftp")
    protocol: str = Field(default="ftp")
    host: str = Field(..., min_length=1)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = ""
    password: str = ""
    oauth_scope: str | None = None

    @field_validator("type")
    @classmethod
    def type_ftp(cls, v: str) -> str:
        if v != "ftp":
            raise ValueError("FtpConnection type must be ftp")
        return v

    @field_validator("protocol")
    @classmethod
    def proto(cls, v: str) -> str:
        if v not in ("ftp", "ftps"):
            raise ValueError("protocol must be ftp or ftps")
        return v


class SshConnection(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="ssh")
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_pem: str | None = Field(
        default=None,
        description="Optional inline PEM; prefer mounting a secret file in production",
    )
    oauth_scope: str | None = None

    @field_validator("type")
    @classmethod
    def type_ssh(cls, v: str) -> str:
        if v != "ssh":
            raise ValueError("SshConnection type must be ssh")
        return v


class SftpConnection(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="sftp")
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_pem: str | None = None
    oauth_scope: str | None = None

    @field_validator("type")
    @classmethod
    def type_sftp(cls, v: str) -> str:
        if v != "sftp":
            raise ValueError("SftpConnection type must be sftp")
        return v


def load_connections(directory: Path) -> dict[str, Any]:
    """Load *.yaml / *.yml connection definitions; keyed by name."""
    out: dict[str, Any] = {}
    if not directory.is_dir():
        LOG.warning("connections directory does not exist: %s", directory)
        return out

    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            LOG.error("failed to read connection file %s: %s", path, e)
            continue
        if not raw:
            continue
        try:
            t = raw.get("type", "")
            if t == "http":
                doc = HttpConnection.model_validate(raw)
            elif t == "ftp":
                doc = FtpConnection.model_validate(raw)
            elif t == "ssh":
                doc = SshConnection.model_validate(raw)
            elif t == "sftp":
                doc = SftpConnection.model_validate(raw)
            else:
                LOG.error("unknown connection type in %s: %r", path, t)
                continue
        except ValidationError as e:
            LOG.error("invalid connection %s: %s", path, e)
            continue
        if doc.name in out:
            LOG.warning("skipping duplicate connection name %r (%s)", doc.name, path)
            continue
        out[doc.name] = doc
        LOG.info("loaded connection %r from %s", doc.name, path)
    return out


def resolve_http_url(
    *,
    base_url: str,
    path_or_url: str | None,
    path: str | None,
) -> str:
    """Build final URL from connection base + step path, or use absolute url."""
    if path_or_url and path_or_url.startswith(("http://", "https://")):
        return path_or_url
    suffix = path or path_or_url or "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return base_url.rstrip("/") + suffix
