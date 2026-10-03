"""The trio-opencode state machine — mirrors ``native/trio-native.js``'s
sequencing (begin -> loop{next -> lead pass | repair -> gate -> pin ->
evaluator -> apply} -> end) but drives ``opencode run`` subprocesses through
``runner.run_turn`` instead of a Claude Code Workflow's ``agent()``.

Kept importable with no other trio_opencode submodule under active
development required at import time: ``runner``/``ocgen`` (owned by other
slices) are imported lazily, inside the functions that need them, so unit
tests of the pieces this module owns (turn-result bookkeeping, wave
sequencing glue, JSON-block parsing, ``.driver.json``/registry shape) do not
require them to exist.

This is the state-machine slice; a fake-``opencode`` integration test of a
full run is a later slice (per the shared spec). Functions are kept small
and mostly pure (git/state side effects go through ``steplib``) so they are
individually testable once that fake exists.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trio_opencode import prompts, rootfree, steplib, waves

HARNESS = "opencode"
DRIVER_FILE = ".driver.json"
RESULT_FILE = ".opencode-result.json"
BUILDER_WORKTREES_DIR = ".trio-opencode/worktrees"
AGENTS = {"lead": "trio-lead", "evaluator": "trio-evaluator",
          "builder": "trio-builder", "repair": "trio-repair",
          "acceptance": "trio-acceptance"}
#: Turn kinds that stop the run immediately, never retried (mirrors the
#: native driver's "role denial handled in-role, gate then stops" plus the
#: OpenCode-specific "config_error" — unknown model/provider, auth missing).
STOP_KINDS = {"permission", "config_error"}
#: A run-ending helper ``code`` (from ``next``/``apply``) mapped to a status
#: word, used only when the helper result itself carries no usable ``status``
#: field (REVIEW-driver.md item 1). ``8`` (needs_land) is never produced by
#: the native helper itself — it is this driver's own root-free land phase.
_CODE_STATUS = {0: "shipped", 2: "blocked", 3: "error", 4: "max_iterations",
               5: "needs_human", 6: "needs_retirement", 8: "needs_land"}

_JSON_FENCE_RE = re.compile(r"```(?:json|JSON|jsonc)?\s*\n(.*?)```", re.DOTALL)

#: STATE.md keys the driver's own helper (``next``/``dispatch``'s
#: ``_lead_running`` gate) owns the cursor for — a lead/repair/evaluator turn
#: that writes one of these (mistaking it for a role-writable line such as
#: `status`, `frozen:` or the rejected-approaches list) corrupts the next
#: helper call's gate (bug 1). ``metrics/trio_loop.py``'s ``_update_state``
#: "Replace owned keys" comment names the same set; ``native``'s lockstep core
#: re-asserts them after every role turn for the same reason.
OWNED_STATE_KEYS = ("iteration", "phase", "evaluated_sha", "evaluator_attempt",
                   "evaluated_repos")

#: `next()`'s own phases while STATE `status` is `running` (REVIEW-driver.md
#: bug 1 item 2): a `status: running` mailbox in any other phase can only be
#: the crash window between a role turn writing a bogus `phase` and
#: `_call_role`'s `finally` restoring it — `idle` additionally covers the
#: moment just after `begin`, before the first `next()` has run at all.
_RESUME_OK_PHASES = {"idle", "lead-running", "repair-running", "lead-done"}


def _owned_state_snapshot(path: Path) -> dict[str, str] | None:
    """The current value of every :data:`OWNED_STATE_KEYS` line in the
    STATE.md at ``path``, or ``None`` when it cannot be read (best effort:
    an unreadable/missing STATE.md must never raise from this guard)."""
    try:
        state = steplib.TL._read_state(path)
    except Exception:  # noqa: BLE001 - best effort guard
        return None
    if not isinstance(state, dict):
        return None
    return {k: state.get(k, "") for k in OWNED_STATE_KEYS}


def _restore_owned_state(ctx: "RunContext", before: dict[str, str] | None, path: Path,
                         label: str) -> None:
    """Bug 1: put back any :data:`OWNED_STATE_KEYS` value a role turn
    changed, leaving ``status``, ``frozen:`` and every other line exactly as
    the turn left them. Best effort — never raises."""
    if before is None:
        return
    try:
        after = steplib.TL._read_state(path)
        if not isinstance(after, dict):
            return
        changed = {k: before[k] for k in OWNED_STATE_KEYS
                  if after.get(k, "") != before[k]}
        if not changed:
            return
        steplib.TL._update_state(path, changed)
        desc = ", ".join(f"{k} {after.get(k, '')!r} -> {v!r}" for k, v in changed.items())
        line = (f"- iter {before.get('iteration', '?')} | loop | restored driver-owned "
               f"STATE key(s) after {label}: {desc}")
        steplib.TL._append_log(ctx.live_mailbox, line)
        ctx.out(line)
    except Exception:  # noqa: BLE001 - best effort guard
        pass


class DriverStop(Exception):
    """Raised to unwind straight to ``end`` with a final result."""

    def __init__(self, status: str, code: int, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.status = status
        self.code = code
        self.reason = reason
        self.extra = extra


# ------------------------------------------------------------------ utils
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        os.write(fd, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def extract_json_block(text: str) -> dict | None:
    """The last fenced ```json/```jsonc/bare ``` block in ``text`` that
    parses as a JSON object; ``None`` when there is none."""
    blocks = _JSON_FENCE_RE.findall(text or "")
    for block in reversed(blocks):
        try:
            obj = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def collect_denials(text: str, denials: list[str] | None = None) -> list[str]:
    found = list(denials or [])
    for line in (text or "").splitlines():
        m = re.match(r"^\s*[-*]?\s*DENIED:\s*(.+)$", line)
        if m:
            found.append(m.group(1).strip())
    return found


def _get_runner():
    from trio_opencode import runner  # noqa: PLC0415 - lazy, another slice
    return runner


def _get_ocgen():
    from trio_opencode import ocgen  # noqa: PLC0415 - lazy, another slice
    return ocgen


def _env_paths(name: str) -> list[str]:
    """Extra author-sandbox mounts a site needs (a toolchain installed in an
    unusual place), as an ``os.pathsep``-separated env var."""
    return [p for p in os.environ.get(name, "").split(os.pathsep) if p.startswith("/")]


def _get_authorbox():
    from trio_opencode import authorbox  # noqa: PLC0415 - lazy
    return authorbox


def _get_config_mod():
    from trio_opencode import config as config_mod  # noqa: PLC0415 - lazy
    return config_mod


def _get_openloop():
    from trio_opencode import openloop  # noqa: PLC0415 - lazy, another slice
    return openloop


def _make_turn_spec(runner_mod, **kwargs: Any):
    """Build ``runner_mod.TurnSpec`` from whichever of ``kwargs`` it
    actually declares, so this module tolerates fields runner.py adds or
    has not added yet (e.g. a ``key_file`` passthrough)."""
    valid = {f.name for f in dataclasses.fields(runner_mod.TurnSpec)}
    return runner_mod.TurnSpec(**{k: v for k, v in kwargs.items() if k in valid})


def _variant_for(cfg: Any, role: str, model: str | None) -> str | None:
    """The configured ``variants.<role>`` for this turn, passed to the
    runner so v2 gets ``-m provider/model#variant`` (previously the variant
    only reached the v1 agent frontmatter and was silently dropped under
    v2). Only applied when the turn uses the role's configured model.

    ``role == "acceptance"`` is a special case (README.md:
    ``variants.acceptance`` is "informational only ... nothing reads this
    value for dispatch"; the acceptance tier is authored by the Lead turn):
    the LEAD's variant is used to dispatch the author turn, never
    ``variants.acceptance`` itself (``config.validate()`` already requires
    the two to agree when ``variants.acceptance`` is set, so this is never a
    silent divergence)."""
    getter = getattr(cfg, "variant_for", None)
    if getter is None:
        return None
    lookup_role = "lead" if role == "acceptance" else role
    try:
        variant = getter(lookup_role)
    except Exception:  # noqa: BLE001 - tolerate test namespaces
        return None
    if not variant or (model and _model_for(cfg, role) not in (None, model)):
        return None
    return variant


def _model_for(cfg: Any, role: str) -> str | None:
    """``cfg.model_for(role)`` when available (``config.Config``'s real
    method — ``models`` is a plain ``{role: model_id}`` dict, not an object
    with attributes); falls back to dict/attribute lookups so a bare
    namespace works in tests that do not construct a full ``Config``."""
    if hasattr(cfg, "model_for"):
        try:
            return cfg.model_for(role)
        except KeyError:
            return None
    models = getattr(cfg, "models", None)
    if isinstance(models, dict):
        return models.get(role)
    return getattr(models, role, None) if models else None


def _turn_timeout_for(cfg: Any, role: str) -> float | None:
    if hasattr(cfg, "turn_timeout_for"):
        return cfg.turn_timeout_for(role)
    timeouts = getattr(cfg, "timeouts", None)
    if timeouts is None:
        return None
    if role == "evaluator":
        return getattr(timeouts, "evaluator_turn_seconds", None)
    return getattr(timeouts, "turn_seconds", None)


def _idle_timeout_for(cfg: Any) -> float | None:
    timeouts = getattr(cfg, "timeouts", None)
    return getattr(timeouts, "idle_seconds", None) if timeouts else None


def _retries_for(cfg: Any) -> tuple[int, tuple[float, ...], bool]:
    """``cfg.retries``'s ``max_attempts``/``backoff_seconds``/
    ``idle_retry_unlimited`` -> the runner's ``TurnSpec`` fields of the same
    meaning; a config-less/partial ``cfg`` (some unit tests) falls back to
    the runner's own dataclass defaults rather than silently using
    different numbers here."""
    retries = getattr(cfg, "retries", None)
    max_attempts = getattr(retries, "max_attempts", None) if retries else None
    backoff = getattr(retries, "backoff_seconds", None) if retries else None
    idle_retry_unlimited = bool(getattr(retries, "idle_retry_unlimited", False)) if retries else False
    return (max_attempts if max_attempts is not None else 3,
           tuple(backoff) if backoff else (10.0, 30.0, 90.0),
           idle_retry_unlimited)


def _key_file_for(cfg: Any) -> str | None:
    provider = getattr(cfg, "provider", None)
    return getattr(provider, "key_file", None) if provider else None


def _mailbox_key(mailbox: Path) -> str:
    return hashlib.sha256(str(Path(mailbox).resolve()).encode()).hexdigest()


def registry_path(root_mailbox: Path) -> Path:
    base = os.environ.get("TRIO_OPENCODE_RUNS_DIR", "").strip()
    runs_dir = (Path(base).expanduser() if base else
                Path.home() / ".local" / "share" / "trio-agent-loop" / "opencode-runs")
    return runs_dir / f"{_mailbox_key(root_mailbox)[:16]}.json"


def recorded_acceptance(root_mailbox: str | Path) -> bool:
    """The r19 frozen-acceptance switch this mailbox's run was STARTED with
    (docs/FROZEN-ACCEPTANCE.md requirement 1: the switch is a start-time
    decision that holds for every resume of the same run). Checked, in
    order: the run registry (written at ``begin``, before anything else),
    then the live mailbox's own ``.opencode-result.json`` (a finished run —
    resuming it starts a fresh run anyway, but the value should still carry
    over), then ``.driver.json`` (a crashed run's own record, read before
    the registry write on a very old crash). Defaults to ``False`` (a
    mailbox from before this switch existed, or one never carrying the key)."""
    root_mailbox = Path(root_mailbox).resolve()
    reg = _read_json(registry_path(root_mailbox))
    if isinstance(reg.get("acceptance_enabled"), bool):
        return reg["acceptance_enabled"]
    result = _read_json(root_mailbox / RESULT_FILE)
    acc = result.get("acceptance") if isinstance(result, dict) else None
    if isinstance(acc, dict) and isinstance(acc.get("enabled"), bool):
        return acc["enabled"]
    dj = _read_json(root_mailbox / DRIVER_FILE)
    if isinstance(dj.get("acceptance_enabled"), bool):
        return dj["acceptance_enabled"]
    return False


def _status_for_code(code: Any) -> str:
    return _CODE_STATUS.get(code, "error")


def _final_status(result: dict) -> str:
    """REVIEW-driver.md item 1: the helper's own ``status`` (from ``next``'s
    stop action, or ``apply``'s ``**_snapshot(...)``) is the authority — it
    already carries the exact STATE.md status word (``shipped``,
    ``blocked``, ``needs_human``, ``needs_retirement``, ``max_iterations``,
    ...); the code map is only a defensive fallback for a result that
    somehow lacks it."""
    status = result.get("status")
    if isinstance(status, str) and status:
        return status
    return _status_for_code(result.get("code"))


def _log_reclaimed(ctx: "RunContext", reclaimed: dict | None) -> None:
    """``logReclaimed`` (native): progress-log-only (LOG.md already got its
    own ``| loop |`` lines from the helper's ``_reclaim_builders`` itself)."""
    if not isinstance(reclaimed, dict):
        return
    for x in reclaimed.get("merged") or []:
        ctx.out(f"previous-run builder {x.get('branch')}@{str(x.get('tip'))[:12]} "
               f"({x.get('id')}) merged into HEAD")
    for x in reclaimed.get("removed") or []:
        ctx.out(f"previous-run builder {x.get('branch')} already merged: worktree removed")
    for x in reclaimed.get("discarded") or []:
        ctx.out(f"previous-run builder {x.get('branch')}@{x.get('tip')} discarded "
               f"({x.get('reason') or 'not reusable'})")
    for x in reclaimed.get("kept") or []:
        ctx.out(f"previous-run builder {x.get('branch')} kept: {x.get('reason')}")


def _log_human_notes(ctx: "RunContext", human_notes: Any) -> None:
    for note in human_notes or []:
        ctx.out(f"HUMAN.md: {note}")


def _driver_lock_path(repo: Path, root_mailbox: Path) -> Path:
    """REVIEW-driver.md item 7: one ``fcntl.flock`` per mailbox, held for the
    whole run, acquired before anything else is touched (before
    ``rootfree.prepare``/``ocgen.generate``) — a git worktree shares its
    parent repo's git-common-dir, so this path is identical whether computed
    from the root mailbox's checkout or its (not-yet-created) Lead worktree."""
    common_dir = Path(steplib.TL._git(repo, "rev-parse", "--path-format=absolute",
                                      "--git-common-dir").stdout.strip())
    return common_dir / "trio-opencode" / _mailbox_key(root_mailbox)[:16] / "driver.lock"


def _driver_json_paths(root_mailbox: Path, repo: Path, root_free: bool) -> list[Path]:
    """REVIEW-driver.md item 7: ``resume`` must find a dead run's orphan turn
    process via *either* ``.driver.json`` location — the root mailbox (an
    in-place run, or a root-free run that crashed before ``prepare``) and the
    live mailbox inside the Lead worktree (the common root-free case)."""
    paths = [root_mailbox / DRIVER_FILE]
    if not root_free:
        return paths
    try:
        mrel = rootfree.mailbox_rel(repo, root_mailbox)
        rec = rootfree.load_record(repo, rootfree.loop_slug(mrel))
    except rootfree.RootFreeError:
        rec = None
    if rec is not None:
        paths.append(Path(rec.path) / rec.mailbox_rel / DRIVER_FILE)
    return paths


# ------------------------------------------------------------- run context
@dataclasses.dataclass(frozen=True)
class AuthorSetup:
    """One acceptance-author turn's containment (see ``RunContext.author_setup``)."""
    level: str                      # "sandbox" | "no-shell" | "none" (no isolation context)
    env: dict[str, str]             # env overrides for the turn
    argv_prefix: tuple[str, ...]    # e.g. the bwrap wrapper; () when none
    shell: bool                     # may the author run commands at all
    note: str


def _seed_author_cache(dest: Path, src: "str | None") -> str:
    """``dest`` (created), filled once from ``src`` when that exists and is
    small. Best effort: a cold cache only costs OpenCode a refetch."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if src and Path(src).is_dir() and not any(dest.iterdir()):
            total = sum(f.stat().st_size for f in Path(src).rglob("*") if f.is_file())
            if total <= 256 * 1024 * 1024:
                shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
    except (OSError, shutil.Error):
        pass
    return str(dest)


class RunContext:
    """Everything one ``run()`` call threads through the pass functions."""

    def __init__(self, *, root_mailbox: Path, live_mailbox: Path, repo: Path,
                cfg: Any, token: str, exec_id: str, run_dir: Path,
                log_dir: Path, env: dict[str, str], root_free: bool,
                lead_record: "rootfree.LeadWorktree | None",
                out: Callable[[str], None], cancel: threading.Event,
                cancel_code: dict | None = None,
                acceptance: bool = False) -> None:
        self.root_mailbox = root_mailbox
        self.live_mailbox = live_mailbox
        self.repo = repo
        self.cfg = cfg
        self.token = token
        self.exec_id = exec_id
        self.run_dir = run_dir
        self.log_dir = log_dir
        self.env = env
        self.root_free = root_free
        self.lead_record = lead_record
        self.out = out
        self.cancel = cancel
        #: Shared with ``run()``'s signal handler (``stop_now``, same dict):
        #: ``{"code": 143}`` once a SIGTERM was received, ``{"code": 130}``
        #: for SIGINT or no signal at all (the default) — the exit code a
        #: cancelled ``DriverStop`` carries follows which signal actually
        #: asked for the stop.
        self.cancel_code = cancel_code if cancel_code is not None else {"code": 130}
        self.role_denials: list[dict[str, str]] = []
        #: Per-``next()``-action records (REVIEW-driver.md item 11), mirroring
        #: native's ``iterations[]``: one dict per lead/repair pass, filled in
        #: by ``_run_lead_pass``/``_run_role_with_gate``.
        self.iterations: list[dict[str, Any]] = []
        self.tmpdir: str | None = None
        self.driver_json_path = live_mailbox / DRIVER_FILE
        #: Every LIVE turn, keyed by label — builders run concurrently
        #: (ThreadPoolExecutor, one wave), so this is a dict, not a single
        #: "current turn" (mirrors native's single-turn field only in spirit:
        #: `resume` must be able to kill every one of them, not just the
        #: last one recorded). A turn is removed the moment it ends (success,
        #: failure, or a stop-kind) — never left stale for `resume` to find.
        self.turns: dict[str, dict[str, Any]] = {}
        self._turns_lock = threading.Lock()
        #: H8: the ONE lock every ``.driver.json`` writer (this object's own
        #: `write_driver_json`, and open-loop's `_make_sidecar_writer`,
        #: which replaces ``TL._write_open_loop_sidecars`` for the run and
        #: writes the SAME file from the Lead thread) serializes on, so the
        #: last write to land always carries both writers' keys instead of
        #: two concurrent writers racing `_atomic_write_json` and whichever
        #: finishes last silently dropping the other's fields. Always
        #: uncontended for a lockstep run (only `write_driver_json` itself
        #: ever writes this file there).
        self.driver_json_lock = threading.Lock()
        #: `_call_role`'s pre-turn :data:`OWNED_STATE_KEYS` snapshot for the
        #: guarded turn currently in flight (``None`` otherwise), persisted
        #: into ``.driver.json`` so a crash mid-turn leaves `resume` a record
        #: to restore from — `_normalize_resume_phase`'s own rewrite is only
        #: the fallback for a ``.driver.json`` with no snapshot at all.
        self.state_snapshot: dict[str, str] | None = None
        self._last_phase = "begin"
        self._last_iteration = 0
        # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): `acceptance` is
        # the resolved switch for this run (fixed at `run()`'s call);
        # `acc` is the script's held digest (native's `ACC` global, mirrored
        # here since RunContext is this driver's one long-lived object
        # across the whole run, same role); `acc_log` mirrors native's
        # `accLog` (coverage_refusals/replanned/ship_refused/author_attempts,
        # folded into the final result's `acceptance` summary).
        self.acceptance = bool(acceptance)
        self.acc: dict[str, Any] | None = None
        self.acc_log: dict[str, Any] = {
            "coverage_refusals": [], "replanned": False,
            "ship_refused": [], "author_attempts": 0,
        }
        #: Open-loop only (D8): extra ``.driver.json`` keys the open-loop
        #: sidecar writer wants preserved (``open_loop``, ``session_ids``,
        #: ``evaluator_sessions``, ``builders``, ``lint``, ``quality``, ...)
        #: when THIS object's own `write_driver_json` (a role turn's
        #: `on_spawn`/`on_turn_end`) writes in between two sidecar writes.
        #: Always ``{}`` for a lockstep run.
        self.driver_extra: dict[str, Any] = {}
        #: CLI style ``run()`` detected and the root the agent bodies live
        #: under -- what ``ocgen.generate_author_env`` needs to write the
        #: acceptance author's own, narrower per-turn config.
        self.cli_style: str = "v2"
        self.agents_root: Path | None = None
        #: Isolation level of the latest author turn (``author_setup``);
        #: ``sandbox`` makes the tool-call audit moot (see
        #: :func:`audit_rows_for_level`).
        self.author_level: str | None = None

    # -- acceptance author isolation -----------------------------------
    def author_forbidden_roots(self) -> list[Path]:
        """Roots whose contents the acceptance author must never read: the
        loop repository (every declared repo, the root-free Lead worktree),
        its git dir and main checkout, and the mailboxes -- the same set the
        post-hoc audit (``AcceptanceController._audit_forbidden``) judges
        against, plus the run's own dir."""
        roots: list[Path] = [self.repo, self.live_mailbox, self.root_mailbox, self.run_dir]
        rec = self.lead_record
        if rec is not None:
            roots.append(Path(rec.path))
            for info in (rec.repos or {}).values():
                for key in ("path", "main"):
                    if isinstance(info, dict) and info.get(key):
                        roots.append(Path(info[key]))
        for base in list(roots[:1]):
            try:
                out = subprocess.run(["git", "-C", str(base), "rev-parse", "--git-common-dir"],
                                     capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0 and out.stdout.strip():
                git_dir = (Path(base) / out.stdout.strip()).resolve()
                roots.append(git_dir)
                if git_dir.name == ".git":
                    roots.append(git_dir.parent)
        seen: dict[str, Path] = {}
        for r in roots:
            seen.setdefault(str(r), Path(r))
        return list(seen.values())

    def author_setup(self, export: Path, tool: str | None, *,
                     iteration: Any = "?") -> "AuthorSetup":
        """How the next acceptance-author turn is contained
        (``trio_opencode/authorbox.py``): the per-turn permission config
        (``ocgen.AuthorIsolation``), a private scratch area OUTSIDE the
        repository (``TMPDIR``, OpenCode's data/state dirs -- where it saves
        a truncated tool output, a path the model is told to read back; the
        run's own dirs live under the repo's git dir), and the isolation
        level: ``sandbox`` (the opencode process runs under bwrap, only the
        export visible, a shell is safe) when a probe shows bwrap really
        starts here, else ``no-shell`` (no shell, path-checked file tools).
        Symlinks leaving the export are removed first. The level is logged
        loudly once per run. One stable scratch dir per run, so a same-session
        re-prompt finds its session again. A context built by a test (no
        ``agents_root``) gets an empty, shell-allowed setup."""
        if self.agents_root is None:
            return AuthorSetup(level="none", env={}, argv_prefix=(), shell=True, note="")
        ocgen, authorbox = _get_ocgen(), _get_authorbox()
        export = Path(export)
        removed = authorbox.sanitize_export(export)
        scratch = export.parent / f"author-{self.exec_id[:8]}"
        #: The author's own OpenCode config + XDG config home. A sibling of
        #: the scratch dir, NOT under the run dir (a forbidden root the
        #: sandbox never mounts: ``Agent not found``) and NOT inside the
        #: scratch dir (the author may write there; the sandbox mounts this
        #: one read-only and the permission rules never allow it).
        cfg_root = export.parent / f"author-cfg-{self.exec_id[:8]}"
        env: dict[str, str] = {}
        have_scratch = True
        try:
            for name in ("tmp", "xdg-data", "xdg-state", "home"):
                (scratch / name).mkdir(parents=True, exist_ok=True)
            for name in ("opencode", "xdg-config"):
                (cfg_root / name).mkdir(parents=True, exist_ok=True)
        except OSError:
            have_scratch = False
        if have_scratch:
            env.update({
                "TMPDIR": str(scratch / "tmp"), "TMP": str(scratch / "tmp"),
                "TEMP": str(scratch / "tmp"), "HOME": str(scratch / "home"),
                "XDG_DATA_HOME": str(scratch / "xdg-data"),
                "XDG_STATE_HOME": str(scratch / "xdg-state"),
                "XDG_CONFIG_HOME": str(cfg_root / "xdg-config"),
            })
        if not have_scratch:
            usable, detail = False, "no scratch dir"
        elif os.environ.get("TRIO_OPENCODE_AUTHOR_ISOLATION", "auto").strip().lower() == "no-shell":
            usable, detail = False, "forced by TRIO_OPENCODE_AUTHOR_ISOLATION=no-shell"
        else:
            usable, detail = authorbox.bwrap_usable()
        level = ocgen.LEVEL_SANDBOX if usable else ocgen.LEVEL_NO_SHELL
        forbidden = [str(r) for r in self.author_forbidden_roots()]
        iso = ocgen.AuthorIsolation(
            export=str(export), forbidden=tuple(forbidden),
            allow_dirs=(str(scratch),) if have_scratch else (), tool=tool, level=level)
        env.update(ocgen.generate_author_env(
            self.run_dir, self.cfg, self.agents_root, isolation=iso, style=self.cli_style,
            oc_dir=(cfg_root / "opencode") if have_scratch else None))
        if level == ocgen.LEVEL_NO_SHELL:
            # a git ancestor of the export would become OpenCode's project
            # root (grep/glob ``path: ../..`` then counts as inside it); the
            # caller removes this guard after the turn (``release_author``)
            authorbox.ensure_project_root(export)
        prefix: tuple[str, ...] = ()
        if level == ocgen.LEVEL_SANDBOX:
            if have_scratch:
                # The run's own cache lives under the run dir, a forbidden root
                # (mounting it would make the repo's path exist in the sandbox).
                # The author gets a cache beside its scratch, seeded once with
                # the run's (OpenCode's models catalog), and has no need of more.
                env["XDG_CACHE_HOME"] = _seed_author_cache(
                    scratch / "xdg-cache", self.turn_env().get("XDG_CACHE_HOME"))
            turn_env = {**self.turn_env(), **env}
            prefix = tuple(authorbox.sandbox_prefix(
                export=export, scratch=scratch,
                config_dir=cfg_root if have_scratch else env["OPENCODE_CONFIG_DIR"],
                env=turn_env, opencode_bin=getattr(self.cfg, "opencode_bin", "opencode"),
                forbidden=forbidden,
                extra_ro=([os.path.dirname(tool)] if tool and os.path.isabs(tool) else [])
                + _env_paths("TRIO_OPENCODE_AUTHOR_SANDBOX_RO"),
                extra_rw=_env_paths("TRIO_OPENCODE_AUTHOR_SANDBOX_RW")))
        note = (f"author isolation: {level} ({detail})" if usable else
                f"author isolation: {level} -- no OS sandbox here ({detail}); the author gets "
                "NO shell and only path-checked file tools confined to its export")
        if removed:
            note += f"; removed {len(removed)} symlink(s) leaving the export"
        if self.acc_log.get("author_isolation") != level:
            self.acc_log["author_isolation"] = level
            self.out(f"acceptance: {note}")
            try:
                steplib.TL._append_log(self.live_mailbox,
                                       f"- iter {iteration} | loop | acceptance: {note}")
            except Exception:  # noqa: BLE001 - the LOG line is best effort
                pass
        self.author_level = level
        return AuthorSetup(level=level, env=env, argv_prefix=prefix,
                           shell=(level != ocgen.LEVEL_NO_SHELL), note=note)

    def release_author(self, export: Path) -> None:
        """Undo what ``author_setup`` added to the export for the turn (the
        project-root guard), so the validator and the freeze see the export
        as built."""
        if self.agents_root is not None:
            _get_authorbox().release_project_root(Path(export))

    # -- .driver.json -----------------------------------------------------
    def write_driver_json(self, *, phase: str, iteration: int) -> None:
        self._last_phase, self._last_iteration = phase, iteration
        with self._turns_lock:
            turns_snapshot = dict(self.turns)
        payload = {
            "driver": HARNESS, "pid": os.getpid(), "run_token": self.token,
            "exec_id": self.exec_id, "iteration": iteration, "phase": phase,
            "live_mailbox": str(self.live_mailbox), "root_mailbox": str(self.root_mailbox),
            "acceptance_enabled": self.acceptance,
            "turns": turns_snapshot,
            # Bug 1, crash-window case: the pre-turn OWNED_STATE_KEYS snapshot
            # for whichever guarded (lead/repair/evaluator) turn is currently
            # in flight, or `None` between turns / for an unguarded turn --
            # `_normalize_resume_phase` restores from this on resume instead
            # of guessing a phase, when a crash never reached `_call_role`'s
            # own `finally` restore.
            "state_snapshot": self.state_snapshot,
            "root_free": {"enabled": self.root_free,
                         "branch": self.lead_record.branch if self.lead_record else None,
                         "path": str(self.lead_record.path) if self.lead_record else None},
            "updated_at": _now_iso(),
        }
        if self.driver_extra:
            for key, value in self.driver_extra.items():
                payload.setdefault(key, value)
        with self.driver_json_lock:
            _atomic_write_json(self.driver_json_path, payload)

    def on_spawn(self, pid: int, pgid: int, *, label: str, session_id: str | None) -> None:
        with self._turns_lock:
            self.turns[label] = {"pid": pid, "pgid": pgid,
                                 "session_id": session_id, "started_at": _now_iso()}
        # REVIEW-driver.md item 7/crash-safety: `.driver.json` must record
        # the turn's pid/pgid on disk the moment it spawns — a crash mid-turn
        # otherwise leaves `resume` nothing to find and kill (the previous
        # on-disk copy, from the `{action}-running` write before this turn
        # even started, still has no record of it).
        self.write_driver_json(phase=self._last_phase, iteration=self._last_iteration)

    def on_turn_end(self, label: str) -> None:
        """The turn's process has exited (success, failure, or a stop kind):
        drop it from ``.driver.json`` so `resume` never tries to kill an
        already-dead pid/pgid that may since have been reused by an
        unrelated process."""
        with self._turns_lock:
            self.turns.pop(label, None)
        self.write_driver_json(phase=self._last_phase, iteration=self._last_iteration)

    def turn_env(self) -> dict[str, str]:
        """This run's per-loop OpenCode env plus (REVIEW-driver.md item 16)
        ``TMPDIR``/``TMP``/``TEMP`` pinned to ``begin``'s scratch dir, so
        every role turn actually uses it instead of the host default."""
        env = dict(self.env)
        if self.tmpdir:
            env["TMPDIR"] = self.tmpdir
            env["TMP"] = self.tmpdir
            env["TEMP"] = self.tmpdir
        return env


# ------------------------------------------------------------ role turns
def _call_role(ctx: RunContext, *, role: str, agent: str, model: str, prompt: str,
               cwd: Path, label: str, session_id: str | None = None,
               turn_timeout: float | None = None, idle_timeout: float | None = None,
               guard_state_override: bool | None = None,
               env_extra: dict[str, str] | None = None,
               argv_prefix: tuple[str, ...] = ()) -> Any:
    """One role turn, retried once on a null-equivalent (``ok=False`` and
    not a stop kind), matching the native driver's ``runAgentTwice``.
    Raises :class:`DriverStop` on a permission/config_error result or two
    consecutive failures.

    ``guard_state_override`` (D7, open-loop only): this function's own
    before/after STATE.md snapshot below assumes nothing else writes
    STATE.md while the turn runs -- true for lockstep (one role turn at a
    time), false for open-loop, where ``steplib.TL.run_open_loop`` itself
    writes the SAME owned keys (the iteration cursor) from another thread
    while an evaluator turn is in flight; racing the two restores a stale
    snapshot over a legitimate concurrent bump. Open-loop callers pass
    ``False`` here and rely entirely on ``openloop._StateGuard`` (which
    wraps ``TL._update_state`` itself, so it always restores from the
    latest legitimate write, never a stale pre-turn one). ``None`` (every
    lockstep call site) keeps this function's own historical behaviour."""
    runner = _get_runner()
    # NB: `or` would treat an explicit, valid `0` (the "no wall-clock limit"
    # sentinel - config.py / README.md "Container / no-time-limit mode") as
    # unset and silently fall through to the 3600s default; only `None`
    # (never-configured) means "fall through" here.
    if turn_timeout is None:
        turn_timeout = _turn_timeout_for(ctx.cfg, role)
    if turn_timeout is None:
        turn_timeout = 3600.0
    if idle_timeout is None:
        idle_timeout = _idle_timeout_for(ctx.cfg)
    if idle_timeout is None:
        idle_timeout = 600.0
    # Bug 1 (REVIEW-driver.md): lead/repair/evaluator turns run against the
    # live mailbox's own STATE.md and occasionally mistake a driver-owned
    # cursor key for a role-writable line (e.g. a plan turn writing its own
    # `phase`) — builders run concurrently in their OWN worktrees (never the
    # live mailbox) and the acceptance author turn never touches STATE.md at
    # all, so neither is guarded here.
    guard_state = (role in ("lead", "repair", "evaluator")
                  if guard_state_override is None else guard_state_override)
    state_path = ctx.live_mailbox / "STATE.md"
    state_before = _owned_state_snapshot(state_path) if guard_state else None
    if guard_state:
        # Persist the pre-turn snapshot to `.driver.json` BEFORE the turn
        # spawns: a crash mid-turn (e.g. the Evaluator SIGKILLed right after
        # it writes a bogus `phase`) never reaches the `finally` below, so
        # `resume` needs this on disk to restore from instead of guessing.
        ctx.state_snapshot = state_before
        ctx.write_driver_json(phase=ctx._last_phase, iteration=ctx._last_iteration)
    try:
        if ctx.cancel.is_set():
            raise DriverStop("cancelled", ctx.cancel_code.get("code", 130), f"{label}: cancelled",
                             role_denials=ctx.role_denials)
        last = None
        for attempt in (1, 2):
            this_label = label if attempt == 1 else f"{label} (retry)"

            def spawn(pid: int, pgid: int, _label=this_label, _sid=[session_id]) -> None:
                ctx.on_spawn(pid, pgid, label=_label, session_id=_sid[0])

            max_attempts, backoff, idle_retry_unlimited = _retries_for(ctx.cfg)
            spec = _make_turn_spec(
                runner, role=role, agent=agent, model=model, prompt=prompt,
                variant=_variant_for(ctx.cfg, role, model),
                cwd=str(cwd), session_id=session_id, label=this_label,
                env={**ctx.turn_env(), **(env_extra or {})}, argv_prefix=tuple(argv_prefix),
                turn_timeout=turn_timeout, idle_timeout=idle_timeout,
                opencode_bin=getattr(ctx.cfg, "opencode_bin", "opencode"),
                key_file=_key_file_for(ctx.cfg), log_dir=str(ctx.log_dir),
                max_attempts=max_attempts, backoff=backoff,
                idle_retry_unlimited=idle_retry_unlimited,
            )
            result = runner.run_turn(spec, on_spawn=spawn, cancel=ctx.cancel)
            ctx.on_turn_end(this_label)
            last = result
            denials = collect_denials(result.text, list(getattr(result, "denials", []) or []))
            for d in denials:
                ctx.role_denials.append({"label": label, "text": d[:400]})
            if getattr(result, "kind", None) == "cancelled":
                # REVIEW-driver.md item 6: a cancelled turn (SIGTERM/SIGINT
                # mid turn, or cancel set before spawn) stops the run at
                # once, never retried — the caller unwinds straight to `end`.
                raise DriverStop("cancelled", ctx.cancel_code.get("code", 130), f"{label}: cancelled",
                                 role_denials=ctx.role_denials)
            if getattr(result, "kind", None) == "permission":
                # A permission the generated config denies killed the turn:
                # surface it as a role denial too (never bypassed, never
                # retried), not only in the stop reason -- concurrent
                # open-loop turns otherwise lose which role was refused.
                ctx.role_denials.append({"label": label, "text": (
                    f"permission denied: {result.error or 'permission requested'}")[:400]})
            if getattr(result, "kind", None) in STOP_KINDS:
                raise DriverStop(
                    "error", 3,
                    f"{label}: {result.kind}: {result.error or ''}",
                    role_denials=ctx.role_denials,
                )
            if result.ok:
                return result
            ctx.out(f"{label}: turn failed ({result.kind}: {result.error}), try {attempt}")
        raise DriverStop("error", 3, f"{label}: turn failed twice: "
                         f"{last.kind if last else '?'}: {last.error if last else '?'}",
                         role_denials=ctx.role_denials)
    finally:
        if guard_state:
            _restore_owned_state(ctx, state_before, state_path, label)
            # The turn ended (normally or via DriverStop) inside this same
            # process, so `_call_role`'s own restore above already ran --
            # clear the persisted snapshot so a later crash/resume never
            # mistakes a PAST turn's snapshot for the current one's.
            ctx.state_snapshot = None
            ctx.write_driver_json(phase=ctx._last_phase, iteration=ctx._last_iteration)


def _structured(ctx: RunContext, *, result: Any, required: tuple[str, ...],
                cwd: Path, role: str, agent: str, model: str, label: str,
                advisory: bool = False, defaults: dict | None = None,
                env_extra: dict[str, str] | None = None,
                argv_prefix: tuple[str, ...] = ()) -> dict:
    """Parse the fenced ```json block from a turn's text; one re-prompt in
    the same session on a missing/malformed block.

    ``advisory`` (REVIEW-driver.md items 4/5 — the builder's own report, and
    the Lead's integrate report): when still missing/malformed after the one
    re-prompt, never stop the run; return whatever object was parsed (if
    any) with ``defaults`` filled in for the missing required keys, and log
    it. Git (the builder's verified branch; the integrate call's merge/
    cleanup results) remains the authority either way.
    """
    obj = extract_json_block(result.text)
    if obj is not None and all(k in obj for k in required):
        return obj
    problem = ("no fenced ```json block found" if obj is None
              else f"missing key(s): {[k for k in required if k not in obj]}")
    schema_hint = "{" + ", ".join(f'"{k}": ...' for k in required) + "}"
    retry = _call_role(
        ctx, role=role, agent=agent, model=model,
        prompt=prompts.reprompt(problem, schema_hint), cwd=cwd,
        label=f"{label} (reprompt)", session_id=result.session_id, env_extra=env_extra,
        argv_prefix=argv_prefix,
    )
    obj2 = extract_json_block(retry.text)
    if obj2 is not None and all(k in obj2 for k in required):
        return obj2
    if advisory:
        out = dict(obj2 if isinstance(obj2, dict) else (obj if isinstance(obj, dict) else {}))
        for k in required:
            out.setdefault(k, (defaults or {}).get(k, ""))
        ctx.out(f"{label}: no usable structured result after one re-prompt; "
                f"continuing with defaults for {list(required)}")
        return out
    raise DriverStop("error", 3,
                     f"{label}: no usable structured result after one re-prompt")


# ------------------------------------------------------- builder worktrees
def _create_builder_worktree(ctx: RunContext, *, iteration: int, slice_id: str,
                             index: int, dispatch_head: str) -> tuple[Path, str]:
    exec8 = ctx.exec_id[:8]
    branch = f"trio-oc/{exec8}/i{iteration}-{slice_id}"
    path = ctx.repo / BUILDER_WORKTREES_DIR / f"{exec8}-b{index}"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Record intent in the ownership ledger BEFORE `git worktree add`: a
    # crash between the two leaves a ledger entry whose branch/worktree
    # never actually exists, which native/trio_native_step.py's
    # `_reclaim_builders` (via `_previous_builders`) tolerates fine — the
    # candidate's `_branch_sha` comes back `None`, so it is simply released
    # and skipped, nothing raises. The reverse order is unsafe: a crash
    # AFTER `git worktree add` but before this ledger write leaves a REAL
    # worktree/branch the ledger never recorded, which `_reclaim_builders`
    # can then never find, reuse or clean up — a permanent leak.
    steplib.ledger_append(ctx.live_mailbox, ctx.repo, {
        "kind": "builder", "exec_id": ctx.exec_id, "run_id": f"oc-{exec8}",
        "path": str(path), "branch": branch, "id": slice_id,
        "iteration": iteration, "verified_by": "driver-created",
    })
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(ctx.repo), "worktree", "add", "-b", branch, str(path),
         dispatch_head],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise DriverStop("error", 3,
                         f"builder worktree for {slice_id}: git worktree add failed: "
                         f"{r.stderr.strip()}")
    return path, branch


def _mailbox_rel(repo: Path, mailbox: Path) -> str | None:
    try:
        return mailbox.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def _verify_builder_branch(ctx: RunContext, *, dispatch_head: str, branch: str,
                           slice_id: str, path: Path) -> tuple[bool, str]:
    """git-only verification: the branch contains the dispatch head, has at
    least one ``slice(<id>):`` commit on top, commits no ``loop/`` path, and
    is checked out nowhere but its own driver-created worktree."""
    TL = steplib.TL
    tip = TL._git(ctx.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").stdout.strip()
    if not tip:
        return False, f"branch {branch} does not exist"
    if not TL._git_is_ancestor(ctx.repo, dispatch_head, tip):
        return False, f"branch {branch} does not contain the dispatch HEAD {dispatch_head[:12]}"
    commits = TL._git(ctx.repo, "rev-list", f"{dispatch_head}..{tip}").stdout.split()
    if not commits:
        return False, f"branch {branch} has no commits since the dispatch HEAD"
    subjects = TL._git(ctx.repo, "log", "--format=%s", f"{dispatch_head}..{tip}").stdout
    if f"slice({slice_id}):" not in subjects:
        return False, f"branch {branch} has no `slice({slice_id}):` commit"
    mailbox_rel = _mailbox_rel(ctx.repo, ctx.live_mailbox)
    for sha in commits:
        for p in TL._commit_paths(ctx.repo, sha):
            if TL._path_in_mailbox(p, mailbox_rel):
                return False, f"commit {sha[:12]} commits mailbox files ({p})"
    return True, ""


def _dispatch_wave(ctx: RunContext, *, iteration: int, wave: int) -> str:
    # r19: `op_dispatch` itself raises (StepError -> `ok: False`) when the
    # acceptance pack is unfrozen, off its pin, or a check is unmapped in
    # PLAN.md (`_acc_dispatch_refusal`) -- unlike `coverage`/`gate`/`apply`,
    # it carries no `acceptance` digest of its own to `accTake` (mirrors
    # native/trio-native.js's `dispatch` call exactly: no `accTake` there).
    d = steplib.dispatch(ctx.live_mailbox, ctx.repo, ctx.token, iteration=iteration, wave=wave,
                         acceptance=ctx.acceptance, acc=ctx.acc)
    if not d["ok"]:
        raise DriverStop("error", 3, f"dispatch it{iteration} w{wave}: {d['error']}")
    return d["head"]


def _run_one_builder(ctx: RunContext, *, iteration: int, wave: int, index: int,
                     s: dict, dispatch_head: str) -> dict:
    path, branch = _create_builder_worktree(
        ctx, iteration=iteration, slice_id=s["id"], index=index, dispatch_head=dispatch_head)
    model = _model_for(ctx.cfg, "builder")
    prompt = prompts.builder_prompt(iteration, s, dispatch_head, str(ctx.live_mailbox),
                                    str(ctx.repo), ctx.tmpdir)
    result = _call_role(ctx, role="builder", agent=AGENTS["builder"], model=model,
                        prompt=prompt, cwd=path, label=f"builder it{iteration}w{wave} {s['id']}")
    # REVIEW-driver.md item 4: the builder's own structured report is purely
    # advisory — git (`_verify_builder_branch`) decides whether the slice is
    # accepted; a missing/malformed report never stops the run.
    reported = _structured(
        ctx, result=result, required=("summary",),
        cwd=path, role="builder", agent=AGENTS["builder"], model=model,
        label=f"builder it{iteration}w{wave} {s['id']}",
        advisory=True, defaults={"summary": "(no structured report)"},
    )
    ok, why = _verify_builder_branch(ctx, dispatch_head=dispatch_head, branch=branch,
                                     slice_id=s["id"], path=path)
    # The slice id is driver-known, authoritative regardless of what (or
    # whether) the builder's own advisory report said (item 4) — the
    # integrate prompt indexes accepted reports by `r["id"]`.
    reported["id"] = s["id"]
    return {"id": s["id"], "branch": branch, "path": str(path), "ok": ok, "reason": why,
            "report": reported}


def _log_builder_line(ctx: RunContext, iteration: int, sid: str, summary: str) -> None:
    line = f"- iter {iteration} | builder | {sid}: {summary}"
    log_path = ctx.live_mailbox / "LOG.md"
    try:
        existing = log_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if line in existing.splitlines():
        return
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _cleanup(ctx: RunContext, *, branches: list[str], drop_unmerged: dict[str, str] | None = None) -> dict:
    drop = ",".join(f"{old}={new}" for old, new in (drop_unmerged or {}).items())
    out = steplib.cleanup(ctx.live_mailbox, ctx.repo, ctx.token,
                          branches=",".join(branches), drop_unmerged=drop or None)
    if not out["ok"]:
        raise DriverStop("error", 3, f"cleanup: {out['error']}")
    return out


# --------------------------------------------------------------- one wave
def _run_wave(ctx: RunContext, *, iteration: int, wave_index: int, wave: list[dict],
             dispatch_head: str) -> tuple[list[dict], list[dict], dict]:
    """Runs every builder of one wave concurrently; returns
    ``(accepted_reports, refused, merge_by_id)``."""
    outcomes: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(wave))) as pool:
        futures = {
            pool.submit(_run_one_builder, ctx, iteration=iteration, wave=wave_index,
                       index=i + 1, s=s, dispatch_head=dispatch_head): s
            for i, s in enumerate(wave)
        }
        for fut in futures:
            outcomes.append(fut.result())

    accepted, refused = [], []
    merge_by_id: dict[str, str] = {}
    for o in outcomes:
        if o["ok"]:
            accepted.append(o["report"])
            merge_by_id[o["id"]] = o["branch"]
            _log_builder_line(ctx, iteration, o["id"],
                              o["report"].get("summary", "") or "(no summary)")
        else:
            refused.append({"id": o["id"], "branch": o["branch"], "reason": o["reason"],
                            "own_branch": o["branch"]})
    return accepted, refused, merge_by_id


def _integrate(ctx: RunContext, *, iteration: int, wave_index: int, last: bool,
              accepted: list[dict], merge_by_id: dict[str, str], cwd: Path,
              session_id: str | None) -> dict:
    model = _model_for(ctx.cfg, "lead")
    acc_pass = (prompts.acc_pass_lines("lead", str(ctx.live_mailbox), _acc_tool(ctx))
               if ctx.acceptance and last else None)
    prompt = prompts.integrate_prompt(iteration, wave_index, last, accepted, merge_by_id,
                                      str(ctx.live_mailbox), str(ctx.repo), ctx.tmpdir,
                                      acc_pass=acc_pass)
    result = _call_role(ctx, role="lead", agent=AGENTS["lead"], model=model, prompt=prompt,
                        cwd=cwd, label=f"lead integrate it{iteration}w{wave_index}",
                        session_id=session_id)
    # REVIEW-driver.md item 5: a missing/malformed integrate report never
    # stops the run either — git (the merges `cleanup` actually saw) is the
    # authority on what merged; continue with an empty/advisory summary.
    integ = _structured(
        ctx, result=result, required=("merged", "conflicts", "summary"),
        cwd=cwd, role="lead", agent=AGENTS["lead"], model=model,
        label=f"lead integrate it{iteration}w{wave_index}",
        advisory=True,
        defaults={"merged": [], "conflicts": [],
                 "summary": "(integrate report missing; continuing with git authority)"},
    )
    integ["_session_id"] = result.session_id
    return integ


def _track_kept(kept: list[dict], cl: dict) -> list[dict]:
    """``trackKept`` (native): the running set of builder branches a pass's
    ``cleanup`` calls could not remove, across the whole pass — a branch
    drops out once a later cleanup reports it removed or dropped."""
    by_branch = {k["branch"]: k["reason"] for k in kept}
    for x in cl.get("removed") or []:
        by_branch.pop(x["branch"], None)
    for x in cl.get("dropped") or []:
        if x.get("dropped"):
            by_branch.pop(x["branch"], None)
    for x in cl.get("kept") or []:
        by_branch[x["branch"]] = x["reason"]
    return [{"branch": b, "reason": r} for b, r in by_branch.items()]


# ------------------------------------------------------------- lead pass
def _get_events():
    from trio_opencode import events  # noqa: PLC0415 - lazy, another slice
    return events


#: Tool-input keys that carry text the author WROTE (a file body, an edit's
#: old/new strings, a patch) rather than something it asked to read. The
#: audit judges what the author LOOKED AT; a written pack that merely
#: *mentions* a repository path (a goal that names absolute paths makes a
#: check or AUTHOR.md quoting one natural) is not a read and used to discard
#: the attempt as "reads inside the loop repository".
AUTHORED_TEXT_KEYS = frozenset({
    "content", "oldString", "newString", "old_string", "new_string",
    "patch", "patchText", "patch_text", "newText", "oldText",
})


#: Tools whose ``pattern`` is a regex/literal to FIND inside files, not a path:
#: the file or directory they search is their ``path`` (judged as usual). A
#: ``glob`` pattern IS path-like (``../x/*``) and stays audited.
CONTENT_SEARCH_TOOLS = frozenset({"grep", "search", "ripgrep", "rg"})


def audit_tool_input(tool_input: dict, tool: str | None = None) -> dict:
    """``tool_input`` without the keys that only carry text, never a path the
    call reads: authored text (see :data:`AUTHORED_TEXT_KEYS`) and the
    ``pattern`` of a content search (:data:`CONTENT_SEARCH_TOOLS`) -- a no-shell
    author that grepped its own export's GOAL.md for a goal sentence naming
    ``/app/src/worker/requirements.txt`` was discarded as having "read inside
    the loop repository" though nothing outside its export was touched. What is
    left is the part of an author tool call the isolation audit should judge."""
    drop = set(AUTHORED_TEXT_KEYS)
    if str(tool or "").lower() in CONTENT_SEARCH_TOOLS:
        drop.add("pattern")
    return {k: v for k, v in tool_input.items() if k not in drop}


_PERMISSION_REFUSAL_RE = re.compile(
    r"rule which prevents|prevents you from using|rejected permission|permission denied"
    r"|denied by (?:a )?(?:rule|permission)|not allowed (?:by|in) ", re.IGNORECASE)


def refused_by_permission(state: dict) -> bool:
    """A tool call the permission rules refused before it ran (status
    ``error`` with a rule-denial message). It read nothing, so the isolation
    audit must not count the ATTEMPT: with deny rules in place a refused read
    of the repository is the mechanism working, not contamination. Any other
    error (file not found, ...) still counts -- the author looked."""
    if not isinstance(state, dict) or state.get("status") != "error":
        return False
    return bool(_PERMISSION_REFUSAL_RE.search(str(state.get("error") or "")))


#: The one benign row recorded for a sandboxed author turn.
SANDBOXED_ROW = {"type": "tool_use", "name": "sandboxed-author", "input": {}}


def sandboxed(ctx: RunContext) -> bool:
    """Was the latest author turn contained by the OS sandbox? Then the
    isolation is not judged from tool-call text: the repository did not exist
    for the process, so a command that merely NAMES it (``ls /app`` -> no such
    directory) read nothing and must not discard the session. The audit is the
    net where the isolation is weaker (``no-shell``)."""
    return getattr(ctx, "author_level", None) == "sandbox"


def _persist_author_tool_calls(ctx: RunContext, result: Any, marker: str) -> None:
    """req. 4: convert the author turn's own NDJSON event log (one ``.jsonl``
    per attempt, already written and scrubbed by ``runner.py`` — see
    ``result.log_paths``) into the row shape
    ``metrics/trio-acceptance.py::_audit_inputs`` understands, and persist it
    as one JSONL file keyed by ``marker`` (exactly the string
    ``op_acceptance_freeze`` passes ``_native_author_audit`` —
    ``f"{marker}-a{attempt}"``) under ``steplib.AUTHOR_TOOLCALLS_DIR``, for
    :func:`trio_opencode.steplib._opencode_author_audit` to find. Best
    effort: a read/parse failure here only means that attempt's audit falls
    back to the helper's own limited one, never a run failure."""
    if steplib.AUTHOR_TOOLCALLS_DIR is None:
        return
    events_mod = _get_events()
    entries: list[dict] = []
    for log_path in getattr(result, "log_paths", []) or []:
        if sandboxed(ctx):
            entries = [dict(SANDBOXED_ROW)]
            break
        if not str(log_path).endswith(".jsonl"):
            continue
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            ev = events_mod.parse_line(line)
            if ev is None:
                continue
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            is_tool = ev.get("type") in ("tool_use", "tool") or part.get("type") == "tool"
            if not is_tool:
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if refused_by_permission(state):
                continue
            tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
            entries.append({"type": "tool_use", "name": part.get("tool"),
                            "input": audit_tool_input(tool_input, part.get("tool"))})
    out_path = Path(steplib.AUTHOR_TOOLCALLS_DIR) / f"{marker}.jsonl"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _acc_tool(ctx: RunContext) -> str:
    tool = ctx.acc.get("tool") if isinstance(ctx.acc, dict) else None
    return tool if isinstance(tool, str) and tool else "metrics/trio-acceptance.py"


