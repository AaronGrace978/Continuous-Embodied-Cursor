# SPDX-License-Identifier: AGPL-3.0-or-later
import math
import random

import pytest

from embodied_cursor.geometry import ZERO, Vec2
from embodied_cursor.motor import minjerk


def test_quintic_meets_all_six_boundary_values():
    rng = random.Random(1)
    for _ in range(50):
        p0, v0, a0, p1, v1, a1 = (rng.uniform(-500, 500) for _ in range(6))
        T = rng.uniform(0.05, 2.0)
        c = minjerk.quintic(p0, v0, a0, p1, v1, a1, T)
        assert minjerk.poly_eval(c, 0.0) == pytest.approx((p0, v0, a0), abs=1e-6)
        assert minjerk.poly_eval(c, T) == pytest.approx((p1, v1, a1), abs=1e-5)


def test_rest_to_rest_matches_the_closed_form_profile():
    T, d = 0.8, 600.0
    traj = minjerk.plan(ZERO, ZERO, ZERO, Vec2(d, 0), ZERO, [T])
    for i in range(21):
        tau = i / 20
        p, v, _ = traj.sample(tau * T)
        assert p.x == pytest.approx(d * minjerk.s_curve(tau), abs=1e-6)
        assert p.y == pytest.approx(0.0, abs=1e-9)
        assert v.x >= -1e-9
    _, v_mid, _ = traj.sample(T / 2)
    assert v_mid.x == pytest.approx(minjerk.PEAK_SPEED_FACTOR * d / T)


def test_peak_acceleration_factor():
    T, d = 1.0, 1.0
    traj = minjerk.plan(ZERO, ZERO, ZERO, Vec2(d, 0), ZERO, [T])
    peak = max(abs(traj.sample(i / 2000 * T)[2].x) for i in range(2001))
    assert peak == pytest.approx(minjerk.PEAK_ACCEL_FACTOR, rel=1e-4)


def test_remainder_of_a_plan_is_the_plan_from_that_state():
    """Re-planning from a sampled state with the time left changes nothing."""
    T = 0.6
    goal = Vec2(420.0, -130.0)
    traj = minjerk.plan(Vec2(5, 7), Vec2(30, -20), Vec2(100, 50), goal, ZERO, [T])
    t0 = 0.23
    p, v, a = traj.sample(t0)
    rest = minjerk.plan(p, v, a, goal, ZERO, [T - t0])
    for i in range(11):
        dt = (T - t0) * i / 10
        expect = traj.sample(t0 + dt)
        got = rest.sample(dt)
        for e, g in zip(expect, got):
            assert g.x == pytest.approx(e.x, abs=1e-5)
            assert g.y == pytest.approx(e.y, abs=1e-5)


def test_receding_horizon_tracking_is_a_contraction_at_any_tick_rate():
    """Holding the horizon fixed and stepping dt must converge, not ring up."""
    tau = 0.05
    for dt in (0.001, 0.004, 1 / 120, 1 / 60, 1 / 30, 0.049):
        p, v, a = Vec2(100.0, -40.0), Vec2(900.0, 300.0), Vec2(-2000.0, 5000.0)
        for _ in range(int(1.0 / dt)):
            traj = minjerk.plan(p, v, a, ZERO, ZERO, [tau])
            p, v, a = traj.sample(dt)
        assert p.norm() < 1e-3, dt
        assert v.norm() < 1e-1, dt


def test_via_points_are_hit_with_continuous_jerk_and_snap():
    via = [Vec2(200, 150), Vec2(400, 120)]
    durs = [0.3, 0.25, 0.35]
    traj = minjerk.plan(ZERO, ZERO, ZERO, Vec2(600, 0), ZERO, durs, via)
    assert traj.duration == pytest.approx(0.9)

    t = 0.0
    for i, w in enumerate(via):
        t += durs[i]
        p, _, _ = traj.sample(t)
        assert p.dist(w) < 1e-6
        before, after = traj.segments[i], traj.segments[i + 1]
        for cb, ca in ((before.cx, after.cx), (before.cy, after.cy)):
            scale = 1.0 + abs(minjerk.poly_jerk(ca, 0.0))
            assert abs(minjerk.poly_jerk(cb, before.T) - minjerk.poly_jerk(ca, 0.0)) / scale < 1e-6
            scale = 1.0 + abs(minjerk.poly_snap(ca, 0.0))
            assert abs(minjerk.poly_snap(cb, before.T) - minjerk.poly_snap(ca, 0.0)) / scale < 1e-6
            # Velocity and acceleration are continuous by construction.
            assert minjerk.poly_eval(cb, before.T)[1:] == pytest.approx(minjerk.poly_eval(ca, 0.0)[1:], abs=1e-5)

    p_end, v_end, a_end = traj.sample(traj.duration)
    assert p_end.dist(Vec2(600, 0)) < 1e-6
    assert v_end.norm() < 1e-5
    assert a_end.norm() < 1e-3


