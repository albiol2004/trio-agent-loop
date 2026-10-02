"use strict";
/* Loads the real dashboard/app.js into a vm context with a fake DOM that
 * counts writes, a scriptable fetch / EventSource and a manual clock, then
 * runs a list of ops (JSON on stdin: {"ops": [...]}) against it.
 *
 * Output (JSON on stdout): {"results": [one entry per op], "errors": [...]}.
 *
 * Write accounting: every mutation of an element (textContent / innerHTML /
 * child insertion or removal / className / classList / attribute / hidden /
 * value / dataset / style / any other property set) is attributed to the
 * nearest ancestor-or-self that has an id, so a test can ask "how many DOM
 * writes landed inside #needs-list since the last reset". Mutations of a
 * subtree that is not attached to an id'd element are not counted (they are
 * not visible; the attach itself is counted on the parent). */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BASE_MS = Date.parse("2026-06-01T12:00:00Z");
let clock = 0; // virtual ms since BASE_MS
let ctx = null;

/* ------------------------------ write counts ------------------------------ */

let counts = Object.create(null);
function owner(node) {
  for (let n = node; n; n = n._parent) if (n._id) return n._id;
  return null;
}
function note(node) {
  const id = owner(node);
  if (id) counts[id] = (counts[id] || 0) + 1;
}

/* ------------------------------ selectors ------------------------------ */

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /^([a-zA-Z][a-zA-Z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=("?)([^\]"]*)\5)?\]/y;
  let m;
  re.lastIndex = 0;
  while (re.lastIndex < text.length && (m = re.exec(text))) {
    if (m[1]) out.tag = m[1].toUpperCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else if (m[4]) out.attrs.push([m[4], m[6]]);
  }
  return out;
}
function nodeMatches(node, c) {
  if (node._tag === "#TEXT") return false;
  if (c.tag && node._tag !== c.tag) return false;
  if (c.id && node._id !== c.id) return false;
  const cls = String(node.className || "").split(/\s+/);
  for (const k of c.classes) if (!cls.includes(k)) return false;
  for (const [k, v] of c.attrs) {
    const has = node._attrs.has(k) || (k.startsWith("data-") && node._dataset &&
      node._dataset[k.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] !== undefined);
    if (!has) return false;
    if (v !== undefined) {
      const got = node._attrs.has(k) ? node._attrs.get(k) : String(node._dataset[k.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())]);
      if (String(got) !== v) return false;
    }
  }
  return true;
}
function selectorChain(sel) {
  return sel.split(",").map((part) => part.trim().split(/\s+/).map(parseCompound));
}
function matchesChain(node, chain, scopeRoot) {
  let i = chain.length - 1;
  if (!nodeMatches(node, chain[i])) return false;
  let cur = node._parent;
  i -= 1;
  while (i >= 0) {
    while (cur && !nodeMatches(cur, chain[i])) cur = cur._parent;
    if (!cur) return false;
    cur = cur._parent;
    i -= 1;
  }
  return true;
}
function descendants(node, out) {
  for (const c of node._nodes) {
    if (c._tag !== "#TEXT") out.push(c);
    descendants(c, out);
  }
  return out;
}
function queryAll(roots, sel, includeRoots) {
  const chains = selectorChain(sel);
  const seen = new Set();
  const found = [];
  for (const root of roots) {
    const pool = descendants(root, []);
    if (includeRoots) pool.unshift(root);
    for (const n of pool) {
      if (seen.has(n)) continue;
      if (chains.some((ch) => matchesChain(n, ch))) {
        seen.add(n);
        found.push(n);
      }
    }
  }
  return found;
}

/* ------------------------------ fake DOM ------------------------------ */

const OWN_SKIP = new Set(["_nodes", "_parent", "_attrs", "_listeners", "_dataset", "_style", "_tag", "_id", "_text"]);
const handler = {
  set(target, key, value) {
    if (typeof key === "string" && key[0] === "_") {
      target[key] = value;
      return true;
    }
    note(target);
    if (key === "id") target._id = value;
    Reflect.set(target, key, value, target);
    return true;
  },
};

