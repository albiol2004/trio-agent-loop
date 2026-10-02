#!/usr/bin/env python3
"""Manual docker-based bootstrap check for ``TrioOpenCodeAgent.install()``.

Mimics ``environment.exec``/``environment.upload_file`` with ``docker
exec``/``docker cp`` against a throwaway container built from a real
Terminal-Bench task image: runs the same dependency-probe, bundle-upload,
extract and version-check sequence ``install()`` does, then removes the
container. NOT collected by the default ``pytest tbench/tests`` run (needs
docker, network access to pull the image, and the real bundled assets on
this host) -- run it directly:

    python3 tbench/tests/docker_bootstrap_check.py <image> [<image> ...]

Never uploads the API key file -- this only exercises install(), not run().
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle  # noqa: E402

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _docker_exec(container: str, command: str, user: str = "root") -> subprocess.CompletedProcess:
    return _run(["docker", "exec", "-u", user, container, "bash", "-lc", command])


def _docker_cp(src: Path, container: str, dest: str) -> subprocess.CompletedProcess:
    return _run(["docker", "cp", str(src), f"{container}:{dest}"])


def check_image(image: str, *, repo_root: Path = DEFAULT_REPO_ROOT) -> dict[str, Any]:
    container = f"tbench-bootstrap-{uuid.uuid4().hex[:8]}"
    report: dict[str, Any] = {"image": image, "container": container}
    try:
        start = _run(["docker", "run", "-d", "--name", container, image, "sleep", "infinity"])
        if start.returncode != 0:
            report["error"] = f"docker run failed: {start.stderr.strip()}"
            return report

        alpine = _docker_exec(container, "[ -f /etc/alpine-release ]")
        ldd = _docker_exec(container, "ldd --version 2>&1 || true")
        import shell  # local import: tests/ already has tbench/ on sys.path

        report["musl"] = shell.is_musl_output(
            ldd.stdout + ldd.stderr, has_alpine_release=(alpine.returncode == 0)
        )

        probe = _docker_exec(
            container,
            (
                "ok=1; command -v git >/dev/null 2>&1 || ok=0; "
                "command -v tar >/dev/null 2>&1 || ok=0; "
                "command -v pgrep >/dev/null 2>&1 || ok=0; "
                "( [ -d /etc/ssl/certs ] || [ -f /etc/ssl/cert.pem ] ) || ok=0; echo $ok"
            ),
        )
        report["deps_already_present"] = probe.stdout.strip() == "1"

        if not report["deps_already_present"]:
            manager = None
            for candidate in ("apt-get", "dnf", "microdnf", "yum", "apk", "zypper", "pacman"):
                check = _docker_exec(container, f"command -v {candidate} >/dev/null 2>&1")
                if check.returncode == 0:
                    manager = candidate
                    break
            report["package_manager"] = manager
            install_cmd = {
                "apt-get": "apt-get update && apt-get install -y git tar ca-certificates procps",
                "microdnf": "microdnf install -y git tar ca-certificates procps-ng",
                "dnf": "dnf install -y git tar ca-certificates procps-ng",
                "yum": "yum install -y git tar ca-certificates procps-ng",
                "apk": "apk add --no-cache git tar ca-certificates procps",
                "zypper": "zypper --non-interactive install git tar ca-certificates procps",
                "pacman": "pacman -Sy --noconfirm git tar ca-certificates procps-ng",
            }.get(manager or "")
            if install_cmd:
                install = _docker_exec(container, install_cmd)
                report["install_rc"] = install.returncode
                report["install_stderr_tail"] = install.stderr[-500:]

        git_version = _docker_exec(container, "git --version")
        report["git_version"] = git_version.stdout.strip()
        report["git_version_at_least_2_30"] = shell.git_version_at_least(
            git_version.stdout + git_version.stderr
        )

        if report["musl"]:
            report["skipped_bundle"] = "musl base image; TrioOpenCodeAgent.install() raises here"
            return report

        paths = bundle.ensure_bundle(repo_root=repo_root)
        _docker_exec(container, "mkdir -p /opt/trio/python /opt/trio/repo /opt/trio/bin")
        _docker_cp(paths.python_tarball, container, "/tmp/trio-python.tar.gz")
        _docker_cp(paths.repo_tarball, container, "/tmp/trio-repo.tar.gz")
        _docker_cp(paths.opencode_binary, container, "/opt/trio/bin/opencode")

        extract = _docker_exec(
            container,
            (
                "tar xzf /tmp/trio-python.tar.gz -C /opt/trio/python && "
                "tar xzf /tmp/trio-repo.tar.gz -C /opt/trio/repo && "
                "rm -f /tmp/trio-python.tar.gz /tmp/trio-repo.tar.gz && "
                "chmod +x /opt/trio/bin/opencode && chmod -R a+rX /opt/trio"
            ),
        )
        report["extract_rc"] = extract.returncode
        report["extract_stderr_tail"] = extract.stderr[-500:]

        py_check = _docker_exec(container, "/opt/trio/python/bin/python3 --version")
        report["python_rc"] = py_check.returncode
        report["python_version"] = (py_check.stdout or py_check.stderr).strip()

        oc_check = _docker_exec(container, "/opt/trio/bin/opencode --version")
        report["opencode_rc"] = oc_check.returncode
        report["opencode_version"] = (oc_check.stdout or oc_check.stderr).strip()

        repo_listing = _docker_exec(container, "ls /opt/trio/repo")
        report["repo_listing"] = repo_listing.stdout.strip().splitlines()

        pwd_check = _docker_exec(container, "pwd")
        report["workdir"] = pwd_check.stdout.strip()

        return report
    finally:
        _run(["docker", "rm", "-f", container])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+")
    args = parser.parse_args(argv)
    ok = True
    for image in args.images:
        print(f"=== {image} ===")
        report = check_image(image)
        for key, value in report.items():
            print(f"  {key}: {value}")
        if report.get("error") or report.get("python_rc") not in (0, None) or report.get("opencode_rc") not in (0, None):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
