// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require("node:test");
const assert = require("node:assert/strict");
const { AirTyper, keys, hit, gesture } = require("../renderer/air-keyboard.js");
const { KeyboardState } = require("../lib/keyboard.cjs");
function hand(typer, id = "Left", key = "f", tip = 8, bent = false) {
  const cell = keys.find((item) => item.id === key);
  const x = 1 - typer.area.x - (cell.x + cell.w / 2) * typer.area.w;
  const y = typer.area.y + (cell.y + cell.h / 2) * typer.area.h;
  const points = Array.from({ length: 21 }, () => ({ x: 0.99, y: 0.99, z: 0 }));
  points[tip - 3] = { x, y: y + 0.12, z: 0 };
  points[tip - 2] = { x, y: y + 0.07, z: 0 };
  points[tip] = { x, y: y + (bent ? 0.13 : 0), z: 0 };
  return { id, points };
}
function arm(typer, hands) {
  for (const now of [0, 60, 120])
    assert.deepEqual(typer.update(hands, now).keys, []);
}
test("a finger taps its settled key once, despite moving across rows while bending", () => {
  const typer = new AirTyper();
  arm(typer, [hand(typer)]);
  assert.deepEqual(
    typer.update([hand(typer, "Left", "f", 8, true)], 150).keys,
    [],
  );
  assert.deepEqual(
    typer.update([hand(typer, "Left", "f", 8, true)], 200).keys,
    ["f"],
  );
  for (let now = 230; now < 500; now += 30)
    assert.deepEqual(
      typer.update([hand(typer, "Left", "f", 8, true)], now).keys,
      [],
    );
  for (const now of [510, 570, 630]) typer.update([hand(typer)], now);
  typer.update([hand(typer, "Left", "f", 8, true)], 660);
  assert.deepEqual(
    typer.update([hand(typer, "Left", "f", 8, true)], 710).keys,
    ["f"],
  );
});
test("both hands and independent fingers can type without moving the pointer", () => {
  const typer = new AirTyper();
  const open = [hand(typer, "Left", "a", 20), hand(typer, "Right", "j", 12)];
  arm(typer, open);
  const bent = [
    hand(typer, "Left", "a", 20, true),
    hand(typer, "Right", "j", 12, true),
  ];
  typer.update(bent, 150);
  assert.deepEqual(typer.update(bent, 200).keys, ["a", "j"]);
});
test("lost tracking, long frame gaps, and repeated timestamps cannot confirm a tap", () => {
  for (const loss of ["missing", "gap", "duplicate"]) {
    const typer = new AirTyper();
    arm(typer, [hand(typer)]);
    const bent = [hand(typer, "Left", "f", 8, true)];
    typer.update(bent, 150);
    if (loss === "missing") typer.update([], 180);
    if (loss === "duplicate")
      for (let i = 0; i < 20; i++) typer.update(bent, 150);
    assert.deepEqual(typer.update(bent, loss === "gap" ? 800 : 220).keys, []);
  }
});
test("key gutters and locations outside the calibrated surface do not type", () => {
  assert.equal(hit(-0.01, 0.5), null);
  assert.equal(hit(0.5, 1.01), null);
  assert.equal(hit(keys[12].x, keys[12].y), null);
  const typer = new AirTyper();
  const sample = hand(typer);
  sample.points[8].x = -1;
  for (let i = 0; i < 10; i++)
    assert.deepEqual(typer.update([sample], i * 30).keys, []);
});
test("two corner calibration requires separate deliberate pinches and never types", () => {
  const typer = new AirTyper();
  typer.calibrate();
  function corner(x, y, pinched) {
    const sample = hand(typer);
    sample.points[0] = { x: 0.5, y: 0.8 };
    sample.points[9] = { x: 0.5, y: 0.6 };
    sample.points[8] = { x: 1 - x, y };
    sample.points[4] = { x: 1 - x + (pinched ? 0.01 : 0.2), y };
    return [sample];
  }
  for (const now of [0, 100, 200, 400])
    assert.deepEqual(typer.update(corner(0.1, 0.2, true), now).keys, []);
  assert.equal(typer.calibration.step, 1);
  typer.update(corner(0.9, 0.9, true), 500);
  assert.equal(typer.calibration.step, 1);
  typer.update(corner(0.9, 0.9, false), 600);
  for (const now of [700, 800, 900, 1100])
    typer.update(corner(0.9, 0.9, true), now);
  assert.equal(typer.calibration, null);
  assert.ok(Math.abs(typer.area.w - 0.8) < 1e-8);
});
test("Shift, Caps and symbols use one mapping and no modifiers remain held", () => {
  const state = new KeyboardState();
  assert.equal(state.press("Shift"), null);
  assert.deepEqual(state.press("a"), { text: "A" });
  assert.deepEqual(state.press("a"), { text: "a" });
  state.press("CapsLock");
  assert.deepEqual(state.press("a"), { text: "A" });
  state.press("Shift");
  assert.deepEqual(state.press("a"), { text: "a" });
  state.press("Shift");
  assert.deepEqual(state.press("1"), { text: "!" });
  assert.deepEqual(state.press("Enter"), { key: "Return" });
  assert.throws(() => state.press("untrusted"), /Unsupported/);
});

test("two and three finger poses are distinct from pointing and pinching", () => {
  function pose(count) {
    const points = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.7, z: 0 }));
    points[0] = { x: 0.5, y: 0.9, z: 0 };
    points[4] = { x: 0.1, y: 0.6, z: 0 };
    for (const [index, tip] of [8, 12, 16, 20].entries()) {
      const x = 0.35 + index * 0.1;
      points[tip - 3] = { x, y: 0.7, z: 0 };
      points[tip - 2] = { x, y: 0.55, z: 0 };
      points[tip] = { x, y: index < count ? 0.3 : 0.8, z: 0 };
    }
    return points;
  }
  assert.equal(gesture(pose(2)), "scroll");
  assert.equal(gesture(pose(3)), "keyboard");
  assert.equal(gesture(pose(4)), "pointer");
  const pinched = pose(2);
  pinched[4] = pinched[8];
  assert.equal(gesture(pinched), "pointer");
  const rotated = pose(2).map((point) => ({
    x: point.y,
    y: 1 - point.x,
    z: 0,
  }));
  assert.equal(gesture(rotated), "scroll");
});

test("a hand identity jump discards an armed finger", () => {
  const typer = new AirTyper();
  arm(typer, [hand(typer)]);
  const bent = hand(typer, "Left", "f", 8, true);
  bent.points[0] = { x: 0.2, y: 0.2, z: 0 };
  typer.update([bent], 150);
  assert.deepEqual(typer.update([bent], 220).keys, []);
});

test("a stalled frame cannot confirm calibration", () => {
  const typer = new AirTyper();
  typer.calibrate();
  const sample = hand(typer);
  sample.points[0] = { x: 0.5, y: 0.8, z: 0 };
  sample.points[9] = { x: 0.5, y: 0.5, z: 0 };
  sample.points[4] = { ...sample.points[8] };
  typer.update([sample], 0);
  typer.update([sample], 100);
  typer.update([sample], 600);
  assert.equal(typer.calibration.step, 0);
});
