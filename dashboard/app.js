"use strict";

/* Trio Loop Dashboard — frontend.
 * No build step, no external dependencies. Talks to the backend at
 * /api/overview (every workspace's board), /api/loop, /api/transcript (SSE),
 * and loop controls. Loops are keyed by workspace root + mailbox name. */

const state = {
  boardTimer: null,
  loops: [],
  inbox: [],
  workspaces: [],
  scanned: 0,
  byKey: new Map(),
  showReadInbox: false,
  updatedAt: null,
  lastOk: null,
  loaded: false,
  tab: "all",
  workspace: "",
  query: "",
  sort: { key: "activity", dir: "desc" },

  /* drawer */
  activeLoop: null,
  boardSignature: null,
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

/* Loop to auto-open via `#loop=<name>` or `#root=<path>&loop=<name>` once
 * board data confirms it exists (set in init(), consumed by refreshBoard()). */
let pendingLoopHash = null;

/* Workspace of the loop open in the drawer; detail requests carry it. */
let currentRoot = "";
function withRoot(url) {
  if (!currentRoot) return url;
  return url + (url.includes("?") ? "&" : "?") + "root=" + encodeURIComponent(currentRoot);
}

const BOARD_POLL_MS = 5000;
const TABS = ["running", "attention", "all", "archived"];
const DRAWER_TABS = ["overview", "timeline", "files", "graph", "transcripts"];

/* ------------------------------ helpers ------------------------------ */

function el(id) {
  return document.getElementById(id);
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

function normVerdict(raw) {
  if (!raw) return "none";
  const v = String(raw).trim().toUpperCase();
  if (v === "SHIP") return "ship";
  if (v === "ITERATE") return "iterate";
  if (v === "BLOCKED") return "blocked";
  if (v === "NEEDS_HUMAN") return "needs_human";
  return "none";
}

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

function isArchived(loop) {
  return String(loop.name || "").split("/")[0].startsWith("loop-archive");
}

/* Keep the STATE.md word factual; "unknown" is the API's missing-value marker. */
function statusWord(loop) {
  const raw = loop.status == null ? "" : String(loop.status);
  const value = raw.trim();
  return !value || value === "unknown" ? "—" : raw;
}

/* Prefer the persisted verdict, then the last parsed segment verdict. */
function latestVerdict(loop) {
  const final = String(loop.final_verdict || "").trim();
  if (final) return final.toUpperCase();
  const seq = verdictSeq(loop);
  return seq.length ? String(seq[seq.length - 1]).toUpperCase() : null;
}

/* ------------------------------ board ------------------------------ */

/* Attention kinds that ask for a decision now; everything else on a
 * finished loop is a review note (still listed, just not at the top). */
const NEEDS_KINDS = new Set([
  "needs_human", "blocked", "interrupted", "orphaned", "queue_fault",
]);
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };
const STORE_KEY = "trio.board.v2";
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const STALE_AFTER_MS = 30 * 1000;

function loopKey(root, name) {
  return root + "::" + name;
}

function isNeedsItem(item, loop) {
  if (NEEDS_KINDS.has(item.kind) || item.severity === "high") return true;
  return Boolean(loop && loop.running);
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (TABS.includes(saved.tab)) state.tab = saved.tab;
    if (typeof saved.workspace === "string") state.workspace = saved.workspace;
    if (saved.sort && typeof saved.sort.key === "string") state.sort = saved.sort;
  } catch (err) {
    /* private window or blocked storage: defaults are fine */
  }
}

function savePrefs() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      tab: state.tab, workspace: state.workspace, sort: state.sort,
    }));
  } catch (err) {
    /* ignore */
  }
}

/* GOAL.md heading, else the mission's first clause, else the mailbox name. */
function loopTitle(loop) {
  if (loop.title) return loop.title;
  const mission = String(loop.mission || "").split(/(?<=[.;:])\s/)[0].trim();
  return mission ? oneLine(mission, 90) : loop.name;
}

/* One state word per loop, from facts only: live evidence first, then the
 * latest verdict, then the STATE.md status word. */
function stateBadge(loop) {
  const badge = document.createElement("span");
  let tone = "neutral";
  let text = statusWord(loop);
  const verdict = normVerdict(latestVerdict(loop));
  if (loop.running) {
    tone = "live";
    text = "Running";
  } else if (verdict === "ship") {
    tone = "positive";
    text = "Shipped";
  } else if (verdict === "needs_human") {
    tone = "warning";
    text = "Needs human";
  } else if (verdict === "blocked") {
    tone = "negative";
    text = "Blocked";
  } else if (verdict === "iterate") {
    text = "Iterating";
  }
  badge.className = "badge badge-" + tone;
  badge.appendChild(span("badge-icon", { live: "●", positive: "✓", warning: "!", negative: "✕", neutral: "○" }[tone]));
  badge.appendChild(document.createTextNode(text));
  const facts = ["STATE.md: " + statusWord(loop)];
  if (latestVerdict(loop)) facts.push("verdict: " + latestVerdict(loop));
  if (loop.running) facts.push("live via " + (loop.running_sources || []).join(", "));
  badge.title = facts.join(" · ");
  return badge;
}

