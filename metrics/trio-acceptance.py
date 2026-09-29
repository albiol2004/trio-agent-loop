#!/usr/bin/env python3
"""trio-acceptance.py -- the frozen-acceptance runner and manifest (r19).

The acceptance author (a separate role, never the Lead) turns GOAL.md into
black-box checks under ``acceptance/``; the driver validates them at the
loop's base, freezes them in a driver-made commit, and runs them at every
integration pin. This module is the one implementation of that contract
(MAILBOX-SCHEMA.md "Frozen acceptance (r19)"), shared by the loop core
(metrics/trio_loop.py), trioctl and the native helper.

Usage:
  trio-acceptance.py run --mailbox <mb> --tree <path|sha> [--ids ACC-01,..] [--out F]
  trio-acceptance.py validate --export <dir>
  trio-acceptance.py hash --mailbox <mb>
  trio-acceptance.py verify --mailbox <mb> --pin <sha256>

Check outcomes (from the check's exit code):
  0 -> PASS (and every `expect.stdout` regex matches), 1 -> FAIL,
  77 or an unmet `needs` entry -> UNAVAILABLE, killed at `timeout_s` ->
  FAIL (reason `timeout`), anything else -> ERROR, re-run once; still
  ERROR -> FAIL (reason `error`).

Isolation: the tree is copied (no .git, node_modules, caches, no mailbox)
into a fresh temp dir; each check runs there in its own process group,
under bwrap (`--unshare-all`: loopback only, read-only /, tmpfs /tmp) when
available, else unsandboxed with a dead http(s) proxy (``sandbox: none``).
The environment is scrubbed of ``*_TOKEN``/``*_KEY``/``*_SECRET``/
``*_PASSWORD`` and proxy variables.

`run` exits 0 when every check PASSes, 1 on any FAIL, 3 when the only
non-PASS outcomes are UNAVAILABLE, 2 on a usage/manifest error. `verify`
exits 0 when the pack hash equals --pin, else 1. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: Contract version of this module's library functions (callers check it).
ACCEPTANCE_API = 1
MANIFEST_VERSION = 1
PACK_DIR = "acceptance"
MANIFEST = "MANIFEST.json"
FROZEN = "FROZEN"
AMENDMENTS = "AMENDMENTS.md"
AUTHOR_NOTES = "AUTHOR.md"
INPUT_DIR = ".acceptance-input"

KINDS = ("behaviour", "guard", "doc")
#: Kinds that must FAIL at base and need a coverage mapping.
COVERED_KINDS = ("behaviour", "doc")
MAX_GUARDS = 3
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 120
DEFAULT_BUDGET_S = 300
MAX_BUDGET_S = 300
MIN_CHECKS = 5
MAX_CHECKS = 25
RETRY_DROP_FRACTION = 0.30
EXIT_UNAVAILABLE = 77
EXCERPT_BYTES = 2048
CHECK_ID_RE = re.compile(r"^ACC-[0-9]{1,4}$")
BINDING_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SETUP_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHECK_KEYS = {
    "id", "goal_ref", "goal_quote", "kind", "surface", "run", "expect",
    "timeout_s", "needs", "binds", "network",
}
#: The only per-check fields an Evaluator amendment may change (§3.4).
AMENDABLE_KEYS = ("run", "expect", "timeout_s", "binds", "needs")
IMMUTABLE_KEYS = ("id", "goal_quote", "kind")
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv",
    ".mypy_cache", ".ruff_cache", ".tox",
})
#: Never part of the pack hash (a check run in place may leave them).
HASH_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", "node_modules"})
SCRUB_ENV_RE = re.compile(
    r"(?:_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSWD)$|^(?:https?_proxy|all_proxy|ftp_proxy|no_proxy)$",
    re.IGNORECASE,
)
SANDBOX_ENV = "TRIO_ACCEPTANCE_SANDBOX"  # auto (default) | bwrap | none
CACHE_ENV = "TRIO_ACCEPTANCE_CACHE"
PROVIDES_FROM_ENV = "TRIO_ACCEPTANCE_PROVIDES_FROM"
STATE_ENV = "TRIO_ACCEPTANCE_STATE"
DEAD_PROXY = "http://127.0.0.1:9"
#: Vendored Trio loop files removed from the author's export (§2.2).
TRIO_METRICS_FILES = (
    "trio_loop.py", "trio-metrics.py", "trio-shadow.py", "trio-check.py",
    "trio-acceptance.py",
)
MAILBOX_MARKERS = ("QUEUE.md", "STATE.md", "VERDICT.md", "LOG.md")


class ManifestError(ValueError):
    """The acceptance manifest cannot be read or is structurally invalid."""


# ---------------------------------------------------------------- hashing


def pack_files(acc_dir: Path) -> list[str]:
    """Sorted pack-relative paths that make up the pin (FROZEN excluded)."""
    out: list[str] = []
    acc_dir = Path(acc_dir)
    if not acc_dir.is_dir():
        return out
    for root, dirs, files in os.walk(acc_dir):
        dirs[:] = sorted(d for d in dirs if d not in HASH_SKIP_DIRS)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            rel = (Path(root) / name).relative_to(acc_dir).as_posix()
            if rel == FROZEN:
                continue
            out.append(rel)
    return sorted(out)


def _pack_copy_ignore(directory: str, names: list[str]) -> set[str]:
    """shutil ignore callback: everything `pack_files` leaves out of the pin
    (FROZEN is re-added by callers that need it)."""
    return {n for n in names
            if n in HASH_SKIP_DIRS or n == FROZEN or n.endswith((".pyc", ".pyo"))}


def manifest_sha256(acc_dir: Path) -> str:
    """sha256 over sorted (relpath NUL bytes NUL) of acceptance/** except FROZEN."""
    acc_dir = Path(acc_dir)
    digest = hashlib.sha256()
    for rel in pack_files(acc_dir):
        path = acc_dir / rel
        if path.is_symlink():
            data = b"symlink:" + os.readlink(path).encode()
        else:
            data = path.read_bytes()
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def read_frozen(acc_dir: Path) -> dict[str, Any]:
    """Parse FROZEN: its key lines plus the ordered pin chain."""
    path = Path(acc_dir) / FROZEN
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields: dict[str, Any] = {"pins": []}
    for line in text.splitlines():
        m = re.match(r"^pin\[(\d+)\]:\s*([0-9a-f]{64})\s*(.*)$", line.strip())
        if m:
            fields["pins"].append({"n": int(m.group(1)), "sha256": m.group(2),
                                   "note": m.group(3).strip()})
            continue
        key, sep, value = line.partition(":")
        if sep and re.match(r"^[a-z_]+$", key.strip()):
            fields[key.strip()] = value.strip()
    return fields


def latest_pin(acc_dir: Path) -> str | None:
    pins = read_frozen(acc_dir).get("pins") or []
    return pins[-1]["sha256"] if pins else None


# --------------------------------------------------------------- manifest


def load_manifest(acc_dir: Path) -> dict[str, Any]:
    path = Path(acc_dir) / MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"{path}: cannot read ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: top level is not an object")
    if data.get("acceptance_version") != MANIFEST_VERSION:
        raise ManifestError(
            f"{path}: acceptance_version is {data.get('acceptance_version')!r}, "
            f"expected {MANIFEST_VERSION}")
    if not isinstance(data.get("checks"), list):
        raise ManifestError(f"{path}: `checks` is not a list")
    return data


def dump_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _goal_texts(goal_text: str | None, notes_text: str | None) -> list[str]:
    return [t for t in (goal_text, notes_text) if t]


def check_errors(check: Any, manifest: dict[str, Any], goal_text: str | None = None,
                 notes_text: str | None = None, acc_root: Path | None = None) -> list[str]:
    """Problems of one check entry ([] = valid)."""
    if not isinstance(check, dict):
        return ["check entry is not an object"]
    errs: list[str] = []
    cid = check.get("id")
    if not isinstance(cid, str) or not CHECK_ID_RE.match(cid):
        errs.append(f"id {cid!r} is not ACC-NN")
    unknown = sorted(set(check) - CHECK_KEYS)
    if unknown:
        errs.append(f"unknown key(s) {', '.join(unknown)}")
    if check.get("kind") not in KINDS:
        errs.append(f"kind {check.get('kind')!r} is not one of {', '.join(KINDS)}")
    quote = check.get("goal_quote")
    if not isinstance(quote, str) or not quote.strip():
        errs.append("goal_quote is empty")
    elif goal_text is not None and not any(
        quote in text for text in _goal_texts(goal_text, notes_text)
    ):
        errs.append("goal_quote is not a verbatim substring of GOAL.md (or the notes)")
    run = check.get("run")
    if not (isinstance(run, list) and run and all(isinstance(a, str) and a for a in run)):
        errs.append("run must be a non-empty argv list of strings")
    elif acc_root is not None:
        for arg in run:
            if arg.startswith(PACK_DIR + "/") and not (acc_root.parent / arg).exists():
                errs.append(f"run names {arg}, which does not exist")
    expect = check.get("expect", {"exit": 0})
    if not isinstance(expect, dict) or set(expect) - {"exit", "stdout"}:
        errs.append("expect must be an object with `exit` (0) and optional `stdout`")
    else:
        if expect.get("exit", 0) != 0:
            errs.append("expect.exit must be 0 (a check exits 0 on PASS)")
        pats = expect.get("stdout", [])
        if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
            errs.append("expect.stdout must be a list of regexes")
        else:
            for pat in pats:
                try:
                    re.compile(pat)
                except re.error as exc:
                    errs.append(f"expect.stdout regex {pat!r}: {exc}")
    timeout = check.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or not 0 < timeout <= MAX_TIMEOUT_S:
        errs.append(f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
    needs = check.get("needs", [])
    setups = {s.get("id") for s in manifest.get("setup") or [] if isinstance(s, dict)}
    if not isinstance(needs, list) or not all(isinstance(n, str) and n for n in needs):
        errs.append("needs must be a list of strings")
    else:
        for need in needs:
            if need.startswith("setup:") and need[6:] not in setups:
                errs.append(f"needs {need} names no declared setup entry")
    binds = check.get("binds", [])
    declared = manifest.get("bindings") or {}
    if not isinstance(binds, list) or not all(isinstance(b, str) for b in binds):
        errs.append("binds must be a list of binding names")
    else:
        for name in binds:
            if name not in declared:
                errs.append(f"binds {name} is not declared in `bindings`")
    if check.get("network", "loopback") not in ("loopback", "none"):
        errs.append("network must be `loopback` (checks never reach beyond it)")
    return errs


def manifest_errors(manifest: dict[str, Any], goal_text: str | None = None,
                    notes_text: str | None = None) -> list[str]:
    """Pack-level problems (per-check problems: ``check_errors``)."""
    errs: list[str] = []
    budget = manifest.get("budget_s", DEFAULT_BUDGET_S)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) \
            or not 0 < budget <= MAX_BUDGET_S:
        errs.append(f"budget_s must be in (0, {MAX_BUDGET_S}]")
    bindings = manifest.get("bindings") or {}
    if not isinstance(bindings, dict):
        errs.append("bindings must be an object")
    else:
        for name, spec in bindings.items():
            if not BINDING_RE.match(str(name)):
                errs.append(f"binding {name!r} is not UPPER_SNAKE")
            if not isinstance(spec, dict) or not isinstance(spec.get("default"), str):
                errs.append(f"binding {name}: needs a string `default`")
            elif goal_text is not None and spec.get("goal_quote") and not any(
                spec["goal_quote"] in t for t in _goal_texts(goal_text, notes_text)
            ):
                errs.append(f"binding {name}: goal_quote is not verbatim in GOAL.md")
    setups = manifest.get("setup") or []
    if not isinstance(setups, list):
        errs.append("setup must be a list")
    else:
        seen: set[str] = set()
        for entry in setups:
            if not isinstance(entry, dict) or not SETUP_ID_RE.match(str(entry.get("id") or "")):
                errs.append(f"setup entry {entry!r} needs a kebab-case id")
                continue
            if entry["id"] in seen:
                errs.append(f"setup id {entry['id']} is duplicated")
            seen.add(entry["id"])
            if not isinstance(entry.get("cmd"), str) or not entry["cmd"].strip():
                errs.append(f"setup {entry['id']}: needs a `cmd` string")
            prov = entry.get("provides")
            if not isinstance(prov, str) or not prov or prov.startswith("/") or ".." in prov.split("/"):
                errs.append(f"setup {entry['id']}: `provides` must be a relative path")
    ids = [c.get("id") for c in manifest.get("checks") or [] if isinstance(c, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1 and i})
    if dup:
        errs.append(f"duplicate check id(s): {', '.join(dup)}")
    return errs


def covered_ids(manifest: dict[str, Any]) -> list[str]:
    """Ids of checks that need a coverage mapping (behaviour/doc)."""
    return [c["id"] for c in manifest.get("checks") or []
            if isinstance(c, dict) and c.get("kind") in COVERED_KINDS and c.get("id")]


# ------------------------------------------------------------ tree copies


def _copy_ignore(root: Path, exclude: set[str]):
    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).resolve().relative_to(root).as_posix()
        out = {n for n in names if n in SKIP_DIRS}
        for name in names:
            child = f"{rel}/{name}" if rel != "." else name
            if child in exclude:
                out.add(name)
        return out
    return ignore


def copy_tree(src: Path, dst: Path, exclude: Iterable[str] = ()) -> None:
    src = Path(src).resolve()
    shutil.copytree(src, dst, symlinks=True, ignore=_copy_ignore(src, set(exclude)))


def _git(repo: Path, *args: str, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[-300:]}")
    return proc


def git_toplevel(path: Path) -> Path | None:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 else None


def archive_tree(repo: Path, rev: str, dest: Path) -> str:
    """`git archive <rev> | tar -x` into *dest*; returns the full sha."""
    sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", sha],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout,
                         capture_output=True)
    archive.stdout.close()  # type: ignore[union-attr]
    err = archive.stderr.read().decode(errors="replace") if archive.stderr else ""
    if archive.wait() != 0 or tar.returncode != 0:
        raise RuntimeError(f"git archive {sha[:12]} failed: {err.strip() or tar.stderr[-300:]!r}")
    return sha


def _is_sha_like(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value)) or value in ("HEAD",) \
        or value.startswith(("HEAD~", "HEAD^", "refs/"))


# ------------------------------------------------------------- sandboxing

_SANDBOX_CACHE: dict[str, bool] = {}


def sandbox_mode() -> str:
    """`bwrap` when bwrap works (loopback only), else `none`."""
    wanted = os.environ.get(SANDBOX_ENV, "auto").strip().lower() or "auto"
    if wanted == "none":
        return "none"
    if "ok" not in _SANDBOX_CACHE:
        ok = False
        if shutil.which("bwrap"):
            try:
                ok = subprocess.run(
                    ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                     "--unshare-all", "--die-with-parent", "true"],
                    capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        _SANDBOX_CACHE["ok"] = ok
    if _SANDBOX_CACHE["ok"]:
        return "bwrap"
    if wanted == "bwrap":
        raise RuntimeError("TRIO_ACCEPTANCE_SANDBOX=bwrap but bwrap does not work here")
    return "none"


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if SCRUB_ENV_RE.search(key):
            env.pop(key, None)
    return env


def _sandbox_argv(argv: list[str], cwd: Path, writable: list[Path],
                  readable: list[Path]) -> list[str]:
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    for path in readable:
        cmd += ["--ro-bind", str(path), str(path)]
    for path in writable:
        cmd += ["--bind", str(path), str(path)]
    cmd += ["--unshare-all", "--die-with-parent", "--chdir", str(cwd), "--", *argv]
    return cmd


# --------------------------------------------------------------- running


def _tail(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return data[-EXCERPT_BYTES:]


def _reason_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""


def _attempt(argv: list[str], cwd: Path, env: dict[str, str], timeout: float,
             sandbox: str, writable: list[Path], readable: list[Path]) -> dict[str, Any]:
    cmd = _sandbox_argv(argv, cwd, writable, readable) if sandbox == "bwrap" else argv
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        return {"exit": None, "error": f"cannot start: {exc}", "output": "",
                "wall_s": 0.0, "timeout": False}
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = b""
    else:
        # Reap stray children the check left in its group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return {"exit": proc.returncode, "output": _tail(out), "timeout": timed_out,
            "wall_s": round(time.monotonic() - started, 2)}


def _classify(check: dict[str, Any], attempt: dict[str, Any]) -> tuple[str, str | None]:
    """(outcome, reason): PASS | FAIL | UNAVAILABLE | ERROR."""
    if attempt.get("timeout"):
        return "FAIL", "timeout"
    code = attempt.get("exit")
    if code is None:
        return "ERROR", attempt.get("error") or "runner failure"
    if code == 0:
        for pat in (check.get("expect") or {}).get("stdout") or []:
            if not re.search(pat, attempt.get("output") or "", re.MULTILINE):
                return "FAIL", f"stdout does not match {pat!r}"
        return "PASS", None
    if code == 1:
        return "FAIL", _reason_line(attempt.get("output") or "") or "exit 1"
    if code == EXIT_UNAVAILABLE:
        return "UNAVAILABLE", _reason_line(attempt.get("output") or "") or "exit 77"
    return "ERROR", f"exit {code}"


def run_one(check: dict[str, Any], copy_root: Path, work: Path, env: dict[str, str],
            sandbox: str, readable: list[Path], unmet: list[str]) -> dict[str, Any]:
    cid = check["id"]
    if unmet:
        return {"id": cid, "outcome": "UNAVAILABLE", "reason": "needs " + ", ".join(unmet),
                "excerpt": "", "wall_s": 0.0}
    timeout = float(check.get("timeout_s", DEFAULT_TIMEOUT_S))
    wall = 0.0
    last: dict[str, Any] = {}
    for attempt_no in (1, 2):
        scratch = work / f"{cid}-{attempt_no}"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "tmp").mkdir(exist_ok=True)
        run_env = dict(env, ACC_ID=cid, ACC_WORK=str(scratch), TMPDIR=str(scratch / "tmp"))
        last = _attempt(list(check["run"]), copy_root, run_env, timeout, sandbox,
                        [copy_root, work], readable)
        wall += last["wall_s"]
        outcome, reason = _classify(check, last)
        if outcome != "ERROR":
            return {"id": cid, "outcome": outcome, "reason": reason,
                    "excerpt": last.get("output", ""), "wall_s": round(wall, 2),
                    **({"rerun": True} if attempt_no == 2 else {})}
    return {"id": cid, "outcome": "FAIL", "reason": "error", "error": True,
            "detail": reason, "excerpt": last.get("output", ""),
            "wall_s": round(wall, 2), "rerun": True}


def _cache_dir() -> Path:
    env = os.environ.get(CACHE_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".cache") / "trio-agent-loop" / "acceptance"


def resolve_setups(manifest: dict[str, Any], sources: list[Path], copy_root: Path,
                   log: list[str], allow_setup: bool = True) -> dict[str, Path | None]:
    """Provide each setup's directory: from the tree/source dirs, the cache,
    or by running its `cmd` once (the only step allowed network access)."""
    provided: dict[str, Path | None] = {}
    for entry in manifest.get("setup") or []:
        sid, rel = entry["id"], entry["provides"]
        found = next((s / rel for s in sources if (s / rel).is_dir()), None)
        if found is None:
            key = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]
            cached = _cache_dir() / f"{sid}-{key}" / rel
            if cached.is_dir():
                found = cached
            elif allow_setup:
                build = Path(tempfile.mkdtemp(prefix=f"acc-setup-{sid}-"))
                try:
                    tree = build / "tree"
                    copy_tree(copy_root, tree, exclude={PACK_DIR})
                    proc = subprocess.run(["sh", "-c", entry["cmd"]], cwd=str(tree),
                                          env=scrubbed_env(), capture_output=True,
                                          text=True, timeout=900)
                    if proc.returncode == 0 and (tree / rel).is_dir():
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(tree / rel), str(cached))
                        found = cached
                        log.append(f"setup {sid}: built into the cache")
                    else:
                        log.append(f"setup {sid}: `{entry['cmd']}` failed "
                                   f"(exit {proc.returncode})")
                except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                    log.append(f"setup {sid}: {exc}")
                finally:
                    shutil.rmtree(build, ignore_errors=True)
        provided[sid] = found
        if found is not None:
            target = copy_root / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(found, target)
    return provided


def _plan_bindings(mailbox: Path | None) -> dict[str, str]:
    if mailbox is None or not (mailbox / "PLAN.md").is_file():
        return {}
    path = Path(__file__).resolve().with_name("trio-metrics.py")
    try:
        spec = importlib.util.spec_from_file_location("trio_metrics_acc", path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        parsed = module.parse_plan_acceptance(
            (mailbox / "PLAN.md").read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - bindings fall back to manifest defaults
        return {}
    return dict(parsed.get("bindings") or {})


def binding_values(manifest: dict[str, Any], plan_bindings: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in (manifest.get("bindings") or {}).items():
        if isinstance(spec, dict):
            out[name] = str(plan_bindings.get(name, spec.get("default", "")))
    return out


def run_pack(
    acc_src: Path,
    tree: Path | str,
    *,
    repo: Path | None = None,
    ids: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
    plan_bindings: dict[str, str] | None = None,
    extra_sources: Iterable[Path] = (),
    manifest: dict[str, Any] | None = None,
    allow_setup: bool = True,
) -> dict[str, Any]:
    """Run the pack in *acc_src* against *tree* (a directory, or a revision
    of *repo* extracted with `git archive`). Never writes into *tree*."""
    acc_src = Path(acc_src)
    manifest = manifest if manifest is not None else load_manifest(acc_src)
    wanted = set(ids) if ids else None
    started = time.monotonic()
    sandbox = sandbox_mode()
    tmp = Path(tempfile.mkdtemp(prefix="trio-acceptance-"))
    result: dict[str, Any] = {
        "manifest_sha256": manifest_sha256(acc_src), "pin_ok": None,
        "tree": str(tree), "tree_head": None, "sandbox": sandbox, "passed": 0,
        "failed": 0, "unavailable": 0, "total": 0, "wall_s": 0.0, "results": [],
        "log": [],
    }
    pin = latest_pin(acc_src)
    if pin is not None:
        result["pin_ok"] = pin == result["manifest_sha256"]
    try:
        copy_root = tmp / "tree"
        sources: list[Path] = []
        tree_path = Path(str(tree))
        if tree_path.is_dir():
            copy_tree(tree_path, copy_root, exclude=set(exclude) | {INPUT_DIR})
            sources.append(tree_path.resolve())
            top = git_toplevel(tree_path)
            if top is not None:
                head = _git(top, "rev-parse", "HEAD", check=False).stdout.strip()
                result["tree_head"] = head or None
        else:
            if repo is None:
                raise ManifestError(f"tree {tree!r} is not a directory and no repo was given")
            result["tree_head"] = archive_tree(repo, str(tree), copy_root)
            for rel in exclude:
                shutil.rmtree(copy_root / rel, ignore_errors=True)
        if repo is not None:
            top = git_toplevel(repo)
            if top is not None:
                sources.append(top)
        sources.extend(Path(p) for p in extra_sources)
        env_extra = os.environ.get(PROVIDES_FROM_ENV, "").strip()
        sources.extend(Path(p) for p in env_extra.split(os.pathsep) if p)
        pack_dst = copy_root / PACK_DIR
        if pack_dst.exists() or pack_dst.is_symlink():
            result["log"].append("tree has its own top-level acceptance/; shadowed by the pack")
            if pack_dst.is_dir() and not pack_dst.is_symlink():
                shutil.rmtree(pack_dst)
            else:
                pack_dst.unlink()
        # The run sees exactly the hashed pack (eval-r19 finding 8): a
        # directory the pin skips (node_modules, caches) never reaches it.
        shutil.copytree(acc_src, pack_dst, symlinks=True, ignore=_pack_copy_ignore)
        work = tmp / "work"
        work.mkdir()
        provided = resolve_setups(manifest, sources, copy_root, result["log"], allow_setup)
        readable = sorted({p.resolve() for p in provided.values() if p is not None})
        env = scrubbed_env()
        if sandbox == "none":
            env.update({"http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
                        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
                        "no_proxy": "localhost,127.0.0.1,::1",
                        "NO_PROXY": "localhost,127.0.0.1,::1"})
        env.update({"ACC_TREE": str(copy_root), "ACC_DIR": str(pack_dst),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        for name, value in binding_values(manifest, plan_bindings or {}).items():
            env[f"ACC_BIND_{name}"] = value
        budget = float(manifest.get("budget_s", DEFAULT_BUDGET_S) or DEFAULT_BUDGET_S)
        for check in manifest.get("checks") or []:
            if not isinstance(check, dict) or not check.get("id"):
                continue
            if wanted is not None and check["id"] not in wanted:
                continue
            if time.monotonic() - started > budget:
                res = {"id": check["id"], "outcome": "FAIL", "reason": "budget",
                       "over_budget": True, "excerpt": "", "wall_s": 0.0}
            else:
                unmet = []
                for need in check.get("needs") or []:
                    if need.startswith("setup:"):
                        if provided.get(need[6:]) is None:
                            unmet.append(need)
                    elif shutil.which(need, path=env.get("PATH")) is None:
                        unmet.append(need)
                res = run_one(check, copy_root, work, env, sandbox, readable, unmet)
            result["results"].append(res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for res in result["results"]:
        result["total"] += 1
        key = {"PASS": "passed", "FAIL": "failed", "UNAVAILABLE": "unavailable"}[res["outcome"]]
        result[key] += 1
    result["wall_s"] = round(time.monotonic() - started, 2)
    return result


def run_exit_code(result: dict[str, Any]) -> int:
    if result["failed"]:
        return 1
    if result["unavailable"]:
        return 3
    return 0


def summary_line(result: dict[str, Any]) -> str:
    """`p/t PASS` plus the non-PASS ids, for LOG lines and prompts."""
    bad = [f"{r['id']} {r['outcome']}" + (f" ({r['reason']})" if r.get("reason") else "")
           for r in result.get("results") or [] if r["outcome"] != "PASS"]
    line = f"{result['passed']}/{result['total']} PASS"
    if result.get("unavailable"):
        line += f", unavailable={result['unavailable']}"
    return line + (": " + "; ".join(bad) if bad else "")


# ------------------------------------------------------- freeze validation


def freeze_filter(manifest: dict[str, Any], base_result: dict[str, Any] | None,
                  goal_text: str | None, notes_text: str | None = None,
                  acc_root: Path | None = None) -> dict[str, Any]:
    """Apply §2.6 to an authored manifest and its run at base.

    Returns ``{"manifest": kept manifest, "dropped": [(id, reason)],
    "unavailable_at_base": [ids], "retry": bool, "fatal": [pack errors]}``.
    """
    fatal = manifest_errors(manifest, goal_text, notes_text)
    by_id = {r["id"]: r for r in (base_result or {}).get("results") or []}
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[str, str]] = []
    unavailable: list[str] = []
    guards = 0
    seen: set[str] = set()
    checks = [c for c in manifest.get("checks") or []]
    for check in checks:
        cid = check.get("id") if isinstance(check, dict) else None
        label = str(cid or "?")
        errs = check_errors(check, manifest, goal_text, notes_text, acc_root)
        if errs:
            reason = "goal-quote-mismatch" if any("goal_quote is not" in e for e in errs) \
                else "schema: " + errs[0]
            dropped.append((label, reason))
            continue
        if cid in seen:
            dropped.append((label, "duplicate-id"))
            continue
        seen.add(cid)
        res = by_id.get(cid)
        if base_result is not None and res is None:
            dropped.append((cid, "not-run-at-base"))
            continue
        if res is not None:
            if res.get("over_budget"):
                dropped.append((cid, "over-budget"))
                continue
            if res.get("error"):
                dropped.append((cid, "broken-at-base"))
                continue
            if res["outcome"] == "PASS" and check["kind"] in COVERED_KINDS:
                dropped.append((cid, "passes-at-base"))
                continue
            if res["outcome"] == "UNAVAILABLE":
                unavailable.append(cid)
        if check["kind"] == "guard":
            guards += 1
            if guards > MAX_GUARDS:
                dropped.append((cid, "guard-limit"))
                continue
        kept.append(check)
    if len(kept) > MAX_CHECKS:
        for check in kept[MAX_CHECKS:]:
            dropped.append((check["id"], "pack-limit"))
        kept = kept[:MAX_CHECKS]
    new = dict(manifest)
    new["checks"] = kept
    total = len(checks) or 1
    retry = bool(fatal) or (len(dropped) / total > RETRY_DROP_FRACTION) or len(kept) < MIN_CHECKS
    return {"manifest": new, "dropped": dropped, "unavailable_at_base": unavailable,
            "retry": retry, "fatal": fatal}


# ------------------------------------------------------------ the export


def _is_mailbox_dir(path: Path) -> bool:
    names = {p.name for p in path.iterdir()} if path.is_dir() else set()
    return bool(names & {"GOAL.md", "PLAN.md"}) and bool(names & set(MAILBOX_MARKERS))


def build_export(repo: Path, base: str, dest: Path, mailbox: Path,
                 notes: Path | None = None) -> dict[str, Any]:
    """The author's workspace (§2.2): `git archive <base>` into *dest* (no
    .git), minus every mailbox dir, archives, sessions, .trio*, .cursor/
    and the vendored Trio metrics files; `.acceptance-input/` gets GOAL.md
    (from the live mailbox) and the optional notes file only."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    sha = archive_tree(repo, base, dest)
    removed: list[str] = []
    top = git_toplevel(repo) or Path(repo).resolve()
    try:
        own_rel = Path(mailbox).resolve().relative_to(top).as_posix()
    except ValueError:
        own_rel = None
    if own_rel and (dest / own_rel).exists():
        shutil.rmtree(dest / own_rel, ignore_errors=True)
        removed.append(own_rel + "/")
    for root, dirs, files in os.walk(dest, topdown=True):
        here = Path(root)
        keep = []
        for d in sorted(dirs):
            child = here / d
            rel = child.relative_to(dest).as_posix()
            if d in ("archive", ".sessions", ".cursor", ".git") or d.startswith(".trio") \
                    or _is_mailbox_dir(child):
                shutil.rmtree(child, ignore_errors=True)
                removed.append(rel + "/")
            else:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            rel = (here / f).relative_to(dest).as_posix()
            if f.startswith(".trio") or (here.name == "metrics" and f in TRIO_METRICS_FILES):
                (here / f).unlink(missing_ok=True)
                removed.append(rel)
    inputs = dest / INPUT_DIR
    inputs.mkdir()
    goal = Path(mailbox) / "GOAL.md"
    shutil.copy2(goal, inputs / "GOAL.md")
    notes_path = notes if notes is not None else Path(mailbox) / "ACCEPTANCE-NOTES.md"
    notes_copied = None
    if notes_path and Path(notes_path).is_file():
        shutil.copy2(notes_path, inputs / "ACCEPTANCE-NOTES.md")
        notes_copied = "ACCEPTANCE-NOTES.md"
    return {"export": str(dest), "base": sha, "removed": sorted(removed),
            "goal_sha256": hashlib.sha256(goal.read_bytes()).hexdigest(),
            "notes": notes_copied}


# ------------------------------------------------------------ the audit

#: Mailbox files a tool call must never read (outside the export's own
#: public surface: a product `docs/PLAN.md` kept in the export is fine).
AUDIT_MAILBOX_FILES = ("PLAN.md", "VERDICT.md", "QUEUE.md", "STATE.md", "LOG.md",
                       "REPORT.md")
#: Keys of a transcript row that carry what a tool returned (never
#: evidence of what the author asked to read).
_AUDIT_OUTPUT_KEY_RE = re.compile(
    r"output|result|stdout|stderr|response|observation", re.IGNORECASE)
_AUDIT_CALL_KIND_RE = re.compile(r"tool|function|command|shell|exec", re.IGNORECASE)
_AUDIT_RESULT_KIND_RE = re.compile(r"output|result|response", re.IGNORECASE)
#: Argument keys of a tool call (whatever the runtime nests them under).
_AUDIT_ARG_KEYS = frozenset({
    "command", "cmd", "args", "argv", "arguments", "input", "parameters", "params",
    "file_path", "filepath", "path", "paths", "pattern", "glob", "query", "target",
    "cwd", "directory", "dir", "filename", "file", "files", "script",
})
_AUDIT_TOKEN_RE = re.compile(r"[^\s'\"`;|&<>(){}\[\],=\\]+")


def _audit_inputs(entry: Any) -> list[str]:
    """The strings a transcript row *asked for*: tool-call arguments.

    Tool results (role ``tool``, an output/result node, or an output key)
    and chat text are skipped: what a tool printed (a README naming
    PLAN.md, ``which node``'s toolchain path) or what the model said is not
    evidence of what the author read. A row that is not JSON is taken as a
    raw command line."""
    data: Any = entry
    if not isinstance(entry, (dict, list)):
        text = str(entry)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return [text]
        if not isinstance(data, (dict, list)):
            return [text]
    out: list[str] = []

    def walk(value: Any, in_call: bool) -> None:
        if isinstance(value, dict):
            kind = " ".join(str(value.get(k) or "") for k in ("type", "kind"))
            if str(value.get("role") or "").lower() == "tool" \
                    or _AUDIT_RESULT_KIND_RE.search(kind):
                return
            call = in_call or bool(_AUDIT_CALL_KIND_RE.search(kind))
            for key, item in value.items():
                if _AUDIT_OUTPUT_KEY_RE.search(str(key)):
                    continue
                walk(item, call or str(key).lower() in _AUDIT_ARG_KEYS)
        elif isinstance(value, list):
            for item in value:
                walk(item, in_call)
        elif isinstance(value, str) and in_call:
            out.append(value)
    walk(data, False)
    return out


def _norm(path: Path | str) -> list[str]:
    """normpath and realpath spellings of *path* (symlinked roots)."""
    raw = os.path.normpath(str(path))
    real = os.path.realpath(str(path))
    return sorted({raw, real})


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


#: Shell commands that read or search what their path arguments name (an
#: ancestor of the loop repository given to one of them reaches the repo).
_AUDIT_READ_CMDS = frozenset({
    "cat", "head", "tail", "less", "more", "bat", "strings", "xxd", "od", "nl", "wc",
    "sed", "awk", "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "tree",
    "du", "ls", "stat", "file", "diff",
})
#: Of those, the recursive searchers: even `/` given to them reaches the repo.
_AUDIT_RECURSIVE_CMDS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "tree", "du",
})
_AUDIT_PREFIX_WORDS = frozenset({"sudo", "time", "nohup", "env", "command", "exec",
                                 "xargs", "nice", "builtin"})
_AUDIT_SEGMENT_RE = re.compile(r"&&|\|\||[;|\n]")
_AUDIT_PROC_RE = re.compile(r"^/proc/[^/]+/(?:cwd|root)(?:/|$)")


def _audit_expand(token: str, cwd: str | None) -> tuple[str | None, str | None]:
    """(absolute normalized path, or None when unresolvable; hit reason)."""
    if token.startswith(("$OLDPWD", "${OLDPWD}")):
        return None, "names $OLDPWD (the previous working directory)"
    if _AUDIT_PROC_RE.match(token):
        return None, "reads a process's cwd/root through /proc"
    home = os.environ.get("HOME", "")
    for pre in ("${HOME}", "$HOME"):
        if home and (token == pre or token.startswith(pre + "/")):
            token = home + token[len(pre):]
    for pre in ("${PWD}", "$PWD"):
        if token == pre or token.startswith(pre + "/"):
            if cwd is None:
                return None, None
            token = cwd + token[len(pre):]
    if token.startswith("~"):
        token = os.path.expanduser(token)
    if token.startswith("$") or "$(" in token or "`" in token:
        return None, None
    if not token.startswith("/"):
        if cwd is None:
            return None, None
        token = os.path.join(cwd, token)
    return os.path.normpath(token), None


def _ancestor_of(path: str, roots: Iterable[str]) -> bool:
    base = path.rstrip("/") + "/" if path != "/" else "/"
    return any(r != path and r.startswith(base) for r in roots)


def audit_transcript(entries: Iterable[Any], export: Path,
                     forbidden: Iterable[Path] = (),
                     allowed: Iterable[Path] = ()) -> dict[str, Any]:
    """Mechanical isolation audit of an author session's tool calls (§2.2).

    Best-effort (DESIGN §10): only evidence that the author *read* the
    loop's plan or code counts. A tool-call argument (never a tool's output
    or chat text) contaminates when it

    - resolves inside a *forbidden* root (the loop repository, its git dir
      or main checkout, the mailbox) -- relative paths resolve against the
      tracked `cd` of the command line (the export by default), `$HOME`,
      `${HOME}`, `~` and `$PWD` are expanded;
    - is an *ancestor* of a forbidden root given to a reading/searching
      command (cat, grep, find, rg, ...) or as a bare tool path (Grep,
      Glob, Read, LS); `/` counts only for recursive searchers;
    - is `cd <root or ancestor>`, names `$OLDPWD`, or `/proc/<pid>/cwd|root`;
    - names a lab hidden-pack path (`/hidden/`, `speed/hard`).

    A mailbox file name (PLAN.md, REPORT.md, LOG.md, ...) counts only when
    it resolves inside a forbidden root (eval-r19b finding 5/E10: a product
    that writes REPORT.md to a scratch dir is not the loop's mailbox). URL
    and route literals, $TMPDIR and toolchain paths never contaminate."""
    export_root = _norm(Path(export).resolve())[0]
    export_roots = _norm(Path(export).resolve())
    export_roots += [r for p in allowed for r in _norm(Path(p))]
    bad_roots = sorted({r for p in forbidden for r in _norm(Path(p))})
    hits: list[str] = []

    def is_bad(path: str) -> bool:
        spellings = _norm(path)
        if any(_under(sp, export_roots) for sp in spellings):
            return False
        return any(_under(sp, bad_roots) for sp in spellings)

    def is_ancestor(path: str) -> bool:
        return any(_ancestor_of(sp, bad_roots) for sp in _norm(path))

    for entry in entries:
        for text in _audit_inputs(entry):
            bare = len(text.split()) == 1  # a tool's own path argument
            cwd: str | None = export_root
            prev: str | None = None
            for segment in _AUDIT_SEGMENT_RE.split(text):
                words = segment.strip().lstrip("({").split()
                while words and (words[0] in _AUDIT_PREFIX_WORDS
                                 or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])):
                    words = words[1:]
                cmd = os.path.basename(words[0]) if words else ""
                if cmd == "cd":
                    arg = words[1].strip("'\"") if len(words) > 1 else "~"
                    if arg == "-":
                        cwd, prev = prev, cwd
                        continue
                    path, why = _audit_expand(arg, cwd)
                    if why:
                        hits.append(f"{why}: {segment.strip()[:120]}")
                    if path is not None and (is_bad(path) or (path != "/" and is_ancestor(path))):
                        hits.append(f"cd into the loop repository or an ancestor of it: {arg}")
                    prev, cwd = cwd, path
                    continue
                recursive = cmd in _AUDIT_RECURSIVE_CMDS or (
                    cmd == "ls" and any(w.startswith("-") and "R" in w for w in words[1:]))
                reader = recursive or cmd in _AUDIT_READ_CMDS
                for token in _AUDIT_TOKEN_RE.findall(segment):
                    token = token.rstrip(".:").lstrip(":")
                    if not token or "://" in token:
                        continue
                    marked = "/" + token.strip("/") + "/"
                    if "/hidden/" in marked or "/speed/hard/" in marked:
                        hits.append(f"lab hidden-pack path: {token}")
                        continue
                    name = token.rsplit("/", 1)[-1]
                    if not ("/" in token or token.startswith(("~", "$"))
                            or name in AUDIT_MAILBOX_FILES):
                        continue
                    path, why = _audit_expand(token, cwd)
                    if why:
                        hits.append(f"{why}: {token}")
                        continue
                    if path is None:
                        continue
                    if is_bad(path):
                        hits.append(f"reads inside the loop repository: {token}")
                    elif is_ancestor(path) and (bare or (reader and (path != "/" or recursive))):
                        hits.append(f"searches an ancestor of the loop repository: {token}")
    return {"contaminated": bool(hits), "hits": hits[:40]}


def audit_output(texts: Iterable[str], forbidden: Iterable[Path]) -> dict[str, Any]:
    """The limited audit when the runtime exposes no tool-call rows: the
    authored pack is contaminated only when it spells a forbidden root (the
    loop repository or mailbox path), which the author could only have
    learned by looking outside its export. String mentions of PLAN.md or
    route literals are not evidence of a read."""
    roots = sorted({r for p in forbidden for r in _norm(Path(p)) if r not in ("/", ".")})
    hits: list[str] = []
    for text in texts:
        for root in roots:
            if root in text:
                hits.append(f"authored output names the loop repository path {root}")
    return {"contaminated": bool(hits), "hits": sorted(set(hits))[:40]}


# ------------------------------------------------------------ amendments


#: A pack path named inside a check's `run` argv (`acceptance/checks/x.py`,
#: `$ACC_DIR/fakes/y`, also inside an `sh -c` string).
_PACK_REF_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.@+/-])" + re.escape(PACK_DIR) + r"|\$\{?ACC_DIR\}?)/"
    r"([A-Za-z0-9_.@+-][A-Za-z0-9_./@+-]*)")


def run_references(check: Any) -> set[str]:
    """Pack-relative paths (files or directories) a check's `run` names."""
    out: set[str] = set()
    if not isinstance(check, dict):
        return out
    for arg in check.get("run") or []:
        if not isinstance(arg, str):
            continue
        for m in _PACK_REF_RE.finditer(arg):
            rel = os.path.normpath(m.group(1)).strip("/")
            if rel and rel != "." and not rel.startswith(".."):
                out.add(rel)
    return out


def _ref_covers(ref: str, rel: str) -> bool:
    """True when *ref* (a run reference) names *rel* or a directory holding it."""
    return rel == ref or rel.startswith(ref.rstrip("/") + "/")


def file_owners(manifests: Iterable[dict[str, Any]], rel: str) -> set[str]:
    """Ids whose `run` (in any of *manifests*) names *rel* or its directory."""
    owners: set[str] = set()
    for manifest in manifests:
        for check in manifest.get("checks") or []:
            if isinstance(check, dict) and check.get("id"):
                if any(_ref_covers(ref, rel) or _ref_covers(rel, ref)
                       for ref in run_references(check)):
                    owners.add(check["id"])
    return owners


def amendment_problems(old: dict[str, Any], new: dict[str, Any],
                       changed_files: Iterable[str], amended: Iterable[str],
                       old_files: Iterable[str] | None = None) -> list[str]:
    """Mechanical scope rule of one Evaluator amendment (§3.4 rule 1).

    eval-r19b finding 1: every changed pack file is attributed to the checks
    whose `run` names it (a file no `run` names -- a shared helper any check
    may import -- belongs to every check), and each of those checks must be
    one of the amended (named, counted) ids. An amended check's `run` may
    not be pointed at a file another check's `run` names, nor at an existing
    pack file no check names (a shared helper)."""
    probs: list[str] = []
    old_by = {c["id"]: c for c in old.get("checks") or [] if isinstance(c, dict) and "id" in c}
    new_by = {c["id"]: c for c in new.get("checks") or [] if isinstance(c, dict) and "id" in c}
    amended = set(amended)
    all_ids = set(old_by) | set(new_by)
    old_set = set(old_files) if old_files is not None else None
    removed = sorted(set(old_by) - set(new_by))
    if removed:
        probs.append(f"check(s) removed: {', '.join(removed)}")
    added = sorted(set(new_by) - set(old_by))
    if added:
        probs.append(f"check(s) added: {', '.join(added)}")
    for cid, before in old_by.items():
        after = new_by.get(cid)
        if after is None:
            continue
        for key in IMMUTABLE_KEYS:
            if before.get(key) != after.get(key):
                probs.append(f"{cid}: `{key}` is immutable")
        for key in set(before) | set(after):
            if key in IMMUTABLE_KEYS or before.get(key) == after.get(key):
                continue
            if key not in AMENDABLE_KEYS:
                probs.append(f"{cid}: `{key}` may not be amended")
            elif cid not in amended:
                probs.append(f"{cid}: changed but not named in the amend commit")
    for key in set(old) | set(new):
        if key == "checks" or old.get(key) == new.get(key):
            continue
        probs.append(f"manifest `{key}` may not be amended")
    for rel in sorted(set(changed_files)):
        if rel in (MANIFEST, AMENDMENTS):
            continue
        if not rel.startswith(("checks/", "fakes/")):
            probs.append(f"{rel} is outside the amendable pack files")
            continue
        owners = file_owners((old, new), rel)
        shared = not owners
        if shared:
            owners = set(all_ids)
        others = sorted(owners - amended)
        if others:
            why = ("is named by no check's `run` (a shared helper any check may use)"
                   if shared else "is used by")
            probs.append(f"{rel} {why} {', '.join(others[:8])}"
                         f"{' ...' if len(others) > 8 else ''}, which the amendment does not "
                         "name (every check whose files change must be amended and counted)")
    for cid in sorted(amended):
        before, after = old_by.get(cid), new_by.get(cid)
        if before is None or after is None or before.get("run") == after.get("run"):
            continue
        for ref in sorted(run_references(after)):
            others = sorted(file_owners((old,), ref) - {cid})
            if others:
                probs.append(f"{cid}: `run` now points at {ref}, which belongs to "
                             f"{', '.join(others[:8])}")
            elif old_set is not None and any(_ref_covers(ref, f) for f in old_set) \
                    and not file_owners((old,), ref):
                probs.append(f"{cid}: `run` now points at {ref}, a shared pack file "
                             "no check's `run` named")
    return probs


def amendments_logged(text: str) -> list[str]:
    """ACC ids with an `## ACC-NN · iter N · ...` record in AMENDMENTS.md."""
    return re.findall(r"^##\s+(ACC-[0-9]{1,4})\b", text, re.MULTILINE)


# ------------------------------------------------------------ driver state


def state_file(repo: Path, mailbox: Path) -> Path:
    """`$STATE/<loop-id>/acceptance.json`, outside the repo and every
    agent worktree."""
    env = os.environ.get(STATE_ENV, "").strip()
    if env:
        base = Path(env).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        base = (Path(xdg) if xdg else Path.home() / ".local" / "state") \
            / "trio-agent-loop" / "acceptance"
    top = git_toplevel(repo) or Path(repo).resolve()
    common = _git(top, "rev-parse", "--git-common-dir", check=False).stdout.strip()
    common_path = (top / common).resolve() if common else top
    try:
        rel = Path(mailbox).resolve().relative_to(top).as_posix()
    except ValueError:
        rel = Path(mailbox).name
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", rel).strip("-") or "loop"
    key = hashlib.sha256(f"{common_path}\0{rel}".encode()).hexdigest()[:12]
    return base / f"{slug}-{key}" / "acceptance.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frozen_text(pin: str, base: str, author: str, dropped: list[tuple[str, str]],
                pins: list[tuple[str, str]], frozen_utc: str | None = None,
                extra: dict[str, str] | None = None) -> str:
    lines = [f"manifest_sha256: {pin}", f"base: {base}",
             f"frozen_utc: {frozen_utc or utc_now()}", f"author: {author}",
             "dropped: " + ("; ".join(f"{i} {r}" for i, r in dropped) or "none")]
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    for n, (sha, note) in enumerate(pins):
        lines.append(f"pin[{n}]: {sha} {note}".rstrip())
    return "\n".join(lines) + "\n"


def write_frozen_pack(src_acc: Path, dst_acc: Path, manifest: dict[str, Any]) -> str:
    """Copy the authored pack into the mailbox with the kept manifest and an
    empty AMENDMENTS.md; returns its manifest_sha256 (FROZEN not yet written)."""
    dst_acc = Path(dst_acc)
    if dst_acc.exists():
        shutil.rmtree(dst_acc)
    shutil.copytree(src_acc, dst_acc, ignore=shutil.ignore_patterns(
        FROZEN, "__pycache__", "*.pyc", ".pytest_cache", "node_modules"))
    (dst_acc / MANIFEST).write_text(dump_manifest(manifest), encoding="utf-8")
    (dst_acc / AMENDMENTS).write_text("", encoding="utf-8")
    return manifest_sha256(dst_acc)


# ------------------------------------------------------- driver commits


def _git_path(top: Path, name: str) -> Path:
    raw = _git(top, "rev-parse", "--git-path", name).stdout.strip()
    path = Path(raw)
    return path if path.is_absolute() else (top / path)


def _unhashed_pathspecs(rels: list[str]) -> list[str]:
    """Exclude pathspecs for what the pin skips, so a driver commit never
    sweeps a planted node_modules/ or cache into the pack."""
    out = []
    for rel in rels:
        for name in sorted(HASH_SKIP_DIRS):
            out.append(f":(exclude,glob){rel}/**/{name}/**")
        out.append(f":(exclude,glob){rel}/**/*.py[co]")
    return out