def _acc_plan_arg(plan: dict) -> dict:
    """``accPlanArg()`` (native): the coverage op's own small view of the
    plan (ids + covers + lead_integration + acceptance_bindings), never the
    whole structured plan (briefs/writes/reads are irrelevant to coverage)."""
    return {
        "slices": [{"id": s["id"], "covers": s.get("covers") if isinstance(s.get("covers"), list) else []}
                  for s in plan.get("slices", [])],
        "lead_integration": plan.get("lead_integration") if isinstance(plan.get("lead_integration"), list) else [],
        "acceptance_bindings": plan.get("acceptance_bindings") if isinstance(plan.get("acceptance_bindings"), dict) else {},
    }


def _coverage_gate(ctx: RunContext, *, iteration: int, rec: dict, plan: dict,
                   human_answer: str | None, reclaimed: dict | None,
                   begin_iteration: int | None) -> tuple[dict, str | None]:
    """``coverageGate()`` (native): before any builder wave (or the solo
    pass) starts, the helper's ``coverage`` op checks the structured plan's
    ``covers``/``lead_integration``/``acceptance_bindings`` against the
    frozen pack. One re-plan with the refusal text; a second refusal stops
    the loop. Returns the (possibly re-planned, brief-annotated) plan."""
    lead_model = _model_for(ctx.cfg, "lead")
    session_id: str | None = None
    for attempt in (1, 2):
        c = steplib.coverage(ctx.live_mailbox, ctx.repo, ctx.token, iteration=iteration,
                             attempt=attempt, plan=_acc_plan_arg(plan), acc=ctx.acc)
        if not c["ok"]:
            raise DriverStop("error", 3, f"coverage it{iteration}: {c['error']}")
        stop = steplib.acc_stop(c)
        if stop:
            ctx.acc_log["coverage_refusals"].append(
                {"iteration": iteration, "attempt": attempt, "refusals": (c.get("refusals") or [])[:10]})
            raise DriverStop(stop["status"], stop.get("code") or 3, stop["reason"])
        ctx.acc = steplib.acc_take(ctx.acc, c, "coverage")
        if c.get("covered_ok"):
            rec["acceptance"] = {**(rec.get("acceptance") or {}),
                                 "coverage": "ok" if attempt == 1 else "ok after re-plan"}
            return prompts.acc_briefed(plan, c.get("briefs")), session_id
        refusals = (c.get("refusals") or [])[:10]
        ctx.acc_log["coverage_refusals"].append(
            {"iteration": iteration, "attempt": attempt, "refusals": refusals})
        rec["acceptance"] = {**(rec.get("acceptance") or {}), "coverage_refused": refusals}
        ctx.out(f"iteration {iteration}: acceptance coverage refused ({'; '.join(refusals[:3])})"
               + ("; re-planning once" if attempt == 1 else ""))
        if attempt == 2:
            raise DriverStop("error", 3, "acceptance coverage refused after a re-plan: "
                             + "; ".join(c.get("refusals") or [])[:400])
        ctx.acc_log["replanned"] = True
        acc_lines = prompts.acc_plan_lines(str(ctx.live_mailbox), _acc_tool(ctx), ctx.acc,
                                           refusals=c.get("refusals") or [])
        prompt = prompts.lead_plan_prompt(
            iteration, str(ctx.live_mailbox), str(ctx.repo), ctx.tmpdir,
            human_answer=human_answer, reclaimed=reclaimed, begin_iteration=begin_iteration,
            acc_lines=acc_lines, plan_schema_hint=prompts.PLAN_SCHEMA_HINT_ACC)
        result = _call_role(ctx, role="lead", agent=AGENTS["lead"], model=lead_model,
                            prompt=prompt, cwd=ctx.repo, label=f"lead plan it{iteration} (re-plan)")
        session_id = result.session_id
        plan = _structured(ctx, result=result, required=("slices",), cwd=ctx.repo, role="lead",
                          agent=AGENTS["lead"], model=lead_model,
                          label=f"lead plan it{iteration} (re-plan)")
        problem = waves.check_slices(plan)
        if problem:
            raise DriverStop("error", 3, f"lead re-plan it{iteration}: {problem}")
    raise DriverStop("error", 3, "acceptance coverage: no attempt left")


