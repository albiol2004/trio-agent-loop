/* Cases for dashboard/ui/store.js and transport.js, run in node:
 *
 *   node --import ./registry/tests/ui_esm_register.mjs registry/tests/ui_store_cases.mjs
 *
 * Prints ONE JSON object {caseName: {value} | {error}}; test_ui_store.py
 * asserts on the values. Everything the product touches is a fake: fetch,
 * EventSource, the document/window, storage and the timers (fake time). */
import { effect } from "@preact/signals-core";
import { createStore, loopKey, loopEntityId } from "../../dashboard/ui/store.js";
import { startTransport, bindLocation } from "../../dashboard/ui/transport.js";
import {
  relTime, fmtSize, fmtDuration, oneLine, plural, normVerdict, verdictSeq, latestVerdict,
  isArchived, statusWord, loopTitle, iterationText, phaseText, derivedStateOf, stateBadgeInfo,
} from "../../dashboard/ui/format.js";

const ROOT_A = "/w/a";
const ROOT_B = "/w/b";
const T0 = Date.parse("2026-06-01T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");

/* ------------------------------ fixtures ------------------------------ */

function card(name, extra = {}) {
  return Object.assign({
    name, loop_id: "lid-" + name, title: name[0].toUpperCase() + name.slice(1),
    mission: "Do " + name, status: "iterating", iteration: 2, max_iterations: 5,
    final_verdict: null, running: false, running_sources: [],
    last_activity: "2026-05-30T08:00:00+00:00", segments: [{ verdict_sequence: "IS" }],
    driver_phase: "idle",
  }, extra);
}

function item(id, loop, extra = {}) {
  return Object.assign({
    id, loop, kind: "needs_human", severity: "high", headline: loop + " needs a human",
    detail: "answer HUMAN.md", read: false,
  }, extra);
}

function snapshot(epoch = "e1", seq = 7) {
  return {
    workspaces: [
      {
        root: ROOT_A, name: "a", elapsed_ms: 3, unattributed_processes: 0,
        loops: [card("alpha", { running: true, running_sources: ["driver"] }),
          card("beta", { final_verdict: "SHIP" })],
        inbox: [item("i1", "alpha"),
          item("i2", "beta", { kind: "drift", severity: "low", headline: "beta drifted", detail: "" })],
      },
      { root: ROOT_B, name: "b", worktree: true, elapsed_ms: 2, loops: [card("gamma")], inbox: [] },
    ],
    scanned: 2, worktrees_scanned: 1, scan_roots: [ROOT_A, ROOT_B], broker: "disabled",
    updated_at: "2026-06-01T11:59:58+00:00", elapsed_ms: 12, epoch, seq,
  };
}

function overviewOf(snap) {
  const copy = JSON.parse(JSON.stringify(snap));
  delete copy.epoch;
  delete copy.seq;
  return copy;
}

const loopChange = (op, root, c) => ({
  op, kind: "loop", id: c.loop_id, data: op === "upsert" ? { root, card: c } : null,
});
const inboxChange = (op, root, it) => ({
  op, kind: "inbox", id: it.id, data: op === "upsert" ? { root, item: it } : null,
});
const delta = (changes, extra = {}) => Object.assign({
  epoch: "e1", seq: 8, from_seq: 7, updated_at: "2026-06-01T12:00:01+00:00", changes,
}, extra);

function detailOf(extra = {}) {
  return Object.assign({
    name: "alpha", mission: "Do alpha", status: "iterating", iteration: 2,
    commits: [{ sha: "a".repeat(40), short: "aaaaaaa", subject: "slice(s1): first" }],
    sessions: [],
  }, extra);
}

/* ------------------------------- fakes -------------------------------- */

function makeClock(start = T0) {
  const clock = { now: start, timers: [], nextId: 1 };
  clock.setTimeout = (fn, ms) => {
    const t = { id: clock.nextId++, at: clock.now + ms, fn, every: 0 };
    clock.timers.push(t);
    return t.id;
  };
  clock.setInterval = (fn, ms) => {
    const t = { id: clock.nextId++, at: clock.now + ms, fn, every: ms };
    clock.timers.push(t);
    return t.id;
  };
  clock.clear = (id) => {
    clock.timers = clock.timers.filter((t) => t.id !== id);
  };
  clock.settle = async () => {
    for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve));
  };
  clock.advance = async (ms) => {
    const end = clock.now + ms;
    for (;;) {
      const due = clock.timers.filter((t) => t.at <= end)
        .sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      clock.now = Math.max(clock.now, due.at);
      if (due.every) due.at += due.every;
      else clock.timers = clock.timers.filter((t) => t !== due);
      due.fn();
      await clock.settle();
    }
    clock.now = end;
    await clock.settle();
  };
  return clock;
}

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  close() {
    this.readyState = 2;
  }
  emit(type, data) {
    this.readyState = 1;
    const ev = { data: JSON.stringify(data), lastEventId: (data.epoch || "") + ":" + (data.seq ?? "") };
    for (const fn of this.listeners[type] || []) fn(ev);
  }
  fail(closed) {
    this.readyState = closed ? 2 : 0;
    for (const fn of this.listeners.error || []) fn({});
  }
}

