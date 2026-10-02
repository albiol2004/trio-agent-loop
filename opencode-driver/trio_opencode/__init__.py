"""trio-opencode: a standalone (no Omnigent, no broker) Trio loop driver for
the OpenCode CLI. Mirrors ``native/trio-native.js`` (the Claude Workflow
driver) but drives ``opencode run`` directly as a subprocess and never
imports anything under ``omnigent/``.

See ``opencode-driver/../../SPEC.md`` (the shared design spec) for the full
contract. Submodules:

- ``config`` — load/validate ``config.json``.
- ``ocgen`` — generate the per-run OpenCode config dir (agents, permissions).
- ``doctor`` — environment/config checks for the ``doctor`` CLI command.
- ``prompts`` — per-role prompt text.
- ``runner`` — one role turn = one ``opencode run`` subprocess.
- ``events`` — JSON event stream parsing/classification.
- ``steplib`` — adapter over ``native/trio_native_step.py``'s loop-core ops.
- ``waves`` — builder wave planning (ported from ``trio-native.js``).
- ``rootfree`` — the root-free Lead worktree (create/seed/land/teardown).
- ``driver`` — the state machine tying all of the above together.
- ``cli`` — the ``trio-opencode`` command line entry point.
"""

__version__ = "0.1.0"
