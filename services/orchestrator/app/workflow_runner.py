from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from lxml import etree
from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator

LOG = logging.getLogger("workflow")


def _load_oauth_enforcement():
    try:
        from app.oauth_policy import (
            enforce_connection_oauth as eco,
            enforce_egress as ef,
            enforce_workflow_invocation as ew,
        )
    except ImportError:
        import importlib.util

        _policy = Path(__file__).resolve().parent / "oauth_policy.py"
        spec = importlib.util.spec_from_file_location(
            "minicloud_oauth_policy_standalone",
            _policy,
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        ef, ew, eco = (
            mod.enforce_egress,
            mod.enforce_workflow_invocation,
            mod.enforce_connection_oauth,
        )
    return ef, ew, eco


def _load_resolve_http_url():
    try:
        from app.connections import resolve_http_url as rh
    except ImportError:
        import importlib.util

        _conn = Path(__file__).resolve().parent / "connections.py"
        spec = importlib.util.spec_from_file_location(
            "minicloud_connections_standalone",
            _conn,
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        rh = mod.resolve_http_url
    return rh


_enforce_egress, _enforce_workflow_invocation, _enforce_connection_oauth = _load_oauth_enforcement()
_resolve_http_url = _load_resolve_http_url()


def _require_connection(
    connections: dict[str, Any],
    name: str,
    expect_type: str,
    step_id: str,
) -> Any:
    if name not in connections:
        raise RuntimeError(f"step {step_id!r}: unknown connection {name!r}")
    c = connections[name]
    got = getattr(c, "type", None)
    if got != expect_type:
        raise RuntimeError(
            f"step {step_id!r}: connection {name!r} has type {got!r}, expected {expect_type!r}",
        )
    return c


class WhenCondition(BaseModel):
    """Optioneel per stap: voer alleen uit als `context[context_key]` matcht (IF / CASE-arm)."""

    context_key: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("context_key", "variable"),
    )
    equals: str | None = None
    not_equals: str | None = None
    one_of: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one(self) -> WhenCondition:
        parts = [self.equals is not None, self.one_of is not None, self.not_equals is not None]
        if sum(parts) != 1:
            raise ValueError("when: specify exactly one of equals, not_equals, one_of")
        return self


class WhenMixin(BaseModel):
    when: WhenCondition | None = None


class HttpRequestConfig(BaseModel):
    method: str = "GET"
    url: str | None = Field(
        default=None,
        description="Volledige URL (zonder connection), of absolute override (https://…) met connection",
    )
    path: str | None = Field(
        default=None,
        description="Pad t.o.v. connection base_url (met connection); mag met of zonder leading /",
    )
    headers: dict[str, str] = Field(default_factory=dict)
    body_from: str = Field(
        ...,
        description="initial | previous | <step_id>",
    )
    timeout_seconds: float = Field(default=60, ge=1, le=300)


class XsltWorkflowStep(WhenMixin):
    type: Literal["xslt"] = "xslt"
    id: str
    xslt: str = Field(..., min_length=1)
    input_from: str | None = Field(
        default=None,
        description="initial | previous | <step_id>",
    )


class HttpWorkflowStep(WhenMixin):
    type: Literal["http"] = "http"
    id: str
    connection: str | None = Field(
        default=None,
        description="Naam uit connections/*.yaml; dan base_url + path, of volledige url in http.url",
    )
    http: HttpRequestConfig

    @model_validator(mode="after")
    def http_connection_rules(self) -> HttpWorkflowStep:
        if self.connection is None:
            if not self.http.url:
                raise ValueError("http.url is required when connection is omitted")
        elif self.http.url and self.http.path:
            raise ValueError("http: with connection, use at most one of http.url (absolute) and http.path")
        return self


class FtpRequestConfig(BaseModel):
    protocol: Literal["ftp", "ftps"] = "ftp"
    host: str | None = Field(
        default=None,
        description="Verplicht zonder connection; bij connection uit connection-definitie",
    )
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


class FtpWorkflowStep(WhenMixin):
    type: Literal["ftp"] = "ftp"
    id: str
    connection: str | None = None
    ftp: FtpRequestConfig

    @model_validator(mode="after")
    def ftp_connection_rules(self) -> FtpWorkflowStep:
        if self.connection is None:
            if not self.ftp.host:
                raise ValueError("ftp.host is required when connection is omitted")
        return self


class SshRequestConfig(BaseModel):
    host: str | None = Field(default=None, description="Verplicht zonder connection")
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, description="Verplicht zonder connection")
    password: str | None = None
    private_key_from: str | None = Field(
        default=None,
        description="PEM-key: initial | previous | <step_id>",
    )
    command: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=60, ge=5, le=600)


