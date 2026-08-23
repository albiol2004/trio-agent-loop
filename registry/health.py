#!/usr/bin/env python3
"""Collect lineage, installation, and generated-file health information.

The collector is intentionally read-only.  Repository data comes from the
explicit ``root`` argument, and global harness data comes only from ``home``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


_REGISTRY_SUFFIXES = (
    ("claude", "skill", Path(".claude/skills")),
    ("claude", "command", Path(".claude/commands")),
    ("claude", "agent", Path(".claude/agents")),
    ("codex", "skill", Path(".agents/skills")),
    ("codex", "agent", Path(".codex/agents")),
    ("omp", "command", Path(".omp/agent/commands")),
    ("omp", "agent", Path(".omp/agent/agents")),
    ("opencode", "command", Path(".config/opencode/commands")),
    ("opencode", "agent", Path(".config/opencode/agents")),
    ("kimi", "skill", Path(".kimi-code/skills")),
    ("zcode", "skill", Path(".zcode/skills")),
)

_SCAN_MODULE = None


def _load_scan():
    """Load the shared scanner without importing optional dependencies."""
    global _SCAN_MODULE
    if _SCAN_MODULE is not None:
        return _SCAN_MODULE
    path = Path(__file__).resolve().parent / "scan.py"
    spec = importlib.util.spec_from_file_location("trio_registry_health_scan", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load registry scanner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # scan.py initializes a legacy HOME constant at import time. Replace that
    # lookup with a harmless registry-local sentinel before loading it; every
    # later scan is explicitly pointed at ``root`` or ``home`` below.
    with patch.object(Path, "home", return_value=path.parent):
        spec.loader.exec_module(module)
    _SCAN_MODULE = module
    return module


def _directories(home: Path) -> dict[tuple[str, str], Path]:
    """Return the same harness/surface map used by ``dashboard/serve.py``."""
    return {
        (harness, surface): home / suffix
        for harness, surface, suffix in _REGISTRY_SUFFIXES
    }


def _load_outputs(root: Path) -> tuple[dict[Path, str], dict[Path, Path]]:
    """Load generator output bytes and best-effort canonical source mappings."""
    path = root / "prompts" / "generate.py"
    if not path.is_file():
        return {}, {}
    try:
        spec = importlib.util.spec_from_file_location(
            "trio_registry_health_generate", path)
        if spec is None or spec.loader is None:
            return {}, {}
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        outputs = {
            Path(output).resolve(): content
            for output, content in module.all_outputs().items()
        }
        sources: dict[Path, Path] = {}
        for flavor in sorted(
                p.stem for p in module.OVERLAYS_DIR.glob("*.md")):
            overlay = module.load_overlay(flavor)
            for role, _target, relative in overlay.targets:
                sources[(root / relative).resolve()] = (
                    root / "prompts" / "canonical" / f"{role}.md")
        for relative, _style in module.EMBEDDED:
            sources.setdefault((root / relative).resolve(), (
                root / "prompts" / "protocol-essentials.md"))
        for source, destinations in module.DOCUMENTS:
            canonical = root / "prompts" / "documents" / f"{source}.md"
            for relative, _frontmatter in destinations:
                sources.setdefault((root / relative).resolve(), canonical)
        return outputs, sources
    except Exception:
        # Health must still report manifests and installs if generation itself
        # is temporarily unreadable.
        return {}, {}


def _manifest(directory: Path, harness: str) -> dict:
    """Verify one ``.trio-hashes`` file and its listed basenames."""
    manifest = directory / ".trio-hashes"
    record = {
        "harness": harness,
        "directory": str(directory),
        "present": manifest.is_file(),
        "status": "missing-manifest",
        "entries": [],
    }
    if not manifest.is_file():
        return record
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        record["status"] = "drift"
        return record
    statuses = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            record["entries"].append({"file": line.strip(), "status": "mismatch"})
            statuses.append("mismatch")
            continue
        expected, filename = parts
        target = directory / filename
        if Path(filename).name != filename or not target.is_file():
            status = "missing-file"
        else:
            try:
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                actual = ""
            status = "match" if actual == expected else "mismatch"
        record["entries"].append({"file": filename, "status": status})
        statuses.append(status)
    record["status"] = "ok" if all(s == "match" for s in statuses) else "drift"
    return record


def _has_file(directory: Path) -> bool:
    """Check recursively because skills are stored below named directories."""
    try:
        return directory.is_dir() and any(p.is_file() for p in directory.rglob("*"))
    except OSError:
        return False


def _dangling(
    directories: dict[tuple[str, str], Path],
    generated: set[str],
) -> list[dict]:
    """Find empty registry directories and unmanifested files."""
    found: dict[str, dict] = {}
    for directory in set(directories.values()):
        if not directory.is_dir():
            continue
        try:
            children = [directory, *directory.rglob("*")]
        except OSError:
            continue
        for child in children:
            if child == directory or not child.is_dir():
                continue
            try:
                if not any(child.iterdir()):
                    found[str(child)] = {
                        "path": str(child),
                        "kind": "empty-dir",
                        "deletable": False,
                    }
            except OSError:
                continue
        for child in children:
            if not child.is_dir():
                continue
            manifest = child / ".trio-hashes"
            if not manifest.is_file():
                continue
            try:
                names = {
                    line.split("  ", 1)[1]
                    for line in manifest.read_text(encoding="utf-8").splitlines()
                    if "  " in line
                }
                files = [item for item in child.iterdir() if item.is_file()]
            except OSError:
                continue
            for item in files:
                if item.name == ".trio-hashes" or item.name in names:
                    continue
                if str(item.resolve()) in generated:
                    continue
                found[str(item)] = {
                    "path": str(item),
                    "kind": "orphan-file",
                    "deletable": True,
                }
    return [found[path] for path in sorted(found)]


def _generated_status(path: Path | None, outputs: dict[Path, str]) -> str:
    """Compare a generated target with the bytes produced by ``all_outputs``."""
    if path is None or path not in outputs:
        return "missing"
    try:
        actual = path.read_bytes()
    except OSError:
        return "missing"
    expected = str(outputs[path]).encode("utf-8")
    return "in-sync" if actual == expected else "stale"


def _install_path(
    entry: dict | None,
    home: Path | None,
    directories: dict[tuple[str, str], Path],
) -> Path | None:
    """Predict a missing global destination from its canonical entry."""
    if entry is None or home is None:
        return None
    directory = directories.get((entry["harness"], entry["surface"]))
    if directory is None:
        return None
    path = Path(entry["path"])
    if entry["surface"] == "skill":
        return directory / path.parent.name / path.name
    return directory / path.name


def _lineage(
    root: Path,
    home: Path | None,
    canonical: list[dict],
    installed: list[dict],
    index: dict,
    outputs: dict[Path, str],
    sources: dict[Path, Path],
    directories: dict[tuple[str, str], Path],
) -> tuple[list[dict], set[str]]:
    """Build one stable three-hop row for each scanner group."""
    statuses = {
        item["path"]: item["status"]
        for group in index["groups"]
        for item in group["installations"]
    }
    rows = []
    selected_targets: set[str] = set()
    for group in index["groups"]:
        name = group["name"]
        entries = sorted(
            [entry for entry in canonical if entry["name"] == name],
            key=lambda item: (item["harness"], item["surface"], item["path"]),
        )
        generated = [
            entry for entry in entries
            if Path(entry["path"]).resolve() in outputs
        ]
        target = generated[0] if generated else None
        if target is not None:
            target_path = Path(target["path"]).resolve()
            selected_targets.add(str(target_path))
            canonical_path = sources.get(target_path)
            canonical_status = (
                "canonical"
                if canonical_path is not None and canonical_path.is_file()
                else "missing"
            )
            wrapper = target
            wrapper_status = _generated_status(target_path, outputs)
        else:
            canonical_entry = entries[0] if entries else None
            wrapper = entries[1] if len(entries) > 1 else canonical_entry
            canonical_path = (
                Path(canonical_entry["path"]) if canonical_entry else None
            )
            canonical_status = "canonical" if canonical_entry else "missing"
            target_path = Path(wrapper["path"]) if wrapper else None
            wrapper_status = "canonical" if wrapper else "missing"
        matching = None
        if wrapper is not None:
            matching = next(
                (
                    entry for entry in installed
                    if entry["name"] == name
                    and entry["harness"] == wrapper["harness"]
                    and entry["surface"] == wrapper["surface"]
                ),
                None,
            )
        else:
            matching = next(
                (entry for entry in installed if entry["name"] == name),
                None,
            )
        installed_path = (
            Path(matching["path"]) if matching else
            _install_path(wrapper, home, directories)
        )
        rows.append({
            "name": name,
            "hops": [
                {
                    "role": "canonical",
                    "path": str(canonical_path) if canonical_path else None,
                    "status": canonical_status,
                },
                {
                    "role": "generated_or_wrapper",
                    "path": str(target_path) if target_path else None,
                    "status": wrapper_status,
                },
                {
                    "role": "installed",
                    "path": str(installed_path) if installed_path else None,
                    "status": statuses.get(
                        matching["path"], "missing") if matching else "missing",
                    "harness": (
                        wrapper["harness"] if wrapper else
                        matching["harness"] if matching else None
                    ),
                },
            ],
        })
    # Some generated artifacts (for example portable prompt references) are
    # not registry entries. Keep every generator target visible.
    for path in sorted(outputs):
        if str(path) in selected_targets:
            continue
        relative = path.relative_to(root).as_posix()
        source = sources.get(path)
        target_entry = next(
            (
                entry for entry in canonical
                if Path(entry["path"]).resolve() == path
            ),
            None,
        )
        installed_entry = next(
            (
                entry for entry in installed
                if target_entry
                and entry["name"] == target_entry["name"]
                and entry["harness"] == target_entry["harness"]
                and entry["surface"] == target_entry["surface"]
            ),
            None,
        )
        installed_path = (
            Path(installed_entry["path"]) if installed_entry else
            _install_path(target_entry, home, directories)
        )
        rows.append({
            "name": relative,
            "hops": [
                {
                    "role": "canonical",
                    "path": str(source) if source else None,
                    "status": (
                        "canonical" if source and source.is_file() else "missing"
                    ),
                },
                {
                    "role": "generated_or_wrapper",
                    "path": str(path),
                    "status": _generated_status(path, outputs),
                },
                {
                    "role": "installed",
                    "path": str(installed_path) if installed_path else None,
                    "status": statuses.get(
                        installed_entry["path"], "missing")
                    if installed_entry else "missing",
                    "harness": (
                        target_entry["harness"] if target_entry else None
                    ),
                },
            ],
        })
    return rows, selected_targets


def _generate_check(root: Path) -> dict:
    """Run the existing read-only generator check without raising to callers."""
    result = {
        "command": "python3 prompts/generate.py --check",
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
    }
    try:
        completed = subprocess.run(
            ["python3", "prompts/generate.py", "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        result.update({
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        })
    except subprocess.TimeoutExpired as exc:
        result.update({
            "timed_out": True,
            "stdout": exc.stdout.decode() if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            "stderr": exc.stderr.decode() if isinstance(exc.stderr, bytes)
            else (exc.stderr or ""),
        })
    except Exception as exc:
        result["stderr"] = str(exc)
    return result


def collect_health(root: Path, home: Path | None = None) -> dict:
    """Return a stable, read-only health report for an explicit repository."""
    root = Path(root).resolve()
    explicit_home = Path(home).resolve() if home is not None else None
    if explicit_home is not None and not explicit_home.is_dir():
        explicit_home = None
    directories = _directories(explicit_home) if explicit_home else {}
    outputs, sources = _load_outputs(root)
    scan = _load_scan()
    scan.REPO = root
    scan.HOME = explicit_home or root / ".trio-health-no-home"
    scan.reset_generated_paths_cache()
    canonical = scan.collect_canonical()
    installed = scan.collect(None) if explicit_home else []
    index = scan.build_index(canonical + installed)
    manifests = [
        _manifest(directory, harness)
        for (harness, _surface), directory in directories.items()
    ]
    installed_harnesses = [
        {
            "harness": harness,
            "installed": _has_file(directory),
            "directory": str(directory),
        }
        for (harness, _surface), directory in directories.items()
    ]
    # Use the scanner's managed-path contract as a second guard when a
    # generator target exists on disk but generation cannot be reloaded.
    generated = {str(path) for path in outputs} | set(scan.generated_paths())
    lineage, _selected = _lineage(
        root, explicit_home, canonical, installed, index, outputs, sources,
        directories)
    return {
        "lineage": lineage,
        "manifests": manifests,
        "installed_harnesses": installed_harnesses,
        "generate_check": _generate_check(root),
        "dangling": _dangling(directories, generated),
    }
