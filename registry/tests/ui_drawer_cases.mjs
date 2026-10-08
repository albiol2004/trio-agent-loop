/* Cases for dashboard/ui/drawer.js, run in the browser harness
 * (ui_browser.cjs): the real vendored preact + signals + htm, the real store,
 * the real index.html drawer shell, a real MutationObserver. fetch is a stub
 * answering /api/loop with fixture detail JSON shaped like serve.py's
 * _handle_loop_detail; the clock (store.now) is driven by hand. Sets
 * window.__uiResults and window.__uiDone; test_ui_drawer.py asserts on the
 * values. */
import { createStore } from "/ui/store.js";
import { mountDrawer } from "/ui/drawer.js";

const results = {};
window.__uiResults = results;

let rootCounter = 0; // notes are module state keyed by loop key: one root per case keeps cases apart
const NOW = Date.parse("2026-06-01T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
const settle = (ms = 120) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (id) => document.getElementById(id);
const text = (id) => $(id).textContent;

/* ------------------------------ fixtures ------------------------------ */

function card(name, extra = {}) {
  return Object.assign({
    name, loop_id: "lid-" + name, title: name[0].toUpperCase() + name.slice(1),
    mission: "Do " + name, status: "iterating", iteration: 2, max_iterations: 5,
    final_verdict: null, running: false, running_sources: [],
    last_activity: iso(NOW - 4 * 60000), segments: [{ verdict_sequence: "IS" }],
    driver_phase: "idle",
    controls: {
      driver: "portable",
      start: { enabled: true, reason: "Start the portable driver" },
      stop: { enabled: false, reason: "Not running" },
    },
  }, extra);
}

function snapshot(cards, root) {
  return {
    workspaces: [{ root, name: "a", elapsed_ms: 3, unattributed_processes: 0, loops: cards, inbox: [] }],
    scanned: 1, worktrees_scanned: 0, scan_roots: [root], broker: "disabled",
    updated_at: iso(NOW), elapsed_ms: 5, epoch: "e1", seq: 1,
  };
}

function commit(n, slice = "ui-board") {
  const sha = String(n).padStart(2, "0").repeat(20);
  return { sha, short: sha.slice(0, 7), slice, subject: "change number " + n };
}

function detailFixture(extra = {}) {
  return Object.assign(card("alpha"), {
    path: "alpha", mission: "Ship the drawer as Preact components.",
    mode: "open-loop",
    commits: Array.from({ length: 10 }, (_, i) => commit(10 - i, i % 2 ? "ui-board" : "ui-drawer")),
    slices: [
      { id: "ui-board", iteration: 1, repo: ".", lifecycle: "shipped", retired_sha: "abcdef1234567", verdict: "SHIP", open_faults: [] },
      { id: "ui-drawer", iteration: 2, repo: "web", lifecycle: "building", open_faults: ["F-1"] },
    ],
    iterations: [{ n: 1, lifecycle: "shipped" }, { n: 2, lifecycle: "building" }],
    timeline: [
      { seq: 1, iteration: 1, role: "lead", summary: "planned the board", verdict: null, scope: null, slice: null, started_at: iso(NOW - 600000), duration_sec: 45 },
      { seq: 2, iteration: 1, role: "evaluator", summary: "VERDICT: SHIP — all good", verdict: "SHIP", scope: "board", slice: "ui-board", started_at: iso(NOW - 500000), duration_sec: 125 },
      { seq: 3, iteration: 2, role: "builder", summary: "wrote the drawer", verdict: null, scope: null, slice: "ui-drawer", started_at: iso(NOW - 300000), duration_sec: null },
    ],
    sessions: [{ id: "s1" }, { id: "s2" }, { id: "s3" }],
    queue: { retired: [], faults: [] },
  }, extra);
}

/* A store with a stubbed network. `net.detail` is what /api/loop answers
 * (a fresh JSON copy per request), `net.hold` makes it wait for release(). */
function setup({ cards, detail, legacy } = {}) {
  const net = {
    detail: detail || detailFixture(), posts: [], hold: null, status: 200, postResult: { ok: true, data: { pid: 4242 } },
  };
  const fetchStub = async (url, opts = {}) => {
    if (opts.method === "POST") {
      net.posts.push({ url, body: JSON.parse(opts.body) });
      const r = net.postResult;
      return { ok: r.ok, status: r.ok ? 200 : 500, json: async () => r.data };
    }
    if (net.hold) await net.hold;
    const body = JSON.parse(JSON.stringify(net.detail));
    return { ok: net.status === 200, status: net.status, json: async () => body };
  };
  const store = createStore({ fetch: fetchStub, storage: null, now: () => NOW });
  rootCounter += 1;
  const root = "/w/case" + rootCounter;
  store.applySnapshot(snapshot(cards || [card("alpha"), card("beta")], root));
  const calls = { open: [], close: 0, detail: [], tab: [] };
  const rel = (key) => (key ? key.slice(root.length) : key); // "::alpha"
  const hooks = legacy || {
    onOpen: (loop) => calls.open.push(rel(loop.key)),
    onClose: () => { calls.close += 1; },
    onDetail: (d, loop) => calls.detail.push({ mission: d.mission, loop: loop && rel(loop.key) }),
    onTab: (tab, loop) => calls.tab.push({ tab, loop: loop && rel(loop.key) }),
  };
  const unmount = mountDrawer(store, { legacy: hooks });
  return { store, net, calls, unmount, root, key: root + "::alpha", beta: root + "::beta" };
}

function teardown(ctx) {
  ctx.store.closeLoop();
  ctx.unmount();
}

function originButton() {
  let button = $("origin-btn");
  if (!button) {
    button = document.createElement("button");
    button.id = "origin-btn";
    button.textContent = "origin";
    $("main").appendChild(button);
  }
  button.focus();
  return button;
}

function observe(nodes) {
  const records = [];
  const observer = new MutationObserver((list) => {
    for (const r of list) records.push(r);
  });
  for (const node of nodes) {
    observer.observe(node, { subtree: true, childList: true, attributes: true, characterData: true });
  }
  return {
    records,
    flush() { for (const r of observer.takeRecords()) records.push(r); return records; },
    stop() { this.flush(); observer.disconnect(); return records; },
  };
}

const inReltime = (node) => {
  const el = node.nodeType === 1 ? node : node.parentElement;
  return Boolean(el && el.closest("[data-reltime]"));
};

const rowsOf = (id, cls) => Array.from($(id).querySelectorAll(cls));

const cases = {};
const test = (name, fn) => { cases[name] = fn; };

/* --------------------------------- cases --------------------------------- */

test("open_renders_the_detail", async () => {
  const ctx = setup();
  const origin = originButton();
  const before = { hidden: $("drawer").hidden, scrimHidden: $("drawer-scrim").hidden };
  await ctx.store.openLoop(ctx.key);
  await settle();
  const v = {
    before,
    drawerHidden: $("drawer").hidden,
    scrimHidden: $("drawer-scrim").hidden,
    inert: ["main", "appbar"].map((id) => (document.getElementById(id) || document.querySelector("." + id) || {}).inert),
    activeId: document.activeElement && document.activeElement.id,
    originFocused: document.activeElement === origin,
    name: text("drawer-name"),
    where: text("drawer-where"),
    whereTitle: $("drawer-where").title,
    badge: text("drawer-badge"),
    badgeClass: $("drawer-badge").className,
    mission: text("drawer-mission"),
    missionTitle: $("drawer-mission").title,
    factIter: text("fact-iter"),
    factVerdict: text("fact-verdict"),
    factVerdictClass: $("fact-verdict").className,
    factActivity: text("fact-activity"),
    factActivityIsTime: Boolean($("fact-activity").querySelector("time[data-reltime]")),
    factSessions: text("fact-sessions"),
    strip: text("drawer-strip"),
    stripCells: rowsOf("drawer-strip", ".hist-cell").map((c) => c.textContent),
    commitsHidden: $("commits-section").hidden,
    commitRows: rowsOf("commit-list", ".commit-row").map((r) => ({
      sha: r.querySelector(".commit-sha").textContent,
      slice: (r.querySelector(".slice-chip") || {}).textContent,
      subject: r.querySelector(".commit-subject").textContent,
    })),
    moreButton: (rowsOf("commit-list", "button")[0] || {}).textContent,
    slicesHidden: $("slices-section").hidden,
    sliceRows: rowsOf("slice-list", ".slice-row").map((r) => ({
      text: r.textContent,
      chip: r.querySelector(".lifecycle-chip").className,
      repo: (r.querySelector(".slice-repo") || {}).textContent,
      verdict: (r.querySelector(".verdict-word") || {}).textContent,
      fault: (r.querySelector(".fault-chip") || {}).textContent,
    })),
    headers: rowsOf("drawer-timeline", ".iter-header").map((h) => h.textContent),
    timelineRows: rowsOf("drawer-timeline", ".timeline-row").map((r) => ({
      role: r.querySelector(".role-chip").textContent,
      dur: r.querySelector(".timeline-dur").textContent,
      text: r.querySelector(".timeline-text").textContent,
    })),
    tabs: rowsOf("drawer-tabs", "button").map((b) => ({
      text: b.textContent, active: b.classList.contains("active"), pressed: b.getAttribute("aria-pressed"),
    })),
    views: Object.fromEntries(["overview", "timeline", "files", "graph", "transcripts"]
      .map((t) => [t, $("view-" + t).hidden])),
    controlsHidden: $("drawer-controls").hidden,
    openKey: ctx.store.openKey.value, root: ctx.root,
    open: ctx.calls.open,
    detail: ctx.calls.detail,
    close: ctx.calls.close,
    tabCalls: ctx.calls.tab,
  };
  teardown(ctx);
  return v;
});

test("loading_state_until_the_detail_arrives", async () => {
  const ctx = setup();
  let release;
  ctx.net.hold = new Promise((resolve) => { release = resolve; });
  const pending = ctx.store.openLoop(ctx.key);
  await settle();
  const loading = {
    badge: text("drawer-badge"), timeline: text("drawer-timeline"), mission: text("drawer-mission"),
    factIter: text("fact-iter"), factVerdict: text("fact-verdict"), factActivity: text("fact-activity"),
    factSessions: text("fact-sessions"), commitsHidden: $("commits-section").hidden,
    name: text("drawer-name"), detailCalls: ctx.calls.detail.length, openCalls: ctx.calls.open.length,
  };
  release();
  await pending;
  await settle();
  const loaded = { badge: text("drawer-badge"), factIter: text("fact-iter"), detailCalls: ctx.calls.detail.length };
  teardown(ctx);
  return { loading, loaded };
});

test("detail_failure_is_shown_in_the_timeline", async () => {
  const ctx = setup();
  ctx.net.status = 500;
  await ctx.store.openLoop(ctx.key);
  await settle();
  const v = { timeline: text("drawer-timeline"), badge: text("drawer-badge"), detailCalls: ctx.calls.detail.length };
  teardown(ctx);
  return v;
});

test("equal_detail_and_clock_ticks_write_only_reltime_text", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const before = text("fact-activity");
  const detailBefore = ctx.store.detail.value;
  const watch = observe([$("drawer")]);
  await ctx.store.refreshDetail(); // a fresh, deep-equal object
  await settle();
  const sameDetail = ctx.store.detail.value === detailBefore;
  ctx.store.now.value = NOW + 70000;
  await settle(200);
  ctx.store.now.value = NOW + 140000;
  await settle(200);
  const records = watch.stop();
  const v = {
    before, after: text("fact-activity"), sameDetail,
    detailCalls: ctx.calls.detail.length,
    childList: records.filter((r) => r.type === "childList").length,
    attributes: records.filter((r) => r.type === "attributes").map((r) => r.attributeName),
    characterData: records.filter((r) => r.type === "characterData").length,
    characterDataOutsideReltime: records
      .filter((r) => r.type === "characterData" && !inReltime(r.target)).length,
  };
  teardown(ctx);
  return v;
});

