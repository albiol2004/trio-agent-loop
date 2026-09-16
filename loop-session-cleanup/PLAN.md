# Plan — iteration 1

## Objective

Make Trio Omnigent drivers leak-free against an unfixed Omnigent: role
titles that prune can match and that `_parse_session_title` can parse,
a prune backstop after every abort path, signal/exception-safe headless
cleanup, and archive-failure that still deletes terminals.

## Title scheme (locked)

Skill `sys_session_create` titles and headless `OmnigentRunner._title`:

`trioctl <mailbox.name> <role>:iteration <N>`

Example: `trioctl loop-session-cleanup lead:iteration 1`

- Starts with `trioctl <mailbox.name> ` so title-scoped prune matches.
- Contains `:` after the prefix so unfixed Omnigent
  `_parse_session_title` (live `~/omnigent/.../tool_dispatch.py`
  ~4089-4113) returns non-None agent/title.
- Open-loop suffix stays after that string (` lead-pass`,
  ` slice-eval:<id>`).
- Registration-anchor sessions keep their existing titles (no
  `trioctl <mailbox.name> ` prefix) and must never be closed or pruned.

## Slices

```yaml
slices:
  - id: skill-titles-and-backstop
    writes: [omnigent/entrypoints/trio-omnigent/SKILL.md, omnigent/entrypoints/trio-omnigent/prompts/lead.md, omnigent/entrypoints/trio-omnigent/prompts/evaluator.md]
    reads: []
    status: complete
    iteration: 1
    accepts: ["SKILL.md titles use the locked scheme", "step 8 close+prune backstop", "abort/orphan/idle-timeout recreate path"]
  - id: headless-signals
    writes: [omnigent/trioctl, omnigent/tests/test_omnigent_loop.py]
    reads: []
    status: complete
    iteration: 1
    accepts: ["command_loop prunes on SIGINT/SIGTERM and exceptions"]
  - id: archive-policy
    writes: [omnigent/trioctl, omnigent/tests/test_trioctl.py]
    reads: []
    status: complete
    iteration: 1
    accepts: ["archive failure retries once then deletes unless --keep-unarchived"]
  - id: prune-scheme
    writes: [omnigent/trioctl, omnigent/tests/test_trioctl.py, omnigent/tests/test_omnigent_loop.py]
    reads: []
    status: complete
    iteration: 1
    accepts: ["new titles match with --include-sub-agents", "anchors never match"]
  - id: docs
    writes: [SETUP-BY-OMNIGENT.md, omnigent/trioctl]
    reads: []
    status: complete
    iteration: 1
    accepts: ["SETUP documents cleanup backstop and title scheme", "VERSION minor bump"]
```

Waves: slice 1 alone (skill/prompts). Slices 2–4 all write
`omnigent/trioctl` — serial, never a parallel wave. Slice 5 last
(VERSION lives in trioctl).

## Acceptance mapping

| GOAL | Slice |
|---|---|
| Skill titles prefix + colon parse | skill-titles-and-backstop, prune-scheme |
| Step 8 close + prune backstop | skill-titles-and-backstop |
| Abort, interrupt, orphan/idle-timeout | skill-titles-and-backstop |
| Anchors never closed/deleted | skill-titles-and-backstop, prune-scheme |
| Headless prune on signal/exception | headless-signals |
| Archive failure retry then delete | archive-policy |
| Prune matches new titles | prune-scheme |
| Tests + VERSION + SETUP docs | archive-policy, prune-scheme, docs |

## Verification standard

mode: `implement-then-smoke`

Promised evidence under `loop-session-cleanup/evidence/iter1/`:

- `pytest.txt` — `python3 -m pytest -q omnigent/tests/test_trioctl.py omnigent/tests/test_omnigent_loop.py`
- `prune-dry-run.txt` — fake-broker prune `--dry-run --include-sub-agents`
- `skill-step8.txt` — exact final SKILL.md step 8 text
- `compile.txt` — `python3 -W error -m py_compile` on touched Python
