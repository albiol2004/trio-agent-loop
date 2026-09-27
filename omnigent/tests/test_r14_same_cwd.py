"""r14 S-1: same-cwd session creation is serialised until the chat binds.

Diag 2026-09-27 (iter5 cp-gold-only-obo): a Lead and a slice-eval launched
83 ms apart at the same root cwd each bound the OTHER's Cursor chat
(Omnigent's forwarder takes the first new chat under
`~/.cursor/chats/<md5(cwd)>/`). trioctl now holds a per-workspace file lock
from just before create until the broker reports `external_session_id`
(bounded by TRIO_SAME_CWD_BIND_WAIT_S), and classifies a first-prompt hold
whose only user row is a sibling's prompt as `mirror_crosswired`.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "trioctl"


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


trioctl = _load("trioctl_r14_same_cwd")


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setenv("TRIO_SAME_CWD_BIND_POLL_S", "0.01")


class BindLagClient:
    """Chat binds ``lag`` seconds after the session id exists."""

    def __init__(self, lag: float, *, callback: bool = True, bind: bool = True):
        self.lag = lag
        self.bind = bind
        self.callback = callback
        self.lock = threading.Lock()
        self.creates: list[tuple[str, float, str]] = []  # (sid, t, workspace)
        self.bind_at: dict[str, float] = {}
        self.count = 0

    def _new(self, workspace):
        with self.lock:
            self.count += 1
            sid = f"s{self.count}"
            now = time.monotonic()
            self.creates.append((sid, now, workspace))
            self.bind_at[sid] = now + self.lag
        return sid

    def create_session(self, agent_id, model, prompt, title, workspace=None,
                       on_session_id=None, **_kw):
        sid = self._new(workspace)
        if self.callback and on_session_id is not None:
            on_session_id(sid)
        return {"id": sid}

    def session_chat_binding(self, sid):
        if self.bind and time.monotonic() >= self.bind_at[sid]:
            return f"chat-{sid}"
        return None

    def wait_session(self, sid, timeout=None, interval=None):
        return {"status": "idle"}

    def get_items(self, sid):
        return []


class NoCallbackClient(BindLagClient):
    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        return {"id": self._new(workspace)}


def _runner(client, workspace: Path, monkeypatch, seen: list):
    runner = trioctl.OmnigentRunner(
        repo=workspace, broker_client=client, config={}, interval=0,
        workspace=str(workspace),
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda role, *a, **k: f"{role} prompt\n")
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        seen.append({"role": role, "meta": dict(dispatch["meta"]),
                     "session_id": dispatch["session_id"]})
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner


def _mailbox(tmp_path: Path) -> Path:
    mailbox = tmp_path / "loop"
    mailbox.mkdir(exist_ok=True)
    (mailbox / "LOG.md").write_text("# Log\n")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n")
    return mailbox


def _together(*calls):
    barrier = threading.Barrier(len(calls))
    errors: list[BaseException] = []

    def go(fn):
        barrier.wait()
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(fn,)) for fn in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return errors


@pytest.mark.parametrize("client_cls", [BindLagClient, NoCallbackClient])
def test_same_workspace_creates_are_serialised_until_bind(tmp_path, monkeypatch, client_cls):
    lag = 0.3
    client = client_cls(lag)
    root = tmp_path / "product"
    root.mkdir()
    mailbox = _mailbox(tmp_path)
    seen: list = []
    # Two runners (Lead thread + slice-eval pool in open-loop) at one root.
    lead = _runner(client, root, monkeypatch, seen)
    evaluator = _runner(client, root, monkeypatch, seen)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": "abc"}

    errors = _together(
        lambda: lead.run("lead", 1, mailbox),
        lambda: evaluator.run("evaluator", 1, mailbox, ctx),
    )
    assert errors == []
    (first, t1, _w1), (second, t2, _w2) = sorted(client.creates, key=lambda c: c[1])
    assert t2 >= client.bind_at[first], "second same-cwd create before the first chat bound"
    by_sid = {s["session_id"]: s for s in seen}
    assert "same_cwd_serialised" not in by_sid[first]["meta"]
    meta = by_sid[second]["meta"]
    assert meta["same_cwd_serialised"] is True
    assert meta["same_cwd_wait_s"] >= lag * 0.5
    waiter = lead if by_sid[second]["role"] == "lead" else evaluator
    (event,) = waiter.driver_meta["same_cwd_serialised"]
    assert event["same_cwd_serialised"] is True
    assert event["workspace"] == str(root.resolve())
    assert event["wait_s"] == meta["same_cwd_wait_s"]
    json.dumps(waiter.driver_meta)  # reaches `.driver.json` as plain JSON


def test_different_workspaces_do_not_wait(tmp_path, monkeypatch):
    lag = 0.5
    client = BindLagClient(lag)
    mailbox = _mailbox(tmp_path)
    seen: list = []
    runners = []
    for name in ("root", "eval-wt"):
        ws = tmp_path / name
        ws.mkdir()
        runners.append(_runner(client, ws, monkeypatch, seen))
    errors = _together(
        lambda: runners[0].run("lead", 1, mailbox),
        lambda: runners[1].run("evaluator", 1, mailbox),
    )
    assert errors == []
    (first, t1, w1), (_second, t2, w2) = sorted(client.creates, key=lambda c: c[1])
    assert w1 != w2
    assert t2 < client.bind_at[first], "different workspaces must overlap"
    assert all("same_cwd_serialised" not in s["meta"] for s in seen)
    assert all("same_cwd_serialised" not in r.driver_meta for r in runners)


def test_bind_timeout_releases_the_lock_logs_and_proceeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIO_SAME_CWD_BIND_WAIT_S", "0.2")
    client = BindLagClient(0.0, bind=False)
    root = tmp_path / "product"
    root.mkdir()
    mailbox = _mailbox(tmp_path)
    seen: list = []
    runner = _runner(client, root, monkeypatch, seen)
    assert runner.run("lead", 1, mailbox) == 0
    assert seen and seen[0]["session_id"] == "s1"
    line = f"trioctl: same-cwd bind wait timed out for s1 ({root.resolve()}); continuing"
    deadline = time.monotonic() + 5
    err = ""
    while time.monotonic() < deadline:
        err += capsys.readouterr().err
        if line in err:
            break
        time.sleep(0.02)
    assert line in err
    assert err.count("same-cwd bind wait timed out") == 1
    # Released: the next same-cwd create gets the lock at once.
    gate = trioctl._SameCwdGate(client, str(root))
    gate.acquire()
    assert not gate.contended
    gate.release()
    assert runner.run("evaluator", 1, mailbox) == 0
    assert "same_cwd_serialised" not in seen[-1]["meta"]


def test_failed_create_releases_immediately(tmp_path, monkeypatch):
    class Refusing(BindLagClient):
        def create_session(self, *a, on_session_id=None, **k):
            on_session_id("dead")
            raise trioctl.broker_http.PromptDeliveryFailed("refused", status_code=400)

    client = Refusing(10.0)
    root = tmp_path / "product"
    root.mkdir()
    runner = _runner(client, root, monkeypatch, [])
    with pytest.raises(trioctl.broker_http.PromptDeliveryFailed):
        runner.run("lead", 1, _mailbox(tmp_path))
    gate = trioctl._SameCwdGate(client, str(root))
    gate.acquire()
    assert not gate.contended
    gate.release()


class CrosswireClient:
    """Lead and slice-eval both uncertain: each session mirrors the other's prompt."""

    def __init__(self):
        self.prompts: dict[str, str] = {}
        self.sids: dict[str, str] = {}
        self.both = threading.Barrier(2)
        self.foreign: str | None = None

    def create_session(self, agent_id, model, prompt, title, workspace=None,
                       on_session_id=None, **_kw):
        sid = "lead-s" if prompt.startswith("lead") else "eval-s"
        self.prompts[sid] = prompt
        on_session_id(sid)
        self.both.wait(5)  # both in flight, both ids known
        raise trioctl.broker_http.PromptDeliveryUncertain(
            f"first prompt on {sid}: 1 saved user row(s), none equal", sid)

    def get_items(self, sid):
        if self.foreign is not None:
            text = self.foreign
        else:
            other = "eval-s" if sid == "lead-s" else "lead-s"
            text = self.prompts[other]
        return {"data": [{"role": "user", "content": [{"type": "input_text", "text": text}]}]}

    def session_chat_binding(self, sid):
        return None  # never binds: the bounded wait (0 s here) releases


