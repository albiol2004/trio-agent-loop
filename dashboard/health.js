(() => {
  "use strict";

  let currentRoot = "";
  const pageState = document.getElementById("page-state");
  const lineage = document.getElementById("health-lineage");
  const manifests = document.getElementById("health-manifests");
  const installed = document.getElementById("health-installed");
  const generate = document.getElementById("health-generate");
  const dangling = document.getElementById("health-dangling");

  function withRoot(url) {
    return currentRoot
      ? `${url}${url.includes("?") ? "&" : "?"}root=${
        encodeURIComponent(currentRoot)}`
      : url;
  }

  // Building every value as a text node keeps paths and harness data inert.
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

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const raw = await response.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = raw;
      }
    }
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.error : data;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }

  function requestForRoot(url, options = {}) {
    return request(withRoot(url), options);
  }

  function statusBadge(status) {
    const value = String(status || "unknown");
    const className = value.replace(/[^a-z0-9_-]/gi, "-");
    return createElement("span", value, `status-badge status-${className}`);
  }

  function emptyMessage(container, text) {
    container.replaceChildren(createElement("p", text, "health-empty"));
  }

  function renderLineage(rows) {
    lineage.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      emptyMessage(lineage, "No registry lineage found.");
      return;
    }
    rows.forEach((row) => {
      const item = createElement("article", "", "health-item");
      item.append(createElement("h3", row.name || "unnamed", "health-name"));
      const hops = createElement("ol", "", "health-hops");
      (Array.isArray(row.hops) ? row.hops : []).forEach((hop) => {
        const line = createElement("li", "", "health-hop");
        const role = String(hop.role || "").replaceAll("_", " ");
        line.append(
          createElement("span", role, "health-role"),
          createElement("span", hop.path || "—", "health-path"),
          statusBadge(hop.status),
        );
        if (hop.harness) {
          line.append(createElement("span", hop.harness, "health-role"));
        }
        hops.append(line);
      });
      item.append(hops);
      lineage.append(item);
    });
  }

  function renderManifests(rows) {
    manifests.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      emptyMessage(manifests, "No explicit home directory supplied.");
      return;
    }
    rows.forEach((row) => {
      const item = createElement("article", "", "health-item");
      const heading = createElement("div", "", "health-name");
      heading.append(
        createElement("strong", row.harness || "unknown"),
        statusBadge(row.status),
      );
      item.append(heading, createElement("div", row.directory, "health-path"));
      (Array.isArray(row.entries) ? row.entries : []).forEach((entry) => {
        const detail = createElement("div", "", "health-hop");
        detail.append(
          createElement("span", entry.file, "health-path"),
          statusBadge(entry.status),
        );
        item.append(detail);
      });
      manifests.append(item);
    });
  }

  function renderInstalled(rows) {
    installed.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      emptyMessage(installed, "No explicit home directory supplied.");
      return;
    }
    rows.forEach((row) => {
      const item = createElement("div", "", "health-item");
      item.append(
        createElement("strong", row.harness || "unknown", "health-name"),
        createElement("div", row.directory, "health-path"),
        statusBadge(row.installed ? "installed" : "missing"),
      );
      installed.append(item);
    });
  }

  function renderGenerate(check) {
    generate.replaceChildren();
    const result = check || {};
    const state = result.timed_out
      ? "stale"
      : result.exit_code === 0 ? "ok" : "drift";
    const summary = createElement("div", "", "health-name");
    summary.append(
      createElement("span", result.command || "generator check"),
      statusBadge(result.timed_out ? "timed-out" : state),
    );
    const output = [result.stdout, result.stderr]
      .filter((value) => value)
      .join("\n")
      .trim() || "no output";
    generate.append(
      summary,
      createElement("div", `exit code: ${
        result.exit_code == null ? "—" : result.exit_code}`, "health-path"),
      createElement("pre", output),
    );
  }

  async function deleteDangling(item, button) {
    button.disabled = true;
    setPageState(`deleting ${item.path}…`);
    try {
      await requestForRoot(
        `/api/registry/file?path=${encodeURIComponent(item.path)}`,
        {method: "DELETE"},
      );
      await loadHealth();
    } catch (error) {
      button.disabled = false;
      setPageState(error.message || "delete failed", true);
    }
  }

  function renderDangling(rows) {
    dangling.replaceChildren();
    if (!Array.isArray(rows) || !rows.length) {
      emptyMessage(dangling, "No dangling artifacts found.");
      return;
    }
    rows.forEach((row) => {
      const item = createElement("div", "", "health-dangling-item");
      item.append(createElement(
        "span", `${row.kind || "artifact"} · ${row.path}`,
        "health-dangling-path",
      ));
      if (row.deletable && row.kind === "orphan-file") {
        const button = createElement("button", "Delete", "health-delete");
        button.type = "button";
        button.addEventListener("click", () => deleteDangling(row, button));
        item.append(button);
      }
      dangling.append(item);
    });
  }

  function render(data) {
    renderLineage(data && data.lineage);
    renderManifests(data && data.manifests);
    renderInstalled(data && data.installed_harnesses);
    renderGenerate(data && data.generate_check);
    renderDangling(data && data.dangling);
    setPageState("health loaded");
  }

  async function loadHealth() {
    if (!currentRoot) {
      setPageState("waiting for workspace");
      return;
    }
    setPageState("scanning registry health…");
    try {
      const data = await requestForRoot("/api/registry/health");
      if (!data || typeof data !== "object") {
        throw new Error("health response is invalid");
      }
      render(data);
    } catch (error) {
      setPageState(error.message || "could not load health", true);
    }
  }

  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path
      ? event.detail.path
      : "";
    loadHealth();
  });
})();
