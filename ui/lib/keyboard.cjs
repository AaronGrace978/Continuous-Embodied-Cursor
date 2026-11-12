// SPDX-License-Identifier: AGPL-3.0-or-later
const { keys } = require("../renderer/air-keyboard.js");
const NATIVE = {
  Backspace: "BackSpace",
  Tab: "Tab",
  Enter: "Return",
  Escape: "Escape",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  ArrowUp: "Up",
  ArrowDown: "Down",
  Delete: "Delete",
};
class KeyboardState {
  constructor() {
    this.reset();
  }
  reset() {
    this.shift = false;
    this.caps = false;
  }
  press(id) {
    const key = keys.find((key) => key.id === id);
    if (!key) throw new Error("Unsupported keyboard key");
    if (id === "Shift" || id === "ShiftRight") {
      this.shift = !this.shift;
      return null;
    }
    if (id === "CapsLock") {
      this.caps = !this.caps;
      return null;
    }
    if (NATIVE[id]) return { key: NATIVE[id] };
    const upper = /^[a-z]$/.test(key.text)
      ? this.shift !== this.caps
      : this.shift;
    const text = upper ? key.shifted || key.text.toUpperCase() : key.text;
    this.shift = false;
    return { text };
  }
}
module.exports = { KeyboardState };
