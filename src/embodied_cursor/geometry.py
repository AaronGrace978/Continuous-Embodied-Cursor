# SPDX-License-Identifier: GPL-3.0-or-later
"""Small 2-D geometry kit shared by perception and motor code.

Coordinates are global desktop pixels: x grows right, y grows down, and the
origin is the top-left corner of the virtual desktop spanning all monitors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence


@dataclass(frozen=True, slots=True)
class Vec2:
    x: float = 0.0
    y: float = 0.0

    def __add__(self, o: "Vec2") -> "Vec2":
        return Vec2(self.x + o.x, self.y + o.y)

    def __sub__(self, o: "Vec2") -> "Vec2":
        return Vec2(self.x - o.x, self.y - o.y)

    def __mul__(self, k: float) -> "Vec2":
        return Vec2(self.x * k, self.y * k)

    __rmul__ = __mul__

    def __truediv__(self, k: float) -> "Vec2":
        return Vec2(self.x / k, self.y / k)

    def __neg__(self) -> "Vec2":
        return Vec2(-self.x, -self.y)

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y

    def dot(self, o: "Vec2") -> float:
        return self.x * o.x + self.y * o.y

    def cross(self, o: "Vec2") -> float:
        return self.x * o.y - self.y * o.x

    def norm(self) -> float:
        return math.hypot(self.x, self.y)

    def dist(self, o: "Vec2") -> float:
        return math.hypot(self.x - o.x, self.y - o.y)

    def unit(self) -> "Vec2":
        n = self.norm()
        return Vec2(self.x / n, self.y / n) if n > 1e-12 else Vec2(0.0, 0.0)

    def lerp(self, o: "Vec2", t: float) -> "Vec2":
        return Vec2(self.x + (o.x - self.x) * t, self.y + (o.y - self.y) * t)

    def rounded(self) -> tuple[int, int]:
        return (int(math.floor(self.x + 0.5)), int(math.floor(self.y + 0.5)))

    def is_finite(self) -> bool:
        return math.isfinite(self.x) and math.isfinite(self.y)


ZERO = Vec2(0.0, 0.0)


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle; ``x, y`` is the top-left corner."""

    x: float
    y: float
    w: float
    h: float

    @staticmethod
    def from_ltrb(left: float, top: float, right: float, bottom: float) -> "Rect":
        return Rect(left, top, max(0.0, right - left), max(0.0, bottom - top))

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> Vec2:
        return Vec2(self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def empty(self) -> bool:
        return self.w <= 0.0 or self.h <= 0.0

    def corners(self) -> tuple[Vec2, Vec2, Vec2, Vec2]:
        return (
            Vec2(self.left, self.top),
            Vec2(self.right, self.top),
            Vec2(self.right, self.bottom),
            Vec2(self.left, self.bottom),
        )

    def contains(self, p: Vec2, margin: float = 0.0) -> bool:
        """True when ``p`` lies inside the rectangle shrunk by ``margin``."""
        return (
            self.left + margin <= p.x < self.right - margin
            and self.top + margin <= p.y < self.bottom - margin
        )

    def contains_rect(self, o: "Rect") -> bool:
        return (
            self.left <= o.left
            and self.top <= o.top
            and self.right >= o.right
            and self.bottom >= o.bottom
        )

    def intersects(self, o: "Rect") -> bool:
        return (
            self.left < o.right
            and o.left < self.right
            and self.top < o.bottom
            and o.top < self.bottom
        )

    def intersection(self, o: "Rect") -> "Rect":
        left, top = max(self.left, o.left), max(self.top, o.top)
        right, bottom = min(self.right, o.right), min(self.bottom, o.bottom)
        return Rect.from_ltrb(left, top, right, bottom)

    def union(self, o: "Rect") -> "Rect":
        return Rect.from_ltrb(
            min(self.left, o.left),
            min(self.top, o.top),
            max(self.right, o.right),
            max(self.bottom, o.bottom),
        )

    def inflate(self, m: float) -> "Rect":
        """Grow (or, for negative ``m``, shrink) by ``m`` on every side."""
        w = max(0.0, self.w + 2 * m)
        h = max(0.0, self.h + 2 * m)
        c = self.center
        return Rect(c.x - w / 2.0, c.y - h / 2.0, w, h)

    def translate(self, d: Vec2) -> "Rect":
        return Rect(self.x + d.x, self.y + d.y, self.w, self.h)

    def clamp(self, p: Vec2, margin: float = 0.0) -> Vec2:
        """Nearest point to ``p`` inside the rectangle shrunk by ``margin``."""
        mx = min(margin, self.w / 2.0)
        my = min(margin, self.h / 2.0)
        return Vec2(
            min(max(p.x, self.left + mx), self.right - mx),
            min(max(p.y, self.top + my), self.bottom - my),
        )

    def distance_to(self, p: Vec2) -> float:
        return p.dist(self.clamp(p))

    def point_at(self, fx: float, fy: float) -> Vec2:
        """Point at fractional position ``(fx, fy)`` with (0, 0) top-left."""
        return Vec2(self.x + self.w * fx, self.y + self.h * fy)

    def segment_hits(self, a: Vec2, b: Vec2) -> bool:
        """True when segment ``a-b`` passes through the open interior."""
        return self.segment_clip(a, b) is not None

    def segment_clip(self, a: Vec2, b: Vec2) -> tuple[float, float] | None:
        """Liang-Barsky clip against the open interior.

        Returns the parameter interval ``(t0, t1)`` of the part of ``a-b``
        strictly inside the rectangle, or ``None`` when the segment only
        touches the boundary or misses entirely.
        """
        if self.empty:
            return None
        dx, dy = b.x - a.x, b.y - a.y
        t0, t1 = 0.0, 1.0
        for p, q in (
            (-dx, a.x - self.left),
            (dx, self.right - a.x),
            (-dy, a.y - self.top),
            (dy, self.bottom - a.y),
        ):
            if abs(p) < 1e-12:
                if q <= 0.0:
                    return None
                continue
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return None
                t1 = min(t1, r)
        if t1 - t0 <= 1e-9:
            return None
        return (t0, t1)

    def subtract(self, o: "Rect") -> list["Rect"]:
        """The parts of this rectangle not covered by ``o`` (up to four)."""
        if not self.intersects(o):
            return [self]
        out: list[Rect] = []
        i = self.intersection(o)
        if i.top > self.top:
            out.append(Rect.from_ltrb(self.left, self.top, self.right, i.top))
        if i.bottom < self.bottom:
            out.append(Rect.from_ltrb(self.left, i.bottom, self.right, self.bottom))
        if i.left > self.left:
            out.append(Rect.from_ltrb(self.left, i.top, i.left, i.bottom))
        if i.right < self.right:
            out.append(Rect.from_ltrb(i.right, i.top, self.right, i.bottom))
        return [r for r in out if not r.empty]

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)


