/* Cases for dashboard/ui/common.js, run in the browser harness
 * (ui_browser.cjs): the real vendored preact + signals + htm, the real
 * components, a real DOM and a real MutationObserver. Sets window.__uiResults
 * and window.__uiDone; test_ui_common.py asserts on the values. */
import {
  html, RelTime, StateBadge, VerdictHistory, EmptyState, SeverityBadge, mount,
} from "/ui/common.js";
import { createStore } from "/ui/store.js";
import { relTime } from "/ui/format.js";

const results = {};
window.__uiResults = results;

const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

function scratch() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  return host;
}

/* Plain data for a rendered node: tag, class, title, own text, children. */
function shape(node) {
  if (node.nodeType === 3) return { text: node.data };
  const out = { tag: node.tagName.toLowerCase() };
  if (node.getAttribute("class") !== null) out.class = node.getAttribute("class");
  if (node.getAttribute("title") !== null) out.title = node.getAttribute("title");
  if (node.getAttribute("aria-label") !== null) out.aria = node.getAttribute("aria-label");
  out.children = Array.from(node.childNodes).map(shape);
  return out;
}

function makeStore(nowMs) {
  return createStore({
    fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
    EventSource: undefined, storage: null, now: () => nowMs,
  });
}

const cases = {};
const test = (name, fn) => { cases[name] = fn; };

