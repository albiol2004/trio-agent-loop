/* Entry of the dashboard page (GOAL DoD6): the signals store, its feed
 * (/api/stream with the /api/overview fallback), the location binding
 * (URL = open loop), the clock, the board and the loop drawer.
 *
 * The drawer's heavy views (Timeline tab, Files, Graph, sessions and
 * transcripts, the Unblock section; DECISION D4-3) stay classic code in
 * /app.js. That script runs first (classic deferred scripts run before module
 * scripts) and offers its entry points as window.trioLegacy; they are handed
 * to the drawer as its legacy hooks and driven only by store changes, never
 * by a timer. */
import { effect, untracked } from "@preact/signals-core";
import { createStore } from "./store.js";
import { bindLocation, startTransport } from "./transport.js";
import { mountBoard } from "./board.js";
import { mountDrawer } from "./drawer.js";

const store = createStore();
window.__trioStore = store; // for tests and the legacy views (app.js)

startTransport(store); // sets store.retry / store.refreshBoard
bindLocation(store, window);
store.startClock();
mountBoard(store);

const legacy = window.trioLegacy || {};
mountDrawer(store, { legacy });

/* Legacy views that follow the open loop's card and the store clock. */
let lastKey = null;
let lastLoop = null;
effect(() => {
  const key = store.openKey.value;
  const sig = key === null ? undefined : store.loopByKey(key);
  const loop = sig ? sig.value : null;
  untracked(() => {
    // the drawer's own onOpen covers the first value for a key
    if (key !== lastKey) {
      lastKey = key;
      lastLoop = loop;
      return;
    }
    if (loop && loop !== lastLoop) {
      lastLoop = loop;
      if (legacy.onLoop) legacy.onLoop(loop);
    }
  });
});

effect(() => {
  store.now.value; // eslint-disable-line no-unused-expressions
  untracked(() => {
    if (legacy.onTick) legacy.onTick();
  });
});
