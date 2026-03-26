from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib import error as urlerror
from urllib import request as urlrequest

import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

LOG = logging.getLogger("storage")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DATA_DIR = Path(os.environ.get("STORAGE_DATA_DIR", "/app/data"))
KV_DIR = DATA_DIR / "kv"
RUNTIME_WORKFLOWS_DIR = DATA_DIR / "runtime" / "workflows"
RUNTIME_CONNECTIONS_DIR = DATA_DIR / "runtime" / "connections"

READ_TOKEN = os.environ.get("STORAGE_SERVICE_READ_TOKEN", "").strip()
WRITE_TOKEN = os.environ.get("STORAGE_SERVICE_WRITE_TOKEN", "").strip()
ADMIN_TOKEN = os.environ.get("STORAGE_SERVICE_ADMIN_TOKEN", "").strip()
ACL_ENABLED = os.environ.get("STORAGE_ACL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_ROLE = os.environ.get("STORAGE_DEFAULT_ROLE", "orchestrator").strip()
ACL_READ_ROLES_RAW = os.environ.get("STORAGE_ACL_READ_ROLES", "orchestrator").strip()
ACL_WRITE_ROLES_RAW = os.environ.get("STORAGE_ACL_WRITE_ROLES", "orchestrator").strip()
ACL_BUCKET_OVERRIDES_RAW = os.environ.get("STORAGE_ACL_BUCKET_OVERRIDES", "").strip()
ACL_POLICY_RAW = os.environ.get("STORAGE_ACL_POLICY", "").strip()

STORAGE_EVENT_ENABLED = os.environ.get("STORAGE_EVENT_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
STORAGE_EVENT_REQUIRED = os.environ.get("STORAGE_EVENT_REQUIRED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
STORAGE_EVENT_TIMEOUT_SECONDS = float(os.environ.get("STORAGE_EVENT_TIMEOUT_SECONDS", "5"))
STORAGE_EVENT_EGRESS_URL = os.environ.get(
    "STORAGE_EVENT_EGRESS_URL",
    "http://egress-rabbitmq:8080/publish",
).strip()
STORAGE_EVENT_DOMAIN = os.environ.get("STORAGE_EVENT_DOMAIN", "Storage").strip() or "Storage"
STORAGE_EVENT_SERVICE = os.environ.get("STORAGE_EVENT_SERVICE", "KV").strip() or "KV"
STORAGE_EVENT_ACTION = os.environ.get("STORAGE_EVENT_ACTION", "Updated").strip() or "Updated"
STORAGE_EVENT_VERSION = os.environ.get("STORAGE_EVENT_VERSION", "1").strip() or "1"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")

app = FastAPI(title="MiniCloud Storage", version="0.1.0")


def _load_acl_policy() -> dict[str, Any]:
    def _split_roles(raw: str) -> list[str]:
        if not raw:
            return []
        out: list[str] = []
        for p in raw.split(","):
            v = p.strip()
            if v:
                out.append(v)
        return out

    base: dict[str, Any] = {
        "default": {
            "read_roles": _split_roles(ACL_READ_ROLES_RAW),
            "write_roles": _split_roles(ACL_WRITE_ROLES_RAW),
        },
        "buckets": {},
    }

    if ACL_BUCKET_OVERRIDES_RAW:
        try:
            parsed_overrides = json.loads(ACL_BUCKET_OVERRIDES_RAW)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid STORAGE_ACL_BUCKET_OVERRIDES JSON: {exc}") from exc
        if not isinstance(parsed_overrides, dict):
            raise RuntimeError("STORAGE_ACL_BUCKET_OVERRIDES must be a JSON object")
        base["buckets"] = parsed_overrides

    if not ACL_POLICY_RAW:
        return base
    try:
        parsed = json.loads(ACL_POLICY_RAW)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid STORAGE_ACL_POLICY JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("STORAGE_ACL_POLICY must be a JSON object")
    default_cfg = parsed.get("default", {})
    buckets_cfg = parsed.get("buckets", {})
    if not isinstance(default_cfg, dict) or not isinstance(buckets_cfg, dict):
        raise RuntimeError("STORAGE_ACL_POLICY requires object fields: default, buckets")
    return {"default": default_cfg, "buckets": buckets_cfg}


ACL_POLICY = _load_acl_policy()


def _ensure_dirs() -> None:
    KV_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)


def _check_bearer_or_raise(authorization: str | None, secret: str, *, realm: str) -> None:
    if not secret:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=f"Missing Bearer token for {realm}")
    token = authorization.removeprefix("Bearer ").strip()
    if token != secret:
        raise HTTPException(status_code=403, detail=f"Invalid Bearer token for {realm}")


def _extract_roles(roles_header: str | None) -> set[str]:
    roles: set[str] = set()
    if roles_header:
        for p in roles_header.split(","):
            v = p.strip()
            if v:
                roles.add(v)
    if not roles and DEFAULT_ROLE:
        roles.add(DEFAULT_ROLE)
    return roles


def _allowed_roles_for(bucket: str, action: str) -> set[str]:
    action_key = f"{action}_roles"
    buckets = ACL_POLICY.get("buckets", {})
    bucket_cfg = buckets.get(bucket)
    if isinstance(bucket_cfg, dict) and action_key in bucket_cfg:
        src = bucket_cfg[action_key]
    else:
        src = ACL_POLICY.get("default", {}).get(action_key, [])
    if not isinstance(src, list):
        return set()
    return {str(x).strip() for x in src if str(x).strip()}


def _enforce_acl(bucket: str, action: str, roles_header: str | None) -> None:
    if not ACL_ENABLED:
        return
    roles = _extract_roles(roles_header)
    allowed = _allowed_roles_for(bucket, action)
    if "*" in allowed:
        return
    if roles & allowed:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"Access denied for bucket {bucket!r} action {action!r}. "
            f"Caller roles: {sorted(roles)} allowed: {sorted(allowed)}"
        ),
    )


