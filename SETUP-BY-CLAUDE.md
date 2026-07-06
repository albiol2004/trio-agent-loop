# Setup instructions for a Claude session

You (Claude Code / Claude app with shell access) have been given this repo to
install the "trio" duo agent loop on the local machine. Do this:

1. Clone or you already have this repo checked out. From its root, run:
   `./install.sh --global`
   This copies `.claude/agents/trio-*.md` → `~/.claude/agents/` and
   `.claude/skills/{trio,trio-init}` → `~/.claude/skills/`, making the
   `/trio` and `/trio-init` commands and the trio-lead / trio-evaluator /
   trio-scout / trio-builder agents available in **every** project on the
   machine. (Use `./install.sh /path/to/project` instead to install into a
   single project's `.claude/`, committed with that repo.)
2. Verify: the files exist under `~/.claude/agents/` and `~/.claude/skills/`.
   New sessions pick them up automatically; the current session may need a
   restart to see new skills.
3. Tell the user how to use it, briefly:
   - `/trio-init <goal>` in any project → creates the `loop/` mailbox
     (GOAL/PLAN/REPORT/VERDICT/STATE/LOG.md). GOAL.md carries
     `profile: software | data` — data profile switches the Evaluator to
     data ground truth (reconciliation, integrity, reproducibility).
   - `/trio` → ONE supervised iteration (recommended first run).
   - `/loop /trio` → autonomous; iterates until the Evaluator's VERDICT.md
     first line reads SHIP or BLOCKED; Esc pauses; `max_iterations` in
     loop/STATE.md (default 10) is the budget cap.
4. Do NOT modify the role files during install. Model tiers are set in the
   agent frontmatter (`model: opus` for lead/evaluator, `sonnet` for
   workers); the README's Tuning section explains when to change them.

Design rationale and research citations live in README.md — read it if the
user asks why the loop is shaped this way.
