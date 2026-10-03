"""Requirement-map gate: the author lists every GOAL requirement with >=1
normal and >=1 HARD check in MANIFEST `requirements`; the freeze gate turns a
missing hard check into a gap (one retry, then freeze + loud record); packs
frozen before the rule keep working with a warning. Real subprocess CLI,
real git, the real loop core with fake roles."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T
from metrics.tests import test_r19_trio_check_acceptance as TC

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "metrics" / "trio-acceptance.py"


def _load():
    spec = importlib.util.spec_from_file_location("trio_acceptance_reqmap", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _load()
GOAL = ("# GOAL\n\nThe CLI prints hello to the named user.\n"
        "Never print secret values.\nUnknown users exit with code 2.\n")
NOTES = "Note: names are case-insensitive.\n"


def chk(cid, kind="behaviour", quote="prints hello"):
    return {"id": cid, "goal_ref": "GOAL.md:3", "goal_quote": quote, "kind": kind,
            "surface": "cli", "run": ["python3", f"acceptance/checks/{cid}.py"],
            "expect": {"exit": 0}, "timeout_s": 20, "needs": [], "binds": [],
            "network": "loopback"}


def req(rid="R1", normal=("ACC-01",), hard=(("ACC-02", "boundary"),), quote="prints hello",
        **kw):
    entry = {"id": rid, "goal_ref": "GOAL.md:3", "goal_quote": quote,
             "normal": list(normal),
             "hard": [{"check": c, "type": t} for c, t in hard]}
    entry.update(kw)
    return entry


def manifest(requirements="ok", n=6):
    checks = [chk(f"ACC-{k:02d}") for k in range(1, n + 1)]
    m = {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
         "checks": checks}
    if requirements == "ok":
        m["requirements"] = [req()]
    elif requirements is not None:
        m["requirements"] = requirements
    return m


def base_result(manifest_, passing=()):
    return {"results": [{"id": c["id"], "outcome": "PASS" if c["id"] in passing else "FAIL",
                         "error": None, "over_budget": False}
                        for c in manifest_["checks"]]}


def ids(n=6):
    return [f"ACC-{k:02d}" for k in range(1, n + 1)]


def problems(requirements, kept=None, dropped=None, goal=GOAL, notes=None):
    m = {"requirements": requirements}
    return TA.requirements_problems(m, ids() if kept is None else kept, goal, notes, dropped)


# --------------------------------------------------- requirements_problems


def test_complete_map_has_no_problems():
    reqs = [req("R1", ("ACC-01",), (("ACC-02", "boundary"),)),
            req("R2", ("ACC-03",), (("ACC-04", "failure-mode"),), quote="Never print secret"),
            req("R3", ("ACC-05",), (("ACC-06", "malformed-input"), ("ACC-04", "concurrency")),
                quote="Unknown users exit")]
    reqs[0]["hard"].append({"check": "ACC-05", "type": "interaction", "with": "R3"})
    assert problems(reqs) == []
    # One check may serve several requirements; a notes quote is accepted.
    assert problems([req("R1", quote="names are case-insensitive")], notes=NOTES) == []
    assert problems([req("R1"), req("R2")]) == []


@pytest.mark.parametrize("value", [None, [], "R1", {"id": "R1"}])
def test_missing_map(value):
    m = {} if value is None else {"requirements": value}
    got = TA.requirements_problems(m, ids(), GOAL)
    assert got == ["requirements: missing — list every GOAL requirement and constraint "
                   "with >=1 normal and >=1 hard check"]


def test_no_hard_check_and_empty_normal():
    got = problems([req("R1", hard=()), req("R2", normal=(), quote="Never print secret")])
    assert got == ["requirements: R1 has no hard check", "requirements: R2 has no normal check"]


def test_hard_check_dropped_at_freeze_names_the_reason():
    kept = [c for c in ids() if c != "ACC-02"]
    got = problems([req()], kept=kept, dropped=[("ACC-02", "passes-at-base")])
    assert got == ["requirements: R1 hard check ACC-02 was dropped at freeze (passes-at-base)",
                   "requirements: R1 has no hard check"]


def test_unknown_check_ids():
    got = problems([req("R1", normal=("ACC-09",), hard=(("ACC-77", "boundary"),))])
    assert "requirements: R1 normal check ACC-09 unknown" in got
    assert "requirements: R1 hard check ACC-77 unknown" in got


def test_goal_quote_must_be_verbatim():
    got = problems([req("R3", quote="never reached")])
    assert got == ["requirements: R3 goal_quote is not a verbatim substring of GOAL.md "
                   "or the notes"]
    assert problems([req("R3", quote="  ")]) == ["requirements: R3 goal_quote is empty"]
    # Without a GOAL text (unit callers) only emptiness is checked.
    assert problems([req("R3", quote="never reached")], goal=None) == []


def test_duplicate_ids_bad_type_unknown_keys():
    got = problems([req("R1"), req("R1")])
    assert got == ["requirements: R1 is duplicated"]
    got = problems([req("R1", hard=(("ACC-02", "fuzzy"),))])
    assert any("type 'fuzzy' is not one of boundary, interaction" in g for g in got)
    assert "requirements: R1 has no hard check" in got
    got = problems([req("R1", severity="high")])
    assert got == ["requirements: R1 has unknown key(s) severity"]
    entry = req("R1")
    entry["hard"][0]["why"] = "x"
    assert any("unknown key(s) why" in g for g in problems([entry]))
    assert any("not an object" in g for g in problems(["R1"]))


def test_interaction_needs_a_valid_other_requirement():
    def inter(**kw):
        entry = req("R1", hard=())
        entry["hard"] = [{"check": "ACC-02", "type": "interaction", **kw}]
        return entry
    for kw in ({}, {"with": "R9"}, {"with": "R1"}, {"with": 3}):
        got = problems([inter(**kw), req("R2")])
        assert any("is an interaction and needs `with`" in g for g in got), kw
        assert "requirements: R1 has no hard check" in got
    assert problems([inter(**{"with": "R2"}), req("R2")]) == []
    entry = req("R1", hard=())
    entry["hard"] = [{"check": "ACC-02", "type": "boundary", "with": "R2"}]
    got = problems([entry, req("R2")])
    assert any("`with` is only allowed for type interaction" in g for g in got)


def test_check_both_normal_and_hard_does_not_count_as_hard():
    got = problems([req("R1", normal=("ACC-01", "ACC-02"), hard=(("ACC-02", "boundary"),))])
    assert any("ACC-02 is also listed as normal" in g for g in got)
    assert got[-1] == "requirements: R1 has no hard check"
    # The same check may be hard for one requirement and normal for another.
    assert problems([req("R1"), req("R2", normal=("ACC-02",), hard=(("ACC-03", "boundary"),))]) == []


def test_problem_order_is_deterministic():
    reqs = [req("R2", hard=()), req("R1", normal=("ACC-09",), quote="zzz")]
    first = problems(copy.deepcopy(reqs))
    assert first == problems(copy.deepcopy(reqs))
    assert [g.split()[1] for g in first] == ["R2", "R1", "R1", "R1"]
    assert all(g.startswith("requirements: ") and "\n" not in g for g in first)


def test_has_requirements_map_and_legacy_warning():
    assert not TA.has_requirements_map({"checks": []})
    assert TA.has_requirements_map({"requirements": []})
    assert not TA.has_requirements_map({"requirements": "x"})
    assert TA.legacy_pack_warning({"requirements": [req()]}) is None
    text = TA.legacy_pack_warning({"checks": []})
    assert text == ("legacy frozen pack: no requirements map (frozen before the "
                    "requirement-map rule); hard-check coverage is not enforced for this pack")


# ------------------------------------------------------------ freeze_filter


def run_filter(m, passing=(), **kw):
    return TA.freeze_filter(m, base_result(m, passing), GOAL, NOTES, **kw)


def test_freeze_filter_complete_map_is_clean():
    m = manifest()
    out = run_filter(m)
    assert out["requirement_gaps"] == [] and out["retry"] is False and out["fatal"] == []
    assert out["manifest"]["requirements"] == m["requirements"]
    assert out["dropped"] == []


def test_freeze_filter_missing_map_is_a_gap_and_retries():
    out = run_filter(manifest(None))
    assert out["retry"] is True
    assert out["requirement_gaps"] == [TA.MISSING_MAP_GAP]
    assert TA.MISSING_MAP_GAP in out["fatal"]
    assert len(out["manifest"]["checks"]) == 6  # the survivors are still kept


def test_freeze_filter_no_hard_check_retries_with_the_gap_in_fatal():
    out = run_filter(manifest([req("R1", hard=())]))
    assert out["retry"] is True
    assert out["requirement_gaps"] == ["requirements: R1 has no hard check"]
    assert out["fatal"] == out["requirement_gaps"]


def test_freeze_filter_hard_check_dropped_at_base():
    out = run_filter(manifest(), passing=("ACC-02",))
    assert ("ACC-02", "passes-at-base") in out["dropped"]
    assert out["retry"] is True
    assert out["requirement_gaps"] == [
        "requirements: R1 hard check ACC-02 was dropped at freeze (passes-at-base)",
        "requirements: R1 has no hard check"]
    assert "ACC-02" not in [c["id"] for c in out["manifest"]["checks"]]


def test_freeze_filter_interaction_with_unknown_requirement():
    entry = req("R1", hard=())
    entry["hard"] = [{"check": "ACC-02", "type": "interaction", "with": "R7"}]
    out = run_filter(manifest([entry]))
    assert out["retry"] is True
    assert any("needs `with`" in g for g in out["requirement_gaps"])


def test_freeze_filter_guard_can_be_a_hard_check():
    m = manifest()
    m["checks"][1]["kind"] = "guard"
    out = run_filter(m, passing=("ACC-02",))  # a guard may pass at base
    assert out["requirement_gaps"] == [] and out["retry"] is False


# ---------------------------------------------------------------- CLI


def cli(*args, env=None):
    e = {**os.environ, "TRIO_ACCEPTANCE_SANDBOX": "none", **(env or {})}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, env=e)


def write_export(root: Path, m: dict) -> Path:
    export = root / "export"
    (export / ".acceptance-input").mkdir(parents=True)
    (export / ".acceptance-input" / "GOAL.md").write_text(GOAL)
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True)
    for c in m["checks"]:
        (acc / "checks" / f"{c['id']}.py").write_text("raise SystemExit(1)\n")
    (acc / "MANIFEST.json").write_text(json.dumps(m))
    return export


def test_validate_prints_requirement_gaps_and_exits_nonzero(tmp_path):
    export = write_export(tmp_path, manifest([req("R1", hard=())]))
    proc = cli("validate", "--export", str(export))
    assert proc.returncode == 1
    assert "REQUIREMENT GAP: requirements: R1 has no hard check" in proc.stdout
    assert "INVALID pack: requirements:" not in proc.stdout
    assert "the driver would ask for one retry" in proc.stdout


def test_validate_missing_map_prints_the_gap(tmp_path):
    export = write_export(tmp_path, manifest(None))
    proc = cli("validate", "--export", str(export))
    assert proc.returncode == 1
    assert f"REQUIREMENT GAP: {TA.MISSING_MAP_GAP}" in proc.stdout


def test_validate_complete_map_exits_zero(tmp_path):
    export = write_export(tmp_path, manifest())
    proc = cli("validate", "--export", str(export))
    assert proc.returncode == 0, proc.stdout
    assert "REQUIREMENT GAP" not in proc.stdout


# ------------------------------------------------------- legacy frozen pack


def legacy_mailbox(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    mb = repo / "loop"
    (mb / "acceptance" / "checks").mkdir(parents=True)
    (mb / "GOAL.md").write_text(GOAL)
    m = manifest(None, n=2)
    for c in m["checks"]:
        (mb / "acceptance" / "checks" / f"{c['id']}.py").write_text("raise SystemExit(0)\n")
    (mb / "acceptance" / "MANIFEST.json").write_text(json.dumps(m, indent=2))
    (mb / "acceptance" / "AMENDMENTS.md").write_text("")
    pin = TA.manifest_sha256(mb / "acceptance")
    (mb / "acceptance" / "FROZEN").write_text(
        TA.frozen_text(pin, "b" * 40, "m s", [], [(pin, "freeze")]))
    tree = tmp_path / "tree"
    tree.mkdir()
    return mb, pin


def test_legacy_pack_runs_warns_on_stderr_and_pin_still_verifies(tmp_path):
    mb, pin = legacy_mailbox(tmp_path)
    before = (mb / "acceptance" / "MANIFEST.json").read_bytes()
    proc = cli("run", "--mailbox", str(mb), "--tree", str(tmp_path / "tree"))
    assert proc.returncode == 0, proc.stderr
    assert "WARNING legacy frozen pack: no requirements map" in proc.stderr
    assert json.loads(proc.stdout)["results"][0]["outcome"] == "PASS"
    ver = cli("verify", "--mailbox", str(mb), "--pin", pin)
    assert ver.returncode == 0 and f"pin ok {pin}" in ver.stdout
    assert cli("hash", "--mailbox", str(mb)).stdout.strip() == pin
    assert (mb / "acceptance" / "MANIFEST.json").read_bytes() == before  # never rewritten


def test_mapped_pack_run_has_no_legacy_warning(tmp_path):
    mb, _pin = legacy_mailbox(tmp_path)
    m = json.loads((mb / "acceptance" / "MANIFEST.json").read_text())
    m["requirements"] = [req("R1", ("ACC-01",), (("ACC-02", "boundary"),))]
    (mb / "acceptance" / "MANIFEST.json").write_text(json.dumps(m))
    proc = cli("run", "--mailbox", str(mb), "--tree", str(tmp_path / "tree"))
    assert proc.returncode == 0 and "legacy" not in proc.stderr


# --------------------------------------------------------------- amendments


def test_amendment_cannot_change_the_requirement_map():
    old = manifest()
    for new_reqs in (None, [req("R1", hard=())], [req("R1"), req("R2")]):
        new = copy.deepcopy(old)
        if new_reqs is None:
            del new["requirements"]
        else:
            new["requirements"] = new_reqs
        probs = TA.amendment_problems(old, new, ["MANIFEST.json"], [])
        assert "manifest `requirements` may not be amended" in probs
    # An unchanged map does not trip the rule.
    assert not any("requirements" in p
                   for p in TA.amendment_problems(old, copy.deepcopy(old), [], []))


# -------------------------------------------------------------- trio-check


def test_trio_check_warns_for_a_legacy_frozen_pack(tmp_path):
    mb = TC.make(tmp_path)
    found = TC.levels(mb)
    assert any(lv == "WARN" and msg == TA.legacy_pack_warning({}) for lv, msg in found)
    assert not [f for f in found if f[0] == "VIOLATION"]


def test_trio_check_is_quiet_for_a_mapped_pack_and_warns_on_recorded_gaps(tmp_path):
    mb = TC.make(tmp_path, frozen=False, commit=False)
    acc = mb / "acceptance"
    m = json.loads((acc / "MANIFEST.json").read_text())
    m["requirements"] = [req("R1", ("ACC-01",), (("ACC-02", "boundary"),))]
    (acc / "MANIFEST.json").write_text(json.dumps(m))
    pin = TC.TA.manifest_sha256(acc)

    def freeze(**extra):
        (acc / "FROZEN").write_text(
            TC.TA.frozen_text(pin, "b" * 40, "m s", [], [(pin, "freeze")], extra=extra))
        return TC.levels(mb)

    clean = freeze()
    assert not any("requirement" in msg or "legacy" in msg for _lv, msg in clean)
    gapped = freeze(requirement_gaps="requirements: R1 has no hard check")
    assert any(lv == "WARN" and "requirement-map gaps" in msg
               and "R1 has no hard check" in msg for lv, msg in gapped)


# ----------------------------------------------------------- the loop core


def authors_of(fake):
    return [c["author"] for c in fake.contexts if "author" in c]


def gapped_pack():
    return {"requirements": [{"id": "R1", "goal_ref": "GOAL.md:3",
                              "goal_quote": "feature f1 works", "normal": ["ACC-01"],
                              "hard": []}]}


def test_loop_one_retry_lists_the_gaps_then_the_fixed_pack_freezes_clean(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}],
                  author=[{"pack": gapped_pack()}, {}])
    assert T.run(mb, fake) == 0
    authors = authors_of(fake)
    assert len(authors) == 2  # exactly one retry
    assert authors[0]["fatal"] == [] and authors[0]["dropped"] == []
    assert authors[1]["fatal"] == ["requirements: R1 has no hard check"]
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert "requirement_gaps" not in frozen
    lg = T.log(mb)
    assert "author retry: requirements: R1 has no hard check" in lg
    assert "WARNING" not in lg and "acceptance: frozen 8 check(s)" in lg
    m = json.loads((mb / "acceptance" / "MANIFEST.json").read_text())
    assert m["requirements"][0]["hard"] == [{"check": "ACC-02", "type": "boundary"}]


def test_loop_missing_map_is_one_retry_then_frozen_with_the_gap_recorded(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}],
                  author=[{"pack": {"requirements": None}}, {"pack": {"requirements": None}}])
    assert T.run(mb, fake) == 0
    authors = authors_of(fake)
    assert len(authors) == 2 and authors[1]["fatal"] == [TA.MISSING_MAP_GAP]
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert f"requirement_gaps: {TA.MISSING_MAP_GAP}" in frozen
    lg = T.log(mb)
    assert ("acceptance: WARNING frozen with requirement-map gaps after the retry: 1 ("
            + TA.MISSING_MAP_GAP[:60]) in lg
    # The frozen LOG line keeps its format; the survivors froze as before.
    assert "acceptance: frozen 8 check(s) @" in lg and "(dropped 0; author fake-model)" in lg
    assert "WARNING legacy frozen pack" not in lg


def test_loop_hard_check_dropped_at_base_both_times_freezes_with_gaps(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    pack = {"passing_at_base": (2,)}
    fake = T.Fake(repo, mb, lead_script=[{"covers": [c for c in T.ALL if c != "ACC-02"]}],
                  eval_script=[{"verdict": "SHIP"}], author=[{"pack": pack}, {"pack": pack}])
    assert T.run(mb, fake) == 0
    authors = authors_of(fake)
    assert len(authors) == 2
    assert {d[0] for d in authors[1]["dropped"]} == {"ACC-02"}
    assert any("R1 hard check ACC-02 was dropped at freeze (passes-at-base)" in f
               for f in authors[1]["fatal"])
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert "ACC-02 passes-at-base" in frozen and "requirement_gaps: requirements: R1 hard" in frozen
    assert "WARNING frozen with requirement-map gaps after the retry: 2 (" in T.log(mb)
    assert "author retry: 1 dropped; 2 requirement gap(s)" in T.log(mb)


def freeze_legacy_pack(mb: Path, repo: Path) -> None:
    """Freeze a pack with no requirement map exactly as the pre-rule core did."""
    ctl = trio_loop.AcceptanceController(mb, repo, T.Fake(repo, mb, lead_script=[],
                                                          eval_script=[]), {"enabled": True})
    base = T.git(repo, "rev-parse", "HEAD")
    ctl.state.update({"status": "authoring", "base": base})
    export = mb.parent.parent / "legacy-export"
    info = ctl.ta.build_export(repo, base, export, mb)
    T.author_pack(export, requirements=None)
    acc = export / "acceptance"
    manifest_ = ctl.ta.load_manifest(acc)
    fresh = mb.parent.parent / "legacy-fresh"
    ctl.ta.build_export(repo, base, fresh, mb)
    run_ = ctl.ta.run_pack(acc, fresh, exclude={"acceptance", ".acceptance-input"},
                           manifest=manifest_)
    filtered = ctl.ta.freeze_filter(manifest_, run_, T.GOAL, None, acc)
    filtered.update({"requirement_gaps": [], "fatal": [], "retry": False})
    ctl._freeze(1, export, filtered, {"model": "old", "session": "s"}, info, T.GOAL, None)
    assert "requirement_gaps" not in (mb / "acceptance" / "FROZEN").read_text()


def test_loop_resume_of_a_legacy_frozen_pack_warns_exactly_once(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    freeze_legacy_pack(mb, repo)
    assert "WARNING legacy" not in T.log(mb)
    line = ("acceptance: WARNING legacy frozen pack: no requirements map (frozen before "
            "the requirement-map rule); hard-check coverage is not enforced for this pack")
    for _ in range(2):  # two controller starts (e.g. two native helper ops)
        ctl = trio_loop.AcceptanceController(
            mb, repo, T.Fake(repo, mb, lead_script=[], eval_script=[]), {"enabled": True})
        ctl.on_resume()
        assert ctl._resume_error is None
        ctl.start(1)  # a frozen pack is left alone and not re-announced
    assert T.log(mb).count(line) == 1
    assert T.log(mb).count("WARNING") == 1


def test_loop_mapped_pack_resume_has_no_legacy_warning(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    ctl = trio_loop.AcceptanceController(mb, repo, fake, {"enabled": True})
    ctl.on_resume()
    assert "WARNING" not in T.log(mb)
