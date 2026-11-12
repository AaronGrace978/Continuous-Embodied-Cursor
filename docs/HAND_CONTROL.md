# Hand scrolling and air typing

The desktop app has two camera modes: hand mouse and air typing. Both run
locally. Connect **Live desktop** to control an application, or use
**Simulation** to rehearse without system input.

## Hand mouse

Show an open hand for 250 ms to arm the pointer. Point with the index finger
and pinch thumb to index for 60 ms to click once. Release before clicking again.

To scroll, extend **index and middle**, with ring and little finger curled and
thumb apart. Hold the pose for 180 ms. The pose's starting point is the neutral
position: move up/down for vertical scrolling, left/right for horizontal
scrolling, and return to neutral to stop. The cursor stays over the original
scroll target. The 3.5% camera-space dead zone filters small movements. Each axis
ramps to a maximum of ten wheel notches per second. Open the hand for 250 ms to
return to pointing; a pinch immediately after scrolling cannot accidentally
click.

To summon the keyboard, extend **index, middle, and ring**, curl the little
finger, and keep the thumb apart. Hold for 800 ms. The cursor freezes during
the gesture. A button and **Ctrl+Alt+K** also show/hide the keyboard. If the
operating system reserves that shortcut, use the button or gesture.

## Typing with fingers

1. Use the hand mouse to click the text field you want to type into.
2. Summon the keyboard. The floating keyboard does not take focus from your
   application. While it is open, camera input switches to typing and stops
   driving the pointer.
3. Keep one or both hands visible, with fingertips facing the camera. The
   floating keyboard shows fingertip dots and hovered keys; the camera preview
   overlays the same key geometry on your hands.
4. Extend a finger over a key for at least 100 ms, then bend that finger to tap.
   Index, middle, ring, and little fingers on either hand can type independently.
   The chosen key is the settled location **before** the bend, so bending across
   a row boundary does not select a neighboring key. Straighten again before
   the next tap. Holding a finger bent never repeats a key.
5. Tap **Shift** for one uppercase letter or symbol, **Caps** for persistent
   uppercase, and **Space**, navigation, or editing keys as needed. Thumb taps
   are not inferred; use any supported finger over Space.
6. Tap the grid's **Hide** key to return to hand mouse mode, then show an open
   hand to rearm the pointer. Stop ends both modes and releases the webcam.

This is calibrated camera-space typing, with a visible keyboard surface. It
cannot infer arbitrary text from hidden hands or guarantee conventional
physical-keyboard touch typing. It does not guess or autocorrect words.
Lighting, camera angle, frame rate, overlapping fingers, and occlusion affect
recognition. Keep your hands separated and use deliberate taps while learning.
Real webcam accuracy and comfortable sustained typing speed require trials on
your hardware. Automated tests use synthetic landmarks, not a measured typing
accuracy benchmark.

## Calibration

The default typing surface occupies most of the lower camera image. To adjust
it, select **Calibrate** on the floating keyboard or **Calibrate typing area**
in the main window while the camera is active:

1. Point at the desired **upper-left** corner of the mirrored preview and hold
   a thumb/index pinch for 350 ms.
2. Release the pinch, point at the **lower-right** corner, and hold another
   pinch for 350 ms.
3. The grid moves to the selected rectangle. Extend your fingers to arm typing.

Calibration sends no keystrokes. Areas smaller than 35% of image width or 25%
of image height, and reversed corners, are rejected. Calibration stays in
memory for the current app session; it is not written to settings. Recalibrate
if the camera or your seating position moves.

## Input ownership and recovery

The Python engine remains the sole owner of native input. During a camera
session, keyboard actions enter a bounded queue consumed by that engine. Task
execution and hand control cannot compete for the pointer. Native keys are
released in `finally` blocks; Shift and Caps are software states rather than
held OS modifiers.

Finger tracking is independent for each hand/finger. Missing fingers, hand
identity jumps, or frame gaps over 250 ms discard pending taps. A tap requires
a stable extended finger, a bend below 130 degrees for at least 45 ms, and a
release above 155 degrees before another tap. Gaps between keys are inactive.
A held three-finger summon cannot repeatedly toggle the keyboard.

**Ctrl+Alt+Esc**, Stop, disconnect, and physical mouse override end camera input.
Pending keyboard requests from an earlier stop are invalidated and cannot be
replayed into a new session. The keyboard hides during automated goals, stopping,
and disconnect, and resets software modifiers when opened/closed.

No video, images, or full landmark arrays cross into Python or a model provider.
Only normalized pointer/pinch/gesture samples and individual keyboard actions
reach the Python engine. The floating renderer receives normalized fingertip
feedback for visualization. Keystrokes are not added to task history. Visible
text in the target app remains subject to the existing accessibility observation
and planner behavior when you later run an automated goal.

## Verification

```bash
python -m pytest -q -m "not desktop"
cd ui
npm test
npm run test:e2e
npm run test:electron
# Native key and focus checks; use an isolated desktop:
CEC_TEST_LIVE_INDICATOR=1 npm run test:electron
```

The tests cover scrolling limits, neutral zones, frame stalls, gesture latches,
key geometry, two-hand taps, repeat suppression, calibration, modifiers, queue
cancellation, and actual typing through the Python bridge. The opt-in Electron
check creates a target app, injects native text/editing keys, and checks that
showing and using the keyboard leaves the target focused. Windows native tests
require Windows; an X11 result does not establish Win32 behavior.
