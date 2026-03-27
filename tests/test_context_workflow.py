"""Workflow context (set/extract) and optional when (IF/CASE)."""
from __future__ import annotations

import json

import httpx
import pytest
import yaml

from tests.conftest import load_fastapi_app, load_workflow_runner_standalone


@pytest.mark.asyncio
async def test_context_set_and_when_liquid():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: ctx_when_demo
steps:
  - id: set_flag
    type: context_set
    context_key: flag
    value: "on"
  - id: branch_msg
    type: liquid
    when:
      context_key: flag
      equals: "on"
    input_from: initial
    template: "branch-ok"
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, trace, ctx = await wr.run_workflow(
            doc,
            '{"x": 1}',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-ctx",
            httpx_client=client,
        )

    assert ctx["flag"] == "on"
    assert final.strip() == "branch-ok"
    assert trace[0]["type"] == "context_set"
    assert trace[1]["type"] == "liquid"


@pytest.mark.asyncio
async def test_context_extract_json_and_when_skip():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: ctx_extract_demo
steps:
  - id: pick
    type: context_extract_json
    context_key: code
    input_from: initial
    json_path: /code
  - id: skip_me
    type: liquid
    when:
      context_key: code
      equals: "NO"
    input_from: initial
    template: "should not appear"
  - id: tail
    type: liquid
    input_from: initial
    template: "tail"
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, trace, ctx = await wr.run_workflow(
            doc,
            '{"code": "YES", "n": 1}',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-ctx2",
            httpx_client=client,
        )

    assert ctx["code"] == "YES"
    assert final.strip() == "tail"
    assert trace[1]["skipped"] is True


@pytest.mark.asyncio
async def test_context_extract_xml_xpath():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: ctx_xml_demo
steps:
  - id: xp
    type: context_extract_xml
    context_key: kind
    input_from: initial
    xpath: /root/item/@id
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        _final, _out, trace, ctx = await wr.run_workflow(
            doc,
            '<?xml version="1.0"?><root><item id="abc"/></root>',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-ctx3",
            httpx_client=client,
        )

    assert ctx["kind"] == "abc"
    assert trace[0]["type"] == "context_extract_xml"


@pytest.mark.asyncio
async def test_json_set_uses_var_ref_and_mirror():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: json_set_demo
steps:
  - id: v
    type: context_set
    variable: label
    value: "patched"
  - id: j
    type: json_set
    input_from: initial
    json_path: /a/x
    value_from: var:label
    also_variable: fulljson
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, trace, ctx = await wr.run_workflow(
            doc,
            '{"a": {"x": 1}, "b": true}',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-jsonset",
            httpx_client=client,
        )

    assert json.loads(final)["a"]["x"] == "patched"
    assert "patched" in ctx["fulljson"]
    assert trace[1]["type"] == "json_set"


@pytest.mark.asyncio
async def test_xml_set_text_attribute():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: xml_set_demo
steps:
  - id: w
    type: context_set
    variable: v
    value: "z"
  - id: x
    type: xml_set_text
    input_from: initial
    xpath: /root/item
    attribute: id
    value_from: var:v
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, _trace, _ctx = await wr.run_workflow(
            doc,
            '<?xml version="1.0"?><root><item id="old"/></root>',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-xmlset",
            httpx_client=client,
        )

    assert 'id="z"' in final


@pytest.mark.asyncio
async def test_storage_write_then_read_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: storage_rw_demo
steps:
  - id: put_value
    type: storage_write
    storage:
      bucket: workflows
      key: sample/key
      value_from: initial
      also_variable: write_result
  - id: read_value
    type: storage_read
    storage:
      bucket: workflows
      key: sample/key
      output_field: value
      also_variable: read_value
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    storage_app = load_fastapi_app("storage")
    transport = httpx.ASGITransport(app=storage_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://storage.test",
    ) as client:
        final, _out, trace, ctx = await wr.run_workflow(
            doc,
            "hello-storage",
            transformers_base_url="http://unused",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-storage-rw",
            httpx_client=client,
            storage_base_url="http://storage.test",
        )

    assert final == "hello-storage"
    assert ctx["read_value"] == "hello-storage"
    assert "stored" in ctx["write_result"]
    assert trace[0]["type"] == "storage_write"
    assert trace[1]["type"] == "storage_read"


