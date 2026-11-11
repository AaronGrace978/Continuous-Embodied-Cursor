# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed-loop behaviour of the body against the simulated desktop."""

import pytest

from embodied_cursor.backends.sim import (
    Button,
    Dialog,
    Draggable,
    DropZone,
    Menu,
    MenuBar,
    ScrollList,
    SimDesktop,
    Slider,
    TextField,
    Window,
    make_body,
)
from embodied_cursor.body import BodyConfig
from embodied_cursor.geometry import Rect, Vec2
from embodied_cursor.intents import intent_from_obj
from embodied_cursor.world.memory import SpatialMemory


def do(body, **obj):
    return body.execute(intent_from_obj(obj))


@pytest.fixture
def desk():
    d = SimDesktop(pointer=Vec2(200, 800))
    d.win = d.add(Window("Editor", Rect(100, 100, 1400, 800)))
    return d


def max_tick_jump(body):
    """Largest change in velocity between consecutive motor ticks, px/s."""
    s = body.trace.samples
    return max(((b.vel - a.vel).norm() for a, b in zip(s, s[1:])), default=0.0)


def test_click_lands_inside_the_button(desk):
    ok = desk.win.add(Button("Export", Rect(1200, 300, 90, 32)))
    body = make_body(desk)
    out = do(body, action="click", target='button "Export"')
    assert out.ok, out.describe()
    assert ok.clicks == 1
    at = desk.events("click", "Export")[0][3]["at"]
    assert ok.rect.contains(Vec2(*at), margin=4)
    assert 0.2 < out.duration < 1.2


def test_target_that_jumps_mid_flight_is_followed_without_stopping(desk):
    btn = desk.win.add(Button("Save", Rect(1200, 300, 90, 32)))
    desk.at(0.20, lambda: btn.move_to(1000, 620))
    body = make_body(desk)
    out = do(body, action="click", target='button "Save"')
    assert out.ok, out.describe()
    assert btn.clicks == 1
    # One continuous movement: after take-off the cursor never stalls before arriving.
    samples = body.trace.samples
    press_t = next(e["t"] for e in body.trace.events if e["kind"] == "press")
    flight = [s for s in samples if 0.1 < s.t < press_t - 0.15]
    assert min(s.vel.norm() for s in flight) > 100
    assert max_tick_jump(body) < 400


def test_a_target_that_escapes_during_the_press_is_reported(desk):
    def run(escape_at=None):
        d = SimDesktop(pointer=Vec2(200, 800))
        win = d.add(Window("Editor", Rect(100, 100, 1400, 800)))
        btn = win.add(Button("Save", Rect(1200, 300, 90, 32)))
        if escape_at is not None:
            d.at(escape_at, lambda: btn.move_to(600, 600))
        body = make_body(d)
        out = do(body, action="click", target='button "Save"')
        press = next(e["t"] for e in body.trace.events if e["kind"] == "press")
        return out, btn, press

    out, btn, press = run()
    assert btn.clicks == 1 and "target_moved_away" not in out.observed

    # Too late for perception to have seen it before the button went down.
    out, btn, _ = run(escape_at=press - 0.004)
    assert btn.clicks == 0
    assert out.observed.get("target_moved_away") is True


def test_target_that_slides_continuously_is_tracked(desk):
    btn = desk.win.add(Button("Drifting", Rect(900, 300, 80, 30)))
    desk.animate(btn, Vec2(400, 250), 3.0, linear=True)
    body = make_body(desk)
    out = do(body, action="click", target='button "Drifting"')
    assert out.ok, out.describe()
    assert btn.clicks == 1
    assert out.duration < 1.5


def test_foveation_keeps_a_moving_target_fresh_between_full_snapshots(desk):
    btn = desk.win.add(Button("Drifting", Rect(900, 300, 80, 30)))
    desk.animate(btn, Vec2(300, 200), 2.0, linear=True)
    body = make_body(desk, snapshot_hz=0.5)
    out = do(body, action="click", target='button "Drifting"')
    assert out.ok, out.describe()
    assert btn.clicks == 1
    assert body.perception.stats.snapshots <= 2
    assert body.perception.stats.frames > 20