class FakeNode {
  constructor(tag) {
    this._tag = String(tag).toUpperCase();
    this.tagName = this._tag;
    this._nodes = [];
    this._parent = null;
    this._attrs = new Map();
    this._listeners = {};
    this._id = null;
    this._text = "";
    this.className = "";
    this.hidden = false;
    this.value = "";
    this.disabled = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.nodeType = tag === "#text" ? 3 : 1;
    return new Proxy(this, handler);
  }
  get dataset() {
    if (!this._dataset) {
      const self = this;
      this._dataset = new Proxy({}, {
        set(t, k, v) { note(self); t[k] = String(v); return true; },
        deleteProperty(t, k) { note(self); delete t[k]; return true; },
      });
    }
    return this._dataset;
  }
  get style() {
    if (!this._style) {
      const self = this;
      this._style = new Proxy({}, {
        set(t, k, v) { note(self); t[k] = v; return true; },
        get(t, k) {
          if (k === "setProperty") return (name, v) => { note(self); t[name] = v; };
          if (k === "removeProperty") return (name) => { note(self); delete t[name]; };
          return t[k];
        },
      });
    }
    return this._style;
  }
  get classList() {
    const self = this;
    const list = () => String(self.className || "").split(/\s+/).filter(Boolean);
    return {
      add(c) { if (!list().includes(c)) self.className = list().concat(c).join(" "); },
      remove(c) { if (list().includes(c)) self.className = list().filter((x) => x !== c).join(" "); },
      toggle(c, force) {
        const has = list().includes(c);
        const want = force === undefined ? !has : Boolean(force);
        if (want && !has) self.className = list().concat(c).join(" ");
        else if (!want && has) self.className = list().filter((x) => x !== c).join(" ");
        return want;
      },
      contains(c) { return list().includes(c); },
    };
  }
  get textContent() {
    if (this._tag === "#TEXT") return this._text;
    return this._nodes.map((n) => n.textContent).join("");
  }
  set textContent(value) {
    if (this._tag === "#TEXT") { this._text = String(value); return; }
    for (const n of this._nodes) n._parent = null;
    this._nodes = [];
    if (String(value) !== "") {
      const t = new FakeNode("#text");
      t._text = String(value);
      t._parent = this;
      this._nodes.push(t);
    }
  }
  set innerHTML(value) {
    for (const n of this._nodes) n._parent = null;
    this._nodes = [];
    this._html = String(value);
  }
  get innerHTML() { return this._html || ""; }
  get children() { return this._nodes.filter((n) => n._tag !== "#TEXT"); }
  get childNodes() { return this._nodes.slice(); }
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get parentNode() { return this._parent; }
  get parentElement() { return this._parent; }
  get nextElementSibling() {
    if (!this._parent) return null;
    const sib = this._parent.children;
    return sib[sib.indexOf(this) + 1] || null;
  }
  get previousElementSibling() {
    if (!this._parent) return null;
    const sib = this._parent.children;
    return sib[sib.indexOf(this) - 1] || null;
  }
  _detach(node) {
    if (node._parent) {
      const p = node._parent;
      const i = p._nodes.indexOf(node);
      if (i >= 0) p._nodes.splice(i, 1);
      note(p);
      node._parent = null;
    }
  }
  _adopt(node, index) {
    if (node._tag === "#FRAGMENT") {
      const kids = node._nodes.slice();
      node._nodes = [];
      let at = index;
      for (const k of kids) {
        k._parent = null;
        this._adopt(k, at === undefined ? undefined : at++);
      }
      return;
    }
    this._detach(node);
    node._parent = this;
    if (index === undefined) this._nodes.push(node);
    else this._nodes.splice(index, 0, node);
  }
  appendChild(child) { note(this); this._adopt(child); return child; }
  insertBefore(child, ref) {
    note(this);
    if (!ref) { this._adopt(child); return child; }
    this._detach(child);
    this._adopt(child, this._nodes.indexOf(ref));
    return child;
  }
  removeChild(child) { this._detach(child); return child; }
  remove() { if (this._parent) this._parent._detach(this); }
  replaceWith(...nodes) {
    const p = this._parent;
    if (!p) return;
    note(p);
    const i = p._nodes.indexOf(this);
    p._nodes.splice(i, 1);
    this._parent = null;
    let at = i;
    for (const n of nodes) { p._adopt(n, at); at += 1; }
  }
  replaceChildren(...nodes) {
    note(this);
    for (const n of this._nodes) n._parent = null;
    this._nodes = [];
    for (const n of nodes) this._adopt(n);
  }
  append(...nodes) { for (const n of nodes) this.appendChild(typeof n === "string" ? document.createTextNode(n) : n); }
  setAttribute(k, v) { note(this); this._attrs.set(k, String(v)); }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  hasAttribute(k) { return this._attrs.has(k); }
  removeAttribute(k) { note(this); this._attrs.delete(k); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    for (const fn of this._listeners[ev.type] || []) fn(ev);
    return true;
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  querySelectorAll(sel) { return queryAll([this], sel); }
  querySelector(sel) { return queryAll([this], sel)[0] || null; }
  closest(sel) {
    const chains = selectorChain(sel);
    for (let n = this; n; n = n._parent) {
      if (chains.some((ch) => matchesChain(n, ch))) return n;
    }
    return null;
  }
  contains(node) {
    for (let n = node; n; n = n._parent) if (n === this) return true;
    return false;
  }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  _serialize() {
    if (this._tag === "#TEXT") return ["#text", this._text];
    const props = {};
    for (const k of Object.keys(this)) {
      if (k[0] === "_" || k === "tagName" || k === "nodeType" || OWN_SKIP.has(k)) continue;
      if (typeof this[k] === "function") continue;
      props[k] = this[k];
    }
    return [this._tag, props, [...this._attrs.entries()].sort(), this._dataset ? { ...this._dataset } : {},
      this._style ? { ...this._style } : {}, this._nodes.map((n) => n._serialize())];
  }
  isEqualNode(other) {
    return Boolean(other) && JSON.stringify(this._serialize()) === JSON.stringify(other._serialize());
  }
}

const byId = new Map();
const HIDDEN_AT_START = new Set(["board-error", "drawer", "drawer-scrim", "board-partial", "broker-note"]);
const document = {
  readyState: "loading",
  hidden: false,
  activeElement: null,
  _listeners: {},
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag),
  createTextNode: (text) => { const t = new FakeNode("#text"); t._text = String(text); return t; },
  createDocumentFragment: () => new FakeNode("#fragment"),
  getElementById(id) {
    if (!byId.has(id)) {
      const node = new FakeNode("div");
      node._id = id;
      if (HIDDEN_AT_START.has(id)) node.hidden = true;
      byId.set(id, node);
    }
    return byId.get(id);
  },
  addEventListener(type, fn) { (document._listeners[type] = document._listeners[type] || []).push(fn); },
  removeEventListener() {},
  querySelectorAll: (sel) => queryAll([...byId.values()], sel, true),
  querySelector: (sel) => queryAll([...byId.values()], sel, true)[0] || null,
  contains: (node) => Boolean(node) && owner(node) !== null,
};

