"use strict";
/* End-to-end browser harness for the whole dashboard page (GOAL DoD6, DoD3):
 * the Preact + signals board and drawer plus the legacy islands of app.js,
 * served by a lab dashboard/serve.py that test_ui_e2e.py started. Prints ONE
 * JSON object on stdout (and writes it to $TRIO_UI_E2E_OUT when that is set,
 * the evidence dump the Lead collects).
 *
 *   node ui_e2e_harness.cjs <config.json>
 *   config: { playwright, chromium, scenario: "push" | "poll", base, root,
 *             otherRoot, loops: [names], humanLoop, shipLoop, fillLoop,
 *             shipStatePath, shipStateText, idleMs, pollWaitMs, scanMs }
 *
 * "push": open the page, wait for the snapshot, open the needs_human loop's
 * drawer by clicking its row, expand the commits panel, scroll the board,
 * select text in a row, set the workspace filter and the search query; then
 * observe the listed sections with MutationObservers for idleMs (>= 65 s: the
 * window always spans a wall-clock minute boundary); rewrite the shipped
 * loop's STATE.md and check what survived; walk the drawer's views; mark an
 * inbox item read; reload on the hash; Escape. Every request URL, page error
 * and console error of the whole run is recorded.
 * "poll": the lab refuses the stream (TRIO_DASH_STREAM=0): the board must be
 * filled and kept fresh from /api/overview. */
