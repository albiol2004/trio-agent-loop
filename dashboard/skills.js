const CUSTOM_VALUE = "__trio_custom__";

// Mirrors registry/scan.py's project collect directories. Unsupported pairs
// never reach the create endpoint.
const PROJECT_REGISTRY_DIRS = {
  "claude:skill": ".claude/skills",
  "claude:command": ".claude/commands",
  "claude:agent": ".claude/agents",
  "opencode:agent": ".opencode/agents",
  "cursor:skill": ".cursor/skills",
};
const PROJECT_DESTINATIONS = {
  claude: ["skill", "command", "agent"],
  opencode: ["agent"],
  cursor: ["skill"],
};
const GLOBAL_REGISTRY_DIRS = {
  "claude:skill": ".claude/skills",
  "claude:command": ".claude/commands",
  "claude:agent": ".claude/agents",
  "codex:skill": ".agents/skills",
  "codex:agent": ".codex/agents",
  "omp:command": ".omp/agent/commands",
  "omp:agent": ".omp/agent/agents",
  "opencode:command": ".config/opencode/commands",
  "opencode:agent": ".config/opencode/agents",
  "kimi:skill": ".kimi-code/skills",
  "zcode:skill": ".zcode/skills",
};

function registryDestinationPath(
  scope, harness, surface, name, project = "", formats = {}
) {
  const normalizedScope = String(scope || "global").toLowerCase();
  const normalizedSurface = String(surface || "").toLowerCase();
  const key = `${String(harness || "").toLowerCase()}:${normalizedSurface}`;
  const relative = normalizedScope === "project"
    ? PROJECT_REGISTRY_DIRS[key] : GLOBAL_REGISTRY_DIRS[key];
  if (
    !relative || !name ||
    (normalizedScope === "project" && !project)
  ) return null;
  const base = normalizedScope === "project"
    ? String(project).replace(/\/+$/, "") : "~";
  const extension = normalizedSurface === "skill"
    ? `${name}/SKILL.md`
    : `${name}.${formats[key] === "toml" ? "toml" : "md"}`;
  return `${base}/${relative}/${extension}`;
}

// These small pure helpers are also exported for the no-DOM regression tests.
// The browser editor uses the same functions, so tests exercise real save
// validation and catalog selection decisions rather than source markers.
function withRoot(url, root = "") {
  return root
    ? `${url}${url.includes("?") ? "&" : "?"}root=${encodeURIComponent(root)}`
    : url;
}

function jsonSchemaError(text, wasPresent = false) {
  const value = text == null ? "" : String(text);
  if (!value.trim() && !wasPresent) return null;
  try {
    JSON.parse(value);
    return null;
  } catch (error) {
    return `Invalid JSON: ${error.message}`;
  }
}

function validateJsonSchemaField(text, wasPresent = false) {
  const error = jsonSchemaError(text, wasPresent);
  return {
    valid: !error,
    fieldError: Boolean(error),
    blocksSave: Boolean(error),
    error,
  };
}