/* RelTime: a clock tick moves the text node's data and nothing else. */
test("reltime_ticks_are_character_data_only", async () => {
  const iso = "2026-06-01T11:59:00Z";
  const store = makeStore(Date.parse(iso) + 10000); // "10s ago"
  const host = scratch();
  let rowRenders = 0;
  const Row = ({ store: s }) => {
    rowRenders += 1;
    return html`<p class="row"><${RelTime} store=${s} iso=${iso} /></p>`;
  };
  mount(host, html`<${Row} store=${store} />`);
  await settle();
  const time = host.querySelector("time[data-reltime]");
  const textNode = time.firstChild;
  const steps = [];
  const records = [];
  const observer = new MutationObserver((list) => {
    for (const r of list) {
      records.push({ type: r.type, target: r.target === textNode ? "text" : r.target.nodeName });
    }
  });
  observer.observe(host, {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
  async function advanceTo(secondsAfterIso) {
    records.length = 0;
    store.now.value = Date.parse(iso) + secondsAfterIso * 1000;
    await settle();
    observer.takeRecords().forEach((r) => records.push({ type: r.type, target: "pending" }));
    steps.push({
      secondsAfterIso, text: time.textContent, types: records.map((r) => r.type),
      targets: Array.from(new Set(records.map((r) => r.target))),
      sameTextNode: time.firstChild === textNode, childNodes: time.childNodes.length,
      expected: relTime(iso, store.now.value),
    });
  }
  const initial = time.textContent;
  await advanceTo(15); // +5 s
  await advanceTo(75); // +60 s
  await advanceTo(95);
  await advanceTo(119); // "1m ago" still
  await advanceTo(120); // "2m ago"
  await advanceTo(120); // same value again: nothing may change
  observer.disconnect();
  return {
    initial, steps, rowRenders, datetime: time.getAttribute("datetime"),
    hasTitle: time.getAttribute("title") !== "" && time.getAttribute("title") !== null,
    attrs: time.getAttributeNames().sort(),
  };
});

/* The "59s ago" -> "1m ago" boundary, on its own element. */
test("reltime_across_the_minute_boundary", async () => {
  const iso = "2026-06-01T11:59:00Z";
  const store = makeStore(Date.parse(iso) + 58000);
  const host = scratch();
  mount(host, html`<${RelTime} store=${store} iso=${iso} prefix="last write " suffix="!" />`);
  await settle();
  const time = host.querySelector("time");
  const textNode = time.firstChild;
  const seen = [];
  const types = [];
  const observer = new MutationObserver((list) => list.forEach((r) => types.push(r.type)));
  observer.observe(host, { subtree: true, childList: true, attributes: true, characterData: true });
  seen.push(time.textContent);
  for (const ms of [59000, 60000, 61000]) {
    store.now.value = Date.parse(iso) + ms;
    await settle();
    seen.push(time.textContent);
  }
  observer.takeRecords().forEach((r) => types.push(r.type));
  observer.disconnect();
  return { seen, types, sameTextNode: time.firstChild === textNode };
});

test("reltime_without_a_timestamp", async () => {
  const store = makeStore(Date.parse("2026-06-01T12:00:00Z"));
  const host = scratch();
  mount(host, html`<${RelTime} store=${store} iso=${null} />`);
  await settle();
  const time = host.querySelector("time");
  return { text: time.textContent, datetime: time.getAttribute("datetime"), title: time.getAttribute("title") };
});

/* A re-render with a new timestamp updates the same element. */
test("reltime_follows_a_new_iso_prop", async () => {
  const store = makeStore(Date.parse("2026-06-01T12:00:00Z"));
  const host = scratch();
  mount(host, html`<${RelTime} store=${store} iso="2026-06-01T11:59:00Z" />`);
  await settle();
  const first = host.querySelector("time");
  const before = first.textContent;
  mount(host, html`<${RelTime} store=${store} iso="2026-06-01T11:00:00Z" />`);
  await settle();
  return { before, after: host.querySelector("time").textContent, same: host.querySelector("time") === first };
});

const LOOP_RUNNING = {
  name: "alpha", status: "iterating", running: true, running_sources: ["driver", "lock"],
  segments: [{ verdict_sequence: "ISI" }],
};
const LOOP_SHIPPED = { name: "beta", status: "shipped", final_verdict: "SHIP", segments: [{ verdict_sequence: "IS" }] };
const LOOP_HELD = {
  name: "gamma", status: "iterating", loop_state: { state: "held", summary: "waiting for a human" },
  segments: [],
};

test("state_badge_and_history_render_the_app_js_dom", async () => {
  const host = scratch();
  const out = {};
  for (const [label, loop] of [["running", LOOP_RUNNING], ["shipped", LOOP_SHIPPED], ["held", LOOP_HELD]]) {
    const cell = scratch();
    mount(cell, html`<div><${StateBadge} loop=${loop} /><${VerdictHistory} loop=${loop} /></div>`);
    await settle();
    out[label] = shape(cell.firstChild);
  }
  const long = { name: "n", segments: [{ verdict_sequence: "ISISISISISISISBH" }] };
  const cell = scratch();
  mount(cell, html`<${VerdictHistory} loop=${long} />`);
  await settle();
  out.long = shape(cell.firstChild);
  host.remove();
  return out;
});

test("empty_state_and_severity_badge", async () => {
  const cell = scratch();
  mount(cell, html`<div>
    <${EmptyState} icon="✓" text="Nothing needs you." />
    <${SeverityBadge} item=${{ severity: "high" }} />
    <${SeverityBadge} item=${{ severity: "medium" }} />
    <${SeverityBadge} item=${{ severity: "low" }} />
    <${SeverityBadge} item=${{}} />
    <${SeverityBadge} item=${{ severity: "odd" }} />
  </div>`);
  await settle();
  return Array.from(cell.firstChild.children).map(shape);
});

test("mount_clears_the_placeholder_once_and_keeps_nodes_across_renders", async () => {
  const container = scratch();
  container.innerHTML = "<p id=\"placeholder\">Loading…</p><p>more static</p>";
  mount(container, html`<ul><li class="a">one</li></ul>`);
  await settle();
  const first = container.querySelector("li");
  const afterFirst = { children: container.children.length, placeholder: !!container.querySelector("#placeholder") };
  mount(container, html`<ul><li class="a">two</li></ul>`);
  await settle();
  const unmount = mount(container, html`<ul><li class="a">three</li></ul>`);
  await settle();
  const sameNode = container.querySelector("li") === first;
  const text = container.textContent;
  unmount();
  return { afterFirst, sameNode, text, afterUnmount: container.children.length };
});

for (const [name, fn] of Object.entries(cases)) {
  try {
    results[name] = { value: await fn() };
  } catch (err) {
    results[name] = { error: String((err && err.stack) || err) };
  }
}
window.__uiDone = true;