test("a_changed_unrelated_loop_card_rewrites_nothing_in_the_drawer", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const watch = observe([$("drawer")]);
  // beta changes; alpha (the open loop) does not
  ctx.store.applyOverview(snapshot([card("alpha"), card("beta", { iteration: 3 })], ctx.root));
  await settle();
  const records = watch.stop();
  const v = {
    childList: records.filter((r) => r.type === "childList").length,
    attributes: records.filter((r) => r.type === "attributes").length,
    characterData: records.length - records.filter((r) => r.type !== "characterData").length,
  };
  teardown(ctx);
  return v;
});

test("live_running_state_reaches_the_badge_without_a_refetch", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const before = text("drawer-badge");
  ctx.store.applyOverview(snapshot([card("alpha", { running: true, running_sources: ["driver"] }), card("beta")], ctx.root));
  await settle();
  const v = {
    before, after: text("drawer-badge"), badgeClass: $("drawer-badge").querySelector(".badge").className,
    detailCalls: ctx.calls.detail.length,
  };
  teardown(ctx);
  return v;
});

test("show_all_commits_survives_a_changed_detail", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const key = ctx.key;
  const collapsed = rowsOf("commit-list", ".commit-row").length;
  const firstRow = rowsOf("commit-list", ".commit-row")[0];
  rowsOf("commit-list", "button")[0].click();
  await settle();
  const expandedRows = rowsOf("commit-list", ".commit-row");
  const afterToggle = {
    rows: expandedRows.length, expanded: ctx.store.isExpanded("commits:" + key),
    button: rowsOf("commit-list", "button")[0].textContent,
  };
  const keptNode = expandedRows[0] === firstRow;
  const newest = commit(11, "ui-drawer");
  ctx.net.detail = detailFixture({ commits: [newest, ...detailFixture().commits] });
  await ctx.store.refreshDetail();
  await settle();
  const after = rowsOf("commit-list", ".commit-row");
  const v = {
    collapsed, afterToggle, keptNode,
    afterRows: after.length,
    first: after[0].querySelector(".commit-sha").textContent,
    expanded: ctx.store.isExpanded("commits:" + key),
    button: rowsOf("commit-list", "button")[0].textContent,
    sameNodeForOldRow: after[1] === expandedRows[0],
    detailCalls: ctx.calls.detail.length,
  };
  // collapse again
  rowsOf("commit-list", "button")[0].click();
  await settle();
  v.collapsedAgain = rowsOf("commit-list", ".commit-row").length;
  v.buttonAgain = rowsOf("commit-list", "button")[0].textContent;
  teardown(ctx);
  return v;
});

