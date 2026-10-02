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
  actionNotes: new Map(),
  broker: "disabled",

  /* drawer */
  activeLoop: null,
  boardSignature: null,
  detail: null,
  drawerTab: "overview",
  graphSel: null,
  compare: [],
  tlOpen: new Set(),
  tlSignature: null,

  /* transcript stream (inside the drawer's sessions section) */
  sessions: [],
  activePath: null,
  es: null,
  offset: 0,
  size: null,
  records: 0,
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
  "error", "needs_retirement", "needs_land", "held", "conflict", "budget",
  "iteration_cap",
]);

/* Derived loop states (server: loop_actions.derive_state) shown as their
 * own badge instead of the raw STATE.md word. */
const LOOP_STATE_BADGES = {
  error: ["negative", "Error"],
  needs_retirement: ["warning", "Needs retirement"],
  needs_land: ["warning", "Needs land"],
  held: ["warning", "Held"],
  conflict: ["negative", "Conflict"],
  budget: ["warning", "Budget spent"],
  iteration_cap: ["warning", "Iteration cap"],
  answered: ["warning", "Answered — restart"],
};

function derivedStateOf(loop) {
  return loop && loop.loop_state ? loop.loop_state.state : null;
}
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };
const STORE_KEY = "trio.board.v2";
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const STALE_AFTER_MS = 30 * 1000;

function loopKey(root, name) {
  return root + "::" + name;
}

