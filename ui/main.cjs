// SPDX-License-Identifier: AGPL-3.0-or-later
const {
  app,
  BrowserWindow,
  ipcMain,
  globalShortcut,
  screen,
  safeStorage,
  dialog,
  shell,
  Tray,
  Menu,
  nativeImage,
} = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");
const { SettingsStore } = require("./lib/settings.cjs");
const { PythonBridge } = require("./lib/bridge.cjs");
const { VoiceController } = require("./lib/voice.cjs");
const { KeyboardState } = require("./lib/keyboard.cjs");
const keyboardState = new KeyboardState();
let keyboardVisible = false,
  keyboardEpoch = 0,
  keyboardPending = 0,
  keyboardTail = Promise.resolve();
if (process.env.CEC_USER_DATA_DIR) {
  fs.mkdirSync(process.env.CEC_USER_DATA_DIR, { recursive: true });
  app.setPath("userData", path.resolve(process.env.CEC_USER_DATA_DIR));
}
const hasInstanceLock = app.requestSingleInstanceLock();
if (!hasInstanceLock) app.quit();
app.on("second-instance", () => {
  if (mainWindow) openApp();
});
const renderer = (name) => path.join(__dirname, "renderer", name);
const MAIN_URL = pathToFileURL(renderer("index.html")).href;
const INDICATOR_URL = pathToFileURL(renderer("indicator.html")).href;
const KEYBOARD_URL = pathToFileURL(renderer("keyboard.html")).href;
let mainWindow, indicator, keyboard, bridge, store, tray, voice;
let voiceShortcutAvailable = false;
let snapshot = {
  phase: "disconnected",
  mode: "live",
  connected: false,
  running: false,
};
let busy = false,
  docked = false,
  quitting = false,
  shortcutAvailable = false,
  stopTimer;
