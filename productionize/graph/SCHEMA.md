# Productionize graph — node schema v1

The graph is the executable form of the glossary. One JSON file per domain:
`productionize/graph/<domain>.json` → `{ "version": 1, "nodes": [ <node>, ... ] }`.

## Design decisions

- **Node = one glossary entry.** Fine-grained probes (liveness vs readiness vs startup) stay separate nodes.
- **Cross-domain duplicates are NOT merged away.** Nodes carry `cluster: <canonical-id>`; the driver
  collapses a cluster to one verdict per run (first failing node in the cluster fails the cluster).
  Supersedes the "merge into one node" sketch in `../CHECKLIST.md` — same canonical IDs.
- **An id MAY appear in more than one domain file** when it is the same concern seen from
  two domains (e.g. `backup-restore-drill` in data and reliability). Every instance MUST carry
  the same `cluster` value; the driver collapses duplicates at plan time (first domain file wins).
- **`depends_on` may reference any glossary id**, including ids not yet promoted to nodes
  (validated against the glossary heading universe). Cycles among *present* nodes are an error.

## Node fields

| field | type | required | rule |
|---|---|---|---|
| `id` | string | yes | kebab-case, globally unique, MUST exist as a `### <id>` heading in some `glossary/*.md` |
| `domain` | string | yes | one of the 11 domain slugs; matches the file name |
| `title` | string | yes | human one-liner |
| `summary` | string | yes | 1-2 sentences (what + why) |
| `check` | enum | yes | `probe` \| `judgment` \| `user-decision` |
| `probe` | string | yes | check=probe → executable recipe; judgment → evidence to inspect; user-decision → question + options |
| `applies_if` | string[] | yes | subset of: `all web-api spa cli library mobile ml-service data-pipeline monorepo` |
| `severity` | enum | yes | `critical` \| `important` \| `nice-to-have` |
| `depends_on` | string[] | no | ids that must pass/be present first; may reference non-promoted glossary ids |
| `cluster` | string | no | canonical dedupe id (see CHECKLIST.md), e.g. `health-check-contracts` |
| `min_tier` | enum | iff judgment | `light` \| `standard` \| `frontier` — minimum assessor model tier |
| `glossary_ref` | string | yes | `glossary/<domain>.md#<id>` |
| `sources` | string[] | yes | canonical URLs |

## Validation

`python3 productionize/driver/validate_graph.py` checks: required fields, enums, id uniqueness,
glossary-id existence, probe/tier conditional requirements, cluster canonical-id membership,
unresolved `depends_on` (error unless target is a known glossary id), and cycles among present nodes.
Exit 0 = valid.