function isNeedsItem(item, loop) {
  // A low-severity "interrupted" means liveness was not fully checked
  // (broker unknown), so it is a note, not a call to act.
  if (item.severity === "low" && item.kind === "interrupted") return false;
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

const HUMAN_STATUS = new Set(["needs_human", "awaiting_human", "awaiting_user"]);

/* One state word per loop, from facts only: live evidence first, then a
 * STATE.md hand-off to a person (or blocked), then the latest verdict, then
 * the STATE.md status word. */
function stateBadge(loop) {
  const badge = document.createElement("span");
  let tone = "neutral";
  let text = statusWord(loop);
  const verdict = normVerdict(latestVerdict(loop));
  const status = String(loop.status || "").trim().toLowerCase().replace(/-/g, "_");
  const derived = derivedStateOf(loop);
  if (loop.running) {
    tone = "live";
    text = "Running";
  } else if (LOOP_STATE_BADGES[derived]) {
    [tone, text] = LOOP_STATE_BADGES[derived];
  } else if (HUMAN_STATUS.has(status)) {
    // STATE.md handing the loop to a person outranks an older verdict file.
    tone = "warning";
    text = "Needs human";
  } else if (status === "blocked") {
    tone = "negative";
    text = "Blocked";
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
  if (loop.loop_state && loop.loop_state.summary) facts.push(derived + ": " + loop.loop_state.summary);
  badge.title = facts.join(" · ");
  return badge;
}

function iterationText(loop) {
  if (loop.iteration == null) return "—";
  return loop.max_iterations != null
    ? loop.iteration + " of " + loop.max_iterations
    : String(loop.iteration);
}

/* The driver phase is only current while the loop is live; a stale
 * sidecar's phase ("lead done", "blocked") would contradict the badge. */
function phaseText(loop) {
  if (!loop.running) return "";
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
      loop.worktree = Boolean(ws.worktree);
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
  state.broker = data.broker || "disabled";
  state.worktreesScanned = data.worktrees_scanned || 0;
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
    if (state.activeLoop) {
      refreshDetail({ quiet: true });
      refreshActions();
    }
  } catch (err) {
    showBoardError(
      state.loaded
        ? "Lost contact with the dashboard server (" + err.message + "). Showing the last data received; retrying every 5 seconds."
        : "Cannot reach the dashboard server (" + err.message + "). Retrying every 5 seconds."
    );
    renderLive();
  }
  schedulePoll();
}

/* Poll every 5 s while visible; hidden tabs stop polling (the server only
 * rebuilds when someone asks). A build older than the server's rebuild
 * interval means a fresh one is on its way, so look again sooner, once. */
function schedulePoll() {
  clearTimeout(state.boardTimer);
  if (document.hidden) return;
  const age = ageMs(state.updatedAt);
  const soon = state.loaded && age != null && age > 18000 && !state.quickRepoll;
  state.quickRepoll = soon;
  state.boardTimer = setTimeout(refreshBoard, soon ? 1500 : BOARD_POLL_MS);
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

/* "2 via driver · 1 process only": distinct phases, else evidence counts. */
function runningContext(running) {
  const phases = Array.from(new Set(running.map(phaseText).filter(Boolean)));
  if (phases.length) return phases.slice(0, 2).join(", ") + (phases.length > 2 ? "…" : "");
  const byDriver = running.filter((l) => (l.running_sources || []).includes("driver")).length;
  const other = running.length - byDriver;
  return [byDriver ? byDriver + " via driver" : "", other ? other + " via process or session" : ""]
    .filter(Boolean).join(" · ");
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
      (running.length ? "; " + plural(running.length, "loop") + " running." : "; none running.");
  } else if (running.length) {
    verdict = "All clear: " + plural(running.length, "loop") + " running, nothing needs you.";
  } else {
    verdict = "All clear. No loop is running and nothing needs you.";
  }
  el("verdict").textContent = verdict;
  const latest = loops.slice().sort((a, b) =>
    String(b.last_activity || "").localeCompare(String(a.last_activity || "")))[0];
  const wtShown = state.workspaces.filter((w) => w.worktree).length;
  const coverage = "Covers " + plural(wsCount - wtShown, "workspace") +
    (wtShown ? " and " + plural(wtShown, "worktree") : "") + " with loops (" +
    plural(state.scanned, "workspace") + (state.worktreesScanned ? " and " + plural(state.worktreesScanned, "worktree") : "") +
    " scanned).";
  el("verdict-sub").textContent = (latest
    ? "Most recent activity: " + loopTitle(latest) + " (" + latest.workspace + "), " + relTime(latest.last_activity) + ". "
    : "Start a loop with /trio-init in a project; it appears here on the next poll. ") + coverage;
  const brokerNote = el("broker-note");
  const brokerText = {
    disabled: "Broker liveness is not configured, so loops driven only through Omnigent broker sessions cannot show as running.",
    unreachable: "The Omnigent broker did not answer; loops driven only through broker sessions may be running but show as not running.",
    truncated: "The Omnigent broker session list was too long to read in full; some broker-only loops may not show as running.",
  }[state.broker];
  brokerNote.hidden = !brokerText;
  brokerNote.textContent = brokerText || "";

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
    running.length ? runningContext(running) : "No live driver, process or session",
    running.length ? "live" : "", "#running");
  tile("Shipped, last 7 days", shipped7.length,
    plural(loops.filter((l) => normVerdict(latestVerdict(l)) === "ship").length, "shipped loop") + " in total", "", null);
  tile("Loops tracked", loops.length,
    "in " + plural(wsCount - wtShown, "workspace") + (wtShown ? " + " + plural(wtShown, "worktree") : ""), "", "#loops");
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
  el("notes-sub").textContent = "Drift, overlap, repair and unconfirmed-stop notes on " + plural(groups.notes.length, "loop") + " that are not running";
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
  const busy = state.workspaces.filter((w) => w.unattributed_processes > 0);
  for (const ws of busy) {
    const note = document.createElement("p");
    note.className = "run-note";
    note.textContent = plural(ws.unattributed_processes, "process", "processes") +
      " working in " + ws.name + " name no mailbox, so no loop there is marked running.";
    list.appendChild(note);
  }
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
  if (loop.driver === "claude-workflow") {
    const tag = span("row-flag row-flag-muted", "Native (claude-workflow)");
    tag.title = "Claude-native Trio loop driven by the trio-native Workflow";
    nameCell.appendChild(tag);
  }
  const diag = diagnosisFlag(loop.diagnosis);
  if (diag) nameCell.appendChild(diag);
  if (loop.worktree && (loop.worktree_reasons || []).length) {
    const tag = span("row-flag row-flag-muted", "Worktree: " + loop.worktree_reasons.join(", "));
    tag.title = "Shown because this linked worktree's copy is " + loop.worktree_reasons.join(", ") +
      "; untouched committed copies of the main checkout's mailboxes are hidden.";
    nameCell.appendChild(tag);
  }
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

/* One-line diagnosis status for a card (full result lives in the drawer). */
function diagnosisFlag(d) {
  if (!d || !d.status) return null;
  let text;
  let cls = "row-flag row-flag-muted";
  if (d.status === "running") {
    text = "Diagnosing (" + (d.harness || "?") + ")… " + (d.last_event || "");
    cls = "row-flag";
  } else if (d.status === "done") {
    text = "Diagnosis: " + (d.state || "?") + (d.proposed_fix && d.proposed_fix !== "none"
      ? " → " + d.proposed_fix + (d.proposed_fix_rejected ? " (rejected)" : "") : "")
      + (d.needs_human_input ? " · needs your input" : "");
    if (d.needs_human_input) cls = "row-flag";
  } else {
    text = "Diagnosis failed";
  }
  const flag = span(cls, oneLine(text, 90));
  flag.title = (d.harness || "") + " " + (d.model || "") + (d.finished_at ? " · " + d.finished_at : "")
    + (d.error ? " · " + d.error : "");
  return flag;
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

/* Action feedback survives polls: a local note (in flight, or refused
 * before the server recorded anything) wins until the server's own record
 * for the mailbox is newer. */
function actionNote(loop) {
  const local = state.actionNotes.get(loop.key);
  const server = loop.last_action;
  if (local && (!server || !server.updated_at ||
      Date.parse(server.updated_at) < local.at || local.pending)) {
    return local;
  }
  if (!server) return null;
  const tone = { failed: "error", exited: "error", running: "ok", finished: "ok", stopped: "ok", stopping: "pending" }[server.outcome] || "ok";
  const when = server.updated_at ? " · " + relTime(server.updated_at) : "";
  return { text: (server.message || server.outcome) + when, tone };
}

async function controlLoop(action) {
  const loop = state.byKey.get(state.activeLoop);
  if (!loop) return;
  const controls = loop.controls || {};
  const driver = controls.driver || "portable";
  if (!(controls[action] && controls[action].enabled)) return;
  if (action === "stop" && !window.confirm("Stop " + loopTitle(loop) + "? " + controls.stop.reason)) return;
  const key = loop.key;
  state.actionNotes.set(key, { text: action === "start" ? "Starting the " + driver + " driver…" : "Stopping…", tone: "pending", pending: true, at: Date.now() });
  renderDrawerControls(loop);
  let note;
  try {
    const res = await fetch("/api/loop/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: loop.root, driver }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    note = { text: action === "start" ? "Started (PID " + data.pid + ")." : "Stop requested for PID " + data.pid + ".", tone: "ok", at: Date.now() };
  } catch (err) {
    note = { text: "Loop " + action + " failed: " + err.message, tone: "error", at: Date.now() };
  }
  state.actionNotes.set(key, note);
  renderDrawerControls(state.byKey.get(key) || loop);
  await refreshBoard();
}

/* Capabilities come from the server (`controls`), which also enforces them. */
function renderDrawerControls(loop) {
  const wrap = el("drawer-controls");
  if (!loop) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const controls = loop.controls || {
    start: { enabled: false, reason: "Controls unavailable." },
    stop: { enabled: false, reason: "Controls unavailable." },
  };
  const pending = Boolean(state.actionNotes.get(loop.key)?.pending);
  const start = el("loop-start");
  const stop = el("loop-stop");
  start.disabled = pending || !controls.start.enabled;
  stop.disabled = pending || !controls.stop.enabled;
  start.title = controls.start.reason;
  stop.title = controls.stop.reason || controls.start.reason;
  const note = el("loop-control-note");
  const status = actionNote(loop);
  if (status) {
    note.textContent = status.text;
    note.className = "control-note control-" + status.tone;
    note.setAttribute("role", status.tone === "error" ? "alert" : "status");
  } else {
    note.textContent = controls.start.enabled ? "" : controls.start.reason;
    if (!controls.start.enabled && controls.stop.enabled) note.textContent = controls.stop.reason;
    note.className = "control-note caption";
    note.setAttribute("role", "status");
  }
}

/* ---------------------------- unblock panel ----------------------------
 * /api/loop/actions: derived state, the server's fix allowlist with each
 * fix's applicability, the latest read-only diagnosis, the answer box and
 * the append-only action log. The server re-checks every precondition on
 * each click; destructive fixes come back as confirm_required with the
 * exact commands, and only an explicit confirm click runs them. */

let actionsTimer = null;
const MAXIT_FIXES = new Set(["rerun", "rerun_more_iterations", "reset_and_rerun",
  "native_start", "native_reset_and_start"]);

function actionsUrl(loop) {
  return "/api/loop/actions?root=" + encodeURIComponent(loop.root) +
    "&loop=" + encodeURIComponent(loop.name);
}

async function refreshActions() {
  const key = state.activeLoop;
  const loop = state.byKey.get(key);
  if (!loop) return;
  clearTimeout(actionsTimer);
  try {
    const res = await fetch(actionsUrl(loop), { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (state.activeLoop !== key) return;
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    state.actions = data;
    renderActions(data);
  } catch (err) {
    if (state.activeLoop !== key) return;
    el("actions-section").hidden = false;
    el("actions-state").textContent = "Unblock actions unavailable: " + err.message;
  }
  const d = state.actions && state.actions.diagnosis;
  if (d && d.status === "running" && state.activeLoop === key) {
    actionsTimer = setTimeout(refreshActions, 2000);
  }
}

async function postLoopAction(path, body) {
  const loop = state.byKey.get(state.activeLoop);
  if (!loop) return { status: 0, data: { error: "no loop open" } };
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ root: loop.root, loop: loop.name }, body)),
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data };
}

function confirmCommands({ title, lead, commands, notes, okText }) {
  const dlg = el("confirm-dialog");
  el("confirm-title").textContent = title || "Confirm";
  el("confirm-lead").textContent = lead || "";
  el("confirm-commands").textContent = (commands || []).join("\n");
  const list = el("confirm-notes");
  list.textContent = "";
  for (const note of notes || []) {
    const li = document.createElement("li");
    li.textContent = note;
    list.appendChild(li);
  }
  el("confirm-ok").textContent = okText || "Run these commands";
  dlg.returnValue = "";
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    dlg.showModal();
  });
}

function setNote(id, text, tone) {
  const note = el(id);
  note.textContent = text || "";
  note.className = "control-note caption" + (tone ? " control-" + tone : "");
}

/* Workflow script facts of a claude-workflow loop (server view model
 * native_script: recorded = what the last run ran, next = what Start/Resume
 * would run now). Pure: one {label, text, warn} row per line; any missing
 * field reads "unknown". Rendered only through textContent. */
function nativeScriptRows(view) {
  if (!view || typeof view !== "object") return [];
  const known = (v) => (v === null || v === undefined || v === "" ? "unknown" : String(v));
  const release = (v) => (v === true ? "release ✓" : v === false ? "not the installed release" : "release: unknown");
  const describe = (s) => {
    s = s && typeof s === "object" ? s : {};
    const path = s.scope === "none" ? "none found" : known(s.path);
    return {
      text: path + " · scope " + known(s.scope) + " · " + release(s.is_release) +
        " · sha " + known(s.sha_short),
      warn: s.is_release === false,
    };
  };
  const rows = [];
  const rec = describe(view.recorded);
  rows.push({ label: "Workflow script (last run)", text: rec.text, warn: rec.warn });
  const next = view.next && typeof view.next === "object" ? view.next : {};
  const nx = describe(next);
  rows.push({ label: "Start/Resume would run", text: nx.text, warn: nx.warn });
  const rel = next.release && typeof next.release === "object" ? next.release : {};
  rows.push({ label: "Installed release script",
    text: known(rel.path) + " · sha " + known(rel.sha_short), warn: false });
  return rows;
}

