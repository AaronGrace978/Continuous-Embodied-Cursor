# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end on a real X11 desktop: XTEST in, AT-SPI out, a GTK app in between.

These drive the actual pointer, so they are skipped unless a display, the
accessibility bus and GTK 3 are all there. Run them alone with
``pytest -m desktop``; leave the mouse be while they run.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.desktop

PLAYGROUND = Path(__file__).resolve().parent.parent / "examples" / "playground_gtk.py"


def _available() -> str:
    if sys.platform != "linux" or not os.environ.get("DISPLAY"):
        return "no X11 display"
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Atspi, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        return f"PyGObject with Atspi and Gtk 3 is not installed ({exc})"
    return ""


_missing = _available()
if _missing:
    pytestmark = [pytest.mark.desktop, pytest.mark.skip(reason=_missing)]


class App:
    def __init__(self, log: Path, process: subprocess.Popen) -> None:
        self.log, self.process = log, process

    def events(self, kind: str | None = None, name: str | None = None) -> list[dict]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [r for r in rows if (kind is None or r["kind"] == kind) and (name is None or r["name"] == name)]

    def wait_for(self, kind: str, name: str, timeout: float = 2.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.events(kind, name)
            if found:
                return found[-1]
            time.sleep(0.02)
        raise AssertionError(f"the app never reported {kind} {name!r}; it saw {self.events()[-5:]}")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    log = tmp_path_factory.mktemp("playground") / "events.jsonl"
    log.touch()
    env = dict(os.environ, NO_AT_BRIDGE="0", GTK_MODULES="gail:atk-bridge")
    process = subprocess.Popen([sys.executable, str(PLAYGROUND), "--log", str(log), "--at", "420,160"], env=env)
    yield App(log, process)
    process.terminate()
    process.wait(timeout=5)


@pytest.fixture
def session(app):
    from embodied_cursor.backends.linux import open_session
    from embodied_cursor.body import BodyConfig

    s = open_session(use_memory=False, config=BodyConfig(occlusion_patience=3.0))
    deadline = time.time() + 10
    while time.time() < deadline and s.world.find_one('button "Export"') is None:
        s.settle(1.0)
    if s.world.find_one('button "Export"') is None:
        s.close()
        pytest.skip("the playground window did not show up in the accessibility tree")
    app.log.write_text("")
    yield s
    s.close()


def do(session, **obj):
    from embodied_cursor.intents import intent_from_obj

    return session.body.execute(intent_from_obj(obj))


def test_click_reaches_the_application(session, app):
    out = do(session, action="click", target='button "Export"')
    assert out.ok, out.describe()
    app.wait_for("clicked", "Export")


def test_checkbox_state_is_confirmed_from_the_accessibility_tree(session, app):
    box = session.world.find_one('checkbox "Wrap lines"')
    want = "not_checked" if box.has("checked") else "checked"
    out = do(session, action="click", target='checkbox "Wrap lines"', expect={"on": 'checkbox "Wrap lines"', "state": want})
    assert out.ok, out.describe()
    assert app.wait_for("toggled", "Wrap lines")["active"] is (want == "checked")


@pytest.mark.parametrize("want", [73, 12])
def test_slider_is_dragged_to_an_exact_value(session, app, want):
    before = session.world.find_one('slider "Volume"').value
    out = do(session, action="scrub", target='slider "Volume"', value=want)
    assert out.ok, out.describe()
    time.sleep(0.1)
    values = [e["value"] for e in app.events("value", "Volume")]
    assert values[-1] == want
    # It closed in on the value without running past it.
    if want > before:
        assert max(values) <= want + 1
    else:
        assert min(values) >= want - 1


def test_nested_menu_entry_from_a_single_intent(session, app):
    out = do(session, action="click", target='menu_item "PDF document"')
    assert out.ok, out.describe()
    app.wait_for("activated", "PDF document")
    assert [e["name"] for e in app.events("activated")] == ["PDF document"]
    assert "revealing" in session.body.trace.kinds()


def test_typing_text_outside_the_keyboard_layout(session, app):
    text = "naïve café €5 — ok"
    out = do(session, action="type", target="text_field", text=text, clear=True, enter=True)
    assert out.ok, out.describe()
    assert app.wait_for("submitted", "Search")["text"] == text
    assert out.observed["text"] == text


def test_a_button_that_keeps_moving_is_caught(session, app):
    assert do(session, action="click", target='toggle "Start hopping"').ok
    try:
        hits = 0
        for _ in range(6):
            before = len(app.events("clicked", "Catch me"))
            out = do(session, action="click", target='button "Catch me"')
            assert out.ok, out.describe()
            time.sleep(0.15)
            landed = len(app.events("clicked", "Catch me")) > before
            hits += landed
            # A miss is possible when the button leaves in the instant of the
            # press, but then the body must have noticed.
            assert landed or out.observed.get("target_moved_away"), out.to_obj()
            do(session, action="move", target='label "Status"')
        assert hits >= 4
    finally:
        do(session, action="click", target='toggle "Start hopping"')


def test_waits_for_a_window_that_covers_the_target(session, app):
    assert do(session, action="click", target='button "Cover soon"').ok
    app.wait_for("covered", "Guarded", timeout=3.0)
    out = do(session, action="click", target='button "Guarded"')
    assert out.ok, out.describe()
    uncovered = app.wait_for("uncovered", "Guarded")
    clicked = app.wait_for("clicked", "Guarded")
    assert clicked["t"] > uncovered["t"]
    assert "waiting" in session.body.trace.kinds()


def test_yields_to_a_person_moving_the_mouse(session, app):
    from embodied_cursor.backends.x11 import X11Actuator

    do(session, action="move", target='label "Status"')
    hand = X11Actuator()

    def nudge():
        p = hand.position()
        hand.move_to(int(p.x) - 70, int(p.y) + 40)

    timer = threading.Timer(0.18, nudge)
    timer.start()
    before = len(app.events("clicked", "Export"))
    out = do(session, action="click", target='button "Export"')
    timer.join()
    hand.close()
    assert out.status == "interrupted" and out.reason == "human_override", out.describe()
    time.sleep(0.2)
    assert len(app.events("clicked", "Export")) == before


def test_perception_keeps_up_with_the_motor_loop(session, app):
    do(session, action="scrub", target='slider "Volume"', fraction=0.5, duration=1.5)
    stats = session.perception.stats
    assert stats.errors == 0
    assert stats.worst_frame_s < 0.05
    assert session.body.overruns <= session.body.ticks * 0.02