function iterationText(loop) {
  if (loop.iteration == null) return "—";
  return loop.max_iterations != null
    ? loop.iteration + " of " + loop.max_iterations
    : String(loop.iteration);
}

function phaseText(loop) {
  const phase = String(loop.driver_phase || "").trim();
  const sub = loop.running_substate;
  const parts = [];
  if (phase && phase.toLowerCase() !== "idle") parts.push(phase.replace(/[-_]/g, " "));
  if (sub) parts.push(sub === "both" ? "lead and evaluator active" : sub + " active");
  return parts.join(" · ");
}

function historyEl(loop) {
  const seq = verdictSeq(loop);
  const wrap = document.createElement("span");
  wrap.className = "hist";
  if (!seq.length) {
    wrap.appendChild(span("hist-none", "—"));
    wrap.title = "No evaluator verdicts parsed from LOG.md";
    return wrap;
  }
  const letter = { ship: "S", iterate: "I", blocked: "B", needs_human: "H" };
  const shown = seq.slice(-12);
  for (const v of shown) wrap.appendChild(span("hist-cell hist-" + v, letter[v] || "?"));
  wrap.title = "Verdicts, oldest to newest: " + seq.map((v) => letter[v]).join(" ");
  wrap.setAttribute("aria-label", wrap.title);
  return wrap;
}

function ageMs(iso) {
  const t = Date.parse(iso || "");
  return Number.isNaN(t) ? null : Date.now() - t;
}

/* ---------------------------- data ---------------------------- */

function ingestOverview(data) {
  const loops = [];
  const inbox = [];
  const workspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
  for (const ws of workspaces) {
    for (const loop of ws.loops || []) {
      loop.root = ws.root;
      loop.workspace = ws.name;
      loop.key = loopKey(ws.root, loop.name);
      loops.push(loop);
    }
    for (const item of ws.inbox || []) {
      item.root = ws.root;
      item.workspace = ws.name;
      item.key = loopKey(ws.root, item.loop);
      inbox.push(item);
    }
  }
  state.workspaces = workspaces;
  state.scanned = data.scanned || workspaces.length;
  state.loops = loops;
  state.inbox = inbox;
  state.byKey = new Map(loops.map((loop) => [loop.key, loop]));
  state.updatedAt = data.updated_at || null;
}

async function refreshBoard() {
  clearTimeout(state.boardTimer);
  try {
    const res = await fetch("/api/overview", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    ingestOverview(data);
    state.lastOk = Date.now();
    state.loaded = true;
    hideBoardError();
    renderAll();
    if (pendingLoopHash) {
      const target = resolveHashLoop(pendingLoopHash);
      if (target) {
        pendingLoopHash = null;
        openDrawer(target.key);
      }
    }
    if (state.activeLoop) refreshDetail({ quiet: true });
  } catch (err) {
    showBoardError(
      state.loaded
        ? "Lost contact with the dashboard server (" + err.message + "). Showing the last data received; retrying every 5 seconds."
        : "Cannot reach the dashboard server (" + err.message + "). Retrying every 5 seconds."
    );
    renderLive();
  }
  state.boardTimer = setTimeout(refreshBoard, BOARD_POLL_MS);
}

function resolveHashLoop(hash) {
  if (hash.root) return state.byKey.get(loopKey(hash.root, hash.name)) || null;
  return state.loops.find((loop) => loop.name === hash.name) || null;
}

function showBoardError(msg) {
  el("board-error").hidden = false;
  el("board-error-text").textContent = msg;
}

function hideBoardError() {
  el("board-error").hidden = true;
}

/* ---------------------------- render ---------------------------- */

function renderAll() {
  const groups = attentionGroups();
  renderLive();
  renderSummary(groups);
  renderNeeds(groups);
  renderRunning();
  renderWorkspaceFilter();
  renderTabs();
  renderBoard();
  renderNotes(groups);
  renderDrawerInbox();
}

function renderLive() {
  const live = el("live");
  const text = el("live-text");
  const buildAge = ageMs(state.updatedAt);
  const sinceOk = state.lastOk ? Date.now() - state.lastOk : null;
  let cls = "live-ok";
  let label;
  if (!state.lastOk) {
    cls = el("board-error").hidden ? "live-connecting" : "live-offline";
    label = el("board-error").hidden ? "Connecting" : "Offline";
  } else if (!el("board-error").hidden) {
    cls = "live-offline";
    label = "Offline · data from " + fmtClock(state.updatedAt);
  } else if ((buildAge != null && buildAge > STALE_AFTER_MS) || sinceOk > STALE_AFTER_MS) {
    cls = "live-stale";
    label = "Stale · data from " + fmtClock(state.updatedAt);
  } else {
    label = "Live · " + fmtClock(state.updatedAt);
  }
  live.className = "live " + cls;
  text.textContent = label;
  live.title = "Polls every " + BOARD_POLL_MS / 1000 + " s. Data built at " +
    (state.updatedAt ? new Date(state.updatedAt).toLocaleString() : "—");
}

/* Unread attention grouped per loop, split into "needs you" and notes. */
function attentionGroups() {
  const needs = new Map();
  const notes = new Map();
  let readCount = 0;
  for (const item of state.inbox) {
    if (item.read) {
      readCount += 1;
      if (!state.showReadInbox) continue;
    }
    const loop = state.byKey.get(item.key);
    const target = isNeedsItem(item, loop) ? needs : notes;
    if (!target.has(item.key)) target.set(item.key, { key: item.key, loop, items: [] });
    target.get(item.key).items.push(item);
  }
  const order = (g) => Math.min(...g.items.map((i) => SEVERITY_RANK[i.severity] ?? 3));
  const sortGroups = (map) => Array.from(map.values()).sort((a, b) =>
    order(a) - order(b) ||
    String(b.loop?.last_activity || "").localeCompare(String(a.loop?.last_activity || "")));
  for (const g of [...needs.values(), ...notes.values()]) {
    g.items.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3));
  }
  return { needs: sortGroups(needs), notes: sortGroups(notes), readCount };
}