def _validate_name(name: str, *, field: str) -> str:
    if not _SAFE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {name!r}")
    return name


def _normalize_key(key: str) -> str:
    parts = [p for p in key.split("/") if p and p not in (".", "..")]
    if not parts:
        raise HTTPException(status_code=400, detail="Invalid key")
    return "/".join(parts)


def _kv_path(bucket: str, key: str) -> Path:
    safe_bucket = _validate_name(bucket, field="bucket")
    safe_key = _normalize_key(key)
    full = KV_DIR / safe_bucket / f"{safe_key}.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    return full


def _kv_bucket_dir(bucket: str) -> Path:
    safe_bucket = _validate_name(bucket, field="bucket")
    return KV_DIR / safe_bucket


def _runtime_path(kind: str, name: str) -> Path:
    safe_name = _validate_name(name, field="name")
    if kind == "workflow":
        base = RUNTIME_WORKFLOWS_DIR
    elif kind == "connection":
        base = RUNTIME_CONNECTIONS_DIR
    else:
        raise RuntimeError(f"Unsupported runtime kind: {kind!r}")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{safe_name}.json"


def _has_acl_access(bucket: str, action: str, roles_header: str | None) -> bool:
    if not ACL_ENABLED:
        return True
    roles = _extract_roles(roles_header)
    allowed = _allowed_roles_for(bucket, action)
    if "*" in allowed:
        return True
    return bool(roles & allowed)


def _normalize_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    parts = [p for p in prefix.split("/") if p and p not in (".", "..")]
    return "/".join(parts)


