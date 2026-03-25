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
let allWorkflows = [];          // [{name, group, invocation, step_count, step_types}]
let currentWorkflow = null;     // full workflow detail (with steps)
let currentWorkflowName = null; // selected workflow name
let currentTrace = null;        // full trace when viewing a run
let currentRunId = null;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await loadWorkflows();
  document.getElementById("connStatus").textContent = "Connected";
  document.getElementById("connStatus").classList.add("ok");

  document.getElementById("btnViewDesign").addEventListener("click", showDesignOverlay);
  document.getElementById("btnRefreshRuns").addEventListener("click", () => loadRunsForWorkflow(currentWorkflowName));
  document.getElementById("detailClose").addEventListener("click", closeDetail);

  document.querySelectorAll(".detail-tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
});

// ---------------------------------------------------------------------------
// Sidebar: grouped workflow tree
// ---------------------------------------------------------------------------
async function loadWorkflows() {
  try {
    const data = await api("/api/workflows");
    allWorkflows = data.workflows || [];
    renderWorkflowTree(allWorkflows);
  } catch (e) {
    console.error("loadWorkflows", e);
  }
}

function renderWorkflowTree(workflows) {
  const tree = document.getElementById("workflowTree");
  tree.innerHTML = "";

  // Group by group name
  const groups = {};
  for (const w of workflows) {
    const g = w.group || "General";
    if (!groups[g]) groups[g] = [];
    groups[g].push(w);
  }

  const sortedGroups = Object.keys(groups).sort();
  for (const groupName of sortedGroups) {
    const items = groups[groupName];

    // Group header
    const header = document.createElement("li");
    header.className = "group-header";
    header.innerHTML = `<span class="chevron open">&#9656;</span> ${esc(groupName)} <span class="group-count">${items.length}</span>`;

    // Group items container
    const itemList = document.createElement("ul");
    itemList.className = "group-items";

    for (const w of items) {
      const li = document.createElement("li");
      li.className = "wf-item";
      li.dataset.name = w.name;
      li.innerHTML = `<span class="wf-icon">&#9679;</span> ${esc(w.name)}`;
      li.addEventListener("click", () => selectWorkflow(w.name));
      itemList.appendChild(li);
    }

    // Toggle collapse
    header.addEventListener("click", () => {
      itemList.classList.toggle("collapsed");
      const chev = header.querySelector(".chevron");
      chev.classList.toggle("open");
    });

    tree.appendChild(header);
    tree.appendChild(itemList);
  }
}

// ---------------------------------------------------------------------------
// Workflow detail view
// ---------------------------------------------------------------------------
async function selectWorkflow(name) {
  currentWorkflowName = name;
  currentRunId = null;
  currentTrace = null;
  closeDetail();
  highlightSidebar(name);
  setBreadcrumb([{ label: name }]);

  show("workflowPanel"); hide("runPanel"); hide("placeholder");

  try {
    const wf = await api(`/api/workflows/${encodeURIComponent(name)}`);
    currentWorkflow = wf;
    renderWorkflowDetail(wf);
  } catch (e) {
    console.error("selectWorkflow", e);
  }

  await loadRunsForWorkflow(name);
}

