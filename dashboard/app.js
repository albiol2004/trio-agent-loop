"use strict";

/* Trio Loop Dashboard — read-only frontend.
 * No build step, no external dependencies. Talks to the backend at
 * /api/board, /api/loop and /api/transcript (SSE). */

const state = {
  boardTimer: null,
  loops: [],
  updatedAt: null,
  tab: "all",

  /* drawer */
  activeLoop: null,
  detail: null,

  /* transcript stream (inside the drawer's sessions section) */
  sessions: [],
  activePath: null,
  es: null,
  offset: 0,
  size: null,
  follow: true,
  pendingLines: [],
  rafPending: false,
};

const BOARD_POLL_MS = 5000;
const TABS = ["all", "running", "shipped", "blocked", "idle"];

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

function escAttr(value) {
  return esc(value).replace(/`/g, "&#96;");
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
  if (s.length > max) return s.slice(0, max - 1).trimEnd() + "…";
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

function fmtDuration(sec) {
  if (sec == null) return "—";
  sec = Number(sec);
  if (!Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return Math.round(sec) + "s";
  const mins = Math.floor(sec / 60);
  if (mins < 60) return mins + "m";
  const hrs = Math.floor(mins / 60);
  return hrs + "h " + (mins % 60) + "m";
}

/* Map arbitrary STATE.md status text onto buckets. */
function normStatus(raw) {
  const s = String(raw ?? "").trim().toLowerCase();
  if (!s) return "unknown";
  if (/(running|active|iterat|in progress|working|pending|queued|awaiting)/.test(s)) {
    return "running";
  }
  if (/(complet|done|finish|ship|passed|succeed|closed)/.test(s)) {
    return "completed";
  }
  if (/(block|fail|error|stuck|abort|halt)/.test(s)) return "blocked";
  return "unknown";
}

function normVerdict(raw) {
  if (!raw) return "none";
  const v = String(raw).trim().toUpperCase();
  if (v === "SHIP") return "ship";
  if (v === "ITERATE") return "iterate";
  if (v === "BLOCKED") return "blocked";
  return "none";
}

/* Single display state for a loop: running beats verdict, verdict beats rest. */
function loopState(loop) {
  const status = normStatus(loop.status);
  const verdict = normVerdict(loop.final_verdict);
  if (status === "running") return "running";
  if (status === "blocked" || verdict === "blocked") return "blocked";
  if (verdict === "ship") return "shipped";
  return "idle";
}

const STATE_LABEL = {
  running: "RUNNING",
  shipped: "SHIPPED",
  blocked: "BLOCKED",
  idle: "IDLE",
};

/* Verdict sequence from segments, e.g. ["iterate", "ship"]. */
function verdictSeq(loop) {
  const seq = (loop.segments || [])
    .map((s) => s.verdict_sequence || "")
    .join("");
  const out = [];
  for (const ch of seq) {
    if (ch === "S") out.push("ship");
    else if (ch === "I") out.push("iterate");
    else if (ch === "B") out.push("blocked");
  }
  return out;
}

/* Segmented verdict bar: one hairline segment per verdict. */
function segbarEl(seq, large) {
  const bar = document.createElement("div");
  bar.className = "segbar" + (large ? " segbar-large" : "");
  if (!seq.length) {
    const empty = document.createElement("div");
    empty.className = "segbar-empty";
    bar.appendChild(empty);
    return bar;
  }
  for (const v of seq) {
    bar.appendChild(span("seg seg-" + v, ""));
  }
  return bar;
}

/* Mono sequence legend: S→I→S with per-verdict coloring. */
function seqLegendEl(seq) {
  const legend = document.createElement("span");
  legend.className = "seg-seq";
  const letter = { ship: "S", iterate: "I", blocked: "B" };
  seq.forEach((v, i) => {
    if (i > 0) legend.appendChild(document.createTextNode("→"));
    legend.appendChild(span("seq-" + v, letter[v]));
  });
  return legend;
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
    renderTabs();
    renderBoard();
    if (state.activeLoop) refreshDetail({ quiet: true });
  } catch (err) {
    setBoardStatus("error", "offline");
    showBoardError("Cannot reach the dashboard server: " + err.message);
  }
  state.boardTimer = setTimeout(refreshBoard, BOARD_POLL_MS);
}

function setBoardStatus(cls, text) {
  el("board-status").className = "board-status status-" + cls;
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

function updateAggregates() {
  const total = state.loops.length;
  const active = state.loops.filter((l) => loopState(l) === "running").length;
  el("agg-loops").textContent = total + (total === 1 ? " loop" : " loops");
  el("agg-active").textContent = active + " active";
  el("agg-active").classList.toggle("is-active", active > 0);
  const at = state.updatedAt ? fmtClock(state.updatedAt) : null;
  el("updated-at").textContent = "updated " + (at || "—");
}

function renderTabs() {
  const nav = el("tabs");
  nav.textContent = "";
  for (const tab of TABS) {
    const count = tab === "all"
      ? state.loops.length
      : state.loops.filter((l) => loopState(l) === tab).length;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab";
    if (state.tab === tab) btn.classList.add("active");
    if (count === 0) btn.classList.add("tab-empty");
    btn.textContent = tab;
    btn.appendChild(span("tab-count", String(count)));
    btn.setAttribute("aria-pressed", String(state.tab === tab));
    btn.addEventListener("click", () => {
      state.tab = tab;
      history.replaceState(null, "", tab === "all" ? "#" : "#" + tab);
      renderTabs();
      renderBoard();
    });
    nav.appendChild(btn);
  }
}

function visibleLoops() {
  const rank = { running: 0, blocked: 1, idle: 2, shipped: 3 };
  return state.loops
    .filter((l) => state.tab === "all" || loopState(l) === state.tab)
    .sort((a, b) => {
      const r = rank[loopState(a)] - rank[loopState(b)];
      if (r !== 0) return r;
      return String(b.last_activity || "").localeCompare(String(a.last_activity || ""));
    });
}

function renderBoard() {
  updateAggregates();
  const grid = el("card-grid");
  grid.textContent = "";
  if (!state.loops.length) {
    const empty = document.createElement("div");
    empty.className = "board-empty";
    empty.textContent = "No loop mailboxes found. Start one with /trio-init in this project.";
    grid.appendChild(empty);
    return;
  }
  const loops = visibleLoops();
  if (!loops.length) {
    const empty = document.createElement("div");
    empty.className = "board-empty";
    empty.textContent = "No " + state.tab + " loops.";
    grid.appendChild(empty);
    return;
  }
  loops.forEach((loop, i) => {
    const card = cardEl(loop);
    card.style.animationDelay = Math.min(i * 40, 320) + "ms";
    grid.appendChild(card);
  });
}

function cardEl(loop) {
  const display = loopState(loop);
  const card = document.createElement("button");
  card.type = "button";
  card.className = "loop-card";
  card.dataset.loop = loop.name;
  if (state.activeLoop === loop.name) card.classList.add("active");
  card.setAttribute("aria-label", "Open detail for " + loop.name);

  const top = document.createElement("div");
  top.className = "card-top";
  top.appendChild(span("status-label status-" + display, STATE_LABEL[display]));
  const iter = document.createElement("span");
  iter.className = "card-iter";
  const cur = loop.iteration != null ? loop.iteration : "–";
  const max = loop.max_iterations != null ? loop.max_iterations : "–";
  iter.innerHTML = "<strong>" + esc(cur) + "</strong> / " + esc(max) + " iter";
  top.appendChild(iter);
  card.appendChild(top);

  const name = document.createElement("h3");
  name.className = "card-name";
  name.textContent = loop.name;
  card.appendChild(name);

  if (loop.mission) {
    const mission = document.createElement("p");
    mission.className = "card-mission";
    mission.textContent = loop.mission;
    mission.title = loop.mission;
    card.appendChild(mission);
  }

  const bottom = document.createElement("div");
  bottom.className = "card-bottom";
  bottom.appendChild(segbarEl(verdictSeq(loop), false));
  bottom.appendChild(span("card-activity", relTime(loop.last_activity)));
  card.appendChild(bottom);

  card.addEventListener("click", () => openDrawer(loop.name));
  return card;
}

function markActiveCard() {
  for (const card of document.querySelectorAll(".loop-card")) {
    card.classList.toggle("active", card.dataset.loop === state.activeLoop);
  }
}

/* ------------------------------ drawer ------------------------------ */

async function openDrawer(name) {
  if (!el("drawer").hidden && state.activeLoop === name) return;
  closeStream();
  state.activeLoop = name;
  state.detail = null;
  state.sessions = [];
  state.activePath = null;

  el("drawer-name").textContent = name;
  el("drawer-badge").className = "status-label status-idle";
  el("drawer-badge").textContent = "LOADING";
  el("drawer-mission").textContent = "";
  el("fact-iter").textContent = "—";
  el("fact-verdict").textContent = "—";
  el("fact-verdict").className = "";
  el("fact-activity").textContent = "—";
  el("fact-sessions").textContent = "—";
  el("drawer-strip").textContent = "";
  el("drawer-timeline").innerHTML = '<div class="timeline-empty">Loading…</div>';
  el("session-list").textContent = "";
  el("sessions-details").open = false;
  setPaneStatus("idle", "loading…");
  renderTranscript();

  el("drawer").hidden = false;
  el("drawer-scrim").hidden = false;
  markActiveCard();
  await refreshDetail({ quiet: false });
}

function closeDrawer() {
  closeStream();
  state.activeLoop = null;
  state.detail = null;
  state.sessions = [];
  state.activePath = null;
  state.pendingLines = [];
  el("drawer").hidden = true;
  el("drawer-scrim").hidden = true;
  markActiveCard();
}

async function refreshDetail({ quiet }) {
  const name = state.activeLoop;
  if (!name) return;
  try {
    const res = await fetch("/api/loop?name=" + encodeURIComponent(name), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    if (state.activeLoop !== name) return;
    const detail = await res.json();
    state.detail = detail;
    state.sessions = Array.isArray(detail.sessions) ? detail.sessions : [];
    renderDetail(detail);
    if (!quiet) renderSessionList();
  } catch (err) {
    if (!quiet) {
      el("drawer-timeline").innerHTML =
        '<div class="timeline-empty">Failed to load detail: ' + esc(err.message) + "</div>";
    }
  }
}

function renderDetail(detail) {
  const display = loopState(detail);
  const badge = el("drawer-badge");
  badge.className = "status-label status-" + display;
  badge.textContent = STATE_LABEL[display];

  el("drawer-mission").textContent = detail.mission || "No mission recorded.";

  const cur = detail.iteration != null ? detail.iteration : "–";
  const max = detail.max_iterations != null ? detail.max_iterations : "–";
  el("fact-iter").textContent = cur + " of " + max;

  const verdict = detail.final_verdict ? String(detail.final_verdict).toUpperCase() : "—";
  const fv = el("fact-verdict");
  fv.textContent = verdict;
  fv.className = detail.final_verdict ? "verdict-" + normVerdict(detail.final_verdict) : "";

  el("fact-activity").textContent = relTime(detail.last_activity);
  el("fact-sessions").textContent = String(state.sessions.length);

  const strip = el("drawer-strip");
  strip.textContent = "";
  const seq = verdictSeq(detail);
  strip.appendChild(segbarEl(seq, true));
  strip.appendChild(
    seq.length ? seqLegendEl(seq) : span("seg-seq", "no verdicts yet")
  );

  renderTimeline(detail.timeline || []);

  if (!state.activePath) {
    setPaneStatus(
      "idle",
      state.sessions.length
        ? state.sessions.length + (state.sessions.length === 1 ? " session" : " sessions")
        : "no sessions"
    );
  }
}

function renderTimeline(entries) {
  const view = el("drawer-timeline");
  view.textContent = "";
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "timeline-empty";
    empty.textContent = "No log entries yet.";
    view.appendChild(empty);
    return;
  }
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "timeline-row";

    row.appendChild(span("role-chip role-" + cssClass(entry.role), entry.role || "?"));
    row.appendChild(span("timeline-dur", fmtDuration(entry.duration_sec)));

    const text = document.createElement("div");
    text.className = "timeline-text";
    let summary = entry.summary || "";
    if (entry.verdict) {
      text.appendChild(
        span("verdict-word verdict-" + entry.verdict.toLowerCase(), entry.verdict)
      );
      text.appendChild(document.createTextNode(" "));
      summary = summary.replace(/^VERDICT:\s*(SHIP|ITERATE|BLOCKED)\s*[—–-]\s*/i, "");
    }
    text.appendChild(span("timeline-dur timeline-iter-inline", "iter " + entry.iteration + " · "));
    text.appendChild(document.createTextNode(summary));
    row.appendChild(text);

    view.appendChild(row);
  }
}

/* --------------------------- sessions --------------------- */

function renderSessionList() {
  const list = el("session-list");
  list.textContent = "";
  if (!state.sessions.length) return;

  if (!state.sessions.some((s) => s.kind === "subagent")) {
    const frag = document.createDocumentFragment();
    for (const s of state.sessions) frag.appendChild(sessionItem(s));
    list.appendChild(frag);
    return;
  }

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
      roots.push(s);
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
    '<span class="session-meta">' + meta.join(" · ") + "</span>";
  item.addEventListener("click", () => openSession(s.path));
  return item;
}

function openSession(path) {
  if (state.activePath === path && state.es) return;

  closeStream();
  state.activePath = path;
  state.offset = 0;
  state.size = null;
  state.follow = true;
  updateFollowBtn();
  setOffsetHint();
  renderTranscript();
  renderSessionList();
  setPaneStatus("connecting", "connecting…");
  showTranscriptNotice("Waiting for stream…");

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
    } catch { return; }
    state.offset = Number(data.offset) || state.offset;
    if (data.record) {
      queueLine(data.record);
    }
    setOffsetHint();
  });

  es.addEventListener("error", (ev) => {
    if (state.es !== es) return;
    if (typeof ev.data === "string" && ev.data) {
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
    if (es.readyState === EventSource.CLOSED) {
      setPaneStatus("disconnected", "disconnected");
      if (state.es === es) state.es = null;
    } else {
      setPaneStatus("connecting", "reconnecting…");
    }
  });
}

function closeStream() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
}

/* --------------------------- transcript trace --------------------------- */

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
  for (const rec of lines) appendRecord(view, rec);
  if (state.follow) view.scrollTop = view.scrollHeight;
}

function timeEl(rec) {
  const t = fmtClock(rec && rec.timestamp);
  return t ? span("tr-time", t) : null;
}

function textOf(parts) {
  return (Array.isArray(parts) ? parts : [])
    .filter((p) => p && p.type === "text" && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n")
    .trim();
}

/* Dispatch one JSONL record into trace blocks. */
function appendRecord(view, rec) {
  if (!rec || typeof rec !== "object") return;
  hideTranscriptNotice();

  if (rec.type === "message") {
    const msg = rec.message || {};
    const role = msg.role;
    const parts = Array.isArray(msg.content) ? msg.content : [];

    if (role === "user") {
      const text = textOf(parts);
      if (text) view.appendChild(msgBlock(rec, "user", "user", text));
      return;
    }

    if (role === "assistant") {
      for (const part of parts) {
        if (!part || typeof part !== "object") continue;
        if (part.type === "text" && part.text && part.text.trim()) {
          view.appendChild(msgBlock(rec, "assistant", "assistant", part.text.trim()));
        } else if (part.type === "thinking" && part.thinking) {
          view.appendChild(thinkingBlock(rec, part.thinking));
        } else if (part.type === "toolCall") {
          view.appendChild(toolCallBlock(rec, part));
        }
      }
      return;
    }

    if (role === "toolResult") {
      view.appendChild(toolResultBlock(rec, msg));
      return;
    }

    metaRow(view, rec, role || "message");
    return;
  }

  if (rec.type === "custom") {
    /* tool_execution_start duplicates the assistant's toolCall block. */
    if (rec.customType === "tool_execution_start") return;
    metaRow(view, rec, String(rec.customType || "custom"));
    return;
  }

  if (rec.type === "custom_message") {
    const text = typeof rec.content === "string" ? oneLine(rec.content, 200) : "";
    metaRow(view, rec, String(rec.customType || "note"), text);
    return;
  }

  if (rec.type === "session") {
    metaRow(view, rec, "session", rec.title || rec.cwd || "");
    return;
  }
  if (rec.type === "title" || rec.type === "title_change") {
    metaRow(view, rec, "title", rec.title || "");
    return;
  }
  if (rec.type === "model_change") {
    metaRow(view, rec, "model", rec.model || "");
    return;
  }
  if (rec.type === "thinking_level_change") {
    metaRow(view, rec, "thinking level", rec.thinkingLevel || "");
    return;
  }
  if (rec.type === "compaction") {
    metaRow(view, rec, "compacted", oneLine(rec.summary || "", 120));
    return;
  }

  metaRow(view, rec, String(rec.type || "record"));
}

/* user / assistant text block */
function msgBlock(rec, cls, tag, text) {
  const block = document.createElement("div");
  block.className = "tr-msg tr-" + cls;

  const head = document.createElement("div");
  head.className = "tr-msg-head";
  head.appendChild(span("tr-tag", tag));
  const t = timeEl(rec);
  if (t) head.appendChild(t);
  block.appendChild(head);

  const body = document.createElement("p");
  body.className = "tr-msg-body";
  body.textContent = text;
  block.appendChild(body);
  return block;
}

/* thinking: collapsed details with one-line preview */
function thinkingBlock(rec, text) {
  const block = document.createElement("details");
  block.className = "tr-thinking";

  const summary = document.createElement("summary");
  summary.appendChild(span("tr-tag", "thinking"));
  summary.appendChild(span("tr-preview", oneLine(text, 90)));
  const t = timeEl(rec);
  if (t) summary.appendChild(t);
  block.appendChild(summary);

  const body = document.createElement("p");
  body.className = "tr-thinking-body";
  body.textContent = text;
  block.appendChild(body);
  return block;
}

/* tool call: name + intent, arguments behind the fold */
function toolCallBlock(rec, part) {
  const block = document.createElement("details");
  block.className = "tr-tool";

  const summary = document.createElement("summary");
  summary.appendChild(span("tr-tool-name", String(part.name || "tool")));
  if (part.intent) summary.appendChild(span("tr-tool-intent", oneLine(part.intent, 90)));
  const t = timeEl(rec);
  if (t) summary.appendChild(t);
  block.appendChild(summary);

  if (part.arguments && Object.keys(part.arguments).length) {
    const body = document.createElement("pre");
    body.className = "tr-tool-body";
    body.textContent = JSON.stringify(part.arguments, null, 2);
    block.appendChild(body);
  }
  return block;
}

/* tool result: indented under its call, errors in red */
function toolResultBlock(rec, msg) {
  const block = document.createElement("details");
  block.className = "tr-result" + (msg.isError ? " is-error" : "");

  const summary = document.createElement("summary");
  summary.appendChild(
    span("tr-tool-name", (msg.isError ? "error · " : "result · ") + String(msg.toolName || "tool"))
  );
  const text = textOf(msg.content);
  if (text) summary.appendChild(span("tr-tool-intent", oneLine(text, 90)));
  const hasImage = (Array.isArray(msg.content) ? msg.content : []).some(
    (p) => p && p.type === "image"
  );
  if (hasImage) summary.appendChild(span("tr-tool-intent", "[image]"));
  const t = timeEl(rec);
  if (t) summary.appendChild(t);
  block.appendChild(summary);

  if (text) {
    const body = document.createElement("pre");
    body.className = "tr-tool-body";
    body.textContent = text;
    block.appendChild(body);
  }
  return block;
}

/* quiet single-line meta row for session markers */
function metaRow(view, rec, label, text) {
  const row = document.createElement("div");
  row.className = "tr-meta";
  row.appendChild(span("tr-tag", label));
  if (text) row.appendChild(span("tr-meta-text", String(text)));
  const t = timeEl(rec);
  if (t) row.appendChild(t);
  view.appendChild(row);
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
  el("pane-status").className = "stream-status status-" + key;
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
  el("offset-hint").textContent = parts.join(" · ");
}

/* ------------------------------- boot ------------------------------- */

function init() {
  const hash = location.hash.replace(/^#/, "");
  if (TABS.includes(hash)) state.tab = hash;

  el("drawer-close").addEventListener("click", closeDrawer);
  el("drawer-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !el("drawer").hidden) closeDrawer();
  });
  el("follow-btn").addEventListener("click", () => {
    state.follow = !state.follow;
    updateFollowBtn();
  });

  el("card-grid").innerHTML =
    '<div class="board-empty">Loading loops…</div>';
  refreshBoard();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
