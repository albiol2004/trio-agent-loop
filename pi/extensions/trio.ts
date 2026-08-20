import { mkdir, open, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import {
  createAgentSession,
  DefaultResourceLoader,
  SessionManager,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";

const READ_TOOLS = ["read", "grep", "find", "ls"];
const WRITE_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"];
// trio-productionize-dispatch:start
const PI_PRODUCTIONIZE_DISPATCH = `## Pi dispatch

- \`scout\` (probe batches): use this extension's existing \`runRole\`
  agent-session mechanism with the read-only Scout role and \`READ_TOOLS\`.
- \`assessor:<tier>\` (judgment batches): use the same \`runRole\`
  mechanism with the Lead-equivalent assessor role at the requested tier.
- \`user\` nodes: ask the user directly through the Pi UI before recording
  the decision as evidence.
- Delivery first: every agent writes
  \`pz-run/results/<batch-stem>.json\` before replying; the orchestrator
  records verdicts from that file.
`;
// trio-productionize-dispatch:end

async function exists(path: string): Promise<boolean> {
  try { await readFile(path); return true; } catch { return false; }
}

async function runRole(
  cwd: string,
  model: any,
  systemPrompt: string,
  task: string,
  tools: string[],
): Promise<string> {
  const loader = new DefaultResourceLoader({
    cwd,
    systemPromptOverride: () => systemPrompt,
  });
  await loader.reload();
  const { session } = await createAgentSession({
    cwd,
    model,
    thinkingLevel: "high",
    tools,
    resourceLoader: loader,
    sessionManager: SessionManager.inMemory(cwd),
  });
  let output = "";
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
      output += event.assistantMessageEvent.delta;
    }
  });
  try {
    await session.prompt(task);
    return output.trim();
  } finally {
    unsubscribe();
    session.dispose();
  }
}

