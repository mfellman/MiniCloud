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
async function apiRequest(path, options = {}) {
  const r = await fetch(path, options);
  const text = await r.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }
  if (!r.ok) {
    throw new Error(data.detail || `API ${path}: ${r.status}`);
  }
  return data;
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
let rerunContext = null;        // {workflowName, runId, originalPayload}
let rabbitStatus = null;
let currentStorageBucket = null;
let currentStorageKeys = [];
let currentStoragePage = 1;
let currentStorageObject = null;
let currentStorageObjectBucket = null;
let currentStorageObjectKey = null;
let identitySession = { authenticated: false, username: null, groups: [], scopes: [] };
let iamUsers = [];
let iamPermissions = [];
let selectedIamUser = null;
let selectedIamUserPermissions = [];
let authGateBound = false;
const STORAGE_PAGE_SIZE = 50;
const DESIGN_PRESET_KEY_PREFIX = "minicloud.designPresets.";
const THEME_PREF_KEY = "minicloud.dashboardTheme";
let authSession = { auth_enabled: false, username: null };
const loopViewState = {};

// ---------------------------------------------------------------------------
// Theme helpers
// ---------------------------------------------------------------------------
function readThemePreference() {
  const raw = localStorage.getItem(THEME_PREF_KEY) || "auto";
  return ["auto", "dark", "light"].includes(raw) ? raw : "auto";
}

function resolveTheme(preference) {
  if (preference === "dark" || preference === "light") return preference;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(preference) {
  const resolved = resolveTheme(preference);
  document.documentElement.setAttribute("data-theme", resolved);
}

function initTheme() {
  const preference = readThemePreference();
  applyTheme(preference);

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (readThemePreference() === "auto") {
      applyTheme("auto");
    }
  });
}

function initThemeSelector() {
  const selectEl = document.getElementById("themeSelect");
  if (!selectEl) return;

  selectEl.value = readThemePreference();
  selectEl.addEventListener("change", () => {
    const next = selectEl.value;
    localStorage.setItem(THEME_PREF_KEY, next);
    applyTheme(next);
  });
}

initTheme();

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  initThemeSelector();
  const authenticated = await ensureAuthenticatedGate();
  if (!authenticated) {
    return;
  }

  await loadAuthSession();
  await loadWorkflows();
  document.getElementById("connStatus").textContent = "Connected";
  document.getElementById("connStatus").classList.add("ok");

  document.getElementById("btnViewDesign").addEventListener("click", showDesignOverlay);
  document.getElementById("btnRefreshRuns").addEventListener("click", () => loadRunsForWorkflow(currentWorkflowName));
  document.getElementById("navRabbitMQ").addEventListener("click", selectRabbitMQView);
  document.getElementById("navStorage").addEventListener("click", selectStorageView);
  document.getElementById("navAccess").addEventListener("click", selectAccessView);
  document.getElementById("btnRabbitRefresh").addEventListener("click", loadRabbitMQView);
  document.getElementById("btnRabbitPeek").addEventListener("click", peekRabbitMessagesFromInput);
  document.getElementById("btnStorageRefresh").addEventListener("click", loadStorageView);
  document.getElementById("btnStorageLoadKeys").addEventListener("click", loadStorageKeysFromInput);
  document.getElementById("btnStoragePrevPage").addEventListener("click", () => changeStoragePage(-1));
  document.getElementById("btnStorageNextPage").addEventListener("click", () => changeStoragePage(1));
  document.getElementById("btnStorageDownload").addEventListener("click", downloadStorageObject);
  document.getElementById("btnAccessRefresh").addEventListener("click", loadAccessView);
  document.getElementById("btnAccessLogin").addEventListener("click", accessLogin);
  document.getElementById("btnAccessSave").addEventListener("click", saveAccessPermissions);
  document.getElementById("accessUserSelect").addEventListener("change", onAccessUserChanged);
  document.getElementById("accessWorkflowSelect").addEventListener("change", renderAccessPermissionToggles);
  document.getElementById("btnSignOut").addEventListener("click", signOutDashboard);
  document.getElementById("storageKeyFilter").addEventListener("input", () => {
    currentStoragePage = 1;
    renderStorageKeysPage();
  });
  document.getElementById("detailClose").addEventListener("click", closeDetail);
  document.getElementById("copyDetailInput").addEventListener("click", () => copyTextFromElement("detailInput"));
  document.getElementById("copyDetailOutput").addEventListener("click", () => copyTextFromElement("detailOutput"));

  // Rerun modal handlers
  document.getElementById("rerunModalClose").addEventListener("click", closeRerunModal);
  document.getElementById("rerunModalBackdrop").addEventListener("click", closeRerunModal);
  document.getElementById("rerunModalCancel").addEventListener("click", closeRerunModal);
  document.getElementById("rerunModalConfirm").addEventListener("click", submitRerun);

  // Schedule modal handlers
  document.getElementById("btnScheduleWorkflow").addEventListener("click", openScheduleModal);
  document.getElementById("btnViewSchedules").addEventListener("click", openSchedulesListModal);
  document.getElementById("scheduleModalClose").addEventListener("click", closeScheduleModal);
  document.getElementById("scheduleModalBackdrop").addEventListener("click", closeScheduleModal);
  document.getElementById("scheduleModalCancel").addEventListener("click", closeScheduleModal);
  document.getElementById("scheduleModalConfirm").addEventListener("click", submitSchedule);
  document.getElementById("scheduleTemplateSelect").addEventListener("change", onScheduleTemplateChanged);

  // Schedules list modal
  document.getElementById("schedulesListModalBackdrop").addEventListener("click", closeSchedulesListModal);
  document.getElementById("schedulesListModalClose").addEventListener("click", closeSchedulesListModal);
  document.getElementById("schedulesListModalClose2").addEventListener("click", closeSchedulesListModal);

  // Schedule templates panel (admin)
  document.getElementById("navScheduleTemplates").addEventListener("click", selectScheduleTemplatesView);
  document.getElementById("btnCreateTemplate").addEventListener("click", openTemplateModal);
  document.getElementById("btnTemplatesRefresh").addEventListener("click", loadScheduleTemplates);
  document.getElementById("templateModalClose").addEventListener("click", closeTemplateModal);
  document.getElementById("templateModalBackdrop").addEventListener("click", closeTemplateModal);
  document.getElementById("templateModalCancel").addEventListener("click", closeTemplateModal);
  document.getElementById("templateModalConfirm").addEventListener("click", submitTemplate);

  document.querySelectorAll(".detail-tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
});

function showAuthGate(message = "") {
  const gate = document.getElementById("authGate");
  const status = document.getElementById("authGateStatus");
  gate.classList.remove("hidden");
  if (message) {
    status.textContent = message;
    status.classList.add("error");
  } else {
    status.textContent = "Use default users: admin/admin, operator/operator, viewer/viewer.";
    status.classList.remove("error");
  }
}

function hideAuthGate() {
  const gate = document.getElementById("authGate");
  gate.classList.add("hidden");
}

function bindAuthGateHandlers() {
  if (authGateBound) return;
  authGateBound = true;

  const loginBtn = document.getElementById("btnAuthLogin");
  const usernameEl = document.getElementById("authLoginUsername");
  const passwordEl = document.getElementById("authLoginPassword");

  const doLogin = async () => {
    const username = (usernameEl.value || "").trim();
    const password = (passwordEl.value || "").trim();
    if (!username || !password) {
      showAuthGate("Username and password are required.");
      return;
    }
    try {
      await apiRequest("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      window.location.reload();
    } catch (e) {
      showAuthGate(`Login failed: ${e.message || e}`);
    }
  };

  loginBtn.addEventListener("click", doLogin);
  passwordEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      doLogin();
    }
  });
}

async function ensureAuthenticatedGate() {
  bindAuthGateHandlers();
  await loadIdentitySession();
  if (identitySession.authenticated) {
    hideAuthGate();
    return true;
  }
  showAuthGate();
  return false;
}

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
  currentWorkflow = null;
  closeDetail();
  highlightSidebar(name);
  setBreadcrumb([{ label: name }]);

  show("workflowPanel"); hide("runPanel"); hide("rabbitPanel"); hide("storagePanel"); hide("accessPanel"); hide("scheduleTemplatesPanel"); hide("placeholder");
  document.getElementById("wfTitle").textContent = name;
  document.getElementById("wfGroupLabel").textContent = "Loading\u2026";
  document.getElementById("wfStats").innerHTML = "";

  try {
    const wf = await api(`/api/workflows/${encodeURIComponent(name)}`);
    currentWorkflow = normalizeWorkflow(wf, name);
    renderWorkflowDetail(currentWorkflow);
  } catch (e) {
    console.error("selectWorkflow", e);
    const fallback = allWorkflows.find(w => w && w.name === name);
    if (fallback) {
      currentWorkflow = normalizeWorkflow(fallback, name);
      renderWorkflowDetail(currentWorkflow);
      document.getElementById("wfGroupLabel").textContent = "Workflow detail unavailable";
    } else {
      document.getElementById("wfGroupLabel").textContent = "Error loading workflow";
    }
  }

  await loadRunsForWorkflow(name);
}

