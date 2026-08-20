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
            nodes[n["id"]] = n
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
    latest = latest_verdicts(Path(require(args, "--state")))
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
    if len(sys.argv) < 2 or sys.argv[1] not in ("plan", "record", "status"):
        die("usage: run.py plan|record|status ...")
    cmd, args = sys.argv[1], sys.argv[2:]
    {"plan": cmd_plan, "record": cmd_record, "status": cmd_status}[cmd](args)


if __name__ == "__main__":
    main()
