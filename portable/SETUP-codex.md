# Codex uses its dedicated bundle

Do not run Codex through this portable driver. Install the Codex skill, custom
agents, and native-first fallback runner instead:

```bash
./install.sh --codex
```

Then ask Codex to run a Trio loop. Native custom agents are preferred; isolated
`codex exec` role sessions are used only when the current task does not expose
native spawn controls. See `SETUP-BY-CODEX.md`.
