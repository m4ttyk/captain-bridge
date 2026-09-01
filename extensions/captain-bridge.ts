import { stat } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Binding = {
  ship: string;
  assignment: string;
  officer: string;
  sessionId: string;
  boundAt: string;
};

type EventKind =
  | "session-started"
  | "agent-started"
  | "agent-settled"
  | "session-shutdown"
  | "result-ready";

export default function captainBridge(pi: ExtensionAPI) {
  const ship = process.env.CAPTAIN_BRIDGE_SHIP;
  if (!ship) return;

  const assignment = process.env.CAPTAIN_BRIDGE_ASSIGNMENT;
  const officer = process.env.CAPTAIN_BRIDGE_OFFICER;
  let binding: Binding | undefined;
  let resultReadyEmitted = false;
  let resultReadyPending = false;

  function notify(ctx: ExtensionContext, message: string) {
    ctx.ui.notify(`Captain Bridge: ${message}`, "warning");
  }

  async function emit(kind: EventKind, ctx: ExtensionContext): Promise<boolean> {
    if (!binding) return false;

    try {
      const result = await pi.exec(
        "captain",
        [
          "_event",
          "emit",
          "--ship",
          binding.ship,
          "--kind",
          kind,
          "--assignment",
          binding.assignment,
          "--session-id",
          binding.sessionId,
        ],
        { timeout: 10_000 },
      );
      if (result.code !== 0) {
        const detail = result.stderr.trim();
        notify(ctx, `${kind} was not recorded${detail ? `: ${detail}` : ` (exit ${result.code})`}`);
        return false;
      }
      return true;
    } catch (error) {
      notify(ctx, `${kind} was not recorded: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (!assignment || !officer) {
      notify(ctx, "CAPTAIN_BRIDGE_ASSIGNMENT and CAPTAIN_BRIDGE_OFFICER must be set");
      return;
    }

    binding = {
      ship,
      assignment,
      officer,
      sessionId: ctx.sessionManager.getSessionId(),
      boundAt: new Date().toISOString(),
    };
    resultReadyEmitted = false;
    resultReadyPending = false;

    try {
      pi.appendEntry("captain-bridge-binding", binding);
    } catch (error) {
      notify(ctx, `session binding was not persisted: ${error instanceof Error ? error.message : String(error)}`);
    }
    await emit("session-started", ctx);
  });

  pi.on("agent_start", async (_event, ctx) => {
    await emit("agent-started", ctx);
  });

  pi.on("agent_settled", async (_event, ctx) => {
    if (!binding) return;
    await emit("agent-settled", ctx);
    if (resultReadyEmitted || resultReadyPending) return;
    resultReadyPending = true;

    const resultPath = join(binding.ship, "assignments", binding.assignment, "result.md");
    try {
      if (!(await stat(resultPath)).isFile()) {
        notify(ctx, `${resultPath} is not a file`);
        return;
      }
      if (await emit("result-ready", ctx)) {
        resultReadyEmitted = true;
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        notify(ctx, `could not check ${resultPath}: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      resultReadyPending = false;
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    await emit("session-shutdown", ctx);
  });
}
