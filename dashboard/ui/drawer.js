/* The loop drawer (frame + overview) as Preact components over the signals
 * store (api:UiDrawer). Ports of app.js openDrawer/closeDrawer, renderDetail,
 * renderDetailFacts, renderCommits, renderSlices, renderTimeline,
 * renderDrawerTabs/showDrawerTab and renderDrawerControls/controlLoop, with
 * the same classes and texts.
 *
 * Every data-driven part of the static drawer shell in index.html is its own
 * Preact root reading only the derived signals it needs; the derived signals
 * (`stable`) keep their previous value while the data they are made of is
 * unchanged, so an equal detail or a clock tick re-renders nothing and the
 * only DOM write a tick causes is the text of a RelTime. The heavy views
 * (timeline tab, files, graph, transcripts, unblock panel, inbox list) stay
 * app.js code and are reached only through the optional `legacy` hooks, each
 * called from an effect when its input changes, never from a timer. */
import { Fragment } from "preact";
import { useMemo } from "preact/hooks";
import { computed, effect, signal, untracked } from "@preact/signals";
import { html, mount, RelTime, StateBadge, VerdictHistory } from "./common.js";
import { fmtDuration, loopTitle, normVerdict, verdictSeq } from "./format.js";

const DRAWER_TABS = ["overview", "timeline", "files", "graph", "transcripts"];
const COMMIT_PREVIEW = 8;
const LIFECYCLES = [
  "planned", "in_flight", "pending_eval", "shipped", "abandoned",
  "building", "retired", "faulted", "repairing",
];
/* Canonical order of the slice-count summary of an iteration header. */
const SLICE_LIFECYCLE_ORDER = ["shipped", "building", "retired", "faulted", "repairing", "planned"];
const FOCUSABLE = 'button:not([disabled]), [href], input, select, [tabindex]:not([tabindex="-1"])';

/* Action notes survive refreshes and re-opens of a drawer: module state keyed
 * by loop key; `notes` is replaced on every change so readers re-run. */
const notes = signal(new Map());

function setNote(key, note) {
  const next = new Map(notes.peek());
  next.set(key, note);
  notes.value = next;
}

function cssClass(value) {
  return String(value).replace(/[^a-z0-9]+/gi, "-").toLowerCase();
}

/* A computed whose value keeps its identity while its JSON is unchanged, so
 * its readers do not re-render for a refetch that changed nothing they show. */
function stable(fn) {
  let text;
  let value;
  return computed(() => {
    const next = fn();
    const json = JSON.stringify(next === undefined ? null : next);
    if (json === text) return value;
    text = json;
    value = next;
    return next;
  });
}

/* ------------------------- imperative, guarded writes ------------------------- */

function setHidden(node, hidden) {
  if (node && node.hidden !== hidden) node.hidden = hidden;
}

function setProp(node, name, value) {
  if (node && node[name] !== value) node[name] = value;
}

/* ------------------------- overview components ------------------------- */

function Badge({ ctx }) {
  const loop = ctx.badge.value;
  if (!loop) return ctx.store.openKey.value === null ? "—" : "LOADING";
  return html`<${StateBadge} loop=${loop} />`;
}

function Title({ ctx }) {
  const head = ctx.head.value;
  return head ? head.name : "—";
}

function Where({ ctx }) {
  const head = ctx.head.value;
  return head ? head.where : "";
}

function Mission({ ctx }) {
  const mission = ctx.mission.value;
  return mission === null ? "" : (mission || "No mission recorded.");
}

function FactIter({ ctx }) {
  const facts = ctx.facts.value;
  return facts ? facts.iter : "—";
}

function FactVerdict({ ctx }) {
  const facts = ctx.facts.value;
  return facts ? facts.verdict : "—";
}

function FactActivity({ ctx }) {
  const facts = ctx.facts.value;
  return facts ? html`<${RelTime} store=${ctx.store} iso=${facts.activity} />` : "—";
}

function FactSessions({ ctx }) {
  const facts = ctx.facts.value;
  return facts ? facts.sessions : "—";
}

