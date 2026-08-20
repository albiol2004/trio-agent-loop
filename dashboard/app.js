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
  drawerTab: "overview",
  graphSel: null,
  compare: [],

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
const DRAWER_TABS = ["overview", "timeline", "files", "graph", "transcripts"];

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
  if (/(running|active|iterating|in progress|working)/.test(s)) {
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
  if (v === "NEEDS_HUMAN") return "needs_human";
  return "none";
}

/* Single display state for a loop: running beats verdict, verdict beats rest. */
function loopState(loop) {
  const status = normStatus(loop.status);
  const verdict = normVerdict(loop.final_verdict);
  // A terminal verdict always wins over a stale status word. Fall back to
  // the latest logged segment verdict when VERDICT.md is not yet written.
  const seg = verdictSeq(loop);
  const lastSeg = seg.length ? seg[seg.length - 1] : "none";
  const eff = verdict !== "none" ? verdict : lastSeg;
  if (eff === "ship") return "shipped";
  if (eff === "blocked") return "blocked";
  if (eff === "needs_human") return "needs_human";
  if (verdict === "ship") return "shipped";
  if (verdict === "blocked") return "blocked";
  if (verdict === "needs_human") return "needs_human";
  if (status === "blocked") return "blocked";
  // "running" only when the mailbox is actively moving (fresh activity).
  if (status === "running") {
    const last = loop.last_activity ? new Date(loop.last_activity).getTime() : NaN;
    if (Number.isFinite(last) && Date.now() - last < 2 * 60 * 1000) return "running";
    return "idle";
  }
  return "idle";
}

const STATE_LABEL = {
  running: "RUNNING",
  shipped: "SHIPPED",
  blocked: "BLOCKED",
  needs_human: "NEEDS HUMAN",
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
    else if (ch === "H") out.push("needs_human");
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

function renderInbox(inbox) {
  const section = el("inbox");
  const list = el("inbox-list");
  list.textContent = "";
  const items = Array.isArray(inbox) ? inbox : [];
  section.hidden = items.length === 0;
  if (!items.length) return;
  el("inbox-count").textContent = String(items.length);
  for (const item of items) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "inbox-item inbox-" + cssClass(item.severity || "low");
    row.appendChild(span("inbox-sev", (item.severity || "").toUpperCase()));
    const main = document.createElement("span");
    main.className = "inbox-main";
    const head = document.createElement("span");
    head.className = "inbox-headline";
    head.appendChild(span("inbox-loop", item.loop || "?"));
    head.appendChild(document.createTextNode(item.headline || ""));
    main.appendChild(head);
    if (item.detail) {
      const det = span("inbox-detail", item.detail);
      det.title = item.detail;
      main.appendChild(det);
    }
    row.appendChild(main);
    row.addEventListener("click", () => openDrawer(item.loop));
    list.appendChild(row);
  }
}

