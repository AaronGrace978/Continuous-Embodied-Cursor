// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { SettingsStore, validate } = require("../lib/settings.cjs");
function file(t) {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "cec-settings-"));
  t.after(() => fs.rmSync(folder, { recursive: true, force: true }));
  return path.join(folder, "settings.json");
}
const storage = {
  isEncryptionAvailable: () => true,
  getSelectedStorageBackend: () => "test",
  encryptString: (text) => Buffer.from("encrypted:" + text),
  decryptString: (data) => data.toString().slice(10),
};
test("key is encrypted on disk, retained across saves, and never returned to renderer", (t) => {
  const filename = file(t),
    store = new SettingsStore(filename, storage, {});
  const publicSettings = store.save({
    apiKey: "secret-value",
    model: "example",
  });
  assert.equal(publicSettings.apiKeyConfigured, true);
  assert.equal(publicSettings.keyStorage, "encrypted");
  assert.equal(Object.hasOwn(publicSettings, "apiKey"), false);
  assert.equal(
    fs.readFileSync(filename, "utf8").includes("secret-value"),
    false,
  );
  store.save({ provider: "offline" });
  assert.equal(store.config().apiKey, "secret-value");
  const reloaded = new SettingsStore(filename, storage, {});
  assert.equal(reloaded.config().apiKey, "secret-value");
  reloaded.save({ clearKey: true });
  assert.equal(reloaded.public().apiKeyConfigured, false);
});
test("Linux basic_text fallback keeps key only in memory", (t) => {
  const filename = file(t),
    store = new SettingsStore(
      filename,
      { ...storage, getSelectedStorageBackend: () => "basic_text" },
      {},
    );
  store.save({ apiKey: "session-secret" });
  assert.equal(store.public().keyStorage, "session");
  assert.equal(
    fs.readFileSync(filename, "utf8").includes("session-secret"),
    false,
  );
  assert.equal(
    new SettingsStore(filename, storage, {}).public().apiKeyConfigured,
    false,
  );
});
test("environment key is used without being persisted or returned", (t) => {
  const filename = file(t),
    store = new SettingsStore(filename, storage, {
      OLLAMA_API_KEY: "environment-secret",
    });
  assert.equal(store.config().apiKey, "environment-secret");
  assert.equal(store.public().keyStorage, "environment");
  store.save({ model: "test" });
  assert.equal(
    fs.readFileSync(filename, "utf8").includes("environment-secret"),
    false,
  );
});
test("invalid local endpoints, provider names and values are rejected", () => {
  for (const localUrl of [
    "http://evil.example",
    "http://secret@localhost",
    "http://localhost/path",
    "file:///tmp/key",
  ])
    assert.throws(() => validate({ localUrl }));
  assert.throws(() => validate({ provider: "shell" }));
  assert.throws(() => validate({ indicator: "true" }));
  assert.throws(() => validate({ pythonPath: ["python", "-c", "code"] }));
  assert.throws(() => validate({ speed: "unsafe" }));
  assert.throws(() => validate({ localFirst: "yes" }));
  assert.equal(validate({ speed: "fast", localFirst: false }).speed, "fast");
});
