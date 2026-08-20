#!/usr/bin/env python3
"""Validate productionize graph node files against schema v1. Stdlib only."""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # productionize/
GRAPH = ROOT / "graph"
GLOSSARY = ROOT / "glossary"

DOMAINS = {"code-quality", "testing", "reliability", "scalability", "security",
           "observability", "data", "deployment", "llmops", "ops-economics", "docs-dx"}
CHECKS = {"probe", "judgment", "user-decision"}
SEVERITIES = {"critical", "important", "nice-to-have"}
APPLIES = {"all", "web-api", "spa", "cli", "library", "mobile", "ml-service",
           "data-pipeline", "monorepo"}
TIERS = {"light", "standard", "frontier"}
CLUSTERS = {"idempotency-keys", "health-check-contracts", "secrets-management",
            "sbom-provenance", "telemetry-pii-redaction", "retry-backoff-breakers",
            "provider-fallback-chain", "quota-policy", "abuse-friction", "slo-framework",
            "db-migrations", "backup-dr", "nonprod-data-isolation",
            "release-telemetry-attribution", "preview-environments", "graceful-lifecycle"}
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

errors, warnings = [], []


def err(msg): errors.append(msg)
def warn(msg): warnings.append(msg)


def glossary_ids():
    ids = set()
    for f in GLOSSARY.glob("*.md"):
        for line in f.read_text().splitlines():
            if line.startswith("### "):
                ids.add(line[4:].strip())
    return ids


def main():
    known = glossary_ids()
    if not known:
        err("no glossary files found under " + str(GLOSSARY))
    files = sorted(p for p in GRAPH.glob("*.json"))
    if not files:
        err("no graph node files found under " + str(GRAPH))
        return report()

    all_nodes = {}
    for f in files:
        domain = f.stem
        if domain not in DOMAINS:
            err(f"{f.name}: unknown domain")
        try:
            doc = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            err(f"{f.name}: invalid JSON: {e}")
            continue
        if doc.get("version") != 1:
            err(f"{f.name}: version must be 1")
        nodes = doc.get("nodes")
        if not isinstance(nodes, list):
            err(f"{f.name}: 'nodes' must be a list")
            continue
        for i, n in enumerate(nodes):
            loc = f"{f.name}#{n.get('id', f'<index {i}>') if isinstance(n, dict) else f'<index {i}>'}"
            if not isinstance(n, dict):
                err(f"{f.name}: node {i} is not an object")
                continue
            nid = n.get("id")
            if not nid or not isinstance(nid, str) or not KEBAB.match(nid):
                err(f"{loc}: id missing or not kebab-case")
            elif nid in all_nodes:
                # Cross-domain duplicate: legal only when every instance carries
                # the same non-null cluster (same concern, deduped at runtime).
                prev = all_nodes[nid]
                if not n.get("cluster") or n.get("cluster") != prev[1]:
                    err(f"{loc}: duplicate id (also in {prev[0]}); duplicates "
                        f"must share one cluster on every instance")
            else:
                all_nodes[nid] = (f.name, n.get("cluster"))
                if nid not in known:
                    err(f"{loc}: id not found as a glossary heading")
            if n.get("domain") != domain:
                err(f"{loc}: domain must be '{domain}'")
            for req in ("title", "summary", "probe", "glossary_ref"):
                if not n.get(req):
                    err(f"{loc}: missing '{req}'")
            if n.get("check") not in CHECKS:
                err(f"{loc}: bad check {n.get('check')!r}")
            if n.get("check") == "judgment" and n.get("min_tier") not in TIERS:
                err(f"{loc}: judgment node needs min_tier in {sorted(TIERS)}")
            if n.get("severity") not in SEVERITIES:
                err(f"{loc}: bad severity {n.get('severity')!r}")
            ai = n.get("applies_if")
            if not isinstance(ai, list) or not ai or not set(ai) <= APPLIES:
                err(f"{loc}: applies_if must be a non-empty subset of {sorted(APPLIES)}")
            if "cluster" in n and n["cluster"] not in CLUSTERS:
                err(f"{loc}: unknown cluster {n['cluster']!r}")
            if not isinstance(n.get("sources"), list) or not n.get("sources"):
                err(f"{loc}: sources must be a non-empty list")
            gr = n.get("glossary_ref", "")
            if isinstance(gr, str) and gr and not re.match(r"^glossary/[a-z-]+\.md#[a-z0-9-]+$", gr):
                err(f"{loc}: bad glossary_ref {gr!r}")

    # depends_on resolution + cycle detection
    for f in files:
        for n in json.loads(f.read_text()).get("nodes", []):
            if not isinstance(n, dict):
                continue
            for dep in n.get("depends_on", []) or []:
                if dep not in all_nodes and dep not in known:
                    err(f"{f.name}#{n.get('id')}: depends_on '{dep}' is neither a node nor a glossary id")
                elif dep not in all_nodes:
                    warn(f"{n.get('id')}: depends_on '{dep}' not yet promoted to a node")

    edges = {nid: [d for d in (node.get("depends_on") or []) if d in all_nodes]
             for f in files for node in json.loads(f.read_text()).get("nodes", [])
             if isinstance(node, dict) for nid in [node.get("id")] if nid}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in edges}

    def visit(u, stack):
        color[u] = GRAY
        for v in edges.get(u, []):
            if color.get(v) == GRAY:
                err(f"dependency cycle: {' -> '.join(stack + [u, v])}")
            elif color.get(v) == WHITE:
                visit(v, stack + [u])
        color[u] = BLACK

    for nid in edges:
        if color[nid] == WHITE:
            visit(nid, [])

    return report(len(all_nodes), len(files))


def report(node_count=0, file_count=0):
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"---\n{node_count} nodes across {file_count} file(s); "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
