(() => {
  "use strict";

  const STORAGE_KEY = "trio.workspace";
  const meta = document.querySelector(".topbar-meta");
  if (!meta) return;
  const holder = document.createElement("label");
  holder.className = "workspace-nav";
  const caption = document.createElement("span");
  caption.className = "workspace-nav-label";
  caption.textContent = "Workspace";
  const select = document.createElement("select");
  select.className = "select";
  select.setAttribute("aria-label", "Workspace");
  holder.append(caption, select);
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
