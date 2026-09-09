import assert from "node:assert/strict";

import captainBridge from "../extensions/captain-bridge.ts";

type Event = { willContinue?: boolean; messages?: unknown[] };
type Handler = (event: Event, ctx: unknown) => Promise<void>;

const previous = {
  ship: process.env.CAPTAIN_BRIDGE_SHIP,
  assignment: process.env.CAPTAIN_BRIDGE_ASSIGNMENT,
  officer: process.env.CAPTAIN_BRIDGE_OFFICER,
};
process.env.CAPTAIN_BRIDGE_SHIP = "/tmp/captain-bridge-extension-ship";
process.env.CAPTAIN_BRIDGE_ASSIGNMENT = "assignment_test";
process.env.CAPTAIN_BRIDGE_OFFICER = "officer";

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
  const officerOnlyHandlers: Record<string, Handler> = {};
  const officerOnlyPi = {
    on(event: string, handler: Handler) {
      officerOnlyHandlers[event] = handler;
    },
    appendEntry() {},
  };
  captainBridge(officerOnlyPi as never);
  assert.equal(officerOnlyHandlers.session_start, undefined);
} finally {
  for (const [name, value] of Object.entries(previous)) {
    if (value === undefined) delete process.env[`CAPTAIN_BRIDGE_${name.toUpperCase()}`];
    else process.env[`CAPTAIN_BRIDGE_${name.toUpperCase()}`] = value;
  }
}

console.log("captain bridge extension lifecycle check passed");
