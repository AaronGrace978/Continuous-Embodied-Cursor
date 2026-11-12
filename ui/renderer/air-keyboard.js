// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";
// Shared geometry for the camera, floating keyboard, and deterministic tests.
(() => {
  const letters = (text) => [...text].map((text) => ({ id: text, text }));
  const symbol = (text, shifted) => ({ id: text, text, shifted });
  const rows = [
    [
      symbol("`", "~"),
      ...[..."1234567890"].map((text, i) => symbol(text, "!@#$%^&*()"[i])),
      symbol("-", "_"),
      symbol("=", "+"),
      { id: "Backspace", width: 2 },
    ],
    [
      { id: "Tab", width: 1.5 },
      ...letters("qwertyuiop"),
      symbol("[", "{"),
      symbol("]", "}"),
      { ...symbol("\\", "|"), width: 1.5 },
    ],
    [
      { id: "CapsLock", label: "Caps", width: 1.75 },
      ...letters("asdfghjkl"),
      symbol(";", ":"),
      symbol("'", '"'),
      { id: "Enter", width: 2.25 },
    ],
    [
      { id: "Shift", width: 2.25 },
      ...letters("zxcvbnm"),
      symbol(",", "<"),
      symbol(".", ">"),
      symbol("/", "?"),
      { id: "ShiftRight", label: "Shift", width: 2.75 },
    ],
    [
      { id: "Escape", label: "Esc" },
      { id: "ArrowLeft", label: "←" },
      { id: "Space", label: "Space", text: " ", width: 6 },
      { id: "ArrowDown", label: "↓" },
      { id: "ArrowUp", label: "↑" },
      { id: "ArrowRight", label: "→" },
      { id: "Delete", label: "Del" },
      { id: "HideKeyboard", label: "Hide", width: 1.5 },
    ],
  ];
  const keys = rows.flatMap((row, y) => {
    const total = row.reduce((sum, key) => sum + (key.width || 1), 0);
    let x = 0;
    return row.map((key) => {
      const width = (key.width || 1) / total;
      const result = {
        ...key,
        x,
        y: y / rows.length,
        w: width,
        h: 1 / rows.length,
      };
      x += width;
      return result;
    });
  });
  function hit(x, y) {
    return (
      keys.find(
        (key) =>
          x >= key.x + 0.006 &&
          x <= key.x + key.w - 0.006 &&
          y >= key.y + 0.012 &&
          y <= key.y + key.h - 0.012,
      )?.id || null
    );
  }
  const distance = (a, b, aspect = 1) =>
    Math.hypot(a.x - b.x, (a.y - b.y) * aspect, (a.z || 0) - (b.z || 0));
  function angle(a, b, c, aspect) {
    const ab = distance(a, b, aspect),
      bc = distance(b, c, aspect),
      ac = distance(a, c, aspect);
    if (ab * bc < 1e-8) return 0;
    return (
      (Math.acos(
        Math.max(
          -1,
          Math.min(1, (ab * ab + bc * bc - ac * ac) / (2 * ab * bc)),
        ),
      ) *
        180) /
      Math.PI
    );
  }
  function extended(points, tip, aspect) {
    return (
      angle(points[tip - 3], points[tip - 2], points[tip], aspect) > 155 &&
      distance(points[0], points[tip], aspect) >
        distance(points[0], points[tip - 2], aspect) * 1.08
    );
  }
  function gesture(points, aspect = 1) {
    const fingers = [8, 12, 16, 20].map((tip) => extended(points, tip, aspect));
    const palm = distance(points[0], points[9], aspect);
    const pinch = palm ? distance(points[4], points[8], aspect) / palm : 0;
    // Pinching always belongs to the existing click recognizer.
    if (pinch > 0.55 && fingers[0] && fingers[1] && !fingers[3])
      return fingers[2] ? "keyboard" : "scroll";
    return "pointer";
  }
  class AirTyper {
    constructor() {
      this.area = { x: 0.06, y: 0.25, w: 0.88, h: 0.68 };
      this.reset();
    }
    reset() {
      this.fingers = new Map();
      this.wrists = new Map();
      this.lastTime = null;
    }
    calibrate() {
      this.reset();
      this.calibration = { step: 0, since: null, released: true };
    }
    update(hands, now, aspect = 1) {
      const result = {
        keys: [],
        tips: [],
        status: "Extend a finger over a key, then bend it to tap.",
      };
      if (
        this.lastTime !== null &&
        (now <= this.lastTime || now - this.lastTime > 250)
      ) {
        this.reset();
        if (this.calibration) this.calibration.since = null;
      }
      this.lastTime = now;
      if (this.calibration) {
        const cal = this.calibration,
          points = hands[0]?.points;
        result.status =
          cal.step === 0
            ? "Calibration: point at the upper-left corner, then hold a pinch."
            : "Release, point at the lower-right corner, then hold a pinch.";
        if (!points) {
          cal.since = null;
          return result;
        }
        const palm = distance(points[0], points[9], aspect);
        const pinch = palm ? distance(points[4], points[8], aspect) / palm : 1;
        if (pinch > 0.55) {
          cal.released = true;
          cal.since = null;
        }
        if (pinch < 0.35 && cal.released) {
          cal.since ??= now;
          if (now - cal.since >= 350) {
            const point = { x: 1 - points[8].x, y: points[8].y };
            if (!cal.step) {
              cal.start = point;
              cal.step = 1;
            } else {
              const w = point.x - cal.start.x,
                h = point.y - cal.start.y;
              if (w >= 0.35 && h >= 0.25) {
                this.area = { ...cal.start, w, h };
                this.calibration = null;
                this.reset();
                result.status =
                  "Calibrated. Extend your fingers to arm typing.";
              } else {
                cal.step = 0;
                result.status =
                  "Area too small or reversed. Start again at the upper-left corner.";
              }
            }
            cal.since = null;
            cal.released = false;
          }
        }
        return result;
      }
      if (!hands.length)
        result.status =
          "Tracking paused. Show your hands and extend fingers to resume.";
      const seen = new Set();
      const handIds = new Set();
      for (const hand of hands) {
        if (handIds.has(hand.id)) continue;
        handIds.add(hand.id);
        const wrist = hand.points[0],
          previous = this.wrists.get(hand.id);
        if (previous && distance(wrist, previous, aspect) > 0.18)
          for (const id of this.fingers.keys())
            if (id.startsWith(hand.id + ":")) this.fingers.delete(id);
        this.wrists.set(hand.id, wrist);
        for (const tip of [8, 12, 16, 20]) {
          const id = hand.id + ":" + tip,
            points = hand.points;
          seen.add(id);
          const point = points[tip];
          const x = (1 - point.x - this.area.x) / this.area.w,
            y = (point.y - this.area.y) / this.area.h;
          const key = hit(x, y);
          const bend = angle(points[tip - 3], points[tip - 2], point, aspect);
          let finger = this.fingers.get(id);
          if (!finger) {
            finger = {
              key: null,
              since: now,
              armed: false,
              bent: null,
              latched: false,
            };
            this.fingers.set(id, finger);
          }
          result.tips.push({
            id,
            x,
            y,
            key: finger.armed ? finger.key : key,
            armed: finger.armed,
          });
          if (bend > 155) {
            finger.bent = null;
            if (key !== finger.key) {
              finger.key = key;
              finger.since = now;
              finger.armed = false;
            }
            if (key && now - finger.since >= 100) {
              finger.armed = true;
              finger.latched = false;
            }
          } else if (bend < 130) {
            finger.bent ??= now;
            if (finger.armed && !finger.latched && now - finger.bent >= 45) {
              result.keys.push(finger.key);
              finger.latched = true;
              finger.armed = false;
              finger.since = now;
            }
          } else {
            finger.bent = null;
          }
        }
      }
      for (const id of this.fingers.keys())
        if (!seen.has(id)) this.fingers.delete(id);
      for (const id of this.wrists.keys())
        if (!handIds.has(id)) this.wrists.delete(id);
      return result;
    }
  }
  const exported = { rows, keys, hit, angle, gesture, AirTyper };
  if (typeof module !== "undefined") module.exports = exported;
  else globalThis.AirKeyboard = exported;
})();
