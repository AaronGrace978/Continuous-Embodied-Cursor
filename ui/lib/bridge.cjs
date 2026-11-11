// SPDX-License-Identifier: AGPL-3.0-or-later
const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const fs = require("node:fs");
class PythonBridge extends EventEmitter {
  constructor({
    root,
    packaged = false,
    pythonPath = "",
    environment = process.env,
  }) {
    super();
    Object.assign(this, { root, environment, packaged, pythonPath });
    this.pending = new Map();
    this.counter = 0;
    this.child = null;
  }
  start() {
    if (!this.ready) this.ready = this._start();
    return this.ready;
  }
  async _start() {
    const virtual = path.join(
      this.root,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
    const explicit = this.pythonPath || this.environment.CEC_PYTHON;
    const candidates = explicit
      ? [[explicit, []]]
      : [
          ...(fs.existsSync(virtual) ? [[virtual, []]] : []),
          ...(process.platform === "win32"
            ? [
                ["py", ["-3"]],
                ["python", []],
              ]
            : [["python3", []]]),
        ];
    for (const [command, prefix] of candidates) {
      try {
        await this._spawn(command, prefix);
        return;
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    }
    throw new Error(
      "Python 3.10+ was not found. Choose your Python executable in Settings → Runtime.",
    );
  }
  _spawn(command, prefix) {
    return new Promise((resolve, reject) => {
      const source = this.packaged
        ? path.join(this.root, "python")
        : path.join(this.root, "src");
      const environment = {
        ...this.environment,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONPATH:
          source +
          (this.environment.PYTHONPATH
            ? path.delimiter + this.environment.PYTHONPATH
            : ""),
      };
      const child = spawn(
        command,
        [...prefix, "-u", "-m", "embodied_cursor.desktop"],
        {
          cwd: this.root,
          env: environment,
          windowsHide: true,
          stdio: ["pipe", "pipe", "pipe"],
          shell: false,
        },
      );
      this.child = child;
      let buffer = "",
        started = false;
      const timer = setTimeout(() => {
        child.kill();
        reject(
          new Error("Python backend did not start. Check Settings → Runtime."),
        );
      }, 15000);
      child.once("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.stderr.on("data", () => {});
      child.stdout.on("data", (chunk) => {
        buffer += chunk.toString("utf8");
        if (buffer.length > 8 * 1024 * 1024) {
          child.kill();
          return;
        }
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          let message;
          try {
            message = JSON.parse(line);
          } catch {
            continue;
          }
          if (message.event === "ready" && !started) {
            started = true;
            clearTimeout(timer);
            resolve();
          }
          if (message.event) this.emit("event", message);
          else if (this.pending.has(message.id)) {
            const pending = this.pending.get(message.id);
            this.pending.delete(message.id);
            clearTimeout(pending.timer);
            message.ok
              ? pending.resolve(message.result)
              : pending.reject(
                  new Error(message.error || "Desktop request failed"),
                );
          }
        }
      });
      child.once("exit", () => {
        clearTimeout(timer);
        if (!started)
          reject(
            new Error(
              "Python backend could not load. Select Python 3.10+ in Settings → Runtime.",
            ),
          );
        if (this.child === child) this.child = null;
        for (const pending of this.pending.values()) {
          clearTimeout(pending.timer);
          pending.reject(new Error("Desktop backend stopped"));
        }
        this.pending.clear();
        this.ready = null;
        this.emit("exit");
      });
    });
  }
  async request(op, data = {}, timeout = 180000) {
    await this.start();
    if (!this.child || this.child.killed)
      throw new Error("Desktop backend is disconnected");
    const id = ++this.counter;
    return new Promise((resolve, reject) => {
      const timer =
        timeout > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              if (op === "run") this.kill();
              reject(new Error("Desktop request timed out"));
            }, timeout)
          : null;
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(
        JSON.stringify({ id, op, ...data }) + "\n",
        (error) => {
          if (error && this.pending.delete(id)) {
            clearTimeout(timer);
            reject(new Error("Desktop backend is disconnected"));
          }
        },
      );
    });
  }
  kill() {
    if (this.child) this.child.kill();
  }
  close() {
    if (this.child) this.child.stdin.end();
  }
}
module.exports = { PythonBridge };