def test_works_with_perception_latency():
    d = SimDesktop(pointer=Vec2(200, 800), latency=0.08)
    win = d.add(Window("Editor", Rect(100, 100, 1400, 800)))
    btn = win.add(Button("Save", Rect(1200, 300, 90, 32)))
    d.at(0.25, lambda: btn.move_to(900, 500))
    body = make_body(d)
    out = do(body, action="click", target='button "Save"')
    assert out.ok, out.describe()
    assert btn.clicks == 1


def test_dialog_covering_the_target_is_reported_not_clicked_through(desk):
    btn = desk.win.add(Button("Delete", Rect(1200, 300, 90, 32)))
    dlg = desk.add(Dialog("Unsaved changes", Rect(1000, 200, 500, 300)))
    desk.at(0.15, dlg.show)
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="click", target='button "Delete"')
    assert not out.ok
    assert out.reason == "occluded"
    assert out.observed["occluder"]["name"] == "Unsaved changes"
    assert btn.clicks == 0
    assert not desk.events("press")
    assert body.controller.speed == 0.0


def test_partly_covered_target_is_clicked_on_its_visible_part(desk):
    btn = desk.win.add(Button("Wide", Rect(800, 300, 400, 40)))
    dlg = desk.add(Dialog("Notice", Rect(950, 200, 600, 300)))
    desk.at(0.1, dlg.show)
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="click", target='button "Wide"')
    assert out.ok, out.describe()
    assert btn.clicks == 1
    at = Vec2(*desk.events("click", "Wide")[0][3]["at"])
    assert at.x < 950


def test_cover_that_goes_away_in_time_is_waited_out(desk):
    btn = desk.win.add(Button("OK", Rect(1200, 300, 90, 32)))
    dlg = desk.add(Dialog("Toast", Rect(1000, 200, 500, 300)))
    desk.at(0.1, dlg.show)
    desk.at(0.7, dlg.hide)
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="click", target='button "OK"')
    assert out.ok, out.describe()
    assert btn.clicks == 1
    assert "waiting" in body.trace.kinds()


def test_missing_target_fails_with_not_found(desk):
    body = make_body(desk, config=BodyConfig(resolve_timeout=0.5))
    out = do(body, action="click", target='button "Nope"')
    assert out.status == "failed" and out.reason == "not_found"
    assert out.duration == pytest.approx(0.5, abs=0.1)


def test_target_that_appears_late_is_picked_up(desk):
    btn = Button("Late", Rect(700, 400, 90, 32))
    desk.at(0.6, lambda: desk.win.add(btn))
    body = make_body(desk)
    out = do(body, action="click", target='button "Late"')
    assert out.ok, out.describe()
    assert btn.clicks == 1


def test_disabled_target_is_not_clicked(desk):
    btn = desk.win.add(Button("Apply", Rect(700, 400, 90, 32)))
    btn.enabled = False
    body = make_body(desk)
    out = do(body, action="click", target='button "Apply"')
    assert out.reason == "disabled"
    assert not desk.events("press")


def test_expectation_confirms_the_effect(desk):
    box = desk.win.add(Button("Wrap lines", Rect(700, 400, 120, 28), checkable=True))
    body = make_body(desk)
    out = do(
        body,
        action="click",
        target='checkbox "Wrap lines"',
        expect={"on": 'checkbox "Wrap lines"', "state": "checked"},
    )
    assert out.ok, out.describe()
    assert box.checked

    out = do(
        body,
        action="click",
        target='checkbox "Wrap lines"',
        expect={"on": 'checkbox "Wrap lines"', "state": "checked", "timeout": 0.4},
    )
    assert out.reason == "expectation_failed"


def test_double_click(desk):
    btn = desk.win.add(Button("Open", Rect(700, 400, 90, 32)))
    body = make_body(desk)
    out = do(body, action="click", target="Open", count=2)
    assert out.ok
    assert btn.clicks == 2


