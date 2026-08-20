#!/usr/bin/env python3
"""Productionize driver — mechanical state machine for a productionize run.

Stdlib only. The driver never judges code: it prunes the node graph by
project profile, topologically orders the work, assigns each node an
executor (scout / assessor tier / user), and records verdicts. Agents do
the checking; the driver owns plan and state.

Subcommands:
  plan    --profile p.json [--graph DIR] [--out plan.json]
  record  --state s.jsonl --node ID --verdict V [--evidence TEXT]
  status  --state s.jsonl --plan plan.json

Profile JSON: {"name": str, "tags": ["web-api", ...]} — a node applies when
"all" is in its applies_if or any tag intersects.

State JSONL (append-only): {"ts", "node", "verdict", "evidence"} with
verdict in pass|fail|na|blocked. Later records supersede earlier ones.
"""
import json, sys, time
from pathlib import Path

GRAPH_DIR = Path(__file__).resolve().parent.parent / "graph"
EXECUTORS = {"probe": "scout", "user-decision": "user"}
VERDICTS = {"pass", "fail", "na", "blocked"}


def load_nodes(graph_dir):
    nodes = {}
    for f in sorted(Path(graph_dir).glob("*.json")):
        if f.stem == "SCHEMA":
            continue
        doc = json.loads(f.read_text())
        for n in doc.get("nodes", []):
            # Cross-domain duplicates (same id + cluster) collapse: first
            # domain file wins (sorted order), they describe one concern.
            nodes.setdefault(n["id"], n)
    return nodes


def topo_order(nodes):
    """Kahn over kept nodes; edges to pruned/absent targets are dropped."""
    indeg = {nid: 0 for nid in nodes}
    adj = {nid: [] for nid in nodes}
    for nid, n in nodes.items():
        for dep in n.get("depends_on", []) or []:
            if dep in nodes:
                adj[dep].append(nid)
                indeg[nid] += 1
    queue = sorted(nid for nid, d in indeg.items() if d == 0)
    order = []
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v in sorted(adj[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
        queue.sort()
    if len(order) != len(nodes):
        stuck = sorted(set(nodes) - set(order))
        die(f"dependency cycle among: {', '.join(stuck)}")
    return order


def cmd_plan(args):
    profile_path = require(args, "--profile")
    profile = json.loads(Path(profile_path).read_text())
    tags = set(profile.get("tags", []))
    graph_dir = Path(opt(args, "--graph", str(GRAPH_DIR)))
    nodes = load_nodes(graph_dir)
    if not nodes:
        die(f"no nodes found under {graph_dir}")
    kept = {nid: n for nid, n in nodes.items()
            if "all" in n.get("applies_if", []) or tags & set(n.get("applies_if", []))}
    order = topo_order(kept)
    plan_nodes = []
    for nid in order:
        n = kept[nid]
        entry = {
            "id": nid,
            "domain": n["domain"],
            "check": n["check"],
            "severity": n["severity"],
            "executor": EXECUTORS.get(n["check"], "assessor:" + n.get("min_tier", "standard")),
            "probe": n["probe"],
            "glossary_ref": n["glossary_ref"],
        }
        if n.get("cluster"):
            entry["cluster"] = n["cluster"]
        deps = [d for d in n.get("depends_on", []) or [] if d in kept]
        if deps:
            entry["depends_on"] = deps
        plan_nodes.append(entry)
    clusters = {}
    for e in plan_nodes:
        if "cluster" in e:
            clusters.setdefault(e["cluster"], []).append(e["id"])
    plan = {
        "profile": profile,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node_count": len(plan_nodes),
        "clusters": clusters,
        "nodes": plan_nodes,
    }
    out = opt(args, "--out")
    text = json.dumps(plan, indent=2) + "\n"
    if out:
        Path(out).write_text(text)
        print(f"wrote {out}: {len(plan_nodes)} nodes "
              f"({len(nodes) - len(plan_nodes)} pruned), {len(clusters)} clusters")
    else:
        sys.stdout.write(text)


def cmd_record(args):
    state_path = Path(require(args, "--state"))
    node = require(args, "--node")
    verdict = require(args, "--verdict")
    if verdict not in VERDICTS:
        die(f"verdict must be one of {sorted(VERDICTS)}")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "node": node, "verdict": verdict,
           "evidence": opt(args, "--evidence", "")}
    with state_path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"recorded {node}: {verdict}")