const ACTIVE = new Set(["planning", "moving", "stopping", "camera"]);
function keyboardSnapshot() {
  return {
    visible: keyboardVisible,
    shift: keyboardState.shift,
    caps: keyboardState.caps,
    camera: snapshot.phase === "camera",
  };
}
function setKeyboardVisible(visible) {
  if (typeof visible !== "boolean")
    throw new Error("Choose keyboard visibility");
  if (visible && voice?.active)
    throw new Error("Finish voice input before opening the air keyboard");
  if (visible && (!snapshot.connected || (busy && snapshot.phase !== "camera")))
    throw new Error("Connect an idle desktop or enable hand control first");
  keyboardVisible = visible;
  keyboardEpoch++;
  keyboardState.reset();
  if (visible) {
    const area = screen.getDisplayNearestPoint(
      screen.getCursorScreenPoint(),
    ).workArea;
    const width = Math.min(960, area.width - 24),
      height = Math.min(350, area.height - 24);
    keyboard.setBounds({
      x: Math.round(area.x + (area.width - width) / 2),
      y: area.y + area.height - height - 12,
      width,
      height,
    });
    keyboard.showInactive();
  } else keyboard?.hide();
  broadcast({ event: "keyboard-state", data: keyboardSnapshot() });
  return keyboardSnapshot();
}
function broadcast(message) {
  if (quitting) return;
  if (message.event === "snapshot") snapshot = { ...message.data };
  if (message.event === "phase") snapshot = { ...snapshot, ...message.data };
  if (message.event === "keyboard-toggle" && snapshot.phase === "camera") {
    // A queued gesture may arrive while Stop/disconnect is unwinding.
    try {
      setKeyboardVisible(!keyboardVisible);
    } catch {
      /* Ownership has ended. */
    }
  }
  if (
    message.event === "phase" &&
    keyboardVisible &&
    snapshot.phase === "camera"
  )
    broadcast({ event: "keyboard-state", data: keyboardSnapshot() });
  if (message.event === "hand-stopped" && keyboardVisible)
    setKeyboardVisible(false);
  if (
    ["phase", "snapshot"].includes(message.event) &&
    keyboardVisible &&
    (!snapshot.connected ||
      ["planning", "moving", "stopping"].includes(snapshot.phase))
  )
    setKeyboardVisible(false);
  for (const win of [mainWindow, indicator, keyboard])
    if (win && !win.isDestroyed()) win.webContents.send("cec:event", message);
  if (["phase", "snapshot"].includes(message.event)) updateIndicator();
}
function dotIcon(color) {
  const bytes = Buffer.alloc(24 * 24 * 4);
  const rgb = color === "active" ? [255, 106, 61] : [137, 139, 144];
  for (let y = 0; y < 24; y++)
    for (let x = 0; x < 24; x++) {
      const i = (y * 24 + x) * 4;
      if ((x - 11.5) ** 2 + (y - 11.5) ** 2 < 70) {
        bytes[i] = rgb[2];
        bytes[i + 1] = rgb[1];
        bytes[i + 2] = rgb[0];
        bytes[i + 3] = 255;
      }
    }
  return nativeImage.createFromBitmap(bytes, {
    width: 24,
    height: 24,
    scaleFactor: 1,
  });
}
function updateIndicator() {
  if (quitting) return;
  const active = ACTIVE.has(snapshot.phase);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setTitle(
      `Continuous Cursor · ${snapshot.phase === "moving" ? "Moving" : snapshot.phase.charAt(0).toUpperCase() + snapshot.phase.slice(1)}`,
    );
    mainWindow.setProgressBar(active ? 2 : -1, { mode: "indeterminate" });
    if (process.platform === "win32")
      mainWindow.setOverlayIcon(
        active ? dotIcon("active") : null,
        active ? "Cursor control is active" : "",
      );
  }
  if (indicator && !indicator.isDestroyed()) {
    indicator.setFocusable(docked && !busy);
    if (
      docked ||
      (snapshot.connected &&
        store.settings.indicator &&
        snapshot.mode === "live")
    )
      indicator.showInactive();
    else indicator.hide();
  }
  if (tray && !tray.isDestroyed()) {
    tray.setImage(dotIcon(active ? "active" : "idle"));
    tray.setToolTip(
      `Continuous Cursor: ${snapshot.phase}${snapshot.mode === "simulation" ? " (simulation)" : ""}`,
    );
  }
}
function setupBridge() {
  const instance = new PythonBridge({
    root: app.isPackaged
      ? process.resourcesPath
      : path.resolve(__dirname, ".."),
    packaged: app.isPackaged,
    pythonPath: store.settings.pythonPath,
  });
  bridge = instance;
  instance.on("event", (message) => {
    if (bridge === instance) broadcast(message);
  });
  instance.on("exit", () => {
    if (bridge !== instance) return;
    busy = false;
    snapshot = {
      phase: "disconnected",
      connected: false,
      running: false,
      mode: snapshot.mode,
    };
    broadcast({ event: "snapshot", data: snapshot });
  });
  return instance;
}
async function stop() {
  voice?.cancel();
  setKeyboardVisible(false);
  if (mainWindow && !mainWindow.isDestroyed())
    mainWindow.webContents.send("cec:event", {
      event: "stop-requested",
      data: {},
    });
  const current = bridge;
  if (!current?.child) return { stopped: true };
  clearTimeout(stopTimer);
  await current.request("stop", {}, 3000).catch(() => {});
  // If a provider request cannot unwind, terminate only this app's owned worker.
  stopTimer = setTimeout(() => {
    if (bridge === current && busy) current.kill();
  }, 1500);
  return { stopping: true };
}
function register(channel, handler, overlay = false, withSender = false) {
  ipcMain.handle("cec:" + channel, async (event, ...args) => {
    const url = event.senderFrame?.url?.split("#")[0];
    if (
      url !== MAIN_URL &&
      !(overlay && [INDICATOR_URL, KEYBOARD_URL].includes(url))
    )
      throw new Error("Untrusted app frame");
    try {
      return await (withSender
        ? handler(event.sender.id, ...args)
        : handler(...args));
    } catch (error) {
      const key = store.config().apiKey;
      throw new Error(
        key
          ? String(error.message).replaceAll(key, "[redacted]")
          : error.message,
      );
    }
  });
}
function openApp() {
  if (voice?.active) voice.cancel();
  docked = false;
  indicator.setSize(342, 74);
  indicator.setFocusable(false);
  broadcast({ event: "dock", data: { docked } });
  updateIndicator();
  mainWindow.show();
  mainWindow.focus();
}
function openDock() {
  if (voice?.active) voice.cancel();
  if (!snapshot.connected)
    throw new Error("Connect a desktop or playground first");
  docked = true;
  const area = screen.getDisplayNearestPoint(
    screen.getCursorScreenPoint(),
  ).workArea;
  const width = Math.min(460, area.width - 24);
  indicator.setBounds({
    x: area.x + area.width - width - 12,
    y: area.y + 12,
    width,
    height: 292,
  });
  broadcast({ event: "dock", data: { docked } });
  mainWindow.hide();
  updateIndicator();
  indicator.show();
  return { docked };
}
app.whenReady().then(() => {
  if (!hasInstanceLock) return;
  store = new SettingsStore(
    path.join(app.getPath("userData"), "settings.json"),
    safeStorage,
  );
  voice = new VoiceController(
    () =>
      new PythonBridge({
        root: app.isPackaged
          ? process.resourcesPath
          : path.resolve(__dirname, ".."),
        packaged: app.isPackaged,
        pythonPath: store.settings.pythonPath,
        module: "embodied_cursor.voice",
        environment: {
          ...process.env,
          CEC_VOICE_MODEL_DIR:
            process.env.CEC_VOICE_MODEL_DIR ||
            path.join(app.getPath("userData"), "voice-model"),
        },
      }),
    () => {
      broadcast({ event: "voice-reset", data: {} });
      broadcast({ event: "voice-state", data: { active: false } });
    },
  );
  const webPreferences = {
    preload: path.join(__dirname, "preload.cjs"),
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    backgroundThrottling: false,
  };
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 1024,
    minHeight: 720,
    title: "CEC / Control room",
    icon: path.join(
      __dirname,
      "assets",
      process.platform === "win32" ? "icon.ico" : "icon.png",
    ),
    backgroundColor: "#101215",
    show: false,
    webPreferences,
  });
  const cameraPermission = (contents, permission, details) => {
    if (permission !== "media") return false;
    const types =
      details?.mediaTypes || (details?.mediaType ? [details.mediaType] : []);
    if (types.length !== 1) return false;
    const url = contents.getURL().split("#")[0];
    if (types[0] === "video")
      return contents === mainWindow?.webContents && url === MAIN_URL;
    return (
      types[0] === "audio" &&
      [MAIN_URL, INDICATOR_URL].includes(url) &&
      voice.permits(contents.id)
    );
  };
  mainWindow.webContents.session.setPermissionRequestHandler(
    (contents, permission, callback, details) => {
      callback(cameraPermission(contents, permission, details));
    },
  );
  mainWindow.webContents.session.setPermissionCheckHandler(
    (contents, permission, _origin, details) =>
      cameraPermission(contents, permission, details),
  );
  indicator = new BrowserWindow({
    width: 342,
    height: 74,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    show: false,
    backgroundColor: "#151b20",
    webPreferences,
  });
  keyboard = new BrowserWindow({
    width: 960,
    height: 350,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    show: false,
    backgroundColor: "#10171d",
    webPreferences,
  });
  const area = screen.getPrimaryDisplay().workArea;
  indicator.setPosition(area.x + area.width - 362, area.y + 20);
  for (const win of [mainWindow, indicator, keyboard]) {
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    win.webContents.on("will-navigate", (event) => event.preventDefault());
    win.webContents.on("will-attach-webview", (event) =>
      event.preventDefault(),
    );
  }
  mainWindow.setMenuBarVisibility(false);
  setupBridge();
  register(
    "state",
    () => ({
      ...snapshot,
      busy,
      platform: process.platform,
      shortcutAvailable,
      docked,
      voiceShortcutAvailable,
      voiceActive: voice.active,
      voiceAutoRun: store.settings.voiceAutoRun,
    }),
    true,
  );
  register("settings", () => store.public());
  register("save-settings", async (value) => {
    if (busy || voice.active)
      throw new Error(
        "Finish the task or voice recording before changing settings",
      );
    const oldPython = store.settings.pythonPath;
    const result = store.save(value);
    if (oldPython !== store.settings.pythonPath) {
      voice.cancel();
      bridge.close();
      bridge.kill();
      setupBridge();
      broadcast({
        event: "snapshot",
        data: { phase: "disconnected", mode: snapshot.mode, connected: false },
      });
    }
    updateIndicator();
    if (bridge.child)
      await bridge.request("configure-substrates", {
        enabled: store.settings.substratesEnabled,
      });
    return result;
  });
  register("connect", async (mode) => {
    if (!["live", "simulation"].includes(mode) || busy)
      throw new Error("Choose a mode while no task is running");
    const result = await bridge.request("connect", { mode });
    await bridge.request("configure-substrates", {
      enabled: store.settings.substratesEnabled,
    });
    return result;
  });
  register("disconnect", async () => {
    await stop();
    const current = bridge;
    try {
      return await current.request("disconnect", {}, 3000);
    } catch {
      current.kill();
      return { disconnected: true };
    }
  });
  register("models", () =>
    bridge.request("models", { config: store.config() }, 20000),
  );
  register("test-provider", () =>
    bridge.request("test-provider", { config: store.config() }, 55000),
  );
  register("dock", openDock);
  register(
    "voice",
    async (owner, op, payload) => {
      if (op === "begin" && (busy || !snapshot.connected))
        throw new Error(
          "Connect a desktop and finish the current task before recording",
        );
      try {
        return await voice.handle(owner, op, payload);
      } finally {
        broadcast({ event: "voice-state", data: { active: voice.active } });
      }
    },
    true,
    true,
  );
  register(
    "clear-context",
    () => {
      if (busy)
        throw new Error("Stop the current task before clearing context");
      return bridge.request("clear-context");
    },
    true,
  );
  register(
    "run",
    async (goal, options) => {
      if (options?.voiceToken !== undefined &&
          (!store.settings.voiceAutoRun || options.voiceToken !== voice.epoch))
        throw new Error(
          "Automatic voice execution is off or that command was cancelled. Review the transcript and choose Run",
        );
      if (busy || voice.active || !snapshot.connected)
        throw new Error(
          "Connect a desktop and wait for the current task to finish",
        );
      if (typeof goal !== "string" || !goal.trim() || goal.length > 4000)
        throw new Error("Enter a goal of up to 4,000 characters");
      busy = true;
      indicator.setFocusable(false);
      indicator.blur();
      mainWindow.blur();
      if (store.settings.minimizeOnRun && snapshot.mode === "live")
        mainWindow.minimize();
      try {
        return await bridge.request(
          "run",
          {
            goal,
            config: store.config(),
            controllerWindows: [mainWindow, indicator, keyboard].map((win) => {
              const handle = win.getNativeWindowHandle();
              return (
                handle.length >= 8
                  ? handle.readBigUInt64LE()
                  : BigInt(handle.readUInt32LE())
              ).toString();
            }),
          },
          600000,
        );
      } finally {
        busy = false;
        clearTimeout(stopTimer);
        updateIndicator();
      }
    },
    true,
  );
  register("camera", async () => {
    if (busy || voice.active || !snapshot.connected)
      throw new Error("Connect a desktop and finish the current task first");
    busy = true;
    try {
      return await bridge.request("camera", {}, 0);
    } finally {
      busy = false;
      clearTimeout(stopTimer);
    }
  });
  register("keyboard-state", keyboardSnapshot, true);
  register(
    "keyboard-toggle",
    (visible) => setKeyboardVisible(visible ?? !keyboardVisible),
    true,
  );
  register(
    "keyboard-calibrate",
    () => {
      if (!keyboardVisible || snapshot.phase !== "camera")
        throw new Error(
          "Enable camera and open the keyboard before calibrating",
        );
      broadcast({ event: "keyboard-calibrate", data: {} });
      return { calibrating: true };
    },
    true,
  );
  register("keyboard-feedback", (data) => {
    if (!keyboardVisible || snapshot.phase !== "camera") return;
    if (
      !data ||
      !Array.isArray(data.tips) ||
      data.tips.length > 8 ||
      typeof data.status !== "string" ||
      data.status.length > 180 ||
      data.tips.some(
        (tip) =>
          !tip ||
          !Number.isFinite(tip.x) ||
          !Number.isFinite(tip.y) ||
          typeof tip.id !== "string",
      )
    )
      throw new Error("Invalid keyboard feedback");
    broadcast({ event: "keyboard-feedback", data });
  });
  register(
    "keyboard-press",
    (id) => {
      if (typeof id !== "string" || keyboardPending >= 24)
        throw new Error("Keyboard input queue is full");
      const epoch = keyboardEpoch;
      keyboardPending++;
      const result = keyboardTail
        .then(async () => {
          if (
            epoch !== keyboardEpoch ||
            !keyboardVisible ||
            !snapshot.connected ||
            (busy && snapshot.phase !== "camera")
          )
            throw new Error("Keyboard input is paused");
          if (id === "HideKeyboard") return setKeyboardVisible(false);
          const input = keyboardState.press(id);
          if (input) await bridge.request("keyboard-input", input, 2000);
          broadcast({ event: "keyboard-press", data: { id } });
          broadcast({ event: "keyboard-state", data: keyboardSnapshot() });
          return { typed: Boolean(input) };
        })
        .finally(() => {
          keyboardPending--;
        });
      keyboardTail = result.catch(() => {});
      return result;
    },
    true,
  );
  register("hand", (sample) => bridge.request("hand", { sample }, 2000));
  register("stop", stop, true);
  register("open-app", openApp, true);
  register("export-trace", async () => {
    if (busy) throw new Error("Wait for the task to finish before exporting");
    const chosen = await dialog.showSaveDialog(mainWindow, {
      defaultPath: "cursor-trace.json",
      filters: [{ name: "Cursor trace", extensions: ["json"] }],
    });
    if (chosen.canceled) return { canceled: true };
    const data = await bridge.request("trace");
    fs.writeFileSync(chosen.filePath, JSON.stringify(data, null, 2), "utf8");
    return { saved: true };
  });
  register("open-external", (name) => {
    const urls = {
      keys: "https://ollama.com/settings/keys",
      models: "https://ollama.com/search?c=cloud",
      docs: "https://github.com/AaronGrace978/Continuous-Embodied-Cursor#electron-desktop-app",
    };
    if (!Object.hasOwn(urls, name)) throw new Error("Unsupported link");
    return shell.openExternal(urls[name]);
  });
  shortcutAvailable = globalShortcut.register(
    "CommandOrControl+Alt+Escape",
    () => {
      void stop();
    },
  );
  voiceShortcutAvailable = globalShortcut.register(
    "CommandOrControl+Alt+Space",
    () => {
      if (busy || !snapshot.connected) return;
      const owner = [mainWindow, indicator].find(
        (win) => win?.webContents.id === voice.owner,
      );
      const target = owner || (docked ? indicator : mainWindow);
      if (target && !target.isDestroyed())
        target.webContents.send("cec:event", {
          event: "voice-toggle",
          data: {},
        });
    },
  );
  globalShortcut.register("CommandOrControl+Alt+K", () => {
    try {
      setKeyboardVisible(!keyboardVisible);
    } catch {
      /* Connect first. */
    }
  });
  tray = new Tray(dotIcon("idle"));
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open Continuous Cursor", click: openApp },
      {
        label: "Stop cursor",
        click: () => {
          void stop();
        },
      },
      { type: "separator" },
      { label: "Quit", click: () => app.quit() },
    ]),
  );
  tray.on("double-click", openApp);
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
    if (!quitting) app.quit();
  });
  mainWindow.loadFile(renderer("index.html"));
  indicator.loadFile(renderer("indicator.html"));
  keyboard.loadFile(renderer("keyboard.html"));
});
app.on("before-quit", () => {
  if (quitting) return;
  quitting = true;
  voice?.cancel();
  globalShortcut.unregisterAll();
  clearTimeout(stopTimer);
  bridge?.close();
  const current = bridge;
  setTimeout(() => current?.kill(), 1000).unref();
  tray?.destroy();
  tray = null;
});
app.on("window-all-closed", () => app.quit());