function makeFetch(routes, calls) {
  return async (url, init = {}) => {
    const path = String(url).split("?")[0];
    const call = {
      url: String(url), path, method: init.method || "GET",
      body: init.body ? JSON.parse(init.body) : null, cache: init.cache || null,
    };
    calls.push(call);
    const route = routes[path];
    if (route === undefined) return { ok: false, status: 404, json: async () => ({ error: "no route" }) };
    const out = typeof route === "function" ? await route(call) : route;
    const status = out && out.__status ? out.__status : 200;
    const body = out && out.__status ? out.body : out;
    return {
      ok: status < 400, status,
      json: async () => JSON.parse(JSON.stringify(body === undefined ? {} : body)),
    };
  };
}

function makeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
  };
}

function makeDocument() {
  const listeners = [];
  const doc = {
    hidden: false,
    addEventListener: (type, fn) => { if (type === "visibilitychange") listeners.push(fn); },
    removeEventListener: (type, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
    setHidden(value) {
      doc.hidden = value;
      for (const fn of listeners.slice()) fn();
    },
    listenerCount: () => listeners.length,
  };
  return doc;
}

function makeWindow(hash = "") {
  const listeners = [];
  const win = {
    location: { hash, pathname: "/" },
    history: {
      replaceState(state, title, url) {
        if (typeof url === "string" && url.startsWith("#")) win.location.hash = url;
        else win.location.hash = "";
      },
    },
    addEventListener: (type, fn) => { if (type === "hashchange") listeners.push(fn); },
    removeEventListener: (type, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
    setHash(value) {
      win.location.hash = value;
      for (const fn of listeners.slice()) fn();
    },
  };
  return win;
}

/* A store plus fakes. `routes` map a path to a body or a (call) => body. */
function world(routes = {}, options = {}) {
  FakeEventSource.instances = [];
  const clock = makeClock();
  const calls = [];
  const defaults = {
    "/api/overview": () => Object.assign(overviewOf(snapshot()), { updated_at: iso(clock.now - 2000) }),
    "/api/loop": () => detailOf(),
  };
  const fetch = makeFetch(Object.assign(defaults, routes), calls);
  const storage = options.storage || makeStorage();
  const store = createStore({
    fetch, EventSource: FakeEventSource, storage, now: () => clock.now,
    setInterval: clock.setInterval, clearInterval: clock.clear,
  });
  const doc = makeDocument();
  const transportOpts = Object.assign({
    fetch, EventSource: FakeEventSource, document: doc, now: () => clock.now,
    setTimeout: clock.setTimeout, clearTimeout: clock.clear,
  }, options.transport || {});
  return {
    clock, calls, store, storage, doc, fetch,
    start: () => startTransport(store, transportOpts),
    es: () => FakeEventSource.instances,
    paths: (path) => calls.filter((c) => c.path === path),
    clearCalls: () => { calls.length = 0; },
  };
}

/* Counts how often a signal notifies after its first run. */
function watch(sig) {
  const counter = { n: 0 };
  let first = true;
  effect(() => {
    sig.value; // eslint-disable-line no-unused-expressions
    if (first) first = false;
    else counter.n += 1;
  });
  return counter;
}

function watchAll(store) {
  const out = {
    loopIds: watch(store.loopIds), inboxIds: watch(store.inboxIds), loops: watch(store.loops),
    inbox: watch(store.inbox), meta: watch(store.meta), workspaces: watch(store.workspaces),
    epoch: watch(store.epoch), seq: watch(store.seq),
  };
  for (const id of store.loopIds.peek()) out["loop:" + id] = watch(store.loopSig(id));
  for (const id of store.inboxIds.peek()) out["inbox:" + id] = watch(store.inboxSig(id));
  return out;
}

const counts = (watched) => Object.fromEntries(Object.entries(watched).map(([k, v]) => [k, v.n]));

/* ------------------------------- cases -------------------------------- */

const cases = {};
const test = (name, fn) => { cases[name] = fn; };

test("entity_ids", () => ({
  keyed: loopKey("/w/a", "alpha"),
  withLoopId: loopEntityId("/w/a", { loop_id: "lid-1", name: "alpha" }),
  pathId: loopEntityId("/w/a", { name: "alpha" }),
}));

test("snapshot_builds_keyed_entities", () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  const alpha = w.store.loopSig("lid-alpha").value;
  const gamma = w.store.loopSig("lid-gamma").value;
  const i2 = w.store.inboxSig("i2").value;
  return {
    loaded: w.store.loaded.value, epoch: w.store.epoch.value, seq: w.store.seq.value,
    loopIds: w.store.loopIds.value, inboxIds: w.store.inboxIds.value,
    alpha: { id: alpha.id, key: alpha.key, root: alpha.root, workspace: alpha.workspace, worktree: alpha.worktree, running: alpha.running },
    gamma: { key: gamma.key, workspace: gamma.workspace, worktree: gamma.worktree },
    item: { root: i2.root, workspace: i2.workspace, key: i2.key, kind: i2.kind },
    byKey: w.store.loopByKey(ROOT_B + "::gamma").value.id,
    missing: w.store.loopByKey("/nope::x") === undefined,
    loops: w.store.loops.value.map((l) => l.name),
    inbox: w.store.inbox.value.map((i) => i.id),
    workspaces: w.store.workspaces.value.map((h) => h.name),
    metaKeys: Object.keys(w.store.meta.value).sort(),
    scanned: w.store.meta.value.scanned,
  };
});

test("delta_notifies_only_the_changed_loop", () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  const watched = watchAll(w.store);
  const x = card("beta", { final_verdict: "SHIP", iteration: 9 });
  const result = w.store.applyDelta(delta([loopChange("upsert", ROOT_A, x)]));
  return {
    ok: result.ok, loopIds: Array.from(result.loopIds), kinds: Array.from(result.kinds),
    afterDelta: counts(watched), seq: w.store.seq.value,
    betaIteration: w.store.loopSig("lid-beta").value.iteration,
  };
});

