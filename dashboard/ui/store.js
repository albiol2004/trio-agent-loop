/* Signals store of the dashboard frontend.
 *
 * Holds the board as keyed entities (the /api/stream model: meta, workspace,
 * loop, inbox; see dashboard/live_model.py). Every loop and every inbox item
 * has its own signal, and an entity whose JSON (keys sorted) is unchanged is
 * never reassigned: a reconnect snapshot or a poll of unchanged data notifies
 * nobody, and a delta that changes one loop notifies that loop's signal only.
 * Runs in the browser and in node (imports only signals-core and format.js);
 * the transport (transport.js) feeds it, the components (common.js, board.js,
 * drawer.js) read it. */
import { signal, computed, batch } from "@preact/signals-core";

export const CLOCK_MS = 5000;
const STORE_KEY = "trio.board.v2";
const TABS = ["running", "attention", "all", "archived"];

export function loopKey(root, name) {
  return root + "::" + name;
}

/* Same id as live_model.loop_entity_id. */
export function loopEntityId(root, card) {
  return card.loop_id ? card.loop_id : "path:" + root + "::" + card.name;
}

/* JSON with object keys sorted (python's json.dumps(sort_keys=True) shape):
 * two values are "the same entity" iff their canonical strings are equal. */
function canon(value) {
  if (Array.isArray(value)) return "[" + value.map(canon).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort()
      .filter((k) => value[k] !== undefined)
      .map((k) => JSON.stringify(k) + ":" + canon(value[k])).join(",") + "}";
  }
  return JSON.stringify(value === undefined ? null : value);
}

