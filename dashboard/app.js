"use strict";

/* Trio Loop Dashboard — read-only frontend.
 * No build step, no external dependencies. Talks to the backend at
 * /api/board, /api/sessions and /api/transcript (SSE). */

/* All state lives in module scope. */
const state = {
  boardTimer: null,
  loops: [],
  updatedAt: null,

  /* transcript pane */
  activeLoop: null,   /* loop whose pane is open */
  sessions: [],
  activePath: null,   /* session file currently being streamed */
  es: null,           /* active EventSource */
  offset: 0,
  size: null,
  follow: true,
  pendingLines: [],
  rafPending: false,
};

const BOARD_POLL_MS = 5000;

/* ------------------------------ helpers ------------------------------ */

function el(id) {
  return document.getElementById(id);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function cssClass(value) {
  return String(value).replace(/[^a-z0-9]+/gi, "-").toLowerCase();
}

function span(cls, text) {
  const node = document.createElement("span");
  node.className = cls;
  node.textContent = text;
  return node;
}

function oneLine(text, max = 160) {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  if (s.length > max) return s.slice(0, max - 1).trimEnd() + "\u2026";
  return s;
}

function fmtClock(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function relTime(iso) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 5) return "just now";
  if (secs < 60) return secs + "s ago";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.floor(hrs / 24);
  if (days < 7) return days + "d ago";
  const d = new Date(t);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(1) + "KB";
  return (n / 1048576).toFixed(1) + "MB";
}

/* Map arbitrary STATE.md status text onto the four badge buckets. */
function normStatus(raw) {
  const s = String(raw ?? "").trim().toLowerCase();
  if (!s) return "unknown";
  if (/(running|active|iterat|in progress|working|pending|queued)/.test(s)) {
    return "running";
  }
  if (/(complet|done|finish|ship|passed|succeed|closed)/.test(s)) {
    return "completed";
  }
  if (/(block|fail|error|stuck|abort|halt)/.test(s)) return "blocked";
  return "unknown";
}

/* Map a VERDICT.md verdict onto SHIP/ITERATE/BLOCKED/none. */
function normVerdict(raw) {
  if (!raw) return "none";
  const v = String(raw).trim().toUpperCase();
  if (v === "SHIP") return "ship";
  if (v === "ITERATE") return "iterate";
  if (v === "BLOCKED") return "blocked";
  return "none";
}

/* ------------------------------ board ------------------------------ */

