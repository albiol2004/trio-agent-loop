"""ocgen.py — generated opencode.json + per-role agent files, permissions,
and the hand-written YAML frontmatter renderer (SPEC.md "Generated OpenCode
config")."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode import ocgen

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cfg(*, container_mode: bool = False) -> config_mod.Config:
    d = config_mod._default_dict()
    return config_mod.Config(
        opencode_bin=d["opencode_bin"],
        models=dict(d["models"]),
        variants=dict(d["variants"]),
        provider=config_mod.ProviderConfig(**d["provider"]),
        timeouts=config_mod.TimeoutsConfig(
            turn_seconds=d["timeouts"]["turn_seconds"],
            idle_seconds=d["timeouts"]["idle_seconds"],
            evaluator_turn_seconds=d["timeouts"]["evaluator_turn_seconds"],
        ),
        retries=config_mod.RetriesConfig(
            max_attempts=d["retries"]["max_attempts"],
            backoff_seconds=tuple(d["retries"]["backoff_seconds"]),
        ),
        max_iterations=d["max_iterations"],
        root_free=d["root_free"],
        container_mode=container_mode,
    )


# --------------------------------------------------------------------------
# A tiny, test-only YAML-subset parser: enough to round-trip the block
# mappings render_frontmatter() produces (2-space indent, "key: value" or
# "key:" + nested block, double-quoted scalars, true/false/null, no lists,
# no comments, no flow collections). This is deliberately not a general
# YAML parser -- it only has to understand what ocgen.py itself emits.
# --------------------------------------------------------------------------

def _unquote(token: str) -> object:
    token = token.strip()
    if token == "":
        return ""
    if token[0] == '"':
        return json.loads(token)
    if token == "true":
        return True
    if token == "false":
        return False
    if token == "null":
        return None
    return token


def _parse_yaml_block(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        this_indent = len(line) - len(line.lstrip(" "))
        if this_indent < indent:
            break
        if this_indent > indent:
            raise AssertionError(f"unexpected indent at line {i}: {line!r}")
        content = line.strip()
        assert ":" in content or content.endswith(":"), f"not a mapping line: {content!r}"
        if content[0] == '"':
            # quoted key: find the matching close-quote
            end_q = content.index('"', 1)
            while content[end_q - 1] == "\\":
                end_q = content.index('"', end_q + 1)
            key = json.loads(content[: end_q + 1])
            rest = content[end_q + 1:]
            assert rest.startswith(":"), f"expected ':' after quoted key: {content!r}"
            value_str = rest[1:].strip()
        else:
            key, _, value_str = content.partition(":")
            key = key.strip()
            value_str = value_str.strip()
        if value_str == "":
            nested, next_i = _parse_yaml_block(lines, i + 1, indent + 2)
            result[key] = nested
            i = next_i
        else:
            result[key] = _unquote(value_str)
            i += 1
    return result, i


def parse_frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    body = text[4:end]
    lines = body.splitlines()
    result, i = _parse_yaml_block(lines, 0, 0)
    assert i == len(lines)
    return result


# --------------------------------------------------------------------------
# render_frontmatter()
# --------------------------------------------------------------------------

def test_render_frontmatter_round_trips_nested_pattern_maps():
    fm = {
        "description": "Independent adversarial Trio evaluator; verifies and never repairs product code.",
        "mode": "primary",
        "model": "opencode-go/deepseek-v4.1-flash",
        "permission": {
            "external_directory": "deny",
            "bash": {"*": "allow", "git push*": "deny", "opencode*": "deny"},
            "task": {"*": "deny", "trio-scout": "allow"},
            "todowrite": "allow",
        },
    }
    text = ocgen.render_frontmatter(fm)
    assert text.startswith("---\n")
    assert text.rstrip("\n").endswith("---")
    parsed = parse_frontmatter(text)
    assert parsed == fm


def test_render_frontmatter_deterministic_key_order():
    fm = {"description": "x", "mode": "primary", "model": "a/b", "permission": {"edit": "allow"}}
    text1 = ocgen.render_frontmatter(fm)
    text2 = ocgen.render_frontmatter(dict(fm))
    assert text1 == text2
    # keys appear in insertion order
    order = [ln.split(":")[0].strip() for ln in text1.strip("\n").splitlines()[1:-1] if ":" in ln]
    assert order[:3] == ["description", "mode", "model"]


@pytest.mark.parametrize("raw", ["*", "git push*", "git * -f", "rm -rf ~*", "curl *|*sh*", "$HOME*", "a b"])
def test_glob_like_keys_and_values_are_quoted(raw: str):
    fm = {"permission": {"bash": {raw: "deny", "safe": raw}}}
    text = ocgen.render_frontmatter(fm)
    quoted = json.dumps(raw)
    assert quoted in text


def test_plain_tokens_are_not_quoted():
    fm = {"mode": "primary", "model": "opencode-go/deepseek-v4.1-flash"}
    text = ocgen.render_frontmatter(fm)
    assert "mode: primary\n" in text
    assert "model: opencode-go/deepseek-v4.1-flash\n" in text


def test_no_ask_value_anywhere_in_permissions():
    for role, perm in ocgen.PERMISSIONS.items():
        text = ocgen.render_frontmatter({"permission": perm})
        assert not re.search(r':\s*"?ask"?\s*$', text, re.MULTILINE), f"{role} permission contains 'ask'"


def test_no_auto_flag_in_permissions_or_renderer_output():
    for perm in ocgen.PERMISSIONS.values():
        text = ocgen.render_frontmatter({"permission": perm})
        assert "--auto" not in text


# --------------------------------------------------------------------------
# PERMISSIONS table shape
# --------------------------------------------------------------------------

def test_permissions_has_all_five_roles():
    # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md) added a 6th role,
    # "acceptance" (the author agent) -- always generated (see ocgen.py's
    # module docstring note), used only when a run's switch is on.
    assert set(ocgen.PERMISSIONS.keys()) == {
        "lead", "evaluator", "builder", "repair", "scout", "acceptance"}


def test_permissions_common_denies():
    for role, perm in ocgen.PERMISSIONS.items():
        assert perm["external_directory"] == "deny"
        assert perm["doom_loop"] == "deny"
        assert perm["question"] == "deny"
        assert perm["read"] == {"*": "allow"}
        assert perm["todowrite"] == "allow"


def test_permissions_webfetch_websearch_by_role():
    assert ocgen.PERMISSIONS["lead"]["webfetch"] == "allow"
    assert ocgen.PERMISSIONS["lead"]["websearch"] == "allow"
    assert ocgen.PERMISSIONS["evaluator"]["webfetch"] == "allow"
    assert ocgen.PERMISSIONS["evaluator"]["websearch"] == "allow"
    for role in ("builder", "repair", "scout"):
        assert ocgen.PERMISSIONS[role]["webfetch"] == "deny"
        assert ocgen.PERMISSIONS[role]["websearch"] == "deny"


def test_permissions_edit_by_role():
    for role in ("lead", "evaluator", "builder", "repair"):
        assert ocgen.PERMISSIONS[role]["edit"] == "allow"
    assert ocgen.PERMISSIONS["scout"]["edit"] == "deny"


def test_permissions_task_by_role():
    for role in ("lead", "evaluator"):
        assert ocgen.PERMISSIONS[role]["task"] == {"*": "deny", "trio-scout": "allow"}
    for role in ("builder", "repair", "scout"):
        assert ocgen.PERMISSIONS[role]["task"] == "deny"


def test_permissions_bash_deny_list_has_last_word_and_star_first():
    for role in ("lead", "evaluator", "builder", "repair"):
        bash = ocgen.PERMISSIONS[role]["bash"]
        keys = list(bash.keys())
        assert keys[0] == "*"
        assert bash["*"] == "allow"
        for pattern in ("git push*", "git reset --hard*", "rm -rf /*", "sudo *", "opencode*", "chmod -R 777*"):
            assert bash[pattern] == "deny", pattern


def test_evaluator_bash_allows_worktree_add_detach_after_denies():
    bash = ocgen.PERMISSIONS["evaluator"]["bash"]
    keys = list(bash.keys())
    assert bash["git worktree add --detach*"] == "allow"
    assert keys.index("git worktree add --detach*") > keys.index("git worktree remove*")


def test_builder_repair_bash_has_no_worktree_add_detach_allow():
    for role in ("builder", "repair"):
        bash = ocgen.PERMISSIONS[role]["bash"]
        assert "git worktree add --detach*" not in bash or bash["git worktree add --detach*"] == "deny"


def test_scout_bash_is_deny_first_allow_list():
    bash = ocgen.PERMISSIONS["scout"]["bash"]
    keys = list(bash.keys())
    assert keys[0] == "*"
    assert bash["*"] == "deny"
    for pattern in ("git log*", "git show*", "git diff*", "git status*", "ls*", "cat *", "grep *", "rg *", "find *", "head *", "tail *", "wc *"):
        assert bash[pattern] == "allow"


def test_no_permission_value_is_ask_anywhere():
    def walk(v):
        if isinstance(v, dict):
            for sub in v.values():
                yield from walk(sub)
        else:
            yield v

    for perm in ocgen.PERMISSIONS.values():
        for value in walk(perm):
            assert value != "ask"


# --------------------------------------------------------------------------
# generate()
# --------------------------------------------------------------------------

def test_generate_writes_opencode_json_and_agent_files_v1(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()

    env = ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=mailbox, style="v1")

    opencode_json_path = run_dir / "opencode" / "opencode.json"
    assert opencode_json_path.is_file()
    doc = json.loads(opencode_json_path.read_text(encoding="utf-8"))
    assert doc["autoupdate"] is False
    assert doc["share"] == "disabled"
    assert doc["model"] == cfg.model_for("lead")
    for builtin in ("build", "plan", "general", "explore"):
        assert doc["agent"][builtin]["disable"] is True
    assert "apiKey" not in json.dumps(doc)

    for role in ("lead", "evaluator", "builder", "repair", "scout"):
        agent_path = run_dir / "opencode" / "agent" / f"trio-{role}.md"
        assert agent_path.is_file()
        text = agent_path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        assert fm["model"] == cfg.model_for(role)
        if role == "scout":
            assert fm["mode"] == "subagent"
            assert fm["hidden"] is True
        else:
            assert fm["mode"] == "primary"
        # body: non-empty, frontmatter-free, carries the driver note
        body = text.split("\n---\n", 1)[1]
        assert body.strip() != ""
        assert not body.lstrip().startswith("---")
        assert "driver" in body
        assert "opencode run" in body

    assert env["OPENCODE_CONFIG"] == str(opencode_json_path)
    assert env["OPENCODE_CONFIG_DIR"] == str(run_dir / "opencode")
    for flag in (
        "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_SHARE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS", "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    ):
        assert env[flag] == "1"
    for xdg_var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(env[xdg_var]).is_dir()
        assert str(run_dir) in env[xdg_var]

    # never includes the key itself anywhere
    dump = json.dumps(env) + opencode_json_path.read_text(encoding="utf-8")
    assert "OPENCODE_API_KEY" not in dump or env.get("OPENCODE_API_KEY") is None


def test_generate_cache_dir_override_is_used_and_not_created_under_run_dir(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    mailbox = tmp_path / "mailbox"
    shared_cache = tmp_path / "shared-cache"

    env = ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=mailbox, cache_dir=shared_cache)

    assert env["XDG_CACHE_HOME"] == str(shared_cache)
    assert shared_cache.is_dir()


def test_generate_agent_bodies_are_nonempty_and_frontmatter_free_from_source():
    """Each generated body is the role's source agent body (frontmatter
    stripped) preceded by the driver note -- never empty, never still
    carrying a leading '---' block. lead/evaluator/builder/repair come from
    opencode-driver/agents/trio-<role>.md; scout from
    opencode/agents/trio-scout.md."""
    cfg = _cfg()
    for role in ("lead", "evaluator", "builder", "repair", "scout"):
        description, body = ocgen._load_role_body(REPO_ROOT, role)
        assert description
        assert body.strip() != ""
        assert not body.lstrip().startswith("---")


def test_generated_role_bodies_use_standalone_driver_not_plugin_rules(tmp_path: Path):
    """lead/evaluator/builder/repair are generated from this driver's own
    opencode-driver/agents/ bodies, never the in-OpenCode plugin's
    opencode/agents/ bodies: the plugin's rigid bash-allowlist language and
    its "never commit" rule must not leak into the standalone driver's
    generated agents, and the evaluator's new goal-derived rigor section
    must be present. Scout has no canonical body of its own and still comes
    from opencode/agents/trio-scout.md."""
    cfg = _cfg()
    run_dir = tmp_path / "run"
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=mailbox, style="v1")

    for role in ("lead", "evaluator", "builder", "repair"):
        text = (run_dir / "opencode" / "agent" / f"trio-{role}.md").read_text(encoding="utf-8")
        assert "Every other Bash command is denied" not in text, role
        assert "Never commit or push" not in text, role

    evaluator_text = (run_dir / "opencode" / "agent" / "trio-evaluator.md").read_text(encoding="utf-8")
    assert "## Closing unverified claims" in evaluator_text

    scout_description, scout_body = ocgen._load_role_body(REPO_ROOT, "scout")
    plugin_scout = (REPO_ROOT / "opencode" / "agents" / "trio-scout.md").read_text(encoding="utf-8")
    assert scout_body in plugin_scout
    assert scout_description


def test_generate_raises_ocgen_error_for_missing_source(tmp_path: Path):
    cfg = _cfg()
    fake_repo = tmp_path / "empty-repo"
    (fake_repo / "opencode" / "agents").mkdir(parents=True)
    # no trio-lead.md etc present
    with pytest.raises(ocgen.OcgenError):
        ocgen.generate(run_dir=tmp_path / "run", cfg=cfg, repo_root=fake_repo, mailbox=tmp_path / "mailbox")


def test_generate_external_directory_allows_worktree_tmp_glob_for_non_scout_roles(tmp_path: Path):
    """REVIEW-driver.md item 16: builders (and lead/evaluator/repair, which
    may also need TMPDIR-relative scratch writes) run with TMPDIR pointed at
    ``<repo>/.trio-opencode/worktrees/tmp-<exec id>``, outside their own
    working directory, so a flat ``external_directory: deny`` would reject
    any tool call that touches it. The module-level ``ocgen.PERMISSIONS``
    table (asserted flat "deny" elsewhere in this file) is the template;
    ``generate()`` overlays a repo-specific pattern map per generated agent,
    never mutating that shared table."""
    cfg = _cfg()
    run_dir = tmp_path / "run"
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()

    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=mailbox, style="v1")

    real_repo = str(REPO_ROOT.resolve())
    base = f"{real_repo}/.trio-opencode/worktrees/tmp-"
    for role in ("lead", "evaluator", "builder", "repair"):
        text = (run_dir / "opencode" / "agent" / f"trio-{role}.md").read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        ext = fm["permission"]["external_directory"]
        assert ext["*"] == "deny", role
        assert ext[f"{base}*"] == "allow", role
        assert ext[f"{base}*/**"] == "allow", role
        # never mutated the shared template
        assert ocgen.PERMISSIONS[role]["external_directory"] == "deny"

    scout_text = (run_dir / "opencode" / "agent" / "trio-scout.md").read_text(encoding="utf-8")
    scout_fm = parse_frontmatter(scout_text)
    assert scout_fm["permission"]["external_directory"] == "deny"


def test_generate_never_writes_the_api_key(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox")
    for path in run_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "OPENCODE_API_KEY" not in text


# --------------------------------------------------------------------------
# generate() — v2 (default): agents defined INLINE in opencode.json, no
# agent/*.md files, no "variant" key on any agent (variant goes on the
# runner's -m flag instead).
# --------------------------------------------------------------------------

def test_generate_v2_is_the_default_style(tmp_path: Path):
    cfg = _cfg()
    env_default = ocgen.generate(run_dir=tmp_path / "run-default", cfg=cfg,
                                 repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox")
    env_v2 = ocgen.generate(run_dir=tmp_path / "run-v2", cfg=cfg, repo_root=REPO_ROOT,
                            mailbox=tmp_path / "mailbox", style="v2")
    doc_default = json.loads((tmp_path / "run-default" / "opencode" / "opencode.json").read_text())
    doc_v2 = json.loads((tmp_path / "run-v2" / "opencode" / "opencode.json").read_text())
    assert doc_default == doc_v2
    assert set(env_default) == set(env_v2)


def test_generate_v2_writes_no_agent_md_files(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    assert not (run_dir / "opencode" / "agent").exists()


def test_generate_v2_opencode_json_has_inline_agents_disable_and_share(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))

    assert doc["autoupdate"] is False
    assert doc["share"] == "disabled"
    assert doc["model"] == cfg.model_for("lead")
    for builtin in ("build", "plan", "general", "explore"):
        assert doc["agent"][builtin]["disable"] is True

    for role in ("lead", "evaluator", "builder", "repair", "scout"):
        entry = doc["agent"][f"trio-{role}"]
        assert entry["model"] == cfg.model_for(role)
        assert "variant" not in entry, f"{role}: v2 agent entries never carry a variant key"
        assert entry["prompt"].strip() != ""
        assert "opencode run" in entry["prompt"]
        if role == "scout":
            assert entry["mode"] == "subagent"
            assert entry["hidden"] is True
        else:
            assert entry["mode"] == "primary"
        assert "permission" in entry

    dump = json.dumps(doc)
    assert "apiKey" not in dump
    assert "OPENCODE_API_KEY" not in dump
    assert not re.search(r':\s*"?ask"?,?\s*$', dump, re.MULTILINE)


def test_generate_v2_env_has_project_disable_and_no_share_flag(tmp_path: Path):
    cfg = _cfg()
    run_dir = tmp_path / "run"
    env = ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_DISABLE_SHARE" not in env  # v2 disables sharing via config, not env


# --------------------------------------------------------------------------
# container_mode (Container / no-time-limit mode): external_directory
# relaxed to "allow" for every role, bash deny-list swapped for the
# container-safe one, scout's edit/bash restrictions unchanged.
# --------------------------------------------------------------------------

def test_container_mode_default_off_behaves_like_before(tmp_path: Path):
    cfg = _cfg(container_mode=False)
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    for role in ("lead", "evaluator", "builder", "repair"):
        ext = doc["agent"][f"trio-{role}"]["permission"]["external_directory"]
        assert isinstance(ext, dict) and ext["*"] == "deny"
    assert doc["agent"]["trio-scout"]["permission"]["external_directory"] == "deny"


def test_container_mode_external_directory_allow_for_every_role(tmp_path: Path):
    cfg = _cfg(container_mode=True)
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    for role in ("lead", "evaluator", "builder", "repair", "scout"):
        entry = doc["agent"][f"trio-{role}"]
        assert entry["permission"]["external_directory"] == "allow", role
    # the shared template is never mutated
    for role in ocgen.PERMISSIONS:
        assert ocgen.PERMISSIONS[role]["external_directory"] == "deny"


def test_container_mode_scout_edit_and_bash_unchanged(tmp_path: Path):
    cfg = _cfg(container_mode=True)
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    scout_perm = doc["agent"]["trio-scout"]["permission"]
    assert scout_perm["edit"] == "deny"
    assert scout_perm["bash"] == ocgen._SCOUT_BASH
    assert scout_perm["external_directory"] == "allow"


def test_container_mode_bash_deny_list_contents(tmp_path: Path):
    cfg = _cfg(container_mode=True)
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    for role in ("lead", "evaluator", "builder", "repair"):
        bash = doc["agent"][f"trio-{role}"]["permission"]["bash"]
        keys = list(bash.keys())
        assert keys[0] == "*"
        assert bash["*"] == "allow"
        # the over-broad glob is gone, replaced by exact-ish root-wipe entries
        assert "rm -rf /*" not in bash
        assert bash["rm -rf /"] == "deny"
        assert bash["rm -rf / *"] == "deny"
        # non-root rm -rf is no longer denied by a root glob
        assert "rm -rf /app/build" not in bash
        for pattern in ("rm -rf ~*", "rm -rf ..*", "rm -rf $HOME*", "sudo *",
                       "curl *|*sh*", "wget *|*sh*", "opencode*", "chmod -R 777*",
                       "git push*", "git reset --hard*", "git worktree remove*",
                       "git branch -D*", "git branch -d*", "git update-ref*",
                       "git config --global*", "git checkout -f*"):
            assert bash[pattern] == "deny", pattern
        # new remote/network-destructive denies
        for pattern in ("git remote add*", "git remote set-url*", "scp *",
                       "rsync *:*", "ssh *", "nc *", "ncat *"):
            assert bash[pattern] == "deny", pattern


def test_container_mode_evaluator_keeps_worktree_add_detach_allow(tmp_path: Path):
    cfg = _cfg(container_mode=True)
    run_dir = tmp_path / "run"
    ocgen.generate(run_dir=run_dir, cfg=cfg, repo_root=REPO_ROOT, mailbox=tmp_path / "mailbox", style="v2")
    doc = json.loads((run_dir / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    bash = doc["agent"]["trio-evaluator"]["permission"]["bash"]
    keys = list(bash.keys())
    assert bash["git worktree add --detach*"] == "allow"
    assert keys.index("git worktree add --detach*") > keys.index("git worktree remove*")


def test_container_mode_never_mutates_shared_bash_deny_tuple():
    # generating with container_mode=True must never mutate the module-level
    # non-container deny tuple/table used by every other (default) run.
    before = dict(ocgen.PERMISSIONS["builder"]["bash"])
    cfg = _cfg(container_mode=True)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ocgen.generate(run_dir=Path(td) / "run", cfg=cfg, repo_root=REPO_ROOT,
                       mailbox=Path(td) / "mailbox", style="v2")
    assert ocgen.PERMISSIONS["builder"]["bash"] == before
    assert "rm -rf /*" in ocgen.PERMISSIONS["builder"]["bash"]


def test_v2_agent_entry_carries_variant_when_configured():
    cfg = _cfg()
    cfg.variants.update({"lead": "max", "scout": "max", "builder": None})
    data = ocgen._build_opencode_json_v2(cfg, REPO_ROOT)
    agents = data["agent"]
    lead = next(v for k, v in agents.items() if k.endswith("lead"))
    scout = next(v for k, v in agents.items() if k.endswith("scout"))
    builder = next(v for k, v in agents.items() if k.endswith("builder"))
    assert lead["variant"] == "max"
    assert scout["variant"] == "max"
    assert "variant" not in builder
