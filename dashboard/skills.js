(() => {
  "use strict";

  const DESTINATIONS = {
    claude: ["skill", "command", "agent"],
    codex: ["skill", "agent"],
    kimi: ["skill"],
    zcode: ["skill"],
    omp: ["command", "agent"],
    opencode: ["command", "agent"]
  };
  let currentRoot = "";
  const state = {
    entries: [],
    selectedPath: null,
    current: null,
    filter: { scope: "all", harness: "all", surface: "all" },
    busy: false
  };

  function withRoot(url) {
    return currentRoot ? `${url}${url.includes("?") ? "&" : "?"}root=${encodeURIComponent(currentRoot)}` : url;
  }
  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path ? event.detail.path : "";
    refresh();
  });

  const $ = (id) => document.getElementById(id);
  const tree = $("skills-tree");
  const entryList = $("entry-list");
  const pageState = $("page-state");
  const editorEmpty = $("editor-empty");
  const editorMain = $("editor-main");
  const bodyText = $("body-text");
  const editorMessage = $("editor-message");

  function setPageState(text, error = false) {
    pageState.textContent = text;
    pageState.classList.toggle("is-error", error);
  }

  async function api(url, options = {}) {
    const response = await fetch(withRoot(url), options);
    const raw = await response.text();
    let data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (_) { data = raw; }
    }
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.error : data;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function displayName(value) {
    return String(value || "").replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function valueText(value) {
    if (Array.isArray(value)) return value.join(", ");
    if (value && typeof value === "object") return JSON.stringify(value);
    return String(value ?? "");
  }

  function statusClass(status) {
    return `status-${String(status || "unknown").replace(/_/g, "-")}`;
  }

  function selectedEntries() {
    return state.entries.filter((entry) =>
      (state.filter.scope === "all" ||
       (state.filter.scope === "project" ? (currentRoot && entry.path.startsWith(currentRoot)) : entry.scope === state.filter.scope)) &&
      (state.filter.harness === "all" || entry.harness === state.filter.harness) &&
      (state.filter.surface === "all" || entry.surface === state.filter.surface)
    );
  }

  function makeStatus(status) {
    const label = status || "unknown";
    const badge = document.createElement("span");
    badge.className = `status-badge ${statusClass(label)}`;
    badge.textContent = label;
    return badge;
  }

  function makeTreeDetails(label, count) {
    const details = document.createElement("details");
    details.className = "tree-group";
    details.open = true;
    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.textContent = displayName(label);
    const number = document.createElement("span");
    number.className = "tree-count";
    number.textContent = count;
    summary.append(title, number);
    details.append(summary);
    return { details, children: document.createElement("div") };
  }

  function makeTreeButton(label, count, attrs, active) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tree-surface";
    button.dataset.scope = attrs.scope;
    button.dataset.harness = attrs.harness;
    button.dataset.surface = attrs.surface;
    button.classList.toggle("is-active", active);
    const name = document.createElement("span");
    name.textContent = displayName(label);
    const number = document.createElement("span");
    number.className = "tree-count";
    number.textContent = count;
    button.append(name, number);
    return button;
  }

  function renderTree() {
    tree.replaceChildren();
    const all = document.createElement("button");
    all.type = "button";
    all.className = "tree-all";
    all.textContent = `All entries  ·  ${selectedEntries().length}`;
    all.classList.toggle("is-active", Object.values(state.filter).every((value) => value === "all"));
    all.dataset.scope = "all"; all.dataset.harness = "all"; all.dataset.surface = "all";
    tree.append(all);

    const scopes = new Map();
    selectedEntries().forEach((entry) => {
      const scope = entry.scope || "unknown";
      const harness = entry.harness || "unknown";
      const surface = entry.surface || "unknown";
      if (!scopes.has(scope)) scopes.set(scope, new Map());
      if (!scopes.get(scope).has(harness)) scopes.get(scope).set(harness, new Map());
      const surfaceMap = scopes.get(scope).get(harness);
      surfaceMap.set(surface, (surfaceMap.get(surface) || 0) + 1);
    });

    [...scopes.keys()].sort().forEach((scope) => {
      const harnesses = scopes.get(scope);
      const scopeCount = [...harnesses.values()].reduce((total, surfaces) => total + [...surfaces.values()].reduce((a, b) => a + b, 0), 0);
      const scopeGroup = makeTreeDetails(scope, scopeCount);
      scopeGroup.details.classList.add("tree-scope");
      const scopeChildren = scopeGroup.children;
      harnesses.forEach((surfaces, harness) => {
        const harnessCount = [...surfaces.values()].reduce((a, b) => a + b, 0);
        const harnessGroup = makeTreeDetails(harness, harnessCount);
        harnessGroup.details.classList.add("tree-harness");
        const surfaceChildren = harnessGroup.children;
        [...surfaces.keys()].sort().forEach((surface) => {
          surfaceChildren.append(makeTreeButton(surface, surfaces.get(surface), {
            scope, harness, surface
          }, state.filter.scope === scope && state.filter.harness === harness && state.filter.surface === surface));
        });
        harnessGroup.details.append(surfaceChildren);
        scopeChildren.append(harnessGroup.details);
      });
      scopeGroup.details.append(scopeChildren);
      tree.append(scopeGroup.details);
    });
  }

  function listFilterLabel() {
    const parts = [state.filter.scope, state.filter.harness, state.filter.surface].filter((value) => value !== "all");
    return parts.length ? parts.map(displayName).join(" / ") : "All locations";
  }

  function renderList() {
    const entries = selectedEntries().sort((a, b) =>
      String(a.name).localeCompare(String(b.name)) || String(a.harness).localeCompare(String(b.harness)) || String(a.path).localeCompare(String(b.path))
    );
    $("list-count").textContent = `${entries.length} ${entries.length === 1 ? "entry" : "entries"}`;
    $("list-filter").textContent = listFilterLabel();
    entryList.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "editor-empty";
      empty.textContent = "No files in this location.";
      entryList.append(empty);
      return;
    }
    entries.forEach((entry) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "entry-row";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(entry.path === state.selectedPath));
      row.classList.toggle("is-active", entry.path === state.selectedPath);
      row.dataset.path = entry.path;
      const head = document.createElement("span"); head.className = "entry-head";
      const name = document.createElement("span"); name.className = "entry-name"; name.textContent = entry.name || entry.path;
      const size = document.createElement("span"); size.className = "entry-size"; size.textContent = formatBytes(entry.size);
      head.append(name, size);
      const meta = document.createElement("span"); meta.className = "entry-meta";
      meta.append(makeStatus(entry.status), document.createTextNode(`${entry.harness || "?"} / ${entry.surface || "?"}`));
      row.append(head, meta);
      entryList.append(row);
    });
  }

  function renderFrontmatter(fields) {
    const table = $("frontmatter-table");
    table.replaceChildren();
    const keys = Object.keys(fields || {}).sort();
    if (!keys.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td"); cell.className = "frontmatter-empty"; cell.textContent = "No frontmatter fields";
      row.append(cell); table.append(row); return;
    }
    keys.forEach((key) => {
      const row = document.createElement("tr");
      const heading = document.createElement("th"); heading.scope = "row"; heading.textContent = key;
      const value = document.createElement("td"); value.textContent = valueText(fields[key]);
      row.append(heading, value); table.append(row);
    });
  }

  function setEditorMessage(text, kind = "") {
    editorMessage.textContent = text;
    editorMessage.className = `message${kind ? ` is-${kind}` : ""}`;
  }

  function renderEditor() {
    if (!state.current) {
      editorEmpty.hidden = false; editorMain.hidden = true; return;
    }
    editorEmpty.hidden = true; editorMain.hidden = false;
    const { entry, frontmatter, body } = state.current;
    $("editor-title").textContent = entry.name || "Untitled file";
    $("editor-path").textContent = entry.path;
    const status = $("editor-status");
    status.replaceChildren(makeStatus(entry.status));
    renderFrontmatter(frontmatter);
    bodyText.value = body;
    $("body-size").textContent = `${formatBytes(new TextEncoder().encode(body).length)} body`;
    setEditorMessage("No changes");
    $("import-name").value = entry.name || "";
    updateImportSurfaces();
  }

  async function selectEntry(path) {
    const entry = state.entries.find((candidate) => candidate.path === path);
    if (!entry) return;
    state.selectedPath = path;
    state.current = null;
    renderList();
    editorEmpty.hidden = false; editorMain.hidden = true;
    setPageState("reading file…");
    try {
      const file = await api(`/api/registry/file?path=${encodeURIComponent(path)}`);
      state.current = { entry, frontmatter: file.frontmatter || {}, body: typeof file.body === "string" ? file.body : "" };
      renderEditor();
      setPageState("registry ready");
    } catch (error) {
      setPageState(error.message, true);
      editorEmpty.hidden = false; editorMain.hidden = true;
      editorEmpty.replaceChildren();
      const strong = document.createElement("strong"); strong.textContent = "Unable to read file";
      editorEmpty.append(strong, document.createTextNode(error.message));
    }
  }

  function setFilter(scope, harness, surface) {
    state.filter = { scope, harness, surface };
    const visible = selectedEntries();
    if (state.selectedPath && !visible.some((entry) => entry.path === state.selectedPath)) {
      state.selectedPath = null; state.current = null;
    }
    renderTree(); renderList(); renderEditor();
  }

  function serializeFrontmatter(fields) {
    const keys = Object.keys(fields || {});
    if (!keys.length) return "";
    const lines = keys.sort().map((key) => {
      const value = fields[key];
      let encoded = Array.isArray(value) ? `[${value.join(", ")}]` : String(value ?? "");
      encoded = encoded.replace(/[\r\n]/g, " ");
      return `${key}: ${encoded}`;
    });
    return `---\n${lines.join("\n")}\n---\n`;
  }

  function fullContent() {
    if (!state.current) return bodyText.value;
    return serializeFrontmatter(state.current.frontmatter) + bodyText.value;
  }

  function setBusy(busy) {
    state.busy = busy;
    [$("save-file"), $("delete-file"), $("import-toggle"), $("new-skill"), $("import-file"), $("new-create")].forEach((button) => {
      if (button) button.disabled = busy;
    });
  }

  async function refresh(selectPath = null) {
    setPageState("scanning registry…");
    try {
      const registry = await api("/api/registry");
      const statusByPath = new Map();
      (registry.groups || []).forEach((group) => (group.installations || []).forEach((installation) => {
        if (installation.path) statusByPath.set(installation.path, installation.status);
      }));
      state.entries = (registry.entries || []).filter((entry) => entry && entry.path).map((entry) => ({
        ...entry,
        status: entry.managed ? "managed" : (statusByPath.get(entry.path) || (entry.scope === "canonical" ? "canonical" : "unknown"))
      }));
      renderTree(); renderList();
      setPageState(`${state.entries.length} files · registry ready`);
      if (selectPath && state.entries.some((entry) => entry.path === selectPath)) {
        await selectEntry(selectPath);
      } else if (state.selectedPath && state.entries.some((entry) => entry.path === state.selectedPath)) {
        await selectEntry(state.selectedPath);
      } else {
        state.selectedPath = null; state.current = null; renderEditor();
      }
      return registry;
    } catch (error) {
      state.entries = []; state.current = null; renderTree(); renderList(); renderEditor();
      setPageState(error.message, true);
      return null;
    }
  }

  function destinationOptions(select, preferredHarness = "claude") {
    select.replaceChildren();
    Object.keys(DESTINATIONS).forEach((harness) => {
      const option = document.createElement("option"); option.value = harness; option.textContent = displayName(harness);
      option.selected = harness === preferredHarness; select.append(option);
    });
  }

  function updateSurfaceSelect(select, harness, preferred = "skill") {
    select.replaceChildren();
    (DESTINATIONS[harness] || ["skill"]).forEach((surface) => {
      const option = document.createElement("option"); option.value = surface; option.textContent = displayName(surface);
      option.selected = surface === preferred; select.append(option);
    });
  }

  function updateImportSurfaces() {
    const harness = $("import-harness").value || "claude";
    const preferred = (DESTINATIONS[harness] || []).includes($("import-surface").value) ? $("import-surface").value : "skill";
    updateSurfaceSelect($("import-surface"), harness, preferred);
  }

  async function saveFile() {
    if (!state.current || state.busy) return;
    const path = state.current.entry.path;
    setBusy(true); setEditorMessage("Saving…");
    try {
      await api("/api/registry/file", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content: fullContent() }) });
      await refresh(path);
      setEditorMessage("Saved", "ok");
    } catch (error) { setEditorMessage(error.message, "error"); setPageState(error.message, true); }
    finally { setBusy(false); }
  }

  async function deleteFile() {
    if (!state.current || state.busy) return;
    const { path, name } = state.current.entry;
    if (!window.confirm(`Delete ${name || path}? This cannot be undone.`)) return;
    setBusy(true); setEditorMessage("Deleting…");
    try {
      await api(`/api/registry/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
      state.selectedPath = null; state.current = null;
      await refresh();
      setEditorMessage("Deleted", "ok");
    } catch (error) { setEditorMessage(error.message, "error"); setPageState(error.message, true); }
    finally { setBusy(false); }
  }

  async function importFile() {
    if (!state.current || state.busy) return;
    const name = $("import-name").value.trim();
    if (!name) { setEditorMessage("Enter a destination name", "error"); $("import-name").focus(); return; }
    setBusy(true); setEditorMessage("Copying…");
    try {
      const result = await api("/api/registry/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        from_path: state.current.entry.path,
        to_harness: $("import-harness").value,
        to_surface: $("import-surface").value,
        name,
        mode: "copy"
      }) });
      $("import-panel").hidden = true;
      await refresh(result && result.path ? result.path : null);
      setEditorMessage("Imported", "ok");
    } catch (error) { setEditorMessage(error.message, "error"); setPageState(error.message, true); }
    finally { setBusy(false); }
  }

  async function createSkill() {
    const form = $("new-form");
    if (state.busy || !form.checkValidity()) { form.reportValidity(); return; }
    const name = $("new-name").value.trim();
    setBusy(true);
    try {
      const result = await api("/api/registry/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        harness: $("new-harness").value,
        surface: $("new-surface").value,
        name,
        content: ""
      }) });
      $("new-dialog").close();
      $("new-name").value = "";
      await refresh(result && result.path ? result.path : null);
      if (!result || !result.path) {
        const created = state.entries.find((entry) => entry.name === name && entry.harness === $("new-harness").value && entry.surface === $("new-surface").value);
        if (created) await selectEntry(created.path);
      }
    } catch (error) { setPageState(error.message, true); }
    finally { setBusy(false); }
  }

  tree.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-scope]");
    if (!button) return;
    setFilter(button.dataset.scope, button.dataset.harness, button.dataset.surface);
  });
  entryList.addEventListener("click", (event) => {
    const row = event.target.closest("button[data-path]");
    if (row) selectEntry(row.dataset.path);
  });
  bodyText.addEventListener("input", () => {
    if (!state.current) return;
    setEditorMessage("Unsaved changes", "error");
    $("body-size").textContent = `${formatBytes(new TextEncoder().encode(bodyText.value).length)} body`;
  });
  $("save-file").addEventListener("click", saveFile);
  $("delete-file").addEventListener("click", deleteFile);
  $("import-toggle").addEventListener("click", () => { $("import-panel").hidden = !$("import-panel").hidden; });
  $("import-cancel").addEventListener("click", () => { $("import-panel").hidden = true; });
  $("import-file").addEventListener("click", importFile);
  $("import-harness").addEventListener("change", updateImportSurfaces);
  $("new-harness").addEventListener("change", () => updateSurfaceSelect($("new-surface"), $("new-harness").value));
  $("new-skill").addEventListener("click", () => { $("new-dialog").showModal(); $("new-name").focus(); });
  $("new-form").addEventListener("submit", (event) => { event.preventDefault(); if (event.submitter && event.submitter.id === "new-cancel") $("new-dialog").close(); else createSkill(); });
  $("new-dialog").addEventListener("click", (event) => { if (event.target === $("new-dialog")) $("new-dialog").close(); });

  document.querySelectorAll("[data-scope-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter.scope = button.dataset.scopeFilter;
      renderTree(); renderList();
    });
  });
  destinationOptions($("import-harness"));
  destinationOptions($("new-harness"));
  updateSurfaceSelect($("import-surface"), $("import-harness").value, "skill");
  updateSurfaceSelect($("new-surface"), $("new-harness").value, "skill");
  refresh();
})();
