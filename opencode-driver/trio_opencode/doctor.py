"""``trio-opencode doctor``: environment/config sanity checks, run before a
loop starts. Prints a single JSON report ``{"checks": [...], "ok": bool}``
and returns an exit code (0 all required checks pass, 1 otherwise).

The API key file is read here — and ONLY here, among everything this
package's own code does outside ``runner.py`` at turn-spawn time — to put
its value in the ``opencode models`` child process environment so the
catalog check can run with auth. The value is never printed, logged, or put
in the JSON report: every string this module builds for a detail message is
run through :func:`_scrub` first, which also guards against the child
process itself ever echoing the key back on stdout/stderr.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import ocgen

Config = config_mod.Config

#: Files that must exist for a functioning trio-opencode checkout (SPEC.md:
#: doctor checks these are present).
_REQUIRED_REPO_FILES = (
    Path("native") / "trio_native_step.py",
    Path("metrics") / "trio-shadow.py",
    Path("metrics") / "trio_loop.py",
)

_MIN_GIT_VERSION = (2, 30)
_MIN_PYTHON_VERSION = (3, 10)
#: v2.0.20 is the primary target; 1.18.33 (or any v1.18+) is still supported
#: through feature detection (SPEC.md "OpenCode v2.0.20 ... supersedes the
#: v1 facts where they differ").
_EXPECTED_OPENCODE_VERSION = "2.0.20"
_MIN_OPENCODE_VERSION = (1, 18)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
#: A YAML/JSON "key: value" line whose value is exactly (optionally quoted,
#: optionally trailed by a JSON comma) "ask".
_ASK_VALUE_RE = re.compile(r':\s*"?ask"?,?\s*$', re.MULTILINE)


def _repo_root() -> Path:
    # opencode-driver/trio_opencode/doctor.py -> parents[2] is the repo root
    # (same convention as opencode-driver/tests/conftest.py's REPO_ROOT).
    return Path(__file__).resolve().parents[2]


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _scrub(text: str, secrets: list[str]) -> str:
    """Never let a secret value reach a printed detail string. Also redacts
    any line mentioning authorization/api key headers, mirroring the
    runner's log-scrubbing rule (SPEC.md "Hard constraints")."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    lines = out.splitlines()
    scrubbed = []
    for line in lines:
        if re.search(r"authorization|api[_-]?key", line, re.IGNORECASE):
            scrubbed.append("[redacted]")
        else:
            scrubbed.append(line)
    return "\n".join(scrubbed)


def _parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    major, minor, patch = m.group(1), m.group(2), m.group(3) or "0"
    return (int(major), int(minor), int(patch))


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def _check_config(cfg: Config) -> dict[str, Any]:
    errors = config_mod.validate(cfg)
    if errors:
        return _check("config", False, "; ".join(errors))
    return _check("config", True, "ok")


def _check_model_tiers(cfg: Config) -> dict[str, Any]:
    tiers = {role: cfg.models.get(role) for role in ("lead", "evaluator", "acceptance")}
    distinct = {v for v in tiers.values() if v is not None}
    if len(distinct) <= 1:
        return _check("model_tiers", True, f"lead/evaluator/acceptance all use {distinct or {}}")
    return _check(
        "model_tiers", False,
        f"acceptance must use the same model as lead/evaluator, never cheaper: {tiers}",
    )