test("identical_snapshot_and_overview_notify_nobody", () => {
  const w = world();
  const snap = snapshot();
  w.store.applySnapshot(snap);
  const watched = watchAll(w.store);
  w.store.applySnapshot(snapshot());
  const afterSnapshot = counts(watched);
  w.store.applyOverview(overviewOf(snapshot()));
  const afterOverview = counts(watched);
  const changed = overviewOf(snapshot());
  changed.workspaces[1].loops[0].iteration = 4;
  w.store.applyOverview(changed);
  return { afterSnapshot, afterOverview, afterChange: counts(watched) };
});

test("delta_refused_on_gap_or_foreign_epoch", () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  const watched = watchAll(w.store);
  const change = [loopChange("upsert", ROOT_B, card("gamma", { iteration: 9 }))];
  const gap = w.store.applyDelta(delta(change, { from_seq: 9, seq: 10 }));
  const foreign = w.store.applyDelta(delta(change, { epoch: "e2" }));
  const replay = w.store.applyDelta(delta(change, { from_seq: 6, seq: 7 }));
  const noChanges = w.store.applyDelta({ epoch: "e1", from_seq: 7, seq: 8 });
  const empty = w.store.applyDelta(null);
  return {
    gap: gap.ok, foreign: foreign.ok, replay: replay.ok, noChanges: noChanges.ok, empty: empty.ok,
    gapIds: gap.loopIds.size, seq: w.store.seq.value, epoch: w.store.epoch.value,
    gammaIteration: w.store.loopSig("lid-gamma").value.iteration,
    notified: Object.values(counts(watched)).reduce((a, b) => a + b, 0),
  };
});

test("delta_membership_order_and_removal", () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  const watched = watchAll(w.store);
  const delta1 = w.store.applyDelta(delta([
    loopChange("upsert", ROOT_B, card("delta")),
    loopChange("remove", ROOT_A, card("alpha")),
    inboxChange("remove", ROOT_A, item("i1", "alpha")),
  ]));
  const afterFirst = { loopIds: w.store.loopIds.value, inboxIds: w.store.inboxIds.value };
  const removedSig = w.store.loopSig("lid-alpha");
  const removedKey = w.store.loopByKey(ROOT_A + "::alpha");
  const counted = counts(watched);
  // an identical upsert (same JSON, other key order) must not notify
  const betaSame = {};
  for (const k of Object.keys(card("beta", { final_verdict: "SHIP" })).reverse()) {
    betaSame[k] = card("beta", { final_verdict: "SHIP" })[k];
  }
  const before = counts(watched);
  w.store.applyDelta(delta([loopChange("upsert", ROOT_A, betaSame)], { from_seq: 8, seq: 9 }));
  const after = counts(watched);
  return {
    ok: delta1.ok, afterFirst, removedSig: removedSig === undefined, removedKey: removedKey === undefined,
    loopIdsNotified: counted.loopIds, inboxIdsNotified: counted.inboxIds,
    reorderedUpsertNotifiedBeta: after["loop:lid-beta"] - before["loop:lid-beta"],
    reorderedUpsertNotifiedLoopIds: after.loopIds - before.loopIds,
  };
});

test("workspace_change_reaches_its_loops_only", () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  const watched = watchAll(w.store);
  const header = { root: ROOT_B, name: "renamed", worktree: true, elapsed_ms: 2 };
  w.store.applyDelta(delta([{ op: "upsert", kind: "workspace", id: ROOT_B, data: header }]));
  return {
    gamma: w.store.loopSig("lid-gamma").value.workspace,
    alphaNotified: counts(watched)["loop:lid-alpha"],
    gammaNotified: counts(watched)["loop:lid-gamma"],
    headers: w.store.workspaces.value.map((h) => h.name),
  };
});