const fs = require("fs");

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const { chromium } = require(cfg.playwright);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* The sections that must stay untouched while idle (the accept's list). */
const SECTIONS = ["kpis", "needs-list", "running-list", "loop-rows", "notes-list",
  "workspace-filter", "tabs", "drawer-inbox-list", "drawer-strip", "commit-list",
  "slice-list", "drawer-timeline", "view-timeline", "view-files", "view-graph",
  "session-list"];

/* Counts every SSE event by type (and every EventSource opened) in the page. */
const INIT_SCRIPT = `(() => {
  const Native = window.EventSource;
  window.__sse = { opened: [], events: [] };
  if (typeof Native !== "function") return;
  class Counting extends Native {
    constructor(url, opts) {
      super(url, opts);
      window.__sse.opened.push(String(url));
      for (const type of ["snapshot", "delta", "tick", "error"]) {
        this.addEventListener(type, () => {
          window.__sse.events.push({ type, url: String(url), t: Date.now() });
        });
      }
    }
  }
  window.EventSource = Counting;
})();`;

/* ---- in-page helpers (run through page.evaluate) ---- */

/* MutationObservers per element id. childList and attributes records are
 * rebuilds; characterData is allowed only on text under [data-reltime] (a
 * clock tick), everything else of it is `charOther`. */
function installObservers(args) {
  const { ids, key, row } = args;
  /* With `row` (a loop name): a record counts as inside that loop's row when
   * its target is in the row, or when it only adds/removes the row itself (a
   * keyed move); every other non-reltime record is counted in `outsideRow`. */
  const isRow = (n) => Boolean(n && n.nodeType === 1 && n.matches("tr.loop-row") &&
    String(n.getAttribute("data-key") || "").endsWith("::" + row));
  const inRow = (rec) => {
    const t = rec.target && (rec.target.nodeType === 1 ? rec.target : rec.target.parentElement);
    if (t && isRow(t.closest("tr.loop-row"))) return true;
    if (rec.type !== "childList") return false;
    const moved = Array.from(rec.addedNodes).concat(Array.from(rec.removedNodes));
    return moved.length > 0 && moved.every(isRow);
  };
  window.__mo = window.__mo || {};
  const bag = (window.__mo[key] = {});
  const describe = (rec) => rec.type + ":" + (rec.attributeName || "") + ":" +
    String((rec.target && (rec.target.nodeValue || rec.target.id || rec.target.className ||
      rec.target.nodeName)) || "").slice(0, 60);
  for (const id of ids) {
    const node = document.getElementById(id);
    if (!node) { bag[id] = { missing: true, childList: 0, attributes: 0, charReltime: 0, charOther: 0, outsideRow: 0, samples: [], outsideSamples: [] }; continue; }
    const entry = { missing: false, childList: 0, attributes: 0, charReltime: 0, charOther: 0, outsideRow: 0, samples: [], outsideSamples: [] };
    const take = (records) => {
      for (const rec of records) {
        const reltime = rec.type === "characterData" && rec.target && rec.target.parentElement &&
          rec.target.parentElement.closest("[data-reltime]");
        if (row && !reltime && !inRow(rec)) {
          entry.outsideRow += 1;
          if (entry.outsideSamples.length < 5) entry.outsideSamples.push(describe(rec));
        }
        if (rec.type === "childList") entry.childList += 1;
        else if (rec.type === "attributes") entry.attributes += 1;
        else {
          const owner = rec.target && rec.target.parentElement;
          if (owner && owner.closest("[data-reltime]")) { entry.charReltime += 1; continue; }
          entry.charOther += 1;
        }
        if (entry.samples.length < 5) entry.samples.push(describe(rec));
      }
    };
    const observer = new MutationObserver(take);
    observer.observe(node, { childList: true, subtree: true, characterData: true, attributes: true });
    entry.take = take;
    entry.observer = observer;
    bag[id] = entry;
  }
}

function readObservers(args) {
  const { ids, key } = args;
  const out = {};
  for (const id of ids) {
    const entry = (window.__mo[key] || {})[id];
    if (!entry) { out[id] = null; continue; }
    if (entry.observer) entry.take(entry.observer.takeRecords());
    const node = document.getElementById(id);
    out[id] = {
      missing: entry.missing, childList: entry.childList, attributes: entry.attributes,
      charReltime: entry.charReltime, charOther: entry.charOther, samples: entry.samples,
      outsideRow: entry.outsideRow, outsideSamples: entry.outsideSamples,
      textLen: node ? (node.textContent || "").trim().length : 0,
    };
  }
  return out;
}

function stopObservers(key) {
  for (const entry of Object.values(window.__mo[key] || {})) {
    if (entry.observer) entry.observer.disconnect();
  }
}

/* The element that scrolls #loop-rows: the nearest scrollable ancestor, else
 * the document. Scrolls it by `by` px (when > 0) and reports it. */
function scrollBoard(by) {
  const rows = document.getElementById("loop-rows");
  let container = null;
  for (let node = rows.parentElement; node && node !== document.documentElement; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) {
      container = node;
      break;
    }
  }
  const target = container || document.scrollingElement;
  if (by > 0) target.scrollTop = Math.min(by, target.scrollHeight - target.clientHeight);
  return {
    container: container ? (container.id || container.className || container.tagName) : "document",
    scrollTop: Math.round(target.scrollTop),
    scrollable: target.scrollHeight - target.clientHeight,
  };
}

/* Select the first cell of a row (its mailbox name). The page behind an open
 * drawer is inert, and Chrome reports "" for Selection.toString() inside inert
 * content, so what is compared is the Range itself (its text and its anchor
 * node, kept in window.__selAnchor to prove the very same DOM node survives). */
function selectRowText(name) {
  const row = document.querySelector('#loop-rows tr.loop-row[data-key$="::' + name + '"]');
  if (!row) return null;
  const cell = row.querySelector("th, td");
  const range = document.createRange();
  range.selectNodeContents(cell);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  window.__selAnchor = selection.anchorNode;
  return range.toString();
}

