"""eval-r17rc L-1: receipt-read regression vs 84d55e5.

The r18a fix narrowed `_is_file_read` to stop counting HTTP response bodies
and other non-file `.read()`/`.decode()` calls as file text. As a side
effect it also stopped the L7 tautology lint from recognizing
`json.load(open(<receipt path>))`, `with open(<receipt path>) as fh: ...`
and pandas `read_csv`/`read_json`/... readers over a receipts path as
receipt reads, even though the *path itself* clearly names `results/` or
`evidence/`. These tests pin the fixed behaviour: the three regressed forms
are flagged again, `json.loads(Path(...).read_text())` (never broken) still
is, and the false positives the r18a fix removed (HTTP response bodies;
files the test's own action wrote) stay unflagged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = ROOT / "metrics" / "trio-check.py"
_spec = importlib.util.spec_from_file_location("trio_check_r17rc_lowfix", CHECK_PATH)
CHECK = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CHECK)


def flags(text: str) -> list[str]:
    return CHECK.python_test_flags("tests/test_x.py", text)


def receipt_flags(text: str) -> list[str]:
    return [f for f in flags(text) if "receipt" in f]


# --- regressed positives: must be flagged again -----------------------------

def test_json_load_of_open_receipt_is_flagged():
    src = ('import json\n\ndef test_r():\n'
           '    data = json.load(open("results/recon.json"))\n    assert data["ok"]\n')
    assert receipt_flags(src)


def test_with_open_receipt_then_json_load_is_flagged():
    src = ('import json\n\ndef test_r():\n'
           '    with open("evidence/run.json") as fh:\n        data = json.load(fh)\n'
           '    assert data["ok"]\n')
    assert receipt_flags(src)


def test_pandas_read_csv_of_receipt_is_flagged():
    src = ('import pandas as pd\n\ndef test_r():\n'
           '    df = pd.read_csv("results/recon.csv")\n    assert len(df) == 3\n')
    assert receipt_flags(src)


def test_pandas_read_csv_via_path_variable_is_flagged():
    src = ('import pandas as pd\nfrom pathlib import Path\n\ndef test_r():\n'
           '    p = Path("results") / "recon.csv"\n    df = pd.read_csv(p)\n'
           '    assert len(df) == 3\n')
    assert receipt_flags(src)


# --- never-broken positive: still flagged -----------------------------------

def test_json_loads_of_path_read_text_still_flagged():
    src = ('import json\nfrom pathlib import Path\n\ndef test_r():\n'
           '    data = json.loads(Path("results/recon.json").read_text())\n'
           '    assert data["ok"]\n')
    assert receipt_flags(src)


# --- negatives the r18a fix removed: must stay unflagged --------------------

def test_http_response_body_is_not_a_receipt_read():
    src = 'def test_x(resp):\n    body = resp.read()\n    assert body\n'
    assert not receipt_flags(src)


def test_json_loads_of_http_body_is_not_a_receipt_read():
    src = ('import json\n\ndef test_x(resp):\n    data = json.loads(resp.read().decode())\n'
           '    assert data["status"] == "ok"\n')
    assert not receipt_flags(src)


def test_json_results_key_is_not_a_receipt_read():
    src = 'import json\n\ndef test_x(out):\n    data = json.loads(out)\n    assert data["results"] == []\n'
    assert not receipt_flags(src)


def test_open_receipt_path_for_writing_is_not_a_receipt_read():
    src = ('def test_x():\n'
           '    with open("results/out.json", "w") as fh:\n        fh.write("{}")\n'
           '    assert True\n')
    assert not receipt_flags(src)


def test_open_of_file_the_test_itself_wrote_is_not_under_results_or_evidence():
    # The test's own tmp fixture, not a results/evidence receipt: open()
    # over a path with neither directory name is never flagged as a
    # receipt read.
    src = ('import json\n\ndef test_x(tmp_path):\n'
           '    p = tmp_path / "scratch.json"\n    p.write_text(\'{"ok": true}\')\n'
           '    data = json.load(open(p))\n    assert data["ok"]\n')
    assert not receipt_flags(src)