test("prefs_load_and_save", () => {
  const stored = makeStorage({ "trio.board.v2": JSON.stringify({
    tab: "running", workspace: "a", sort: { key: "name", dir: "asc" } }) });
  const w = world({}, { storage: stored });
  const loaded = {
    tab: w.store.tab.value, workspace: w.store.workspace.value, sort: w.store.sort.value,
    query: w.store.query.value,
  };
  w.store.setTab("archived");
  w.store.setWorkspace("b");
  w.store.setSort({ key: "activity", dir: "desc" });
  w.store.setQuery("zz");
  w.store.setTab("bogus");
  const saved = JSON.parse(stored.data.get("trio.board.v2"));
  const bad = world({}, { storage: makeStorage({ "trio.board.v2": "{nope" }) });
  const odd = world({}, { storage: makeStorage({ "trio.board.v2": JSON.stringify({ tab: "weird", sort: 5 }) }) });
  const none = createStore({ fetch: async () => ({}), storage: null });
  none.setTab("running");
  return {
    loaded, saved, tabAfterBogus: w.store.tab.value, queryNotSaved: !("query" in saved),
    corrupt: { tab: bad.store.tab.value, sort: bad.store.sort.value },
    odd: { tab: odd.store.tab.value, sort: odd.store.sort.value },
    noStorage: none.tab.value,
    defaults: {
      tab: world().store.tab.value, workspace: world().store.workspace.value,
      showReadInbox: world().store.showReadInbox.value, drawerTab: world().store.drawerTab.value,
    },
  };
});

test("expanded_and_show_read", () => {
  const w = world();
  const seen = [];
  effect(() => { seen.push(w.store.isExpanded("commits:k")); });
  const before = w.store.expanded.value;
  w.store.toggleExpanded("commits:k");
  const during = w.store.expanded.value;
  w.store.toggleExpanded("commits:k");
  w.store.toggleShowRead();
  return {
    seen, replaced: before !== during, size: w.store.expanded.value.size,
    showRead: w.store.showReadInbox.value,
  };
});

test("set_inbox_read_posts_per_root_and_flips_only_those_items", async () => {
  const w = world({ "/api/inbox/read": { ok: true }, "/api/inbox/unread": { ok: true } });
  const snap = snapshot();
  snap.workspaces[1].inbox = [item("i3", "gamma")];
  w.store.applySnapshot(snap);
  const watched = watchAll(w.store);
  const items = [w.store.inboxSig("i1").value, w.store.inboxSig("i3").value];
  await w.store.setInboxRead(items, true);
  const posts = w.calls.filter((c) => c.method === "POST").map((c) => ({ path: c.path, body: c.body }));
  const read = Object.fromEntries(w.store.inbox.value.map((i) => [i.id, i.read]));
  const notified = counts(watched);
  await w.store.setInboxRead([w.store.inboxSig("i1").value], false);
  return {
    posts, read, notified: { i1: notified["inbox:i1"], i2: notified["inbox:i2"], i3: notified["inbox:i3"], loopIds: notified.loopIds },
    unread: w.store.inboxSig("i1").value.read,
    secondPost: w.calls.filter((c) => c.method === "POST").pop().path,
  };
});

test("set_inbox_read_rejects_with_the_server_error", async () => {
  const w = world({ "/api/inbox/read": () => ({ __status: 403, body: { error: "workspace root is not allowed" } }) });
  w.store.applySnapshot(snapshot());
  let error = null;
  try {
    await w.store.setInboxRead([w.store.inboxSig("i1").value], true);
  } catch (err) {
    error = err.message;
  }
  return { error, stillUnread: w.store.inboxSig("i1").value.read };
});

test("control_loop_posts_root_and_driver", async () => {
  const w = world({
    "/api/loop/start": { pid: 4242 },
    "/api/loop/stop": () => ({ __status: 409, body: { error: "not running" } }),
  });
  const loop = Object.assign({}, card("alpha"), { root: ROOT_A, controls: { driver: "native" } });
  const started = await w.store.controlLoop(loop, "start");
  const stopped = await w.store.controlLoop(Object.assign({}, loop, { controls: {} }), "stop");
  const posts = w.calls.filter((c) => c.method === "POST").map((c) => ({ path: c.path, body: c.body }));
  return { started, stopped, posts };
});

