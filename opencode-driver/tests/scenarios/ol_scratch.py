"""Open-loop happy path whose Evaluator leaves untracked probe files behind
before it says SHIP (``test_scratch_out.py``).

Same flow as ``ol_happy.py``; the integration-eval additionally writes, into
the product worktree it grades, what a real agent leaves there: a probe at the
``.trio-opencode/`` root, a file under a mangled run-scratch path and (when
``FAKE_SCRATCH_PRODUCT`` is set) a genuinely untracked product file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402
import ol_happy  # noqa: E402
import ol_multirepo  # noqa: E402


def _litter(lead_wt: Path) -> None:
    for rel, body in ((".trio-opencode/eval-probe.py", "print(1)\n"),
                      (".trio-opencode/run-0123abcd/tmp/opencode/acc_iter1.json", "{}\n")):
        common.write(lead_wt / rel, body)
    if os.environ.get("FAKE_SCRATCH_NESTED"):   # a product dir that merely shares the name
        common.write(lead_wt / "sub" / ".trio-opencode" / "real.py", "x = 1\n")
    if os.environ.get("FAKE_SCRATCH_PRODUCT") and not os.environ.get("FAKE_SCRATCH_MULTI"):
        common.write(lead_wt / "src" / ".worker-state.json", "{}\n")


def _litter_multi(ctx) -> None:
    """Multi-repo (``FAKE_SCRATCH_MULTI`` = ``home``/``be``/``both``): the
    same litter in the pinned checkout of each named repo; with
    ``FAKE_SCRATCH_PRODUCT`` a genuinely untracked product file in ``be``."""
    pins = ol_common.pin_lines(ctx)
    where = os.environ["FAKE_SCRATCH_MULTI"]
    for name in (("home", "be") if where == "both" else (where,)):
        _litter(Path(pins[name][0]))
    if os.environ.get("FAKE_SCRATCH_PRODUCT"):
        common.write(Path(pins["be"][0]) / "src" / ".worker-state.json", "{}\n")


def handle(ctx) -> None:
    multi = bool(os.environ.get("FAKE_SCRATCH_MULTI"))
    if ctx.agent == "trio-evaluator" and ol_common.context(ctx)[0] == "integration-eval":
        if multi:
            _litter_multi(ctx)
        else:
            _litter(_lead_wt(ctx))
    (ol_multirepo if multi else ol_happy).handle(ctx)


def _lead_wt(ctx) -> Path:
    m = ol_happy._LEAD_WT_RE.search(ctx.prompt)
    return Path(m.group(1))
