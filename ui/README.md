# Continuous Cursor · Electron

A motion-control console for the existing Python motor and perception backends.
Graphite surfaces, orange action cues, a cursor field and a command deck.
Windows uses Win32/UI Automation; Linux uses X11/AT-SPI. No web server is exposed:
the isolated renderer talks to Electron, which owns a Python process over stdio.

## Run from source

Install Node.js **22.12 or newer** (Node 24 recommended) and Python **3.10 or newer**.
From the repository root, set up Python using the main README. On Windows:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,windows]"
cd ui
npm ci
npm start
```

On Linux, prepare `.venv` with `python3 -m venv .venv` and
`.venv/bin/python -m pip install -e '.[dev]'`, then run the same npm commands in
`ui`. Live Linux control requires an X11 desktop and its native dependencies.
The app does not connect to a desktop or move the pointer on launch.

The app finds the repository's virtual environment first, then system Python.
To use another interpreter, set **Settings → Runtime → Python executable**, or
set `CEC_PYTHON` before launching. Supply an executable path, not a shell command.

## Ollama Cloud

1. Open **Settings** and select **Ollama Cloud**.
2. Create a key at <https://ollama.com/settings/keys> and enter it in the app.
   Alternatively, supply `OLLAMA_API_KEY` in the app's environment.
3. Load the model list or enter a model name directly. The default is
   `gpt-oss:120b`; use an exact name available to your account. Cloud API names
   can differ from local `-cloud` model aliases.
4. Choose **Test connection**. This sends a small prompt using your selected
   model and verifies authenticated generation. Loading the public model catalog
   alone does not verify a key or model access.
5. Save settings, connect **Live desktop**, enter a goal, and choose **Run task**.

The native Ollama endpoints are `https://ollama.com/api/tags` and
`https://ollama.com/api/chat`. A local Ollama installation is unnecessary for
Cloud. The planner receives your goal, visible accessibility control text, and
prior action outcomes. It receives no screenshots. Requests can consume your
Ollama Cloud allowance.

Keys are encrypted with Electron's OS-backed `safeStorage` when secure storage
is available. If Linux offers only the `basic_text` fallback, the key stays in
memory for that session and is not persisted. Environment keys are not written
to settings. Saved keys are never returned through the renderer API; the settings
form receives only whether a key is configured. The API key is sent only to the
Cloud endpoint, and redirects are rejected.

**Local Ollama** supports a loopback endpoint such as `http://127.0.0.1:11434`.
Cloud credentials are not sent to the local endpoint. **Offline commands** uses
the existing heuristic planner and makes no model requests.

## Activity and stop controls

- **Connected · idle:** perception is connected, with no active task.
- **Planning:** a planner is choosing the next actions.
- **Cursor active:** the body is executing an intent. A wait or unresolved target
  can keep this state visible even when the pointer is momentarily stationary.
- **Stopping:** cancellation is in progress.

While a live desktop is connected, an always-on-top strip shows the current
state and a **Stop** button, even when the main app is minimized. The app also
updates its tray icon, window title, and Windows taskbar overlay. The strip can
be disabled in Settings; it is hidden for simulation sessions.

Stop from the strip, the main window, the tray menu, or **Ctrl+Alt+Esc**. The
shortcut's availability is reported in the main window if registration fails.
Moving the physical mouse interrupts an executing action through the existing
human-override detector. During model planning, use Stop or the shortcut.

Cancellation is checked by the motor loop. A blocked model request that does
not unwind within 1.5 seconds causes the app's owned backend process to stop;
the late model reply cannot execute. Reconnect afterward. Closing the app ends
its own backend session; an unrelated `cec daemon` is not stopped or reused.

By default the app minimizes before a live task, then refreshes perception so
the control panel does not cover the target application. This is configurable.

## Playground and traces

**Try the playground** connects a deterministic simulated desktop and selects
offline commands. Try `click Export`, `set Volume to 70`, or
`fill Notes with hello world`. No system input or model connection is needed.

The desktop map is a schematic of actual accessibility bounds, not a screenshot.
Session history and the activity feed stay in memory; no chat transcript is saved.
**Export trace** opens a save dialog for a JSON motor trace, scene and last report.

## Tests

```bash
npm test
npm run test:e2e
npm run test:electron
```

The Node tests exercise settings persistence, key handling and the real Python
stdio protocol. Browser tests drive the production renderer with the real Python
simulation and an HTTP Ollama stub. The Electron smoke test launches the actual
sandboxed app, checks preload/IPC, executes a simulated task and cancels another.
The default tests use no paid Cloud account and do not move the system pointer.
An additional `CEC_TEST_LIVE_INDICATOR=1` smoke check connects the live desktop,
verifies the always-on-top strip and cancels an unresolved target from that strip.
Run that opt-in check in an isolated desktop session.

Browser tests use `CHROMIUM_PATH` if supplied, the system Chromium on Linux when
present, or Playwright's browser (`npx playwright install chromium`). Native
Electron tests need a graphical session. On a headless Linux CI runner:

```bash
xvfb-run -a npm run test:electron
```

Restricted containers may require `CEC_ELECTRON_TEST_ARGS='--no-sandbox --disable-gpu'`
for the test runner. These switches are not part of the application's startup
configuration. `CEC_USER_DATA_DIR` can isolate test or portable app settings.
Live Win32/UIA and X11 integration tests remain in the Python test suite.

## Packaging

```bash
npm run package
npm run dist
# On Linux, validate the actual packaged AppImage:
npm run test:appimage
```

`package` creates an unpacked app; `dist` creates a Windows NSIS installer or
Linux AppImage on the corresponding build host. Electron Builder includes the
Python source in app resources. **Python itself is not bundled**: the installed
app still needs Python 3.10+ and, on Windows, `comtypes`. Use the interpreter from
the Python setup above or install `comtypes` into your chosen interpreter.

Build Windows installers on Windows and Linux AppImages on Linux. The CI desktop
workflow builds an installer/AppImage as an Actions artifact, without publishing
a GitHub release. A code-signing identity is not configured.
