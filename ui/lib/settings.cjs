// SPDX-License-Identifier: AGPL-3.0-or-later
const fs = require("node:fs");
const path = require("node:path");
const DEFAULTS = Object.freeze({
  provider: "ollama-cloud",
  model: "gpt-oss:120b",
  localUrl: "http://127.0.0.1:11434",
  indicator: true,
  minimizeOnRun: true,
  pythonPath: "",
});
function validate(input, previous = DEFAULTS) {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new Error("Invalid settings");
  const next = { ...previous };
  for (const field of ["provider", "model", "localUrl", "pythonPath"]) {
    if (input[field] !== undefined) {
      if (typeof input[field] !== "string" || input[field].length > 1000)
        throw new Error(`Invalid ${field}`);
      next[field] = input[field].trim();
    }
  }
  if (!["ollama-cloud", "ollama-local", "offline"].includes(next.provider))
    throw new Error("Choose a supported planner");
  const url = new URL(next.localUrl);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !["", "/"].includes(url.pathname)
  )
    throw new Error("Local Ollama must use a loopback URL");
  for (const field of ["indicator", "minimizeOnRun"]) {
    if (input[field] !== undefined) {
      if (typeof input[field] !== "boolean")
        throw new Error(`Invalid ${field}`);
      next[field] = input[field];
    }
  }
  return next;
}
class SettingsStore {
  constructor(filename, safeStorage, environment = process.env) {
    this.filename = filename;
    this.safeStorage = safeStorage;
    this.environment = environment;
    this.settings = { ...DEFAULTS };
    this.key = "";
    this.encrypted = "";
    try {
      const stored = JSON.parse(fs.readFileSync(filename, "utf8"));
      this.settings = validate(stored);
      if (stored.encryptedKey && this.canEncrypt()) {
        this.key = safeStorage.decryptString(
          Buffer.from(stored.encryptedKey, "base64"),
        );
        this.encrypted = stored.encryptedKey;
      }
    } catch (error) {
      if (error.code !== "ENOENT")
        this.warning =
          "Saved settings could not be loaded. Re-enter your connection settings.";
    }
  }
  canEncrypt() {
    return (
      this.safeStorage.isEncryptionAvailable() &&
      (!this.safeStorage.getSelectedStorageBackend ||
        this.safeStorage.getSelectedStorageBackend() !== "basic_text")
    );
  }
  public() {
    return {
      ...this.settings,
      apiKeyConfigured: Boolean(this.key || this.environment.OLLAMA_API_KEY),
      keyStorage: this.key
        ? this.encrypted
          ? "encrypted"
          : "session"
        : this.environment.OLLAMA_API_KEY
          ? "environment"
          : "none",
      warning: this.warning || "",
    };
  }
  config() {
    return {
      ...this.settings,
      apiKey: this.key || this.environment.OLLAMA_API_KEY || "",
    };
  }
  save(input) {
    const next = validate(input, this.settings);
    let key = this.key;
    if (input.clearKey) key = "";
    if (input.apiKey !== undefined && input.apiKey !== "") {
      if (
        typeof input.apiKey !== "string" ||
        input.apiKey.length > 4096 ||
        /[\r\n]/.test(input.apiKey)
      )
        throw new Error("Invalid API key");
      key = input.apiKey.trim();
    }
    const encrypted =
      key && this.canEncrypt()
        ? this.safeStorage.encryptString(key).toString("base64")
        : "";
    fs.mkdirSync(path.dirname(this.filename), { recursive: true });
    fs.writeFileSync(
      this.filename + ".tmp",
      JSON.stringify({ ...next, encryptedKey: encrypted }, null, 2),
      { mode: 0o600 },
    );
    fs.renameSync(this.filename + ".tmp", this.filename);
    this.settings = next;
    this.key = key;
    this.encrypted = encrypted;
    this.warning = "";
    return this.public();
  }
}
module.exports = { SettingsStore, DEFAULTS, validate };
