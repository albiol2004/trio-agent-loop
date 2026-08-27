"""Offline tests for the Omnigent role runner and headless loop command."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from metrics import trio_loop


SCRIPT = Path(__file__).parents[1] / "trioctl"


def load_trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def profile() -> dict:
    roles = {}
    for role in ("lead", "evaluator", "builder", "scout"):
        roles[role] = {
            "provider": "claude",
            "model": f"{role}-model",
            "effort": "medium",
        }
    return {"version": 1, "roles": roles}


PLAN = """\
```yaml
slices:
  - id: coordination
    writes: [loop/STATE.md]
    reads: []
```
"""


def make_mailbox(parent: Path) -> Path:
    mailbox = parent / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    return mailbox


class FakeBrokerClient:
    """Broker double exposing create, lifecycle polling, and read operations."""

    def __init__(self, mailbox: Path) -> None:
        self.mailbox = mailbox
        self.sessions: dict[str, str] = {}
        self.status_indexes: dict[str, int] = {}
        self.item_polls: dict[str, int] = {}
        self.statuses_seen: list[str] = []
        self.prompts: list[str] = []
        self.titles: list[str] = []
        self.calls: list[str] = []

    def create(
        self,
        agent_id: str,
        model: str,
        message: str,
        title: str,
    ) -> dict[str, str]:
        session_id = f"session-{len(self.sessions) + 1}"
        role = "lead" if agent_id == "lead-agent" else "evaluator"
        self.sessions[session_id] = role
        self.status_indexes[session_id] = 0
        self.item_polls[session_id] = 0
        self.prompts.append(message)
        self.titles.append(title)
        self.calls.append(f"create:{role}:{model}")
        return {"id": session_id}

    def get_session(self, session_id: str) -> dict[str, str]:
        """Expose the pre-turn idle, active, and post-turn idle states."""
        statuses = ("idle", "running", "idle")
        index = min(self.status_indexes[session_id], len(statuses) - 1)
        self.status_indexes[session_id] += 1
        status = statuses[index]
        self.statuses_seen.append(status)
        self.calls.append(f"status:{session_id}:{status}")
        return {"id": session_id, "status": status}

    def get_items(
        self,
        session_id: str,
        *,
        limit: int = 100,
        order: str = "asc",
    ) -> dict[str, list[dict[str, str]]]:
        """Return an assistant item only after the simulated turn settles."""
        if order == "desc" and limit in (1, 10):
            poll = self.item_polls[session_id]
            self.item_polls[session_id] += 1
            self.calls.append(f"items:{session_id}:{poll}")
            item = {"id": "user-item", "role": "user", "type": "message"}
            if poll >= 2:
                item = {
                    "id": "assistant-item",
                    "role": "assistant",
                    "type": "message",
                    "status": "completed",
                }
            return {"data": [item]}

        role = self.sessions[session_id]
        self.calls.append(f"read:{session_id}")
        if role == "lead":
            with (self.mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write("- iter 1 | lead | completed\n")
        else:
            (self.mailbox / "VERDICT.md").write_text(
                "VERDICT: SHIP\n",
                encoding="utf-8",
            )
        return {"items": [{"role": "assistant", "content": role}]}


def test_one_headless_iteration_ships_without_cursor_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    home = tmp_path / "home"
    registry = home / ".omnigent" / "agents" / "trio-omnigent-roles"
    registry.mkdir(parents=True)
    (registry / "registry.json").write_text(
        json.dumps(
            {
                "trio-omnigent-lead": {"agent_id": "lead-agent"},
                "trio-omnigent-evaluator": {"agent_id": "evaluator-agent"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("OMNIGENT_HOME", raising=False)
    cursor_calls: list[str] = []
    real_which = shutil.which

    def no_cursor_agent(command: str):
        if command == "cursor-agent":
            cursor_calls.append(command)
            raise AssertionError("headless Omnigent loop used cursor-agent")
        return real_which(command)

    monkeypatch.setattr(trioctl.shutil, "which", no_cursor_agent)
    broker = FakeBrokerClient(mailbox)
    runner = trioctl.OmnigentRunner(
        repo=tmp_path,
        broker_client=broker,
        config=profile(),
        interval=0,
    )

    result = trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path)

    assert result == 0
    assert runner.session_ids == {
        "lead": "session-1",
        "evaluator": "session-2",
    }
    assert len(broker.prompts) == 2
    for prompt in broker.prompts:
        assert str(mailbox.resolve()) in prompt
        assert "iteration 1" in prompt
        assert str(tmp_path.resolve()) in prompt
    assert all("mailbox" in title and "1" in title for title in broker.titles)
    assert broker.statuses_seen == ["idle", "running", "idle"] * 2
    assert cursor_calls == []
    assert "status: shipped" in (mailbox / "STATE.md").read_text()


def test_repair_prompt_adds_verdict_scope_and_repair_log_format(
    tmp_path: Path,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: ITERATE scope=local:src/app.py\n",
        encoding="utf-8",
    )
    runner = trioctl.OmnigentRunner(repo=Path(__file__).parents[2])

    prompt = runner._prompt("repair", 2, mailbox)

    assert "You are Trio Lead" in prompt
    assert "scope=local:src/app.py" in prompt
    assert "- iter 2 | repair |" in prompt


def test_prompt_no_context_and_lockstep_context_are_byte_identical(
    tmp_path: Path,
) -> None:
    """api:OpenLoopPromptEnv parity: None and mode!='open-loop' must leave
    the prompt untouched, matching portable/driver.sh's build_prompt."""
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    runner = trioctl.OmnigentRunner(repo=Path(__file__).parents[2])

    no_context = runner._prompt("lead", 3, mailbox)
    explicit_none = runner._prompt("lead", 3, mailbox, None)
    lockstep = runner._prompt(
        "lead", 3, mailbox, {"mode": "lockstep", "kind": "lead-pass"}
    )

    assert explicit_none == no_context
    assert lockstep == no_context