function Strip({ ctx }) {
  const strip = ctx.strip.value;
  if (!strip) return null;
  if (verdictSeq(strip).length) {
    return html`<${VerdictHistory} loop=${strip} /><span class="caption">${" S shipped · I iterate · B blocked · H needs human, oldest first"}</span>`;
  }
  const latest = strip.final_verdict ? String(strip.final_verdict).toUpperCase() : "none";
  return html`<span class="caption">${"No evaluator verdicts parsed from LOG.md. Latest recorded verdict: " + latest + "."}</span>`;
}

function CommitRow({ commit }) {
  return html`<div class="commit-row">
    <span class="commit-sha" title=${commit.sha || ""}>${commit.short || (commit.sha || "").slice(0, 7)}</span>
    ${commit.slice ? html`<span class="meta-chip slice-chip">${commit.slice}</span>` : null}
    <span class="commit-subject" title=${commit.subject || ""}>${commit.subject || ""}</span>
  </div>`;
}

function CommitList({ ctx }) {
  const { store } = ctx;
  const commits = ctx.commits.value;
  const key = store.openKey.value;
  const name = "commits:" + key;
  const showAll = key !== null && store.isExpanded(name);
  const shown = showAll ? commits : commits.slice(0, COMMIT_PREVIEW);
  const rows = shown.map((c, i) => html`<${CommitRow} key=${c.sha || "i" + i} commit=${c} />`);
  if (commits.length > COMMIT_PREVIEW) {
    rows.push(html`<button key="more" type="button" class="btn btn-ghost btn-small"
      onClick=${() => store.toggleExpanded(name)}>${showAll ? "Show fewer commits" : "Show all " + commits.length + " commits"}</button>`);
  }
  return rows;
}

function lifecycleChipFor(lifecycle) {
  const s = String(lifecycle || "");
  const label = s.replace("_", " ");
  return html`<span class=${"lifecycle-chip lifecycle-" + s}>${label.charAt(0).toUpperCase() + label.slice(1)}</span>`;
}

function SliceRow({ slice }) {
  const repo = typeof slice.repo === "string" ? slice.repo.trim() : "";
  const faults = Array.isArray(slice.open_faults) ? slice.open_faults : [];
  return html`<div class="slice-row">
    <span class="slice-id mono">${String(slice.id ?? "")}</span>
    ${repo && repo !== "." && repo !== "home"
      ? html`<span class="meta-chip slice-repo mono" title=${"repo: " + repo}>${repo}</span>` : null}
    ${lifecycleChipFor(slice.lifecycle)}
    ${slice.retired_sha
      ? html`<span class="slice-sha mono" title=${String(slice.retired_sha)}>${String(slice.retired_sha).slice(0, 7)}</span>` : null}
    ${slice.verdict
      ? html`<span class=${"verdict-word verdict-" + normVerdict(slice.verdict)}>${String(slice.verdict).toUpperCase()}</span>` : null}
    ${faults.map((fid, i) => html`<span key=${fid + ":" + i} class="meta-chip fault-chip">${String(fid)}</span>`)}
  </div>`;
}

function SliceList({ ctx }) {
  return ctx.slices.value.map((s, i) => html`<${SliceRow} key=${s.id ?? "i" + i} slice=${s} />`);
}

/* Lifecycle comes from detail.iterations; absent (older server), fall back to
 * the timeline verdicts: last verdict wins, else entries mean in_flight. */
function iterLifecycle(n, data) {
  for (const it of data.iterations) {
    if (it && Number(it.n) === Number(n) && LIFECYCLES.includes(it.lifecycle)) return it.lifecycle;
  }
  let lastVerdict = null;
  let count = 0;
  for (const e of data.entries) {
    if (e.iteration == null || Number(e.iteration) !== Number(n)) continue;
    count++;
    if (e.verdict) lastVerdict = String(e.verdict).toUpperCase();
  }
  if (lastVerdict === "SHIP") return "shipped";
  if (lastVerdict === "ITERATE" || lastVerdict === "BLOCKED") return "abandoned";
  if (lastVerdict === "NEEDS_HUMAN") return "pending_eval";
  return count ? "in_flight" : "planned";
}

