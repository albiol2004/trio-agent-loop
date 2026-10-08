/* Pure formatting helpers of the dashboard frontend (no DOM, no globals).
 * Ports of the helpers in app.js with the clock passed in, so the same input
 * gives the same string as app.js. */

export function oneLine(text, max = 160) {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  if (s.length > max) return s.slice(0, max - 1).trimEnd() + "…";
  return s;
}

export function plural(n, word, many) {
  return n + " " + (n === 1 ? word : many || word + "s");
}

export function fmtClock(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/* "just now" / "42s ago" / "3m ago" / "5h ago" / "2d ago" / "Jun 3"; `nowMs`
 * is the clock in epoch milliseconds. */
export function relTime(iso, nowMs) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const secs = Math.max(0, Math.round((nowMs - t) / 1000));
  if (secs < 5) return "just now";
  if (secs < 60) return secs + "s ago";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.floor(hrs / 24);
  if (days < 7) return days + "d ago";
  const d = new Date(t);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date(nowMs).getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

export function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(1) + "KB";
  return (n / 1048576).toFixed(1) + "MB";
}

export function fmtDuration(sec) {
  if (sec == null) return "—";
  sec = Number(sec);
  if (!Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return Math.round(sec) + "s";
  const mins = Math.floor(sec / 60);
  if (mins < 60) return mins + "m";
  const hrs = Math.floor(mins / 60);
  return hrs + "h " + (mins % 60) + "m";
}

export function normVerdict(raw) {
  if (!raw) return "none";
  const v = String(raw).trim().toUpperCase();
  if (v === "SHIP") return "ship";
  if (v === "ITERATE") return "iterate";
  if (v === "BLOCKED") return "blocked";
  if (v === "NEEDS_HUMAN") return "needs_human";
  return "none";
}

/* Verdict sequence from segments, e.g. ["iterate", "ship"]. */
export function verdictSeq(loop) {
  const seq = (loop.segments || [])
    .map((s) => s.verdict_sequence || "")
    .join("");
  const out = [];
  for (const ch of seq) {
    if (ch === "S") out.push("ship");
    else if (ch === "I") out.push("iterate");
    else if (ch === "B") out.push("blocked");
    else if (ch === "H") out.push("needs_human");
  }
  return out;
}

export function isArchived(loop) {
  return String(loop.name || "").split("/")[0].startsWith("loop-archive");
}

/* Keep the STATE.md word factual; "unknown" is the API's missing-value marker. */
export function statusWord(loop) {
  const raw = loop.status == null ? "" : String(loop.status);
  const value = raw.trim();
  return !value || value === "unknown" ? "—" : raw;
}

/* Prefer the persisted verdict, then the last parsed segment verdict. */
export function latestVerdict(loop) {
  const final = String(loop.final_verdict || "").trim();
  if (final) return final.toUpperCase();
  const seq = verdictSeq(loop);
  return seq.length ? String(seq[seq.length - 1]).toUpperCase() : null;
}

/* Derived loop states (server: loop_actions.derive_state) shown as their
 * own badge instead of the raw STATE.md word. */
export const LOOP_STATE_BADGES = {
  error: ["negative", "Error"],
  needs_retirement: ["warning", "Needs retirement"],
  needs_land: ["warning", "Needs land"],
  held: ["warning", "Held"],
  conflict: ["negative", "Conflict"],
  budget: ["warning", "Budget spent"],
  iteration_cap: ["warning", "Iteration cap"],
  answered: ["warning", "Answered — restart"],
  finished: ["positive", "Finished"],
  between_roles: ["neutral", "Between roles"],
  paused: ["neutral", "Paused"],
};

export function derivedStateOf(loop) {
  return loop && loop.loop_state ? loop.loop_state.state : null;
}

const HUMAN_STATUS = new Set(["needs_human", "awaiting_human", "awaiting_user"]);
const BADGE_ICONS = { live: "●", positive: "✓", warning: "!", negative: "✕", neutral: "○" };

/* GOAL.md heading, else the mission's first clause, else the mailbox name. */
export function loopTitle(loop) {
  if (loop.title) return loop.title;
  const mission = String(loop.mission || "").split(/(?<=[.;:])\s/)[0].trim();
  return mission ? oneLine(mission, 90) : loop.name;
}

/* One state word per loop, from facts only: live evidence first, then a
 * STATE.md hand-off to a person (or blocked), then the latest verdict, then
 * the STATE.md status word. Returns the data app.js's stateBadge renders:
 * {tone, icon, text, className, title}. */
export function stateBadgeInfo(loop) {
  let tone = "neutral";
  let text = statusWord(loop);
  const verdict = normVerdict(latestVerdict(loop));
  const status = String(loop.status || "").trim().toLowerCase().replace(/-/g, "_");
  const derived = derivedStateOf(loop);
  if (loop.running) {
    tone = "live";
    text = "Running";
  } else if (LOOP_STATE_BADGES[derived]) {
    [tone, text] = LOOP_STATE_BADGES[derived];
  } else if (derived === "unmapped") {
    // A STATE.md word outside the status map is shown verbatim.
    tone = "neutral";
    text = String(loop.loop_state.status_raw || statusWord(loop));
  } else if (HUMAN_STATUS.has(status)) {
    // STATE.md handing the loop to a person outranks an older verdict file.
    tone = "warning";
    text = "Needs human";
  } else if (status === "blocked") {
    tone = "negative";
    text = "Blocked";
  } else if (verdict === "ship") {
    tone = "positive";
    text = "Shipped";
  } else if (verdict === "needs_human") {
    tone = "warning";
    text = "Needs human";
  } else if (verdict === "blocked") {
    tone = "negative";
    text = "Blocked";
  } else if (verdict === "iterate") {
    text = "Iterating";
  }
  const facts = ["STATE.md: " + statusWord(loop)];
  if (latestVerdict(loop)) facts.push("verdict: " + latestVerdict(loop));
  if (loop.running) facts.push("live via " + (loop.running_sources || []).join(", "));
  if (loop.loop_state && loop.loop_state.summary) facts.push(derived + ": " + loop.loop_state.summary);
  return {
    tone,
    icon: BADGE_ICONS[tone],
    text,
    className: "badge badge-" + tone,
    title: facts.join(" · "),
  };
}

export function iterationText(loop) {
  if (loop.iteration == null) return "—";
  return loop.max_iterations != null
    ? loop.iteration + " of " + loop.max_iterations
    : String(loop.iteration);
}

/* The driver phase is only current while the loop is live; a stale
 * sidecar's phase ("lead done", "blocked") would contradict the badge. */
export function phaseText(loop) {
  if (!loop.running) return "";
  const phase = String(loop.driver_phase || "").trim();
  const sub = loop.running_substate;
  const parts = [];
  if (phase && phase.toLowerCase() !== "idle") parts.push(phase.replace(/[-_]/g, " "));
  if (sub) parts.push(sub === "both" ? "lead and evaluator active" : sub + " active");
  return parts.join(" · ");
}
