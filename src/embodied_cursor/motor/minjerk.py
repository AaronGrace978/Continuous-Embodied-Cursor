# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimum-jerk trajectories.

Human point-to-point reaches are well described by the trajectory that
minimises integrated squared jerk (Flash & Hogan, 1985). The minimiser is a
quintic polynomial per axis, which gives the familiar straight path with a
bell-shaped speed profile.

Two properties make it a good fit for closed-loop cursor control:

* Any state ``(p, v, a)`` can be the start of a new minimum-jerk plan, so a
  target that moves mid-flight is handled by re-planning from the current
  state. Position, velocity and acceleration stay continuous across the
  re-plan, so the path bends instead of restarting.
* The remainder of an optimal plan is itself optimal, so sampling the plan at
  successive ticks is an exact discretisation of the continuous-time feedback
  law (Hoff & Arbib, 1993) at any tick rate, with no integration error.

Curved reaches around obstacles use the via-point form: positions are pinned
at intermediate knots while velocity and acceleration there are free, and
optimality requires jerk and snap to be continuous across each knot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..geometry import Vec2

Coeffs = tuple[float, float, float, float, float, float]


def quintic(p0: float, v0: float, a0: float, p1: float, v1: float, a1: float, T: float) -> Coeffs:
    """Coefficients of the quintic meeting the six boundary values over ``[0, T]``."""
    if T <= 0.0:
        raise ValueError("duration must be positive")
    d = p1 - p0
    T2 = T * T
    T3 = T2 * T
    c3 = (20.0 * d - (8.0 * v1 + 12.0 * v0) * T - (3.0 * a0 - a1) * T2) / (2.0 * T3)
    c4 = (-30.0 * d + (14.0 * v1 + 16.0 * v0) * T + (3.0 * a0 - 2.0 * a1) * T2) / (2.0 * T3 * T)
    c5 = (12.0 * d - 6.0 * (v1 + v0) * T + (a1 - a0) * T2) / (2.0 * T3 * T2)
    return (p0, v0, a0 / 2.0, c3, c4, c5)


def poly_eval(c: Coeffs, t: float) -> tuple[float, float, float]:
    """Position, velocity and acceleration of the quintic at time ``t``."""
    c0, c1, c2, c3, c4, c5 = c
    p = c0 + t * (c1 + t * (c2 + t * (c3 + t * (c4 + t * c5))))
    v = c1 + t * (2.0 * c2 + t * (3.0 * c3 + t * (4.0 * c4 + t * 5.0 * c5)))
    a = 2.0 * c2 + t * (6.0 * c3 + t * (12.0 * c4 + t * 20.0 * c5))
    return p, v, a


def poly_jerk(c: Coeffs, t: float) -> float:
    return 6.0 * c[3] + t * (24.0 * c[4] + t * 60.0 * c[5])


def poly_snap(c: Coeffs, t: float) -> float:
    return 24.0 * c[4] + 120.0 * c[5] * t


def jerk_cost(c: Coeffs, T: float) -> float:
    """Integral of squared jerk over ``[0, T]``."""
    a, b, d = 6.0 * c[3], 24.0 * c[4], 60.0 * c[5]
    T2 = T * T
    return (
        a * a * T
        + a * b * T2
        + (b * b + 2.0 * a * d) * T2 * T / 3.0
        + b * d * T2 * T2 / 2.0
        + d * d * T2 * T2 * T / 5.0
    )


def s_curve(tau: float) -> float:
    """Normalised minimum-jerk position profile on ``[0, 1]``."""
    tau = min(max(tau, 0.0), 1.0)
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


def s_curve_inverse(s: float) -> float:
    """Phase at which the normalised profile reaches fraction ``s``."""
    s = min(max(s, 0.0), 1.0)
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if s_curve(mid) < s:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


PEAK_SPEED_FACTOR = 1.875
"""Peak speed of a rest-to-rest minimum-jerk reach is ``1.875 * distance / T``."""

PEAK_ACCEL_FACTOR = 10.0 / math.sqrt(3.0)
"""Peak acceleration is ``5.7735 * distance / T**2``."""


@dataclass(frozen=True, slots=True)
class Segment:
    cx: Coeffs
    cy: Coeffs
    T: float


