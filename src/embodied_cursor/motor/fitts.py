# SPDX-License-Identifier: GPL-3.0-or-later
"""Movement-time law.

Fitts's law (Shannon form, MacKenzie 1992) predicts how long a person takes to
acquire a target of width ``W`` at distance ``D``::

    MT = a + b * log2(D / W + 1)

Using it to set the duration of each reach is what makes the cursor slow down
for small or distant targets without any special-casing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .minjerk import PEAK_ACCEL_FACTOR, PEAK_SPEED_FACTOR


@dataclass(frozen=True, slots=True)
class FittsLaw:
    a: float = 0.07
    """Intercept in seconds: the cost of starting and stopping a reach."""

    b: float = 0.10
    """Slope in seconds per bit of index of difficulty."""

    min_width: float = 2.0
    """Targets narrower than this are treated as this wide."""

    def index_of_difficulty(self, distance: float, width: float) -> float:
        return math.log2(max(distance, 0.0) / max(width, self.min_width) + 1.0)

    def movement_time(self, distance: float, width: float) -> float:
        return self.a + self.b * self.index_of_difficulty(distance, width)


def kinematic_floor(distance: float, v_max: float, a_max: float) -> float:
    """Shortest rest-to-rest minimum-jerk duration within the speed and acceleration caps."""
    distance = max(distance, 0.0)
    return max(
        PEAK_SPEED_FACTOR * distance / v_max,
        math.sqrt(PEAK_ACCEL_FACTOR * distance / a_max),
    )