/* "3 shipped · 1 faulted" for the slices of one iteration, open-loop only. */
function sliceCountSummary(iterNum, slices) {
  const counts = new Map();
  for (const s of slices) {
    if (!s || s.iteration == null || Number(s.iteration) !== Number(iterNum)) continue;
    const st = String(s.lifecycle || "");
    counts.set(st, (counts.get(st) || 0) + 1);
  }
  const parts = [];
  for (const st of SLICE_LIFECYCLE_ORDER) {
    const n = counts.get(st);
    if (n) parts.push(n + " " + st);
  }
  for (const [st, n] of counts) {
    if (!SLICE_LIFECYCLE_ORDER.includes(st)) parts.push(n + " " + st);
  }
  return parts.join(" · ");
}

/* Entries grouped strictly by iteration (file order kept inside a group). */
function groupTimeline(data) {
  const groups = new Map();
  data.entries.forEach((entry, index) => {
    const key = entry.iteration ?? null;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ entry, index });
  });
  const order = [...groups.keys()].sort((x, y) => (x == null ? 1e9 : x) - (y == null ? 1e9 : y));
  return order.map((iter) => ({
    iter,
    lifecycle: iter == null ? null : iterLifecycle(iter, data),
    summary: iter != null && data.mode === "open-loop" ? sliceCountSummary(iter, data.slices) : "",
    rows: groups.get(iter),
  }));
}

function TimelineRow({ entry }) {
  let summary = entry.summary || "";
  if (entry.verdict) {
    summary = summary.replace(/^VERDICT:\s*(SHIP|ITERATE|BLOCKED|NEEDS_HUMAN)\s*[—–-]\s*/i, "");
  }
  return html`<div class="timeline-row">
    <span class=${"role-chip role-" + cssClass(entry.role)}>${entry.role || "?"}</span>
    <span class="timeline-dur">${fmtDuration(entry.duration_sec)}</span>
    <div class="timeline-text" title=${summary}>${entry.verdict
      ? html`<span class=${"verdict-word verdict-" + entry.verdict.toLowerCase()}>${entry.verdict}</span>${" "}` : null}${entry.scope
      ? html`<span class="meta-chip scope-chip">${String(entry.scope)}</span>${" "}` : null}${entry.slice
      ? html`<span class="meta-chip slice-chip">${String(entry.slice)}</span>${" "}` : null}${summary}</div>
  </div>`;
}

function Timeline({ ctx }) {
  const { store } = ctx;
  const data = ctx.timeline.value;
  const groups = useMemo(() => (data ? groupTimeline(data) : []), [data]);
  if (!data) {
    if (store.openKey.value === null) return null;
    if (store.detailStatus.value === "error") {
      return html`<span class="timeline-empty">${"Failed to load detail: " + store.detailError.value}</span>`;
    }
    return html`<span class="timeline-empty">Loading…</span>`;
  }
  if (!data.entries.length) return html`<div class="timeline-empty">No log entries yet.</div>`;
  return groups.map((group) => html`<${Fragment} key=${"g" + group.iter}>
    <div class="iter-header">
      <span class="iter-header-label">${group.iter == null ? "unattributed" : "Iteration " + group.iter}</span>
      ${group.lifecycle ? lifecycleChipFor(group.lifecycle) : null}
      ${group.summary ? html`<span class="iter-slice-summary">${group.summary}</span>` : null}
    </div>
    ${group.rows.map(({ entry, index }) =>
      html`<${TimelineRow} key=${"e" + (entry.seq ?? index) + ":" + (entry.started_at || "")} entry=${entry} />`)}
  <//>`);
}

function Tabs({ ctx }) {
  const current = ctx.store.drawerTab.value;
  return DRAWER_TABS.map((tab) => html`<button key=${tab} type="button"
    class=${"drawer-tab" + (current === tab ? " active" : "")}
    aria-pressed=${String(current === tab)}
    onClick=${() => { ctx.store.drawerTab.value = tab; }}>${tab.charAt(0).toUpperCase() + tab.slice(1)}</button>`);
}

