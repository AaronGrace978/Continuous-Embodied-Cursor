// SPDX-License-Identifier: AGPL-3.0-or-later
const api = window.cursor;
const activePhases = new Set(["planning", "moving", "stopping", "camera"]);
let state = {},
  pending = false,
  localError = "";
const byId = (id) => document.getElementById(id);
let voiceMessage = "Local speech · off";
const voiceCapture = new LocalVoice.Capture({
  api,
  onState: ({ message }) => {
    voiceMessage = message;
    render();
  },
  onTranscript: async (text, token) => {
    byId("dockGoal").value = text;
    render();
    const settings = await api.state();
    voiceMessage = settings.voiceAutoRun
      ? "Recognized command submitted."
      : "Transcript ready. Review it, then Run.";
    render();
    if (settings.voiceAutoRun) await submitGoal(text, { voiceToken: token });
  },
});
async function submitGoal(goal, options) {
  if (!goal || pending || activePhases.has(state.phase) || !state.connected)
    return;
  pending = true;
  localError = "";
  render({ detail: "Starting your instruction…" });
  try {
    await api.run(goal, options);
    if (options?.voiceToken === undefined) byId("dockGoal").value = "";
  } catch (error) {
    localError = error.message;
  } finally {
    pending = false;
    render(await api.state());
  }
}
function render(patch = {}) {
  state = { ...state, ...patch };
  const active = pending || state.running || activePhases.has(state.phase);
  const names = {
    planning: "Planning the next step",
    moving: "Cursor active",
    stopping: "Stopping…",
    ready: "Session active · ready",
    disconnected: "Disconnected",
    camera: "Following your hand",
  };
  byId("phase").textContent = names[state.phase] || "Connecting…";
  byId("detail").textContent =
    state.detail || state.goal || "Continuous Cursor · Ctrl+Alt+Esc to stop";
  byId("dot").className =
    "dot" +
    (active ? " active" : "") +
    (state.phase === "stopping" ? " stopping" : "");
  byId("stop").disabled =
    (!active && !voiceCapture.active) || state.phase === "stopping";
  byId("copilot").hidden = !state.docked;
  document.body.classList.toggle("docked", Boolean(state.docked));
  byId("send").disabled =
    active || voiceCapture.active || state.voiceActive || !state.connected;
  byId("dockGoal").disabled = active || voiceCapture.active || !state.connected;
  byId("clearContext").disabled =
    active || voiceCapture.active || state.voiceActive || !state.connected;
  byId("voiceControls").hidden = !state.docked;
  byId("voiceButton").disabled =
    !state.connected ||
    (active && !voiceCapture.active) ||
    (state.voiceActive && !voiceCapture.active);
  byId("voiceButton").textContent =
    voiceCapture.phase === "recording"
      ? "Finish ■"
      : voiceCapture.active
        ? "Cancel ×"
        : "Voice ◉";
  byId("voiceButton").setAttribute("aria-pressed", String(voiceCapture.active));
  byId("voiceStatus").textContent = voiceMessage;
  const mode = state.mode === "simulation" ? "SIMULATION · " : "LIVE · ";
  byId("sessionInfo").textContent =
    mode +
    `${state.completedTasks || 0} tasks finished · ${state.contextTurns || 0} remembered`;
  byId("feedback").textContent =
    localError || state.detail || "Ready for your next instruction.";
  const subs = state.substrates;
  byId("substrateStatus").textContent = !subs?.enabled
    ? "Substrates off"
    : `Motion ${subs.motion?.connected ? "connected" : "offline"} · ChronoSoul ${subs.emotion?.connected ? "connected" : "offline"}`;
}
api.onEvent((message) => {
  if (message.event === "voice-toggle") {
    void voiceCapture.toggle();
    return;
  }
  if (message.event === "voice-reset") {
    voiceCapture.cancel(false);
    return;
  }
  if (message.event === "voice-state") {
    render({ voiceActive: message.data.active });
    return;
  }
  if (["phase", "snapshot", "dock"].includes(message.event))
    render(message.data);
  if (message.event === "report")
    render({
      detail:
        message.data.status === "done"
          ? "Task finished. Still here — what next?"
          : `Paused: ${message.data.reason || message.data.status}`,
    });
});
api
  .state()
  .then(render)
  .catch((error) => {
    localError = error.message;
    render();
  });
byId("commandForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const goal = byId("dockGoal").value.trim();
  if (!goal || pending || activePhases.has(state.phase) || !state.connected)
    return;
  await submitGoal(goal);
});
byId("clearContext").addEventListener("click", async () => {
  try {
    await api.clearContext();
    localError = "";
    render(await api.state());
  } catch (error) {
    localError = error.message;
    render();
  }
});
byId("stop").addEventListener("click", () =>
  api.stop().catch((error) => {
    localError = error.message;
    render();
  }),
);
byId("openApp").addEventListener("click", () => api.openApp());

byId("voiceButton").addEventListener("click", () => void voiceCapture.toggle());
window.addEventListener("beforeunload", () => voiceCapture.cancel());
