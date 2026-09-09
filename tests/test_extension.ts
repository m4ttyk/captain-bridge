import assert from "node:assert/strict";

import captainBridge from "../extensions/captain-bridge.ts";

type Event = { willContinue?: boolean; messages?: unknown[]; toolName?: string };
type Handler = (event: Event, ctx: unknown) => Promise<unknown> | unknown;

const previous = {
  ship: process.env.CAPTAIN_BRIDGE_SHIP,
  assignment: process.env.CAPTAIN_BRIDGE_ASSIGNMENT,
  officer: process.env.CAPTAIN_BRIDGE_OFFICER,
  role: process.env.CAPTAIN_BRIDGE_ROLE,
};
process.env.CAPTAIN_BRIDGE_SHIP = "/tmp/captain-bridge-extension-ship";
process.env.CAPTAIN_BRIDGE_ASSIGNMENT = "assignment_test";
process.env.CAPTAIN_BRIDGE_OFFICER = "officer";
process.env.CAPTAIN_BRIDGE_ROLE = "worker";

try {
  const events: string[] = [];
  const responses: string[] = [];
  const notifications: string[] = [];
  const handlers: Record<string, Handler> = {};
  const pi = {
    on(event: string, handler: Handler) {
      handlers[event] = handler;
    },
    async exec(_command: string, args: string[]) {
      events.push(args[args.indexOf("--kind") + 1]);
      const responseIndex = args.indexOf("--response");
      if (responseIndex >= 0) responses.push(args[responseIndex + 1]);
      return {
        code: 0,
        stderr: "",
        stdout: JSON.stringify(args.includes("result-ready") ? { officerWoken: true } : {}),
      };
    },
    appendEntry() {},
  };
  captainBridge(pi as never);

  const context = {
    sessionManager: { getSessionId: () => "session_test" },
    ui: { notify(message: string) { notifications.push(message); } },
  };
  await handlers.session_start({}, context);
  await handlers.agent_end({ willContinue: true, messages: [{ role: "assistant", content: "continue" }] }, context);
  assert.equal(events.includes("result-ready"), false);
  assert.equal(events.includes("agent-settled"), false);

  await handlers.agent_end(
    {
      messages: [
        { role: "assistant", content: [{ type: "thinking", thinking: "hidden" }, { type: "text", text: "Done" }] },
        { role: "toolResult", content: "ignored" },
      ],
    },
    context,
  );
  assert.equal(events.filter((event) => event === "result-ready").length, 1);
  assert.deepEqual(responses, ["Done"]);

  await handlers.agent_end({ messages: [{ role: "assistant", content: [{ type: "toolCall", name: "bad" }] }] }, context);
  assert.equal(events.filter((event) => event === "result-ready").length, 2);
  assert.deepEqual(responses, ["Done", ""]);
  assert.deepEqual(notifications, []);
  assert.equal(handlers.tool_call, undefined);

  const officerTools = ["task", "eval", "vibe_spawn", "vibe_enable", "read", "bash", "hub"];
  let activeTools = [...officerTools];
  const officerHandlers: Record<string, Handler> = {};
  const officerPi = {
    on(event: string, handler: Handler) {
      officerHandlers[event] = handler;
    },
    getActiveTools() {
      return activeTools;
    },
    async setActiveTools(names: string[]) {
      activeTools = names;
    },
  };
  delete process.env.CAPTAIN_BRIDGE_ASSIGNMENT;
  process.env.CAPTAIN_BRIDGE_ROLE = "officer";
  captainBridge(officerPi as never);
  await officerHandlers.session_start({}, context);
  assert.deepEqual(activeTools, ["read", "bash", "hub"]);

  await officerPi.setActiveTools(officerTools);
  for (const toolName of ["task", "eval", "vibe_spawn", "vibe_enable"]) {
    const result = await officerHandlers.tool_call({ toolName }, context);
    assert.deepEqual(result, {
      block: true,
      reason: "Officer delegation is disabled; use the captain assignment CLI or Herdr to delegate work.",
    });
  }
  assert.equal(await officerHandlers.tool_call({ toolName: "read" }, context), undefined);

  delete process.env.CAPTAIN_BRIDGE_ROLE;
  const unmarkedHandlers: Record<string, Handler> = {};
  const unmarkedPi = {
    on(event: string, handler: Handler) {
      unmarkedHandlers[event] = handler;
    },
  };
  captainBridge(unmarkedPi as never);
  assert.equal(unmarkedHandlers.session_start, undefined);
  assert.equal(unmarkedHandlers.tool_call, undefined);

  process.env.CAPTAIN_BRIDGE_ASSIGNMENT = "assignment_worker";
  process.env.CAPTAIN_BRIDGE_ROLE = "worker";
  const markedWorkerHandlers: Record<string, Handler> = {};
  const markedWorkerPi = {
    on(event: string, handler: Handler) {
      markedWorkerHandlers[event] = handler;
    },
    async exec() {
      return { code: 0, stderr: "", stdout: "{}" };
    },
    appendEntry() {},
  };
  captainBridge(markedWorkerPi as never);
  assert.notEqual(markedWorkerHandlers.session_start, undefined);
  assert.equal(markedWorkerHandlers.tool_call, undefined);

  process.env.CAPTAIN_BRIDGE_ASSIGNMENT = "assignment_test";
  process.env.CAPTAIN_BRIDGE_ROLE = "worker";

  const failureHandlers: Record<string, Handler> = {};
  const failurePi = {
    on(event: string, handler: Handler) {
      failureHandlers[event] = handler;
    },
    async exec(_command: string, args: string[]) {
      return {
        code: 0,
        stderr: "",
        stdout: JSON.stringify(args.includes("result-ready") ? { officerWoken: false, wakeError: "offline" } : {}),
      };
    },
    appendEntry() {},
  };
  captainBridge(failurePi as never);
  await failureHandlers.session_start({}, context);
  await failureHandlers.agent_end({}, context);
  assert.equal(notifications.some((message) => message.includes("offline")), true);

  delete process.env.CAPTAIN_BRIDGE_ASSIGNMENT;
  delete process.env.CAPTAIN_BRIDGE_ROLE;
} finally {
  for (const [name, value] of Object.entries(previous)) {
    if (value === undefined) delete process.env[`CAPTAIN_BRIDGE_${name.toUpperCase()}`];
    else process.env[`CAPTAIN_BRIDGE_${name.toUpperCase()}`] = value;
  }
}

console.log("captain bridge extension lifecycle check passed");