function sameList(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function describeError(data, response) {
  return (data && data.error) || "HTTP " + response.status;
}

export function createStore(opts = {}) {
  const fetchFn = opts.fetch || ((...args) => globalThis.fetch(...args));
  const nowFn = opts.now || Date.now;
  const setIntervalFn = opts.setInterval || ((...args) => globalThis.setInterval(...args));
  const clearIntervalFn = opts.clearInterval || ((...args) => globalThis.clearInterval(...args));
  let storage = opts.storage;
  if (storage === undefined) {
    try {
      storage = globalThis.localStorage;
    } catch (err) {
      storage = null;
    }
  }

  /* ------------------------- data signals ------------------------- */
  const epoch = signal(null);
  const seq = signal(null);
  const meta = signal({});
  const workspaces = signal([]);
  const loopIds = signal([]);
  const inboxIds = signal([]);
  const loaded = signal(false);
  const now = signal(nowFn());
  const feed = signal({
    mode: "stream", status: "connecting", error: null, lastOk: null, updatedAt: null,
  });

  /* Raw entity tables (the stream model) and the per-entity signals. */
  const raw = { workspaces: new Map(), loops: new Map(), inbox: new Map() };
  const loopSigs = new Map();
  const inboxSigs = new Map();
  const loopCanon = new Map();
  const inboxCanon = new Map();
  const keyIndex = new Map(); // loop key -> loop id
  const idKey = new Map(); // loop id -> loop key
  let metaCanon = canon({});
  let workspacesCanon = canon([]);

  const loops = computed(() => loopIds.value.map((id) => loopSigs.get(id))
    .filter(Boolean).map((s) => s.value));
  const inbox = computed(() => inboxIds.value.map((id) => inboxSigs.get(id))
    .filter(Boolean).map((s) => s.value));

  function loopSig(id) {
    return loopSigs.get(id);
  }

  function inboxSig(id) {
    return inboxSigs.get(id);
  }

  /* Reads loopIds, so an effect that looks a loop up re-runs when loops come
   * and go. */
  function loopByKey(key) {
    loopIds.value; // eslint-disable-line no-unused-expressions
    const id = keyIndex.get(key);
    return id === undefined ? undefined : loopSigs.get(id);
  }

  /* ------------------------- UI signals ------------------------- */
  const tab = signal("all");
  const workspace = signal("");
  const query = signal("");
  const sort = signal({ key: "activity", dir: "desc" });
  const showReadInbox = signal(false);
  const openKey = signal(null);
  const drawerTab = signal("overview");
  const expanded = signal(new Set());
  const detail = signal(null);
  const detailStatus = signal("idle");
  const detailError = signal(null);

  function loadPrefs() {
    try {
      const text = storage && storage.getItem(STORE_KEY);
      if (!text) return;
      const saved = JSON.parse(text);
      if (TABS.includes(saved.tab)) tab.value = saved.tab;
      if (typeof saved.workspace === "string") workspace.value = saved.workspace;
      if (saved.sort && typeof saved.sort.key === "string") sort.value = saved.sort;
    } catch (err) {
      /* private window or blocked storage: defaults are fine */
    }
  }

  function savePrefs() {
    try {
      if (storage) {
        storage.setItem(STORE_KEY, JSON.stringify({
          tab: tab.value, workspace: workspace.value, sort: sort.value,
        }));
      }
    } catch (err) {
      /* ignore */
    }
  }
  loadPrefs();

  /* ------------------------- reconciliation ------------------------- */
  function wsHeader(root) {
    return raw.workspaces.get(root) || null;
  }

  function buildLoop(id, entry) {
    const header = wsHeader(entry.root);
    return Object.assign({}, entry.card, {
      id,
      key: loopKey(entry.root, entry.card.name),
      root: entry.root,
      workspace: header ? header.name : "",
      worktree: Boolean(header && header.worktree),
    });
  }

  function buildItem(entry) {
    const header = wsHeader(entry.root);
    return Object.assign({}, entry.item, {
      root: entry.root,
      workspace: header ? header.name : "",
      key: loopKey(entry.root, entry.item.loop),
    });
  }

  function syncLoop(id) {
    const entry = raw.loops.get(id);
    const known = loopSigs.get(id);
    if (!entry || !entry.card) {
      if (known) {
        loopSigs.delete(id);
        loopCanon.delete(id);
        const oldKey = idKey.get(id);
        idKey.delete(id);
        if (oldKey !== undefined && keyIndex.get(oldKey) === id) keyIndex.delete(oldKey);
      }
      return;
    }
    const value = buildLoop(id, entry);
    const text = canon(value);
    if (known && loopCanon.get(id) === text) return;
    loopCanon.set(id, text);
    const oldKey = idKey.get(id);
    if (oldKey !== undefined && oldKey !== value.key && keyIndex.get(oldKey) === id) {
      keyIndex.delete(oldKey);
    }
    idKey.set(id, value.key);
    keyIndex.set(value.key, id);
    if (known) known.value = value;
    else loopSigs.set(id, signal(value));
  }

  function syncItem(id) {
    const entry = raw.inbox.get(id);
    const known = inboxSigs.get(id);
    if (!entry || !entry.item) {
      if (known) {
        inboxSigs.delete(id);
        inboxCanon.delete(id);
      }
      return;
    }
    const value = buildItem(entry);
    const text = canon(value);
    if (known && inboxCanon.get(id) === text) return;
    inboxCanon.set(id, text);
    if (known) known.value = value;
    else inboxSigs.set(id, signal(value));
  }

  function workspaceOrder() {
    const order = Array.isArray(meta.peek().workspace_order)
      ? meta.peek().workspace_order : Array.from(raw.workspaces.keys());
    const seen = new Set();
    const out = [];
    for (const root of order) {
      if (!raw.workspaces.has(root) || seen.has(root)) continue;
      seen.add(root);
      out.push(root);
    }
    return out;
  }

  /* Entity ids in server order: workspace order, then model order. */
  function syncOrder() {
    const order = workspaceOrder();
    const byRoot = new Map(order.map((root) => [root, { loops: [], inbox: [] }]));
    for (const [id, entry] of raw.loops) {
      const group = byRoot.get(entry.root);
      if (group) group.loops.push(id);
    }
    for (const [id, entry] of raw.inbox) {
      const group = byRoot.get(entry.root);
      if (group) group.inbox.push(id);
    }
    const nextLoops = [];
    const nextInbox = [];
    for (const group of byRoot.values()) {
      nextLoops.push(...group.loops);
      nextInbox.push(...group.inbox);
    }
    if (!sameList(nextLoops, loopIds.peek())) loopIds.value = nextLoops;
    if (!sameList(nextInbox, inboxIds.peek())) inboxIds.value = nextInbox;
    const headers = order.map((root) => raw.workspaces.get(root));
    const text = canon(headers);
    if (text !== workspacesCanon) {
      workspacesCanon = text;
      workspaces.value = headers;
    }
  }

  function setMeta(next) {
    const text = canon(next);
    if (text === metaCanon) return;
    metaCanon = text;
    meta.value = next;
  }

  /* Replace every entity from an /api/overview-shaped object (snapshot and
   * poll). Unchanged entities keep their signals untouched. */
  function ingest(overview) {
    const nextMeta = {};
    for (const key of Object.keys(overview)) {
      if (key !== "workspaces" && key !== "epoch" && key !== "seq") nextMeta[key] = overview[key];
    }
    raw.workspaces = new Map();
    raw.loops = new Map();
    raw.inbox = new Map();
    const order = [];
    for (const ws of Array.isArray(overview.workspaces) ? overview.workspaces : []) {
      const header = {};
      for (const key of Object.keys(ws)) {
        if (key !== "loops" && key !== "inbox") header[key] = ws[key];
      }
      raw.workspaces.set(ws.root, header);
      order.push(ws.root);
      for (const card of ws.loops || []) {
        raw.loops.set(loopEntityId(ws.root, card), { root: ws.root, card });
      }
      for (const item of ws.inbox || []) raw.inbox.set(item.id, { root: ws.root, item });
    }
    nextMeta.workspace_order = order;
    setMeta(nextMeta);
    for (const id of Array.from(loopSigs.keys())) if (!raw.loops.has(id)) syncLoop(id);
    for (const id of Array.from(inboxSigs.keys())) if (!raw.inbox.has(id)) syncItem(id);
    for (const id of raw.loops.keys()) syncLoop(id);
    for (const id of raw.inbox.keys()) syncItem(id);
    syncOrder();
    loaded.value = true;
  }

  function applySnapshot(snap) {
    if (!snap || typeof snap !== "object") return;
    batch(() => {
      epoch.value = snap.epoch === undefined ? null : snap.epoch;
      seq.value = snap.seq === undefined ? null : snap.seq;
      ingest(snap);
    });
  }

  function applyOverview(overview) {
    if (!overview || typeof overview !== "object") return;
    batch(() => ingest(overview));
  }

  /* One delta in place. A delta of another epoch, or one that does not start
   * at the store's seq (a gap or a replay), is refused and nothing changes:
   * the caller must resync from a snapshot. */
  function applyDelta(delta) {
    const refused = { ok: false, loopIds: new Set(), kinds: new Set() };
    if (!delta || epoch.peek() === null || delta.epoch !== epoch.peek() ||
        delta.from_seq !== seq.peek() || !Array.isArray(delta.changes)) {
      return refused;
    }
    const changedLoops = new Set();
    const kinds = new Set();
    batch(() => {
      const dirtyLoops = new Set();
      const dirtyItems = new Set();
      const dirtyRoots = new Set();
      let nextMeta = null;
      for (const change of delta.changes) {
        if (!change) continue;
        if (change.kind === "meta") {
          kinds.add("meta");
          if (change.op === "upsert" && change.data) {
            nextMeta = Object.assign({}, change.data);
            if (!Array.isArray(nextMeta.workspace_order)) {
              nextMeta.workspace_order = meta.peek().workspace_order;
            }
          }
          continue;
        }
        const table = { workspace: raw.workspaces, loop: raw.loops, inbox: raw.inbox }[change.kind];
        if (!table) continue;
        kinds.add(change.kind);
        if (change.kind === "loop") changedLoops.add(change.id);
        if (change.op === "remove") table.delete(change.id);
        else if (change.op === "upsert" && change.data) table.set(change.id, change.data);
        else continue;
        if (change.kind === "loop") dirtyLoops.add(change.id);
        else if (change.kind === "inbox") dirtyItems.add(change.id);
        else dirtyRoots.add(change.id);
      }
      if (nextMeta) setMeta(nextMeta);
      if (delta.updated_at !== undefined && meta.peek().updated_at !== delta.updated_at) {
        setMeta(Object.assign({}, meta.peek(), { updated_at: delta.updated_at }));
      }
      if (dirtyRoots.size) {
        // a workspace header changed: its loops and items carry its name
        for (const [id, entry] of raw.loops) if (dirtyRoots.has(entry.root)) dirtyLoops.add(id);
        for (const [id, entry] of raw.inbox) if (dirtyRoots.has(entry.root)) dirtyItems.add(id);
      }
      for (const id of dirtyLoops) syncLoop(id);
      for (const id of dirtyItems) syncItem(id);
      syncOrder();
      seq.value = delta.seq;
    });
    return { ok: true, loopIds: changedLoops, kinds };
  }

  /* ------------------------- feed + clock ------------------------- */
  function setFeed(patch) {
    const current = feed.peek();
    for (const key of Object.keys(patch)) {
      if (current[key] !== patch[key]) {
        feed.value = Object.assign({}, current, patch);
        return;
      }
    }
  }

  let clockTimer = null;
  function startClock() {
    if (clockTimer !== null) return;
    now.value = nowFn();
    clockTimer = setIntervalFn(() => {
      now.value = nowFn();
    }, CLOCK_MS);
  }

  function stopClock() {
    if (clockTimer === null) return;
    clearIntervalFn(clockTimer);
    clockTimer = null;
  }

  /* ------------------------- drawer / detail ------------------------- */
  let detailBusy = null;
  let detailAgain = false;

  async function fetchDetail() {
    const key = openKey.peek();
    const sig = key === null ? undefined : loopByKeyQuiet(key);
    if (!sig) return;
    const loop = sig.peek();
    try {
      const res = await fetchFn("/api/loop?name=" + encodeURIComponent(loop.name) +
        "&root=" + encodeURIComponent(loop.root), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (openKey.peek() !== key) return; // another loop (or none) is open now
      batch(() => {
        if (detail.peek() === null || canon(detail.peek()) !== canon(data)) detail.value = data;
        if (detailStatus.peek() !== "ok") detailStatus.value = "ok";
        if (detailError.peek() !== null) detailError.value = null;
      });
    } catch (err) {
      if (openKey.peek() !== key) return;
      batch(() => {
        detailError.value = err.message;
        if (detail.peek() === null) detailStatus.value = "error";
      });
    }
  }

  function loopByKeyQuiet(key) {
    const id = keyIndex.get(key);
    return id === undefined ? undefined : loopSigs.get(id);
  }

  /* Refetches coalesce: a call during a fetch costs one more fetch after it. */
  function refreshDetail() {
    if (detailBusy) {
      detailAgain = true;
      return detailBusy;
    }
    detailBusy = (async () => {
      try {
        await fetchDetail();
      } finally {
        detailBusy = null;
        if (detailAgain) {
          detailAgain = false;
          if (openKey.peek() !== null) refreshDetail();
        }
      }
    })();
    return detailBusy;
  }

  function openLoop(key) {
    if (openKey.peek() === key) return Promise.resolve();
    batch(() => {
      openKey.value = key;
      drawerTab.value = "overview";
      detail.value = null;
      detailError.value = null;
      detailStatus.value = "loading";
    });
    return refreshDetail();
  }

  function closeLoop() {
    batch(() => {
      openKey.value = null;
      drawerTab.value = "overview";
      detail.value = null;
      detailError.value = null;
      detailStatus.value = "idle";
    });
  }

  /* ------------------------- actions ------------------------- */
  async function setInboxRead(items, read) {
    const byRoot = new Map();
    for (const item of items) {
      if (!byRoot.has(item.root)) byRoot.set(item.root, []);
      byRoot.get(item.root).push(item.id);
    }
    for (const [root, ids] of byRoot) {
      const response = await fetchFn("/api/inbox/" + (read ? "read" : "unread"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, root }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(describeError(data, response));
    }
    batch(() => {
      for (const item of items) {
        const entry = raw.inbox.get(item.id);
        if (entry && entry.root === item.root) {
          entry.item = Object.assign({}, entry.item, { read });
          syncItem(item.id);
        }
      }
    });
  }

  async function controlLoop(loop, action) {
    const controls = loop.controls || {};
    const driver = controls.driver || "portable";
    let result;
    try {
      const res = await fetchFn("/api/loop/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: loop.root, driver }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(describeError(data, res));
      result = { ok: true, data, error: null };
    } catch (err) {
      result = { ok: false, data: null, error: err.message };
    }
    if (typeof store.refreshBoard === "function") await store.refreshBoard();
    return result;
  }

  function setTab(value) {
    if (!TABS.includes(value) || tab.peek() === value) return;
    tab.value = value;
    savePrefs();
  }

  function setWorkspace(value) {
    const next = String(value ?? "");
    if (workspace.peek() === next) return;
    workspace.value = next;
    savePrefs();
  }

  function setQuery(value) {
    const next = String(value ?? "");
    if (query.peek() !== next) query.value = next;
  }

  function setSort(next) {
    const current = sort.peek();
    if (!next || typeof next.key !== "string") return;
    if (current.key === next.key && current.dir === next.dir) return;
    sort.value = { key: next.key, dir: next.dir };
    savePrefs();
  }

  function toggleShowRead() {
    showReadInbox.value = !showReadInbox.peek();
  }

  function toggleExpanded(name) {
    const next = new Set(expanded.peek());
    if (next.has(name)) next.delete(name);
    else next.add(name);
    expanded.value = next;
  }

  function isExpanded(name) {
    return expanded.value.has(name);
  }

  const store = {
    epoch, seq, meta, workspaces, loopIds, inboxIds, loops, inbox, loaded, now, feed,
    loopSig, inboxSig, loopByKey,
    tab, workspace, query, sort, showReadInbox,
    openKey, drawerTab, expanded, detail, detailStatus, detailError,
    applySnapshot, applyDelta, applyOverview,
    openLoop, closeLoop, refreshDetail,
    setInboxRead, controlLoop,
    retry: null,
    refreshBoard: null,
    setTab, setWorkspace, setQuery, setSort, toggleShowRead,
    toggleExpanded, isExpanded,
    startClock, stopClock,
    setFeed,
  };
  return store;
}
