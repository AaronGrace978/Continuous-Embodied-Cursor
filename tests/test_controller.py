# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import pytest

from embodied_cursor.geometry import ZERO, Rect, Vec2
from embodied_cursor.motor.controller import MotionController, MotionGoal, MotorConfig
from embodied_cursor.motor.fitts import FittsLaw, kinematic_floor
from embodied_cursor.motor.routing import route

DT = 1 / 120


def run(ctrl, goal_fn, seconds, dt=DT):
    """Step the controller, returning the (t, pos, vel, acc) samples."""
    out = []
    t = 0.0
    for _ in range(int(round(seconds / dt))):
        goal = goal_fn(t)
        if goal is not None:
            ctrl.set_goal(goal)
        ctrl.step(dt)
        t += dt
        out.append((t, ctrl.pos, ctrl.vel, ctrl.acc))
    return out


def test_fitts_time_grows_with_distance_and_shrinks_with_width():
    law = FittsLaw()
    assert law.movement_time(800, 10) > law.movement_time(800, 80)
    assert law.movement_time(800, 20) > law.movement_time(100, 20)
    assert law.movement_time(0, 20) == pytest.approx(law.a)


def test_reach_is_straight_smooth_and_takes_fitts_time():
    ctrl = MotionController(Vec2(100, 100))
    goal = MotionGoal(Vec2(900, 500), width=24)
    expected = ctrl.config.fitts.movement_time(Vec2(100, 100).dist(goal.pos), 24)
    samples = run(ctrl, lambda t: goal, expected + 0.1)

    assert ctrl.pos.dist(goal.pos) < 1e-6
    assert ctrl.speed < 1e-6
    arrival = next(t for t, p, _, _ in samples if p.dist(goal.pos) < 0.5)
    assert arrival == pytest.approx(expected, abs=0.08)

    direction = (goal.pos - Vec2(100, 100)).unit()
    speeds = []
    for _, p, v, _ in samples:
        assert abs((p - Vec2(100, 100)).cross(direction)) < 1e-6
        speeds.append(v.norm())
    peak = speeds.index(max(speeds))
    assert all(speeds[i] <= speeds[i + 1] + 1e-6 for i in range(peak))
    assert all(speeds[i] >= speeds[i + 1] - 1e-6 for i in range(peak, len(speeds) - 1))
    assert ctrl.replans == 1


def test_small_targets_are_approached_more_slowly():
    def time_to(width):
        ctrl = MotionController(ZERO)
        goal = MotionGoal(Vec2(700, 0), width=width)
        samples = run(ctrl, lambda t: goal, 2.0)
        return next(t for t, p, _, _ in samples if p.dist(goal.pos) < 0.5)

    assert time_to(6) > time_to(40) > time_to(200)


def test_target_jump_bends_the_path_without_stopping():
    ctrl = MotionController(ZERO)
    first, second = Vec2(800, 0), Vec2(700, 260)

    def goal(t):
        return MotionGoal(first if t < 0.18 else second, width=30)

    samples = run(ctrl, goal, 1.5)
    assert ctrl.pos.dist(second) < 1e-3
    assert ctrl.speed < 1e-3

    # Velocity is continuous: no tick changes it by more than the plan's own
    # acceleration could.
    for (_, _, v0, a0), (_, _, v1, a1) in zip(samples, samples[1:]):
        bound = (max(a0.norm(), a1.norm()) * 1.5 + 2000.0) * DT
        assert (v1 - v0).norm() <= bound

    arrival = next(i for i, (_, p, _, _) in enumerate(samples) if p.dist(second) < 1.0)
    jump = int(0.18 / DT)
    assert min(v.norm() for _, _, v, _ in samples[jump:arrival - 12]) > 150.0
    assert ctrl.replans == 2


def test_small_shift_near_the_end_is_corrected_quickly():
    ctrl = MotionController(ZERO)
    goal = MotionGoal(Vec2(400, 0), width=40)
    run(ctrl, lambda t: goal, 1.0)
    assert ctrl.remaining == 0.0

    nudged = MotionGoal(Vec2(404, 3), width=40)
    samples = run(ctrl, lambda t: nudged, 0.2)
    assert ctrl.pos.dist(nudged.pos) < 1e-3
    settle = next(t for t, p, _, _ in samples if p.dist(nudged.pos) < 0.25)
    assert settle <= ctrl.config.tau_min + DT


