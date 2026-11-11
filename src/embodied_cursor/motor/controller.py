# SPDX-License-Identifier: GPL-3.0-or-later
"""Continuous-time motion controller for the pointer.

The controller owns the cursor's kinematic state ``(position, velocity,
acceleration)`` and advances it along a minimum-jerk plan. It is fed a
:class:`MotionGoal` every tick by whichever skill is running. When the goal is
unchanged the plan is simply sampled; when the goal moves, the plan is rebuilt
from the current state, so the path bends without a discontinuity in velocity
or acceleration.

Nothing here touches the operating system. The body feeds the returned
position to an actuator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..geometry import ZERO, Rect, Vec2, clamp_to_any, polyline_length
from . import minjerk
from .fitts import FittsLaw, kinematic_floor
from .routing import route


@dataclass(frozen=True, slots=True)
class MotorConfig:
    fitts: FittsLaw = field(default_factory=FittsLaw)

    v_max: float = 9000.0
    """Speed cap in px/s. Reaches are stretched in time to respect it."""

    a_max: float = 150000.0
    """Acceleration cap in px/s^2."""

    tau_min: float = 0.05
    """Shortest planning horizon in seconds.

    Once a reach has less than this left, the controller keeps re-planning
    over this horizon, which turns it into a stable tracking servo for targets
    that keep moving.
    """

    jump_min: float = 3.0
    """A goal shift of at least this many pixels can start a new submovement."""

    jump_fraction: float = 0.25
    """...provided it is also at least this fraction of the target width."""

    predict_horizon: float = 0.12
    """How far ahead, in seconds, a moving target's velocity is extrapolated."""

    acc_memory: float = 0.3
    """Plans longer than this many seconds progressively forget the current acceleration."""

    route_margin: float = 10.0
    """Clearance in pixels kept from obstacles."""

    tempo: float = 1.0
    """Global duration multiplier. Above 1 is slower and gentler."""


@dataclass(frozen=True, slots=True)
class MotionGoal:
    pos: Vec2
    """Where the target is right now."""

    width: float = 16.0
    """Size of the target along its tighter axis, for the movement-time law."""

    vel: Vec2 = ZERO
    """Estimated target velocity in px/s."""

    obstacles: tuple[Rect, ...] = ()
    """Regions the pointer must not pass over on the way."""

    duration: float | None = None
    """Explicit reach duration, overriding the movement-time law."""


