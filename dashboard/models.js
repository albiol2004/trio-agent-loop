(() => {
  "use strict";

  let currentRoot = "";
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
      const input = createElement("input", "", "model-input");
      input.type = "text";
      input.value = row.model || "";
      input.setAttribute("aria-label", `${row.agent} model`);
      const save = createElement("button", "Save", "model-save");
      save.type = "button";
      save.addEventListener("click", () => saveModel(row, input, save));
      cell.append(input, save);
    } else {
      cell.append(createElement("span", row.model || "—"));
      if (row.override_file) {
        cell.append(createElement(
          "span", `edit in ${row.override_file}`, "model-detail"));
      }
    }
    if (row.warning) {
      cell.append(createElement("span", row.warning, "model-warning"));
    }
  }

  function render(data) {
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
      const value = input.value.trim();
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