function normalizeWorkflow(raw, fallbackName = "") {
  const wf = raw || {};
  const steps = Array.isArray(wf.steps) ? wf.steps : [];
  const invocationRaw = wf.invocation && typeof wf.invocation === "object" ? wf.invocation : {};
  const stepTypes = Array.isArray(wf.step_types) ? wf.step_types : [];
  const stepCount = Number.isFinite(wf.step_count) ? wf.step_count : steps.length;

  const examplePayloads = Array.isArray(wf.example_payloads)
    ? wf.example_payloads
        .filter(ep => ep && typeof ep.name === "string" && ep.payload && typeof ep.payload === "object")
        .map(ep => ({ name: ep.name.trim(), payload: ep.payload }))
        .filter(ep => ep.name)
    : [];

  return {
    ...wf,
    name: typeof wf.name === "string" && wf.name ? wf.name : fallbackName,
    group: typeof wf.group === "string" && wf.group ? wf.group : "General",
    invocation: {
      allow_http: !!invocationRaw.allow_http,
      allow_schedule: !!invocationRaw.allow_schedule,
    },
    example_payloads: examplePayloads,
    steps,
    step_types: stepTypes,
    step_count: stepCount,
  };
}

function renderWorkflowDetail(wf) {
  document.getElementById("wfTitle").textContent = wf.name;
  document.getElementById("wfGroupLabel").textContent = `Group: ${wf.group || "General"}`;

  // Stat cards
  const trigger = [];
  if (wf.invocation && wf.invocation.allow_http) trigger.push("HTTP");
  if (wf.invocation && wf.invocation.allow_schedule) trigger.push("Schedule");

  const statsEl = document.getElementById("wfStats");
  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Steps</div>
      <div class="stat-value">${Number.isFinite(wf.step_count) ? wf.step_count : ((wf.steps || []).length)}</div>
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

      const rerunBtn = document.createElement("button");
      rerunBtn.className = "btn btn-small";
      rerunBtn.textContent = "⟲ Re-run";
      rerunBtn.title = "Re-run this workflow";
      rerunBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openRerunModal(run.request_id, name);
      });

      tr.innerHTML = `
        <td><span class="badge ${statusCls}">${esc(run.status)}</span></td>
        <td style="font-family:var(--mono);font-size:12px">${esc(shortId(run.request_id))}</td>
        <td>${formatTime(run.started_at)}</td>
        <td>${formatDuration(run.duration_ms)}</td>
        <td style="color:var(--accent);font-size:12px">View &rarr;</td>`;
      
      // Insert the rerun button into the last td
      const lastTd = tr.lastChild;
      lastTd.appendChild(rerunBtn);
      
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

  hide("placeholder"); hide("workflowPanel"); hide("rabbitPanel"); hide("storagePanel"); hide("accessPanel"); hide("scheduleTemplatesPanel"); show("runPanel");

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
  container.appendChild(buildPipeline(steps, trace.steps || [], { traceRoot: trace, scopePath: "" }));
}

// ---------------------------------------------------------------------------
// Rerun modal
// ---------------------------------------------------------------------------
async function openRerunModal(runId, workflowName) {
  try {
    // Fetch the original trace to extract the payload
    const trace = await api(`/api/traces/${encodeURIComponent(runId)}`);
    
    // Extract the original payload from the trace
    const originalPayload = trace.payload || { xml: "" };
    const payloadStr = typeof originalPayload === "string" 
      ? originalPayload 
      : JSON.stringify(originalPayload, null, 2);
    
    // Store the context for submission
    rerunContext = { workflowName, runId, originalPayload };
    
    // Populate and show the modal
    document.getElementById("rerunWorkflowName").textContent = 
      `Workflow: ${esc(workflowName)} — Original run: ${esc(shortId(runId))}`;
    document.getElementById("rerunPayload").value = payloadStr;
    document.getElementById("rerunError").classList.add("hidden");
    
    const modal = document.getElementById("rerunModal");
    modal.classList.remove("hidden");
  } catch (e) {
    console.error("openRerunModal", e);
    alert(`Failed to open rerun modal: ${e.message || e}`);
  }
}

function closeRerunModal() {
  const modal = document.getElementById("rerunModal");
  modal.classList.add("hidden");
  rerunContext = null;
}

async function submitRerun() {
  if (!rerunContext) {
    console.warn("submitRerun: no rerun context");
    return;
  }

  const payloadEl = document.getElementById("rerunPayload");
  const errorEl = document.getElementById("rerunError");
  const confirmBtn = document.getElementById("rerunModalConfirm");
  
  let parsed;
  try {
    parsed = JSON.parse(payloadEl.value || "{}");
  } catch (e) {
    errorEl.textContent = `Invalid JSON payload: ${e.message || e}`;
    errorEl.classList.remove("hidden");
    return;
  }

  if (!parsed || typeof parsed !== "object" || typeof parsed.xml !== "string" || !parsed.xml.trim()) {
    errorEl.textContent = "Payload must be a JSON object with non-empty string field 'xml'.";
    errorEl.classList.remove("hidden");
    return;
  }

  confirmBtn.disabled = true;
  errorEl.classList.add("hidden");
  
  try {
    const resp = await fetch(`/api/run/${encodeURIComponent(rerunContext.workflowName)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ xml: parsed.xml }),
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      errorEl.textContent = `Re-run failed (${resp.status}): ${data.detail || JSON.stringify(data)}`;
      errorEl.classList.remove("hidden");
      return;
    }

    // Success — close modal and refresh the run history
    closeRerunModal();
    await loadRunsForWorkflow(rerunContext.workflowName);
  } catch (e) {
    errorEl.textContent = `Re-run failed: ${e.message || e}`;
    errorEl.classList.remove("hidden");
  } finally {
    confirmBtn.disabled = false;
  }
}

function buildStepTraceMap(traceSteps) {
  const map = {};
  for (const s of traceSteps) {
    if (s.step) map[s.step] = s;
  }
  return map;
}

function getStepTracePath(trace, fallbackStepId = "") {
  if (trace && typeof trace.input_ref === "string" && trace.input_ref.startsWith("steps/")) {
    return trace.input_ref.slice("steps/".length).replace(/\.input$/, "");
  }
  if (trace && typeof trace.output_ref === "string" && trace.output_ref.startsWith("steps/")) {
    return trace.output_ref.slice("steps/".length).replace(/\.output$/, "");
  }
  return fallbackStepId;
}

// ---------------------------------------------------------------------------
// Schedule modal
// ---------------------------------------------------------------------------
const SCHEDULER_API_URL = "/api/scheduler";  // Proxied through orchestrator to scheduler service
let scheduleContext = { workflow: null };
let scheduleTemplates = [];  // Cache van available templates

async function openScheduleModal() {
  if (!currentWorkflowName) {
    alert("No workflow selected");
    return;
  }

  // Load templates
  await loadScheduleTemplatesForDropdown();

  // Pre-fill with first example payload
  let defaultPayload = "<root/>";
  if (currentWorkflow && Array.isArray(currentWorkflow.example_payloads) && currentWorkflow.example_payloads.length > 0) {
    const ep = currentWorkflow.example_payloads[0];
    if (ep && ep.payload && ep.payload.xml) {
      defaultPayload = ep.payload.xml;
    }
  }

  scheduleContext = { workflow: currentWorkflowName };
  document.getElementById("scheduleWorkflowName").textContent = `Workflow: ${esc(currentWorkflowName)}`;
  document.getElementById("scheduleTemplateSelect").value = "";
  document.getElementById("scheduleCronExpression").value = "0 0 * * *";  // Default: daily midnight
  document.getElementById("schedulePayload").value = defaultPayload;
  document.getElementById("scheduleError").classList.add("hidden");

  const modal = document.getElementById("scheduleModal");
  modal.classList.remove("hidden");
}

async function loadScheduleTemplatesForDropdown() {
  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/named-schedules`);
    if (!resp.ok) return;
    const data = await resp.json();
    scheduleTemplates = data.schedules || [];

    const selectEl = document.getElementById("scheduleTemplateSelect");
    // Clear old options (keep the first one)
    while (selectEl.children.length > 1) {
      selectEl.removeChild(selectEl.children[1]);
    }

    for (const t of scheduleTemplates) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `${t.name} (${t.cron_expression})`;
      selectEl.appendChild(opt);
    }
  } catch (e) {
    console.error("loadScheduleTemplatesForDropdown", e);
  }
}

function onScheduleTemplateChanged() {
  const templateId = document.getElementById("scheduleTemplateSelect").value;
  if (!templateId) {
    document.getElementById("scheduleCronExpression").value = "0 0 * * *";
    return;
  }

  const template = scheduleTemplates.find(t => t.id === templateId);
  if (template) {
    document.getElementById("scheduleCronExpression").value = template.cron_expression;
  }
}

function closeScheduleModal() {
  const modal = document.getElementById("scheduleModal");
  modal.classList.add("hidden");
  scheduleContext = { workflow: null };
}