def test_prompt_open_loop_lead_pass_includes_retired_entry_procedure(
    tmp_path: Path,
) -> None:
    """Regression: an Omnigent Lead committed 3 slice(<id>): commits but
    never appended a retired: entry to QUEUE.md, because the role never
    received the canonical Open-loop procedure. The rendered block must
    say so explicitly."""
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    runner = trioctl.OmnigentRunner(repo=Path(__file__).parents[2])
    context = {
        "mode": "open-loop",
        "slice": None,
        "sha": None,
        "kind": "lead-pass",
    }
    base_prompt = runner._prompt("lead", 3, mailbox)

    prompt = runner._prompt("lead", 3, mailbox, context)

    assert prompt.startswith("OPEN-LOOP CONTEXT: kind=lead-pass\n")
    assert "commits without appending a `retired:` entry is incomplete" in prompt
    assert "Backpressure" in prompt
    assert "concurrently" in prompt
    assert prompt.endswith(base_prompt)
    assert prompt[: -len(base_prompt)].endswith("\n\n")


def test_prompt_open_loop_slice_eval_includes_slice_and_sha(
    tmp_path: Path,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    runner = trioctl.OmnigentRunner(repo=Path(__file__).parents[2])
    context = {
        "mode": "open-loop",
        "slice": "coordination",
        "sha": "abc1234",
        "kind": "slice-eval",
    }
    base_prompt = runner._prompt("evaluator", 5, mailbox)

    prompt = runner._prompt("evaluator", 5, mailbox, context)

    assert prompt.startswith(
        "OPEN-LOOP CONTEXT: kind=slice-eval slice=coordination sha=abc1234\n"
    )
    assert "## slice coordination @abc1234 — SHIP" in prompt
    assert "## slice coordination @abc1234 — ITERATE" in prompt
    assert "never edit `retired:` entries" in prompt.lower()
    assert prompt.endswith(base_prompt)
    assert prompt[: -len(base_prompt)].endswith("\n\n")


def test_prompt_open_loop_integration_eval_prepends_context_block(
    tmp_path: Path,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    runner = trioctl.OmnigentRunner(repo=Path(__file__).parents[2])
    context = {
        "mode": "open-loop",
        "slice": None,
        "sha": None,
        "kind": "integration-eval",
    }
    base_prompt = runner._prompt("evaluator", 5, mailbox)

    prompt = runner._prompt("evaluator", 5, mailbox, context)

    assert prompt.startswith("OPEN-LOOP CONTEXT: kind=integration-eval\n")
    assert "VERDICT: SHIP" in prompt
    assert "VERDICT: BLOCKED" in prompt
    assert prompt.endswith(base_prompt)
    assert prompt[: -len(base_prompt)].endswith("\n\n")


def test_title_unchanged_for_lockstep_and_no_context(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    no_context = trioctl.OmnigentRunner._title(mailbox, 4, "lead", None)
    lockstep = trioctl.OmnigentRunner._title(
        mailbox, 4, "lead", {"mode": "lockstep", "kind": "lead-pass"}
    )

    expected = f"trioctl {mailbox.name} iteration 4 lead"
    assert no_context == expected
    assert lockstep == expected


def test_title_includes_kind_for_lead_pass_and_integration_eval(
    tmp_path: Path,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    lead_pass = trioctl.OmnigentRunner._title(
        mailbox,
        4,
        "lead",
        {"mode": "open-loop", "slice": None, "sha": None, "kind": "lead-pass"},
    )
    integration = trioctl.OmnigentRunner._title(
        mailbox,
        4,
        "evaluator",
        {
            "mode": "open-loop",
            "slice": None,
            "sha": None,
            "kind": "integration-eval",
        },
    )

    assert lead_pass == f"trioctl {mailbox.name} iteration 4 lead lead-pass"
    assert integration == (
        f"trioctl {mailbox.name} iteration 4 evaluator integration-eval"
    )


def test_title_includes_slice_id_for_slice_eval(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    title = trioctl.OmnigentRunner._title(
        mailbox,
        4,
        "evaluator",
        {
            "mode": "open-loop",
            "slice": "coordination",
            "sha": "abc1234",
            "kind": "slice-eval",
        },
    )

    assert title == (
        f"trioctl {mailbox.name} iteration 4 evaluator slice-eval:coordination"
    )


def test_run_retries_once_on_restart_blip_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: right after an Omnigent restart, a session went
    idle+bound+running for a few seconds with zero items, then the broker
    showed the runner unbound again (runner_id: None) — three Lead passes
    were consumed this way. run() must retry once, silently, rather than
    counting the blip as a completed pass."""
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    sleep_calls: list[float] = []
    monkeypatch.setattr(trioctl.time, "sleep", lambda s: sleep_calls.append(s))

    class RestartBlipThenGoodClient:
        def __init__(self) -> None:
            self.creates = 0

        def create(self, agent_id, model, message, title):
            self.creates += 1
            return {"id": f"session-{self.creates}"}

        def wait(self, session_id, timeout=None, interval=None):
            if self.creates == 1:
                return {"id": session_id, "status": "idle", "runner_id": None}
            return {"id": session_id, "status": "idle", "runner_id": "runner-1"}

        def get_items(self, session_id):
            if self.creates == 1:
                return {"items": []}
            with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write("- iter 1 | lead | completed\n")
            return {"items": [{"role": "assistant", "content": "lead"}]}

    client = RestartBlipThenGoodClient()
    runner = trioctl.OmnigentRunner(repo=tmp_path, broker_client=client)
    monkeypatch.setattr(runner, "_agent_id", lambda role: "lead-agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "lead-model")

    result = runner.run("lead", 1, mailbox)

    assert result == 0
    assert client.creates == 2
    assert sleep_calls == [10.0]


def test_run_fails_after_second_consecutive_restart_blip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blip on the retry too is a real failure, not another retry."""
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    monkeypatch.setattr(trioctl.time, "sleep", lambda s: None)

    class AlwaysBlipClient:
        def __init__(self) -> None:
            self.creates = 0

        def create(self, agent_id, model, message, title):
            self.creates += 1
            return {"id": f"session-{self.creates}"}

        def wait(self, session_id, timeout=None, interval=None):
            return {"id": session_id, "status": "idle", "runner_id": None}

        def get_items(self, session_id):
            return {"items": []}

    client = AlwaysBlipClient()
    runner = trioctl.OmnigentRunner(repo=tmp_path, broker_client=client)
    monkeypatch.setattr(runner, "_agent_id", lambda role: "lead-agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "lead-model")

    result = runner.run("lead", 1, mailbox)

    assert result == 1
    assert client.creates == 2  # exactly one retry, no retry loop


def test_create_forwards_runner_id_when_client_supports_it(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    calls: list[str | None] = []

    class RunnerAwareClient:
        def create_session(
            self,
            agent_id: str,
            model: str,
            message: str,
            title: str,
            runner_id: str | None = None,
        ) -> dict[str, str]:
            calls.append(runner_id)
            return {"id": "session-1"}

    created = trioctl.OmnigentRunner._create(
        RunnerAwareClient(),
        "agent-1",
        "model-1",
        "prompt",
        "title",
        runner_id="runner-7",
    )

    assert created == {"id": "session-1"}
    assert calls == ["runner-7"]


def test_create_omits_runner_id_for_legacy_client(tmp_path: Path) -> None:
    """The fakes in this module and test_trioctl.py take the legacy
    4-argument `create`/`create_session` shape and must keep working."""
    trioctl = load_trioctl()
    calls: list[tuple[str, str, str, str]] = []

    class LegacyClient:
        def create(
            self, agent_id: str, model: str, message: str, title: str
        ) -> dict[str, str]:
            calls.append((agent_id, model, message, title))
            return {"id": "session-1"}

    created = trioctl.OmnigentRunner._create(
        LegacyClient(),
        "agent-1",
        "model-1",
        "prompt",
        "title",
        runner_id="runner-7",
    )

    assert created == {"id": "session-1"}
    assert calls == [("agent-1", "model-1", "prompt", "title")]


def test_loop_runner_id_flag_is_parsed_and_threaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trioctl = load_trioctl()
    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeLoop:
        @staticmethod
        def run_loop(*args: object, **kwargs: object) -> int:
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "OmnigentRunner", CapturingRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: FakeLoop)
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "loop",
            "--mailbox",
            "mailbox",
            "--max-iterations",
            "1",
            "--runner-id",
            "runner-9",
        ]
    )

    assert args.func(args) == 0
    assert captured["runner_id"] == "runner-9"


def test_loop_wait_timeout_is_parsed_and_threaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trioctl = load_trioctl()
    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeLoop:
        @staticmethod
        def run_loop(*args: object, **kwargs: object) -> int:
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "OmnigentRunner", CapturingRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: FakeLoop)
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "loop",
            "--mailbox",
            "mailbox",
            "--max-iterations",
            "1",
            "--wait-timeout",
            "17",
        ]
    )

    assert args.func(args) == 0
    assert captured["timeout"] == 17.0


def test_loop_prune_sessions_flag_runs_after_exit_including_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--prune-sessions` must clean up after the loop returns regardless
    of its exit code, and must never run when the flag is absent."""
    trioctl = load_trioctl()
    prune_calls: list[tuple[Path, object]] = []

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

    class FailingLoop:
        @staticmethod
        def run_loop(*args: object, **kwargs: object) -> int:
            return 5  # e.g. NEEDS_HUMAN/locked

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "OmnigentRunner", CapturingRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: FailingLoop)
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url: prune_calls.append((mailbox, base_url)),
    )
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "loop",
            "--mailbox",
            "mailbox",
            "--max-iterations",
            "1",
            "--prune-sessions",
        ]
    )

    result = args.func(args)

    assert result == 5  # the loop's own exit code is preserved
    assert prune_calls == [((tmp_path / "mailbox").resolve(), args.base_url)]


def test_loop_without_prune_sessions_flag_never_prunes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trioctl = load_trioctl()
    prune_calls: list[object] = []

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

    class FakeLoop:
        @staticmethod
        def run_loop(*args: object, **kwargs: object) -> int:
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "OmnigentRunner", CapturingRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: FakeLoop)
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda *a, **k: prune_calls.append((a, k)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", "mailbox", "--max-iterations", "1"]
    )

    assert args.func(args) == 0
    assert prune_calls == []
