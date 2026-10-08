/* The dashboard's main board as keyed Preact components over the signals
 * store (GOAL DoD6).
 *
 * mountBoard(store, doc) renders into the id'd containers of index.html. A
 * delta that changes one loop re-renders that loop's row (and its running
 * card / attention row) only: every row reads its own loopSig(id); the lists
 * are driven by id/membership computeds that keep their previous value when
 * nothing about the list changed. Every relative time is common.js RelTime, a
 * signal-bound text node, so a clock tick yields characterData and nothing
 * else. The search input is never written while it has focus.
 *
 * Rendering rules (classes, labels, counts, empty states, ordering) are the
 * ones app.js renders; this file replaces renderLive .. renderDrawerInbox. */
import { computed, effect, signal } from "@preact/signals-core";
import { useMemo } from "preact/hooks";
import {
  html, mount, RelTime, StateBadge, VerdictHistory, EmptyState, SeverityBadge,
} from "./common.js";
import {
  fmtClock, iterationText, isArchived, latestVerdict, loopTitle, normVerdict, oneLine,
  phaseText, plural,
} from "./format.js";

const TABS = ["running", "attention", "all", "archived"];
const TAB_LABELS = { running: "Running", attention: "Attention", all: "All", archived: "Archived" };
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };
const NEEDS_KINDS = new Set([
  "needs_human", "blocked", "interrupted", "orphaned", "queue_fault",
  "error", "needs_retirement", "needs_land", "held", "conflict", "budget",
  "iteration_cap",
]);
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const STALE_AFTER_MS = 30 * 1000;
const BOARD_POLL_S = 5;

/* ------------------------------ helpers ------------------------------ */