async function submitSchedule() {
  const cronEl = document.getElementById("scheduleCronExpression");
  const payloadEl = document.getElementById("schedulePayload");
  const templateSelectEl = document.getElementById("scheduleTemplateSelect");
  const errorEl = document.getElementById("scheduleError");
  const confirmBtn = document.getElementById("scheduleModalConfirm");

  const cron = (cronEl.value || "").trim();
  const payload = (payloadEl.value || "").trim();
  const templateId = (templateSelectEl.value || "").trim() || null;

  if (!cron) {
    errorEl.textContent = "Cron expression is required.";
    errorEl.classList.remove("hidden");
    return;
  }

  if (!payload) {
    errorEl.textContent = "Payload is required.";
    errorEl.classList.remove("hidden");
    return;
  }

  confirmBtn.disabled = true;
  errorEl.classList.add("hidden");

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/schedules`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User": identitySession.username || "anonymous"
      },
      body: JSON.stringify({
        workflow_name: scheduleContext.workflow,
        cron_expression: cron,
        named_schedule_id: templateId,
        payload: payload
      })
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      const detail = Array.isArray(data.detail)
        ? data.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (data.detail || JSON.stringify(data));
      errorEl.textContent = `Schedule failed (${resp.status}): ${detail}`;
      errorEl.classList.remove("hidden");
      return;
    }

    closeScheduleModal();
    // Show success and refresh schedules list
    alert("Schedule created successfully!");
    await openSchedulesListModal();  // Refresh the list
  } catch (e) {
    errorEl.textContent = `Schedule failed: ${e.message || e}`;
    errorEl.classList.remove("hidden");
  } finally {
    confirmBtn.disabled = false;
  }
}

async function openSchedulesListModal() {
  const modal = document.getElementById("schedulesListModal");
  const emptyEl = document.getElementById("schedulesListEmpty");
  const tableEl = document.getElementById("schedulesListTable");
  const bodyEl = document.getElementById("schedulesListBody");

  modal.classList.remove("hidden");

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/schedules`, {
      headers: { "X-User": identitySession.username || "anonymous" }
    });

    if (!resp.ok) {
      emptyEl.textContent = `Failed to load schedules: ${resp.status}`;
      tableEl.style.display = "none";
      return;
    }

    const schedules = await resp.json();
    // Filter to current workflow only (if needed)
    const filtered = schedules.filter(s => !currentWorkflowName || s.workflow_name === currentWorkflowName);

    if (filtered.length === 0) {
      emptyEl.textContent = "No schedules for this workflow.";
      tableEl.style.display = "none";
      return;
    }

    bodyEl.innerHTML = "";
    for (const sched of filtered) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(sched.workflow_name)}</td>
        <td><code style="font-size:12px;background:var(--bg-alt);padding:2px 4px;">${esc(sched.cron_expression)}</code></td>
        <td>${sched.next_run_time ? new Date(sched.next_run_time).toLocaleString() : "Never"}</td>
        <td>
          <button class="btn-small" onclick="runScheduleNow('${esc(sched.job_id)}', event)">Run now</button>
          <button class="btn-small" onclick="deleteSchedule('${esc(sched.job_id)}', event)">Delete</button>
        </td>
      `;
      bodyEl.appendChild(tr);
    }

    emptyEl.style.display = "none";
    tableEl.style.display = "table";
  } catch (e) {
    console.error("openSchedulesListModal", e);
    emptyEl.textContent = `Error loading schedules: ${e.message || e}`;
    tableEl.style.display = "none";
  }
}

function closeSchedulesListModal() {
  const modal = document.getElementById("schedulesListModal");
  modal.classList.add("hidden");
}

async function runScheduleNow(jobId, event) {
  event.stopPropagation();

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/schedules/${encodeURIComponent(jobId)}/run`, {
      method: "POST",
      headers: { "X-User": identitySession.username || "anonymous" }
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      const detail = Array.isArray(data.detail)
        ? data.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (data.detail || JSON.stringify(data));
      alert(`Manual run failed (${resp.status}): ${detail}`);
      return;
    }

    await openSchedulesListModal();
    alert("Manual run triggered successfully.");
  } catch (e) {
    alert(`Manual run failed: ${e.message || e}`);
  }
}

async function deleteSchedule(jobId, event) {
  event.stopPropagation();
  if (!confirm(`Delete this schedule?`)) return;

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/schedules/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      headers: { "X-User": identitySession.username || "anonymous" }
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      const detail = Array.isArray(data.detail)
        ? data.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (data.detail || JSON.stringify(data));
      alert(`Delete failed (${resp.status}): ${detail}`);
      return;
    }

    // Refresh the schedules list
    await openSchedulesListModal();
  } catch (e) {
    alert(`Delete failed: ${e.message || e}`);
  }
}

// ---------------------------------------------------------------------------
// Schedule Template Management (Admin)
// ---------------------------------------------------------------------------
let templateEditContext = { id: null };

function selectScheduleTemplatesView() {
  currentWorkflowName = null;
  currentRunId = null;
  currentTrace = null;
  currentWorkflow = null;
  closeDetail();
  setBreadcrumb([{ label: "Schedule Templates" }]);

  hide("workflowPanel");
  hide("runPanel");
  hide("rabbitPanel");
  hide("storagePanel");
  hide("accessPanel");
  hide("placeholder");
  show("scheduleTemplatesPanel");

  loadScheduleTemplates();
}

async function loadScheduleTemplates() {
  const emptyEl = document.getElementById("templatesEmpty");
  const tableEl = document.getElementById("templatesListTable");
  const bodyEl = document.getElementById("templatesListBody");

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/named-schedules`);
    if (!resp.ok) {
      emptyEl.textContent = `Failed to load templates: ${resp.status}`;
      tableEl.style.display = "none";
      return;
    }

    const data = await resp.json();
    const templates = data.schedules || [];

    if (templates.length === 0) {
      emptyEl.textContent = "No templates created yet.";
      tableEl.style.display = "none";
      return;
    }

    bodyEl.innerHTML = "";
    for (const t of templates) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${esc(t.name)}</strong></td>
        <td><code style="font-size:12px;background:var(--bg-alt);padding:2px 4px;">${esc(t.cron_expression)}</code></td>
        <td>${esc(t.description || "—")}</td>
        <td>
          <button class="btn-small" onclick="editTemplate('${esc(t.id)}', event)">Edit</button>
          <button class="btn-small" onclick="deleteTemplate('${esc(t.id)}', event)" style="color:var(--error);">Delete</button>
        </td>
      `;
      bodyEl.appendChild(tr);
    }

    emptyEl.style.display = "none";
    tableEl.style.display = "table";
  } catch (e) {
    console.error("loadScheduleTemplates", e);
    emptyEl.textContent = `Error loading templates: ${e.message || e}`;
    tableEl.style.display = "none";
  }
}

function openTemplateModal() {
  templateEditContext = { id: null };
  document.getElementById("templateModalTitle").textContent = "Create Schedule Template";
  document.getElementById("templateName").value = "";
  document.getElementById("templateCronExpression").value = "0 0 * * *";
  document.getElementById("templateDescription").value = "";
  document.getElementById("templateError").classList.add("hidden");

  const modal = document.getElementById("templateModal");
  modal.classList.remove("hidden");
}

function editTemplate(templateId, event) {
  event.stopPropagation();
  const template = scheduleTemplates.find(t => t.id === templateId);
  if (!template) {
    alert("Template not found");
    return;
  }

  templateEditContext = { id: templateId };
  document.getElementById("templateModalTitle").textContent = "Edit Schedule Template";
  document.getElementById("templateName").value = template.name;
  document.getElementById("templateCronExpression").value = template.cron_expression;
  document.getElementById("templateDescription").value = template.description || "";
  document.getElementById("templateError").classList.add("hidden");

  const modal = document.getElementById("templateModal");
  modal.classList.remove("hidden");
}

function closeTemplateModal() {
  const modal = document.getElementById("templateModal");
  modal.classList.add("hidden");
  templateEditContext = { id: null };
}

