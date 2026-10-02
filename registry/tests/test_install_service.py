"""dashboard/service/install-service.sh writes the svc port file from the
effective TRIO_DASH_PORT (environment, else the service env file, else 9470).

Every run happens in a throwaway git repo and a throwaway HOME; the real HOME
and ~/.services are never touched."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SERVICE_SRC = Path(__file__).resolve().parents[2] / "dashboard" / "service"
FILES = ("install-service.sh", "run", "env.example")


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


class InstallServicePortTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="install-svc-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.repo = root / "repo"
        self.home = root / "home"
        self.home.mkdir()
        svc = self.repo / "dashboard" / "service"
        svc.mkdir(parents=True)
        for name in FILES:
            shutil.copy2(SERVICE_SRC / name, svc / name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "fixture")
        self.sha = _git(self.repo, "rev-parse", "HEAD")
        self.svc_dir = self.home / ".services" / "trio-dash"
        self.port_file = self.svc_dir / "port"

    def _install(self, port: str | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "TRIO_DASH_"))}
        env["HOME"] = str(self.home)
        if port is not None:
            env["TRIO_DASH_PORT"] = port
        return subprocess.run(
            ["bash", str(self.repo / "dashboard" / "service" / "install-service.sh"), self.sha],
            env=env, capture_output=True, text=True,
        )

    def _write_env(self, text: str) -> None:
        self.svc_dir.mkdir(parents=True)
        (self.svc_dir / "env").write_text(text)

    def test_environment_port_wins(self) -> None:
        r = self._install("22555")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "22555")

    def test_env_file_port_when_environment_unset(self) -> None:
        self._write_env("TRIO_DASH_CHECKOUT=/x\nTRIO_DASH_PORT=9555\n")
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9555")

    def test_environment_beats_env_file(self) -> None:
        self._write_env("TRIO_DASH_PORT=9555\n")
        r = self._install("22555")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "22555")

    def test_last_env_file_assignment_wins(self) -> None:
        self._write_env("TRIO_DASH_PORT=9555\n#TRIO_DASH_PORT=1\nTRIO_DASH_PORT=9666\n")
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9666")

    def test_default_when_neither_set(self) -> None:
        self._write_env("TRIO_DASH_CHECKOUT=/x\n")
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9470")

    def test_default_from_fresh_env_example(self) -> None:
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9470")

    def test_non_numeric_port_refused_without_port_file(self) -> None:
        r = self._install("abc")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.port_file.exists())

    def test_out_of_range_port_refused_and_keeps_existing_port_file(self) -> None:
        self._write_env("TRIO_DASH_CHECKOUT=/x\n")
        self.assertEqual(self._install("22555").returncode, 0)
        r = self._install("70000")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.port_file.read_text().strip(), "22555")
        r = self._install("0")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
