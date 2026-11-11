# SPDX-License-Identifier: GPL-3.0-or-later
"""Interfaces between the portable core and a concrete desktop.

A desktop backend supplies three things: a clock, an actuator that moves the
real pointer and reads it back, and one or more perception sources that
describe what is on screen. The simulator implements all three in-process,
which is what makes the closed loop testable deterministically.
"""

from __future__ import annotations

import time
from typing import Protocol, Sequence, runtime_checkable

from .geometry import Rect, Vec2
from .world.element import UIElement


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    def sleep_until(self, t: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep_until(self, t: float) -> None:
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class SimClock:
    """Manually advanced clock. Subscribers are told about every advance."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._listeners: list = []

    def now(self) -> float:
        return self._now

    def subscribe(self, fn) -> None:
        self._listeners.append(fn)

    def advance(self, dt: float) -> None:
        if dt <= 0:
            return
        self._now += dt
        for fn in self._listeners:
            fn(self._now)

    def sleep_until(self, t: float) -> None:
        self.advance(t - self._now)


@runtime_checkable
class Actuator(Protocol):
    """Drives the pointer and keyboard, and reads the pointer back."""

    def position(self) -> Vec2:
        """Where the system pointer actually is."""
        ...

    def move_to(self, x: int, y: int) -> None: ...

    def button(self, button: int, down: bool) -> None:
        """Press or release a button: 1 left, 2 middle, 3 right."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Turn the wheel by whole notches; positive ``dy`` scrolls down."""
        ...

    def key(self, key: str, down: bool) -> None:
        """Press or release a named key such as ``ctrl``, ``Return`` or ``a``."""
        ...

    def type_text(self, text: str) -> None: ...


class PerceptionSource(Protocol):
    """A structured view of part of the screen. Never a screenshot."""

    def owns(self, ident: str) -> bool:
        """Whether an element id was issued by this source."""
        ...

    def monitors(self) -> Sequence[Rect]: ...

    def snapshot(self, budget: float | None = None) -> list[UIElement] | None:
        """Every element the source can see.

        A source whose full listing is slow may spread the work over several
        calls: it returns ``None`` when ``budget`` seconds run out and picks
        up where it left off next time.
        """
        ...

    def refresh(self, ids: Sequence[str]) -> tuple[list[UIElement], list[str]]:
        """Re-read just these elements. Returns ``(current, vanished_ids)``.

        This is the fast path that keeps the attended elements fresh at frame
        rate without re-reading the whole tree.
        """
        ...

    def wants_snapshot(self) -> bool:
        """True when the source knows its last snapshot is out of date."""
        ...


BUTTONS = {"left": 1, "middle": 2, "right": 3}