def test_human_moving_the_mouse_takes_over_immediately(desk):
    btn = desk.win.add(Button("Export", Rect(1200, 300, 90, 32)))
    desk.at(0.2, lambda: desk.bump_pointer(-60, 45))
    body = make_body(desk)
    out = do(body, action="click", target='button "Export"')
    assert out.status == "interrupted" and out.reason == "human_override"
    assert btn.clicks == 0
    moves_then = desk.moves
    assert out.duration < 0.25
    # The body let go: it issued no further pointer motion.
    assert desk.moves == moves_then
    # ...and the next intent starts from wherever the person left the pointer.
    out = do(body, action="click", target='button "Export"')
    assert out.ok
    assert btn.clicks == 1


def test_human_override_releases_a_held_button(desk):
    desk.win.add(Draggable("Card", Rect(300, 300, 120, 60)))
    desk.win.add(DropZone("Done", Rect(1000, 300, 300, 300)))
    desk.at(0.9, lambda: desk.bump_pointer(0, 80))
    body = make_body(desk)
    out = do(body, action="drag", target="Card", to="Done", duration=1.5)
    assert out.reason == "human_override"
    presses, releases = desk.events("press"), desk.events("release")
    assert len(presses) == 1 and len(releases) == 1


# -- sliders -----------------------------------------------------------------


@pytest.mark.parametrize("want", [0, 13, 50, 87.5, 100])
def test_scrub_reaches_the_requested_value(desk, want):
    s = desk.win.add(Slider("Volume", Rect(400, 500, 400, 20), value=30))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Volume"', value=want)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(want, abs=0.2)
    assert s.grabs == 1


def test_scrub_adapts_to_a_reversed_nonlinear_control(desk):
    s = desk.win.add(
        Slider(
            "Gain",
            Rect(400, 500, 500, 20),
            lo=0,
            hi=10,
            value=1,
            reverse=True,
            curve=lambda f: f * f,
            inverse=lambda f: f**0.5,
        )
    )
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Gain"', value=6.4)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(6.4, abs=0.1)


def test_scrub_vertical_slider(desk):
    s = desk.win.add(Slider("Zoom", Rect(1300, 300, 18, 300), lo=10, hi=400, value=100))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Zoom"', fraction=0.75)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(10 + 0.75 * 390, abs=4)


def test_scrub_stepped_slider_lands_on_the_nearest_step(desk):
    s = desk.win.add(Slider("Rating", Rect(400, 500, 300, 20), lo=0, hi=10, value=2, step=1))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Rating"', value=7)
    assert out.ok, out.describe()
    assert s.value == 7


def test_slow_scrub_takes_the_requested_time(desk):
    s = desk.win.add(Slider("Timeline", Rect(300, 700, 900, 16), lo=0, hi=120, value=0))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Timeline"', value=90, duration=2.0)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(90, abs=1.2)
    assert out.duration > 2.0
    # It swept through the range rather than jumping.
    values = [e[3]["value"] for e in desk.events("value", "Timeline")]
    assert len(values) > 100
    assert values == sorted(values)


def test_scrub_follows_a_slider_that_moves_while_being_dragged(desk):
    s = desk.win.add(Slider("Volume", Rect(400, 500, 400, 20), value=10))
    desk.at(0.5, lambda: desk.animate(desk.win, Vec2(0, 120), 0.5))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Volume"', value=80, duration=1.2)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(80, abs=1.0)


def test_scrub_feels_for_the_part_of_a_slider_that_responds(desk, tmp_path):
    """Reported bounds often include dead space, such as a value readout."""
    s = desk.win.add(Slider("Volume", Rect(400, 500, 400, 30), value=20, band=(0.6, 1.0)))
    memory = SpatialMemory.load(tmp_path / "memory.json")
    body = make_body(desk, memory=memory)
    out = do(body, action="scrub", target='slider "Volume"', value=73)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(73, abs=1.0)
    assert s.grabs == 2
    grip = next(e for e in body.trace.events if e["kind"] == "gripped")
    assert grip["across"] == 0.75

    # Having found it once, it goes straight there the next time.
    out = do(body, action="scrub", target='slider "Volume"', value=30)
    assert out.ok, out.describe()
    assert s.value == pytest.approx(30, abs=1.0)
    assert s.grabs == 3