function renderNativeScript(view) {
  const list = el("actions-script");
  if (!list) return;
  list.textContent = "";
  const rows = nativeScriptRows(view);
  list.hidden = rows.length === 0;
  for (const row of rows) {
    const li = document.createElement("li");
    li.appendChild(span("script-label", row.label + ": "));
    li.appendChild(span(row.warn ? "mono script-warn" : "mono", (row.warn ? "⚠ " : "") + row.text));
    list.appendChild(li);
  }
}

function renderActions(data) {
  el("actions-section").hidden = false;
  renderNativeScript(data.driver === "claude-workflow" ? data.native_script : null);
  const st = data.state || {};
  const detail = st.detail || {};
  const parts = [(st.state || "unknown").replace(/_/g, " ")];
  if (st.summary) parts.push(st.summary);
  if (data.driver) parts.push("driver " + data.driver);
  if (detail.run_id) parts.push("run " + detail.run_id);
  if (detail.session_id) parts.push("session " + String(detail.session_id).slice(0, 8));
  if (detail.api_equiv_usd != null) parts.push("≈$" + Number(detail.api_equiv_usd).toFixed(2) + " API-equivalent");
  if (detail.acceptance) {
    const a = detail.acceptance;
    parts.push("frozen acceptance " + (a.checks || 0) + " checks" + (a.pin ? " pin " + a.pin : "") +
      (a.ship_gate ? ", SHIP gate " + a.ship_gate : "") +
      (a.coverage_refusals ? ", " + a.coverage_refusals + " coverage refusal(s)" : "") +
      (a.ship_refused ? ", " + a.ship_refused + " SHIP refused" : "") +
      (a.tamper_events ? ", " + a.tamper_events + " tamper restore(s)" : "") +
      (a.audit_limited ? ", author audit limited" : ""));
  }
  el("actions-state").textContent = parts.join(" · ");

  const sel = el("diagnose-harness");
  if (!sel.options.length && data.harnesses) {
    for (const name of ["codex", "cursor"]) {
      const h = data.harnesses[name];
      if (!h) continue;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = (name === "cursor" ? "Cursor · " : "Codex · ") + h.model +
        (h.effort ? " (" + h.effort + ")" : "") +
        (name === "cursor" ? " — not sandboxed, see warning" : " — OS read-only sandbox") +
        (h.available ? "" : " — not installed");
      if (h.warning) opt.title = h.warning;
      opt.disabled = !h.available;
      sel.appendChild(opt);
    }
    sel.value = data.harnesses.default;
  }
  const running = data.diagnosis && data.diagnosis.status === "running";
  el("diagnose-btn").disabled = running;
  el("diagnose-btn").textContent = running ? "Diagnosing…" : "Diagnose";
  renderDiagnosis(data.diagnosis);

  const proposed = (((data.diagnosis || {}).result || {}).proposed_fix || {}).id;
  const list = el("fix-list");
  const unavailable = (data.fixes || []).filter((fix) => !fix.applicable);
  // Rebuild the rows only when the fixes change: a poll must not wipe a
  // typed max-iterations value or a row's result note.
  const signature = JSON.stringify([data.loop, data.root, proposed, st.state, st.iteration,
    (data.fixes || []).filter((f) => f.applicable).map((f) => [f.id, f.commands_preview])]);
  if (signature !== state.fixSignature) {
    state.fixSignature = signature;
    list.textContent = "";
    for (const fix of data.fixes || []) {
      if (fix.applicable) list.appendChild(fixRow(fix, fix.id === proposed, st));
    }
    if (!list.children.length) list.appendChild(span("caption", "No fix applies right now."));
  }
  const ul = el("fix-unavailable-list");
  ul.textContent = "";
  for (const fix of unavailable) {
    const li = document.createElement("li");
    li.appendChild(span("mono", fix.id));
    li.appendChild(document.createTextNode(" — " + (fix.reason || "")));
    ul.appendChild(li);
  }
  el("fix-unavailable").hidden = unavailable.length === 0;

  const answer = data.answer || {};
  el("answer-box").hidden = !answer.allowed;
  el("answer-stop").textContent = answer.allowed
    ? "Stopped at: " + (answer.stop || "?") + (answer.entries && answer.entries.length
      ? " · earlier answers: " + answer.entries.length : "")
    : "";
  const reset = el("answer-reset");
  reset.disabled = answer.reset_allowed === false;
  if (reset.disabled) reset.checked = false;
  reset.parentElement.title = answer.reset_reason || "";

  const log = el("action-log-list");
  log.textContent = "";
  for (const entry of (data.log || []).slice(-20).reverse()) {
    const li = document.createElement("li");
    const who = entry.who ? (entry.who.user || entry.who.addr || "") : "";
    const what = [entry.action, entry.fix || entry.harness || entry.answer_id || "",
      entry.ok === false ? "FAILED" : entry.ok === true ? "ok" : "",
      entry.exit_code != null ? "exit " + entry.exit_code : "",
      entry.reason ? "— " + entry.reason : ""].filter(Boolean).join(" ");
    li.appendChild(span("mono", (entry.at || "").replace("T", " ").replace("Z", "")));
    li.appendChild(document.createTextNode(" " + what + (who ? " · " + who : "")));
    log.appendChild(li);
  }
}