def commit_paths(repo: Path, rels: list[str], message: str,
                 lock_timeout: float = 120.0) -> str | None:
    """Commit exactly *rels* (repo-relative; additions, edits and deletions)
    on HEAD as the driver, under git's own index.lock.

    The commit's tree is HEAD's tree plus the working-tree state of *rels*
    only -- never anything else the Lead has staged -- and the real index
    gets the same entries, so a concurrent `git add -A && git commit` in
    the same worktree can neither sweep the pack into its own commit nor
    drop it. Another git process holding the lock is waited for (bounded).
    Returns the new commit sha, or None when *rels* are unchanged.
    """
    top = git_toplevel(repo) or Path(repo).resolve()
    index = _git_path(top, "index")
    lock = index.with_name(index.name + ".lock")
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{lock} stayed locked for {lock_timeout:g}s")
            time.sleep(0.2)
    tmp_head = index.with_name(f"trio-acceptance-head.{os.getpid()}")
    tmp_real = index.with_name(f"trio-acceptance-real.{os.getpid()}")
    try:
        old = _git(top, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        env_head = dict(os.environ, GIT_INDEX_FILE=str(tmp_head))
        if old:
            _git(top, "read-tree", old, env=env_head)
        _git(top, "add", "-f", "-A", "--", *rels, *_unhashed_pathspecs(rels), env=env_head)
        tree = _git(top, "write-tree", env=env_head).stdout.strip()
        if old and tree == _git(top, "rev-parse", f"{old}^{{tree}}").stdout.strip():
            os.close(fd)
            fd = -1
            lock.unlink(missing_ok=True)
            return None
        args = ["commit-tree", tree, "-F", "-"] + (["-p", old] if old else [])
        new = subprocess.run(["git", "-C", str(top), *args], input=message,
                             capture_output=True, text=True)
        if new.returncode != 0:
            raise RuntimeError(f"git commit-tree: {new.stderr.strip()[-300:]}")
        sha = new.stdout.strip()
        subject = message.splitlines()[0] if message else "acceptance"
        update = ["update-ref", "-m", f"commit: {subject}", "HEAD", sha]
        if old:
            update.append(old)
        _git(top, *update)
        if index.exists():
            shutil.copy2(index, tmp_real)
        env_real = dict(os.environ, GIT_INDEX_FILE=str(tmp_real))
        if not index.exists():
            _git(top, "read-tree", sha, env=env_real)
        _git(top, "add", "-f", "-A", "--", *rels, *_unhashed_pathspecs(rels), env=env_real)
        data = tmp_real.read_bytes()
        os.write(fd, data)
        os.close(fd)
        fd = -1
        os.replace(lock, index)
        return sha
    finally:
        if fd != -1:
            os.close(fd)
            lock.unlink(missing_ok=True)
        tmp_head.unlink(missing_ok=True)
        tmp_real.unlink(missing_ok=True)


def restore_pack_files(repo: Path, rev: str, acc_rel: str, acc_dir: Path) -> None:
    """Replace the working-tree pack with *acc_rel* as committed in *rev*."""
    top = git_toplevel(repo) or Path(repo).resolve()
    stage = Path(tempfile.mkdtemp(prefix="trio-acceptance-restore-"))
    try:
        archive = subprocess.Popen(["git", "-C", str(top), "archive", "--format=tar", rev,
                                    "--", acc_rel], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", str(stage)], stdin=archive.stdout,
                             capture_output=True)
        archive.stdout.close()  # type: ignore[union-attr]
        if archive.wait() != 0 or tar.returncode != 0:
            raise RuntimeError(f"cannot read {acc_rel} at {rev[:12]}")
        acc_dir = Path(acc_dir)
        # Build the new pack next to the old one and swap by rename, so a
        # reader never sees a half-copied pack (eval-r19b finding 4).
        acc_dir.parent.mkdir(parents=True, exist_ok=True)
        tag = f"{os.getpid()}-{time.monotonic_ns()}"
        fresh = acc_dir.with_name(f".{acc_dir.name}.restore-{tag}")
        old = acc_dir.with_name(f".{acc_dir.name}.old-{tag}")
        shutil.copytree(stage / acc_rel, fresh, symlinks=True)
        try:
            if acc_dir.exists() or acc_dir.is_symlink():
                os.replace(acc_dir, old)
            os.replace(fresh, acc_dir)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)
            if old.is_symlink():
                old.unlink()
            else:
                shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def pack_hash_at(repo: Path, rev: str, acc_rel: str) -> str | None:
    """manifest_sha256 of the pack as committed in *rev* (git objects only)."""
    proc = _git(repo, "ls-tree", "-r", "-z", rev, "--", acc_rel + "/", check=False)
    if proc.returncode != 0:
        return None
    entries = []
    for raw in proc.stdout.split("\0"):
        if not raw.strip():
            continue
        meta, _tab, path = raw.partition("\t")
        mode, _type, blob = meta.split()
        rel = path[len(acc_rel) + 1:]
        parts = rel.split("/")
        if rel == FROZEN or any(p in HASH_SKIP_DIRS for p in parts[:-1]) \
                or rel.endswith((".pyc", ".pyo")):
            continue
        entries.append((rel, mode, blob))
    digest = hashlib.sha256()
    for rel, mode, blob in sorted(entries):
        data = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
                              capture_output=True).stdout
        if mode == "120000":
            data = b"symlink:" + data
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


