#!/usr/bin/env python3
"""dashboard/tests/dom_smoke.py — headless-DOM smoke test for the Slices UI.

Verifies (PLAN.md's ``ui-slices`` slice, GOAL.md's B5) that the loop
detail's Slices section, the per-slice lifecycle chips, and the Timeline
slice-count summary actually render in a browser DOM, not just that the
data reaches the frontend. Boots dashboard/serve.py on an ephemeral port
against a mailbox root, renders one open-loop loop's detail (reached via
the `#loop=<name>` deep link) with headless chromium's ``--dump-dom``, and
asserts on the rendered markup. Fails on any real browser console error.

Usage:
    dashboard/tests/dom_smoke.py                 builds its own temp
                                                   fixture root (two
                                                   mailboxes: one
                                                   open-loop, one
                                                   lockstep) and tests it.
    dashboard/tests/dom_smoke.py --root PATH      tests an existing
                                                   mailbox root instead
                                                   (e.g. this repo).

`loop-*/` mailbox directories are gitignored, so a bare checkout has none
— the no-argument mode is what makes this test runnable in a worktree.
Stdlib only; no third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"

# --- fixture shas/ids (40-hex-char shas; QUEUE.md/VERDICT.md format) -------

SHA_RETIRED_ONLY = "1" * 40   # retired-slice: retired, never graded
SHA_FIXED_OLD = "2" * 40      # fixed-slice: superseded entry
SHA_FIXED_NEW = "3" * 40      # fixed-slice: latest entry, graded SHIP
SHA_FAULTED = "4" * 40        # faulted-slice: graded ITERATE, open fault
SHA_REPAIRING = "5" * 40      # repairing-slice: graded ITERATE, taken fault

OPEN_LOOP_NAME = "loop-dom-smoke-open"
LOCKSTEP_NAME = "loop-dom-smoke-lockstep"

FAULT_OPEN_ID = "f-open-1"
FAULT_TAKEN_ID = "f-taken-1"

CONSOLE_ERROR_PATTERNS = re.compile(
    r"CONSOLE|Uncaught|TypeError|ReferenceError|SyntaxError|Failed to load resource"
)


# ------------------------------- fixture mailbox ---------------------------


def _slice_entry(sid: str, iteration: int, status: str) -> str:
    return (
        f"  - id: {sid}\n"
        f"    iteration: {iteration}\n"
        f"    writes: []\n"
        f"    reads: []\n"
        f"    status: {status}\n"
        f"    accepts: []"
    )


def build_open_loop_mailbox(root: Path) -> Path:
    """One open-loop mailbox exercising every slice lifecycle state, a
    slice with TWO retired entries for the same id (superseded), a graded
    SHIP, a graded ITERATE, an `open` fault and a `taken` fault."""
    mailbox = root / OPEN_LOOP_NAME
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text(
        "# DOM smoke fixture\n\nmission: exercise the Slices UI.\n",
        encoding="utf-8",
    )
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nmax_iterations: 3\nstatus: running\n",
        encoding="utf-8",
    )
    (mailbox / "LOG.md").write_text(
        "- iter 1 | lead | slices retired and graded\n"
        "- iter 1 | evaluator | VERDICT: SHIP\n",
        encoding="utf-8",
    )
    slices_yaml = "\n".join([
        _slice_entry("planned-slice", 1, "planned"),
        _slice_entry("building-slice", 1, "in_progress"),
        _slice_entry("retired-slice", 1, "in_progress"),
        _slice_entry("fixed-slice", 1, "complete"),
        _slice_entry("faulted-slice", 1, "complete"),
        _slice_entry("repairing-slice", 1, "in_progress"),
    ])
    (mailbox / "PLAN.md").write_text(
        f"# Plan\n\n```yaml\nslices:\n{slices_yaml}\n```\n", encoding="utf-8"
    )
    (mailbox / "QUEUE.md").write_text(
        "```yaml\n"
        "retired:\n"
        f"  - slice: retired-slice\n    sha: {SHA_RETIRED_ONLY}\n"
        "    at: 2026-08-20T10:00:00Z\n"
        f"  - slice: fixed-slice\n    sha: {SHA_FIXED_OLD}\n"
        "    at: 2026-08-20T10:05:00Z\n"
        f"  - slice: fixed-slice\n    sha: {SHA_FIXED_NEW}\n"
        "    at: 2026-08-21T09:00:00Z\n"
        f"  - slice: faulted-slice\n    sha: {SHA_FAULTED}\n"
        "    at: 2026-08-21T10:00:00Z\n"
        f"  - slice: repairing-slice\n    sha: {SHA_REPAIRING}\n"
        "    at: 2026-08-21T11:00:00Z\n"
        "```\n"
        "```yaml\n"
        "faults:\n"
        f"  - id: {FAULT_OPEN_ID}\n    slice: faulted-slice\n"
        f"    observed_at: {SHA_FAULTED}\n"
        "    scope: [dashboard/app.js]\n"
        "    reason: console error on load\n"
        "    status: open\n"
        f"  - id: {FAULT_TAKEN_ID}\n    slice: repairing-slice\n"
        f"    observed_at: {SHA_REPAIRING}\n"
        "    scope: [dashboard/app.js]\n"
        "    reason: lifecycle regex mismatch\n"
        "    status: taken\n"
        "```\n",
        encoding="utf-8",
    )
    (mailbox / "VERDICT.md").write_text(
        f"## slice fixed-slice @{SHA_FIXED_NEW} — SHIP\n\nfix landed cleanly.\n\n"
        f"## slice faulted-slice @{SHA_FAULTED} — ITERATE\n\nregression found.\n\n"
        f"## slice repairing-slice @{SHA_REPAIRING} — ITERATE\n\nfix in progress.\n",
        encoding="utf-8",
    )
    return mailbox


def build_lockstep_mailbox(root: Path) -> Path:
    """A lockstep mailbox (no QUEUE.md) alongside the open-loop one."""
    mailbox = root / LOCKSTEP_NAME
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text(
        "# DOM smoke fixture (lockstep)\n\nmission: sanity-check lockstep rendering.\n",
        encoding="utf-8",
    )
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nmax_iterations: 3\nstatus: running\n",
        encoding="utf-8",
    )
    (mailbox / "LOG.md").write_text(
        "- iter 1 | lead | building the thing\n", encoding="utf-8"
    )
    slices_yaml = "\n".join([
        _slice_entry("ls-a", 1, "complete"),
        _slice_entry("ls-b", 1, "in_progress"),
    ])
    (mailbox / "PLAN.md").write_text(
        f"# Plan\n\n```yaml\nslices:\n{slices_yaml}\n```\n", encoding="utf-8"
    )
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    return mailbox


def discover_open_loop_mailbox(root: Path) -> str:
    """Pick an open-loop mailbox (GOAL.md + QUEUE.md) under an existing
    --root; prefers the two mailboxes this very GOAL.md/PLAN.md names."""
    preferred = ["loop-slice-lifecycle", "loop-open-loop"]
    for name in preferred:
        d = root / name
        if (d / "GOAL.md").is_file() and (d / "QUEUE.md").is_file():
            return name
    candidates = sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "GOAL.md").is_file() and (p / "QUEUE.md").is_file()
    )
    if not candidates:
        raise SystemExit(f"no open-loop mailbox (GOAL.md + QUEUE.md) found under {root}")
    return candidates[0]


# --------------------------------- server -----------------------------------


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(root: Path, port: int, log_path: Path, live_dir: Path) -> subprocess.Popen:
    """serve.py on *root*; its per-user live paths (the inbox read-state the
    running trio-dash owns, its state dir, the native run registry) point into
    *live_dir*, never the real HOME (r20 review round 2, finding 5)."""
    log_file = open(log_path, "w", encoding="utf-8")
    env = {**os.environ,
           "TRIO_DASH_INBOX_STATE": str(live_dir / "inbox-state.json"),
           "TRIO_DASH_STATE_DIR": str(live_dir / "trio-dash-state"),
           "TRIO_NATIVE_RUNS_DIR": str(live_dir / "native-runs"),
           # GOAL DoD3 makes the board open an SSE stream; --dump-dom's
           # virtual-time budget never ends with a fetch pending, so it polls.
           "TRIO_DASH_STREAM": "0"}
    proc = subprocess.Popen(
        [sys.executable, str(SERVE_PATH), "--root", str(root), "--port", str(port)],
        stdout=log_file, stderr=subprocess.STDOUT, env=env,
    )
    proc._dom_smoke_log_file = log_file  # keep a ref so it isn't gc'd early
    return proc


def wait_for_server(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/board", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionRefusedError, OSError) as exc:
            last_err = exc
        time.sleep(0.15)
    raise SystemExit(f"dashboard/serve.py never became ready on {base_url}: {last_err}")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log_file = getattr(proc, "_dom_smoke_log_file", None)
    if log_file:
        log_file.close()


def api_get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -------------------------------- chromium ----------------------------------


def find_chromium() -> str:
    for candidate in ("/usr/bin/chromium-browser", "chromium-browser", "chromium", "google-chrome"):
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    raise SystemExit(
        "no headless chromium found (looked for chromium-browser, chromium, google-chrome)"
    )


def dump_dom(binary: str, url: str, user_data_dir: Path) -> tuple[str, str]:
    cmd = [
        binary,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        f"--user-data-dir={user_data_dir}",
        "--dump-dom",
        "--virtual-time-budget=8000",
        "--enable-logging=stderr",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout, result.stderr


# -------------------------------- assertions --------------------------------


class SmokeFailure(Exception):
    pass


def check(condition: bool, what: str, excerpt: str = "") -> None:
    if condition:
        print(f"PASS: {what}")
        return
    print(f"FAIL: {what}")
    if excerpt:
        print("--- offending DOM excerpt ---")
        print(excerpt[:2000])
    raise SmokeFailure(what)


def extract_section(dom: str, section_id: str) -> str:
    m = re.search(
        r'<section id="' + re.escape(section_id) + r'".*?</section>', dom, re.S
    )
    return m.group(0) if m else ""


def run_assertions(dom: str, stderr: str, name: str, api_slices: list) -> None:
    # 1. Slices section heading present, one row per slice.
    slices_html = extract_section(dom, "slices-section")
    check(bool(slices_html), "Slices section heading present", dom[:1500])
    check("Slices</h3>" in slices_html, "Slices section has an 'Slices' heading", slices_html)

    rows = re.findall(r'<div class="slice-row">(.*?)</div>', slices_html, re.S)
    check(
        len(rows) == len(api_slices),
        f"one Slices row per slice ({len(rows)} rows for {len(api_slices)} API slices)",
        slices_html,
    )

    # 2. Every row has a lifecycle chip element whose class starts with lifecycle-.
    chip_re = re.compile(r'class="lifecycle-chip lifecycle-[a-z_]+"')
    missing = [i for i, row in enumerate(rows) if not chip_re.search(row)]
    check(
        not missing,
        f"every one of {len(rows)} slice rows has a lifecycle-* chip",
        "\n".join(rows[i] for i in missing[:1]),
    )

    # 3. A graded slice (retired_sha + verdict both set) shows a short sha
    #    and its verdict word.
    graded = next(
        (s for s in api_slices if s.get("retired_sha") and s.get("verdict")), None
    )
    if graded is None:
        print("SKIP: no graded slice (retired_sha + verdict) in this mailbox")
    else:
        row = next((r for r in rows if f'>{graded["id"]}<' in r), None)
        check(row is not None, f"graded slice '{graded['id']}' has a row", slices_html)
        sha_m = re.search(r'class="slice-sha mono"[^>]*>([0-9a-f]{7,12})<', row)
        check(
            bool(sha_m),
            f"graded slice '{graded['id']}' row shows a 7-12 char short sha",
            row,
        )
        verdict_word = str(graded["verdict"]).upper()
        check(
            f">{verdict_word}<" in row,
            f"graded slice '{graded['id']}' row shows its verdict word ({verdict_word})",
            row,
        )

    # 4. A slice with an open fault shows that fault's id.
    faulted = next((s for s in api_slices if s.get("open_faults")), None)
    if faulted is None:
        print("SKIP: no slice with open_faults in this mailbox")
    else:
        row = next((r for r in rows if f'>{faulted["id"]}<' in r), None)
        check(row is not None, f"faulted slice '{faulted['id']}' has a row", slices_html)
        fault_id = faulted["open_faults"][0]
        check(
            f'fault-chip">{fault_id}<' in row,
            f"faulted slice '{faulted['id']}' row shows open fault id '{fault_id}'",
            row,
        )

    # 5. Timeline iteration row shows a slice-count summary.
    summary_m = re.search(
        r'class="iter-slice-summary">([^<]*)<', dom
    )
    check(summary_m is not None, "Timeline iteration row has a slice-count summary element", dom[:1500])
    summary_text = summary_m.group(1) if summary_m else ""
    check(
        bool(re.match(r"^\d+ (shipped|building|retired|faulted|repairing|planned)", summary_text)),
        f"Timeline slice-count summary matches the expected pattern (got {summary_text!r})",
        summary_text,
    )

    # 6. Deep link actually opened the drawer for this loop (sanity check
    #    underpinning every assertion above).
    drawer_open_m = re.search(r'<div id="drawer"[^>]*>', dom)
    check(
        drawer_open_m is not None and "hidden" not in drawer_open_m.group(0),
        f"#loop={name} deep link opened the drawer without a click",
        drawer_open_m.group(0) if drawer_open_m else "",
    )

    # 7. Console errors.
    console_lines = [ln for ln in stderr.splitlines() if CONSOLE_ERROR_PATTERNS.search(ln)]
    check(
        not console_lines,
        "no console errors reported by chromium",
        "\n".join(console_lines[:10]),
    )
    print(f"console errors: {len(console_lines)}")


# ----------------------------------- main ------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=None,
        help="existing mailbox root to test (default: build a temp fixture root)",
    )
    args = parser.parse_args(argv)

    binary = find_chromium()

    tmp_root_ctx = None
    if args.root:
        root = Path(args.root).expanduser().resolve()
        if not root.is_dir():
            print(f"error: --root {args.root} is not a directory", file=sys.stderr)
            return 2
        target_name = discover_open_loop_mailbox(root)
    else:
        tmp_root_ctx = tempfile.TemporaryDirectory(prefix="dom-smoke-")
        root = Path(tmp_root_ctx.name)
        build_open_loop_mailbox(root)
        build_lockstep_mailbox(root)
        target_name = OPEN_LOOP_NAME

    port = free_port()
    server_log = Path(tempfile.mkstemp(prefix="dom-smoke-serve-", suffix=".log")[1])
    user_data_dir_ctx = tempfile.TemporaryDirectory(prefix="dom-smoke-chromium-")
    live_dir_ctx = tempfile.TemporaryDirectory(prefix="dom-smoke-live-paths-")
    proc = None
    try:
        proc = start_server(root, port, server_log, Path(live_dir_ctx.name))
        base_url = f"http://127.0.0.1:{port}"
        wait_for_server(base_url)

        board = api_get(base_url, "/api/board")
        loop_names = {loop["name"] for loop in board.get("loops", [])}
        check(
            target_name in loop_names,
            f"server board lists the target mailbox '{target_name}'",
            json.dumps(sorted(loop_names)),
        )

        detail = api_get(
            base_url, "/api/loop?name=" + urllib.parse.quote(target_name)
        )
        check(
            detail.get("mode") == "open-loop",
            f"'{target_name}' reports mode == 'open-loop'",
            json.dumps(detail.get("mode")),
        )
        api_slices = detail.get("slices") or []
        check(bool(api_slices), f"'{target_name}' has at least one slice", json.dumps(detail)[:1500])

        url = f"{base_url}/#loop=" + urllib.parse.quote(target_name)
        dom, stderr = dump_dom(binary, url, Path(user_data_dir_ctx.name))
        run_assertions(dom, stderr, target_name, api_slices)
    except SmokeFailure:
        return 1
    finally:
        if proc is not None:
            stop_server(proc)
        user_data_dir_ctx.cleanup()
        live_dir_ctx.cleanup()
        try:
            server_log.unlink()
        except OSError:
            pass
        if tmp_root_ctx is not None:
            tmp_root_ctx.cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
