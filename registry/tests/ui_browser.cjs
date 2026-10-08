"use strict";
/* Browser harness for the dashboard frontend's components (api:UiHarness).
 *
 *   node registry/tests/ui_browser.cjs <case_rel>
 *
 * Opens headless chromium on http://ui.test/ where EVERY request is answered
 * by page.route (no server, no network):
 *   /                      dashboard/index.html with every <script> removed,
 *                          an inline import map (dashboard/vendor/importmap.json)
 *                          in <head> and <script type="module"
 *                          src="/tests/<case_rel>"> appended to <body>
 *   /vendor/* /ui/* /app.css   -> dashboard/
 *   /tests/*               -> registry/tests/
 *   anything else          -> 404, recorded in "errors"
 * Waits for window.__uiDone === true and prints ONE JSON line
 *   {"results": window.__uiResults, "errors": [...], "console": [...]}
 * "errors" holds page errors, 404s and a timeout; "console" the console
 * errors. Needs TRIO_DASH_PW_CORE and TRIO_DASH_CHROMIUM (the python side,
 * ui_browser_support.run_cases, sets both). */
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const DASHBOARD = path.join(REPO, "dashboard");
const TESTS = __dirname;
const ORIGIN = "http://ui.test";
const DONE_TIMEOUT_MS = 60000;

const TYPES = {
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".html": "text/html; charset=utf-8",
};

function pageHtml(caseRel) {
  const importmap = fs.readFileSync(path.join(DASHBOARD, "vendor", "importmap.json"), "utf8");
  let html = fs.readFileSync(path.join(DASHBOARD, "index.html"), "utf8");
  html = html.replace(/<script\b[\s\S]*?<\/script>/gi, "");
  html = html.replace(/<head>/i, () => "<head>\n<script type=\"importmap\">" + importmap.trim() + "</script>");
  const tag = "<script type=\"module\" src=\"/tests/" + caseRel + "\"></script>\n";
  return html.replace(/<\/body>/i, () => tag + "</body>");
}

/* The file under `base` that `rel` names, or null when it escapes `base`. */
function inside(base, rel) {
  const target = path.resolve(base, rel);
  return target.startsWith(base + path.sep) && fs.existsSync(target) &&
    fs.statSync(target).isFile() ? target : null;
}

async function main() {
  const caseRel = process.argv[2];
  if (!caseRel) throw new Error("usage: ui_browser.cjs <case_rel>");
  const { chromium } = require(process.env.TRIO_DASH_PW_CORE);
  const errors = [];
  const consoleErrors = [];
  let results = null;
  const browser = await chromium.launch({
    executablePath: process.env.TRIO_DASH_CHROMIUM,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    let fatal;
    const fatalPromise = new Promise((resolve) => { fatal = resolve; });
    page.on("pageerror", (err) => {
      errors.push("pageerror: " + (err && err.message || err));
      fatal("pageerror");
    });
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== ORIGIN) {
        errors.push("blocked request: " + url.href);
        return route.abort();
      }
      let file = null;
      if (url.pathname === "/") {
        return route.fulfill({ status: 200, contentType: TYPES[".html"], body: pageHtml(caseRel) });
      }
      if (url.pathname === "/favicon.ico") return route.fulfill({ status: 204, body: "" });
      if (/^\/(vendor|ui)\//.test(url.pathname) || url.pathname === "/app.css") {
        file = inside(DASHBOARD, url.pathname.slice(1));
      } else if (url.pathname.startsWith("/tests/")) {
        file = inside(TESTS, url.pathname.slice("/tests/".length));
      }
      if (!file) {
        errors.push("404 " + url.pathname);
        return route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
      }
      return route.fulfill({
        status: 200,
        contentType: TYPES[path.extname(file)] || "application/octet-stream",
        body: fs.readFileSync(file),
        headers: { "cache-control": "no-cache" },
      });
    });
    await page.goto(ORIGIN + "/");
    const done = page.waitForFunction(() => window.__uiDone === true, null, { timeout: DONE_TIMEOUT_MS })
      .then(() => "done", () => { errors.push("timeout waiting for window.__uiDone"); return "timeout"; });
    await Promise.race([done, fatalPromise]);
    results = await page.evaluate(() => window.__uiResults === undefined ? null : window.__uiResults)
      .catch(() => null);
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify({ results, errors, console: consoleErrors }) + "\n");
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({
    results: null, errors: ["harness: " + (err && err.stack || err)], console: [],
  }) + "\n");
  process.exit(1);
});
