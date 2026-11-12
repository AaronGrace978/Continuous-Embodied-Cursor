// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";
const buttons = new Map();
for (const key of AirKeyboard.keys) {
  const button = document.createElement("button");
  button.className = "key";
  button.dataset.key = key.id;
  button.textContent = key.label || key.id;
  button.style.left = key.x * 100 + "%";
  button.style.top = key.y * 100 + "%";
  button.style.width = key.w * 100 + "%";
  button.style.height = key.h * 100 + "%";
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () =>
    window.cursor.keyboardPress(key.id).catch(showError),
  );
  buttons.set(key.id, button);
  document.getElementById("keys").append(button);
}
function showError(error) {
  document.getElementById("status").textContent = error.message;
}
function state(data) {
  for (const key of AirKeyboard.keys) {
    const upper = /^[a-z]$/.test(key.text || "")
      ? data.shift !== data.caps
      : data.shift;
    const button = buttons.get(key.id);
    button.textContent =
      key.label ||
      (key.text
        ? upper
          ? key.shifted || key.text.toUpperCase()
          : key.text
        : key.id);
    button.classList.toggle(
      "selected",
      (["Shift", "ShiftRight"].includes(key.id) && data.shift) ||
        (key.id === "CapsLock" && data.caps),
    );
  }
  document.getElementById("mode").textContent = data.camera
    ? "Finger typing · local only"
    : "Mouse typing · enable camera for air typing";
  if (data.status) document.getElementById("status").textContent = data.status;
}
window.cursor.onEvent((message) => {
  if (message.event === "keyboard-state") state(message.data);
  if (message.event === "keyboard-press") {
    const button = buttons.get(message.data.id);
    button?.classList.add("pressed");
    setTimeout(() => button?.classList.remove("pressed"), 170);
  }
  if (message.event === "keyboard-feedback") {
    document.querySelectorAll(".fingertip").forEach((tip) => tip.remove());
    for (const button of buttons.values()) button.classList.remove("hover");
    for (const tip of message.data.tips || []) {
      if (tip.x < 0 || tip.x > 1 || tip.y < 0 || tip.y > 1) continue;
      const dot = document.createElement("span");
      dot.className = "fingertip";
      dot.style.left = tip.x * 100 + "%";
      dot.style.top = tip.y * 100 + "%";
      document.getElementById("keys").append(dot);
      if (tip.key) buttons.get(tip.key)?.classList.add("hover");
    }
    document.getElementById("status").textContent = message.data.status;
  }
});
window.cursor.keyboardState().then(state).catch(showError);
document
  .getElementById("close")
  .addEventListener("click", () =>
    window.cursor.keyboardToggle(false).catch(showError),
  );
document
  .getElementById("calibrate")
  .addEventListener("click", () =>
    window.cursor.keyboardCalibrate().catch(showError),
  );
