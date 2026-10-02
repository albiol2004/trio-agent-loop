"""Regression scenario for the runner's key-scrub (runner.py's
``_handle_stdout_line``): the Lead's very first turn echoes the raw provider
key back on stdout (in ordinary text AND in a ``DENIED:`` line) and on
stderr, then reports a provider error whose own message also carries the
key. None of this may ever reach ``TurnResult.text``/``.error``/``.denials``,
any on-disk turn log, ``.opencode-result.json``, the registry record,
``.driver.json`` or the CLI's own stdout/stderr."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def handle(ctx) -> None:
    if ctx.agent == "trio-lead" and common.is_plan_call(ctx):
        key = os.environ.get("OPENCODE_API_KEY", "")
        ctx.stderr(f"debug: key in use: {key}")
        ctx.text(f"planning notes, key echoed right here: {key}")
        ctx.text("DENIED: a safe line with no secret in it")
        ctx.error("ProviderAuthError", f"invalid api key: {key}")
        return
    ctx.error("UnknownError", f"leak_key.py: unexpected call: {ctx.agent} {ctx.prompt[:120]!r}")
