"""Shared pytest fixtures: MiniCloud services under `services/<path>/app` as package `app`."""
from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[1]

# On conftest import (before first orchestrator app.main import).
os.environ.setdefault(
    "WORKFLOWS_DIR",
    str(REPO_ROOT / "workflows"),
)
os.environ.setdefault(
    "CONNECTIONS_DIR",
    str(REPO_ROOT / "connections"),
)
os.environ.setdefault(
    "TRACES_DIR",
    str(REPO_ROOT / ".tmp" / "traces"),
)

# Keep exactly one service root on sys.path; otherwise the wrong `app.main` wins.
_ACTIVE_SERVICE_ROOTS: list[str] = []


def service_dir(relative: str) -> Path:
    return REPO_ROOT / "services" / relative


def _clear_app_package() -> None:
    to_del = [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]
    for k in to_del:
        del sys.modules[k]


def load_fastapi_app(relative_service_path: str) -> FastAPI:
    """
    Load `app.main:app` from one service. Clears package `app` to avoid conflicts
    between services (each uses `app.main`).
    """
    global _ACTIVE_SERVICE_ROOTS  # noqa: PLW0603
    root = service_dir(relative_service_path)
    if not (root / "app" / "main.py").is_file():
        raise FileNotFoundError(f"No app/main.py under {root}")
    _clear_app_package()
    for p in _ACTIVE_SERVICE_ROOTS:
        if p in sys.path:
            sys.path.remove(p)
    _ACTIVE_SERVICE_ROOTS.clear()
    svc_root = str(root.resolve())
    sys.path.insert(0, svc_root)
    _ACTIVE_SERVICE_ROOTS.append(svc_root)
    mod = importlib.import_module("app.main")
    return mod.app


@pytest.fixture
def transformers_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("transformers")


@pytest.fixture
def gateway_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("gateway")


@pytest.fixture
def orchestrator_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("orchestrator")


@pytest.fixture
def egress_http_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("egressServices/http")


@pytest.fixture
def egress_ftp_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("egressServices/ftp")


@pytest.fixture
def egress_ssh_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("egressServices/ssh")


@pytest.fixture
def egress_rabbitmq_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("egressServices/rabbitmq")


@pytest.fixture
def dashboard_app() -> Iterator[FastAPI]:
    yield load_fastapi_app("dashboard")


@pytest.fixture
def orchestrator_workflows_dir() -> Path:
    return REPO_ROOT / "workflows"


def load_workflow_runner_standalone():
    """
    Load `workflow_runner` as a standalone module (no `app` package) so another
    service can be loaded as `app` in parallel (e.g. transformers for ASGITransport).
    """
    import importlib.util

    path = REPO_ROOT / "services" / "orchestrator" / "app" / "workflow_runner.py"
    spec = importlib.util.spec_from_file_location(
        "minicloud_workflow_runner_standalone",
        path,
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    _ns = {k: v for k, v in vars(mod).items() if not k.startswith("_")}
    mod.WorkflowDoc.model_rebuild(_types_namespace=_ns)
    return mod