test("open_loop_fetches_detail_and_drops_stale_answers", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const w = world({
    "/api/loop": async (call) => {
      if (call.url.includes("name=alpha")) {
        await gate;
        return detailOf({ name: "alpha" });
      }
      return detailOf({ name: "beta" });
    },
  });
  w.store.applySnapshot(snapshot());
  const first = w.store.openLoop(ROOT_A + "::alpha");
  const statusWhileLoading = w.store.detailStatus.value;
  const second = w.store.openLoop(ROOT_A + "::beta");
  release();
  await first;
  await second;
  await w.clock.settle();
  return {
    url: w.paths("/api/loop")[0].url, cache: w.paths("/api/loop")[0].cache,
    statusWhileLoading, openKey: w.store.openKey.value, detailName: w.store.detail.value.name,
    status: w.store.detailStatus.value, tab: w.store.drawerTab.value,
    fetched: w.paths("/api/loop").map((c) => c.url),
  };
});

test("identical_detail_does_not_reassign", async () => {
  const w = world();
  w.store.applySnapshot(snapshot());
  await w.store.openLoop(ROOT_A + "::alpha");
  const watched = watch(w.store.detail);
  await w.store.refreshDetail();
  await w.store.refreshDetail();
  const unchanged = watched.n;
  return { unchanged, status: w.store.detailStatus.value, fetches: w.paths("/api/loop").length };
});

test("changed_detail_reassigns_and_close_resets", async () => {
  let version = 0;
  const w = world({ "/api/loop": () => detailOf({ mission: "v" + version }) });
  w.store.applySnapshot(snapshot());
  await w.store.openLoop(ROOT_A + "::alpha");
  const watched = watch(w.store.detail);
  version = 1;
  await w.store.refreshDetail();
  const changed = watched.n;
  w.store.closeLoop();
  return {
    changed, mission: w.store.detail.value, status: w.store.detailStatus.value,
    openKey: w.store.openKey.value,
  };
});

test("detail_error_keeps_the_last_detail", async () => {
  let fail = false;
  const w = world({ "/api/loop": () => (fail ? { __status: 500, body: { error: "x" } } : detailOf()) });
  w.store.applySnapshot(snapshot());
  await w.store.openLoop(ROOT_A + "::alpha");
  fail = true;
  await w.store.refreshDetail();
  const kept = { status: w.store.detailStatus.value, hasDetail: w.store.detail.value !== null, error: w.store.detailError.value };
  const w2 = world({ "/api/loop": () => ({ __status: 500, body: {} }) });
  w2.store.applySnapshot(snapshot());
  await w2.store.openLoop(ROOT_A + "::alpha");
  return { kept, first: { status: w2.store.detailStatus.value, error: w2.store.detailError.value } };
});

test("clock_ticks_now_every_5s", async () => {
  const w = world();
  const seen = [];
  effect(() => { seen.push(w.store.now.value); });
  w.store.startClock();
  w.store.startClock();
  await w.clock.advance(12000);
  w.store.stopClock();
  await w.clock.advance(10000);
  return { seen: seen.map((t) => t - T0) };
});

/* ------------------------------- format ------------------------------- */

test("format_helpers", () => {
  const rel = (secs) => relTime(iso(T0 - secs * 1000), T0);
  const badge = (loop) => {
    const info = stateBadgeInfo(loop);
    return [info.className, info.icon, info.text, info.title];
  };
  const base = { name: "n", status: "iterating", segments: [] };
  return {
    rel: [rel(0), rel(4), rel(5), rel(59), rel(60), rel(3599), rel(3600), rel(86399), rel(86400),
      rel(6 * 86400), rel(8 * 86400)],
    relEdge: [relTime(null, T0), relTime("", T0), relTime("garbage", T0),
      relTime(iso(T0 + 60000), T0), relTime("2024-02-03T10:00:00Z", T0)],
    size: [fmtSize(0), fmtSize(1023), fmtSize(1024), fmtSize(1536), fmtSize(1048576), fmtSize("x")],
    duration: [fmtDuration(null), fmtDuration(-1), fmtDuration("x"), fmtDuration(59.6), fmtDuration(60),
      fmtDuration(3599), fmtDuration(3600), fmtDuration(7384)],
    oneLine: [oneLine("  a \n b\tc "), oneLine("x".repeat(200)).length, oneLine("x".repeat(200), 10), oneLine(null)],
    plural: [plural(1, "item"), plural(2, "item"), plural(0, "match", "matches")],
    verdicts: [normVerdict(" ship "), normVerdict("NEEDS_HUMAN"), normVerdict("zzz"), normVerdict(null),
      verdictSeq({ segments: [{ verdict_sequence: "IS" }, { verdict_sequence: "BH" }, {}] }),
      latestVerdict({ final_verdict: " ship " }), latestVerdict({ segments: [{ verdict_sequence: "IB" }] }),
      latestVerdict({})],
    archived: [isArchived({ name: "loop-archive-2026/x" }), isArchived({ name: "loop-a" })],
    statusWord: [statusWord({ status: "unknown" }), statusWord({}), statusWord({ status: " going " })],
    title: [loopTitle({ title: "T", name: "n" }), loopTitle({ mission: "Fix it. Then more", name: "n" }),
      loopTitle({ name: "n" })],
    iteration: [iterationText({}), iterationText({ iteration: 2 }), iterationText({ iteration: 2, max_iterations: 5 })],
    phase: [phaseText({ running: false, driver_phase: "lead" }),
      phaseText({ running: true, driver_phase: "lead-done", running_substate: "both" }),
      phaseText({ running: true, driver_phase: "idle", running_substate: "evaluator" }),
      phaseText({ running: true })],
    derived: [derivedStateOf({ loop_state: { state: "held" } }), derivedStateOf({}), derivedStateOf(null)],
    badges: {
      running: badge({ ...base, running: true, running_sources: ["driver", "lock"] }),
      held: badge({ ...base, loop_state: { state: "held", summary: "waiting" } }),
      unmapped: badge({ ...base, loop_state: { state: "unmapped", status_raw: "WeIrD" } }),
      human: badge({ ...base, status: "awaiting-human" }),
      blocked: badge({ ...base, status: "blocked" }),
      shipped: badge({ ...base, final_verdict: "SHIP" }),
      verdictHuman: badge({ ...base, segments: [{ verdict_sequence: "H" }] }),
      verdictBlocked: badge({ ...base, final_verdict: "BLOCKED" }),
      iterating: badge({ ...base, segments: [{ verdict_sequence: "I" }] }),
      plain: badge({ name: "n", status: "unknown" }),
    },
  };
});