def visible_parts(target: Rect, occluders: Iterable[Rect]) -> list[Rect]:
    """Decompose the part of ``target`` not hidden by ``occluders``.

    The pieces are axis-aligned, pairwise disjoint, and together cover exactly
    the unoccluded area. The decomposition favours wide horizontal bands, so a
    piece is not necessarily the largest rectangle that would fit.
    """
    parts = [target] if not target.empty else []
    for occ in occluders:
        if not parts:
            break
        nxt: list[Rect] = []
        for part in parts:
            nxt.extend(part.subtract(occ))
        parts = nxt
    return parts


def clamp_to_any(p: Vec2, rects: Sequence[Rect]) -> Vec2:
    """Clamp ``p`` into the nearest of ``rects`` (a multi-monitor desktop)."""
    if not rects:
        return p
    best, best_d = p, math.inf
    for r in rects:
        if r.contains(p):
            return p
        # Stay strictly inside so the result still tests as contained.
        c = Vec2(
            min(max(p.x, r.left), max(r.left, r.right - 1.0)),
            min(max(p.y, r.top), max(r.top, r.bottom - 1.0)),
        )
        d = c.dist(p)
        if d < best_d:
            best, best_d = c, d
    return best


def polyline_length(points: Sequence[Vec2]) -> float:
    return sum(points[i].dist(points[i + 1]) for i in range(len(points) - 1))
