import { mkdir, open, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import {
  createAgentSession,
  DefaultResourceLoader,
  SessionManager,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";

const READ_TOOLS = ["read", "grep", "find", "ls"];
const WRITE_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"];

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

        while (iteration < max) {
          iteration += 1;
          state = state.replace(/^iteration:.*$/m, `iteration: ${iteration}`)
            .replace(/^status:.*$/m, "status: running");
          await writeFile(statePath, state);
          ctx.ui.notify(`Trio iteration ${iteration}: scout`, "info");

          const scout = await runRole(ctx.cwd, ctx.model,
            "You are a read-only Scout. Research only; return dense file/line evidence and never modify files.",
            `Read loop/GOAL.md, STATE.md, the previous verdict, and relevant code. Brief the Lead for iteration ${iteration}.`,
            READ_TOOLS);

          await writeFile(join(loop, "BUILDER_TASK.md"), "DELEGATE: NO\n");
          await runRole(ctx.cwd, ctx.model,
            "You are the Lead. Own planning, judgment-heavy implementation, tests, PLAN.md and REPORT.md. Never commit. Write BUILDER_TASK.md as DELEGATE: NO or one bounded mechanical task.",
            `Run iteration ${iteration} for loop/GOAL.md. Scout evidence:\n${scout}`,
            WRITE_TOOLS);

          const builderTask = await readFile(join(loop, "BUILDER_TASK.md"), "utf8");
          if (builderTask.startsWith("DELEGATE: YES")) {
            const builder = await runRole(ctx.cwd, ctx.model,
              "You are a mechanical Builder. Follow BUILDER_TASK.md exactly, touch only owned files, never edit loop/, and never make design decisions.",
              builderTask,
              WRITE_TOOLS);
            await runRole(ctx.cwd, ctx.model,
              "You are the Lead returning for final review. Own and correct the Builder diff, rerun checks, and update REPORT.md. Never commit.",
              `Review iteration ${iteration}. Builder report:\n${builder}`,
              WRITE_TOOLS);
          }

          const evalScout = await runRole(ctx.cwd, ctx.model,
            "You are a read-only evaluator Scout. Inspect GOAL.md, PLAN.md and the diff. Do not read REPORT.md and do not issue a verdict.",
            `Find blast radius, test-integrity risks, edge cases, and API/version concerns for iteration ${iteration}.`,
            READ_TOOLS);
          await runRole(ctx.cwd, ctx.model,
            "You are the independent Evaluator. Never fix code. Verify the goal and PLAN.md yourself before reading REPORT.md. Write VERDICT.md with SHIP, ITERATE, or BLOCKED on line one.",
            `Evaluate iteration ${iteration}. Scout evidence to verify:\n${evalScout}`,
            WRITE_TOOLS);

          const verdict = (await readFile(join(loop, "VERDICT.md"), "utf8")).split(/\r?\n/, 1)[0];
          state = (await readFile(statePath, "utf8")).replace(/^status:.*$/m, `status: ${verdict.replace("VERDICT: ", "").toLowerCase()}`);
          await writeFile(statePath, state);
          if (verdict === "VERDICT: SHIP" || verdict === "VERDICT: BLOCKED") {
            ctx.ui.notify(verdict, verdict.endsWith("SHIP") ? "info" : "warning");
            return;
          }
          if (verdict !== "VERDICT: ITERATE") throw new Error(`Malformed verdict: ${verdict}`);
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
}
