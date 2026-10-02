import time
from pathlib import Path

import pytest

import bundle


def _make_python_src(root: Path) -> Path:
    src = root / "pythons" / "cpython-3.12.14-linux-x86_64-gnu"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "python3").write_text("#!/bin/sh\necho fake-python\n")
    (src / "lib").mkdir()
    (src / "lib" / "marker.txt").write_text("x")
    return src


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "opencode-driver" / "trio_opencode").mkdir(parents=True)
    (repo / "opencode-driver" / "trio_opencode" / "cli.py").write_text("print('cli')\n")
    (repo / "opencode-driver" / "tests").mkdir(parents=True)
    (repo / "opencode-driver" / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    (repo / "opencode-driver" / "agents").mkdir(parents=True)
    (repo / "opencode-driver" / "agents" / "trio-lead.md").write_text("# driver lead\n")
    (repo / "native").mkdir()
    (repo / "native" / "trio_native_step.py").write_text("# step\n")
    (repo / "metrics").mkdir()
    (repo / "metrics" / "trio_loop.py").write_text("# loop\n")
    (repo / "opencode" / "agents").mkdir(parents=True)
    (repo / "opencode" / "agents" / "trio-lead.md").write_text("# lead\n")
    (repo / "prompts" / "canonical").mkdir(parents=True)
    (repo / "prompts" / "canonical" / "acceptance-native-lead.md").write_text("# acc lead\n")
    pycache = repo / "native" / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00")
    return repo


def test_build_python_tarball_round_trips(tmp_path: Path):
    src = _make_python_src(tmp_path)
    out = bundle.build_python_tarball(python_src=src, bundle_dir=tmp_path / "bundle")
    assert out.is_file()
    import tarfile
    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "./bin/python3" in names or "bin/python3" in names


def test_build_python_tarball_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        bundle.build_python_tarball(python_src=tmp_path / "nope", bundle_dir=tmp_path / "bundle")


def test_build_python_tarball_is_cached_until_source_changes(tmp_path: Path):
    src = _make_python_src(tmp_path)
    bundle_dir = tmp_path / "bundle"
    out1 = bundle.build_python_tarball(python_src=src, bundle_dir=bundle_dir)
    mtime1 = out1.stat().st_mtime
    time.sleep(0.01)
    out2 = bundle.build_python_tarball(python_src=src, bundle_dir=bundle_dir)
    assert out2.stat().st_mtime == mtime1  # not rebuilt

    time.sleep(0.05)
    (src / "bin" / "python3").write_text("changed\n")
    out3 = bundle.build_python_tarball(python_src=src, bundle_dir=bundle_dir)
    assert out3.stat().st_mtime > mtime1  # rebuilt because a source file changed


def test_build_repo_tarball_excludes_tests_and_pycache(tmp_path: Path):
    repo = _make_repo(tmp_path)
    out = bundle.build_repo_tarball(repo_root=repo, bundle_dir=tmp_path / "bundle")
    import tarfile
    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert any("cli.py" in n for n in names)
    assert any(n.endswith("trio-lead.md") for n in names)
    # r19 acceptance fragments the driver reads at runtime
    assert any(n.endswith("prompts/canonical/acceptance-native-lead.md") for n in names)
    # opencode-driver/agents (standalone driver's own generated bodies) and
    # opencode/agents (the shared scout) are both carried, distinctly.
    assert any(n.endswith("opencode-driver/agents/trio-lead.md") for n in names)
    assert any(n.endswith("opencode/agents/trio-lead.md") and "opencode-driver" not in n
               for n in names)
    assert not any("tests" in n for n in names)
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)


def test_build_repo_tarball_missing_all_subdirs_raises(tmp_path: Path):
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    with pytest.raises(FileNotFoundError):
        bundle.build_repo_tarball(repo_root=empty_repo, bundle_dir=tmp_path / "bundle")


def test_ensure_bundle_requires_opencode_binary(tmp_path: Path):
    src = _make_python_src(tmp_path)
    repo = _make_repo(tmp_path)
    with pytest.raises(FileNotFoundError):
        bundle.ensure_bundle(
            repo_root=repo,
            bundle_dir=tmp_path / "bundle",
            python_src=src,
            opencode_binary=tmp_path / "no-such-binary",
        )


def test_ensure_bundle_builds_all_three(tmp_path: Path):
    src = _make_python_src(tmp_path)
    repo = _make_repo(tmp_path)
    opencode_bin = tmp_path / "opencode"
    opencode_bin.write_bytes(b"\x7fELF-fake-binary")

    paths = bundle.ensure_bundle(
        repo_root=repo, bundle_dir=tmp_path / "bundle", python_src=src,
        opencode_binary=opencode_bin,
    )
    assert paths.python_tarball.is_file()
    assert paths.repo_tarball.is_file()
    assert paths.opencode_binary == opencode_bin