def _held(mailbox: Path, sid: str) -> dict:
    return json.loads((mailbox / ".sessions" / f"held-{sid}.json").read_text())


def _crosswire_runners(tmp_path, monkeypatch, client):
    root = tmp_path / "product"
    root.mkdir()
    runners = []
    for _ in range(2):
        runner = trioctl.OmnigentRunner(
            repo=root, broker_client=client, config={}, interval=0, workspace=str(root),
        )
        monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
        monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
        monkeypatch.setattr(runner, "_prompt", lambda role, *a, **k: f"{role} prompt\n")
        monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)
        runners.append(runner)
    return runners


def test_sibling_prompt_mirror_is_held_as_mirror_crosswired(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIO_SAME_CWD_BIND_WAIT_S", "0")
    client = CrosswireClient()
    mailbox = _mailbox(tmp_path)
    lead, evaluator = _crosswire_runners(tmp_path, monkeypatch, client)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": "abc"}
    errors = _together(
        lambda: lead.run("lead", 1, mailbox),
        lambda: evaluator.run("evaluator", 1, mailbox, ctx),
    )
    assert len(errors) == 2 and all(isinstance(e, trioctl.TrioctlError) for e in errors)
    ev = _held(mailbox, "eval-s")
    assert ev["hold"] == "mirror_crosswired" and ev["reason"] == "mirror_crosswired"
    assert ev["crosswired_with"] == "lead-s" and ev["crosswired_role"] == "lead"
    assert "none equal" in ev["detail"]
    ld = _held(mailbox, "lead-s")
    assert ld["reason"] == "mirror_crosswired" and ld["crosswired_with"] == "eval-s"
    assert ld["crosswired_kind"] == "slice-eval" and ld["crosswired_slice"] == "A"
    # Still held: the mailbox refuses any further role.
    with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
        lead.run("lead", 2, mailbox)


def test_foreign_row_stays_a_generic_uncertain_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIO_SAME_CWD_BIND_WAIT_S", "0")
    client = CrosswireClient()
    client.foreign = "some follow-up hook text"
    mailbox = _mailbox(tmp_path)
    lead, evaluator = _crosswire_runners(tmp_path, monkeypatch, client)
    errors = _together(
        lambda: lead.run("lead", 1, mailbox),
        lambda: evaluator.run("evaluator", 1, mailbox),
    )
    assert len(errors) == 2
    for sid in ("lead-s", "eval-s"):
        record = _held(mailbox, sid)
        assert record["hold"] == "first_prompt_uncertain"
        assert "crosswired_with" not in record


def test_broker_client_reports_id_before_launch_and_reads_binding(monkeypatch):
    bh = trioctl.broker_http
    client = bh.BrokerClient()
    calls: list = []

    def request(method, path, payload=None, expected_status=200):
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/sessions"):
            return {"id": "s9"}
        if method == "GET" and path == "/v1/sessions/s9":
            return {"id": "s9", "external_session_id": "chat-1"}
        if method == "GET" and "/items" in path:
            return {"data": [{"role": "user", "content": "hi"}]}
        return {"runner_id": "r"}

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_resolve_host_id", lambda host: "h")
    seen: list = []
    client.create_session("a", "m", "hi", on_session_id=lambda sid: seen.append((sid, len(calls))))
    assert seen == [("s9", 1)]  # right after POST /v1/sessions, before launch/prompt
    assert client.session_chat_binding("s9") == "chat-1"
    monkeypatch.setattr(client, "get_session", lambda sid: {"external_session_id": None})
    assert client.session_chat_binding("s9") is None