def _list_json_documents(directory: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for p in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("Skipping invalid JSON file %s: %s", p, exc)
            continue
        if isinstance(raw, dict):
            docs.append(raw)
    return docs


def _publish_storage_event(payload: dict[str, Any]) -> bool:
    if not STORAGE_EVENT_ENABLED:
        return False

    event_body = {
        "event_type": "storage.write",
        "bucket": payload.get("bucket"),
        "key": payload.get("key"),
        "content_type": payload.get("content_type"),
        "updated_at": payload.get("updated_at"),
        "value": payload.get("value"),
    }
    post_body = {
        "message": json.dumps(event_body, ensure_ascii=False),
        "properties": {
            "Domain": STORAGE_EVENT_DOMAIN,
            "Service": STORAGE_EVENT_SERVICE,
            "Action": STORAGE_EVENT_ACTION,
            "Version": STORAGE_EVENT_VERSION,
            "Bucket": str(payload.get("bucket", "")),
            "Key": str(payload.get("key", "")),
        },
        "headers": {
            "source": "storage",
        },
        "content_type": "application/json",
        "persistent": True,
    }

    req = urlrequest.Request(
        STORAGE_EVENT_EGRESS_URL,
        data=json.dumps(post_body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=STORAGE_EVENT_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise RuntimeError(f"event publish returned status {status}")
    except (urlerror.URLError, RuntimeError) as exc:
        if STORAGE_EVENT_REQUIRED:
            raise RuntimeError(f"failed to publish storage event: {exc}") from exc
        LOG.warning("storage event publish failed (non-fatal): %s", exc)
        return False
    return True


class StorageWriteBody(BaseModel):
    value: str = Field(default="")
    content_type: str = Field(default="text/plain")


class RuntimeDocumentBody(BaseModel):
    document: dict[str, Any]


@app.on_event("startup")
def startup() -> None:
    _ensure_dirs()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    _ensure_dirs()
    return {"status": "ready"}


@app.get("/v1/storage/{bucket}/{key:path}")
def storage_read(
    bucket: str,
    key: str,
    authorization: Annotated[str | None, Header()] = None,
    x_storage_roles: Annotated[str | None, Header(alias="X-Storage-Roles")] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="storage-read")
    _enforce_acl(bucket, "read", x_storage_roles)
    p = _kv_path(bucket, key)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Storage key not found")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data


@app.get("/v1/storage")
def storage_list_buckets(
    limit: int = 200,
    authorization: Annotated[str | None, Header()] = None,
    x_storage_roles: Annotated[str | None, Header(alias="X-Storage-Roles")] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="storage-read")
    safe_limit = max(1, min(limit, 1000))

    buckets: list[str] = []
    if KV_DIR.exists():
        for child in sorted(KV_DIR.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if not _SAFE_NAME.fullmatch(name):
                continue
            if _has_acl_access(name, "read", x_storage_roles):
                buckets.append(name)
            if len(buckets) >= safe_limit:
                break
    return {"buckets": buckets}


@app.get("/v1/storage/{bucket}")
def storage_list_keys(
    bucket: str,
    prefix: str | None = None,
    limit: int = 200,
    authorization: Annotated[str | None, Header()] = None,
    x_storage_roles: Annotated[str | None, Header(alias="X-Storage-Roles")] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="storage-read")
    _enforce_acl(bucket, "read", x_storage_roles)

    safe_limit = max(1, min(limit, 1000))
    bucket_dir = _kv_bucket_dir(bucket)
    safe_prefix = _normalize_prefix(prefix)

    keys: list[str] = []
    if bucket_dir.exists():
        for p in sorted(bucket_dir.rglob("*.json")):
            rel = p.relative_to(bucket_dir).as_posix()
            if not rel.endswith(".json"):
                continue
            key = rel[:-5]
            if safe_prefix and not key.startswith(safe_prefix):
                continue
            keys.append(key)
            if len(keys) >= safe_limit:
                break

    return {
        "bucket": _validate_name(bucket, field="bucket"),
        "prefix": safe_prefix,
        "keys": keys,
    }


@app.put("/v1/storage/{bucket}/{key:path}")
def storage_write(
    bucket: str,
    key: str,
    body: StorageWriteBody,
    authorization: Annotated[str | None, Header()] = None,
    x_storage_roles: Annotated[str | None, Header(alias="X-Storage-Roles")] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, WRITE_TOKEN, realm="storage-write")
    _enforce_acl(bucket, "write", x_storage_roles)
    p = _kv_path(bucket, key)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "bucket": _validate_name(bucket, field="bucket"),
        "key": _normalize_key(key),
        "value": body.value,
        "content_type": body.content_type,
        "updated_at": now,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        event_published = _publish_storage_event(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "stored", "event_published": event_published, **payload}


@app.get("/internal/workflows")
def list_runtime_workflows(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="runtime-read")
    return {"workflows": _list_json_documents(RUNTIME_WORKFLOWS_DIR)}


@app.get("/internal/connections")
def list_runtime_connections(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="runtime-read")
    return {"connections": _list_json_documents(RUNTIME_CONNECTIONS_DIR)}


@app.put("/internal/workflows/{name}")
def upsert_runtime_workflow(
    name: str,
    body: RuntimeDocumentBody,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    p = _runtime_path("workflow", name)
    doc = dict(body.document)
    doc.setdefault("name", name)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {"status": "upserted", "name": name, "kind": "workflow"}


@app.put("/internal/connections/{name}")
def upsert_runtime_connection(
    name: str,
    body: RuntimeDocumentBody,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    p = _runtime_path("connection", name)
    doc = dict(body.document)
    doc.setdefault("name", name)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {"status": "upserted", "name": name, "kind": "connection"}


@app.post("/internal/bootstrap/from-yaml")
def bootstrap_from_yaml(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    # Optional migration helper: import mounted YAML docs into runtime JSON store.
    src_workflows = Path(os.environ.get("WORKFLOWS_DIR", "/app/workflows"))
    src_connections = Path(os.environ.get("CONNECTIONS_DIR", "/app/connections"))

    wf_count = 0
    for y in sorted(list(src_workflows.glob("*.yaml")) + list(src_workflows.glob("*.yml"))):
        try:
            raw = yaml.safe_load(y.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("Skipping invalid workflow YAML %s: %s", y, exc)
            continue
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        _runtime_path("workflow", str(raw["name"])) .write_text(
            json.dumps(raw, ensure_ascii=False),
            encoding="utf-8",
        )
        wf_count += 1

    conn_count = 0
    for y in sorted(list(src_connections.glob("*.yaml")) + list(src_connections.glob("*.yml"))):
        try:
            raw = yaml.safe_load(y.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("Skipping invalid connection YAML %s: %s", y, exc)
            continue
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        _runtime_path("connection", str(raw["name"])) .write_text(
            json.dumps(raw, ensure_ascii=False),
            encoding="utf-8",
        )
        conn_count += 1

    return {
        "status": "bootstrapped",
        "workflows": wf_count,
        "connections": conn_count,
    }


# ---------------------------------------------------------------------------
# Single-item read
# ---------------------------------------------------------------------------

@app.get("/internal/workflows/{name}")
def get_runtime_workflow(
    name: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="runtime-read")
    p = _runtime_path("workflow", name)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Workflow {name!r} not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/internal/connections/{name}")
def get_runtime_connection(
    name: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, READ_TOKEN, realm="runtime-read")
    p = _runtime_path("connection", name)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Connection {name!r} not found")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# YAML upload  (POST /internal/upload/workflows/{name})
# Accepts raw YAML body (text/x-yaml or application/yaml or text/plain).
# After upload: call POST /admin/reload on orchestrator to hot-reload.
# ---------------------------------------------------------------------------

@app.post("/internal/upload/workflows/{name}")
async def upload_workflow_yaml(
    name: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    _validate_name(name, field="name")
    raw = await request.body()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400, detail="Workflow document must be a YAML mapping")
    doc.setdefault("name", name)
    p = _runtime_path("workflow", name)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    LOG.info("workflow uploaded: name=%r", name)
    return {"status": "uploaded", "name": name, "kind": "workflow"}


@app.post("/internal/upload/connections/{name}")
async def upload_connection_yaml(
    name: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    _validate_name(name, field="name")
    raw = await request.body()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400, detail="Connection document must be a YAML mapping")
    doc.setdefault("name", name)
    p = _runtime_path("connection", name)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    LOG.info("connection uploaded: name=%r", name)
    return {"status": "uploaded", "name": name, "kind": "connection"}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@app.delete("/internal/workflows/{name}")
def delete_runtime_workflow(
    name: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    p = _runtime_path("workflow", name)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Workflow {name!r} not found")
    p.unlink()
    LOG.info("workflow deleted: name=%r", name)
    return {"status": "deleted", "name": name, "kind": "workflow"}


@app.delete("/internal/connections/{name}")
def delete_runtime_connection(
    name: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_bearer_or_raise(authorization, ADMIN_TOKEN, realm="runtime-admin")
    p = _runtime_path("connection", name)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Connection {name!r} not found")
    p.unlink()
    LOG.info("connection deleted: name=%r", name)
    return {"status": "deleted", "name": name, "kind": "connection"}
