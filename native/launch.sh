#!/usr/bin/env bash
# trio-native lab launcher: one headless Claude Code session that only
# launches the saved `trio-native` workflow and returns its result JSON.
#
#   launch.sh start  --mailbox /abs/repo/loop [--max-iterations N] [--helper /abs/trio_native_step.py]
#                    [--run-token T] [--timeout SECONDS] [--acceptance]
#   launch.sh resume --mailbox /abs/repo/loop --run-id wf_… [--session UUID] [--timeout SECONDS]
#
# start   records a fresh --session-id and the exact args JSON (including
#         --run-token, generated when not given) in <mailbox>/.native-launch.json,
#         then runs the workflow. A start that never actually began the
#         workflow (lock refused, unparseable output, no `claude`) restores
#         the previous record.
# resume  (only after a kill or crash mid-run) reopens the SAME session with
#         --resume and asks for resumeFromRunId with the byte-identical args:
#         workflow journals live under the launching session. After a
#         held / error / budget result, use `start` again (a journal resume
#         would replay the same stop). The record is validated first (it is
#         mailbox data): canonical-UUID session_id, wf_ run id, args with only
#         the workflow's keys (mailbox = this one, bounded caps, allowlisted
#         models, this release's helper, a run_token); else exit 2.
#
# Flags: --permission-mode auto (auto mode; no permission-skipping flag is
# used or needed), --settings '{"worktree":{"baseRef":"head"}}' (isolated
# builders fork from the Lead's HEAD, not origin/HEAD; command-line only,
# no settings file is edited), --model opus, --output-format json.
# Run from the product repo (the cwd is the checkout builders fork from);
# the default cwd is the git toplevel of the mailbox.
#
# Output: the workflow's result JSON (parsed from the one fenced block the
# session is told to print) on stdout; the raw session output is kept under
# <mailbox>/.native-runs/. Exit 0 when a result was parsed, 3 when not,
# 2 on usage errors. Default --timeout: 6 h (vps-pool runs about 4 h).
#
# Dashboard records (native-dash): only for a run that actually started
# (its --run-token reached <mailbox>/.session.json via the workflow's
# `begin`), the same result (or the error) is written to
# <mailbox>/.native-result.json with the session id, the workflow run id
# (wf_…, found under $CLAUDE_CONFIG_DIR/projects/*/<session>/) and
# api_equiv_usd, and the run is registered in
# ${TRIO_NATIVE_RUNS_DIR:-~/.local/share/trio-agent-loop/native-runs}/<key>.json
# (key = first 16 hex of sha256(realpath mailbox)) so trio-dash finds it. A
# run that never started writes neither file; its outcome instead goes to
# <mailbox>/.native-runs/<session>.result.json. See README.md "Run registry
# and result".
#
# Frozen acceptance (r19, see "Frozen acceptance" in README.md) IS
# implemented by the native driver via `start --acceptance` (args.acceptance
# runs the full protocol). `resume` cannot carry it (metrics/native_args.py's
# resume schema does not know the `acceptance` key yet; such a record is
# refused -- start again). So when this run did NOT opt in via
# `start --acceptance` but the mailbox/environment nonetheless signals
# acceptance (acceptance/FROZEN present, or TRIO_ACCEPTANCE / the profile's
# `[acceptance] enabled` is on), that signal will be silently ignored for
# THIS execution: the launcher prints a loud stderr warning and records
# `"acceptance": "unsupported-in-native-v01"` (with `acceptance_detected`) in
# .native-result.json and the printed result. A `start --acceptance` run
# never gets this warning (it IS the supported path).
set -euo pipefail

CLAUDE_BIN="${TRIO_NATIVE_CLAUDE:-claude}"
# Roles run pytest in the checkout and in builder worktrees: no .pyc files
# (probe 2 blocker A; `begin` also excludes the artefacts in info/exclude).
export PYTHONDONTWRITEBYTECODE=1
# Headless `claude -p` (2.1.280) terminates background tasks, and the
# Workflow is one, 600 s after the launching turn ends unless this is 0
# (probe 3 blocker E). Environment only: no settings file is involved.
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
MODEL="opus"
SETTINGS='{"worktree":{"baseRef":"head"}}'
SUFFIX='Launch only; do not edit files, settings or permissions; output the result JSON verbatim in one fenced block and stop.'

