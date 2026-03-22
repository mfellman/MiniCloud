from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

LOG = logging.getLogger("workflow")


class HttpRequestConfig(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body_from: str = Field(
        ...,
        description="initial | previous | <step_id>",
    )
    timeout_seconds: float = Field(default=60, ge=1, le=300)


class XsltWorkflowStep(BaseModel):
    type: Literal["xslt"] = "xslt"
    id: str
    xslt: str = Field(..., min_length=1)
    input_from: str | None = Field(
        default=None,
        description="initial | previous | <step_id>",
    )


class HttpWorkflowStep(BaseModel):
    type: Literal["http"] = "http"
    id: str
    http: HttpRequestConfig


class FtpRequestConfig(BaseModel):
    protocol: Literal["ftp", "ftps"] = "ftp"
    host: str = Field(..., min_length=1)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = ""
    password: str = ""
    action: Literal[
        "list", "retrieve", "fetch", "store", "delete", "nlst"
    ] = "list"
    remote_path: str = "/"
    body_from: str | None = Field(
        default=None,
        description="Voor store: initial | previous | <step_id>",
    )
    body_encoding: Literal["utf8", "base64"] = "utf8"
    timeout_seconds: float = Field(default=60, ge=5, le=600)

    @model_validator(mode="after")
    def store_requires_body_from(self) -> FtpRequestConfig:
        if self.action == "store" and self.body_from is None:
            raise ValueError("ftp: bij action store is body_from verplicht")
        return self


class FtpWorkflowStep(BaseModel):
    type: Literal["ftp"] = "ftp"
    id: str
    ftp: FtpRequestConfig


class SshRequestConfig(BaseModel):
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_from: str | None = Field(
        default=None,
        description="PEM-key: initial | previous | <step_id>",
    )
    command: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=60, ge=5, le=600)


class SshWorkflowStep(BaseModel):
    type: Literal["ssh"] = "ssh"
    id: str
    ssh: SshRequestConfig


class SftpRequestConfig(BaseModel):
    host: str = Field(..., min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1)
    password: str | None = None
    private_key_from: str | None = Field(
        default=None,
        description="PEM-key: initial | previous | <step_id>",
    )
    action: Literal["list", "retrieve", "fetch", "store", "delete"] = "list"
    remote_path: str = Field(default=".", description="Bestand of map.")
    body_from: str | None = Field(
        default=None,
        description="Voor store: initial | previous | <step_id>",
    )
    body_encoding: Literal["utf8", "base64"] = "utf8"
    timeout_seconds: float = Field(default=60, ge=5, le=600)

    @model_validator(mode="after")
    def store_requires_body_from(self) -> SftpRequestConfig:
        if self.action == "store" and self.body_from is None:
            raise ValueError("sftp: bij action store is body_from verplicht")
        return self


class SftpWorkflowStep(BaseModel):
    type: Literal["sftp"] = "sftp"
    id: str
    sftp: SftpRequestConfig


class Xml2JsonWorkflowStep(BaseModel):
    type: Literal["xml2json"] = "xml2json"
    id: str
    input_from: str | None = Field(
        default=None,
        description="XML-bron: initial | previous | <step_id>",
    )


class Json2XmlWorkflowStep(BaseModel):
    type: Literal["json2xml"] = "json2xml"
    id: str
    input_from: str | None = Field(
        default=None,
        description="JSON-string: initial | previous | <step_id>",
    )


class LiquidWorkflowStep(BaseModel):
    type: Literal["liquid"] = "liquid"
    id: str
    template: str = Field(..., min_length=1, description="Liquid-sjabloon")
    input_from: str | None = Field(
        default=None,
        description="JSON-context string: initial | previous | <step_id>",
    )


WorkflowStep = Annotated[
    Union[
        XsltWorkflowStep,
        HttpWorkflowStep,
        FtpWorkflowStep,
        SshWorkflowStep,
        SftpWorkflowStep,
        Xml2JsonWorkflowStep,
        Json2XmlWorkflowStep,
        LiquidWorkflowStep,
    ],
    Field(discriminator="type"),
]


