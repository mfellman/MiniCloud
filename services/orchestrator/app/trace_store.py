"""Persistent trace storage for workflow runs.

Stores a hierarchical trace JSON per run, with step input/output as separate
files so the trace stays compact while full payloads are always available.

Directory layout::

    {traces_dir}/
        {request_id}/
            trace.json
            steps/
                {step_id}.input
                {step_id}.output
                {loop_id}.iter_{n}.{substep_id}.input
                {loop_id}.iter_{n}.{substep_id}.output

Environment:
    TRACES_DIR          — base directory (default /app/traces)
    TRACES_MAX_RUNS     — max stored runs before oldest is pruned (default 200)
    TRACES_PREVIEW_LEN  — chars of input/output inlined in trace.json (default 500)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trace_store")

TRACES_DIR = Path(os.environ.get("TRACES_DIR", "/app/traces"))
TRACES_MAX_RUNS = int(os.environ.get("TRACES_MAX_RUNS", "200"))
TRACES_PREVIEW_LEN = int(os.environ.get("TRACES_PREVIEW_LEN", "500"))


def _preview(text: str) -> str:
    if len(text) <= TRACES_PREVIEW_LEN:
        return text
    return text[:TRACES_PREVIEW_LEN] + f"… ({len(text)} chars total)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StepTrace:
    """Builder for a single step's trace entry. Writes input/output files."""

    def __init__(self, run_dir: Path, path_prefix: str, step_id: str, step_type: str):
        self._run_dir = run_dir
        self._steps_dir = run_dir / "steps"
        self._path_prefix = path_prefix  # e.g. "" or "loop1.iter_0."
        self._step_id = step_id
        self._step_type = step_type
        self._started = time.monotonic()
        self._started_at = _now_iso()
        self._entry: dict[str, Any] = {
            "step": step_id,
            "type": step_type,
            "started_at": self._started_at,
        }

    @property
    def file_key(self) -> str:
        return f"{self._path_prefix}{self._step_id}"

    def record_input(self, text: str) -> None:
        self._entry["input_ref"] = f"steps/{self.file_key}.input"
        self._entry["input_preview"] = _preview(text)
        self._entry["input_bytes"] = len(text.encode("utf-8"))
        p = self._steps_dir / f"{self.file_key}.input"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def record_output(self, text: str) -> None:
        self._entry["output_ref"] = f"steps/{self.file_key}.output"
        self._entry["output_preview"] = _preview(text)
        self._entry["output_bytes"] = len(text.encode("utf-8"))
        p = self._steps_dir / f"{self.file_key}.output"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def record_context_snapshot(self, ctx: dict[str, str]) -> None:
        self._entry["context_after"] = dict(ctx)

    def finish(self, *, ok: bool = True, skipped: bool = False, reason: str | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        elapsed = time.monotonic() - self._started
        self._entry["finished_at"] = _now_iso()
        self._entry["duration_ms"] = round(elapsed * 1000, 1)
        if skipped:
            self._entry["status"] = "skipped"
            if reason:
                self._entry["reason"] = reason
        else:
            self._entry["status"] = "ok" if ok else "failed"
        if extra:
            self._entry.update(extra)
        return self._entry


class LoopTrace:
    """Builder for a loop step (for_each / repeat_until). Collects iteration children."""

    def __init__(self, run_dir: Path, path_prefix: str, step_id: str, step_type: str):
        self._run_dir = run_dir
        self._path_prefix = path_prefix
        self._step_id = step_id
        self._step_type = step_type
        self._started = time.monotonic()
        self._started_at = _now_iso()
        self._iterations: list[dict[str, Any]] = []

    def begin_iteration(self, index: int) -> IterationTrace:
        return IterationTrace(
            self._run_dir,
            f"{self._path_prefix}{self._step_id}.iter_{index}.",
            index,
        )

    def add_iteration(self, it: dict[str, Any]) -> None:
        self._iterations.append(it)

    def finish(self, *, ok: bool = True, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        elapsed = time.monotonic() - self._started
        entry: dict[str, Any] = {
            "step": self._step_id,
            "type": self._step_type,
            "started_at": self._started_at,
            "finished_at": _now_iso(),
            "duration_ms": round(elapsed * 1000, 1),
            "status": "ok" if ok else "failed",
            "iterations": len(self._iterations),
            "children": self._iterations,
        }
        if extra:
            entry.update(extra)
        return entry


class IterationTrace:
    """Collects step traces for one loop iteration."""

    def __init__(self, run_dir: Path, path_prefix: str, index: int):
        self._run_dir = run_dir
        self._path_prefix = path_prefix
        self._index = index
        self._steps: list[dict[str, Any]] = []

    @property
    def path_prefix(self) -> str:
        return self._path_prefix

    def step(self, step_id: str, step_type: str) -> StepTrace:
        return StepTrace(self._run_dir, self._path_prefix, step_id, step_type)

    def loop(self, step_id: str, step_type: str) -> LoopTrace:
        return LoopTrace(self._run_dir, self._path_prefix, step_id, step_type)

    def add_step(self, entry: dict[str, Any]) -> None:
        self._steps.append(entry)

    def finish(self, *, context_snapshot: dict[str, str] | None = None,
               until_matched: bool | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "iteration": self._index,
            "steps": self._steps,
        }
        if context_snapshot is not None:
            result["context"] = context_snapshot
        if until_matched is not None:
            result["until_matched"] = until_matched
        return result


class RunTrace:
    """Root trace for a single workflow run."""

    def __init__(self, request_id: str, workflow_name: str,
                 workflow_definition: list[dict[str, Any]] | None = None):
        self._request_id = request_id
        self._workflow_name = workflow_name
        self._workflow_definition = workflow_definition
        self._started = time.monotonic()
        self._started_at = _now_iso()
        self._steps: list[dict[str, Any]] = []
        self._run_dir = TRACES_DIR / request_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "steps").mkdir(exist_ok=True)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def step(self, step_id: str, step_type: str) -> StepTrace:
        return StepTrace(self._run_dir, "", step_id, step_type)

    def loop(self, step_id: str, step_type: str) -> LoopTrace:
        return LoopTrace(self._run_dir, "", step_id, step_type)

    def add_step(self, entry: dict[str, Any]) -> None:
        self._steps.append(entry)

    def finish(
        self,
        *,
        status: str = "succeeded",
        final_output: str | None = None,
        context: dict[str, str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        elapsed = time.monotonic() - self._started
        doc: dict[str, Any] = {
            "workflow": self._workflow_name,
            "request_id": self._request_id,
            "status": status,
            "started_at": self._started_at,
            "finished_at": _now_iso(),
            "duration_ms": round(elapsed * 1000, 1),
            "steps": self._steps,
        }
        if self._workflow_definition is not None:
            doc["workflow_definition"] = self._workflow_definition
        if final_output is not None:
            doc["final_output_preview"] = _preview(final_output)
            # Store full final output
            p = self._run_dir / "steps" / "_final.output"
            p.write_text(final_output, encoding="utf-8")
            doc["final_output_ref"] = "steps/_final.output"
        if context is not None:
            doc["context"] = context
        if error is not None:
            doc["error"] = error

        trace_path = self._run_dir / "trace.json"
        trace_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        _prune_old_runs()
        return doc


def begin_run_trace(request_id: str, workflow_name: str,
                    workflow_definition: list[dict[str, Any]] | None = None) -> RunTrace:
    return RunTrace(request_id, workflow_name, workflow_definition=workflow_definition)


# --- Query / listing ---

def list_traces(limit: int = 50, workflow: str | None = None) -> list[dict[str, Any]]:
    """List recent traces (newest first), reading only the top-level metadata."""
    if not TRACES_DIR.is_dir():
        return []
    runs: list[tuple[float, Path]] = []
    for p in TRACES_DIR.iterdir():
        trace_json = p / "trace.json"
        if p.is_dir() and trace_json.is_file():
            runs.append((trace_json.stat().st_mtime, p))
    runs.sort(key=lambda x: x[0], reverse=True)
    result = []
    for _, run_dir in runs:
        if len(result) >= limit:
            break
        try:
            doc = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
            if workflow and doc.get("workflow") != workflow:
                continue
            result.append({
                "request_id": doc.get("request_id"),
                "workflow": doc.get("workflow"),
                "status": doc.get("status"),
                "started_at": doc.get("started_at"),
                "duration_ms": doc.get("duration_ms"),
                "error": doc.get("error"),
            })
        except Exception:
            continue
    return result


def get_trace(request_id: str) -> dict[str, Any] | None:
    """Get the full trace JSON for a run."""
    # Validate request_id to prevent directory traversal
    safe_id = Path(request_id).name
    if safe_id != request_id or ".." in request_id:
        return None
    trace_path = TRACES_DIR / safe_id / "trace.json"
    if not trace_path.is_file():
        return None
    return json.loads(trace_path.read_text(encoding="utf-8"))


def get_step_data(request_id: str, step_path: str, kind: str) -> str | None:
    """Read input or output file for a step. kind = 'input' | 'output'."""
    safe_id = Path(request_id).name
    if safe_id != request_id or ".." in request_id:
        return None
    if kind not in ("input", "output"):
        return None
    # Sanitize step_path: only allow alphanumeric, dots, underscores, hyphens
    safe_path = step_path.replace("/", "").replace("\\", "")
    if safe_path != step_path or ".." in step_path:
        return None
    file_path = TRACES_DIR / safe_id / "steps" / f"{safe_path}.{kind}"
    if not file_path.is_file():
        return None
    return file_path.read_text(encoding="utf-8")


def _prune_old_runs() -> None:
    """Remove oldest runs if we exceed TRACES_MAX_RUNS."""
    if not TRACES_DIR.is_dir():
        return
    runs: list[tuple[float, Path]] = []
    for p in TRACES_DIR.iterdir():
        if p.is_dir() and (p / "trace.json").is_file():
            runs.append(((p / "trace.json").stat().st_mtime, p))
    if len(runs) <= TRACES_MAX_RUNS:
        return
    runs.sort(key=lambda x: x[0])
    to_remove = len(runs) - TRACES_MAX_RUNS
    for _, run_dir in runs[:to_remove]:
        try:
            shutil.rmtree(run_dir)
            LOG.info("pruned old trace: %s", run_dir.name)
        except OSError as e:
            LOG.warning("failed to prune %s: %s", run_dir.name, e)