def latest_verdicts(state_path):
    latest = {}
    if state_path.exists():
        for line in state_path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["node"]] = rec
    return latest


def cmd_status(args):
    plan = json.loads(Path(require(args, "--plan")).read_text())
    state_path = Path(require(args, "--state"))
    latest = latest_verdicts(state_path)
    counts = {v: 0 for v in sorted(VERDICTS)}
    counts["pending"] = 0
    failed, blocked = [], []
    by_domain = {}
    for e in plan["nodes"]:
        rec = latest.get(e["id"])
        v = rec["verdict"] if rec else "pending"
        counts[v] = counts.get(v, 0) + 1
        d = by_domain.setdefault(e["domain"], {k: 0 for k in list(VERDICTS) + ["pending"]})
        d[v] += 1
        if v == "fail":
            failed.append((e["severity"], e["id"], (rec or {}).get("evidence", "")))
        elif v == "blocked":
            blocked.append((e["id"], (rec or {}).get("evidence", "")))
    total = plan["node_count"]
    done = total - counts["pending"]
    print(f"progress: {done}/{total} decided "
          f"(pass {counts['pass']}, fail {counts['fail']}, "
          f"na {counts['na']}, blocked {counts['blocked']}, pending {counts['pending']})")
    for domain in sorted(by_domain):
        d = by_domain[domain]
        print(f"  {domain}: pass {d['pass']} fail {d['fail']} na {d['na']} "
              f"blocked {d['blocked']} pending {d['pending']}")
    if failed:
        print("\nfailures (critical first):")
        rank = {"critical": 0, "important": 1, "nice-to-have": 2}
        for sev, nid, ev in sorted(failed, key=lambda x: rank.get(x[0], 3)):
            print(f"  [{sev}] {nid}" + (f" — {ev[:120]}" if ev else ""))
    if blocked:
        print("\nblocked:")
        for nid, ev in blocked:
            print(f"  {nid}" + (f" — {ev[:120]}" if ev else ""))
    triaged = load_triage(state_path)
    untriaged = [nid for _sev, nid, _ev in failed if nid not in triaged]
    if untriaged:
        print(f"\nuntriaged failures: {len(untriaged)} — "
              f"run triage before seeding the fix loop")