async function submitTemplate() {
  const nameEl = document.getElementById("templateName");
  const cronEl = document.getElementById("templateCronExpression");
  const descEl = document.getElementById("templateDescription");
  const errorEl = document.getElementById("templateError");
  const confirmBtn = document.getElementById("templateModalConfirm");

  const name = (nameEl.value || "").trim();
  const cron = (cronEl.value || "").trim();
  const desc = (descEl.value || "").trim();

  if (!name) {
    errorEl.textContent = "Name is required.";
    errorEl.classList.remove("hidden");
    return;
  }

  if (!cron) {
    errorEl.textContent = "Cron expression is required.";
    errorEl.classList.remove("hidden");
    return;
  }

  confirmBtn.disabled = true;
  errorEl.classList.add("hidden");

  try {
    const isCreate = !templateEditContext.id;
    const url = isCreate 
      ? `${SCHEDULER_API_URL}/named-schedules`
      : `${SCHEDULER_API_URL}/named-schedules/${encodeURIComponent(templateEditContext.id)}`;
    const method = isCreate ? "POST" : "PUT";

    const resp = await fetch(url, {
      method: method,
      headers: {
        "Content-Type": "application/json",
        "X-User": identitySession.username || "anonymous"
      },
      body: JSON.stringify({
        name: name,
        cron_expression: cron,
        description: desc
      })
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      errorEl.textContent = `Failed (${resp.status}): ${data.detail || JSON.stringify(data)}`;
      errorEl.classList.remove("hidden");
      return;
    }

    closeTemplateModal();
    alert(`Template ${isCreate ? "created" : "updated"} successfully!`);
    await loadScheduleTemplates();
    await loadScheduleTemplatesForDropdown();
  } catch (e) {
    errorEl.textContent = `Failed: ${e.message || e}`;
    errorEl.classList.remove("hidden");
  } finally {
    confirmBtn.disabled = false;
  }
}

async function deleteTemplate(templateId, event) {
  event.stopPropagation();
  if (!confirm(`Delete this template?`)) return;

  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/named-schedules/${encodeURIComponent(templateId)}`, {
      method: "DELETE",
      headers: { "X-User": identitySession.username || "anonymous" }
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = {};
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
      alert(`Delete failed (${resp.status}): ${data.detail || JSON.stringify(data)}`);
      return;
    }

    alert("Template deleted successfully!");
    await loadScheduleTemplates();
    await loadScheduleTemplatesForDropdown();
  } catch (e) {
    alert(`Delete failed: ${e.message || e}`);
  }
}


// ---------------------------------------------------------------------------
// Workflow design overlay
// ---------------------------------------------------------------------------
function showDesignOverlay() {
  if (!currentWorkflow) {
    console.warn("showDesignOverlay: no workflow loaded");
    return;
  }
  if (!currentWorkflow.steps || currentWorkflow.steps.length === 0) {
    console.warn("showDesignOverlay: workflow has no steps");
    return;
  }

  const overlay = document.createElement("div");
  const allowsHttp = !!(currentWorkflow.invocation && currentWorkflow.invocation.allow_http);
  const canRun = allowsHttp && canRunWorkflow(currentWorkflow.name);
  const canManualScheduledRun = canTriggerScheduledRun();
  const localPresets = loadDesignPresets(currentWorkflow.name);
  const workflowPresets = getWorkflowExamplePresets(currentWorkflow);
  const presets = [...workflowPresets];
  for (const p of localPresets) {
    if (!presets.some(existing => existing.name === p.name)) {
      presets.push(p);
    }
  }
  const initialPayload = presets.length > 0
    ? String(presets[0].payload || defaultDesignPayload(currentWorkflow))
    : defaultDesignPayload(currentWorkflow);
  overlay.className = "design-overlay";
  overlay.innerHTML = `
    <div class="design-modal">
      <div class="design-modal-header">
        <h3>Workflow Design: ${esc(currentWorkflow.name)}</h3>
        <button class="btn-close" id="designClose">&times;</button>
      </div>
      <div class="design-modal-body" id="designBody">
        <section class="design-trigger">
          <div class="design-trigger-header">
            <h4>Trigger from browser</h4>
            <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;">
              <button class="btn btn-primary" id="designRunBtn" ${canRun ? "" : "disabled"}>Run workflow</button>
              <button class="btn" id="designRunScheduledBtn" ${canManualScheduledRun ? "" : "disabled"}>Run scheduled now</button>
            </div>
          </div>
          <p class="design-trigger-note">${canRun ? "Workflow allows HTTP invocation and your account has run rights." : (allowsHttp ? "Workflow allows HTTP invocation, but your account lacks run rights." : "Workflow does not allow HTTP invocation (allow_http=false). Use 'Run scheduled now' for manual scheduler trigger.")}</p>
          <div class="design-preset-row">
            <label for="designPresetSelect">Payload preset:</label>
            <select id="designPresetSelect" class="design-preset-select"></select>
            <input id="designPresetName" class="design-preset-name" type="text" placeholder="preset name" />
            <button class="btn" id="designPresetSaveBtn">Save</button>
            <button class="btn" id="designPresetDeleteBtn">Delete</button>
          </div>
          <label for="designPayload">Payload (JSON):</label>
          <textarea id="designPayload" class="design-payload">${esc(initialPayload)}</textarea>
          <pre id="designRunOutput" class="code-block">Run output will appear here.</pre>
        </section>

        <section class="design-layout">
          <div class="design-flow" id="designFlow"></div>
          <aside class="design-step-detail" id="designStepDetailPanel">
            <div class="design-step-detail-title" id="designStepDetailTitle">Step details</div>
            <div class="design-step-detail-subtitle">Click a step in the design to inspect configuration.</div>
            <pre id="designStepDetail" class="code-block">No step selected.</pre>
            <div class="design-step-tabs" id="designStepTabs">
              <button class="design-step-tab active" data-design-tab="info">Info</button>
              <button class="design-step-tab" data-design-tab="code">Code</button>
            </div>
            <pre id="designStepCode" class="code-block hidden">No step selected.</pre>
          </aside>
        </section>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  const flowEl = overlay.querySelector("#designFlow");
  flowEl.appendChild(buildPipeline(currentWorkflow.steps, null));

  const runBtn = overlay.querySelector("#designRunBtn");
  if (runBtn) {
    runBtn.addEventListener("click", () => runWorkflowFromDesignOverlay(overlay));
  }
  const runScheduledBtn = overlay.querySelector("#designRunScheduledBtn");
  if (runScheduledBtn) {
    runScheduledBtn.addEventListener("click", () => runWorkflowViaSchedulerFromDesignOverlay(overlay));
  }

  initDesignStepTabs(overlay);

  initDesignPresetControls(overlay, presets);

  overlay.querySelector("#designClose").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

function initDesignStepTabs(overlay) {
  const tabs = overlay.querySelectorAll("#designStepTabs .design-step-tab");
  const infoEl = overlay.querySelector("#designStepDetail");
  const codeEl = overlay.querySelector("#designStepCode");
  if (!tabs.length || !infoEl || !codeEl) return;

  tabs.forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      const target = tabBtn.getAttribute("data-design-tab") || "info";
      tabs.forEach((b) => b.classList.remove("active"));
      tabBtn.classList.add("active");

      if (target === "code") {
        infoEl.classList.add("hidden");
        codeEl.classList.remove("hidden");
      } else {
        codeEl.classList.add("hidden");
        infoEl.classList.remove("hidden");
      }
    });
  });
}

function getWorkflowExamplePresets(workflow) {
  if (!workflow || !Array.isArray(workflow.example_payloads)) {
    return [];
  }
  return workflow.example_payloads
    .filter(ep => ep && typeof ep.name === "string" && ep.payload && typeof ep.payload === "object")
    .map(ep => ({ name: ep.name.trim(), payload: JSON.stringify(ep.payload, null, 2) }))
    .filter(ep => ep.name && ep.payload);
}

function defaultDesignPayload(workflow = currentWorkflow) {
  const preset = getWorkflowExamplePresets(workflow)[0];
  if (preset && preset.payload) {
    return preset.payload;
  }
  return JSON.stringify({ xml: "<?xml version=\"1.0\"?><root/>" }, null, 2);
}

function designPresetStorageKey(workflowName) {
  return `${DESIGN_PRESET_KEY_PREFIX}${workflowName}`;
}

function loadDesignPresets(workflowName) {
  try {
    const raw = localStorage.getItem(designPresetStorageKey(workflowName));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(p => p && typeof p.name === "string" && typeof p.payload === "string")
      .map(p => ({ name: p.name.trim(), payload: p.payload }))
      .filter(p => p.name);
  } catch {
    return [];
  }
}

function saveDesignPresets(workflowName, presets) {
  try {
    localStorage.setItem(designPresetStorageKey(workflowName), JSON.stringify(presets));
  } catch (_e) {
    // Ignore quota/storage errors in browser-only UX helper.
  }
}

function initDesignPresetControls(overlay, presets) {
  const selectEl = overlay.querySelector("#designPresetSelect");
  const nameEl = overlay.querySelector("#designPresetName");
  const payloadEl = overlay.querySelector("#designPayload");
  const saveBtn = overlay.querySelector("#designPresetSaveBtn");
  const deleteBtn = overlay.querySelector("#designPresetDeleteBtn");

  const renderOptions = (selected = "") => {
    selectEl.innerHTML = "";
    const custom = document.createElement("option");
    custom.value = "__custom__";
    custom.textContent = "Custom payload";
    selectEl.appendChild(custom);

    for (const p of presets) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name;
      selectEl.appendChild(opt);
    }
    if (selected && presets.some(p => p.name === selected)) {
      selectEl.value = selected;
      nameEl.value = selected;
    } else {
      selectEl.value = "__custom__";
      if (!nameEl.value) nameEl.value = "";
    }
  };

  renderOptions(presets[0]?.name || "");
  if (presets[0]) {
    payloadEl.value = presets[0].payload;
    nameEl.value = presets[0].name;
  }

  selectEl.addEventListener("change", () => {
    const selected = selectEl.value;
    if (selected === "__custom__") {
      return;
    }
    const preset = presets.find(p => p.name === selected);
    if (!preset) return;
    payloadEl.value = preset.payload;
    nameEl.value = preset.name;
  });

  payloadEl.addEventListener("input", () => {
    if (selectEl.value !== "__custom__") {
      selectEl.value = "__custom__";
    }
  });

  saveBtn.addEventListener("click", () => {
    const name = (nameEl.value || "").trim();
    if (!name) {
      return;
    }
    const payload = payloadEl.value || defaultDesignPayload();
    const idx = presets.findIndex(p => p.name === name);
    if (idx >= 0) presets[idx] = { name, payload };
    else presets.unshift({ name, payload });
    saveDesignPresets(currentWorkflow.name, presets);
    renderOptions(name);
  });

  deleteBtn.addEventListener("click", () => {
    const targetName = selectEl.value !== "__custom__"
      ? selectEl.value
      : (nameEl.value || "").trim();
    if (!targetName) return;
    const idx = presets.findIndex(p => p.name === targetName);
    if (idx < 0) return;
    presets.splice(idx, 1);
    saveDesignPresets(currentWorkflow.name, presets);
    renderOptions("");
  });
}

