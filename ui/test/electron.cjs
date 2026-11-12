// SPDX-License-Identifier: AGPL-3.0-or-later
const { _electron: electron } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
(async () => {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "cec-electron-"));
  let app;
  let startupLog = "";
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
    app.process().stderr.on("data", (chunk) => {
      startupLog = (startupLog + chunk.toString()).slice(-12000);
    });
    // Windows are created and navigated independently. Poll the current set,
    // including windows that appeared before their initial navigation finished.
    const until = Date.now() + 30000;
    let page;
    while (!page && Date.now() < until) {
      page = app
        .windows()
        .find((candidate) => /\/index\.html(?:#.*)?$/.test(candidate.url()));
      if (!page) await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.ok(
      page,
      `Control room did not load; windows: ${app
        .windows()
        .map((win) => win.url())
        .join(", ")}\n${startupLog}`,
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
        "Session active · ready",
    );
    await page.click(".brand");
    await page.evaluate(() => {
      location.hash = "workspace";
    });
    assert.equal(
      (await page.evaluate(() => window.cursor.state())).connected,
      true,
    );
    assert.equal(
      (await page.evaluate(() => window.cursor.settings())).voiceAutoRun,
      false,
    );
    await page.click("#voiceButton");
    await page.waitForFunction(() =>
      /Prepare local voice|Install the voice extra/.test(
        document.getElementById("voiceStatus").textContent,
      ),
    );
    assert.equal(
      (await page.evaluate(() => window.cursor.state())).connected,
      true,
    );
    assert.equal(
      await page.evaluate(async () => {
        try {
          await window.cursor.run("click Export", { voiceToken: -1 });
          return false;
        } catch {
          return true;
        }
      }),
      true,
      "A cancelled voice command must not execute",
    );
    const voiceRuntime = await page.evaluate(() =>
      window.cursor.voice("status"),
    );
    if (voiceRuntime.available) {
      // Readiness metadata is a fixture; no decoding uses these dummy files.
      // Chromium's fake audio device exercises real permission/capture cleanup.
      const fixture = path.join(userData, "voice-model");
      fs.mkdirSync(fixture, { recursive: true });
      for (const name of [".cec-ready", "model.bin", "config.json"])
        fs.writeFileSync(
          path.join(fixture, name),
          "test-only readiness fixture",
        );
      await page.click("#voiceButton");
      await page.waitForFunction(
        () => voiceCapture.phase === "recording" && voiceCapture.count > 0,
      );
      await page.evaluate(() => voiceCapture.cancel());
      await page.waitForFunction(() => !voiceCapture.active);
      assert.equal(
        await page.evaluate(async () => {
          try {
            const stream = await navigator.mediaDevices.getUserMedia({
              audio: true,
            });
            stream.getTracks().forEach((track) => track.stop());
            return false;
          } catch {
            return true;
          }
        }),
        true,
        "Microphone access must end with the recording lease",
      );
      fs.rmSync(fixture, { recursive: true, force: true });
    }
    await page.click("#runButton");
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task complete"),
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
    assert.equal(
      (await page.evaluate(() => window.cursor.settings())).minimizeOnRun,
      false,
    );
    await page.click("#dockButton");
    const dock = app
      .windows()
      .find((candidate) => candidate.url().endsWith("/indicator.html"));
    assert.ok(dock);
    await dock.waitForSelector("#copilot:not([hidden])");
    assert.ok((await dock.evaluate(() => window.cursor.state())).connected);
    await dock.fill("#dockGoal", "fill Notes with still here");
    await dock.click("#send");
    await dock.waitForFunction(
      () =>
        !document.getElementById("send").disabled &&
        document
          .getElementById("sessionInfo")
          .textContent.includes("2 tasks finished"),
    );
    await dock.fill("#dockGoal", "click Export");
    await dock.click("#send");
    await dock.waitForFunction(
      () =>
        !document.getElementById("send").disabled &&
        document
          .getElementById("sessionInfo")
          .textContent.includes("3 tasks finished"),
    );
    fs.mkdirSync(path.resolve(__dirname, "../test-results"), {
      recursive: true,
    });
    await dock.screenshot({
      path: path.resolve(__dirname, "../test-results/copilot.png"),
    });
    await dock.click("#clearContext");
    await dock.waitForFunction(() =>
      document
        .getElementById("sessionInfo")
        .textContent.includes("0 remembered"),
    );
    assert.equal(
      (await dock.evaluate(() => window.cursor.state())).connected,
      true,
    );
    await dock.click("#openApp");
    await page.waitForFunction(
      async () => !(await window.cursor.state()).docked,
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
          "Session active · ready",
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
          'data:text/html,<title>CEC Native Motion Target</title><textarea id="target" autofocus style="width:90%;height:120px"></textarea>',
        );
        target.show();
        target.focus();
        return target.id;
      });
      const target = app
        .windows()
        .find((win) => win.url().startsWith("data:text/html"));
      await target.waitForSelector("#target");
      const moveReport = await page.evaluate(() =>
        window.cursor.run('move to window "CEC Native Motion Target"'),
      );
      assert.equal(moveReport.status, "done", JSON.stringify(moveReport));
      const nativePosition = await app.evaluate(
        ({ screen, BrowserWindow }, id) => ({
          pointer: screen.getCursorScreenPoint(),
          bounds: BrowserWindow.fromId(id).getBounds(),
          minimized: BrowserWindow.getAllWindows().some(
            (win) =>
              win.getTitle().includes("Continuous Cursor") && win.isMinimized(),
          ),
        }),
        targetId,
      );
      assert.ok(
        nativePosition.pointer.x >= nativePosition.bounds.x &&
          nativePosition.pointer.x <
            nativePosition.bounds.x + nativePosition.bounds.width,
      );
      assert.ok(
        nativePosition.pointer.y >= nativePosition.bounds.y &&
          nativePosition.pointer.y <
            nativePosition.bounds.y + nativePosition.bounds.height,
      );
      assert.equal(nativePosition.minimized, false);
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
        () => document.getElementById("historyCount").textContent === "6",
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
    // Backend exit can arrive after overlays and the tray are destroyed.
    // Exercise quitting with the keyboard visible instead of only an idle UI.
    await page.evaluate(async () => {
      await window.cursor.connect("simulation");
      await window.cursor.keyboardToggle(true);
    });
    console.log(
      "PASS: native Electron launch, sandboxed preload/IPC, real Python task, stop and indicator window.",
    );
  } catch (error) {
    const result = path.resolve(__dirname, "../test-results");
    fs.mkdirSync(result, { recursive: true });
    fs.writeFileSync(
      path.join(result, "electron-startup.log"),
      `${error.message}\n${startupLog}`,
      "utf8",
    );
    if (app) {
      for (const [index, page] of app.windows().entries())
        await page
          .screenshot({
            path: path.join(result, `electron-failure-${index}.png`),
            timeout: 2000,
          })
          .catch(() => {});
    }
    throw error;
  } finally {
    if (app) {
      let forced = false;
      const timer = setTimeout(() => {
        forced = true;
        app.process().kill("SIGKILL");
      }, 10000);
      try {
        await app.close();
      } finally {
        clearTimeout(timer);
      }
      if (forced)
        throw new Error("Electron did not shut down within 10 seconds");
    }
    fs.rmSync(userData, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