/* ------------------------------ timers and clock ------------------------------ */

const timers = new Map();
let timerSeq = 0;
function addTimer(fn, ms, repeat) {
  const id = ++timerSeq;
  const delay = Math.max(0, Number(ms) || 0);
  timers.set(id, { fn, at: clock + delay, every: repeat ? Math.max(1, delay) : null });
  return id;
}
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}
async function advance(ms) {
  await flush(); // let pending promise work (fetch continuations) arm its timers
  const target = clock + ms;
  for (;;) {
    let best = null;
    for (const [id, t] of timers) {
      if (t.at <= target && (!best || t.at < best[1].at)) best = [id, t];
    }
    if (!best) break;
    const [id, t] = best;
    clock = Math.max(clock, t.at);
    ctx.__clock.t = clock;
    if (t.every) t.at = clock + t.every;
    else timers.delete(id);
    try { t.fn(); } catch (err) { errors.push("timer: " + String((err && err.stack) || err)); }
    await flush();
  }
  clock = target;
  ctx.__clock.t = clock;
}

/* ------------------------------ fetch / EventSource ------------------------------ */

const fetchLog = [];
let routes = {};
function fakeFetch(url, opts) {
  const method = (opts && opts.method) || "GET";
  fetchLog.push({ url: String(url), method });
  const route = routes[String(url).split("?")[0]];
  let status = 404;
  let body = { error: "not found" };
  if (route !== undefined) {
    if (route && typeof route === "object" && "__status" in route) {
      status = route.__status;
      body = route.body;
    } else {
      status = 200;
      body = route;
    }
  }
  const text = JSON.stringify(body);
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(vm.runInContext("JSON.parse", ctx)(text)),
    text: () => Promise.resolve(text),
  });
}