async function refreshBoard() {
  try {
    const res = await fetch("/api/board", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    state.loops = Array.isArray(data.loops) ? data.loops : [];
  renderInbox(data.inbox);
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
  state.compare = [];
  state.sessions = [];
  state.activePath = null;
  state.drawerTab = "overview";
  state.graphSel = null;

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
  setPaneStatus("idle", "loading…");
  renderTranscript();

  el("drawer").hidden = false;
  el("drawer-scrim").hidden = false;
  markActiveCard();
  renderDrawerTabs();
  showDrawerTab();
  await refreshDetail({ quiet: false });
}

function closeDrawer() {
  closeStream();
  state.activeLoop = null;
  state.detail = null;
  state.compare = [];
  state.sessions = [];
  state.activePath = null;
  state.pendingLines = [];
  state.drawerTab = "overview";
  state.graphSel = null;
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

/* --------------------- iteration lifecycle + compare --------------------- */

const LIFECYCLES = ["planned", "in_flight", "pending_eval", "shipped", "abandoned"];

function iterMeta(n) {
  const its =
    state.detail && Array.isArray(state.detail.iterations)
      ? state.detail.iterations
      : [];
  for (const it of its) {
    if (it && Number(it.n) === Number(n)) return it;
  }
  return null;
}

/* Lifecycle comes from the backend (detail.iterations). When the field is
 * absent (older server), fall back to timeline verdicts so badges still
 * render: last verdict wins, else presence of entries means in_flight. */
function iterLifecycle(n) {
  const meta = iterMeta(n);
  if (meta && LIFECYCLES.includes(meta.lifecycle)) return meta.lifecycle;
  const entries =
    state.detail && Array.isArray(state.detail.timeline) ? state.detail.timeline : [];
  let lastVerdict = null;
  let count = 0;
  for (const e of entries) {
    if (e.iteration == null || Number(e.iteration) !== Number(n)) continue;
    count++;
    if (e.verdict) lastVerdict = String(e.verdict).toUpperCase();
  }
  if (lastVerdict === "SHIP") return "shipped";
  if (lastVerdict === "ITERATE" || lastVerdict === "BLOCKED") return "abandoned";
  if (lastVerdict === "NEEDS_HUMAN") return "pending_eval";
  return count ? "in_flight" : "planned";
}

function lifecycleChip(n) {
  const st = iterLifecycle(n);
  return span("lifecycle-chip lifecycle-" + st, st.replace("_", " "));
}

function toggleCompare(n) {
  if (n == null) return;
  const i = state.compare.indexOf(n);
  if (i >= 0) {
    state.compare.splice(i, 1);
  } else {
    state.compare.push(n);
    if (state.compare.length > 2) state.compare.shift();
  }
  renderTimelineView();
}

function renderDetail(detail) {
  const display = loopState(detail);
  const badge = el("drawer-badge");
  badge.className = "status-label status-" + display;
  badge.textContent = STATE_LABEL[display];

  const missionEl = el("drawer-mission");
  missionEl.textContent = detail.mission || "No mission recorded.";
  missionEl.title = detail.mission || "";

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

  const commits = Array.isArray(detail.commits) ? detail.commits : [];
  const csec = el("commits-section");
  const clist = el("commit-list");
  clist.textContent = "";
  csec.hidden = commits.length === 0;
  for (const c of commits) {
    const row = document.createElement("div");
    row.className = "commit-row";
    const sha = span("commit-sha", c.short || (c.sha || "").slice(0, 7));
    sha.title = c.sha || "";
    row.appendChild(sha);
    if (c.slice) row.appendChild(span("meta-chip slice-chip", c.slice));
    const subj = span("commit-subject", c.subject || "");
    subj.title = c.subject || "";
    row.appendChild(subj);
    clist.appendChild(row);
  }

  renderTimeline(detail.timeline || []);
  renderTimelineView();
  renderFilesView();
  renderGraphView();

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
  /* Every entry is shown, grouped under per-iteration headers — the loop's
   * full narrative, not a per-(iteration, role) digest. Parallel
   * iterations interleave in file order, so group strictly by iteration
   * (file order preserved within each group). */
  const groups = new Map();
  for (const entry of entries) {
    const key = entry.iteration ?? null;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  const order = [...groups.keys()].sort(
    (x, y) => (x == null ? 1e9 : x) - (y == null ? 1e9 : y)
  );
  for (const iter of order) {
    const hdr = document.createElement("div");
    hdr.className = "iter-header";
    hdr.appendChild(
      span("iter-header-label", iter == null ? "unattributed" : "Iteration " + iter)
    );
    if (iter != null) hdr.appendChild(lifecycleChip(iter));
    view.appendChild(hdr);
    for (const entry of groups.get(iter)) {
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
      summary = summary.replace(
        /^VERDICT:\s*(SHIP|ITERATE|BLOCKED|NEEDS_HUMAN)\s*[—–-]\s*/i,
        ""
      );
    }
    if (entry.scope) {
      text.appendChild(span("meta-chip scope-chip", String(entry.scope)));
      text.appendChild(document.createTextNode(" "));
    }
    if (entry.slice) {
      text.appendChild(span("meta-chip slice-chip", String(entry.slice)));
      text.appendChild(document.createTextNode(" "));
    }
    text.appendChild(document.createTextNode(summary));
    text.title = summary;
    row.appendChild(text);
    view.appendChild(row);
    }
  }
}

/* ------------------------------ drawer views ------------------------------ */

function renderDrawerTabs() {
  const nav = el("drawer-tabs");
  nav.textContent = "";
  for (const tab of DRAWER_TABS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "drawer-tab" + (state.drawerTab === tab ? " active" : "");
    btn.textContent = tab;
    btn.setAttribute("aria-pressed", String(state.drawerTab === tab));
    btn.addEventListener("click", () => {
      state.drawerTab = tab;
      renderDrawerTabs();
      showDrawerTab();
    });
    nav.appendChild(btn);
  }
}

/* Toggle the active view without refetching: content is re-rendered from
 * the cached state.detail (renders are cheap, and data arrival re-renders
 * the visible view via renderDetail). */
function showDrawerTab() {
  for (const tab of DRAWER_TABS) {
    el("view-" + tab).hidden = state.drawerTab !== tab;
  }
  renderTimelineView();
  renderFilesView();
  renderGraphView();
}

function appendEmpty(view, msg) {
  const empty = document.createElement("div");
  empty.className = "view-empty";
  empty.textContent = msg;
  view.appendChild(empty);
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const key in attrs || {}) node.setAttribute(key, attrs[key]);
  return node;
}

function entryTitle(entry) {
  let t =
    "iter " + (entry.iteration ?? "?") + " · " + (entry.role || "?");
  if (entry.slice) t += " · " + entry.slice;
  t += " — " + oneLine(entry.summary, 200);
  if (entry.duration_sec != null) t += " · " + fmtDuration(entry.duration_sec);
  if (entry.scope) t += " · scope " + String(entry.scope);
  return t;
}

function fmtHm(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes());
}

function parseMs(iso) {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/* Wall-clock span for one entry; null when it has no usable timing. */
function tlSpan(entry) {
  const s = parseMs(entry.started_at);
  const e = parseMs(entry.ended_at);
  const d =
    entry.duration_sec != null && Number.isFinite(Number(entry.duration_sec))
      ? Number(entry.duration_sec) * 1000
      : null;
  if (s != null && e != null) return { start: s, end: e };
  if (s != null && d != null) return { start: s, end: s + d };
  if (e != null && d != null) return { start: e - d, end: e };
  if (e != null) return { start: e, end: e };
  if (s != null) return { start: s, end: s };
  return null;
}

function tlRole(entry) {
  return String(entry.role || "?").toLowerCase();
}

function tickStep(rangeMs, plotW) {
  const steps = [
    30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 7200e3,
    10800e3, 21600e3, 43200e3, 86400e3,
  ];
  for (const s of steps) {
    if (s / Math.max(1, rangeMs) * plotW >= 80) return s;
  }
  return steps[steps.length - 1];
}

function scopeLabel(scope) {
  const s = String(scope);
  if (s.startsWith("local:")) {
    const paths = s.slice(6).split(",").filter(Boolean);
    const one = paths[0] || "";
    const tail = one.length > 14 ? one.slice(0, 13) + "…" : one;
    const more = paths.length > 1 ? " +" + (paths.length - 1) : "";
    return "local:" + tail + more;
  }
  return s.length > 18 ? s.slice(0, 17) + "…" : s;
}

function shortSlice(id) {
  const s = String(id);
  return s.length > 16 ? s.slice(0, 15) + "…" : s;
}

function fitText(text, px) {
  const max = Math.max(1, Math.floor(px / 5.6));
  const s = String(text);
  return s.length > max ? s.slice(0, Math.max(1, max - 1)) + "…" : s;
}

/* --------------------------- Timeline view --------------------------- */

/* Iteration-centric swimlane: one row per iteration, entries in seq order
 * as role-colored blocks (wall-clock positioned when timings exist), the
 * row's last verdict at the right edge, and PLAN.md slice pills beneath. */
function renderTimelineView() {
  const view = el("view-timeline");
  if (view.hidden) return;
  view.textContent = "";
  if (!state.detail) {
    appendEmpty(view, "Loading…");
    return;
  }
  const entries = Array.isArray(state.detail.timeline) ? state.detail.timeline : [];
  if (!entries.length) {
    appendEmpty(view, "No log entries yet.");
    return;
  }
  const slices = Array.isArray(state.detail.slices) ? state.detail.slices : [];

  const iters = [];
  const seenIter = new Set();
  for (const e of entries) {
    const k = e.iteration == null ? null : Number(e.iteration);
    if (!seenIter.has(k)) {
      seenIter.add(k);
      iters.push(k);
    }
  }
  iters.sort((x, y) => (x == null ? 1e9 : x) - (y == null ? 1e9 : y));

  const timed = entries.some((e) => tlSpan(e));
  const W = Math.max(360, view.clientWidth - 2);
  const ML = 148;
  const MR = 100;
  const MT = 8;
  const MB = timed ? 24 : 10;
  const BLOCK_H = 22;
  const PILL_H = 12;
  const ROW_GAP = 12;
  const plotW = W - ML - MR;

  let minT = Infinity;
  let maxT = -Infinity;
  if (timed) {
    for (const e of entries) {
      const sp = tlSpan(e);
      if (sp) {
        minT = Math.min(minT, sp.start);
        maxT = Math.max(maxT, sp.end);
      }
    }
    if (!(maxT > minT)) {
      minT = 0;
      maxT = 1;
    }
  }
  const xTime = (t) => ML + ((t - minT) / (maxT - minT)) * plotW;

  const rows = iters.map((it) => ({
    it,
    entries: entries.filter(
      (e) => (e.iteration == null ? null : Number(e.iteration)) === it
    ),
    slices: slices.filter(
      (sl) => sl && sl.iteration != null && Number(sl.iteration) === it
    ),
  }));
  const maxCount = Math.max(1, ...rows.map((r) => r.entries.length));
  const seqW = Math.max(28, Math.min(110, plotW / maxCount - 6));

  let totalH = 0;
  const yOf = rows.map((r) => {
    const y = totalH;
    totalH += BLOCK_H + (r.slices.length ? PILL_H + 5 : 0) + ROW_GAP;
    return y;
  });
  const H = MT + totalH + MB;
  const svg = svgEl("svg", {
    class: "tl-svg",
    viewBox: "0 0 " + W + " " + H,
    width: W,
    height: H,
    role: "img",
    "aria-label": "Loop timeline",
  });

  rows.forEach((row, ri) => {
    const y = MT + yOf[ri];
    if (ri > 0) {
      svg.appendChild(
        svgEl("line", {
          class: "tl-rowline",
          x1: 0, x2: W, y1: y - ROW_GAP / 2, y2: y - ROW_GAP / 2,
        })
      );
    }
    if (row.it == null) {
      const lab = svgEl("text", {
        class: "tl-lane",
        x: ML - 8,
        y: y + BLOCK_H / 2 + 3.5,
        "text-anchor": "end",
      });
      lab.textContent = "—";
      svg.appendChild(lab);
    } else {
      const fo = svgEl("foreignObject", {
        x: 0, y: y, width: ML - 8, height: BLOCK_H,
      });
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tl-lanebtn" +
        (state.compare.includes(row.it) ? " selected" : "");
      btn.setAttribute("aria-pressed", String(state.compare.includes(row.it)));
      btn.title = "Click to compare iteration " + row.it;
      btn.appendChild(span("tl-lanebtn-label", "iter " + row.it));
      btn.appendChild(lifecycleChip(row.it));
      btn.addEventListener("click", () => toggleCompare(row.it));
      fo.appendChild(btn);
      svg.appendChild(fo);
    }

    let untimedN = 0;
    row.entries.forEach((entry, i) => {
      const role = tlRole(entry);
      const sp = timed ? tlSpan(entry) : null;
      if (timed && !sp) {
        /* Untimed entries in a timed chart: gutter markers right of the plot. */
        const mx = ML + plotW + 6 + untimedN * 8;
        untimedN++;
        const m = svgEl("rect", {
          class: "tl-untimed",
          x: mx, y: y + BLOCK_H / 2 - 3, width: 6, height: 6,
        });
        const tip = svgEl("title", {});
        tip.textContent = entryTitle(entry) + " · no timestamps";
        m.appendChild(tip);
        svg.appendChild(m);
        return;
      }
      let bx;
      let bw;
      if (timed) {
        bx = xTime(sp.start);
        bw = Math.max(3, xTime(sp.end) - bx);
      } else {
        bx = ML + i * (seqW + 6);
        bw = seqW;
      }
      const g = svgEl("g", {});
      const seg = svgEl("rect", {
        class: "tl-seg tl-role-" + cssClass(role),
        x: bx, y: y, width: bw, height: BLOCK_H, rx: 4,
      });
      const tip = svgEl("title", {});
      tip.textContent = entryTitle(entry);
      seg.appendChild(tip);
      g.appendChild(seg);
      if (bw >= 44) {
        const t = svgEl("text", {
          class: "tl-block-label",
          x: bx + 5,
          y: y + BLOCK_H / 2 + 3.5,
        });
        t.textContent = fitText(entry.slice ? "bld " + shortSlice(entry.slice) : role, bw - 10);
        g.appendChild(t);
        if (entry.duration_sec != null && bw >= 92) {
          const d = svgEl("text", {
            class: "tl-block-dur",
            x: bx + bw - 5,
            y: y + BLOCK_H / 2 + 3.5,
            "text-anchor": "end",
          });
          d.textContent = fmtDuration(entry.duration_sec);
          g.appendChild(d);
        }
      }
      if (entry.scope) {
        const sc = svgEl("text", {
          class: "tl-scope",
          x: bx + bw + 5,
          y: y + BLOCK_H / 2 + 3.5,
        });
        sc.textContent = scopeLabel(entry.scope);
        const stip = svgEl("title", {});
        stip.textContent = String(entry.scope);
        sc.appendChild(stip);
        g.appendChild(sc);
      }
      svg.appendChild(g);
    });

    /* Last verdict of the iteration at the right edge. */
    const vEntry = [...row.entries].reverse().find((e) => e.verdict);
    if (vEntry) {
      const vc = normVerdict(vEntry.verdict);
      const v = svgEl("text", {
        class: "tl-verdict tl-verdict-" + vc,
        x: W - 4,
        y: y + BLOCK_H / 2 + 3.5,
        "text-anchor": "end",
      });
      v.textContent = String(vEntry.verdict).toUpperCase();
      const vtip = svgEl("title", {});
      vtip.textContent = entryTitle(vEntry);
      v.appendChild(vtip);
      svg.appendChild(v);
    }

    /* Slice pills beneath the row; click jumps to the graph node. */
    if (row.slices.length) {
      let px = ML;
      const py = y + BLOCK_H + 5;
      for (const sl of row.slices) {
        const id = String(sl.id ?? "");
        const st = normSliceStatus(sl.status);
        const wpx = Math.min(190, Math.max(44, id.length * 5.6 + 14));
        if (px + wpx > ML + plotW + 30) break;
        const pill = svgEl("g", { class: "tl-pill", cursor: "pointer" });
        pill.appendChild(
          svgEl("rect", {
            class: "tl-pill-rect tl-pill-" + st,
            x: px, y: py, width: wpx, height: PILL_H, rx: 6,
          })
        );
        const pt = svgEl("text", {
          class: "tl-pill-label",
          x: px + 7,
          y: py + PILL_H / 2 + 3,
        });
        pt.textContent = fitText(id, wpx - 12);
        pill.appendChild(pt);
        const ptip = svgEl("title", {});
        ptip.textContent =
          id + " · " + st.replace("_", " ") +
          " · writes: " + (Array.isArray(sl.writes) ? sl.writes.length : 0) +
          " · reads: " + (Array.isArray(sl.reads) ? sl.reads.length : 0);
        pill.appendChild(ptip);
        pill.addEventListener("click", () => {
          state.graphSel = id;
          state.drawerTab = "graph";
          renderDrawerTabs();
          showDrawerTab();
        });
        svg.appendChild(pill);
        px += wpx + 6;
      }
    }
  });

  if (timed) {
    const step = tickStep(maxT - minT, plotW);
    for (let t = Math.floor(minT / step) * step; t <= maxT; t += step) {
      const gx = xTime(t);
      svg.appendChild(
        svgEl("line", { class: "tl-grid", x1: gx, y1: MT, x2: gx, y2: MT + totalH })
      );
      const lab = svgEl("text", {
        class: "tl-axis-label",
        x: gx,
        y: MT + totalH + 14,
        "text-anchor": "middle",
      });
      lab.textContent = fmtHm(t);
      svg.appendChild(lab);
    }
  }

  const wrap = document.createElement("div");
  wrap.className = "tl-wrap";
  wrap.appendChild(svg);

  const overlaps =
    state.detail && Array.isArray(state.detail.overlaps) ? state.detail.overlaps : [];
  if (overlaps.length) {
    const bar = document.createElement("div");
    bar.className = "overlap-notice";
    for (const ov of overlaps) {
      const line = document.createElement("div");
      line.className = "overlap-line";
      line.appendChild(
        span("overlap-head",
             "Iterations " + ov.a + " and " + ov.b + " overlap (" +
             String(ov.relation || "").replace("-", "/") + ")")
      );
      const paths = (ov.paths || []).join(", ");
      const det = span("overlap-paths", paths);
      det.title = paths;
      line.appendChild(det);
      bar.appendChild(line);
    }
    view.appendChild(bar);
  }

  if (state.compare.length) {
    view.appendChild(comparePanel());
  }

  view.appendChild(wrap);
}

/* Side-by-side compare of up to two selected iterations. */
function comparePanel() {
  const panel = document.createElement("div");
  panel.className = "compare-panel";

  const head = document.createElement("div");
  head.className = "compare-head";
  head.appendChild(span("section-label", "Compare iterations"));
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "follow-btn";
  clear.textContent = "Clear selection";
  clear.addEventListener("click", () => {
    state.compare = [];
    renderTimelineView();
  });
  head.appendChild(clear);
  panel.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "compare-grid";

  const cols = state.compare.map((n) => {
    const meta = iterMeta(n) || {};
    const files = Array.isArray(meta.files) ? meta.files : [];
    const criteria = Array.isArray(meta.criteria) ? meta.criteria : [];
    return { n, meta, files, criteria };
  });
  const shared =
    cols.length === 2 ? new Set(cols[0].files.filter((f) => cols[1].files.includes(f))) : new Set();

  for (const col of cols) {
    const card = document.createElement("div");
    card.className = "compare-col";

    const title = document.createElement("div");
    title.className = "compare-title";
    title.appendChild(span("compare-iter", "Iteration " + col.n));
    title.appendChild(lifecycleChip(col.n));
    card.appendChild(title);

    const facts = document.createElement("dl");
    facts.className = "compare-facts";
    const addFact = (label, value) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value == null || value === "" ? "—" : String(value);
      facts.appendChild(dt);
      facts.appendChild(dd);
    };
    addFact("Verdict", col.meta.verdict || "—");
    addFact("Duration", col.meta.duration_sec != null ? fmtDuration(col.meta.duration_sec) : "—");
    addFact("Started", col.meta.started || "—");
    addFact("Ended", col.meta.ended || "—");
    card.appendChild(facts);

    if (col.criteria.length) {
      const cl = document.createElement("div");
      cl.className = "compare-criteria";
      for (const c of col.criteria) {
        const row = document.createElement("div");
        row.className = "compare-crit";
        row.appendChild(span("crit-outcome crit-" + String(c.outcome || "").toLowerCase(), c.outcome || "?"));
        row.appendChild(span("crit-id", c.id || ""));
        const t = span("crit-title", c.title || "");
        t.title = c.title || "";
        row.appendChild(t);
        cl.appendChild(row);
      }
      card.appendChild(cl);
    }

    const fl = document.createElement("div");
    fl.className = "compare-files";
    if (!col.files.length) {
      fl.appendChild(span("crit-title", "No attributed files"));
    }
    for (const f of col.files) {
      const chip = span("compare-file" + (shared.has(f) ? " shared" : ""), f);
      chip.title = shared.has(f) ? f + " — also written by the other iteration" : f;
      fl.appendChild(chip);
    }
    card.appendChild(fl);

    grid.appendChild(card);
  }

  panel.appendChild(grid);
  return panel;
}

/* --------------------------- Files view --------------------------- */

function renderFilesView() {
  const view = el("view-files");
  if (view.hidden) return;
  view.textContent = "";
  if (!state.detail) {
    appendEmpty(view, "Loading…");
    return;
  }
  const slices = Array.isArray(state.detail.slices) ? state.detail.slices : null;
  if (!slices || !slices.length) {
    appendEmpty(view, "PLAN.md has no slices block.");
    return;
  }
  const shadow = new Map();
  for (const s of (state.detail.slice_activity &&
    state.detail.slice_activity.slices) || []) {
    if (s && s.id != null) {
      shadow.set(String(s.id), new Set((s.undeclared || []).map(String)));
    }
  }
  const rows = new Map();
  const iters = new Set();
  const MAIL = "@mailbox";
  const bump = (rows, key, iterKey, id, drift) => {
    let row = rows.get(key);
    if (!row) {
      row = { label: key, cells: new Map() };
      rows.set(key, row);
    }
    let c = row.cells.get(iterKey);
    if (!c) {
      c = { count: 0, ids: [], drift: false };
      row.cells.set(iterKey, c);
    }
    c.count += 1;
    if (!c.ids.includes(id)) c.ids.push(id);
    if (drift) c.drift = true;
  };
  for (const s of slices) {
    const id = String(s.id ?? "");
    const iterKey = s.iteration == null ? "—" : String(s.iteration);
    iters.add(iterKey);
    for (const w of Array.isArray(s.writes) ? s.writes : []) {
      if (typeof w !== "string" || !w) continue;
      if (w.startsWith("api:")) continue; // interface names are not files
      const key = w.startsWith("loop/") ? MAIL : w;
      bump(rows, key, iterKey, id, false);
    }
    const und = shadow.get(id);
    if (und) {
      for (const u of und) {
        const key = u.startsWith("loop/") ? MAIL : u;
        bump(rows, key, iterKey, id, true);
      }
    }
  }
  const rowKeys = [...rows.keys()].filter((k) => k !== MAIL).sort();
  if (rows.has(MAIL)) rowKeys.push(MAIL);
  const iterCols = [...iters].sort((a, b) =>
    a === "—" ? 1 : b === "—" ? -1 : Number(a) - Number(b)
  );
  if (!rowKeys.length) {
    appendEmpty(view, "No file writes recorded in PLAN.md slices.");
    return;
  }
  let maxCount = 1;
  for (const k of rowKeys) {
    for (const c of rows.get(k).cells.values()) {
      maxCount = Math.max(maxCount, c.count);
    }
  }

  const wrap = document.createElement("div");
  wrap.className = "heat-wrap";
  const grid = document.createElement("div");
  grid.className = "heat-grid";
  grid.setAttribute("role", "table");
  grid.setAttribute("aria-label", "Slice file writes per iteration");
  grid.style.gridTemplateColumns =
    "minmax(110px, 1fr) repeat(" + iterCols.length + ", 34px)";

  const head = document.createElement("div");
  head.className = "heat-file heat-hdr";
  head.setAttribute("role", "columnheader");
  head.textContent = "file";
  grid.appendChild(head);
  for (const it of iterCols) {
    const h = document.createElement("div");
    h.className = "heat-hdr";
    h.textContent = it;
    h.setAttribute("role", "columnheader");
    grid.appendChild(h);
  }

  for (const key of rowKeys) {
    const row = rows.get(key);
    const label = key === MAIL ? "loop/ (mailbox)" : key;
    const lf = document.createElement("div");
    lf.className = "heat-file heat-rowfile";
    lf.textContent = label;
    lf.setAttribute("role", "rowheader");
    lf.title = key === MAIL ? "loop/ mailbox paths" : label;
    grid.appendChild(lf);
    for (const it of iterCols) {
      const c = row.cells.get(it);
      const cell = document.createElement("div");
      cell.className = "heat-cell" + (c ? " hot" : " empty") +
        (c && c.drift ? " drift" : "");
      if (c) {
        const a = 0.1 + 0.65 * (c.count / maxCount);
        cell.style.background = "rgba(232, 163, 61, " + a.toFixed(3) + ")";
        cell.title = label + (it === "—" ? " · unplanned" : " · iter " + it) +
          " — " + c.ids.join(", ") +
          (c.drift ? " (undeclared)" : "");
      } else {
        cell.title = label + (it === "—" ? " · unplanned" : " · iter " + it) +
          " — no writes";
      }
      grid.appendChild(cell);
    }
  }
  wrap.appendChild(grid);
  view.appendChild(wrap);
}

/* --------------------------- Graph view --------------------------- */

function graphNodeW(id) {
  return Math.min(168, Math.max(64, Math.round(id.length * 6.4 + 20)));
}

function normSliceStatus(raw) {
  const s = String(raw || "").trim().toLowerCase();
  if (s === "complete" || s === "completed" || s === "done") return "complete";
  if (
    s === "in_progress" || s === "in-progress" ||
    s === "running" || s === "active"
  ) return "in_progress";
  return "planned";
}

function renderGraphView() {
  const view = el("view-graph");
  if (view.hidden) return;
  view.textContent = "";
  if (!state.detail) {
    appendEmpty(view, "Loading…");
    return;
  }
  const slices = Array.isArray(state.detail.slices) ? state.detail.slices : null;
  if (!slices || !slices.length) {
    appendEmpty(view, "PLAN.md has no slices block.");
    return;
  }
  const NODE_H = 26;
  const V_GAP = 16;
  const L_GAP = 56;

  /* Layers: numbered iterations ascending; slices without an iteration get
   * their own layer at the end, in declaration order. */
  const numbered = slices
    .map((s, i) => ({ s, i }))
    .filter((x) => x.s.iteration != null)
    .sort((a, b) => a.s.iteration - b.s.iteration || a.i - b.i);
  const layers = [];
  for (const x of numbered) {
    const last = layers[layers.length - 1];
    if (last && last[0].s.iteration === x.s.iteration) last.push(x);
    else layers.push([x]);
  }
  for (const x of slices
    .map((s, i) => ({ s, i }))
    .filter((x) => x.s.iteration == null)) {
    layers.push([x]);
  }

  const widths = layers.map((layer) =>
    Math.max(...layer.map((x) => graphNodeW(String(x.s.id ?? x.i))))
  );
  const W =
    20 + widths.reduce((a, b) => a + b, 0) +
    Math.max(0, layers.length - 1) * L_GAP + 20;
  const xs = [];
  let cx = 20;
  for (const w of widths) {
    xs.push(cx);
    cx += w + L_GAP;
  }
  const maxNodes = Math.max(...layers.map((l) => l.length));
  const H = 24 + maxNodes * (NODE_H + V_GAP) + 24;
  const pos = new Map();
  for (let li = 0; li < layers.length; li++) {
    const total = layers[li].length * (NODE_H + V_GAP);
    let y = (H - total) / 2;
    for (const x of layers[li]) {
      pos.set(x.i, { x: xs[li], y, w: widths[li] });
      y += NODE_H + V_GAP;
    }
  }

  /* Edges A→B when B reads something A writes (string equality, api: names
   * included). Dashed when the coupling is api:-only. */
  const edges = [];
  for (let bi = 0; bi < slices.length; bi++) {
    const B = slices[bi];
    const reads = new Set((Array.isArray(B.reads) ? B.reads : []).map(String));
    for (let ai = 0; ai < slices.length; ai++) {
      if (ai === bi) continue;
      const A = slices[ai];
      const matched = (Array.isArray(A.writes) ? A.writes : [])
        .map(String)
        .filter((w) => reads.has(w) && !w.startsWith("loop/"));
      if (matched.length) edges.push({ from: ai, to: bi, matched });
    }
  }

  const wrap = document.createElement("div");
  wrap.className = "graph-wrap";
  const svg = svgEl("svg", {
    class: "g-svg",
    viewBox: "0 0 " + W + " " + H,
    width: W,
    height: H,
    role: "img",
    "aria-label": "Slice dependency graph",
  });
  const defs = svgEl("defs", {});
  const mk = svgEl("marker", {
    id: "g-arrow",
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 6.5,
    markerHeight: 6.5,
    orient: "auto-start-reverse",
  });
  mk.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: "g-arrow-path" }));
  defs.appendChild(mk);
  const mks = svgEl("marker", {
    id: "g-arrow-sel",
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 7,
    markerHeight: 7,
    orient: "auto-start-reverse",
  });
  mks.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: "g-arrow-sel-path" }));
  defs.appendChild(mks);
  svg.appendChild(defs);

  const sel = state.graphSel;
  const hasSel = sel != null && slices.some((s) => String(s.id ?? "") === sel);
  if (!hasSel) state.graphSel = null;

  for (const e of edges) {
    const A = pos.get(e.from);
    const B = pos.get(e.to);
    const ax = A.x + A.w;
    const ay = A.y + NODE_H / 2;
    const sameLayer = B.x === A.x;
    let d;
    if (sameLayer) {
      /* Route around the right side of the layer so the arrow enters B's
       * right edge pointing left (source and target share a column). */
      const bx = B.x + B.w;
      const by = B.y + NODE_H / 2;
      d = "M " + ax + " " + ay +
        " C " + (ax + 40) + " " + ay +
        ", " + (bx + 40) + " " + by +
        ", " + bx + " " + by;
    } else {
      const bx = B.x;
      const by = B.y + NODE_H / 2;
      d = "M " + ax + " " + ay + " L " + bx + " " + by;
    }
    const apiOnly = e.matched.every((m) => m.startsWith("api:"));
    const isSel =
      hasSel &&
      (String(slices[e.from].id ?? "") === sel ||
        String(slices[e.to].id ?? "") === sel);
    const cls =
      "g-edge" + (apiOnly ? " g-edge-api" : "") +
      (isSel ? " g-edge-sel" : hasSel ? " g-edge-dim" : "");
    const line = svgEl("path", { class: cls, d });
    line.setAttribute("marker-end", isSel ? "url(#g-arrow-sel)" : "url(#g-arrow)");
    const tip = svgEl("title", {});
    tip.textContent =
      String(slices[e.from].id ?? e.from) + " → " +
      String(slices[e.to].id ?? e.to) + ": " + e.matched.join(", ");
    line.appendChild(tip);
    svg.appendChild(line);
  }

  for (let i = 0; i < slices.length; i++) {
    const s = slices[i];
    const id = String(s.id ?? i);
    const p = pos.get(i);
    const status = normSliceStatus(s.status);
    const g = svgEl("g", {
      class:
        "g-node g-node-" + status +
        (id === state.graphSel ? " g-node-sel" : ""),
      transform: "translate(" + p.x + ", " + p.y + ")",
      "data-id": id,
      tabindex: "0",
      role: "button",
    });
    g.appendChild(
      svgEl("rect", { class: "g-node-bg", width: p.w, height: NODE_H, rx: 6 })
    );
    const maxChars = Math.max(4, Math.floor((p.w - 16) / 6.1));
    const label = svgEl("text", {
      class: "g-node-label",
      x: 8,
      y: NODE_H / 2 + 3.5,
    });
    label.textContent =
      id.length > maxChars ? id.slice(0, maxChars - 1) + "…" : id;
    g.appendChild(label);
    const tip = svgEl("title", {});
    tip.textContent =
      id + " · " + status +
      (s.iteration != null ? " · iter " + s.iteration : "");
    g.appendChild(tip);
    g.addEventListener("click", (ev) => {
      ev.stopPropagation();
      state.graphSel = state.graphSel === id ? null : id;
      renderGraphView();
    });
    g.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        state.graphSel = state.graphSel === id ? null : id;
        renderGraphView();
      }
    });
    svg.appendChild(g);
  }
  svg.addEventListener("click", (ev) => {
    if (ev.target === svg) {
      state.graphSel = null;
      renderGraphView();
    }
  });

  wrap.appendChild(svg);
  view.appendChild(wrap);
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
