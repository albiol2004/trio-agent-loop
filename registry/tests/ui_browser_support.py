"""Discovery and runner for the dashboard frontend's browser harness
(api:UiHarness): node + playwright-core + chromium, and ``run_cases`` which
drives ``ui_browser.cjs`` on one case module.

Discovery is the one test_dash_stream_dom.py uses: TRIO_DASH_PW_CORE or
``require.resolve('playwright-core')`` or ~/personal/*/node_modules;
TRIO_DASH_CHROMIUM or ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome.
Tests using this module SKIP, with ``skip_reason()``, when a piece is missing;
they never pass vacuously.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HARNESS = HERE / "ui_browser.cjs"


def _find_playwright_core() -> str | None:
    env = os.environ.get("TRIO_DASH_PW_CORE")
    if env:
        return env if os.path.exists(env) else None
    node = shutil.which("node")
    if node:
        probe = subprocess.run(
            [node, "-e", "console.log(require.resolve('playwright-core'))"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
        resolved = probe.stdout.strip()
        if probe.returncode == 0 and resolved and os.path.exists(resolved):
            return resolved
    home = os.path.expanduser("~/personal")
    for pattern in ("*/node_modules/playwright-core",
                    "*/*/node_modules/playwright-core"):
        for found in sorted(glob.glob(os.path.join(home, pattern))):
            if os.path.isfile(os.path.join(found, "package.json")):
                return found
    return None


def _revision_key(path: str):
    """Newest chromium-<rev> first: compare revisions numerically."""
    marker = "chromium-"
    tail = path.rsplit(marker, 1)[-1].split("/", 1)[0]
    return (int(tail) if tail.isdigit() else -1, path)


def _find_chromium() -> str | None:
    env = os.environ.get("TRIO_DASH_CHROMIUM")
    if env:
        return env if os.path.isfile(env) else None
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")),
        key=_revision_key, reverse=True)
    found += [shutil.which(n) for n in
              ("chromium-browser", "chromium", "google-chrome")]
    return next((c for c in found if c and os.path.isfile(c)), None)


NODE = shutil.which("node")
PLAYWRIGHT_CORE = _find_playwright_core() if NODE else None
CHROMIUM = _find_chromium()


def skip_reason() -> str | None:
    if not NODE:
        return "node is not installed"
    if not PLAYWRIGHT_CORE:
        return ("playwright-core not found (set TRIO_DASH_PW_CORE or install "
                "it where node can resolve it)")
    if not CHROMIUM:
        return "no chromium found (set TRIO_DASH_CHROMIUM)"
    return None


def run_cases(case_rel: str, timeout: float = 120) -> dict:
    """Run registry/tests/<case_rel> (an ES module) in headless chromium and
    return ``{"results": {...}, "errors": [...], "console": [...]}``."""
    reason = skip_reason()
    if reason:
        raise RuntimeError(reason)
    proc = subprocess.run(
        [NODE, str(HARNESS), case_rel], capture_output=True, text=True,
        timeout=timeout, cwd=str(REPO_ROOT),
        env=dict(os.environ, TRIO_DASH_PW_CORE=PLAYWRIGHT_CORE,
                 TRIO_DASH_CHROMIUM=CHROMIUM, TZ="UTC"))
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise AssertionError(
            f"no harness output (exit {proc.returncode}); stderr: {proc.stderr[-1500:]}")
    return json.loads(lines[-1])