function plural(n, word, many) {
  return n + " " + (n === 1 ? word : many || word + "s");
}

function renderSummary(groups) {
  const loops = state.loops;
  const running = loops.filter((l) => l.running);
  const needsLoops = groups.needs.filter((g) => g.items.some((i) => !i.read));
  const shipped7 = loops.filter((l) => normVerdict(latestVerdict(l)) === "ship" &&
    (ageMs(l.verdict_mtime) ?? Infinity) <= WEEK_MS);
  const wsCount = state.workspaces.length;

  let verdict;
  if (!loops.length) {
    verdict = "No loop mailboxes found yet.";
  } else if (needsLoops.length) {
    verdict = plural(needsLoops.length, "loop") + (needsLoops.length === 1 ? " needs" : " need") + " you" +
      (running.length ? "; " + plural(running.length, "loop") + " running." : "; nothing is running.");
  } else if (running.length) {
    verdict = "All clear: " + plural(running.length, "loop") + " running, nothing needs you.";
  } else {
    verdict = "All clear. Nothing is running and nothing needs you.";
  }
  el("verdict").textContent = verdict;
  const latest = loops.slice().sort((a, b) =>
    String(b.last_activity || "").localeCompare(String(a.last_activity || "")))[0];
  el("verdict-sub").textContent = latest
    ? "Most recent activity: " + loopTitle(latest) + " (" + latest.workspace + "), " + relTime(latest.last_activity) + "."
    : "Start a loop with /trio-init in a project; it appears here on the next poll.";

  const partial = state.workspaces.filter((w) => w.error);
  el("board-partial").hidden = partial.length === 0;
  el("board-partial").textContent = partial.length
    ? "Could not read " + partial.map((w) => w.name).join(", ") + ". Other workspaces are shown."
    : "";

  const kpis = el("kpis");
  kpis.textContent = "";
  const unread = groups.needs.reduce((n, g) => n + g.items.filter((i) => !i.read).length, 0);
  const tile = (label, value, context, tone, target) => {
    const node = document.createElement(target ? "a" : "div");
    node.className = "kpi" + (tone ? " kpi-" + tone : "");
    if (target) node.href = target;
    node.appendChild(span("eyebrow", label));
    node.appendChild(span("kpi-value", String(value)));
    node.appendChild(span("kpi-context", context));
    kpis.appendChild(node);
  };
  tile("Needs you", needsLoops.length,
    needsLoops.length ? plural(unread, "unread item") : "Nothing waiting",
    needsLoops.length ? "warning" : "", "#needs");
  tile("Running now", running.length,
    running.length ? running.map((l) => phaseText(l) || "live").slice(0, 2).join(", ") : "No live driver, process or session",
    running.length ? "live" : "", "#running");
  tile("Shipped, last 7 days", shipped7.length,
    plural(loops.filter((l) => normVerdict(latestVerdict(l)) === "ship").length, "shipped loop") + " in total", "", null);
  tile("Loops tracked", loops.length,
    "across " + plural(wsCount, "workspace") + " (" + state.scanned + " scanned)", "", "#loops");
}

function inboxMarkButton(item) {
  const mark = document.createElement("button");
  mark.type = "button";
  mark.className = "btn btn-ghost btn-small";
  mark.textContent = item.read ? "Mark unread" : "Mark read";
  mark.setAttribute(
    "aria-label",
    (item.read ? "Mark unread: " : "Mark read: ") + (item.headline || "item")
  );
  mark.addEventListener("click", (event) => {
    event.stopPropagation();
    setInboxRead([item], !item.read);
  });
  return mark;
}

function severityBadge(item) {
  const sev = item.severity || "low";
  const label = { high: "Action", medium: "Check", low: "Note" }[sev] || sev;
  const tone = { high: "negative", medium: "warning", low: "neutral" }[sev] || "neutral";
  const badge = span("badge badge-" + tone, "");
  badge.appendChild(span("badge-icon", { high: "!", medium: "▲", low: "•" }[sev] || "•"));
  badge.appendChild(document.createTextNode(label));
  return badge;
}

