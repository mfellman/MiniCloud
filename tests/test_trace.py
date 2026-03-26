"""Tests for trace_store and trace integration in workflow runs."""
from __future__ import annotations

import json
import os
import tempfile

import httpx
import pytest

from tests.conftest import REPO_ROOT, load_fastapi_app, load_workflow_runner_standalone


def _load_trace_store(traces_dir: str):
    """Load trace_store module with overridden env vars."""
    import importlib
    import importlib.util

    # Set env BEFORE loading the module
    os.environ["TRACES_DIR"] = traces_dir
    os.environ["TRACES_MAX_RUNS"] = "10"
    os.environ["TRACES_PREVIEW_LEN"] = "100"

    path = REPO_ROOT / "services" / "orchestrator" / "app" / "trace_store.py"
    spec = importlib.util.spec_from_file_location("trace_store_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestTraceStoreUnit:
    """Unit tests for trace_store module."""

    def test_begin_run_trace_returns_run_trace(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-1", "wf-1")
        assert type(rt).__name__ == "RunTrace"

    def test_run_trace_creates_directory(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-dir-test", "wf-1")
        assert (tmp_path / "req-dir-test").is_dir()
        assert (tmp_path / "req-dir-test" / "steps").is_dir()

    def test_step_trace_records_input_output(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-io", "wf-1")
        st = rt.step("step1", "xslt")
        st.record_input("<xml>hello</xml>")
        st.record_output("<xml>world</xml>")
        entry = st.finish(ok=True)

        assert entry["step"] == "step1"
        assert entry["type"] == "xslt"
        assert entry["status"] == "ok"
        assert "input_ref" in entry
        assert "output_ref" in entry

        # Verify files were written
        inp = (tmp_path / "req-io" / "steps" / "step1.input").read_text(encoding="utf-8")
        assert inp == "<xml>hello</xml>"
        out = (tmp_path / "req-io" / "steps" / "step1.output").read_text(encoding="utf-8")
        assert out == "<xml>world</xml>"

    def test_run_trace_finish_writes_json(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-finish", "wf-1")
        st = rt.step("s1", "context_set")
        st.record_input("val1")
        st.record_output("val1")
        rt.add_step(st.finish(ok=True))

        doc = rt.finish(status="succeeded", final_output="<done/>", context={"k": "v"})

        assert doc["status"] == "succeeded"
        assert doc["workflow"] == "wf-1"
        assert doc["request_id"] == "req-finish"
        assert len(doc["steps"]) == 1
        assert "final_output_ref" in doc

        # Verify trace.json on disk
        trace_json = json.loads(
            (tmp_path / "req-finish" / "trace.json").read_text(encoding="utf-8")
        )
        assert trace_json["status"] == "succeeded"
        assert trace_json["request_id"] == "req-finish"

    def test_loop_trace_with_iterations(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-loop", "wf-loop")
        lt = rt.loop("loop1", "for_each")

        for i in range(3):
            it = lt.begin_iteration(i)
            st = it.step("inner", "xslt")
            st.record_input(f"<item>{i}</item>")
            st.record_output(f"<out>{i}</out>")
            it.add_step(st.finish(ok=True))
            lt.add_iteration(it.finish())

        entry = lt.finish(ok=True, extra={"items_count": 3})
        rt.add_step(entry)

        assert entry["type"] == "for_each"
        assert entry["iterations"] == 3
        assert len(entry["children"]) == 3

        # Verify iteration files
        for i in range(3):
            inp_file = tmp_path / "req-loop" / "steps" / f"loop1.iter_{i}.inner.input"
            assert inp_file.exists()
            assert inp_file.read_text(encoding="utf-8") == f"<item>{i}</item>"

    def test_list_traces(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        for i in range(3):
            rt = ts.begin_run_trace(f"req-list-{i}", f"wf-{i}")
            rt.finish(status="succeeded")

        traces = ts.list_traces(limit=10)
        assert len(traces) == 3
        ids = {t["request_id"] for t in traces}
        assert ids == {"req-list-0", "req-list-1", "req-list-2"}

    def test_get_trace(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-get", "wf-get")
        rt.finish(status="succeeded")

        doc = ts.get_trace("req-get")
        assert doc is not None
        assert doc["request_id"] == "req-get"

    def test_get_trace_not_found(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        assert ts.get_trace("nonexistent") is None

    def test_get_trace_traversal_blocked(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        assert ts.get_trace("../etc/passwd") is None

    def test_get_step_data(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        rt = ts.begin_run_trace("req-sd", "wf-1")
        st = rt.step("s1", "xslt")
        st.record_input("<in/>")
        st.record_output("<out/>")
        st.finish(ok=True)
        rt.finish(status="succeeded")

        assert ts.get_step_data("req-sd", "s1", "input") == "<in/>"
        assert ts.get_step_data("req-sd", "s1", "output") == "<out/>"
        assert ts.get_step_data("req-sd", "s1", "other") is None

    def test_get_step_data_traversal_blocked(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        assert ts.get_step_data("req-1", "../../etc/passwd", "input") is None

    def test_prune_old_runs(self, tmp_path):
        ts = _load_trace_store(str(tmp_path))
        # TRACES_MAX_RUNS is set to 10, create 12
        for i in range(12):
            rt = ts.begin_run_trace(f"prune-{i:03d}", "wf-prune")
            rt.finish(status="succeeded")

        # After pruning, should have at most 10
        remaining = [p for p in tmp_path.iterdir() if p.is_dir() and (p / "trace.json").is_file()]
        assert len(remaining) <= 10


@pytest.mark.asyncio
async def test_trace_integration_minimal_workflow(tmp_path):
    """
    Run the minimal workflow with tracing enabled and verify trace files are created.
    """
    os.environ["TRACES_DIR"] = str(tmp_path)
    os.environ["TRACES_PREVIEW_LEN"] = "200"

    ts = _load_trace_store(str(tmp_path))
    wr = load_workflow_runner_standalone()
    workflows = wr.load_workflows(
        REPO_ROOT / "workflows",
    )
    doc = workflows["minimal"]

    rt = ts.begin_run_trace("trace-minimal", "minimal")

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
            request_id="trace-minimal",
            httpx_client=client,
            run_trace=rt,
        )

    doc_trace = rt.finish(status="succeeded", final_output=final_xml, context=_ctx)

    # Verify trace was written
    trace_json_path = tmp_path / "trace-minimal" / "trace.json"
    assert trace_json_path.exists()

    trace_doc = json.loads(trace_json_path.read_text(encoding="utf-8"))
    assert trace_doc["status"] == "succeeded"
    assert trace_doc["workflow"] == "minimal"
    assert len(trace_doc["steps"]) >= 1  # at least the xslt step

    # Verify step I/O files
    steps_dir = tmp_path / "trace-minimal" / "steps"
    input_files = list(steps_dir.glob("*.input"))
    output_files = list(steps_dir.glob("*.output"))
    assert len(input_files) >= 1
    assert len(output_files) >= 1

    # Verify final output was stored
    final_out = (steps_dir / "_final.output").read_text(encoding="utf-8")
    assert "<wrapped>" in final_out


@pytest.mark.asyncio
async def test_trace_integration_transform_demo(tmp_path):
    """
    Run transform_demo with tracing — confirms multi-step traces work.
    """
    os.environ["TRACES_DIR"] = str(tmp_path)

    ts = _load_trace_store(str(tmp_path))
    wr = load_workflow_runner_standalone()
    workflows = wr.load_workflows(
        REPO_ROOT / "workflows",
    )
    doc = workflows["transform_demo"]

    rt = ts.begin_run_trace("trace-td", "transform_demo")

    tf_app = load_fastapi_app("transformers")
    transport = httpx.ASGITransport(app=tf_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://transformers.test",
    ) as client:
        final, _outputs, trace, _ctx = await wr.run_workflow(
            doc,
            '<greet><name>MiniCloud</name></greet>',
            transformers_base_url="http://transformers.test",
            egress_http_url="http://unused/call",
            egress_ftp_url="http://unused/ftp",
            egress_ssh_url="http://unused/exec",
            egress_sftp_url="http://unused/sftp",
            request_id="trace-td",
            httpx_client=client,
            run_trace=rt,
        )

    doc_trace = rt.finish(status="succeeded", final_output=final, context=_ctx)

    assert doc_trace["status"] == "succeeded"
    assert len(doc_trace["steps"]) > 1  # multi-step workflow

    # Each step should have timing info
    for step_entry in doc_trace["steps"]:
        assert "started_at" in step_entry
        assert "duration_ms" in step_entry

