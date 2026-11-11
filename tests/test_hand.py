# SPDX-License-Identifier: AGPL-3.0-or-later
import math

import pytest

from embodied_cursor.geometry import Rect, Vec2
from embodied_cursor.hand import HandController, HandSample


def hand(x=0.5, y=0.5, pinch=1):
    return HandSample(x, y, pinch)


def armed(bounds=Rect(0, 0, 1920, 1080)):
    controller = HandController(bounds)
    assert controller.update(hand(), 0)[0] is None
    assert controller.update(hand(), 0.3)[0] is not None
    return controller


def test_open_hand_is_required_before_motion_or_click():
    controller = HandController(Rect(0, 0, 100, 100))
    for i in range(10):
        point, click, _ = controller.update(hand(pinch=0.1), i / 10)
        assert point is None and not click
    controller.update(hand(), 1)
    assert not controller.armed
    assert controller.update(hand(), 1.3)[0] == Vec2(49.5, 49.5)


def test_pinch_clicks_once_freezes_pointer_and_needs_release():
    controller = armed()
    start = controller.position
    assert not controller.update(hand(x=0.8, pinch=0.2), 0.4)[1]
    point, click, _ = controller.update(hand(x=0.8, pinch=0.2), 0.5)
    assert click and point == start
    assert not controller.update(hand(pinch=0.2), 1)[1]
    # Noise inside the hysteresis band cannot reset the click latch.
    assert not controller.update(hand(pinch=0.45), 1.1)[1]
    assert not controller.update(hand(pinch=0.2), 1.2)[1]
    controller.update(hand(), 1.3)
    controller.update(hand(pinch=0.2), 1.4)
    assert controller.update(hand(pinch=0.2), 1.5)[1]


def test_losing_tracking_disarms_and_clears_pending_click():
    controller = armed()
    controller.update(hand(pinch=0.2), 0.4)
    assert controller.update(None, 0.5)[0] is None
    assert not controller.armed and controller.position is None
    for stamp in (0.6, 1, 2):
        point, click, _ = controller.update(hand(pinch=0.2), stamp)
        assert point is None and not click


def test_repeated_motor_ticks_on_one_frame_cannot_confirm_a_pinch():
    controller = armed()
    for _ in range(30):
        assert not controller.update(hand(pinch=0.2), 0.4)[1]
    assert controller.update(hand(pinch=0.2), 0.5)[1]


def test_short_pinches_do_not_click_and_smoothing_is_time_based():
    controller = armed()
    controller.update(hand(pinch=0.2), 0.4)
    assert not controller.update(hand(), 0.43)[1]
    initial = controller.position
    point, _, _ = controller.update(hand(x=0.9), 0.44)
    assert initial.x < point.x < 1919
    for index in range(30):
        point, _, _ = controller.update(hand(x=0.9), 0.45 + index / 60)
    assert point.x == pytest.approx(1919, abs=1)


def test_negative_monitor_coordinates_and_edges_are_clamped():
    controller = armed(Rect(-1920, -200, 1920, 1080))
    for index in range(30):
        point, _, _ = controller.update(hand(x=1, y=0), 1 + index / 30)
    assert point.x == pytest.approx(-1, abs=0.01)
    assert point.y == pytest.approx(-200, abs=0.01)


@pytest.mark.parametrize('sample', [[], {}, {'x': True, 'y': 0.5, 'pinch': 1},
    {'x': math.nan, 'y': 0.5, 'pinch': 1}, {'x': 0.5, 'y': math.inf, 'pinch': 1},
    {'x': -1, 'y': 0.5, 'pinch': 1}, {'x': 0.5, 'y': 0.5, 'pinch': -1},
    {'x': '0.5', 'y': 0.5, 'pinch': 1}])
def test_malformed_landmarks_cannot_move_the_pointer(sample):
    with pytest.raises(ValueError):
        HandSample.parse(sample)


def test_valid_landmarks_and_absent_hand():
    assert HandSample.parse(None) is None
    assert HandSample.parse({'x': 0.5, 'y': 0.4, 'pinch': 0.2}) == hand(0.5, 0.4, 0.2)
