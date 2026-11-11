// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { PythonBridge } = require("../lib/bridge.cjs");
test("real stdio bridge connects, observes, executes and disconnects a simulation", async (t) => {
  const bridge = new PythonBridge({ root: path.resolve(__dirname, "../..") });
  t.after(() => bridge.close());
  const phases = [];
  bridge.on("event", (message) => {
    if (message.event === "phase") phases.push(message.data.phase);
  });
  const connection = await bridge.request("connect", { mode: "simulation" });
  assert.equal(connection.mode, "simulation");
  const report = await bridge.request("run", {
    goal: "click Export",
    config: { provider: "offline" },
  });
  assert.equal(report.status, "done");
  assert.equal(report.outcomes[0].intent.action, "click");
  assert.ok(phases.includes("planning"));
  assert.ok(phases.includes("moving"));
  await bridge.request("disconnect");
});
