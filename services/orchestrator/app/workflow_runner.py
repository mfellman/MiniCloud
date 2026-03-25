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


# --- Loop step types ---

# Forward reference: substeps inside loops use the same WorkflowStep union.
# We define the models here; the union is updated below.

class ForEachWorkflowStep(WhenMixin):
    """Iterate over items in a JSON array, running substeps per item."""

    type: Literal["for_each"] = "for_each"
    id: str
    input_from: str | None = Field(
        default=None,
        description="JSON source: initial | previous | <step_id>",
    )
    items_path: str = Field(
        default="/",
        description="JSON Pointer to the array (default: root document is the array)",
    )
    as_key: str = Field(
        default="item",
        validation_alias=AliasChoices("as_key", "as"),
        description="Context key for the current item (JSON string)",
    )
    index_as: str | None = Field(
        default=None,
        description="Optional context key for the current 0-based index",
    )
    max_iterations: int = Field(default=100, ge=1, le=10000)
    steps: list[Any] = Field(..., min_length=1)


class RepeatUntilWorkflowStep(WhenMixin):
    """Repeat substeps until a context condition is met or max_iterations reached."""

    type: Literal["repeat_until"] = "repeat_until"
    id: str
    max_iterations: int = Field(default=20, ge=1, le=10000)
    until: WhenCondition
    steps: list[Any] = Field(..., min_length=1)


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
        ForEachWorkflowStep,
        RepeatUntilWorkflowStep,
    ],
    Field(discriminator="type"),
]


