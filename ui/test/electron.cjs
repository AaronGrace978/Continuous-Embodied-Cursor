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
        "--use-fake-device-for-media-stream",
      ],
      env: { ...process.env, CEC_USER_DATA_DIR: userData },
    });
    await app.firstWindow();
    const page = await Promise.any(
      app.windows().map(async (candidate) => {
        await candidate.waitForURL("**/index.html");
        return candidate;
      }),
    );
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
    const media = await page.evaluate(async () => {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: true,
        audio: false,
      });
      const kinds = stream.getTracks().map((track) => track.kind);
      stream.getTracks().forEach((track) => track.stop());
      let microphoneDenied = false;
      try {
        const audio = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        audio.getTracks().forEach((track) => track.stop());
      } catch {
        microphoneDenied = true;
      }
      const vision = await import(
        "../renderer/vendor/vision/vision_bundle.mjs"
      );
      return {
        kinds,
        microphoneDenied,
        wasm: await vision.FilesetResolver.isSimdSupported(),
      };
    });
    assert.deepEqual(media.kinds, ["video"]);
    assert.equal(media.microphoneDenied, true);
    assert.equal(typeof media.wasm, "boolean");
    await page.click("#trySimulation");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent ===
        "Connected · idle",
    );
    await page.click(".brand");
    await page.evaluate(() => {
      location.hash = "workspace";
    });
    assert.equal(
      (await page.evaluate(() => window.cursor.state())).connected,
      true,
    );
    await page.click("#runButton");
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task complete"),
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
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
        () => document.getElementById("stopDock").hidden,
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
      // Real native text entry: the keyboard must never take focus from this app.
      const targetId = await app.evaluate(async ({ BrowserWindow }) => {
        const target = new BrowserWindow({
          width: 500,
          height: 240,
          x: 30,
          y: 100,
          webPreferences: { contextIsolation: true, sandbox: true },
        });
        await target.loadURL(
          'data:text/html,<textarea id="target" autofocus style="width:90%;height:120px"></textarea>',
        );
        target.show();
        target.focus();
        return target.id;
      });
      const target = app
        .windows()
        .find((win) => win.url().startsWith("data:text/html"));
      await target.waitForSelector("#target");
      await page.evaluate(() => window.cursor.keyboardToggle(true));
      const keyboard = app
        .windows()
        .find((win) => win.url().endsWith("keyboard.html"));
      await keyboard.waitForSelector('[data-key="f"]');
      assert.equal(
        await app.evaluate(
          ({ BrowserWindow }, id) => BrowserWindow.fromId(id).isFocused(),
          targetId,
        ),
        true,
      );
      for (const id of ["h", "i", "Shift", "1", "Backspace", "Enter", "a"])
        await keyboard.evaluate((id) => window.cursor.keyboardPress(id), id);
      await target.waitForFunction(
        () => document.getElementById("target").value === "hi\na",
      );
      assert.equal(
        await app.evaluate(
          ({ BrowserWindow }, id) => BrowserWindow.fromId(id).isFocused(),
          targetId,
        ),
        true,
      );
      await keyboard.waitForFunction(() => !document.querySelector(".pressed"));
      await keyboard.screenshot({
        path: path.resolve(__dirname, "../test-results/keyboard.png"),
      });
      await keyboard.evaluate(() =>
        window.cursor.keyboardPress("HideKeyboard"),
      );
      assert.equal(
        (await page.evaluate(() => window.cursor.keyboardState())).visible,
        false,
      );
      await app.evaluate(
        ({ BrowserWindow }, id) => BrowserWindow.fromId(id).destroy(),
        targetId,
      );
      await page.evaluate(() => window.cursor.openApp());
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
    assert.equal(count, 3);
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