def _author_phase(ctx: RunContext, *, iteration: int) -> None:
    """``authorPhase()`` (native): export -> author turn -> freeze
    (validation at base, audit, driver freeze commit), at most one
    validation retry and one contaminated re-run. Raises :class:`DriverStop`
    on any acceptance stop/error; returns (``ctx.acc`` now frozen) once the
    pack is frozen."""
    ex = steplib.acceptance_export(ctx.live_mailbox, ctx.repo, ctx.token,
                                   iteration=iteration, attempt=1, acc=ctx.acc)
    if not ex["ok"]:
        raise DriverStop("error", 3, f"acceptance-export it{iteration}: {ex['error']}")
    stop = steplib.acc_stop(ex)
    if stop:
        raise DriverStop(stop["status"], stop.get("code") or 3, stop["reason"])
    ctx.acc = steplib.acc_take(ctx.acc, ex, "acceptance-export")
    if ex.get("frozen"):
        if steplib.acc_frozen(ctx.acc):
            return
        raise DriverStop("error", 3,
                         "acceptance-export: frozen, but not by begin or the helper's freeze")
    export = Path(ex["export"])
    tool = ex.get("tool") or _acc_tool(ctx)
    ctx.out(f"acceptance: author export of {str(ex.get('base'))[:12]} "
           f"({ex.get('removed')} path(s) filtered out)")
    prior = {"contaminated": False, "retried": False}
    retry: dict = {}
    model = _model_for(ctx.cfg, "acceptance")
    for attempt in (1, 2, 3):
        ctx.acc_log["author_attempts"] = attempt
        marker = f"{ex['marker']}-a{attempt}"
        setup = ctx.author_setup(export, tool, iteration=iteration)
        try:
            prompt = prompts.author_prompt(str(export), tool, marker, attempt,
                                           notes=bool(ex.get("notes")), retry=retry,
                                           shell=setup.shell)
            result = _call_role(ctx, role="acceptance", agent=AGENTS["acceptance"], model=model,
                                prompt=prompt, cwd=export,
                                label=f"acceptance author it{iteration}#{attempt}",
                                env_extra=setup.env, argv_prefix=setup.argv_prefix)
            _persist_author_tool_calls(ctx, result, marker)
            author_out = _structured(
                ctx, result=result, required=("checks", "summary"), cwd=export, role="acceptance",
                agent=AGENTS["acceptance"], model=model,
                label=f"acceptance author it{iteration}#{attempt}", advisory=True,
                defaults={"checks": None, "summary": "(no structured report)"},
                env_extra=setup.env, argv_prefix=setup.argv_prefix,
            )
        finally:
            ctx.release_author(export)
        author_summary = {
            "exit": 0,
            "checks": author_out.get("checks") if isinstance(author_out.get("checks"), int) else None,
            "summary": str(author_out.get("summary") or "")[:200],
        }
        fr = steplib.step_long(
            steplib.acceptance_freeze, ctx.live_mailbox, ctx.repo, ctx.token,
            iteration=iteration, attempt=attempt, marker=ex["marker"], prior=prior,
            author=author_summary, model=model, acc=ctx.acc,
        )
        if not fr["ok"]:
            raise DriverStop("error", 3, f"acceptance-freeze it{iteration}: {fr['error']}")
        stop = steplib.acc_stop(fr)
        if stop:
            raise DriverStop(stop["status"], stop.get("code") or 3, stop["reason"])
        ctx.acc = steplib.acc_take(ctx.acc, fr, "acceptance-freeze")
        audit = fr.get("audit") or {}
        if fr.get("action") == "frozen":
            ctx.out(
                f"acceptance: frozen {fr.get('checks')} check(s), pin "
                f"{str(ctx.acc.get('pin') if ctx.acc else None)[:12]} @"
                f"{str(ctx.acc.get('pin_commit') if ctx.acc else None)[:12]}"
                + (f"; dropped {len(fr.get('dropped') or [])}" if fr.get("dropped") else "")
                + ("; author audit limited (no author transcript found)"
                   if audit.get("limited") else ""))
            return
        if fr.get("action") == "reauthor":
            prior["contaminated"] = True
            retry = {"prefix": fr.get("prefix") or ""}
            ctx.out(f"acceptance: author session contaminated "
                   f"({'; '.join((fr.get('hits') or [])[:2])}); re-authoring once")
            continue
        if fr.get("action") == "retry":
            prior["retried"] = True
            retry = {"dropped": fr.get("dropped") or [], "fatal": fr.get("fatal") or []}
            ctx.out(f"acceptance: validation at base asks for one retry "
                   f"({len(fr.get('dropped') or [])} dropped)")
            continue
        raise DriverStop("error", 3, f"acceptance-freeze: unexpected action {fr.get('action')!r}")
    raise DriverStop("error", 3, "acceptance author: no freeze after 3 attempts")