/* One row per loop: the most severe item leads, the rest are counted. */
function attentionRow(group) {
  const lead = group.items[0];
  const loop = group.loop;
  const row = document.createElement("div");
  row.className = "attn-row";
  if (group.items.every((i) => i.read)) row.classList.add("is-read");
  row.dataset.key = group.key;

  row.appendChild(severityBadge(lead));

  const main = document.createElement("div");
  main.className = "attn-main";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "attn-open";
  open.textContent = loop ? loopTitle(loop) : lead.loop;
  open.setAttribute("aria-label", "Open " + (loop ? loopTitle(loop) : lead.loop) + ": " + lead.headline);
  open.addEventListener("click", () => openDrawer(group.key));
  main.appendChild(open);
  main.appendChild(span("attn-where mono", lead.workspace + " / " + lead.loop));
  const reason = document.createElement("p");
  reason.className = "attn-reason";
  reason.textContent = lead.headline || "";
  if (group.items.length > 1) {
    reason.appendChild(span("attn-more", " + " + plural(group.items.length - 1, "more item")));
  }
  main.appendChild(reason);
  if (lead.detail) {
    const detail = span("attn-detail", oneLine(lead.detail, 220));
    detail.title = lead.detail;
    main.appendChild(detail);
  }
  row.appendChild(main);

  const side = document.createElement("div");
  side.className = "attn-side";
  side.appendChild(span("caption", loop && loop.last_activity ? relTime(loop.last_activity) : ""));
  const mark = document.createElement("button");
  mark.type = "button";
  mark.className = "btn btn-ghost btn-small";
  const allRead = group.items.every((i) => i.read);
  mark.textContent = allRead ? "Mark unread" : (group.items.length > 1 ? "Mark all read" : "Mark read");
  mark.setAttribute("aria-label", mark.textContent + ": " + (loop ? loopTitle(loop) : lead.loop));
  mark.addEventListener("click", (event) => {
    event.stopPropagation();
    setInboxRead(group.items, !allRead);
  });
  side.appendChild(mark);
  row.appendChild(side);
  row.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    openDrawer(group.key);
  });
  return row;
}

function emptyState(icon, text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.appendChild(span("empty-icon", icon));
  node.appendChild(span("", text));
  return node;
}

function renderNeeds(groups) {
  const list = el("needs-list");
  const toggle = el("inbox-toggle");
  const unreadGroups = groups.needs.filter((g) => g.items.some((i) => !i.read));
  el("needs-count").textContent = unreadGroups.length ? String(unreadGroups.length) : "";
  toggle.hidden = groups.readCount === 0;
  toggle.textContent = (state.showReadInbox ? "Hide read (" : "Show read (") + groups.readCount + ")";
  toggle.setAttribute("aria-expanded", String(state.showReadInbox));
  list.textContent = "";
  if (!groups.needs.length) {
    list.appendChild(emptyState("✓", "Nothing needs you. Loops that block, ask for human verification or stop mid-loop appear here."));
    return;
  }
  for (const group of groups.needs) list.appendChild(attentionRow(group));
}

function renderNotes(groups) {
  const details = el("notes");
  details.hidden = groups.notes.length === 0;
  const count = groups.notes.reduce((n, g) => n + g.items.length, 0);
  el("notes-count").textContent = String(count);
  el("notes-sub").textContent = "Drift, overlap and repair notes on " + plural(groups.notes.length, "loop") + " that are not running";
  const list = el("notes-list");
  list.textContent = "";
  for (const group of groups.notes) list.appendChild(attentionRow(group));
}

function renderRunning() {
  const list = el("running-list");
  const running = state.loops.filter((l) => l.running)
    .sort((a, b) => String(b.last_activity || "").localeCompare(String(a.last_activity || "")));
  el("running-count").textContent = running.length ? String(running.length) : "";
  list.textContent = "";
  if (!running.length) {
    list.appendChild(emptyState("○", "No loop is running. A loop shows here while its driver, lock, process or session is live."));
    return;
  }
  for (const loop of running) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "run-card";
    card.dataset.key = loop.key;
    card.addEventListener("click", () => openDrawer(loop.key));
    const top = document.createElement("span");
    top.className = "run-top";
    top.appendChild(stateBadge(loop));
    top.appendChild(span("caption", "last write " + relTime(loop.last_activity)));
    card.appendChild(top);
    card.appendChild(span("run-title", loopTitle(loop)));
    card.appendChild(span("attn-where mono", loop.workspace + " / " + loop.name));
    const meta = document.createElement("span");
    meta.className = "run-meta";
    meta.appendChild(span("", "Iteration " + iterationText(loop)));
    const phase = phaseText(loop);
    if (phase) meta.appendChild(span("", phase));
    card.appendChild(meta);
    if (loop.iteration != null && loop.max_iterations) {
      const bar = document.createElement("span");
      bar.className = "meter";
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", "Iteration " + loop.iteration + " of " + loop.max_iterations);
      const fill = document.createElement("span");
      fill.className = "meter-fill";
      fill.style.width = Math.min(100, (loop.iteration / loop.max_iterations) * 100) + "%";
      bar.appendChild(fill);
      card.appendChild(bar);
    }
    card.appendChild(span("caption", "Live via " + (loop.running_sources || []).join(", ")));
    list.appendChild(card);
  }
}

function renderWorkspaceFilter() {
  const select = el("workspace-filter");
  const names = state.workspaces.map((w) => [w.root, w.name, (w.loops || []).length]);
  const sig = JSON.stringify(names);
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All workspaces";
    select.appendChild(all);
    for (const [root, name, count] of names) {
      const option = document.createElement("option");
      option.value = root;
      option.textContent = name + " (" + count + ")";
      option.title = root;
      select.appendChild(option);
    }
  }
  if (state.workspace && !names.some(([root]) => root === state.workspace)) state.workspace = "";
  select.value = state.workspace;
}