const sources = [];
class FakeEventSource {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.listeners = {};
    this.onerror = null;
    this.onopen = null;
    this.onmessage = null;
    sources.push(this);
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  close() { this.readyState = 2; }
  _fire(type, ev) {
    if (this["on" + type]) this["on" + type](ev);
    for (const fn of this.listeners[type] || []) fn(ev);
  }
}
FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;
FakeEventSource.prototype.CONNECTING = 0;
FakeEventSource.prototype.OPEN = 1;
FakeEventSource.prototype.CLOSED = 2;

/* ------------------------------ context ------------------------------ */

const errors = [];
function boot(opts) {
  const windowObj = {
    addEventListener() {},
    confirm: () => true,
    document,
  };
  if (!opts.noEventSource) windowObj.EventSource = FakeEventSource;
  ctx = {
    document,
    window: windowObj,
    localStorage: { getItem: () => null, setItem() {} },
    location: { hash: opts.hash || "", pathname: "/", search: "" },
    history: {
      replaceState(_s, _t, url) {
        if (typeof url === "string" && url[0] === "#") ctx.location.hash = url;
        else if (typeof url === "string") ctx.location.hash = "";
      },
    },
    requestAnimationFrame: (fn) => addTimer(fn, 16, false),
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    setTimeout: (fn, ms) => addTimer(fn, ms, false),
    clearTimeout: (id) => { timers.delete(id); },
    clearInterval: (id) => { timers.delete(id); },
    fetch: fakeFetch,
    URLSearchParams,
    console: {
      warn() {},
      log() {},
      error(...a) { errors.push("console.error: " + a.map(String).join(" ")); },
    },
  };
  if (!opts.noEventSource) ctx.EventSource = FakeEventSource;
  windowObj.setTimeout = ctx.setTimeout;
  vm.createContext(ctx);
  vm.runInContext(`
    globalThis.__clock = { t: 0 };
    (function () {
      const Real = Date;
      const BASE = ${BASE_MS};
      class FakeDate extends Real {
        constructor(...a) { if (a.length === 0) super(FakeDate.now()); else super(...a); }
        static now() { return BASE + globalThis.__clock.t; }
      }
      globalThis.Date = FakeDate;
    })();
  `, ctx);
  const appPath = process.env.STREAM_CLIENT_APP_JS ||
    path.join(__dirname, "..", "..", "dashboard", "app.js");
  vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });
}

/* ------------------------------ op interpreter ------------------------------ */

