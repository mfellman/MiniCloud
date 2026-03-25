/* MiniCloud Dashboard — client-side application */
"use strict";

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`API ${path}: ${r.status}`);
  return r.json();
}
async function apiText(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`API ${path}: ${r.status}`);
  return r.text();
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentView = null;   // { type: "workflow"|"run", id: string }
let selectedStep = null;  // step id for detail panel
let currentTrace = null;  // full trace object when viewing a run

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await loadWorkflows();
  await loadRuns();
  document.getElementById("connStatus").textContent = "Connected";
  document.getElementById("connStatus").classList.add("ok");

  document.getElementById("refreshRuns").addEventListener("click", loadRuns);
  document.getElementById("detailClose").addEventListener("click", closeDetail);

  // Tab switching in detail panel
  document.querySelectorAll(".detail-tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Auto-refresh runs every 10 s
  setInterval(loadRuns, 10000);
});

// ---------------------------------------------------------------------------
// Sidebar: Workflows
// ---------------------------------------------------------------------------
async function loadWorkflows() {
  try {
    const data = await api("/api/workflows");
    const ul = document.getElementById("workflowList");
    ul.innerHTML = "";
    (data.workflows || []).forEach(w => {
      const li = document.createElement("li");
      li.textContent = w.name;
      li.dataset.name = w.name;
      li.addEventListener("click", () => selectWorkflow(w.name));
      ul.appendChild(li);
    });
  } catch (e) {
    console.error("loadWorkflows", e);
  }
}

async function selectWorkflow(name) {
  currentView = { type: "workflow", id: name };
  highlightSidebar("workflowList", name);
  clearSidebarHighlight("runList");
  closeDetail();

  try {
    const wf = await api(`/api/workflows/${encodeURIComponent(name)}`);
    showWorkflowPanel(wf);
  } catch (e) {
    console.error("selectWorkflow", e);
  }
}

function showWorkflowPanel(wf) {
  hide("placeholder"); hide("runPanel"); show("workflowPanel");
  document.getElementById("wfTitle").textContent = wf.name;

  const invParts = [];
  if (wf.invocation.allow_http) invParts.push("HTTP");
  if (wf.invocation.allow_schedule) invParts.push("Schedule");
  document.getElementById("wfInvocation").textContent = invParts.join(" + ") || "None";

  const container = document.getElementById("wfGraph");
  container.innerHTML = "";
  container.appendChild(buildPipeline(wf.steps, null));
}

// ---------------------------------------------------------------------------
// Sidebar: Runs
// ---------------------------------------------------------------------------
async function loadRuns() {
  try {
    const data = await api("/api/traces?limit=50");
    const ul = document.getElementById("runList");
    ul.innerHTML = "";
    (data.traces || []).forEach(t => {
      const li = document.createElement("li");
      li.classList.add("run-item");
      li.dataset.id = t.request_id;

      const statusClass = t.status === "succeeded" ? "dot-ok"
        : t.status === "failed" ? "dot-failed" : "dot-running";

      li.innerHTML = `
        <span class="run-name"><span class="dot ${statusClass}"></span> ${esc(t.workflow)}</span>
        <span class="run-meta">
          <span>${formatTime(t.started_at)}</span>
          <span>${formatDuration(t.duration_ms)}</span>
        </span>`;
      li.addEventListener("click", () => selectRun(t.request_id));
      ul.appendChild(li);
    });
  } catch (e) {
    console.error("loadRuns", e);
  }
}

async function selectRun(requestId) {
  currentView = { type: "run", id: requestId };
  highlightSidebar("runList", requestId);
  clearSidebarHighlight("workflowList");
  closeDetail();

  try {
    const trace = await api(`/api/traces/${encodeURIComponent(requestId)}`);
    currentTrace = trace;
    showRunPanel(trace);
  } catch (e) {
    console.error("selectRun", e);
  }
}