def test_scrub_gives_up_on_a_slider_that_does_not_respond(desk):
    s = desk.win.add(Slider("Locked", Rect(400, 500, 400, 30), value=20, band=(2.0, 3.0)))
    body = make_body(desk)
    out = do(body, action="scrub", target='slider "Locked"', value=73)
    assert out.status == "failed" and out.reason == "not_adjustable", out.describe()
    assert s.value == 20
    assert not desk._down


def test_scrub_refuses_something_without_a_value(desk):
    desk.win.add(Button("Plain", Rect(400, 500, 100, 30)))
    body = make_body(desk)
    out = do(body, action="scrub", target="Plain", value=3)
    assert out.reason == "not_adjustable"
    assert not desk.events("press")


# -- menus -------------------------------------------------------------------


def build_menus(desk):
    bar = desk.win.add(MenuBar("", Rect(100, 100, 1400, 30)))
    file = bar.add(Menu("File", Rect(100, 100, 60, 30)))
    edit = bar.add(Menu("Edit", Rect(160, 100, 60, 30)))
    file.item("New")
    file.item("Open")
    export = file.submenu("Export")
    file.item("Print")
    file.item("Close")
    export.item("As PNG")
    export.item("As SVG")
    pdf = export.item("As PDF")
    edit.item("Undo")
    return file, edit, export, pdf


def test_submenu_is_reached_without_crossing_sibling_items(desk):
    file, edit, export, pdf = build_menus(desk)
    body = make_body(desk, snapshot_hz=60)
    assert do(body, action="click", target='menu "File"', expect={"appears": 'menu "Export"'}).ok
    assert do(body, action="hover", target='menu "Export"', expect={"appears": 'menu_item "As PDF"'}).ok
    before = len(desk.log)
    out = do(body, action="click", target='menu_item "As PDF"')
    assert out.ok, out.describe()
    assert pdf.activated == 1
    entered = [e[2] for e in desk.log[before:] if e[1] == "enter"]
    assert "Print" not in entered and "Close" not in entered
    assert export.opens == 1


def test_naive_straight_line_would_have_lost_the_submenu(desk):
    """The hazard is real: without hover-safe routing the submenu collapses."""
    file, edit, export, pdf = build_menus(desk)
    body = make_body(desk, snapshot_hz=60)
    do(body, action="click", target='menu "File"')
    do(body, action="hover", target='menu "Export"', expect={"appears": 'menu_item "As PDF"'})
    start, goal = desk.pointer, pdf.rect.center
    for i in range(1, 41):
        p = start.lerp(goal, i / 40)
        desk.move_to(*p.rounded())
        desk.clock.advance(0.004)
    assert not export.open


def test_memory_replays_the_menu_path_without_a_planner(desk, tmp_path):
    file, edit, export, pdf = build_menus(desk)
    memory = SpatialMemory.load(tmp_path / "memory.json")
    body = make_body(desk, memory=memory, snapshot_hz=60)

    # First time: each step is spelled out, and the body learns what reveals what.
    assert do(body, action="click", target='menu "File"').ok
    assert do(body, action="hover", target='menu "Export"', expect={"appears": 'menu_item "As PDF"'}).ok
    assert do(body, action="click", target='menu_item "As PDF"').ok
    assert pdf.activated == 1
    body.rest(1.0)
    assert not file.open
    body.close()

    # A fresh body with the saved memory gets there from a single intent.
    body2 = make_body(desk, memory=SpatialMemory.load(tmp_path / "memory.json"), snapshot_hz=60)
    out = do(body2, action="click", target='menu_item "As PDF"')
    assert out.ok, out.describe()
    assert pdf.activated == 2
    assert "revealing" in body2.trace.kinds()