function fixRow(fix, isProposed, st) {
  const row = document.createElement("div");
  row.className = "fix-row" + (isProposed ? " fix-proposed" : "");
  const head = document.createElement("div");
  head.className = "fix-head";
  head.appendChild(span("fix-title", fix.title));
  head.appendChild(span("fix-id mono caption", fix.id));
  if (fix.destructive) head.appendChild(span("fix-tag fix-tag-destructive", "needs confirm"));
  else if (fix.requires_confirm) head.appendChild(span("fix-tag", "needs confirm"));
  if (isProposed) head.appendChild(span("fix-tag", "proposed by diagnosis"));
  row.appendChild(head);
  if ((fix.commands_preview || []).length) {
    const pre = document.createElement("pre");
    pre.className = "fix-commands mono";
    pre.textContent = fix.commands_preview.join("\n");
    row.appendChild(pre);
  }
  for (const note of fix.notes || []) row.appendChild(span("caption fix-note", note));
  const bar = document.createElement("div");
  bar.className = "actions-row";
  let input = null;
  if (MAXIT_FIXES.has(fix.id)) {
    const label = document.createElement("label");
    label.className = "caption fix-arg";
    label.textContent = "max iterations ";
    input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.max = "200";
    input.className = "input input-num";
    const cur = Number(st.iteration || 0);
    const cap = Number(st.max_iterations || 0);
    input.value = String(fix.id === "rerun_more_iterations" || st.state === "iteration_cap"
      ? cur + Math.max(2, Math.min(cap || 4, 10)) : (cap > cur ? cap : cur + (cap || 4)));
    label.appendChild(input);
    bar.appendChild(label);
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn " + (fix.destructive ? "btn-destructive" : "btn-secondary");
  btn.textContent = fix.destructive || fix.requires_confirm ? "Review & apply…" : "Apply";
  const note = document.createElement("span");
  note.className = "control-note caption";
  note.setAttribute("role", "status");
  btn.addEventListener("click", () => {
    const args = {};
    if (input && input.value) args.max_iterations = Number(input.value);
    applyFix(fix, args, btn, note);
  });
  bar.appendChild(btn);
  bar.appendChild(note);
  row.appendChild(bar);
  return row;
}

async function applyFix(fix, args, btn, note) {
  btn.disabled = true;
  note.textContent = "Checking preconditions…";
  note.className = "control-note caption control-pending";
  try {
    let { status, data } = await postLoopAction("/api/loop/fix", { fix: fix.id, args });
    // A confirm carries the preview's token; when the loop changed in
    // between, the server answers with the new plan, shown again here.
    for (let round = 0; round < 3 && status === 409 && data.confirm_required; round++) {
      const plan = data.plan || {};
      const ok = await confirmCommands({
        title: plan.title || fix.title,
        lead: (data.plan_changed ? "The plan CHANGED since you last reviewed it (the loop state moved). " : "") +
          "The server re-checked the preconditions just now. Confirming runs exactly these commands" +
          (plan.destructive ? " (they change history or loop state):" : ":"),
        commands: plan.commands_preview, notes: plan.notes,
      });
      if (!ok) {
        note.textContent = "Cancelled; nothing ran.";
        note.className = "control-note caption";
        return;
      }
      ({ status, data } = await postLoopAction("/api/loop/fix",
        { fix: fix.id, args, confirm: true, confirm_token: plan.confirm_token }));
    }
    if (status >= 200 && status < 300 && data.ok) {
      const started = (data.results || []).filter((r) => r.detached);
      note.textContent = started.length
        ? "Started (PID " + started.map((r) => r.pid).join(", ") + "). Output: " + started[0].log
        : "Done.";
      note.className = "control-note caption control-ok";
    } else {
      const failed = (data.results || []).find((r) => !r.ok);
      note.textContent = "Not applied: " + (data.error || (failed && (failed.error || failed.output)) || "HTTP " + status);
      note.className = "control-note caption control-error";
    }
    setNote("fix-result", fix.id + ": " + note.textContent,
      note.className.includes("control-ok") ? "ok" : "error");
  } catch (err) {
    note.textContent = "Request failed: " + err.message;
    note.className = "control-note caption control-error";
  } finally {
    btn.disabled = false;
    refreshActions();
    refreshBoard();
  }
}

function renderDiagnosis(d) {
  const panel = el("diagnosis-panel");
  panel.textContent = "";
  if (!d) { panel.hidden = true; return; }
  panel.hidden = false;
  const head = document.createElement("div");
  head.className = "diagnosis-head";
  head.appendChild(span("fix-title", "Diagnosis"));
  head.appendChild(span("caption", [d.harness, d.model, d.effort].filter(Boolean).join(" · ") +
    " · " + (d.status || "") + (d.finished_at ? " · " + relTime(d.finished_at) : d.started_at ? " · started " + relTime(d.started_at) : "")));
  panel.appendChild(head);
  if (d.status === "running") {
    panel.appendChild(span("caption", "Read-only agent working… " + (d.events || 0) + " events · " + (d.last_event || "")));
    return;
  }
  if (d.status === "failed") {
    panel.appendChild(span("control-note control-error", "Failed: " + (d.error || "unknown error")));
    if (d.answer_tail) {
      const pre = document.createElement("pre");
      pre.className = "fix-commands mono";
      pre.textContent = d.answer_tail;
      panel.appendChild(pre);
    }
    return;
  }
  const r = d.result || {};
  const text = document.createElement("p");
  text.className = "diagnosis-text";
  text.textContent = r.diagnosis || "(no diagnosis text)";
  panel.appendChild(text);
  panel.appendChild(span("caption", "State: " + (r.state || "?") + (r.state_note ? " (" + r.state_note + ")" : "")));
  if ((r.evidence || []).length) {
    const ul = document.createElement("ul");
    ul.className = "diagnosis-evidence";
    for (const e of r.evidence) {
      const li = document.createElement("li");
      li.textContent = e;
      ul.appendChild(li);
    }
    panel.appendChild(ul);
  }
  const fix = r.proposed_fix || {};
  const fixLine = document.createElement("p");
  fixLine.className = "diagnosis-fix";
  fixLine.textContent = "Proposed fix: " + (fix.id || "none") +
    (fix.rejected ? " — REJECTED by the server (" + fix.rejected + ")" : "") +
    (fix.server_check ? " — server: " + fix.server_check : "");
  panel.appendChild(fixLine);
  // Only commands the server planned itself are shown as commands.
  const cmds = fix.server_commands || [];
  if (cmds.length) {
    panel.appendChild(span("caption", "Server-validated commands:"));
    const pre = document.createElement("pre");
    pre.className = "fix-commands mono";
    pre.textContent = cmds.join("\n");
    panel.appendChild(pre);
  }
  const said = fix.agent_commands_preview || [];
  if (said.length) {
    const box = document.createElement("details");
    box.className = "agent-said";
    const sum = document.createElement("summary");
    sum.textContent = "Agent said (unverified agent text — never run, not what a fix executes)";
    box.appendChild(sum);
    const pre = document.createElement("pre");
    pre.className = "mono";
    pre.textContent = said.join("\n");
    box.appendChild(pre);
    panel.appendChild(box);
  }
  if (r.needs_human_input) {
    const q = document.createElement("p");
    q.className = "diagnosis-question";
    q.textContent = "Needs your input: " + (r.question || "(no question given)");
    panel.appendChild(q);
  }
  if (d.integrity && d.integrity.warning) {
    panel.appendChild(span("control-note control-error", d.integrity.warning + ": " + d.integrity.changed.join(", ")));
  }
}

async function startDiagnosis() {
  const harness = el("diagnose-harness").value || undefined;
  el("diagnose-btn").disabled = true;
  setNote("diagnose-note", "Starting a read-only diagnosis…", "pending");
  try {
    let { status, data } = await postLoopAction("/api/loop/diagnose", { harness });
    if (status === 409 && data.accept_exposure_required) {
      const ok = await confirmCommands({
        title: "Cursor is not sandboxed",
        lead: data.warning || "",
        commands: [], okText: "Accept and run Cursor",
      });
      if (!ok) { setNote("diagnose-note", "Cancelled; use Codex for a sandboxed diagnosis."); return; }
      ({ status, data } = await postLoopAction("/api/loop/diagnose", { harness, accept_exposure: true }));
    }
    if (status === 202) setNote("diagnose-note", "Running (" + data.diagnosis.harness + " · " + data.diagnosis.model + ").", "ok");
    else if (status === 429) setNote("diagnose-note", "Busy: " + (data.error || "too many diagnoses"), "error");
    else setNote("diagnose-note", "Not started: " + (data.error || "HTTP " + status), "error");
  } catch (err) {
    setNote("diagnose-note", "Request failed: " + err.message, "error");
  }
  refreshActions();
}

async function submitAnswer(ev) {
  ev.preventDefault();
  const answer = el("answer-text").value;
  const reset = el("answer-reset").checked;
  setNote("answer-note", "Checking…", "pending");
  el("answer-restart").hidden = true;
  try {
    let { status, data } = await postLoopAction("/api/loop/answer", { answer, reset });
    for (let round = 0; round < 3 && status === 409 && data.confirm_required; round++) {
      const plan = data.plan || {};
      const ok = await confirmCommands({
        title: "Record this answer",
        lead: (data.plan_changed ? "The loop CHANGED since the preview. " : "") +
          "Appends this entry to HUMAN.md" + (plan.reset ? " and resets STATE.md:" : ":"),
        commands: [plan.entry || "", ""].concat(plan.commands_preview || []),
        okText: "Write answer",
      });
      if (!ok) { setNote("answer-note", "Cancelled; nothing was written."); return; }
      ({ status, data } = await postLoopAction("/api/loop/answer",
        { answer, reset, confirm: true, confirm_token: plan.confirm_token }));
    }
    if (status === 200 && data.ok) {
      el("answer-text").value = "";
      setNote("answer-note", "Answer " + data.answer_id + " recorded in HUMAN.md" + (reset ? "; STATE reset." : "."), "ok");
      const wrap = el("answer-restart");
      wrap.textContent = "";
      for (const fix of data.restart || []) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary";
        btn.textContent = "Restart: " + fix.title;
        const note = document.createElement("span");
        note.className = "control-note caption";
        btn.addEventListener("click", () => applyFix(fix, {}, btn, note));
        wrap.appendChild(btn);
        wrap.appendChild(note);
      }
      wrap.hidden = !(data.restart || []).length;
    } else {
      setNote("answer-note", "Not written: " + (data.error || "HTTP " + status), "error");
    }
  } catch (err) {
    setNote("answer-note", "Request failed: " + err.message, "error");
  }
  refreshActions();
  refreshBoard();
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
  state.tlOpen.clear();
  state.tlSignature = null;
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
  state.offset = 0;
  state.size = null;
  state.records = 0;
  setOffsetHint();
  renderTranscript();

  el("drawer").hidden = false;
  el("drawer-scrim").hidden = false;
  setBackgroundInert(true);
  el("drawer-close").focus();
  markActiveCard();
  renderDrawerTabs();
  showDrawerTab();
  renderDrawerInbox();
  state.actions = null;
  state.fixSignature = null;
  setNote("fix-result", "");
  el("actions-section").hidden = true;
  el("diagnose-harness").textContent = "";
  setNote("diagnose-note", "");
  setNote("answer-note", "");
  el("answer-restart").hidden = true;
  refreshActions();
  await refreshDetail({ quiet: false });
}

