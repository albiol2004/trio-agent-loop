/* Shared leaf components of the dashboard frontend (Preact + signals + htm).
 * Same class names and texts as the DOM app.js builds. */
import { h, render } from "preact";
import { useMemo } from "preact/hooks";
import { computed } from "@preact/signals";
import htm from "htm";
import { relTime, stateBadgeInfo, verdictSeq } from "./format.js";

export const html = htm.bind(h);

/* A relative time ("4m ago"). Its only child is a computed signal, which
 * @preact/signals renders as a text node it updates in place: when
 * store.now moves, that text node's data changes (characterData) and neither
 * this component nor its parent re-renders. */
export function RelTime({ store, iso, prefix = "", suffix = "" }) {
  const text = useMemo(
    () => computed(() => prefix + relTime(iso, store.now.value) + suffix),
    [store, iso, prefix, suffix]);
  const absolute = iso ? new Date(iso).toLocaleString() : "No activity recorded";
  return html`<time data-reltime="" datetime=${iso || ""} title=${absolute}>${text}</time>`;
}

/* One state word per loop (see format.stateBadgeInfo). */
export function StateBadge({ loop }) {
  const info = stateBadgeInfo(loop);
  return html`<span class=${info.className} title=${info.title}><span class="badge-icon">${info.icon}</span>${info.text}</span>`;
}

const VERDICT_LETTER = { ship: "S", iterate: "I", blocked: "B", needs_human: "H" };

/* The verdict history cells of a board row (app.js historyEl). */
export function VerdictHistory({ loop }) {
  const seq = verdictSeq(loop);
  if (!seq.length) {
    return html`<span class="hist" title="No evaluator verdicts parsed from LOG.md"><span class="hist-none">—</span></span>`;
  }
  const title = "Verdicts, oldest to newest: " + seq.map((v) => VERDICT_LETTER[v]).join(" ");
  return html`<span class="hist" title=${title} aria-label=${title}>${seq.slice(-12).map((v, i) =>
    html`<span key=${i} class=${"hist-cell hist-" + v}>${VERDICT_LETTER[v] || "?"}</span>`)}</span>`;
}

export function EmptyState({ icon, text }) {
  return html`<div class="empty"><span class="empty-icon">${icon}</span><span>${text}</span></div>`;
}

const SEVERITY_LABEL = { high: "Action", medium: "Check", low: "Note" };
const SEVERITY_TONE = { high: "negative", medium: "warning", low: "neutral" };
const SEVERITY_ICON = { high: "!", medium: "▲", low: "•" };

/* The severity pill of an inbox item (app.js severityBadge). */
export function SeverityBadge({ item }) {
  const sev = item.severity || "low";
  return html`<span class=${"badge badge-" + (SEVERITY_TONE[sev] || "neutral")}><span class="badge-icon">${SEVERITY_ICON[sev] || "•"}</span>${SEVERITY_LABEL[sev] || sev}</span>`;
}

const cleared = new WeakSet();

/* Render `vnode` into `container`. The first mount into a container drops
 * the static placeholder children of the HTML shell; returns an unmount. */
export function mount(container, vnode) {
  if (!cleared.has(container)) {
    cleared.add(container);
    container.textContent = "";
  }
  render(vnode, container);
  return function unmount() {
    render(null, container);
  };
}