def test_tracks_a_target_moving_at_constant_velocity():
    ctrl = MotionController(ZERO)
    vel = Vec2(180, -60)
    start = Vec2(500, 300)

    def goal(t):
        return MotionGoal(start + vel * t, width=30, vel=vel)

    samples = run(ctrl, goal, 2.0)
    t_end = samples[-1][0]
    assert ctrl.pos.dist(start + vel * t_end) < 0.5
    assert (ctrl.vel - vel).norm() < 2.0
    # ...and it stayed locked on for the whole last half second.
    for t, p, _, _ in samples[-60:]:
        assert p.dist(start + vel * t) < 0.5


def test_follows_a_target_with_unknown_velocity_with_bounded_lag():
    ctrl = MotionController(ZERO)
    vel = Vec2(200, 0)

    def goal(t):
        return MotionGoal(Vec2(300, 100) + vel * t, width=30)

    samples = run(ctrl, goal, 2.0)
    t_end = samples[-1][0]
    lag = ctrl.pos.dist(Vec2(300, 100) + vel * t_end)
    assert lag < vel.norm() * ctrl.config.tau_min * 1.5


def test_speed_and_acceleration_caps_stretch_the_reach():
    cfg = MotorConfig(v_max=1500.0, a_max=20000.0)
    ctrl = MotionController(ZERO, cfg)
    goal = MotionGoal(Vec2(1800, 0), width=40)
    samples = run(ctrl, lambda t: goal, 4.0)
    assert ctrl.pos.dist(goal.pos) < 1e-3
    assert max(v.norm() for _, _, v, _ in samples) <= 1500.0 * 1.001
    assert max(a.norm() for _, _, _, a in samples) <= 20000.0 * 1.001
    assert kinematic_floor(1800, 1500, 20000) > cfg.fitts.movement_time(1800, 40)


def test_explicit_duration_overrides_the_law():
    ctrl = MotionController(ZERO)
    goal = MotionGoal(Vec2(300, 0), width=20, duration=1.5)
    samples = run(ctrl, lambda t: goal, 2.0)
    arrival = next(t for t, p, _, _ in samples if p.dist(goal.pos) < 0.5)
    assert 1.2 < arrival <= 1.5 + DT


def test_curves_around_an_obstacle():
    ctrl = MotionController(Vec2(100, 300))
    wall = Rect(400, 150, 80, 300)
    goal = MotionGoal(Vec2(800, 300), width=30, obstacles=(wall,))
    samples = run(ctrl, lambda t: goal, 2.0)

    assert ctrl.pos.dist(goal.pos) < 1e-3
    assert len(ctrl.via) >= 1
    assert not ctrl.route_blocked
    for _, p, _, _ in samples:
        assert not wall.contains(p)
    # It flowed past the corner rather than stopping at it.
    arrival = next(i for i, (_, p, _, _) in enumerate(samples) if p.dist(goal.pos) < 1.0)
    assert min(v.norm() for _, _, v, _ in samples[10:arrival - 12]) > 100.0
    ys = [p.y for _, p, _, _ in samples]
    assert max(ys) > 450 or min(ys) < 150


def test_obstacle_appearing_mid_flight_reroutes_smoothly():
    ctrl = MotionController(Vec2(100, 300))
    wall = Rect(500, 100, 60, 400)
    target = Vec2(900, 300)

    def goal(t):
        return MotionGoal(target, width=30, obstacles=(wall,) if t > 0.1 else ())

    samples = run(ctrl, goal, 2.5)
    assert ctrl.pos.dist(target) < 1e-3
    for t, p, _, _ in samples:
        if t > 0.1:
            assert not wall.contains(p)
    for (_, _, v0, a0), (_, _, v1, a1) in zip(samples, samples[1:]):
        assert (v1 - v0).norm() <= (max(a0.norm(), a1.norm()) * 1.5 + 2000.0) * DT


