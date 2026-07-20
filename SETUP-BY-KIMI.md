# Trio setup for Kimi Code

This guide installs the Trio mailbox skills for Kimi Code from a fresh clone.
It uses the official Kimi Code CLI and an honest sequential fallback. Kimi's
current documentation describes the fixed built-in `coder`, `explore`, and
`plan` sub-agents plus automatic dispatch, but does not document custom role
names or per-role model pinning. This is an inference from that documented
surface, not a claim that Kimi has no sub-agents. The bundled runner therefore
starts one fresh Kimi print-mode process per role and preserves the Lead →
Builder → Lead review → independent Evaluator contract.

## 1. Install Kimi Code

On macOS or Linux, run the official installer in a terminal:

```sh
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
kimi --version
```

The installer and platform alternatives are documented in the official
[Kimi Code getting-started guide](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html).
Start `kimi`, then enter `/login` and complete the provider setup. The same
guide documents the browser-based Kimi Code login flow.

Validate a non-interactive request after login (this reaches the Kimi service):

```sh
kimi -m kimi-code/k3 -p "Reply with exactly: Kimi Trio smoke test passed"
```

The `-p` print-mode and `-m` model-selection forms are the documented CLI
surface in the [Kimi command reference](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html).
The [configuration reference](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html)
defines the aliases `kimi-code/k3` and `kimi-code/kimi-for-coding`.

## 2. Install Trio's Kimi skills

From this repository clone:

```sh
./install.sh --kimi
```

The installer copies both skills and the runner to
`${KIMI_CODE_HOME:-$HOME/.kimi-code}/skills/`. Kimi Code discovers user skills
there; the [Agent Skills documentation](https://moonshotai.github.io/kimi-code/en/customization/skills)
describes the directory-form `SKILL.md` layout and `/skill:<name>` invocation.
Start a new `kimi` session after installing, then confirm `trio` and `trio-init`
appear in the available skills.

To keep Kimi's data and skills in a separate location, set
`KIMI_CODE_HOME=/path/to/kimi-code` before running the installer. This variable
is documented in the [Kimi data-locations guide](https://moonshotai.github.io/kimi-code/en/configuration/data-locations.html).

## 3. Initialize and run a mailbox

In the project root, start Kimi and initialize a mailbox:

```text
/skill:trio-init Add a bounded, tested change to the project
```

Then run one supervised iteration:

```text
/skill:trio
```

The Kimi skill creates short context files inside the mailbox and invokes the
installed runner sequentially:

```sh
"${KIMI_CODE_HOME:-$HOME/.kimi-code}/skills/trio/scripts/run-role.sh" \
  scout loop/scout-context.md loop/scout-result.md .
```

The accepted roles are `scout`, `lead`, `builder`, and `evaluator`; the runner
syntax is:

```text
run-role.sh ROLE CONTEXT_FILE RESULT_FILE [PROJECT_ROOT]
```

Role/model mapping is fixed to source-documented aliases:

| Trio role | Kimi model alias |
|---|---|
| Lead | `kimi-code/k3` |
| Evaluator | `kimi-code/k3` |
| Scout | Kimi K2.7 Code (`kimi-code/kimi-for-coding`) |
| Builder | Kimi K2.7 Code (`kimi-code/kimi-for-coding`) |

The runner is deliberately sequential and portable. It does not claim native
custom-role orchestration or an autonomous Trio loop; continue iterations only
after inspecting `VERDICT.md`, and stop on `SHIP` or `BLOCKED`.

> Safety: Kimi's documented `-p` mode runs regular tool calls under its `auto`
> permission policy. The Builder and post-Builder Lead can therefore modify the
> selected project without an interactive approval prompt. Use this fallback
> only in a trusted working directory and review the mailbox and diff between
> iterations.

## Primary references

- [Kimi Code product and official installer](https://github.com/MoonshotAI/kimi-code)
- [Getting started and `/login`](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html)
- [CLI `-p` and `-m` options](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html)
- [Model aliases and configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html)
- [Agent Skills and discovery](https://moonshotai.github.io/kimi-code/en/customization/skills)
- [Built-in agents and sub-agents](https://moonshotai.github.io/kimi-code/en/customization/agents)