# ------------------------------------------------ git-derived pin chain

FREEZE_SUBJECT_RE = re.compile(r"^acceptance: freeze \d+ checks?\b")
RESTORE_SUBJECT_RE = re.compile(r"^acceptance: restore\b")
PIN_SUBJECT_RE = re.compile(r"^acceptance: pin\b")
AMEND_SUBJECT_RE = re.compile(
    r"^acceptance: amend (ACC-[0-9]{1,4}(?:\s*,\s*ACC-[0-9]{1,4})*) "
    r"\((evaluator|human)\b[^)]*\): \S")
PIN_TRAILER = "Acceptance-Pin"
HUMAN_TRAILER = "Acceptance-Human-Amend"
#: The Evaluator amendment budget (§3.4; trio_loop uses the same numbers).
AMEND_MAX = 2
AMEND_SHARE = 0.25


def _show(repo: Path, rev: str, rel: str) -> str | None:
    proc = _git(repo, "show", f"{rev}:{rel}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def _object_ids(repo: Path, specs: list[str]) -> list[str | None]:
    """`git cat-file --batch-check` for many `<rev>:<path>` specs at once."""
    if not specs:
        return []
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check"],
                          input="\n".join(specs) + "\n", capture_output=True, text=True)
    out: list[str | None] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        out.append(parts[0] if len(parts) >= 2 and parts[-1] != "missing" else None)
    return out + [None] * (len(specs) - len(out))