function ControlNote({ ctx }) {
  const view = ctx.controls.value;
  if (!view) return "";
  return html`${view.text}${view.iso ? html`<${RelTime} store=${ctx.store} iso=${view.iso} prefix=" · " />` : null}`;
}

/* ------------------------- start / stop controls ------------------------- */

/* Action feedback survives refreshes: a local note (in flight, or refused
 * before the server recorded anything) wins until the server's own record
 * for the mailbox is newer. */
function actionNote(loop, local) {
  const server = loop.last_action;
  if (local && (!server || !server.updated_at ||
      Date.parse(server.updated_at) < local.at || local.pending)) {
    return { text: local.text, tone: local.tone, iso: null };
  }
  if (!server) return null;
  const tone = {
    failed: "error", exited: "error", running: "ok", finished: "ok", stopped: "ok", stopping: "pending",
  }[server.outcome] || "ok";
  return { text: server.message || server.outcome, tone, iso: server.updated_at || null };
}

/* Capabilities come from the server (`controls`), which also enforces them. */
function controlView(loop, localNotes) {
  if (!loop) return null;
  const controls = loop.controls || {
    start: { enabled: false, reason: "Controls unavailable." },
    stop: { enabled: false, reason: "Controls unavailable." },
  };
  const local = localNotes.get(loop.key);
  const pending = Boolean(local && local.pending);
  const status = actionNote(loop, local);
  let text;
  let noteClass;
  let noteRole;
  let iso = null;
  if (status) {
    text = status.text;
    iso = status.iso;
    noteClass = "control-note control-" + status.tone;
    noteRole = status.tone === "error" ? "alert" : "status";
  } else {
    text = controls.start.enabled ? "" : controls.start.reason;
    if (!controls.start.enabled && controls.stop.enabled) text = controls.stop.reason;
    noteClass = "control-note caption";
    noteRole = "status";
  }
  return {
    startDisabled: pending || !controls.start.enabled,
    stopDisabled: pending || !controls.stop.enabled,
    startTitle: controls.start.reason || "",
    stopTitle: controls.stop.reason || controls.start.reason || "",
    text: text || "",
    iso,
    noteClass,
    noteRole,
  };
}

/* ------------------------- mount ------------------------- */

