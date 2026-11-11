// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";
const $ = (id) => document.getElementById(id);
const api = window.cursor;
const ACTIVE = new Set(["planning", "moving", "stopping", "camera"]);
const labels = {
  disconnected: "Disconnected",
  connecting: "Connecting…",
  ready: "Connected · idle",
  planning: "Planning",
  moving: "Cursor active",
  stopping: "Stopping…",
  camera: "Hand control active",
};
let state = {
  phase: "disconnected",
  connected: false,
  running: false,
  mode: "live",
  elements: [],
};
let settings = {},
  selectedMode = "live",
  submitting = false,
  connecting = false,
  clearingKey = false;
let activity = [],
  history = [],
  trail = [],
  startedAt = null,
  finishedDuration = null,
  toastTimer;
let lastPointer = "",
  lastMap = "";
let pendingGoals = [],
  queuePaused = false,
  cameraStarting = false;
let cameraRun = 0,
  cameraSessionRequested = false;
const camera = new HandCamera({
  video: $("cameraVideo"),
  canvas: $("cameraCanvas"),
  onSample: (sample) => api.hand(sample),
  onStatus: (status) => {
    $("cameraStatus").textContent = status;
  },
  onError: (error) => {
    toast(error.message, true);
    void stopCamera();
  },
});
function renderQueue() {
  $("queueCard").hidden = !pendingGoals.length;
  $("queueCount").textContent = String(pendingGoals.length);
  $("queueStatus").textContent = queuePaused
    ? "Paused. Review the last result, then resume when ready."
    : "Runs in order. Pauses if a task needs you.";
  $("resumeQueue").hidden = !queuePaused;
  $("resumeQueue").disabled =
    submitting || ACTIVE.has(state.phase) || !state.connected;
  $("queueList").replaceChildren();
  for (const [index, goal] of pendingGoals.entries()) {
    const row = document.createElement("li"),
      name = document.createElement("span"),
      remove = document.createElement("button");
    name.textContent = goal;
    remove.className = "text-button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", "Remove queued task " + (index + 1));
    remove.addEventListener("click", () => {
      pendingGoals.splice(index, 1);
      renderQueue();
    });
    row.append(name, remove);
    $("queueList").append(row);
  }
}
async function drainQueue() {
  if (
    !queuePaused &&
    pendingGoals.length &&
    !submitting &&
    state.connected &&
    !ACTIVE.has(state.phase) &&
    !camera.active
  ) {
    const goal = pendingGoals.shift();
    renderQueue();
    await executeGoal(goal);
  }
}
async function stopCamera() {
  cameraRun++;
  camera.stop();
  cameraStarting = false;
  $("cameraPreview").hidden = true;
  $("cameraStatus").textContent = "Camera off";
  render();
  if (cameraSessionRequested || state.phase === "camera")
    await attempt(() => api.stop());
}
async function toggleCamera() {
  if (camera.active || cameraStarting) {
    await stopCamera();
    return;
  }
  if (!state.connected || submitting || ACTIVE.has(state.phase)) return;
  const run = ++cameraRun;
  cameraStarting = true;
  queuePaused = true;
  $("cameraPreview").hidden = false;
  render();
  try {
    if (!(await camera.start()) || run !== cameraRun) return;
    cameraSessionRequested = true;
    const finished = api.camera();
    finished
      .finally(() => {
        cameraSessionRequested = false;
      })
      .catch(() => {});
    // Tracking starts when the engine acknowledges exclusive cursor ownership.
    await new Promise((resolve, reject) => {
      let timer;
      const unsubscribe = api.onEvent((message) => {
        if (message.event === "phase" && message.data.phase === "camera") {
          clearTimeout(timer);
          unsubscribe();
          resolve();
        }
      });
      timer = setTimeout(() => {
        unsubscribe();
        reject(
          new Error("Hand controller did not start. Retry the connection."),
        );
      }, 5000);
      finished.then(
        () => {
          clearTimeout(timer);
          unsubscribe();
          reject(new Error("Hand control stopped."));
        },
        (error) => {
          clearTimeout(timer);
          unsubscribe();
          reject(error);
        },
      );
    });
    if (run !== cameraRun) return;
    cameraStarting = false;
    camera.track();
    render();
    await finished;
  } catch (error) {
    if (run === cameraRun) toast(error.message, true);
  } finally {
    if (run === cameraRun) await stopCamera();
  }
}