def _check_opencode_binary(cfg: Config, *, env: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    """Resolve ``cfg.opencode_bin`` against ``env``'s own ``PATH`` — never
    the bare ``shutil.which(cmd)`` default, which reads THIS PROCESS's real
    ``os.environ["PATH"]``. ``run()``'s caller passes an isolated test env
    (with a fake ``opencode`` first on its PATH) precisely so every later
    check that resolves this same binary never falls through to a real
    install elsewhere on the host PATH."""
    found = shutil.which(cfg.opencode_bin, path=env.get("PATH"))
    if found is None:
        return _check("opencode_binary", False, f"{cfg.opencode_bin!r} not found on PATH"), None
    return _check("opencode_binary", True, found), found


def _check_opencode_version(binary_path: str | None, *, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    if binary_path is None:
        return _check("opencode_version", False, "skipped: opencode binary not found")
    from . import runner as runner_mod  # noqa: PLC0415 - avoid a hard import cycle at module load
    try:
        proc = subprocess.run(
            [binary_path, "--version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
            env=runner_mod.pwd_env(env, str(cwd)), cwd=str(cwd),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("opencode_version", False, f"opencode --version failed: {exc}")
    text = (proc.stdout + proc.stderr).strip()
    version = _parse_version(text)
    if version is None:
        return _check("opencode_version", False, f"could not parse version from {text!r}")
    version_str = ".".join(str(p) for p in version)
    if version_str == _EXPECTED_OPENCODE_VERSION:
        return _check("opencode_version", True, version_str)
    if version[0] >= 2:
        return _check(
            "opencode_version", True,
            f"WARNING: expected {_EXPECTED_OPENCODE_VERSION}, found {version_str} (still v2.x, proceeding)",
        )
    if version[:2] >= _MIN_OPENCODE_VERSION:
        return _check(
            "opencode_version", True,
            f"WARNING: {version_str} is v1 (primary target is {_EXPECTED_OPENCODE_VERSION}); "
            "supported via feature detection only",
        )
    return _check(
        "opencode_version", False,
        f"opencode {version_str} is older than the minimum supported 1.18",
    )


def _check_cli_caps(binary_path: str | None, *, env: dict[str, str], cwd: Path) -> tuple[dict[str, Any], Any]:
    """Feature-detects ``binary_path``'s ``opencode run`` (SPEC.md "Feature
    detection") and fails outright when a required flag (json format,
    model, agent, session) is missing — exactly the condition
    ``runner.run_turn`` itself refuses a turn for."""
    if binary_path is None:
        return _check("cli_caps", False, "skipped: opencode binary not found"), None
    from . import runner as runner_mod  # noqa: PLC0415 - avoid a hard import cycle at module load
    caps = runner_mod.detect_cli(binary_path, env, cwd=str(cwd))
    if caps.missing:
        return _check(
            "cli_caps", False,
            f"unsupported opencode version: missing {', '.join(caps.missing)}",
        ), caps
    return _check("cli_caps", True, f"style={caps.style} version={caps.version or 'unknown'}"), caps


def _check_key_file(cfg: Config) -> dict[str, Any]:
    path = Path(cfg.provider.key_file)
    try:
        st = path.stat()
    except OSError as exc:
        return _check("key_file", False, f"cannot stat {path}: {exc}")
    if not stat.S_ISREG(st.st_mode):
        return _check("key_file", False, f"{path} is not a regular file")
    if st.st_size == 0:
        return _check("key_file", False, f"{path} is empty")
    mode = stat.S_IMODE(st.st_mode)
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return _check(
            "key_file", True,
            f"WARNING: {path} is readable by group/other (mode {oct(mode)}); "
            "recommend chmod 600",
        )
    return _check("key_file", True, f"{path} ok ({st.st_size} bytes, mode {oct(mode)})")


def _read_key(path: Path) -> str:
    """Read the key file's contents for use as a child-process env value
    only. Never logged, printed, or stored anywhere else."""
    return path.read_text(encoding="utf-8").replace("\r", "").replace("\n", "").strip()


def _check_git_version() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "--version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("git_version", False, f"git --version failed: {exc}")
    version = _parse_version(proc.stdout)
    if version is None:
        return _check("git_version", False, f"could not parse git version from {proc.stdout!r}")
    if version[:2] >= _MIN_GIT_VERSION:
        return _check("git_version", True, ".".join(str(p) for p in version))
    return _check(
        "git_version", False,
        f"git {'.'.join(str(p) for p in version)} is older than the minimum {_MIN_GIT_VERSION}",
    )


def _check_python_version() -> dict[str, Any]:
    v = sys.version_info
    if (v.major, v.minor) >= _MIN_PYTHON_VERSION:
        return _check("python_version", True, f"{v.major}.{v.minor}.{v.micro}")
    return _check(
        "python_version", False,
        f"python {v.major}.{v.minor} is older than the minimum {_MIN_PYTHON_VERSION}",
    )


def _check_repo_files(repo_root: Path) -> dict[str, Any]:
    missing = [str(p) for p in _REQUIRED_REPO_FILES if not (repo_root / p).is_file()]
    if missing:
        return _check("repo_files", False, f"missing: {', '.join(missing)}")
    return _check("repo_files", True, "ok")


def _generate_doctor_run(
    cfg: Config, repo_root: Path, base_env: dict[str, str], style: str,
) -> tuple[Path, dict[str, str]] | dict[str, Any]:
    """Generate an isolated OpenCode config dir under
    ``${XDG_STATE_HOME}/trio-agent-loop/opencode-doctor/`` for this doctor
    invocation. Returns ``(run_dir, ocgen_env)`` on success, or a failed
    check dict on error."""
    state_home = base_env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    root = Path(state_home) / "trio-agent-loop" / "opencode-doctor"
    try:
        root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))
        ocgen_env = ocgen.generate(
            run_dir=run_dir, cfg=cfg, repo_root=repo_root, mailbox=run_dir / "mailbox",
            style=style,
        )
    except (OSError, ocgen.OcgenError) as exc:
        return _check("generate_config", False, f"failed to generate opencode config: {exc}")
    return run_dir, ocgen_env


def _check_generated_agents(run_dir: Path, style: str) -> dict[str, Any]:
    """Check the generated config contains no "ask" permission value: the
    per-role agent/*.md files under v1, or the inline ``agent`` map in
    ``opencode.json`` itself under v2 (no .md files are written for it)."""
    offenders: list[str] = []
    if style == "v1":
        agent_dir = run_dir / "opencode" / "agent"
        for path in sorted(agent_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if _ASK_VALUE_RE.search(text):
                offenders.append(path.name)
    else:
        cfg_path = run_dir / "opencode" / "opencode.json"
        text = cfg_path.read_text(encoding="utf-8")
        if _ASK_VALUE_RE.search(text):
            offenders.append(cfg_path.name)
    if offenders:
        return _check("no_ask_permissions", False, f"'ask' permission value found in: {', '.join(offenders)}")
    return _check("no_ask_permissions", True, "ok")


def _check_no_auto_flag(repo_root: Path) -> dict[str, Any]:
    """Static check: ``runner.py``'s argv-building code never includes the
    literal ``"--auto"``, other than inside an assertion that checks it is
    absent (e.g. ``assert "--auto" not in argv``)."""
    runner_path = repo_root / "opencode-driver" / "trio_opencode" / "runner.py"
    if not runner_path.is_file():
        return _check("no_auto_flag", True, "skipped: runner.py not present yet")
    text = runner_path.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if "--auto" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "assert" in stripped and "not in" in stripped:
            continue
        offenders.append(f"line {lineno}: {stripped}")
    if offenders:
        return _check("no_auto_flag", False, f"'--auto' found outside an assertion: {'; '.join(offenders)}")
    return _check("no_auto_flag", True, "ok")


_MODEL_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _live_probe_model(
    binary_path: str, model_id: str, *, cfg: Config, cwd: Path, env: dict[str, str],
    log_dir: Path,
) -> tuple[bool, str]:
    """Live-probe one model id that ``opencode models`` didn't list. v2's
    catalog only lists providers registered in OpenCode's own auth store,
    never ones supplied purely via ``OPENCODE_API_KEY`` in the child env —
    so a model configured for env-key auth (e.g. ``opencode-go/*``)
    legitimately never appears there even though `opencode run -m ...`
    works fine with the key (live-verified). Runs a minimal
    ``opencode run --standalone --format json -m <id> "Reply with exactly:
    OK"`` turn (stdin DEVNULL, the same env-key + resolved ``PWD`` a real
    turn gets, ~150s timeout, one retry on a transient error — reusing
    ``runner.run_turn``'s own retry/timeout machinery rather than
    reimplementing it) and passes if it returns any text. Never raises;
    the returned detail string still needs the caller's ``_scrub``."""
    from . import runner as runner_mod  # noqa: PLC0415 - avoid a hard import cycle at module load
    label = "opencode-models-probe-" + (_MODEL_ID_SANITIZE_RE.sub("-", model_id).strip("-") or "model")
    spec = runner_mod.TurnSpec(
        role="doctor", agent="trio-scout", model=model_id,
        prompt="Reply with exactly: OK", cwd=str(cwd), env=env,
        turn_timeout=150.0, idle_timeout=150.0, max_attempts=2, backoff=(5.0,),
        label=label, log_dir=str(log_dir), opencode_bin=binary_path,
        key_file=cfg.provider.key_file,
    )
    result = runner_mod.run_turn(spec)
    if result.ok and (result.text or "").strip():
        return True, f"live probe ok ({result.kind}, {result.attempts} attempt(s))"
    return False, (
        f"live probe failed: {result.kind}: {result.error or 'no text returned'} "
        f"({result.attempts} attempt(s))"
    )


def _check_opencode_models(
    cfg: Config, binary_path: str | None, repo_root: Path, base_env: dict[str, str],
    generated: tuple[Path, dict[str, str]] | dict[str, Any] | None,
    caps: Any = None,
) -> dict[str, Any]:
    if binary_path is None:
        return _check("opencode_models", False, "skipped: opencode binary not found")
    if generated is None or isinstance(generated, dict):
        return _check("opencode_models", False, "skipped: no generated config (see generate_config check)")
    run_dir, ocgen_env = generated

    key_path = Path(cfg.provider.key_file)
    try:
        st = key_path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            raise OSError("key file missing or empty")
        key_value = _read_key(key_path)
    except OSError:
        return _check("opencode_models", False, "skipped: key file missing/empty (see key_file check)")
    if not key_value:
        return _check("opencode_models", False, "skipped: key file is empty after stripping CR/LF")

    from . import runner as runner_mod  # noqa: PLC0415 - avoid a hard import cycle at module load

    child_env = dict(base_env)
    child_env.update(ocgen_env)
    child_env[cfg.provider.key_env] = key_value
    child_env = runner_mod.pwd_env(child_env, str(repo_root))

    style = caps.style if caps is not None and getattr(caps, "style", None) in ("v1", "v2") else "v2"

    # v2's `models` subcommand takes `--standalone` (SPEC.md: "opencode
    # models --standalone (v2) / opencode models (v1)").
    argv = [binary_path, "models"]
    if style == "v2":
        argv.append("--standalone")
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60, env=child_env, cwd=str(repo_root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("opencode_models", False, _scrub(f"opencode models failed: {exc}", [key_value]))

    lines = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    configured = sorted(set(cfg.models.values()))
    missing = [mid for mid in configured if mid not in lines]
    if not missing:
        return _check(
            "opencode_models", True,
            f"all configured model ids present in catalog (opencode models): {', '.join(configured)}",
        )

    probe_log_dir = run_dir / "opencode-models-probe"
    probe_results = {
        mid: _live_probe_model(
            binary_path, mid, cfg=cfg, cwd=repo_root, env=child_env, log_dir=probe_log_dir,
        )
        for mid in missing
    }
    still_failing = [mid for mid in missing if not probe_results[mid][0]]
    catalog_ids = [m for m in configured if m not in missing]
    probed_ok_ids = [m for m in missing if probe_results[m][0]]

    if still_failing:
        reasons = "; ".join(f"{mid}: {probe_results[mid][1]}" for mid in still_failing)
        detail = (
            f"model id(s) unreachable via catalog (opencode models) or live probe: "
            f"{reasons}"
        )
        return _check("opencode_models", False, _scrub(detail, [key_value]))

    parts = []
    if catalog_ids:
        parts.append(f"in catalog: {', '.join(catalog_ids)}")
    if probed_ok_ids:
        parts.append(f"via live probe: {', '.join(probed_ok_ids)}")
    detail = "all configured model ids present (" + "; ".join(parts) + ")"
    return _check("opencode_models", True, _scrub(detail, [key_value]))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(cfg: Config, *, env: dict[str, str] | None = None, out: Callable[[str], None] = print) -> int:
    """Run every doctor check for ``cfg``, print the JSON report via
    ``out``, and return 0 if every check passed, else 1.

    ``env`` overrides the base environment used for locating XDG dirs and
    for subprocess calls (tests pass an isolated env instead of relying on
    the real process environment).
    """
    base_env = dict(env) if env is not None else dict(os.environ)
    repo_root = _repo_root()

    checks: list[dict[str, Any]] = []

    checks.append(_check_config(cfg))
    checks.append(_check_model_tiers(cfg))

    binary_check, binary_path = _check_opencode_binary(cfg, env=base_env)
    checks.append(binary_check)
    checks.append(_check_opencode_version(binary_path, env=base_env, cwd=repo_root))
    caps_check, caps = _check_cli_caps(binary_path, env=base_env, cwd=repo_root)
    checks.append(caps_check)
    style = caps.style if caps is not None and caps.style in ("v1", "v2") else "v2"

    checks.append(_check_key_file(cfg))

    checks.append(_check_git_version())
    checks.append(_check_python_version())
    checks.append(_check_repo_files(repo_root))

    generated = _generate_doctor_run(cfg, repo_root, base_env, style)
    if isinstance(generated, dict):
        checks.append(generated)
        checks.append(_check("no_ask_permissions", False, "skipped: no generated config"))
    else:
        checks.append(_check_generated_agents(generated[0], style))

    checks.append(_check_no_auto_flag(repo_root))
    checks.append(_check_opencode_models(cfg, binary_path, repo_root, base_env, generated, caps))

    ok = all(c["ok"] for c in checks)
    report = {"checks": checks, "ok": ok}
    out(json.dumps(report, indent=2))
    return 0 if ok else 1