function uiState() {
  const drawer = document.getElementById("drawer");
  const toggle = Array.from(document.querySelectorAll("#commit-list button"))
    .map((b) => b.textContent.trim());
  const scroll = (() => {
    const rows = document.getElementById("loop-rows");
    for (let node = rows.parentElement; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) return Math.round(node.scrollTop);
    }
    return Math.round(document.scrollingElement.scrollTop);
  })();
  return {
    hash: location.hash,
    drawerOpen: Boolean(drawer) && !drawer.hidden,
    drawerName: (document.getElementById("drawer-name") || {}).textContent,
    commitButtons: toggle,
    commitRows: document.querySelectorAll("#commit-list .commit-row").length,
    scrollTop: scroll,
    selection: (() => {
      const sel = window.getSelection();
      return sel.rangeCount ? sel.getRangeAt(0).toString() : "";
    })(),
    selectionAnchorKept: (() => {
      const sel = window.getSelection();
      return Boolean(window.__selAnchor) && sel.anchorNode === window.__selAnchor &&
        window.__selAnchor.isConnected;
    })(),
    workspace: document.getElementById("workspace-filter").value,
    query: document.getElementById("loop-search").value,
  };
}

function rowTextOf(name) {
  const row = document.querySelector('#loop-rows tr.loop-row[data-key$="::' + name + '"]');
  return row ? row.textContent.replace(/\s+/g, " ").trim() : null;
}

function agoLabels() {
  return Array.from(document.querySelectorAll("#loop-rows time[data-reltime]"))
    .map((t) => t.textContent.trim());
}

/* ---- scenario plumbing ---- */

const result = {
  scenario: cfg.scenario, errors: [], console: [], requests: [], steps: {},
};

async function openBrowser() {
  const browser = await chromium.launch({
    executablePath: cfg.chromium,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    env: Object.assign({}, process.env, { TMPDIR: cfg.userDataDir }),
    timeout: 30000,
  });
  const context = await browser.newContext({ viewport: { width: 1400, height: 700 } });
  const page = await context.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") result.console.push(msg.text().slice(0, 300));
  });
  page.on("pageerror", (err) => result.errors.push(String(err).slice(0, 300)));
  page.on("request", (req) => {
    result.requests.push({ url: req.url(), method: req.method(), type: req.resourceType(), t: Date.now() });
  });
  await page.addInitScript(INIT_SCRIPT);
  return { browser, page };
}

async function waitBoard(page, names, timeout = 25000) {
  try {
    await page.waitForFunction((want) => {
      const text = (document.getElementById("loop-rows") || {}).textContent || "";
      return want.every((name) => text.includes(name));
    }, names, { timeout });
    return true;
  } catch (_e) {
    return false;
  }
}

const countEvents = (page) => page.evaluate(() => window.__sse.events.reduce((acc, e) => {
  acc[e.type] = (acc[e.type] || 0) + 1;
  return acc;
}, {}));

const overviewCount = () => result.requests.filter((r) => new URL(r.url).pathname === "/api/overview").length;

/* ---- the push scenario ---- */

