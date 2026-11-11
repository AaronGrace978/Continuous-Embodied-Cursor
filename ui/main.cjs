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
let mainWindow, indicator, bridge, store, tray;
let snapshot = {
  phase: "disconnected",
  mode: "live",
  connected: false,
  running: false,
};
let busy = false,
  quitting = false,
  shortcutAvailable = false,
  stopTimer;
const ACTIVE = new Set(["planning", "moving", "stopping"]);
function broadcast(message) {
  if (message.event === "snapshot") snapshot = { ...message.data };
  if (message.event === "phase") snapshot = { ...snapshot, ...message.data };
  for (const win of [mainWindow, indicator])
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
    if (
      snapshot.connected &&
      store.settings.indicator &&
      snapshot.mode === "live"
    )
      indicator.showInactive();
    else indicator.hide();
  }
  if (tray) {
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
function register(channel, handler, overlay = false) {
  ipcMain.handle("cec:" + channel, async (event, ...args) => {
    const url = event.senderFrame?.url?.split("#")[0];
    if (url !== MAIN_URL && !(overlay && url === INDICATOR_URL))
      throw new Error("Untrusted app frame");
    try {
      return await handler(...args);
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
  mainWindow.show();
  mainWindow.focus();
}
app.whenReady().then(() => {
  if (!hasInstanceLock) return;
  store = new SettingsStore(
    path.join(app.getPath("userData"), "settings.json"),
    safeStorage,
  );
  const webPreferences = {
    preload: path.join(__dirname, "preload.cjs"),
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
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
  const area = screen.getPrimaryDisplay().workArea;
  indicator.setPosition(area.x + area.width - 362, area.y + 20);
  for (const win of [mainWindow, indicator]) {
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
    }),
    true,
  );
  register("settings", () => store.public());
  register("save-settings", async (value) => {
    if (busy) throw new Error("Stop the current task before changing settings");
    const oldPython = store.settings.pythonPath;
    const result = store.save(value);
    if (oldPython !== store.settings.pythonPath) {
      bridge.close();
      bridge.kill();
      setupBridge();
      broadcast({
        event: "snapshot",
        data: { phase: "disconnected", mode: snapshot.mode, connected: false },
      });
    }
    updateIndicator();
    return result;
  });
  register("connect", async (mode) => {
    if (!["live", "simulation"].includes(mode) || busy)
      throw new Error("Choose a mode while no task is running");
    return bridge.request("connect", { mode });
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
  register("run", async (goal) => {
    if (busy || !snapshot.connected)
      throw new Error(
        "Connect a desktop and wait for the current task to finish",
      );
    if (typeof goal !== "string" || !goal.trim() || goal.length > 4000)
      throw new Error("Enter a goal of up to 4,000 characters");
    busy = true;
    if (store.settings.minimizeOnRun && snapshot.mode === "live")
      mainWindow.minimize();
    try {
      return await bridge.request("run", { goal, config: store.config() });
    } finally {
      busy = false;
      clearTimeout(stopTimer);
    }
  });
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
});
app.on("before-quit", () => {
  if (quitting) return;
  quitting = true;
  globalShortcut.unregisterAll();
  clearTimeout(stopTimer);
  bridge?.close();
  const current = bridge;
  setTimeout(() => current?.kill(), 1000).unref();
  tray?.destroy();
});
app.on("window-all-closed", () => app.quit());