def _run_lead_pass(ctx: RunContext, *, iteration: int, rec: dict,
                   human_answer: str | None, reclaimed: dict | None,
                   begin_iteration: int | None) -> None:
    """One full Lead pass, attempt 1: plan -> waves (dispatch, builders,
    verify, integrate, cleanup). Mutates ``rec`` (this iteration's record,
    REVIEW-driver.md item 11) with everything a caller retrying at attempt 2
    (:func:`_run_role_with_gate`) or the final result needs: ``slices``,
    ``waves``/``planned_waves``, ``plan_notes``, ``refused``, ``conflicts``
    and ``kept`` (the running set of un-cleaned-up builder branches, per
    item 12's ``soloLeadPrompt`` ``kept`` argument)."""
    cwd = ctx.repo
    lead_model = _model_for(ctx.cfg, "lead")
    # Bug fix (e2e, r19 acceptance): `ctx.acc` carries `next()`'s own
    # `errors` key (a SHIP the frozen-acceptance gate refused, surfaced to
    # the very next Lead dispatch) straight through `acc_take` untouched
    # (it is not one of the restricted `ACC_KEYS`), but this call never
    # read it — so "ACCEPTANCE ERRORS FROM THE DRIVER" never reached a
    # real plan prompt. Mirrors native/trio-native.js's
    # `accPlanLines(n, ...)`, which reads `n.acceptance.errors` the same
    # way at the same call site.
    acc_errors = ctx.acc.get("errors") if ctx.acceptance and isinstance(ctx.acc, dict) else None
    acc_lines = (prompts.acc_plan_lines(str(ctx.live_mailbox), _acc_tool(ctx), ctx.acc,
                                        errors=acc_errors)
                if ctx.acceptance else None)
    plan_prompt = prompts.lead_plan_prompt(
        iteration, str(ctx.live_mailbox), str(ctx.repo), ctx.tmpdir,
        human_answer=human_answer, reclaimed=reclaimed, begin_iteration=begin_iteration,
        acc_lines=acc_lines,
        plan_schema_hint=prompts.PLAN_SCHEMA_HINT_ACC if ctx.acceptance else prompts.PLAN_SCHEMA_HINT)
    plan_result = _call_role(ctx, role="lead", agent=AGENTS["lead"], model=lead_model,
                             prompt=plan_prompt, cwd=cwd, label=f"lead plan it{iteration}")
    plan = _structured(ctx, result=plan_result, required=("slices",), cwd=cwd, role="lead",
                       agent=AGENTS["lead"], model=lead_model, label=f"lead plan it{iteration}")
    problem = waves.check_slices(plan)
    if problem:
        raise DriverStop("error", 3, f"lead plan it{iteration}: {problem}")
    session_id = plan_result.session_id
    if ctx.acceptance:
        # r19: coverage before any builder (or the solo pass) starts.
        plan, replan_session = _coverage_gate(
            ctx, iteration=iteration, rec=rec, plan=plan, human_answer=human_answer,
            reclaimed=reclaimed, begin_iteration=begin_iteration)
        session_id = replan_session or session_id
    slices = plan["slices"]
    rec["slices"] = [s["id"] for s in slices]
    rec["plan_notes"] = plan.get("notes") if isinstance(plan.get("notes"), str) else ""
    if not slices:
        rec["waves"] = []
        rec["planned_waves"] = []
        # No product code this iteration: the Lead finishes the pass alone.
        acc_pass = (prompts.acc_pass_lines("lead", str(ctx.live_mailbox), _acc_tool(ctx))
                   if ctx.acceptance else None)
        solo = prompts.solo_lead_prompt(
            iteration, 1, "This iteration changes no product code (empty plan).",
            str(ctx.live_mailbox), str(ctx.repo), ctx.tmpdir, human_answer=human_answer,
            acc_pass=acc_pass)
        _call_role(ctx, role="lead", agent=AGENTS["lead"], model=lead_model, prompt=solo,
                  cwd=cwd, label=f"lead solo it{iteration}", session_id=session_id)
        return

    planned_waves = waves.plan_waves(slices)
    rec["waves"] = [[s["id"] for s in w] for w in planned_waves]
    rec["planned_waves"] = [list(w) for w in rec["waves"]]
    rec["conflicts"] = []
    rec["refused"] = []
    rec["kept"] = rec.get("kept") or []
    remaining_refusal_budget = {s["id"]: 1 for s in slices}
    remaining_conflict_budget = {s["id"]: 1 for s in slices}
    k = 0
    while k < len(planned_waves):
        if ctx.cancel.is_set():
            raise DriverStop("cancelled", ctx.cancel_code.get("code", 130), "cancelled between waves")
        wave = planned_waves[k]
        last = k == len(planned_waves) - 1
        head = _dispatch_wave(ctx, iteration=iteration, wave=k + 1)
        accepted, refused, merge_by_id = _run_wave(ctx, iteration=iteration, wave_index=k + 1,
                                                    wave=wave, dispatch_head=head)
        # One re-dispatch per refused slice, as a new single-builder wave.
        by_id = {s["id"]: s for s in wave}
        redispatch: list[dict] = []
        for x in refused:
            sid = x["id"]
            rec["refused"].append({"id": sid, "reason": x["reason"]})
            if remaining_refusal_budget.get(sid, 0) <= 0:
                raise DriverStop("error", 3,
                                 f"builders refused after a re-dispatch: {sid}: {x['reason']}")
            remaining_refusal_budget[sid] = 0
            redispatch.append(waves.redispatch_refused(by_id[sid], x))
        if accepted:
            integ = _integrate(ctx, iteration=iteration, wave_index=k + 1, last=last and not redispatch,
                              accepted=accepted, merge_by_id=merge_by_id, cwd=cwd,
                              session_id=session_id)
            session_id = integ.pop("_session_id", session_id)
            cl = _cleanup(ctx, branches=list(merge_by_id.values()))
            rec["kept"] = _track_kept(rec["kept"], cl)
            conflicts = waves.wave_conflicts({"merge": [{"id": i, "branch": b}
                                                        for i, b in merge_by_id.items()]},
                                             integ, cl)
            rec["conflicts"].extend(conflicts)
            # REVIEW-driver.md item 2: a slice that conflicts again after
            # already having been re-dispatched once stops the whole run
            # with status "conflict" (never the generic "error"), reporting
            # every such slice at once (native's `again`/`describe`).
            again = []
            for c in conflicts:
                sid = c["id"]
                if remaining_conflict_budget.get(sid, 0) <= 0:
                    again.append(c)
                    continue
                remaining_conflict_budget[sid] = 0
                redispatch.append(waves.redispatch_slice(by_id[sid], c))
            if again:
                desc = "; ".join(
                    f"{c['id']} ({c['branch']}) on {', '.join(c.get('files') or []) or 'unreported files'}"
                    for c in again
                )
                raise DriverStop("conflict", 3,
                                 f"merge conflict after a re-dispatch: {desc}",
                                 conflicts=again)
        if redispatch:
            planned_waves.insert(k + 1, redispatch)
            rec["waves"] = [[s["id"] for s in w] for w in planned_waves]
        k += 1