class MotionController:
    def __init__(
        self,
        pos: Vec2,
        config: MotorConfig | None = None,
        bounds: Sequence[Rect] = (),
    ) -> None:
        self.config = config or MotorConfig()
        self.bounds: tuple[Rect, ...] = tuple(bounds)
        self.pos = pos
        self.vel = ZERO
        self.acc = ZERO
        self._traj: minjerk.Trajectory | None = None
        self._t = 0.0
        self._goal: MotionGoal | None = None
        self._since_goal = 0.0
        self._via: tuple[Vec2, ...] = ()
        self._durations: list[float] = []
        self.replans = 0
        self.route_blocked = False

    # -- state -----------------------------------------------------------

    @property
    def goal(self) -> MotionGoal | None:
        return self._goal

    @property
    def via(self) -> tuple[Vec2, ...]:
        """Corners the current plan is threaded through."""
        return self._via

    @property
    def remaining(self) -> float:
        """Seconds left in the current plan."""
        if self._traj is None:
            return 0.0
        return max(0.0, self._traj.duration - self._t)

    @property
    def speed(self) -> float:
        return self.vel.norm()

    @property
    def moving(self) -> bool:
        return self._traj is not None and (self.remaining > 0.0 or self.speed > 1e-6)

    def reset(self, pos: Vec2) -> None:
        """Adopt ``pos`` as the cursor position, at rest, with no plan."""
        self.pos = pos
        self.vel = ZERO
        self.acc = ZERO
        self._traj = None
        self._goal = None
        self._via = ()
        self._durations = []
        self._t = 0.0

    # -- goals -----------------------------------------------------------

    def set_goal(self, goal: MotionGoal) -> None:
        """Aim at ``goal``. Cheap to call every tick with the latest estimate."""
        old = self._goal
        if old is None or self._traj is None:
            self._replan(goal, remaining=0.0, fresh=True, jump=False)
            return

        expected = old.pos + old.vel * self._since_goal
        shift = goal.pos.dist(expected)
        tracking = goal.vel.norm() > 1e-6 or old.vel.norm() > 1e-6
        if (
            shift < 0.01
            and not tracking
            and goal.obstacles == old.obstacles
            and goal.duration == old.duration
        ):
            return

        cfg = self.config
        jump = shift > max(cfg.jump_min, cfg.jump_fraction * goal.width)
        self._replan(goal, remaining=self.remaining, fresh=False, jump=jump)

    def brake(self) -> None:
        """Come to rest smoothly from the current state, abandoning the goal."""
        speed = self.speed
        self._goal = None
        self._via = ()
        if speed < 1.0 and self.acc.norm() < 1.0:
            self.vel = ZERO
            self.acc = ZERO
            self._traj = None
            self._t = 0.0
            return
        T = max(self.config.tau_min, 2.0 * speed / self.config.a_max, 0.08)
        stop = self.pos + self.vel * (T / 2.0)
        self._durations = [T]
        self._traj = minjerk.plan(self.pos, self.vel, self.acc, stop, ZERO, self._durations)
        self._t = 0.0

    def _replan(self, goal: MotionGoal, remaining: float, fresh: bool, jump: bool) -> None:
        cfg = self.config
        self.replans += 1

        r = route(self.pos, goal.pos, goal.obstacles, cfg.route_margin, self.bounds) if goal.obstacles else None
        via = r.via if r else ()
        self.route_blocked = bool(r and r.blocked)
        length = polyline_length([self.pos, *via, goal.pos])

        if fresh or jump:
            if goal.duration is not None:
                D = goal.duration
            else:
                D = cfg.fitts.movement_time(length, goal.width) * cfg.tempo
            if not fresh:
                D = max(D, remaining)
        else:
            D = remaining
        if not via and length > 1e-6:
            # Already closing on the goal faster than the plan allows for: a
            # longer duration would carry the cursor past it and back. A
            # smooth brake from speed v over distance d takes 2d/v.
            closing = (self.vel - goal.vel).dot(goal.pos - self.pos) / length
            if closing > 0.0:
                D = min(D, 2.0 * length / closing)
        D = max(D, cfg.tau_min, kinematic_floor(length, cfg.v_max, cfg.a_max))

        # Carrying the current acceleration into a long, slow plan makes the
        # minimum-jerk solution unwind it over the whole duration, which shows
        # up as a visible swing the wrong way. Over long horizons it is let go
        # instead; position and velocity stay continuous regardless.
        if D > cfg.acc_memory:
            self.acc = self.acc * (cfg.acc_memory / D) ** 2

        lead = min(D, cfg.predict_horizon)
        aim = goal.pos + goal.vel * lead
        if self.bounds:
            aim = clamp_to_any(aim, self.bounds)

        if via:
            durations = self._via_timing(via, aim, goal.vel, D, reuse=not (fresh or jump))
            traj = minjerk.plan(self.pos, self.vel, self.acc, aim, goal.vel, durations, via)
            traj, via, durations = self._ensure_clear(traj, via, durations, aim, goal)
        else:
            durations = [D]
            traj = minjerk.plan(self.pos, self.vel, self.acc, aim, goal.vel, durations)

        self._traj = traj
        self._via = tuple(via)
        self._durations = list(durations)
        self._t = 0.0
        self._goal = goal
        self._since_goal = 0.0

    def _via_timing(
        self, via: Sequence[Vec2], aim: Vec2, vf: Vec2, D: float, reuse: bool
    ) -> list[float]:
        prev = self._durations
        if reuse and len(prev) == len(via) + 1 and len(self._via) == len(via):
            # Same route as last tick: keep the knot schedule, minus elapsed time.
            left = prev[:]
            left[0] = max(left[0] - self._t, 0.02 * D)
            scale = D / sum(left)
            return [d * scale for d in left]
        pts = [self.pos, *via, aim]
        lengths = [pts[i].dist(pts[i + 1]) for i in range(len(pts) - 1)]
        durations = minjerk.allocate_times(lengths, D)
        if len(via) <= 3:
            durations = minjerk.optimise_times(self.pos, self.vel, self.acc, aim, vf, via, durations)
        return durations

    def _ensure_clear(
        self,
        traj: minjerk.Trajectory,
        via: Sequence[Vec2],
        durations: list[float],
        aim: Vec2,
        goal: MotionGoal,
    ) -> tuple[minjerk.Trajectory, Sequence[Vec2], list[float]]:
        """Tighten a curved plan until it no longer clips an obstacle.

        A smooth curve through the route's corners can bulge into an obstacle.
        Pinning the midpoint of every leg pulls the curve towards the polyline,
        which is known to be clear.
        """
        hazards = [o for o in goal.obstacles if not o.contains(self.pos) and not o.contains(goal.pos)]
        for _ in range(2):
            if not self._clips(traj, hazards):
                return traj, via, durations
            pts = [self.pos, *via, aim]
            dense: list[Vec2] = []
            for i in range(len(pts) - 1):
                dense.append(pts[i].lerp(pts[i + 1], 0.5))
                if i + 1 < len(pts) - 1:
                    dense.append(pts[i + 1])
            via = dense
            pts = [self.pos, *via, aim]
            lengths = [pts[i].dist(pts[i + 1]) for i in range(len(pts) - 1)]
            durations = minjerk.allocate_times(lengths, sum(durations))
            traj = minjerk.plan(self.pos, self.vel, self.acc, aim, goal.vel, durations, via)
        if self._clips(traj, hazards):
            self.route_blocked = True
        return traj, via, durations

    @staticmethod
    def _clips(traj: minjerk.Trajectory, hazards: Sequence[Rect]) -> bool:
        if not hazards:
            return False
        D = traj.duration
        steps = max(12, int(D / 0.006))
        for i in range(1, steps):
            p, _, _ = traj.sample(D * i / steps)
            if any(o.contains(p) for o in hazards):
                return True
        return False

    # -- time ------------------------------------------------------------

    def step(self, dt: float) -> Vec2:
        """Advance ``dt`` seconds and return the new cursor position."""
        if self._traj is None or dt <= 0.0:
            return self.pos
        self._t += dt
        self._since_goal += dt
        p, v, a = self._traj.sample(self._t)
        if self.bounds:
            p = clamp_to_any(p, self.bounds)
        self.pos, self.vel, self.acc = p, v, a
        if self._goal is None and self._t >= self._traj.duration:
            # A brake has finished.
            self.vel = ZERO
            self.acc = ZERO
            self._traj = None
            self._t = 0.0
        return self.pos
