Read-only reconnaissance. Do not edit any file. Do not run servers.

Confirm the SKILL.md focused validation `-k` names exist under
`/home/coder/omnigent-dev/tests`.

SKILL.md command is:

```
uv run pytest -q tests/tools/builtins/test_sys_session.py tests/runner/test_runner_dispatch.py tests/server/integration/test_sessions_child_sessions.py -k 'reasoning_effort or session_create_spawns_child_under_caller'
```

Report:

1. Whether each of those three files exists under `/home/coder/omnigent-dev/`.
2. Exact `def test_...` names (file:line) whose names contain
   `reasoning_effort` and/or `session_create_spawns_child_under_caller`
   in those three files, and if none, any matching names elsewhere under
   `/home/coder/omnigent-dev/tests`.
3. Whether `registered_native_agent_create_derives_launch_args_from_root_spec`
   exists anywhere under `/home/coder/omnigent-dev/tests` (it must not be
   required).

Return a short factual list only.