@dataclass(frozen=True, slots=True)
class Trajectory:
    """Piecewise-quintic plan, C2-continuous across segments."""

    segments: tuple[Segment, ...]
    end_pos: Vec2
    end_vel: Vec2

    @property
    def duration(self) -> float:
        return sum(s.T for s in self.segments)

    def sample(self, t: float) -> tuple[Vec2, Vec2, Vec2]:
        """State at time ``t``.

        Past the end the plan coasts at its terminal velocity, which keeps a
        cursor glued to a target that is itself moving.
        """
        if t <= 0.0:
            s = self.segments[0]
            px, vx, ax = poly_eval(s.cx, 0.0)
            py, vy, ay = poly_eval(s.cy, 0.0)
            return Vec2(px, py), Vec2(vx, vy), Vec2(ax, ay)
        for s in self.segments:
            if t <= s.T:
                px, vx, ax = poly_eval(s.cx, t)
                py, vy, ay = poly_eval(s.cy, t)
                return Vec2(px, py), Vec2(vx, vy), Vec2(ax, ay)
            t -= s.T
        return self.end_pos + self.end_vel * t, self.end_vel, Vec2(0.0, 0.0)

    def cost(self) -> float:
        return sum(jerk_cost(s.cx, s.T) + jerk_cost(s.cy, s.T) for s in self.segments)


