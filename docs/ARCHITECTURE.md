# Architecture

The cursor is a continuous-time physical object with a slow mind and a fast body. A planner decides what should happen. A 120 Hz loop decides how the pointer moves, from the live state of the desktop, and it keeps deciding until the action is done.

Four properties follow from that split.

## Continuous trajectory dynamics

`motor/minjerk.py` plans each reach as the quintic that minimises integrated squared jerk from the cursor's current position, velocity and acceleration (Flash & Hogan, 1985). Re-planning from that state leaves position, velocity and acceleration continuous, so a target that moves mid-flight bends the path. The remainder of an optimal plan is itself optimal, so sampling it at the motor rate is an exact discretisation of the continuous feedback law (Hoff & Arbib, 1993).

`motor/controller.py` rebuilds the plan whenever the goal moves by more than a small jump. Two guards keep a re-plan from doing something a hand would not:

- If the cursor is already closing on the goal, the horizon is capped at the time a smooth stop would take (`2 · distance / closing speed`). A button that lands in front of a fast cursor is braked onto, rather than flown past.
- A plan longer than `acc_memory` (0.3 s) scales the carried acceleration down by `(acc_memory / duration)²`. A long, slow drag does not inherit a deceleration left over from the move that preceded it and swing backwards.

Duration comes from Fitts's law in Shannon form (`motor/fitts.py`): `MT = a + b · log2(D / W + 1)`, and from a kinematic floor so the speed and acceleration caps (`v_max` 9000 px/s, `a_max` 150000 px/s²) are respected. Small targets take longer. `tempo` on the command line stretches every reach.

Curved reaches use the via-point form of the same polynomial. `motor/routing.py` builds a visibility graph around obstacles inflated by a margin and searches it. The obstacles are the things a pointer must not merely touch: the siblings of a menu item, which would close the menu. An obstacle that already contains the start or the goal is dropped, because it cannot be avoided.

## Real-time feedback

`world/perception.py` schedules reading the way a fovea does. The attended set — the current target, the obstacles on the route, the control being dragged — is re-read every frame, 60 Hz by default. Everything else is a full snapshot at about 1 Hz, sooner when a source says the tree changed. A full accessibility tree is too slow to steer by; the attended set is not.

Each skill in `skills.py` is a generator the body steps once per tick. On every step it locates its target again in the world model. The motion goal is updated, and the controller re-plans if the goal moved. There is no round trip to the planner for that.

Consequences that show up in the traces:

- A button that jumps or slides is followed. The reach in `docs/traces/moving-target.svg` is re-planned dozens of times and the speed never drops to zero to "start over".
- A popup that covers the target makes the cursor hold short of it (`waiting` in the trace) until the cover goes away. The click is delivered to the button, not to the popup (`docs/traces/popup.svg`).
- A slider is steered by the value it reports. `Response` records pointer-offset against value as the drag proceeds. A sweep asks the value to follow a minimum-jerk S-curve and sends the pointer where the table says that value lives, so a quadratic or cubic response still moves smoothly (`docs/traces/scrub.svg`).

Readings taken while the pointer is moving lag the pointer by however long the application takes to answer, and that lag is not known in advance. The sweep therefore refuses two kinds of runaway:

- It never aims more than half of the remaining value gap past the latest reading (`_SWEEP_CAUTION`). A response that steepens while a reading is in flight cannot be extrapolated off the end of the control.
- The aim point itself may move at most twice the speed the sweep was timed for (`_SWEEP_PACE`). Past that, a late reading makes the control look as though it needs ever more travel, and the aim chases its own delay.

The sweep also stops short, by a margin on the order of the prepared-for latency times the peak speed. Homing then closes the gap from readings taken with the pointer at rest, which are exact: bracket the wanted value between settled readings, or step on from the nearest one. A flat stretch (a stepped control, a dead zone) doubles the step until the value changes. A reading that would end the drag is held for `scrub_confirm` (0.16 s) first, because on a stepped control the report still in flight can be the next step up. If the value after release is still wrong, the outcome is `value_mismatch`.

The lag of a continuous control is measured from the first lean: the time from the pointer's first movement to the first change in the value, trusted only if the value keeps changing afterwards. A stepped control answers once, when a step is crossed, and that guess is thrown out.