def test_via_solution_is_the_jerk_minimiser():
    """Any other knot velocity/acceleration costs more."""
    via = [Vec2(300, 200)]
    durs = [0.4, 0.5]
    best = minjerk.plan(ZERO, ZERO, ZERO, Vec2(600, 0), ZERO, durs, via)
    p, v, a = best.sample(durs[0])
    rng = random.Random(3)
    for _ in range(40):
        dv = Vec2(rng.uniform(-50, 50), rng.uniform(-50, 50))
        da = Vec2(rng.uniform(-500, 500), rng.uniform(-500, 500))
        first = minjerk.Segment(
            minjerk.quintic(0, 0, 0, p.x, v.x + dv.x, a.x + da.x, durs[0]),
            minjerk.quintic(0, 0, 0, p.y, v.y + dv.y, a.y + da.y, durs[0]),
            durs[0],
        )
        second = minjerk.Segment(
            minjerk.quintic(p.x, v.x + dv.x, a.x + da.x, 600, 0, 0, durs[1]),
            minjerk.quintic(p.y, v.y + dv.y, a.y + da.y, 0, 0, 0, durs[1]),
            durs[1],
        )
        other = minjerk.Trajectory((first, second), Vec2(600, 0), ZERO)
        assert other.cost() >= best.cost() * (1 - 1e-9)


def test_jerk_cost_matches_numeric_integration():
    c = minjerk.quintic(0, 10, -30, 250, -5, 12, 0.7)
    n = 20000
    h = 0.7 / n
    numeric = sum(minjerk.poly_jerk(c, (i + 0.5) * h) ** 2 for i in range(n)) * h
    assert minjerk.jerk_cost(c, 0.7) == pytest.approx(numeric, rel=1e-6)


def test_s_curve_inverse_round_trips():
    for i in range(101):
        s = i / 100
        assert minjerk.s_curve(minjerk.s_curve_inverse(s)) == pytest.approx(s, abs=1e-9)


def test_time_allocation_sums_and_orders():
    d = minjerk.allocate_times([100, 300, 100], 1.0)
    assert sum(d) == pytest.approx(1.0)
    assert all(x > 0 for x in d)
    # Equal-length end legs include the acceleration and braking, so they take
    # longer per pixel than the cruise through the middle.
    assert d[0] / 100 > d[1] / 300
    assert d[0] == pytest.approx(d[2], rel=1e-6)


def test_optimised_times_never_cost_more():
    via = [Vec2(250, 180)]
    start_v = Vec2(900, 0)
    durs = minjerk.allocate_times([300, 320], 0.7)
    base = minjerk.plan(ZERO, start_v, ZERO, Vec2(500, 0), ZERO, durs, via).cost()
    better = minjerk.optimise_times(ZERO, start_v, ZERO, Vec2(500, 0), ZERO, via, durs)
    assert sum(better) == pytest.approx(0.7)
    assert minjerk.plan(ZERO, start_v, ZERO, Vec2(500, 0), ZERO, better, via).cost() <= base * (1 + 1e-9)


def test_coasts_at_terminal_velocity_past_the_end():
    vf = Vec2(120, -40)
    traj = minjerk.plan(ZERO, ZERO, ZERO, Vec2(300, 0), vf, [0.5])
    p, v, a = traj.sample(0.75)
    assert p.dist(Vec2(300, 0) + vf * 0.25) < 1e-6
    assert v == vf
    assert a.norm() == 0


def test_rejects_bad_timing():
    with pytest.raises(ValueError):
        minjerk.plan(ZERO, ZERO, ZERO, Vec2(1, 0), ZERO, [0.0])
    with pytest.raises(ValueError):
        minjerk.plan(ZERO, ZERO, ZERO, Vec2(1, 0), ZERO, [0.1], [Vec2(0.5, 0)])
    assert math.isfinite(minjerk.plan(ZERO, ZERO, ZERO, ZERO, ZERO, [0.1]).cost())
