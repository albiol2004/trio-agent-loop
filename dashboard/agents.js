(() => {
  "use strict";

  const CUSTOM_VALUE = "__trio_custom__";
  let currentRoot = "";
  const state = {
    matrix: [],
    columns: [],
    support: {},
    harnesses: [],
    modelTiers: [],
    toolPolicies: [],
    modelChoices: {},
    schema: { keys: {} },
    managedEntries: [],
    selectedName: null,
    current: null,
    targetSelections: { new: [], editor: [] },
    scopes: { new: null, editor: null },
    catalogValues: { new: {}, editor: {} },
    catalogControls: { new: new Map(), editor: new Map() },
    harnessDefaults: { new: {}, editor: {} },
    newDefaultsToken: 0,
    destinationTokens: { new: 0, editor: 0 },
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

  function cloneValue(value) {
    if (Array.isArray(value)) return value.map((item) => cloneValue(item));
    if (value && typeof value === "object") {
      const copy = {};
      Object.entries(value).forEach(([key, child]) => {
        copy[key] = cloneValue(child);
      });
      return copy;
    }
    return value;
  }

  function valuesEqual(left, right) {
    if (left === right) return true;
    if (left === undefined || right === undefined) return left === right;
    try {
      return JSON.stringify(left) === JSON.stringify(right);
    } catch (_) {
      return false;
    }
  }

  function mergeHarnessMaps(base, overlay) {
    const result = cloneValue(base || {});
    Object.entries(overlay || {}).forEach(([harness, fields]) => {
      result[harness] = Object.assign(
        result[harness] && typeof result[harness] === "object"
          ? result[harness] : {},
        cloneValue(fields || {})
      );
    });
    return result;
  }

  function newAgentSettings() {
    const tierControl = $("new-model-tier");
    const policyControl = $("new-tool-policy");
    return {
      modelTier: tierControl && tierControl.value
        ? tierControl.value : (state.modelTiers[0] || "cheap"),
      toolPolicy: policyControl && policyControl.value
        ? policyControl.value : (state.toolPolicies[0] || "read-only")
    };
  }

  function supportedHarnesses() {
    return state.harnesses.filter((harness) =>
      !state.support[harness] || state.support[harness].supported
    );
  }

  function targetableHarnesses(prefix) {
    return supportedHarnesses();
  }

  function selectedTargets(prefix) {
    const container = $(`${prefix}-harnesses`);
    if (!container) return [];
    return [...container.querySelectorAll("input[data-harness]:checked")]
      .map((input) => input.dataset.harness);
  }

  function catalogFieldValue(control) {
    if (control.widget === "checkbox") return control.control.checked;
    if (control.customControl &&
        control.control.value === CUSTOM_VALUE) {
      return control.customControl.value;
    }
    if (control.widget === "map") {
      const text = control.control.value.trim();
      if (!text) return undefined;
      try {
        return JSON.parse(text);
      } catch (_) {
        return text;
      }
    }
    const value = control.control.value;
    return value === "" ? undefined : value;
  }

  function collectCatalogValues(prefix) {
    // Keep effective values in state so a rerender does not blank defaults.
    // Return a separate map containing only values that differ from defaults.
    const values = cloneValue(state.catalogValues[prefix] || {});
    const defaults = state.harnessDefaults && state.harnessDefaults[prefix]
      ? state.harnessDefaults[prefix] : {};
    const result = {};
    Object.entries(values).forEach(([harness, fields]) => {
      if (!fields || typeof fields !== "object" || Array.isArray(fields)) return;
      const defaultFields = defaults[harness] || {};
      Object.entries(fields).forEach(([key, value]) => {
        if (valuesEqual(value, defaultFields[key])) return;
        result[harness] = result[harness] || {};
        result[harness][key] = value;
      });
    });
    state.catalogControls[prefix].forEach((control) => {
      const value = catalogFieldValue(control);
      const harnessValues = result[control.harness] &&
        typeof result[control.harness] === "object" &&
        !Array.isArray(result[control.harness])
        ? result[control.harness] : {};
      const defaultValue = defaults[control.harness]
        ? defaults[control.harness][control.key] : undefined;
      const empty = value === undefined ||
        valuesEqual(value, defaultValue) ||
        (control.widget === "checkbox" &&
         value === false && !control.originalPresent &&
         defaultValue === undefined);
      if (empty) {
        delete harnessValues[control.key];
      } else {
        harnessValues[control.key] = value;
      }
      if (Object.keys(harnessValues).length) {
        result[control.harness] = harnessValues;
      } else {
        delete result[control.harness];
      }
    });
    state.catalogValues[prefix] = mergeHarnessMaps(defaults, result);
    return result;
  }

  function renderHarnessTargets(prefix, selected) {
    const container = $(`${prefix}-harnesses`);
    if (!container) return;
    const allowed = new Set(targetableHarnesses(prefix));
    const active = (selected || []).filter((harness) => allowed.has(harness));
    state.targetSelections[prefix] = active;
    container.replaceChildren();
    targetableHarnesses(prefix).forEach((harness) => {
      const label = document.createElement("label");
      label.className = "harness-check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = harness;
      input.dataset.harness = harness;
      input.checked = active.includes(harness);
      input.addEventListener("change", () => {
        collectCatalogValues(prefix);
        state.targetSelections[prefix] = selectedTargets(prefix);
        renderCatalog(prefix);
        refreshDestinations(prefix);
        if (prefix === "editor" && state.current) {
          setEditorMessage("Unsaved changes", "error");
        }
      });
      label.append(input, document.createTextNode(harness));
      container.append(label);
    });
  }

  function makeCatalogField(prefix, harness, spec, values, panel) {
    const row = document.createElement("label");
    row.className = "catalog-field";
    const title = document.createElement("span");
    title.className = "field-label";
    title.textContent = `${spec.key}${spec.required ? " *" : ""}`;
    row.append(title);

    const hasValue = Object.prototype.hasOwnProperty.call(values, spec.key);
    const initial = hasValue ? values[spec.key] : "";
    let control;
    let customControl = null;
    let widget = spec.widget || "text";
    if (widget === "checkbox") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(initial);
      row.append(control);
    } else if (widget === "select" || Array.isArray(spec.enum) ||
               typeof spec.values_from === "string") {
      control = document.createElement("select");
      control.className = "field-control";
      const source = typeof spec.values_from === "string" &&
        spec.values_from.startsWith("models:")
        ? spec.values_from.slice("models:".length) : "";
      const choices = Array.isArray(spec.enum)
        ? spec.enum
        : (state.modelChoices[source] || []);
      if (!spec.required) {
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "—";
        control.append(empty);
      }
      choices.forEach((choice) => {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        control.append(option);
      });
      const current = initial === null || initial === undefined
        ? "" : String(initial);
      if (current && !choices.includes(current)) {
        const option = document.createElement("option");
        option.value = current;
        option.textContent = current;
        control.append(option);
      }
      const custom = document.createElement("option");
      custom.value = CUSTOM_VALUE;
      custom.textContent = "custom…";
      control.append(custom);
      customControl = document.createElement("input");
      customControl.type = "text";
      customControl.className = "field-control catalog-custom";
      customControl.value = current;
      const known = choices.includes(current) || current === "";
      control.value = known ? current : CUSTOM_VALUE;
      customControl.hidden = control.value !== CUSTOM_VALUE;
      customControl.disabled = customControl.hidden;
      control.addEventListener("change", () => {
        const customValue = control.value === CUSTOM_VALUE;
        customControl.hidden = !customValue;
        customControl.disabled = !customValue;
      });
      row.append(control, customControl);
    } else if (widget === "permission-grid" || spec.type === "map") {
      control = document.createElement("textarea");
      control.className = "field-control field-textarea";
      control.rows = 5;
      control.spellcheck = false;
      control.value = initial && typeof initial === "object"
        ? JSON.stringify(initial, null, 2)
        : (initial || "");
      row.append(control);
      widget = "map";
    } else if (widget === "textarea") {
      control = document.createElement("textarea");
      control.className = "field-control field-textarea";
      control.value = initial == null ? "" : String(initial);
      row.append(control);
    } else {
      control = document.createElement("input");
      control.type = "text";
      control.className = "field-control";
      control.value = initial == null ? "" : String(initial);
      row.append(control);
    }
    if (spec.help) {
      const help = document.createElement("span");
      help.className = "managed-note-text";
      help.textContent = spec.help;
      row.append(help);
    }
    panel.append(row);
    return {
      harness,
      key: spec.key,
      widget,
      control,
      customControl,
      originalPresent: hasValue
    };
  }

  function renderCatalog(prefix) {
    const container = $(`${prefix}-catalog`);
    if (!container) return;
    collectCatalogValues(prefix);
    const selected = state.targetSelections[prefix] || [];
    const values = state.catalogValues[prefix] || {};
    const controls = new Map();
    state.catalogControls[prefix] = controls;
    container.replaceChildren();
    if (!selected.length) {
      const note = document.createElement("span");
      note.className = "managed-note-text";
      note.textContent = "Select at least one harness.";
      container.append(note);
      return;
    }
    const tabList = document.createElement("div");
    tabList.className = "catalog-tab-list";
    const panels = new Map();
    selected.forEach((harness, index) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "catalog-tab";
      tab.textContent = harness;
      tab.setAttribute("aria-selected", String(index === 0));
      const panel = document.createElement("div");
      panel.className = "catalog-panel";
      panel.dataset.harness = harness;
      panel.hidden = index !== 0;
      const specs = state.schema.keys[`${harness}:agent`] || [];
      const harnessValues = values[harness] || {};
      specs.forEach((spec) => {
        const control = makeCatalogField(
          prefix, harness, spec, harnessValues, panel);
        controls.set(`${harness}:${spec.key}`, control);
      });
      if (!specs.length) {
        const note = document.createElement("span");
        note.className = "managed-note-text";
        note.textContent = "No catalog fields available.";
        panel.append(note);
      }
      tab.addEventListener("click", () => {
        panels.forEach((candidate, candidateHarness) => {
          const active = candidateHarness === harness;
          candidate.hidden = !active;
          const candidateTab = [...tabList.children].find(
            (item) => item.textContent === candidateHarness);
          if (candidateTab) {
            candidateTab.classList.toggle("is-active", active);
            candidateTab.setAttribute("aria-selected", String(active));
          }
        });
      });
      tab.classList.toggle("is-active", index === 0);
      tabList.append(tab);
      panels.set(harness, panel);
    });
    container.append(tabList);
    panels.forEach((panel) => container.append(panel));
  }

  async function refreshDestinations(prefix) {
    const list = $(`${prefix}-destinations`);
    if (!list) return;
    const name = prefix === "new"
      ? $("new-name").value.trim()
      : (state.current && state.current.name);
    const harnesses = state.targetSelections[prefix] || [];
    list.replaceChildren();
    if (!name || !harnesses.length) {
      const note = document.createElement("li");
      note.textContent = name ? "Select target harnesses." : "Enter a name.";
      list.append(note);
      return;
    }
    const scope = $(`${prefix}-scope`).value;
    const params = new URLSearchParams({
      name,
      scope,
      harnesses: harnesses.join(",")
    });
    if (scope === "project" && currentRoot) params.set("project", currentRoot);
    const token = ++state.destinationTokens[prefix];
    try {
      const result = await api(
        `/api/registry/agents/destinations?${params.toString()}`);
      if (token !== state.destinationTokens[prefix]) return;
      (result.destinations || []).forEach((destination) => {
        const item = document.createElement("li");
        const globalOnly = scope === "project" &&
          destination.scope_used === "global";
        item.textContent = globalOnly
          ? `${destination.harness} · global only → ${destination.path}`
          : `${destination.harness} · ${destination.path} (${destination.format})`;
        list.append(item);
      });
    } catch (error) {
      if (token !== state.destinationTokens[prefix]) return;
      const item = document.createElement("li");
      item.textContent = error.message;
      list.append(item);
    }
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

  function appendBrokerAgentId(cell, target) {
    if (!cell || cell.harness !== "omnigent" ||
        typeof cell.agent_id !== "string" || !cell.agent_id) return;
    const label = document.createElement("span");
    label.className = "matrix-agent-id";
    label.title = cell.agent_id;
    label.textContent = `id ${cell.agent_id.slice(0, 12)}`;
    target.append(label);
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
          appendBrokerAgentId(cell, td);
        } else {
          td.append(makeStatusBadge(cell.status));
          appendBrokerAgentId(cell, td);
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

    state.catalogControls.editor = new Map();
    state.harnessDefaults = state.harnessDefaults || {};
    state.harnessDefaults.editor = cloneValue(cur.harness_defaults || {});
    state.catalogValues.editor = mergeHarnessMaps(
      state.harnessDefaults.editor, cur.harness_overrides || {});
    // Do not default-check every harness: Save would install globally.
    const targets = state.targetSelections.editor.slice();
    if (!state.scopes.editor) {
      state.scopes.editor = currentRoot ? "project" : "global";
    }
    $("editor-scope").value = state.scopes.editor;
    renderHarnessTargets("editor", targets);
    renderCatalog("editor");
    refreshDestinations("editor");
    setEditorMessage("No changes");
  }

  async function selectAgent(name) {
    const changingAgent = state.selectedName !== name;
    state.selectedName = name;
    state.creating = false;
    if (changingAgent) {
      state.targetSelections.editor = [];
      state.catalogControls.editor = new Map();
    }
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
    const scope = $("editor-scope").value;
    const payload = {
      name: cur.name,
      description: $("field-description").value,
      model_tier: $("field-model-tier").value,
      tool_policy: $("field-tool-policy").value,
      instructions: $("field-instructions").value,
      spawns: cur.spawns || [],
      harness_overrides: collectCatalogValues("editor"),
      harnesses: selectedTargets("editor"),
      scope
    };
    if (scope === "project" && currentRoot) payload.project = currentRoot;
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
    const harnesses = selectedTargets("new");
    if (!harnesses.length) {
      setMatrixMessage("Select at least one target harness.", "error");
      return;
    }
    const scope = $("new-scope").value;
    const settings = newAgentSettings();
    const payload = {
      name,
      description: "TODO: describe this agent.",
      model_tier: settings.modelTier,
      tool_policy: settings.toolPolicy,
      instructions: "",
      harnesses,
      scope,
      harness_overrides: collectCatalogValues("new")
    };
    if (scope === "project" && currentRoot) payload.project = currentRoot;
    setBusy(true);
    try {
      await api("/api/registry/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      $("new-dialog").close();
      $("new-name").value = "";
      state.targetSelections.new = [];
      state.scopes.new = null;
      state.catalogValues.new = {};
      state.harnessDefaults.new = {};
      state.catalogControls.new = new Map();
      await refresh();
      await selectAgent(name);
    } catch (error) {
      setMatrixMessage(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function fetchNewDefaults(preserveValues = false) {
    const settings = newAgentSettings();
    const overrides = preserveValues ? collectCatalogValues("new") : {};
    const token = ++state.newDefaultsToken;
    const params = new URLSearchParams({
      model_tier: settings.modelTier,
      tool_policy: settings.toolPolicy
    });
    const result = await api(
      `/api/registry/agents/defaults?${params.toString()}`);
    if (token !== state.newDefaultsToken) return;
    const defaults = result && result.harness_defaults
      ? result.harness_defaults : {};
    state.harnessDefaults.new = cloneValue(defaults);
    state.catalogValues.new = mergeHarnessMaps(defaults, overrides);
    renderCatalog("new");
  }

  async function openNewDialog() {
    state.catalogValues.new = {};
    state.harnessDefaults.new = {};
    state.catalogControls.new = new Map();
    state.scopes.new = currentRoot ? "project" : "global";
    $("new-scope").value = state.scopes.new;
    renderHarnessTargets("new", targetableHarnesses("new"));
    refreshDestinations("new");
    $("new-dialog").showModal();
    $("new-name").focus();
    const catalog = $("new-catalog");
    catalog.replaceChildren();
    const loading = document.createElement("span");
    loading.className = "managed-note-text";
    loading.textContent = "Loading renderer defaults…";
    catalog.append(loading);
    try {
      await fetchNewDefaults();
    } catch (error) {
      renderCatalog("new");
      setMatrixMessage(error.message, "error");
    }
  }

  // ---- Refresh --------------------------------------------------------------

  async function loadAgentsMeta() {
    const meta = await api("/api/registry/agents");
    state.support = meta.support || {};
    state.harnesses = meta.harnesses || [];
    state.modelTiers = meta.model_tiers || [];
    state.toolPolicies = meta.tool_policies || [];
    try {
      const schema = await api("/api/registry/schema");
      state.schema = { keys: (schema && schema.keys) || {} };
    } catch (_) {
      state.schema = { keys: {} };
    }
    state.modelChoices = {};
    if (currentRoot) {
      try {
        const models = await apiRoot("/api/registry/models");
        const available = models && models.available &&
          typeof models.available === "object" ? models.available : {};
        Object.keys(available).forEach((harness) => {
          if (!Array.isArray(available[harness])) return;
          state.modelChoices[harness] = available[harness]
            .filter((model) => typeof model === "string" && model);
        });
        (models.rows || []).forEach((row) => {
          if (!row || typeof row.harness !== "string" ||
              typeof row.model !== "string") return;
          const choices = state.modelChoices[row.harness] || [];
          if (!choices.includes(row.model)) choices.push(row.model);
          state.modelChoices[row.harness] = choices;
        });
      } catch (_) {
        state.modelChoices = {};
      }
    }
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
    state.scopes.editor = null;
    state.scopes.new = null;
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
  $("editor-scope").addEventListener("change", () => {
    state.scopes.editor = $("editor-scope").value;
    if (state.current) setEditorMessage("Unsaved changes", "error");
    collectCatalogValues("editor");
    renderHarnessTargets("editor", state.targetSelections.editor);
    renderCatalog("editor");
    refreshDestinations("editor");
  });
  $("editor-catalog").addEventListener("input", () => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
  });
  $("editor-catalog").addEventListener("change", () => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
  });

  $("new-agent").addEventListener("click", openNewDialog);
  $("new-name").addEventListener("input", () => refreshDestinations("new"));
  ["new-model-tier", "new-tool-policy"].forEach((id) => {
    const control = $(id);
    if (control) {
      control.addEventListener("change", () => {
        fetchNewDefaults(true).catch((error) => {
          setMatrixMessage(error.message, "error");
        });
      });
    }
  });
  $("new-scope").addEventListener("change", () => {
    state.scopes.new = $("new-scope").value;
    collectCatalogValues("new");
    renderHarnessTargets("new", state.targetSelections.new);
    renderCatalog("new");
    refreshDestinations("new");
  });
  $("new-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.submitter && event.submitter.id === "new-cancel") $("new-dialog").close();
    else createAgent();
  });
  $("new-dialog").addEventListener("click", (event) => { if (event.target === $("new-dialog")) $("new-dialog").close(); });

  refresh();
})();