def test_closed_menu_path_is_read_off_the_element_tree(desk):
    """No memory and no planner: the hidden entry's ancestors say what to open."""
    file, edit, export, pdf = build_menus(desk)
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="click", target='menu_item "As PDF"')
    assert out.ok, out.describe()
    assert pdf.activated == 1
    reveal = next(e for e in body.trace.events if e["kind"] == "revealing")
    assert len(reveal["via"]) == 2


# -- drag, scroll, type -------------------------------------------------------


def test_drag_and_drop_onto_an_element(desk):
    card = desk.win.add(Draggable("Card", Rect(300, 300, 120, 60)))
    zone = desk.win.add(DropZone("Done", Rect(1000, 300, 300, 300)))
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="drag", target="Card", to="Done")
    assert out.ok, out.describe()
    assert card.dropped_on == "Done"
    assert zone.received == ["Card"]


def test_drag_by_offset(desk):
    s = desk.win.add(Slider("Balance", Rect(400, 500, 400, 20), lo=-1, hi=1, value=-1))
    body = make_body(desk)
    out = do(body, action="drag", target='slider "Balance"', offset=[150, 0])
    assert out.ok, out.describe()
    assert s.value > -1


def test_scroll_until_a_row_comes_into_view(desk):
    rows = [f"Row {i}" for i in range(60)]
    lst = desk.win.add(ScrollList("Files", Rect(300, 200, 300, 240), rows))
    body = make_body(desk, snapshot_hz=60)
    assert not body.world.find('list_item "Row 41"')
    out = do(body, action="scroll", target='list "Files"', until='list_item "Row 41"')
    assert out.ok, out.describe()
    assert lst.offset > 0
    out = do(body, action="click", target='list_item "Row 41"')
    assert out.ok, out.describe()
    assert desk.find("Row 41").clicks == 1
    at = Vec2(*desk.events("click", "Row 41")[0][3]["at"])
    assert lst.rect.contains(at)


def test_scroll_gives_up_when_the_row_never_shows(desk):
    desk.win.add(ScrollList("Files", Rect(300, 200, 300, 240), ["a", "b"]))
    body = make_body(desk, config=BodyConfig(scroll_limit=5))
    out = do(body, action="scroll", target='list "Files"', until='list_item "zzz"')
    assert out.reason == "not_found"


def test_type_into_a_field(desk):
    field = desk.win.add(TextField("Search", Rect(500, 200, 300, 30)))
    body = make_body(desk)
    out = do(body, action="type", target='text_field "Search"', text="quarterly report", enter=True)
    assert out.ok, out.describe()
    assert field.text == "quarterly report"
    assert field.submitted == ["quarterly report"]


def test_key_chord_presses_and_releases_in_order(desk):
    body = make_body(desk)
    out = do(body, action="key", keys="ctrl+shift+s")
    assert out.ok
    assert desk.keys == [
        ("ctrl", True), ("shift", True), ("s", True),
        ("s", False), ("shift", False), ("ctrl", False),
    ]


def test_wait_for_something_to_appear(desk):
    dlg = desk.add(Dialog("Done", Rect(600, 300, 300, 200)))
    desk.at(0.5, dlg.show)
    body = make_body(desk, snapshot_hz=60)
    out = do(body, action="wait", expect={"appears": 'dialog "Done"', "timeout": 2})
    assert out.ok
    assert 0.45 < out.duration < 0.7


# -- multi-monitor -------------------------------------------------------------


def test_reach_across_monitors_stays_on_the_desktop():
    left, right = Rect(0, 300, 1280, 720), Rect(1280, 0, 1920, 1080)
    d = SimDesktop(monitors=(left, right), pointer=Vec2(100, 900))
    win = d.add(Window("Viewer", Rect(1400, 20, 1600, 300)))
    btn = win.add(Button("Top right", Rect(2900, 40, 100, 30)))
    body = make_body(d)
    out = do(body, action="click", target="Top right")
    assert out.ok, out.describe()
    assert btn.clicks == 1
    for s in body.trace.samples:
        assert left.contains(s.pos) or right.contains(s.pos)
    assert body.world.monitor_at(btn.rect.center) == 1