def _amend_diff_problems(repo: Path, sha: str, acc_rel: str, ids: list[str]) -> list[str]:
    diff = _git(repo, "show", "--format=", "-U0", sha, "--",
                f"{acc_rel}/{AMENDMENTS}", check=False).stdout
    probs = []
    added = set(re.findall(r"^\+##\s+(ACC-[0-9]{1,4})\b", diff, re.MULTILINE))
    missing = [i for i in ids if i not in added]
    if missing:
        probs.append(f"no AMENDMENTS.md record for {', '.join(missing)}")
    if re.findall(r"^-(?!--)(.+)$", diff, re.MULTILINE):
        probs.append("AMENDMENTS.md is append-only (lines removed)")
    return probs


def base_fails_lost(repo: Path, acc_rel: str, ref_rev: str, new_rev: str,
                    base: str) -> list[str]:
    """Ids that FAIL at *base* with the pack of *ref_rev* but not with the
    pack of *new_rev* (both read from git objects; whole pack each)."""
    repo = Path(repo)
    mailbox_rel = acc_rel.rsplit("/", 1)[0] if "/" in acc_rel else ""
    exclude = {mailbox_rel} if mailbox_rel else set()
    work = Path(tempfile.mkdtemp(prefix="trio-acceptance-verify-"))
    try:
        runs = []
        for tag, rev in (("ref", ref_rev), ("new", new_rev)):
            acc = work / tag / PACK_DIR
            restore_pack_files(repo, rev, acc_rel, acc)
            runs.append(run_pack(acc, base, repo=repo, exclude=exclude))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    after = {r["id"]: r["outcome"] for r in runs[1]["results"]}
    return sorted(r["id"] for r in runs[0]["results"]
                  if r["outcome"] == "FAIL" and after.get(r["id"]) != "FAIL")


