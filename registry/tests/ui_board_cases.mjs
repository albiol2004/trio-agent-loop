/* Cases for dashboard/ui/board.js, run in the browser harness
 * (ui_browser.cjs): the real vendored preact + signals + htm, the real
 * dashboard/index.html shell, a real DOM and real MutationObservers. fetch and
 * EventSource are stubbed through createStore opts; store.now is driven by
 * hand. Sets window.__uiResults and window.__uiDone; test_ui_board.py asserts
 * on the values. */
import { mountBoard } from "/ui/board.js";
import { createStore } from "/ui/store.js";

const results = {};
window.__uiResults = results;

const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

const NOW = Date.parse("2026-06-01T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString();
const ROOT_ONE = "/w/one";
const ROOT_TWO = "/w/two";

/* The pristine shell, restored before every case. */
const PRISTINE = document.getElementById("app").cloneNode(true);
function freshDom() {
  document.getElementById("app").replaceWith(PRISTINE.cloneNode(true));
}

function stubFetch(calls, reply) {
  return async (url, init) => {
    calls.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
    const r = reply ? reply(url) : { ok: true, status: 200, data: {} };
    return { ok: r.ok, status: r.status, json: async () => r.data };
  };
}

function makeStore(nowMs = NOW, calls = [], reply = null) {
  return createStore({
    fetch: stubFetch(calls, reply), EventSource: undefined, storage: null, now: () => nowMs,
  });
}

/* Cards in the api:LiveStream snapshot shape. */
function card(over) {
  return Object.assign({
    name: "x", loop_id: "L-x", title: "", mission: "", status: "iterating",
    final_verdict: null, segments: [], running: false, running_sources: [],
    last_activity: iso(NOW - 600000), iteration: 1, max_iterations: 5,
  }, over);
}

function item(over) {
  return Object.assign({
    id: "i?", loop: "x", severity: "low", kind: "drift", headline: "h", detail: "", read: false,
  }, over);
}

const ALPHA = card({
  name: "alpha-build", loop_id: "L-alpha", title: "Alpha build", mission: "Build alpha. Then ship.",
  status: "iterating", running: true, running_sources: ["driver"], driver_phase: "lead",
  last_activity: iso(NOW - 30000), iteration: 2, max_iterations: 5,
  segments: [{ verdict_sequence: "II" }],
});
const BETA = card({
  name: "beta-docs", loop_id: "L-beta", title: "Beta docs", status: "shipped",
  final_verdict: "SHIP", verdict_mtime: iso(NOW - 2 * 86400000),
  last_activity: iso(NOW - 3 * 3600000), iteration: 3, max_iterations: 3,
  segments: [{ verdict_sequence: "IS" }],
});
const GAMMA = card({
  name: "gamma-fix", loop_id: "L-gamma", title: "Gamma fix", mission: "Fix gamma.",
  status: "needs_human", last_activity: iso(NOW - 600000), iteration: 1, max_iterations: 4,
  segments: [{ verdict_sequence: "I" }],
});
const DELTA = card({
  name: "loop-archive/delta-old", loop_id: "L-delta", title: "Delta old", status: "shipped",
  last_activity: iso(NOW - 9 * 86400000), iteration: null, max_iterations: null,
});

const INBOX = [
  item({ id: "i1", loop: "gamma-fix", severity: "high", kind: "blocked",
    headline: "Blocked on review", detail: "Waiting for a human to approve the plan." }),
  item({ id: "i2", loop: "gamma-fix", severity: "medium", kind: "needs_human", headline: "Second thing" }),
  item({ id: "i3", loop: "loop-archive/delta-old", severity: "low", kind: "drift",
    headline: "Drift noticed", detail: "" }),
  item({ id: "i4", loop: "beta-docs", severity: "high", kind: "queue_fault",
    headline: "Queue fault", read: true }),
];

function snapshot(over = {}) {
  return Object.assign({
    epoch: "e1", seq: 1, updated_at: iso(NOW - 2000), scanned: 2, broker: "ok",
    worktrees_scanned: 0,
    workspaces: [
      { root: ROOT_ONE, name: "one", worktree: false,
        loops: [ALPHA, BETA], inbox: INBOX.filter((i) => i.loop === "beta-docs") },
      { root: ROOT_TWO, name: "two", worktree: false,
        loops: [GAMMA, DELTA], inbox: INBOX.filter((i) => i.loop !== "beta-docs") },
    ],
  }, over);
}

function loopUpsert(root, c) {
  return { op: "upsert", kind: "loop", id: c.loop_id, data: { root, card: c } };
}

function delta(store, changes, updatedAt) {
  const from = store.seq.peek();
  return store.applyDelta({
    epoch: store.epoch.peek(), seq: from + 1, from_seq: from,
    updated_at: updatedAt || store.meta.peek().updated_at, changes,
  });
}

/* ------------------------------ observation ------------------------------ */

const SECTION_IDS = new Set([
  "live", "live-text", "verdict", "verdict-sub", "board-error", "board-partial", "broker-note",
  "kpis", "needs-count", "inbox-toggle", "needs-list", "running-count", "running-list",
  "workspace-filter", "tabs", "loop-rows", "notes", "notes-count", "notes-sub", "notes-list",
  "drawer-inbox-list", "drawer-inbox-section", "loop-search",
]);

/* Which part of the board a node belongs to: a loop row, a running card, an
 * attention row (by loop key), else its section id. */
function zone(node) {
  let n = node.nodeType === 1 ? node : node.parentNode;
  while (n && n !== document.body) {
    if (n.matches) {
      if (n.matches("tr.loop-row")) return "row:" + n.dataset.id;
      if (n.matches(".run-card")) return "card:" + n.dataset.key;
      if (n.matches(".attn-row") && !n.closest("#drawer-inbox-list")) return "attn:" + n.dataset.key;
      if (n.id && SECTION_IDS.has(n.id)) return "section:" + n.id;
    }
    n = n.parentNode;
  }
  return "other";
}

function observe() {
  const pending = [];
  const observer = new MutationObserver((list) => pending.push(...list));
  observer.observe(document.getElementById("app"), {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
  return {
    take() {
      pending.push(...observer.takeRecords());
      return pending.splice(0).map((r) => ({
        type: r.type,
        zone: zone(r.target),
        inReltime: Boolean((r.target.nodeType === 1 ? r.target : r.target.parentElement)
          .closest("[data-reltime]")),
        attr: r.attributeName || null,
      }));
    },
    stop() { observer.disconnect(); },
  };
}

function tally(records) {
  const out = {};
  for (const r of records) {
    const key = r.zone;
    out[key] = out[key] || { childList: 0, attributes: 0, characterData: 0 };
    out[key][r.type] += 1;
  }
  return out;
}

/* ------------------------------ DOM readers ------------------------------ */

const text = (node) => (node ? node.textContent : null);
const q = (sel, root = document) => root.querySelector(sel);
const qa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function rowData(tr) {
  const cells = Array.from(tr.children);
  return {
    id: tr.dataset.id,
    key: tr.dataset.key,
    cls: tr.className,
    title: text(q(".row-open", tr)),
    titleAttr: q(".row-open", tr).getAttribute("title"),
    sub: text(q("th .row-sub", tr)),
    flags: qa(".row-flag", tr).map((n) => n.textContent),
    ws: text(cells[1]),
    wsTitle: cells[1].getAttribute("title"),
    badge: text(q(".badge", cells[2])),
    badgeClass: q(".badge", cells[2]).className,
    phase: text(q(".row-sub", cells[2])),
    iteration: text(cells[3]),
    hist: text(cells[4]),
    activity: text(cells[5]),
    activityDatetime: q("time", cells[5]).getAttribute("datetime"),
  };
}

function attnData(row) {
  return {
    key: row.dataset.key,
    cls: row.className,
    badge: text(q(".badge", row)),
    open: text(q(".attn-open", row)),
    openLabel: q(".attn-open", row).getAttribute("aria-label"),
    where: text(q(".attn-where", row)),
    reason: text(q(".attn-reason", row)),
    more: text(q(".attn-more", row)),
    detail: text(q(".attn-detail", row)),
    caption: text(q(".attn-side .caption", row)),
    mark: text(q(".attn-side button", row)),
    markLabel: q(".attn-side button", row).getAttribute("aria-label"),
  };
}

function cardData(node) {
  return {
    key: node.dataset.key,
    badge: text(q(".badge", node)),
    lastWrite: text(q(".run-top .caption", node)),
    title: text(q(".run-title", node)),
    where: text(q(".attn-where", node)),
    meta: qa(".run-meta span", node).map((n) => n.textContent),
    meter: q(".meter", node) ? q(".meter", node).getAttribute("aria-label") : null,
    fill: q(".meter-fill", node) ? q(".meter-fill", node).style.width : null,
    live: text(qa(".caption", node).pop()),
  };
}

function kpiData() {
  return qa("#kpis > *").map((n) => ({
    tag: n.tagName.toLowerCase(),
    cls: n.className,
    href: n.getAttribute("href"),
    eyebrow: text(q(".eyebrow", n)),
    value: text(q(".kpi-value", n)),
    context: text(q(".kpi-context", n)),
  }));
}

function boardDump() {
  return {
    verdict: text(q("#verdict")),
    verdictSub: text(q("#verdict-sub")),
    brokerNote: { hidden: q("#broker-note").hidden, text: text(q("#broker-note")) },
    partial: { hidden: q("#board-partial").hidden, text: text(q("#board-partial")) },
    kpis: kpiData(),
    needsCount: text(q("#needs-count")),
    toggle: {
      hidden: q("#inbox-toggle").hidden, text: text(q("#inbox-toggle")),
      expanded: q("#inbox-toggle").getAttribute("aria-expanded"),
    },
    needs: qa("#needs-list > .attn-row").map(attnData),
    needsEmpty: q("#needs-list > .empty") ? text(q("#needs-list > .empty")) : null,
    runningCount: text(q("#running-count")),
    runCards: qa("#running-list .run-card").map(cardData),
    runNotes: qa("#running-list .run-note").map((n) => n.textContent),
    runEmpty: q("#running-list > .empty") ? text(q("#running-list > .empty")) : null,
    wsOptions: qa("#workspace-filter option").map((o) => ({
      value: o.value, text: o.textContent, title: o.getAttribute("title"), selected: o.selected,
    })),
    tabs: qa("#tabs button").map((b) => ({
      text: b.textContent, label: b.firstChild.data, count: text(q(".seg-count", b)),
      pressed: b.getAttribute("aria-pressed"), cls: b.className,
    })),
    rows: qa("#loop-rows > tr").map((tr) => tr.classList.contains("loop-row")
      ? rowData(tr) : { cls: tr.className, text: text(tr) }),
    sortAria: qa(".loop-table th[aria-sort]").map((th) => th.getAttribute("aria-sort")),
    notes: {
      hidden: q("#notes").hidden, count: text(q("#notes-count")), sub: text(q("#notes-sub")),
      rows: qa("#notes-list > .attn-row").map(attnData),
    },
  };
}

function liveDump() {
  const live = q("#live");
  return {
    text: text(q("#live-text")), cls: live.className, title: live.getAttribute("title"),
    reltimeText: q("#live-text [data-reltime]") ? text(q("#live-text [data-reltime]")) : null,
  };
}

function bannerDump() {
  return { hidden: q("#board-error").hidden, text: text(q("#board-error-text")) };
}

async function setup(opts = {}) {
  freshDom();
  const calls = [];
  const store = makeStore(opts.now || NOW, calls, opts.reply || null);
  const unmount = mountBoard(store, document);
  if (!opts.noSnapshot) store.applySnapshot(opts.snapshot || snapshot());
  await settle();
  return { store, calls, unmount };
}

const cases = {};
const test = (name, fn) => { cases[name] = fn; };

/* ------------------------------ cases ------------------------------ */

test("initial_render", async () => {
  freshDom();
  const store = makeStore();
  const unmount = mountBoard(store, document);
  await settle();
  const before = {
    verdict: text(q("#verdict")), kpis: qa("#kpis > .skeleton").length,
    needs: qa("#needs-list > .skeleton").length, running: qa("#running-list > .skeleton").length,
    rows: qa("#loop-rows > tr.row-loading").length,
  };
  store.applySnapshot(snapshot());
  await settle();
  const dump = boardDump();
  unmount();
  await settle();
  return { before, dump, afterUnmount: { rows: qa("#loop-rows > *").length, kpis: qa("#kpis > *").length } };
});

test("banners", async () => {
  const snap = snapshot({ broker: "unreachable" });
  snap.workspaces[1].error = "boom";
  const { store, unmount } = await setup({ snapshot: snap });
  const dump = boardDump();
  unmount();
  return { brokerNote: dump.brokerNote, partial: dump.partial };
});

test("delta_isolation", async () => {
  const { store, unmount } = await setup();
  store.setFeed({ status: "live", mode: "stream", lastOk: NOW, updatedAt: iso(NOW - 2000) });
  await settle();
  const obs = observe();
  // loop X = alpha (running: its row and its running card)
  delta(store, [loopUpsert(ROOT_ONE, Object.assign({}, ALPHA, { status: "iterating-fast" }))]);
  await settle();
  const alpha = tally(obs.take());
  // loop X = gamma (listed in "Needs you")
  delta(store, [loopUpsert(ROOT_TWO, Object.assign({}, GAMMA, { status: "blocked_by_review" }))]);
  await settle();
  const gamma = tally(obs.take());
  // a delta that changes nothing a loop shows: an unchanged upsert
  delta(store, [loopUpsert(ROOT_TWO, Object.assign({}, GAMMA, { status: "blocked_by_review" }))]);
  await settle();
  const unchanged = tally(obs.take());
  obs.stop();
  const rows = Object.fromEntries(qa("tr.loop-row").map((tr) => [tr.dataset.id, rowData(tr)]));
  unmount();
  return { alpha, gamma, unchanged, alphaBadge: rows["L-alpha"].badge, gammaBadge: rows["L-gamma"].badge };
});

test("loop_added_and_removed", async () => {
  const { store, unmount } = await setup();
  const obs = observe();
  const extra = card({ name: "omega", loop_id: "L-omega", title: "Omega", last_activity: iso(NOW - 5000000) });
  delta(store, [loopUpsert(ROOT_ONE, extra)]);
  await settle();
  const added = { zones: tally(obs.take()), ids: qa("tr.loop-row").map((tr) => tr.dataset.id),
    verdictSub: text(q("#verdict-sub")), kpi: kpiData()[3].value };
  delta(store, [{ op: "remove", kind: "loop", id: "L-omega", data: null }]);
  await settle();
  const removed = { zones: tally(obs.take()), ids: qa("tr.loop-row").map((tr) => tr.dataset.id) };
  obs.stop();
  unmount();
  return { added, removed };
});

test("clock_tick_is_character_data_only", async () => {
  const { store, unmount } = await setup();
  const agoBefore = qa("[data-reltime]").map((n) => n.textContent);
  const obs = observe();
  const steps = [];
  // 70 s later: crosses the minute boundary of alpha's "30s ago" (-> "1m ago")
  for (const dt of [5000, 70000, 70000, 3600000]) {
    store.now.value = NOW + dt;
    await settle();
    const records = obs.take();
    steps.push({
      dt,
      records: records.length,
      childList: records.filter((r) => r.type === "childList").length,
      attributes: records.filter((r) => r.type === "attributes").length,
      outsideReltime: records.filter((r) => r.type === "characterData" && !r.inReltime).length,
      zones: Object.keys(tally(records)).sort(),
    });
  }
  obs.stop();
  const agoAfter = qa("[data-reltime]").map((n) => n.textContent);
  const reltimeCount = qa("[data-reltime]").length;
  const activityCells = qa("tr.loop-row").map((tr) => [tr.dataset.id, rowData(tr).activity]);
  unmount();
  return { agoBefore, agoAfter, steps, reltimeCount, activityCells };
});

test("stale_feed_flags_the_indicator", async () => {
  const { store, unmount } = await setup();
  store.setFeed({ status: "live", mode: "stream", lastOk: NOW, updatedAt: iso(NOW - 2000) });
  await settle();
  const fresh = liveDump();
  store.now.value = NOW + 10000;
  await settle();
  const stillFresh = liveDump();
  store.now.value = NOW + 40000;
  await settle();
  const stale = liveDump();
  store.setFeed({ lastOk: NOW + 40000, updatedAt: iso(NOW + 39000) });
  await settle();
  const recovered = liveDump();
  unmount();
  return { fresh, stillFresh, stale, recovered };
});

test("focus_and_caret_survive_updates", async () => {
  const { store, unmount } = await setup();
  const search = q("#loop-search");
  search.focus();
  search.value = "alp";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  search.setSelectionRange(1, 2);
  await settle();
  const typed = { query: store.query.value, rows: qa("tr.loop-row").map((tr) => tr.dataset.id) };
  const obs = observe();
  delta(store, [loopUpsert(ROOT_ONE, Object.assign({}, ALPHA, { status: "iterating-fast" }))]);
  await settle();
  store.now.value = NOW + 70000;
  await settle();
  delta(store, [loopUpsert(ROOT_TWO, Object.assign({}, GAMMA, { last_activity: iso(NOW + 1000) }))]);
  await settle();
  store.setTab("running");
  store.setTab("all");
  await settle();
  const searchMutations = obs.take().filter((r) => r.zone === "section:loop-search").length;
  obs.stop();
  const result = {
    typed,
    activeIsSearch: document.activeElement === search,
    value: search.value, selectionStart: search.selectionStart, selectionEnd: search.selectionEnd,
    rows: qa("tr.loop-row").map((tr) => tr.dataset.id),
    searchMutations,
    tabCounts: qa("#tabs .seg-count").map((n) => n.textContent),
  };
  unmount();
  return result;
});

test("equal_sort_keys_order_by_name_then_id", async () => {
  const same = iso(NOW - 900000);
  const a = card({ name: "same", loop_id: "L-9", title: "", last_activity: same });
  const b = card({ name: "same", loop_id: "L-1", title: "", last_activity: same });
  const c = card({ name: "aaa", loop_id: "L-5", title: "", last_activity: same });
  const snap = snapshot();
  snap.workspaces = [
    { root: ROOT_ONE, name: "one", worktree: false, loops: [a, c], inbox: [] },
    { root: ROOT_TWO, name: "two", worktree: false, loops: [b], inbox: [] },
  ];
  const { store, unmount } = await setup({ snapshot: snap });
  const order = () => qa("tr.loop-row").map((tr) => tr.dataset.id);
  const initial = order();
  delta(store, [{ op: "remove", kind: "loop", id: "L-1", data: null }]);
  await settle();
  const removed = order();
  delta(store, [loopUpsert(ROOT_TWO, b)]);
  await settle();
  const readded = order();
  delta(store, [{ op: "remove", kind: "loop", id: "L-9", data: null }]);
  await settle();
  delta(store, [loopUpsert(ROOT_ONE, a)]);
  await settle();
  const readdedOther = order();
  // the same ties under the other sort keys
  q(".th-sort[data-sort=workspace]").click();
  await settle();
  const byWorkspace = order();
  q(".th-sort[data-sort=loop]").click();
  await settle();
  const byLoop = order();
  unmount();
  return { initial, removed, readded, readdedOther, byWorkspace, byLoop };
});

test("live_indicator_states", async () => {
  const { store, unmount } = await setup();
  const states = {};
  states.connecting = Object.assign(liveDump(), { banner: bannerDump() });
  store.setFeed({ status: "live", mode: "stream", lastOk: NOW, updatedAt: iso(NOW - 2000), error: null });
  await settle();
  states.live = Object.assign(liveDump(), { banner: bannerDump() });
  store.setFeed({ status: "reconnecting" });
  await settle();
  states.reconnecting = Object.assign(liveDump(), { banner: bannerDump() });
  store.setFeed({ mode: "poll", status: "live", lastOk: NOW, updatedAt: iso(NOW - 2000) });
  await settle();
  states.poll = Object.assign(liveDump(), { banner: bannerDump() });
  store.setFeed({ status: "offline", error: "HTTP 503" });
  await settle();
  states.offline = Object.assign(liveDump(), { banner: bannerDump() });
  let retried = 0;
  store.retry = () => { retried += 1; };
  q("#board-retry").click();
  store.setFeed({ mode: "stream", status: "live", lastOk: NOW + 1, error: null });
  await settle();
  states.back = Object.assign(liveDump(), { banner: bannerDump() });
  unmount();
  // never loaded: the error banner says so
  freshDom();
  const lonely = makeStore();
  const unmount2 = mountBoard(lonely, document);
  lonely.setFeed({ mode: "poll", status: "offline", error: "boom" });
  await settle();
  states.neverLoaded = Object.assign(liveDump(), { banner: bannerDump(), verdict: text(q("#verdict")) });
  unmount2();
  return { states, retried };
});

test("interactions", async () => {
  const { store, calls, unmount } = await setup();
  const out = {};
  // open a loop: row click, then Enter on another row
  q("tr[data-id=L-alpha] .row-sub").click();
  await settle();
  out.afterClick = { openKey: store.openKey.value,
    active: qa("tr.loop-row.active").map((tr) => tr.dataset.id) };
  q("tr[data-id=L-gamma]").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await settle();
  out.afterEnter = { openKey: store.openKey.value,
    active: qa("tr.loop-row.active").map((tr) => tr.dataset.id),
    drawerRows: qa("#drawer-inbox-list .attn-row").map((n) => ({
      cls: n.className, badge: text(q(".badge", n)), reason: text(q(".attn-reason", n)),
      detail: q(".attn-detail", n) ? text(q(".attn-detail", n)) : null,
      mark: text(q("button", n)), label: q("button", n).getAttribute("aria-label"),
    })),
    sectionHidden: q("#drawer-inbox-section").hidden };
  // the attention row's open button, the running card
  store.closeLoop();
  q("#needs-list .attn-open").click();
  await settle();
  out.afterAttnOpen = store.openKey.value;
  q("#running-list .run-card").click();
  await settle();
  out.afterCardOpen = store.openKey.value;
  store.closeLoop();
  await settle();
  out.closed = { active: qa("tr.loop-row.active").length,
    drawerRows: qa("#drawer-inbox-list .attn-row").length, sectionHidden: q("#drawer-inbox-section").hidden };

  // mark every item of the group read
  q("#needs-list .attn-side button").click();
  await settle();
  out.markCalls = calls.filter((c) => c.url.startsWith("/api/inbox/"));
  out.afterMark = { needs: boardDump().needsEmpty, count: text(q("#needs-count")),
    toggle: boardDump().toggle };
  // reveal the read ones
  q("#inbox-toggle").click();
  await settle();
  const shown = boardDump();
  out.afterShow = { showRead: store.showReadInbox.value, toggle: shown.toggle,
    needs: shown.needs.map((r) => ({ key: r.key, cls: r.cls, mark: r.mark })), count: shown.needsCount };

  // tabs, workspace filter and sort
  q("#tabs button").click(); // Running
  await settle();
  out.running = { tab: store.tab.value, rows: qa("tr.loop-row").map((tr) => tr.dataset.id),
    pressed: qa("#tabs button").map((b) => b.getAttribute("aria-pressed")) };
  qa("#tabs button")[2].click(); // All
  await settle();
  const select = q("#workspace-filter");
  select.value = ROOT_TWO;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
  out.workspace = { value: store.workspace.value, rows: qa("tr.loop-row").map((tr) => tr.dataset.id),
    selected: select.value, tabCounts: qa("#tabs .seg-count").map((n) => n.textContent) };
  select.value = "";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
  q(".th-sort[data-sort=loop]").click();
  await settle();
  out.sortAsc = { sort: store.sort.value, aria: boardDump().sortAria,
    rows: qa("tr.loop-row").map((tr) => tr.dataset.id) };
  q(".th-sort[data-sort=loop]").click();
  await settle();
  out.sortDesc = { sort: store.sort.value, aria: boardDump().sortAria,
    rows: qa("tr.loop-row").map((tr) => tr.dataset.id) };
  q(".th-sort[data-sort=activity]").click();
  await settle();
  out.sortActivity = { sort: store.sort.value, aria: boardDump().sortAria };
  // no match
  const search = q("#loop-search");
  search.value = "zzz-nothing";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  await settle();
  out.noMatch = boardDump().rows;
  unmount();
  return out;
});

test("mark_read_failure_shows_the_banner", async () => {
  const { store, unmount } = await setup({
    reply: () => ({ ok: false, status: 500, data: { error: "nope" } }),
  });
  q("#needs-list .attn-side button").click();
  await settle();
  const failed = bannerDump();
  const needsStill = qa("#needs-list > .attn-row").length;
  store.setFeed({ status: "live", mode: "stream", lastOk: NOW + 5, updatedAt: iso(NOW + 4) });
  await settle();
  const cleared = bannerDump();
  unmount();
  return { failed, needsStill, cleared };
});

test("attention_ordering_and_unread_flags", async () => {
  const { store, unmount } = await setup();
  const dump = boardDump();
  // a running loop with a note-level item is a "needs you" row
  const items = [
    item({ id: "n1", loop: "alpha-build", severity: "low", kind: "drift", headline: "Alpha drift" }),
  ];
  const snap = snapshot();
  snap.workspaces[0].inbox = snap.workspaces[0].inbox.concat(items);
  store.applySnapshot(Object.assign(snap, { epoch: "e1", seq: 2 }));
  await settle();
  const after = boardDump();
  unmount();
  return { notesBefore: dump.notes, needsBefore: dump.needs.map((r) => r.key),
    needsAfter: after.needs.map((r) => ({ key: r.key, badge: r.badge })),
    rowFlags: after.rows.map((r) => [r.id, r.flags]) };
});

for (const [name, fn] of Object.entries(cases)) {
  try {
    results[name] = { value: await fn() };
  } catch (err) {
    results[name] = { error: String((err && err.stack) || err) };
  }
}
window.__uiDone = true;
