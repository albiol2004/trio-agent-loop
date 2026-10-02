import shell


def test_build_launch_command_never_contains_auto_or_skip_permissions():
    cmd = shell.build_launch_command(
        python_bin="/opt/trio/python/bin/python3",
        cli_path="/opt/trio/repo/opencode-driver/trio_opencode/cli.py",
        mailbox_dir="/app/loop",
        max_iterations=8,
        config_path="/opt/trio/config.json",
        opencode_bin_dir="/opt/trio/bin",
        log_path="/logs/agent/trio-driver.log",
        exit_code_path="/logs/agent/trio-exit-code",
        cwd="/app",
    )
    assert "--auto" not in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_build_launch_command_runs_detached_and_captures_exit_code():
    cmd = shell.build_launch_command(
        python_bin="/opt/trio/python/bin/python3",
        cli_path="/opt/trio/repo/opencode-driver/trio_opencode/cli.py",
        mailbox_dir="/app/loop",
        max_iterations=8,
        config_path="/opt/trio/config.json",
        opencode_bin_dir="/opt/trio/bin",
        log_path="/logs/agent/trio-driver.log",
        exit_code_path="/logs/agent/trio-exit-code",
        cwd="/app",
    )
    assert "nohup setsid" in cmd
    assert "disown" in cmd
    assert "/logs/agent/trio-exit-code" in cmd
    assert "start" in cmd
    assert "--mailbox" in cmd
    assert "--max-iterations 8" in cmd
    assert "--in-place" in cmd


def test_build_launch_command_resume_subcommand():
    cmd = shell.build_launch_command(
        python_bin="python3", cli_path="cli.py", mailbox_dir="/app/loop",
        max_iterations=4, config_path="/cfg.json", opencode_bin_dir="/bin",
        log_path="/log", exit_code_path="/exit", cwd="/app", subcommand="resume",
    )
    assert " resume " in cmd or cmd.count("resume") >= 1
    assert " start " not in cmd


def _base_launch_kwargs(**overrides):
    kwargs = dict(
        python_bin="/opt/trio/python/bin/python3",
        cli_path="/opt/trio/repo/opencode-driver/trio_opencode/cli.py",
        mailbox_dir="/app/loop",
        max_iterations=8,
        config_path="/opt/trio/config.json",
        opencode_bin_dir="/opt/trio/bin",
        log_path="/logs/agent/trio-driver.log",
        exit_code_path="/logs/agent/trio-exit-code",
        cwd="/app",
    )
    kwargs.update(overrides)
    return kwargs


def test_build_launch_command_open_loop_flags_off_by_default_is_byte_identical():
    # No pass-through kwargs at all vs. every one of them passed at its
    # documented default: both must produce the exact same command line as
    # before these kwargs existed.
    bare = shell.build_launch_command(**_base_launch_kwargs())
    explicit_defaults = shell.build_launch_command(
        **_base_launch_kwargs(
            slice_eval_concurrency=None,
            no_isolate_workers=False,
            slice_eval_drain_seconds=None,
            no_kill_check=False,
        )
    )
    assert bare == explicit_defaults
    for flag in (
        "--slice-eval-concurrency",
        "--no-isolate-workers",
        "--slice-eval-drain-seconds",
        "--no-kill-check",
    ):
        assert flag not in bare


def test_build_launch_command_emits_slice_eval_concurrency_when_set():
    cmd = shell.build_launch_command(**_base_launch_kwargs(slice_eval_concurrency=6))
    assert "--slice-eval-concurrency 6" in cmd
    assert "--no-isolate-workers" not in cmd
    assert "--slice-eval-drain-seconds" not in cmd
    assert "--no-kill-check" not in cmd


def test_build_launch_command_emits_no_isolate_workers_when_true():
    cmd = shell.build_launch_command(**_base_launch_kwargs(no_isolate_workers=True))
    assert "--no-isolate-workers" in cmd
    assert "--slice-eval-concurrency" not in cmd


def test_build_launch_command_emits_slice_eval_drain_seconds_when_set():
    cmd = shell.build_launch_command(**_base_launch_kwargs(slice_eval_drain_seconds=45))
    assert "--slice-eval-drain-seconds 45" in cmd


def test_build_launch_command_emits_no_kill_check_when_true():
    cmd = shell.build_launch_command(**_base_launch_kwargs(no_kill_check=True))
    assert "--no-kill-check" in cmd


def test_build_launch_command_emits_all_open_loop_flags_together():
    cmd = shell.build_launch_command(
        **_base_launch_kwargs(
            slice_eval_concurrency=2,
            no_isolate_workers=True,
            slice_eval_drain_seconds=30.5,
            no_kill_check=True,
        )
    )
    assert "--slice-eval-concurrency 2" in cmd
    assert "--no-isolate-workers" in cmd
    assert "--slice-eval-drain-seconds 30.5" in cmd
    assert "--no-kill-check" in cmd
    assert "--auto" not in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_build_launch_command_rejects_unknown_subcommand():
    import pytest

    with pytest.raises(ValueError):
        shell.build_launch_command(
            python_bin="python3", cli_path="cli.py", mailbox_dir="/app/loop",
            max_iterations=4, config_path="/cfg.json", opencode_bin_dir="/bin",
            log_path="/log", exit_code_path="/exit", cwd="/app", subcommand="land",
        )


def test_build_poll_command_shape():
    cmd = shell.build_poll_command("/logs/agent/trio-exit-code")
    assert "/logs/agent/trio-exit-code" in cmd
    assert "RUNNING" in cmd


def test_build_workdir_baseline_script_sets_repo_local_identity():
    script = shell.build_workdir_baseline_script("/app")
    assert "git config user.name" in script
    assert "trio-bench" in script
    assert "git config --global" not in script
    assert "git init" in script
    assert "task: baseline" in script
    assert "find . -xdev -type f -size +20M" in script


def test_build_detached_init_script_uses_trio_ws():
    script = shell.build_detached_init_script("/trio-ws")
    assert "/trio-ws" in script
    assert "git init" in script


def test_build_goal_commit_script():
    script = shell.build_goal_commit_script("/app")
    assert "git add loop" in script
    assert "loop: goal" in script


def test_build_find_xdg_data_dirs_command():
    cmd = shell.build_find_xdg_data_dirs_command("/app/.git")
    assert "/app/.git/trio-opencode" in cmd
    assert "xdg/data" in cmd


def test_is_musl_output():
    assert shell.is_musl_output("musl libc (x86_64)\n", has_alpine_release=False)
    assert shell.is_musl_output("", has_alpine_release=True)
    assert not shell.is_musl_output("ldd (Debian GLIBC 2.36-9) 2.36\n", has_alpine_release=False)


def test_parse_git_version():
    assert shell.parse_git_version("git version 2.39.2") == (2, 39, 2)
    assert shell.parse_git_version("git version 2.30") == (2, 30, 0)
    assert shell.parse_git_version("not git at all") is None


def test_git_version_at_least():
    assert shell.git_version_at_least("git version 2.39.2") is True
    assert shell.git_version_at_least("git version 2.20.1") is False
    assert shell.git_version_at_least("huh") is None


def test_git_safe_directory_cmd_is_global_and_wildcard():
    assert "--global" in shell.GIT_SAFE_DIRECTORY_CMD
    assert "safe.directory" in shell.GIT_SAFE_DIRECTORY_CMD
