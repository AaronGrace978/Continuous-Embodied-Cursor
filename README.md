# Continuous Embodied Cursor

It moves your cursor so you don't have to.

The pointer is a small dynamical system. A planner names what should happen — *open File, then Export as PDF; set Volume to 30* — and a fast loop carries it out: minimum-jerk reaches, re-aimed every tick from the accessibility tree, slowed for small targets, bent around menus, and closed on the value a slider actually reports.

```
pip install -e ".[dev]"
cec demo --out traces
```

`cec demo` is a simulation and does not move the system pointer. On a Linux desktop with X11 and AT-SPI:

```
cec look
cec do 'open File > Export as > PDF document; set Volume to 30'
```

## Windows

Install with Python 3.10 or newer in PowerShell on the Windows desktop you want to control:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,windows]"
.\.venv\Scripts\cec look
.\.venv\Scripts\cec do 'click Calculator'
```

The CLI automatically selects the native Windows backend. `look` lists actual
controls exposed through Windows UI Automation. Choose a visible name from that
listing for `do` or `intent`. The Windows backend uses SetCursorPos/GetCursorPos,
SendInput for buttons, wheel and keyboard, UTF-16 Unicode text input, per-monitor
DPI awareness, and EnumDisplayMonitors for desktop geometry (including negative
monitor coordinates). UI Automation provides the control hierarchy, live bounds,
focus, enabled/visible/selected/checked/expanded states, editable text and range
values. The existing closed-loop motor and human-override detection are shared.

Run this inside an interactive Windows session, not WSL or a Windows service.
Windows prevents a normal process injecting input into elevated applications;
run both at the same integrity level. Secure desktops, including UAC prompts,
are not supported. Custom applications must expose controls through UI Automation;
there is no screenshot fallback. `--display` remains an X11-only option.

`cec daemon` and `cec send` use a per-user Windows named pipe with the Windows
default DACL (owner, administrators and SYSTEM). `--socket` may override the full
pipe name (`\\.\pipe\your-name`). Pipe messages are UTF-8 JSON byte messages;
Unix sockets continue to use newline-delimited JSON. Windows spatial memory is
stored under `%LOCALAPPDATA%\embodied-cursor\memory.json`.

To validate real Windows input and perception (these tests move your pointer):

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_windows_desktop.py
```

## Two speeds

The planner thinks in goals. The body moves in real time.

| | Planner | Body |
|---|---|---|
| When | At a decision: the start, a failure, a surprise dialog, the end of a plan | Every tick, 120 Hz |
| Input | A short text listing of the windows and controls | The world model, refreshed at 60 Hz around the pointer |
| Output | Intents: click, scrub, type, drag, key, scroll, wait | A continuous position, velocity and acceleration |

Between those decisions the planner is not in the loop. A button that jumps, a popup that covers the target, a slider whose response is curved: the body answers on the next tick.

![A reach re-planned in flight after the button jumps](docs/traces/moving-target.png)

The button above jumps 400 px and then slides. The reach is re-planned 41 times from the cursor's current position, velocity and acceleration. In the 200 ms after the jump the speed stays above 1977 px/s; the path bends, it does not restart. Peak speed on this run is 3054 px/s, and the largest velocity change between two 120 Hz ticks is 286 px/s.

![A nested menu, routed around the items that would close it](docs/traces/nested-menu.png)

One intent, `open File > Export as > PDF document`. The path through the closed menus is read off the element tree. The route passes through a via-point so the pointer never crosses Print or Close, and the submenu stays open.

## Commands

```
cec demo [scenario] [--out DIR]          moving-target, popup, nested-menu, scrub
cec look [--json]                        the screen as the planner sees it
cec find 'slider "Volume"'               elements matching a selector
cec do 'click Export; set Volume to 70'  plain commands, planned offline
cec run 'export this as a PDF'           open-ended goal, planned by a language model
cec intent '{"action":"click","target":"button \"Export\""}'
cec daemon                               keep perception warm; serve a Unix socket
cec send '{"op":"status"}'
cec render trace.json -o trace.svg
```

`cec do` understands clauses separated by semicolons: click, double-click, right-click, hover, move to, press a key chord or a button, set/slide a control to a number or a percent, drag onto, scroll, wait for, check, uncheck, type, and `fill Search with "embodied cursor" and press enter`.

`cec run` talks to an OpenAI-compatible server. Point it wherever the model is:

```
export CEC_LLM_BASE_URL=http://127.0.0.1:8080/v1
export CEC_LLM_API_KEY=...          # optional; OPENAI_API_KEY is also read
export CEC_LLM_MODEL=...
```

The model is shown the same text listing as `cec look` and answers with intents as JSON. It is asked for element names, and the controller finds them.

`cec daemon` keeps the accessibility tree warm between requests. The socket is mode 0600, under `$XDG_RUNTIME_DIR` or `/tmp/embodied-cursor-<uid>/`. One request moves the pointer at a time.

While a skill is running, a person moving the mouse aborts it. The outcome is `human_override`, and the click is not delivered.

## Layout

```
src/embodied_cursor/
  motor/        minimum-jerk plans, Fitts timing, hover-safe routing
  world/        elements, selectors, the world model, foveated perception, spatial memory
  skills.py     reach, click, drag, scrub, type, scroll: generators stepped every tick
  body.py       the 120 Hz loop
  brain/        text observation, the planner protocol, heuristic and LLM planners, the agent
  backends/     simulator, Linux X11/XTEST + AT-SPI, Windows Win32 + UI Automation
  cli.py        cec
  daemon.py     the local socket
  render.py     an SVG of a recorded trace
examples/playground_gtk.py
```

The design of each piece is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```
python3 -m pytest -q -m "not desktop"          # simulator; no display
python3 -m pytest -q tests/test_live_desktop.py  # a real X11 session, GTK 3, AT-SPI
```

The live tests drive the actual pointer. Leave the mouse alone while they run. They launch `examples/playground_gtk.py` themselves.

## Limits

These are the edges of what this tree does today.

- The Linux live backend requires an X11 display. A Wayland session works only through XWayland, and the pointer moved is the X pointer. Windows uses its native Win32/UI Automation backend. macOS is not implemented.
- The Linux actuator is XTEST: motion, buttons, keys and the wheel. That protocol has no pressure axis, and this program does not open `/dev/uinput`.
- The controller is arithmetic in this process. It does not need a GPU. A language model, if you configure one, is a separate HTTP server. The tests of that path use a local stub; no model is shipped or called from here.
- Perception reads the window stack and the AT-SPI tree. An application that does not expose itself there is a titled rectangle. Chromium puts its page into the accessibility tree when started with `--force-renderer-accessibility`. There is no Chrome DevTools client.
- A target that leaves in the instant the button goes down is reported as `target_moved_away`. The planner can retry. The press itself cannot be taken back.
- Scrubbing a control servos on the value the control reports. In the simulator, sixteen controls (linear, reversed, quadratic, cubic, square-root, and integer steps) at perception delays of 0, 30, 60 and 120 ms all finished on the requested value, and none ran more than one unit past it. On the GTK playground here, the volume slider landed on 73 and on 12 with the same bound. The drag stops short and then homes on readings taken at rest, so it takes a few seconds. If the final reading is still wrong, the outcome is `value_mismatch` rather than a false success.

## License

GNU Affero General Public License v3.0 or later. It is the GPL with one further term: anyone who runs a modified copy as a network service must offer its source to the people using it. See [LICENSE](LICENSE).