def _chunk(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def cmd_batches(args):
    """Write per-domain batch briefing files from a plan.

    Probes land in <out>/, judgment nodes in a sibling jbatches/ directory
    (same convention both dogfood runs converged on). Each file carries the
    node id, severity, executor, and the probe/assessment recipe verbatim —
    agents NEVER read plan.json (context poisoning on large plans).
    """
    plan = json.loads(Path(require(args, "--plan")).read_text())
    out = Path(require(args, "--out"))
    chunk = int(opt(args, "--chunk", "7"))
    out.mkdir(parents=True, exist_ok=True)
    jdir = out.parent / "jbatches"  # dogfood convention
    jdir.mkdir(parents=True, exist_ok=True)

    probes, judgments = {}, {}
    for n in plan["nodes"]:
        if n["check"] == "user-decision":
            continue  # asked to the user directly, never batched to agents
        sink = probes if n["check"] == "probe" else judgments
        sink.setdefault(n["domain"], []).append(n)

    index = []
    for base_dir, grouped, is_judge in ((out, probes, False), (jdir, judgments, True)):
        for domain in sorted(grouped):
            for i, chunk_nodes in enumerate(_chunk(grouped[domain], chunk), 1):
                fname = base_dir / f"{domain}-{i}.md"
                with fname.open("w") as fh:
                    for n in chunk_nodes:
                        label = "Assessment" if is_judge else "Probe"
                        fh.write(f"## {n['id']} [{n['severity']}] executor={n['executor']}\n")
                        fh.write(f"{label}: {n['probe']}\n\n")
                index.append({"file": str(fname), "check": "judgment" if is_judge else "probe",
                              "ids": [n["id"] for n in chunk_nodes]})
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    print(f"wrote {len(index)} batch files: "
          f"{sum(len(b['ids']) for b in index if b['check'] == 'probe')} probe nodes, "
          f"{sum(len(b['ids']) for b in index if b['check'] == 'judgment')} judgment nodes")


def cmd_record_batch(args):
    """Bulk-record verdicts from a JSON array on stdin or --file.

    Dogfood-proven: agents return one array per batch; recording it in one
    call is atomic, validates every id against the plan (catches typos and
    ids the agent invented), and reports skipped unknowns instead of
    silently accepting them.
    """
    state_path = Path(require(args, "--state"))
    plan = json.loads(Path(require(args, "--plan")).read_text())
    valid_ids = {n["id"] for n in plan["nodes"]}
    src = opt(args, "--file")
    text = Path(src).read_text() if src else sys.stdin.read()
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        # agents sometimes wrap the array in prose; extract the first [...] block
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            die("no JSON array found in input")
        rows = json.loads(text[start:end + 1])
    if not isinstance(rows, list):
        die("expected a JSON array of {node, verdict, evidence}")
    recorded, skipped = 0, []
    with state_path.open("a") as fh:
        for row in rows:
            node, verdict = row.get("node"), row.get("verdict")
            if node not in valid_ids:
                skipped.append(node)
                continue
            if verdict not in VERDICTS:
                skipped.append(f"{node}(bad verdict {verdict!r})")
                continue
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "node": node, "verdict": verdict,
                   "evidence": row.get("evidence", "")}
            fh.write(json.dumps(rec) + "\n")
            recorded += 1
    print(f"recorded {recorded}, skipped {len(skipped)}")
    for s in skipped:
        print(f"  skipped: {s}")


def cmd_report(args):
    """Generate REPORT.md from plan + verdicts (mechanical close-out)."""
    plan = json.loads(Path(require(args, "--plan")).read_text())
    latest = latest_verdicts(Path(require(args, "--state")))
    nodes = {n["id"]: n for n in plan["nodes"]}
    lines = ["# Production-Readiness Audit — Report", ""]
    profile = plan.get("profile", {})
    lines.append(f"Profile: `{profile.get('name', '?')}` "
                 f"({', '.join(profile.get('tags', []))}). "
                 f"{plan['node_count']} nodes planned; {len(latest)} decided.")
    counts = {v: 0 for v in sorted(VERDICTS)}
    for rec in latest.values():
        counts[rec["verdict"]] = counts.get(rec["verdict"], 0) + 1
    sev_fails = {}
    for nid, rec in latest.items():
        if rec["verdict"] == "fail":
            sev = nodes.get(nid, {}).get("severity", "?")
            sev_fails[sev] = sev_fails.get(sev, 0) + 1
    lines += ["", "## Verdict distribution",
              f"- pass: {counts['pass']}  |  fail: {counts['fail']} "
              f"({', '.join(f'{v} {k}' for k, v in sorted(sev_fails.items()))})  |  "
              f"na: {counts['na']}  |  blocked: {counts['blocked']}"]

    def section(title, pred):
        lines.extend(["", f"## {title}"])
        for nid, rec in latest.items():
            if pred(nid, rec):
                n = nodes.get(nid, {})
                lines.append(f"- **{nid}** [{n.get('severity', '?')}] "
                             f"({n.get('domain', '?')}): {rec.get('evidence', '')}")

    section("Passes", lambda _i, r: r["verdict"] == "pass")
    section("Blocked (need a live environment to conclude)",
            lambda _i, r: r["verdict"] == "blocked")
    lines += ["", "## Critical failures by domain"]
    by_dom = {}
    for nid, rec in latest.items():
        n = nodes.get(nid, {})
        if rec["verdict"] == "fail" and n.get("severity") == "critical":
            by_dom.setdefault(n.get("domain", "?"), []).append((nid, rec))
    for dom in sorted(by_dom):
        lines.append(f"### {dom} ({len(by_dom[dom])} critical fails)")
        for nid, rec in by_dom[dom]:
            lines.append(f"- **{nid}**: {rec.get('evidence', '')}")
    open_decisions = [n for n in plan["nodes"]
                      if n["check"] == "user-decision" and n["id"] not in latest]
    if open_decisions:
        lines += ["", f"## Open user decisions ({len(open_decisions)})",
                  "Policy choices only the user can make; each links to its "
                  "glossary entry for option trade-offs.", ""]
        for n in open_decisions:
            q = n["probe"].replace("\n", " ")
            lines.append(f"- [{n['severity']}] **{n['id']}** ({n['domain']}): {q[:200]}")
    out = opt(args, "--out")
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text)
        print(f"wrote {out}: {len(lines)} lines")
    else:
        sys.stdout.write(text)


