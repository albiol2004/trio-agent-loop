"""Harbor custom agent: runs the trio-opencode loop (Lead/Builder/Evaluator/
Repair/Scout over ``opencode run``) inside a Terminal-Bench 4.0 task
container, treating the whole container as the product.

Loaded as ``--agent trio_tbench_agent:TrioOpenCodeAgent`` with ``tbench/``
on ``PYTHONPATH`` (see ``run_job.sh``); its sibling modules (``bundle``,
``configgen``, ``goal``, ``shell``, ``usage``) are imported bare, as
top-level modules alongside this one, not as a ``tbench.`` package -- they
must resolve the same way whether Harbor imports this file from
``PYTHONPATH=tbench`` or a test imports them directly.

Design summary (see the builder's final report for the full rationale):

* ``install()`` ensures git/tar/ca-certificates/procps, uploads+extracts a
  bundled standalone CPython and the trio-opencode repo slice, uploads the
  OpenCode binary, and verifies both run. Fails fast and clearly on a musl
  base image (the bundled binaries are glibc-dynamic).
* ``run()`` uploads the API key file straight from its host path to
  ``/run/trio/opencode.key`` via ``environment.upload_file`` -- the key
  bytes never pass through this process -- turns the task's working
  directory (or a separate ``/trio-ws``, if the working directory is
  already a git repo the task is ABOUT) into the loop's mailbox repo,
  writes ``loop/GOAL.md``, launches the driver detached, and polls until it
  exits with no wall-clock deadline of its own. It then collects the
  mailbox/runtime logs, deletes the key, computes token usage defensively
  from OpenCode's session storage, and writes
  ``/logs/agent/trio-summary.json`` / ``trio-usage.json``.
"""
from __future__ import annotations

import asyncio
import json
import time
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, NonZeroAgentExitCodeError
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext, ModelUsage

import bundle
import configgen
import goal
import shell

_TBENCH_DIR = Path(__file__).resolve().parent


