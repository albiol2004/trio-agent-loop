"""r20: `git replace` refs never reach the acceptance gate.

A role can write `refs/replace/<evaluated sha>` pointing at a commit whose
tree is complete (or whose pack is clean); a plain git read of the evaluated
sha then sees the replacement while the real commit, what push/clone carry,
stays incomplete. Every git read of trio-acceptance.py, trio_loop.py and
trio-shadow.py runs with `--no-replace-objects` + GIT_NO_REPLACE_OBJECTS=1,
so the pack pre-run archives the REAL tree, `pack_hash_at` hashes the REAL
pack, and the commit gate keeps refusing an offender hidden behind a
replacement. Real git, no mocks."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_r19_shadow_acceptance_gate import SH, TA, gate, git, setup

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / "metrics" / "trio_loop.py"


def _replace(repo: Path, sha: str, replacement: str) -> None:
    git(repo, "replace", sha, replacement)
    # the replacement is in effect for a NAIVE reader (plain git honours it)
    naive = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
                           capture_output=True, text=True, check=True).stdout.strip()
    real = git(repo, "--no-replace-objects", "rev-parse", f"{sha}^{{tree}}")
    assert naive == git(repo, "rev-parse", f"{replacement}^{{tree}}") != real


def _commit_with_tree(repo: Path, tree: str, parent: str, message: str) -> str:
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message)


def test_pack_pre_run_archives_the_real_tree_not_the_replacement(tmp_path):
    """The evaluated sha's product is incomplete; a replace ref points it at a
    commit whose tree has the complete product. archive_tree (what run_pack
    evaluates) must extract the incomplete, real tree."""
    repo, mb = setup(tmp_path)
    evaluated = git(repo, "rev-parse", "HEAD")           # app.py = "v1" (incomplete)
    (repo / "app.py").write_text("print('hello')\n")     # the "complete" product ...
    git(repo, "commit", "-qam", "slice(cli): complete (never the evaluated sha)")
    complete = git(repo, "rev-parse", "HEAD")
    git(repo, "reset", "-q", "--hard", evaluated)
    _replace(repo, evaluated, complete)
    dest = tmp_path / "tree"
    assert TA.archive_tree(repo, evaluated, dest) == evaluated
    assert (dest / "app.py").read_text() == "v1\n"
    # symlinks_at / _object_ids read the same real objects
    assert TA.symlinks_at(repo, evaluated, "loop/acceptance") == []


def test_pack_hash_at_hashes_the_real_pack_not_the_replacement(tmp_path):
    repo, mb = setup(tmp_path)
    prefix = "loop/acceptance"
    real_pin = TA.manifest_sha256(mb / "acceptance")
    evaluated = git(repo, "rev-parse", "HEAD")
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    git(repo, "commit", "-qam", "tampered pack (the replacement)")
    tampered = git(repo, "rev-parse", "HEAD")
    git(repo, "reset", "-q", "--hard", evaluated)
    _replace(repo, evaluated, tampered)
    assert TA.pack_hash_at(repo, evaluated, prefix) == real_pin
    assert SH.pack_hash_at(repo, evaluated, prefix) == real_pin
    chain = TA.derive_pin_chain(repo, prefix)
    assert chain["head_pack"] == real_pin


def test_commit_gate_keeps_refusing_an_offender_hidden_by_a_replace_ref(tmp_path):
    """A slice commit that touches the pack is an offender (r19 C5). Pointing a
    replace ref from that commit at a clean commit (same parent, the pre-tamper
    tree) must not make --require-commits accept it."""
    repo, mb = setup(tmp_path)
    parent = git(repo, "rev-parse", "HEAD")
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    git(repo, "commit", "-qam", "slice(cli): make the check pass")
    offender = git(repo, "rev-parse", "HEAD")
    assert gate(mb).returncode == 1
    clean = _commit_with_tree(repo, f"{parent}^{{tree}}", parent, "slice(cli): looks clean")
    _replace(repo, offender, clean)
    proc = gate(mb)
    assert proc.returncode == 1, proc.stdout
    assert "acceptance gate:" in proc.stdout and "only the driver" in proc.stdout
    assert SH.acceptance_offenders(mb) != []
    # the gate as the loop driver runs it (trio_loop._commit_gate) agrees
    import importlib.util
    spec = importlib.util.spec_from_file_location("trio_loop_r20rr", LOOP)
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    ok, _text = tl._commit_gate(mb, repo)
    assert ok is False
    # and the driver's own git helper reads the real object
    assert tl._git(repo, "rev-parse", f"{offender}^{{tree}}").stdout.strip() \
        == git(repo, "--no-replace-objects", "rev-parse", f"{offender}^{{tree}}")


def test_every_git_argv_of_the_gate_modules_ignores_replace_refs():
    import re
    for rel in ("metrics/trio-acceptance.py", "metrics/trio_loop.py", "metrics/trio-shadow.py",
                "omnigent/trioctl"):
        text = (ROOT / rel).read_text()
        argvs = re.findall(r'\["git",\s*"([^"]*)"', text)
        assert argvs and all(a == "--no-replace-objects" for a in argvs), (rel, argvs)
        assert re.search(r'^\s+"git",\n\s+"(?!--no-replace-objects)', text, re.M) is None, rel
        assert 'GIT_NO_REPLACE_OBJECTS' in text, rel


@pytest.mark.parametrize("module", ["trio-acceptance.py", "trio_loop.py"])
def test_git_env_carries_the_switch(module):
    import importlib.util
    spec = importlib.util.spec_from_file_location("m_" + module.replace("-", "_").replace(".py", ""),
                                                  ROOT / "metrics" / module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.git_env()["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert mod.git_env({"X": "y"})["X"] == "y"
