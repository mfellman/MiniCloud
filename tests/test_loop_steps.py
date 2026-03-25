"""Tests for for_each and repeat_until loop step types."""
from __future__ import annotations

import json

import httpx
import pytest
import yaml

from tests.conftest import load_fastapi_app, load_workflow_runner_standalone


async def _run(wr, raw_yaml: str, initial: str):
    doc = wr.WorkflowDoc.model_validate(yaml.safe_load(raw_yaml))
    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transformers.test") as client:
        return await wr.run_workflow(
            doc,
            initial,
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-loop",
            httpx_client=client,
        )


@pytest.mark.asyncio
async def test_for_each_basic():
    """for_each iterates over a JSON array, runs liquid per item, collects results."""
    wr = load_workflow_runner_standalone()
    final, outputs, trace, ctx = await _run(
        wr,
        """
name: fe_test
steps:
  - id: loop
    type: for_each
    input_from: initial
    items_path: /names
    as: item
    index_as: i
    steps:
      - id: greet
        type: liquid
        input_from: "var:item"
        template: "Hello {{ name }}!"
""",
        '{"names": [{"name": "Alice"}, {"name": "Bob"}, {"name": "Charlie"}]}',
    )
    result = json.loads(final)
    assert len(result) == 3
    assert result[0].strip() == "Hello Alice!"
    assert result[1].strip() == "Hello Bob!"
    assert result[2].strip() == "Hello Charlie!"
    loop_trace = [t for t in trace if t["step"] == "loop"]
    assert loop_trace[0]["type"] == "for_each"
    assert loop_trace[0]["iterations"] == 3


@pytest.mark.asyncio
async def test_for_each_root_array():
    """for_each with items_path=/ treats the entire input as the array."""
    wr = load_workflow_runner_standalone()
    final, outputs, trace, ctx = await _run(
        wr,
        """
name: fe_root
steps:
  - id: loop
    type: for_each
    input_from: initial
    items_path: /
    as: item
    steps:
      - id: echo
        type: context_set
        variable: last
        value_from: "var:item"
""",
        '[1, 2, 3]',
    )
    assert ctx["last"] == "3"


@pytest.mark.asyncio
async def test_for_each_max_iterations_exceeded():
    """for_each raises error when items exceed max_iterations."""
    wr = load_workflow_runner_standalone()
    with pytest.raises(RuntimeError, match="exceeds max_iterations"):
        await _run(
            wr,
            """
name: fe_max
steps:
  - id: loop
    type: for_each
    input_from: initial
    items_path: /
    max_iterations: 2
    as: item
    steps:
      - id: noop
        type: context_set
        variable: x
        value: "y"
""",
            '[1, 2, 3]',
        )


@pytest.mark.asyncio
async def test_repeat_until_basic():
    """repeat_until loops substeps until context condition is met."""
    wr = load_workflow_runner_standalone()
    # Each iteration: build JSON with counter from context, increment via Liquid, store back.
    final, outputs, trace, ctx = await _run(
        wr,
        """
name: ru_test
steps:
  - id: init
    type: context_set
    variable: count
    value: "0"
  - id: poll
    type: repeat_until
    max_iterations: 10
    until:
      context_key: count
      equals: "3"
    steps:
      - id: prep
        type: json_set
        input_from: initial
        json_path: /c
        value_from: var:count
      - id: inc
        type: liquid
        input_from: prep
        template: "{{ c | plus: 1 }}"
      - id: save
        type: context_set
        variable: count
        value_from: inc
""",
        '{"c": 0}',
    )
    assert ctx["count"] == "3"
    poll_trace = [t for t in trace if t["step"] == "poll"]
    assert poll_trace[0]["type"] == "repeat_until"
    assert poll_trace[0]["iterations"] == 3


@pytest.mark.asyncio
async def test_repeat_until_max_iterations_exceeded():
    """repeat_until raises error when condition is never met."""
    wr = load_workflow_runner_standalone()
    with pytest.raises(RuntimeError, match="condition not met after 2 iterations"):
        await _run(
            wr,
            """
name: ru_max
steps:
  - id: loop
    type: repeat_until
    max_iterations: 2
    until:
      context_key: done
      equals: "yes"
    steps:
      - id: noop
        type: context_set
        variable: x
        value: "y"
""",
            '{}',
        )


@pytest.mark.asyncio
async def test_for_each_with_xslt():
    """for_each can run XSLT substeps (uses real transformers ASGI)."""
    wr = load_workflow_runner_standalone()
    final, outputs, trace, ctx = await _run(
        wr,
        """
name: fe_xslt
steps:
  - id: loop
    type: for_each
    input_from: initial
    items_path: /docs
    as: item
    steps:
      - id: wrap
        type: xslt
        input_from: "var:item"
        xslt: |
          <?xml version="1.0"?>
          <xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
            <xsl:template match="/"><out><xsl:copy-of select="."/></out></xsl:template>
          </xsl:stylesheet>
""",
        '{"docs": ["<a/>", "<b/>"]}',
    )
    result = json.loads(final)
    assert len(result) == 2
    assert "<out>" in result[0]
    assert "<a/>" in result[0]
    assert "<b/>" in result[1]


@pytest.mark.asyncio
async def test_for_each_with_when():
    """for_each respects when condition on the loop itself."""
    wr = load_workflow_runner_standalone()
    final, outputs, trace, ctx = await _run(
        wr,
        """
name: fe_when
steps:
  - id: flag
    type: context_set
    variable: mode
    value: "skip"
  - id: loop
    type: for_each
    when:
      context_key: mode
      equals: "run"
    input_from: initial
    items_path: /
    as: item
    steps:
      - id: noop
        type: context_set
        variable: x
        value: "y"
""",
        '[1, 2]',
    )
    # Loop should be skipped
    loop_trace = [t for t in trace if t["step"] == "loop"]
    assert loop_trace[0]["skipped"] is True