/* ------------------------------ transport ------------------------------ */

const tick = (updatedAt = "2026-06-01T12:00:03+00:00", seq = 7) => ({ epoch: "e1", seq, updated_at: updatedAt });
const sinceOf = (url) => {
  const q = String(url).split("?")[1];
  return q ? decodeURIComponent(new URLSearchParams(q).get("since")) : null;
};

test("stream_snapshot_never_polls_while_healthy", async () => {
  const w = world();
  w.start();
  const first = w.es();
  const initialFeed = w.store.feed.value;
  first[0].emit("snapshot", snapshot());
  for (let i = 0; i < 8; i++) {
    await w.clock.advance(10000);
    first[0].emit("tick", tick(iso(w.clock.now)));
  }
  return {
    sources: first.map((s) => s.url), overviewFetches: w.paths("/api/overview").length,
    initialFeed, feed: w.store.feed.value, loops: w.store.loops.value.map((l) => l.name),
    updatedAt: w.store.feed.value.updatedAt, lastOkAge: w.clock.now - w.store.feed.value.lastOk,
  };
});

test("tick_notifies_feed_only", async () => {
  const w = world();
  w.start();
  w.es()[0].emit("snapshot", snapshot());
  const watched = watchAll(w.store);
  const feed = watch(w.store.feed);
  w.es()[0].emit("tick", tick());
  return { counts: counts(watched), feed: feed.n, updatedAt: w.store.feed.value.updatedAt };
});

test("closed_stream_polls_every_5s_until_a_snapshot", async () => {
  const w = world();
  w.start();
  w.es()[0].fail(true);
  await w.clock.settle();
  const out = { atFailure: w.paths("/api/overview").length };
  out.feedAfterFailure = w.store.feed.value;
  out.loadedFromPoll = w.store.loaded.value;
  for (const label of ["t5", "t10", "t15"]) {
    w.clearCalls();
    await w.clock.advance(5000);
    out[label] = w.paths("/api/overview").length;
  }
  w.clearCalls();
  await w.clock.advance(15000); // 30 s of fallback: the stream is retried
  out.pollsIn15 = w.paths("/api/overview").length;
  out.sources = w.es().length;
  const retried = w.es()[w.es().length - 1];
  retried.emit("snapshot", snapshot("e1", 9));
  w.clearCalls();
  await w.clock.advance(20000);
  out.afterSnapshot = w.paths("/api/overview").length;
  out.feedAfterSnapshot = w.store.feed.value;
  out.retriedUrl = retried.url;
  return out;
});

test("error_while_connecting_is_reconnecting_until_the_next_event", async () => {
  const w = world();
  w.start();
  const es = w.es()[0];
  es.emit("snapshot", snapshot());
  const statuses = [w.store.feed.value.status];
  es.fail(false);
  statuses.push(w.store.feed.value.status);
  const mode = w.store.feed.value.mode;
  es.emit("tick", tick());
  statuses.push(w.store.feed.value.status);
  return { statuses, mode, polls: w.paths("/api/overview").length, sources: w.es().length };
});

test("no_event_source_polls_at_once_and_every_5s", async () => {
  const w = world({}, { transport: { EventSource: undefined } });
  w.start();
  await w.clock.settle();
  const first = w.paths("/api/overview").length;
  w.clearCalls();
  await w.clock.advance(10000);
  return {
    first, later: w.paths("/api/overview").length, sources: w.es().length,
    loops: w.store.loops.value.map((l) => l.name), mode: w.store.feed.value.mode,
    status: w.store.feed.value.status,
  };
});

