// SPDX-License-Identifier: AGPL-3.0-or-later
const { contextBridge, ipcRenderer } = require("electron");
const invoke = (channel, ...args) =>
  ipcRenderer.invoke("cec:" + channel, ...args);
contextBridge.exposeInMainWorld(
  "cursor",
  Object.freeze({
    state: () => invoke("state"),
    settings: () => invoke("settings"),
    saveSettings: (value) => invoke("save-settings", value),
    models: () => invoke("models"),
    testProvider: () => invoke("test-provider"),
    connect: (mode) => invoke("connect", mode),
    disconnect: () => invoke("disconnect"),
    run: (goal) => invoke("run", goal),
    stop: () => invoke("stop"),
    exportTrace: () => invoke("export-trace"),
    openApp: () => invoke("open-app"),
    openExternal: (name) => invoke("open-external", name),
    onEvent: (callback) => {
      const handler = (_event, message) => callback(message);
      ipcRenderer.on("cec:event", handler);
      return () => ipcRenderer.removeListener("cec:event", handler);
    },
  }),
);