function renderWorkflowDetail(wf) {
  document.getElementById("wfTitle").textContent = wf.name;
  document.getElementById("wfGroupLabel").textContent = `Group: ${wf.group || "General"}`;

  // Stat cards
  const trigger = [];
  if (wf.invocation.allow_http) trigger.push("HTTP");
  if (wf.invocation.allow_schedule) trigger.push("Schedule");

  const statsEl = document.getElementById("wfStats");
  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Steps</div>
      <div class="stat-value">${wf.step_count || wf.steps.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trigger</div>
      <div class="stat-value" style="font-size:16px">${trigger.join(" + ") || "None"}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Step Types</div>
      <div class="stat-value" style="font-size:14px">${(wf.step_types || []).join(", ") || "-"}</div>
    </div>`;
}

async function loadRunsForWorkflow(name) {
  if (!name) return;
  const tbody = document.getElementById("runTableBody");
  tbody.innerHTML = `<tr><td colspan="5" style="color:var(--text-muted);text-align:center;padding:24px">Loading&#8230;</td></tr>`;

  try {
    const data = await api(`/api/traces?workflow=${encodeURIComponent(name)}&limit=50`);
    const runs = data.traces || [];
    tbody.innerHTML = "";

    if (runs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="color:var(--text-muted);text-align:center;padding:24px">No runs yet</td></tr>`;
      return;
    }

    for (const run of runs) {
      const tr = document.createElement("tr");
      const statusCls = run.status === "succeeded" ? "badge-green"
        : run.status === "failed" ? "badge-red" : "badge-orange";

      tr.innerHTML = `
        <td><span class="badge ${statusCls}">${esc(run.status)}</span></td>
        <td style="font-family:var(--mono);font-size:12px">${esc(shortId(run.request_id))}</td>
        <td>${formatTime(run.started_at)}</td>
        <td>${formatDuration(run.duration_ms)}</td>
        <td style="color:var(--accent);font-size:12px">View &rarr;</td>`;
      tr.addEventListener("click", () => selectRun(run.request_id, name));
      tbody.appendChild(tr);
    }
  } catch (e) {
    console.error("loadRunsForWorkflow", e);
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--red);text-align:center;padding:24px">Failed to load runs</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Run detail view
// ---------------------------------------------------------------------------
async function selectRun(requestId, workflowName) {
  currentRunId = requestId;
  closeDetail();
  setBreadcrumb([
    { label: workflowName || "Workflow", action: () => selectWorkflow(workflowName) },
    { label: shortId(requestId) },
  ]);

  hide("placeholder"); hide("workflowPanel"); show("runPanel");

  try {
    const trace = await api(`/api/traces/${encodeURIComponent(requestId)}`);
    currentTrace = trace;
    renderRunDetail(trace);
  } catch (e) {
    console.error("selectRun", e);
  }
}

function renderRunDetail(trace) {
  document.getElementById("runTitle").textContent = trace.workflow;

  const badge = document.getElementById("runStatus");
  badge.textContent = trace.status;
  badge.className = "badge " + (trace.status === "succeeded" ? "badge-green"
    : trace.status === "failed" ? "badge-red" : "badge-orange");

  document.getElementById("runMeta").innerHTML =
    `<span>${formatTime(trace.started_at)}</span>` +
    `<span>${formatDuration(trace.duration_ms)}</span>` +
    `<span style="font-family:var(--mono);font-size:12px">${esc(trace.request_id)}</span>`;

  const errorEl = document.getElementById("runError");
  if (trace.error) {
    errorEl.textContent = trace.error;
    errorEl.classList.remove("hidden");
  } else {
    errorEl.classList.add("hidden");
  }

  const container = document.getElementById("runGraph");
  container.innerHTML = "";

  const steps = trace.workflow_definition || trace.steps;
  const stepTraces = buildStepTraceMap(trace.steps || []);
  container.appendChild(buildPipeline(steps, stepTraces));
}

function buildStepTraceMap(traceSteps) {
  const map = {};
  for (const s of traceSteps) {
    if (s.step) map[s.step] = s;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Workflow design overlay
// ---------------------------------------------------------------------------
function showDesignOverlay() {
  if (!currentWorkflow) return;

  const overlay = document.createElement("div");
  overlay.className = "design-overlay";
  overlay.innerHTML = `
    <div class="design-modal">
      <div class="design-modal-header">
        <h3>Workflow Design: ${esc(currentWorkflow.name)}</h3>
        <button class="btn-close" id="designClose">&times;</button>
      </div>
      <div class="design-modal-body" id="designBody"></div>
    </div>`;

  document.body.appendChild(overlay);

  const body = overlay.querySelector("#designBody");
  body.appendChild(buildPipeline(currentWorkflow.steps, null));

  overlay.querySelector("#designClose").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
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

  if (step.when) {
    const whenEl = document.createElement("div");
    whenEl.style.cssText = "font-size:11px;color:var(--orange);margin-top:4px";
    const cond = step.when.equals != null ? `== "${step.when.equals}"`
      : step.when.not_equals != null ? `!= "${step.when.not_equals}"`
      : step.when.one_of ? `in [${step.when.one_of.join(", ")}]` : "";
    whenEl.textContent = `when ${step.when.context_key} ${cond}`;
    node.appendChild(whenEl);
  }

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
    ? `for_each \u2014 ${step.id} (as ${step.as_key || "item"})`
    : `repeat_until \u2014 ${step.id}`;
  const iters = trace ? ` \u00b7 ${trace.iterations || "?"} iterations` : "";

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
  document.querySelectorAll(".pipe-node.selected").forEach(n => n.classList.remove("selected"));
  if (nodeEl.classList.contains("pipe-node")) nodeEl.classList.add("selected");

  show("detailPanel");
  document.getElementById("detailTitle").textContent = `${step.id} (${step.type})`;

  const metaObj = { ...step };
  if (trace) metaObj._trace = trace;
  document.getElementById("detailMeta").textContent = JSON.stringify(metaObj, null, 2);

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
}

function switchTab(tab) {
  document.querySelectorAll(".detail-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("tabInput").classList.toggle("hidden", tab !== "input");
  document.getElementById("tabOutput").classList.toggle("hidden", tab !== "output");
  document.getElementById("tabMeta").classList.toggle("hidden", tab !== "meta");
}

// ---------------------------------------------------------------------------
// Breadcrumb navigation
// ---------------------------------------------------------------------------
function setBreadcrumb(parts) {
  const bc = document.getElementById("breadcrumb");
  bc.innerHTML = "";

  const home = document.createElement("a");
  home.textContent = "Workflows";
  home.addEventListener("click", () => {
    currentWorkflowName = null;
    currentRunId = null;
    currentTrace = null;
    closeDetail();
    clearSidebarHighlight();
    show("placeholder"); hide("workflowPanel"); hide("runPanel");
    setBreadcrumb([]);
  });
  bc.appendChild(home);

  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "/";
    bc.appendChild(sep);

    const isLast = i === parts.length - 1;
    if (isLast) {
      const span = document.createElement("span");
      span.className = "current";
      span.textContent = parts[i].label;
      bc.appendChild(span);
    } else {
      const a = document.createElement("a");
      a.textContent = parts[i].label;
      if (parts[i].action) a.addEventListener("click", parts[i].action);
      bc.appendChild(a);
    }
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function show(id) { document.getElementById(id).classList.remove("hidden"); }
function hide(id) { document.getElementById(id).classList.add("hidden"); }

function highlightSidebar(name) {
  document.querySelectorAll(".wf-item").forEach(li => {
    li.classList.toggle("active", li.dataset.name === name);
  });
}
function clearSidebarHighlight() {
  document.querySelectorAll(".wf-item").forEach(li => li.classList.remove("active"));
}

function esc(s) {
  if (s == null) return "";
  const el = document.createElement("span");
  el.textContent = String(s);
  return el.innerHTML;
}

function shortId(id) {
  if (!id) return "";
  return id.length > 12 ? id.substring(0, 8) + "\u2026" : id;
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