test("silent_stream_falls_back_after_45s", async () => {
  const w = world();
  w.start();
  w.es()[0].emit("snapshot", snapshot());
  await w.clock.advance(40000);
  const quiet = w.paths("/api/overview").length;
  await w.clock.advance(6000);
  return { quiet, silent: w.paths("/api/overview").length, mode: w.store.feed.value.mode, sourceState: w.es()[0].readyState };
});

test("poll_failure_is_offline_with_the_error", async () => {
  let fail = true;
  const w = world({ "/api/overview": () => (fail ? { __status: 500, body: {} } : overviewOf(snapshot())) },
    { transport: { EventSource: undefined } });
  w.start();
  await w.clock.settle();
  const failed = w.store.feed.value;
  const loadedWhileFailing = w.store.loaded.value;
  fail = false;
  await w.clock.advance(5000);
  return { failed, loadedWhileFailing, recovered: w.store.feed.value, loaded: w.store.loaded.value };
});

test("stale_build_repolls_sooner_once", async () => {
  const w = world({ "/api/overview": () => Object.assign(overviewOf(snapshot()), { updated_at: iso(T0 - 60000) }) },
    { transport: { EventSource: undefined } });
  w.start();
  await w.clock.settle();
  w.clearCalls();
  await w.clock.advance(1600);
  const soon = w.paths("/api/overview").length;
  w.clearCalls();
  await w.clock.advance(1600);
  const notAgain = w.paths("/api/overview").length;
  return { soon, notAgain };
});

test("hidden_tab_closes_the_stream_and_visible_reopens_with_since", async () => {
  const w = world();
  w.start();
  w.es()[0].emit("snapshot", snapshot());
  w.clearCalls();
  w.doc.setHidden(true);
  const hiddenStates = w.es().map((s) => s.readyState);
  await w.clock.advance(120000);
  const whileHidden = w.calls.length;
  w.doc.setHidden(false);
  return {
    hiddenStates, whileHidden, sources: w.es().map((s) => s.url), since: sinceOf(w.es()[1].url),
  };
});

test("gap_and_foreign_epoch_resync_from_the_known_position", async () => {
  const w = world();
  w.start();
  w.es()[0].emit("snapshot", snapshot());
  const change = [loopChange("upsert", ROOT_B, card("gamma", { iteration: 9 }))];
  w.es()[0].emit("delta", delta(change, { from_seq: 9, seq: 10 }));
  const afterGap = { sources: w.es().length, firstState: w.es()[0].readyState, since: sinceOf(w.es()[1].url) };
  w.es()[1].emit("delta", delta(change, { epoch: "e2" }));
  const afterForeign = { sources: w.es().length, since: sinceOf(w.es()[2].url) };
  w.es()[2].emit("delta", delta(change));
  return {
    afterGap, afterForeign, applied: w.store.loopSig("lid-gamma").value.iteration, seq: w.store.seq.value,
    resumedSources: w.es().length,
  };
});

test("stream_refused_more_than_5_times_gives_up_to_polling", async () => {
  const w = world();
  w.start();
  w.es()[0].emit("snapshot", snapshot());
  const bad = delta([], { epoch: "other" });
  for (let i = 0; i < 6; i++) w.es()[w.es().length - 1].emit("delta", bad);
  await w.clock.settle();
  return { mode: w.store.feed.value.mode, polls: w.paths("/api/overview").length, sources: w.es().length };
});

test("detail_refetch_rules", async () => {
  const w = world();
  w.start();
  const es = w.es()[0];
  es.emit("snapshot", snapshot());
  await w.store.openLoop(ROOT_A + "::alpha");
  const out = { openFetch: w.paths("/api/loop").length };
  const step = async (label, fn) => {
    w.clearCalls();
    fn();
    await w.clock.settle();
    out[label] = w.paths("/api/loop").length;
  };
  await step("tick", () => es.emit("tick", tick()));
  await step("otherLoop", () => es.emit("delta", delta([loopChange("upsert", ROOT_A, card("beta", { iteration: 9 }))])));
  await step("openLoop", () => es.emit("delta", delta(
    [loopChange("upsert", ROOT_A, card("alpha", { running: true, running_sources: ["driver"], iteration: 3 }))],
    { from_seq: 8, seq: 9 })));
  await step("sameEpochSameSnapshot", () => {
    const s = snapshot("e1", 9);
    s.workspaces[0].loops[0] = card("alpha", { running: true, running_sources: ["driver"], iteration: 3 });
    s.workspaces[0].loops[1] = card("beta", { final_verdict: "SHIP", iteration: 9 });
    es.emit("snapshot", s);
  });
  await step("newEpochSnapshot", () => es.emit("snapshot", snapshot("e2", 1)));
  return out;
});

test("stop_ends_everything", async () => {
  const w = world();
  const transport = w.start();
  const retryBefore = typeof w.store.retry;
  const es = w.es()[0];
  es.emit("snapshot", snapshot());
  transport.stop();
  const afterStop = { retry: w.store.retry, state: es.readyState, listeners: w.doc.listenerCount() };
  w.clearCalls();
  await w.clock.advance(120000);
  es.emit("tick", tick());
  return { retryBefore, afterStop, calls: w.calls.length, timers: w.clock.timers.length };
});

