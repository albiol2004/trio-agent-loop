"""Offline tests for `trioctl omnigent loop --observe-workers`.

The fake broker plays the Lead by executing the exact worker command it
received in its session prompt, as a subprocess with a minimal env, so
routing is proven through the prompt rather than inherited environment.
"""

from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from metrics import trio_loop
from test_omnigent_loop import (
    FakeBrokerClient,
    _install_role_registry,
    load_trioctl,
    make_mailbox,
)
from test_worker_events import load_events

SECRET = "PROMPT_SECRET_TOKEN_do_not_log"
LEAKED_KEY = "sk-live1234567890abcdefXYZ"
OBSERVE_HEADING = "## Worker observability"

CLAUDE_PROFILE = """\
version = 1
[roles.lead]
provider = "claude"
model = "lead-model"
effort = "medium"
[roles.evaluator]
provider = "claude"
model = "evaluator-model"
effort = "medium"
[roles.builder]
provider = "claude"
model = "builder-model"
effort = "medium"
[roles.scout]
provider = "claude"
model = "scout-model"
effort = "medium"
"""

WORKER_PROFILE = """\
version = 1
[roles.lead]
provider = "cursor"
model_family = "grok-4.6"
fallback_model = "cursor-grok-4.6-medium"
effort = "medium"
[roles.evaluator]
provider = "cursor"
model_family = "grok-4.6"
fallback_model = "cursor-grok-4.6-medium"
effort = "medium"
[roles.builder]
provider = "cursor"
model_family = "glm-5.2"
fallback_model = "glm-5.2-max"
effort = "max"
[roles.scout]
provider = "cursor"
model_family = "glm-5.2"
fallback_model = "glm-5.2-max"
effort = "max"
"""

FAKE_CURSOR = """\
#!/usr/bin/env python3
import sys, time
if sys.argv[1:2] == ["models"]:
    print("glm-5.2-max - GLM 5.2 Max")
    sys.exit(0)
sys.stdin.read()
time.sleep(0.4)
print("OK")
"""


def _worker_env(tmp_path: Path) -> dict[str, str]:
    """Minimal env for a Lead shell: no TRIO_* vars from the loop."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    agent = fake_bin / "cursor-agent"
    agent.write_text(FAKE_CURSOR, encoding="utf-8")
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    config_home = tmp_path / "worker-xdg"
    (config_home / "trio-agent-loop").mkdir(parents=True, exist_ok=True)
    (config_home / "trio-agent-loop" / "omnigent.toml").write_text(
        WORKER_PROFILE, encoding="utf-8"
    )
    return {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config_home),
        "LANG": "C.UTF-8",
    }


def _observed_command(prompt: str) -> str:
    assert OBSERVE_HEADING in prompt
    block = prompt.split(OBSERVE_HEADING, 1)[1]
    body = block.split("```\n", 1)[1].split("\n```", 1)[0]
    return body.replace("\\\n", " ")


class LeadDispatchingBroker(FakeBrokerClient):
    """On the Lead's first full read, run workers exactly as prompted."""

    def __init__(self, mailbox: Path, tmp_path: Path, **kwargs) -> None:
        super().__init__(mailbox, **kwargs)
        self.tmp_path = tmp_path
        self.worker_results: list[subprocess.CompletedProcess] = []
        self.commands: list[list[str]] = []

    def _dispatch(self, prompt: str) -> None:
        template = _observed_command(prompt)
        task = self.tmp_path / "task.md"
        task.write_text(f"do the slice {SECRET}\n", encoding="utf-8")
        env = _worker_env(self.tmp_path)

        def one(slice_id: str) -> None:
            argv = shlex.split(
                template.replace("<builder|scout|docs>", "builder")
                .replace("<slice-id>", slice_id)
                .replace("<task-file>", str(task))
            )
            self.commands.append(argv)
            self.worker_results.append(
                subprocess.run(
                    argv,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            )

        threads = [
            threading.Thread(target=one, args=(s,)) for s in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        (self.mailbox / "REPORT.md").write_text(
            "# Report\n\n## Wave decisions\n"
            "agent-reported: ready a, b, c; wave 1 = [a, b]; "
            f"c serial because it reads a's output. api_key={LEAKED_KEY}\n"
            "\n## Checks\nall green\n",
            encoding="utf-8",
        )

    def get_items(self, session_id, *, limit=100, order="asc"):
        is_full_read = not (order == "desc" and limit in (1, 10))
        if (
            is_full_read
            and self.sessions.get(session_id) == "lead"
            and not self.read_counts.get(session_id)
        ):
            self._dispatch(self.prompts[0])
        return super().get_items(session_id, limit=limit, order=order)


def _observed_loop(tmp_path, monkeypatch, broker_factory, argv_extra=()):
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    _install_role_registry(tmp_path, monkeypatch)
    config = tmp_path / "loop-profile.toml"
    config.write_text(CLAUDE_PROFILE, encoding="utf-8")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(config))
    for name in (
        "TRIO_WORKER_EVENTS_FILE",
        "TRIO_WORKER_RUN_ID",
        "TRIO_WORKER_ITERATION",
        "TRIO_WORKER_SLICE",
        "TRIO_WORKER_EVENTS_MAILBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    broker = broker_factory(mailbox)
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url: broker)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: trio_loop)
    captured: dict = {}
    real_runner = trioctl.OmnigentRunner

    def capturing_runner(**kwargs):
        runner = real_runner(**kwargs, interval=0)
        captured["runner"] = runner
        captured["kwargs"] = kwargs
        return runner

    monkeypatch.setattr(trioctl, "OmnigentRunner", capturing_runner)
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "loop",
            "--mailbox",
            str(mailbox),
            "--max-iterations",
            "1",
            "--keep-sessions",
            *argv_extra,
        ]
    )
    code = args.func(args)
    return trioctl, mailbox, broker, captured, code