test("few_commits_and_no_slices_hide_their_sections", async () => {
  const ctx = setup({ detail: detailFixture({ commits: [commit(1)], slices: [], timeline: [] }) });
  await ctx.store.openLoop(ctx.key);
  await settle();
  const v = {
    commitsHidden: $("commits-section").hidden, rows: rowsOf("commit-list", ".commit-row").length,
    buttons: rowsOf("commit-list", "button").length, slicesHidden: $("slices-section").hidden,
    timeline: text("drawer-timeline"),
  };
  teardown(ctx);
  return v;
});

test("escape_closes_and_restores_focus_unless_a_dialog_is_open", async () => {
  const ctx = setup();
  const origin = originButton();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const opened = { hidden: $("drawer").hidden, active: document.activeElement.id };
  const dialog = $("confirm-dialog");
  dialog.showModal();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle();
  const withDialog = {
    hidden: $("drawer").hidden, openKey: ctx.store.openKey.value, close: ctx.calls.close,
  };
  dialog.close();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle();
  const v = {
    opened, withDialog,
    closed: {
      drawerHidden: $("drawer").hidden, scrimHidden: $("drawer-scrim").hidden,
      openKey: ctx.store.openKey.value, close: ctx.calls.close,
      restored: document.activeElement === origin,
      mainInert: $("main").inert, appbarInert: document.querySelector(".appbar").inert,
      badge: text("drawer-badge"), name: text("drawer-name"),
    },
  };
  // Escape with the drawer closed does nothing more
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle();
  v.closeAfterIdleEscape = ctx.calls.close;
  teardown(ctx);
  return v;
});