export default function trioExtension(pi: ExtensionAPI) {
  pi.registerCommand("trio", {
    description: "Run the native in-process Trio Lead/Evaluator loop",
    handler: async (args, ctx) => {
      if (!ctx.model) {
        ctx.ui.notify("Select an authenticated model before /trio.", "error");
        return;
      }

      const loop = join(ctx.cwd, "loop");
      const lock = join(loop, ".native-pi-lock");
      await mkdir(loop, { recursive: true });
      let lockHandle: Awaited<ReturnType<typeof open>> | undefined;
      try {
        lockHandle = await open(lock, "wx");
      } catch {
        ctx.ui.notify("Another Trio run owns loop/.", "error");
        return;
      }

      try {
        const goalPath = join(loop, "GOAL.md");
        if (!(await exists(goalPath))) {
          if (!args.trim()) {
            ctx.ui.notify("Use /trio <goal> for a new mailbox.", "error");
            return;
          }
          await writeFile(goalPath, `# Goal\nprofile: software\n${args.trim()}\n`);
        }
        const statePath = join(loop, "STATE.md");
        if (!(await exists(statePath))) {
          await writeFile(statePath, "iteration: 0\nmax_iterations: 10\nstatus: ready\n");
        }
        for (const name of ["PLAN.md", "REPORT.md", "VERDICT.md", "LOG.md"]) {
          const path = join(loop, name);
          if (!(await exists(path))) await writeFile(path, name === "LOG.md" ? "# Trio loop log\n" : "");
        }

        let state = await readFile(statePath, "utf8");
        let iteration = Number(state.match(/^iteration:\s*(\d+)/m)?.[1] ?? 0);
        const max = Number(state.match(/^max_iterations:\s*(\d+)/m)?.[1] ?? 10);

        // Consecutive builder-direct repair counter (driver-internal; mirrors
        // portable/driver.sh's .repairs file so a resume never exceeds the
        // 2-repair cap; reset to 0 on every full Lead pass).
        let repairCount = 0;
        try {
          repairCount = Number((await readFile(join(loop, ".repairs"), "utf8")).trim()) || 0;
        } catch { /* fresh mailbox */ }
        let repairPending = false;  // next iteration's lead slot runs a repair pass

        while (iteration < max) {
          iteration += 1;
          state = state.replace(/^iteration:.*$/m, `iteration: ${iteration}`)
            .replace(/^status:.*$/m, "status: running");
          await writeFile(statePath, state);
          if (repairPending) {
            // Builder-direct repair pass for VERDICT: ITERATE scope=local:<paths>.
            ctx.ui.notify(`Trio iteration ${iteration}: repair`, "info");
            await runRole(ctx.cwd, ctx.model,
              "You are the Repair pass for a scoped ITERATE verdict. Read loop/VERDICT.md and fix exactly the failure scope it names (the scope=local paths) with the smallest correct diff: no re-planning, no refactoring, no scope expansion. Do not touch loop/ files except appending one line to loop/LOG.md. Never commit.",
              `Repair iteration ${iteration}: apply the scoped fix from loop/VERDICT.md (scope=local:<paths>).`,
              WRITE_TOOLS);
            repairPending = false;
          } else {
          ctx.ui.notify(`Trio iteration ${iteration}: scout`, "info");

          const scout = await runRole(ctx.cwd, ctx.model,
            "You are a read-only Scout. Research only; return dense file/line evidence and never modify files.",
            `Read loop/GOAL.md, STATE.md, the previous verdict, and relevant code. Brief the Lead for iteration ${iteration}.`,
            READ_TOOLS);

          const initialLeadPrompt = "You are the Lead on the initial planning pass. Own architecture and PLAN.md, but do not edit product code. Before implementation, declare the iteration's `## Verification standard` in PLAN.md: the per-criterion evidence that will count as verified, plus the mode (test-first | implement-then-smoke | human-gate), folding in GOAL.md's verification floor if present. Every code-changing increment must set BUILDER_TASK.md to DELEGATE: YES with one well-specified main implementation task, approach, owned files, done-check, and forbidden scope. Use DELEGATE: NO with a reason only for SHIP/BLOCKED or a no-code increment. Never commit.";
          await writeFile(join(loop, "BUILDER_TASK.md"), "DELEGATE: NO\n");
          await runRole(ctx.cwd, ctx.model,
            initialLeadPrompt,
            `Run iteration ${iteration} for loop/GOAL.md. Scout evidence:\n${scout}`,
            WRITE_TOOLS);

          let builderTask = await readFile(join(loop, "BUILDER_TASK.md"), "utf8");
          let delegates = /^DELEGATE: YES\s*\n\S/m.test(builderTask);
          let declines = /^DELEGATE: NO\s*\n\S/m.test(builderTask);
          if (!delegates && !declines) {
            await runRole(ctx.cwd, ctx.model,
              initialLeadPrompt,
              `Retry iteration ${iteration}: BUILDER_TASK.md was malformed or lacked a reason. Correct that role-contract breach without editing product code. Scout evidence:\n${scout}`,
              WRITE_TOOLS);
            builderTask = await readFile(join(loop, "BUILDER_TASK.md"), "utf8");
            delegates = /^DELEGATE: YES\s*\n\S/m.test(builderTask);
            declines = /^DELEGATE: NO\s*\n\S/m.test(builderTask);
          }
          if (!delegates && !declines) {
            state = (await readFile(statePath, "utf8")).replace(/^status:.*$/m, "status: error");
            await writeFile(statePath, state);
            throw new Error("Lead twice wrote a malformed or unexplained BUILDER_TASK.md decision");
          }
          if (delegates) {
            const builder = await runRole(ctx.cwd, ctx.model,
              "You are the primary Builder. Perform the main implementation pass in BUILDER_TASK.md, including substantive logic, tests, and integration work. Follow the Lead's approach and repository patterns, touch only owned files, never edit loop/, and stop if architectural intent is ambiguous.",
              builderTask,
              WRITE_TOOLS);
            const reviewPrompt = "You are the Lead returning for final review. Inspect and own the complete Builder diff, make only necessary corrective edits, rerun checks, and update REPORT.md. Include an Implementation provenance section separating primary Builder work from Lead corrections. Do not replace the main Builder pass with a rewrite. Never commit.";
            await runRole(ctx.cwd, ctx.model, reviewPrompt,
              `Review iteration ${iteration}. Builder report:\n${builder}`, WRITE_TOOLS);
            let report = await readFile(join(loop, "REPORT.md"), "utf8");
            if (!/^## Implementation provenance\s*$/m.test(report)) {
              await runRole(ctx.cwd, ctx.model, reviewPrompt,
                `Retry the iteration ${iteration} review: REPORT.md omitted mandatory implementation provenance. Builder report:\n${builder}`,
                WRITE_TOOLS);
              report = await readFile(join(loop, "REPORT.md"), "utf8");
            }
            if (!/^## Implementation provenance\s*$/m.test(report)) {
              state = (await readFile(statePath, "utf8")).replace(/^status:.*$/m, "status: error");
              await writeFile(statePath, state);
              throw new Error("Lead twice omitted mandatory Builder implementation provenance");
            }
          }
          }  // end full Lead pass (else of repairPending)

          const evalScout = await runRole(ctx.cwd, ctx.model,
            "You are a read-only evaluator Scout. Inspect GOAL.md, PLAN.md and the diff. Do not read REPORT.md and do not issue a verdict.",
            `Find blast radius, test-integrity risks, edge cases, and API/version concerns for iteration ${iteration}.`,
            READ_TOOLS);
          await runRole(ctx.cwd, ctx.model,
            "You are the independent Evaluator. Never fix code. Verify the goal and PLAN.md yourself before reading REPORT.md. The first line of VERDICT.md must be VERDICT: SHIP, VERDICT: ITERATE (optionally with scope=design or scope=local:<comma-separated-paths>), VERDICT: NEEDS_HUMAN, or VERDICT: BLOCKED. Emit scope=local ONLY when the failure is provably local (single file or listed files, no API/contract change); use plain ITERATE or scope=design otherwise. Emit NEEDS_HUMAN when every agent-verifiable criterion passes but PLAN.md criteria tagged `verify: human` remain — then the `## Human check` section with exact steps is mandatory. Check the produced evidence against the `## Verification standard` declared in PLAN.md; insufficient evidence is an ITERATE with the evidence gap as the failure scope.",
            `Evaluate iteration ${iteration}. Scout evidence to verify:\n${evalScout}`,
            WRITE_TOOLS);

          const verdictLine = (await readFile(join(loop, "VERDICT.md"), "utf8")).split(/\r?\n/, 1)[0];
          if (!/^VERDICT:\s/.test(verdictLine)) throw new Error(`Malformed verdict: ${verdictLine}`);
          // Split the first line into the verdict word and an optional scope=
          // suffix so scoped ITERATE and NEEDS_HUMAN route like portable/driver.sh.
          const verdictRest = verdictLine.replace(/^VERDICT:\s*/, "");
          const verdictWord = verdictRest.split(/\s+/, 1)[0];
          const scope = verdictRest.slice(verdictWord.length).trim();
          state = (await readFile(statePath, "utf8")).replace(/^status:.*$/m, `status: ${verdictWord.toLowerCase()}`);
          await writeFile(statePath, state);
          if (verdictWord === "SHIP" || verdictWord === "BLOCKED" || verdictWord === "NEEDS_HUMAN") {
            ctx.ui.notify(verdictLine, verdictWord === "SHIP" ? "info" : "warning");
            return;
          }
          if (verdictWord !== "ITERATE") throw new Error(`Malformed verdict: ${verdictLine}`);
          if (scope === "" || scope === "scope=design") {
            // Full Lead iteration (implicit or explicit): repair streak resets.
            repairCount = 0;
            repairPending = false;
            await writeFile(join(loop, ".repairs"), "0\n");
          } else if (scope.startsWith("scope=local:")) {
            if (repairCount < 2) {
              repairCount += 1;
              await writeFile(join(loop, ".repairs"), `${repairCount}\n`);
              repairPending = true;
            } else {
              // Repair cap hit: force a full Lead iteration and reset.
              repairCount = 0;
              repairPending = false;
              await writeFile(join(loop, ".repairs"), "0\n");
            }
          } else {
            throw new Error(`Malformed verdict: ${verdictLine}`);
          }
        }
        ctx.ui.notify(`Trio reached max_iterations (${max}).`, "warning");
      } finally {
        if (lockHandle) {
          await lockHandle.close();
        }
        await rm(lock, { force: true });
      }
    },
  });
  pi.registerCommand("trio-productionize", {
    description: "Run the production-readiness audit",
    handler: async (args, ctx) => {
      const pzHome = process.env.TRIO_PZ_HOME
        || join(homedir(), ".local/share/trio-agent-loop/productionize");
      const commandPath = join(pzHome, "command.md");
      if (!(await exists(commandPath))) {
        ctx.ui.notify(
          "Productionize assets are not installed. From the agent-trio-template repo run: ./install.sh --productionize (or any harness install flag, which also installs them).",
          "error",
        );
        return;
      }

      const command = await readFile(commandPath, "utf8");
      const argumentsSection = args.trim()
        ? `\n\n## User arguments\n\n${args.trim()}\n`
        : "";
      pi.sendUserMessage(`${command.trim()}\n\n${PI_PRODUCTIONIZE_DISPATCH}${argumentsSection}`);
    },
  });
}

// trio-protocol:start
// ## Trio protocol essentials
// 
// - Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
// - `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
// - `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
// - Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
// trio-protocol:end