def _all_observe_text(mailbox: Path) -> str:
    text = ""
    for path in (mailbox / ".observe").rglob("*"):
        if path.is_file():
            text += path.read_text(encoding="utf-8", errors="replace")
    return text


def test_observe_routes_real_lead_prompt_to_exact_cli_and_summarizes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    trioctl, mailbox, broker, captured, code = _observed_loop(
        tmp_path,
        monkeypatch,
        lambda mb: LeadDispatchingBroker(mb, tmp_path),
        ["--observe-workers"],
    )
    assert code == 0
    observe = captured["kwargs"]["observe"]
    run_id = observe["run_id"]
    lead_prompt, eval_prompt = broker.prompts
    # Exact interpreter + this trioctl + explicit correlation fields.
    trioctl_path = str(Path(trioctl.__file__).resolve())
    assert f"{shlex.quote(sys.executable)} {trioctl_path} omnigent run" in (
        lead_prompt
    )
    assert f"--worker-run-id {run_id}" in lead_prompt
    assert "--worker-iteration 1" in lead_prompt
    assert f"--mailbox {mailbox.resolve()}" in lead_prompt
    assert str(observe["workers"]) in lead_prompt
    assert "## Wave decisions" in lead_prompt
    assert OBSERVE_HEADING not in eval_prompt
    for argv in broker.commands:
        assert argv[1] == trioctl_path
    for result in broker.worker_results:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"

    summary_path = observe["summary"]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == run_id
    item = summary["iterations"]["1"]
    assert item["builder_count"] == 2
    assert {b["slice"] for b in item["builders"]} == {"a", "b"}
    assert all(b["outcome"] == "ok" for b in item["builders"])
    assert len(item["overlaps"]) == 1
    assert item["overlaps"][0]["overlap_ns"] > 0
    assert item["builder_union_ns"] < item["builder_sum_ns"]
    roles = {p["role"]: p for p in item["phases"]}
    assert roles["lead"]["outcome"] == "ok"
    assert roles["evaluator"]["outcome"] == "ok"
    assert roles["lead"]["duration_ns"] > 0
    assert roles["evaluator"]["duration_ns"] > 0
    wave = roles["lead"]["wave_report"]
    assert "c serial because it reads a's output" in wave
    assert roles["lead"]["wave_report_source"] == "agent_reported"
    assert "Checks" not in wave
    assert item["unknowns"] == []
    assert "not measured causality" in summary["caution"]

    dumped = _all_observe_text(mailbox)
    assert SECRET not in dumped
    assert LEAKED_KEY not in dumped
    err = capsys.readouterr().err
    assert f"observe run {run_id}" in err
    assert "overlap pairs 1" in err
    assert "status: shipped" in (mailbox / "STATE.md").read_text()


