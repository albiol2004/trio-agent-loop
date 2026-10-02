"use strict";
/* Loads dashboard/app.js into a vm context with a minimal fake DOM and feeds
 * records (JSON on stdin: {"records": [...]}) to its real appendRecord().
 * Prints a JSON summary: per-record rendered blocks and any thrown error. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = "";
    this.children = [];
    this._text = "";
    this.hidden = false;
    this.dataset = {};
    this.style = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    const self = this;
    this.classList = {
      add(c) { if (!self.className.split(" ").includes(c)) self.className = (self.className + " " + c).trim(); },
      remove(c) { self.className = self.className.split(" ").filter((x) => x && x !== c).join(" "); },
      toggle() {},
      contains(c) { return self.className.split(" ").includes(c); },
    };
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get childElementCount() { return this.children.length; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener() {}
  setAttribute() {}
  querySelectorAll() { return []; }
}

const byId = new Map();
const document = {
  readyState: "loading",
  createElement: (tag) => new FakeElement(tag),
  createDocumentFragment: () => new FakeElement("#fragment"),
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, new FakeElement("div"));
    return byId.get(id);
  },
  addEventListener() {},
  querySelectorAll: () => [],
  contains: () => false,
};

const ctx = {
  document,
  window: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: "", pathname: "/" },
  history: { replaceState() {} },
  requestAnimationFrame: () => 0,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout() {},
  console: { warn() {}, log() {}, error() {} },
};
ctx.window.document = document;
vm.createContext(ctx);
const appPath = path.join(__dirname, "..", "..", "dashboard", "app.js");
vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });

function tags(node, out) {
  if (node.className === "tr-tag") out.push(node.textContent);
  for (const c of node.children) tags(c, out);
  return out;
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const view = document.getElementById("transcript-view");
const results = [];
for (const rec of input.records) {
  const before = view.children.length;
  let error = null;
  try {
    ctx.appendRecord(view, rec);
  } catch (err) {
    error = String((err && err.message) || err);
  }
  const blocks = view.children.slice(before).map((b) => ({
    className: b.className,
    tag: tags(b, [])[0] || null,
    text: b.textContent,
  }));
  results.push({ blocks, error });
}
process.stdout.write(JSON.stringify({
  records: results,
  viewText: view.textContent,
  viewClasses: view.children.map((b) => b.className),
}));