usage() { sed -n '2,31p' "$0" >&2; exit 2; }

[ $# -ge 1 ] || usage
mode="$1"; shift
case "$mode" in start|resume) ;; *) usage ;; esac

mailbox="" max_iterations="4" helper="" run_token="" run_id="" session="" timeout_s="21600" acceptance=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mailbox) mailbox="$2"; shift 2 ;;
    --max-iterations) max_iterations="$2"; shift 2 ;;
    --helper) helper="$2"; shift 2 ;;
    --run-token) run_token="$2"; shift 2 ;;
    --run-id) run_id="$2"; shift 2 ;;
    --session) session="$2"; shift 2 ;;
    --timeout) timeout_s="$2"; shift 2 ;;
    --acceptance) acceptance="1"; shift ;;
    *) echo "launch.sh: unknown argument $1" >&2; usage ;;
  esac
done
if [ "$mode" = resume ] && [ -n "$acceptance" ]; then
  echo "launch.sh: --acceptance is a start flag (resume replays the recorded args)" >&2; exit 2
fi
case "$mailbox" in /*) ;; *) echo "launch.sh: --mailbox must be an absolute path" >&2; exit 2 ;; esac
[ -d "$mailbox" ] || { echo "launch.sh: $mailbox is not a directory" >&2; exit 2; }
[[ "$timeout_s" =~ ^[0-9]{1,6}$ ]] || { echo "launch.sh: --timeout must be whole seconds" >&2; exit 2; }
# The helper and this launcher always come from the same directory (one
# resolved release root): the workflow never falls back to CURRENT's helper.
self_dir="$(cd "$(dirname "$0")" && pwd -P)"
own_helper="$self_dir/trio_native_step.py"
repo="$(git -C "$mailbox" rev-parse --show-toplevel)"
record="$mailbox/.native-launch.json"
runs="$mailbox/.native-runs"

# prepare (python): every value that reaches the claude argv or prompt is
# validated here against a strict schema, and every mailbox file is read and
# written without following symlinks (README "Launcher hardening"). Mailbox
# data (a committed .native-launch.json) never chooses a flag, a helper, a
# session outside the canonical-UUID form, or free prompt text. Prints
# session, token, args JSON, previous-record copy and the pre-launch claim
# snapshot, one per line; exit 2 (nothing written) on any refusal.
prep="$(TRIO_NATIVE_LAUNCH_ACCEPTANCE="$acceptance" python3 - "$mode" "$mailbox" "$max_iterations" "$helper" "$run_token" "$run_id" \
  "$session" "$own_helper" <<'PY'
import hashlib, importlib.util, json, os, re, stat, sys, tempfile, uuid
mode, mailbox, max_it, helper, token, run_id, session, own_helper = sys.argv[1:9]
SIDECARS = (".native-launch.json", ".native-launch.json.tmp", ".native-result.json",
            ".session.json", ".native-runs", ".lock")


def refuse(msg):
    print("launch.sh: " + msg, file=sys.stderr)
    sys.exit(2)


# The one resume-args / mailbox-path validator, shared byte-identically with
# trio-dash (eval3 findings 4, 6, 7): this release's metrics/native_args.py,
# next to this launcher's native/ dir (never a repository's copy).
_na_path = os.path.join(os.path.dirname(os.path.dirname(own_helper)), "metrics", "native_args.py")
try:
    _spec = importlib.util.spec_from_file_location("launch_native_args", _na_path)
    NA = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(NA)
except (OSError, ImportError, SyntaxError, AttributeError) as exc:
    refuse(f"this release has no usable {_na_path} ({exc})")
TOKEN_RE = NA.RUN_TOKEN_RE


def read_nofollow(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        refuse(f"{path}: {exc} (a symlink is never followed)")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            refuse(f"{path} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def write_atomic(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def digest(path):
    try:
        raw = read_nofollow(path)
    except SystemExit:
        return "refused"
    return hashlib.sha256(raw).hexdigest() if raw is not None else "-"


real = os.path.realpath(mailbox)
if len(mailbox) > 1:
    mailbox = mailbox.rstrip("/") or "/"
problem = NA.path_problem(mailbox) or NA.path_problem(real)
if not problem and not NA.is_canonical_path(mailbox):
    problem = "is not canonical (a '.', '..' or empty component)"
if problem:
    # Printable paths (spaces, non-ASCII, punctuation) are fine: the path is
    # one argv element and reaches prompts only JSON-encoded (args) or
    # shell-quoted (trio-native.js); control characters never do.
    refuse(f"--mailbox {problem}")
for name in SIDECARS:
    path = os.path.join(mailbox, name)
    if os.path.islink(path):
        refuse(f"{path} is a symlink; refusing to read or write through it")
runs = os.path.join(mailbox, ".native-runs")
for sub in (runs, os.path.join(mailbox, ".lock")):
    if os.path.isdir(sub) and not os.path.islink(sub):
        for entry in os.scandir(sub):
            if entry.is_symlink():
                refuse(f"{entry.path} is a symlink; refusing to run in this mailbox")
own_helper_real = os.path.realpath(own_helper)
record = os.path.join(mailbox, ".native-launch.json")


def check_helper(value, what):
    if not isinstance(value, str) or not os.path.isabs(value) \
            or os.path.realpath(value) != own_helper_real:
        refuse(f"{what} is not this release's helper ({own_helper_real}); the helper always "
               "comes from the launcher's own release directory")


if mode == "start":
    if helper:
        check_helper(helper, "--helper")
    problem = NA.path_problem(own_helper_real)
    if problem:
        refuse(f"the helper path {own_helper_real!r} {problem}")
    if not re.fullmatch(r"[0-9]{1,3}", max_it) or not 1 <= int(max_it) <= 200:
        refuse("--max-iterations must be an integer 1..200")
    session = str(uuid.uuid4())
    # A run_token that trio-native.js's own validation would reject is
    # treated as not given, so the token we track is the one begin uses.
    if not TOKEN_RE.fullmatch(token or ""):
        token = "ls-" + session.replace("-", "")[:12]
    args = {"mailbox": mailbox, "max_iterations": int(max_it), "helper": own_helper_real,
            "run_token": token}
    if os.environ.get("TRIO_NATIVE_LAUNCH_ACCEPTANCE") == "1":
        # r19 frozen acceptance (args.acceptance). A resume replays recorded
        # args through metrics/native_args.py, which does not know the key
        # yet: such a record is refused there (start again instead).
        args["acceptance"] = True
    args_json = json.dumps(args, separators=(",", ":"))
    # Keep the previous record (a start that never began the workflow
    # restores it); copied without following links, to a fresh name.
    prev = ""
    old = read_nofollow(record)
    if not os.path.exists(runs):
        os.mkdir(runs)
    if old is not None:
        prev = os.path.join(runs, f"launch-record.{session}.prev.json")
        fd = os.open(prev, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, old)
        finally:
            os.close(fd)
    write_atomic(record, (json.dumps({"session_id": session, "args": args_json}, indent=2)
                          + "\n").encode("utf-8"))
else:
    if not NA.RUN_ID_RE.fullmatch(run_id or ""):
        refuse("resume needs --run-id wf_[A-Za-z0-9_-]{1,64}")
    raw = read_nofollow(record)
    if raw is None:
        refuse(f"no {record}: nothing to resume (use start)")
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        refuse(f"{record} is not JSON; use start")
    if not isinstance(rec, dict):
        refuse(f"{record} is not a JSON object; use start")
    try:
        recorded = NA.canonical_session_id(rec.get("session_id"))
    except NA.NativeArgsError:
        refuse("the recorded session_id is not a canonical UUID; use start")
    if session:
        try:
            NA.canonical_session_id(session)
        except NA.NativeArgsError:
            refuse("--session must be a canonical UUID")
    session = session or recorded
    # The same schema trio-dash previews with (metrics/native_args.py).
    raw_args = rec.get("args")
    if not isinstance(raw_args, str):
        refuse("the recorded args are not a JSON object; use start")
    try:
        args = NA.validate_args(raw_args, mailbox=mailbox, helper=own_helper_real,
                                require_run_token=True)
    except NA.NativeArgsError as exc:
        refuse(f"the recorded {exc}; use start")
    token = args["run_token"]
    args_json = NA.resume_args_json(args)  # rebuilt from validated fields
    prev = ""
if not os.path.exists(runs):
    os.mkdir(runs)  # only once every check passed: a refusal writes nothing
snapshot = "|".join(digest(os.path.join(mailbox, n)) for n in (".session.json", ".lock/pid"))
for value in (session, token, args_json, prev, snapshot):
    if "\n" in value:
        refuse("internal: multi-line value")
print(session)
print(token)
print(args_json)
print(prev)
print(snapshot)
PY
)" || exit 2
{ read -r session; read -r our_token; read -r args_json; read -r prev_record; read -r claim_snapshot; } <<<"$prep"

# `start --acceptance` IS the supported path (args.acceptance runs the full
# r19 protocol, same trust model as the Cursor path; see README.md "Frozen
# acceptance"). Only when THIS run did not opt in that way, but the mailbox
# or environment signals acceptance anyway (a frozen pack already present,
# or the switch resolves ON via TRIO_ACCEPTANCE / the profile's `[acceptance]
# enabled`, as trioctl resolves it), warn loudly: that signal has no effect
# on this execution (no frozen SHIP gate, pre-runs or amendments -- trio-
# shadow's pack guard still blocks pack tampering) and is recorded as
# `acceptance: unsupported-in-native-v01` in the result (r20 review F6,
# narrowed once native gained real `--acceptance` support).
acceptance_why=""
if [ -z "$acceptance" ]; then
acceptance_why="$(python3 - "$mailbox" <<'PY'
import json, os, sys
from pathlib import Path
why = []
if Path(sys.argv[1], "acceptance", "FROZEN").is_file():
    why.append("frozen pack: acceptance/FROZEN")
env = os.environ.get("TRIO_ACCEPTANCE", "").strip().lower()
if env in ("1", "true", "on", "yes"):
    why.append("TRIO_ACCEPTANCE=" + os.environ["TRIO_ACCEPTANCE"].strip())
elif env not in ("0", "false", "off", "no"):
    # the profile trioctl reads (`config_path`: TRIOCTL_CONFIG, else XDG), parsed
    # the way trioctl's `acceptance_settings` does (`bool(enabled)`: 1 and a
    # non-empty string such as "false" are ON)
    override = os.environ.get("TRIOCTL_CONFIG")
    if override:
        profile = Path(override).expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        profile = base / "trio-agent-loop" / "omnigent.toml"
    try:
        import tomllib
        table = tomllib.loads(profile.read_text(encoding="utf-8")).get("acceptance")
    except (OSError, ValueError, ImportError):
        table = None
    if isinstance(table, dict) and bool(table.get("enabled", False)):
        value = table.get("enabled")
        shown = repr(value) if isinstance(value, str) else json.dumps(value)
        why.append(f"profile: [acceptance] enabled = {shown} ({profile})")
print(json.dumps(why) if why else "")
PY
)" || acceptance_why=""
if [ -n "$acceptance_why" ]; then
  {
    echo "launch.sh: WARNING: ================================================================"
    echo "launch.sh: WARNING: frozen acceptance is signalled but this run did not opt in."
    echo "launch.sh: WARNING: detected: $acceptance_why"
    echo "launch.sh: WARNING: this run has NO frozen SHIP gate, pre-runs or amendments (the"
    echo "launch.sh: WARNING: trio-shadow pack guard still blocks tampering). Use \`launch.sh"
    echo "launch.sh: WARNING: start --acceptance\` (native) or \`trioctl omnigent loop --acceptance\`"
    echo "launch.sh: WARNING: (Cursor) for an acceptance-gated run. Recorded as"
    echo "launch.sh: WARNING: acceptance: unsupported-in-native-v01 in .native-result.json."
    echo "launch.sh: WARNING: ================================================================"
  } >&2
fi
fi  # [ -z "$acceptance" ]

if [ "$mode" = start ]; then
  prompt="Run the saved workflow trio-native with args ${args_json}. ${SUFFIX}"
  session_flags=(--session-id "$session")
else
  prompt="Resume the saved workflow trio-native with resumeFromRunId \"${run_id}\" and the byte-identical args ${args_json}. ${SUFFIX}"
  # A canonical UUID (validated above) is a single value, never a flag.
  session_flags=(--resume "$session")
fi

raw="$runs/${session}.$(date -u +%Y%m%dT%H%M%SZ).$mode.json"
# Fresh output files, created exclusively and without following a link.
python3 - "$raw" "$raw.err" <<'PY' || exit 2
import os, sys
for path in sys.argv[1:]:
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except OSError as exc:
        print(f"launch.sh: cannot create {path}: {exc}", file=sys.stderr)
        sys.exit(2)
PY
launcher_path="$self_dir/$(basename "$0")"
claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
launched_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
# TRIO_NATIVE_LAUNCH_{MAILBOX,TOKEN} are test-support only: they let a fake
# `claude` (tests/test_launch.py, tests/test_dash_records.py) simulate
# `begin` writing .session.json without parsing the prompt; the real
# `claude` binary ignores them.
(cd "$repo" && TRIO_NATIVE_LAUNCH_MAILBOX="$mailbox" TRIO_NATIVE_LAUNCH_TOKEN="$our_token" \
  timeout "$timeout_s" "$CLAUDE_BIN" -p "$prompt" \
  "${session_flags[@]}" \
  --model "$MODEL" \
  --permission-mode auto \
  --settings "$SETTINGS" \
  --output-format json) >>"$raw" 2>>"$raw.err"
rc=$?
set -e

python3 - "$raw" "$rc" "$session" "$mode" "$record" "${prev_record:-}" \
  "$mailbox" "$claude_dir" "${run_id:-}" "$launcher_path" "$launched_at" \
  "$our_token" "$claim_snapshot" "$self_dir" "$repo" "$acceptance_why" <<'PY'
import fcntl, glob, hashlib, json, os, re, stat, sys, tempfile, time
from pathlib import Path
raw, rc, session, mode, record, prev = sys.argv[1:7]
mailbox, claude_dir, given_run_id, launcher_path, launched_at = sys.argv[7:12]
our_token, claim_snapshot, self_dir, repo_dir, acceptance_why = sys.argv[12:17]
rc = int(rc)
launcher = {"session_id": session, "exit_code": rc, "raw": raw}
# r20 F6: frozen acceptance detected but this run did not opt in via
# `start --acceptance` (acceptance_why is only ever computed in that case).
ACCEPTANCE = ({"acceptance": "unsupported-in-native-v01",
               "acceptance_detected": json.loads(acceptance_why)} if acceptance_why else {})
launcher.update(ACCEPTANCE)


def _read_bytes(path) -> bytes | None:
    """A regular file's bytes without following a symlink (None otherwise)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_json(path):
    data = _read_bytes(path)
    try:
        value = json.loads(data.decode("utf-8")) if data is not None else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _regular_or_absent(path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return True
    except OSError:
        return False


# The saved workflow script should be this release's (README "Launcher
# hardening"): reported, never enforced (a checkout run is legitimate).
# Report the file Claude Code will actually run for `trio-native`, by its
# documented precedence for saved workflows (code.claude.com/docs/en/workflows
# "Save the workflow for reuse"): project workflows load from every
# `.claude/workflows/` between the working directory and the repository
# root, the one closest to the working directory winning; a project workflow
# shadows a personal one ($CLAUDE_CONFIG_DIR/workflows, default
# ~/.claude/workflows). The session runs with cwd = repo (the git toplevel).
WORKFLOW_FILE = "trio-native.js"


def _sha256(path):
    data = _read_bytes(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def workflow_script_facts(cwd, top, config_dir, release_dir) -> dict:
    """{workflow_script, workflow_script_scope (project|user|none),
    workflow_script_is_release, workflow_script_sha256,
    workflow_script_candidates: [{path, scope, exists, sha256}]} — the
    candidates in precedence order; the first existing one is the script."""
    dirs = []
    here = os.path.abspath(cwd)
    stop = os.path.abspath(top) if top else here
    while True:
        dirs.append(here)
        if here == stop or os.path.dirname(here) == here:
            break
        here = os.path.dirname(here)
    if stop not in dirs:  # cwd outside the toplevel: only cwd itself
        dirs = dirs[:1]
    candidates = [(os.path.join(d, ".claude", "workflows", WORKFLOW_FILE), "project")
                  for d in dirs]
    candidates.append((os.path.join(config_dir, "workflows", WORKFLOW_FILE), "user"))
    ours = os.path.join(release_dir, WORKFLOW_FILE)
    ours_real = os.path.realpath(ours)
    ours_bytes = _read_bytes(ours_real)
    listed, chosen = [], None
    for path, scope in candidates:
        real = os.path.realpath(path)
        digest = _sha256(real)
        exists = digest is not None
        listed.append({"path": path, "real_path": real, "scope": scope,
                       "exists": exists, "sha256": digest})
        if exists and chosen is None:
            chosen = listed[-1]
    if chosen is None:
        return {"workflow_script": None, "workflow_script_scope": "none",
                "workflow_script_is_release": False, "workflow_script_sha256": None,
                "workflow_script_candidates": listed}
    real = chosen["real_path"]
    is_release = real == ours_real or (
        ours_bytes is not None and _read_bytes(real) == ours_bytes)
    return {"workflow_script": real, "workflow_script_scope": chosen["scope"],
            "workflow_script_is_release": bool(is_release),
            "workflow_script_sha256": chosen["sha256"],
            "workflow_script_candidates": listed}


try:
    WORKFLOW_SCRIPT = workflow_script_facts(repo_dir, repo_dir, claude_dir, self_dir)
except OSError:
    WORKFLOW_SCRIPT = {"workflow_script": None, "workflow_script_scope": "unknown",
                       "workflow_script_is_release": False, "workflow_script_sha256": None,
                       "workflow_script_candidates": []}
launcher.update(WORKFLOW_SCRIPT)


def _runs_dir() -> Path:
    return Path(os.environ.get("TRIO_NATIVE_RUNS_DIR", "").strip()
                or Path.home() / ".local/share/trio-agent-loop/native-runs").expanduser()


def _registry_path() -> Path:
    real = str(Path(mailbox).resolve())
    return _runs_dir() / (hashlib.sha256(real.encode()).hexdigest()[:16] + ".json")


def _write_json_atomic(path: Path, data: dict) -> None:
    """mkstemp in the target's directory + rename (never a fixed temp name);
    a target that is a symlink or not a regular file is refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _regular_or_absent(path):
        raise OSError(f"{path} is a symlink or not a regular file")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _claim_digest(name) -> str:
    data = _read_bytes(Path(mailbox, name))
    return hashlib.sha256(data).hexdigest() if data is not None else "-"


def started() -> bool:
    """The workflow actually began iff its `begin` wrote our own run_token
    into <mailbox>/.session.json (README.md "Run registry and result"). A
    resume must also have re-taken the mailbox: .session.json or the lock
    pid changed during this launch (a stale record of the killed run with
    the same token never counts)."""
    data = _read_json(Path(mailbox, ".session.json"))
    if not (data and data.get("driver") == "claude-workflow"
            and data.get("session") == our_token):
        return False
    if mode == "resume":
        now = "|".join(_claim_digest(n) for n in (".session.json", ".lock/pid"))
        return now != claim_snapshot
    return True


STARTED = started()


def _restore_or_remove_record() -> None:
    """A START that did not actually begin the workflow must not leave a
    session/args record a later `resume` could target (eval-native-v0b N7,
    extended to every not-started start, not only a lock refusal). A resume
    never touches the record: a failed or refused resume keeps the only
    resume record (eval2 finding 4)."""
    if mode != "start":
        launcher["record"] = "kept"
        return
    if not _regular_or_absent(record):
        launcher["record"] = "left (not a regular file)"
        return
    if prev and _regular_or_absent(prev) and os.path.isfile(prev):
        os.replace(prev, record)
        launcher["record"] = "restored"
    else:
        try:
            os.remove(record)
        except OSError:
            pass
        launcher["record"] = "removed"


def find_run_id():
    """The workflow run id (wf_…) of this session, newest first."""
    if given_run_id:
        return given_run_id
    base = os.path.join(glob.escape(claude_dir), "projects", "*", glob.escape(session))
    found = []
    for pattern, rx in (
            (os.path.join(base, "workflows", "wf_*.json"), r"^(wf_[\w-]+)\.json$"),
            (os.path.join(base, "subagents", "workflows", "wf_*"), r"^(wf_[\w-]+)$"),
            (os.path.join(base, "workflows", "scripts", "*-wf_*.js"), r"-(wf_[\w-]+)\.js$")):
        for path in glob.glob(pattern):
            m = re.search(rx, os.path.basename(path))
            if m:
                try:
                    found.append((os.path.getmtime(path), m.group(1)))
                except OSError:
                    pass
    return max(found)[1] if found else None


def _update_registry(fields: dict) -> None:
    """Atomic read-modify-write of the run registry, flock-guarded (a
    sidecar `<registry file>.lock`, shared with the helper's own `_register`)
    so a concurrent writer's record for a different, possibly newer, run is
    never lost. Applied only when the existing record's run_token is ours,
    unset, or the record is already finished — otherwise a live different
    run owns it and this (belated) write backs off entirely."""
    reg = _registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    lock_path = reg.with_name(reg.name + ".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600)
        with os.fdopen(lock_fd, "a+", encoding="utf-8") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                if not _regular_or_absent(reg):
                    return
                data = _read_json(reg) or {}
                existing_token = data.get("run_token")
                if existing_token not in (None, our_token) and data.get("state") != "finished":
                    return
                data.update({"schema": 1, "driver": "claude-workflow",
                             "mailbox": str(Path(mailbox).resolve())})
                data.update(fields)
                _write_json_atomic(reg, data)
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def persist(result):
    """<mailbox>/.native-result.json and the run registry, for a run that
    actually started (never fatal)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    session_rec = _read_json(Path(mailbox, ".session.json")) or {}
    run_id = find_run_id()
    lau = result.get("launcher") if isinstance(result.get("launcher"), dict) else {}
    keep = ("status", "verdict", "code", "reason", "iteration", "held_step",
            "end_error", "conflicts", "dangling_worktrees", "role_denials",
            "human_check", "commit_shas", "lock", "agents_used",
            "eval_worktrees_removed", "acceptance")
    out = {k: result.get(k) for k in keep if k in result}
    out.update({"schema": 1, "source": "launcher", "driver": "claude-workflow",
                "mode": mode, "session_id": session, "run_id": run_id,
                "run_token": our_token, "exit_code": rc, "raw": raw,
                "launcher": launcher_path, "launched_at": launched_at,
                "finished_at": now,
                "session_started_at": session_rec.get("started_at"),
                "api_equiv_usd": lau.get("api_equiv_usd"),
                "api_equiv_usd_note": lau.get("api_equiv_usd_note")})
    out.update(WORKFLOW_SCRIPT)
    out.update(ACCEPTANCE)
    try:
        _write_json_atomic(Path(mailbox, ".native-result.json"), out)
    except OSError:
        pass
    _update_registry({"state": "finished", "status": out.get("status"),
                       "session_id": session, "run_id": run_id,
                       "run_token": our_token, "launcher": launcher_path,
                       "finished_at": now,
                       "result_path": str(Path(mailbox).resolve() / ".native-result.json"),
                       "updated_at": now, **WORKFLOW_SCRIPT,
                       **({"acceptance": out["acceptance"]}
                          if isinstance(out.get("acceptance"), dict) else {})})


def persist_not_started(result):
    """A run that never actually started never touches .native-result.json
    nor the registry; its outcome goes to a per-session file instead."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = dict(result)
    out.setdefault("launcher", {})
    if not isinstance(out["launcher"], dict):
        out["launcher"] = {}
    out["launcher"].update(launcher)
    out.update({"schema": 1, "source": "launcher", "driver": "claude-workflow",
                "mode": mode, "session_id": session, "started": False,
                "run_token": our_token, "launched_at": launched_at,
                "finished_at": now})
    try:
        _write_json_atomic(Path(mailbox, ".native-runs", f"{session}.result.json"), out)
    except OSError:
        pass


err = (_read_bytes(raw + ".err") or b"").decode("utf-8", errors="replace")
# claude -p's bg-wait ceiling killed the Workflow (probe 3 blocker E); the
# run is recoverable with `launch.sh resume --run-id` or a fresh start.
bg_killed = "Background tasks still running" in err
if bg_killed:
    launcher["bg_wait_ceiling"] = True


def fail(reason, head=None):
    if bg_killed:
        reason = ("bg-wait ceiling terminated workflow: claude -p killed its "
                  "background tasks (CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS); "
                  "resume with launch.sh resume --run-id, or start again")
    if head:
        launcher["result_head"] = head
        reason += "; session reply begins: " + json.dumps(head)
    result = {"status": "error", "reason": reason, "launcher": launcher}
    if STARTED:
        persist(result)
    else:
        _restore_or_remove_record()
        persist_not_started(result)
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(3)


body = (_read_bytes(raw) or b"").decode("utf-8", errors="replace")
try:
    outer = json.loads(body)
except ValueError:
    outer = None
if outer is None:
    text = body
elif isinstance(outer, dict):
    text = outer.get("result")
    text = text if isinstance(text, str) else ""
    # The session's total_cost_usd / modelUsage[].costUSD are API list-price
    # estimates, not what a subscription is billed: label them so.
    usd = outer.get("total_cost_usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        launcher["api_equiv_usd"] = usd
    usage = outer.get("modelUsage")
    if isinstance(usage, dict):
        by_model = {m: u["costUSD"] for m, u in usage.items()
                    if isinstance(u, dict) and isinstance(u.get("costUSD"), (int, float))}
        if by_model:
            launcher["api_equiv_usd_by_model"] = by_model
    if "api_equiv_usd" in launcher or "api_equiv_usd_by_model" in launcher:
        launcher["api_equiv_usd_note"] = "estimate at API list price, not billed"
else:
    fail("session output is JSON but not an object")
# ```json / ```JSON / ```jsonc / a bare fence; CRLF tolerated.
blocks = re.findall(r"```(?:jsonc?|json5)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", text,
                    re.S | re.I)
dicts = []
for block in blocks:
    try:
        value = json.loads(block)
    except ValueError:
        continue
    if isinstance(value, dict):
        dicts.append(value)
# The workflow result carries `status`; a trailing non-result block loses.
results = [d for d in dicts if "status" in d] or dicts
if not results:
    # First 300 chars of the final reply: e.g. a launch session that
    # refused to run the workflow (probe 3 G).
    head = (text or "").strip()[:300]
    fail("no fenced result JSON in the session output", head or None)
result = results[-1]
if STARTED:
    if prev and _regular_or_absent(prev) and os.path.isfile(prev):
        try:
            os.remove(prev)
        except OSError:
            pass
else:
    _restore_or_remove_record()
result.setdefault("launcher", {})
if not isinstance(result["launcher"], dict):
    result["launcher"] = {}
result["launcher"].update(launcher)
if STARTED:
    persist(result)
else:
    persist_not_started(result)
print(json.dumps(result, indent=2, sort_keys=True))
sys.exit(0)
PY