def test_default_off_keeps_prompt_and_mailbox_unchanged(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    trioctl, mailbox, broker, captured, code = _observed_loop(
        tmp_path, monkeypatch, FakeBrokerClient
    )
    assert code == 0
    assert "observe" not in captured["kwargs"]
    assert not (mailbox / ".observe").exists()
    assert all(OBSERVE_HEADING not in p for p in broker.prompts)
    assert all("Wave decisions" not in p for p in broker.prompts)
    assert "observe" not in capsys.readouterr().err
    args = trioctl.parser().parse_args(["omnigent", "loop"])
    assert args.observe_workers is False


def test_prompt_correlation_is_per_iteration_and_role(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: ITERATE scope=local:a.py\n", encoding="utf-8"
    )
    observe = trioctl._start_observation(mailbox)
    runner = trioctl.OmnigentRunner(repo=tmp_path, observe=observe)
    plain = trioctl.OmnigentRunner(repo=tmp_path)
    one = runner._prompt("lead", 1, mailbox)
    two = runner._prompt("lead", 2, mailbox)
    repair = runner._prompt("repair", 3, mailbox)
    evaluator = runner._prompt("evaluator", 2, mailbox)
    open_loop = runner._prompt(
        "lead", 4, mailbox, {"mode": "open-loop", "kind": "lead-pass"}
    )
    assert "--worker-iteration 1 " in one and "--worker-iteration 2" not in one
    assert "--worker-iteration 2 " in two
    assert "--worker-iteration 3 " in repair
    assert "--worker-iteration 4 " in open_loop
    assert open_loop.startswith("OPEN-LOOP CONTEXT: kind=lead-pass")
    for prompt in (one, two, repair, open_loop):
        assert f"--worker-run-id {observe['run_id']} " in prompt
    assert OBSERVE_HEADING not in evaluator
    # Default-off prompt is byte-identical to the pre-feature rendering.
    assert plain._prompt("lead", 1, mailbox) + trioctl.OmnigentRunner(
        repo=tmp_path, observe=observe
    )._observe_block(1, mailbox) == one


def test_resume_mints_fresh_run_and_setup_failure_does_not_block(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    first = trioctl._start_observation(mailbox)
    second = trioctl._start_observation(mailbox)
    assert first["run_id"] != second["run_id"]
    assert first["dir"].parent == second["dir"].parent == mailbox / ".observe"

    (tmp_path / "other").mkdir()
    blocked = make_mailbox(tmp_path / "other")
    (blocked / ".observe").write_text("not a dir", encoding="utf-8")
    captured: dict = {}

    class CapturingRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class ThreeLoop:
        @staticmethod
        def run_loop(*args, **kwargs) -> int:
            return 3

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "OmnigentRunner", CapturingRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: ThreeLoop)
    args = trioctl.parser().parse_args(
        [
            "omnigent", "loop", "--mailbox", str(blocked),
            "--keep-sessions", "--observe-workers",
        ]
    )
    assert args.func(args) == 3
    assert "observe" not in captured
    assert "continuing without observation" in capsys.readouterr().err


def test_summary_and_phase_failures_never_change_loop_outcome(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    class InterruptLoop:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, **kwargs) -> int:
            # A Lead pass is started, then the operator interrupts.
            runner._observe_phase("start", str(uuid.uuid4()), "lead", 1)
            raise KeyboardInterrupt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: InterruptLoop)
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox), "--keep-sessions",
         "--observe-workers"]
    )
    assert args.func(args) == 130
    runs = list((mailbox / ".observe").iterdir())
    summary = json.loads((runs[0] / "summary.json").read_text())
    phase = summary["iterations"]["1"]["phases"][0]
    assert phase["outcome"] == "unknown" and phase["duration_ns"] is None
    assert any("no end record" in n for n in summary["iterations"]["1"]["unknowns"])

    # Offline recompute (e.g. after SIGKILL) matches the written summary.
    capsys.readouterr()
    recompute = trioctl.parser().parse_args(
        ["omnigent", "observe-summary", "--mailbox", str(mailbox),
         "--run-id", runs[0].name]
    )
    assert recompute.func(recompute) == 0
    assert json.loads(capsys.readouterr().out) == summary

    # Summary crash keeps the loop's own exit code.
    class ThreeLoop:
        @staticmethod
        def run_loop(*a, **k) -> int:
            return 3

    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: ThreeLoop)

    def boom(*a, **k):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(trioctl.worker_events, "summarize", boom)
    assert args.func(args) == 3
    assert "observe summary failed" in capsys.readouterr().err

    # Unwritable phases shard: the role pass still succeeds.
    observe = trioctl._start_observation(mailbox)
    Path(str(observe["phases"]) + ".d").write_text("x", encoding="utf-8")
    runner = trioctl.OmnigentRunner(repo=tmp_path, observe=observe)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: 0)
    assert runner.run("lead", 1, mailbox) == 0
    assert trioctl.worker_events.TELEMETRY_WARN in capsys.readouterr().err


