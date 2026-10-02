from goal import (
    HEAVY_DIR_EXCLUDE_PATTERNS,
    QUEUE_MD_TEMPLATE,
    mailbox_files,
    render_goal_md,
    render_queue_md,
)


def test_goal_md_includes_instruction_verbatim():
    instruction = "Line one.\nLine two with a trailing detail."
    rendered = render_goal_md(instruction, workdir="/app", mode="workdir")
    assert rendered.startswith(instruction)


def test_goal_md_has_environment_section_and_workdir():
    rendered = render_goal_md("Do the thing.", workdir="/workspace", mode="workdir")
    assert "## Environment" in rendered
    assert "`/workspace`" in rendered
    assert "no hidden tests" in rendered
    assert "must not search the internet" in rendered


def test_goal_md_detached_mode_mentions_trio_ws_and_workdir():
    rendered = render_goal_md("Fix the repo.", workdir="/repo", mode="detached")
    assert "/trio-ws" in rendered
    assert "`/repo`" in rendered


def test_goal_md_workdir_mode_does_not_mention_trio_ws():
    rendered = render_goal_md("Fix it.", workdir="/app", mode="workdir")
    assert "/trio-ws" not in rendered


def test_mailbox_files_is_goal_md_only():
    files = mailbox_files("Do X.", workdir="/app", mode="workdir")
    assert set(files.keys()) == {"GOAL.md"}
    assert files["GOAL.md"] == render_goal_md("Do X.", workdir="/app", mode="workdir")


def test_mailbox_files_open_loop_false_by_default_is_byte_identical():
    # Off-by-default: no open_loop kwarg at all, and open_loop=False
    # explicitly, both produce the exact same pre-existing GOAL.md-only
    # mailbox.
    implicit = mailbox_files("Do X.", workdir="/app", mode="workdir")
    explicit = mailbox_files("Do X.", workdir="/app", mode="workdir", open_loop=False)
    assert implicit == explicit
    assert set(explicit.keys()) == {"GOAL.md"}


def test_mailbox_files_open_loop_true_adds_queue_md():
    files = mailbox_files("Do X.", workdir="/app", mode="workdir", open_loop=True)
    assert set(files.keys()) == {"GOAL.md", "QUEUE.md"}
    assert files["GOAL.md"] == render_goal_md("Do X.", workdir="/app", mode="workdir")
    assert files["QUEUE.md"] == render_queue_md()


def test_render_queue_md_is_the_empty_queue_skeleton():
    rendered = render_queue_md()
    assert rendered == QUEUE_MD_TEMPLATE
    assert "retired:" in rendered
    assert "faults:" in rendered
    assert rendered.count("```yaml") == 2


def test_render_queue_md_parses_as_an_empty_queue_with_no_errors():
    # Exercise the real reader (metrics/trio-metrics.py's parse_queue_block,
    # what metrics/trio_loop.py / olqueue.py's TL._read_queue wraps) so this
    # stays honest about what the driver actually accepts, not just about
    # the literal template text.
    import importlib.util
    from pathlib import Path

    metrics_path = Path(__file__).resolve().parents[2] / "metrics" / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("_trio_metrics_for_test", metrics_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    result = mod.parse_queue_block(render_queue_md())
    assert result == {
        "retired": [],
        "faults": [],
        "errors": [],
        "malformed_slices": [],
    }


def test_goal_md_ends_with_single_trailing_newline():
    rendered = render_goal_md("x", workdir="/app", mode="workdir")
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


def test_heavy_dir_exclude_patterns_cover_common_heavy_dirs():
    assert "node_modules/" in HEAVY_DIR_EXCLUDE_PATTERNS
    assert ".venv/" in HEAVY_DIR_EXCLUDE_PATTERNS
    assert "__pycache__/" in HEAVY_DIR_EXCLUDE_PATTERNS
    assert "*.ckpt" in HEAVY_DIR_EXCLUDE_PATTERNS
