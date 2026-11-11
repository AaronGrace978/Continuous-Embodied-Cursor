// SPDX-License-Identifier: AGPL-3.0-or-later
function render(state) {
  const names = {
    planning: "Thinking through your task",
    moving: "Cursor active · moving",
    stopping: "Stopping…",
    ready: "Connected · idle",
    disconnected: "Disconnected",
    camera: "Following your hand · pinch to click",
  };
  document.getElementById("phase").textContent =
    names[state.phase] || "Connecting…";
  document.getElementById("detail").textContent =
    state.goal || "Continuous Cursor · Ctrl+Alt+Esc to stop";
  document.getElementById("dot").className =
    "dot" +
    (["planning", "moving", "stopping", "camera"].includes(state.phase)
      ? " active"
      : "") +
    (state.phase === "stopping" ? " stopping" : "");
  document.getElementById("stop").disabled = ![
    "planning",
    "moving",
    "camera",
  ].includes(state.phase);
}
window.cursor.state().then(render);
window.cursor.onEvent((message) => {
  if (["phase", "snapshot"].includes(message.event)) render(message.data);
});
document
  .getElementById("stop")
  .addEventListener("click", () => window.cursor.stop());
document
  .getElementById("openApp")
  .addEventListener("click", () => window.cursor.openApp());