test("close_button_and_scrim_close_the_drawer", async () => {
  const ctx = setup();
  const origin = originButton();
  await ctx.store.openLoop(ctx.key);
  await settle();
  $("drawer-close").click();
  await settle();
  const byButton = { hidden: $("drawer").hidden, openKey: ctx.store.openKey.value, restored: document.activeElement === origin };
  await ctx.store.openLoop(ctx.beta);
  await settle();
  const reopened = { hidden: $("drawer").hidden, name: text("drawer-name"), where: text("drawer-where"), active: document.activeElement.id };
  $("drawer-scrim").click();
  await settle();
  const byScrim = { hidden: $("drawer").hidden, scrimHidden: $("drawer-scrim").hidden, openKey: ctx.store.openKey.value };
  const v = { byButton, reopened, byScrim, close: ctx.calls.close, open: ctx.calls.open };
  teardown(ctx);
  return v;
});

test("tab_key_wraps_focus_inside_the_open_drawer", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  $("drawer-close").focus();
  const event = new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true });
  document.dispatchEvent(event);
  const wrapped = $("drawer").contains(document.activeElement) && document.activeElement.id !== "drawer-close";
  const prevented = event.defaultPrevented;
  const forward = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
  const lastBefore = document.activeElement;
  document.dispatchEvent(forward);
  const v = {
    wrapped, prevented, forwardPrevented: forward.defaultPrevented,
    backToFirst: document.activeElement.id === "drawer-close", lastWasNotFirst: lastBefore.id !== "drawer-close",
  };
  teardown(ctx);
  return v;
});

