(() => {
  "use strict";

  const STORAGE_KEY = "trio.workspace";
  const meta = document.querySelector(".topbar-meta");
  if (!meta) return;
  const holder = document.createElement("label");
  holder.className = "workspace-nav";
  holder.style.cssText = "display:inline-flex;align-items:center;gap:6px;color:var(--muted);font: 12px var(--mono);";
  holder.textContent = "Workspace";
  const select = document.createElement("select");
  select.className = "field-control";
  select.style.cssText = "min-height:26px;padding:3px 7px;width:auto;max-width:260px;";
  select.setAttribute("aria-label", "Workspace");
  holder.append(select);
  meta.prepend(holder);

  function announce(path) {
    window.dispatchEvent(new CustomEvent("trio:workspace", { detail: { path } }));
  }
  function render(workspaces) {
    select.replaceChildren();
    workspaces.forEach((workspace) => {
      const option = document.createElement("option");
      option.value = workspace.path;
      const label = String(workspace.path || workspace.id || "").split("/").filter(Boolean).pop();
      option.textContent = (label || workspace.path) + (workspace.has_loop ? " (loop/)" : "");
      option.title = workspace.path;
      select.append(option);
    });
    if (!workspaces.length) {
      const option = document.createElement("option");
      option.textContent = "No workspaces";
      select.append(option);
      select.disabled = true;
      announce("");
      return;
    }
    const saved = localStorage.getItem(STORAGE_KEY);
    const chosen = workspaces.some((workspace) => workspace.path === saved) ? saved : workspaces[0].path;
    select.value = chosen;
    localStorage.setItem(STORAGE_KEY, chosen);
    announce(chosen);
  }
  select.addEventListener("change", () => {
    localStorage.setItem(STORAGE_KEY, select.value);
    announce(select.value);
  });
  fetch("/api/workspaces", { cache: "no-store" })
    .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
    .then((workspaces) => render(Array.isArray(workspaces) ? workspaces : []))
    .catch(() => render([]));
})();
