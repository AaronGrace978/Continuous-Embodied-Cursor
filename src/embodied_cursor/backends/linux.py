# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemble a body for a live Linux/X11 desktop."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..body import Body, BodyConfig
from ..motor.controller import MotorConfig
from ..runtime import PerceptionSource, RealClock
from ..world.memory import SpatialMemory
from ..world.model import WorldModel
from ..world.perception import Perception
from .x11 import BackendUnavailable, X11Actuator, X11Windows


def default_memory_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "embodied-cursor" / "memory.json"


@dataclass
class Session:
    body: Body
    perception: Perception
    world: WorldModel
    actuator: X11Actuator
    sources: list
    notes: list[str]
    """Human-readable remarks about anything that could not be set up."""

    def settle(self, timeout: float = 3.0) -> bool:
        """Wait until the whole desktop has been read at least once more."""
        clock = self.body.clock
        since = clock.now()
        self.perception.request_snapshot()
        while clock.now() - since < timeout:
            if self.perception.fresh_since(since):
                return True
            clock.sleep_until(clock.now() + 0.01)
        return False

    def close(self) -> None:
        try:
            self.body.close()
        finally:
            self.perception.stop()
            for src in self.sources:
                close = getattr(src, "close", None)
                if close:
                    close()
            self.actuator.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_session(
    display: str | None = None,
    memory_path: Path | str | None = None,
    use_memory: bool = True,
    motor: MotorConfig | None = None,
    config: BodyConfig | None = None,
    perception_hz: float = 60.0,
    snapshot_hz: float = 1.0,
    extra_sources: list[PerceptionSource] | None = None,
) -> Session:
    """Connect to the X server and the accessibility bus and start perceiving.

    Raises :class:`BackendUnavailable` when there is no X display to drive.
    A missing accessibility stack is not fatal: the session then only knows
    about windows, and says so in ``notes``.
    """
    if sys.platform != "linux":
        raise BackendUnavailable("the live backend currently supports Linux with X11 only")
    if os.environ.get("WAYLAND_DISPLAY") and not (display or os.environ.get("DISPLAY")):
        raise BackendUnavailable("Wayland sessions are not supported yet; an X11 (or XWayland) display is needed")

    notes: list[str] = []
    actuator = X11Actuator(display)
    windows = X11Windows(display)
    sources: list = [windows]
    try:
        from .atspi import AtspiSource

        sources.append(AtspiSource(windows))
    except BackendUnavailable as exc:
        notes.append(f"accessibility tree unavailable: {exc}")
    sources.extend(extra_sources or [])

    # Keep the motor loop's timing tight while the perception thread works.
    sys.setswitchinterval(0.001)

    clock = RealClock()
    world = WorldModel()
    perception = Perception(sources, world, clock, perception_hz, snapshot_hz)
    perception.start()
    if perception.stats.errors:
        notes.append(f"perception error during start-up: {perception.stats.last_error}")

    memory = None
    if use_memory:
        memory = SpatialMemory.load(memory_path or default_memory_path())
    body = Body(actuator, perception, clock, memory=memory, motor=motor, config=config)
    return Session(body, perception, world, actuator, sources, notes)
