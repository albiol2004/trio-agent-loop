"""r19 C8: acceptance prompts -- generated from canonical sources, the
author config on the evaluator's model, and every acceptance block rendered
only while the switch is on (switch off: byte-identical renders)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


T = _load("trioctl_r19_c8", ROOT / "omnigent" / "trioctl")
GEN = _load("gen_r19_c8", ROOT / "prompts" / "generate.py")
CTXS = {
    "lead-pass": ("lead", {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None}),
    "slice-eval": ("evaluator", {"mode": "open-loop", "kind": "slice-eval", "slice": "s1",
                                 "sha": "a" * 40, "acceptance_covered": "ACCEPTANCE (covered): ACC-01 FAIL x [sole-cover]"}),
    "integration": ("evaluator", {"mode": "open-loop", "kind": "integration-eval",
                                  "pinned_sha": "b" * 40, "evaluator_attempt": "a1", "iteration": 3,
                                  "acceptance": {"text": "FROZEN ACCEPTANCE @bbbbbbbbbbbb: 1/2 PASS"}}),
    "lockstep-lead": ("lead", {}),
    "lockstep-eval": ("evaluator", {"evaluator_attempt": "a", "pinned_sha": "c" * 40,
                                    "acceptance": {"text": "FROZEN ACCEPTANCE @cccccccccccc: 2/2 PASS"}}),
}


def _render(tmp_path, role, ctx, acceptance):
    mb = tmp_path / "mb"
    mb.mkdir(exist_ok=True)
    kw = {"acceptance": acceptance} if acceptance is not None else {}
    return T.OmnigentRunner(repo=tmp_path, config={}, **kw)._prompt(role, 3, mb, dict(ctx))


def _strip(ctx):
    return {k: v for k, v in ctx.items() if not k.startswith("acceptance")}


@pytest.mark.parametrize("name", sorted(CTXS))
def test_switch_off_renders_are_unchanged(tmp_path, name):
    role, ctx = CTXS[name]
    plain = _render(tmp_path, role, _strip(ctx), None)
    assert _render(tmp_path, role, _strip(ctx), {"enabled": False}) == plain
    for marker in ("FROZEN ACCEPTANCE", "ACCEPTANCE (covered)", "acceptance wait"):
        assert marker not in plain


@pytest.mark.parametrize("name", sorted(CTXS))
def test_switch_on_adds_the_acceptance_blocks(tmp_path, name):
    role, ctx = CTXS[name]
    text = _render(tmp_path, role, ctx, {"enabled": True})
    base = _render(tmp_path, role, _strip(ctx), None)
    assert text.startswith(base.rstrip("\n"))
    if role == "lead":
        assert "trioctl omnigent acceptance wait --mailbox" in text and "covers:" in text
        assert "ACCEPTANCE-DISPUTE:" in text
    elif ctx.get("kind") == "slice-eval":
        assert "ACCEPTANCE (covered): ACC-01 FAIL" in text and "sole-cover" in text
        assert "trioctl omnigent acceptance run" not in text
    else:
        assert "FROZEN ACCEPTANCE @" in text and "## Frozen acceptance" in text
        assert "acceptance: amend ACC-NN (evaluator, iter N)" in text
    assert not re.search(r"`git [^`]*`", text[len(base):]), "no backticked git in acceptance blocks"


def test_acceptance_errors_ride_on_the_lead_prompt(tmp_path):
    text = _render(tmp_path, "lead", {"acceptance_errors": ["unmapped acceptance check(s): ACC-02"]},
                   {"enabled": True})
    assert "ACCEPTANCE REFUSAL" in text and "ACC-02" in text


def test_generated_acceptance_sources():
    outputs = GEN.all_outputs()
    for source, dest in GEN.ACCEPTANCE_PROMPTS:
        assert outputs[ROOT / dest] == (ROOT / "prompts" / "canonical" / source).read_text()
        assert (ROOT / dest).read_text() == outputs[ROOT / dest]
    cfg = (ROOT / GEN.ACCEPTANCE_ROLE_CONFIG).read_text()
    ev = (ROOT / GEN.EVALUATOR_ROLE_CONFIG).read_text()
    model = re.search(r"^  model: (\S+)$", ev, re.M).group(1)
    assert f"  model: {model}\n" in cfg and "name: trio-omnigent-acceptance" in cfg
    assert "spawn: false" in cfg and "{export}" not in cfg
    assert "{export}" in (PROMPTS / "acceptance.md").read_text()


def test_install_templates_the_author_model_and_ships_the_runner():
    text = (ROOT / "install.sh").read_text()
    assert "for role in lead evaluator builder scout docs acceptance; do" in text
    assert 'EVAL_MODEL="$(sed -n' in text and "trio-acceptance.py" in text
    skill = (ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "SKILL.md").read_text()
    assert "omnigent/trio-omnigent-roles/acceptance" in skill
    assert T.REGISTRY_PROFILE in skill
