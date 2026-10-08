/* Feeds the store (store.js) from the dashboard server, and binds the open
 * loop to the URL. No DOM rendering.
 *
 * startTransport: EventSource /api/stream (a snapshot, then keyed deltas and
 * ticks) with /api/overview polling as the fallback; a port of the push
 * object in app.js (stream open / resync / idle watchdog / poll / retry /
 * visibility). bindLocation: openKey <-> "#root=<root>&loop=<name>". */
import { effect, untracked } from "@preact/signals-core";

export const POLL_MS = 5000;
export const STREAM_URL = "/api/stream";
export const STREAM_IDLE_MS = 45000;
export const STREAM_RETRY_MS = 30000;
export const STREAM_MAX_RESYNCS = 5;
const QUICK_REPOLL_AFTER_MS = 18000;
const QUICK_REPOLL_MS = 1500;
const ES_CONNECTING = 0;
const ES_CLOSED = 2;

/* opts: {fetch, EventSource, document, setTimeout, clearTimeout, now}, each
 * defaulting to the global of the same name. Returns {stop(), retry()}. */
export function startTransport(store, opts = {}) {
  const fetchFn = opts.fetch || ((...args) => globalThis.fetch(...args));
  const setTimer = opts.setTimeout || ((...args) => globalThis.setTimeout(...args));
  const clearTimer = opts.clearTimeout || ((...args) => globalThis.clearTimeout(...args));
  const nowFn = opts.now || Date.now;
  const doc = opts.document !== undefined ? opts.document : globalThis.document;
  const ES = opts.EventSource !== undefined ? opts.EventSource : globalThis.EventSource;

  const push = {
    mode: "stream", es: null, idleTimer: null, retryTimer: null, pollTimer: null,
    resyncs: 0, quickRepoll: false, stopped: false,
  };

  const streamSupported = () => typeof ES === "function";
  const hidden = () => Boolean(doc && doc.hidden);

  function setMode(mode) {
    push.mode = mode;
    store.setFeed({ mode });
  }

  function markLive(updatedAt) {
    const patch = { mode: "stream", status: "live", error: null, lastOk: nowFn() };
    if (updatedAt) patch.updatedAt = updatedAt;
    store.setFeed(patch);
  }

  /* The id of the loop open in the drawer, when there is one. */
  function openLoopId() {
    const key = store.openKey.peek();
    const sig = key === null ? undefined : untracked(() => store.loopByKey(key));
    return sig ? sig.peek().id : null;
  }

  function openLoopValue() {
    const key = store.openKey.peek();
    const sig = key === null ? undefined : untracked(() => store.loopByKey(key));
    return sig ? sig.peek() : null;
  }

  /* ---- stream events ---- */
  function onSnapshot(data) {
    push.resyncs = 0;
    const wasOpen = store.openKey.peek() !== null;
    const before = openLoopValue();
    const knownEpoch = store.epoch.peek();
    store.applySnapshot(data);
    markLive(data.updated_at);
    // A loop that was already open refetches its detail when the server is a
    // new process or its card moved; a loop a pending hash just opened has
    // fetched its own detail.
    if (wasOpen && store.openKey.peek() !== null &&
        (data.epoch !== knownEpoch || openLoopValue() !== before)) {
      store.refreshDetail();
    }
  }

  function onDelta(data) {
    const previousId = openLoopId();
    const result = store.applyDelta(data);
    if (!result.ok) {
      resyncStream();
      return;
    }
    push.resyncs = 0;
    markLive(data.updated_at);
    if (store.openKey.peek() !== null && result.loopIds.size) {
      const currentId = openLoopId();
      const key = store.openKey.peek();
      if ((previousId !== null && result.loopIds.has(previousId)) ||
          (currentId !== null && result.loopIds.has(currentId)) ||
          result.loopIds.has("path:" + key)) {
        store.refreshDetail();
      }
    }
  }

  /* Nothing changed: only the liveness indicator moves. */
  function onTick(data) {
    markLive(data.updated_at);
  }

  const HANDLERS = { snapshot: onSnapshot, delta: onDelta, tick: onTick };

  function onStreamEvent(es, type, ev) {
    if (push.stopped || push.es !== es) return;
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch (err) {
      return;
    }
    if (!data || typeof data !== "object") return;
    // Any event proves the stream healthy: polling and the retry timer stop.
    push.mode = "stream";
    clearTimer(push.pollTimer);
    clearTimer(push.retryTimer);
    armStreamIdle();
    HANDLERS[type](data);
  }

  function onStreamError(es) {
    if (push.stopped || push.es !== es) return;
    // CONNECTING: the browser is reconnecting by itself (Last-Event-ID); the
    // idle watchdog covers it never succeeding. CLOSED: it gave up (503/404).
    if (es.readyState === ES_CLOSED) startPolling();
    else if (es.readyState === ES_CONNECTING) store.setFeed({ status: "reconnecting" });
  }

  function armStreamIdle() {
    clearTimer(push.idleTimer);
    push.idleTimer = setTimer(startPolling, STREAM_IDLE_MS);
  }

  function closeStream() {
    clearTimer(push.idleTimer);
    const es = push.es;
    push.es = null;
    if (es) {
      try {
        es.close();
      } catch (err) {
        /* already closed */
      }
    }
  }

  /* (Re)open the stream, resuming from the store's position when it has one. */
  function openStream() {
    closeStream();
    const epoch = store.epoch.peek();
    const seq = store.seq.peek();
    const since = epoch != null && seq != null
      ? "?since=" + encodeURIComponent(epoch + ":" + seq) : "";
    let es;
    try {
      es = new ES(STREAM_URL + since);
    } catch (err) {
      startPolling();
      return;
    }
    push.es = es;
    for (const type of Object.keys(HANDLERS)) {
      es.addEventListener(type, (ev) => onStreamEvent(es, type, ev));
    }
    es.addEventListener("error", () => onStreamError(es));
    armStreamIdle();
  }

  /* A refused delta: reopen from the store's position (the server answers
   * with the missed deltas or a fresh snapshot). A stream that keeps being
   * refused is given up on. */
  function resyncStream() {
    push.resyncs += 1;
    if (push.resyncs > STREAM_MAX_RESYNCS) startPolling();
    else openStream();
  }

  /* ---- polling fallback ---- */
  function startPolling() {
    if (push.stopped) return;
    const already = push.mode === "poll";
    setMode("poll");
    closeStream();
    clearTimer(push.retryTimer);
    if (streamSupported() && !hidden()) push.retryTimer = setTimer(retryStream, STREAM_RETRY_MS);
    if (!already) pollBoard();
  }

  function retryStream() {
    if (push.stopped || hidden() || push.mode !== "poll" || !streamSupported()) return;
    openStream();
  }

  async function pollBoard() {
    clearTimer(push.pollTimer);
    try {
      const res = await fetchFn("/api/overview", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (push.stopped || push.mode !== "poll") return; // the stream took over meanwhile
      store.applyOverview(data);
      store.setFeed({
        mode: "poll", status: "live", error: null, lastOk: nowFn(),
        updatedAt: data.updated_at || null,
      });
      if (store.openKey.peek() !== null) store.refreshDetail();
    } catch (err) {
      if (push.stopped || push.mode !== "poll") return;
      store.setFeed({ mode: "poll", status: "offline", error: err.message });
    }
    schedulePoll();
  }

  /* Poll every 5 s while visible and in fallback; hidden tabs stop polling
   * (the server only rebuilds when someone asks). A build older than the
   * server's rebuild interval means a fresh one is on its way, so look again
   * sooner, once. */
  function schedulePoll() {
    clearTimer(push.pollTimer);
    if (push.stopped || hidden() || push.mode !== "poll") return;
    const stamp = Date.parse(store.feed.peek().updatedAt || "");
    const age = Number.isNaN(stamp) ? null : nowFn() - stamp;
    const soon = store.loaded.peek() && age != null && age > QUICK_REPOLL_AFTER_MS &&
      !push.quickRepoll;
    push.quickRepoll = Boolean(soon);
    push.pollTimer = setTimer(pollBoard, soon ? QUICK_REPOLL_MS : POLL_MS);
  }

  /* ---- lifecycle ---- */
  function pauseFeed() {
    closeStream();
    clearTimer(push.retryTimer);
    clearTimer(push.pollTimer);
  }

  function resumeFeed() {
    if (push.mode === "poll") {
      if (streamSupported()) {
        clearTimer(push.retryTimer);
        push.retryTimer = setTimer(retryStream, STREAM_RETRY_MS);
      }
      pollBoard();
    } else {
      openStream();
    }
  }

  function onVisibility() {
    if (push.stopped) return;
    if (hidden()) pauseFeed();
    else resumeFeed();
  }

  /* After loop actions: while the stream is healthy the server pushes the
   * result, so only the polling fallback fetches here. */
  function refreshBoard() {
    if (push.mode === "poll") return pollBoard();
    if (!push.es && !hidden()) openStream();
    return Promise.resolve();
  }

  /* "Retry" on the error banner: reconnect the stream, or poll right now. */
  function retry() {
    if (push.mode === "poll") pollBoard();
    else openStream();
  }

  function stop() {
    push.stopped = true;
    pauseFeed();
    if (doc && typeof doc.removeEventListener === "function") {
      doc.removeEventListener("visibilitychange", onVisibility);
    }
    if (store.retry === retry) store.retry = null;
    if (store.refreshBoard === refreshBoard) store.refreshBoard = null;
  }

  store.retry = retry;
  store.refreshBoard = refreshBoard;
  if (doc && typeof doc.addEventListener === "function") {
    doc.addEventListener("visibilitychange", onVisibility);
  }
  if (hidden()) {
    pauseFeed();
  } else if (!streamSupported()) {
    setMode("poll");
    pollBoard();
  } else {
    setMode("stream");
    openStream();
  }
  return { stop, retry };
}

/* ------------------------------ location ------------------------------ */

function parseHash(win) {
  const params = new URLSearchParams(String(win.location.hash || "").replace(/^#/, ""));
  const name = params.get("loop");
  return name ? { root: params.get("root") || "", name } : null;
}

/* The Loop a hash target names: root + name, else the one loop of that name. */
function resolveTarget(store, target) {
  if (target.root) {
    const sig = store.loopByKey(target.root + "::" + target.name);
    if (sig) return sig.peek();
  }
  const named = store.loops.peek().filter((loop) => loop.name === target.name);
  return named.length === 1 ? named[0] : null;
}

/* Keeps location.hash and store.openKey in step. A hash present at bind time
 * (or set before the data is there) is pending and opens once the store has
 * loaded and the loop exists. Returns an unbind function. */
export function bindLocation(store, win) {
  let pending = parseHash(win);
  let lastKey = null;

  const stopOpen = effect(() => {
    store.loopIds.value; // eslint-disable-line no-unused-expressions
    if (!pending || !store.loaded.value) return;
    const loop = untracked(() => resolveTarget(store, pending));
    if (!loop) return;
    pending = null;
    untracked(() => store.openLoop(loop.key));
  });

  const stopUrl = effect(() => {
    const key = store.openKey.value;
    if (key !== null) {
      pending = null;
      const sig = untracked(() => store.loopByKey(key));
      if (sig) {
        const loop = sig.peek();
        const hash = "#root=" + encodeURIComponent(loop.root) +
          "&loop=" + encodeURIComponent(loop.name);
        if (win.location.hash !== hash) win.history.replaceState(null, "", hash);
      }
    } else if (lastKey !== null && win.location.hash) {
      win.history.replaceState(null, "", win.location.pathname);
    }
    lastKey = key;
  });

  function onHashChange() {
    const target = parseHash(win);
    if (!target) {
      pending = null;
      if (store.openKey.peek() !== null) store.closeLoop();
      return;
    }
    const loop = store.loaded.peek() ? resolveTarget(store, target) : null;
    if (loop) {
      pending = null;
      store.openLoop(loop.key);
    } else {
      pending = target;
    }
  }

  win.addEventListener("hashchange", onHashChange);
  return function unbind() {
    stopOpen();
    stopUrl();
    win.removeEventListener("hashchange", onHashChange);
  };
}