def _solve(A: list[list[float]], rhs: list[list[float]]) -> list[list[float]]:
    """Gaussian elimination with partial pivoting; one solution per rhs column."""
    n = len(A)
    m = len(rhs)
    M = [row[:] + [rhs[k][i] for k in range(m)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-18:
            raise ArithmeticError("singular via-point system")
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        inv = 1.0 / M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] * inv
            if f != 0.0:
                row_r, row_c = M[r], M[col]
                for c in range(col, n + m):
                    row_r[c] -= f * row_c[c]
    out = [[0.0] * n for _ in range(m)]
    for k in range(m):
        for i in range(n - 1, -1, -1):
            acc = M[i][n + k]
            for j in range(i + 1, n):
                acc -= M[i][j] * out[k][j]
            out[k][i] = acc / M[i][i]
    return out


def _knot_residual(
    pts: Sequence[float],
    vels: Sequence[float],
    accs: Sequence[float],
    durations: Sequence[float],
) -> list[float]:
    """Jerk and snap discontinuities at each interior knot, for one axis."""
    polys = [
        quintic(pts[i], vels[i], accs[i], pts[i + 1], vels[i + 1], accs[i + 1], durations[i])
        for i in range(len(durations))
    ]
    out: list[float] = []
    for i in range(len(polys) - 1):
        T = durations[i]
        out.append(poly_jerk(polys[i], T) - poly_jerk(polys[i + 1], 0.0))
        out.append(poly_snap(polys[i], T) - poly_snap(polys[i + 1], 0.0))
    return out


def plan(
    p0: Vec2,
    v0: Vec2,
    a0: Vec2,
    pf: Vec2,
    vf: Vec2,
    durations: Sequence[float],
    via: Sequence[Vec2] = (),
) -> Trajectory:
    """Minimum-jerk plan from state ``(p0, v0, a0)`` to ``(pf, vf, 0)``.

    ``via`` lists positions to pass through and ``durations`` the time spent on
    each of the ``len(via) + 1`` segments.
    """
    k = len(via)
    if len(durations) != k + 1:
        raise ValueError("need one duration per segment")
    if any(T <= 0.0 for T in durations):
        raise ValueError("segment durations must be positive")

    if k == 0:
        T = durations[0]
        seg = Segment(
            quintic(p0.x, v0.x, a0.x, pf.x, vf.x, 0.0, T),
            quintic(p0.y, v0.y, a0.y, pf.y, vf.y, 0.0, T),
            T,
        )
        return Trajectory((seg,), pf, vf)

    xs = [p0.x, *(w.x for w in via), pf.x]
    ys = [p0.y, *(w.y for w in via), pf.y]
    n = 2 * k

    def residual(pts: Sequence[float], start: tuple[float, float], end_v: float, u: Sequence[float]) -> list[float]:
        vels = [start[0], *(u[2 * i] for i in range(k)), end_v]
        accs = [start[1], *(u[2 * i + 1] for i in range(k)), 0.0]
        return _knot_residual(pts, vels, accs, durations)

    # The residual is affine in the unknown knot velocities and accelerations:
    # r(u) = A u + b. A depends only on the timing, so both axes share it.
    zero_pts = [0.0] * (k + 2)
    zero_u = [0.0] * n
    cols = []
    for j in range(n):
        e = [0.0] * n
        e[j] = 1.0
        cols.append(residual(zero_pts, (0.0, 0.0), 0.0, e))
    A = [[cols[j][i] for j in range(n)] for i in range(n)]
    bx = residual(xs, (v0.x, a0.x), vf.x, zero_u)
    by = residual(ys, (v0.y, a0.y), vf.y, zero_u)
    ux, uy = _solve(A, [[-b for b in bx], [-b for b in by]])

    vxs = [v0.x, *(ux[2 * i] for i in range(k)), vf.x]
    axs = [a0.x, *(ux[2 * i + 1] for i in range(k)), 0.0]
    vys = [v0.y, *(uy[2 * i] for i in range(k)), vf.y]
    ays = [a0.y, *(uy[2 * i + 1] for i in range(k)), 0.0]
    segs = tuple(
        Segment(
            quintic(xs[i], vxs[i], axs[i], xs[i + 1], vxs[i + 1], axs[i + 1], durations[i]),
            quintic(ys[i], vys[i], ays[i], ys[i + 1], vys[i + 1], ays[i + 1], durations[i]),
            durations[i],
        )
        for i in range(k + 1)
    )
    return Trajectory(segs, pf, vf)


def allocate_times(lengths: Sequence[float], total: float) -> list[float]:
    """Split ``total`` seconds across path segments of the given lengths.

    Knots are placed where a straight rest-to-rest reach of the same total
    length would be at that fraction of the distance, so the speed along a
    curved path keeps roughly the same bell shape as along a straight one.
    """
    L = sum(lengths)
    if L <= 0.0 or len(lengths) <= 1:
        return [total / max(1, len(lengths))] * max(1, len(lengths))
    out: list[float] = []
    acc, prev = 0.0, 0.0
    for length in lengths[:-1]:
        acc += length
        t = s_curve_inverse(acc / L) * total
        out.append(t - prev)
        prev = t
    out.append(total - prev)
    floor = total * 0.02
    out = [max(d, floor) for d in out]
    scale = total / sum(out)
    return [d * scale for d in out]


def optimise_times(
    p0: Vec2,
    v0: Vec2,
    a0: Vec2,
    pf: Vec2,
    vf: Vec2,
    via: Sequence[Vec2],
    durations: Sequence[float],
    sweeps: int = 2,
    iterations: int = 12,
) -> list[float]:
    """Shift knot times, keeping the total fixed, to lower integrated jerk.

    Coordinate descent with a golden-section search per knot. It matters most
    when re-planning from a moving state, where the rest-to-rest allocation
    heuristic is a poor guess for the first segment.
    """
    d = list(durations)
    if len(d) < 2:
        return d
    total = sum(d)
    floor = total * 0.02
    gr = (math.sqrt(5.0) - 1.0) / 2.0

    def cost(ds: Sequence[float]) -> float:
        return plan(p0, v0, a0, pf, vf, ds, via).cost()

    for _ in range(sweeps):
        for i in range(len(d) - 1):
            pair = d[i] + d[i + 1]
            lo, hi = floor, pair - floor
            if hi <= lo:
                continue

            def f(x: float, i: int = i, pair: float = pair) -> float:
                trial = d[:]
                trial[i], trial[i + 1] = x, pair - x
                return cost(trial)

            x1 = hi - gr * (hi - lo)
            x2 = lo + gr * (hi - lo)
            f1, f2 = f(x1), f(x2)
            for _ in range(iterations):
                if f1 < f2:
                    hi, x2, f2 = x2, x1, f1
                    x1 = hi - gr * (hi - lo)
                    f1 = f(x1)
                else:
                    lo, x1, f1 = x1, x2, f2
                    x2 = lo + gr * (hi - lo)
                    f2 = f(x2)
            best = 0.5 * (lo + hi)
            if f(best) < cost(d):
                d[i], d[i + 1] = best, pair - best
    return d
