"""The author-isolation audit judges what the author LOOKED AT, never what it
wrote: a check or AUTHOR.md that merely mentions the loop repository's path
(Terminal-Bench goals name `/app/...`) must not discard the session."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ta():
    spec = importlib.util.spec_from_file_location("ta_authored_text",
                                                  ROOT / "metrics" / "trio-acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _ta()


def _call(name: str, **inp) -> str:
    return json.dumps({"type": "tool_use", "name": name, "input": inp})


@pytest.fixture
def dirs(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    return export, repo


@pytest.mark.parametrize("row", [
    # prose the author wrote naming the repo path (the cargo-flight-dispatch case)
    lambda repo: _call("write", path="acceptance/AUTHOR.md",
                       content=f"checks run in a copy of the tree, not at `{repo}`\n"),
    # a check that names a GOAL-mandated absolute path
    lambda repo: _call("write", path="acceptance/checks/acc_09.py",
                       content=f"assert exists('{repo}/requirements.txt')\n"),
    lambda repo: _call("edit", path="acceptance/AUTHOR.md",
                       oldString="x", newString=f"see {repo}/requirements.txt"),
    lambda repo: _call("Edit", file_path="acceptance/AUTHOR.md",
                       old_string=f"{repo}/a", new_string=f"{repo}/b"),
])
def test_written_text_naming_the_repo_is_not_a_read(dirs, row):
    export, repo = dirs
    audit = TA.audit_transcript([row(repo)], export, forbidden=[repo])
    assert audit["contaminated"] is False, audit


@pytest.mark.parametrize("row", [
    lambda repo: _call("read", path=f"{repo}/requirements.txt"),
    lambda repo: _call("write", path=f"{repo}/pwned.py", content="x"),     # the PATH is audited
    lambda repo: _call("edit", filePath=f"{repo}/a.py", oldString="x", newString="y"),
    lambda repo: _call("bash", command=f"ls {repo}"),
    lambda repo: _call("glob", pattern="**/*", path=str(repo)),
    lambda repo: _call("grep", pattern="hidden", path=str(repo)),
])
def test_real_reads_and_paths_are_still_flagged(dirs, row):
    export, repo = dirs
    audit = TA.audit_transcript([row(repo)], export, forbidden=[repo])
    assert audit["contaminated"] is True, audit