# ------------------------------------------------------------ repair pass
def _run_repair_pass(ctx: RunContext, *, iteration: int, scope: str | None,
                     attempt: int, gate: dict | None = None) -> None:
    model = _model_for(ctx.cfg, "repair")
    acc_pass = (prompts.acc_pass_lines("repair", str(ctx.live_mailbox), _acc_tool(ctx))
               if ctx.acceptance else None)
    prompt = prompts.repair_prompt(iteration, attempt, scope, str(ctx.live_mailbox),
                                   str(ctx.repo), ctx.tmpdir, gate, acc_pass=acc_pass)
    _call_role(ctx, role="repair", agent=AGENTS["repair"], model=model, prompt=prompt,
              cwd=ctx.repo, label=f"repair it{iteration} attempt{attempt}")


# ------------------------------------------------------------------ gate
def _run_role_with_gate(ctx: RunContext, *, role: str, iteration: int, scope: str | None,
                        start_attempt: int, rec: dict, human_answer: str | None,
                        reclaimed: dict | None, begin_iteration: int | None) -> dict:
    """One ``next()`` action's full attempt loop (native's
    ``for (attempt = n.attempt; attempt <= 2; attempt++)``), gating after
    every attempt. ``start_attempt`` (REVIEW-driver.md item 9) is
    ``next()``'s own ``attempt``: 1 for a fresh pass, or 2 when this
    iteration's ``lead-running``/``repair-running`` phase already recorded a
    failed gate attempt 1 (a resumed run, or one that failed the gate and
    was re-dispatched by a fresh `next()` call) — in which case attempt 1's
    *pass* (the full Lead plan+waves, or the first repair turn) is skipped
    entirely and this call goes straight to the attempt-2 retry prompt, per
    native. The Lead's attempt-2 retry additionally re-runs cleanup on any
    builder branches attempt 1 could not clean up (item 12, ``rec["kept"]``,
    native's post-solo ``trackKept``)."""
    g: dict | None = None
    for attempt in range(max(1, int(start_attempt or 1)), 3):
        if role == "lead" and attempt == 1:
            _run_lead_pass(ctx, iteration=iteration, rec=rec, human_answer=human_answer,
                           reclaimed=reclaimed, begin_iteration=begin_iteration)
        elif role == "lead":
            acc_pass = (prompts.acc_pass_lines("lead", str(ctx.live_mailbox), _acc_tool(ctx))
                       if ctx.acceptance else None)
            solo = prompts.solo_lead_prompt(
                iteration, attempt, "Finish this Lead pass.", str(ctx.live_mailbox),
                str(ctx.repo), ctx.tmpdir, gate=g, kept=rec.get("kept"),
                human_answer=human_answer, acc_pass=acc_pass)
            _call_role(ctx, role="lead", agent=AGENTS["lead"], model=_model_for(ctx.cfg, "lead"),
                      prompt=solo, cwd=ctx.repo, label=f"lead solo it{iteration}#{attempt}")
            kept = rec.get("kept") or []
            if kept:
                cl = _cleanup(ctx, branches=[k["branch"] for k in kept])
                rec["kept"] = _track_kept(kept, cl)
        else:
            _run_repair_pass(ctx, iteration=iteration, scope=scope, attempt=attempt, gate=g)
        g = steplib.gate(ctx.live_mailbox, ctx.repo, ctx.token, role=role, iteration=iteration,
                         attempt=attempt, acceptance=ctx.acceptance, acc=ctx.acc)
        if not g["ok"]:
            raise DriverStop("error", 3, f"gate it{iteration} {role} attempt{attempt}: {g['error']}")
        if ctx.acceptance:
            stop = steplib.acc_stop(g)
            if stop:
                raise DriverStop(stop["status"], stop.get("code") or 3, stop["reason"])
            ctx.acc = steplib.acc_take(ctx.acc, g, "gate")
        rec["gate_attempts"] = attempt
        if g["pass"] or g.get("final"):
            break
    if g is None:
        raise DriverStop("error", 3, f"{role}: no gate attempt left (attempt {start_attempt})")
    if not g["pass"]:
        raise DriverStop("error", 3,
                         f"gate breach after {role} (2 attempts): {g['failures']}")
    return g