async function refreshBoard() {
  try {
    const res = await fetch("/api/board", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    state.loops = Array.isArray(data.loops) ? data.loops : [];
    state.updatedAt = data.updated_at || null;
    setBoardStatus("live", "live");
    hideBoardError();
    renderBoard();
    updateUpdatedAt();
  } catch (err) {
    setBoardStatus("offline", "offline");
    showBoardError(
      "Cannot reach /api/board (" + err.message + "). Retrying every " +
      BOARD_POLL_MS / 1000 + "s."
    );
  } finally {
    state.boardTimer = setTimeout(refreshBoard, BOARD_POLL_MS);
  }
}

function setBoardStatus(cls, text) {
  el("board-status").className = "board-status " + cls;
  el("board-status-text").textContent = text;
}

function showBoardError(msg) {
  const node = el("board-error");
  node.hidden = false;
  node.textContent = msg;
}

function hideBoardError() {
  el("board-error").hidden = true;
}

function updateUpdatedAt() {
  const node = el("updated-at");
  if (!state.updatedAt) {
    node.textContent = "Updated \u2014";
    node.title = "";
    return;
  }
  node.textContent =
    "Updated " + fmtClock(state.updatedAt) + " (" + relTime(state.updatedAt) + ")";
  node.title = new Date(state.updatedAt).toLocaleString();
}

function renderBoard() {
  const grid = el("card-grid");
  grid.textContent = "";
  if (!state.loops.length) {
    const empty = document.createElement("div");
    empty.className = "board-empty";
    empty.textContent = "No loops found.";
    grid.appendChild(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const loop of state.loops) frag.appendChild(cardEl(loop));
  grid.appendChild(frag);
}

function cardEl(loop) {
  const card = document.createElement("article");
  card.className = "loop-card";
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.dataset.loop = loop.name;
  if (state.activeLoop === loop.name) card.classList.add("active");

  const status = normStatus(loop.status);
  const statusText = loop.status ? String(loop.status) : "unknown";
  const verdict = normVerdict(loop.final_verdict);
  const verdictText = loop.final_verdict
    ? String(loop.final_verdict).toUpperCase()
    : "none";
  const mission = loop.mission ? String(loop.mission) : "";
  const iter = loop.iteration ?? "?";
  const maxIt = loop.max_iterations ?? "?";
  const summary = loop.last_entry_summary
    ? String(loop.last_entry_summary)
    : "no activity";
  const activityAbs = loop.last_activity
    ? new Date(loop.last_activity).toLocaleString()
    : "";

  card.innerHTML =
    '<div class="card-top">' +
      '<h3 class="card-name">' + esc(loop.name) + "</h3>" +
      '<span class="badge status-' + status + '" title="Status: ' +
        esc(statusText) + '">' + esc(statusText) + "</span>" +
    "</div>" +
    '<p class="card-mission" title="' + escAttr(mission) + '">' +
      (mission ? esc(mission) : "\u2014") +
    "</p>" +
    '<div class="card-meta">' +
      '<span class="badge verdict-badge verdict-' + verdict + '">' +
        esc(verdictText) + "</span>" +
      '<span class="card-iter">iter ' + esc(iter) + " / " + esc(maxIt) + "</span>" +
    "</div>" +
    '<div class="card-foot">' +
      '<span class="card-activity"' +
        (activityAbs ? ' title="' + escAttr(activityAbs) + '"' : "") + ">" +
        relTime(loop.last_activity) + "</span>" +
      '<span class="card-summary" title="' + escAttr(summary) + '">' +
        esc(summary) + "</span>" +
    "</div>";

  card.addEventListener("click", () => openTranscript(loop.name));
  card.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openTranscript(loop.name);
    }
  });
  return card;
}