class SshWorkflowStep(WhenMixin):
    type: Literal["ssh"] = "ssh"
    id: str
    connection: str | None = None
    ssh: SshRequestConfig

    @model_validator(mode="after")
    def ssh_connection_rules(self) -> SshWorkflowStep:
        if self.connection is None:
            if not self.ssh.host or not self.ssh.username:
                raise ValueError("ssh.host and ssh.username are required when connection is omitted")
        else:
            if self.ssh.host:
                raise ValueError("ssh.host must not be set when connection is set (use connection definition)")
            if self.ssh.username:
                raise ValueError("ssh.username must not be set when connection is set (use connection definition)")
        return self


class SftpRequestConfig(BaseModel):
    host: str | None = Field(default=None, description="Verplicht zonder connection")
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, description="Verplicht zonder connection")
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


class SftpWorkflowStep(WhenMixin):
    type: Literal["sftp"] = "sftp"
    id: str
    connection: str | None = None
    sftp: SftpRequestConfig

    @model_validator(mode="after")
    def sftp_connection_rules(self) -> SftpWorkflowStep:
        if self.connection is None:
            if not self.sftp.host or not self.sftp.username:
                raise ValueError("sftp.host and sftp.username are required when connection is omitted")
        else:
            if self.sftp.host:
                raise ValueError("sftp.host must not be set when connection is set")
            if self.sftp.username:
                raise ValueError("sftp.username must not be set when connection is set")
        return self


class Xml2JsonWorkflowStep(WhenMixin):
    type: Literal["xml2json"] = "xml2json"
    id: str
    input_from: str | None = Field(
        default=None,
        description="XML-bron: initial | previous | <step_id>",
    )


class Json2XmlWorkflowStep(WhenMixin):
    type: Literal["json2xml"] = "json2xml"
    id: str
    input_from: str | None = Field(
        default=None,
        description="JSON-string: initial | previous | <step_id>",
    )


class LiquidWorkflowStep(WhenMixin):
    type: Literal["liquid"] = "liquid"
    id: str
    template: str = Field(..., min_length=1, description="Liquid-sjabloon")
    input_from: str | None = Field(
        default=None,
        description="JSON-context string: initial | previous | <step_id>",
    )


class ContextSetWorkflowStep(WhenMixin):
    type: Literal["context_set"] = "context_set"
    id: str
    context_key: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("context_key", "variable"),
    )
    value: str | None = None
    value_from: str | None = Field(
        default=None,
        description="initial | previous | <step_id> | context:<key>",
    )

    @model_validator(mode="after")
    def value_xor(self) -> ContextSetWorkflowStep:
        if (self.value is None) == (self.value_from is None):
            raise ValueError("context_set: specify exactly one of value, value_from")
        return self


class ContextExtractJsonWorkflowStep(WhenMixin):
    type: Literal["context_extract_json"] = "context_extract_json"
    id: str
    context_key: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("context_key", "variable"),
    )
    input_from: str | None = Field(
        default=None,
        description="JSON text: initial | previous | <step_id>",
    )
    json_path: str = Field(
        ...,
        min_length=1,
        description="JSON Pointer (RFC 6901), e.g. /items/0/code",
    )


class ContextExtractXmlWorkflowStep(WhenMixin):
    type: Literal["context_extract_xml"] = "context_extract_xml"
    id: str
    context_key: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("context_key", "variable"),
    )
    input_from: str | None = Field(
        default=None,
        description="XML: initial | previous | <step_id>",
    )
    xpath: str = Field(..., min_length=1)


