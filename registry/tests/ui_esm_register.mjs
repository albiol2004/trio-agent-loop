/* Node ESM resolve hook for the dashboard frontend's modules (api:UiHarness).
 *
 *   node --import ./registry/tests/ui_esm_register.mjs <case.mjs>
 *
 * Resolves the bare specifiers of dashboard/vendor/importmap.json to the
 * vendored files (what the browser's import map does), and loads the
 * extension-less-package .js files under dashboard/ as ES modules. */
import { readFileSync } from "node:fs";
import { register } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const dashboardDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "dashboard");
const importmap = JSON.parse(readFileSync(path.join(dashboardDir, "vendor", "importmap.json"), "utf8"));

const targets = {};
for (const [specifier, url] of Object.entries(importmap.imports)) {
  targets[specifier] = pathToFileURL(path.join(dashboardDir, url.replace(/^\//, ""))).href;
}

const hooks = `
let targets = {};
let dashboardUrl = "";
export function initialize(data) {
  targets = data.targets;
  dashboardUrl = data.dashboardUrl;
}
export async function resolve(specifier, context, nextResolve) {
  if (Object.prototype.hasOwnProperty.call(targets, specifier)) {
    return { url: targets[specifier], shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
export async function load(url, context, nextLoad) {
  if (url.startsWith(dashboardUrl) && /\\.(js|mjs)$/.test(url)) {
    return nextLoad(url, { ...context, format: "module" });
  }
  return nextLoad(url, context);
}
`;

register("data:text/javascript," + encodeURIComponent(hooks), {
  parentURL: import.meta.url,
  data: { targets, dashboardUrl: pathToFileURL(dashboardDir).href + "/" },
});