test("tabs_switch_the_views_and_never_touch_the_legacy_islands", async () => {
  const ctx = setup();
  const islands = ["view-timeline", "view-files", "view-graph", "view-transcripts", "actions-section", "drawer-inbox-list"]
    .map($);
  const snapshots = islands.map((n) => n.outerHTML);
  const watch = observe(islands);
  await ctx.store.openLoop(ctx.key);
  await settle();
  const buttons = () => rowsOf("drawer-tabs", "button");
  const filesButton = buttons().find((b) => b.textContent === "Files");
  filesButton.click();
  await settle();
  const views = () => Object.fromEntries(["overview", "timeline", "files", "graph", "transcripts"]
    .map((t) => [t, $("view-" + t).hidden]));
  const files = {
    views: views(), tab: ctx.store.drawerTab.value, tabCalls: ctx.calls.tab.slice(),
    pressed: buttons().map((b) => [b.textContent, b.getAttribute("aria-pressed"), b.classList.contains("active")]),
    sameButton: buttons().find((b) => b.textContent === "Files") === filesButton,
  };
  for (const name of ["Timeline", "Graph", "Transcripts", "Overview"]) {
    buttons().find((b) => b.textContent === name).click();
    await settle();
  }
  const afterCycle = { views: views(), tabCalls: ctx.calls.tab.map((c) => c.tab) };
  // a changed detail while on another tab re-renders the overview only
  ctx.store.drawerTab.value = "files";
  await settle();
  ctx.net.detail = detailFixture({ mission: "A new mission.", commits: [commit(11)] });
  await ctx.store.refreshDetail();
  await settle();
  ctx.store.closeLoop();
  await settle();
  const records = watch.stop();
  const v = {
    root: ctx.root, files, afterCycle,
    islandRecords: records.map((r) => r.type + ":" + (r.attributeName || "") + ":" + r.target.id),
    islandsUnchanged: islands.every((n, i) => n.outerHTML === snapshots[i]),
    tabCallsTotal: ctx.calls.tab.map((c) => c.tab),
    tabLoop: ctx.calls.tab[0].loop,
  };
  ctx.unmount();
  return v;
});

test("legacy_hooks_fire_once_per_change", async () => {
  const ctx = setup();
  await ctx.store.openLoop(ctx.key);
  await settle();
  const afterOpen = { open: ctx.calls.open.slice(), detail: ctx.calls.detail.length };
  await ctx.store.refreshDetail(); // equal
  await settle();
  const afterEqual = ctx.calls.detail.length;
  ctx.net.detail = detailFixture({ mission: "Changed." });
  await ctx.store.refreshDetail();
  await settle();
  const afterChanged = ctx.calls.detail.map((d) => d.mission);
  ctx.store.now.value = NOW + 600000; // a tick calls nothing
  await settle(200);
  const afterTick = { detail: ctx.calls.detail.length, open: ctx.calls.open.length, close: ctx.calls.close };
  await ctx.store.openLoop(ctx.beta);
  await settle();
  const afterSecond = { open: ctx.calls.open.slice(), close: ctx.calls.close, detail: ctx.calls.detail.length };
  ctx.store.closeLoop();
  await settle();
  const v = {
    afterOpen, afterEqual, afterChanged, afterTick, afterSecond,
    afterClose: { close: ctx.calls.close, open: ctx.calls.open.length, tabs: ctx.calls.tab.length },
  };
  ctx.unmount();
  return v;
});

