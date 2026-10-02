"use strict";
/* Real-browser harness for the dashboard's push stream (GOAL DoD3: zero DOM
 * rebuilds of unchanged sections). Drives headless chromium through
 * playwright-core against a lab dashboard/serve.py that test_dash_stream_dom.py
 * started, and prints ONE JSON object on stdout.
 *
 * Usage: node stream_dom_harness.cjs <config.json>
 *   config: { playwright, chromium, userDataDir, base, root, loops: [names],
 *             scenario: "push" | "poll",
 *             idleMs, mutate: {path, text, loop}, drawerLoop }
 *
 * "push" scenario: waits for the board and the first snapshot, observes every
 * board section with a MutationObserver over an idle window (counting SSE
 * events by type through a wrapped window.EventSource and CDP
 * Network.dataReceived bytes / requests per path), rewrites a mailbox STATE.md
 * and times the row change, opens the drawer and observes its sections over
 * another idle window.
 * "poll" scenario: the stream is refused (TRIO_DASH_STREAM=0): the board must
 * fill from /api/overview, which must be requested repeatedly.
 */
const fs = require("fs");

const BOARD_SECTIONS = ["kpis", "needs-list", "running-list", "loop-rows",
  "notes-list", "workspace-filter", "tabs"];
const DRAWER_SECTIONS = ["drawer-strip", "commit-list", "slice-list",
  "drawer-timeline", "drawer-inbox-list", "drawer-mission", "drawer-badge"];

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const { chromium } = require(cfg.playwright);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* Counts every SSE event by type (and every EventSource opened) in the page. */
const INIT_SCRIPT = `(() => {
  const Native = window.EventSource;
  if (typeof Native !== "function") return;
  window.__sse = { opened: [], events: [] };
  class Counting extends Native {
    constructor(url, opts) {
      super(url, opts);
      window.__sse.opened.push(String(url));
      for (const type of ["snapshot", "delta", "tick", "error", "message"]) {
        this.addEventListener(type, () => {
          window.__sse.events.push({ type, url: String(url), t: Date.now() });
        });
      }
    }
  }
  window.EventSource = Counting;
})();`;

/* Installs observers on the listed element ids; records per id. */
function installObservers(ids) {
  window.__mo = window.__mo || {};
  for (const id of ids) {
    const node = document.getElementById(id);
    if (!node) { window.__mo[id] = { missing: true, records: 0, sample: [] }; continue; }
    const entry = { missing: false, records: 0, sample: [] };
    window.__mo[id] = entry;
    const observer = new MutationObserver((records) => {
      for (const rec of records) {
        entry.records += 1;
        if (entry.sample.length < 4) {
          entry.sample.push(rec.type + ":" + (rec.attributeName || "") + ":" +
            String((rec.target && (rec.target.textContent || rec.target.nodeValue)) || "").slice(0, 80));
        }
      }
    });
    observer.observe(node, { childList: true, subtree: true, characterData: true, attributes: true });
    entry.observer = observer;
  }
}

function readObservers(ids) {
  const out = {};
  for (const id of ids) {
    const entry = window.__mo[id];
    if (!entry) { out[id] = null; continue; }
    // takeRecords() flushes anything still queued so late writes count too.
    if (entry.observer) {
      for (const rec of entry.observer.takeRecords()) {
        entry.records += 1;
        if (entry.sample.length < 4) entry.sample.push(rec.type + ":" + (rec.attributeName || ""));
      }
    }
    const node = document.getElementById(id);
    out[id] = { missing: entry.missing, records: entry.records, sample: entry.sample,
      text_len: node ? (node.textContent || "").trim().length : 0,
      children: node ? node.children.length : 0 };
  }
  return out;
}

function resetObservers(ids) {
  for (const id of ids) {
    const entry = window.__mo[id];
    if (entry) { entry.records = 0; entry.sample = []; }
  }
}

