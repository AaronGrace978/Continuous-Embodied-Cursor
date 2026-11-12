// SPDX-License-Identifier: AGPL-3.0-or-later
(function (root, factory) {
  const exports = factory();
  if (typeof module === "object" && module.exports) module.exports = exports;
  else root.LocalVoice = exports;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function encodeWave(chunks, count) {
    const bytes = new Uint8Array(44 + count * 2),
      view = new DataView(bytes.buffer);
    const word = (offset, text) => {
      for (let i = 0; i < text.length; i++)
        bytes[offset + i] = text.charCodeAt(i);
    };
    word(0, "RIFF");
    view.setUint32(4, 36 + count * 2, true);
    word(8, "WAVEfmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 16000, true);
    view.setUint32(28, 32000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    word(36, "data");
    view.setUint32(40, count * 2, true);
    let index = 44;
    for (const chunk of chunks)
      for (const sample of chunk) {
        const value = Number.isFinite(sample)
          ? Math.max(-1, Math.min(1, sample))
          : 0;
        view.setInt16(
          index,
          Math.round(value * (value < 0 ? 32768 : 32767)),
          true,
        );
        index += 2;
      }
    return bytes;
  }
  function base64(bytes) {
    let text = "";
    for (let i = 0; i < bytes.length; i += 16384)
      text += String.fromCharCode(...bytes.subarray(i, i + 16384));
    return btoa(text);
  }
  class Capture {
    constructor({
      api,
      onState = () => {},
      onTranscript = () => {},
      getUserMedia,
      AudioContextClass,
    }) {
      this.api = api;
      this.onState = onState;
      this.onTranscript = onTranscript;
      this.getUserMedia =
        getUserMedia ||
        (() =>
          navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true },
            video: false,
          }));
      this.AudioContextClass = AudioContextClass || globalThis.AudioContext;
      this.epoch = 0;
      this.phase = "idle";
    }
    get active() {
      return this.phase !== "idle";
    }
    state(phase, message) {
      this.phase = phase;
      this.onState({ phase, message });
    }
    release() {
      clearTimeout(this.timer);
      this.stream?.getTracks().forEach((track) => {
        track.onended = null;
        track.stop();
      });
      this.stream = null;
      if (this.processor) {
        this.processor.onaudioprocess = null;
        this.processor.disconnect();
      }
      this.source?.disconnect();
      this.mute?.disconnect();
      const context = this.context;
      this.context = this.source = this.processor = this.mute = null;
      return context?.close().catch(() => {});
    }
    async start() {
      if (this.active) return;
      const epoch = ++this.epoch;
      this.state("starting", "Opening microphone…");
      try {
        await this.api.voice("begin");
        if (epoch !== this.epoch) return;
        const stream = await this.getUserMedia();
        if (epoch !== this.epoch) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        this.stream = stream;
        stream.getTracks().forEach((track) => {
          track.onended = () => {
            if (epoch === this.epoch) this.cancel();
          };
        });
        const context = new this.AudioContextClass({ sampleRate: 16000 });
        this.context = context;
        if (context.sampleRate !== 16000)
          throw new Error("This device could not open 16 kHz audio");
        this.chunks = [];
        this.count = 0;
        this.source = context.createMediaStreamSource(stream);
        this.processor = context.createScriptProcessor(4096, 1, 1);
        this.mute = context.createGain();
        this.mute.gain.value = 0;
        this.source.connect(this.processor);
        this.processor.connect(this.mute);
        this.mute.connect(context.destination);
        this.processor.onaudioprocess = (event) => {
          if (epoch !== this.epoch || this.phase !== "recording") return;
          const chunk = event.inputBuffer
            .getChannelData(0)
            .slice(0, 480000 - this.count);
          this.chunks.push(chunk);
          this.count += chunk.length;
          this.onState({
            phase: "recording",
            message: `Listening · ${Math.ceil(this.count / 16000)} / 30 seconds`,
          });
          if (this.count >= 480000) void this.finish();
        };
        await context.resume();
        if (epoch !== this.epoch) return;
        this.state(
          "recording",
          "Listening locally. Press Voice again to finish.",
        );
        this.timer = setTimeout(() => void this.finish(), 30000);
      } catch (error) {
        if (epoch !== this.epoch) return;
        await this.release();
        await this.api.voice("cancel").catch(() => {});
        this.state(
          "idle",
          error.name === "NotAllowedError"
            ? "Microphone permission was denied. Typed commands still work."
            : error.message,
        );
      }
    }
    async finish() {
      if (this.phase === "starting") return this.cancel();
      if (this.phase !== "recording") return;
      const epoch = this.epoch,
        chunks = this.chunks,
        count = this.count;
      this.state("transcribing", "Transcribing on this computer…");
      try {
        await this.release();
        await this.api.voice("end");
        if (epoch !== this.epoch) return;
        const result = await this.api.voice("transcribe", {
          audio: base64(encodeWave(chunks, count)),
        });
        if (epoch !== this.epoch) return;
        this.chunks = [];
        this.state("idle", "Transcript ready. Review it, then Run.");
        await this.onTranscript(result.text, result.token);
      } catch (error) {
        if (epoch !== this.epoch) return;
        await this.api.voice("cancel").catch(() => {});
        this.state("idle", error.message);
      }
    }
    cancel(notify = true) {
      const wasActive = this.active;
      this.epoch++;
      this.chunks = [];
      this.count = 0;
      void this.release();
      if (wasActive || notify) this.state("idle", "Voice input stopped");
      if (notify) void this.api.voice("cancel").catch(() => {});
    }
    toggle() {
      return this.phase === "idle"
        ? this.start()
        : this.phase === "recording" || this.phase === "starting"
          ? this.finish()
          : this.cancel();
    }
  }
  return { Capture, encodeWave };
});