function toast(message, error = false) {
  $("toast").textContent = String(message).replace(
    /^Error invoking remote method '[^']+': (Error: )?/,
    "",
  );
  $("toast").className = "toast" + (error ? " error" : "");
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      $("toast").hidden = true;
    },
    error ? 10000 : 4500,
  );
}
async function attempt(fn) {
  try {
    return await fn();
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}
function providerLabel(value) {
  return (
    {
      "ollama-cloud": "Ollama Cloud",
      "ollama-local": "Local Ollama",
      offline: "Offline commands",
    }[value] || "Ollama Cloud"
  );
}
function updateSettings(next) {
  settings = next;
  $("providerName").textContent = providerLabel(settings.provider);
  $("modelName").textContent =
    settings.provider === "offline"
      ? "No model needed"
      : settings.model || "Choose a model";
  render();
}
function addActivity(message, detail = "", error = false) {
  activity.unshift({ message, detail, error, time: new Date() });
  activity = activity.slice(0, 80);
  $("activityCount").textContent = String(activity.length);
  const feed = $("activityFeed");
  feed.replaceChildren();
  for (const entry of activity) {
    const row = document.createElement("div");
    row.className = "activity-row" + (entry.error ? " error" : "");
    const icon = document.createElement("span");
    icon.className = "activity-symbol";
    if (entry.error) icon.textContent = "!";
    else icon.append(createIcon("arrow"));
    const text = document.createElement("div");
    text.className = "activity-message";
    text.textContent = entry.message;
    if (entry.detail) {
      const detail = document.createElement("small");
      detail.textContent = entry.detail;
      text.append(detail);
    }
    const time = document.createElement("time");
    time.textContent = entry.time.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    row.append(icon, text, time);
    feed.append(row);
  }
}
function render() {
  const active = ACTIVE.has(state.phase) || submitting || cameraStarting;
  const handActive =
    camera.active || cameraStarting || state.phase === "camera";
  const phase = state.phase;
  $("statusBadge").className = "status-badge " + phase;
  $("statusLabel").textContent = labels[phase] || phase;
  $("liveMode").classList.toggle("selected", selectedMode === "live");
  $("simulationMode").classList.toggle(
    "selected",
    selectedMode === "simulation",
  );
  $("liveMode").setAttribute("aria-pressed", String(selectedMode === "live"));
  $("simulationMode").setAttribute(
    "aria-pressed",
    String(selectedMode === "simulation"),
  );
  $("liveMode").disabled = active || connecting;
  $("simulationMode").disabled = active || connecting;
  $("connectButton").disabled = connecting || state.phase === "connecting";
  $("connectButton").replaceChildren(
    document.createTextNode(
      connecting || phase === "connecting"
        ? "Connecting…"
        : state.connected
          ? "Disconnect"
          : selectedMode === "simulation"
            ? "Start simulation"
            : "Connect desktop",
    ),
  );
  $("connectButton").append(createIcon(state.connected ? "close" : "arrow"));
  $("runButton").disabled =
    !state.connected ||
    handActive ||
    state.phase === "stopping" ||
    !$("goal").value.trim();
  $("runButton").firstChild.textContent =
    submitting || ACTIVE.has(state.phase) ? "Queue task " : "Run task ";
  $("goal").disabled = handActive;
  $("cameraButton").disabled = !state.connected || (!handActive && active);
  $("cameraButton").textContent = handActive
    ? "Disable camera ■"
    : "Enable camera ↗";
  $("exportTrace").disabled = !state.connected || active;
  $("stopDock").hidden = !active;
  $("stopButton").disabled = phase === "stopping";
  $("activeDescription").textContent =
    phase === "planning"
      ? "Thinking through your task"
      : phase === "stopping"
        ? "Stopping…"
        : state.mode === "simulation"
          ? "Simulation running"
          : "Cursor active";
  $("sessionOrb").classList.toggle("active", active);
  $("sessionOrb").dataset.phase = phase;
  const description =
    phase === "planning"
      ? ["PLANNING", "Choosing the next move. Hold position."]
      : phase === "moving"
        ? [
            "IN MOTION",
            state.mode === "simulation"
              ? "Only the simulated pointer is moving."
              : "Your desktop cursor is being controlled.",
          ]
        : phase === "camera"
          ? ["HAND CONTROL", "Following your hand. Pinch to click."]
          : phase === "stopping"
            ? ["STOPPING", "Cancelling input. Returning control."]
            : state.connected
              ? [
                  "ARMED / IDLE",
                  state.mode === "simulation"
                    ? "Playground mode. Your mouse stays yours."
                    : "No input until you run a task.",
                ]
              : ["STANDBY", "Connect a desktop. Take the controls."];
  $("sessionTitle").textContent = description[0];
  $("sessionDescription").textContent = description[1];
  $("mapTag").textContent = state.connected
    ? state.mode === "simulation"
      ? "SIMULATION"
      : "LIVE DESKTOP"
    : "NO CONNECTION";
  $("environmentValue").textContent = !state.connected
    ? "Not connected"
    : state.mode === "simulation"
      ? "Simulation"
      : state.platform === "win32"
        ? "Windows · native"
        : "Linux · X11";
  $("elementCount").textContent = state.connected
    ? String((state.elements || []).length)
    : "—";
  $("perceptionValue").textContent = state.connected
    ? `${state.perceptionMs ?? 0} ms`
    : "—";
  $("pointerPosition").textContent =
    state.connected && state.pointer
      ? `X ${state.pointer[0]}   Y ${state.pointer[1]}`
      : "X —   Y —";
  $("goalHint").textContent = !state.connected
    ? "Connect your desktop, or try the simulation to get a feel for things."
    : state.mode === "simulation"
      ? "Simulation · These example controls are in the playground. Your real pointer stays put."
      : "Live desktop · Use names from the desktop map. Run task gives the cursor control.";
  $("mapEmpty").hidden = state.connected;
  $("mapSvg").hidden = !state.connected;
  if (state.connected) drawMap();
  else {
    trail = [];
    lastMap = "";
    lastPointer = "";
  }
  renderQueue();
}
function svgElement(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function drawMap() {
  const elements = (state.elements || [])
    .filter((el) => Array.isArray(el.bounds))
    .slice(0, 220);
  const monitors = state.monitors?.length
    ? state.monitors
    : [[0, 0, 1000, 640]];
  const left = Math.min(...monitors.map((m) => m[0])),
    top = Math.min(...monitors.map((m) => m[1]));
  const right = Math.max(...monitors.map((m) => m[0] + m[2])),
    bottom = Math.max(...monitors.map((m) => m[1] + m[3]));
  const width = Math.max(1, right - left),
    height = Math.max(1, bottom - top);
  const identity = JSON.stringify([elements, state.pointer, state.mode]);
  if (identity === lastMap) return;
  lastMap = identity;
  const svg = $("mapSvg");
  svg.replaceChildren();
  svg.setAttribute(
    "viewBox",
    `${left - 15} ${top - 15} ${width + 30} ${height + 30}`,
  );
  const unit = width / 1000;
  for (const el of elements) {
    const [x, y, w, h] = el.bounds;
    if (w < 1 || h < 1) continue;
    const window = ["window", "dialog", "panel"].includes(el.role);
    const button = ["button", "checkbox", "toggle"].includes(el.role);
    const rect = svgElement("rect", {
      x,
      y,
      width: w,
      height: h,
      rx: Math.min(9 * unit, h / 4),
      fill: window
        ? "#141e25"
        : button
          ? "#3f3428"
          : el.role === "text_field"
            ? "#1b2b35"
            : "#1c2e39",
      stroke: window ? "#456575" : "#69838a",
      "stroke-width": Math.max(1, 1.2 * unit),
      opacity: window ? 0.96 : 0.92,
    });
    rect.append(svgElement("title", {}, `${el.role}: ${el.name || "Unnamed"}`));
    svg.append(rect);
    if (el.role === "slider" && el.range && el.value !== undefined) {
      const f = Math.max(
        0,
        Math.min(
          1,
          (el.value - el.range[0]) / (el.range[1] - el.range[0] || 1),
        ),
      );
      svg.append(
        svgElement("rect", {
          x: x + 2,
          y: y + 2,
          width: Math.max(0, (w - 4) * f),
          height: Math.max(1, h - 4),
          rx: Math.min(8 * unit, h / 3),
          fill: "#f47d4c",
        }),
      );
    }
    if (w > 55 * unit && h > 15 * unit) {
      const title = el.name || el.text || el.role;
      svg.append(
        svgElement(
          "text",
          {
            x: x + 10 * unit,
            y: y + Math.min(h - 3, 21 * unit),
            fill: "#a6c7d6",
            "font-size": window ? 13 * unit : 12 * unit,
            "font-family": "Segoe UI, sans-serif",
          },
          title.slice(0, Math.max(4, Math.floor(w / (7 * unit)))),
        ),
      );
      if (el.text && el.name && h > 45 * unit)
        svg.append(
          svgElement(
            "text",
            {
              x: x + 10 * unit,
              y: y + 42 * unit,
              fill: "#80a8be",
              "font-size": 11 * unit,
            },
            el.text.slice(0, 55),
          ),
        );
    }
    if (el.states?.includes("checked"))
      svg.append(
        svgElement(
          "text",
          {
            x: x + w - 20 * unit,
            y: y + Math.min(24 * unit, h - 4),
            fill: "#ff9d66",
            "font-size": 16 * unit,
          },
          "✓",
        ),
      );
  }
  if (state.pointer) {
    const key = state.pointer.join(",");
    if (key !== lastPointer) {
      trail.push([...state.pointer]);
      trail = trail.slice(-100);
      lastPointer = key;
    }
    if (trail.length > 1)
      svg.append(
        svgElement("polyline", {
          points: trail.map((p) => p.join(",")).join(" "),
          fill: "none",
          stroke: "#81dcf8",
          "stroke-width": 2 * unit,
          "stroke-dasharray": `${3 * unit} ${5 * unit}`,
          opacity: 0.7,
        }),
      );
    const [x, y] = state.pointer;
    const cursor = svgElement("g", {
      transform: `translate(${x} ${y}) scale(${unit * 1.3})`,
    });
    cursor.append(
      svgElement("path", {
        d: "M0 0 L0 24 L7 18 L13 30 L18 27 L12 16 L22 15 Z",
        fill: "#ff6a3d",
        stroke: "#ffc29e",
        "stroke-width": 1.5,
      }),
    );
    cursor.append(
      svgElement("rect", {
        x: 23,
        y: 22,
        width: 42,
        height: 20,
        rx: 5,
        fill: "#ff6a3d",
      }),
    );
    cursor.append(
      svgElement(
        "text",
        {
          x: 31,
          y: 36,
          fill: "#14191d",
          "font-size": 10,
          "font-family": "Segoe UI, sans-serif",
        },
        "CEC",
      ),
    );
    svg.append(cursor);
  }
}
function renderHistory() {
  $("historyCount").textContent = String(history.length);
  const list = $("historyList");
  list.replaceChildren();
  if (!history.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent =
      "No tasks yet. Your next idea is a good place to start.";
    list.append(empty);
    return;
  }
  for (const report of history) {
    const card = document.createElement("article");
    card.className = "history-task";
    const title = document.createElement("h3");
    title.textContent = report.goal;
    const status = document.createElement("span");
    status.className =
      "history-status" + (report.status === "failed" ? " failed" : "");
    status.textContent = report.status;
    const summary = document.createElement("p");
    summary.textContent = `${report.outcomes?.length || 0} actions · ${report.rounds || 0} planning rounds · ${report.mode === "simulation" ? "Simulation" : "Live desktop"}`;
    card.append(title, status, summary);
    if (report.reason) {
      const reason = document.createElement("p");
      reason.textContent = report.reason;
      card.append(reason);
    }
    list.append(card);
  }
}
function receive(message) {
  if (message.event === "stop-requested") {
    cameraRun++;
    queuePaused = true;
    camera.stop();
    cameraStarting = false;
    $("cameraPreview").hidden = true;
    $("cameraStatus").textContent = "Camera off";
    renderQueue();
  } else if (message.event === "hand-status") {
    $("cameraStatus").textContent = message.data.status;
    $("companionMessage").textContent = message.data.status;
  } else if (message.event === "hand-stopped") {
    cameraRun++;
    camera.stop();
    $("cameraPreview").hidden = true;
    $("cameraStatus").textContent =
      message.data.reason === "human_override"
        ? "Paused · you moved the mouse"
        : "Camera off";
    render();
  }
  if (message.event === "snapshot" || message.event === "phase") {
    state = { ...state, ...message.data };
    if (state.phase === "stopping") queuePaused = true;
    if (!state.connected) {
      queuePaused = true;
      camera.stop();
      $("cameraPreview").hidden = true;
    }
    if (message.event === "phase")
      $("companionMessage").textContent =
        {
          planning: "Let me work out the next step…",
          moving: "On it.",
          ready: "Ready for your next idea.",
          disconnected: "Ready when you are.",
          stopping: "Handing control back to you.",
          camera: "Show me an open hand.",
        }[state.phase] || "Connecting…";
    render();
  } else if (message.event === "activity") {
    const data = message.data;
    if (data.kind === "plan")
      addActivity(
        `Plan ${data.round}: ${data.intents?.join("; ") || "Goal complete"}`,
        data.note || "",
      );
    else if (data.kind === "intent") {
      $("companionMessage").textContent =
        `Working on ${data.intent?.action || "the next step"}…`;
      addActivity(
        `Executing ${data.intent?.action || "action"}`,
        data.intent?.target ? JSON.stringify(data.intent.target) : "",
      );
    } else if (data.kind === "surprise")
      addActivity("Taking another look", data.what);
    else if (data.kind === "press")
      addActivity(
        "Mouse button pressed",
        `At ${data.at?.join(", ") || "target"}`,
      );
  } else if (message.event === "report") {
    const report = { ...message.data, mode: state.mode };
    if (report.status !== "done") queuePaused = true;
    $("companionMessage").textContent =
      report.status === "done"
        ? "Done. What's next?"
        : "I stopped here. Take a look at the result.";
    history.unshift(report);
    history = history.slice(0, 30);
    renderHistory();
    addActivity(
      report.status === "done"
        ? "Task complete"
        : report.status === "interrupted"
          ? "Task interrupted"
          : "Task finished without completion",
      report.reason || report.goal,
      report.status === "failed",
    );
    toast(
      report.status === "done"
        ? "Task complete. Control returned."
        : report.reason || report.status,
      report.status === "failed",
    );
  }
}
async function connect() {
  if (connecting) return;
  connecting = true;
  render();
  try {
    if (state.connected) {
      queuePaused = true;
      await stopCamera();
      await api.disconnect();
      addActivity("Desktop disconnected");
    } else {
      const result = await api.connect(selectedMode);
      addActivity(
        selectedMode === "simulation"
          ? "Playground connected"
          : "Desktop connected",
        result.notes?.join(" ") || "Ready for a task.",
      );
    }
  } catch (error) {
    toast(error.message, true);
    addActivity("Connection failed", error.message, true);
  } finally {
    connecting = false;
    render();
  }
}
async function setMode(mode) {
  if (submitting || ACTIVE.has(state.phase)) return;
  if (state.connected) await attempt(() => api.disconnect());
  selectedMode = mode;
  render();
}
async function run() {
  if (!state.connected || camera.active || cameraStarting) return;
  const goal = $("goal").value.trim();
  if (!goal) return;
  if (
    submitting ||
    camera.active ||
    cameraStarting ||
    ACTIVE.has(state.phase)
  ) {
    if (pendingGoals.length >= 20) {
      toast("The queue is full. Wait for a task to finish.");
      return;
    }
    pendingGoals.push(goal);
    $("goal").value = "";
    render();
    addActivity("Task queued", goal);
    return;
  }
  await executeGoal(goal);
}
async function executeGoal(goal) {
  if (!pendingGoals.length) queuePaused = false;
  submitting = true;
  startedAt = Date.now();
  finishedDuration = null;
  render();
  try {
    const report = await api.run(goal);
    if (report?.status !== "done") queuePaused = true;
  } catch (error) {
    queuePaused = true;
    toast(error.message, true);
    addActivity("Task stopped", error.message, true);
  } finally {
    finishedDuration = Date.now() - startedAt;
    startedAt = null;
    submitting = false;
    render();
    void drainQueue();
  }
}
function showWorkspace(historyView = false) {
  $("workspaceView").hidden = historyView;
  $("historyView").hidden = !historyView;
  $("workspaceNav").classList.toggle("selected", !historyView);
  $("historyNav").classList.toggle("selected", historyView);
  $("pageTitle").textContent = historyView
    ? "SESSION MEMORY / RUN LOG"
    : "THE CONTROL ROOM / SYSTEM OVERVIEW";
}
function providerFields() {
  const provider = $("provider").value;
  $("cloudFields").hidden = provider !== "ollama-cloud";
  $("localFields").hidden = provider !== "ollama-local";
  $("modelFields").hidden = provider === "offline";
}
function keyStatus() {
  const descriptions = {
    encrypted: "Key saved with OS encryption. Leave blank to keep it.",
    session:
      "Key is stored for this session only; secure OS storage is unavailable.",
    environment: "Using OLLAMA_API_KEY from the app environment.",
    none: "No key saved. Your key will be encrypted when secure OS storage is available.",
  };
  $("keyStatus").textContent =
    descriptions[settings.keyStorage] || descriptions.none;
  $("removeKey").hidden =
    !settings.apiKeyConfigured || settings.keyStorage === "environment";
}
function openSettings() {
  if (
    submitting ||
    camera.active ||
    cameraStarting ||
    ACTIVE.has(state.phase)
  ) {
    toast("Stop the task before changing settings.");
    return;
  }
  $("provider").value = settings.provider || "ollama-cloud";
  $("model").value = settings.model || "";
  $("localUrl").value = settings.localUrl || "http://127.0.0.1:11434";
  $("pythonPath").value = settings.pythonPath || "";
  $("indicatorPreference").checked = settings.indicator !== false;
  $("minimizePreference").checked = settings.minimizeOnRun !== false;
  $("speedPreference").value = settings.speed || "responsive";
  $("localFirstPreference").checked = settings.localFirst !== false;
  $("apiKey").value = "";
  clearingKey = false;
  $("saveStatus").textContent = "";
  $("connectionResult").textContent =
    "A connection test sends a small prompt to your selected model.";
  $("connectionResult").className = "field-note";
  providerFields();
  keyStatus();
  $("settingsDialog").showModal();
}
async function saveSettings() {
  const next = await api.saveSettings({
    provider: $("provider").value,
    model: $("model").value,
    localUrl: $("localUrl").value,
    pythonPath: $("pythonPath").value,
    apiKey: $("apiKey").value,
    clearKey: clearingKey,
    indicator: $("indicatorPreference").checked,
    minimizeOnRun: $("minimizePreference").checked,
    speed: $("speedPreference").value,
    localFirst: $("localFirstPreference").checked,
  });
  $("apiKey").value = "";
  clearingKey = false;
  updateSettings(next);
  keyStatus();
  $("saveStatus").textContent =
    next.keyStorage === "session"
      ? "Saved. API key is session-only."
      : "Settings saved.";
  return next;
}
if (!api) {
  toast(
    "Open this interface with Electron: run npm start in the ui folder.",
    true,
  );
} else {
  document.querySelector(".brand").addEventListener("click", (event) => {
    event.preventDefault();
    showWorkspace();
  });
  api.onEvent(receive);
  Promise.all([api.state(), api.settings()])
    .then(([initial, config]) => {
      state = { ...state, ...initial };
      selectedMode = state.mode;
      updateSettings(config);
      if (!initial.shortcutAvailable)
        $("shortcutText").textContent =
          "Global shortcut unavailable · use Stop";
      if (config.warning) toast(config.warning, true);
    })
    .catch((error) => toast(error.message, true));
  $("connectButton").addEventListener("click", connect);
  $("liveMode").addEventListener("click", () => setMode("live"));
  $("simulationMode").addEventListener("click", () => setMode("simulation"));
  $("trySimulation").addEventListener("click", () =>
    attempt(async () => {
      updateSettings(await api.saveSettings({ provider: "offline" }));
      await setMode("simulation");
      await connect();
      $("goal").value = "click Export";
      render();
    }),
  );
  $("runButton").addEventListener("click", run);
  $("goal").addEventListener("input", render);
  $("goal").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (!$("runButton").disabled) void run();
    }
  });
  for (const chip of document.querySelectorAll("[data-goal]"))
    chip.addEventListener("click", () => {
      $("goal").value = chip.dataset.goal;
      $("goal").focus();
      render();
    });
  $("stopButton").addEventListener("click", () => {
    queuePaused = true;
    if (camera.active || cameraStarting) void stopCamera();
    else void attempt(() => api.stop());
  });
  $("cameraButton").addEventListener("click", () => void toggleCamera());
  $("resumeQueue").addEventListener("click", () => {
    queuePaused = false;
    void drainQueue();
    renderQueue();
  });
  $("clearQueue").addEventListener("click", () => {
    pendingGoals = [];
    renderQueue();
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      !$("settingsDialog").open &&
      (submitting || camera.active || cameraStarting)
    ) {
      queuePaused = true;
      if (camera.active || cameraStarting) void stopCamera();
      else void attempt(() => api.stop());
    }
  });
  for (const id of ["settingsNav", "topSettings", "modelButton"])
    $(id).addEventListener("click", openSettings);
  $("closeSettings").addEventListener("click", () => {
    $("settingsDialog").close();
    $("apiKey").value = "";
  });
  $("settingsDialog").addEventListener("close", () => {
    $("apiKey").value = "";
  });
  $("settingsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void attempt(async () => {
      await saveSettings();
      $("settingsDialog").close();
      toast("Settings saved.");
    });
  });
  $("provider").addEventListener("change", providerFields);
  $("removeKey").addEventListener("click", () => {
    clearingKey = true;
    $("apiKey").value = "";
    $("keyStatus").textContent = "Saved key will be removed when you save.";
  });
  $("getKey").addEventListener("click", () =>
    attempt(() => api.openExternal("keys")),
  );
  $("guideButton").addEventListener("click", () =>
    attempt(() => api.openExternal("docs")),
  );
  $("workspaceNav").addEventListener("click", () => showWorkspace());
  $("historyNav").addEventListener("click", () => showWorkspace(true));
  $("clearActivity").addEventListener("click", () => {
    activity = [];
    $("activityCount").textContent = "0";
    $("activityFeed").replaceChildren();
  });
  $("exportTrace").addEventListener("click", () =>
    attempt(async () => {
      const result = await api.exportTrace();
      if (result.saved) toast("Trace exported.");
    }),
  );
  $("refreshModels").addEventListener("click", () =>
    attempt(async () => {
      $("refreshModels").disabled = true;
      try {
        await saveSettings();
        const result = await api.models();
        $("modelOptions").replaceChildren();
        for (const model of result.models) {
          const option = document.createElement("option");
          option.value = model;
          $("modelOptions").append(option);
        }
        $("connectionResult").textContent =
          `${result.models.length} models loaded. Choose one or type its exact name. This does not verify the API key.`;
      } finally {
        $("refreshModels").disabled = false;
      }
    }),
  );
  $("testConnection").addEventListener("click", () =>
    attempt(async () => {
      $("testConnection").disabled = true;
      $("connectionResult").textContent = "Sending a small test prompt…";
      try {
        await saveSettings();
        const result = await api.testProvider();
        $("connectionResult").textContent =
          `Connected to ${result.model} · ${result.latencyMs} ms`;
        $("connectionResult").className = "field-note connection-success";
      } catch (error) {
        $("connectionResult").textContent = error.message;
        $("connectionResult").className = "field-note connection-error";
        throw error;
      } finally {
        $("testConnection").disabled = false;
      }
    }),
  );
}
setInterval(() => {
  const elapsed = startedAt ? Date.now() - startedAt : finishedDuration;
  $("taskTime").textContent =
    elapsed == null ? "—" : `${(elapsed / 1000).toFixed(1)} s`;
}, 100);
render();
window.addEventListener("beforeunload", () => camera.stop());
document.addEventListener("pointermove", (event) => {
  $("sessionOrb").style.setProperty(
    "--gaze-x",
    `${(event.clientX / innerWidth - 0.5) * 7}px`,
  );
  $("sessionOrb").style.setProperty(
    "--gaze-y",
    `${(event.clientY / innerHeight - 0.5) * 5}px`,
  );
});

$("desktopMap").addEventListener("pointermove", (event) => {
  if (
    state.connected ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  )
    return;
  const box = $("desktopMap").getBoundingClientRect();
  const x = (event.clientX - box.left) / box.width - 0.5;
  const y = (event.clientY - box.top) / box.height - 0.5;
  $("kineticArt").style.transform =
    `translate(${x * 12}px,${y * 10}px) rotate(${x * 3}deg)`;
});
$("desktopMap").addEventListener("pointerleave", () => {
  $("kineticArt").style.transform = "";
});
