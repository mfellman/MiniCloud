from __future__ import annotations

import logging
import os
import json
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from app.connections import (
    FtpConnection,
    HttpConnection,
    RabbitMqConnection,
    SftpConnection,
    SshConnection,
    load_connections,
)
from app.workflow_runner import WorkflowDoc, load_workflows

LOG = logging.getLogger("orchestrator.runtime_store")


class RuntimeStore:
    """Abstraction for loading workflow and connection definitions."""

    def load_workflows(self) -> dict[str, WorkflowDoc]:
        raise NotImplementedError

    def load_connections(self) -> dict[str, Any]:
        raise NotImplementedError


class FileRuntimeStore(RuntimeStore):
    def __init__(self, workflows_dir: Path, connections_dir: Path):
        self.workflows_dir = workflows_dir
        self.connections_dir = connections_dir

    def load_workflows(self) -> dict[str, WorkflowDoc]:
        return load_workflows(self.workflows_dir)

    def load_connections(self) -> dict[str, Any]:
        return load_connections(self.connections_dir)


class HttpRuntimeStore(RuntimeStore):
    """Load runtime definitions from an external storage service.

    Expected endpoints:
      - GET {base}/internal/workflows
      - GET {base}/internal/connections

    Response shapes:
      - {"workflows": [{"name": ..., "group": ..., "invocation": ..., "steps": [...]}]}
      - {"connections": [{... connection document ...}]}
    """

    def __init__(self, base_url: str, timeout_seconds: float = 10.0, bearer_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.bearer_token = bearer_token.strip()

    def _get_json(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urlrequest.Request(url, method="GET")
        if self.bearer_token:
            req.add_header("Authorization", f"Bearer {self.bearer_token}")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    raise RuntimeError(f"storage request failed with status {status}: {url}")
                raw = resp.read().decode("utf-8")
        except urlerror.URLError as exc:
            raise RuntimeError(f"storage request failed: {url}: {exc}") from exc

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError(f"storage response must be an object: {url}")
        return data

    def load_workflows(self) -> dict[str, WorkflowDoc]:
        payload = self._get_json("/internal/workflows")
        rows = payload.get("workflows")
        if not isinstance(rows, list):
            raise RuntimeError("storage workflows payload must include a workflows list")

        out: dict[str, WorkflowDoc] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("workflow entry must be an object")
            doc = WorkflowDoc.model_validate(row)
            out[doc.name] = doc
        return out

    def load_connections(self) -> dict[str, Any]:
        payload = self._get_json("/internal/connections")
        rows = payload.get("connections")
        if not isinstance(rows, list):
            raise RuntimeError("storage connections payload must include a connections list")

        out: dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("connection entry must be an object")
            t = row.get("type")
            if t == "http":
                doc = HttpConnection.model_validate(row)
            elif t == "ftp":
                doc = FtpConnection.model_validate(row)
            elif t == "ssh":
                doc = SshConnection.model_validate(row)
            elif t == "sftp":
                doc = SftpConnection.model_validate(row)
            elif t == "rabbitmq":
                doc = RabbitMqConnection.model_validate(row)
            else:
                raise RuntimeError(f"unknown connection type from storage: {t!r}")
            out[doc.name] = doc
        return out


def build_runtime_store(*, workflows_dir: Path, connections_dir: Path) -> RuntimeStore:
    """Select runtime store backend.

    ORCH_RUNTIME_STORE=file (default) reads from mounted directories.
    ORCH_RUNTIME_STORE=http reads from STORAGE_SERVICE_URL.
    """
    backend = os.environ.get("ORCH_RUNTIME_STORE", "file").strip().lower()
    if backend == "file":
        return FileRuntimeStore(workflows_dir=workflows_dir, connections_dir=connections_dir)

    if backend == "http":
        base_url = os.environ.get("STORAGE_SERVICE_URL", "").strip()
        if not base_url:
            raise RuntimeError("ORCH_RUNTIME_STORE=http requires STORAGE_SERVICE_URL")
        timeout_seconds = float(os.environ.get("ORCH_STORAGE_TIMEOUT_SECONDS", "10"))
        bearer_token = os.environ.get("ORCH_STORAGE_BEARER_TOKEN", "")
        return HttpRuntimeStore(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            bearer_token=bearer_token,
        )

    raise RuntimeError(
        f"Unknown ORCH_RUNTIME_STORE={backend!r}; supported: file, http"
    )