function catalogChoiceState(value, choices) {
  const current = value === null || value === undefined ? "" : String(value);
  const known = Array.isArray(choices)
    ? choices.map((choice) => String(choice)) : [];
  const offList = Boolean(current) && !known.includes(current);
  return {
    value: current,
    selected: offList ? CUSTOM_VALUE : current,
    offList,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    catalogChoiceState,
    jsonSchemaError,
    registryDestinationPath,
    validateJsonSchemaField,
    withRoot,
  };
} else {
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
    modelChoices: {},
    agentNames: [],
    catalogRoot: null,
    catalogKey: null,
    catalogPromise: null,
    formRenderToken: 0,
    filter: { scope: "all", harness: "all", surface: "all" },
    busy: false
  };

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
    const response = await fetch(withRoot(url, currentRoot), options);
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

  function sourcePromptPath(prompt) {
    if (!currentRoot || typeof prompt !== "string") return "";
    const relative = prompt.trim();
    if (!relative.startsWith("prompts/") ||
        relative.split("/").includes("..")) return "";
    return `${currentRoot.replace(/\/+$/, "")}/${relative}`;
  }

  function isPromptPath(path) {
    if (!currentRoot || typeof path !== "string") return false;
    const root = currentRoot.replace(/\/+$/, "");
    const relative = path.startsWith(`${root}/`) ? path.slice(root.length + 1) : "";
    return relative.startsWith("prompts/") &&
      !relative.split("/").includes("..");
  }

  function renderManagedSource(source, managed) {
    const panel = $("managed-source");
    const regen = $("regen-file");
    if (!panel || !regen) return;
    const visible = Boolean(managed && source);
    panel.hidden = !visible;
    regen.hidden = !visible;
    regen.disabled = !visible;
    if (!visible) return;
    const prompt = typeof source.prompt === "string" ? source.prompt : "";
    $("managed-source-prompt").textContent = prompt || "unknown";
    $("managed-source-overlay").textContent =
      typeof source.overlay === "string" ? source.overlay : "none";
    const open = $("managed-source-open");
    const absolute = sourcePromptPath(prompt);
    open.hidden = !absolute;
    if (absolute) {
      open.href = `/skills.html?path=${encodeURIComponent(absolute)}`;
    } else {
      open.removeAttribute("href");
    }
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

  const STRUCTURED_WIDGETS = new Set([
    "permission-grid", "spawns-select", "json-schema"
  ]);

  function declaredWidgetFor(spec) {
    if (typeof spec.values_from === "string" &&
        spec.values_from.startsWith("models:")) return "select";
    if (spec.widget) return spec.widget;
    return "text";
  }

  // Defensive: even for a *known* schema field, if the actual value on disk
  // is a nested map (or array-of-maps) never trust a scalar widget over the
  // data's real shape — force it onto the raw YAML path. Structured catalog
  // widgets are the deliberate exceptions because they own their object shape.
  function effectiveWidgetFor(spec, hasValue, value) {
    const widget = declaredWidgetFor(spec);
    if (hasValue && !STRUCTURED_WIDGETS.has(widget) &&
        widget !== "raw" && classifyUnknownWidget(value) === "raw") {
      return "raw";
    }
    return widget;
  }

  function isObjectMap(value) {
    return value && typeof value === "object" && !Array.isArray(value);
  }

  function cloneValue(value) {
    if (Array.isArray(value)) return value.map((item) => cloneValue(item));
    if (isObjectMap(value)) {
      const copy = {};
      Object.entries(value).forEach(([key, child]) => {
        copy[key] = cloneValue(child);
      });
      return copy;
    }
    return value;
  }

  function uniqueStrings(values) {
    const seen = new Set();
    const output = [];
    values.forEach((value) => {
      if (typeof value !== "string" || !value.trim()) return;
      const text = value.trim();
      if (seen.has(text)) return;
      seen.add(text);
      output.push(text);
    });
    return output;
  }

  function commaValues(value) {
    if (Array.isArray(value)) {
      return value.map((item) => String(item).trim()).filter(Boolean);
    }
    if (value === null || value === undefined || value === "") return [];
    return String(value).split(",").map((item) => item.trim()).filter(Boolean);
  }

  function jsonText(value) {
    if (typeof value === "string") return value;
    if (value === null || value === undefined) return "";
    try {
      return JSON.stringify(value, null, 2) || "";
    } catch (_) {
      return String(value);
    }
  }

  function makePermissionGrid(initialValue) {
    const grid = document.createElement("div");
    grid.className = "permission-grid";
    const header = document.createElement("div");
    header.className = "permission-grid-head";
    const patternHead = document.createElement("span");
    patternHead.textContent = "Tool pattern";
    const ruleHead = document.createElement("span");
    ruleHead.textContent = "Rule";
    header.append(patternHead, ruleHead);
    grid.append(header);

    const source = isObjectMap(initialValue) ? cloneValue(initialValue) : {};
    const rows = [];
    const appendMap = (map, path, depth) => {
      Object.entries(map).forEach(([pattern, value]) => {
        const row = document.createElement("div");
        row.className = "permission-row";
        const patternText = document.createElement("span");
        patternText.className = "permission-pattern";
        patternText.textContent = pattern;
        patternText.style.paddingLeft = `${depth * 16}px`;
        row.append(patternText);

        if (isObjectMap(value)) {
          row.classList.add("permission-group");
          const groupNote = document.createElement("span");
          groupNote.className = "field-help";
          groupNote.textContent = "nested rules";
          row.append(groupNote);
          grid.append(row);
          appendMap(value, path.concat(pattern), depth + 1);
          return;
        }

        const select = document.createElement("select");
        select.className = "field-control permission-value";
        ["allow", "deny"].forEach((choice) => {
          const option = document.createElement("option");
          option.value = choice;
          option.textContent = choice;
          select.append(option);
        });
        const current = value === null || value === undefined ? "" : String(value);
        if (!["allow", "deny"].includes(current)) {
          const option = document.createElement("option");
          option.value = current;
          option.textContent = current || "empty";
          select.append(option);
        }
        select.value = current;
        row.append(select);
        grid.append(row);
        rows.push({
          path: path.concat(pattern),
          control: select,
          originalValue: value
        });
      });
    };
    appendMap(source, [], 0);
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "field-help";
      empty.textContent = "No permission rules.";
      grid.append(empty);
    }
    return { grid, value: source, rows };
  }

  function buildFieldRow(spec, initialValue, opts = {}) {
    const {
      unknown = false,
      disabled = false,
      rawText = "",
      choices = []
    } = opts;
    const row = document.createElement("div");
    row.className = `field-row${unknown ? " field-unknown" : ""}`;
    const widget = declaredWidgetFor(spec);
    const label = document.createElement("label");
    label.className = "field-label";
    const labelText = `${spec.key}${spec.required ? " *" : ""}`;
    let control;
    let customControl = null;
    let permissionValue = null;
    let permissionRows = null;
    let offList = false;
    let errorNode = null;
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
      const sourceChoices = spec.values_from ? choices : (spec.enum || []);
      const knownChoices = uniqueStrings(sourceChoices.map((choice) => String(choice)));
      const choiceState = catalogChoiceState(initialValue, knownChoices);
      const current = choiceState.value;
      offList = choiceState.offList;
      const visibleChoices = [...knownChoices];
      if (offList && spec.values_from) visibleChoices.push(current);
      visibleChoices.forEach((choice) => {
        const option = document.createElement("option");
        option.value = choice; option.textContent = choice;
        control.append(option);
      });
      const customOption = document.createElement("option");
      customOption.value = CUSTOM_VALUE;
      customOption.textContent = "custom…";
      control.append(customOption);
      customControl = document.createElement("input");
      customControl.type = "text";
      customControl.className = "field-control";
      customControl.value = current;
      customControl.hidden = !offList;
      customControl.disabled = disabled || !offList;
      control.value = choiceState.selected;
      control.addEventListener("change", () => {
        const custom = control.value === CUSTOM_VALUE;
        customControl.hidden = !custom;
        customControl.disabled = disabled || !custom;
      });
      label.append(labelText, control, customControl);
    } else if (widget === "textarea") {
      control = document.createElement("textarea");
      control.className = "field-control field-textarea";
      control.rows = 3;
      control.value = initialValue == null ? "" : String(initialValue);
      label.append(labelText, control);
    } else if (widget === "permission-grid") {
      const built = makePermissionGrid(initialValue);
      control = built.grid;
      permissionValue = built.value;
      permissionRows = built.rows;
      label.append(labelText, control);
    } else if (widget === "spawns-select") {
      control = document.createElement("select");
      control.className = "field-control field-spawns";
      control.multiple = true;
      const current = commaValues(initialValue);
      const knownChoices = uniqueStrings(choices);
      const visibleChoices = uniqueStrings([...knownChoices, ...current]);
      control.size = Math.min(Math.max(visibleChoices.length, 3), 8);
      visibleChoices.forEach((choice) => {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        option.selected = current.includes(choice);
        control.append(option);
      });
      offList = current.some((choice) => !knownChoices.includes(choice));
      label.append(labelText, control);
    } else if (widget === "json-schema") {
      control = document.createElement("textarea");
      control.className = "field-control field-textarea field-json-schema";
      control.rows = 8;
      control.spellcheck = false;
      control.value = jsonText(initialValue);
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
    if (spec.required &&
        ["text", "textarea", "select", "json-schema"].includes(widget)) {
      control.required = true;
    }
    if (offList) {
      row.classList.add("field-offlist");
      control.classList.add("field-offlist");
      if (customControl) customControl.classList.add("field-offlist");
      const warning = document.createElement("p");
      warning.className = "field-help field-offlist-help";
      warning.textContent = "Current value is not in the known catalog.";
      row.append(warning);
    }
    if (disabled) {
      if (widget === "permission-grid") {
        permissionRows.forEach((item) => { item.control.disabled = true; });
      } else {
        control.disabled = true;
      }
      if (customControl) customControl.disabled = true;
    }
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
    if (widget === "json-schema") {
      errorNode = document.createElement("p");
      errorNode.className = "field-help field-error";
      errorNode.hidden = true;
      row.append(errorNode);
    }
    return {
      row, control, widget, spec, customControl, permissionValue,
      permissionRows, errorNode,
      isArrayOriginally: Array.isArray(initialValue), wasPresent: false,
      unknownOriginalType: null, jsonError: null
    };
  }

  async function preloadWidgetCatalogs() {
    const rootKey = currentRoot || "";
    if (state.catalogRoot === rootKey) return;
    if (state.catalogPromise && state.catalogKey === rootKey) {
      await state.catalogPromise;
      return;
    }
    state.catalogKey = rootKey;
    state.catalogPromise = (async () => {
      try {
        const data = await api("/api/registry/models");
        const choices = {};
        const rows = data && Array.isArray(data.rows) ? data.rows : [];
        rows.forEach((row) => {
          if (!row || typeof row.harness !== "string" ||
              typeof row.model !== "string") return;
          const values = choices[row.harness] || [];
          values.push(row.model);
          choices[row.harness] = values;
        });
        if (currentRoot === rootKey) {
          Object.keys(choices).forEach((harness) => {
            choices[harness] = uniqueStrings(choices[harness]);
          });
          state.modelChoices = choices;
          state.catalogRoot = rootKey;
        }
      } catch (_) {
        if (currentRoot === rootKey) {
          state.modelChoices = {};
          state.catalogRoot = rootKey;
        }
      }
    })();
    await state.catalogPromise;
    if (state.catalogKey === rootKey) state.catalogPromise = null;
  }

  async function renderFrontmatterForm() {
    const renderToken = ++state.formRenderToken;
    const container = $("frontmatter-form");
    const cur = state.current;
    if (!cur || cur.format === "text") {
      container.replaceChildren();
      state.formControls = [];
      return;
    }

    await preloadWidgetCatalogs();
    if (renderToken !== state.formRenderToken || cur !== state.current) return;
    container.replaceChildren();
    state.formControls = [];
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
          body: JSON.stringify({
            format: cur.format,
            frontmatter: payload,
            body: "",
            harness: cur.entry.harness,
            surface: cur.entry.surface,
            quoted_keys: cur.quotedKeys || []
          })
        });
        rawTexts = splitTopLevelBlocks(extractFrontmatterBlock(resp.content, cur.format), cur.format);
      } catch (error) {
        if (renderToken !== state.formRenderToken || cur !== state.current) return;
        setEditorMessage(`Could not preload raw YAML for: ${rawKeys.join(", ")} (${error.message})`, "error");
      }
    }
    if (renderToken !== state.formRenderToken || cur !== state.current) return;

    const disabled = Boolean(cur.managed);
    specs.forEach((spec) => {
      const has = Object.prototype.hasOwnProperty.call(frontmatter, spec.key);
      const value = has ? frontmatter[spec.key] : (
        spec.type === "bool" ? false :
        spec.widget === "permission-grid" ? {} : ""
      );
      const effectiveWidget = effectiveWidgetFor(spec, has, value);
      const specForRow = effectiveWidget === spec.widget ? spec : { ...spec, widget: effectiveWidget };
      const rawText = effectiveWidget === "raw" ? (rawTexts[spec.key] || "") : "";
      const source = typeof spec.values_from === "string" &&
        spec.values_from.startsWith("models:")
        ? spec.values_from.slice("models:".length) : null;
      const choices = source
        ? (state.modelChoices[source] || [])
        : (spec.widget === "spawns-select" ? state.agentNames : []);
      const built = buildFieldRow(specForRow, value, {
        disabled, rawText, choices
      });
      built.wasPresent = has;
      if (built.widget === "json-schema") updateJsonSchemaError(built);
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

  function valueAtPath(root, path) {
    let value = root;
    for (const key of path) {
      if (!isObjectMap(value)) return undefined;
      value = value[key];
    }
    return value;
  }

  function setValueAtPath(root, path, value) {
    if (!path.length) return;
    let target = root;
    path.slice(0, -1).forEach((key) => {
      if (!isObjectMap(target[key])) target[key] = {};
      target = target[key];
    });
    target[path[path.length - 1]] = value;
  }

  function permissionGridValue(fc) {
    const value = cloneValue(fc.permissionValue || {});
    fc.permissionRows.forEach((row) => {
      const original = row.originalValue;
      const originalText = original === null || original === undefined
        ? "" : String(original);
      const selected = row.control.value;
      const next = selected === originalText ? original : selected;
      setValueAtPath(value, row.path, next);
    });
    return value;
  }

  function updateJsonSchemaError(fc) {
    if (fc.widget !== "json-schema") return null;
    const text = fc.control.value;
    const validation = validateJsonSchemaField(text, fc.wasPresent);
    const message = validation.error;
    if (fc.errorNode) {
      fc.errorNode.textContent = message || "";
      fc.errorNode.hidden = !message;
    }
    fc.row.classList.toggle("field-error", validation.fieldError);
    fc.control.classList.toggle("field-error", validation.fieldError);
    fc.jsonError = message;
    return message;
  }

  function computeFieldValue(fc) {
    const { widget, control } = fc;
    if (widget === "checkbox") return control.checked;
    if (widget === "raw") return { "$yaml": control.value };
    if (widget === "permission-grid") return permissionGridValue(fc);
    if (widget === "spawns-select") {
      return [...control.options]
        .filter((option) => option.selected)
        .map((option) => option.value)
        .join(", ");
    }
    if (widget === "json-schema") {
      updateJsonSchemaError(fc);
      return control.value;
    }
    if (widget === "select" && fc.customControl) {
      return control.value === CUSTOM_VALUE
        ? fc.customControl.value : control.value;
    }
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
    if (widget === "permission-grid") {
      return !isObjectMap(value) || Object.keys(value).length === 0;
    }
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
      const value = computeFieldValue(fc);
      if (fc.widget === "json-schema" && fc.jsonError) {
        return `"${fc.spec.key}" must contain valid JSON`;
      }
      if (!fc.spec.required) continue;
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

  function setFormControlValue(fc, value) {
    if (fc.widget === "checkbox") {
      fc.control.checked = Boolean(value);
      return;
    }
    if (fc.widget === "permission-grid") {
      const source = isObjectMap(value) ? value : {};
      fc.permissionValue = cloneValue(source);
      fc.permissionRows.forEach((row) => {
        const current = valueAtPath(source, row.path);
        if (current !== undefined) row.originalValue = current;
        const text = current === null || current === undefined
          ? "" : String(current);
        if (![...row.control.options].some((option) => option.value === text)) {
          const option = document.createElement("option");
          option.value = text;
          option.textContent = text || "empty";
          row.control.append(option);
        }
        row.control.value = text;
      });
      return;
    }
    if (fc.widget === "spawns-select") {
      const selected = new Set(commaValues(value));
      selected.forEach((name) => {
        if ([...fc.control.options].some((option) => option.value === name)) return;
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        fc.control.append(option);
      });
      [...fc.control.options].forEach((option) => {
        option.selected = selected.has(option.value);
      });
      return;
    }
    if (fc.widget === "json-schema") {
      fc.control.value = jsonText(value);
      updateJsonSchemaError(fc);
      return;
    }
    if (fc.widget === "select" && fc.customControl) {
      const text = value === null || value === undefined ? "" : String(value);
      const regular = [...fc.control.options].some(
        (option) => option.value === text && option.value !== CUSTOM_VALUE
      );
      fc.control.value = regular ? text : CUSTOM_VALUE;
      fc.customControl.value = text;
      const custom = fc.control.value === CUSTOM_VALUE;
      fc.customControl.hidden = !custom;
      fc.customControl.disabled = fc.control.disabled || !custom;
      return;
    }
    fc.control.value = value === null || value === undefined ? "" : value;
  }

  function snapshotFormControls() {
    return state.formControls.map((fc) => ({
      key: fc.spec.key,
      widget: fc.widget,
      value: STRUCTURED_WIDGETS.has(fc.widget) || fc.customControl
        ? computeFieldValue(fc)
        : (fc.widget === "checkbox" ? fc.control.checked : fc.control.value)
    }));
  }

  function applyFormSnapshot(snapshot) {
    if (!snapshot) return;
    snapshot.forEach((item) => {
      const fc = state.formControls.find((candidate) => candidate.spec.key === item.key);
      if (!fc) return;
      setFormControlValue(fc, item.value);
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
            harness: cur.entry.harness, surface: cur.entry.surface,
            quoted_keys: cur.quotedKeys || []
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
    const { entry, body, format, managed, source, readOnly } = cur;
    $("editor-title").textContent = entry.name || "Untitled file";
    $("editor-path").textContent = entry.path;
    const status = $("editor-status");
    status.replaceChildren(makeStatus(entry.status));
    $("body-label").textContent = format === "toml" ? "Developer instructions" : "Body";

    $("frontmatter-section").hidden = format === "text";
    const managedNote = $("managed-note");
    managedNote.hidden = !managed;
    if (managed) {
      managedNote.textContent = source
        ? "Generated by prompts/generate.py — form and body are read-only. "
          + "Edit the source, then regenerate and re-install."
        : "Managed cache — read only.";
    }
    renderManagedSource(source, managed);
    $("raw-toggle").checked = false;
    $("raw-toggle").disabled = managed || readOnly || format === "text";
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
    bodyText.disabled = Boolean(managed || readOnly);
    $("body-size").textContent = `${formatBytes(new TextEncoder().encode(body).length)} body`;
    setEditorMessage(
      readOnly ? "Prompt source — read only" :
        (managed ? "Managed source — read only" : "No changes")
    );
    $("save-file").disabled = Boolean(managed || readOnly);
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
        quotedKeys: file.quoted_keys || [],
        managed: Boolean(file.managed),
        source: file.source || entry.source || null,
        readOnly: false,
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

  async function selectSource(path) {
    if (!isPromptPath(path)) return;
    state.selectedPath = path;
    state.current = null;
    renderList();
    editorEmpty.hidden = false; editorMain.hidden = true;
    setPageState("reading prompt source…");
    try {
      const file = await api(
        `/api/registry/file?path=${encodeURIComponent(path)}`);
      const body = typeof file.body === "string" ? file.body : "";
      const name = path.split("/").pop() || path;
      state.current = {
        entry: {
          name,
          path,
          harness: "source",
          surface: "prompt",
          scope: "source",
          status: "source",
          size: new TextEncoder().encode(body).length,
        },
        format: file.format || "text",
        frontmatter: file.frontmatter || {},
        body,
        quotedKeys: file.quoted_keys || [],
        managed: false,
        source: null,
        readOnly: true,
        rawMode: false,
        rawBaseline: undefined,
        formSnapshot: null,
      };
      await renderEditor();
      setPageState("prompt source ready");
    } catch (error) {
      setPageState(error.message, true);
      editorEmpty.hidden = false; editorMain.hidden = true;
      editorEmpty.replaceChildren();
      const strong = document.createElement("strong");
      strong.textContent = "Unable to read prompt source";
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
    const managedLock = Boolean(
      state.current && (state.current.managed || state.current.readOnly)
    );
    [$("delete-file"), $("import-toggle"), $("new-skill"), $("import-file"),
      $("new-create"), $("regen-file")].forEach((button) => {
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
      state.agentNames = uniqueStrings(
        state.entries
          .filter((entry) => entry.surface === "agent")
          .map((entry) => String(entry.name || ""))
      );
      renderTree(); renderList();
      setPageState(`${state.entries.length} files · registry ready`);
      // Topology nodes deep-link here with ?path=<absolute file>.
      const linkedPath = new URLSearchParams(location.search).get("path");
      if (selectPath && state.entries.some((entry) => entry.path === selectPath)) {
        await selectEntry(selectPath);
      } else if (linkedPath && state.entries.some((entry) => entry.path === linkedPath)) {
        await selectEntry(linkedPath);
      } else if (linkedPath && isPromptPath(linkedPath)) {
        await selectSource(linkedPath);
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

  function destinationsForScope(scope) {
    return scope === "project" ? PROJECT_DESTINATIONS : schema.destinations;
  }

  function destinationOptions(
    select, preferredHarness = "claude", destinations = schema.destinations
  ) {
    select.replaceChildren();
    const harnesses = Object.keys(destinations || {});
    const selectedHarness = harnesses.includes(preferredHarness)
      ? preferredHarness : harnesses[0];
    harnesses.forEach((harness) => {
      const option = document.createElement("option");
      option.value = harness;
      option.textContent = displayName(harness);
      option.selected = harness === selectedHarness;
      select.append(option);
    });
  }

  function updateSurfaceSelect(
    select, harness, preferred = "skill", destinations = schema.destinations
  ) {
    select.replaceChildren();
    const surfaces = (destinations && destinations[harness]) || [];
    const selectedSurface = surfaces.includes(preferred)
      ? preferred : surfaces[0];
    surfaces.forEach((surface) => {
      const option = document.createElement("option");
      option.value = surface;
      option.textContent = displayName(surface);
      option.selected = surface === selectedSurface;
      select.append(option);
    });
  }

  function updateImportSurfaces() {
    const harness = $("import-harness").value || "claude";
    const preferred = (schema.destinations[harness] || []).includes($("import-surface").value) ? $("import-surface").value : "skill";
    updateSurfaceSelect($("import-surface"), harness, preferred);
  }

  function refreshDestinationSelects() {
    destinationOptions($("import-harness"));
    updateSurfaceSelect($("import-surface"), $("import-harness").value, "skill");
    refreshNewDestinationSelects(true);
  }

  function refreshNewDestinationSelects(resetScope = false) {
    const scope = $("new-scope");
    if (!scope) return;
    if (resetScope) scope.value = currentRoot ? "project" : "global";
    const destinations = destinationsForScope(scope.value);
    const harness = $("new-harness").value;
    destinationOptions($("new-harness"), harness, destinations);
    updateSurfaceSelect(
      $("new-surface"),
      $("new-harness").value,
      $("new-surface").value || "skill",
      destinations,
    );
    renderNewDestination();
  }

  function renderNewDestination() {
    const list = $("new-destinations");
    if (!list) return;
    list.replaceChildren();
    const item = document.createElement("li");
    const scope = $("new-scope").value;
    const harness = $("new-harness").value;
    const surface = $("new-surface").value;
    const name = $("new-name").value.trim();
    const destinations = destinationsForScope(scope);
    if (!name) {
      item.textContent = "Enter a name.";
    } else if (scope === "project" && !currentRoot) {
      item.textContent = "Choose a workspace for project scope.";
    } else if (
      !destinations[harness] || !destinations[harness].includes(surface)
    ) {
      item.textContent = "Selected destination is unavailable in this scope.";
    } else {
      const path = registryDestinationPath(
        scope, harness, surface, name, currentRoot, schema.formats);
      item.textContent = `${harness}:${surface} · ${path}`;
    }
    list.append(item);
  }

  async function saveFile() {
    if (
      !state.current || state.busy ||
      state.current.managed || state.current.readOnly
    ) return;
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
          body: JSON.stringify({
            format: cur.format,
            frontmatter,
            body: bodyText.value,
            harness: cur.entry.harness,
            surface: cur.entry.surface,
            path,
            quoted_keys: cur.quotedKeys || []
          })
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

  async function regenerateFile() {
    const cur = state.current;
    if (!cur || state.busy || !cur.managed || !cur.source) return;
    if (!window.confirm(
      "Regenerate all prompt outputs and re-install this harness?")) return;
    setBusy(true); setEditorMessage("Regenerating…");
    try {
      const payload = { path: cur.entry.path };
      if (currentRoot) payload.root = currentRoot;
      const result = await api("/api/registry/regenerate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await refresh(cur.entry.path);
      const count = Array.isArray(result.wrote) ? result.wrote.length : 0;
      setEditorMessage(
        `Regenerated ${count} file${count === 1 ? "" : "s"} and re-installed`,
        "ok"
      );
    } catch (error) {
      setEditorMessage(error.message, "error");
      setPageState(error.message, true);
    } finally {
      setBusy(false);
    }
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
    const scope = $("new-scope").value;
    const harness = $("new-harness").value;
    const surface = $("new-surface").value;
    const destinations = destinationsForScope(scope);
    if (
      !destinations[harness] || !destinations[harness].includes(surface)
    ) {
      setPageState("Selected destination is unavailable in this scope.", true);
      return;
    }
    if (scope === "project" && !currentRoot) {
      setPageState("Choose a workspace for project scope.", true);
      return;
    }
    const payload = {
      harness,
      surface,
      name,
      content: "",
      scope,
    };
    if (scope === "project") payload.project = currentRoot;
    setBusy(true);
    try {
      const result = await api("/api/registry/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      $("new-dialog").close();
      $("new-name").value = "";
      await refresh(result && result.path ? result.path : null);
      if (!result || !result.path) {
        const created = state.entries.find((entry) =>
          entry.name === name && entry.harness === harness &&
          entry.surface === surface);
        if (created) await selectEntry(created.path);
      }
    } catch (error) { setPageState(error.message, true); }
    finally { setBusy(false); }
  }

  function openNewDialog() {
    $("new-scope").value = currentRoot ? "project" : "global";
    refreshNewDestinationSelects();
    $("new-dialog").showModal();
    $("new-name").focus();
  }

  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path ? event.detail.path : "";
    state.modelChoices = {};
    state.catalogRoot = null;
    state.catalogKey = null;
    state.catalogPromise = null;
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
  $("frontmatter-form").addEventListener("input", (event) => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
    const fc = state.formControls.find((item) => item.control === event.target);
    if (fc && fc.widget === "json-schema") updateJsonSchemaError(fc);
  });
  $("frontmatter-raw-text").addEventListener("input", () => {
    if (state.current) setEditorMessage("Unsaved changes", "error");
  });
  $("raw-toggle").addEventListener("change", onRawToggle);
  $("save-file").addEventListener("click", saveFile);
  $("regen-file").addEventListener("click", regenerateFile);
  $("delete-file").addEventListener("click", deleteFile);
  $("import-toggle").addEventListener("click", () => { $("import-panel").hidden = !$("import-panel").hidden; });
  $("import-cancel").addEventListener("click", () => { $("import-panel").hidden = true; });
  $("import-file").addEventListener("click", importFile);
  $("import-harness").addEventListener("change", updateImportSurfaces);
  $("new-scope").addEventListener("change", () => refreshNewDestinationSelects());
  $("new-harness").addEventListener("change", () => {
    const destinations = destinationsForScope($("new-scope").value);
    updateSurfaceSelect(
      $("new-surface"),
      $("new-harness").value,
      "skill",
      destinations,
    );
    renderNewDestination();
  });
  $("new-surface").addEventListener("change", renderNewDestination);
  $("new-name").addEventListener("input", renderNewDestination);
  $("new-skill").addEventListener("click", openNewDialog);
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
}
