(() => {
  "use strict";

  let currentRoot = "";
  const state = {
    matrix: [],
    columns: [],
    support: {},
    modelTiers: [],
    toolPolicies: [],
    managedEntries: [],
    selectedName: null,
    current: null,
    creating: false,
    busy: false
  };

  const $ = (id) => document.getElementById(id);
  const pageState = $("page-state");
  const matrixWrap = $("matrix-wrap");
  const matrixMessage = $("matrix-message");
  const managedList = $("managed-list");
  const editorEmpty = $("editor-empty");
  const editorMain = $("editor-main");
  const editorMessage = $("editor-message");

  function withRoot(url) {
    return currentRoot ? `${url}${url.includes("?") ? "&" : "?"}root=${encodeURIComponent(currentRoot)}` : url;
  }

  function setPageState(text, error = false) {
    pageState.textContent = text;
    pageState.classList.toggle("is-error", error);
  }

  function setMatrixMessage(text, kind = "") {
    matrixMessage.textContent = text || "";
    matrixMessage.className = `matrix-message message${kind ? ` is-${kind}` : ""}`;
  }

  function setEditorMessage(text, kind = "") {
    editorMessage.textContent = text;
    editorMessage.className = `message${kind ? ` is-${kind}` : ""}`;
  }

  async function parseResponse(response) {
    const raw = await response.text();
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (_) { return raw; }
  }

  // Used for /api/registry — the only agents.js endpoint that is scoped to
  // the selected workspace root.
  async function apiRoot(url, options = {}) {
    const response = await fetch(withRoot(url), options);
    const data = await parseResponse(response);
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.error : data;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }

  // Used for /api/registry/agents*, /api/registry/install — none of these
  // take a ?root= query param on the server, so never append one.
  async function api(url, options = {}) {
    const response = await fetch(url, options);
    const data = await parseResponse(response);
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data : null;
      const message = detail && detail.error ? detail.error : (typeof data === "string" ? data : `${response.status} ${response.statusText}`);
      const error = new Error(detail && detail.reason ? `${message} (${detail.reason})` : message);
      error.payload = detail;
      throw error;
    }
    return data;
  }

  function statusClass(status) {
    return `status-${String(status || "unknown").replace(/_/g, "-")}`;
  }

  function makeStatusBadge(status) {
    const badge = document.createElement("span");
    const label = status || "unknown";
    badge.className = `status-badge ${statusClass(label)}`;
    badge.textContent = label;
    return badge;
  }

  function computeColumns(matrix, support) {
    const seen = new Set();
    const columns = [];
    matrix.forEach((agent) => {
      (agent.harnesses || []).forEach((harness) => {
        if (!seen.has(harness)) { seen.add(harness); columns.push(harness); }
      });
    });
    const supported = columns.filter((h) => !support[h] || support[h].supported);
    const unsupported = columns.filter((h) => support[h] && !support[h].supported);
    return [...supported, ...unsupported];
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll(".matrix-action").forEach((button) => { button.disabled = busy; });
    $("new-agent").disabled = busy;
    if (state.current) {
      $("save-agent").disabled = busy;
      $("delete-agent").disabled = busy;
    }
  }

  // ---- Matrix -----------------------------------------------------------

  function renderMatrixEmpty() {
    matrixWrap.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "editor-empty";
    const strong = document.createElement("strong");
    strong.textContent = "No canonical agents yet";
    empty.append(strong, document.createTextNode(
      "Canonical agents live in registry/canonical-agents/ — add a Markdown file there, or use “+ New agent”, to see it in this matrix."));
    matrixWrap.append(empty);
  }

  function renderMatrix() {
    $("matrix-count").textContent = `${state.matrix.length} ${state.matrix.length === 1 ? "agent" : "agents"}`;
    if (!state.matrix.length) {
      renderMatrixEmpty();
      return;
    }
    const table = document.createElement("table");
    table.className = "matrix-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const agentTh = document.createElement("th");
    agentTh.textContent = "Agent";
    headRow.append(agentTh);
    state.columns.forEach((harness) => {
      const th = document.createElement("th");
      const supportInfo = state.support[harness];
      const unsupported = Boolean(supportInfo && !supportInfo.supported);
      th.textContent = harness;
      if (unsupported) {
        th.className = "col-unsupported";
        th.title = supportInfo.reason || "unsupported";
        const reason = document.createElement("span");
        reason.className = "th-reason";
        reason.textContent = supportInfo.reason || "unsupported";
        th.append(reason);
      }
      headRow.append(th);
    });
    thead.append(headRow);
    table.append(thead);

    const tbody = document.createElement("tbody");
    state.matrix.forEach((agent) => {
      const row = document.createElement("tr");
      row.dataset.name = agent.name;
      row.classList.toggle("is-active", agent.name === state.selectedName);

      const nameTd = document.createElement("td");
      const rowButton = document.createElement("button");
      rowButton.type = "button";
      rowButton.className = "matrix-row-btn";
      rowButton.dataset.name = agent.name;
      const name = document.createElement("span");
      name.className = "agent-name";
      name.textContent = agent.name;
      const tags = document.createElement("div");
      tags.className = "agent-tags";
      const tierTag = document.createElement("span"); tierTag.className = "agent-tag"; tierTag.textContent = agent.model_tier;
      const policyTag = document.createElement("span"); policyTag.className = "agent-tag"; policyTag.textContent = agent.tool_policy;
      tags.append(tierTag, policyTag);
      const desc = document.createElement("p");
      desc.className = "agent-desc";
      desc.textContent = agent.description || "";
      rowButton.append(name, tags, desc);
      nameTd.append(rowButton);
      row.append(nameTd);

      const cellsByHarness = new Map((agent.cells || []).map((cell) => [cell.harness, cell]));
      state.columns.forEach((harness) => {
        const td = document.createElement("td");
        td.className = "matrix-cell";
        const cell = cellsByHarness.get(harness);
        if (!cell) {
          td.textContent = "—";
        } else if (cell.status === "unsupported") {
          td.classList.add("is-unsupported");
          td.title = cell.reason || "";
          td.append(makeStatusBadge(cell.status));
        } else {
          td.append(makeStatusBadge(cell.status));
          if (cell.status === "missing" || cell.status === "stale") {
            const actions = document.createElement("div");
            actions.className = "matrix-cell-actions";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "action-button matrix-action";
            button.textContent = cell.status === "missing" ? "Install" : "Update";
            button.dataset.agent = agent.name;
            button.dataset.harness = harness;
            actions.append(button);
            td.append(actions);
          }
        }
        row.append(td);
      });

      tbody.append(row);
    });
    table.append(tbody);
    matrixWrap.replaceChildren(table);
  }

  function renderManaged() {
    managedList.replaceChildren();
    if (!state.managedEntries.length) {
      const empty = document.createElement("p");
      empty.className = "managed-note-text";
      empty.textContent = "No generator-managed agent files found for this workspace.";
      managedList.append(empty);
      return;
    }
    state.managedEntries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "managed-row";
      const left = document.createElement("span");
      const name = document.createElement("span"); name.className = "managed-name"; name.textContent = entry.name || entry.path;
      const hw = document.createElement("span"); hw.className = "managed-hw"; hw.textContent = ` ${entry.harness || "?"} · managed by generate.py`;
      left.append(name, hw);
      row.append(left, makeStatusBadge("managed"));
      managedList.append(row);
    });
  }

  // ---- Editor -------------------------------------------------------------

  function renderEditor() {
    if (!state.current) {
      editorEmpty.hidden = false; editorMain.hidden = true;
      return;
    }
    editorEmpty.hidden = true; editorMain.hidden = false;
    const cur = state.current;
    $("editor-title").textContent = cur.name || "Untitled agent";
    $("editor-path").textContent = cur.path || "—";
    $("field-description").value = cur.description || "";
    $("field-instructions").value = cur.instructions || "";

    const tierSelect = $("field-model-tier");
    tierSelect.replaceChildren();
    state.modelTiers.forEach((tier) => {
      const option = document.createElement("option");
      option.value = tier; option.textContent = tier;
      option.selected = tier === cur.model_tier;
      tierSelect.append(option);
    });

    const policySelect = $("field-tool-policy");
    policySelect.replaceChildren();
    state.toolPolicies.forEach((policy) => {
      const option = document.createElement("option");
      option.value = policy; option.textContent = policy;
      option.selected = policy === cur.tool_policy;
      policySelect.append(option);
    });

    setEditorMessage("No changes");
  }

  async function selectAgent(name) {
    state.selectedName = name;
    state.creating = false;
    renderMatrix();
    editorEmpty.hidden = false; editorMain.hidden = true;
    setPageState("reading agent…");
    try {
      const detail = await api(`/api/registry/agents/file?name=${encodeURIComponent(name)}`);
      state.current = detail;
      renderEditor();
      setPageState("registry ready");
    } catch (error) {
      state.current = null;
      setPageState(error.message, true);
      editorEmpty.hidden = false; editorMain.hidden = true;
      editorEmpty.replaceChildren();
      const strong = document.createElement("strong"); strong.textContent = "Unable to read agent";
      editorEmpty.append(strong, document.createTextNode(error.message));
    }
  }

  // ---- Mutations ------------------------------------------------------------

  async function installAgent(agentName, harness) {
    if (state.busy) return;
    setBusy(true);
    setMatrixMessage(`Installing ${agentName} for ${harness}…`);
    try {
      await api("/api/registry/install", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: agentName, harness })
      });
      await refresh();
      setMatrixMessage(`${agentName} installed for ${harness}`, "ok");
    } catch (error) {
      // error.message already folds in payload.reason, if the server sent one
      // (see api()'s error construction above).
      setMatrixMessage(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveAgent() {
    if (!state.current || state.busy) return;
    const cur = state.current;
    const payload = {
      name: cur.name,
      description: $("field-description").value,
      model_tier: $("field-model-tier").value,
      tool_policy: $("field-tool-policy").value,
      instructions: $("field-instructions").value
    };
    setBusy(true); setEditorMessage("Saving…");
    try {
      await api("/api/registry/agents/file", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      Object.assign(cur, payload);
      await refresh();
      setEditorMessage("Saved", "ok");
    } catch (error) {
      setEditorMessage(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function deleteAgent() {
    if (!state.current || state.busy) return;
    const name = state.current.name;
    if (!window.confirm(`Delete ${name}? This cannot be undone.`)) return;
    setBusy(true); setEditorMessage("Deleting…");
    try {
      await api(`/api/registry/agents/file?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      state.selectedName = null; state.current = null;
      await refresh();
      setEditorMessage("Deleted", "ok");
    } catch (error) {
      setEditorMessage(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function createAgent() {
    const form = $("new-form");
    if (state.busy || !form.checkValidity()) { form.reportValidity(); return; }
    const name = $("new-name").value.trim();
    setBusy(true);
    try {
      const defaultTier = state.modelTiers[0] || "cheap";
      const defaultPolicy = state.toolPolicies[0] || "read-only";
      await api("/api/registry/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          description: "TODO: describe this agent.",
          model_tier: defaultTier,
          tool_policy: defaultPolicy,
          instructions: ""
        })
      });
      $("new-dialog").close();
      $("new-name").value = "";
      await refresh();
      await selectAgent(name);
    } catch (error) {
      setMatrixMessage(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  // ---- Refresh --------------------------------------------------------------

  async function loadAgentsMeta() {
    const meta = await api("/api/registry/agents");
    state.support = meta.support || {};
    state.modelTiers = meta.model_tiers || [];
    state.toolPolicies = meta.tool_policies || [];
  }

  async function refresh() {
    setPageState("scanning registry…");
    try {
      await loadAgentsMeta();
      const registry = await apiRoot("/api/registry");
      state.matrix = registry.agent_matrix || [];
      state.columns = computeColumns(state.matrix, state.support);

      const statusByPath = new Map();
      (registry.groups || []).forEach((group) => (group.installations || []).forEach((installation) => {
        if (installation.path) statusByPath.set(installation.path, installation.status);
      }));
      state.managedEntries = (registry.entries || [])
        .filter((entry) => entry && entry.surface === "agent" && entry.managed)
        .map((entry) => ({ ...entry, status: statusByPath.get(entry.path) || "managed" }));

      renderMatrix();
      renderManaged();
      setPageState(`${state.matrix.length} canonical agents · registry ready`);

      if (state.selectedName && state.matrix.some((agent) => agent.name === state.selectedName)) {
        await selectAgent(state.selectedName);
      } else {
        state.selectedName = null; state.current = null; renderEditor();
      }
    } catch (error) {
      state.matrix = []; state.columns = []; state.managedEntries = [];
      state.current = null;
      renderMatrix(); renderManaged(); renderEditor();
      setPageState(error.message, true);
    }
  }

  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path ? event.detail.path : "";
    refresh();
  });

  matrixWrap.addEventListener("click", (event) => {
    const actionButton = event.target.closest("button[data-agent][data-harness]");
    if (actionButton) {
      installAgent(actionButton.dataset.agent, actionButton.dataset.harness);
      return;
    }
    const rowButton = event.target.closest("button[data-name]");
    if (rowButton) selectAgent(rowButton.dataset.name);
  });

  $("save-agent").addEventListener("click", saveAgent);
  $("delete-agent").addEventListener("click", deleteAgent);
  $("field-description").addEventListener("input", () => { if (state.current) setEditorMessage("Unsaved changes", "error"); });
  $("field-instructions").addEventListener("input", () => { if (state.current) setEditorMessage("Unsaved changes", "error"); });
  $("field-model-tier").addEventListener("change", () => { if (state.current) setEditorMessage("Unsaved changes", "error"); });
  $("field-tool-policy").addEventListener("change", () => { if (state.current) setEditorMessage("Unsaved changes", "error"); });

  $("new-agent").addEventListener("click", () => { $("new-dialog").showModal(); $("new-name").focus(); });
  $("new-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.submitter && event.submitter.id === "new-cancel") $("new-dialog").close();
    else createAgent();
  });
  $("new-dialog").addEventListener("click", (event) => { if (event.target === $("new-dialog")) $("new-dialog").close(); });

  refresh();
})();
