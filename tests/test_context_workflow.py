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
