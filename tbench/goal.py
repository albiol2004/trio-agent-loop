"""Pure rendering helpers for the trio-opencode mailbox files written into a
Terminal-Bench task container before the driver loop starts.
"""
from __future__ import annotations


def render_goal_md(instruction: str, *, workdir: str, mode: str) -> str:
    """Render ``loop/GOAL.md``: the task instruction verbatim, followed by an
    ``## Environment`` section telling the agents this container itself is
    the product.

    ``mode`` is ``"workdir"`` (the mailbox's git repo IS the task's working
    directory) or ``"detached"`` (the working directory was already a git
    repo the task is ABOUT, so the mailbox lives separately at
    ``/trio-ws`` while the agents still act on ``workdir``).
    """
    instruction = instruction.rstrip("\n")
    lines = [
        instruction,
        "",
        "## Environment",
        "",
        "- You are working inside this task's container; work in this "
        "environment -- the environment is the product.",
        f"- The task working directory is `{workdir}`.",
        "- Changes anywhere in the container (files, installed packages, "
        "running services, configs) count toward the grade, not only "
        "changes under the working directory.",
        "- When you finish, an external grader inspects the final state of "
        "this container's declared outputs.",
        "- There are no hidden tests available to you, and you must not "
        "search the internet for this task's reference solution or "
        "grader.",
        "- Deliverables must exist at the exact paths the instruction "
        "names.",
    ]
    if mode == "detached":
        lines.append(
            f"- `{workdir}` is itself a pre-existing git repository that "
            "this task is about (its own history may matter and must not "
            "be rewritten casually) -- this loop's own mailbox lives "
            "separately at `/trio-ws` and is not itself a deliverable; "
            "only the container's declared outputs, including anything "
            f"under `{workdir}`, are graded."
        )
    return "\n".join(lines) + "\n"


#: Minimal valid ``QUEUE.md`` (MAILBOX-SCHEMA.md "v1 open-loop extension"):
#: both fenced ```yaml blocks present with their top-level key and no
#: entries ("A present block's list may be empty"). This is enough for
#: ``metrics/trio_loop.py``'s ``mode="auto"`` dispatch (gated purely on
#: ``(mailbox / "QUEUE.md").is_file()``) to select open-loop, and for
#: ``metrics/trio-metrics.py``'s ``read_queue``/``parse_queue_block`` (via
#: ``opencode-driver/trio_opencode/olqueue.py``'s ``TL._read_queue``) to
#: parse it with zero entries and zero errors.
QUEUE_MD_TEMPLATE = (
    "```yaml\n"
    "retired:\n"
    "```\n"
    "\n"
    "```yaml\n"
    "faults:\n"
    "```\n"
)


def render_queue_md() -> str:
    """Render the empty-queue skeleton written to ``loop/QUEUE.md`` when
    ``open_loop=True``. See :data:`QUEUE_MD_TEMPLATE`."""
    return QUEUE_MD_TEMPLATE


def mailbox_files(
    instruction: str, *, workdir: str, mode: str, open_loop: bool = False
) -> dict[str, str]:
    """Files to write into a fresh mailbox before ``trio-opencode start``.

    Only ``GOAL.md`` is required: ``op_begin``
    (``native/trio_native_step.py``) hard-requires it to exist and
    auto-creates ``STATE.md``/``LOG.md`` with sane v1-compatible defaults
    when they are missing (see ``metrics/trio_loop.py:_read_state`` and
    ``native/trio_native_step.py:op_begin``). ``PLAN.md``/``REPORT.md``/
    ``VERDICT.md`` are written by the Lead/Evaluator roles during the run,
    not seeded here.

    ``open_loop=True`` (default ``False``, byte-identical to before this
    kwarg existed) additionally seeds ``QUEUE.md`` with an empty-queue
    skeleton: the driver (``metrics/trio_loop.py``) and ``trio-opencode``'s
    own CLI select open-loop mode purely on ``QUEUE.md``'s presence as a
    file in the mailbox, never on a flag.
    """
    files = {"GOAL.md": render_goal_md(instruction, workdir=workdir, mode=mode)}
    if open_loop:
        files["QUEUE.md"] = render_queue_md()
    return files


#: Static exclude patterns always applied to a fresh "workdir" baseline
#: commit's ``.git/info/exclude``, on top of individually-discovered files
#: over the 20MB threshold (added separately -- see ``shell.py``). These are
#: directory/extension patterns too broad to enumerate file-by-file.
HEAVY_DIR_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    "*.ckpt",
    "*.safetensors",
    "*.whl",
    ".cache/",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.zip",
)
