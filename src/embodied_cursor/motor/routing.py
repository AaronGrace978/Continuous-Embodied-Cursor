# SPDX-License-Identifier: GPL-3.0-or-later
"""Hover-safe routing.

A pointer has side effects merely by passing over things: menus switch,
submenus collapse, tooltips and hover cards open. The router finds the
shortest polyline from the cursor to its goal that stays clear of a set of
rectangles, using a visibility graph over their (inflated) corners. The
controller then threads a minimum-jerk curve through the polyline's corners.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Sequence

from ..geometry import Rect, Vec2

_CORNER_PUSH = 1.0


@dataclass(frozen=True, slots=True)
class Route:
    via: tuple[Vec2, ...]
    """Corners to pass through, in order, excluding start and goal."""

    blocked: bool = False
    """True when no clear path exists; ``via`` is then empty."""


def _strictly_inside(r: Rect, p: Vec2) -> bool:
    return r.left < p.x < r.right and r.top < p.y < r.bottom


def keepout_zones(start: Vec2, goal: Vec2, obstacles: Sequence[Rect], margin: float) -> list[Rect]:
    """Inflate obstacles by ``margin``, backing off near the endpoints.

    An obstacle that already contains the start or the goal cannot be avoided
    and is dropped. One that merely sits close to an endpoint gets a smaller
    margin so the endpoint stays outside its keep-out zone.
    """
    zones: list[Rect] = []
    for o in obstacles:
        if o.empty or o.contains(start) or o.contains(goal):
            continue
        m = min(margin, 0.9 * o.distance_to(start), 0.9 * o.distance_to(goal))
        zone = o.inflate(m) if m > 0.25 else o
        if _strictly_inside(zone, start) or _strictly_inside(zone, goal):
            continue
        zones.append(zone)
    return zones


def segment_clear(a: Vec2, b: Vec2, zones: Sequence[Rect]) -> bool:
    return not any(z.segment_hits(a, b) for z in zones)


def route(
    start: Vec2,
    goal: Vec2,
    obstacles: Sequence[Rect],
    margin: float = 10.0,
    bounds: Sequence[Rect] = (),
) -> Route:
    """Shortest obstacle-free polyline from ``start`` to ``goal``.

    ``bounds`` lists the monitor rectangles; corners outside all of them are
    not usable because the pointer cannot leave the desktop.
    """
    if not obstacles:
        return Route(())

    # Obstacles far from the straight line cannot be on a shortest path worth taking.
    reach = max(200.0, 0.75 * start.dist(goal))
    corridor = Rect.from_ltrb(
        min(start.x, goal.x) - reach,
        min(start.y, goal.y) - reach,
        max(start.x, goal.x) + reach,
        max(start.y, goal.y) + reach,
    )
    zones = keepout_zones(start, goal, [o for o in obstacles if o.intersects(corridor)], margin)
    if segment_clear(start, goal, zones):
        return Route(())

    nodes: list[Vec2] = [start, goal]
    for z in zones:
        for c, (sx, sy) in zip(z.corners(), ((-1, -1), (1, -1), (1, 1), (-1, 1))):
            p = Vec2(c.x + sx * _CORNER_PUSH, c.y + sy * _CORNER_PUSH)
            if any(_strictly_inside(other, p) for other in zones):
                continue
            if bounds and not any(b.contains(p) for b in bounds):
                continue
            nodes.append(p)

    visible: dict[tuple[int, int], bool] = {}

    def can_see(i: int, j: int) -> bool:
        key = (i, j) if i < j else (j, i)
        hit = visible.get(key)
        if hit is None:
            hit = segment_clear(nodes[i], nodes[j], zones)
            visible[key] = hit
        return hit

    # A* over the implicit complete graph; edges are tested lazily.
    n = len(nodes)
    best = [float("inf")] * n
    parent = [-1] * n
    best[0] = 0.0
    closed = [False] * n
    heap: list[tuple[float, int]] = [(start.dist(goal), 0)]
    while heap:
        _, i = heapq.heappop(heap)
        if closed[i]:
            continue
        if i == 1:
            break
        closed[i] = True
        for j in range(1, n):
            if closed[j] or j == i:
                continue
            g = best[i] + nodes[i].dist(nodes[j])
            if g < best[j] and can_see(i, j):
                best[j] = g
                parent[j] = i
                heapq.heappush(heap, (g + nodes[j].dist(goal), j))

    if parent[1] < 0:
        return Route((), blocked=True)
    path: list[Vec2] = []
    i = parent[1]
    while i > 0:
        path.append(nodes[i])
        i = parent[i]
    path.reverse()
    return Route(tuple(path))
