# trio-bridge — proposal card contract

`trio-bridge` is a cross-harness **suggestion skill**: a read-only background scanner that dispatches scout subagents to find candidate work in a project, deduplicates their findings, and writes **proposal cards** into a `proposals/` directory at the project root. It never edits project code, never starts loops, and is safe to re-run (idempotent).

## Layout

| Path | Purpose |
| --- | --- |
| `skills/trio-bridge/SKILL.md` | Skill source for Claude and Codex harnesses (`~/.claude/skills/trio-bridge/`, `~/.agents/skills/trio-bridge/`). |
| `commands/trio-bridge.md` | Omp native command source (`~/.omp/agent/commands/trio-bridge.md`) — same procedure, command-style `description`-only frontmatter. |
| `README.md` | This contract document. |

## Proposal card format

Every proposal is a markdown file `proposals/<kebab-id>.md` with YAML frontmatter and a two-section body:

```markdown
---
id: <kebab-id>
fingerprint: <sha256 hex>
origin: <path>
class: A|B|C
evidence:
  - <file:line or command>
confidence: high|medium|low
created: <YYYY-MM-DD>
---

# <kebab-id>

## Suggestion
<one to three sentences: the concrete change to propose to a Trio loop>

## Rationale
<why it matters, tied to the evidence>
```

### Frontmatter keys

| Key | Meaning |
| --- | --- |
| `id` | Short stable kebab-case slug derived from the origin and problem (e.g., `docs-setup-stale`). Unique per finding; a card with an existing id is a duplicate. |
| `fingerprint` | Content hash of the suggestion: `sha256sum` of the normalized `origin|summary|rationale` text (whitespace trimmed, internal newlines collapsed to spaces). The dedupe key. |
| `origin` | The file or path the finding points at. |
| `class` | Confidence tier of the proposal: `A`, `B`, or `C` (see below). |
| `evidence` | YAML list of `file:line` references or exact failing commands that prove the finding. |
| `confidence` | Scout-assessed certainty: `low`, `medium`, or `high`. |
| `created` | Scan date in ISO-8601 (`YYYY-MM-DD`). |

### Body

- `## Suggestion` — the concrete change to propose to a Trio loop, in one to three sentences.
- `## Rationale` — why the change matters, tied to the evidence.

### Classes

- **A — reversible / high-confidence / mechanical.** Safe to hand to a loop directly; failure is cheap to undo (e.g., a stale doc reference that contradicts the current code, a missing check that can be added mechanically).
- **B — needs judgment / likely correct.** Probably right but requires interpretation or a trade-off; a loop should verify before committing (e.g., config drift where the intended value is ambiguous).
- **C — human-only / strategic or risky.** Requires a human decision — product direction, large blast radius, or irreversible actions. A loop must NOT act on a class C card.

## Candidate sourcing

Scouts look for these categories of candidate work:

- stale or contradictory documentation,
- manifest/config drift,
- failing or missing checks,
- TODO/FIXME clusters,
- missing verification for recent changes.

Each scout returns a concise list of candidate objects `{origin, summary, evidence, confidence, class}`; the orchestrator reads only those lists — heavy scanning never enters the orchestrator context, and the user sees at most a 5-line summary.

## Deduplication rules

A candidate is skipped when any of these holds:

1. Its fingerprint matches the `fingerprint` frontmatter of an existing `proposals/*.md` card.
2. Its normalized text matches or substantially overlaps the `mission:` line of any live `loop*/GOAL.md` — the work is already claimed by an active loop.
3. It is a near-duplicate of another candidate from the same run (same origin + same summary wording).

Fingerprints are deterministic: summary and rationale wording is tied to the concrete evidence, so the same finding always produces the same hash.

## Idempotency

Re-running the scan never creates duplicates and never rewrites existing cards. Cards are immutable once written: a later scan either matches a card's fingerprint (skip) or writes a new id. This makes the skill safe to schedule in the background on any cadence.
