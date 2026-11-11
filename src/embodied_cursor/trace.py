# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flight recorder for the motor loop.

Every tick's kinematic state and every notable event (target locked,
re-plan, press, occlusion, ...) is kept in memory so that a run can be
inspected, plotted, or asserted on in tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .geometry import Vec2


@dataclass(frozen=True, slots=True)
class Sample:
    t: float
    pos: Vec2
    vel: Vec2
    goal: Vec2 | None
    buttons: int
    """Bit mask of held buttons: bit 0 is the left button."""


@dataclass
class Trace:
    max_samples: int = 200_000
    samples: list[Sample] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def sample(self, t: float, pos: Vec2, vel: Vec2, goal: Vec2 | None, buttons: int) -> None:
        if len(self.samples) >= self.max_samples:
            del self.samples[: self.max_samples // 10]
        self.samples.append(Sample(t, pos, vel, goal, buttons))

    def event(self, t: float, kind: str, **data: Any) -> None:
        self.events.append({"t": round(t, 4), "kind": kind, **data})

    def clear(self) -> None:
        self.samples.clear()
        self.events.clear()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def path_length(self) -> float:
        return sum(a.pos.dist(b.pos) for a, b in zip(self.samples, self.samples[1:]))

    def peak_speed(self) -> float:
        return max((s.vel.norm() for s in self.samples), default=0.0)

    def to_obj(self) -> dict[str, Any]:
        return {
            "samples": [
                [
                    round(s.t, 4),
                    round(s.pos.x, 2),
                    round(s.pos.y, 2),
                    round(s.vel.x, 1),
                    round(s.vel.y, 1),
                    None if s.goal is None else round(s.goal.x, 1),
                    None if s.goal is None else round(s.goal.y, 1),
                    s.buttons,
                ]
                for s in self.samples
            ],
            "columns": ["t", "x", "y", "vx", "vy", "goal_x", "goal_y", "buttons"],
            "events": self.events,
        }

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_obj()), encoding="utf-8")
