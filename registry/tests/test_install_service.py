"""dashboard/service/install-service.sh writes the svc port file from the
effective TRIO_DASH_PORT: the service env file's last assignment (svc sources
it after the environment), else the environment, else 9470.

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

    def _svc_port(self, environment_port: str | None) -> str:
        """The TRIO_DASH_PORT `run` sees: svc sources ./env (set -a) after the
        inherited environment, then execs run (default 9470)."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "TRIO_DASH_"))}
        env["HOME"] = str(self.home)
        if environment_port is not None:
            env["TRIO_DASH_PORT"] = environment_port
        return subprocess.run(
            ["bash", "-c",
             'cd "$1"; set -a; . ./env; set +a; printf %s "${TRIO_DASH_PORT:-9470}"',
             "_", str(self.svc_dir)],
            env=env, capture_output=True, text=True, check=True,
        ).stdout

    def _last_port_assignment(self) -> str:
        lines = [ln.strip() for ln in (self.svc_dir / "env").read_text().splitlines()
                 if ln.strip().startswith("TRIO_DASH_PORT=")]
        return lines[-1].split("=", 1)[1]

    def test_environment_port_wins(self) -> None:
        r = self._install("22555")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "22555")
        self.assertEqual(self._last_port_assignment(), "22555")

    def test_env_file_port_when_environment_unset(self) -> None:
        self._write_env("TRIO_DASH_CHECKOUT=/x\nTRIO_DASH_PORT=9555\n")
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9555")

    def test_env_file_beats_environment(self) -> None:
        """GOAL constraint 'port file from the effective TRIO_DASH_PORT' + svc
        sources the env file after the environment, so the env file wins."""
        self._write_env("TRIO_DASH_PORT=9555\n")
        r = self._install("22555")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.port_file.read_text().strip(), "9555")
        self.assertIn(str(self.svc_dir / "env"), r.stderr)
        self.assertIn("9555", r.stderr)
        self.assertEqual((self.svc_dir / "env").read_text(), "TRIO_DASH_PORT=9555\n")
        self.assertEqual(self._svc_port("22555"), "9555")

    def test_port_file_matches_svc_sourced_value(self) -> None:
        cases = [
            ("fresh, env 22555", None, "22555"),
            ("existing 9555, env 22555", "TRIO_DASH_PORT=9555\n", "22555"),
            ("fresh, unset", None, None),
            ("existing without line, env 22555", "TRIO_DASH_CHECKOUT=/x\n", "22555"),
        ]
        for label, env_text, environment in cases:
            with self.subTest(label):
                shutil.rmtree(self.home / ".services", ignore_errors=True)
                _git(self.repo, "worktree", "prune")
                if env_text is not None:
                    self._write_env(env_text)
                r = self._install(environment)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.port_file.read_text().strip(),
                                 self._svc_port(environment))

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
        self.assertFalse((self.svc_dir / "checkout").exists())

    def test_expansion_in_env_file_refused_without_side_effects(self) -> None:
        self._write_env("TRIO_DASH_PORT=${TRIO_DASH_PORT:-22002}\n")
        self.port_file.write_text("1234\n")
        before = (self.svc_dir / "env").read_bytes()
        r = self._install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.port_file.read_text(), "1234\n")
        self.assertEqual((self.svc_dir / "env").read_bytes(), before)
        self.assertFalse((self.svc_dir / "run").exists())
        self.assertFalse((self.svc_dir / "checkout").exists())

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
