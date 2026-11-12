// SPDX-License-Identifier: AGPL-3.0-or-later
// Owns one recording lease and an independent Python speech worker.
class VoiceController {
  constructor(createRuntime, onReset = () => {}) {
    this.createRuntime = createRuntime;
    this.onReset = onReset;
    this.runtime = null;
    this.owner = null;
    this.recording = false;
    this.processing = false;
    this.epoch = 0;
  }
  get active() {
    return this.owner !== null;
  }
  permits(id) {
    return this.owner === id && this.recording;
  }
  async request(op, data = {}, timeout = 15000) {
    const epoch = this.epoch;
    const runtime = (this.runtime ||= this.createRuntime());
    const result = await runtime.request(op, data, timeout);
    if (epoch !== this.epoch) throw new Error("Voice input was cancelled");
    return result;
  }
  async handle(id, op, payload = {}) {
    if (op === "cancel") {
      this.cancel();
      return { cancelled: true };
    }
    if (op === "status") return this.request("status");
    if (op === "prepare") {
      if (this.active || this.processing)
        throw new Error("Finish the current voice operation first");
      const epoch = this.epoch;
      this.processing = true;
      try {
        return await this.request("prepare", {}, 300000);
      } finally {
        if (epoch === this.epoch) this.processing = false;
      }
    }
    if (op === "begin") {
      if (this.active || this.processing)
        throw new Error("Voice input is already active");
      const status = await this.request("status");
      if (this.active || this.processing)
        throw new Error("Voice input is already active");
      if (!status.available) throw new Error(status.setup);
      if (!status.prepared)
        throw new Error(
          "Choose Prepare local voice in Settings before recording",
        );
      this.epoch++;
      this.owner = id;
      this.recording = true;
      this.timer = setTimeout(() => this.cancel(), 90000);
      this.timer.unref?.();
      return { recording: true, maxSeconds: 30 };
    }
    if (id !== this.owner)
      throw new Error("Start a voice recording in this window first");
    if (op === "end") {
      this.recording = false;
      return { recording: false };
    }
    if (op === "transcribe") {
      if (this.recording || this.processing)
        throw new Error("Finish recording before transcription");
      if (
        !payload ||
        typeof payload.audio !== "string" ||
        payload.audio.length > 1300000
      )
        throw new Error("Invalid voice recording");
      const epoch = this.epoch;
      this.processing = true;
      try {
        return {
          ...(await this.request(
            "transcribe",
            { audio: payload.audio },
            120000,
          )),
          token: epoch,
        };
      } finally {
        if (epoch === this.epoch) {
          this.owner = null;
          this.processing = false;
          clearTimeout(this.timer);
        }
      }
    }
    throw new Error("Unsupported voice operation");
  }
  cancel() {
    this.epoch++;
    this.owner = null;
    this.recording = false;
    this.processing = false;
    clearTimeout(this.timer);
    const runtime = this.runtime;
    this.runtime = null;
    runtime?.kill();
    this.onReset();
  }
}
module.exports = { VoiceController };
