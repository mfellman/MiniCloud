"""Integration test: full workflow run (orchestrator workflow_runner + real transformers via ASGI)."""
from __future__ import annotations

import httpx
import pytest

from tests.conftest import REPO_ROOT, load_fastapi_app, load_workflow_runner_standalone


@pytest.mark.asyncio
async def test_run_minimal_workflow_xslt_only():
    """
    Workflow `minimal`: single XSLT step — no egress HTTP.
    Transformers runs in-process via ASGITransport.
    """
    wr = load_workflow_runner_standalone()
    workflows = wr.load_workflows(
        REPO_ROOT / "services" / "orchestrator" / "workflows",
    )
    doc = workflows["minimal"]

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final_xml, _outputs, trace, _ctx = await wr.run_workflow(
            doc,
            '<?xml version="1.0"?><doc><item/></doc>',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="test-orchestration",
            httpx_client=client,
        )

    assert "<wrapped>" in final_xml
    assert "<doc>" in final_xml
    assert len(trace) == 1
    assert trace[0]["type"] == "xslt"
    assert trace[0]["ok"] is True