function sameList(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/* A computed that hands back its previous value while `eq` says the new one
 * is the same, so its subscribers are not notified. */
function stable(fn, eq) {
  let prev;
  let has = false;
  return computed(() => {
    const next = fn();
    if (has && eq(prev, next)) return prev;
    prev = next;
    has = true;
    return next;
  });
}

const jsonEq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function tieBreak(a, b) {
  return String(a.name || "").localeCompare(String(b.name || "")) ||
    (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

const byActivityDesc = (a, b) =>
  String(b.last_activity || "").localeCompare(String(a.last_activity || "")) || tieBreak(a, b);

function setText(node, text) {
  if (!node) return;
  const first = node.firstChild;
  if (first && first.nodeType === 3 && !first.nextSibling) {
    if (first.data !== text) first.data = text;
  } else if (node.textContent !== text) {
    node.textContent = text;
  }
}

function setHidden(node, hidden) {
  if (node && node.hidden !== hidden) node.hidden = hidden;
}

function setAttr(node, name, value) {
  if (node && node.getAttribute(name) !== value) node.setAttribute(name, value);
}

function isNeedsItem(item, loop) {
  // A low-severity "interrupted" means liveness was not fully checked
  // (broker unknown), so it is a note, not a call to act.
  if (item.severity === "low" && item.kind === "interrupted") return false;
  if (NEEDS_KINDS.has(item.kind) || item.severity === "high") return true;
  return Boolean(loop && loop.running);
}

function sevRank(item) {
  return SEVERITY_RANK[item.severity] ?? 3;
}

function matchesTab(tab, loop, unreadByKey) {
  if (tab === "running") return Boolean(loop.running);
  if (tab === "attention") return (unreadByKey.get(loop.key) || 0) > 0;
  if (tab === "archived") return isArchived(loop);
  return true;
}

/* ------------------------------ model ------------------------------ */

/* Derived signals of one store (everything the board needs more than once). */
function buildModel(store) {
  const unreadByKey = stable(() => {
    const counts = new Map();
    for (const item of store.inbox.value) {
      if (!item.read) counts.set(item.key, (counts.get(item.key) || 0) + 1);
    }
    return counts;
  }, (a, b) => a.size === b.size && Array.from(a).every(([k, v]) => b.get(k) === v));

  /* Unread attention grouped per loop, split into "needs you" and notes. */
  const groups = stable(() => {
    const needs = new Map();
    const notes = new Map();
    const showRead = store.showReadInbox.value;
    let readCount = 0;
    for (const item of store.inbox.value) {
      if (item.read) {
        readCount += 1;
        if (!showRead) continue;
      }
      const sig = store.loopByKey(item.key);
      const loop = sig ? sig.value : undefined;
      const target = isNeedsItem(item, loop) ? needs : notes;
      if (!target.has(item.key)) target.set(item.key, { key: item.key, loop, items: [] });
      target.get(item.key).items.push(item);
    }
    const order = (g) => Math.min(...g.items.map(sevRank));
    const sortGroups = (map) => Array.from(map.values()).sort((a, b) =>
      order(a) - order(b) ||
      String(b.loop?.last_activity || "").localeCompare(String(a.loop?.last_activity || "")) ||
      (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
    for (const g of [...needs.values(), ...notes.values()]) {
      g.items.sort((a, b) => sevRank(a) - sevRank(b));
    }
    return { needs: sortGroups(needs), notes: sortGroups(notes), readCount };
  }, (a, b) => {
    const same = (x, y) => x.length === y.length && x.every((g, i) =>
      g.key === y[i].key && g.loop === y[i].loop && sameList(g.items, y[i].items));
    return a.readCount === b.readCount && same(a.needs, b.needs) && same(a.notes, b.notes);
  });

  const matchesFilters = (loop, workspace, q) => {
    if (workspace && loop.root !== workspace) return false;
    if (!q) return true;
    return [loop.title, loop.name, loop.workspace, loop.mission, loop.status]
      .some((v) => String(v || "").toLowerCase().includes(q));
  };

  const visibleIds = stable(() => {
    const workspace = store.workspace.value;
    const q = store.query.value.trim().toLowerCase();
    const tab = store.tab.value;
    const { key, dir } = store.sort.value;
    const unread = unreadByKey.value;
    const sign = dir === "asc" ? 1 : -1;
    const value = (loop) => key === "loop" ? loopTitle(loop).toLowerCase()
      : key === "workspace" ? (loop.workspace + "/" + loop.name).toLowerCase()
      : String(loop.last_activity || "");
    return store.loops.value
      .filter((l) => matchesFilters(l, workspace, q) && matchesTab(tab, l, unread))
      .sort((a, b) => {
        if (key === "activity") {
          const running = Number(Boolean(b.running)) - Number(Boolean(a.running));
          if (running !== 0) return running;
        }
        return sign * value(a).localeCompare(value(b)) || tieBreak(a, b);
      })
      .map((l) => l.id);
  }, sameList);

  const tabCounts = stable(() => {
    const workspace = store.workspace.value;
    const q = store.query.value.trim().toLowerCase();
    const unread = unreadByKey.value;
    const scoped = store.loops.value.filter((l) => matchesFilters(l, workspace, q));
    return TABS.map((tab) => scoped.filter((l) => matchesTab(tab, l, unread)).length);
  }, sameList);

  const runningIds = stable(() => store.loops.value.filter((l) => l.running)
    .sort(byActivityDesc).map((l) => l.id), sameList);

  /* [{root, name, count}] for the workspace filter. */
  const wsOptions = stable(() => {
    const counts = new Map();
    for (const loop of store.loops.value) counts.set(loop.root, (counts.get(loop.root) || 0) + 1);
    return store.workspaces.value.map((w) => ({
      root: w.root, name: w.name, count: counts.get(w.root) || 0,
    }));
  }, jsonEq);

  const busyNotes = stable(() => store.workspaces.value
    .filter((w) => w.unattributed_processes > 0)
    .map((w) => ({ root: w.root, name: w.name, n: w.unattributed_processes })), jsonEq);

  /* The clock only matters for the 7-day window: this changes (rarely) when a
   * loop ages out, never on a plain tick. */
  const shipped7 = computed(() => {
    const now = store.now.value;
    return store.loops.value.filter((l) => normVerdict(latestVerdict(l)) === "ship" &&
      verdictAge(l.verdict_mtime, now) <= WEEK_MS).length;
  });

  const summary = stable(() => {
    const loops = store.loops.value;
    const meta = store.meta.value;
    const workspaces = store.workspaces.value;
    const g = groups.value;
    const running = loops.filter((l) => l.running);
    const needsLoops = g.needs.filter((grp) => grp.items.some((i) => !i.read));
    const unread = g.needs.reduce((n, grp) => n + grp.items.filter((i) => !i.read).length, 0);
    const wsCount = workspaces.length;
    const wtShown = workspaces.filter((w) => w.worktree).length;
    let verdict;
    if (!loops.length) {
      verdict = "No loop mailboxes found yet.";
    } else if (needsLoops.length) {
      verdict = plural(needsLoops.length, "loop") + (needsLoops.length === 1 ? " needs" : " need") +
        " you" + (running.length ? "; " + plural(running.length, "loop") + " running." : "; none running.");
    } else if (running.length) {
      verdict = "All clear: " + plural(running.length, "loop") + " running, nothing needs you.";
    } else {
      verdict = "All clear. No loop is running and nothing needs you.";
    }
    const latest = loops.slice().sort((a, b) =>
      String(b.last_activity || "").localeCompare(String(a.last_activity || "")))[0];
    const scanned = meta.scanned || wsCount;
    const wtScanned = meta.worktrees_scanned || 0;
    const coverage = "Covers " + plural(wsCount - wtShown, "workspace") +
      (wtShown ? " and " + plural(wtShown, "worktree") : "") + " with loops (" +
      plural(scanned, "workspace") + (wtScanned ? " and " + plural(wtScanned, "worktree") : "") +
      " scanned).";
    const broker = {
      disabled: "Broker liveness is not configured, so loops driven only through Omnigent broker sessions cannot show as running.",
      unreachable: "The Omnigent broker did not answer; loops driven only through broker sessions may be running but show as not running.",
      truncated: "The Omnigent broker session list was too long to read in full; some broker-only loops may not show as running.",
    }[meta.broker || "disabled"] || "";
    const partial = workspaces.filter((w) => w.error);
    return {
      verdict,
      latest: latest
        ? { iso: latest.last_activity || null, title: loopTitle(latest), workspace: latest.workspace }
        : null,
      coverage,
      broker,
      partial: partial.length
        ? "Could not read " + partial.map((w) => w.name).join(", ") + ". Other workspaces are shown."
        : "",
      needsLoops: needsLoops.length,
      unread,
      running: running.length,
      runningContext: running.length ? runningContext(running) : "",
      shippedTotal: loops.filter((l) => normVerdict(latestVerdict(l)) === "ship").length,
      loops: loops.length,
      wsCount,
      wtShown,
    };
  }, jsonEq);

  /* Inbox items of the loop open in the drawer. */
  const drawerItems = stable(() => {
    const key = store.openKey.value;
    return key === null ? [] : store.inbox.value.filter((item) => item.key === key);
  }, sameList);

  return {
    drawerItems, unreadByKey, groups, visibleIds, tabCounts, runningIds, wsOptions, busyNotes, shipped7, summary,
  };
}

const models = new WeakMap();
function modelOf(store) {
  let model = models.get(store);
  if (!model) {
    model = buildModel(store);
    models.set(store, model);
  }
  return model;
}

function verdictAge(iso, nowMs) {
  const t = Date.parse(iso || "");
  return Number.isNaN(t) ? Infinity : nowMs - t;
}

/* "2 via driver · 1 process only": distinct phases, else evidence counts. */
function runningContext(running) {
  const phases = Array.from(new Set(running.map(phaseText).filter(Boolean)));
  if (phases.length) return phases.slice(0, 2).join(", ") + (phases.length > 2 ? "…" : "");
  const byDriver = running.filter((l) => (l.running_sources || []).includes("driver")).length;
  const other = running.length - byDriver;
  return [byDriver ? byDriver + " via driver" : "", other ? other + " via process or session" : ""]
    .filter(Boolean).join(" · ");
}

/* ------------------------------ components ------------------------------ */

/* Mark read / unread: store.setInboxRead; a failure is shown in the banner. */
function markRead(ctx, items, read) {
  return ctx.setInboxRead(items, read);
}

function InboxMarkButton({ ctx, item }) {
  const label = item.read ? "Mark unread" : "Mark read";
  const onClick = (event) => {
    event.stopPropagation();
    markRead(ctx, [item], !item.read);
  };
  return html`<button type="button" class="btn btn-ghost btn-small"
    aria-label=${label + ": " + (item.headline || "item")} onClick=${onClick}>${label}</button>`;
}

/* One row per loop: the most severe item leads, the rest are counted. */
function AttentionRow({ ctx, group }) {
  const { store } = ctx;
  const lead = group.items[0];
  const loop = group.loop;
  const title = loop ? loopTitle(loop) : lead.loop;
  const allRead = group.items.every((i) => i.read);
  const markLabel = allRead ? "Mark unread" : (group.items.length > 1 ? "Mark all read" : "Mark read");
  const open = () => store.openLoop(group.key);
  const onRowClick = (event) => {
    if (event.target.closest("button")) return;
    open();
  };
  const onMark = (event) => {
    event.stopPropagation();
    markRead(ctx, group.items, !allRead);
  };
  return html`<div class=${"attn-row" + (allRead ? " is-read" : "")} data-key=${group.key}
      onClick=${onRowClick}>
    <${SeverityBadge} item=${lead} />
    <div class="attn-main">
      <button type="button" class="attn-open"
        aria-label=${"Open " + title + ": " + lead.headline} onClick=${open}>${title}</button>
      <span class="attn-where mono">${lead.workspace + " / " + lead.loop}</span>
      <p class="attn-reason">${lead.headline || ""}${group.items.length > 1
        ? html`<span class="attn-more">${" + " + plural(group.items.length - 1, "more item")}</span>` : null}</p>
      ${lead.detail ? html`<span class="attn-detail" title=${lead.detail}>${oneLine(lead.detail, 220)}</span>` : null}
    </div>
    <div class="attn-side">
      <span class="caption">${loop && loop.last_activity
        ? html`<${RelTime} store=${store} iso=${loop.last_activity} />` : null}</span>
      <button type="button" class="btn btn-ghost btn-small"
        aria-label=${markLabel + ": " + title} onClick=${onMark}>${markLabel}</button>
    </div>
  </div>`;
}

function NeedsList({ ctx }) {
  const { store, model } = ctx;
  if (!store.loaded.value) return html`<div class="row-skeleton skeleton"></div>`;
  const { needs } = model.groups.value;
  if (!needs.length) {
    return html`<${EmptyState} icon="✓"
      text="Nothing needs you. Loops that block, ask for human verification or stop mid-loop appear here." />`;
  }
  return needs.map((group) => html`<${AttentionRow} key=${group.key} ctx=${ctx} group=${group} />`);
}

function NotesList({ ctx }) {
  const { notes } = ctx.model.groups.value;
  return notes.map((group) => html`<${AttentionRow} key=${group.key} ctx=${ctx} group=${group} />`);
}

function RunCard({ ctx, id }) {
  const { store } = ctx;
  const sig = store.loopSig(id);
  const loop = sig ? sig.value : null;
  if (!loop) return null;
  const phase = phaseText(loop);
  const meter = loop.iteration != null && loop.max_iterations
    ? html`<span class="meter" role="img"
        aria-label=${"Iteration " + loop.iteration + " of " + loop.max_iterations}><span
        class="meter-fill"
        style=${{ width: Math.min(100, (loop.iteration / loop.max_iterations) * 100) + "%" }}></span></span>`
    : null;
  return html`<button type="button" class="run-card" data-key=${loop.key}
      onClick=${() => store.openLoop(loop.key)}>
    <span class="run-top">
      <${StateBadge} loop=${loop} />
      <span class="caption"><${RelTime} store=${store} iso=${loop.last_activity} prefix="last write " /></span>
    </span>
    <span class="run-title">${loopTitle(loop)}</span>
    <span class="attn-where mono">${loop.workspace + " / " + loop.name}</span>
    <span class="run-meta">
      <span>${"Iteration " + iterationText(loop)}</span>
      ${phase ? html`<span>${phase}</span>` : null}
    </span>
    ${meter}
    <span class="caption">${"Live via " + (loop.running_sources || []).join(", ")}</span>
  </button>`;
}

function RunningList({ ctx }) {
  const { store, model } = ctx;
  if (!store.loaded.value) return html`<div class="row-skeleton skeleton"></div>`;
  const ids = model.runningIds.value;
  const notes = model.busyNotes.value;
  return [
    ...notes.map((w) => html`<p class="run-note" key=${"note:" + w.root}>${
      plural(w.n, "process", "processes") + " working in " + w.name +
      " name no mailbox, so no loop there is marked running."}</p>`),
    ...(ids.length
      ? ids.map((id) => html`<${RunCard} key=${id} ctx=${ctx} id=${id} />`)
      : [html`<${EmptyState} key="empty" icon="○"
          text="No loop is running. A loop shows here while its driver, lock, process or session is live." />`]),
  ];
}

function KpiTiles({ ctx }) {
  const { store, model } = ctx;
  if (!store.loaded.value) {
    return [0, 1, 2, 3].map((i) => html`<div class="kpi skeleton" key=${i}></div>`);
  }
  const s = model.summary.value;
  const shipped7 = model.shipped7.value;
  // [label, value, context, tone, target]
  const tiles = [
    ["Needs you", s.needsLoops, s.needsLoops ? plural(s.unread, "unread item") : "Nothing waiting",
      s.needsLoops ? "warning" : "", "#needs"],
    ["Running now", s.running,
      s.running ? s.runningContext : "No live driver, process or session",
      s.running ? "live" : "", "#running"],
    ["Shipped, last 7 days", shipped7, plural(s.shippedTotal, "shipped loop") + " in total", "", null],
    ["Loops tracked", s.loops,
      "in " + plural(s.wsCount - s.wtShown, "workspace") +
        (s.wtShown ? " + " + plural(s.wtShown, "worktree") : ""), "", "#loops"],
  ];
  return tiles.map(([label, value, context, tone, target], i) => {
    const children = [
      html`<span class="eyebrow">${label}</span>`,
      html`<span class="kpi-value">${String(value)}</span>`,
      html`<span class="kpi-context">${context}</span>`,
    ];
    const cls = "kpi" + (tone ? " kpi-" + tone : "");
    return target
      ? html`<a key=${"a" + i} class=${cls} href=${target}>${children}</a>`
      : html`<div key=${"d" + i} class=${cls}>${children}</div>`;
  });
}

/* "Most recent activity: Title (ws), 3m ago. Covers ...": the age is a
 * RelTime, the rest is plain text. */
function VerdictSub({ ctx }) {
  const { store, model } = ctx;
  if (!store.loaded.value) return null;
  const s = model.summary.value;
  if (!s.latest) {
    return "Start a loop with /trio-init in a project; it appears here on the next update. " + s.coverage;
  }
  return html`${"Most recent activity: " + s.latest.title + " (" + s.latest.workspace + "), "}<${RelTime}
    store=${store} iso=${s.latest.iso} />${". " + s.coverage}`;
}

function WorkspaceOptions({ ctx }) {
  const { store, model } = ctx;
  const selected = store.workspace.value;
  return [
    html`<option key="" value="" selected=${selected === ""}>All workspaces</option>`,
    ...model.wsOptions.value.map((w) => html`<option key=${w.root} value=${w.root}
      title=${w.root} selected=${selected === w.root}>${w.name + " (" + w.count + ")"}</option>`),
  ];
}

function TabButtons({ ctx }) {
  const { store, model } = ctx;
  const active = store.tab.value;
  const counts = model.tabCounts.value;
  return TABS.map((tab, i) => html`<button key=${tab} type="button" class="seg-btn"
      aria-pressed=${String(active === tab)} onClick=${() => store.setTab(tab)}>${
      TAB_LABELS[tab]}<span class="seg-count">${String(counts[i])}</span></button>`);
}

/* One-line diagnosis status for a card (the full result lives in the drawer). */
function diagnosisInfo(d) {
  if (!d || !d.status) return null;
  let text;
  let cls = "row-flag row-flag-muted";
  if (d.status === "running") {
    text = "Diagnosing (" + (d.harness || "?") + ")… " + (d.last_event || "");
    cls = "row-flag";
  } else if (d.status === "done") {
    text = "Diagnosis: " + (d.state || "?") + (d.proposed_fix && d.proposed_fix !== "none"
      ? " → " + d.proposed_fix + (d.proposed_fix_rejected ? " (rejected)" : "") : "")
      + (d.needs_human_input ? " · needs your input" : "");
    if (d.needs_human_input) cls = "row-flag";
  } else {
    text = "Diagnosis failed";
  }
  const title = (d.harness || "") + " " + (d.model || "") + (d.finished_at ? " · " + d.finished_at : "")
    + (d.error ? " · " + d.error : "");
  return { cls, text: oneLine(text, 90), title };
}

/* One board row. It reads its own loop signal, its unread count and whether
 * it is the open row, nothing else: a delta elsewhere does not re-render it. */
function LoopRow({ ctx, id }) {
  const { store, model } = ctx;
  const sig = store.loopSig(id);
  const loop = sig ? sig.value : null;
  const key = loop ? loop.key : "";
  const unread = useMemo(() => computed(() => model.unreadByKey.value.get(key) || 0),
    [model, key]).value;
  const active = useMemo(() => computed(() => store.openKey.value === key),
    [store, key]).value;
  if (!loop) return null;
  const archived = isArchived(loop);
  const diag = diagnosisInfo(loop.diagnosis);
  const phase = phaseText(loop);
  const reasons = loop.worktree_reasons || [];
  const open = () => store.openLoop(key);
  const onClick = (event) => {
    if (event.target.closest("button, a")) return;
    open();
  };
  const onKeyDown = (event) => {
    if (event.key !== "Enter" || event.target.closest("button, a")) return;
    open();
  };
  return html`<tr class=${"loop-row" + (active ? " active" : "") + (archived ? " is-archived" : "")}
      data-key=${key} data-id=${id} onClick=${onClick} onKeyDown=${onKeyDown}>
    <th scope="row">
      <button type="button" class="row-open" title=${loop.mission || loopTitle(loop)}
        onClick=${open}>${loopTitle(loop)}</button>
      <span class="row-sub mono">${loop.name}</span>
      ${unread > 0 ? html`<span class="row-flag">${plural(unread, "unread item")}</span>` : null}
      ${archived ? html`<span class="row-flag row-flag-muted">Archived</span>` : null}
      ${loop.driver === "claude-workflow" ? html`<span class="row-flag row-flag-muted"
        title="Claude-native Trio loop driven by the trio-native Workflow">Native (claude-workflow)</span>` : null}
      ${diag ? html`<span class=${diag.cls} title=${diag.title}>${diag.text}</span>` : null}
      ${loop.worktree && reasons.length > 0 ? html`<span class="row-flag row-flag-muted"
        title=${"Shown because this linked worktree's copy is " + reasons.join(", ") +
          "; untouched committed copies of the main checkout's mailboxes are hidden."}>${
        "Worktree: " + reasons.join(", ")}</span>` : null}
    </th>
    <td class="col-ws" title=${loop.root}>${loop.workspace}</td>
    <td><${StateBadge} loop=${loop} />${phase ? html`<span class="row-sub">${phase}</span>` : null}</td>
    <td class="num col-iter">${iterationText(loop)}</td>
    <td class="col-hist"><${VerdictHistory} loop=${loop} /></td>
    <td class="num">${loop.last_activity
      ? html`<${RelTime} store=${store} iso=${loop.last_activity} />`
      : html`<time datetime="" title="No activity recorded">—</time>`}</td>
  </tr>`;
}

function LoopRows({ ctx }) {
  const { store, model } = ctx;
  if (!store.loaded.value) {
    return html`<tr class="row-loading" key="loading"><td colspan="6"><div class="row-skeleton skeleton"></div></td></tr>`;
  }
  const ids = model.visibleIds.value;
  const total = store.loopIds.value.length;
  if (!ids.length) {
    const text = !total
      ? "No loop mailboxes found. Start one with /trio-init in a project."
      : "No loops match these filters.";
    return html`<tr class="row-empty" key="empty"><td colspan="6"><${EmptyState} icon="○" text=${text} /></td></tr>`;
  }
  return ids.map((id) => html`<${LoopRow} key=${id} ctx=${ctx} id=${id} />`);
}

function DrawerInboxRows({ ctx }) {
  const items = ctx.model.drawerItems.value;
  return items.map((item) => html`<div key=${item.id}
      class=${"attn-row attn-row-compact" + (item.read ? " is-read" : "")}>
    <${SeverityBadge} item=${item} />
    <div class="attn-main">
      <span class="attn-reason">${item.headline || ""}</span>
      ${item.detail ? html`<span class="attn-detail">${item.detail}</span>` : null}
    </div>
    <${InboxMarkButton} ctx=${ctx} item=${item} />
  </div>`);
}

/* The label of #live-text. The data time is a [data-reltime] node: it moves
 * with the feed, never with the render. */
function LiveLabel({ ctx }) {
  const state = ctx.liveState.value;
  if (!state.time) return state.label;
  return html`${state.label}<span data-reltime="">${state.time}</span>`;
}

/* ------------------------------ mount ------------------------------ */

export function mountBoard(store, doc = document) {
  const model = modelOf(store);
  const disposers = [];
  const unmounts = [];
  const listeners = [];
  const el = (id) => doc.getElementById(id);
  const fx = (fn) => disposers.push(effect(fn));
  const on = (node, type, handler) => {
    if (!node) return;
    node.addEventListener(type, handler);
    listeners.push(() => node.removeEventListener(type, handler));
  };
  const into = (id, vnode) => {
    const node = el(id);
    if (node) unmounts.push(mount(node, vnode));
  };

  /* The banner shows a feed error, or a failed mark-read until the next good
   * feed update. */
  const actionErrorSig = signal(null);

  const ctx = {
    store,
    model,
    setInboxRead(items, read) {
      return store.setInboxRead(items, read).then(
        () => { if (actionErrorSig.peek() !== null) actionErrorSig.value = null; },
        (err) => { actionErrorSig.value = "Could not update the attention list: " + err.message; });
    },
  };

  /* Live indicator state (app.js renderLive + "Reconnecting"). */
  const stale = computed(() => {
    const feed = store.feed.value;
    const now = store.now.value;
    const updatedAt = feed.updatedAt || store.meta.value.updated_at || null;
    const buildAge = updatedAt && !Number.isNaN(Date.parse(updatedAt)) ? now - Date.parse(updatedAt) : null;
    const sinceOk = feed.lastOk ? now - feed.lastOk : null;
    return (buildAge != null && buildAge > STALE_AFTER_MS) || (sinceOk != null && sinceOk > STALE_AFTER_MS);
  });
  const liveState = computed(() => {
    const feed = store.feed.value;
    const offline = feed.status === "offline" || actionErrorSig.value !== null;
    const updatedAt = feed.updatedAt || store.meta.value.updated_at || null;
    const clock = fmtClock(updatedAt);
    let cls = "live-ok";
    let label;
    let time = null;
    if (feed.status === "reconnecting") {
      cls = "live-connecting";
      label = "Reconnecting";
    } else if (!feed.lastOk) {
      cls = offline ? "live-offline" : "live-connecting";
      label = offline ? "Offline" : "Connecting";
    } else if (offline) {
      cls = "live-offline";
      label = "Offline · data from ";
      time = clock;
    } else if (stale.value) {
      cls = "live-stale";
      label = "Stale · data from ";
      time = clock;
    } else {
      label = "Live · ";
      time = clock;
    }
    const title = feed.mode === "stream"
      ? "Push (SSE)"
      : "Polling every " + BOARD_POLL_S + " s (stream unavailable)";
    return {
      cls, title, label, time: time || null,
    };
  });
  ctx.liveState = liveState;

  /* Mark-read failures clear on the next good feed update (app.js
   * hideBoardError on a successful poll). */
  let lastSeenOk = store.feed.peek().lastOk;
  fx(() => {
    const ok = store.feed.value.lastOk;
    if (ok !== lastSeenOk) {
      lastSeenOk = ok;
      if (actionErrorSig.peek() !== null) actionErrorSig.value = null;
    }
  });

  /* ---- header: live indicator, verdict, banners, KPIs ---- */
  fx(() => {
    const { cls, title } = liveState.value;
    const live = el("live");
    if (!live) return;
    if (live.className !== "live " + cls) live.className = "live " + cls;
    if (live.getAttribute("title") !== title) live.setAttribute("title", title);
  });
  into("live-text", html`<${LiveLabel} ctx=${ctx} />`);

  fx(() => {
    setText(el("verdict"), store.loaded.value ? model.summary.value.verdict : "Loading loops");
  });
  into("verdict-sub", html`<${VerdictSub} ctx=${ctx} />`);
  fx(() => {
    const feed = store.feed.value;
    const message = actionErrorSig.value !== null
      ? actionErrorSig.value
      : feed.status === "offline"
        ? (store.loaded.peek()
          ? "Lost contact with the dashboard server (" + feed.error +
            "). Showing the last data received; retrying every " + BOARD_POLL_S + " seconds."
          : "Cannot reach the dashboard server (" + feed.error +
            "). Retrying every " + BOARD_POLL_S + " seconds.")
        : null;
    setHidden(el("board-error"), message === null);
    if (message !== null) setText(el("board-error-text"), message);
  });
  on(el("board-retry"), "click", () => {
    if (typeof store.retry === "function") store.retry();
  });
  fx(() => {
    const partial = store.loaded.value ? model.summary.value.partial : "";
    setHidden(el("board-partial"), !partial);
    setText(el("board-partial"), partial);
  });
  fx(() => {
    const broker = store.loaded.value ? model.summary.value.broker : "";
    setHidden(el("broker-note"), !broker);
    setText(el("broker-note"), broker);
  });
  into("kpis", html`<${KpiTiles} ctx=${ctx} />`);

  /* ---- needs you ---- */
  fx(() => {
    if (!store.loaded.value) return;
    const { needs, readCount } = model.groups.value;
    const unreadGroups = needs.filter((g) => g.items.some((i) => !i.read));
    const showRead = store.showReadInbox.value;
    setText(el("needs-count"), unreadGroups.length ? String(unreadGroups.length) : "");
    const toggle = el("inbox-toggle");
    setHidden(toggle, readCount === 0);
    setText(toggle, (showRead ? "Hide read (" : "Show read (") + readCount + ")");
    setAttr(toggle, "aria-expanded", String(showRead));
  });
  on(el("inbox-toggle"), "click", () => store.toggleShowRead());
  into("needs-list", html`<${NeedsList} ctx=${ctx} />`);

  /* ---- running now ---- */
  fx(() => {
    if (!store.loaded.value) return;
    const n = model.runningIds.value.length;
    setText(el("running-count"), n ? String(n) : "");
  });
  into("running-list", html`<${RunningList} ctx=${ctx} />`);

  /* ---- all loops: filters, tabs, sort, rows ---- */
  const search = el("loop-search");
  on(search, "input", (event) => store.setQuery(event.target.value));
  fx(() => {
    const q = store.query.value;
    // never re-set the input while it has focus: the user's text and caret win
    if (search && search.value !== q && doc.activeElement !== search) search.value = q;
  });
  const select = el("workspace-filter");
  if (select) unmounts.push(mount(select, html`<${WorkspaceOptions} ctx=${ctx} />`));
  on(select, "change", (event) => store.setWorkspace(event.target.value));
  fx(() => {
    // a saved workspace that no longer exists falls back to "All"
    if (!store.loaded.value) return;
    const workspace = store.workspace.value;
    if (workspace && !model.wsOptions.value.some((w) => w.root === workspace)) {
      store.setWorkspace("");
    }
  });
  into("tabs", html`<${TabButtons} ctx=${ctx} />`);
  for (const btn of doc.querySelectorAll(".th-sort")) {
    on(btn, "click", () => {
      const key = btn.dataset.sort;
      const current = store.sort.peek();
      store.setSort(current.key === key
        ? { key, dir: current.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "activity" ? "desc" : "asc" });
    });
  }
  fx(() => {
    const { key, dir } = store.sort.value;
    for (const th of doc.querySelectorAll(".loop-table th[aria-sort]")) {
      const button = th.querySelector(".th-sort");
      const thKey = button && button.dataset.sort;
      setAttr(th, "aria-sort", thKey === key ? (dir === "asc" ? "ascending" : "descending") : "none");
    }
  });
  into("loop-rows", html`<${LoopRows} ctx=${ctx} />`);

  /* ---- review notes ---- */
  fx(() => {
    if (!store.loaded.value) return;
    const { notes } = model.groups.value;
    setHidden(el("notes"), notes.length === 0);
    setText(el("notes-count"), String(notes.reduce((n, g) => n + g.items.length, 0)));
    setText(el("notes-sub"), "Drift, overlap, repair and unconfirmed-stop notes on " +
      plural(notes.length, "loop") + " that are not running");
  });
  into("notes-list", html`<${NotesList} ctx=${ctx} />`);

  /* ---- the open loop's inbox items, inside the drawer ---- */
  fx(() => setHidden(el("drawer-inbox-section"), model.drawerItems.value.length === 0));
  into("drawer-inbox-list", html`<${DrawerInboxRows} ctx=${ctx} />`);

  return function unmount() {
    for (const dispose of disposers.splice(0)) dispose();
    for (const off of listeners.splice(0)) off();
    for (const un of unmounts.splice(0)) un();
  };
}
