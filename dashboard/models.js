const CUSTOM_VALUE = "__trio_custom__";

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

function choicesForHarness(available, harness) {
  const values = available && typeof available === "object"
    ? available[harness] : null;
  if (!Array.isArray(values)) return [];
  const seen = new Set();
  return values.filter((value) => {
    if (typeof value !== "string" || !value || seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { catalogChoiceState, choicesForHarness };
} else {
(() => {
  "use strict";

  let currentRoot = "";
  let available = {};
  const pageState = document.getElementById("page-state");
  const modelsTable = document.getElementById("models-table");
  const tableBody = modelsTable.querySelector("tbody");

  function withRoot(url) {
    return currentRoot
      ? `${url}${url.includes("?") ? "&" : "?"}root=${
        encodeURIComponent(currentRoot)}`
      : url;
  }

  // Centralizing element construction keeps model ids and paths as text nodes.
  function createElement(tag, text = "", className = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text == null ? "" : String(text);
    return element;
  }

  function effectiveModelValue(select) {
    const value = select.value === CUSTOM_VALUE && select.customControl
      ? select.customControl.value : select.value;
    return value || "";
  }

  function setPageState(text, error = false) {
    pageState.textContent = text;
    pageState.classList.toggle("is-error", error);
  }

  async function parseResponse(response) {
    const raw = await response.text();
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (_) {
      return raw;
    }
  }

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const data = await parseResponse(response);
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.error : data;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }

  async function requestForRoot(url, options = {}) {
    return request(withRoot(url), options);
  }

  function renderModelCell(cell, row) {
    if (row.editable && row.path) {
      const select = createElement("select", "", "model-input");
      const choices = choicesForHarness(available, row.harness);
      const choiceState = catalogChoiceState(row.model, choices);
      const current = choiceState.value;
      if (!current) {
        const empty = createElement("option", "—");
        empty.value = "";
        select.append(empty);
      }
      const visibleChoices = [...choices];
      if (choiceState.offList) visibleChoices.push(current);
      visibleChoices.forEach((choice) => {
        const option = createElement("option", choice);
        option.value = choice;
        select.append(option);
      });
      const customOption = createElement("option", "custom…");
      customOption.value = CUSTOM_VALUE;
      select.append(customOption);
      const customControl = createElement(
        "input", "", "model-input model-custom");
      customControl.type = "text";
      customControl.value = current;
      customControl.hidden = !choiceState.offList;
      customControl.disabled = !choiceState.offList;
      customControl.setAttribute("aria-label", `${row.agent} custom model`);
      select.value = choiceState.selected;
      select.customControl = customControl;
      select.setAttribute("aria-label", `${row.agent} model`);
      select.addEventListener("change", () => {
        const custom = select.value === CUSTOM_VALUE;
        customControl.hidden = !custom;
        customControl.disabled = !custom;
      });
      const save = createElement("button", "Save", "model-save");
      save.type = "button";
      save.addEventListener("click", () => saveModel(row, select, save));
      const controls = createElement("div", "", "model-cell-controls");
      controls.append(select, customControl, save);
      cell.append(controls);
    } else {
      cell.append(createElement("span", row.model || "—"));
      if (row.override_file) {
        const link = createElement(
          "a", `edit in ${row.override_file}`, "model-detail");
        link.href = `/skills.html?path=${
          encodeURIComponent(row.override_file)}`;
        cell.append(link);
      }
    }
    if (row.warning) {
      cell.append(createElement("span", row.warning, "model-warning"));
    }
  }

  function render(data) {
    available = data && data.available &&
      typeof data.available === "object" ? data.available : {};
    tableBody.replaceChildren();
    const rows = Array.isArray(data && data.rows) ? data.rows : [];
    rows.forEach((row) => {
      const tableRow = document.createElement("tr");
      tableRow.append(
        createElement("td", row.harness),
        createElement("td", row.agent),
      );
      const modelCell = document.createElement("td");
      renderModelCell(modelCell, row);
      tableRow.append(modelCell);
      tableRow.append(
        createElement("td", row.layer),
        createElement(
          "td",
          row.availability,
          row.availability === "known"
            ? "model-availability-known"
            : "model-availability-unknown",
        ),
      );
      const labels = ["Harness", "Agent", "Model", "Layer", "Availability"];
      [...tableRow.children].forEach((cell, index) => {
        cell.dataset.label = labels[index];
      });
      tableBody.append(tableRow);
    });
    setPageState(`${rows.length} model selections`);
  }

  async function saveModel(row, input, button) {
    button.disabled = true;
    setPageState(`saving ${row.agent}…`);
    try {
      // Fetch the full document so saving one model preserves its body and
      // every unrelated frontmatter key.
      const file = await requestForRoot(
        `/api/registry/file?path=${encodeURIComponent(row.path)}`);
      const frontmatter = file.frontmatter &&
        typeof file.frontmatter === "object"
        ? { ...file.frontmatter }
        : {};
      const value = effectiveModelValue(input).trim();
      if (value) {
        frontmatter.model = value;
      } else {
        delete frontmatter.model;
      }
      const serialized = await request("/api/registry/serialize", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          format: file.format,
          frontmatter,
          body: file.body,
          harness: row.harness,
          surface: "agent",
          path: row.path,
        }),
      });
      await requestForRoot("/api/registry/file", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: row.path, content: serialized.content}),
      });
      await loadModels();
    } catch (error) {
      setPageState(error.message || "save failed", true);
      button.disabled = false;
    }
  }

  async function loadModels() {
    if (!currentRoot) {
      setPageState("waiting for workspace");
      return;
    }
    setPageState("loading models…");
    try {
      render(await requestForRoot("/api/registry/models"));
    } catch (error) {
      setPageState(error.message || "could not load models", true);
      tableBody.replaceChildren();
    }
  }

  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path
      ? event.detail.path
      : "";
    loadModels();
  });
})();
}