test("retry_reopens_the_stream_or_polls_now", async () => {
  const w = world();
  const transport = w.start();
  w.es()[0].emit("snapshot", snapshot());
  transport.retry();
  const reopened = { sources: w.es().length, since: sinceOf(w.es()[1].url) };
  w.es()[1].fail(true);
  await w.clock.settle();
  w.clearCalls();
  transport.retry();
  await w.clock.settle();
  return { reopened, pollsOnRetry: w.paths("/api/overview").length, viaStore: typeof w.store.retry };
});

/* ------------------------------ location ------------------------------ */

test("open_loop_writes_the_hash_and_close_clears_it", async () => {
  const w = world();
  const win = makeWindow();
  w.store.applySnapshot(snapshot());
  const unbind = bindLocation(w.store, win);
  await w.store.openLoop(ROOT_A + "::alpha");
  const opened = win.location.hash;
  w.store.closeLoop();
  const closed = win.location.hash;
  unbind();
  await w.store.openLoop(ROOT_B + "::gamma");
  return { opened, closed, afterUnbind: win.location.hash };
});

test("hash_before_data_opens_after_the_first_snapshot", async () => {
  const w = world();
  const win = makeWindow("#root=%2Fw%2Fa&loop=alpha");
  bindLocation(w.store, win);
  w.start();
  const before = { open: w.store.openKey.value, hash: win.location.hash };
  w.es()[0].emit("snapshot", snapshot());
  await w.clock.settle();
  return {
    before, open: w.store.openKey.value, hash: win.location.hash,
    detailFetches: w.paths("/api/loop").length, detail: w.store.detail.value && w.store.detail.value.name,
  };
});

test("hash_resolution_root_match_else_unique_name", async () => {
  const run = async (hash, snap = snapshot()) => {
    const w = world();
    const win = makeWindow(hash);
    bindLocation(w.store, win);
    w.store.applySnapshot(snap);
    await w.clock.settle();
    return { open: w.store.openKey.value, hash: win.location.hash };
  };
  const dup = snapshot();
  dup.workspaces[1].loops.push(card("alpha", { loop_id: "lid-alpha-b" }));
  return {
    exact: await run("#root=%2Fw%2Fb&loop=gamma"),
    nameOnly: await run("#loop=gamma"),
    rootFallsBackToUniqueName: await run("#root=%2Fgone&loop=gamma"),
    ambiguousName: await run("#loop=alpha", dup),
    ambiguousWithRoot: await run("#root=%2Fw%2Fb&loop=alpha", dup),
    unknown: await run("#root=%2Fw%2Fa&loop=nothing"),
    noLoopParam: await run("#root=%2Fw%2Fa"),
  };
});

test("pending_hash_waits_for_a_loop_that_appears_later", async () => {
  const w = world();
  const win = makeWindow("#root=%2Fw%2Fb&loop=late");
  bindLocation(w.store, win);
  w.store.applySnapshot(snapshot());
  const beforeDelta = { open: w.store.openKey.value, hash: win.location.hash };
  w.store.applyDelta(delta([loopChange("upsert", ROOT_B, card("late"))]));
  await w.clock.settle();
  return { beforeDelta, open: w.store.openKey.value };
});

test("hashchange_opens_and_closes", async () => {
  const w = world();
  const win = makeWindow();
  w.store.applySnapshot(snapshot());
  bindLocation(w.store, win);
  win.setHash("#root=%2Fw%2Fb&loop=gamma");
  await w.clock.settle();
  const opened = w.store.openKey.value;
  win.setHash("#root=%2Fw%2Fa&loop=beta");
  await w.clock.settle();
  const switched = w.store.openKey.value;
  win.setHash("");
  const closed = { open: w.store.openKey.value, hash: win.location.hash };
  win.setHash("#loop=future");
  const pendingOpen = w.store.openKey.value;
  w.store.applyDelta(delta([loopChange("upsert", ROOT_B, card("future"))]));
  await w.clock.settle();
  return { opened, switched, closed, pendingOpen, afterAppear: w.store.openKey.value, hash: win.location.hash };
});

test("closing_does_not_touch_a_pending_hash", async () => {
  const w = world();
  const win = makeWindow("#root=%2Fw%2Fa&loop=alpha");
  bindLocation(w.store, win);
  w.store.closeLoop();
  return { hash: win.location.hash };
});

/* -------------------------------- run --------------------------------- */

const results = {};
for (const [name, fn] of Object.entries(cases)) {
  try {
    results[name] = { value: await fn() };
  } catch (err) {
    results[name] = { error: String((err && err.stack) || err) };
  }
}
process.stdout.write(JSON.stringify(results) + "\n");
process.exit(0);