function showRunPanel(trace) {
  hide("placeholder"); hide("workflowPanel"); show("runPanel");

  document.getElementById("runTitle").textContent = trace.workflow;

  const badge = document.getElementById("runStatus");
  badge.textContent = trace.status;
  badge.className = "badge " + (trace.status === "succeeded" ? "badge-green"
    : trace.status === "failed" ? "badge-red" : "badge-orange");

  document.getElementById("runMeta").textContent =
    `${formatTime(trace.started_at)}  ·  ${formatDuration(trace.duration_ms)}`;

  const container = document.getElementById("runGraph");
  container.innerHTML = "";

  // Use workflow_definition from trace if available, else fall back to trace steps
  const steps = trace.workflow_definition || trace.steps;
  const stepTraces = buildStepTraceMap(trace.steps || []);
  container.appendChild(buildPipeline(steps, stepTraces));
}

// ---------------------------------------------------------------------------
// Build step-trace lookup: step_id → trace entry
// ---------------------------------------------------------------------------
function buildStepTraceMap(traceSteps) {
  const map = {};
  for (const s of traceSteps) {
    if (s.step) map[s.step] = s;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Pipeline rendering
// ---------------------------------------------------------------------------
function buildPipeline(steps, stepTraces) {
  const frag = document.createElement("div");
  frag.className = "pipeline";

  steps.forEach((step, i) => {
    if (i > 0) {
      const conn = document.createElement("div");
      conn.className = "pipe-connector";
      frag.appendChild(conn);
    }

    const isLoop = step.type === "for_each" || step.type === "repeat_until";
    if (isLoop) {
      frag.appendChild(buildLoopNode(step, stepTraces));
    } else {
      frag.appendChild(buildStepNode(step, stepTraces));
    }
  });

  return frag;
}

function buildStepNode(step, stepTraces) {
  const trace = stepTraces ? stepTraces[step.id] : null;
  const node = document.createElement("div");
  node.className = `pipe-node type-${step.type}`;
  if (trace) {
    if (trace.skipped) node.classList.add("status-skipped");
    else if (trace.status === "ok" || trace.ok) node.classList.add("status-ok");
    else if (trace.status === "failed" || trace.ok === false) node.classList.add("status-failed");
  }

  const statusText = trace
    ? (trace.skipped ? "skipped" : (trace.status || (trace.ok ? "ok" : "failed")))
    : "";
  const statusColor = trace
    ? (trace.skipped ? "var(--text-muted)"
      : (trace.status === "ok" || trace.ok) ? "var(--green)" : "var(--red)")
    : "";
  const durText = trace && trace.duration_ms != null ? formatDuration(trace.duration_ms) : "";

  node.innerHTML = `
    <div class="node-header">
      <span class="node-id">${esc(step.id)}</span>
      <span class="node-type">${esc(step.type)}</span>
      ${statusText ? `<span class="node-status" style="color:${statusColor}">${esc(statusText)}</span>` : ""}
    </div>
    ${durText ? `<span class="node-duration">${durText}</span>` : ""}`;

  // When clause indicator
  if (step.when) {
    const whenEl = document.createElement("div");
    whenEl.style.cssText = "font-size:11px;color:var(--orange);margin-top:4px";
    const cond = step.when.equals != null ? `== "${step.when.equals}"`
      : step.when.not_equals != null ? `!= "${step.when.not_equals}"`
      : `in [${step.when.one_of.join(", ")}]`;
    whenEl.textContent = `when ${step.when.context_key} ${cond}`;
    node.appendChild(whenEl);
  }

  // Click handler for detail
  node.addEventListener("click", (e) => {
    e.stopPropagation();
    selectStepNode(step, trace, node);
  });

  return node;
}

function buildLoopNode(step, stepTraces) {
  const trace = stepTraces ? stepTraces[step.id] : null;
  const wrap = document.createElement("div");
  wrap.className = "pipe-loop";

  const label = step.type === "for_each"
    ? `for_each — ${step.id} (as ${step.as_key || "item"})`
    : `repeat_until — ${step.id}`;
  const iters = trace ? ` · ${trace.iterations || "?"} iterations` : "";

  wrap.innerHTML = `<div class="loop-header">
    <svg viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" d="M5.22 14.78a.75.75 0 001.06-1.06L4.56 12h8.69a.75.75 0 000-1.5H4.56l1.72-1.72a.75.75 0 00-1.06-1.06l-3 3a.75.75 0 000 1.06l3 3zm5.56-6.5a.75.75 0 11-1.06-1.06L11.44 5.5H2.75a.75.75 0 010-1.5h8.69L9.72 2.28a.75.75 0 011.06-1.06l3 3a.75.75 0 010 1.06l-3 3z"/></svg>
    ${esc(label)}${iters}
  </div>`;

  const body = document.createElement("div");
  body.className = "loop-body";
  if (step.steps && step.steps.length) {
    body.appendChild(buildPipeline(step.steps, stepTraces));
  }
  wrap.appendChild(body);

  // Click on the loop header for detail
  wrap.querySelector(".loop-header").addEventListener("click", (e) => {
    e.stopPropagation();
    selectStepNode(step, trace, wrap);
  });

  return wrap;
}

// ---------------------------------------------------------------------------
// Step detail panel
// ---------------------------------------------------------------------------
function selectStepNode(step, trace, nodeEl) {
  // Deselect previous
  document.querySelectorAll(".pipe-node.selected").forEach(n => n.classList.remove("selected"));
  if (nodeEl.classList.contains("pipe-node")) nodeEl.classList.add("selected");

  selectedStep = step.id;
  show("detailPanel");
  document.getElementById("detailTitle").textContent = `${step.id} (${step.type})`;

  // Meta tab — show step definition + trace metadata
  const metaObj = { ...step };
  if (trace) metaObj._trace = trace;
  document.getElementById("detailMeta").textContent = JSON.stringify(metaObj, null, 2);

  // Input/Output — try to load from trace API if we have a current trace/run
  if (currentTrace && trace) {
    loadStepIO(currentTrace.request_id, step.id, trace);
  } else {
    document.getElementById("detailInput").textContent = trace?.input_preview || "(no data)";
    document.getElementById("detailOutput").textContent = trace?.output_preview || "(no data)";
  }

  switchTab("input");
}

async function loadStepIO(requestId, stepId, trace) {
  const inputEl = document.getElementById("detailInput");
  const outputEl = document.getElementById("detailOutput");

  // Try full data first, fall back to preview
  try {
    inputEl.textContent = await apiText(
      `/api/traces/${encodeURIComponent(requestId)}/steps/${encodeURIComponent(stepId)}/input`
    );
  } catch {
    inputEl.textContent = trace?.input_preview || "(no input data)";
  }

  try {
    outputEl.textContent = await apiText(
      `/api/traces/${encodeURIComponent(requestId)}/steps/${encodeURIComponent(stepId)}/output`
    );
  } catch {
    outputEl.textContent = trace?.output_preview || "(no output data)";
  }
}

function closeDetail() {
  hide("detailPanel");
  document.querySelectorAll(".pipe-node.selected").forEach(n => n.classList.remove("selected"));
  selectedStep = null;
}

function switchTab(tab) {
  document.querySelectorAll(".detail-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("tabInput").classList.toggle("hidden", tab !== "input");
  document.getElementById("tabOutput").classList.toggle("hidden", tab !== "output");
  document.getElementById("tabMeta").classList.toggle("hidden", tab !== "meta");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function show(id) { document.getElementById(id).classList.remove("hidden"); }
function hide(id) { document.getElementById(id).classList.add("hidden"); }

function highlightSidebar(listId, key) {
  const ul = document.getElementById(listId);
  ul.querySelectorAll("li").forEach(li => {
    const match = (li.dataset.name === key || li.dataset.id === key);
    li.classList.toggle("active", match);
  });
}
function clearSidebarHighlight(listId) {
  document.getElementById(listId).querySelectorAll("li").forEach(li => li.classList.remove("active"));
}

function esc(s) {
  if (s == null) return "";
  const el = document.createElement("span");
  el.textContent = String(s);
  return el.innerHTML;
}

function formatTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return iso; }
}

function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