# ------------------------------------------------------------ pin+evaluate
def _pin_and_evaluate(ctx: RunContext, *, iteration: int) -> dict:
    p = steplib.pin(ctx.live_mailbox, ctx.repo, ctx.token, iteration=iteration)
    if not p["ok"]:
        raise DriverStop("error", 3, f"pin it{iteration}: {p['error']}")
    if p.get("skip_evaluator"):
        return p
    model = _model_for(ctx.cfg, "evaluator")
    turn_timeout = _turn_timeout_for(ctx.cfg, "evaluator")
    eval_cwd = ctx.repo
    acc_lines = None
    if ctx.acceptance:
        # r19 S8.1: the driver pre-runs the pinned pack at the pin, as a
        # detached job (acceptance-freeze/-run/apply all can take minutes);
        # `step_long` polls `pending` the same way native's `stepLong` does.
        ar = steplib.step_long(steplib.acceptance_run, ctx.live_mailbox, ctx.repo, ctx.token,
                               iteration=iteration, sha=p["sha"], acc=ctx.acc)
        if not ar["ok"]:
            raise DriverStop("error", 3, f"acceptance-run it{iteration}: {ar['error']}")
        stop = steplib.acc_stop(ar)
        if stop:
            raise DriverStop(stop["status"], stop.get("code") or 3, stop["reason"])
        ctx.acc = steplib.acc_take(ctx.acc, ar, "acceptance-run")
        ctx.out(f"iteration {iteration}: frozen acceptance pre-run {ar.get('passed')}/"
               f"{ar.get('total')} PASS" + (f", {ar.get('unavailable')} UNAVAILABLE"
                                            if ar.get("unavailable") else ""))
        acc_lines = ["", ar.get("text", ""), "",
                    prompts.acc_evaluator_fragment(str(ctx.live_mailbox), _acc_tool(ctx))]
    prompt = prompts.evaluator_prompt(iteration, p, str(ctx.live_mailbox), str(ctx.repo),
                                      ctx.tmpdir, human_answer=p.get("human_answer"),
                                      acc_lines=acc_lines)
    result = _call_role(ctx, role="evaluator", agent=AGENTS["evaluator"], model=model,
                        prompt=prompt, cwd=eval_cwd, label=f"evaluator it{iteration}",
                        turn_timeout=turn_timeout)
    verdict, _scope = steplib.TL._first_verdict(ctx.live_mailbox / "VERDICT.md")
    bound = steplib.TL._fresh_evaluator_artifact(
        ctx.live_mailbox, iteration,
        {"evaluator_attempt": p["evaluator_attempt"], "pinned_sha": p["sha"]},
    )
    if verdict is None or not bound:
        retry_prompt = prompts.reprompt(
            "VERDICT.md has no parseable verdict bound to this pin "
            f"(attempt {p['evaluator_attempt']}, sha {p['sha']})",
            "N/A — write VERDICT.md directly, per your role instructions",
        )
        _call_role(ctx, role="evaluator", agent=AGENTS["evaluator"], model=model,
                  prompt=retry_prompt, cwd=eval_cwd, label=f"evaluator it{iteration} (reprompt)",
                  session_id=result.session_id)
    return p


def _apply(ctx: RunContext, *, iteration: int, pin: dict, rec: dict) -> dict:
    if ctx.acceptance:
        # `apply` runs the acceptance review (amendments, anti-thrash, the
        # SHIP gate) as a detached job too.
        a = steplib.step_long(steplib.apply, ctx.live_mailbox, ctx.repo, ctx.token,
                              iteration=iteration, attempt=pin["evaluator_attempt"],
                              acceptance=True, acc=ctx.acc)
    else:
        a = steplib.apply(ctx.live_mailbox, ctx.repo, ctx.token, iteration=iteration,
                          attempt=pin["evaluator_attempt"])
    if not a["ok"]:
        raise DriverStop("error", 3, f"apply it{iteration}: {a['error']}")
    if ctx.acceptance and a.get("acceptance"):
        ctx.acc = steplib.acc_take(ctx.acc, a, "apply")
        r = a["acceptance"]
        rec["acceptance"] = {**(rec.get("acceptance") or {}),
                             "verdict_in": r.get("verdict_in"),
                             "ship_refused": bool(r.get("ship_refused")),
                             "ship_gate": r.get("ship_gate")}
        if r.get("ship_refused"):
            ctx.acc_log["ship_refused"].append({"iteration": iteration, "became": a.get("verdict")})
            ctx.out(f"iteration {iteration}: the frozen-acceptance gate refused the SHIP "
                   f"(verdict becomes {a.get('verdict')})")
    return a