function closeDrawer() {
  closeStream();
  clearTimeout(actionsTimer);
  state.actions = null;
  state.activeLoop = null;
  state.detail = null;
  state.compare = [];
  state.tlOpen.clear();
  state.tlSignature = null;
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
    const before = sessionSignature(state.sessions);
    state.sessions = Array.isArray(detail.sessions) ? detail.sessions : [];
    renderDetail(detail);
    if (!quiet || sessionSignature(state.sessions) !== before) renderSessionList();
    if (state.drawerTab === "transcripts") openDefaultSession();
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
  const label = s.replace("_", " ");
  return span("lifecycle-chip lifecycle-" + s, label.charAt(0).toUpperCase() + label.slice(1));
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
    // r15: the declared repo a slice belongs to (absent/"."/"home" = the
    // mailbox repo, shown as nothing, as before).
    const repo = typeof s.repo === "string" ? s.repo.trim() : "";
    if (repo && repo !== "." && repo !== "home") {
      const chip = span("meta-chip slice-repo mono", repo);
      chip.title = "repo: " + repo;
      row.appendChild(chip);
    }
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
    btn.textContent = tab.charAt(0).toUpperCase() + tab.slice(1);
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
  if (state.drawerTab === "transcripts") openDefaultSession();
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



/* --------------------------- Timeline view --------------------------- */

/* Iteration cards in the board's vocabulary: a header with verdict badge,
 * lifecycle and a Compare toggle; a proportional time track when entries
 * carry timings (legend above, tooltip per segment); one row per LOG entry
 * with a right-aligned duration; PLAN.md slice chips that open the graph. */
const TL_ROLES = ["lead", "builder", "evaluator", "repair"];

function verdictTone(v) {
  return { ship: "positive", needs_human: "warning", blocked: "negative" }[v] || "neutral";
}

function verdictBadge(raw) {
  const v = normVerdict(raw);
  const tone = verdictTone(v);
  const b = span("badge badge-" + tone, "");
  b.appendChild(span("badge-icon", { positive: "✓", warning: "!", negative: "✕", neutral: "↻" }[tone]));
  b.appendChild(document.createTextNode(
    { ship: "Ship", iterate: "Iterate", blocked: "Blocked", needs_human: "Needs human" }[v] ||
    String(raw)));
  return b;
}

function tlRoleClass(role) {
  return TL_ROLES.includes(role) ? role : "other";
}

function renderTimelineView() {
  const view = el("view-timeline");
  if (view.hidden) return;
  /* The detail poll re-renders every 5 s. Skip when nothing the timeline
   * shows has changed, so focused controls are not replaced; when it has,
   * focus returns to the same control (by data-focus-key) without scrolling. */
  const signature = JSON.stringify([state.detail, state.compare]);
  if (signature === state.tlSignature && view.childElementCount) {
    requestAnimationFrame(() => syncSummaryToggles(view));
    return;
  }
  state.tlSignature = signature;
  const focused = view.contains(document.activeElement)
    ? document.activeElement.dataset.focusKey : null;
  renderTimelineContent(view);
  if (focused) {
    const again = [...view.querySelectorAll("[data-focus-key]")]
      .find((n) => n.dataset.focusKey === focused);
    if (again) again.focus({ preventScroll: true });
  }
}

function renderTimelineContent(view) {
  view.textContent = "";
  if (!state.detail) {
    appendEmpty(view, "Loading timeline…");
    return;
  }
  const entries = Array.isArray(state.detail.timeline) ? state.detail.timeline : [];
  if (!entries.length) {
    appendEmpty(view, "No LOG.md entries yet. Each role appends one line per pass; they appear here as iterations.");
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
  const rows = iters.map((it) => ({
    it,
    entries: entries.filter(
      (e) => (e.iteration == null ? null : Number(e.iteration)) === it
    ),
    slices: slices.filter(
      (sl) => sl && sl.iteration != null && Number(sl.iteration) === it
    ),
  }));
  const timed = entries.some((e) => tlSpan(e));

  const head = document.createElement("div");
  head.className = "tl-head";
  head.appendChild(span("caption",
    rows.length + (rows.length === 1 ? " iteration · " : " iterations · ") +
    entries.length + (entries.length === 1 ? " entry" : " entries") +
    (rows.some((r) => r.it != null) ? " · select two iterations to compare" : "")));
  if (timed) {
    const legend = document.createElement("div");
    legend.className = "tl-legend";
    legend.setAttribute("aria-label", "Role colours");
    for (const role of [...TL_ROLES, "other"]) {
      const item = span("tl-legend-item", "");
      item.appendChild(span("tl-swatch tl-role-" + role, ""));
      item.appendChild(document.createTextNode(role));
      legend.appendChild(item);
    }
    head.appendChild(legend);
  }
  view.appendChild(head);

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

  if (state.compare.length) view.appendChild(comparePanel());

  for (const row of rows) view.appendChild(timelineIteration(row));
  requestAnimationFrame(() => syncSummaryToggles(view));
}

/* Summaries clamp to three lines. A "Show more" button (keyboard and touch,
 * not a hover-only tooltip) expands one in place; it is hidden when the text
 * already fits. Open state survives the detail poll's re-render. */
const TL_SUMMARY_TOGGLE_MIN = 80;
let tlSummarySeq = 0;

function timelineSummary(text, key) {
  const frag = document.createDocumentFragment();
  const body = span("tl-summary", text || "—");
  frag.appendChild(body);
  if (text.length < TL_SUMMARY_TOGGLE_MIN) return frag;
  body.id = "tl-summary-" + ++tlSummarySeq;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-ghost btn-small tl-more";
  btn.dataset.focusKey = "more:" + key;
  btn.setAttribute("aria-controls", body.id);
  const setOpen = (open) => {
    body.classList.toggle("is-open", open);
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? "Show less" : "Show more";
  };
  setOpen(state.tlOpen.has(key));
  btn.addEventListener("click", () => {
    const open = !state.tlOpen.has(key);
    if (open) state.tlOpen.add(key);
    else state.tlOpen.delete(key);
    setOpen(open);
  });
  frag.appendChild(btn);
  return frag;
}

function syncSummaryToggles(view) {
  if (!view || view.hidden) return;
  for (const btn of view.querySelectorAll(".tl-more")) {
    const body = document.getElementById(btn.getAttribute("aria-controls"));
    if (!body || body.classList.contains("is-open")) continue;
    btn.hidden = body.scrollHeight <= body.clientHeight + 1;
  }
}

let tlResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(tlResizeTimer);
  tlResizeTimer = setTimeout(() => syncSummaryToggles(el("view-timeline")), 150);
});

