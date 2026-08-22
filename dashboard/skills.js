(() => {
  "use strict";

  // Fallback only: used if GET /api/registry/schema fails, so the page never
  // breaks. The server (`registry/scan.py` SURFACE_FORMAT / KEY_SCHEMA) is
  // the single source of truth once the fetch succeeds.
  const FALLBACK_DESTINATIONS = {
    claude: ["skill", "command", "agent"],
    codex: ["skill", "agent"],
    kimi: ["skill"],
    zcode: ["skill"],
    omp: ["command", "agent"],
    opencode: ["command", "agent"]
  };
  let currentRoot = "";
  let schema = { destinations: FALLBACK_DESTINATIONS, formats: {}, keys: {} };
  const state = {
    entries: [],
    selectedPath: null,
    current: null,
    formControls: [],
    filter: { scope: "all", harness: "all", surface: "all" },
    busy: false
  };

  function withRoot(url) {
    return currentRoot ? `${url}${url.includes("?") ? "&" : "?"}root=${encodeURIComponent(currentRoot)}` : url;
  }

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

  function setEditorMessage(text, kind = "") {
    editorMessage.textContent = text;
    editorMessage.className = `message${kind ? ` is-${kind}` : ""}`;
  }

  // ---- Frontmatter helpers -------------------------------------------------
  //
  // The server is the only thing that ever turns a frontmatter value into
  // file bytes (POST /api/registry/serialize). This file never String()s or
  // JSON.stringify()s a value into on-disk content.
  //
  // For "raw" widgets (nested maps like opencode's `permission`, JSON-schema
  // blocks like omp's `output`, or any unknown non-scalar key) we still need
  // *some* YAML text to seed the textarea with. Rather than parsing YAML in
  // JS, we ask the server to serialize exactly those keys in one combined
  // call, then split the returned frontmatter block back into per-key text
  // by scanning for un-indented "key:" lines — the same shape the server
  // itself will accept back, wrapped as {"$yaml": "<edited text>"}.

  function extractFrontmatterBlock(content, format) {
    if (format !== "yaml") return String(content ?? "");
    const lines = String(content ?? "").split("\n");
    if (lines[0] !== "---") return String(content ?? "");
    let end = lines.length;
    for (let i = 1; i < lines.length; i++) {
      if (lines[i] === "---") { end = i; break; }
    }
    return lines.slice(1, end).join("\n");
  }

  function splitTopLevelBlocks(text, format) {
    const lines = String(text ?? "").split("\n");
    const keyRe = format === "toml"
      ? /^\s*("(?:[^"\\]|\\.)*"|[^\s=]+)\s*=(.*)$/
      : /^("(?:[^"\\]|\\.)*"|'[^']*'|[^\s:'"][^:]*):(.*)$/;
    const blocks = {};
    let currentKey = null;
    let currentLines = [];
    const flush = () => {
      if (currentKey !== null) blocks[currentKey] = currentLines.join("\n").replace(/^\n+/, "");
    };
    lines.forEach((line) => {
      if (line.length && !/^\s/.test(line)) {
        const match = line.match(keyRe);
        if (match) {
          flush();
          let rawKey = match[1];
          if (rawKey.startsWith('"') && rawKey.endsWith('"') && rawKey.length >= 2) rawKey = rawKey.slice(1, -1);
          currentKey = rawKey;
          const rest = match[2] || "";
          currentLines = [rest.startsWith(" ") ? rest.slice(1) : rest];
          return;
        }
      }
      if (currentKey !== null) currentLines.push(line);
    });
    flush();
    return blocks;
  }

  function classifyUnknownWidget(value) {
    if (Array.isArray(value)) {
      return value.some((item) => item && typeof item === "object") ? "raw" : "list";
    }
    if (value && typeof value === "object") return "raw";
    return "text";
  }

  // Defensive: even for a *known* schema field, if the actual value on disk
  // is a nested map (or array-of-maps) never trust the schema's declared
  // widget over the data's real shape — force it onto the raw YAML path.
  // This is the guard against the original [object Object] bug: a scalar
  // widget (text/textarea/list) only ever calls String() on a value that is
  // provably not an object, because non-scalars are always routed to "raw".
  function effectiveWidgetFor(spec, hasValue, value) {
    if (hasValue && spec.widget !== "raw" && classifyUnknownWidget(value) === "raw") return "raw";
    return spec.widget || "text";
  }

  function buildFieldRow(spec, initialValue, opts = {}) {
    const { unknown = false, disabled = false, rawText = "" } = opts;
    const row = document.createElement("div");
    row.className = `field-row${unknown ? " field-unknown" : ""}`;
    const widget = spec.widget || "text";
    const label = document.createElement("label");
    label.className = "field-label";
    const labelText = `${spec.key}${spec.required ? " *" : ""}`;
    let control;
    if (widget === "checkbox") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(initialValue);
      label.append(control, ` ${labelText}`);
    } else if (widget === "select") {
      control = document.createElement("select");
      control.className = "field-control";
      if (!spec.required) {
        const empty = document.createElement("option"); empty.value = ""; empty.textContent = "—";
        control.append(empty);
      }
      (spec.enum || []).forEach((choice) => {
        const option = document.createElement("option");
        option.value = choice; option.textContent = choice;
        option.selected = String(initialValue ?? "") === choice;
        control.append(option);
      });
      label.append(labelText, control);
    } else if (widget === "textarea") {
      control = document.createElement("textarea");
      control.className = "field-control field-textarea";
      control.rows = 3;
      control.value = initialValue == null ? "" : String(initialValue);
      label.append(labelText, control);
    } else if (widget === "raw") {
      control = document.createElement("textarea");
      control.className = "field-control field-raw";
      control.rows = 6;
      control.spellcheck = false;
      control.value = rawText;
      label.append(labelText, control);
    } else if (widget === "list") {
      control = document.createElement("input");
      control.type = "text";
      control.className = "field-control";
      control.value = Array.isArray(initialValue) ? initialValue.join(", ") : (initialValue == null ? "" : String(initialValue));
      label.append(labelText, control);
    } else {
      control = document.createElement("input");
      control.type = "text";
      control.className = "field-control";
      control.value = initialValue == null ? "" : String(initialValue);
      label.append(labelText, control);
    }
    if (spec.required && (widget === "text" || widget === "textarea" || widget === "select")) control.required = true;
    if (disabled) control.disabled = true;
    control.dataset.fieldKey = spec.key;
    row.append(label);
    if (unknown) {
      const tag = document.createElement("span");
      tag.className = "field-tag";
      tag.textContent = "unknown key — not in schema";
      row.append(tag);
    }
    if (spec.help) {
      const help = document.createElement("p");
      help.className = "field-help";
      help.textContent = spec.help;
      row.append(help);
    }
    return { row, control, widget, spec, isArrayOriginally: Array.isArray(initialValue), wasPresent: false, unknownOriginalType: null };
  }

  async function renderFrontmatterForm() {
    const container = $("frontmatter-form");
    container.replaceChildren();
    state.formControls = [];
    const cur = state.current;
    if (!cur || cur.format === "text") return;

    const schemaKey = `${cur.entry.harness}:${cur.entry.surface}`;
    const specs = schema.keys[schemaKey] || [];
    const frontmatter = cur.frontmatter || {};
    const knownKeys = new Set(specs.map((spec) => spec.key));
    const unknownKeys = Object.keys(frontmatter).filter((key) => !knownKeys.has(key));

    const rawSpecKeys = specs.filter((spec) => {
      const has = Object.prototype.hasOwnProperty.call(frontmatter, spec.key);
      return has && effectiveWidgetFor(spec, has, frontmatter[spec.key]) === "raw";
    }).map((spec) => spec.key);
    const unknownRawKeys = unknownKeys.filter((key) => classifyUnknownWidget(frontmatter[key]) === "raw");
    const rawKeys = [...rawSpecKeys, ...unknownRawKeys];

    let rawTexts = {};
    if (rawKeys.length) {
      const payload = {};
      rawKeys.forEach((key) => { payload[key] = frontmatter[key]; });
      try {
        const resp = await api("/api/registry/serialize", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ format: cur.format, frontmatter: payload, body: "", harness: cur.entry.harness, surface: cur.entry.surface })
        });
        rawTexts = splitTopLevelBlocks(extractFrontmatterBlock(resp.content, cur.format), cur.format);
      } catch (error) {
        setEditorMessage(`Could not preload raw YAML for: ${rawKeys.join(", ")} (${error.message})`, "error");
      }
    }

    const disabled = Boolean(cur.managed);
    specs.forEach((spec) => {
      const has = Object.prototype.hasOwnProperty.call(frontmatter, spec.key);
      const value = has ? frontmatter[spec.key] : (spec.type === "bool" ? false : "");
      const effectiveWidget = effectiveWidgetFor(spec, has, value);
      const specForRow = effectiveWidget === spec.widget ? spec : { ...spec, widget: effectiveWidget };
      const rawText = effectiveWidget === "raw" ? (rawTexts[spec.key] || "") : "";
      const built = buildFieldRow(specForRow, value, { disabled, rawText });
      built.wasPresent = has;
      container.append(built.row);
      state.formControls.push(built);
    });

    if (unknownKeys.length) {
      const note = document.createElement("p");
      note.className = "field-help field-unknown-note";
      note.textContent = `${unknownKeys.length} unknown key${unknownKeys.length === 1 ? "" : "s"} not in the ${schemaKey} schema — shown below, in file order.`;
      container.append(note);
      unknownKeys.forEach((key) => {
        const value = frontmatter[key];
        const widget = classifyUnknownWidget(value);
        const spec = {
          key, widget, required: false, enum: null, help: null,
          type: widget === "raw" ? "map" : (widget === "list" ? "list" : (typeof value === "boolean" ? "bool" : "string"))
        };
        const rawText = widget === "raw" ? (rawTexts[key] || "") : "";
        const built = buildFieldRow(spec, value, { unknown: true, disabled, rawText });
        built.wasPresent = true;
        built.unknownOriginalType = typeof value;
        container.append(built.row);
        state.formControls.push(built);
      });
    }
  }

  function computeFieldValue(fc) {
    const { widget, control } = fc;
    if (widget === "checkbox") return control.checked;
    if (widget === "raw") return { "$yaml": control.value };
    if (widget === "list") {
      // A "list" widget is always comma-text <-> array, regardless of
      // whether this key was originally present (and regardless of what
      // shape its original value had) — otherwise a schema list field the
      // user fills in for the first time would submit as a bare string.
      const text = control.value;
      return text.trim() === "" ? [] : text.split(",").map((item) => item.trim()).filter((item) => item.length > 0);
    }
    if (fc.unknownOriginalType === "boolean") return control.value.trim().toLowerCase() === "true";
    if (fc.unknownOriginalType === "number") {
      const num = Number(control.value);
      return Number.isNaN(num) ? control.value : num;
    }
    if (fc.spec.type === "int") {
      if (control.value === "") return "";
      const num = Number(control.value);
      return Number.isNaN(num) ? control.value : num;
    }
    return control.value;
  }

  function isEmptyForOmission(value, widget) {
    if (widget === "checkbox") return value === false;
    if (widget === "raw") return !value || !value.$yaml || value.$yaml.trim() === "";
    if (Array.isArray(value)) return value.length === 0;
    return value === "" || value === null || value === undefined;
  }

  // Preserve key order: untouched keys keep the original file's order, then
  // any newly-filled-in schema keys are appended at the end. Never sort.
  function buildOutgoingFrontmatter() {
    const cur = state.current;
    const original = cur.frontmatter || {};
    const out = {};
    const emitted = new Set();
    const byKey = new Map(state.formControls.map((fc) => [fc.spec.key, fc]));
    Object.keys(original).forEach((key) => {
      const fc = byKey.get(key);
      out[key] = fc ? computeFieldValue(fc) : original[key];
      emitted.add(key);
    });
    state.formControls.forEach((fc) => {
      if (emitted.has(fc.spec.key)) return;
      const value = computeFieldValue(fc);
      if (isEmptyForOmission(value, fc.widget)) return;
      out[fc.spec.key] = value;
      emitted.add(fc.spec.key);
    });
    return out;
  }

  function expectedNameFor(entry) {
    const path = entry.path || "";
    const segments = path.split("/");
    const file = segments[segments.length - 1] || "";
    // Case-sensitive to match the server's exact check (p.name == "SKILL.md")
    // in _handle_registry_serialize — a differing case would make the client
    // and server disagree about whether a name mismatch warning applies.
    if (file === "SKILL.md") return segments[segments.length - 2] || "";
    return file.replace(/\.[^.]+$/, "");
  }

  function validateForm() {
    const cur = state.current;
    for (const fc of state.formControls) {
      if (!fc.spec.required) continue;
      const value = computeFieldValue(fc);
      if (isEmptyForOmission(value, fc.widget)) return `"${fc.spec.key}" is required`;
    }
    // Only enforce the name/filename match when the SCHEMA declares a
    // "name" key for this harness:surface — not merely when some unrelated,
    // unknown key happens to also be called "name".
    const schemaKey = `${cur.entry.harness}:${cur.entry.surface}`;
    const schemaHasName = (schema.keys[schemaKey] || []).some((spec) => spec.key === "name");
    const nameControl = schemaHasName ? state.formControls.find((fc) => fc.spec.key === "name") : null;
    if (nameControl && cur.entry.harness !== "omnigent") {
      const nameValue = String(computeFieldValue(nameControl) || "").trim();
      const expected = expectedNameFor(cur.entry);
      if (nameValue && expected && nameValue !== expected) {
        return `"name" (${nameValue}) does not match ${expected}`;
      }
    }
    return null;
  }

  function snapshotFormControls() {
    return state.formControls.map((fc) => ({
      key: fc.spec.key,
      widget: fc.widget,
      value: fc.widget === "checkbox" ? fc.control.checked : fc.control.value
    }));
  }

  function applyFormSnapshot(snapshot) {
    if (!snapshot) return;
    snapshot.forEach((item) => {
      const fc = state.formControls.find((candidate) => candidate.spec.key === item.key);
      if (!fc) return;
      if (item.widget === "checkbox") fc.control.checked = item.value; else fc.control.value = item.value;
    });
  }

  function ensureTrailingNewline(text) {
    return text.endsWith("\n") ? text : `${text}\n`;
  }

  async function onRawToggle(event) {
    const cur = state.current;
    if (!cur || cur.format === "text") { event.target.checked = false; return; }
    if (event.target.checked) {
      cur.formSnapshot = snapshotFormControls();
      setEditorMessage("Loading raw frontmatter…");
      try {
        const frontmatter = buildOutgoingFrontmatter();
        const resp = await api("/api/registry/serialize", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            format: cur.format, frontmatter, body: cur.format === "toml" ? bodyText.value : "",
            harness: cur.entry.harness, surface: cur.entry.surface
          })
        });
        const block = extractFrontmatterBlock(resp.content, cur.format);
        cur.rawBaseline = block;
        cur.rawMode = true;
        $("frontmatter-raw-text").value = block;
        $("frontmatter-form").hidden = true;
        $("frontmatter-raw").hidden = false;
        $("body-editor").hidden = cur.format === "toml";
        setEditorMessage("Editing raw frontmatter");
      } catch (error) {
        event.target.checked = false;
        setEditorMessage(error.message, "error");
      }
    } else {
      const rawNow = $("frontmatter-raw-text").value;
      if (cur.rawBaseline !== undefined && rawNow !== cur.rawBaseline) {
        const proceed = window.confirm("Discard raw frontmatter edits and return to the form view? Typed-field edits made before switching to raw mode are kept.");
        if (!proceed) { event.target.checked = true; return; }
      }
      cur.rawMode = false;
      $("frontmatter-raw").hidden = true;
      $("frontmatter-form").hidden = false;
      $("body-editor").hidden = false;
      await renderFrontmatterForm();
      applyFormSnapshot(cur.formSnapshot);
      cur.formSnapshot = null;
      setEditorMessage("No changes");
    }
  }

  async function renderEditor() {
    if (!state.current) {
      editorEmpty.hidden = false; editorMain.hidden = true; return;
    }
    editorEmpty.hidden = true; editorMain.hidden = false;
    const cur = state.current;
    const { entry, body, format, managed } = cur;
    $("editor-title").textContent = entry.name || "Untitled file";
    $("editor-path").textContent = entry.path;
    const status = $("editor-status");
    status.replaceChildren(makeStatus(entry.status));
    $("body-label").textContent = format === "toml" ? "Developer instructions" : "Body";

    $("frontmatter-section").hidden = format === "text";
    $("managed-note").hidden = !managed;
    $("raw-toggle").checked = false;
    $("raw-toggle").disabled = managed || format === "text";
    cur.rawMode = false;
    cur.rawBaseline = undefined;
    cur.formSnapshot = null;
    $("frontmatter-form").hidden = false;
    $("frontmatter-raw").hidden = true;
    $("body-editor").hidden = false;

    if (format !== "text") {
      await renderFrontmatterForm();
    } else {
      $("frontmatter-form").replaceChildren();
      state.formControls = [];
    }

    bodyText.value = body;
    bodyText.disabled = Boolean(managed);
    $("body-size").textContent = `${formatBytes(new TextEncoder().encode(body).length)} body`;
    setEditorMessage(managed ? "Managed externally — read only" : "No changes");
    $("save-file").disabled = Boolean(managed);
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
      state.current = {
        entry,
        format: file.format || "text",
        frontmatter: file.frontmatter || {},
        body: typeof file.body === "string" ? file.body : "",
        managed: Boolean(file.managed),
        rawMode: false,
        rawBaseline: undefined,
        formSnapshot: null
      };
      await renderEditor();
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

  function setBusy(busy) {
    state.busy = busy;
    const managedLock = Boolean(state.current && state.current.managed);
    [$("delete-file"), $("import-toggle"), $("new-skill"), $("import-file"), $("new-create")].forEach((button) => {
      if (button) button.disabled = busy;
    });
    $("save-file").disabled = busy || managedLock;
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

  async function loadSchema() {
    try {
      const data = await api("/api/registry/schema");
      schema = {
        destinations: (data && data.destinations) || FALLBACK_DESTINATIONS,
        formats: (data && data.formats) || {},
        keys: (data && data.keys) || {}
      };
    } catch (_error) {
      schema = { destinations: FALLBACK_DESTINATIONS, formats: {}, keys: {} };
    }
  }

  function destinationOptions(select, preferredHarness = "claude") {
    select.replaceChildren();
    Object.keys(schema.destinations).forEach((harness) => {
      const option = document.createElement("option"); option.value = harness; option.textContent = displayName(harness);
      option.selected = harness === preferredHarness; select.append(option);
    });
  }

  function updateSurfaceSelect(select, harness, preferred = "skill") {
    select.replaceChildren();
    (schema.destinations[harness] || ["skill"]).forEach((surface) => {
      const option = document.createElement("option"); option.value = surface; option.textContent = displayName(surface);
      option.selected = surface === preferred; select.append(option);
    });
  }

  function updateImportSurfaces() {
    const harness = $("import-harness").value || "claude";
    const preferred = (schema.destinations[harness] || []).includes($("import-surface").value) ? $("import-surface").value : "skill";
    updateSurfaceSelect($("import-surface"), harness, preferred);
  }

  function refreshDestinationSelects() {
    destinationOptions($("import-harness"));
    destinationOptions($("new-harness"));
    updateSurfaceSelect($("import-surface"), $("import-harness").value, "skill");
    updateSurfaceSelect($("new-surface"), $("new-harness").value, "skill");
  }

  async function saveFile() {
    if (!state.current || state.busy || state.current.managed) return;
    const cur = state.current;
    const path = cur.entry.path;
    setBusy(true); setEditorMessage("Saving…");
    try {
      let content;
      let warnings = [];
      if (cur.format === "text") {
        content = bodyText.value;
      } else if (cur.rawMode) {
        // Raw mode bypasses /api/registry/serialize entirely: for TOML the
        // textarea already holds the complete file (no separate frontmatter
        // fence exists), so it is PUT verbatim; for YAML the fences are
        // re-added around the edited block and the untouched body.
        const rawValue = $("frontmatter-raw-text").value;
        content = cur.format === "toml" ? rawValue : `---\n${ensureTrailingNewline(rawValue)}---\n${bodyText.value}`;
      } else {
        const problem = validateForm();
        if (problem) { setEditorMessage(problem, "error"); setBusy(false); return; }
        const frontmatter = buildOutgoingFrontmatter();
        const resp = await api("/api/registry/serialize", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ format: cur.format, frontmatter, body: bodyText.value, harness: cur.entry.harness, surface: cur.entry.surface, path })
        });
        content = resp.content;
        warnings = Array.isArray(resp.warnings) ? resp.warnings : [];
      }
      await api("/api/registry/file", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content }) });
      await refresh(path);
      // Warnings are non-blocking: the save already succeeded, so surface
      // them alongside the success message instead of a transient one the
      // "Saved" message below would otherwise immediately overwrite.
      setEditorMessage(warnings.length ? `Saved — ${warnings.join("; ")}` : "Saved", "ok");
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

  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path ? event.detail.path : "";
    loadSchema().then(() => { refreshDestinationSelects(); return refresh(); });
  });

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
  $("frontmatter-form").addEventListener("input", () => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
  });
  $("frontmatter-raw-text").addEventListener("input", () => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
  });
  $("raw-toggle").addEventListener("change", onRawToggle);
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

  (async () => {
    await loadSchema();
    refreshDestinationSelects();
    await refresh();
  })();
})();
