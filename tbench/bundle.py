"""Build and cache the host-side tarballs ``TrioOpenCodeAgent.install()``
uploads into a Terminal-Bench task container: the standalone CPython
interpreter and the trio-opencode repo slice (``opencode-driver/`` --
including its own ``opencode-driver/agents/`` standalone-driver role
bodies --, ``native/``, ``metrics/``, ``opencode/agents/`` for the shared
scout body). The OpenCode binary is a single file and is uploaded as-is
(no tarball needed).

Idempotent: a tarball is rebuilt only when a source file under it is newer
than the cached tarball, so repeated ``harbor run`` invocations reuse the
same ``tbench4/bundle/*.tar.gz`` across trials. Host-side only -- nothing
here ever runs inside the task container.
"""
from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

#: Repo-relative directories bundled into ``trio-repo.tar.gz``, preserving
#: this exact relative layout so ``steplib.REPO_ROOT``-style lookups
#: (``opencode/agents/trio-scout.md`` and ``opencode-driver/agents/trio-
#: <role>.md`` next to ``opencode-driver/``) keep working once extracted at
#: ``/opt/trio/repo``. ``opencode-driver`` is bundled wholly, so its own
#: ``agents/`` subdir (the standalone driver's generated lead/evaluator/
#: builder/repair bodies -- ocgen.py's ``_load_role_body``) is already
#: carried without a separate entry; ``opencode/agents`` is listed
#: separately because only the scout body under it is needed (the rest of
#: ``opencode/`` is the in-OpenCode plugin, not this driver).
REPO_SUBDIRS: tuple[str, ...] = (
    "opencode-driver", "native", "metrics", "opencode/agents",
    # r19 acceptance: prompts.py reads the canonical Lead/Evaluator
    # fragments (acceptance-native-*.md) at runtime.
    "prompts/canonical",
)

#: Directory names excluded anywhere in the bundled repo tree.
_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "tests"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

#: Cache dir for the tarballs: ``$TBENCH_BUNDLE_DIR``, else ``../tbench4/bundle``
#: next to this checkout (the same layout ``run_job.sh`` assumes).
DEFAULT_BUNDLE_DIR = Path(
    os.environ.get("TBENCH_BUNDLE_DIR")
    or Path(__file__).resolve().parents[1].parent / "tbench4" / "bundle"
)
DEFAULT_PYTHON_SRC = (
    DEFAULT_BUNDLE_DIR / "pythons" / "cpython-3.12.14-linux-x86_64-gnu"
)
#: Host OpenCode binary: ``$TBENCH_OPENCODE_BIN``, else ``~/.opencode/bin/opencode``.
DEFAULT_OPENCODE_BIN = Path(
    os.environ.get("TBENCH_OPENCODE_BIN")
    or Path.home() / ".opencode" / "bin" / "opencode"
)


@dataclass(frozen=True)
class BundlePaths:
    python_tarball: Path
    repo_tarball: Path
    opencode_binary: Path


def _excluded(rel_parts: tuple[str, ...], suffix: str) -> bool:
    if any(part in _EXCLUDE_DIR_NAMES for part in rel_parts):
        return True
    if suffix in _EXCLUDE_SUFFIXES:
        return True
    return False


def _dir_newest_mtime(root: Path) -> float:
    newest = root.stat().st_mtime
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def _needs_rebuild(tarball: Path, newest_source_mtime: float) -> bool:
    if not tarball.is_file():
        return True
    return newest_source_mtime > tarball.stat().st_mtime


def build_python_tarball(
    *, python_src: Path = DEFAULT_PYTHON_SRC, bundle_dir: Path = DEFAULT_BUNDLE_DIR
) -> Path:
    """Tar the standalone CPython tree so that extracting it at
    ``/opt/trio/python`` inside a container yields ``/opt/trio/python/bin/
    python3`` directly (the tree's own ``bin/``, ``lib/``, ... become the
    tarball root)."""
    python_src = Path(python_src)
    bundle_dir = Path(bundle_dir)
    if not python_src.is_dir():
        raise FileNotFoundError(f"bundled python source not found: {python_src}")
    bundle_dir.mkdir(parents=True, exist_ok=True)
    out = bundle_dir / "cpython-3.12.tar.gz"
    if _needs_rebuild(out, _dir_newest_mtime(python_src)):
        tmp = out.with_suffix(out.suffix + ".tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(python_src, arcname=".")
        os.replace(tmp, out)
    return out


def build_repo_tarball(
    *, repo_root: Path, bundle_dir: Path = DEFAULT_BUNDLE_DIR
) -> Path:
    """Tar ``opencode-driver/`` (its own ``agents/`` included), ``native/``,
    ``metrics/`` and ``opencode/agents/`` from *repo_root* (the
    trio-opencode source checkout), excluding ``tests/`` and
    ``__pycache__``, preserving each dir's relative path so extracting at
    ``/opt/trio/repo`` reproduces the same layout."""
    repo_root = Path(repo_root)
    bundle_dir = Path(bundle_dir)
    present = [(rel, repo_root / rel) for rel in REPO_SUBDIRS if (repo_root / rel).exists()]
    if not present:
        raise FileNotFoundError(
            f"none of {REPO_SUBDIRS} found under repo_root={repo_root}"
        )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    out = bundle_dir / "trio-repo.tar.gz"
    newest = max(_dir_newest_mtime(src) for _, src in present)
    if _needs_rebuild(out, newest):
        tmp = out.with_suffix(out.suffix + ".tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            for rel, src in present:
                def _filter(ti: tarfile.TarInfo, _rel: str = rel) -> tarfile.TarInfo | None:
                    name = Path(ti.name)
                    parts = name.parts
                    if _excluded(parts, name.suffix):
                        return None
                    return ti

                tf.add(src, arcname=rel, filter=_filter)
        os.replace(tmp, out)
    return out


def ensure_bundle(
    *,
    repo_root: Path,
    bundle_dir: Path = DEFAULT_BUNDLE_DIR,
    python_src: Path = DEFAULT_PYTHON_SRC,
    opencode_binary: Path = DEFAULT_OPENCODE_BIN,
) -> BundlePaths:
    """Build (or reuse cached) tarballs for this run. Host-side only; never
    executes inside the task container. Raises ``FileNotFoundError`` if a
    required host asset is missing (checked eagerly so a bad install()
    fails fast with a clear message rather than an opaque upload error)."""
    opencode_binary = Path(opencode_binary)
    if not opencode_binary.is_file():
        raise FileNotFoundError(f"opencode binary not found: {opencode_binary}")
    return BundlePaths(
        python_tarball=build_python_tarball(python_src=python_src, bundle_dir=bundle_dir),
        repo_tarball=build_repo_tarball(repo_root=repo_root, bundle_dir=bundle_dir),
        opencode_binary=opencode_binary,
    )