def test_bounds_keep_the_cursor_on_the_desktop():
    screen = Rect(0, 0, 1000, 800)
    ctrl = MotionController(Vec2(50, 400), bounds=[screen])
    wall = Rect(300, -100, 60, 700)
    goal = MotionGoal(Vec2(900, 100), width=30, obstacles=(wall,))
    samples = run(ctrl, lambda t: goal, 2.5)
    assert ctrl.pos.dist(goal.pos) < 1e-3
    for _, p, _, _ in samples:
        assert screen.contains(p)
        assert not wall.contains(p)
    # The only way round is underneath, since the top is off-screen.
    assert max(p.y for _, p, _, _ in samples) > 600


def test_brake_comes_to_rest_monotonically():
    ctrl = MotionController(ZERO)
    goal = MotionGoal(Vec2(1000, 0), width=20)
    run(ctrl, lambda t: goal, 0.2)
    assert ctrl.speed > 500
    ctrl.brake()
    samples = run(ctrl, lambda t: None, 0.5)
    speeds = [v.norm() for _, _, v, _ in samples]
    assert all(a >= b - 1e-6 for a, b in zip(speeds, speeds[1:]))
    assert ctrl.speed == 0.0
    assert not ctrl.moving
    assert 0 < ctrl.pos.x < 1000


def test_tick_rate_does_not_change_the_path():
    def final_and_mid(dt):
        ctrl = MotionController(ZERO)
        goal = MotionGoal(Vec2(640, 480), width=20)
        samples = run(ctrl, lambda t: goal, 0.3, dt)
        return samples[-1]

    t_a, p_a, _, _ = final_and_mid(1 / 30)
    t_b, p_b, _, _ = final_and_mid(1 / 240)
    assert t_a == pytest.approx(t_b, abs=1e-9)
    assert p_a.dist(p_b) < 1e-6


def test_reset_adopts_a_new_position_at_rest():
    ctrl = MotionController(ZERO)
    run(ctrl, lambda t: MotionGoal(Vec2(500, 0)), 0.1)
    ctrl.reset(Vec2(42, 42))
    assert ctrl.pos == Vec2(42, 42)
    assert ctrl.goal is None
    assert ctrl.step(DT) == Vec2(42, 42)


# -- routing ---------------------------------------------------------------


def test_route_is_direct_when_nothing_is_in_the_way():
    r = route(ZERO, Vec2(100, 0), [Rect(40, 50, 20, 20)])
    assert r.via == () and not r.blocked


def test_route_takes_the_shorter_side():
    wall = Rect(40, -10, 20, 110)
    r = route(Vec2(0, 0), Vec2(100, 0), [wall], margin=5)
    assert len(r.via) == 2
    assert all(p.y < -10 for p in r.via)


def test_route_reports_when_enclosed():
    box = [Rect(-50, -60, 100, 20), Rect(-50, 40, 100, 20), Rect(-60, -60, 20, 120), Rect(40, -60, 20, 120)]
    r = route(ZERO, Vec2(300, 0), box, margin=2)
    assert r.blocked and r.via == ()


def test_route_ignores_obstacles_under_the_endpoints():
    r = route(Vec2(10, 10), Vec2(200, 10), [Rect(0, 0, 50, 50), Rect(180, 0, 50, 50)])
    assert r.via == () and not r.blocked


def test_route_shrinks_margin_next_to_an_endpoint():
    # The goal is 4px from the obstacle: a 10px keep-out would swallow it.
    wall = Rect(100, -100, 20, 200)
    r = route(Vec2(0, 0), Vec2(124, 0), [wall], margin=10)
    assert not r.blocked and len(r.via) >= 1


def test_route_threads_between_several_obstacles():
    obstacles = [Rect(100, -200, 30, 380), Rect(250, -180, 30, 400), Rect(400, -200, 30, 380)]
    start, goal = Vec2(0, 0), Vec2(550, 0)
    r = route(start, goal, obstacles, margin=8)
    assert not r.blocked
    pts = [start, *r.via, goal]
    for a, b in zip(pts, pts[1:]):
        assert not any(o.segment_hits(a, b) for o in obstacles)
    length = sum(a.dist(b) for a, b in zip(pts, pts[1:]))
    assert length < 1400
    assert math.isfinite(length)