class TrioOpenCodeAgent(BaseInstalledAgent):
    """See module docstring. No ``options_model``: constructor kwargs are
    plain Python defaults, set via Harbor's ``--ak``/``agents[].kwargs``."""

    _INSTALL_ROOT = "/opt/trio"
    _PYTHON_BIN = f"{_INSTALL_ROOT}/python/bin/python3"
    _CLI_PATH = f"{_INSTALL_ROOT}/repo/opencode-driver/trio_opencode/cli.py"
    _OPENCODE_BIN_DIR = f"{_INSTALL_ROOT}/bin"
    _OPENCODE_BIN_PATH = f"{_OPENCODE_BIN_DIR}/opencode"
    _USAGE_PARSER_PATH = f"{_INSTALL_ROOT}/usage_parser.py"
    _CONFIG_PATH = f"{_INSTALL_ROOT}/config.json"
    _RUN_KEY_PATH = "/run/trio/opencode.key"
    _AGENT_LOG_DIR = "/logs/agent"
    _DRIVER_LOG_PATH = f"{_AGENT_LOG_DIR}/trio-driver.log"
    _DRIVER_EXIT_CODE_PATH = f"{_AGENT_LOG_DIR}/trio-exit-code"
    _TRIO_WS = "/trio-ws"

    _POLL_INTERVAL_SECONDS = 60
    _POLL_EXEC_TIMEOUT_SECONDS = 120

    _PKG_MANAGERS: tuple[str, ...] = (
        "apt-get", "dnf", "microdnf", "yum", "apk", "zypper", "pacman",
    )
    _PKG_NAMES: dict[str, dict[str, str]] = {
        "apt-get": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps"},
        "dnf": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps-ng"},
        "microdnf": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps-ng"},
        "yum": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps-ng"},
        "apk": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps"},
        "zypper": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps"},
        "pacman": {"git": "git", "tar": "tar", "ca_certificates": "ca-certificates", "procps": "procps-ng"},
    }

    def __init__(
        self,
        logs_dir,
        *,
        key_file: str = "~/Documents/OpenCodeKey.txt",
        repo_root: str | None = None,
        bundle_dir: str | None = None,
        python_src: str | None = None,
        opencode_binary: str | None = None,
        models: dict[str, str] | None = None,
        variants: dict[str, str] | None = None,
        provider_id: str = configgen.DEFAULT_PROVIDER_ID,
        key_env: str = configgen.DEFAULT_KEY_ENV,
        max_iterations: int = 8,
        turn_seconds: float = 0,
        idle_seconds: float = 1800,
        evaluator_turn_seconds: float = 0,
        max_attempts: int = 6,
        backoff_seconds: list[float] | None = None,
        idle_retry_unlimited: bool = True,
        root_free: bool = False,
        container_mode: bool = True,
        acceptance: bool = False,
        open_loop: bool = False,
        slice_eval_concurrency: int | None = None,
        no_isolate_workers: bool = False,
        slice_eval_drain_seconds: float | None = None,
        no_kill_check: bool = False,
        **kwargs: Any,
    ) -> None:
        self._key_file = Path(key_file).expanduser()
        self._repo_root = (
            Path(repo_root).expanduser() if repo_root else _TBENCH_DIR.parent
        )
        self._bundle_dir = Path(bundle_dir).expanduser() if bundle_dir else bundle.DEFAULT_BUNDLE_DIR
        self._python_src = Path(python_src).expanduser() if python_src else bundle.DEFAULT_PYTHON_SRC
        self._opencode_binary = (
            Path(opencode_binary).expanduser() if opencode_binary else bundle.DEFAULT_OPENCODE_BIN
        )
        self._models = dict(models) if models else None
        self._variants = dict(variants) if variants else None
        self._provider_id = provider_id
        self._key_env = key_env
        self._max_iterations = int(max_iterations)
        self._turn_seconds = turn_seconds
        self._idle_seconds = idle_seconds
        self._evaluator_turn_seconds = evaluator_turn_seconds
        self._max_attempts = int(max_attempts)
        self._backoff_seconds = list(backoff_seconds) if backoff_seconds else None
        self._idle_retry_unlimited = bool(idle_retry_unlimited)
        self._root_free = bool(root_free)
        self._container_mode = bool(container_mode)
        # r19 frozen acceptance (driver config `acceptance`; off by default,
        # like the installed release). Harbor: `--ak acceptance=true`.
        self._acceptance = bool(acceptance)
        # Open-loop arm (off by default, byte-identical output otherwise).
        # Harbor: `--ak open_loop=true`. Unlike `acceptance`, this is NOT a
        # config.json field -- the driver and trio-opencode's own CLI
        # select open-loop mode purely on `loop/QUEUE.md` existing as a
        # file (see goal.mailbox_files); it never reads a flag for it.
        self._open_loop = bool(open_loop)
        # D12 open-loop pass-through flags (trio_opencode/cli.py `start`/
        # `resume`; no-ops on a lockstep mailbox). Harbor:
        # `--ak slice_eval_concurrency=4 --ak no_isolate_workers=true
        #  --ak slice_eval_drain_seconds=30 --ak no_kill_check=true`.
        self._slice_eval_concurrency = (
            int(slice_eval_concurrency) if slice_eval_concurrency is not None else None
        )
        self._no_isolate_workers = bool(no_isolate_workers)
        self._slice_eval_drain_seconds = (
            float(slice_eval_drain_seconds) if slice_eval_drain_seconds is not None else None
        )
        self._no_kill_check = bool(no_kill_check)
        super().__init__(logs_dir, **kwargs)

    @staticmethod
    def name() -> str:
        return "trio-opencode"

    def get_version_command(self) -> str | None:
        return f"{self._OPENCODE_BIN_PATH} --version"

    # ------------------------------------------------------------- install

    async def install(self, environment: BaseEnvironment) -> None:
        await self._assert_glibc(environment)
        await self._ensure_system_dependencies(environment)
        await self._check_git_version(environment)
        await self.exec_as_root(environment, command=shell.GIT_SAFE_DIRECTORY_CMD)

        paths = bundle.ensure_bundle(
            repo_root=self._repo_root,
            bundle_dir=self._bundle_dir,
            python_src=self._python_src,
            opencode_binary=self._opencode_binary,
        )

        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {self._INSTALL_ROOT}/python {self._INSTALL_ROOT}/repo "
                f"{self._OPENCODE_BIN_DIR}"
            ),
        )
        await environment.upload_file(paths.python_tarball, "/tmp/trio-python.tar.gz")
        await environment.upload_file(paths.repo_tarball, "/tmp/trio-repo.tar.gz")
        await environment.upload_file(paths.opencode_binary, self._OPENCODE_BIN_PATH)
        await environment.upload_file(_TBENCH_DIR / "usage.py", self._USAGE_PARSER_PATH)

        await self.exec_as_root(
            environment,
            command=(
                f"tar xzf /tmp/trio-python.tar.gz -C {self._INSTALL_ROOT}/python && "
                f"tar xzf /tmp/trio-repo.tar.gz -C {self._INSTALL_ROOT}/repo && "
                "rm -f /tmp/trio-python.tar.gz /tmp/trio-repo.tar.gz && "
                f"chmod +x {self._OPENCODE_BIN_PATH} && "
                f"chmod -R a+rX {self._INSTALL_ROOT}"
            ),
        )

        opencode_check = await self.exec_as_root(
            environment, command=f"{self._OPENCODE_BIN_PATH} --version"
        )
        if opencode_check.return_code != 0:
            raise RuntimeError(
                f"opencode --version failed after install: {opencode_check.stderr}"
            )
        python_check = await self.exec_as_root(
            environment, command=f"{self._PYTHON_BIN} --version"
        )
        if python_check.return_code != 0:
            raise RuntimeError(
                f"bundled python3 failed to run after extraction: {python_check.stderr}"
            )

    async def _assert_glibc(self, environment: BaseEnvironment) -> None:
        alpine = await environment.exec(command="[ -f /etc/alpine-release ]", user="root")
        ldd_result = await environment.exec(command="ldd --version 2>&1 || true", user="root")
        ldd_output = (ldd_result.stdout or "") + (ldd_result.stderr or "")
        if shell.is_musl_output(ldd_output, has_alpine_release=(alpine.return_code == 0)):
            raise RuntimeError(
                "musl/Alpine base image detected; the bundled CPython and "
                "OpenCode binaries are glibc-dynamic and cannot run here "
                "(infra error -- TrioOpenCodeAgent does not support musl "
                "task images)."
            )

    async def _ensure_system_dependencies(self, environment: BaseEnvironment) -> None:
        probe = await environment.exec(
            command=(
                "ok=1; "
                "command -v git >/dev/null 2>&1 || ok=0; "
                "command -v tar >/dev/null 2>&1 || ok=0; "
                "command -v pgrep >/dev/null 2>&1 || ok=0; "
                "( [ -d /etc/ssl/certs ] || [ -f /etc/ssl/cert.pem ] ) || ok=0; "
                "echo $ok"
            ),
            user="root",
        )
        if (probe.stdout or "").strip() == "1":
            return

        manager = None
        for candidate in self._PKG_MANAGERS:
            check = await environment.exec(
                command=f"command -v {candidate} >/dev/null 2>&1", user="root"
            )
            if check.return_code == 0:
                manager = candidate
                break
        if manager is None:
            raise RuntimeError(
                "No supported package manager found (tried "
                f"{', '.join(self._PKG_MANAGERS)}) and git/tar/procps/"
                "ca-certificates are not already present; cannot install "
                "system dependencies for TrioOpenCodeAgent (infra error)."
            )

        pkgs = " ".join(self._PKG_NAMES[manager].values())
        env: dict[str, str] | None = None
        if manager == "apt-get":
            command = f"apt-get update && apt-get install -y {pkgs}"
            env = {"DEBIAN_FRONTEND": "noninteractive"}
        elif manager == "microdnf":
            command = f"microdnf install -y {pkgs}"
        elif manager in ("dnf", "yum"):
            command = f"{manager} install -y {pkgs}"
        elif manager == "apk":
            command = f"apk add --no-cache {pkgs}"
        elif manager == "zypper":
            command = f"zypper --non-interactive install {pkgs}"
        elif manager == "pacman":
            command = f"pacman -Sy --noconfirm {pkgs}"
        else:  # pragma: no cover - exhaustive over _PKG_MANAGERS
            raise RuntimeError(f"unsupported package manager: {manager}")

        await self.exec_as_root(environment, command=command, env=env)

    async def _check_git_version(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(command="git --version", user="root")
        output = (result.stdout or "") + (result.stderr or "")
        if shell.git_version_at_least(output) is False:
            self.logger.warning("git version below the required >=2.30 floor: %s", output.strip())
            await self.exec_as_root(
                environment,
                command=(
                    "mkdir -p /logs/agent && "
                    f"printf '%s\\n' {shlex.quote('WARNING: git < 2.30: ' + output.strip())} "
                    ">> /logs/agent/trio-install-notes.txt"
                ),
            )

    # ------------------------------------------------------------------ run

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        workdir = await self._discover_workdir(environment)
        mode = await self._detect_mode(environment, workdir)
        repo_root = self._TRIO_WS if mode == "detached" else workdir
        mailbox_dir = f"{repo_root}/loop"

        await self.exec_as_root(environment, command=shell.GIT_SAFE_DIRECTORY_CMD)
        await self._init_repo(environment, mode=mode, workdir=workdir)
        await self._write_mailbox(
            environment, instruction, workdir=workdir, mode=mode, repo_root=repo_root
        )

        resumed = False
        driver_exit_code: int | None = None
        await self._upload_key(environment)
        t0 = time.monotonic()
        try:
            await self._upload_config_text(
                environment,
                content=json.dumps(self._build_config(), indent=2) + "\n",
                remote_path=self._CONFIG_PATH,
                filename="config.json",
            )
            driver_exit_code = await self._launch_and_wait(
                environment, mailbox_dir=mailbox_dir, repo_root=repo_root, subcommand="start"
            )
            if driver_exit_code == 3:
                self.logger.warning(
                    "trio-opencode driver exited 3 (error); attempting one resume"
                )
                resumed = True
                driver_exit_code = await self._launch_and_wait(
                    environment, mailbox_dir=mailbox_dir, repo_root=repo_root,
                    subcommand="resume",
                )
        finally:
            wall_seconds = round(time.monotonic() - t0, 1)
            await self._collect_logs(environment, mailbox_dir=mailbox_dir, repo_root=repo_root)
            await self._delete_key(environment)

        result = await self._read_opencode_result(environment, mailbox_dir=mailbox_dir)
        usage = await self._compute_usage(environment, repo_root=repo_root)
        self._apply_usage_to_context(usage, context)
        await self._upload_config_text(
            environment,
            content=json.dumps(usage, indent=2) + "\n",
            remote_path=f"{self._AGENT_LOG_DIR}/trio-usage.json",
            filename="trio-usage.json",
        )

        iterations = (result or {}).get("iterations")
        summary = {
            "mode": mode,
            "workdir": workdir,
            "driver_exit_code": driver_exit_code,
            "status": (result or {}).get("status", "unknown"),
            "verdict": (result or {}).get("status"),
            "iterations": len(iterations) if isinstance(iterations, list) else (result or {}).get("iteration"),
            "wall_seconds": wall_seconds,
            "resumed": resumed,
        }
        await self._upload_config_text(
            environment,
            content=json.dumps(summary, indent=2) + "\n",
            remote_path=f"{self._AGENT_LOG_DIR}/trio-summary.json",
            filename="trio-summary.json",
        )

        if driver_exit_code is None:
            raise NonZeroAgentExitCodeError(
                "trio-opencode driver never reported an exit code (see "
                f"{self._DRIVER_LOG_PATH} in the collected agent logs)"
            )

    async def _discover_workdir(self, environment: BaseEnvironment) -> str:
        result = await environment.exec(command="pwd")
        return (result.stdout or "").strip() or "/"

    async def _detect_mode(self, environment: BaseEnvironment, workdir: str) -> str:
        result = await environment.exec(
            command=(
                f"git -C {shlex.quote(workdir)} rev-parse --is-inside-work-tree "
                ">/dev/null 2>&1"
            ),
        )
        return "detached" if result.return_code == 0 else "workdir"

    async def _init_repo(self, environment: BaseEnvironment, *, mode: str, workdir: str) -> None:
        script = (
            shell.build_workdir_baseline_script(workdir)
            if mode == "workdir"
            else shell.build_detached_init_script(self._TRIO_WS)
        )
        result = await self.exec_as_root(environment, command=script, timeout_sec=600)
        self.logger.debug("repo init (%s): rc=%s", mode, result.return_code)

    async def _write_mailbox(
        self,
        environment: BaseEnvironment,
        instruction: str,
        *,
        workdir: str,
        mode: str,
        repo_root: str,
    ) -> None:
        files = goal.mailbox_files(
            instruction, workdir=workdir, mode=mode, open_loop=self._open_loop
        )
        mailbox_dir = f"{repo_root}/loop"
        await self.exec_as_root(environment, command=f"mkdir -p {shlex.quote(mailbox_dir)}")
        for name, content in files.items():
            await self._upload_config_text(
                environment,
                content=content,
                remote_path=f"{mailbox_dir}/{name}",
                filename=name,
            )
        await self.exec_as_root(
            environment, command=shell.build_goal_commit_script(repo_root)
        )

    async def _upload_key(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command="mkdir -p /run/trio")
        # Host file -> container file, directly; the key bytes never pass
        # through this process. See module docstring and HARD RULES.
        await environment.upload_file(self._key_file, self._RUN_KEY_PATH)
        await self.exec_as_root(environment, command=f"chmod 600 {self._RUN_KEY_PATH}")

    async def _delete_key(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command=f"rm -f {self._RUN_KEY_PATH}")

    def _build_config(self) -> dict[str, Any]:
        return configgen.build_driver_config(
            opencode_bin=self._OPENCODE_BIN_PATH,
            models=self._models,
            variants=self._variants,
            provider_id=self._provider_id,
            key_file=self._RUN_KEY_PATH,
            key_env=self._key_env,
            turn_seconds=self._turn_seconds,
            idle_seconds=self._idle_seconds,
            evaluator_turn_seconds=self._evaluator_turn_seconds,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff_seconds,
            idle_retry_unlimited=self._idle_retry_unlimited,
            max_iterations=self._max_iterations,
            root_free=self._root_free,
            container_mode=self._container_mode,
            acceptance=self._acceptance,
        )

    async def _launch_and_wait(
        self,
        environment: BaseEnvironment,
        *,
        mailbox_dir: str,
        repo_root: str,
        subcommand: str,
    ) -> int | None:
        launch_cmd = shell.build_launch_command(
            python_bin=self._PYTHON_BIN,
            cli_path=self._CLI_PATH,
            mailbox_dir=mailbox_dir,
            max_iterations=self._max_iterations,
            config_path=self._CONFIG_PATH,
            opencode_bin_dir=self._OPENCODE_BIN_DIR,
            log_path=self._DRIVER_LOG_PATH,
            exit_code_path=self._DRIVER_EXIT_CODE_PATH,
            cwd=repo_root,
            subcommand=subcommand,
            slice_eval_concurrency=self._slice_eval_concurrency,
            no_isolate_workers=self._no_isolate_workers,
            slice_eval_drain_seconds=self._slice_eval_drain_seconds,
            no_kill_check=self._no_kill_check,
        )
        await self.exec_as_root(environment, command=f"mkdir -p {self._AGENT_LOG_DIR}")
        await self.exec_as_root(environment, command=launch_cmd)

        poll_cmd = shell.build_poll_command(self._DRIVER_EXIT_CODE_PATH)
        while True:
            output = "RUNNING"
            try:
                result = await environment.exec(
                    command=poll_cmd, user="root", timeout_sec=self._POLL_EXEC_TIMEOUT_SECONDS
                )
                output = (result.stdout or "").strip() or "RUNNING"
            except Exception:  # noqa: BLE001 - a transient poll failure must not stop polling
                self.logger.debug("poll exec failed transiently; retrying", exc_info=True)
            if output != "RUNNING":
                try:
                    return int(output)
                except ValueError:
                    self.logger.debug("unexpected poll output %r; continuing to poll", output)
            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)

    async def _collect_logs(
        self, environment: BaseEnvironment, *, mailbox_dir: str, repo_root: str
    ) -> None:
        dest = f"{self._AGENT_LOG_DIR}/trio"
        quoted_mailbox = shlex.quote(mailbox_dir)
        quoted_repo = shlex.quote(repo_root)
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {dest}/mailbox {dest}/runtime && "
                f"cp -a {quoted_mailbox}/. {dest}/mailbox/ 2>/dev/null; "
                f"if [ -d {quoted_repo}/.trio-opencode ]; then "
                f"cp -a {quoted_repo}/.trio-opencode {dest}/runtime/dot-trio-opencode 2>/dev/null; fi; "
                f"if [ -d {quoted_repo}/.git/trio-opencode ]; then "
                f"cp -a {quoted_repo}/.git/trio-opencode {dest}/runtime/git-trio-opencode 2>/dev/null; fi; "
                "true"
            ),
        )

    async def _read_opencode_result(
        self, environment: BaseEnvironment, *, mailbox_dir: str
    ) -> dict[str, Any] | None:
        result = await environment.exec(
            command=f"cat {shlex.quote(mailbox_dir)}/.opencode-result.json 2>/dev/null || true",
        )
        text = (result.stdout or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def _compute_usage(
        self, environment: BaseEnvironment, *, repo_root: str
    ) -> dict[str, Any]:
        common_dir_result = await environment.exec(
            command=(
                f"git -C {shlex.quote(repo_root)} rev-parse "
                "--path-format=absolute --git-common-dir 2>/dev/null"
            ),
        )
        common_dir = (common_dir_result.stdout or "").strip()
        roots: list[str] = []
        if common_dir:
            find_result = await environment.exec(
                command=shell.build_find_xdg_data_dirs_command(common_dir)
            )
            roots = [line.strip() for line in (find_result.stdout or "").splitlines() if line.strip()]

        usage_cmd = (
            f"{self._PYTHON_BIN} {self._USAGE_PARSER_PATH} "
            + " ".join(shlex.quote(root) for root in roots)
        )
        usage_result = await environment.exec(command=usage_cmd, timeout_sec=120)
        try:
            usage = json.loads((usage_result.stdout or "").strip() or "{}")
        except json.JSONDecodeError:
            usage = {}
        if not isinstance(usage, dict) or not usage:
            usage = {"usage_source": "none", "totals": {}, "by_model": {}, "message_count": 0}
        return usage

    @staticmethod
    def _apply_usage_to_context(usage: dict[str, Any], context: AgentContext) -> None:
        totals = usage.get("totals")
        totals = totals if isinstance(totals, dict) else {}
        input_tokens = int(totals.get("input_tokens", 0) or 0)
        cache_read = int(totals.get("cache_read_tokens", 0) or 0)
        output_tokens = int(totals.get("output_tokens", 0) or 0)

        context.n_input_tokens = input_tokens + cache_read
        context.n_output_tokens = output_tokens
        context.n_cache_tokens = cache_read
        context.cost_usd = None

        by_model = usage.get("by_model")
        if isinstance(by_model, dict) and by_model:
            context.model_usage = {
                model: ModelUsage(
                    n_input_tokens=int(t.get("input_tokens", 0) or 0)
                    + int(t.get("cache_read_tokens", 0) or 0),
                    n_cache_tokens=int(t.get("cache_read_tokens", 0) or 0),
                    n_output_tokens=int(t.get("output_tokens", 0) or 0),
                    cost_usd=None,
                )
                for model, t in by_model.items()
                if isinstance(t, dict)
            }