def derive_pin_chain(repo: Path, acc_rel: str, start: str | None = None, *,
                     head: str = "HEAD", driver_commits: Iterable[str] | None = None,
                     human_amends: Iterable[str] | None = None,
                     verify: bool = False) -> dict[str, Any]:
    """The pack's legitimate pin chain, from git objects alone (eval-r19b
    findings 2 and 3; never the working tree, never a subject by itself).

    Walks the first-parent chain ``start..head`` (the whole history when
    *start* is None). The freeze is the FIRST commit that adds
    ``<acc_rel>/FROZEN``; it must be a structural driver freeze (subject,
    ``Acceptance-Pin`` trailer equal to the committed pack hash, FROZEN's
    pin[0] equal to it, nothing outside the pack). After it:

    - ``acceptance: restore``: genuine when it puts back the current pin
      (trailer == committed pack == current pin), touching only the pack;
      it excuses the tamper and rejected amend commits before it.
    - ``acceptance: amend ...``: pending until a pin; structurally checked.
    - ``acceptance: pin``: genuine when it changes only FROZEN (extending
      it with one pin line), its trailer is the committed pack, and the
      pending amend commits form a legitimate amendment: either an adopted
      human amendment (an ``Acceptance-Human-Amend`` trailer naming every
      pending amend commit) or an Evaluator amendment passing
      ``amendment_problems``, its records and the loop budget.
    - anything else touching the pack (a second freeze, a FROZEN re-add, a
      slice commit) is tamper until a genuine restore.

    *driver_commits* / *human_amends* (the driver's record, when visible)
    only add strictness: a freeze/pin/restore not in it, or an adopted
    human amend not in it, is an offender. With *verify*, every Evaluator
    amendment pin is also re-checked for discrimination: the whole pack
    runs at the frozen base before and after, and every check that FAILed
    before must still FAIL (the driver's own rule, eval-r19b finding 1).

    Returns ``{"freeze_commit", "base", "pin", "anchor", "pins": [...],
    "problems": [...], "pending_tamper": [...], "pending_amends": [...],
    "head_pack", "multiple_freezes", "amend_used", "amended_ids",
    "tamper_restores", "driver_commits": [...], "human_amends": [...],
    "frozen_text"}``; ``freeze_commit`` is None when no valid freeze exists.
    """
    repo = Path(repo)
    recorded = set(driver_commits) if driver_commits is not None else None
    humans = set(human_amends) if human_amends is not None else None
    out: dict[str, Any] = {
        "freeze_commit": None, "base": None, "pin": None, "anchor": None, "pins": [],
        "problems": [], "pending_tamper": [], "pending_amends": [], "head_pack": None,
        "multiple_freezes": False, "amend_used": 0, "amended_ids": [],
        "tamper_restores": 0, "driver_commits": [], "human_amends": [], "frozen_text": None,
    }
    rng = f"{start}..{head}" if start else head
    proc = _git(repo, "rev-list", "--first-parent", "--reverse", rng, check=False)
    if proc.returncode != 0:
        out["problems"].append(f"cannot read the history {rng} ({proc.stderr.strip()[-200:]})")
        return out
    chain = [c for c in proc.stdout.split() if c]
    out["head_pack"] = pack_hash_at(repo, head, acc_rel) if chain or start else None
    first_parent = start
    if chain and start is None:
        first_parent = _git(repo, "rev-parse", "-q", "--verify", f"{chain[0]}^",
                            check=False).stdout.strip() or None
    revs = ([first_parent] if first_parent else []) + chain
    trees = _object_ids(repo, [f"{r}:{acc_rel}" for r in revs])
    frozen_ids = _object_ids(repo, [f"{r}:{acc_rel}/{FROZEN}" for r in revs])
    if not first_parent:
        trees, frozen_ids = [None] + trees, [None] + frozen_ids
    touching: list[tuple[str, str | None, bool]] = []
    for i, sha in enumerate(chain):
        prev_tree, tree = trees[i], trees[i + 1]
        if prev_tree != tree:
            touching.append((sha, first_parent if i == 0 else chain[i - 1],
                             frozen_ids[i] is None and frozen_ids[i + 1] is not None))
    if not touching:
        return out
    info: dict[str, tuple[str, str, str]] = {}
    fmt = (f"%x1e%H%x1f%s%x1f%(trailers:key={PIN_TRAILER},valueonly,separator=%x20)"
           f"%x1f%(trailers:key={HUMAN_TRAILER},valueonly,separator=%x20)")
    shas = [t[0] for t in touching]
    for i in range(0, len(shas), 200):
        log = _git(repo, "log", "--no-walk=unsorted", f"--format={fmt}", *shas[i:i + 200],
                   check=False)
        for block in log.stdout.split("\x1e"):
            if not block.strip():
                continue
            parts = (block.strip("\n").split("\x1f") + ["", "", ""])[:4]
            info[parts[0]] = (parts[1], parts[2].strip(), parts[3].strip())

    def files_of(sha: str, parent: str | None) -> list[str]:
        args = ["diff-tree", "-r", "--no-commit-id", "--name-only"]
        args += [parent, sha] if parent else ["--root", sha]
        return [f for f in _git(repo, *args, check=False).stdout.splitlines() if f.strip()]

    problems: list[str] = out["problems"]
    pending_tamper: list[str] = []
    pending_amends: list[dict[str, Any]] = []
    freeze = None
    cur_pin = None
    anchor = None
    anchor_manifest: dict[str, Any] | None = None
    anchor_files: list[str] = []
    checks_total = 1
    amend_used = 0
    amended_ids: set[str] = set()
    frozen_adds = 0
    for sha, parent, adds_frozen in touching:
        subject, pin, human = info.get(sha, ("", "", ""))
        label = f"{sha[:12]} {subject!r}"
        files = files_of(sha, parent)
        outside = [f for f in files if not f.startswith(acc_rel + "/")]
        pack_only = [f for f in files if f.startswith(acc_rel + "/")]
        frozen_adds += int(adds_frozen)
        if freeze is None:
            if not (adds_frozen or FREEZE_SUBJECT_RE.match(subject)):
                if start is not None:
                    if RESTORE_SUBJECT_RE.match(subject) or PIN_SUBJECT_RE.match(subject):
                        problems.append(f"{label}: driver acceptance commit before the freeze")
                    elif AMEND_SUBJECT_RE.match(subject):
                        problems.append(f"{label}: amend before the freeze")
                    else:
                        problems.append(f"{label}: touches {acc_rel}/ before the freeze")
                continue
            why = []
            if not FREEZE_SUBJECT_RE.match(subject):
                why.append("adds FROZEN but is not a driver freeze commit")
            if outside:
                why.append(f"touches files outside acceptance/: {', '.join(outside[:5])}")
            committed = pack_hash_at(repo, sha, acc_rel)
            if not pin or pin != committed:
                why.append("Acceptance-Pin trailer does not match the committed pack")
            text = _show(repo, sha, f"{acc_rel}/{FROZEN}") or ""
            pins0 = re.findall(r"^pin\[0\]:\s*([0-9a-f]{64})", text, re.MULTILINE)
            if not why and (not pins0 or pins0[0] != pin):
                why.append("FROZEN's pin[0] is not the committed pack")
            if recorded is not None and sha not in recorded:
                why.append("not the driver's recorded freeze commit")
            if why:
                problems.append(f"{label}: " + "; ".join(why))
                if adds_frozen:
                    # The FIRST FROZEN add is the freeze; a forged one
                    # leaves the loop without a valid freeze.
                    break
                continue
            freeze = anchor = sha
            cur_pin = pin
            out["freeze_commit"] = sha
            base_m = re.search(r"^base:\s*(\S+)", text, re.MULTILINE)
            out["base"] = base_m.group(1) if base_m else None
            out["pins"].append({"sha256": pin, "commit": sha, "note": "freeze", "kind": "freeze"})
            out["driver_commits"].append(sha)
            anchor_manifest = json.loads(_show(repo, sha, f"{acc_rel}/{MANIFEST}") or "{}")
            anchor_files = _git(repo, "ls-tree", "-r", "--name-only", sha, "--",
                                acc_rel + "/", check=False).stdout.split()
            checks_total = max(1, len(anchor_manifest.get("checks") or []))
            continue
        # ---- after the freeze
        if adds_frozen or FREEZE_SUBJECT_RE.match(subject):
            pending_tamper.append(f"{label}: a second freeze commit (the pack is frozen once "
                                  "per loop" + ("; FROZEN was re-added" if adds_frozen else "")
                                  + ")")
            continue
        m = AMEND_SUBJECT_RE.match(subject)
        if m:
            ids = [i.strip() for i in m.group(1).split(",")]
            why = []
            if outside:
                why.append(f"an amend commit may touch only acceptance/ (also: {', '.join(outside[:5])})")
            why += _amend_diff_problems(repo, sha, acc_rel, ids)
            if why:
                problems.append(f"{label}: " + "; ".join(why))
            pending_amends.append({"sha": sha, "ids": ids, "who": m.group(2), "label": label})
            continue
        if RESTORE_SUBJECT_RE.match(subject) or PIN_SUBJECT_RE.match(subject):
            kind = "restore" if RESTORE_SUBJECT_RE.match(subject) else "pin"
            why = []
            if recorded is not None and sha not in recorded:
                why.append("not an acceptance commit the driver recorded (a subject alone is "
                           "not a driver commit)")
            if outside:
                why.append(f"touches files outside acceptance/: {', '.join(outside[:5])}")
            committed = pack_hash_at(repo, sha, acc_rel)
            if not pin or pin != committed:
                why.append("Acceptance-Pin trailer does not match the committed pack")
            elif kind == "restore":
                if pin != cur_pin:
                    why.append("restores a pack that was never pinned"
                               if pin not in {p["sha256"] for p in out["pins"]}
                               else "restores a superseded pin")
            else:
                extra = [f for f in pack_only if f != f"{acc_rel}/{FROZEN}"]
                if extra:
                    why.append(f"a pin commit may only extend {acc_rel}/FROZEN "
                               f"(also: {', '.join(extra[:5])})")
                elif not pending_amends:
                    why.append("a pin commit with no amend commit to pin")
                elif pending_tamper:
                    why.append("pins over a pack edit that is not an amend commit")
                else:
                    before = _show(repo, anchor, f"{acc_rel}/{FROZEN}") or ""
                    after = _show(repo, sha, f"{acc_rel}/{FROZEN}") or ""
                    new_lines = after[len(before.rstrip("\n")):].strip().splitlines() \
                        if after.startswith(before.rstrip("\n")) else None
                    if new_lines is None or len(new_lines) != 1 \
                            or not re.match(rf"^pin\[\d+\]:\s*{pin}\b", new_lines[0]):
                        why.append(f"a pin commit must extend {acc_rel}/FROZEN by exactly its "
                                   "own pin line")
            if not why and kind == "pin":
                amend_shas = [a["sha"] for a in pending_amends]
                listed = [s for s in re.split(r"[\s,]+", human) if s]
                ids = sorted({i for a in pending_amends for i in a["ids"]})
                if listed:
                    if sorted(set(listed)) != sorted(set(amend_shas)):
                        why.append(f"its {HUMAN_TRAILER} trailer does not name exactly the "
                                   "amend commits it pins")
                    elif humans is not None and [s for s in listed if s not in humans]:
                        why.append("a human adoption the driver state does not record")
                    else:
                        out["pins"].append({"sha256": pin, "commit": sha, "kind": "human",
                                            "note": f"amend {', '.join(ids)} (human)",
                                            "amends": amend_shas})
                        out["human_amends"].extend(amend_shas)
                else:
                    new_manifest = json.loads(_show(repo, sha, f"{acc_rel}/{MANIFEST}") or "{}")
                    # What the amend commits changed (the pin commit itself
                    # only extends FROZEN).
                    changed = [f[len(acc_rel) + 1:] for f in _git(
                        repo, "diff", "--name-only", anchor, parent or sha, "--", acc_rel,
                        check=False).stdout.split()]
                    old_rel = [f[len(acc_rel) + 1:] for f in anchor_files]
                    why += amendment_problems(anchor_manifest or {}, new_manifest, changed, ids,
                                              old_rel)
                    if amend_used + len(ids) > AMEND_MAX \
                            or amend_used + len(ids) > AMEND_SHARE * checks_total:
                        why.append(f"amendment budget exceeded ({amend_used} used + {len(ids)})")
                    if not why and verify:
                        if not out["base"]:
                            why.append("the freeze names no base to re-check the amendment at")
                        else:
                            try:
                                lost = base_fails_lost(repo, acc_rel, anchor, sha, out["base"])
                            except (OSError, RuntimeError, ManifestError) as exc:
                                lost = None
                                why.append(f"cannot re-check the amendment at base ({exc})")
                            if lost:
                                why.append(f"{', '.join(lost)} no longer FAIL at base")
                    if not why:
                        amend_used += len(ids)
                        amended_ids |= set(ids)
                        out["pins"].append({"sha256": pin, "commit": sha, "kind": "amend",
                                            "note": f"amend {', '.join(ids)}",
                                            "amends": amend_shas})
            if why:
                pending_tamper.append(f"{label}: " + "; ".join(why))
                continue
            out["driver_commits"].append(sha)
            if kind == "restore":
                if "(tamper" in subject:
                    out["tamper_restores"] += 1
                pending_tamper.clear()
                pending_amends.clear()
            else:
                cur_pin = pin
                pending_amends.clear()
                anchor_manifest = json.loads(_show(repo, sha, f"{acc_rel}/{MANIFEST}") or "{}")
                anchor_files = _git(repo, "ls-tree", "-r", "--name-only", sha, "--",
                                    acc_rel + "/", check=False).stdout.split()
                if out["pins"][-1]["kind"] == "human":
                    checks_total = max(1, len(anchor_manifest.get("checks") or []))
            anchor = sha
            continue
        pending_tamper.append(f"{label}: touches {acc_rel}/ ({', '.join(pack_only[:3])}) -- "
                              "only the driver freeze/restore and amend commits may")
    out["multiple_freezes"] = frozen_adds > 1
    out["pin"] = cur_pin
    out["anchor"] = anchor
    out["amend_used"] = amend_used
    out["amended_ids"] = sorted(amended_ids)
    out["pending_tamper"] = pending_tamper
    out["pending_amends"] = pending_amends
    if anchor is not None:
        out["frozen_text"] = _show(repo, anchor, f"{acc_rel}/{FROZEN}")
    return out


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------ CLI