def _validate_loop_substeps(raw_steps: list[Any]) -> list[WorkflowStep]:
    """Parse raw dicts into typed WorkflowStep instances (for loop bodies)."""
    # Validate via a temporary WorkflowDoc to reuse the already-rebuilt model.
    tmp = WorkflowDoc.model_validate({"name": "_loop_tmp", "steps": raw_steps})
    return list(tmp.steps)


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
    run_trace: Any = None,
) -> tuple[str, dict[str, str], list[dict[str, Any]], dict[str, str]]:
    outputs: dict[str, str] = {}
    context: dict[str, str] = {}
    previous = initial_xml
    trace: list[dict[str, Any]] = []
    conn_reg: dict[str, Any] = connections if connections is not None else {}
    _rt = run_trace  # RunTrace | NullRunTrace | None

    _enforce_workflow_invocation(granted_scopes, doc.name)

    def _resolve(ref, *, initial, prev, idx):
        return _resolve_input(ref, initial=initial, previous=prev, outputs=outputs, step_index=idx, context=context)

    async def _run_step(step: Any, idx: int, initial: str, prev: str, collector: Any = None) -> str:
        """Execute one step; returns the new 'previous' value. Mutates outputs/context/trace."""
        nonlocal conn_reg

        if isinstance(step, ForEachWorkflowStep):
            return await _exec_for_each(step, initial, prev, idx, collector)
        elif isinstance(step, RepeatUntilWorkflowStep):
            return await _exec_repeat_until(step, initial, prev, idx, collector)
        elif isinstance(step, ContextSetWorkflowStep):
            st = collector.step(step.id, "context_set") if collector else None
            if step.value is not None:
                resolved = step.value
            else:
                resolved = _resolve(step.value_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(resolved)
            context[step.context_key] = resolved
            outputs[step.id] = prev
            if st:
                st.record_output(resolved)
                st.record_context_snapshot(context)
                collector.add_step(st.finish(ok=True, extra={"context_key": step.context_key}))
            trace.append({"step": step.id, "type": "context_set", "ok": True, "context_key": step.context_key})
            return prev
        elif isinstance(step, ContextExtractJsonWorkflowStep):
            st = collector.step(step.id, "context_extract_json") if collector else None
            raw = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(raw)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"context_extract_json {step.id!r}: invalid JSON: {e}") from e
            try:
                found = _json_pointer_get(obj, step.json_path)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"context_extract_json {step.id!r}: json_path {step.json_path!r}: {e}") from e
            s = _to_context_str(found)
            context[step.context_key] = s
            outputs[step.id] = s
            if st:
                st.record_output(s)
                st.record_context_snapshot(context)
                collector.add_step(st.finish(ok=True, extra={"context_key": step.context_key}))
            trace.append({"step": step.id, "type": "context_extract_json", "ok": True})
            return s
        elif isinstance(step, ContextExtractXmlWorkflowStep):
            st = collector.step(step.id, "context_extract_xml") if collector else None
            raw = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(raw)
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
            if st:
                st.record_output(s)
                st.record_context_snapshot(context)
                collector.add_step(st.finish(ok=True, extra={"context_key": step.context_key}))
            trace.append({"step": step.id, "type": "context_extract_xml", "ok": True})
            return s
        elif isinstance(step, JsonSetWorkflowStep):
            st = collector.step(step.id, "json_set") if collector else None
            raw = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(raw)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"json_set {step.id!r}: invalid JSON: {e}") from e
            val_s = _resolve(step.value_from, initial=initial, prev=prev, idx=idx)
            val = _coerce_value_for_json_set(val_s)
            try:
                new_doc = _json_pointer_set(obj, step.json_path, val)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"json_set {step.id!r}: {e}") from e
            out_s = json.dumps(new_doc, ensure_ascii=False)
            outputs[step.id] = out_s
            if step.mirror_to_context:
                context[step.mirror_to_context] = out_s
            if st:
                st.record_output(out_s)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "json_set", "ok": True})
            return out_s
        elif isinstance(step, XmlSetTextWorkflowStep):
            st = collector.step(step.id, "xml_set_text") if collector else None
            raw = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            val = _resolve(step.value_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(raw)
            try:
                tree = etree.fromstring(raw.encode("utf-8"))
            except etree.XMLSyntaxError as e:
                raise RuntimeError(f"xml_set_text {step.id!r}: invalid XML: {e}") from e
            nodes = tree.xpath(step.xpath)
            if not nodes:
                raise RuntimeError(f"xml_set_text {step.id!r}: xpath returned no nodes")
            n0 = nodes[0]
            if not isinstance(n0, etree._Element):
                raise RuntimeError(f"xml_set_text {step.id!r}: xpath must select an element node")
            if step.attribute:
                n0.set(step.attribute, val)
            else:
                n0.text = val
            out_s = etree.tostring(tree, encoding="unicode")
            outputs[step.id] = out_s
            if step.mirror_to_context:
                context[step.mirror_to_context] = out_s
            if st:
                st.record_output(out_s)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "xml_set_text", "ok": True})
            return out_s
        elif isinstance(step, XsltWorkflowStep):
            st = collector.step(step.id, "xslt") if collector else None
            inp = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(inp)
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
            if st:
                st.record_output(body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "xslt", "ok": True})
            return body
        elif isinstance(step, HttpWorkflowStep):
            _enforce_egress(granted_scopes, "http", step_id=step.id)
            st = collector.step(step.id, "http") if collector else None
            spec = step.http
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "http", step.id)
                _enforce_connection_oauth(granted_scopes, getattr(c, "oauth_scope", None), step_id=step.id, connection_name=conn_name)
                final_url = _resolve_http_url(base_url=c.base_url, path_or_url=spec.url, path=spec.path)
                merged_headers = {**getattr(c, "default_headers", {}), **spec.headers}
            else:
                final_url = spec.url or ""
                merged_headers = dict(spec.headers)
            body = _resolve(spec.body_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(body)
            r = await httpx_client.post(
                egress_http_url,
                json={"method": spec.method, "url": final_url, "headers": merged_headers, "body": body, "timeout_seconds": spec.timeout_seconds},
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
            ok = 200 <= out_code < 300
            if st:
                st.record_output(out_body)
                collector.add_step(st.finish(ok=ok, extra={"status_code": out_code}))
            trace.append({"step": step.id, "type": "http", "ok": ok, "status_code": out_code})
            if not ok:
                raise RuntimeError(f"http step {step.id!r} returned status {out_code}")
            return out_body
        elif isinstance(step, FtpWorkflowStep):
            _enforce_egress(granted_scopes, "ftp", step_id=step.id)
            st = collector.step(step.id, "ftp") if collector else None
            spec = step.ftp
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "ftp", step.id)
                _enforce_connection_oauth(granted_scopes, getattr(c, "oauth_scope", None), step_id=step.id, connection_name=conn_name)
                payload = {"protocol": c.protocol, "host": c.host, "port": c.port, "username": c.username, "password": c.password, "action": spec.action, "remote_path": spec.remote_path, "timeout_seconds": spec.timeout_seconds}
            else:
                payload = {"protocol": spec.protocol, "host": spec.host, "port": spec.port, "username": spec.username, "password": spec.password, "action": spec.action, "remote_path": spec.remote_path, "timeout_seconds": spec.timeout_seconds}
            if spec.action == "store":
                raw = _resolve(spec.body_from, initial=initial, prev=prev, idx=idx)
                if spec.body_encoding == "base64":
                    payload["data_base64"] = raw.strip()
                else:
                    payload["data"] = raw
            if st:
                st.record_input(json.dumps(payload, ensure_ascii=False, default=str))
            r = await httpx_client.post(egress_ftp_url, json=payload, headers={"X-Request-ID": request_id, "Content-Type": "application/json"})
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
            if st:
                st.record_output(out_body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "ftp", "ok": True})
            return out_body
        elif isinstance(step, SshWorkflowStep):
            _enforce_egress(granted_scopes, "ssh", step_id=step.id)
            st = collector.step(step.id, "ssh") if collector else None
            spec = step.ssh
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "ssh", step.id)
                _enforce_connection_oauth(granted_scopes, getattr(c, "oauth_scope", None), step_id=step.id, connection_name=conn_name)
                payload = {"host": c.host, "port": c.port, "username": c.username, "password": c.password, "command": spec.command, "timeout_seconds": spec.timeout_seconds}
                pem: str | None = None
                if spec.private_key_from is not None:
                    pem = _resolve(spec.private_key_from, initial=initial, prev=prev, idx=idx)
                elif getattr(c, "private_key_pem", None):
                    pem = c.private_key_pem
                if pem:
                    payload["private_key_pem"] = pem
            else:
                payload = {"host": spec.host, "port": spec.port, "username": spec.username, "password": spec.password, "command": spec.command, "timeout_seconds": spec.timeout_seconds}
                if spec.private_key_from is not None:
                    pem = _resolve(spec.private_key_from, initial=initial, prev=prev, idx=idx)
                    payload["private_key_pem"] = pem
            if st:
                st.record_input(json.dumps({"command": spec.command}, ensure_ascii=False))
            r = await httpx_client.post(egress_ssh_url, json=payload, headers={"X-Request-ID": request_id, "Content-Type": "application/json"})
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
            ok = bool(data.get("ok", False))
            if st:
                st.record_output(out_body)
                collector.add_step(st.finish(ok=ok))
            trace.append({"step": step.id, "type": "ssh", "ok": ok})
            if not ok:
                raise RuntimeError(f"ssh step {step.id!r} exit_status={data.get('exit_status')}")
            return out_body
        elif isinstance(step, SftpWorkflowStep):
            _enforce_egress(granted_scopes, "sftp", step_id=step.id)
            st = collector.step(step.id, "sftp") if collector else None
            spec = step.sftp
            conn_name = step.connection
            if conn_name:
                c = _require_connection(conn_reg, conn_name, "sftp", step.id)
                _enforce_connection_oauth(granted_scopes, getattr(c, "oauth_scope", None), step_id=step.id, connection_name=conn_name)
                payload = {"host": c.host, "port": c.port, "username": c.username, "password": c.password, "action": spec.action, "remote_path": spec.remote_path, "timeout_seconds": spec.timeout_seconds}
                pem: str | None = None
                if spec.private_key_from is not None:
                    pem = _resolve(spec.private_key_from, initial=initial, prev=prev, idx=idx)
                elif getattr(c, "private_key_pem", None):
                    pem = c.private_key_pem
                if pem:
                    payload["private_key_pem"] = pem
            else:
                payload = {"host": spec.host, "port": spec.port, "username": spec.username, "password": spec.password, "action": spec.action, "remote_path": spec.remote_path, "timeout_seconds": spec.timeout_seconds}
                if spec.private_key_from is not None:
                    pem = _resolve(spec.private_key_from, initial=initial, prev=prev, idx=idx)
                    payload["private_key_pem"] = pem
            if spec.action == "store":
                raw = _resolve(spec.body_from, initial=initial, prev=prev, idx=idx)
                if spec.body_encoding == "base64":
                    payload["data_base64"] = raw.strip()
                else:
                    payload["data"] = raw
            if st:
                st.record_input(json.dumps({"action": spec.action, "remote_path": spec.remote_path}, ensure_ascii=False))
            r = await httpx_client.post(egress_sftp_url, json=payload, headers={"X-Request-ID": request_id, "Content-Type": "application/json"})
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
            if st:
                st.record_output(out_body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "sftp", "ok": True})
            return out_body
        elif isinstance(step, Xml2JsonWorkflowStep):
            st = collector.step(step.id, "xml2json") if collector else None
            inp = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(inp)
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
            if st:
                st.record_output(body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "xml2json", "ok": True})
            return body
        elif isinstance(step, Json2XmlWorkflowStep):
            st = collector.step(step.id, "json2xml") if collector else None
            inp = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(inp)
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
            if st:
                st.record_output(body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "json2xml", "ok": True})
            return body
        elif isinstance(step, LiquidWorkflowStep):
            st = collector.step(step.id, "liquid") if collector else None
            ctx_s = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
            if st:
                st.record_input(ctx_s)
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
            if st:
                st.record_output(body)
                collector.add_step(st.finish(ok=True))
            trace.append({"step": step.id, "type": "liquid", "ok": True})
            return body
        else:
            raise RuntimeError(f"unsupported step type: {type(step)!r}")

    async def _exec_substeps(steps: list, initial: str, prev: str, collector: Any = None) -> str:
        """Run a list of (parsed) WorkflowStep objects sequentially."""
        for i, s in enumerate(steps):
            wm = getattr(s, "when", None)
            if wm is not None and not _when_matches(wm, context):
                outputs[s.id] = prev
                if collector:
                    sk = collector.step(s.id, getattr(s, "type", "unknown"))
                    collector.add_step(sk.finish(skipped=True, reason="when"))
                trace.append({"step": s.id, "type": getattr(s, "type", "unknown"), "skipped": True, "reason": "when"})
                continue
            prev = await _run_step(s, i, initial, prev, collector)
        return prev

    async def _exec_for_each(step: ForEachWorkflowStep, initial: str, prev: str, idx: int, collector: Any = None) -> str:
        raw = _resolve(step.input_from, initial=initial, prev=prev, idx=idx)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"for_each {step.id!r}: input is not valid JSON: {e}") from e
        items_path = step.items_path
        if items_path == "/":
            items = obj
        else:
            try:
                items = _json_pointer_get(obj, items_path)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"for_each {step.id!r}: items_path {items_path!r}: {e}") from e
        if not isinstance(items, list):
            raise RuntimeError(f"for_each {step.id!r}: items at {items_path!r} is not an array")
        if len(items) > step.max_iterations:
            raise RuntimeError(f"for_each {step.id!r}: {len(items)} items exceeds max_iterations ({step.max_iterations})")
        substeps = _validate_loop_substeps(step.steps)
        lt = collector.loop(step.id, "for_each") if collector else None
        collected: list[str] = []
        loop_prev = prev
        for i, item in enumerate(items):
            context[step.as_key] = _to_context_str(item)
            if step.index_as:
                context[step.index_as] = str(i)
            it = lt.begin_iteration(i) if lt else None
            loop_prev = await _exec_substeps(substeps, initial, loop_prev, it)
            if lt and it:
                lt.add_iteration(it.finish(context_snapshot=dict(context)))
            collected.append(loop_prev)
        result = json.dumps(collected, ensure_ascii=False)
        outputs[step.id] = result
        if lt and collector:
            collector.add_step(lt.finish(ok=True, extra={"items_count": len(items)}))
        trace.append({"step": step.id, "type": "for_each", "ok": True, "iterations": len(items)})
        return result

    async def _exec_repeat_until(step: RepeatUntilWorkflowStep, initial: str, prev: str, idx: int, collector: Any = None) -> str:
        substeps = _validate_loop_substeps(step.steps)
        lt = collector.loop(step.id, "repeat_until") if collector else None
        for iteration in range(step.max_iterations):
            it = lt.begin_iteration(iteration) if lt else None
            prev = await _exec_substeps(substeps, initial, prev, it)
            matched = _when_matches(step.until, context)
            if lt and it:
                lt.add_iteration(it.finish(context_snapshot=dict(context), until_matched=matched))
            if matched:
                outputs[step.id] = prev
                if lt and collector:
                    collector.add_step(lt.finish(ok=True))
                trace.append({"step": step.id, "type": "repeat_until", "ok": True, "iterations": iteration + 1})
                return prev
        raise RuntimeError(
            f"repeat_until {step.id!r}: condition not met after {step.max_iterations} iterations"
        )

    # --- main execution ---
    for idx, step in enumerate(doc.steps):
        wm = getattr(step, "when", None)
        if wm is not None and not _when_matches(wm, context):
            outputs[step.id] = previous
            if _rt:
                sk = _rt.step(step.id, getattr(step, "type", "unknown"))
                _rt.add_step(sk.finish(skipped=True, reason="when"))
            trace.append({"step": step.id, "type": getattr(step, "type", "unknown"), "skipped": True, "reason": "when"})
            continue
        previous = await _run_step(step, idx, initial_xml, previous, _rt)

    return previous, outputs, trace, context