# --------------------------------------------------------------- registry
def _write_registry(root_mailbox: Path, **fields: Any) -> None:
    path = registry_path(root_mailbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_json(path)
            data.update({"schema": 1, "driver": HARNESS, "harness": HARNESS,
                        "mailbox": str(root_mailbox), "updated_at": _now_iso()})
            data.update({k: v for k, v in fields.items() if v is not None})
            _atomic_write_json(path, data)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# ------------------------------------------------------------------- run
def run(mailbox: str | Path, cfg: Any, *, mode: str = "start",
       max_iterations: int = 4, run_token: str | None = None,
       root_free: bool = True, out: Callable[[str], None] = print,
       _skip_validate: bool = False, acceptance: bool = False,
       isolate_workers: bool | None = None, slice_eval_concurrency: int | None = None,
       slice_eval_drain_seconds: float | None = None, kill_check: bool | None = None,
       poll_seconds: float | None = None) -> dict:
    """Drive one mailbox to a stop. See the module docstring / shared spec
    for the full sequence.

    Config is validated FIRST, before anything else is touched (mailbox
    lock, ``rootfree.prepare``, ``ocgen.generate``): an invalid config (a
    placeholder model id, mismatched lead/evaluator/acceptance tiers, an
    idle timeout below the 180s floor a provider's own silent retries need,
    ...) refuses with ``status: error, code: 3`` and modifies nothing.
    ``_skip_validate`` is a test-only escape hatch for tests that build a
    tiny-timeout ``Config`` on purpose; production callers never pass it.
    Tests may instead set ``TRIO_OPENCODE_TEST_TIMEOUTS=1``, which only lifts
    the ``idle_seconds >= 180`` floor (see ``config.validate``'s docstring).

    REVIEW-driver.md item 7: a process-exclusive ``fcntl.flock`` on this
    mailbox's own lock file is acquired first, before ``rootfree.prepare``,
    ``ocgen.generate`` or anything else touches the mailbox or repo, and held
    for the entire call. A busy lock refuses at once with code 9, modifying
    nothing — this is what makes a concurrent ``start``, a concurrent
    ``resume``, or one of each on the same mailbox all refuse identically and
    atomically (no pid-liveness race)."""
    if not _skip_validate:
        problems = _get_config_mod().validate(cfg)
        if problems:
            return {"status": "error", "code": 3,
                   "reason": "invalid config: " + "; ".join(problems), "harness": HARNESS}

    root_mailbox = Path(mailbox).resolve()
    token = run_token or f"oc-{hashlib.sha256(str(root_mailbox).encode()).hexdigest()[:12]}"

    try:
        repo0 = Path(_git_toplevel(root_mailbox))
    except DriverStop as stop:
        return {"status": stop.status, "code": stop.code, "reason": stop.reason,
               "harness": HARNESS}

    # D1: mode selection mirrors trioctl -- open-loop iff the mailbox has a
    # QUEUE.md (root mailbox, or root-free, an existing Lead worktree's
    # live mailbox). D12: settings resolve/refuse here too, before anything
    # is touched, same as config validation above.
    openloop = _get_openloop()
    is_open_loop = openloop.detect_open_loop(root_mailbox, root_free=root_free, repo=repo0)
    try:
        settings = openloop.resolve_settings(
            cfg, is_open_loop=is_open_loop, isolate_workers=isolate_workers,
            slice_eval_concurrency=slice_eval_concurrency,
            slice_eval_drain_seconds=slice_eval_drain_seconds, kill_check=kill_check,
            poll_seconds=poll_seconds,
        )
    except openloop.SettingsError as exc:
        return {"status": "error", "code": 2, "reason": exc.reason, "harness": HARNESS}
    for notice in settings.get("notices", ()):
        out(notice)

    lock_path = _driver_lock_path(repo0, root_mailbox)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            lock_fh.seek(0)
            held_pid = lock_fh.read().strip() or "?"
        except OSError:
            held_pid = "?"
        lock_fh.close()
        return {"status": "error", "code": 9,
                "reason": f"another trio-opencode driver is running on this mailbox "
                         f"(pid {held_pid})", "harness": HARNESS}

    try:
        lock_fh.seek(0)
        lock_fh.truncate()
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()

        # The driver's own scratch (``.trio-opencode/``) lives inside the
        # product repo; exclude it once, idempotently, in the common git dir
        # (every worktree shares it) before any role can write there -- both
        # modes (lockstep ``begin`` also calls this; open-loop never does).
        # Every declared repo has its own common dir, so each gets it too.
        _exclude_driver_scratch(repo0, *(
            _declared_repo_paths(openloop, root_mailbox) if is_open_loop else ()))

        if mode == "resume":
            # REVIEW-driver.md item 7: kill EVERY dead run's orphan opencode
            # turn (recorded in either possible `.driver.json` location) —
            # concurrent builders means there can be more than one live turn
            # at once, so this reads the current `turns` dict (keyed by
            # label) and falls back to the legacy singular `turn` field for
            # a `.driver.json` written by an older driver version.
            for p in _driver_json_paths(root_mailbox, repo0, root_free):
                data = _read_json(p)
                turns = data.get("turns")
                if isinstance(turns, dict) and turns:
                    live_turns = list(turns.values())
                else:
                    legacy = data.get("turn") or {}
                    live_turns = [legacy] if legacy else []
                for turn in live_turns:
                    pgid, pid = turn.get("pgid"), turn.get("pid")
                    if not pgid:
                        continue
                    runner = _get_runner()
                    try:
                        if pid and runner.is_opencode_process(pid):
                            runner.kill_process_group(pgid)
                    except Exception:  # noqa: BLE001 - best effort cleanup
                        pass

        cancel = threading.Event()
        # ``code`` follows which signal actually asked for the stop: SIGTERM
        # -> 143 (128 + signal number, the conventional shell/POSIX exit code
        # for "killed by SIGTERM"), SIGINT -> 130 (128 + 2) — both are still
        # reported as `status: cancelled`; only the CLI exit code differs.
        stop_now = {"flag": False, "code": 130}

        def _signal_handler(signum, _frame):  # noqa: ANN001
            stop_now["flag"] = True
            stop_now["code"] = 143 if signum == signal.SIGTERM else 130
            cancel.set()

        old_term = signal.signal(signal.SIGTERM, _signal_handler)
        old_int = signal.signal(signal.SIGINT, _signal_handler)

        lead_record = None
        exec_id = uuid.uuid4().hex
        try:
            repo = repo0
            live_mailbox = root_mailbox
            if root_free:
                # D2: multi-repo root-free -- PLAN.md `repos:` entries of the
                # ROOT mailbox, resolved without the aggregates map (it does
                # not exist until `prepare` creates it). Lockstep keeps
                # calling `prepare(root_mailbox)` with no declared repos
                # (unchanged behaviour: `declared=[]` is `prepare`'s own
                # "no repos:" default).
                declared = (openloop.declared_repos_for_prepare(root_mailbox)
                           if is_open_loop else [])
                lead_record = rootfree.prepare(root_mailbox, declared=declared)
                # repos that joined on resume are attached by `prepare`; the
                # aggregates (and their main checkouts) exist only now
                _exclude_driver_scratch(*(
                    p for info in (lead_record.repos or {}).values()
                    for p in (info.get("main"), info.get("path"))))
                # `lead_record.repo` is the *root* checkout's identity path
                # (fine for rootfree.py's own ref-name-based git calls, and
                # for git-common-dir identity); every native step op below
                # instead needs a checkout whose bare `HEAD` IS the Lead's
                # current branch (`op_dispatch`'s `TL._git_head(root)`, the
                # commit gate, ...), which is the Lead's own worktree.
                repo = Path(lead_record.path)
                live_mailbox = lead_record.live_mailbox

            ocgen = _get_ocgen()
            runner_mod = _get_runner()
            exec8 = exec_id[:8]
            # REVIEW-driver.md item 13: logs live under the git-common-dir
            # (survives root-free teardown of the Lead worktree), never
            # inside the live mailbox; the run's own ``run_dir`` is the same
            # place the driver lock lives (a worktree shares its parent
            # repo's git-common-dir).
            run_dir = lock_path.parent.parent / f"run-{exec8}"
            log_dir = run_dir / "logs"
            # Detect the CLI's style ONCE per run (v2 primary target, v1 via
            # feature detection — SPEC.md) so ocgen generates the matching
            # config shape; a binary too old/broken to answer `run --help`
            # usefully still resolves to "v2" here (ocgen's own default) —
            # every actual turn re-runs (cached) detection itself and is the
            # one that refuses a truly unsupported binary.
            caps = runner_mod.detect_cli(
                getattr(cfg, "opencode_bin", "opencode"), dict(os.environ), cwd=str(repo),
            )
            cli_style = caps.style if caps.style in ("v1", "v2") else "v2"
            # ocgen's `repo_root` is where the role bodies live —
            # `opencode-driver/agents/trio-<role>.md` for lead/evaluator/
            # builder/repair, `opencode/agents/trio-scout.md` for scout —
            # this driver's OWN checkout (steplib.REPO_ROOT, same fixed root
            # doctor.py's `_repo_root()` uses), never the arbitrary product
            # repo the mailbox happens to live in.
            env = ocgen.generate(run_dir, cfg, steplib.REPO_ROOT, live_mailbox, style=cli_style)

            ctx = RunContext(root_mailbox=root_mailbox, live_mailbox=live_mailbox, repo=repo,
                             cfg=cfg, token=token, exec_id=exec_id, run_dir=run_dir,
                             log_dir=log_dir, env=env, root_free=root_free,
                             lead_record=lead_record, out=out, cancel=cancel,
                             cancel_code=stop_now, acceptance=acceptance)
            ctx.cli_style = cli_style
            ctx.agents_root = steplib.REPO_ROOT
            if acceptance:
                # r19: the directory steplib's `_opencode_author_audit`
                # override reads the author turn's persisted tool-call JSONL
                # from (see `_persist_author_tool_calls`); set once per run,
                # before any acceptance op can run.
                steplib.AUTHOR_TOOLCALLS_DIR = run_dir / "acceptance-audit"
                steplib.AUTHOR_TOOLCALLS_DIR.mkdir(parents=True, exist_ok=True)

            # D2: open-loop REPLACES `_drive` (never calls steplib.begin/
            # next/etc. -- native's `.lock` and TL's `.lock` are different
            # code on the same dir and not reentrant); everything above
            # (validation, kernel flock, resume orphan kill, signal
            # handlers, rootfree.prepare, ocgen.generate, RunContext) is
            # unchanged either way.
            if is_open_loop:
                return openloop.drive(ctx, mode=mode, max_iterations=max_iterations,
                                      settings=settings, stop_now=stop_now)
            return _drive(ctx, mode=mode, max_iterations=max_iterations, stop_now=stop_now)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def _declared_repo_paths(openloop: Any, root_mailbox: Path) -> list[str]:
    """PLAN.md ``repos:`` checkouts of *root_mailbox* (best effort: a plan
    that is missing or does not parse yet simply declares none)."""
    try:
        return [d["path"] for d in openloop.declared_repos_for_prepare(root_mailbox)]
    except Exception:  # noqa: BLE001 - never let the exclude step stop a run
        return []


def _exclude_driver_scratch(*repos: "str | Path | None") -> None:
    """Best-effort :func:`steplib.ensure_exclude` for each repo (``None`` and
    non-repos skipped): the driver's ``.trio-opencode/`` scratch must never
    show as an untracked product path at SHIP retirement, in ANY repo the
    run's builders, evals or agents write into, not just repo0."""
    for repo in dict.fromkeys(str(r) for r in repos if r):
        try:
            steplib.ensure_exclude(repo)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _git_toplevel(path: Path) -> str:
    import subprocess
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise DriverStop("error", 3, f"{path}: not a git repository")
    return r.stdout.strip()


def _normalize_resume_phase(ctx: RunContext) -> None:
    """Bug 1, crash-window case: a run killed mid-turn never reached
    ``_call_role``'s ``finally`` restore, so STATE `phase` (and the rest of
    :data:`OWNED_STATE_KEYS`) can still be whatever a role turn left them as.
    `needs_human` legitimately stores a reason in `phase`, and
    error/shipped/needs_land/... have their own phases — only `status:
    running` is ever touched here. Best effort: never raises.

    Must be called BEFORE this run's own ``ctx.write_driver_json`` call
    (the "begin" phase write), so the crashed run's ``.driver.json`` --
    specifically its ``state_snapshot`` -- is still on disk to read.

    Preferred path (oc-fix-eval-2/3): the persisted snapshot is read and
    applied BEFORE the `_RESUME_OK_PHASES` early return below, not after --
    a guarded turn that corrupts `evaluated_sha`/`evaluator_attempt`/
    `iteration` while leaving `phase` at one of `next()`'s own OK-looking
    phases (`lead-done`, `lead-running`, ...) is still a crash mid guarded
    turn, and the early return must never skip the restore just because the
    corrupted `phase` happens to look valid. `_call_role` persists the
    pre-turn OWNED_STATE_KEYS snapshot into ``.driver.json`` before every
    guarded (lead/repair/evaluator) turn starts; when a snapshot from THIS
    run is present, it is restored verbatim -- this is exact, unlike
    guessing a phase from `next()`'s own role-selection logic, and it is the
    only path that also recovers `evaluated_sha`/`evaluator_attempt`/
    `iteration` for an Evaluator-turn crash (REVIEW-driver.md: a stale
    persisted `evaluated_sha` must never be reused). The snapshot is cleared
    afterwards so a later crash/resume never reapplies it.

    The snapshot is tied to the run that wrote it via ``run_token`` (the
    same field `write_driver_json` already puts at the top level of the
    very ``.driver.json`` the snapshot lives in) -- `exec_id` is NOT usable
    for this: it is freshly generated on every `run()` call, including the
    resume that is meant to consume this very snapshot, so comparing it
    would reject every legitimate restore. `run_token` is the closest stable
    field instead: an explicit ``--run-token`` stays the same across a
    start/resume pair on the same mailbox, and the default (a hash of the
    mailbox path) is deterministic, so it too matches across that pair. A
    `.driver.json` with no `run_token` at all predates this check and is
    treated as a match. A recorded `run_token` that does not match THIS
    run's -- a different harness invocation sharing the mailbox, or a stale
    file left behind by a switch to a driver that never writes
    ``.driver.json`` at all (native) -- must never roll STATE back to that
    other run's cursor; the snapshot is ignored (as if absent) and, since
    this run never adopts it, it is cleared from ``.driver.json`` the moment
    this run's own `write_driver_json` next runs (`ctx.state_snapshot` is
    already `None` on a fresh `RunContext`).

    Fallback (no usable snapshot -- an older ``.driver.json``, a
    run-identity mismatch, or a best-effort read failure): rewrite `phase`
    to exactly what the helper's own ``next()`` would have written when it
    bumped this iteration into `lead-running`/`repair-running` (``op_next``'s
    ``role = "repair" if TL._counter(mailbox / ".repairs") else "lead"``) --
    UNLESS the Evaluator's pin has already landed (`evaluator_attempt` and
    `evaluated_sha` both set), in which case the crash can only have been
    inside the Evaluator turn itself and `phase` normalises back to its own
    precondition, `lead-done`, instead of re-running the Lead.
    """
    path = ctx.live_mailbox / "STATE.md"
    try:
        state = steplib.TL._read_state(path)
    except Exception:  # noqa: BLE001 - best effort guard
        return
    if not isinstance(state, dict):
        return
    if str(state.get("status", "")).strip().lower() != "running":
        return
    phase = str(state.get("phase", "")).strip()

    snapshot: dict[str, Any] | None = None
    try:
        prior = _read_json(ctx.driver_json_path)
        snap = prior.get("state_snapshot") if isinstance(prior, dict) else None
        if isinstance(snap, dict) and snap:
            prior_token = prior.get("run_token") if isinstance(prior, dict) else None
            if prior_token is None or prior_token == ctx.token:
                snapshot = snap
    except Exception:  # noqa: BLE001 - best effort guard
        snapshot = None

    if snapshot is not None:
        try:
            changed = {k: str(snapshot.get(k, "")) for k in OWNED_STATE_KEYS
                      if state.get(k, "") != snapshot.get(k, "")}
            if changed:
                steplib.TL._update_state(path, changed)
                desc = ", ".join(f"{k} {state.get(k, '')!r} -> {v!r}" for k, v in changed.items())
                line = (f"- iter {state.get('iteration', '?')} | loop | resume: restored "
                       f"driver-owned STATE key(s) from pre-turn snapshot: {desc}")
                steplib.TL._append_log(ctx.live_mailbox, line)
                ctx.out(line)
        except Exception:  # noqa: BLE001 - best effort guard
            pass
        # Clear in-memory; `_drive`'s next `write_driver_json` call persists
        # the clear, so a later crash/resume never reapplies a stale snapshot.
        ctx.state_snapshot = None
        return

    if phase.lower() in _RESUME_OK_PHASES:
        return

    if (str(state.get("evaluator_attempt", "")).strip()
            and str(state.get("evaluated_sha", "")).strip()):
        new_phase = "lead-done"
    else:
        try:
            repairs = steplib.TL._counter(ctx.live_mailbox / ".repairs")
        except Exception:  # noqa: BLE001 - best effort guard
            repairs = 0
        new_phase = "repair-running" if repairs else "lead-running"
    try:
        steplib.TL._update_state(path, {"phase": new_phase})
        line = (f"- iter {state.get('iteration', '?')} | loop | resume: normalised STATE "
               f"phase {phase!r} -> {new_phase!r}")
        steplib.TL._append_log(ctx.live_mailbox, line)
        ctx.out(line)
    except Exception:  # noqa: BLE001 - best effort guard
        pass


def _drive(ctx: RunContext, *, mode: str, max_iterations: int, stop_now: dict) -> dict:
    begin_models = None
    if ctx.acceptance:
        begin_models = {"lead": _model_for(ctx.cfg, "lead"),
                       "evaluator": _model_for(ctx.cfg, "evaluator"),
                       "acceptance": _model_for(ctx.cfg, "acceptance")}
    begin = steplib.begin(ctx.live_mailbox, ctx.repo, ctx.token,
                          acceptance=ctx.acceptance, models=begin_models)
    if not begin["ok"]:
        err = str(begin.get("error", ""))
        # REVIEW-driver.md item 7: a begin refused because the mailbox's own
        # (protocol) lock is held by someone else is a refusal, not a bare
        # error — same exit code (9) as this driver's own kernel lock.
        locked = "locked" in err.lower()
        return {"status": "refused" if locked else "error", "code": 9 if locked else 3,
               "reason": f"begin: {err}", "harness": HARNESS}
    if ctx.acceptance:
        # `begin` refuses an explicit models.acceptance/an off-tier Lead
        # before it even takes the lock -- see `exc_id` check below for the
        # case where begin itself answered a stop (tier refusal, a sealed
        # acc record that does not verify, ...).
        if not re.match(r"^[0-9a-f]{32}$", str(begin.get("exec_id") or "")):
            return {"status": "error", "code": 3,
                   "reason": "begin: no run-execution id (exec_id) from the helper",
                   "harness": HARNESS}
        stop = steplib.acc_stop(begin)
        if stop:
            return {"status": stop["status"], "code": stop.get("code") or 3,
                   "reason": stop["reason"], "harness": HARNESS,
                   "acceptance": {"enabled": True}}
        ctx.acc = steplib.acc_take(ctx.acc, begin, "begin")
    ctx.tmpdir = begin.get("tmpdir")
    begin_iteration = begin.get("iteration", 0)
    reclaimed = begin.get("reclaimed")
    # Bug 1 crash-window case: once, before the first `next()` of this
    # process's run (whether `mode` is "start" or "resume" — a `start` whose
    # mailbox was already `status: running` from an earlier crashed run goes
    # through `begin`'s own reclaim path, not a fresh one, so the same
    # normalisation applies) — and BEFORE this run's own `write_driver_json`
    # below, which would otherwise overwrite `.driver.json` (and the crashed
    # run's `state_snapshot` on it) before `_normalize_resume_phase` can
    # read it.
    _normalize_resume_phase(ctx)
    ctx.write_driver_json(phase="begin", iteration=begin_iteration)
    _write_registry(ctx.root_mailbox, live_mailbox=str(ctx.live_mailbox), repo=str(ctx.repo),
                    lead_worktree=str(ctx.lead_record.path) if ctx.lead_record else None,
                    branch=ctx.lead_record.branch if ctx.lead_record else None,
                    target=ctx.lead_record.target if ctx.lead_record else None,
                    run_token=ctx.token, exec_id=ctx.exec_id, pid=os.getpid(), state="running",
                    begun_at=_now_iso(), acceptance_enabled=(True if ctx.acceptance else None))
    _log_reclaimed(ctx, reclaimed)

    final: dict[str, Any] = {}
    try:
        while True:
            if stop_now["flag"]:
                raise DriverStop("cancelled", stop_now.get("code", 130), "cancelled by signal")
            n = steplib.next_(ctx.live_mailbox, ctx.repo, ctx.token, max_iterations=max_iterations,
                              acceptance=ctx.acceptance, acc=ctx.acc)
            if not n["ok"]:
                raise DriverStop("error", 3, f"next: {n['error']}")
            action = n["action"]
            if action == "stop":
                final = {"status": _final_status(n), "code": n.get("code"),
                        "verdict": n.get("verdict"), "iteration": n.get("iteration"),
                        "commit_shas": n.get("commit_shas") or [],
                        "retirement_fold": n.get("retirement_fold"),
                        "human_check": n.get("human_check")}
                break
            if ctx.acceptance:
                stop = steplib.acc_stop(n)
                if stop:
                    final = {"status": stop["status"], "code": stop.get("code") or 3,
                            "reason": stop["reason"]}
                    break
                ctx.acc = steplib.acc_take(ctx.acc, n, "next")
            iteration = n["iteration"]
            ctx.write_driver_json(phase=f"{action}-running", iteration=iteration)
            # Native pushes one `iterations[]` record per next() action, even
            # "evaluate" (role: null) — REVIEW-driver.md item 11.
            rec: dict[str, Any] = {"iteration": iteration,
                                   "role": None if action == "evaluate" else action}
            ctx.iterations.append(rec)
            if (ctx.acceptance and action in ("lead", "repair")
                    and not steplib.acc_frozen(ctx.acc)):
                # r19: the author phase precedes every role of this run
                # until the pack is frozen (iteration 1 of a fresh loop:
                # before the Lead).
                _author_phase(ctx, iteration=iteration)
            if action == "evaluate":
                # Resumed from a `lead-done`/`repair-done`-equivalent phase:
                # the gate already passed in an earlier (crashed) run of
                # this iteration; go straight to pin+evaluate.
                pass
            elif action in ("lead", "repair"):
                _log_human_notes(ctx, n.get("human_notes"))
                _run_role_with_gate(
                    ctx, role=action, iteration=iteration, scope=n.get("scope"),
                    start_attempt=n.get("attempt", 1), rec=rec,
                    human_answer=n.get("human_answer"), reclaimed=reclaimed,
                    begin_iteration=begin_iteration,
                )
            else:
                raise DriverStop("error", 3, f"next: unknown action {action!r}")
            pin = _pin_and_evaluate(ctx, iteration=iteration)
            a = _apply(ctx, iteration=iteration, pin=pin, rec=rec)
            # Bug 3 defensive read: `step_long`'s own exhausted-poll result
            # (`ok: False`) is already turned into a DriverStop by `_apply`
            # itself before this line is ever reached, but `a.get` (never a
            # bare subscript) keeps this immune to any other helper result
            # shape that lacks `stop`.
            if a.get("stop"):
                final = {"status": _final_status(a), "code": a.get("code"),
                        "verdict": a.get("verdict"), "iteration": iteration,
                        "commit_shas": a.get("commit_shas") or [],
                        "retirement_fold": a.get("retirement_fold"),
                        "human_check": a.get("human_check")}
                break
    except DriverStop as stop:
        final = {"status": stop.status, "code": stop.code, "reason": stop.reason, **stop.extra}

    final.setdefault("role_denials", ctx.role_denials)
    final.setdefault("commit_shas", [])
    final["harness"] = HARNESS
    # REVIEW-driver.md item 11: native's `iterations[]`/`reclaimed_builders`.
    final["iterations"] = ctx.iterations
    final["reclaimed_builders"] = reclaimed
    # REVIEW-driver.md item 13: where this run's per-turn event logs live.
    final["logs_dir"] = str(ctx.log_dir)
    final["lock"] = None
    final["lead_worktree"] = str(ctx.lead_record.path) if ctx.lead_record else None
    final["branch"] = ctx.lead_record.branch if ctx.lead_record else None
    final["land"] = None

    # REVIEW-driver.md item 8: land (while still holding the mailbox lock)
    # -> end (release it) -> write the result to the live mailbox -> only
    # then, if it actually landed, teardown (which also copies the result
    # back to the root mailbox) and re-write the (now teardown-complete)
    # result there too.
    land_result: dict[str, Any] | None = None
    if ctx.root_free and ctx.lead_record and final.get("status") == "shipped":
        land_result = rootfree.land(ctx.lead_record)
        final["land"] = land_result
        if land_result["status"] != "landed":
            final["status"] = "needs_land"
            final["code"] = 8
            steplib.TL._update_state(ctx.live_mailbox / "STATE.md",
                                     {"status": "needs_land",
                                      "phase": f"land-{land_result['phase']}"})
            try:
                steplib.TL._append_log(
                    ctx.live_mailbox,
                    f"- iter {final.get('iteration')} | loop | needs_land "
                    f"({land_result['phase']}): {land_result.get('detail')}",
                )
            except Exception:  # noqa: BLE001 - best effort
                pass

    e = steplib.end(ctx.live_mailbox, ctx.repo, ctx.token,
                    acceptance=ctx.acceptance, acc=ctx.acc)
    final["lock"] = e.get("lock")
    final["dangling_worktrees"] = e.get("dangling_worktrees")
    final["scratch_removed"] = e.get("scratch_removed")
    final["scratch_left"] = e.get("scratch_left")
    final["eval_worktrees_removed"] = e.get("eval_worktrees_removed")

    if ctx.acceptance:
        # req. 6: additive `acceptance` summary, folded from the helper's
        # own `end` facts (status/pin/checks/amendments/tamper/audit, when
        # `end` answered ok) plus this run's own script-side log (native's
        # `RESULT.acceptance`).
        base = e.get("acceptance") if e.get("ok") and isinstance(e.get("acceptance"), dict) else (ctx.acc or {})
        final["acceptance"] = {
            "enabled": True,
            **base,
            "coverage_refusals": ctx.acc_log["coverage_refusals"],
            "replanned": ctx.acc_log["replanned"],
            "ship_refused": ctx.acc_log["ship_refused"],
            "author_attempts": ctx.acc_log["author_attempts"],
        }
        if ctx.acc_log.get("author_isolation"):
            final["acceptance"]["author_isolation"] = ctx.acc_log["author_isolation"]

    try:
        ctx.driver_json_path.unlink()
    except OSError:
        pass
    _atomic_write_json(ctx.live_mailbox / RESULT_FILE, final)

    if land_result is not None and land_result.get("status") == "landed":
        final["teardown"] = rootfree.teardown(ctx.lead_record)
        _atomic_write_json(ctx.root_mailbox / RESULT_FILE, final)

    _write_registry(ctx.root_mailbox, state="finished", status=final.get("status"),
                    finished_at=_now_iso(),
                    acceptance=final.get("acceptance") if ctx.acceptance else None)
    return final
