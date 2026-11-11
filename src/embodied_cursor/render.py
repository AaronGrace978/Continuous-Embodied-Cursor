# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw a recorded run as a self-contained SVG.

The top panel is the screen: where the cursor went, one dot per motor tick
(so spacing shows speed), with presses and releases marked. The bottom panel
is speed against time, where each reach should show up as one smooth bump.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Sequence

from .geometry import Rect
from .trace import Sample, Trace
from .world.element import SURFACE_ROLES, UIElement
from .world.model import WorldModel

_BG = "#0f1320"
_PANEL = "#171c2e"
_GRID = "#2a3150"
_TEXT = "#c9d1e9"
_MUTED = "#7a84a8"
_PRESS = "#ff5d73"
_RELEASE = "#4dd4ac"
_HELD = "#ffb454"
_AIM = "#e8d27a"


def scene_of(world: WorldModel, limit: int = 400) -> list[dict[str, Any]]:
    """Visible, named elements worth drawing behind a trace."""
    out = []
    for el in world.elements():
        if not el.visible or not (el.name or el.role in SURFACE_ROLES):
            continue
        if not (el.interactive or el.role in SURFACE_ROLES):
            continue
        out.append({"role": el.role, "name": el.name, "bounds": [round(v, 1) for v in el.bounds.as_tuple()], "layer": el.layer})
        if len(out) >= limit:
            break
    return out


def _speed_colour(fraction: float) -> str:
    """Cool blue when slow, warm when fast."""
    stops = [(0.0, (86, 156, 255)), (0.5, (160, 110, 255)), (1.0, (255, 110, 160))]
    f = min(max(fraction, 0.0), 1.0)
    for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
        if f <= f1:
            k = (f - f0) / (f1 - f0)
            r, g, b = (round(a + (b_ - a) * k) for a, b_ in zip(c0, c1))
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#ff6ea0"


def _samples(trace: "Trace | dict[str, Any]") -> list[Sample]:
    if isinstance(trace, Trace):
        return trace.samples
    from .geometry import Vec2

    out = []
    for t, x, y, vx, vy, gx, gy, buttons in trace["samples"]:
        out.append(Sample(t, Vec2(x, y), Vec2(vx, vy), None if gx is None else Vec2(gx, gy), buttons))
    return out


