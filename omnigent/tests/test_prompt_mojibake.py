"""Offline regression: a mirrored user row with bounded mojibake lands.

Live run 20260926T114109Z, session 3f5a37b6: the Evaluator prompt was
delivered and ran to completion, but its mirrored user row equalled the
posted prompt except that one EM DASH (U+2014, char ~6109) came back as
two U+FFFD. The exact receipt check raised PromptDeliveryUncertain.

``_row_matches_prompt`` tolerates exactly that: each maximal run of 1-3
U+FFFD in the row stands for one non-ASCII prompt character at that
position. Truncation, a DEL prefix (live2 17c9642c), or a U+FFFD for an
ASCII character still do not match.

``fixtures/live_3f5a37b6_held_user_row.txt`` is the held row, with the
run directory prefix rewritten to ``/run/20260926T114109Z``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


HERE = Path(__file__).parent
SCRIPT = HERE.parent / "trioctl"
HELD_ROW = HERE / "fixtures" / "live_3f5a37b6_held_user_row.txt"
REPO = HERE.parent.parent
FFFD = "�"


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


trioctl = sys.modules.get("trioctl") or _load(SCRIPT, "trioctl")
broker_http = trioctl.broker_http
match = broker_http._row_matches_prompt
norm = broker_http._prompt_text

WANT = "# Trio Evaluator — one headless iteration\nscope=local → repair\n"


def test_exact_row_matches():
    assert match(WANT, WANT)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_fffd_run_for_one_non_ascii_char_matches(run):
    row = WANT.replace("—", FFFD * run)
    assert match(row, WANT)


def test_four_fffd_run_is_rejected():
    assert not match(WANT.replace("—", FFFD * 4), WANT)


def test_two_non_ascii_chars_each_replaced_match():
    row = WANT.replace("—", FFFD * 2).replace("→", FFFD * 3)
    assert match(row, WANT)


def test_fffd_for_an_ascii_char_is_rejected():
    row = WANT.replace("Trio", "T" + FFFD * 2 + "io")  # FFFD for "r"
    assert not match(row, WANT)


def test_extra_fffd_run_where_want_has_ascii_is_rejected():
    row = WANT.replace("one headless", "one" + FFFD + "headless")
    assert not match(row, WANT)
    inserted = WANT.replace("one ", "one " + FFFD * 2)
    assert not match(inserted, WANT)


def test_run_cannot_swallow_two_adjacent_chars():
    want = "a——b"
    assert match("a" + FFFD * 2 + "—b", want)
    assert not match("a" + FFFD * 3 + "b", want)  # one run, two chars


def test_del_prefixed_row_is_rejected():
    row = norm("\x7f" * 200 + WANT.replace("—", FFFD * 2))
    assert not match(row, norm(WANT))
    assert not match(norm("\x7f" * 200 + WANT), norm(WANT))


def test_head_truncated_row_is_rejected():
    row = WANT.replace("—", FFFD * 2)
    assert not match(row[3:], WANT)
    assert not match(WANT[1:], WANT)


def test_tail_truncated_row_is_rejected():
    row = WANT.replace("—", FFFD * 2)
    assert not match(row[:-3], WANT)
    assert not match(WANT[:-1], WANT)
    assert not match(WANT[:-1], WANT[:-1] + "—")  # FFFD-free tail loss
    assert not match(WANT + "x", WANT)


def test_trailing_fffd_beyond_want_is_rejected():
    assert not match(WANT + FFFD, WANT)
    assert not match(WANT + FFFD * 2, WANT + "—" + "x")


def test_fffd_in_want_matches_only_literally():
    want = "x " + FFFD + " — y"
    assert match(want, want)
    assert not match("x " + FFFD * 2 + " — y", want)
    assert not match("x " + FFFD + " " + FFFD * 2 + " y", want)


def test_empty_sides():
    assert match("", "")
    assert not match(FFFD, "")
    assert not match("", "—")


def _held_pair() -> tuple[str, str]:
    row = norm(HELD_ROW.read_text(encoding="utf-8"))
    assert row.count(FFFD) == 2 and FFFD * 2 in row
    want = row.replace(FFFD * 2, "—")
    return row, want


def test_held_row_fixture_shape():
    """The reconstruction matches the canonical protocol line."""
    row, want = _held_pair()
    assert 5000 < row.index(FFFD) < 7000
    line = "- `scope=local:<paths>` — the failure is provably local"
    assert line in want
    assert line in (REPO / "prompts" / "protocol-essentials.md").read_text(
        encoding="utf-8"
    )
    assert row != want


def test_held_row_matches_reconstructed_prompt():
    row, want = _held_pair()
    assert match(row, want)
    assert not match(row[1:], want)
    assert not match(row[:-1], want)
    assert not match(norm("\x7f" * 200 + row), want)


class _RowsBroker(broker_http.BrokerClient):
    def __init__(self, rows: list[str]) -> None:
        super().__init__("http://broker.invalid")
        self._rows = rows

    def get_items(self, session_id: str) -> Any:  # type: ignore[override]
        return {"items": [{"role": "user", "content": r} for r in self._rows]}


def test_ensure_first_prompt_lands_on_held_row():
    row, want = _held_pair()
    raw = HELD_ROW.read_text(encoding="utf-8")
    broker = _RowsBroker([raw])
    broker.ensure_first_prompt("3f5a37b6", want, wait_seconds=0.0)


def test_ensure_first_prompt_still_holds_truncated_mojibake_row():
    row, want = _held_pair()
    broker = _RowsBroker([row[:-10]])
    with pytest.raises(broker_http.PromptDeliveryUncertain):
        broker.ensure_first_prompt("3f5a37b6", want, wait_seconds=0.0)