const vars = {};
function toCtx(value) {
  return vm.runInContext("JSON.parse", ctx)(JSON.stringify(value === undefined ? null : value));
}
function resolveArg(arg) {
  if (arg && typeof arg === "object" && !Array.isArray(arg) && "$ref" in arg) return vars[arg.$ref];
  return toCtx(arg);
}
function plain(value) {
  const seen = new Set();
  const walk = (v) => {
    if (v === undefined) return null;
    if (v === null || typeof v !== "object") return v;
    const tag = Object.prototype.toString.call(v);
    if (tag === "[object Map]") return { __map: Array.from(v.entries(), ([k, x]) => [walk(k), walk(x)]) };
    if (tag === "[object Set]") return { __set: Array.from(v.values(), walk) };
    if (Array.isArray(v)) return v.map(walk);
    if (seen.has(v)) return "[circular]";
    seen.add(v);
    const out = {};
    for (const k of Object.keys(v)) out[k] = walk(v[k]);
    seen.delete(v);
    return out;
  };
  return walk(value);
}

async function runOp(op) {
  switch (op.op) {
    case "boot":
      boot(op);
      return null;
    case "call": {
      const fn = vm.runInContext(op.fn, ctx);
      if (typeof fn !== "function") throw new Error(op.fn + " is not a function");
      const value = await fn(...(op.args || []).map(resolveArg));
      if (op.as) vars[op.as] = value;
      return op.quiet ? null : plain(value);
    }
    case "eval": {
      const value = await vm.runInContext(op.code, ctx);
      if (op.as) vars[op.as] = value;
      return op.quiet ? null : plain(value);
    }
    case "var":
      return plain(vars[op.name]);
    case "routes":
      routes = op.routes;
      return null;
    case "fetches": {
      const out = fetchLog.slice();
      if (op.clear) fetchLog.length = 0;
      return out;
    }
    case "resetCounts":
      counts = Object.create(null);
      return null;
    case "counts":
      return Object.assign({}, counts);
    case "flush":
      await flush();
      return null;
    case "advance":
      await advance(op.ms);
      return null;
    case "esList":
      return sources.map((s) => ({ url: s.url, readyState: s.readyState }));
    case "esOpen": {
      const s = sources[op.index === undefined ? sources.length - 1 : op.index];
      s.readyState = 1;
      s._fire("open", { type: "open" });
      await flush();
      return null;
    }
    case "esEmit": {
      const s = sources[op.index === undefined ? sources.length - 1 : op.index];
      s.readyState = 1;
      s._fire(op.type, { type: op.type, data: JSON.stringify(op.data), lastEventId: op.id || "" });
      await flush();
      return null;
    }
    case "esError": {
      const s = sources[op.index === undefined ? sources.length - 1 : op.index];
      s.readyState = op.closed ? 2 : 0;
      s._fire("error", { type: "error" });
      await flush();
      return null;
    }
    case "setHidden": {
      document.hidden = Boolean(op.value);
      for (const fn of document._listeners.visibilitychange || []) fn({ type: "visibilitychange" });
      await flush();
      return null;
    }
    case "click": {
      const node = op.id ? document.getElementById(op.id) : queryAll([...byId.values()], op.selector, true)[0];
      if (!node) throw new Error("no node to click");
      const ev = { type: "click", target: node, stopPropagation() {}, preventDefault() {} };
      for (let n = node; n; n = n._parent) {
        for (const fn of n._listeners.click || []) fn(Object.assign({}, ev, { currentTarget: n }));
      }
      await flush();
      return null;
    }
    case "text": {
      return document.getElementById(op.id).textContent;
    }
    case "hiddenOf":
      return document.getElementById(op.id).hidden;
    case "timersPending":
      return timers.size;
    default:
      throw new Error("unknown op " + op.op);
  }
}

process.on("unhandledRejection", (err) => {
  errors.push("unhandledRejection: " + String((err && err.stack) || err));
});

(async () => {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const results = [];
  for (const op of input.ops) {
    try {
      results.push({ value: await runOp(op), tag: op.tag });
    } catch (err) {
      results.push({ error: String((err && err.message) || err), tag: op.tag });
    }
  }
  await flush();
  process.stdout.write(JSON.stringify({ results, errors }));
  process.exit(0);
})();
