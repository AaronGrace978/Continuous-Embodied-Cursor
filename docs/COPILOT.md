# Persistent copilot sessions

CEC keeps one desktop session and its perception sources connected until you
choose Disconnect or quit. Finishing or stopping a task leaves that connection
available for your next instruction. Taking the mouse interrupts the current
action and pauses queued work.

## Use the copilot

1. Connect Live desktop, or use the playground to try it without real input.
2. Give a named command such as `click Export` or `fill Notes with hello`.
3. Choose **Copilot ↗** to switch explicitly from the control room to a compact,
   always-on-top command dock. It shows the current action or waiting reason,
   a command box, Stop, and session context counts. Click its heading to return
   to the control room. Drag its header to move it away from your target.
4. Submit another instruction after the previous one finishes. The session stays
   connected. For open-ended follow-ups, the configured model receives the last
   eight tasks and their actual outcomes, capped at 12,000 characters. This
   history includes target identities; current desktop observations still decide
   whether those targets exist. Explicit named commands continue to run locally.
5. **New context** forgets that task history without disconnecting. Disconnect
   also clears it. History is in memory for this session, not saved to disk.

The app no longer minimizes on every live task by default. Upgrading from the
older settings resets that old default once; you can opt back in under Settings.
Keeping the full control room visible can cover a target: use the dock or move
it aside. The dock releases keyboard focus while executing. If a CEC window
still has foreground focus on Windows or a Linux window manager exposing
`_NET_ACTIVE_WINDOW`, keyboard actions stop with `controller_focused` instead of
typing into the command box. Naming the target field lets CEC click to focus it.

The dock accepts one task at a time. The control room's existing queue remains
available for sequences. Stop cancels the active task and pauses that queue;
it does not disconnect the session. Ctrl+Alt+Esc remains the global stop shortcut.

## Relationship to the other substrates

These projects serve different clocks and responsibilities:

| Component | Existing responsibility | CEC connection |
| --- | --- | --- |
| [ChronoSoul](https://github.com/AaronGrace978/continuous-emotional-substrate) | Background affect, memory, consolidation and restart persistence | Optional cached `/state` reader |
| [Continuous Motion Substrate](https://github.com/AaronGrace978/Continuous-motion-substrate) | Persistent 10 Hz spring state, anticipation and trajectory memory; already consumes ChronoSoul affect | Optional cached `/state` reader |
| CEC | Accessibility grounding, menu routing, value feedback and the 120 Hz desktop motor loop | Sole owner of physical input |

Start the substrate services independently using their own setup guides, then
enable **Read live state from Motion Substrate and ChronoSoul** in CEC Settings.
Default endpoints:

- ChronoSoul: `http://127.0.0.1:47321/state`
- Motion: `http://127.0.0.1:47400/state`

Set `CEC_CHRONOSOUL_URL` and `CEC_MOTION_URL` before launching CEC if those local
services use different ports. Only loopback `/state` endpoints are accepted.
Readers run on separate threads at approximately 1 Hz with short request
timeouts. Planning and telemetry read the cache, and the motor loop does no
substrate network I/O. Data older than three seconds is marked offline. A
missing service is shown as offline; no synthetic state is presented as live.

Only numeric affect, tick, memory count and motion vectors are retained from
these endpoints. ChronoSoul transcripts, memories, daydreams and provider keys
are discarded. Cached readings enter the planner context at each planning
round; this data can reach your configured planning provider just like the
current goal and accessibility text. The adapters are off by default.

This is a read-only integration. It does not copy either engine, launch their
models, mutate their databases, send CEC tasks to their chat endpoints, or let
their internal motion directly move the desktop pointer. The spring's target
decay, anxious jitter and dormancy catch-up do not apply to physical input.
Affect is companion state, not permission to act or proof a task succeeded.
Two-way task feedback and companion animation driven by these states are
separate future work.

## What this milestone establishes

The session and command surface persist between tasks, and follow-ups can share
bounded context. Optional local voice input is described below. This does not
add screenshot perception, new platform backends, or a general task-result verifier. It also does not
establish that a particular machine's native movement issue is fixed.

For a movement failure, export its trace and keep the exact goal, platform,
last waiting reason and perception status. Those distinguish an inaccessible
control or model delay from a motor problem. The telemetry now includes the
count of late motor ticks (`motorOverruns`). Existing physical mouse override,
input cleanup, movement limits and simulation mode remain in force.

## Local voice commands

Voice is optional and runs in a separate Python process. It uses a CPU/int8
Whisper `tiny.en` model for English commands. Install the extra into the same
Python environment selected in CEC Settings:

```text
python -m pip install -e ".[voice,windows]"
```

Open **Settings → Local voice → Prepare local voice**. The first preparation
explicitly downloads about 75 MB from Hugging Face; subsequent transcription
loads local files only. A prepared model can be used without internet access.
`CEC_VOICE_MODEL_DIR` can select an existing CEC model directory before launch.
For CLI preparation into that directory, use
`python -m embodied_cursor.voice --prepare` with the same environment variable.
The desktop default is its own user-data `voice-model` directory.

Connect a session and choose **Voice ◉**, speak, then choose **Finish voice**.
Ctrl+Alt+Space starts or finishes recording when the global shortcut is
available. Recording stops after 30 seconds. The transcript appears in the
command box for review and editing; press Run to execute it through the same
controller as typed instructions. To execute immediately after transcription,
explicitly enable **Run recognized commands automatically after recording ends**.
The transcript stays visible when this option is enabled.

Microphone access is scoped to the recording window and ends before decoding.
Audio is held in memory and passed only to the local speech process; it is not
saved or sent to Ollama, ChronoSoul, Motion Substrate, or an audio cloud service.
Submitting a transcript sends its text through the configured planner under the
same rules as typing a goal. The microphone is off on launch and between
recordings. Denied permission or unavailable voice dependencies leave typed
commands available. Unexpected device loss cancels the recording.

**Stop / Ctrl+Alt+Esc** cancels recording or decoding as well as the current
desktop task. Late transcripts and automatically submitted voice commands have
cancellation tokens, so a reply arriving after Stop cannot execute. Voice
recording currently starts only while the desktop is idle; always-listening
wake words and speech interruption during task execution are not implemented.
Hand control and voice recording are exclusive.

Simple follow-ups such as `click that`, `go to it`, and `fill that with hello`
can run locally against the last successful target if its exact ID still
appears in the current observation. A missing target, failed previous task,
or ambiguous reference goes back to the configured planner. Text being filled
keeps its punctuation. The simulator also tracks pressed keys by current state,
so repeated Ctrl+A/fill operations work across a session.

Tests cover bounded audio, capture and cleanup, transcript review, optional
execution after recording, late cancellation, and microphone permission leases.
Decoder output is stubbed in the automated voice flow tests. Recognition accuracy
and real microphone hardware still require trials with the prepared model.
