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
    clearTimeout() {},
    setTimeout: () => 1,
    performance: { now: () => 100 },
    AirKeyboard: { gesture: () => "scroll" },
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

test("two-hand inference preserves the pointer hand and drops samples after Stop", async () => {
  const control = camera(async () => {});
  const points = Array.from({ length: 21 }, (_, i) => ({
    x: 0.3 + i / 100,
    y: 0.5,
    z: 0,
  }));
  points[0] = { x: 0.3, y: 0.8 };
  points[9] = { x: 0.3, y: 0.5 };
  const second = points.map((point) => ({ ...point, x: point.x + 0.2 }));
  control.video = {
    readyState: 2,
    currentTime: 1,
    videoWidth: 640,
    videoHeight: 480,
  };
  control.active = true;
  control.draw = () => {};
  let latest,
    received = [],
    release;
  control.onSample = (sample) => received.push(sample);
  control.detector = {
    detectForVideo: () => ({
      landmarks: [points, second],
      handedness: [[{ categoryName: "Left" }], [{ categoryName: "Right" }]],
    }),
  };
  control.onLandmarks = (hands) => {
    latest = hands;
    return new Promise((done) => {
      release = done;
    });
  };
  control.track();
  assert.equal(latest.length, 2);
  control.stop();
  release();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(received, []);
  control.active = true;
  control.video.currentTime = 2;
  control.onLandmarks = (hands) => {
    latest = hands;
  };
  control.detector = {
    detectForVideo: () => ({
      landmarks: [second, points],
      handedness: [[{ categoryName: "Right" }], [{ categoryName: "Left" }]],
    }),
  };
  control.track();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(control.primaryHand, "Left");
  assert.equal(received[0].gesture, "scroll");
  assert.ok(Math.abs(received[0].x - (1 - points[8].x)) < 1e-8);
});
