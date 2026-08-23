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