async function pushScenario(page) {
  const steps = result.steps;
  const rowSel = (name) => 'tr.loop-row[data-key$="::' + name + '"]';
  result.t_open_ms = Date.now();
  await page.goto(cfg.base + "/", { waitUntil: "domcontentloaded", timeout: 20000 });
  result.board_ready = await waitBoard(page, cfg.loops);
  try {
    await page.waitForFunction(() => window.__sse && window.__sse.events.some((e) => e.type === "snapshot"),
      null, { timeout: 15000 });
    result.snapshot_seen = true;
  } catch (_e) {
    result.snapshot_seen = false;
  }
  steps.after_load = { indicator: await page.evaluate(() => document.getElementById("live-text").textContent),
    title: await page.evaluate(() => document.getElementById("live").title) };

  // -- click a row: the hash names the loop, the drawer opens --
  await page.click(rowSel(cfg.humanLoop));
  steps.hash_after_click = await page.evaluate(() => location.hash);
  steps.hash_expected = "#root=" + encodeURIComponent(cfg.root) + "&loop=" + encodeURIComponent(cfg.humanLoop);
  try {
    await page.waitForFunction(() => {
      const drawer = document.getElementById("drawer");
      const iter = document.getElementById("fact-iter");
      return drawer && !drawer.hidden && iter && iter.textContent.trim() !== "—";
    }, null, { timeout: 15000 });
    steps.drawer_opened = true;
  } catch (_e) {
    steps.drawer_opened = false;
  }
  steps.drawer_name = await page.evaluate(() => document.getElementById("drawer-name").textContent);
  steps.drawer_visible = await page.evaluate(() => {
    const rect = document.getElementById("drawer").getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  steps.controls_visible = await page.evaluate(() => !document.getElementById("drawer-controls").hidden);

  // -- expand the commits panel --
  steps.commits_before = await page.evaluate(() => document.querySelectorAll("#commit-list .commit-row").length);
  await page.click("#commit-list button");
  await page.waitForFunction(() => /fewer/.test(document.querySelector("#commit-list button").textContent),
    null, { timeout: 5000 }).catch(() => {});
  steps.commits_after = await page.evaluate(() => document.querySelectorAll("#commit-list .commit-row").length);

  // -- workspace filter + query (as a user would: change / input events) --
  await page.evaluate((args) => {
    const select = document.getElementById("workspace-filter");
    select.value = args.root;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    const search = document.getElementById("loop-search");
    search.value = args.query;
    search.dispatchEvent(new Event("input", { bubbles: true }));
  }, { root: cfg.root, query: "loop-" });
  await page.waitForFunction((other) => {
    const text = document.getElementById("loop-rows").textContent;
    return !text.includes(other);
  }, cfg.otherLoop, { timeout: 8000 }).catch(() => {});
  steps.rows_after_filter = await page.evaluate(() => document.querySelectorAll("#loop-rows tr.loop-row").length);
  steps.other_loop_hidden = await page.evaluate((other) => !document.getElementById("loop-rows").textContent.includes(other), cfg.otherLoop);

  // -- scroll the board and select text in a row --
  steps.scroll = await page.evaluate(scrollBoard, 250);
  steps.selection = await page.evaluate(selectRowText, cfg.fillLoop);
  await sleep(500);
  const before = await page.evaluate(uiState);
  steps.state_before = before;

  // -- idle window: observers on every listed section --
  await page.evaluate(installObservers, { ids: SECTIONS, key: "idle" });
  await page.evaluate(installObservers, { ids: ["app"], key: "idleApp" });
  const sse0 = await page.evaluate(() => window.__sse.events.length);
  const req0 = result.requests.length;
  const agoBefore = await page.evaluate(agoLabels);
  const idleStart = Date.now();
  await sleep(cfg.idleMs);
  const idleEnd = Date.now();
  steps.idle = {
    start_ms: idleStart, end_ms: idleEnd, window_ms: idleEnd - idleStart,
    spans_minute_boundary: Math.floor(idleStart / 60000) !== Math.floor(idleEnd / 60000),
    sections: await page.evaluate(readObservers, { ids: SECTIONS, key: "idle" }),
    app: await page.evaluate(readObservers, { ids: ["app"], key: "idleApp" }),
    ago_before: agoBefore,
    ago_after: await page.evaluate(agoLabels),
    events: await page.evaluate((n) => window.__sse.events.slice(n).reduce((acc, e) => {
      acc[e.type] = (acc[e.type] || 0) + 1;
      return acc;
    }, {}), sse0),
    requests: result.requests.slice(req0).map((r) => new URL(r.url).pathname),
    state_after: await page.evaluate(uiState),
  };
  await page.evaluate(stopObservers, "idle");
  await page.evaluate(stopObservers, "idleApp");

  // -- another loop's STATE.md rewritten: the row follows, the UI state stays --
  await page.evaluate(installObservers, { ids: ["loop-rows"], key: "mutate", row: cfg.shipLoop });
  const shipBefore = await page.evaluate(rowTextOf, cfg.shipLoop);
  const sse1 = await page.evaluate(() => window.__sse.events.length);
  const clock1 = await page.evaluate(() => window.__trioStore.now.value);
  fs.writeFileSync(cfg.shipStatePath, cfg.shipStateText);
  const m0 = Date.now();
  let changedMs = null;
  while (Date.now() - m0 < 12000) {
    const now = await page.evaluate(rowTextOf, cfg.shipLoop);
    if (now !== shipBefore) { changedMs = Date.now() - m0; break; }
    await sleep(50);
  }
  // two scan ticks and a clock tick after the delta
  const tickDeadline = Date.now() + 15000;
  let ticksAfter = 0;
  let clockMoved = false;
  while (Date.now() < tickDeadline) {
    const events = await page.evaluate((n) => window.__sse.events.slice(n), sse1);
    ticksAfter = events.filter((e) => e.type === "tick").length;
    clockMoved = (await page.evaluate(() => window.__trioStore.now.value)) !== clock1;
    if (ticksAfter >= 2 && clockMoved) break;
    await sleep(250);
  }
  steps.mutate = {
    before: shipBefore,
    after: await page.evaluate(rowTextOf, cfg.shipLoop),
    changed_ms: changedMs,
    ticks_after: ticksAfter,
    clock_moved: clockMoved,
    events: await page.evaluate((n) => window.__sse.events.slice(n).reduce((acc, e) => {
      acc[e.type] = (acc[e.type] || 0) + 1;
      return acc;
    }, {}), sse1),
    observer: await page.evaluate(readObservers, { ids: ["loop-rows"], key: "mutate" }),
    state_after: await page.evaluate(uiState),
  };
  await page.evaluate(stopObservers, "mutate");

  // -- the drawer's views: the legacy islands are populated --
  steps.tabs = {};
  for (const tab of ["Timeline", "Files", "Graph", "Transcripts", "Overview"]) {
    await page.click('#drawer-tabs button:has-text("' + tab + '")');
    const id = "view-" + tab.toLowerCase();
    try {
      await page.waitForFunction((viewId) => {
        const view = document.getElementById(viewId);
        return view && !view.hidden && (view.textContent || "").trim().length > 0;
      }, id, { timeout: 8000 });
    } catch (_e) { /* recorded below */ }
    steps.tabs[tab.toLowerCase()] = await page.evaluate((viewId) => {
      const view = document.getElementById(viewId);
      return { hidden: view.hidden, textLen: (view.textContent || "").trim().length,
        children: view.querySelectorAll("*").length,
        text: (view.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160) };
    }, id);
  }
  // transcript streaming: the Transcripts tab opens the loop's newest session
  await page.click('#drawer-tabs button:has-text("Transcripts")');
  try {
    await page.waitForFunction((needle) => document.getElementById("transcript-view").textContent.includes(needle),
      cfg.transcriptNeedle, { timeout: 10000 });
  } catch (_e) { /* recorded below */ }
  steps.transcript = await page.evaluate(() => ({
    text: document.getElementById("transcript-view").textContent.replace(/\s+/g, " ").trim().slice(0, 300),
    sessions: document.querySelectorAll("#session-list .session-item").length,
    sessionText: document.getElementById("session-list").textContent.replace(/\s+/g, " ").trim().slice(0, 160),
    status: document.getElementById("pane-status-text").textContent,
    offset: document.getElementById("offset-hint").textContent,
  }));
  await page.click('#drawer-tabs button:has-text("Overview")');
  steps.unblock = await page.evaluate(() => {
    const section = document.getElementById("actions-section");
    return { hidden: section.hidden, state: (document.getElementById("actions-state").textContent || "").trim() };
  });

  // -- reload on the hash: the drawer reopens on that loop; Escape clears it --
  steps.hash_before_reload = await page.evaluate(() => location.hash);
  await page.reload({ waitUntil: "domcontentloaded" });
  try {
    await page.waitForFunction(() => {
      const drawer = document.getElementById("drawer");
      const iter = document.getElementById("fact-iter");
      return drawer && !drawer.hidden && iter && iter.textContent.trim() !== "—";
    }, null, { timeout: 20000 });
    steps.reload_drawer_open = true;
  } catch (_e) {
    steps.reload_drawer_open = false;
  }
  steps.reload_name = await page.evaluate(() => document.getElementById("drawer-name").textContent);
  steps.reload_hash = await page.evaluate(() => location.hash);
  await page.keyboard.press("Escape");
  await sleep(300);
  steps.after_escape = await page.evaluate(() => ({
    hash: location.hash, href: location.href,
    drawerHidden: document.getElementById("drawer").hidden,
    inert: document.getElementById("main").inert,
  }));

  // -- read / unread (the drawer is closed: the board is interactive again) --
  steps.read = {};
  const posts = [];
  page.on("request", (req) => { if (req.method() === "POST") posts.push(new URL(req.url()).pathname); });
  const mark = page.locator('#needs-list button:has-text("Mark")').first();
  steps.read.has_button = (await mark.count()) > 0;
  if (steps.read.has_button) {
    steps.read.label_before = (await mark.textContent()).trim();
    await mark.click();
    try {
      await page.waitForFunction(() => /Show read \(\d+\)/.test(document.getElementById("inbox-toggle").textContent) &&
        !document.getElementById("inbox-toggle").hidden, null, { timeout: 8000 });
    } catch (_e) { /* recorded */ }
    steps.read.toggle_after_read = await page.evaluate(() => document.getElementById("inbox-toggle").textContent.trim());
    steps.read.needs_text_after_read = await page.evaluate(() => document.getElementById("needs-list").textContent.replace(/\s+/g, " ").trim().slice(0, 200));
    await page.click("#inbox-toggle");
    const unmark = page.locator('#needs-list button:has-text("Mark unread")').first();
    steps.read.has_unread_button = (await unmark.count()) > 0;
    if (steps.read.has_unread_button) {
      await unmark.click();
      await sleep(1200);
    }
    steps.read.toggle_after_unread = await page.evaluate(() => document.getElementById("inbox-toggle").textContent.trim());
    steps.read.posts = posts.slice();
  }
  result.sse_opened = await page.evaluate(() => window.__sse.opened);
}

/* ---- the polling scenario ---- */

async function pollScenario(page) {
  const steps = result.steps;
  const t0 = Date.now();
  await page.goto(cfg.base + "/", { waitUntil: "domcontentloaded", timeout: 20000 });
  result.board_ready = await waitBoard(page, cfg.loops);
  steps.live = await page.evaluate(() => ({
    text: document.getElementById("live-text").textContent,
    title: document.getElementById("live").title,
  }));
  steps.row_count = await page.evaluate(() => document.querySelectorAll("#loop-rows tr.loop-row").length);
  steps.rows_text = await page.evaluate(() => document.getElementById("loop-rows").textContent);
  const wait = cfg.pollWaitMs - (Date.now() - t0);
  if (wait > 0) await sleep(wait);
  steps.elapsed_ms = Date.now() - t0;
  steps.overview_requests = overviewCount();
  steps.stream_failed = (await page.evaluate(() => window.__sse.opened)).length;
  steps.live_after = await page.evaluate(() => ({
    text: document.getElementById("live-text").textContent,
    title: document.getElementById("live").title,
  }));
}

(async () => {
  let browser = null;
  try {
    const opened = await openBrowser();
    browser = opened.browser;
    if (cfg.scenario === "poll") await pollScenario(opened.page);
    else await pushScenario(opened.page);
    result.overview_requests = overviewCount();
  } catch (err) {
    result.errors.push("harness: " + String((err && err.stack) || err).slice(0, 800));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  const line = JSON.stringify(result);
  if (process.env.TRIO_UI_E2E_OUT) {
    const out = process.env.TRIO_UI_E2E_OUT;
    const target = cfg.scenario === "poll" ? out.replace(/\.json$/, "") + "-poll.json" : out;
    try { fs.writeFileSync(target, JSON.stringify(result, null, 1)); } catch (_e) { /* evidence is best effort */ }
  }
  console.log(line);
})();
