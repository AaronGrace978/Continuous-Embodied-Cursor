// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
function camera(getUserMedia) {
  const context = vm.createContext({
    navigator: { mediaDevices: { getUserMedia } },
    cancelAnimationFrame() {},
  });
  vm.runInContext(
    fs.readFileSync(path.resolve(__dirname, "../renderer/camera.js"), "utf8"),
    context,
  );
  const Camera = vm.runInContext("HandCamera", context);
  return new Camera({
    video: { srcObject: null },
    canvas: { width: 1, height: 1, getContext: () => ({ clearRect() {} }) },
    onSample() {},
    onStatus() {},
    onError(error) {
      throw error;
    },
  });
}
test("denied camera permission gives an actionable error and stops capture", async () => {
  const control = camera(async () => {
    throw Object.assign(new Error(), { name: "NotAllowedError" });
  });
  await assert.rejects(control.start(), /permission was denied/);
  assert.equal(control.active, false);
  assert.equal(control.stream, null);
});
test("stopping while permission is pending closes a late camera stream", async () => {
  let resolve,
    stopped = 0;
  const control = camera(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const pending = control.start();
  control.stop();
  resolve({ getTracks: () => [{ stop: () => stopped++ }] });
  assert.equal(await pending, false);
  assert.equal(stopped, 1);
  assert.equal(control.stream, null);
});
test("camera constraints request video only and closing releases every track", async () => {
  let options,
    stopped = 0;
  const control = camera(async (constraints) => {
    options = constraints;
    throw Object.assign(new Error(), { name: "NotFoundError" });
  });
  await assert.rejects(control.start(), /No camera found/);
  assert.equal(options.audio, false);
  assert.equal(options.video.frameRate.max, 30);
  control.stream = {
    getTracks: () => [{ stop: () => stopped++ }, { stop: () => stopped++ }],
  };
  control.stop();
  assert.equal(stopped, 2);
  assert.equal(control.video.srcObject, null);
});
