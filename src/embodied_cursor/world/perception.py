# SPDX-License-Identifier: AGPL-3.0-or-later
"""Foveated perception scheduler.

Reading an entire accessibility tree can take hundreds of milliseconds, far
too slow to steer a cursor by. Vision solves the same problem with a fovea:
high acuity where attention is, coarse coverage elsewhere. Here:

* the **attended set** (the current target, obstacles, the slider being
  dragged) is re-read every frame, typically 60 Hz;
* the **periphery** (everything else) is refreshed by full snapshots at a low
  rate, sooner if a source reports that something structural changed.

The scheduler can be pumped synchronously from the motor loop (simulator,
tests) or run on its own thread (real desktops, where source calls block on
IPC).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..runtime import Clock, PerceptionSource
from .model import WorldModel


@dataclass
class PerceptionStats:
    frames: int = 0
    snapshots: int = 0
    last_frame_s: float = 0.0
    worst_frame_s: float = 0.0
    last_snapshot_s: float = 0.0
    errors: int = 0
    last_error: str = ""


class Perception:
    def __init__(
        self,
        sources: Sequence[PerceptionSource],
        world: WorldModel,
        clock: Clock,
        hz: float = 60.0,
        snapshot_hz: float = 2.0,
    ) -> None:
        self.sources = list(sources)
        self.world = world
        self.clock = clock
        self.period = 1.0 / hz
        self.snapshot_period = 1.0 / snapshot_hz
        self.stats = PerceptionStats()
        self._attended: tuple[str, ...] = ()
        self._next_frame = -float("inf")
        self._next_snapshot = [-float("inf")] * len(self.sources)
        self._crawling = [False] * len(self.sources)
        self._crawl_started = [0.0] * len(self.sources)
        self._snapshot_epoch = [-float("inf")] * len(self.sources)
        self._requested_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._primed = threading.Event()

    # -- attention -------------------------------------------------------

    def attend(self, ids: Iterable[str]) -> None:
        """Set which elements are kept fresh at frame rate."""
        self._attended = tuple(dict.fromkeys(ids))

    @property
    def attended(self) -> tuple[str, ...]:
        return self._attended

    def request_snapshot(self) -> None:
        """Ask for a full snapshot as soon as possible, ahead of schedule."""
        if self._requested_at is None:
            self._requested_at = self.clock.now()

    def fresh_since(self, t: float) -> bool:
        """Whether every source has completed a snapshot begun at or after ``t``."""
        return all(epoch >= t for epoch in self._snapshot_epoch)

    # -- stepping --------------------------------------------------------

    def prime(self) -> None:
        """Block until every source has delivered one full snapshot."""
        monitors = [m for s in self.sources for m in s.monitors()]
        self.world.set_monitors(monitors)
        for i, src in enumerate(self.sources):
            while True:
                elements = src.snapshot(None)
                if elements is not None:
                    break
            now = self.clock.now()
            self.world.apply_snapshot(elements, now, src.owns)
            self._next_snapshot[i] = now + self.snapshot_period
            self._snapshot_epoch[i] = now
            self.stats.snapshots += 1
        self._primed.set()

    def pump(self, force: bool = False) -> bool:
        """Run one perception frame if one is due. Returns whether it ran."""
        now = self.clock.now()
        if not force and now < self._next_frame:
            return False
        self._next_frame = now + self.period if now - self._next_frame > self.period else self._next_frame + self.period
        self.frame()
        return True

    def frame(self) -> None:
        start = self.clock.now()
        attended = self._attended
        requested = self._requested_at
        for i, src in enumerate(self.sources):
            try:
                ids = [a for a in attended if src.owns(a)]
                if ids:
                    current, vanished = src.refresh(ids)
                    self.world.apply_patch(current, vanished, self.clock.now())

                now = self.clock.now()
                wanted = requested is not None and self._snapshot_epoch[i] < requested
                if self._crawling[i] or wanted or now >= self._next_snapshot[i] or src.wants_snapshot():
                    if not self._crawling[i]:
                        self._crawl_started[i] = now
                    budget = max(0.0, self.period * 0.6 - (now - start))
                    elements = src.snapshot(budget)
                    if elements is None:
                        self._crawling[i] = True
                    else:
                        done = self.clock.now()
                        self._crawling[i] = False
                        self.world.apply_snapshot(elements, done, src.owns)
                        self._next_snapshot[i] = done + self.snapshot_period
                        self._snapshot_epoch[i] = self._crawl_started[i]
                        self.stats.snapshots += 1
                        self.stats.last_snapshot_s = done - self._crawl_started[i]
            except Exception as exc:  # a misbehaving app must not stop the loop
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self._crawling[i] = False
                self._next_snapshot[i] = self.clock.now() + self.snapshot_period
        if requested is not None and self._requested_at == requested and self.fresh_since(requested):
            self._requested_at = None
        took = self.clock.now() - start
        self.stats.frames += 1
        self.stats.last_frame_s = took
        self.stats.worst_frame_s = max(self.stats.worst_frame_s, took)

    # -- threaded mode ---------------------------------------------------

    @property
    def threaded(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """Run perception on a background thread until :meth:`stop`."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cec-perception", daemon=True)
        self._thread.start()
        self._primed.wait()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        try:
            self._run_frames()
        finally:
            for source in self.sources:
                cleanup = getattr(source, "close_thread", None)
                if cleanup:
                    cleanup()

    def _run_frames(self) -> None:
        try:
            self.prime()
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            self._primed.set()
        next_frame = self.clock.now()
        while not self._stop.is_set():
            self.frame()
            next_frame += self.period
            now = self.clock.now()
            if next_frame < now:
                next_frame = now
            self._stop.wait(max(0.0, next_frame - now))