@pytest.mark.asyncio
async def test_storage_steps_acl_requires_role_header(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path / "storage-data"))
    monkeypatch.setenv("STORAGE_ACL_ENABLED", "true")
    monkeypatch.setenv("STORAGE_DEFAULT_ROLE", "")
    monkeypatch.setenv(
        "STORAGE_ACL_POLICY",
        '{"default":{"read_roles":[],"write_roles":[]},"buckets":{"secure":{"read_roles":["orchestrator"],"write_roles":["orchestrator"]}}}',
    )

    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: storage_acl_demo
steps:
  - id: put
    type: storage_write
    storage:
      bucket: secure
      key: demo/item
      value_from: initial
  - id: get
    type: storage_read
    storage:
      bucket: secure
      key: demo/item
      output_field: value
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    storage_app = load_fastapi_app("storage")
    transport = httpx.ASGITransport(app=storage_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://storage.test",
    ) as client:
        with pytest.raises(RuntimeError, match="storage_write step"):
            await wr.run_workflow(
                doc,
                "secret",
                transformers_base_url="http://unused",
                egress_http_url="http://unused/call",
                egress_ftp_url="http://unused/ftp",
                egress_ssh_url="http://unused/exec",
                egress_sftp_url="http://unused/sftp",
                request_id="test-storage-acl-denied",
                httpx_client=client,
                storage_base_url="http://storage.test",
            )

        final, _out, trace, _ctx = await wr.run_workflow(
            doc,
            "secret",
            transformers_base_url="http://unused",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-storage-acl-allowed",
            httpx_client=client,
            storage_base_url="http://storage.test",
            storage_roles_header="orchestrator",
        )

    assert final == "secret"
    assert trace[0]["type"] == "storage_write"
    assert trace[1]["type"] == "storage_read"


@pytest.mark.asyncio
async def test_if_else_basic_branching():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: if_basic
steps:
  - id: set_person
    type: context_set
    variable: person
    value: "Bob"
  - id: choose
    type: if
    condition:
      context_key: person
      equals: "Bob"
    then:
      - id: true_step
        type: liquid
        input_from: initial
        template: "TRUE branch"
    else:
      - id: false_step
        type: liquid
        input_from: initial
        template: "FALSE branch"
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, trace, _ctx = await wr.run_workflow(
            doc,
            '{"x": 1}',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-if-basic",
            httpx_client=client,
        )

    assert final.strip() == "TRUE branch"
    choose_trace = [t for t in trace if t["step"] == "choose"]
    assert choose_trace[0]["type"] == "if"
    assert choose_trace[0]["branch"] == "then"
    assert choose_trace[0]["condition_matched"] is True


@pytest.mark.asyncio
async def test_if_nested_and_optional_else():
    wr = load_workflow_runner_standalone()
    raw = yaml.safe_load(
        """
name: if_nested
steps:
  - id: set_person
    type: context_set
    variable: person
    value: "Bob"
  - id: set_age
    type: context_set
    variable: age
    value: "41"
  - id: outer_if
    type: if
    condition:
      context_key: person
      equals: "Bob"
    then:
      - id: inner_if
        type: if
        condition:
          context_key: age
          equals: "42"
        then:
          - id: hit_42
            type: liquid
            input_from: initial
            template: "Bob is 42!"
        else:
          - id: miss_42
            type: liquid
            input_from: initial
            template: "Bob is niet 42."
  - id: only_then
    type: if
    condition:
      context_key: person
      equals: "Alice"
    then:
      - id: should_not_run
        type: liquid
        input_from: initial
        template: "nope"
""",
    )
    doc = wr.WorkflowDoc.model_validate(raw)

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _out, trace, _ctx = await wr.run_workflow(
            doc,
            '{"x": 1}',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-if-nested",
            httpx_client=client,
        )

    assert final.strip() == "Bob is niet 42."
    outer = [t for t in trace if t["step"] == "outer_if"][0]
    inner = [t for t in trace if t["step"] == "inner_if"][0]
    only_then = [t for t in trace if t["step"] == "only_then"][0]
    assert outer["branch"] == "then"
    assert inner["branch"] == "else"
    assert only_then["branch"] == "else"