function timelineIteration(row) {
  const card = document.createElement("section");
  card.className = "tl-iter";
  const selected = row.it != null && state.compare.includes(row.it);
  if (selected) card.classList.add("is-selected");

  const head = document.createElement("header");
  head.className = "tl-iter-head";
  const title = document.createElement("h3");
  title.className = "tl-iter-title";
  title.textContent = row.it == null ? "Unattributed" : "Iteration " + row.it;
  head.appendChild(title);
  const vEntry = [...row.entries].reverse().find((e) => e.verdict);
  if (vEntry) head.appendChild(verdictBadge(vEntry.verdict));
  if (row.it != null) head.appendChild(lifecycleChip(row.it));
  const spans = row.entries.map(tlSpan).filter(Boolean);
  const total = row.entries.reduce(
    (acc, e) => acc + (Number.isFinite(Number(e.duration_sec)) ? Number(e.duration_sec) : 0), 0);
  const meta = [row.entries.length + (row.entries.length === 1 ? " entry" : " entries")];
  if (total > 0) meta.push(fmtDuration(total) + " logged");
  head.appendChild(span("tl-iter-meta", meta.join(" · ")));
  if (row.it != null) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-small tl-compare";
    btn.dataset.focusKey = "compare:" + row.it;
    btn.textContent = selected ? "Comparing" : "Compare";
    btn.setAttribute("aria-pressed", String(selected));
    btn.setAttribute("aria-label", "Compare iteration " + row.it);
    btn.title = "Select up to two iterations to compare side by side";
    btn.addEventListener("click", () => toggleCompare(row.it));
    head.appendChild(btn);
  }
  card.appendChild(head);

  if (spans.length) card.appendChild(timelineTrack(row.entries, spans));

  const list = document.createElement("ol");
  list.className = "tl-entries";
  for (const entry of row.entries) {
    const li = document.createElement("li");
    li.className = "tl-entry";
    const role = tlRole(entry);
    li.appendChild(span("tl-role tl-role-" + tlRoleClass(role), role));
    const text = document.createElement("div");
    text.className = "tl-entry-text";
    let summary = String(entry.summary || "");
    if (entry.verdict) {
      summary = summary.replace(
        /^VERDICT:\s*(SHIP|ITERATE|BLOCKED|NEEDS_HUMAN)\s*[—–-]\s*/i, "");
    }
    if (entry.slice) text.appendChild(span("chip", String(entry.slice)));
    if (entry.scope) {
      const sc = span("chip chip-warning", scopeLabel(entry.scope));
      sc.title = "Repair scope: " + String(entry.scope);
      text.appendChild(sc);
    }
    text.appendChild(timelineSummary(
      summary, row.it + "\u0000" + role + "\u0000" + summary));
    li.appendChild(text);
    const side = document.createElement("div");
    side.className = "tl-entry-side";
    if (entry.verdict) side.appendChild(verdictBadge(entry.verdict));
    side.appendChild(span("tl-dur", entry.duration_sec != null ? fmtDuration(entry.duration_sec) : "—"));
    li.appendChild(side);
    list.appendChild(li);
  }
  card.appendChild(list);

  if (row.slices.length) {
    const chips = document.createElement("div");
    chips.className = "tl-slices";
    chips.appendChild(span("caption", "Slices"));
    for (const sl of row.slices) {
      const id = String(sl.id ?? "");
      const st = normSliceStatus(sl.status);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip chip-btn chip-" + st;
      chip.dataset.focusKey = "slice:" + row.it + ":" + id;
      chip.textContent = id;
      chip.title =
        id + " · " + st.replace("_", " ") +
        " · writes: " + (Array.isArray(sl.writes) ? sl.writes.length : 0) +
        " · reads: " + (Array.isArray(sl.reads) ? sl.reads.length : 0) +
        " · open in graph";
      chip.addEventListener("click", () => {
        state.graphSel = id;
        state.drawerTab = "graph";
        renderDrawerTabs();
        showDrawerTab();
      });
      chips.appendChild(chip);
    }
    card.appendChild(chips);
  }
  return card;
}

/* One horizontal track for an iteration: each timed entry is a segment
 * positioned by wall clock; start/end clock labels underneath. */