class JsonSetWorkflowStep(WhenMixin):
    """Zet een waarde op een JSON Pointer-pad; bron via value_from (o.a. context: / var:)."""

    type: Literal["json_set"] = "json_set"
    id: str
    input_from: str | None = Field(
        default=None,
        description="JSON document: initial | previous | <step_id>",
    )
    json_path: str = Field(..., min_length=1, description="JSON Pointer naar het te zetten veld")
    value_from: str = Field(
        ...,
        description="Nieuwe waarde: initial | previous | <step_id> | context:k | var:k",
    )
    mirror_to_context: str | None = Field(
        default=None,
        validation_alias=AliasChoices("mirror_to_context", "also_variable"),
        description="Optioneel: volledige JSON-string na wijziging ook onder deze sleutel in context",
    )


class XmlSetTextWorkflowStep(WhenMixin):
    """Schrijf tekst of attribuut op het eerste XPath-resultaat."""

    type: Literal["xml_set_text"] = "xml_set_text"
    id: str
    input_from: str | None = Field(
        default=None,
        description="XML: initial | previous | <step_id>",
    )
    xpath: str = Field(..., min_length=1)
    value_from: str = Field(
        ...,
        description="Nieuwe waarde: initial | previous | <step_id> | context:k | var:k",
    )
    attribute: str | None = Field(
        default=None,
        description="Als gezet: eerste node, dit attribuut; anders elementtekst",
    )
    mirror_to_context: str | None = Field(
        default=None,
        validation_alias=AliasChoices("mirror_to_context", "also_variable"),
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
        ContextSetWorkflowStep,
        ContextExtractJsonWorkflowStep,
        ContextExtractXmlWorkflowStep,
        JsonSetWorkflowStep,
        XmlSetTextWorkflowStep,
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


def _when_matches(wc: WhenCondition, ctx: dict[str, str]) -> bool:
    val = ctx.get(wc.context_key)
    if wc.equals is not None:
        return val == wc.equals
    if wc.one_of is not None:
        return val in wc.one_of
    if wc.not_equals is not None:
        return val != wc.not_equals
    raise RuntimeError("unreachable when")


def _to_context_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _coerce_value_for_json_set(s: str) -> Any:
    """Parse JSON scalars/objects; fallback to plain string."""
    s = s.strip()
    if not s:
        return ""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def _json_pointer_set(root: Any, pointer: str, value: Any) -> Any:
    import copy

    doc = copy.deepcopy(root)
    if pointer in ("", "/"):
        return value
    if not pointer.startswith("/"):
        raise ValueError("json_path must be a JSON Pointer starting with /")
    parts: list[str] = []
    for raw in pointer.strip("/").split("/"):
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    cur: Any = doc
    for key in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            cur = cur[key]
        else:
            raise ValueError(f"json_set: cannot traverse at {pointer!r}")
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return doc


def _json_pointer_get(root: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return root
    if not pointer.startswith("/"):
        raise ValueError("json_path must be a JSON Pointer starting with /")
    cur: Any = root
    for raw in pointer.lstrip("/").split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            cur = cur[key]
        else:
            raise ValueError(f"json_path: cannot index into value at {pointer!r}")
    return cur


def _xml_xpath_first_text(tree: Any, xpath_expr: str) -> str:
    nodes = tree.xpath(xpath_expr)
    if not nodes:
        raise ValueError(f"xpath returned no nodes: {xpath_expr!r}")
    n0 = nodes[0]
    if isinstance(n0, str):
        return n0
    if isinstance(n0, etree._Element):
        t = (n0.text or "").strip()
        if t:
            return t
        return etree.tostring(n0, encoding="unicode", method="text").strip()
    return str(n0)


def _resolve_input(
    ref: str | None,
    *,
    initial: str,
    previous: str,
    outputs: dict[str, str],
    step_index: int,
    context: dict[str, str],
) -> str:
    if ref is None:
        return initial if step_index == 0 else previous
    if ref.startswith("context:") or ref.startswith("var:"):
        k = ref[8:] if ref.startswith("context:") else ref[4:]
        if k not in context:
            raise ValueError(f"unknown context/variable key in ref: {k!r}")
        return context[k]
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
    granted_scopes: frozenset[str] | None = None,
    connections: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str], list[dict[str, Any]], dict[str, str]]:
    outputs: dict[str, str] = {}
    context: dict[str, str] = {}
    previous = initial_xml
    trace: list[dict[str, Any]] = []
    conn_reg: dict[str, Any] = connections if connections is not None else {}

    _enforce_workflow_invocation(granted_scopes, doc.name)

    for idx, step in enumerate(doc.steps):
        wm = getattr(step, "when", None)
        if wm is not None and not _when_matches(wm, context):
            outputs[step.id] = previous
            trace.append(
                {
                    "step": step.id,
                    "type": getattr(step, "type", "unknown"),
                    "skipped": True,
                    "reason": "when",
                },
            )
            continue

        if isinstance(step, ContextSetWorkflowStep):
            if step.value is not None:
                resolved = step.value
            else:
                resolved = _resolve_input(
                    step.value_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                    context=context,
                )
            context[step.context_key] = resolved
            outputs[step.id] = previous
            trace.append(
                {
                    "step": step.id,
                    "type": "context_set",
                    "ok": True,
                    "context_key": step.context_key,
                },
            )
        elif isinstance(step, ContextExtractJsonWorkflowStep):
            raw = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"context_extract_json {step.id!r}: invalid JSON: {e}") from e
            try:
                found = _json_pointer_get(obj, step.json_path)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(
                    f"context_extract_json {step.id!r}: json_path {step.json_path!r}: {e}",
                ) from e
            s = _to_context_str(found)
            context[step.context_key] = s
            outputs[step.id] = s
            previous = s
            trace.append({"step": step.id, "type": "context_extract_json", "ok": True})
        elif isinstance(step, ContextExtractXmlWorkflowStep):
            raw = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            try:
                tree = etree.fromstring(raw.encode("utf-8"))
            except etree.XMLSyntaxError as e:
                raise RuntimeError(f"context_extract_xml {step.id!r}: invalid XML: {e}") from e
            try:
                s = _xml_xpath_first_text(tree, step.xpath)
            except ValueError as e:
                raise RuntimeError(f"context_extract_xml {step.id!r}: {e}") from e
            context[step.context_key] = s
            outputs[step.id] = s
            previous = s
            trace.append({"step": step.id, "type": "context_extract_xml", "ok": True})
        elif isinstance(step, JsonSetWorkflowStep):
            raw = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"json_set {step.id!r}: invalid JSON: {e}") from e
            val_s = _resolve_input(
                step.value_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            val = _coerce_value_for_json_set(val_s)
            try:
                new_doc = _json_pointer_set(obj, step.json_path, val)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"json_set {step.id!r}: {e}") from e
            out_s = json.dumps(new_doc, ensure_ascii=False)
            outputs[step.id] = out_s
            previous = out_s
            if step.mirror_to_context:
                context[step.mirror_to_context] = out_s
            trace.append({"step": step.id, "type": "json_set", "ok": True})
        elif isinstance(step, XmlSetTextWorkflowStep):
            raw = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            val = _resolve_input(
                step.value_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            try:
                tree = etree.fromstring(raw.encode("utf-8"))
            except etree.XMLSyntaxError as e:
                raise RuntimeError(f"xml_set_text {step.id!r}: invalid XML: {e}") from e
            nodes = tree.xpath(step.xpath)
            if not nodes:
                raise RuntimeError(f"xml_set_text {step.id!r}: xpath returned no nodes")
            n0 = nodes[0]
            if not isinstance(n0, etree._Element):
                raise RuntimeError(
                    f"xml_set_text {step.id!r}: xpath must select an element node",
                )
            if step.attribute:
                n0.set(step.attribute, val)
            else:
                n0.text = val
            out_s = etree.tostring(tree, encoding="unicode")
            outputs[step.id] = out_s
            previous = out_s
            if step.mirror_to_context:
                context[step.mirror_to_context] = out_s
            trace.append({"step": step.id, "type": "xml_set_text", "ok": True})
        elif isinstance(step, XsltWorkflowStep):
            inp = _resolve_input(
                step.input_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
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
            _enforce_egress(granted_scopes, "http", step_id=step.id)
            spec = step.http
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "http", step.id)
                _enforce_connection_oauth(
                    granted_scopes,
                    getattr(c, "oauth_scope", None),
                    step_id=step.id,
                    connection_name=conn_name,
                )
                final_url = _resolve_http_url(
                    base_url=c.base_url,
                    path_or_url=spec.url,
                    path=spec.path,
                )
                merged_headers = {**getattr(c, "default_headers", {}), **spec.headers}
            else:
                final_url = spec.url or ""
                merged_headers = dict(spec.headers)
            body = _resolve_input(
                spec.body_from,
                initial=initial_xml,
                previous=previous,
                outputs=outputs,
                step_index=idx,
                context=context,
            )
            r = await httpx_client.post(
                egress_http_url,
                json={
                    "method": spec.method,
                    "url": final_url,
                    "headers": merged_headers,
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
            _enforce_egress(granted_scopes, "ftp", step_id=step.id)
            spec = step.ftp
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "ftp", step.id)
                _enforce_connection_oauth(
                    granted_scopes,
                    getattr(c, "oauth_scope", None),
                    step_id=step.id,
                    connection_name=conn_name,
                )
                payload = {
                    "protocol": c.protocol,
                    "host": c.host,
                    "port": c.port,
                    "username": c.username,
                    "password": c.password,
                    "action": spec.action,
                    "remote_path": spec.remote_path,
                    "timeout_seconds": spec.timeout_seconds,
                }
            else:
                payload = {
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
                    context=context,
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
            _enforce_egress(granted_scopes, "ssh", step_id=step.id)
            spec = step.ssh
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "ssh", step.id)
                _enforce_connection_oauth(
                    granted_scopes,
                    getattr(c, "oauth_scope", None),
                    step_id=step.id,
                    connection_name=conn_name,
                )
                payload = {
                    "host": c.host,
                    "port": c.port,
                    "username": c.username,
                    "password": c.password,
                    "command": spec.command,
                    "timeout_seconds": spec.timeout_seconds,
                }
                pem: str | None = None
                if spec.private_key_from is not None:
                    pem = _resolve_input(
                        spec.private_key_from,
                        initial=initial_xml,
                        previous=previous,
                        outputs=outputs,
                        step_index=idx,
                        context=context,
                    )
                elif getattr(c, "private_key_pem", None):
                    pem = c.private_key_pem
                if pem:
                    payload["private_key_pem"] = pem
            else:
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
                        context=context,
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
            _enforce_egress(granted_scopes, "sftp", step_id=step.id)
            spec = step.sftp
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "sftp", step.id)
                _enforce_connection_oauth(
                    granted_scopes,
                    getattr(c, "oauth_scope", None),
                    step_id=step.id,
                    connection_name=conn_name,
                )
                payload = {
                    "host": c.host,
                    "port": c.port,
                    "username": c.username,
                    "password": c.password,
                    "action": spec.action,
                    "remote_path": spec.remote_path,
                    "timeout_seconds": spec.timeout_seconds,
                }
                pem: str | None = None
                if spec.private_key_from is not None:
                    pem = _resolve_input(
                        spec.private_key_from,
                        initial=initial_xml,
                        previous=previous,
                        outputs=outputs,
                        step_index=idx,
                        context=context,
                    )
                elif getattr(c, "private_key_pem", None):
                    pem = c.private_key_pem
                if pem:
                    payload["private_key_pem"] = pem
            else:
                payload = {
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
                        context=context,
                    )
                    payload["private_key_pem"] = pem
            if spec.action == "store":
                raw = _resolve_input(
                    spec.body_from,
                    initial=initial_xml,
                    previous=previous,
                    outputs=outputs,
                    step_index=idx,
                    context=context,
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
                context=context,
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
                context=context,
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
                context=context,
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

    return previous, outputs, trace, context