def render_svg(
    trace: "Trace | dict[str, Any]",
    scene: "Iterable[dict[str, Any] | UIElement]" = (),
    title: str = "",
    width: int = 1100,
    notes: Sequence[str] = (),
) -> str:
    samples = _samples(trace)
    events = trace.events if isinstance(trace, Trace) else trace.get("events", [])
    if not samples:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="60"><text x="10" y="35">empty trace</text></svg>'

    items = [e if isinstance(e, dict) else {"role": e.role, "name": e.name, "bounds": list(e.bounds.as_tuple()), "layer": e.layer} for e in scene]

    xs = [s.pos.x for s in samples] + [s.goal.x for s in samples if s.goal is not None]
    ys = [s.pos.y for s in samples] + [s.goal.y for s in samples if s.goal is not None]
    box = Rect.from_ltrb(min(xs), min(ys), max(xs), max(ys)).inflate(70)
    # Grow the view to take in elements the path touches or ends near.
    for it in items:
        r = Rect(*it["bounds"])
        if it["role"] not in SURFACE_ROLES and r.intersects(box) and r.w < 900 and r.h < 500:
            box = box.union(r.inflate(12))

    pad = 16
    head = 44 if title else 12
    map_w = width - 2 * pad
    scale = min(map_w / box.w, 560 / box.h, 2.5)
    map_h = box.h * scale
    plot_h = 150
    foot = 18 * len(notes) + (10 if notes else 0)
    height = head + map_h + 34 + plot_h + 46 + foot

    def sx(x: float) -> float:
        return pad + (map_w - box.w * scale) / 2 + (x - box.left) * scale

    def sy(y: float) -> float:
        return head + (y - box.top) * scale

    o: list[str] = []
    o.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height:.0f}" width="{width}" height="{height:.0f}" '
        f'font-family="ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif">'
    )
    o.append(f'<rect width="{width}" height="{height:.0f}" fill="{_BG}"/>')
    if title:
        o.append(f'<text x="{pad}" y="28" fill="{_TEXT}" font-size="17" font-weight="600">{escape(title)}</text>')
    o.append(f'<clipPath id="map"><rect x="{pad}" y="{head}" width="{map_w}" height="{map_h:.1f}" rx="8"/></clipPath>')
    o.append(f'<rect x="{pad}" y="{head}" width="{map_w}" height="{map_h:.1f}" rx="8" fill="{_PANEL}"/>')
    o.append('<g clip-path="url(#map)">')

    for it in sorted(items, key=lambda it: (it.get("layer", 0), it["role"] not in SURFACE_ROLES)):
        r = Rect(*it["bounds"])
        if not r.intersects(box):
            continue
        surface = it["role"] in SURFACE_ROLES
        fill = "#1d2440" if surface else "#263056"
        stroke = "#39446e" if surface else "#56639a"
        o.append(
            f'<rect x="{sx(r.left):.1f}" y="{sy(r.top):.1f}" width="{r.w * scale:.1f}" height="{r.h * scale:.1f}" '
            f'rx="{2 if surface else 4}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
        )
        if it["name"] and r.w * scale > 34 and not surface:
            size = min(12.0, max(8.0, r.h * scale * 0.45))
            label = it["name"] if len(it["name"]) * size * 0.55 < r.w * scale else it["name"][: max(int(r.w * scale / (size * 0.55)) - 1, 1)] + "…"
            o.append(
                f'<text x="{sx(r.center.x):.1f}" y="{sy(r.center.y) + size * 0.35:.1f}" fill="{_MUTED}" '
                f'font-size="{size:.1f}" text-anchor="middle">{escape(label)}</text>'
            )

    # Where the controller was aiming over time: a moving target leaves a trail.
    aims: list[list[tuple[float, float]]] = []
    last = None
    for s in samples:
        if s.goal is None:
            last = None
            continue
        point = (s.goal.x, s.goal.y)
        if last is None or abs(point[0] - last[0]) + abs(point[1] - last[1]) > 60:
            aims.append([point])
        elif point != last:
            aims[-1].append(point)
        last = point
    for run in aims:
        x, y = run[0]
        o.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="none" stroke="{_AIM}" stroke-width="1.2" stroke-dasharray="2 2"/>')
        if len(run) > 1:
            pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in run)
            o.append(f'<polyline points="{pts}" fill="none" stroke="{_AIM}" stroke-width="1.2" stroke-dasharray="4 4"/>')

    peak = max((s.vel.norm() for s in samples), default=1.0) or 1.0
    for a, b in zip(samples, samples[1:]):
        if a.pos == b.pos:
            continue
        colour = _HELD if b.buttons else _speed_colour(b.vel.norm() / peak)
        o.append(
            f'<line x1="{sx(a.pos.x):.1f}" y1="{sy(a.pos.y):.1f}" x2="{sx(b.pos.x):.1f}" y2="{sy(b.pos.y):.1f}" '
            f'stroke="{colour}" stroke-width="{2.6 if b.buttons else 1.8}" stroke-linecap="round"/>'
        )
    step = max(len(samples) // 1500, 1)
    for s in samples[::step]:
        o.append(f'<circle cx="{sx(s.pos.x):.1f}" cy="{sy(s.pos.y):.1f}" r="1.5" fill="#ffffff" fill-opacity="0.55"/>')

    start = samples[0].pos
    o.append(f'<circle cx="{sx(start.x):.1f}" cy="{sy(start.y):.1f}" r="5" fill="none" stroke="{_TEXT}" stroke-width="1.5"/>')
    for ev in events:
        at = ev.get("at")
        if ev["kind"] == "press" and at:
            o.append(f'<circle cx="{sx(at[0]):.1f}" cy="{sy(at[1]):.1f}" r="6.5" fill="none" stroke="{_PRESS}" stroke-width="2"/>')
        elif ev["kind"] == "release" and at:
            o.append(f'<circle cx="{sx(at[0]):.1f}" cy="{sy(at[1]):.1f}" r="3" fill="{_RELEASE}"/>')
    o.append("</g>")

    # -- speed profile ------------------------------------------------------
    py = head + map_h + 34
    t0, t1 = samples[0].t, samples[-1].t
    span = max(t1 - t0, 1e-6)

    def tx(t: float) -> float:
        return pad + 44 + (t - t0) / span * (map_w - 56)

    def vy(v: float) -> float:
        return py + plot_h - v / peak * (plot_h - 12)

    o.append(f'<text x="{pad}" y="{py - 10:.0f}" fill="{_MUTED}" font-size="12">speed (px/s) over time (s)</text>')
    o.append(f'<rect x="{pad}" y="{py:.0f}" width="{map_w}" height="{plot_h}" rx="8" fill="{_PANEL}"/>')
    for frac in (0.5, 1.0):
        y = vy(peak * frac)
        o.append(f'<line x1="{pad + 44}" y1="{y:.1f}" x2="{pad + map_w - 12}" y2="{y:.1f}" stroke="{_GRID}" stroke-dasharray="3 4"/>')
        o.append(f'<text x="{pad + 38}" y="{y + 4:.1f}" fill="{_MUTED}" font-size="10" text-anchor="end">{peak * frac:.0f}</text>')
    ticks = max(int(span), 1)
    every = max(ticks // 10, 1)
    for i in range(0, ticks + 1, every):
        x = tx(t0 + i)
        if x <= pad + map_w - 12:
            o.append(f'<text x="{x:.1f}" y="{py + plot_h + 14:.0f}" fill="{_MUTED}" font-size="10" text-anchor="middle">{i}</text>')

    held_from = None
    for s in samples:
        if s.buttons and held_from is None:
            held_from = s.t
        elif not s.buttons and held_from is not None:
            o.append(
                f'<rect x="{tx(held_from):.1f}" y="{py + 4:.0f}" width="{max(tx(s.t) - tx(held_from), 1.5):.1f}" '
                f'height="{plot_h - 8}" fill="{_HELD}" fill-opacity="0.13"/>'
            )
            held_from = None
    points = " ".join(f"{tx(s.t):.1f},{vy(s.vel.norm()):.1f}" for s in samples[::step])
    o.append(f'<polyline points="{points}" fill="none" stroke="#8fb4ff" stroke-width="1.6" stroke-linejoin="round"/>')
    for ev in events:
        if ev["kind"] == "press":
            o.append(f'<line x1="{tx(ev["t"]):.1f}" y1="{py + 4:.0f}" x2="{tx(ev["t"]):.1f}" y2="{py + plot_h - 4:.0f}" stroke="{_PRESS}" stroke-width="1"/>')

    ly = py + plot_h + 34
    legend = [
        (_speed_colour(0.1), "slow"),
        (_speed_colour(0.95), "fast"),
        (_HELD, "button held"),
        (_AIM, "aim point"),
        (_PRESS, "press"),
        (_RELEASE, "release"),
    ]
    x = pad
    for colour, label in legend:
        o.append(f'<rect x="{x}" y="{ly - 9:.0f}" width="14" height="4" rx="2" fill="{colour}"/>')
        o.append(f'<text x="{x + 20}" y="{ly - 4:.0f}" fill="{_MUTED}" font-size="11">{label}</text>')
        x += 34 + len(label) * 7
    o.append(f'<text x="{width - pad}" y="{ly - 4:.0f}" fill="{_MUTED}" font-size="11" text-anchor="end">one dot per motor tick</text>')
    for i, note in enumerate(notes):
        o.append(f'<text x="{pad}" y="{ly + 18 + 18 * i:.0f}" fill="{_TEXT}" font-size="12">{escape(note)}</text>')
    o.append("</svg>")
    return "\n".join(o)