function timelineTrack(entries, spans) {
  const minT = Math.min(...spans.map((sp) => sp.start));
  const maxT = Math.max(...spans.map((sp) => sp.end));
  const range = Math.max(1, maxT - minT);
  const wrap = document.createElement("div");
  wrap.className = "tl-track-wrap";
  const track = document.createElement("div");
  track.className = "tl-track";
  track.setAttribute("role", "img");
  track.setAttribute("aria-label", "Wall-clock span of each timed entry");
  for (const entry of entries) {
    const sp = tlSpan(entry);
    if (!sp) continue;
    const seg = document.createElement("span");
    seg.className = "tl-seg tl-role-" + tlRoleClass(tlRole(entry));
    seg.style.left = ((sp.start - minT) / range) * 100 + "%";
    seg.style.width = Math.max(0.8, ((sp.end - sp.start) / range) * 100) + "%";
    seg.title = entryTitle(entry);
    track.appendChild(seg);
  }
  wrap.appendChild(track);
  const axis = document.createElement("div");
  axis.className = "tl-axis";
  axis.appendChild(span("", fmtHm(minT)));
  axis.appendChild(span("", fmtDuration((maxT - minT) / 1000)));
  axis.appendChild(span("", fmtHm(maxT)));
  wrap.appendChild(axis);
  return wrap;
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
  clear.className = "btn btn-ghost btn-small";
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

  const head0 = document.createElement("div");
  head0.className = "tl-head";
  head0.appendChild(span("caption",
    rowKeys.length + (rowKeys.length === 1 ? " file" : " files") +
    " written by PLAN.md slices, per iteration"));
  const legend = document.createElement("div");
  legend.className = "tl-legend";
  const more = span("tl-legend-item", "");
  more.appendChild(span("tl-swatch heat-swatch", ""));
  more.appendChild(document.createTextNode("darker = more slices"));
  legend.appendChild(more);
  const drift = span("tl-legend-item", "");
  drift.appendChild(span("tl-swatch heat-swatch-drift", ""));
  drift.appendChild(document.createTextNode("written but undeclared"));
  legend.appendChild(drift);
  head0.appendChild(legend);
  view.appendChild(head0);

  const wrap = document.createElement("div");
  wrap.className = "heat-wrap";
  const grid = document.createElement("div");
  grid.className = "heat-grid";
  grid.setAttribute("role", "table");
  grid.setAttribute("aria-label", "Slice file writes per iteration");
  grid.style.gridTemplateColumns =
    "minmax(160px, 480px) repeat(" + iterCols.length + ", 40px)";

  const head = document.createElement("div");
  head.className = "heat-file heat-hdr";
  head.setAttribute("role", "columnheader");
  head.textContent = "File";
  grid.appendChild(head);
  for (const it of iterCols) {
    const h = document.createElement("div");
    h.className = "heat-hdr";
    h.textContent = it === "—" ? "—" : "It " + it;
    h.title = it === "—" ? "Unplanned" : "Iteration " + it;
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
        cell.style.setProperty("--heat", (a * 100).toFixed(1) + "%");
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

function sessionSignature(sessions) {
  return sessions.map((s) => (s.path ?? "") + ":" + (s.size ?? "") + ":" + (s.status ?? "")).join("|");
}

/* Transcripts tab with nothing selected: open the newest session so the
 * tab never opens onto a blank pane. */
function openDefaultSession() {
  if (state.activePath || !state.detail) {
    if (!state.detail) showTranscriptNotice("Loading sessions…");
    return;
  }
  if (!state.sessions.length) {
    showTranscriptNotice(
      "No session transcripts for this loop yet. Sessions are found by id: " +
      "Claude/Codex sessions as soon as a loop records their id, Omnigent " +
      "sessions when archived. Omnigent drivers export them to the mailbox's " +
      ".sessions/ folder and omp runs keep theirs under ~/.omp."
    );
    return;
  }
  const openable = state.sessions.filter((s) => s.path && s.status !== "deleted");
  const first = openable.find((s) => s.kind !== "subagent") || openable[0];
  if (first) openSession(first.path);
}

function renderSessionList() {
  const list = el("session-list");
  list.textContent = "";
  /* Mailbox exports are written when a role's session is archived; say so
   * rather than let the list read as a live view of the broker session. */
  el("sessions-note").hidden = !state.sessions.some((s) => s.source === "mailbox");
  if (!state.sessions.length) {
    list.appendChild(span("session-empty", state.detail ? "No sessions recorded" : "Loading…"));
    return;
  }

  if (!state.sessions.some((s) => s.kind === "subagent")) {
    const frag = document.createDocumentFragment();
    for (const s of state.sessions) frag.appendChild(sessionItem(s));
    list.appendChild(frag);
    return;
  }

  const byPath = new Map(state.sessions.filter((s) => s.path).map((s) => [s.path, s]));
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
  if (s.status === "deleted") {
    /* The transcript file is gone (retention): a plain row, never a stream. */
    const gone = document.createElement("div");
    gone.className = "session-item session-deleted";
    const text = String(s.label || s.id || "session");
    gone.textContent = /deleted by retention/.test(text) ? text : text + " (deleted by retention)";
    return gone;
  }
  const item = document.createElement("button");
  item.type = "button";
  item.className = "session-item";
  if (s.path && state.activePath === s.path) item.classList.add("active");

  const isSub = s.kind === "subagent";
  if (isSub) {
    item.classList.add("session-subagent");
    item.style.paddingLeft = 28 + Math.max(0, depth - 1) * 14 + "px";
  } else if (subCount > 0) {
    item.classList.add("session-parent");
  }

  const label = s.label ? String(s.label).replace(/\.jsonl$/, "") : s.id;
  const meta = [relTime(s.timestamp), fmtSize(s.size)];
  if (s.agent) meta.unshift(String(s.agent).replace(/^trio-omnigent-/, ""));
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
  state.records = 0;
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
    if (state.es === es) setPaneStatus("connected", "following file");
  });

  es.addEventListener("init", (ev) => {
    if (state.es !== es) return;
    try {
      const data = JSON.parse(ev.data);
      state.offset = Number(data.offset) || 0;
      state.size = data.size != null ? Number(data.size) : null;
      setOffsetHint();
      showTranscriptNotice(state.size ? "Loading transcript…" : "This session file is empty so far; new records appear here as they are written.");
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
  for (const rec of lines) {
    /* One malformed record must not drop the rest of the batch. */
    try {
      appendRecord(view, rec);
    } catch (err) {
      console.warn("transcript record skipped", err);
    }
  }
  state.records += lines.length;
  if (!view.childElementCount) {
    showTranscriptNotice(state.records + " records read, none of them messages yet.");
  }
  if (state.follow) view.scrollTop = view.scrollHeight;
}

/* ISO time of a record, or null. Out-of-range epochs ("1e20") make an
 * Invalid Date, whose toISOString() throws. */
function recordTime(rec) {
  if (!rec) return null;
  if (rec.timestamp) return rec.timestamp;
  const epoch = Number(rec.created_at);
  if (!Number.isFinite(epoch) || epoch <= 0) return null;
  const date = new Date(epoch * 1000);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}

function timeEl(rec) {
  const t = fmtClock(recordTime(rec));
  return t ? span("tr-time", t) : null;
}

/* omp writes `text` parts; Omnigent exports `input_text` / `output_text`. */
const TEXT_PARTS = new Set(["text", "input_text", "output_text"]);

function textOf(parts) {
  return (Array.isArray(parts) ? parts : [])
    .filter((p) => p && TEXT_PARTS.has(p.type) && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n")
    .trim();
}

/* Dispatch one JSONL record into trace blocks. */
function appendRecord(view, rec) {
  if (!rec || typeof rec !== "object") return;
  hideTranscriptNotice();

  /* Claude Code and Codex rollout records have their own shapes. */
  if (appendForeignRecord(view, rec)) return;

  /* Omnigent session header: the first line, with no `type`. */
  if (!rec.type && (rec.agent_name || rec.title)) {
    const who = rec.agent_name ? String(rec.agent_name) : "";
    metaRow(view, rec, "session", [who, rec.title].filter(Boolean).join(" · "));
    return;
  }
  if (rec.type === "resource_event") {
    const kind = String(rec.event_type || "").split(".").pop();
    const res = rec.resource || {};
    const name = res.name || rec.resource_id || rec.resource_type || "";
    metaRow(view, rec, (rec.resource_type || "resource") + " " + (kind || "event"), name);
    return;
  }
  /* Omnigent messages carry role and content at the top level. */
  if (rec.type === "message" && !rec.message && rec.role) {
    const text = textOf(rec.content);
    if (text) view.appendChild(msgBlock(rec, rec.role === "user" ? "user" : "assistant", rec.role, text));
    return;
  }

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
        if (TEXT_PARTS.has(part.type) && part.text && part.text.trim()) {
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
    const n = Array.isArray(rec.compacted_messages) ? rec.compacted_messages.length : 0;
    const text = rec.summary ? oneLine(rec.summary, 120) : n ? n + " earlier messages folded" : "";
    metaRow(view, rec, "compacted", text);
    return;
  }

  metaRow(view, rec, String(rec.type || "record"));
}

/* ---- Claude Code JSONL and Codex rollout records ----
 * Both are normalised onto the omp block shapes so the builders below render
 * them. Everything reaches the DOM through textContent. */

/* tool_use id / call_id -> tool name, so a result row can name its call. */
const transcriptToolNames = new Map();

const CLAUDE_META_TYPES = new Set([
  "attachment", "system", "summary", "queue-operation", "file-history-snapshot", "progress",
]);
const CODEX_TYPES = new Set([
  "session_meta", "response_item", "event_msg", "turn_context", "compacted",
]);

function isPlainObject(v) {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

/* First non-empty string among `keys` of obj, else "". */
function firstString(obj, keys) {
  if (!isPlainObject(obj)) return "";
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === "string" && v.trim()) return v;
    if (Array.isArray(v) && v.length && v.every((x) => typeof x === "string")) return v.join(" ");
  }
  return "";
}

/* One-line summary of a tool's arguments for the call row. */
function toolIntent(args) {
  return oneLine(firstString(args, ["description", "command", "cmd", "file_path", "path", "pattern", "prompt"]), 90);
}

/* Tool output of either vendor -> omp-style content parts. */
function resultParts(content) {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (Array.isArray(content)) {
    return content.map((p) => (typeof p === "string" ? { type: "text", text: p } : p));
  }
  if (isPlainObject(content)) {
    if (content.content !== undefined) return resultParts(content.content);
    if (typeof content.output === "string") return resultParts(content.output);
    return [{ type: "text", text: JSON.stringify(content) }];
  }
  return [];
}

function toolNameFor(id) {
  return (id && transcriptToolNames.get(String(id))) || "tool";
}

function rememberTool(id, name) {
  if (!id) return;
  if (transcriptToolNames.size > 5000) transcriptToolNames.clear();
  transcriptToolNames.set(String(id), String(name || "tool"));
}

function appendClaudeUser(view, rec) {
  const msg = isPlainObject(rec.message) ? rec.message : {};
  const content = msg.content;
  if (rec.isMeta === true) {
    const text = typeof content === "string" ? content : textOf(content);
    metaRow(view, rec, "meta", oneLine(text, 160));
    return;
  }
  if (typeof content === "string") {
    if (content.trim()) view.appendChild(msgBlock(rec, "user", "user", content.trim()));
    return;
  }
  const texts = [];
  for (const part of Array.isArray(content) ? content : []) {
    if (!isPlainObject(part)) continue;
    if (part.type === "tool_result") {
      view.appendChild(toolResultBlock(rec, {
        isError: part.is_error === true,
        toolName: toolNameFor(part.tool_use_id),
        content: resultParts(part.content),
      }));
    } else if (part.type === "image") {
      texts.push("[image]");
    } else if (TEXT_PARTS.has(part.type) && typeof part.text === "string" && part.text.trim()) {
      texts.push(part.text.trim());
    }
  }
  if (texts.length) view.appendChild(msgBlock(rec, "user", "user", texts.join("\n")));
}

function appendClaudeAssistant(view, rec) {
  const content = isPlainObject(rec.message) ? rec.message.content : null;
  if (typeof content === "string") {
    if (content.trim()) view.appendChild(msgBlock(rec, "assistant", "assistant", content.trim()));
    return;
  }
  for (const part of Array.isArray(content) ? content : []) {
    if (!isPlainObject(part)) continue;
    if (part.type === "text" && typeof part.text === "string" && part.text.trim()) {
      view.appendChild(msgBlock(rec, "assistant", "assistant", part.text.trim()));
    } else if (part.type === "thinking" && part.thinking) {
      view.appendChild(thinkingBlock(rec, String(part.thinking)));
    } else if (part.type === "redacted_thinking") {
      metaRow(view, rec, "thinking", "[redacted]");
    } else if (part.type === "tool_use") {
      rememberTool(part.id, part.name);
      const args = isPlainObject(part.input) ? part.input : {};
      view.appendChild(toolCallBlock(rec, {
        name: part.name, arguments: args, intent: toolIntent(args),
      }));
    }
  }
}

/* Short detail for the quiet meta rows of Claude Code record types. */
function claudeMetaDetail(rec) {
  switch (rec.type) {
    case "system":
      return oneLine(typeof rec.content === "string" ? rec.content : rec.subtype || "", 160);
    case "summary":
      return oneLine(rec.summary || "", 160);
    case "attachment": {
      const a = isPlainObject(rec.attachment) ? rec.attachment : {};
      const body = firstString(a, ["content", "stdout", "command", "filename", "path", "prompt", "text"]);
      return oneLine([a.type, body].filter(Boolean).join(" · "), 160);
    }
    case "queue-operation":
      return oneLine([rec.operation, typeof rec.content === "string" ? rec.content : ""].filter(Boolean).join(" · "), 160);
    case "progress": {
      const d = isPlainObject(rec.data) ? rec.data : {};
      return oneLine(d.type || firstString(d, ["message", "command"]), 160);
    }
    default:
      return "";
  }
}

function codexJson(text) {
  if (typeof text !== "string") return null;
  const t = text.trim();
  if (!t || (t[0] !== "{" && t[0] !== "[")) return null;
  try { return JSON.parse(t); } catch { return null; }
}

function appendCodexItem(view, rec, item) {
  switch (item.type) {
    case "message": {
      const text = textOf(item.content);
      if (!text) return;
      if (item.role === "user") view.appendChild(msgBlock(rec, "user", "user", text));
      else if (item.role === "assistant") view.appendChild(msgBlock(rec, "assistant", "assistant", text));
      else metaRow(view, rec, String(item.role || "message"), oneLine(text, 160));
      return;
    }
    case "function_call":
    case "custom_tool_call": {
      const raw = item.type === "function_call" ? item.arguments : (item.input ?? item.arguments);
      let args = isPlainObject(raw) ? raw : codexJson(raw);
      if (!isPlainObject(args)) args = raw === undefined || raw === "" ? {} : { arguments: raw };
      rememberTool(item.call_id || item.id, item.name);
      view.appendChild(toolCallBlock(rec, { name: item.name, arguments: args, intent: toolIntent(args) }));
      return;
    }
    case "function_call_output":
    case "custom_tool_call_output": {
      let out = item.output;
      const parsed = codexJson(out);
      if (isPlainObject(parsed) && typeof parsed.output === "string") out = parsed.output;
      view.appendChild(toolResultBlock(rec, {
        isError: false,
        toolName: toolNameFor(item.call_id),
        content: resultParts(out),
      }));
      return;
    }
    case "reasoning": {
      const parts = Array.isArray(item.summary) ? item.summary : [];
      const text = parts
        .map((p) => (p && typeof p.text === "string" ? p.text : ""))
        .filter(Boolean)
        .join("\n")
        .trim();
      if (text) view.appendChild(thinkingBlock(rec, text));
      else metaRow(view, rec, "thinking", "[no summary]");
      return;
    }
    default: {
      const action = isPlainObject(item.action) ? firstString(item.action, ["command"]) : "";
      metaRow(view, rec, String(item.type || "response_item"), oneLine(action, 160));
    }
  }
}

function appendCodexRecord(view, rec) {
  const payload = isPlainObject(rec.payload) ? rec.payload : {};
  if (rec.type === "session_meta") {
    metaRow(view, rec, "session", [payload.cwd, payload.id].filter(Boolean).join(" · "));
  } else if (rec.type === "response_item") {
    appendCodexItem(view, rec, payload);
  } else if (rec.type === "turn_context") {
    metaRow(view, rec, "turn", oneLine([payload.cwd, payload.model].filter(Boolean).join(" · "), 160));
  } else if (rec.type === "compacted") {
    metaRow(view, rec, "compacted", oneLine(firstString(payload, ["message"]), 160));
  } else {
    const detail = firstString(payload, ["message", "text", "command", "reason", "last_agent_message"]);
    metaRow(view, rec, String(payload.type || rec.type), oneLine(detail, 160));
  }
}

/* True when rec was a Claude Code / Codex record and has been rendered. */
function appendForeignRecord(view, rec) {
  if (rec.type === "user" && isPlainObject(rec.message)) {
    appendClaudeUser(view, rec);
    return true;
  }
  if (rec.type === "assistant" && isPlainObject(rec.message)) {
    appendClaudeAssistant(view, rec);
    return true;
  }
  if (CLAUDE_META_TYPES.has(rec.type)) {
    metaRow(view, rec, String(rec.type), claudeMetaDetail(rec));
    return true;
  }
  if (CODEX_TYPES.has(rec.type) && "payload" in rec) {
    appendCodexRecord(view, rec);
    return true;
  }
  return false;
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
  transcriptToolNames.clear();
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
  const follow = el("follow-btn");
  follow.disabled = !state.activePath;
  follow.title = state.activePath ? "" : "Open a session to follow it";
  if (!state.activePath) {
    el("offset-hint").textContent = "";
    return;
  }
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
    if (el("confirm-dialog").open) return;  // the dialog handles its own keys
    if (ev.key === "Escape" && !el("drawer").hidden) closeDrawer();
    trapDrawerFocus(ev);
  });
  el("diagnose-btn").addEventListener("click", startDiagnosis);
  el("answer-box").addEventListener("submit", submitAnswer);
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
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(state.boardTimer);
    else refreshBoard();
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