TRIAGE_ACTIONS = {"fix", "defer", "dispute", "accept-risk"}


def _triage_path(state_path):
    return state_path.with_name("triage.jsonl")


def cmd_triage(args):
    """Record user triage decisions over failed nodes.

    Triage is separate from verdicts: the verdict is what the audit found,
    the triage decision is what the user wants done about it. Bulk input
    matches record-batch (JSON array on stdin or --file); single decisions
    use --node/--action/--note. `dispute` also re-records the verdict as
    `na` with the user's rationale — latest-wins keeps the audit honest.
    """
    state_path = Path(require(args, "--state"))
    plan = json.loads(Path(require(args, "--plan")).read_text())
    valid = {n["id"]: n for n in plan["nodes"]}
    latest = latest_verdicts(state_path)
    src_file = opt(args, "--file")
    if opt(args, "--node"):
        rows = None  # single-decision mode; --node wins over stdin
    elif src_file or not sys.stdin.isatty():
        text = Path(src_file).read_text() if src_file else sys.stdin.read()
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end <= start:
                die("no JSON array found in triage input")
            rows = json.loads(text[start:end + 1])
    if rows is None:
        rows = [{"node": require(args, "--node"),
                 "action": require(args, "--action"),
                 "note": opt(args, "--note", "")}]
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    recorded, skipped = 0, []
    with _triage_path(state_path).open("a") as fh, state_path.open("a") as vs:
        for row in rows:
            node, action = row.get("node"), row.get("action")
            note = row.get("note", "")
            if node not in valid:
                skipped.append(node)
                continue
            if action not in TRIAGE_ACTIONS:
                skipped.append(f"{node}(bad action {action!r})")
                continue
            if latest.get(node, {}).get("verdict") != "fail":
                skipped.append(f"{node}(not a fail; triage applies to failures)")
                continue
            fh.write(json.dumps({"ts": ts, "node": node, "action": action,
                                 "note": note}) + "\n")
            if action == "dispute":
                vs.write(json.dumps({"ts": ts, "node": node, "verdict": "na",
                                     "evidence": f"user disputed: {note}"}) + "\n")
            recorded += 1
    print(f"triaged {recorded}, skipped {len(skipped)}")
    for s in skipped:
        print(f"  skipped: {s}")


def load_triage(state_path):
    """Latest triage action per node."""
    path = _triage_path(state_path)
    latest = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["node"]] = rec
    return latest


def opt(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def require(args, flag):
    v = opt(args, flag)
    if v is None:
        die(f"missing {flag}")
    return v


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    cmds = {"plan": cmd_plan, "record": cmd_record, "record-batch": cmd_record_batch,
            "batches": cmd_batches, "status": cmd_status, "report": cmd_report,
            "triage": cmd_triage}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        die(f"usage: run.py {"|".join(cmds)} ...")
    cmd, args = sys.argv[1], sys.argv[2:]
    cmds[cmd](args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)  # downstream closed the pipe (e.g. `| head`) — not an error
