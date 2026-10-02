"""Open-loop frozen acceptance, author-isolation variants. Reuses
``ol_acceptance``'s Lead/builder/evaluator handlers; only the author
(``trio-acceptance``) differs, picked by ``FAKE_AUTHOR_MODE``:

``probe``        no OS sandbox (level ``no-shell``): the author tries every way
                 a model reaches for the repository -- absolute and ``../``
                 reads, a symlink the repo committed, glob/grep outside,
                 Code Mode ``execute`` (``fetch file://``, ``session_move``),
                 other tools, and shell commands (``cat``, ``ls``, ``cd /``, ``python -c
                 open()``). The fake authorizes each call against the
                 GENERATED permission config exactly the way OpenCode v2
                 does (``oc_perm``); a refused call is emitted as an ``error``
                 tool part with the rule-denial message, an allowed one is
                 really performed (so a hole shows up as ``leaked`` in
                 ``FAKE_OC_STATE/author-probe.json``). Then an honest pack.
``probe-os``     OS sandbox (level ``sandbox``): the same attempts performed
                 for real by the fake process (it is inside bwrap); what
                 happened is recorded in ``author-probe.json``.
``prose``        no read at all; the pack's AUTHOR.md and a check merely NAME
                 the repository path.
``crash``        the author turn dies with an error event, twice.
``contaminate``  the author always completes a read of the repo (ignoring the
                 config), so both attempts are contaminated.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import oc_perm  # noqa: E402
import ol_acceptance  # noqa: E402

DENIAL = "The user has specified a rule which prevents you from using this specific tool call."


def _permission(ctx) -> dict:
    cfg = json.loads(Path(ctx.env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    return cfg["agent"]["trio-acceptance"]["permission"]


def _record(ctx, key: str, value) -> None:
    path = Path(os.environ["FAKE_OC_STATE"]) / "author-probe.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def _attempt(ctx, key: str, tool: str, inp: dict, allowed: bool, perform) -> None:
    """Emit one tool part; ``perform()`` really does the call when allowed
    and returns what it read ('' when it failed)."""
    if not allowed:
        ctx.tool(tool, "error", inp, DENIAL)
        _record(ctx, key, {"allowed": False, "leaked": False})
        return
    got = perform()
    ctx.tool(tool, "completed", inp, got or "")
    _record(ctx, key, {"allowed": True, "leaked": bool(got and "SECRET" in got)})


def _read(path: str):
    def go():
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""
    return go


def _sh(cmd: str):
    def go():
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, cwd=os.getcwd())
        return r.stdout
    return go


def _probe(ctx) -> None:
    repo = os.environ["FAKE_REPO_PATH"]
    export = ctx.dir
    perm = _permission(ctx)
    rel = os.path.relpath(repo, export)                      # ../../../..../repo
    secret = f"{repo}/README"
    attempts = [
        ("read-abs", "read", {"path": secret}, oc_perm.can_read(perm, export, secret), _read(secret)),
        ("read-dotdot", "read", {"path": f"{rel}/README"},
         oc_perm.can_read(perm, export, f"{rel}/README"), _read(os.path.join(export, rel, "README"))),
        ("read-dotdot-via-inside", "read", {"path": f"sub/../{rel}/README"},
         oc_perm.can_read(perm, export, f"sub/../{rel}/README"),
         _read(os.path.join(export, "sub", "..", rel, "README"))),
        ("read-symlink", "read", {"path": "link-to-readme"},
         oc_perm.can_read(perm, export, "link-to-readme"), _read(os.path.join(export, "link-to-readme"))),
        ("read-dir", "read", {"path": repo}, oc_perm.can_read(perm, export, repo, is_dir=True),
         lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("glob-dotdot", "glob", {"pattern": f"{rel}/*", "path": "."},
         oc_perm.can_glob(perm, export, f"{rel}/*"), lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("glob-abs", "glob", {"pattern": f"{repo}/*"}, oc_perm.can_glob(perm, export, f"{repo}/*"),
         lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("glob-path-outside", "glob", {"pattern": "*", "path": repo},
         oc_perm.can_glob(perm, export, "*", repo), lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("grep-outside", "grep", {"pattern": "SECRET", "path": repo},
         oc_perm.can_grep(perm, export, "SECRET", repo), lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("edit-outside", "edit", {"path": f"{repo}/pwn.txt", "content": "x"},
         oc_perm.can_edit(perm, export, f"{repo}/pwn.txt"), lambda: ""),
    ]
    # OpenCode v2 Code Mode: `execute` runs model-written JS whose `fetch`
    # takes file:// URLs and whose `tools.opencode.session_move` re-roots the
    # session into the repository (both probed against the real 2.0.20)
    attempts += [
        ("exec-fetch-file", "execute",
         {"code": f"return await (await fetch('file://{secret}')).text()"},
         oc_perm.can_tool(perm, "execute"), _read(secret)),
        ("exec-session-move", "execute",
         {"code": f"return await tools.opencode.session_move({{directory: '{repo}'}})"},
         oc_perm.can_tool(perm, "execute"), lambda: "SECRET" if os.path.isdir(repo) else ""),
        ("tool-webfetch", "webfetch", {"url": f"file://{secret}"},
         oc_perm.can_tool(perm, "webfetch"), _read(secret)),
        ("tool-unknown-future", "future_tool", {"path": secret},
         oc_perm.can_tool(perm, "future_tool"), _read(secret)),
    ]
    for cmd_key, cmd in (
        ("sh-cat-dotdot", f"cat {rel}/README"),
        ("sh-cat-abs", f"cat {secret}"),
        ("sh-ls", f"ls -la {repo}"),
        ("sh-cd-root", "cd / && ls"),
        ("sh-python-open", f"python3 -c \"print(open('{secret}').read())\""),
        ("sh-python-concat", "python3 -c \"print(open('/'.join(['" + "','".join(secret.split('/')) + "'])).read())\""),
        ("sh-validate", "python3 /x/trio-acceptance.py validate --export ."),
    ):
        attempts.append((cmd_key, "bash", {"command": cmd}, oc_perm.can_shell(perm, cmd), _sh(cmd)))
    for key, tool, inp, allowed, perform in attempts:
        _attempt(ctx, key, tool, inp, allowed, perform)
    # legitimate work inside the export must keep working
    goal = str(Path(export) / ".acceptance-input" / "GOAL.md")
    _record(ctx, "read-goal", {"allowed": oc_perm.can_read(perm, export, ".acceptance-input/GOAL.md")})
    _record(ctx, "write-pack", {"allowed": oc_perm.can_edit(perm, export, "acceptance/MANIFEST.json")})
    _record(ctx, "glob-inside", {"allowed": oc_perm.can_glob(perm, export, "**/*.py")})
    ctx.tool("read", "completed", {"path": goal}, "goal")


def _probe_os(ctx) -> None:
    """Inside bwrap: do the real operations and record what the process saw."""
    repo = os.environ["FAKE_REPO_PATH"]
    export = os.getcwd()
    rel = os.path.relpath(repo, export)
    seen = {
        "cwd": export,
        "repo_exists": os.path.exists(repo),
        "repo_readme": _read(f"{repo}/README")(),
        "dotdot_readme": _read(os.path.join(export, rel, "README"))(),
        "symlink_readme": _read(os.path.join(export, "link-to-readme"))(),
        "parent_listing": sorted(os.listdir(os.path.dirname(export))),
        "root_listing": sorted(os.listdir("/")),
        "repo_parent_listing": (sorted(os.listdir(os.path.dirname(repo)))
                                if os.path.isdir(os.path.dirname(repo)) else []),
        "python_open": _sh(f"python3 -c \"print(open('{repo}/README').read())\" 2>&1")(),
        "cat_dotdot": _sh(f"cat {rel}/README 2>&1")(),
        "goal_readable": bool(_read(os.path.join(export, ".acceptance-input", "GOAL.md"))()),
        "can_write_export": _try_write(os.path.join(export, "scratch-ok.txt")),
        "can_write_outside": _try_write(f"{repo}/pwn.txt"),
        "home": os.environ.get("HOME"),
        "net_ns_shared": os.path.exists("/sys/class/net/lo"),
    }
    _record(ctx, "os", seen)
    ctx.tool("bash", "completed", {"command": f"ls {repo}"}, "")


def _try_write(path: str) -> bool:
    try:
        Path(path).write_text("x", encoding="utf-8")
        return True
    except OSError:
        return False


def _handle_author(ctx) -> None:
    mode = os.environ.get("FAKE_AUTHOR_MODE", "probe")
    export = Path(ctx.dir)
    if mode == "probe":
        _probe(ctx)
        ol_acceptance._write_pack(export)
    elif mode == "probe-os":
        _probe_os(ctx)
        ol_acceptance._write_pack(export)
    elif mode == "prose":
        ol_acceptance._write_pack(export)
        repo = os.environ["FAKE_REPO_PATH"]
        ctx.tool("write", "completed",
                 {"path": "acceptance/AUTHOR.md",
                  "content": f"the checks run in a copy of the tree, not at `{repo}`\n"})
        ctx.tool("write", "completed",
                 {"path": "acceptance/checks/acc_09.py",
                  "content": f"assert exists('{repo}/requirements.txt')\n"})
    elif mode == "crash":
        ctx.error("UnknownError", "author turn died")
    elif mode == "contaminate":
        repo = os.environ["FAKE_REPO_PATH"]
        ctx.tool("read", "completed", {"path": f"{repo}/README"}, "x")
        ol_acceptance._write_pack(export)
    common.reply(ctx, {"checks": ol_acceptance.N_CHECKS, "summary": f"author mode {mode}"})


def handle(ctx) -> None:
    if ctx.agent == "trio-acceptance":
        _handle_author(ctx)
    else:
        ol_acceptance.handle(ctx)