class InvocationConfig(BaseModel):
    """Wie mag deze workflow aanroepen (gateway/HTTP vs. geplande runner)."""

    allow_http: bool = True
    allow_schedule: bool = False


class WorkflowDoc(BaseModel):
    name: str
    invocation: InvocationConfig = Field(default_factory=InvocationConfig)
    steps: list[WorkflowStep]

    @model_validator(mode="after")
    def unique_step_ids(self) -> WorkflowDoc:
        seen: set[str] = set()
        for s in self.steps:
            if s.id in seen:
                raise ValueError(f"duplicate step id: {s.id!r}")
            seen.add(s.id)
        return self


def load_workflows(directory: Path) -> dict[str, WorkflowDoc]:
    out: dict[str, WorkflowDoc] = {}
    if not directory.is_dir():
        LOG.warning("workflows directory does not exist: %s", directory)
        return out

    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            LOG.error("failed to read %s: %s", path, e)
            continue
        if not raw:
            continue
        try:
            doc = WorkflowDoc.model_validate(raw)
        except ValidationError as e:
            LOG.error("invalid workflow %s: %s", path, e)
            continue
        if doc.name in out:
            LOG.warning("skipping duplicate workflow name %r (%s)", doc.name, path)
            continue
        out[doc.name] = doc
        LOG.info("loaded workflow %r from %s", doc.name, path)
    return out


def _resolve_input(
    ref: str | None,
    *,
    initial: str,
    previous: str,
    outputs: dict[str, str],
    step_index: int,
) -> str:
    if ref is None:
        return initial if step_index == 0 else previous
    if ref == "initial":
        return initial
    if ref == "previous":
        return previous
    if ref not in outputs:
        raise ValueError(f"unknown step id in input_from/body_from: {ref!r}")
    return outputs[ref]