Scrolling uses the same idea on the wheel. A row that is clipped still has a place in the tree, and that place says which way to turn. The first notch measures how many pixels one turn moves the row, including a control whose wheel runs backwards. Later turns come in short bursts that shrink as the row approaches, and they stop once the row sits inside the view, so the search does not depend on being told "down". When the row is not in the tree at all, the wheel falls back to turning the stated way until it appears or the limit is reached.

## Embodied spatial presence

The controller's state is `(position, velocity, acceleration)`, plus which buttons are held. Skills can ask for a moving goal with a feed-forward velocity, which is how a sweep tracks a reference instead of trailing it.

`world/memory.py` remembers, across runs, where elements live, which control revealed a hidden one, and where across a slider's thickness the handle actually responded (`note_grip`). The next drag tries that line first. A target that is inside a closed menu is reached by `structural_reveal`: the opener chain is read off the element's ancestors, and memory is only the fallback.

Hover is part of the motion, not a separate gesture. The router treats menu items as hazards. A hover intent dwells. A scrub can be given a duration, and the sweep's reference takes that long; the pointer speed follows from the control's response.

## Hierarchical control

`brain/agent.py` calls the planner at decision points only: the start, an intent that failed, a dialog or window that appeared and was not the expected result, and the end of the plan. Between consequential actions it requests a fresh snapshot and rests for up to `settle` (0.12 s). A full observation with a visible effect can end that pause early; focus/press echoes alone cannot. A hidden dialog that becomes visible counts as an appearance. A human override ends the episode; it is not handed back to the planner to fight the person. Three identical consecutive failed actions stop the episode.

`brain/planner.py` is the protocol: an `Episode` (goal, text observation, history, surprise) in, a `Plan` (intents, done, note, give up) out.

- `brain/heuristic.py` parses plainly worded clauses. It is stateful: a step whose effect already holds is skipped, a retriable failure is tried again, and `target_moved_away` is reported as a slip. A menu path is one click with a `within` chain; if the tree has no such item, it falls back to clicking each level.
- `brain/automatic.py` chooses the offline parser for supported commands and lazily constructs the configured model planner for other goals. The desktop bridge enables this by default.
- `brain/llm.py` renders the episode as text, posts it to `/chat/completions`, and validates the JSON. A reply that does not parse is sent back for repair, a small number of times. `response_format` is dropped if the server rejects it.

`brain/observation.py` builds the listing: windows front to back, `role "name"` with states and values, and a line for items that live in closed menus (`File > Export as > PDF document`). An empty window that overlaps a window the accessibility tree has already filled in is dropped, so the window system and AT-SPI are not listed twice. The listing is capped (120 elements by default). No pixels, no coordinates for the model to invent.

Intents are the narrow interface, in `intents.py`: `move`, `click`, `hover`, `drag`, `scrub`, `scroll`, `type`, `key`, `wait`. Each can carry an `expect` (an element appears, a state holds, a value lands). The body confirms it against a fresh observation.

## Direct OS hooks

`backends/x11.py` reads the window stack and moves the pointer with XTEST, through `ctypes`. `backends/atspi.py` reads the accessibility tree on the perception thread: roles, names, states, text (through `Atspi.Text`, because the object-level method is a deprecated accessor that drops the field once it has contents), and the value interface for sliders. `backends/linux.py` assembles them into a `Session`. A missing accessibility bus leaves the session with windows only, and says so in `notes`.

`backends/sim.py` is the same `Actuator` and `PerceptionSource` protocols with a scripted desktop, including a perception latency, so the closed-loop behaviour can be tested without a display.

`daemon.py` serves the same operations on a Unix socket the owning user can open: `intent`, `goal`, `observe`, `find`, `cancel`, `events`, `status`, `shutdown`.

## What runs where

```
goal (text)
    │
    ▼
Agent ── calls ──▶ Planner (heuristic or LLM)     only at decision points
    │                    │
    │                    ▼
    │                 intents
    ▼
Body, 120 Hz
    ├─ perception, 60 Hz on the attended set
    ├─ skill generator (re-locate the target)
    └─ motion controller (minimum-jerk re-plan) ──▶ XTEST
```

## File by file

