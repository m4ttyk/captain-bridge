import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";


type AgentEndEvent = {
  willContinue?: boolean;
  messages?: unknown[];
  message?: unknown;
  lastMessage?: unknown;
};

function assistantText(message: unknown): string | undefined {
  if (!message || typeof message !== "object") return undefined;
  const value = message as Record<string, unknown>;
  if (value.role !== "assistant" && value.type !== "assistant") return undefined;
  if (typeof value.text === "string") return value.text.trim();
  if (typeof value.content === "string") return value.content.trim();
  if (!Array.isArray(value.content)) return undefined;
  const text = value.content
    .filter(
      (part): part is Record<string, unknown> =>
        !!part && typeof part === "object" && part.type === "text" && typeof part.text === "string",
    )
    .map((part) => part.text as string)
    .join("\n")
    .trim();
  return text || undefined;
}

function latestAssistantText(event: unknown): string {
  if (!event || typeof event !== "object") return "";
  const value = event as AgentEndEvent;
  const messages = Array.isArray(value.messages)
    ? value.messages
    : value.message !== undefined
      ? [value.message]
      : value.lastMessage !== undefined
        ? [value.lastMessage]
        : [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const text = assistantText(messages[index]);
    if (text !== undefined) return text;
  }
  return "";
}

type Binding = {
  ship: string;
  assignment: string;
  officer: string;
  sessionId: string;
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
  if (assignment === undefined) return;
  const officer = process.env.CAPTAIN_BRIDGE_OFFICER;
  let binding: Binding | undefined;

  function notify(ctx: ExtensionContext, message: string) {
    ctx.ui.notify(`Captain Bridge: ${message}`, "warning");
  }

  async function emit(
    kind: EventKind,
    ctx: ExtensionContext,
    details: Record<string, string> = {},
  ): Promise<boolean> {
    if (!binding) return false;

    try {
      const args = [
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
      ];
      for (const [name, value] of Object.entries(details)) args.push(`--${name}`, value);
      const result = await pi.exec("captain", args, { timeout: 10_000 });
      if (result.code !== 0) {
        const detail = result.stderr.trim();
        notify(ctx, `${kind} was not recorded${detail ? `: ${detail}` : ` (exit ${result.code})`}`);
        return false;
      }
      if (kind === "result-ready") {
        if (typeof result.stdout !== "string" || !result.stdout.trim()) {
          notify(ctx, "result-ready was recorded but Officer nudge status was unavailable");
          return false;
        }
        try {
          const payload = JSON.parse(result.stdout) as { officerWoken?: boolean; wakeError?: string };
          if (payload.officerWoken !== true) {
            notify(ctx, payload.wakeError || "result-ready was recorded but Officer nudge was not delivered");
            return false;
          }
        } catch {
          notify(ctx, "result-ready was recorded but Officer nudge status was unavailable");
          return false;
        }
      }
      return true;
    } catch (error) {
      notify(ctx, `${kind} was not recorded: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (!assignment || !officer) {
      notify(ctx, "CAPTAIN_BRIDGE_ASSIGNMENT and CAPTAIN_BRIDGE_OFFICER must be set for a worker binding");
      return;
    }

    binding = {
      ship,
      assignment,
      officer,
      sessionId: ctx.sessionManager.getSessionId(),
    };

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
  pi.on("agent_end", async (event: AgentEndEvent, ctx) => {
    if (!binding || event.willContinue === true) return;
    await emit("agent-settled", ctx);
    await emit("result-ready", ctx, { response: latestAssistantText(event) });
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    await emit("session-shutdown", ctx);
  });
}