def _worker(we, path, inv, kind, *, run_id, iteration, slice_id=None,
            mono=0, duration=None, role="builder"):
    rec = we.base_record(
        invocation_id=inv, kind=kind, role=role, run_id=run_id,
        iteration=iteration, slice_id=slice_id, monotonic_ns=mono,
        duration_ns=duration,
    )
    we.emit(path, rec)


def test_summary_reports_unknowns_honestly(tmp_path: Path) -> None:
    we = load_events()
    run_dir = tmp_path / "loop" / ".observe" / "obs-x"
    paths = we.observe_paths(run_dir)
    ids = [we.new_invocation_id() for _ in range(6)]
    # iter 1: one complete builder, one spawned-only (crash).
    _worker(we, paths["workers"], ids[0], "spawned", run_id="obs-x",
            iteration="1", slice_id="a", mono=100)
    _worker(we, paths["workers"], ids[0], "exited", run_id="obs-x",
            iteration="1", slice_id="a", mono=500, duration=400)
    _worker(we, paths["workers"], ids[1], "spawned", run_id="obs-x",
            iteration="1", mono=200)
    # Missing run id: excluded, counted.
    _worker(we, paths["workers"], ids[2], "spawned", run_id=None,
            iteration="1", slice_id="z", mono=150)
    # iter 2: lead phase, no builders, no wave report.
    for kind in ("start", "end"):
        we.emit(paths["phases"], we.phase_record(
            invocation_id=ids[3], kind=kind, role="lead", run_id="obs-x",
            iteration=2, outcome="ok" if kind == "end" else None))
    # Scout counted separately, never as a builder.
    _worker(we, paths["workers"], ids[4], "spawned", run_id="obs-x",
            iteration="2", role="scout", mono=1)
    summary = we.summarize(run_dir, "obs-x")
    one = summary["iterations"]["1"]
    assert one["builder_count"] == 2
    assert one["overlaps"] == []
    assert one["builder_sum_ns"] is None
    assert one["builder_union_ns"] is None
    notes = " ".join(one["unknowns"])
    assert "duration unknown" in notes and "no slice label" in notes
    two = summary["iterations"]["2"]
    assert two["builder_count"] == 0
    assert two["other_workers"] == {"scout": 1}
    notes = " ".join(two["unknowns"])
    assert "no builder events recorded" in notes
    assert "Wave decisions" in notes
    assert "different or missing run id" in summary["unknowns"][0]
    assert we.summarize(tmp_path / "missing", "obs-x")["iterations"] == {}


def test_wave_section_redacts_and_caps() -> None:
    we = load_events()
    text = (
        "## Wave decisions\nready: a\nBearer abcdefghijklmnopqrstu\n"
        "token: hunter2hunter2\n" + "x" * 9000 + "\n## Next\nignored\n"
    )
    section = we.wave_section(text)
    assert "abcdefghijklmnopqrstu" not in section
    assert "hunter2hunter2" not in section
    assert "ignored" not in section
    assert len(section) == we.WAVE_REPORT_MAX
    assert we.wave_section("# Report\nno section\n") is None
