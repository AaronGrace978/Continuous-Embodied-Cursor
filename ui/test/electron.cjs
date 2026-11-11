// SPDX-License-Identifier: AGPL-3.0-or-later
const { _electron: electron } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
(async () => {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "cec-electron-"));
  let app;
  try {
    const extra = (process.env.CEC_ELECTRON_TEST_ARGS || "")
      .split(" ")
      .filter(Boolean);
    app = await electron.launch({
      executablePath: process.env.CEC_PACKAGED_APP,
      args: [
        ...(process.env.CEC_PACKAGED_APP
          ? []
          : [path.resolve(__dirname, "..")]),
        ...extra,
      ],
      env: { ...process.env, CEC_USER_DATA_DIR: userData },
    });
    await app.firstWindow();
    const page = await Promise.any(app.windows().map(async candidate => {
      await candidate.waitForURL("**/index.html");
      return candidate;
    }));
    await page.waitForSelector("#trySimulation");
    assert.equal(await page.evaluate(() => typeof require), "undefined");
    const preferences = await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().map((win) =>
        win.webContents.getLastWebPreferences(),
      ),
    );
    assert.ok(
      preferences.every(
        (pref) =>
          pref.contextIsolation && pref.sandbox && !pref.nodeIntegration,
      ),
    );
    await page.click("#trySimulation");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent ===
        "Connected · idle",
    );
    await page.click(".brand");
    await page.evaluate(() => { location.hash = "workspace"; });
    assert.equal((await page.evaluate(() => window.cursor.state())).connected, true);
    await page.click("#runButton");
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task complete"),
    );
    await page.waitForFunction(() => !document.getElementById("goal").disabled);
    await page.fill("#goal", "move to Missing");
    await page.click("#runButton");
    await page.waitForFunction(
      () => !document.getElementById("stopDock").hidden,
    );
    await page.evaluate(() => window.cursor.stop());
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task interrupted"),
    );
    if (process.env.CEC_TEST_LIVE_INDICATOR === "1") {
      await page.waitForFunction(
        () => !document.getElementById("goal").disabled,
      );
      await page.click("#connectButton");
      await page.waitForFunction(
        () =>
          document.getElementById("statusLabel").textContent === "Disconnected",
      );
      await page.click("#liveMode");
      await page.click("#connectButton");
      await page.waitForFunction(
        () =>
          document.getElementById("statusLabel").textContent ===
          "Connected · idle",
      );
      const visible = await app.evaluate(({ BrowserWindow }) =>
        BrowserWindow.getAllWindows().some(
          (win) =>
            win.webContents.getURL().endsWith("indicator.html") &&
            win.isVisible() &&
            win.isAlwaysOnTop(),
        ),
      );
      assert.equal(visible, true);
      const strip = app
        .windows()
        .find((win) => win.url().endsWith("indicator.html"));
      await page.fill("#goal", "move to NonexistentControlForTest");
      await page.click("#runButton");
      await strip.waitForFunction(() =>
        document.getElementById("phase").textContent.includes("Cursor active"),
      );
      const result = path.resolve(__dirname, "../test-results");
      fs.mkdirSync(result, { recursive: true });
      await strip.screenshot({ path: path.join(result, "indicator.png") });
      await strip.evaluate(() => window.cursor.stop());
      await page.waitForFunction(
        () => document.getElementById("historyCount").textContent === "3",
      );
      await strip.evaluate(() => window.cursor.openApp());
      await page.click("#connectButton");
      await page.waitForFunction(
        () =>
          document.getElementById("statusLabel").textContent === "Disconnected",
      );
      console.log(
        "PASS: live X11 session, visible always-on-top indicator, active state and cancellation from indicator IPC.",
      );
    }
    const count = await app.evaluate(
      ({ BrowserWindow }) => BrowserWindow.getAllWindows().length,
    );
    assert.equal(count, 2);
    const result = path.resolve(__dirname, "../test-results");
    fs.mkdirSync(result, { recursive: true });
    await page.screenshot({ path: path.join(result, "electron.png") });
    console.log(
      "PASS: native Electron launch, sandboxed preload/IPC, real Python task, stop and indicator window.",
    );
  } finally {
    await app?.close();
    fs.rmSync(userData, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