function hasUnreadAttention(loop) {
  return state.inbox.some((item) => item.key === loop.key && !item.read);
}

function matchesTab(tab, loop) {
  if (tab === "running") return Boolean(loop.running);
  if (tab === "attention") return hasUnreadAttention(loop);
  if (tab === "archived") return isArchived(loop);
  return true;
}

const TAB_LABELS = { running: "Running", attention: "Attention", all: "All", archived: "Archived" };

function renderTabs() {
  const nav = el("tabs");
  const scoped = state.loops.filter(matchesFilters);
  nav.textContent = "";
  for (const tab of TABS) {
    const count = scoped.filter((loop) => matchesTab(tab, loop)).length;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "seg-btn";
    btn.textContent = TAB_LABELS[tab];
    btn.appendChild(span("seg-count", String(count)));
    btn.setAttribute("aria-pressed", String(state.tab === tab));
    btn.addEventListener("click", () => {
      state.tab = tab;
      savePrefs();
      renderTabs();
      renderBoard();
    });
    nav.appendChild(btn);
  }
}

function matchesFilters(loop) {
  if (state.workspace && loop.root !== state.workspace) return false;
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return [loop.title, loop.name, loop.workspace, loop.mission, loop.status]
    .some((v) => String(v || "").toLowerCase().includes(q));
}

function visibleLoops() {
  const { key, dir } = state.sort;
  const sign = dir === "asc" ? 1 : -1;
  const value = (loop) => key === "loop" ? loopTitle(loop).toLowerCase()
    : key === "workspace" ? (loop.workspace + "/" + loop.name).toLowerCase()
    : String(loop.last_activity || "");
  return state.loops
    .filter((l) => matchesFilters(l) && matchesTab(state.tab, l))
    .sort((a, b) => {
      const running = Number(Boolean(b.running)) - Number(Boolean(a.running));
      if (key === "activity" && running !== 0) return running;
      return sign * value(a).localeCompare(value(b));
    });
}

function boardSignature(loops) {
  return JSON.stringify(
    loops.map((loop) => [
      loop.key,
      loop.title || "",
      loop.status ?? null,
      loop.final_verdict ?? null,
      Boolean(loop.running),
      loop.running_sources || [],
      loop.last_activity ?? null,
      loop.iteration,
      loop.max_iterations,
      verdictSeq(loop),
      loop.driver_phase ?? null,
      hasUnreadAttention(loop),
      state.activeLoop === loop.key,
    ])
  );
}

