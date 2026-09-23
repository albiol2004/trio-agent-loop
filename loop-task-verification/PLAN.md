# Plan

## Verification standard
Mode: implement-then-smoke. Generator consistency, effective prompt propagation, independent review of examples against original acceptance. Examples illustrative only.

```yaml
slices:
  - id: task-verification
    repo: ..
    writes: [.agents/, .claude/, .codex/, codex/, kimi/, omnigent/, omp/, opencode/, pi/, portable/, prompts/, zcode/, examples/, MAILBOX-SCHEMA.md, README.md]
    reads: []
    gate: true
    status: complete
    iteration: 1
    accepts: ["Concrete original-goal examples and evidence in existing PLAN", "Effective prompts and project defaults preserve required checks", "Honest UI and data examples with omitted-criterion counterexamples"]
```
