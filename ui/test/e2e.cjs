// SPDX-License-Identifier: AGPL-3.0-or-later
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { pathToFileURL } = require("node:url");
const { PythonBridge } = require("../lib/bridge.cjs");
const { KeyboardState } = require("../lib/keyboard.cjs");
const keyboardState = new KeyboardState();
let keyboardVisible = false;
const { SettingsStore } = require("../lib/settings.cjs");
(async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "cec-ui-e2e-"));
  const results = path.resolve(__dirname, "../test-results");
  fs.mkdirSync(results, { recursive: true });
  const bridge = new PythonBridge({ root: path.resolve(__dirname, "../..") });
  const store = new SettingsStore(
    path.join(folder, "settings.json"),
    { isEncryptionAvailable: () => false },
    {},
  );
  let snapshot = {
    phase: "disconnected",
    connected: false,
    running: false,
    mode: "live",
    platform: process.platform,
    shortcutAvailable: true,
  };
  let page, browser;
  const errors = [],
    calls = [];
  const assets = new Set([
    "index.html",
    "app.js",
    "camera.js",
    "air-keyboard.js",
    "icons.js",
    "styles.css",
    "fonts.css",
    "fonts/BarlowCondensed-Bold.ttf",
    "fonts/IBMPlexMono-Regular.ttf",
  ]);
  const server = http.createServer((req, res) => {
    const asset = req.url.slice(1);
    if (assets.has(asset)) {
      res.setHeader(
        "Content-Type",
        asset.endsWith(".html")
          ? "text/html"
          : asset.endsWith(".js")
            ? "text/javascript"
            : asset.endsWith(".ttf")
              ? "font/ttf"
              : "text/css",
      );
      res.end(fs.readFileSync(path.resolve(__dirname, "../renderer", asset)));
      return;
    }
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      calls.push({ path: req.url, body: body ? JSON.parse(body) : null });
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/api/tags")
        res.end(
          JSON.stringify({
            models: [{ name: "test-model" }, { name: "other-model" }],
          }),
        );
      else
        res.end(
          JSON.stringify({
            message: {
              content: JSON.stringify({
                intents: [{ action: "click", target: "Export" }],
                done: true,
              }),
            },
          }),
        );
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    browser = await chromium.launch({
      executablePath:
        process.env.CHROMIUM_PATH ||
        (fs.existsSync("/usr/bin/chromium") ? "/usr/bin/chromium" : undefined),
      headless: true,
      args: ["--no-sandbox"],
    });
    page = await browser.newPage({
      viewport: { width: 1440, height: 1060 },
      deviceScaleFactor: 1,
    });
    page.on("pageerror", (error) => errors.push(error.message));
    bridge.on("event", (message) => {
      if (message.event === "snapshot")
        snapshot = { ...snapshot, ...message.data };
      if (message.event === "phase")
        snapshot = { ...snapshot, ...message.data };
      if (page && !page.isClosed())
        void page
          .evaluate((message) => window.__cecDeliver?.(message), message)
          .catch(() => {});
    });
    await page.exposeFunction("__cecInvoke", async (op, arg) => {
      if (op === "keyboardState")
        return {
          visible: keyboardVisible,
          shift: keyboardState.shift,
          caps: keyboardState.caps,
        };
      if (op === "keyboardToggle") {
        keyboardVisible = arg ?? !keyboardVisible;
        await page.evaluate(
          (data) => window.__cecDeliver({ event: "keyboard-state", data }),
          { visible: keyboardVisible },
        );
        return { visible: keyboardVisible };
      }
      if (op === "keyboardCalibrate") {
        await page.evaluate(() =>
          window.__cecDeliver({ event: "keyboard-calibrate", data: {} }),
        );
        return {};
      }
      if (op === "keyboardFeedback") return {};
      if (op === "keyboardPress") {
        if (!keyboardVisible) throw new Error("Keyboard hidden");
        const input = keyboardState.press(arg);
        return input ? bridge.request("keyboard-input", input) : {};
      }
      if (op === "state") return snapshot;
      if (op === "settings") return store.public();
      if (op === "saveSettings") return store.save(arg);
      if (op === "connect") return bridge.request("connect", { mode: arg });
      if (op === "disconnect") return bridge.request("disconnect");
      if (op === "run")
        return bridge.request("run", { goal: arg, config: store.config() });
      if (op === "stop") return bridge.request("stop");
      if (op === "camera") return bridge.request("camera", {}, 0);
      if (op === "hand") return bridge.request("hand", { sample: arg });
      if (op === "models" || op === "testProvider")
        return bridge.request(op === "models" ? "models" : "test-provider", {
          config: store.config(),
        });
      if (op === "exportTrace") {
        await bridge.request("trace");
        return { saved: true };
      }
      if (op === "openExternal") return null;
      throw Error("Unexpected test IPC operation");
    });
    await page.addInitScript(() => {
      const subscribers = [];
      window.__cecDeliver = (message) =>
        subscribers.forEach((fn) => fn(message));
      window.cursor = {
        onEvent: (fn) => {
          subscribers.push(fn);
          return () => {};
        },
      };
      for (const op of [
        "state",
        "settings",
        "saveSettings",
        "connect",
        "disconnect",
        "run",
        "stop",
        "camera",
        "hand",
        "keyboardState",
        "keyboardToggle",
        "keyboardPress",
        "keyboardCalibrate",
        "keyboardFeedback",
        "models",
        "testProvider",
        "exportTrace",
        "openExternal",
      ])
        window.cursor[op] = (arg) => window.__cecInvoke(op, arg);
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/index.html`);
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent === "Disconnected",
    );
    assert.equal(await page.locator("#runButton").isDisabled(), true);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({
      path: path.join(results, "welcome.png"),
      fullPage: true,
    });
    await page.click("#trySimulation");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent ===
        "Connected · idle",
    );
    await page.waitForFunction(
      () => document.querySelectorAll("#mapSvg rect").length >= 6,
    );
    assert.equal(
      await page.locator("#modelName").textContent(),
      "No model needed",
    );
    assert.equal(await page.locator(".companion-eyes i").count(), 2);
    // An unavailable camera must leave the connected desktop usable.
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException("denied", "NotAllowedError");
      };
    });
    await page.click("#cameraButton");
    await page.waitForFunction(() =>
      document
        .getElementById("toast")
        .textContent.includes("permission was denied"),
    );
    assert.equal(await page.locator("#cameraPreview").isHidden(), true);
    await page.click("#runButton");
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task complete"),
    );
    await page.screenshot({
      path: path.join(results, "playground.png"),
      fullPage: true,
    });
    await page.click("#historyNav");
    assert.equal(
      await page.locator(".history-status").first().textContent(),
      "done",
    );
    await page.click("#workspaceNav");
    await page.click("#topSettings");
    await page.selectOption("#provider", "ollama-local");
    await page.fill("#localUrl", `http://127.0.0.1:${server.address().port}`);
    await page.fill("#model", "test-model");
    await page.click("#refreshModels");
    await page.waitForFunction(() =>
      document
        .getElementById("connectionResult")
        .textContent.includes("2 models loaded"),
    );
    await page.click("#testConnection");
    await page.waitForFunction(() =>
      document
        .getElementById("connectionResult")
        .textContent.includes("Connected to test-model"),
    );
    assert.ok(
      calls.some(
        (call) => call.path === "/api/chat" && call.body.stream === false,
      ),
    );
    await page.click("#settingsForm button[type=submit]");
    await page.fill("#goal", "click Export");
    await page.click("#runButton");
    await page.waitForFunction(
      () => document.getElementById("historyCount").textContent === "2",
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
    await page.click("#topSettings");
    await page.selectOption("#provider", "offline");
    await page.click("#settingsForm button[type=submit]");
    await page.fill("#goal", "move to Missing");
    await page.click("#runButton");
    await page.waitForFunction(
      () => !document.getElementById("stopDock").hidden,
    );
    await page.screenshot({
      path: path.join(results, "active.png"),
      fullPage: true,
    });
    await page.fill("#goal", "click Export");
    await page.click("#runButton");
    await page.waitForFunction(
      () => document.getElementById("queueCount").textContent === "1",
    );
    await page.click("#stopButton");
    await page.waitForFunction(() =>
      document
        .getElementById("activityFeed")
        .textContent.includes("Task interrupted"),
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
    assert.equal(await page.locator("#queueCount").textContent(), "1");
    assert.equal(await page.locator("#resumeQueue").isVisible(), true);
    await page.click("#resumeQueue");
    await page.waitForFunction(
      () => document.getElementById("historyCount").textContent === "4",
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
    assert.equal(await page.locator("#queueCard").isHidden(), true);
    await page.fill("#goal", 'fill Notes with "' + "a".repeat(480) + '"');
    await page.click("#runButton");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent === "Cursor active",
    );
    await page.fill("#goal", "click Export");
    await page.click("#runButton");
    await page.fill("#goal", "check Word wrap");
    await page.click("#runButton");
    await page.waitForFunction(
      () => document.getElementById("historyCount").textContent === "7",
    );
    await page.waitForFunction(
      () => document.getElementById("stopDock").hidden,
    );
    assert.equal(
      await page.locator("#historyList h3").nth(0).textContent(),
      "check Word wrap",
    );
    assert.equal(
      await page.locator("#historyList h3").nth(1).textContent(),
      "click Export",
    );
    assert.equal(await page.locator("#queueCard").isHidden(), true);
    // Exercise production camera ownership and Stop over the real Python IPC.
    // Only the hardware/model boundary is stubbed for this headless test.
    await page.evaluate(() => {
      HandCamera.prototype.start = async function () {
        this.active = true;
        return true;
      };
      HandCamera.prototype.track = function () {
        void this.onSample({ x: 0.5, y: 0.5, pinch: 1 });
      };
    });
    await bridge.request("run", { goal: "click Notes" });
    await page.click("#cameraButton");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent ===
        "Hand control active",
    );
    assert.equal(await page.locator("#runButton").isDisabled(), true);
    await page.click("#keyboardButton");
    await page.waitForFunction(() =>
      document.getElementById("keyboardButton").textContent.includes("Hide"),
    );
    await page.evaluate(async () => {
      const cell = AirKeyboard.keys.find((key) => key.id === "f");
      const x = 1 - airTyper.area.x - (cell.x + cell.w / 2) * airTyper.area.w;
      const y = airTyper.area.y + (cell.y + cell.h / 2) * airTyper.area.h;
      function sample(bent) {
        const points = Array.from({ length: 21 }, () => ({
          x: 0.99,
          y: 0.99,
          z: 0,
        }));
        points[5] = { x, y: y + 0.12, z: 0 };
        points[6] = { x, y: y + 0.07, z: 0 };
        points[8] = { x, y: y + (bent ? 0.13 : 0), z: 0 };
        return [{ id: "Left", points }];
      }
      for (const now of [0, 60, 120])
        await camera.onLandmarks(sample(false), now, 1);
      for (const now of [150, 200, 230])
        await camera.onLandmarks(sample(true), now, 1);
    });
    await page.click("#stopButton");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent ===
        "Connected · idle",
    );
    await page.waitForFunction(() =>
      state.elements.find((el) => el.name === "Notes")?.text?.endsWith("f"),
    );
    assert.equal(await page.locator("#cameraPreview").isHidden(), true);
    await page.click("#exportTrace");
    await page.waitForFunction(
      () => document.getElementById("toast").textContent === "Trace exported.",
    );
    await page.screenshot({
      path: path.join(results, "dashboard.png"),
      fullPage: true,
    });
    await page.click("#topSettings");
    await page.selectOption("#provider", "ollama-cloud");
    await page.fill("#apiKey", "a-test-key");
    await page.click("#settingsForm button[type=submit]");
    assert.equal(
      (await page.evaluate(() => window.cursor.settings())).apiKey,
      undefined,
    );
    assert.equal(
      fs
        .readFileSync(path.join(folder, "settings.json"), "utf8")
        .includes("a-test-key"),
      false,
    );
    await page.click("#connectButton");
    await page.waitForFunction(
      () =>
        document.getElementById("statusLabel").textContent === "Disconnected",
    );
    assert.equal(await page.locator("#runButton").isDisabled(), true);
    assert.deepEqual(errors, []);
    console.log(
      "PASS: Python-backed UI, planning, queued goals, pause/resume, camera lifecycle, cancellation, history, export, key storage and disconnect.",
    );
    console.log("Screenshots: ui/test-results/{welcome,active,dashboard}.png");
  } finally {
    bridge.close();
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(folder, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