test("legacy_hooks_are_optional", async () => {
  const ctx = setup({ legacy: {} });
  await ctx.store.openLoop(ctx.key);
  await settle();
  const opened = !$("drawer").hidden;
  ctx.store.drawerTab.value = "graph";
  await settle();
  ctx.store.closeLoop();
  await settle();
  const v = { opened, closed: $("drawer").hidden };
  ctx.unmount();
  return v;
});

test("stop_asks_for_confirmation_and_posts", async () => {
  const running = card("alpha", {
    running: true, running_sources: ["driver"],
    controls: {
      driver: "portable",
      start: { enabled: false, reason: "Already running" },
      stop: { enabled: true, reason: "Stops PID 12" },
    },
  });
  const ctx = setup({ cards: [running, card("beta")] });
  const confirms = [];
  let answer = false;
  const realConfirm = window.confirm;
  window.confirm = (message) => { confirms.push(message); return answer; };
  try {
    await ctx.store.openLoop(ctx.key);
    await settle();
    const shown = {
      hidden: $("drawer-controls").hidden, startDisabled: $("loop-start").disabled,
      stopDisabled: $("loop-stop").disabled, startTitle: $("loop-start").title,
      stopTitle: $("loop-stop").title, note: text("loop-control-note"),
      noteClass: $("loop-control-note").className, noteRole: $("loop-control-note").getAttribute("role"),
    };
    $("loop-stop").click();
    await settle();
    const declined = { confirms: confirms.slice(), posts: ctx.net.posts.length, note: text("loop-control-note") };
    answer = true;
    $("loop-stop").click();
    await settle(200);
    const stopped = {
      root: ctx.root, posts: ctx.net.posts.slice(), note: text("loop-control-note"),
      noteClass: $("loop-control-note").className, stopDisabled: $("loop-stop").disabled,
    };
    ctx.net.postResult = { ok: false, data: { error: "boom" } };
    $("loop-stop").click();
    await settle(200);
    const failed = {
      note: text("loop-control-note"), noteClass: $("loop-control-note").className,
      noteRole: $("loop-control-note").getAttribute("role"),
    };
    // a disabled start button posts nothing
    $("loop-start").click();
    await settle();
    return { shown, declined, stopped, failed, totalPosts: ctx.net.posts.length };
  } finally {
    window.confirm = realConfirm;
    teardown(ctx);
  }
});

test("start_posts_without_confirmation_and_notes_survive_a_reopen", async () => {
  const ctx = setup();
  const realConfirm = window.confirm;
  let confirms = 0;
  window.confirm = () => { confirms += 1; return true; };
  try {
    await ctx.store.openLoop(ctx.key);
    await settle();
    const idle = { note: text("loop-control-note"), startDisabled: $("loop-start").disabled };
    $("loop-start").click();
    await settle(200);
    const started = { root: ctx.root, posts: ctx.net.posts.slice(), note: text("loop-control-note"), confirms };
    ctx.store.closeLoop();
    await settle();
    await ctx.store.openLoop(ctx.key);
    await settle();
    return { idle, started, reopenedNote: text("loop-control-note") };
  } finally {
    window.confirm = realConfirm;
    teardown(ctx);
  }
});

test("server_action_record_shows_a_reltime_note", async () => {
  const withAction = card("alpha", {
    last_action: { outcome: "finished", message: "Loop finished", updated_at: iso(NOW - 3 * 60000) },
  });
  const ctx = setup({ cards: [withAction, card("beta")] });
  await ctx.store.openLoop(ctx.key);
  await settle();
  const note = $("loop-control-note");
  const before = note.textContent;
  const watch = observe([note]);
  ctx.store.now.value = NOW + 120000;
  await settle(200);
  const records = watch.stop();
  const v = {
    before, after: note.textContent, noteClass: note.className,
    hasTime: Boolean(note.querySelector("time[data-reltime]")),
    types: Array.from(new Set(records.map((r) => r.type))),
    allInReltime: records.every((r) => inReltime(r.target)),
  };
  teardown(ctx);
  return v;
});

for (const [name, fn] of Object.entries(cases)) {
  try {
    results[name] = { value: await fn() };
  } catch (err) {
    results[name] = { error: String((err && err.stack) || err) };
  }
}
window.__uiDone = true;
