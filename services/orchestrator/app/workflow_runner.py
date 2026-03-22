from __future__ import annotations

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


WorkflowStep = Annotated[
    Union[XsltWorkflowStep, HttpWorkflowStep],
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
    xslt_apply_url: str,
    http_call_url: str,
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
                xslt_apply_url,
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
        else:
            assert isinstance(step, HttpWorkflowStep)
            spec = step.http
            body = _resolve_input(
                spec.body_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
            )
            r = await httpx_client.post(
                http_call_url,
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

    return previous, outputs, trace