async function runWorkflowFromDesignOverlay(overlay) {
  const payloadEl = overlay.querySelector("#designPayload");
  const outputEl = overlay.querySelector("#designRunOutput");
  const runBtn = overlay.querySelector("#designRunBtn");

  let parsed;
  try {
    parsed = JSON.parse(payloadEl.value || "{}");
  } catch (e) {
    outputEl.textContent = `Invalid JSON payload: ${e.message || e}`;
    return;
  }

  if (!parsed || typeof parsed !== "object" || typeof parsed.xml !== "string" || !parsed.xml.trim()) {
    outputEl.textContent = "Payload must be a JSON object with non-empty string field 'xml'.";
    return;
  }

  runBtn.disabled = true;
  outputEl.textContent = "Running workflow...";
  try {
    const resp = await fetch(`/api/run/${encodeURIComponent(currentWorkflow.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ xml: parsed.xml }),
    });

    const text = await resp.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    if (!resp.ok) {
      outputEl.textContent = `Run failed (${resp.status}):\n${JSON.stringify(data, null, 2)}`;
      return;
    }

    outputEl.textContent = JSON.stringify(data, null, 2);
    await loadRunsForWorkflow(currentWorkflow.name);
  } catch (e) {
    outputEl.textContent = `Run failed: ${e.message || e}`;
  } finally {
    runBtn.disabled = !((currentWorkflow.invocation && currentWorkflow.invocation.allow_http) && canRunWorkflow(currentWorkflow.name));
  }
}

async function runWorkflowViaSchedulerFromDesignOverlay(overlay) {
  const payloadEl = overlay.querySelector("#designPayload");
  const outputEl = overlay.querySelector("#designRunOutput");
  const runScheduledBtn = overlay.querySelector("#designRunScheduledBtn");

  let parsed;
  try {
    parsed = JSON.parse(payloadEl.value || "{}");
  } catch (e) {
    outputEl.textContent = `Invalid JSON payload: ${e.message || e}`;
    return;
  }

  if (!parsed || typeof parsed !== "object" || typeof parsed.xml !== "string" || !parsed.xml.trim()) {
    outputEl.textContent = "Payload must be a JSON object with non-empty string field 'xml'.";
    return;
  }

  runScheduledBtn.disabled = true;
  outputEl.textContent = "Triggering scheduled run...";
  try {
    const resp = await fetch(`${SCHEDULER_API_URL}/workflows/${encodeURIComponent(currentWorkflow.name)}/run`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User": identitySession.username || "anonymous"
      },
      body: JSON.stringify({ payload: parsed.xml }),
    });

    const text = await resp.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    if (!resp.ok) {
      outputEl.textContent = `Scheduled run failed (${resp.status}):\n${JSON.stringify(data, null, 2)}`;
      return;
    }

    outputEl.textContent = JSON.stringify(data, null, 2);
    await loadRunsForWorkflow(currentWorkflow.name);
  } catch (e) {
    outputEl.textContent = `Scheduled run failed: ${e.message || e}`;
  } finally {
    runScheduledBtn.disabled = !canTriggerScheduledRun();
  }
}

// ---------------------------------------------------------------------------
// Pipeline rendering
// ---------------------------------------------------------------------------
function buildPipeline(steps, traceSteps, options = {}) {
  const stepTraces = buildStepTraceMap(traceSteps || []);
  const traceRoot = options.traceRoot || null;
  const scopePath = options.scopePath || "";
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
      frag.appendChild(buildLoopNode(step, stepTraces[step.id] || null, { traceRoot, scopePath }));
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
    const overlay = node.closest(".design-overlay");
    if (overlay) {
      selectDesignStepNode(step, trace, node, overlay);
      return;
    }
    selectStepNode(step, trace, node);
  });

  return node;
}

function buildLoopNode(step, trace, options = {}) {
  const traceRoot = options.traceRoot || null;
  const parentScopePath = options.scopePath || "";
  const loopScopePath = parentScopePath ? `${parentScopePath}.${step.id}` : step.id;
  const wrap = document.createElement("div");
  wrap.className = "pipe-loop";

  const label = step.type === "for_each"
    ? `for_each \u2014 ${step.id} (as ${step.as_key || "item"})`
    : `repeat_until \u2014 ${step.id}`;
  const children = Array.isArray(trace?.children) ? trace.children : [];
  const totalIterations = children.length || Number(trace?.iterations || 0);
  const stateKey = loopScopePath;
  const selected = Math.max(0, Math.min(loopViewState[stateKey] || 0, Math.max(totalIterations - 1, 0)));
  loopViewState[stateKey] = selected;

  const iters = trace ? ` \u00b7 ${trace.iterations || "?"} iterations` : "";
  const navHtml = totalIterations > 0 ? `
    <div class="loop-iteration-nav" data-loop-key="${esc(stateKey)}">
      <button class="loop-iteration-btn" data-dir="prev" ${selected <= 0 ? "disabled" : ""}>&lsaquo;</button>
      <span class="loop-iteration-pill">${selected + 1} / ${totalIterations}</span>
      <button class="loop-iteration-btn" data-dir="next" ${selected >= totalIterations - 1 ? "disabled" : ""}>&rsaquo;</button>
    </div>` : "";

  wrap.innerHTML = `<div class="loop-header">
    <svg viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" d="M5.22 14.78a.75.75 0 001.06-1.06L4.56 12h8.69a.75.75 0 000-1.5H4.56l1.72-1.72a.75.75 0 00-1.06-1.06l-3 3a.75.75 0 000 1.06l3 3zm5.56-6.5a.75.75 0 11-1.06-1.06L11.44 5.5H2.75a.75.75 0 010-1.5h8.69L9.72 2.28a.75.75 0 011.06-1.06l3 3a.75.75 0 010 1.06l-3 3z"/></svg>
    <span>${esc(label)}${iters}</span>
    ${navHtml}
  </div>`;

  const body = document.createElement("div");
  body.className = "loop-body";
  const selectedIteration = children[selected] || null;
  if (step.steps && step.steps.length) {
    if (selectedIteration && Array.isArray(selectedIteration.steps)) {
      body.appendChild(buildLoopIterationView(step, selectedIteration, {
        traceRoot,
        scopePath: `${loopScopePath}.iter_${selected}`,
      }));
    } else {
      body.appendChild(buildPipeline(step.steps, null, {
        traceRoot,
        scopePath: loopScopePath,
      }));
    }
  }
  wrap.appendChild(body);

  wrap.querySelectorAll(".loop-iteration-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const dir = btn.dataset.dir;
      const current = loopViewState[stateKey] || 0;
      const next = dir === "next" ? Math.min(current + 1, totalIterations - 1) : Math.max(current - 1, 0);
      if (next === current) return;
      loopViewState[stateKey] = next;
      if (currentTrace) {
        renderRunDetail(currentTrace);
      }
    });
  });

  wrap.querySelector(".loop-header").addEventListener("click", (e) => {
    e.stopPropagation();
    const overlay = wrap.closest(".design-overlay");
    if (overlay) {
      selectDesignStepNode(step, trace, wrap, overlay);
      return;
    }
    selectStepNode(step, trace, wrap);
  });

  return wrap;
}

function buildLoopIterationView(loopStep, iterationTrace, options = {}) {
  const traceRoot = options.traceRoot || null;
  const scopePath = options.scopePath || "";
  const container = document.createElement("div");
  container.className = "loop-iteration-view";

  const info = document.createElement("div");
  info.className = "loop-iteration-info";
  info.textContent = `Iteration ${Number(iterationTrace.iteration || 0) + 1}`;
  container.appendChild(info);

  const traceMap = buildStepTraceMap(iterationTrace.steps || []);
  for (let i = 0; i < (loopStep.steps || []).length; i++) {
    const stepDef = loopStep.steps[i];
    if (i > 0) {
      const conn = document.createElement("div");
      conn.className = "pipe-connector";
      container.appendChild(conn);
    }
    // Detect new IF structure
    if (stepDef.type === "if" && (Array.isArray(stepDef.then) || Array.isArray(stepDef.else))) {
      const group = {
        contextKey: (stepDef.condition && stepDef.condition.context_key) || "condition",
        trueSteps: stepDef.then || [],
        falseSteps: stepDef.else || [],
      };
      const block = buildIfSplitNode(group, traceMap, { traceRoot, scopePath });
      container.appendChild(block);
      continue;
    }
    // Fallback: legacy when-based grouping (for backward compatibility)
    if (stepDef.when) {
      // Render as a single step node for now (could be extended for legacy split)
      container.appendChild(buildStepNode(stepDef, traceMap));
      continue;
    }
    // Normal step
    container.appendChild(buildStepNode(stepDef, traceMap));
  }

  return container;
}

function buildIfSplitNode(group, traceMap, options = {}) {
  const traceRoot = options.traceRoot || null;
  const block = document.createElement("div");
  block.className = "if-split";

  block.innerHTML = `
    <div class="if-split-header">if (${esc(group.contextKey)})</div>
    <div class="if-split-lanes">
      <div class="if-lane if-lane-true">
        <div class="if-lane-title">true</div>
      </div>
      <div class="if-lane if-lane-false">
        <div class="if-lane-title">false</div>
      </div>
    </div>`;

  const trueLane = block.querySelector(".if-lane-true");
  const falseLane = block.querySelector(".if-lane-false");
  appendIfLaneNodes(trueLane, group.trueSteps, traceMap, traceRoot);
  appendIfLaneNodes(falseLane, group.falseSteps, traceMap, traceRoot);
  return block;
}

function appendIfLaneNodes(laneEl, steps, traceMap, traceRoot) {
  if (!steps || !steps.length) {
    const empty = document.createElement("div");
    empty.className = "if-lane-empty";
    empty.textContent = "(no steps)";
    laneEl.appendChild(empty);
    return;
  }

  steps.forEach((stepDef, idx) => {
    if (idx > 0) {
      const conn = document.createElement("div");
      conn.className = "pipe-connector if-lane-connector";
      laneEl.appendChild(conn);
    }
    laneEl.appendChild(buildStepNode(stepDef, traceMap));
  });
}

// ---------------------------------------------------------------------------
// Step detail panel
// ---------------------------------------------------------------------------
function selectDesignStepNode(step, trace, nodeEl, overlay) {
  overlay.querySelectorAll(".pipe-node.selected").forEach(n => n.classList.remove("selected"));
  overlay.querySelectorAll(".loop-header.selected").forEach(n => n.classList.remove("selected"));

  if (nodeEl.classList.contains("pipe-node")) {
    nodeEl.classList.add("selected");
  } else {
    const hdr = nodeEl.querySelector(".loop-header");
    if (hdr) hdr.classList.add("selected");
  }

  const titleEl = overlay.querySelector("#designStepDetailTitle");
  const detailEl = overlay.querySelector("#designStepDetail");
  const codeEl = overlay.querySelector("#designStepCode");
  if (!titleEl || !detailEl || !codeEl) {
    return;
  }

  titleEl.textContent = `${step.id} (${step.type})`;
  const detail = {
    id: step.id,
    type: step.type,
    when: step.when || null,
    configuration: step,
  };
  if (trace) {
    detail.trace = trace;
  }
  detailEl.textContent = JSON.stringify(detail, null, 2);
  codeEl.textContent = buildDesignStepCodeFragment(step);
}

function buildDesignStepCodeFragment(step) {
  const stepYaml = objectToYaml(step, 2);
  const connections = collectConnectionRefs(step);

  let result = "steps:\n";
  result += "  - " + stepYaml.trimStart();

  if (connections.length) {
    result += "\n\n# Connections referenced by this step\n";
    result += "connections:\n";
    for (const name of connections) {
      result += `  ${name}:\n`;
      result += "    # Add this connection in connections/*.yaml\n";
      result += "    type: http\n";
      result += "    base_url: https://example.invalid\n";
    }
  }

  return result;
}

function collectConnectionRefs(value, refs = new Set()) {
  if (Array.isArray(value)) {
    value.forEach((v) => collectConnectionRefs(v, refs));
    return refs;
  }
  if (!value || typeof value !== "object") {
    return refs;
  }

  Object.entries(value).forEach(([k, v]) => {
    if ((k === "connection" || k.endsWith("_connection")) && typeof v === "string" && v.trim()) {
      refs.add(v.trim());
    }
    collectConnectionRefs(v, refs);
  });
  return refs;
}

function objectToYaml(value, indent = 0) {
  const pad = " ".repeat(indent);
  if (value === null || value === undefined) return "null\n";

  if (typeof value === "string") {
    if (value.includes("\n")) {
      const lines = value.split("\n").map((line) => `${pad}  ${line}`);
      return `|\n${lines.join("\n")}\n`;
    }
    return `${JSON.stringify(value)}\n`;
  }

  if (typeof value === "number" || typeof value === "boolean") {
    return `${String(value)}\n`;
  }

  if (Array.isArray(value)) {
    if (!value.length) return "[]\n";
    return value.map((item) => {
      if (item && typeof item === "object") {
        const nested = objectToYaml(item, indent + 2).trimEnd();
        const lines = nested.split("\n");
        const first = lines.shift() || "";
        const rest = lines.map((l) => `${pad}  ${l}`).join("\n");
        return `${pad}- ${first}${rest ? `\n${rest}` : ""}`;
      }
      return `${pad}- ${objectToYaml(item, 0).trim()}`;
    }).join("\n") + "\n";
  }

  const entries = Object.entries(value);
  if (!entries.length) return "{}\n";
  return entries.map(([k, v]) => {
    if (v && typeof v === "object") {
      return `${pad}${k}:\n${objectToYaml(v, indent + 2)}`.trimEnd();
    }
    return `${pad}${k}: ${objectToYaml(v, 0).trimEnd()}`;
  }).join("\n") + "\n";
}

function selectStepNode(step, trace, nodeEl) {
  document.querySelectorAll(".pipe-node.selected").forEach(n => n.classList.remove("selected"));
  document.querySelectorAll(".loop-header.selected").forEach(n => n.classList.remove("selected"));
  if (nodeEl.classList.contains("pipe-node")) nodeEl.classList.add("selected");

  show("detailPanel");
  document.getElementById("detailTitle").textContent = `${step.id} (${step.type})`;

  const metaObj = { ...step };
  if (trace) metaObj._trace = trace;
  document.getElementById("detailMeta").textContent = JSON.stringify(metaObj, null, 2);
  document.getElementById("detailCode").textContent = buildDesignStepCodeFragment(step);

  if (currentTrace && trace) {
    loadStepIO(currentTrace.request_id, getStepTracePath(trace, step.id), trace);
  } else {
    document.getElementById("detailInput").textContent = trace?.input_preview || "(no data)";
    document.getElementById("detailOutput").textContent = trace?.output_preview || "(no data)";
  }

  switchTab("data");
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
  document.querySelectorAll(".loop-header.selected").forEach(n => n.classList.remove("selected"));
}

function switchTab(tab) {
  document.querySelectorAll(".detail-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("tabData").classList.toggle("hidden", tab !== "data");
  document.getElementById("tabCode").classList.toggle("hidden", tab !== "code");
  document.getElementById("tabMeta").classList.toggle("hidden", tab !== "meta");
}

async function copyTextFromElement(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const text = el.textContent || "";
  try {
    await navigator.clipboard.writeText(text);
  } catch (_e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
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
    clearSideNavSelection();
    show("placeholder"); hide("workflowPanel"); hide("runPanel"); hide("rabbitPanel"); hide("storagePanel"); hide("accessPanel"); hide("scheduleTemplatesPanel");
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
  clearSideNavSelection();
  document.querySelectorAll(".wf-item").forEach(li => {
    li.classList.toggle("active", li.dataset.name === name);
  });
}
function clearSidebarHighlight() {
  document.querySelectorAll(".wf-item").forEach(li => li.classList.remove("active"));
}

function clearSideNavSelection() {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
}

function activateRabbitNav() {
  clearSidebarHighlight();
  clearSideNavSelection();
  const el = document.getElementById("navRabbitMQ");
  if (el) el.classList.add("active");
}

function activateStorageNav() {
  clearSidebarHighlight();
  clearSideNavSelection();
  const el = document.getElementById("navStorage");
  if (el) el.classList.add("active");
}

function activateAccessNav() {
  clearSidebarHighlight();
  clearSideNavSelection();
  const el = document.getElementById("navAccess");
  if (el) el.classList.add("active");
}

async function selectRabbitMQView() {
  currentWorkflowName = null;
  currentRunId = null;
  currentTrace = null;
  currentWorkflow = null;
  closeDetail();
  activateRabbitNav();
  setBreadcrumb([{ label: "RabbitMQ" }]);

  hide("placeholder");
  hide("workflowPanel");
  hide("runPanel");
  hide("storagePanel");
  hide("accessPanel");
  hide("scheduleTemplatesPanel");
  show("rabbitPanel");

  await loadRabbitMQView();
}

async function selectStorageView() {
  currentWorkflowName = null;
  currentRunId = null;
  currentTrace = null;
  currentWorkflow = null;
  currentStorageBucket = null;
  closeDetail();
  activateStorageNav();
  setBreadcrumb([{ label: "Storage" }]);

  hide("placeholder");
  hide("workflowPanel");
  hide("runPanel");
  hide("rabbitPanel");
  hide("accessPanel");
  hide("scheduleTemplatesPanel");
  show("storagePanel");

  await loadStorageView();
}

async function selectAccessView() {
  currentWorkflowName = null;
  currentRunId = null;
  currentTrace = null;
  currentWorkflow = null;
  closeDetail();
  activateAccessNav();
  setBreadcrumb([{ label: "Access" }]);

  hide("placeholder");
  hide("workflowPanel");
  hide("runPanel");
  hide("rabbitPanel");
  hide("storagePanel");
  hide("scheduleTemplatesPanel");
  show("accessPanel");

  await loadAccessView();
}

async function loadRabbitMQView() {
  const statusEl = document.getElementById("rabbitStatus");
  const cardsEl = document.getElementById("rabbitOverviewCards");
  const queuesBody = document.getElementById("rabbitQueuesBody");
  const exchangesBody = document.getElementById("rabbitExchangesBody");
  const outputEl = document.getElementById("rabbitPeekOutput");

  statusEl.className = "rabbit-status";
  statusEl.textContent = "Loading RabbitMQ status...";
  cardsEl.innerHTML = "";
  queuesBody.innerHTML = "<tr><td colspan=\"4\" style=\"color:var(--text-muted);text-align:center;padding:20px\">Loading...</td></tr>";
  exchangesBody.innerHTML = "<tr><td colspan=\"3\" style=\"color:var(--text-muted);text-align:center;padding:20px\">Loading...</td></tr>";

  try {
    rabbitStatus = await api("/api/rabbitmq/status");
    if (!rabbitStatus.enabled) {
      statusEl.classList.add("warn");
      statusEl.textContent = "RabbitMQ inspect API is disabled. Set DASH_RABBITMQ_INSPECT_ENABLED=true on dashboard.";
      queuesBody.innerHTML = "<tr><td colspan=\"4\" style=\"color:var(--text-muted);text-align:center;padding:20px\">Disabled</td></tr>";
      exchangesBody.innerHTML = "<tr><td colspan=\"3\" style=\"color:var(--text-muted);text-align:center;padding:20px\">Disabled</td></tr>";
      outputEl.textContent = "Enable RabbitMQ inspect API first.";
      return;
    }

    statusEl.classList.add("ok");
    statusEl.textContent = `Inspect enabled for vhost ${rabbitStatus.vhost || "/"}.`;

    const [overview, queues, exchanges] = await Promise.all([
      api("/api/rabbitmq/overview"),
      api("/api/rabbitmq/queues"),
      api("/api/rabbitmq/exchanges"),
    ]);

    renderRabbitOverview(overview, cardsEl);
    renderRabbitQueues(queues || [], queuesBody);
    renderRabbitExchanges(exchanges || [], exchangesBody);
  } catch (e) {
    console.error("loadRabbitMQView", e);
    statusEl.classList.remove("ok");
    statusEl.classList.add("error");
    statusEl.textContent = `Failed to load RabbitMQ data: ${e.message || e}`;
    queuesBody.innerHTML = "<tr><td colspan=\"4\" style=\"color:var(--red);text-align:center;padding:20px\">Failed</td></tr>";
    exchangesBody.innerHTML = "<tr><td colspan=\"3\" style=\"color:var(--red);text-align:center;padding:20px\">Failed</td></tr>";
  }
}

function renderRabbitOverview(overview, targetEl) {
  const objectTotals = (overview.object_totals || {});
  targetEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Version</div>
      <div class="stat-value" style="font-size:16px">${esc(overview.rabbitmq_version || "-")}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Queues</div>
      <div class="stat-value">${Number(objectTotals.queues || 0)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Exchanges</div>
      <div class="stat-value">${Number(objectTotals.exchanges || 0)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Connections</div>
      <div class="stat-value">${Number(objectTotals.connections || 0)}</div>
    </div>`;
}

function renderRabbitQueues(queues, tbody) {
  tbody.innerHTML = "";
  if (!queues.length) {
    tbody.innerHTML = "<tr><td colspan=\"4\" style=\"color:var(--text-muted);text-align:center;padding:20px\">No queues found</td></tr>";
    return;
  }

  const sorted = [...queues].sort((a, b) => (b.messages || 0) - (a.messages || 0));
  for (const q of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family:var(--mono);font-size:12px">${esc(q.name || "")}</td>
      <td>${Number(q.messages || 0)}</td>
      <td>${Number(q.consumers || 0)}</td>
      <td style="color:var(--accent);font-size:12px">Peek</td>`;
    tr.addEventListener("click", () => peekRabbitMessages(q.name, 10));
    tbody.appendChild(tr);
  }
}

function renderRabbitExchanges(exchanges, tbody) {
  tbody.innerHTML = "";
  const filtered = (exchanges || []).filter(ex => ex && ex.name);
  if (!filtered.length) {
    tbody.innerHTML = "<tr><td colspan=\"3\" style=\"color:var(--text-muted);text-align:center;padding:20px\">No exchanges found</td></tr>";
    return;
  }

  const sorted = [...filtered].sort((a, b) => String(a.name).localeCompare(String(b.name)));
  for (const ex of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="font-family:var(--mono);font-size:12px">${esc(ex.name || "")}</td>
      <td>${esc(ex.type || "")}</td>
      <td>${ex.durable ? "yes" : "no"}</td>`;
    tbody.appendChild(tr);
  }
}

async function peekRabbitMessagesFromInput() {
  const queue = (document.getElementById("rabbitPeekQueue").value || "").trim();
  const count = Number(document.getElementById("rabbitPeekCount").value || "10");
  await peekRabbitMessages(queue, count);
}

async function peekRabbitMessages(queue, count) {
  const outputEl = document.getElementById("rabbitPeekOutput");
  const queueInput = document.getElementById("rabbitPeekQueue");
  const countInput = document.getElementById("rabbitPeekCount");

  if (!queue) {
    outputEl.textContent = "Queue is required.";
    return;
  }

  const safeCount = Math.max(1, Math.min(100, Number.isFinite(count) ? count : 10));
  queueInput.value = queue;
  countInput.value = String(safeCount);
  outputEl.textContent = "Loading messages...";

  try {
    const params = new URLSearchParams({ queue, count: String(safeCount) });
    const data = await api(`/api/rabbitmq/messages/peek?${params.toString()}`);
    outputEl.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    console.error("peekRabbitMessages", e);
    outputEl.textContent = `Failed to peek messages: ${e.message || e}`;
  }
}

async function loadStorageView() {
  const statusEl = document.getElementById("storageStatus");
  const bucketsBody = document.getElementById("storageBucketsBody");
  const keysBody = document.getElementById("storageKeysBody");
  const objectOutput = document.getElementById("storageObjectOutput");

  currentStorageKeys = [];
  currentStoragePage = 1;
  currentStorageObject = null;
  currentStorageObjectBucket = null;
  currentStorageObjectKey = null;

  statusEl.className = "rabbit-status";
  statusEl.textContent = "Loading storage...";
  bucketsBody.innerHTML = "<tr><td style=\"color:var(--text-muted);text-align:center;padding:20px\">Loading...</td></tr>";
  keysBody.innerHTML = "<tr><td style=\"color:var(--text-muted);text-align:center;padding:20px\">Select a bucket</td></tr>";
  objectOutput.textContent = "Select a key to inspect its object.";

  try {
    const [status, buckets] = await Promise.all([
      api("/api/storage/status"),
      api("/api/storage/buckets"),
    ]);
    const bucketList = buckets.buckets || [];
    statusEl.classList.add("ok");
    statusEl.textContent = `Connected to storage (${bucketList.length} bucket(s), roles: ${status.roles || "-"}).`;
    renderStorageBuckets(bucketList, bucketsBody);

    if (bucketList.length > 0) {
      currentStorageBucket = bucketList[0];
      await loadStorageKeys(currentStorageBucket);
    }
  } catch (e) {
    console.error("loadStorageView", e);
    statusEl.classList.remove("ok");
    statusEl.classList.add("error");
    statusEl.textContent = `Failed to load storage: ${e.message || e}`;
    bucketsBody.innerHTML = "<tr><td style=\"color:var(--red);text-align:center;padding:20px\">Failed</td></tr>";
    keysBody.innerHTML = "<tr><td style=\"color:var(--red);text-align:center;padding:20px\">Failed</td></tr>";
  }
}

function renderStorageBuckets(buckets, tbody) {
  tbody.innerHTML = "";
  if (!buckets.length) {
    tbody.innerHTML = "<tr><td style=\"color:var(--text-muted);text-align:center;padding:20px\">No buckets found</td></tr>";
    return;
  }
  for (const bucket of buckets) {
    const tr = document.createElement("tr");
    if (bucket === currentStorageBucket) tr.classList.add("active-row");
    tr.innerHTML = `<td style="font-family:var(--mono);font-size:12px">${esc(bucket)}</td>`;
    tr.addEventListener("click", async () => {
      currentStorageBucket = bucket;
      renderStorageBuckets(buckets, tbody);
      await loadStorageKeys(bucket);
    });
    tbody.appendChild(tr);
  }
}

async function loadStorageKeysFromInput() {
  if (!currentStorageBucket) return;
  await loadStorageKeys(currentStorageBucket);
}

async function loadStorageKeys(bucket) {
  const keysBody = document.getElementById("storageKeysBody");
  const prefix = (document.getElementById("storagePrefix").value || "").trim();
  const objectOutput = document.getElementById("storageObjectOutput");
  keysBody.innerHTML = "<tr><td style=\"color:var(--text-muted);text-align:center;padding:20px\">Loading...</td></tr>";
  currentStorageObject = null;
  currentStorageObjectBucket = null;
  currentStorageObjectKey = null;
  objectOutput.textContent = "Select a key to inspect its object.";

  try {
    const params = new URLSearchParams({ bucket, limit: "1000" });
    if (prefix) params.set("prefix", prefix);
    const data = await api(`/api/storage/keys?${params.toString()}`);
    currentStorageKeys = data.keys || [];
    currentStoragePage = 1;
    renderStorageKeysPage();
  } catch (e) {
    console.error("loadStorageKeys", e);
    currentStorageKeys = [];
    currentStoragePage = 1;
    keysBody.innerHTML = "<tr><td style=\"color:var(--red);text-align:center;padding:20px\">Failed to load keys</td></tr>";
    updateStoragePager(0, 1);
  }
}

function getFilteredStorageKeys() {
  const term = (document.getElementById("storageKeyFilter").value || "").trim().toLowerCase();
  if (!term) return currentStorageKeys;
  return currentStorageKeys.filter(k => String(k).toLowerCase().includes(term));
}

function renderStorageKeysPage() {
  const keysBody = document.getElementById("storageKeysBody");
  const keys = getFilteredStorageKeys();
  const totalPages = Math.max(1, Math.ceil(keys.length / STORAGE_PAGE_SIZE));
  if (currentStoragePage > totalPages) currentStoragePage = totalPages;
  if (currentStoragePage < 1) currentStoragePage = 1;

  const start = (currentStoragePage - 1) * STORAGE_PAGE_SIZE;
  const end = start + STORAGE_PAGE_SIZE;
  const pageKeys = keys.slice(start, end);
  renderStorageKeys(currentStorageBucket, pageKeys, keysBody);
  updateStoragePager(keys.length, totalPages);
}

function updateStoragePager(totalItems, totalPages) {
  document.getElementById("storagePageInfo").textContent = `Page ${currentStoragePage} / ${totalPages} (${totalItems} keys)`;
  document.getElementById("btnStoragePrevPage").disabled = currentStoragePage <= 1;
  document.getElementById("btnStorageNextPage").disabled = currentStoragePage >= totalPages;
}

function changeStoragePage(delta) {
  currentStoragePage += delta;
  renderStorageKeysPage();
}

function renderStorageKeys(bucket, keys, tbody) {
  tbody.innerHTML = "";
  if (!keys.length) {
    tbody.innerHTML = "<tr><td style=\"color:var(--text-muted);text-align:center;padding:20px\">No keys found</td></tr>";
    return;
  }
  for (const key of keys) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td style="font-family:var(--mono);font-size:12px">${esc(key)}</td>`;
    tr.addEventListener("click", () => loadStorageObject(bucket, key));
    tbody.appendChild(tr);
  }
}

async function loadStorageObject(bucket, key) {
  const output = document.getElementById("storageObjectOutput");
  output.textContent = "Loading object...";
  try {
    const params = new URLSearchParams({ bucket, key });
    const data = await api(`/api/storage/object?${params.toString()}`);
    currentStorageObject = data;
    currentStorageObjectBucket = bucket;
    currentStorageObjectKey = key;
    output.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    console.error("loadStorageObject", e);
    currentStorageObject = null;
    currentStorageObjectBucket = null;
    currentStorageObjectKey = null;
    output.textContent = `Failed to load object: ${e.message || e}`;
  }
}

function hasScope(required) {
  const granted = new Set(identitySession.scopes || []);
  if (granted.has("minicloud:*") || granted.has(required)) return true;
  const parts = required.split(":");
  for (let i = parts.length; i > 1; i--) {
    const candidate = parts.slice(0, i - 1).join(":") + ":*";
    if (granted.has(candidate)) return true;
  }
  return false;
}

function canRunWorkflow(workflowName) {
  return hasScope(`minicloud:workflow:run:${workflowName}`);
}

function canTriggerScheduledRun() {
  const user = (identitySession.username || "").toLowerCase();
  return user === "admin" || user === "operator";
}

async function loadIdentitySession() {
  try {
    const me = await api("/api/auth/me");
    identitySession = {
      authenticated: true,
      username: me.username || null,
      groups: me.groups || [],
      scopes: me.scopes || [],
    };
  } catch (_e) {
    identitySession = { authenticated: false, username: null, groups: [], scopes: [] };
  }
}

async function loadAccessView() {
  const statusEl = document.getElementById("accessStatus");
  const loginCard = document.getElementById("accessLoginCard");
  const adminCard = document.getElementById("accessAdminCard");
  statusEl.className = "rabbit-status";
  statusEl.textContent = "Loading access state...";

  await loadIdentitySession();
  if (!identitySession.authenticated) {
    loginCard.classList.remove("hidden");
    adminCard.classList.add("hidden");
    statusEl.classList.add("warn");
    statusEl.textContent = "Not signed in. Sign in with a default user (admin/operator/viewer).";
    return;
  }

  loginCard.classList.add("hidden");
  statusEl.classList.add("ok");
  statusEl.textContent = `Signed in as ${identitySession.username}.`;

  const isAdmin = (identitySession.groups || []).includes("admins");
  if (!isAdmin) {
    adminCard.classList.add("hidden");
    statusEl.classList.remove("ok");
    statusEl.classList.add("warn");
    statusEl.textContent = `Signed in as ${identitySession.username}. Admin rights are required to manage users.`;
    return;
  }

  adminCard.classList.remove("hidden");
  try {
    const [users, perms] = await Promise.all([
      api("/api/iam/users"),
      api("/api/iam/permissions"),
    ]);
    iamUsers = users || [];
    iamPermissions = perms || [];
    renderAccessSelectors();
    await loadSelectedUserPermissions();
  } catch (e) {
    statusEl.classList.remove("ok");
    statusEl.classList.add("error");
    statusEl.textContent = `Failed to load IAM data: ${e.message || e}`;
  }
}

function renderAccessSelectors() {
  const userSelect = document.getElementById("accessUserSelect");
  const workflowSelect = document.getElementById("accessWorkflowSelect");
  userSelect.innerHTML = "";
  workflowSelect.innerHTML = "";

  for (const user of iamUsers) {
    const opt = document.createElement("option");
    opt.value = user.username;
    opt.textContent = `${user.username} (${(user.groups || []).join(",") || "no-groups"})`;
    userSelect.appendChild(opt);
  }
  selectedIamUser = userSelect.value || null;

  for (const wf of allWorkflows) {
    const opt = document.createElement("option");
    opt.value = wf.name;
    opt.textContent = wf.name;
    workflowSelect.appendChild(opt);
  }
}

async function onAccessUserChanged() {
  const userSelect = document.getElementById("accessUserSelect");
  selectedIamUser = userSelect.value || null;
  await loadSelectedUserPermissions();
}

async function loadSelectedUserPermissions() {
  const statusEl = document.getElementById("accessStatus");
  if (!selectedIamUser) {
    selectedIamUserPermissions = [];
    renderAccessPermissionToggles();
    return;
  }
  try {
    selectedIamUserPermissions = await api(`/api/iam/users/${encodeURIComponent(selectedIamUser)}/permissions`);
    renderAccessPermissionToggles();
  } catch (e) {
    statusEl.classList.remove("ok");
    statusEl.classList.add("error");
    statusEl.textContent = `Failed to load permissions: ${e.message || e}`;
  }
}

function renderAccessPermissionToggles() {
  const workflowName = (document.getElementById("accessWorkflowSelect").value || "").trim();
  const perms = new Set(selectedIamUserPermissions || []);
  document.getElementById("permRunAny").checked = perms.has("minicloud:workflow:run:*");
  document.getElementById("permRetriggerAny").checked = perms.has("minicloud:workflow:retrigger:*");
  document.getElementById("permRunSelected").checked = workflowName ? perms.has(`minicloud:workflow:run:${workflowName}`) : false;
  document.getElementById("permRetriggerSelected").checked = workflowName ? perms.has(`minicloud:workflow:retrigger:${workflowName}`) : false;
}

async function accessLogin() {
  const statusEl = document.getElementById("accessStatus");
  const username = (document.getElementById("accessLoginUsername").value || "").trim();
  const password = (document.getElementById("accessLoginPassword").value || "").trim();
  if (!username || !password) {
    statusEl.className = "rabbit-status warn";
    statusEl.textContent = "Username and password are required.";
    return;
  }
  try {
    await apiRequest("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    await loadAuthSession();
    await loadAccessView();
  } catch (e) {
    statusEl.className = "rabbit-status error";
    statusEl.textContent = `Login failed: ${e.message || e}`;
  }
}

async function saveAccessPermissions() {
  const statusEl = document.getElementById("accessStatus");
  const workflowName = (document.getElementById("accessWorkflowSelect").value || "").trim();
  if (!selectedIamUser) {
    statusEl.className = "rabbit-status warn";
    statusEl.textContent = "Select a user first.";
    return;
  }

  const next = new Set(selectedIamUserPermissions || []);
  const runAny = document.getElementById("permRunAny").checked;
  const retriggerAny = document.getElementById("permRetriggerAny").checked;
  const runSelected = document.getElementById("permRunSelected").checked;
  const retriggerSelected = document.getElementById("permRetriggerSelected").checked;

  next.delete("minicloud:workflow:run:*");
  next.delete("minicloud:workflow:retrigger:*");
  if (workflowName) {
    next.delete(`minicloud:workflow:run:${workflowName}`);
    next.delete(`minicloud:workflow:retrigger:${workflowName}`);
  }
  if (runAny) next.add("minicloud:workflow:run:*");
  if (retriggerAny) next.add("minicloud:workflow:retrigger:*");
  if (workflowName && runSelected) next.add(`minicloud:workflow:run:${workflowName}`);
  if (workflowName && retriggerSelected) next.add(`minicloud:workflow:retrigger:${workflowName}`);

  try {
    selectedIamUserPermissions = await apiRequest(`/api/iam/users/${encodeURIComponent(selectedIamUser)}/permissions`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ permissions: Array.from(next).sort() }),
    });
    renderAccessPermissionToggles();
    statusEl.className = "rabbit-status ok";
    statusEl.textContent = `Permissions updated for ${selectedIamUser}.`;
  } catch (e) {
    statusEl.className = "rabbit-status error";
    statusEl.textContent = `Failed to save permissions: ${e.message || e}`;
  }
}

async function loadAuthSession() {
  const signOutBtn = document.getElementById("btnSignOut");
  try {
    authSession = await api("/auth/session");
  } catch (_e) {
    authSession = { auth_enabled: false, username: null };
  }

  await loadIdentitySession();
  const label = identitySession.authenticated
    ? `Sign out (${identitySession.username})`
    : (authSession.username ? `Sign out (${authSession.username})` : "Sign out");

  if (authSession.auth_enabled || identitySession.authenticated) {
    signOutBtn.classList.remove("hidden");
    signOutBtn.textContent = label;
  } else {
    signOutBtn.classList.add("hidden");
  }
}

function signOutDashboard() {
  if (identitySession.authenticated) {
    fetch("/api/auth/logout", { method: "POST" })
      .finally(() => window.location.reload());
    return;
  }
  if (authSession.auth_enabled) {
    const nonce = Date.now();
    window.location.assign(`/auth/logout?nonce=${encodeURIComponent(String(nonce))}`);
  }
}

function downloadStorageObject() {
  if (!currentStorageObject || !currentStorageObjectKey) {
    return;
  }

  const hasValue = typeof currentStorageObject.value === "string";
  const content = hasValue
    ? currentStorageObject.value
    : JSON.stringify(currentStorageObject, null, 2);
  const mimeType = hasValue
    ? (currentStorageObject.content_type || "text/plain")
    : "application/json";

  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const suffix = hasValue ? ".txt" : ".json";
  const keyPart = String(currentStorageObjectKey).replaceAll("/", "_");
  const bucketPart = String(currentStorageObjectBucket || "bucket");
  a.href = url;
  a.download = `${bucketPart}_${keyPart}${suffix}`;
  a.click();
  URL.revokeObjectURL(url);
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