async def run_workflow(
    doc: WorkflowDoc,
    initial_xml: str,
    *,
    transformers_base_url: str,
    egress_http_url: str,
    egress_ftp_url: str,
    egress_ssh_url: str,
    egress_sftp_url: str,
    request_id: str,
    httpx_client: Any,
) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    outputs: dict[str, str] = {}
    previous = initial_xml
    trace: list[dict[str, Any]] = []

    for idx, step in enumerate(doc.steps):
        if isinstance(step, XsltWorkflowStep):
            inp = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                f"{transformers_base_url.rstrip('/')}/applyXSLT",
                json={"xml": inp, "xslt": step.xslt},
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"xslt step {step.id!r} failed: {detail}")
            body = r.text
            outputs[step.id] = body
            previous = body
            trace.append({"step": step.id, "type": "xslt", "ok": True})
        elif isinstance(step, HttpWorkflowStep):
            spec = step.http
            body = _resolve_input(
                spec.body_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                egress_http_url,
                json={
                    "method": spec.method,
                    "url": spec.url,
                    "headers": spec.headers,
                    "body": body,
                    "timeout_seconds": spec.timeout_seconds,
                },
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"http step {step.id!r} failed (caller): {detail}")
            data = r.json()
            out_code = int(data.get("status_code", 0))
            out_body = str(data.get("body", ""))
            outputs[step.id] = out_body
            previous = out_body
            ok = 200 <= out_code < 300
            trace.append(
                {
                    "step": step.id,
                    "type": "http",
                    "ok": ok,
                    "status_code": out_code,
                },
            )
            if not ok:
                raise RuntimeError(
                    f"http step {step.id!r} returned status {out_code}",
                )
        elif isinstance(step, FtpWorkflowStep):
            spec = step.ftp
            payload: dict[str, Any] = {
                "protocol": spec.protocol,
                "host": spec.host,
                "port": spec.port,
                "username": spec.username,
                "password": spec.password,
                "action": spec.action,
                "remote_path": spec.remote_path,
                "timeout_seconds": spec.timeout_seconds,
            }
            if spec.action == "store":
                raw = _resolve_input(
                    spec.body_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                )
                if spec.body_encoding == "base64":
                    payload["data_base64"] = raw.strip()
                else:
                    payload["data"] = raw
            r = await httpx_client.post(
                egress_ftp_url,
                json=payload,
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"ftp step {step.id!r} failed: {detail}")
            ftp_data = r.json()
            out_body = json.dumps(ftp_data, ensure_ascii=False)
            outputs[step.id] = out_body
            previous = out_body
            trace.append({"step": step.id, "type": "ftp", "ok": True})
        elif isinstance(step, SshWorkflowStep):
            spec = step.ssh
            payload = {
                "host": spec.host,
                "port": spec.port,
                "username": spec.username,
                "password": spec.password,
                "command": spec.command,
                "timeout_seconds": spec.timeout_seconds,
            }
            if spec.private_key_from is not None:
                pem = _resolve_input(
                    spec.private_key_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                )
                payload["private_key_pem"] = pem
            r = await httpx_client.post(
                egress_ssh_url,
                json=payload,
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"ssh step {step.id!r} failed: {detail}")
            data = r.json()
            out_body = json.dumps(data, ensure_ascii=False)
            outputs[step.id] = out_body
            previous = out_body
            ok = bool(data.get("ok", False))
            trace.append({"step": step.id, "type": "ssh", "ok": ok})
            if not ok:
                raise RuntimeError(
                    f"ssh step {step.id!r} exit_status={data.get('exit_status')}",
                )
        elif isinstance(step, SftpWorkflowStep):
            spec = step.sftp
            payload: dict[str, Any] = {
                "host": spec.host,
                "port": spec.port,
                "username": spec.username,
                "password": spec.password,
                "action": spec.action,
                "remote_path": spec.remote_path,
                "timeout_seconds": spec.timeout_seconds,
            }
            if spec.private_key_from is not None:
                pem = _resolve_input(
                    spec.private_key_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                )
                payload["private_key_pem"] = pem
            if spec.action == "store":
                raw = _resolve_input(
                    spec.body_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                )
                if spec.body_encoding == "base64":
                    payload["data_base64"] = raw.strip()
                else:
                    payload["data"] = raw
            r = await httpx_client.post(
                egress_sftp_url,
                json=payload,
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"sftp step {step.id!r} failed: {detail}")
            sftp_data = r.json()
            out_body = json.dumps(sftp_data, ensure_ascii=False)
            outputs[step.id] = out_body
            previous = out_body
            trace.append({"step": step.id, "type": "sftp", "ok": True})
        elif isinstance(step, Xml2JsonWorkflowStep):
            inp = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                f"{transformers_base_url.rstrip('/')}/xml2json",
                json={"xml": inp},
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"xml2json step {step.id!r} failed: {detail}")
            body = r.text
            outputs[step.id] = body
            previous = body
            trace.append({"step": step.id, "type": "xml2json", "ok": True})
        elif isinstance(step, Json2XmlWorkflowStep):
            inp = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                f"{transformers_base_url.rstrip('/')}/json2xml",
                json={"json": inp},
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"json2xml step {step.id!r} failed: {detail}")
            body = r.text
            outputs[step.id] = body
            previous = body
            trace.append({"step": step.id, "type": "json2xml", "ok": True})
        elif isinstance(step, LiquidWorkflowStep):
            ctx_s = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                f"{transformers_base_url.rstrip('/')}/applyLiquid",
                json={"template": step.template, "json": ctx_s},
                headers={"X-Request-ID": request_id, "Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                raise RuntimeError(f"liquid step {step.id!r} failed: {detail}")
            body = r.text
            outputs[step.id] = body
            previous = body
            trace.append({"step": step.id, "type": "liquid", "ok": True})
        else:
            raise RuntimeError(f"unsupported step type: {type(step)!r}")

    return previous, outputs, trace