function renderBoard() {
  const body = el("loop-rows");
  const loops = state.loops.length ? visibleLoops() : [];
  const emptyText = !state.loops.length
    ? "No loop mailboxes found. Start one with /trio-init in a project."
    : loops.length ? "" : "No loops match these filters.";
  for (const th of document.querySelectorAll(".loop-table th[aria-sort]")) {
    const key = th.querySelector(".th-sort")?.dataset.sort;
    th.setAttribute("aria-sort", key === state.sort.key
      ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none");
  }
  const sig = state.tab + "|" + emptyText + "|" + boardSignature(loops);
  if (sig === state.boardSignature) return;
  state.boardSignature = sig;

  if (emptyText) {
    body.textContent = "";
    const tr = document.createElement("tr");
    tr.className = "row-empty";
    const td = document.createElement("td");
    td.colSpan = 6;
    td.appendChild(emptyState("○", emptyText));
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const stale of Array.from(body.querySelectorAll(".row-empty, .row-loading"))) stale.remove();

  const existing = new Map();
  for (const row of body.querySelectorAll(".loop-row")) existing.set(row.dataset.key, row);
  const wanted = new Set(loops.map((loop) => loop.key));
  for (const [key, row] of existing) {
    if (!wanted.has(key)) {
      row.remove();
      existing.delete(key);
    }
  }
  const ordered = loops.map((loop) => {
    const old = existing.get(loop.key);
    if (old) {
      patchCard(old, loop);
      return old;
    }
    return cardEl(loop);
  });
  let cursor = body.firstElementChild;
  for (const row of ordered) {
    if (row === cursor) {
      cursor = cursor.nextElementSibling;
      continue;
    }
    body.insertBefore(row, cursor);
  }
}

/* Rows patch in place: only cells whose content changed are replaced, so
 * focus and hover survive the 5 s poll. */
function patchCard(row, loop) {
  const fresh = cardEl(loop);
  row.className = fresh.className;
  const oldCells = Array.from(row.children);
  const newCells = Array.from(fresh.children);
  newCells.forEach((cell, i) => {
    if (!oldCells[i]) row.appendChild(cell);
    else if (!oldCells[i].isEqualNode(cell)) oldCells[i].replaceWith(cell);
  });
}

function cardEl(loop) {
  const row = document.createElement("tr");
  row.className = "loop-row" + (state.activeLoop === loop.key ? " active" : "") +
    (isArchived(loop) ? " is-archived" : "");
  row.dataset.key = loop.key;

  const nameCell = document.createElement("th");
  nameCell.scope = "row";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "row-open";
  open.textContent = loopTitle(loop);
  open.title = loop.mission || loopTitle(loop);
  open.addEventListener("click", () => openDrawer(loop.key));
  nameCell.appendChild(open);
  const sub = document.createElement("span");
  sub.className = "row-sub mono";
  sub.textContent = loop.name;
  nameCell.appendChild(sub);
  if (hasUnreadAttention(loop)) {
    const n = state.inbox.filter((i) => i.key === loop.key && !i.read).length;
    nameCell.appendChild(span("row-flag", plural(n, "unread item")));
  }
  if (isArchived(loop)) nameCell.appendChild(span("row-flag row-flag-muted", "Archived"));
  row.appendChild(nameCell);

  const ws = document.createElement("td");
  ws.className = "col-ws";
  ws.textContent = loop.workspace;
  ws.title = loop.root;
  row.appendChild(ws);

  const st = document.createElement("td");
  st.appendChild(stateBadge(loop));
  const phase = phaseText(loop);
  if (phase) st.appendChild(span("row-sub", phase));
  row.appendChild(st);

  const it = document.createElement("td");
  it.className = "num col-iter";
  it.textContent = iterationText(loop);
  row.appendChild(it);

  const hist = document.createElement("td");
  hist.className = "col-hist";
  hist.appendChild(historyEl(loop));
  row.appendChild(hist);

  const act = document.createElement("td");
  act.className = "num";
  const time = document.createElement("time");
  time.dateTime = loop.last_activity || "";
  time.textContent = loop.last_activity ? relTime(loop.last_activity) : "—";
  time.title = loop.last_activity ? new Date(loop.last_activity).toLocaleString() : "No activity recorded";
  act.appendChild(time);
  row.appendChild(act);

  row.addEventListener("click", (event) => {
    if (event.target.closest("button, a")) return;
    openDrawer(loop.key);
  });
  return row;
}

async function setInboxRead(items, read) {
  const byRoot = new Map();
  for (const item of items) {
    if (!byRoot.has(item.root)) byRoot.set(item.root, []);
    byRoot.get(item.root).push(item.id);
  }
  try {
    for (const [root, ids] of byRoot) {
      const response = await fetch("/api/inbox/" + (read ? "read" : "unread"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, root }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
    }
    for (const item of items) item.read = read;
    renderAll();
  } catch (err) {
    showBoardError("Could not update the attention list: " + err.message);
  }
}

function renderDrawerInbox() {
  const section = el("drawer-inbox-section");
  const list = el("drawer-inbox-list");
  if (!section || !list) return;
  list.textContent = "";
  const items = state.inbox.filter((item) => item.key === state.activeLoop);
  section.hidden = items.length === 0;
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "attn-row attn-row-compact" + (item.read ? " is-read" : "");
    row.appendChild(severityBadge(item));
    const main = document.createElement("div");
    main.className = "attn-main";
    main.appendChild(span("attn-reason", item.headline || ""));
    if (item.detail) main.appendChild(span("attn-detail", item.detail));
    row.appendChild(main);
    row.appendChild(inboxMarkButton(item));
    list.appendChild(row);
  }
}

async function controlLoop(action) {
  const loop = state.byKey.get(state.activeLoop);
  if (!loop) return;
  const driver = loop.driver === "omnigent" ? "omnigent" : "portable";
  if (action === "stop" && !window.confirm("Stop " + loopTitle(loop) + "? The running " + driver + " driver receives SIGTERM.")) return;
  const note = el("loop-control-note");
  note.textContent = action === "start" ? "Starting…" : "Stopping…";
  try {
    const res = await fetch("/api/loop/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: loop.root, driver }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    note.textContent = action === "start" ? "Start requested (pid " + (data.pid ?? "?") + ")." : "Stop requested.";
    await refreshBoard();
  } catch (err) {
    note.textContent = "Loop " + action + " failed: " + err.message;
  }
}

/* Start/Stop act on the workspace's `loop/` mailbox only (the API has no
 * mailbox parameter), so they are offered for that mailbox alone. */
function renderDrawerControls(loop) {
  const wrap = el("drawer-controls");
  if (!loop) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const controllable = loop.name === "loop";
  const start = el("loop-start");
  const stop = el("loop-stop");
  start.disabled = !controllable || Boolean(loop.running);
  stop.disabled = !controllable || !loop.running;
  const why = !controllable
    ? "Start and stop work on a workspace's loop/ mailbox only; run this one from its session."
    : loop.running ? "Loop is live; stop sends SIGTERM to its driver." : "Starts the " + (loop.driver === "omnigent" ? "omnigent" : "portable") + " driver for this mailbox.";
  start.title = why;
  stop.title = why;
  if (!el("loop-control-note").dataset.busy) el("loop-control-note").textContent = controllable ? "" : why;
}

function markActiveCard() {
  for (const row of document.querySelectorAll(".loop-row")) {
    row.classList.toggle("active", row.dataset.key === state.activeLoop);
  }
}

/* ------------------------------ drawer ------------------------------ */

/* The drawer is modal: the board behind it leaves the tab order and the
 * accessibility tree while it is open. */
function setBackgroundInert(on) {
  for (const node of document.querySelectorAll(".appbar, #main, .skip-link")) {
    node.inert = on;
  }
}

async function openDrawer(key) {
  const loop = state.byKey.get(key);
  if (!loop) return;
  if (!el("drawer").hidden && state.activeLoop === key) return;
  closeStream();
  state.activeLoop = key;
  state.returnFocus = document.activeElement;
  currentRoot = loop.root;
  const name = loop.name;
  history.replaceState(null, "", "#root=" + encodeURIComponent(loop.root) +
    "&loop=" + encodeURIComponent(name));
  state.detail = null;
  state.compare = [];
  state.sessions = [];
  state.activePath = null;
  state.drawerTab = "overview";
  state.graphSel = null;

  el("drawer-name").textContent = loopTitle(loop);
  el("drawer-where").textContent = loop.workspace + " / " + name;
  el("drawer-where").title = loop.root + "/" + name;
  el("loop-control-note").textContent = "";
  renderDrawerControls(loop);
  el("drawer-badge").className = "status-tags";
  el("drawer-badge").textContent = "LOADING";
  el("drawer-mission").textContent = "";
  el("fact-iter").textContent = "—";
  el("fact-verdict").textContent = "—";
  el("fact-verdict").className = "";
  el("fact-activity").textContent = "—";
  el("fact-sessions").textContent = "—";
  el("drawer-strip").textContent = "";
  el("drawer-timeline").textContent = "";
  el("drawer-timeline").appendChild(span("timeline-empty", "Loading…"));
  el("session-list").textContent = "";
  setPaneStatus("idle", "loading…");
  renderTranscript();

  el("drawer").hidden = false;
  el("drawer-scrim").hidden = false;
  setBackgroundInert(true);
  el("drawer-close").focus();
  markActiveCard();
  renderDrawerTabs();
  showDrawerTab();
  renderDrawerInbox();
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
  setBackgroundInert(false);
  history.replaceState(null, "", location.pathname);
  markActiveCard();
  const back = state.returnFocus;
  state.returnFocus = null;
  if (back && document.contains(back)) back.focus();
}

async function refreshDetail({ quiet }) {
  const key = state.activeLoop;
  const loop = state.byKey.get(key);
  if (!loop) return;
  renderDrawerControls(loop);
  try {
    const res = await fetch(withRoot("/api/loop?name=" + encodeURIComponent(loop.name)), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    if (state.activeLoop !== key) return;
    const detail = await res.json();
    state.detail = detail;
    state.sessions = Array.isArray(detail.sessions) ? detail.sessions : [];
    renderDetail(detail);
    if (!quiet) renderSessionList();
  } catch (err) {
    if (!quiet) {
      el("drawer-timeline").textContent = "";
      el("drawer-timeline").appendChild(
        span("timeline-empty", "Failed to load detail: " + err.message)
      );
    }
  }
}

/* --------------------- iteration lifecycle + compare --------------------- */

const LIFECYCLES = [
  "planned", "in_flight", "pending_eval", "shipped", "abandoned",
  "building", "retired", "faulted", "repairing",
];

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

/* Chip from a lifecycle STRING directly (slice rows use this; iteration
 * chips go through lifecycleChip(n), which delegates here). */
function lifecycleChipFor(st) {
  const s = String(st || "");
  return span("lifecycle-chip lifecycle-" + s, s.replace("_", " "));
}

function lifecycleChip(n) {
  return lifecycleChipFor(iterLifecycle(n));
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
  const badge = el("drawer-badge");
  badge.className = "status-tags";
  badge.textContent = "";
  const live = state.byKey.get(state.activeLoop) || {};
  badge.appendChild(stateBadge(Object.assign({}, detail, {
    running: live.running ?? detail.running,
    running_sources: live.running_sources || detail.running_sources,
  })));

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
  if (seq.length) {
    strip.appendChild(historyEl(detail));
    strip.appendChild(span("caption", " S shipped · I iterate · B blocked · H needs human, oldest first"));
  } else {
    strip.appendChild(span("caption", "No evaluator verdicts parsed from LOG.md. Latest recorded verdict: " +
      (detail.final_verdict ? String(detail.final_verdict).toUpperCase() : "none") + "."));
  }

  const commits = Array.isArray(detail.commits) ? detail.commits : [];
  const csec = el("commits-section");
  const clist = el("commit-list");
  clist.textContent = "";
  csec.hidden = commits.length === 0;
  const COMMIT_PREVIEW = 8;
  const showAll = state.showAllCommits === state.activeLoop;
  for (const c of showAll ? commits : commits.slice(0, COMMIT_PREVIEW)) {
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
  if (commits.length > COMMIT_PREVIEW) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "btn btn-ghost btn-small";
    more.textContent = showAll ? "Show fewer commits" : "Show all " + commits.length + " commits";
    more.addEventListener("click", () => {
      state.showAllCommits = showAll ? null : state.activeLoop;
      renderDetail(state.detail);
    });
    clist.appendChild(more);
  }

  renderSlices(detail);

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

/* Slices section (Overview): one row per PLAN.md slice, merged with its
 * derived lifecycle keys (see the /api/loop `slices` contract). */
function renderSlices(detail) {
  const slices = Array.isArray(detail.slices) ? detail.slices : [];
  const ssec = el("slices-section");
  const slist = el("slice-list");
  slist.textContent = "";
  ssec.hidden = slices.length === 0;
  for (const s of slices) {
    if (!s) continue;
    const row = document.createElement("div");
    row.className = "slice-row";
    row.appendChild(span("slice-id mono", String(s.id ?? "")));
    row.appendChild(lifecycleChipFor(s.lifecycle));
    if (s.retired_sha) {
      const sha = span("slice-sha mono", String(s.retired_sha).slice(0, 7));
      sha.title = String(s.retired_sha);
      row.appendChild(sha);
    }
    if (s.verdict) {
      row.appendChild(
        span(
          "verdict-word verdict-" + normVerdict(s.verdict),
          String(s.verdict).toUpperCase()
        )
      );
    }
    if (Array.isArray(s.open_faults)) {
      for (const fid of s.open_faults) {
        row.appendChild(span("meta-chip fault-chip", String(fid)));
      }
    }
    slist.appendChild(row);
  }
}

/* Canonical display order for the Timeline slice-count summary — also the
 * order the accept regex `[0-9]+ (shipped|building|retired|faulted|
 * repairing|planned)` lists them in. */
const SLICE_LIFECYCLE_ORDER = ["shipped", "building", "retired", "faulted", "repairing", "planned"];

/* "3 shipped · 1 faulted" for the slices of one iteration, open-loop only.
 * Reads state.detail directly (same pattern as renderTimelineView etc.)
 * since renderTimeline(entries) is only ever called with detail.timeline. */
function sliceCountSummary(iterNum) {
  const slices =
    state.detail && Array.isArray(state.detail.slices) ? state.detail.slices : [];
  const counts = new Map();
  for (const s of slices) {
    if (!s || s.iteration == null || Number(s.iteration) !== Number(iterNum)) continue;
    const st = String(s.lifecycle || "");
    counts.set(st, (counts.get(st) || 0) + 1);
  }
  const parts = [];
  for (const st of SLICE_LIFECYCLE_ORDER) {
    const n = counts.get(st);
    if (n) parts.push(n + " " + st);
  }
  for (const [st, n] of counts) {
    if (!SLICE_LIFECYCLE_ORDER.includes(st)) parts.push(n + " " + st);
  }
  return parts.join(" · ");
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
    if (iter != null) {
      hdr.appendChild(lifecycleChip(iter));
      if (state.detail && state.detail.mode === "open-loop") {
        const summary = sliceCountSummary(iter);
        if (summary) hdr.appendChild(span("iter-slice-summary", summary));
      }
    }
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

  const labelEl = span("session-label", label);
  labelEl.title = s.label ?? "";
  item.appendChild(labelEl);
  item.appendChild(span("session-meta", meta.join(" · ")));
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
    withRoot("/api/transcript?path=" + encodeURIComponent(path) + "&offset=0")
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
      showTranscriptNotice("Stream error: " + msg);
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

function parseHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const name = params.get("loop");
  return name ? { root: params.get("root") || "", name } : null;
}

function trapDrawerFocus(ev) {
  if (ev.key !== "Tab" || el("drawer").hidden) return;
  const focusable = Array.from(el("drawer").querySelectorAll(
    'button:not([disabled]), [href], input, select, [tabindex]:not([tabindex="-1"])'
  )).filter((node) => node.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault();
    first.focus();
  }
}

function init() {
  loadPrefs();
  pendingLoopHash = parseHash();

  el("drawer-close").addEventListener("click", closeDrawer);
  el("drawer-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !el("drawer").hidden) closeDrawer();
    trapDrawerFocus(ev);
  });
  el("inbox-toggle").addEventListener("click", () => {
    state.showReadInbox = !state.showReadInbox;
    renderAll();
  });
  el("follow-btn").addEventListener("click", () => {
    state.follow = !state.follow;
    updateFollowBtn();
  });
  window.addEventListener("hashchange", () => {
    const target = parseHash();
    if (!target) {
      if (!el("drawer").hidden) closeDrawer();
      return;
    }
    const loop = state.loaded ? resolveHashLoop(target) : null;
    if (loop) openDrawer(loop.key);
    else pendingLoopHash = target;
  });
  el("board-retry").addEventListener("click", refreshBoard);
  el("loop-start").addEventListener("click", () => controlLoop("start"));
  el("loop-stop").addEventListener("click", () => controlLoop("stop"));
  el("loop-search").addEventListener("input", (ev) => {
    state.query = ev.target.value;
    renderTabs();
    renderBoard();
  });
  el("workspace-filter").addEventListener("change", (ev) => {
    state.workspace = ev.target.value;
    savePrefs();
    renderTabs();
    renderBoard();
  });
  for (const btn of document.querySelectorAll(".th-sort")) {
    btn.addEventListener("click", () => {
      const key = btn.dataset.sort;
      state.sort = state.sort.key === key
        ? { key, dir: state.sort.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "activity" ? "desc" : "asc" };
      savePrefs();
      renderBoard();
    });
  }
  /* Relative times ("4m ago") keep moving between polls. */
  setInterval(() => {
    if (!state.loaded) return;
    renderLive();
  }, 5000);
  refreshBoard();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
