#!/usr/bin/env python3
"""vendor.py -- pinned, checksummed local ESM for the dashboard (stdlib only).

The dashboard frontend (Preact + @preact/signals + htm) is served from the
files under this directory; there is no build step and no CDN. MANIFEST.json
pins every package (exact version, registry tarball URL, npm ``dist.integrity``)
and every vendored file (path under dashboard/vendor, its path inside the
tarball, sha256 of the bytes). importmap.json maps the bare specifiers the
modules import to /vendor/ URLs.

    vendor.py --check   offline: exit 0 iff every manifest file exists with its
                        sha256 and every import-map target is a manifest file;
                        exit 1 naming each bad file otherwise.
    vendor.py --fetch   download each pinned tarball, verify its integrity,
                        extract the listed files byte for byte and rewrite the
                        files and the sha256 values in MANIFEST.json. To change
                        a version edit the package's version / tarball /
                        integrity (``npm view <pkg>@<ver> dist.integrity``) first.
                        Never run by tests; needs the network.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL_PREFIX = "/vendor/"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_files(manifest: dict) -> dict:
    """``{relpath under the vendor dir: {"from", "sha256"}}`` over every package."""
    out: dict = {}
    for package in (manifest.get("packages") or {}).values():
        out.update(package.get("files") or {})
    return out


def check(vendor_dir: Path = HERE) -> list[str]:
    """Problems found in ``vendor_dir`` (empty when everything verifies)."""
    try:
        manifest = _load_json(vendor_dir / "MANIFEST.json")
    except (OSError, ValueError) as exc:
        return [f"MANIFEST.json: unreadable ({exc})"]
    problems: list[str] = []
    files = manifest_files(manifest)
    for rel, entry in sorted(files.items()):
        try:
            data = (vendor_dir / rel).read_bytes()
        except OSError:
            problems.append(f"{rel}: missing")
            continue
        if sha256_hex(data) != entry.get("sha256"):
            problems.append(f"{rel}: sha256 mismatch")
    try:
        imports = _load_json(vendor_dir / "importmap.json").get("imports") or {}
    except (OSError, ValueError) as exc:
        return problems + [f"importmap.json: unreadable ({exc})"]
    for spec, target in sorted(imports.items()):
        rel = target[len(URL_PREFIX):] if str(target).startswith(URL_PREFIX) else None
        if rel is None or rel not in files:
            problems.append(f"importmap.json: {spec} -> {target} is not a manifest file")
    return problems


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def fetch(vendor_dir: Path = HERE) -> int:
    manifest = _load_json(vendor_dir / "MANIFEST.json")
    for name, package in manifest["packages"].items():
        with urllib.request.urlopen(package["tarball"], timeout=60) as response:
            blob = response.read()
        if _integrity(blob) != package["integrity"]:
            print(f"{name}@{package['version']}: tarball integrity mismatch", file=sys.stderr)
            return 1
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for rel, entry in package["files"].items():
                member = tar.extractfile(entry["from"])
                if member is None:
                    print(f"{name}: {entry['from']} not in tarball", file=sys.stderr)
                    return 1
                data = member.read()
                target = vendor_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                entry["sha256"] = sha256_hex(data)
    (vendor_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned local ESM for the dashboard.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="offline verification")
    group.add_argument("--fetch", action="store_true", help="download + verify + rewrite")
    args = parser.parse_args(argv)
    if args.fetch:
        return fetch()
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"vendor check FAILED: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    count = len(manifest_files(_load_json(HERE / "MANIFEST.json")))
    print(f"vendor check ok: {count} files verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