(async () => {
  const result = { scenario: cfg.scenario, errors: [], console: [] };
  let browser = null;
  try {
  browser = await chromium.launch({
    executablePath: cfg.chromium,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    env: Object.assign({}, process.env, { TMPDIR: cfg.userDataDir }),
    timeout: 30000,
  });
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
    const page = await context.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error") result.console.push(msg.text().slice(0, 200));
    });
    page.on("pageerror", (err) => result.errors.push(String(err).slice(0, 300)));
    await page.addInitScript(INIT_SCRIPT);

    // CDP: requests per path and bytes received per request.
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    const urlOf = new Map();
    const net = { requests: {}, bytes: 0, bytesByPath: {}, chunks: 0 };
    const pathOf = (url) => { try { return new URL(url).pathname; } catch (_e) { return url; } };
    cdp.on("Network.requestWillBeSent", (ev) => {
      const p = pathOf(ev.request.url);
      urlOf.set(ev.requestId, p);
      net.requests[p] = (net.requests[p] || 0) + 1;
    });
    cdp.on("Network.dataReceived", (ev) => {
      const p = urlOf.get(ev.requestId) || "?";
      net.bytes += ev.encodedDataLength || 0;
      net.chunks += 1;
      net.bytesByPath[p] = (net.bytesByPath[p] || 0) + (ev.encodedDataLength || 0);
    });
    const netSnapshot = () => JSON.parse(JSON.stringify(net));

    const url = cfg.base + "/";
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 20000 });

    // The board shows every fixture loop.
    const wantRows = cfg.loops;
    try {
      await page.waitForFunction((names) => {
        const text = (document.getElementById("loop-rows") || {}).textContent || "";
        return names.every((name) => text.includes(name));
      }, wantRows, { timeout: 20000 });
      result.board_ready = true;
    } catch (_e) {
      result.board_ready = false;
    }
    result.rows_text = await page.evaluate(() => (document.getElementById("loop-rows") || {}).textContent || "");

    if (cfg.scenario === "poll") {
      const t0 = Date.now();
      const before = netSnapshot();
      await sleep(cfg.pollWaitMs || 12000);
      const after = netSnapshot();
      result.elapsed_ms = Date.now() - t0;
      result.overview_requests = (after.requests["/api/overview"] || 0);
      result.stream_requests = (after.requests["/api/stream"] || 0);
      result.requests = after.requests;
      result.before_requests = before.requests;
      result.rows_text = await page.evaluate(() => (document.getElementById("loop-rows") || {}).textContent || "");
      console.log(JSON.stringify(result));
      return;
    }

    // First snapshot over the stream.
    try {
      await page.waitForFunction(
        () => window.__sse && window.__sse.events.some((e) => e.type === "snapshot"),
        null, { timeout: 15000 });
      result.snapshot_seen = true;
    } catch (_e) {
      result.snapshot_seen = false;
    }
    // Let the post-snapshot render settle (stream hand-off, first scan tick).
    await sleep(1500);

    // ---- board idle window ----
    await page.evaluate(installObservers, BOARD_SECTIONS);
    const evBefore = await page.evaluate(() => window.__sse.events.length);
    const netBefore = netSnapshot();
    const t0 = Date.now();
    await sleep(cfg.idleMs || 5000);
    result.board_idle_ms = Date.now() - t0;
    const netAfter = netSnapshot();
    result.board = await page.evaluate(readObservers, BOARD_SECTIONS);
    const events = await page.evaluate((n) => window.__sse.events.slice(n), evBefore);
    result.idle_events = events.reduce((acc, e) => { acc[e.type] = (acc[e.type] || 0) + 1; return acc; }, {});
    result.idle_bytes = netAfter.bytes - netBefore.bytes;
    result.idle_chunks = netAfter.chunks - netBefore.chunks;
    result.idle_requests = {};
    for (const key of Object.keys(netAfter.requests)) {
      const delta = netAfter.requests[key] - (netBefore.requests[key] || 0);
      if (delta) result.idle_requests[key] = delta;
    }
    result.total_requests = netAfter.requests;
    result.sse_opened = await page.evaluate(() => window.__sse.opened);

    // ---- STATE.md rewrite -> row text changes ----
    const rowText = (name) => page.evaluate((n) => {
      const rows = Array.from(document.querySelectorAll("#loop-rows tr"));
      const row = rows.find((r) => (r.textContent || "").includes(n));
      return row ? row.textContent.replace(/\s+/g, " ").trim() : null;
    }, name);
    result.mutate_before = await rowText(cfg.mutate.loop);
    fs.writeFileSync(cfg.mutate.path, cfg.mutate.text);
    const m0 = Date.now();
    result.mutate_changed_ms = null;
    while (Date.now() - m0 < 3000) {
      const now = await rowText(cfg.mutate.loop);
      if (now !== result.mutate_before) { result.mutate_changed_ms = Date.now() - m0; break; }
      await sleep(50);
    }
    result.mutate_after = await rowText(cfg.mutate.loop);
    // Positive control: the same observer sees the rewrite it is watching for.
    result.mutate_records = await page.evaluate(readObservers, ["loop-rows"]);

    // ---- drawer ----
    result.hash_before_open = await page.evaluate(() => location.hash);
    await page.evaluate((hash) => { location.hash = hash; },
      "root=" + encodeURIComponent(cfg.root) + "&loop=" + encodeURIComponent(cfg.drawerLoop));
    try {
      await page.waitForFunction(() => {
        const drawer = document.getElementById("drawer");
        const name = document.getElementById("drawer-name");
        return drawer && !drawer.hidden && name && name.textContent.trim() !== "—";
      }, null, { timeout: 10000 });
      result.drawer_opened = true;
    } catch (_e) {
      result.drawer_opened = false;
    }
    // Wait for the detail (/api/loop) to land, then settle.
    try {
      await page.waitForFunction(
        () => document.getElementById("fact-iter") && document.getElementById("fact-iter").textContent.trim() !== "—",
        null, { timeout: 8000 });
      result.detail_loaded = true;
    } catch (_e) {
      result.detail_loaded = false;
    }
    await sleep(1500);
    result.hash_open = await page.evaluate(() => location.hash);
    result.drawer_name = await page.evaluate(() => (document.getElementById("drawer-name") || {}).textContent);
    await page.evaluate(installObservers, DRAWER_SECTIONS.concat(BOARD_SECTIONS));
    const dEvBefore = await page.evaluate(() => window.__sse.events.length);
    const dNetBefore = netSnapshot();
    const d0 = Date.now();
    await sleep(cfg.idleMs || 5000);
    result.drawer_idle_ms = Date.now() - d0;
    const dNetAfter = netSnapshot();
    result.drawer = await page.evaluate(readObservers, DRAWER_SECTIONS);
    result.drawer_board = await page.evaluate(readObservers, BOARD_SECTIONS);
    const dEvents = await page.evaluate((n) => window.__sse.events.slice(n), dEvBefore);
    result.drawer_idle_events = dEvents.reduce((acc, e) => { acc[e.type] = (acc[e.type] || 0) + 1; return acc; }, {});
    result.drawer_idle_bytes = dNetAfter.bytes - dNetBefore.bytes;
    result.drawer_idle_requests = {};
    for (const key of Object.keys(dNetAfter.requests)) {
      const delta = dNetAfter.requests[key] - (dNetBefore.requests[key] || 0);
      if (delta) result.drawer_idle_requests[key] = delta;
    }
    result.hash_after = await page.evaluate(() => location.hash);
    result.drawer_visible = await page.evaluate(() => {
      const drawer = document.getElementById("drawer");
      return !!drawer && !drawer.hidden && drawer.getBoundingClientRect().width > 0;
    });
    console.log(JSON.stringify(result));
  } catch (err) {
    result.errors.push("harness: " + String((err && err.stack) || err).slice(0, 600));
    console.log(JSON.stringify(result));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();