function escAttr(value) {
  return esc(value).replace(/`/g, "&#96;");
}

function markActiveCard() {
  for (const card of document.querySelectorAll(".loop-card")) {
    card.classList.toggle("active", card.dataset.loop === state.activeLoop);
  }
}

/* --------------------------- transcript pane --------------------------- */

async function openTranscript(name) {
  /* Already showing this loop — leave the stream alone. */
  if (!el("transcript-pane").hidden && state.activeLoop === name) return;

  closeStream();
  state.activeLoop = name;
  state.sessions = [];
  state.activePath = null;
  state.offset = 0;
  state.size = null;
  state.follow = true;
  updateFollowBtn();

  el("pane-loop-name").textContent = name;
  el("layout").classList.add("with-pane");
  el("transcript-pane").hidden = false;
  setPaneStatus("idle", "loading sessions\u2026");
  showTranscriptNotice("Loading sessions\u2026");
  renderTranscript();
  renderSessionList();
  markActiveCard();

  try {
    const res = await fetch("/api/sessions?loop=" + encodeURIComponent(name), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const sessions = await res.json();
    state.sessions = Array.isArray(sessions) ? sessions : [];
    renderSessionList();
    if (state.sessions.length) {
      showTranscriptNotice("Select a session to stream its transcript.");
      setPaneStatus(
        "idle",
        state.sessions.length + " session" + (state.sessions.length === 1 ? "" : "s")
      );
    } else {
      showTranscriptNotice("No matching session found.");
      setPaneStatus("idle", "no sessions");
    }
  } catch (err) {
    showTranscriptNotice("Failed to load sessions: " + err.message);
    setPaneStatus("error", "load failed");
  }
}

function closeTranscript() {
  closeStream();
  state.activeLoop = null;
  state.activePath = null;
  state.sessions = [];
  state.pendingLines = [];
  el("transcript-pane").hidden = true;
  el("layout").classList.remove("with-pane");
  markActiveCard();
}

function renderSessionList() {
  const list = el("session-list");
  list.textContent = "";
  if (!state.sessions.length) return;

  /* No subagents — keep the flat top-level-only rendering. */
  if (!state.sessions.some((s) => s.kind === "subagent")) {
    const frag = document.createDocumentFragment();
    for (const s of state.sessions) frag.appendChild(sessionItem(s));
    list.appendChild(frag);
    return;
  }

  /* Grouped rendering: parents are group headers; subagents nest under
   * the session whose path matches their parent_path (recursively). */
  const byPath = new Map(state.sessions.map((s) => [s.path, s]));
  const childrenOf = new Map();
  const roots = [];
  for (const s of state.sessions) {
    if (s.kind !== "subagent") {
      roots.push(s);
      continue;
    }
    const parentPath =
      s.parent_path && byPath.has(s.parent_path) ? s.parent_path : null;
    if (parentPath) {
      let kids = childrenOf.get(parentPath);
      if (!kids) {
        kids = [];
        childrenOf.set(parentPath, kids);
      }
      kids.push(s);
    } else {
      roots.push(s); /* orphan — no matching parent session in the list */
    }
  }

  const frag = document.createDocumentFragment();
  const visit = (s, depth) => {
    frag.appendChild(sessionItem(s, depth, descendantCount(s.path)));
    for (const child of childrenOf.get(s.path) || []) {
      visit(child, depth + 1);
    }
  };
  for (const root of roots) visit(root, 0);
  list.appendChild(frag);

  function descendantCount(path) {
    let n = 0;
    for (const c of childrenOf.get(path) || []) {
      n += 1 + descendantCount(c.path);
    }
    return n;
  }
}

function sessionItem(s, depth = 0, subCount = 0) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "session-item";
  if (state.activePath === s.path) item.classList.add("active");

  const isSub = s.kind === "subagent";
  if (isSub) {
    item.classList.add("session-subagent");
    item.style.paddingLeft = 28 + Math.max(0, depth - 1) * 14 + "px";
  } else if (subCount > 0) {
    item.classList.add("session-parent");
  }

  const label = s.label ? String(s.label).replace(/\.jsonl$/, "") : s.id;
  const meta = [relTime(s.timestamp), fmtSize(s.size)];
  if (isSub) {
    meta.push("sub");
  } else if (subCount > 0) {
    meta.push(subCount + " sub" + (subCount === 1 ? "" : "s"));
  }

  item.innerHTML =
    '<span class="session-label" title="' + escAttr(s.label ?? "") + '">' +
      esc(label) + "</span>" +
    '<span class="session-meta">' + meta.join(" \u00b7 ") + "</span>";
  item.addEventListener("click", () => openSession(s.path));
  return item;
}

function openSession(path) {
  if (state.activePath === path && state.es) return; /* already streaming */

  closeStream();
  state.activePath = path;
  state.offset = 0;
  state.size = null;
  state.follow = true;
  updateFollowBtn();
  setOffsetHint();
  renderTranscript();
  renderSessionList();
  setPaneStatus("connecting", "connecting\u2026");
  showTranscriptNotice("Waiting for stream\u2026");

  const es = new EventSource(
    "/api/transcript?path=" + encodeURIComponent(path) + "&offset=0"
  );
  state.es = es;

  es.addEventListener("open", () => {
    if (state.es === es) setPaneStatus("connected", "live");
  });

  es.addEventListener("init", (ev) => {
    if (state.es !== es) return;
    try {
      const data = JSON.parse(ev.data);
      state.offset = Number(data.offset) || 0;
      state.size = data.size != null ? Number(data.size) : null;
      setOffsetHint();
    } catch { /* malformed init — ignore */ }
  });

  es.addEventListener("line", (ev) => {
    if (state.es !== es) return;
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch { /* malformed line — skip */ return; }
    state.offset = Number(data.offset) || state.offset;
    if (data.record) {
      queueLine(data.record);
    }
    setOffsetHint();
  });

  es.addEventListener("error", (ev) => {
    if (state.es !== es) return;
    if (typeof ev.data === "string" && ev.data) {
      /* Backend-sent `event: error` payload. */
      let msg = "stream error";
      try {
        msg = JSON.parse(ev.data).error || msg;
      } catch { msg = ev.data; }
      setPaneStatus("error", "error");
      showTranscriptNotice("Stream error: " + esc(msg));
      es.close();
      if (state.es === es) state.es = null;
      return;
    }
    /* Network-level failure; EventSource auto-reconnects. */
    if (es.readyState === EventSource.CLOSED) {
      setPaneStatus("disconnected", "disconnected");
      if (state.es === es) state.es = null;
    } else {
      setPaneStatus("connecting", "reconnecting\u2026");
    }
  });
}

function closeStream() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
}

/* --------------------------- transcript render --------------------------- */

function queueLine(record) {
  state.pendingLines.push(record);
  if (!state.rafPending) {
    state.rafPending = true;
    requestAnimationFrame(flushLines);
  }
}

function flushLines() {
  state.rafPending = false;
  const lines = state.pendingLines;
  state.pendingLines = [];
  if (!lines.length) return;
  const view = el("transcript-view");
  for (const rec of lines) appendLine(view, rec);
  if (state.follow) view.scrollTop = view.scrollHeight;
}

function appendLine(view, rec) {
  hideTranscriptNotice();
  const line = document.createElement("div");
  line.className = "tr-line";

  const time = fmtClock(rec && rec.timestamp);
  if (time) line.appendChild(span("tr-time", time));

  const type = rec && typeof rec.type === "string" ? rec.type : "?";
  line.appendChild(span("tr-type tr-type-" + cssClass(type), type));

  const preview = recordPreview(rec);
  if (preview.role) {
    line.appendChild(
      span("tr-role tr-role-" + cssClass(preview.role), preview.role)
    );
  }
  const text = span("tr-text", preview.text);
  if (preview.text.length > 40) text.title = preview.text;
  line.appendChild(text);

  view.appendChild(line);
}

/* Human preview for a transcript record. */
function recordPreview(rec) {
  if (!rec || typeof rec !== "object") {
    return { role: null, text: "?" };
  }
  switch (rec.type) {
    case "message": {
      const msg = rec.message || {};
      const role = msg.role || "?";
      const parts = Array.isArray(msg.content) ? msg.content : [];
      const textPart = parts.find(
        (p) => p && p.type === "text" && typeof p.text === "string" && p.text.trim()
      );
      if (textPart) return { role, text: oneLine(textPart.text) };
      const toolUse = parts.find((p) => p && p.type === "tool_use");
      if (toolUse) {
        return { role, text: oneLine("tool_use " + (toolUse.name || "")) };
      }
      const other = parts.find((p) => p && typeof p.type === "string");
      if (other) return { role, text: "<" + other.type + ">" };
      return { role, text: "(empty)" };
    }
    case "custom":
      return {
        role: null,
        text: oneLine(rec.customType || "custom"),
      };
    case "tool": {
      const name =
        (rec.tool && (rec.tool.name || rec.name)) || rec.name || "";
      return { role: null, text: oneLine("tool " + name).trim() };
    }
    default: {
      for (const key of ["title", "name", "model", "subtype"]) {
        if (rec[key] != null && rec[key] !== "") {
          return { role: null, text: oneLine(String(rec[key])) };
        }
      }
      return { role: null, text: rec.type || "?" };
    }
  }
}

function renderTranscript() {
  el("transcript-view").textContent = "";
}

function showTranscriptNotice(text) {
  const node = el("transcript-notice");
  node.hidden = false;
  node.textContent = text;
}

function hideTranscriptNotice() {
  el("transcript-notice").hidden = true;
}

function setPaneStatus(key, text) {
  el("pane-status").className = "pane-status status-" + key;
  el("pane-status-text").textContent = text;
}

function updateFollowBtn() {
  const btn = el("follow-btn");
  btn.textContent = state.follow ? "Pause follow" : "Resume follow";
  btn.classList.toggle("paused", !state.follow);
  btn.setAttribute("aria-pressed", String(state.follow));
  if (state.follow && state.activePath) {
    el("transcript-view").scrollTop = el("transcript-view").scrollHeight;
  }
}

function setOffsetHint() {
  const parts = ["offset " + state.offset];
  if (state.size != null) parts.push("size " + fmtSize(state.size));
  el("offset-hint").textContent = parts.join(" \u00b7 ");
}

/* ------------------------------- boot ------------------------------- */

function init() {
  el("pane-close").addEventListener("click", closeTranscript);
  el("follow-btn").addEventListener("click", () => {
    state.follow = !state.follow;
    updateFollowBtn();
  });

  el("card-grid").innerHTML =
    '<div class="board-empty">Loading loops\u2026</div>';
  refreshBoard();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
