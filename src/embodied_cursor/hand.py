# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera-independent hand servo. Camera frames never enter the Python bridge."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import Rect, Vec2


@dataclass(frozen=True)
class HandSample:
    x: float
    y: float
    pinch: float

    @staticmethod
    def parse(data):
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("Invalid hand sample")
        values = [data.get(key) for key in ("x", "y", "pinch")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("Invalid hand coordinates")
        if not (0 <= values[0] <= 1 and 0 <= values[1] <= 1 and 0 <= values[2] <= 5):
            raise ValueError("Hand coordinates are out of range")
        return HandSample(*values)


class HandController:
    """Open to arm, pinch once to click, open again before another click.

    Losing the hand resets arming and smoothing. A held pinch on reacquisition
    cannot click. The pointer freezes while pinching for a steady landing.
    """
    def __init__(self, bounds: Rect):
        if bounds.w < 1 or bounds.h < 1:
            raise ValueError("No usable monitor for hand control")
        self.bounds = bounds
        self.reset()

    def reset(self):
        self.armed = False
        self.open_since = None
        self.pinched_since = None
        self.latched = False
        self.last_time = None
        self.position = None

    def update(self, sample: HandSample | None, now: float):
        if sample is None:
            self.reset()
            return None, False, "Show an open hand"
        dt = min(0.1, max(0, now - self.last_time)) if self.last_time is not None else 0
        self.last_time = now
        if sample.pinch > 0.55:
            self.pinched_since = None
            self.latched = False
            if self.open_since is None:
                self.open_since = now
            if now - self.open_since >= 0.25:
                self.armed = True
        else:
            self.open_since = None
        if not self.armed:
            return None, False, "Open your hand to arm"
        # Use the central 80% of the frame so corners are easy to reach.
        x = min(1, max(0, (sample.x - 0.1) / 0.8))
        y = min(1, max(0, (sample.y - 0.1) / 0.8))
        target = Vec2(self.bounds.x + x * (self.bounds.w - 1), self.bounds.y + y * (self.bounds.h - 1))
        if sample.pinch < 0.35:
            if self.pinched_since is None:
                self.pinched_since = now
            click = not self.latched and now - self.pinched_since >= 0.06
            if click:
                self.latched = True
            return self.position, click and self.position is not None, "Clicked · open to reset" if self.latched else "Pinch to click"
        if sample.pinch > 0.55:
            alpha = 1 - math.exp(-dt / 0.045)
            self.position = target if self.position is None else self.position.lerp(target, alpha)
        return self.position, False, "Following your hand"