export function mountDrawer(store, { legacy = {}, doc = document } = {}) {
  const $ = (id) => doc.getElementById(id);
  const win = doc.defaultView || globalThis;
  const drawer = $("drawer");
  const scrim = $("drawer-scrim");
  const closeButton = $("drawer-close");
  const disposers = [];
  const cleanups = [];
  const run = (fn) => disposers.push(effect(fn));

  /* ---- derived signals: each keeps its identity while its data is equal ---- */
  const liveLoop = computed(() => {
    const key = store.openKey.value;
    if (key === null) return null;
    const sig = store.loopByKey(key);
    return sig ? sig.value : null;
  });
  const fromDetail = (pick) => stable(() => {
    const detail = store.detail.value;
    return detail ? pick(detail) : null;
  });
  const ctx = {
    store,
    head: stable(() => {
      const loop = liveLoop.value;
      return loop
        ? { name: loopTitle(loop), where: loop.workspace + " / " + loop.name, path: loop.root + "/" + loop.name }
        : null;
    }),
    badge: stable(() => {
      const detail = store.detail.value;
      if (!detail) return null;
      const live = liveLoop.value || {};
      return {
        status: detail.status,
        final_verdict: detail.final_verdict,
        segments: detail.segments,
        loop_state: detail.loop_state,
        running: live.running ?? detail.running,
        running_sources: live.running_sources || detail.running_sources,
      };
    }),
    mission: computed(() => {
      const detail = store.detail.value;
      return detail ? (detail.mission || "") : null;
    }),
    facts: fromDetail((d) => ({
      iter: (d.iteration != null ? d.iteration : "–") + " of " + (d.max_iterations != null ? d.max_iterations : "–"),
      verdict: d.final_verdict ? String(d.final_verdict).toUpperCase() : "—",
      verdictClass: d.final_verdict ? "verdict-" + normVerdict(d.final_verdict) : "",
      activity: d.last_activity || null,
      sessions: String(Array.isArray(d.sessions) ? d.sessions.length : 0),
    })),
    strip: fromDetail((d) => ({ segments: d.segments, final_verdict: d.final_verdict })),
    commits: stable(() => {
      const detail = store.detail.value;
      const list = detail && Array.isArray(detail.commits) ? detail.commits : [];
      return list.filter(Boolean).map((c) => ({ sha: c.sha, short: c.short, slice: c.slice, subject: c.subject }));
    }),
    slices: stable(() => {
      const detail = store.detail.value;
      const list = detail && Array.isArray(detail.slices) ? detail.slices : [];
      return list.filter(Boolean).map((s) => ({
        id: s.id, repo: s.repo, lifecycle: s.lifecycle, retired_sha: s.retired_sha,
        verdict: s.verdict, open_faults: s.open_faults, iteration: s.iteration,
      }));
    }),
    timeline: fromDetail((d) => ({
      entries: Array.isArray(d.timeline) ? d.timeline : [],
      mode: d.mode,
      iterations: Array.isArray(d.iterations) ? d.iterations.filter(Boolean).map((i) => ({ n: i.n, lifecycle: i.lifecycle })) : [],
      slices: Array.isArray(d.slices) ? d.slices.filter(Boolean).map((s) => ({ iteration: s.iteration, lifecycle: s.lifecycle })) : [],
    })),
    controls: stable(() => controlView(liveLoop.value, notes.value)),
  };

  /* ---- Preact roots over the static shell ---- */
  const roots = [
    ["drawer-badge", Badge], ["drawer-name", Title], ["drawer-where", Where],
    ["drawer-mission", Mission], ["fact-iter", FactIter], ["fact-verdict", FactVerdict],
    ["fact-activity", FactActivity], ["fact-sessions", FactSessions], ["drawer-strip", Strip],
    ["commit-list", CommitList], ["slice-list", SliceList], ["drawer-timeline", Timeline],
    ["drawer-tabs", Tabs], ["loop-control-note", ControlNote],
  ];
  for (const [id, Component] of roots) {
    const node = $(id);
    if (node) cleanups.push(mount(node, html`<${Component} ctx=${ctx} />`));
  }

  /* ---- attributes of the static containers (guarded: written on change only) ---- */
  run(() => {
    const head = ctx.head.value;
    const where = $("drawer-where");
    if (where) setProp(where, "title", head ? head.path : "");
  });
  run(() => {
    const mission = ctx.mission.value;
    const node = $("drawer-mission");
    if (node) setProp(node, "title", mission || "");
  });
  run(() => {
    const facts = ctx.facts.value;
    const node = $("fact-verdict");
    if (node) setProp(node, "className", facts ? facts.verdictClass : "");
  });
  run(() => {
    setHidden($("commits-section"), ctx.commits.value.length === 0);
    setHidden($("slices-section"), ctx.slices.value.length === 0);
  });
  run(() => {
    const current = store.drawerTab.value;
    for (const tab of DRAWER_TABS) setHidden($("view-" + tab), current !== tab);
  });

  /* ---- start / stop ---- */
  run(() => {
    const view = ctx.controls.value;
    const wrap = $("drawer-controls");
    setHidden(wrap, !view);
    if (!view) return;
    const start = $("loop-start");
    const stop = $("loop-stop");
    const note = $("loop-control-note");
    setProp(start, "disabled", view.startDisabled);
    setProp(stop, "disabled", view.stopDisabled);
    setProp(start, "title", view.startTitle);
    setProp(stop, "title", view.stopTitle);
    setProp(note, "className", view.noteClass);
    if (note.getAttribute("role") !== view.noteRole) note.setAttribute("role", view.noteRole);
  });

  async function controlLoop(action) {
    const key = store.openKey.peek();
    const sig = key === null ? undefined : store.loopByKey(key);
    const loop = sig && sig.peek();
    if (!loop) return;
    const controls = loop.controls || {};
    const driver = controls.driver || "portable";
    if (!(controls[action] && controls[action].enabled)) return;
    if (action === "stop" && !win.confirm("Stop " + loopTitle(loop) + "? " + controls.stop.reason)) return;
    setNote(loop.key, {
      text: action === "start" ? "Starting the " + driver + " driver…" : "Stopping…",
      tone: "pending", pending: true, at: Date.now(),
    });
    const result = await store.controlLoop(loop, action);
    if (result.ok) {
      setNote(loop.key, {
        text: action === "start" ? "Started (PID " + result.data.pid + ")." : "Stop requested for PID " + result.data.pid + ".",
        tone: "ok", at: Date.now(),
      });
    } else {
      setNote(loop.key, { text: "Loop " + action + " failed: " + result.error, tone: "error", at: Date.now() });
    }
  }

  /* ---- frame: hidden, inert, focus, close paths, focus trap ---- */
  function setBackgroundInert(on) {
    for (const node of doc.querySelectorAll(".appbar, #main, .skip-link")) setProp(node, "inert", on);
  }

  let wasOpen = false;
  let lastKey = null;
  let returnFocus = null;
  run(() => {
    const key = store.openKey.value;
    untracked(() => {
      const open = key !== null;
      setHidden(drawer, !open);
      setHidden(scrim, !open);
      setBackgroundInert(open);
      if (open) {
        if (!wasOpen) returnFocus = doc.activeElement;
        if (!wasOpen || key !== lastKey) closeButton.focus();
      } else if (wasOpen) {
        const back = returnFocus;
        returnFocus = null;
        if (back && doc.contains(back) && typeof back.focus === "function") back.focus();
      }
      wasOpen = open;
      lastKey = key;
    });
  });

  const close = () => store.closeLoop();
  const onKeydown = (ev) => {
    const dialog = $("confirm-dialog");
    if (dialog && dialog.open) return; // the dialog handles its own keys
    if (ev.key === "Escape" && !drawer.hidden) store.closeLoop();
    if (ev.key !== "Tab" || drawer.hidden) return;
    const focusable = Array.from(drawer.querySelectorAll(FOCUSABLE))
      .filter((node) => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (ev.shiftKey && doc.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && doc.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  };
  const onStart = () => controlLoop("start");
  const onStop = () => controlLoop("stop");
  const listen = (node, type, fn) => {
    if (!node) return;
    node.addEventListener(type, fn);
    cleanups.push(() => node.removeEventListener(type, fn));
  };
  listen(closeButton, "click", close);
  listen(scrim, "click", close);
  listen(doc, "keydown", onKeydown);
  listen($("loop-start"), "click", onStart);
  listen($("loop-stop"), "click", onStop);

  /* ---- legacy hooks (D4-3): each fires once per change of its input ---- */
  const currentLoop = (key) => {
    const sig = key === null ? undefined : store.loopByKey(key);
    return sig ? sig.peek() : undefined;
  };
  let hookedKey = null;
  run(() => {
    const key = store.openKey.value;
    if (key === hookedKey) return;
    if (key !== null && !store.loopByKey(key)) return; // retried when loops arrive
    untracked(() => {
      hookedKey = key;
      if (key === null) {
        if (legacy.onClose) legacy.onClose();
      } else if (legacy.onOpen) {
        legacy.onOpen(currentLoop(key));
      }
    });
  });
  let hookedDetail = null;
  run(() => {
    const detail = store.detail.value;
    untracked(() => {
      if (detail === null) {
        hookedDetail = null;
        return;
      }
      if (detail === hookedDetail) return;
      hookedDetail = detail;
      if (legacy.onDetail) legacy.onDetail(detail, currentLoop(store.openKey.peek()));
    });
  });
  let hookedTab = store.drawerTab.peek();
  run(() => {
    const tab = store.drawerTab.value;
    untracked(() => {
      if (tab === hookedTab) return;
      hookedTab = tab;
      const key = store.openKey.peek();
      if (key !== null && legacy.onTab) legacy.onTab(tab, currentLoop(key));
    });
  });

  return function unmount() {
    for (const dispose of disposers.splice(0)) dispose();
    for (const cleanup of cleanups.splice(0)) cleanup();
  };
}