def _cmd_run(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    acc = mailbox / PACK_DIR
    repo = git_toplevel(mailbox)
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()] or None
    exclude: set[str] = set()
    if repo is not None:
        try:
            exclude.add(mailbox.relative_to(repo).as_posix())
        except ValueError:
            pass
    tree = args.tree
    if Path(tree).is_dir():
        tree_path = Path(tree).resolve()
        try:
            exclude = {mailbox.relative_to(tree_path).as_posix()}
        except ValueError:
            exclude = set()
    try:
        result = run_pack(acc, tree, repo=repo, ids=ids, exclude=exclude,
                          plan_bindings=_plan_bindings(mailbox))
    except (ManifestError, RuntimeError) as exc:
        print(f"trio-acceptance: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)
    print(f"trio-acceptance: {summary_line(result)} (sandbox {result['sandbox']})",
          file=sys.stderr)
    return run_exit_code(result)


def _cmd_validate(args: argparse.Namespace) -> int:
    export = Path(args.export).resolve()
    acc = export / PACK_DIR
    goal_path = export / INPUT_DIR / "GOAL.md"
    notes_path = export / INPUT_DIR / "ACCEPTANCE-NOTES.md"
    goal = goal_path.read_text(encoding="utf-8") if goal_path.is_file() else None
    notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else None
    try:
        manifest = load_manifest(acc)
    except ManifestError as exc:
        print(f"INVALID: {exc}")
        return 1
    try:
        result = run_pack(acc, export, exclude={PACK_DIR, INPUT_DIR}, manifest=manifest)
    except (ManifestError, RuntimeError) as exc:
        print(f"INVALID: {exc}")
        return 1
    filtered = freeze_filter(manifest, result, goal, notes, acc)
    for err in filtered["fatal"]:
        print(f"INVALID pack: {err}")
    for cid, reason in filtered["dropped"]:
        print(f"WOULD DROP {cid}: {reason}")
    for cid in filtered["unavailable_at_base"]:
        print(f"UNAVAILABLE at base {cid} (kept; resolves to NEEDS_HUMAN unless its needs are met)")
    kept = len(filtered["manifest"]["checks"])
    print(f"base run: {summary_line(result)}; would freeze {kept} check(s)"
          + (" -- the driver would ask for one retry" if filtered["retry"] else ""))
    return 1 if (filtered["fatal"] or filtered["dropped"] or filtered["retry"]) else 0


def _cmd_hash(args: argparse.Namespace) -> int:
    print(manifest_sha256(Path(args.mailbox).resolve() / PACK_DIR))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    got = manifest_sha256(Path(args.mailbox).resolve() / PACK_DIR)
    if got == args.pin.strip().lower():
        print(f"pin ok {got}")
        return 0
    print(f"pin MISMATCH: pack {got} != pinned {args.pin}")
    return 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trio-acceptance.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the frozen pack against a tree")
    r.add_argument("--mailbox", required=True)
    r.add_argument("--tree", required=True, help="a directory, or a revision of the mailbox repo")
    r.add_argument("--ids", help="comma-separated check ids")
    r.add_argument("--out", help="write the JSON result here")
    r.add_argument("--json", action="store_true", help="also print the JSON with --out")
    v = sub.add_parser("validate", help="schema + base run of an author export")
    v.add_argument("--export", required=True)
    h = sub.add_parser("hash", help="print the pack's manifest_sha256")
    h.add_argument("--mailbox", required=True)
    f = sub.add_parser("verify", help="compare the pack hash with a pin")
    f.add_argument("--mailbox", required=True)
    f.add_argument("--pin", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return {"run": _cmd_run, "validate": _cmd_validate, "hash": _cmd_hash,
            "verify": _cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
