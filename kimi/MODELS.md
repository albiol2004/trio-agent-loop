# Kimi model aliases for the Trio sequential runner

The Trio sequential runner (`kimi/skills/trio/scripts/run-role.sh`) maps
each Trio role to a Kimi model alias via `kimi -m <alias>`:

| Trio role | Kimi model alias          |
|-----------|---------------------------|
| scout     | kimi-code/kimi-for-coding |
| builder   | kimi-code/kimi-for-coding |
| lead      | kimi-code/k3              |
| evaluator | kimi-code/k3              |

Source of truth: the `case "$ROLE"` block in
`kimi/skills/trio/scripts/run-role.sh`. The same mapping is asserted by
`kimi/smoke-test.sh`.
