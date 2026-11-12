// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require("node:test");
const assert = require("node:assert/strict");
const { VoiceController } = require("../lib/voice.cjs");
const { Capture } = require("../renderer/voice.js");
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("only the recording owner gets a microphone lease, and late transcripts are cancelled", async () => {
  const pending = deferred();
  let killed = 0;
  const voice = new VoiceController(() => ({
    request: async (op) =>
      op === "status" ? { available: true, prepared: true } : pending.promise,
    kill: () => killed++,
  }));
  await voice.handle(10, "begin");
  assert.equal(voice.permits(10), true);
  assert.equal(voice.permits(11), false);
  await assert.rejects(
    voice.handle(11, "transcribe", { audio: "AAAA" }),
    /Start a voice recording/,
  );
  await voice.handle(10, "end");
  assert.equal(voice.permits(10), false);
  const transcript = voice.handle(10, "transcribe", { audio: "AAAA" });
  voice.cancel();
  pending.resolve({ text: "must never run" });
  await assert.rejects(transcript, /cancelled/);
  assert.equal(voice.active, false);
  assert.equal(killed, 1);
});

test("a failed setup never grants microphone access and each recording gets a new execution token", async () => {
  let prepared = false;
  const voice = new VoiceController(() => ({
    request: async (op) =>
      op === "status"
        ? { available: true, prepared }
        : { text: "click Export" },
    kill() {},
  }));
  await assert.rejects(voice.handle(1, "begin"), /Prepare local voice/);
  assert.equal(voice.permits(1), false);
  prepared = true;
  await voice.handle(1, "begin");
  await voice.handle(1, "end");
  const first = await voice.handle(1, "transcribe", { audio: "AAAA" });
  assert.equal(first.token, voice.epoch);
  await voice.handle(1, "begin");
  assert.notEqual(first.token, voice.epoch);
  voice.cancel();
});

class Audio {
  constructor() {
    this.sampleRate = 16000;
    this.destination = {};
    Audio.last = this;
  }
  node() {
    return { connect() {}, disconnect() {} };
  }
  createMediaStreamSource() {
    return this.node();
  }
  createScriptProcessor() {
    return (this.processor = this.node());
  }
  createGain() {
    return { ...this.node(), gain: { value: 1 } };
  }
  async resume() {}
  async close() {
    this.closed = true;
  }
}
function fixture(overrides = {}) {
  const calls = [],
    transcripts = [],
    states = [];
  let stopped = 0;
  const stream = { getTracks: () => [{ stop: () => stopped++ }] };
  const api = {
    voice: async (op, data) => {
      calls.push({ op, data });
      return { text: "click Export", token: 4 };
    },
  };
  const capture = new Capture({
    api,
    AudioContextClass: Audio,
    getUserMedia: async () => stream,
    onTranscript: (...args) => transcripts.push(args),
    onState: (state) => states.push(state),
    ...overrides,
  });
  return {
    capture,
    calls,
    transcripts,
    states,
    stream,
    stopped: () => stopped,
  };
}

test("audio capture produces bounded PCM, stops the microphone before transcription, and yields a reviewable transcript", async (t) => {
  const f = fixture();
  t.after(() => f.capture.cancel(false));
  await f.capture.start();
  assert.equal(f.capture.phase, "recording");
  Audio.last.processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array(8000).fill(0.1) },
  });
  await f.capture.finish();
  assert.equal(f.stopped(), 1);
  assert.equal(Audio.last.closed, true);
  assert.deepEqual(
    f.calls.map((c) => c.op),
    ["begin", "end", "transcribe"],
  );
  const wav = Buffer.from(f.calls[2].data.audio, "base64");
  assert.equal(wav.toString("ascii", 0, 4), "RIFF");
  assert.equal(wav.readUInt32LE(24), 16000);
  assert.equal(wav.length, 16044);
  assert.deepEqual(f.transcripts, [["click Export", 4]]);
});

test("cancelling while microphone permission is pending closes the late stream", async () => {
  const permission = deferred();
  const f = fixture({ getUserMedia: () => permission.promise });
  const starting = f.capture.start();
  await tick();
  f.capture.cancel();
  permission.resolve(f.stream);
  await starting;
  assert.equal(f.stopped(), 1);
  assert.equal(f.capture.active, false);
  assert.deepEqual(f.transcripts, []);
});

test("a late local decoder response cannot populate or execute a command after Stop", async () => {
  const pending = deferred();
  const f = fixture();
  f.capture.api = {
    voice: async (op) => (op === "transcribe" ? pending.promise : {}),
  };
  await f.capture.start();
  Audio.last.processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array(8000).fill(0.1) },
  });
  const finishing = f.capture.finish();
  await tick();
  f.capture.cancel();
  pending.resolve({ text: "late click", token: 1 });
  await finishing;
  assert.deepEqual(f.transcripts, []);
  assert.equal(f.capture.active, false);
});

test("denied microphone permission leaves typed control available with no recording", async () => {
  const f = fixture({
    getUserMedia: async () => {
      const error = new Error("denied");
      error.name = "NotAllowedError";
      throw error;
    },
  });
  await f.capture.start();
  assert.equal(f.capture.active, false);
  assert.match(f.states.at(-1).message, /Typed commands still work/);
  assert.deepEqual(
    f.calls.map((c) => c.op),
    ["begin", "cancel"],
  );
});

test("unexpected microphone loss cancels the utterance instead of submitting partial speech", async () => {
  let stopped = 0;
  const track = { stop: () => stopped++, onended: null };
  const f = fixture({
    getUserMedia: async () => ({ getTracks: () => [track] }),
  });
  await f.capture.start();
  track.onended();
  await tick();
  assert.equal(stopped, 1);
  assert.equal(f.capture.active, false);
  assert.deepEqual(f.transcripts, []);
  assert.equal(f.calls.at(-1).op, "cancel");
});

test("the capture duration cap bounds the PCM sent to the worker", async () => {
  const f = fixture();
  await f.capture.start();
  Audio.last.processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array(500000).fill(0.1) },
  });
  for (let i = 0; i < 10 && f.capture.active; i++) await tick();
  assert.equal(f.capture.active, false);
  const packet = f.calls.find((call) => call.op === "transcribe");
  assert.equal(Buffer.from(packet.data.audio, "base64").length, 960044);
  assert.equal(f.stopped(), 1);
});
