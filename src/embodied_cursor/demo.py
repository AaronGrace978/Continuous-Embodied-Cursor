# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained demonstrations on the simulated desktop.

Each scenario stages one situation the architecture exists for, runs it
through the same body that drives a real desktop, and returns the numbers
that show what happened. Nothing here needs a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backends.sim import Button, Dialog, Menu, MenuBar, SimDesktop, Slider, Window, make_body
from .body import Body
from .brain.agent import Agent
from .brain.heuristic import HeuristicPlanner
from .geometry import Rect, Vec2
from .intents import intent_from_obj
from .render import render_svg, scene_of


@dataclass
class DemoResult:
    name: str
    title: str
    ok: bool
    facts: list[str] = field(default_factory=list)
    svg: str = ""


def _desk() -> tuple[SimDesktop, Window]:
    desk = SimDesktop(pointer=Vec2(220, 760))
    win = desk.add(Window("Editor", Rect(100, 100, 1400, 800)))
    return desk, win  # type: ignore[return-value]


def _slowest(body: Body, t0: float, t1: float) -> float:
    start = body.trace.samples[0].t
    return min((s.vel.norm() for s in body.trace.samples if t0 <= s.t - start <= t1), default=0.0)


def _jerkiness(body: Body) -> float:
    s = body.trace.samples
    return max(((b.vel - a.vel).norm() for a, b in zip(s, s[1:])), default=0.0)


def moving_target() -> DemoResult:
    desk, win = _desk()
    button = win.add(Button("Save", Rect(1200, 260, 96, 34)))
    desk.at(0.20, lambda: button.move_to(1150, 660))
    desk.at(0.45, lambda: desk.animate(button, Vec2(-300, -60), 0.5))
    body = make_body(desk)
    out = body.execute(intent_from_obj({"action": "click", "target": 'button "Save"'}))
    facts = [
        f"the button jumped 400 px at t=0.20 s, then slid another 300 px; clicked it at t={out.duration:.2f} s",
        f"the trajectory was re-planned in flight {body.controller.replans} times and never restarted: "
        f"in the 200 ms after the jump the cursor never dropped below {_slowest(body, 0.2, 0.4):.0f} px/s",
        f"peak {body.trace.peak_speed():.0f} px/s; largest velocity step between 120 Hz ticks {_jerkiness(body):.0f} px/s",
    ]
    ok = out.ok and button.clicks == 1
    return DemoResult("moving-target", "A target that moves mid-flight", ok, facts, _svg(body, "A target that moves mid-flight", facts))


def popup() -> DemoResult:
    desk, win = _desk()
    button = win.add(Button("Publish", Rect(1150, 300, 110, 36)))
    toast = desk.add(Dialog("Update available", Rect(1040, 250, 380, 150)))
    desk.at(0.25, toast.show)
    desk.at(1.05, toast.hide)
    body = make_body(desk, snapshot_hz=60)
    out = body.execute(intent_from_obj({"action": "click", "target": 'button "Publish"'}))
    waits = [e for e in body.trace.events if e["kind"] == "waiting"]
    facts = [
        "a popup covered the target from t=0.25 s to t=1.05 s",
        f"the cursor held short of it ({len(waits)} wait), then finished the click at t={out.duration:.2f} s",
        f"clicks delivered to the popup: 0; to the button: {button.clicks}",
    ]
    ok = out.ok and button.clicks == 1 and not desk.events("press", "Update available")
    return DemoResult("popup", "A popup appears over the target", ok, facts, _svg(body, "A popup appears over the target", facts))


def _menus(win: Window) -> tuple[Menu, Any]:
    bar = win.add(MenuBar("", Rect(100, 100, 1400, 30)))
    file = bar.add(Menu("File", Rect(100, 100, 60, 30)))
    bar.add(Menu("Edit", Rect(160, 100, 60, 30))).item("Undo")  # type: ignore[union-attr]
    for name in ("New", "Open"):
        file.item(name)  # type: ignore[union-attr]
    export = file.submenu("Export as")  # type: ignore[union-attr]
    for name in ("Print", "Close"):
        file.item(name)  # type: ignore[union-attr]
    export.item("PNG image")
    export.item("SVG drawing")
    return export, export.item("PDF document")


def nested_menu() -> DemoResult:
    desk, win = _desk()
    export, pdf = _menus(win)
    body = make_body(desk, snapshot_hz=60)
    start = len(desk.log)
    report = Agent(body, HeuristicPlanner()).run("open File > Export as > PDF document")
    entered = [e[2] for e in desk.log[start:] if e[1] == "enter"]
    strayed = [n for n in entered if n in ("Print", "Close")]
    facts = [
        "one intent; the path through the closed menus was read off the element tree",
        f"the submenu opened {export.opens} time and stayed open: siblings crossed on the way in: {len(strayed)}",
        f"route bent around them via {[tuple(round(c) for c in v) for v in body.controller.via] or 'a straight line'}",
    ]
    ok = report.ok and pdf.activated == 1 and not strayed
    return DemoResult("nested-menu", "A nested menu, without losing the submenu", ok, facts, _svg(body, "A nested menu, without losing the submenu", facts))


def scrub() -> DemoResult:
    desk, win = _desk()
    slider = win.add(
        Slider("Gain", Rect(380, 520, 620, 22), lo=0, hi=100, value=12, curve=lambda f: f * f, inverse=lambda f: f**0.5)
    )
    body = make_body(desk)
    out = body.execute(intent_from_obj({"action": "scrub", "target": 'slider "Gain"', "value": 64, "duration": 1.6}))
    values = [e[3]["value"] for e in desk.events("value", "Gain")]
    facts = [
        "a slider with a quadratic response, unknown to the controller",
        f"asked for 64, landed on {slider.value:.1f} after sweeping through {len(values)} intermediate values",
        "position was servoed on the value the control reported, not on where the handle ought to be",
    ]
    ok = out.ok and abs(slider.value - 64) <= 1.0
    return DemoResult("scrub", "Scrubbing a non-linear slider by feedback", ok, facts, _svg(body, "Scrubbing a non-linear slider by feedback", facts))


def _svg(body: Body, title: str, facts: list[str]) -> str:
    return render_svg(body.trace, scene_of(body.world), title, notes=facts)


SCENARIOS: dict[str, Callable[[], DemoResult]] = {
    "moving-target": moving_target,
    "popup": popup,
    "nested-menu": nested_menu,
    "scrub": scrub,
}


def run_all(out_dir: Path | str | None = None, only: str | None = None) -> list[DemoResult]:
    results = []
    for name, scenario in SCENARIOS.items():
        if only and name != only:
            continue
        result = scenario()
        results.append(result)
        if out_dir is not None:
            directory = Path(out_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{name}.svg").write_text(result.svg, encoding="utf-8")
    return results