| Path | Role |
|---|---|
| `motor/minjerk.py` | Quintic minimum-jerk plans, including via-points |
| `motor/fitts.py` | Movement time from distance and width |
| `motor/routing.py` | Visibility-graph routes around hover hazards |
| `motor/controller.py` | Kinematic state, re-planning, speed and acceleration caps |
| `world/element.py` | Roles, states, bounds, value |
| `world/model.py` | Tracked elements, hit-testing, occlusion, motion |
| `world/selector.py` | `button "Export"`, role kinship (`button` matches `toggle`) |
| `world/perception.py` | Attended set vs snapshots |
| `world/memory.py` | Where things were, how they were revealed, where a handle gripped |
| `skills.py` | The motor vocabulary, including the scrub servo |
| `body.py` | The fast loop, human-override detection |
| `brain/observation.py` | The text the planner sees |
| `brain/planner.py` | `Episode` and `Plan` |
| `brain/heuristic.py` | The offline command language |
| `brain/llm.py` | The OpenAI-compatible planner |
| `brain/agent.py` | When to think, when to move |
| `backends/sim.py` | Scripted desktop |
| `backends/x11.py`, `atspi.py`, `linux.py` | The live desktop |
| `cli.py`, `daemon.py`, `render.py`, `demo.py` | Driving it and looking at what it did |

## Desktop presence and camera input

`desktop.py` owns one engine thread. An Electron task queue feeds it goals
serially, pausing when a report fails or is interrupted. The desktop agent has
a budget of 24 planning rounds and 160 intents per goal. The main process caps
each task at ten minutes. `thinking` events mark actual planner calls instead
of treating every completed intent as another planning round.

`ui/renderer/camera.js` lazily starts video-only capture and local MediaPipe
Tasks inference. Its runtime is bundled from the pinned npm package; the model
is fetched from Google's model host only after explicit enable. It forwards
one normalized sample at a time through the isolated preload. The Python
bridge retains only the newest sample and expires it after 250 ms. Camera
control has exclusive engine ownership and remains cancellable through stdin.

`hand.py` applies time-based smoothing, open-hand arming, palm-relative pinch
hysteresis, debounce, and a one-click latch. It freezes the pointer during a
pinch and disarms on tracking loss. The engine stops if the physical mouse
deviates from its commanded position. It maps to the monitor containing the
pointer at startup; hand control supports left clicks, not dragging. No camera
frames go to Python or a language model. Camera startup uses generation guards
so a late permission/model result cannot reactivate a stopped session.

`HandSample.gesture` adds pointer, scroll, and keyboard poses. `HandAction`
returns pointer/click output plus wheel deltas and a keyboard summon edge.
The scroll clutch freezes the pointer, debounces entry, applies a dead zone,
and integrates a bounded rate using fresh camera timestamps. Returning to the
pointer requires open-hand arming. Three-finger holds latch once until an open
hand rearms them.

`ui/renderer/air-keyboard.js` owns shared US QWERTY geometry, camera-space
calibration, and per-hand/per-finger tap state. It locks the hovered key after
100 ms of extension, confirms a 45 ms bend with angular hysteresis, and requires
release before another press. Tracking loss, long gaps, identity jumps, and key
gutters disarm taps. The detector tracks up to two hands locally. Typing mode
sends absent pointer samples to Python, so it cannot steer or pinch-click.

`ui/lib/keyboard.cjs` resolves key IDs into text or a small native key allowlist.
Software Shift/Caps state is shared by air and mouse activation. The floating
keyboard is an isolated, sandboxed, nonfocusable BrowserWindow shown with
`showInactive()`. It renders the same geometry and normalized finger feedback.
The main process serializes requests, bounds admission, and invalidates queued
requests when visibility/ownership changes. Python consumes camera-time input
on the existing engine thread via a bounded queue; stop epochs reject late
keystrokes, and native keys are always released. Frames never leave the renderer.
The timer-based inference scheduler continues when the main window is minimized;
startup generation guards also reject late samples after Stop.

Operational details and current recognition limits are in
[HAND_CONTROL.md](HAND_CONTROL.md).

## Platform limitations

The live pointer exists on Linux with X11 and on Windows through Win32/UI
Automation. Wayland needs XWayland. macOS is not implemented.

XTEST has motion, buttons, keys and the wheel. It has no pressure channel, and `/dev/uinput` is not used.

The controller does not use a GPU. A language model is an external server, configured with `CEC_LLM_BASE_URL`, `CEC_LLM_API_KEY` and `CEC_LLM_MODEL` (the `OPENAI_*` names are also read). Tests cover that client against a stub HTTP server.

There is no screenshot pipeline and no Chrome DevTools client. Pages show up when the browser exports them through AT-SPI.

A press that lands as the target leaves is detected and reported. It is not prevented: the button has already gone down.
