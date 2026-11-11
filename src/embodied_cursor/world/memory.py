# SPDX-License-Identifier: GPL-3.0-or-later
"""Spatial and affordance memory.

Two things are remembered across runs, both keyed by an element's semantic
identity ``(app, role, name)`` rather than by a transient id:

* **Where it lives.** The rectangle it was last seen at, anchored to the
  window it was in and tagged with its monitor. This lets the cursor start
  moving towards an element before the accessibility tree has confirmed it
  (a menu item that is still animating open, say) and correct in flight once
  it has.

* **What reveals it.** If an element first showed up right after the cursor
  clicked or hovered something, that trigger is recorded. An element that is
  not on screen can then be reached by replaying the chain of triggers
  (``File`` -> ``Export`` -> ``PDF``) without waking the planner.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..geometry import Rect
from .element import INTERACTIVE_ROLES, SURFACE_ROLES, UIElement
from .model import WorldModel
from .selector import Selector, name_score, normalise_name

_VERSION = 1
Key = tuple[str, str, str]


def key_of(el: UIElement) -> Key:
    return (normalise_name(el.app), el.role, normalise_name(el.name))


@dataclass
class Memo:
    app: str
    role: str
    name: str
    rect: tuple[float, float, float, float]
    surface_role: str = ""
    surface_name: str = ""
    surface_origin: tuple[float, float] = (0.0, 0.0)
    monitor: int | None = None
    seen: int = 0
    last_seen: float = 0.0
    revealed_by: dict[str, str] | None = None
    """``{"app", "role", "name", "how"}`` of the element that brings this one up."""

    grip: float | None = None
    """For a slider: how far across its thickness the draggable part was found."""

    @property
    def key(self) -> Key:
        return (normalise_name(self.app), self.role, normalise_name(self.name))


@dataclass(frozen=True, slots=True)
class Recollection:
    rect: Rect
    confidence: float
    """Rough trust in ``rect``, in ``[0, 1]``."""

    memo: Memo


@dataclass(frozen=True, slots=True)
class RevealStep:
    selector: Selector
    how: str
    """``click`` or ``hover``."""


@dataclass
class SpatialMemory:
    path: Path | None = None
    capacity: int = 5000
    _memos: dict[Key, Memo] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _dirty: bool = False

    # -- learning --------------------------------------------------------

    def observe(self, world: WorldModel, now: float | None = None) -> None:
        """Note where every named, visible, actionable element currently is."""
        now = time.time() if now is None else now
        with self._lock:
            for el in world.elements():
                if not el.visible or not el.name:
                    continue
                if el.role not in INTERACTIVE_ROLES and el.role not in SURFACE_ROLES:
                    continue
                self._note(el, world, now)
            self._evict()

    def note_reveal(self, trigger: UIElement, how: str, revealed: Iterable[UIElement], world: WorldModel) -> None:
        """Record that acting on ``trigger`` made ``revealed`` appear."""
        if not trigger.name:
            return
        now = time.time()
        link = {"app": trigger.app, "role": trigger.role, "name": trigger.name, "how": how}
        with self._lock:
            self._note(trigger, world, now)
            for el in revealed:
                if not el.name or el.role not in INTERACTIVE_ROLES or key_of(el) == key_of(trigger):
                    continue
                memo = self._note(el, world, now)
                memo.revealed_by = dict(link)
            self._dirty = True

    def note_grip(self, el: UIElement, across: float, world: WorldModel) -> None:
        """Record where on a range control a drag actually took hold."""
        if not el.name:
            return
        with self._lock:
            self._note(el, world, time.time()).grip = across
            self._dirty = True

    def grip(self, el: UIElement) -> float | None:
        memo = self._memos.get(key_of(el))
        return memo.grip if memo else None

    def _note(self, el: UIElement, world: WorldModel, now: float) -> Memo:
        key = key_of(el)
        surface = world.surface_of(el)
        rect = el.bounds.as_tuple()
        memo = self._memos.get(key)
        if memo is None:
            memo = self._memos[key] = Memo(app=el.app, role=el.role, name=el.name, rect=rect)
        if memo.rect != rect or memo.seen == 0:
            self._dirty = True
        memo.rect = rect
        memo.surface_role = surface.role if surface.id != el.id else ""
        memo.surface_name = surface.name if surface.id != el.id else ""
        memo.surface_origin = (surface.bounds.x, surface.bounds.y)
        memo.monitor = world.monitor_at(el.bounds.center)
        memo.seen += 1
        memo.last_seen = now
        return memo

    def _evict(self) -> None:
        if len(self._memos) <= self.capacity:
            return
        oldest = sorted(self._memos.values(), key=lambda m: m.last_seen)
        for memo in oldest[: len(self._memos) - self.capacity]:
            del self._memos[memo.key]

    # -- recall ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._memos)

    def lookup(self, selector: Selector | str | dict) -> Memo | None:
        """Best remembered element for ``selector``, whether or not it is on screen."""
        sel = Selector.of(selector)
        if sel.id is not None:
            return None
        best: tuple[float, float, Memo] | None = None
        with self._lock:
            for memo in self._memos.values():
                if sel.role is not None and memo.role != sel.role:
                    continue
                if sel.app is not None and normalise_name(sel.app) not in normalise_name(memo.app):
                    continue
                score = 1.0
                if sel.name is not None:
                    score = name_score(sel.name, memo.name)
                    if score < 0.75 or (sel.exact and score < 1.0):
                        continue
                cand = (score, memo.last_seen, memo)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        return best[2] if best else None

    def recall(self, selector: Selector | str | dict, world: WorldModel) -> Recollection | None:
        """Where ``selector`` was last seen, re-anchored to its window's current position."""
        memo = self.lookup(selector)
        if memo is None:
            return None
        rect = Rect(*memo.rect)
        confidence = 0.3
        if memo.surface_role and memo.surface_name:
            for el in world.elements():
                if (
                    el.role == memo.surface_role
                    and el.visible
                    and normalise_name(el.name) == normalise_name(memo.surface_name)
                    and normalise_name(el.app) == normalise_name(memo.app)
                ):
                    dx = el.bounds.x - memo.surface_origin[0]
                    dy = el.bounds.y - memo.surface_origin[1]
                    rect = Rect(rect.x + dx, rect.y + dy, rect.w, rect.h)
                    confidence = 0.6
                    break
        confidence *= min(1.0, 0.5 + 0.25 * memo.seen)
        return Recollection(rect, confidence, memo)

    def reveal_chain(self, selector: Selector | str | dict, world: WorldModel, max_depth: int = 4) -> list[RevealStep]:
        """Triggers to act on, outermost first, to bring ``selector`` on screen.

        Empty when nothing is known, or when the chain bottoms out on a trigger
        that is neither on screen nor itself revealable.
        """
        steps: list[RevealStep] = []
        memo = self.lookup(selector)
        seen: set[Key] = set()
        with self._lock:
            while memo is not None and memo.revealed_by and len(steps) < max_depth:
                link = memo.revealed_by
                trigger = Selector(role=link["role"], name=link["name"], app=link["app"] or None, exact=True)
                key = (normalise_name(link["app"]), link["role"], normalise_name(link["name"]))
                if key in seen:
                    return []
                seen.add(key)
                steps.append(RevealStep(trigger, link.get("how", "click")))
                if world.find(trigger):
                    steps.reverse()
                    return steps
                memo = self._memos.get(key)
        return []

    # -- persistence -----------------------------------------------------

    def save(self) -> None:
        if self.path is None or not self._dirty:
            return
        with self._lock:
            data = {"version": _VERSION, "memos": [asdict(m) for m in self._memos.values()]}
            self._dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".memory-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def load(path: Path | str) -> "SpatialMemory":
        """Open the memory at ``path``. A missing or unreadable file starts empty."""
        path = Path(path)
        mem = SpatialMemory(path=path)
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return mem
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return mem
        for raw in data.get("memos", []):
            try:
                memo = Memo(
                    app=str(raw["app"]),
                    role=str(raw["role"]),
                    name=str(raw["name"]),
                    rect=tuple(float(v) for v in raw["rect"]),  # type: ignore[arg-type]
                    surface_role=str(raw.get("surface_role", "")),
                    surface_name=str(raw.get("surface_name", "")),
                    surface_origin=tuple(float(v) for v in raw.get("surface_origin", (0, 0))),  # type: ignore[arg-type]
                    monitor=raw.get("monitor"),
                    seen=int(raw.get("seen", 1)),
                    last_seen=float(raw.get("last_seen", 0.0)),
                    revealed_by=dict(raw["revealed_by"]) if raw.get("revealed_by") else None,
                    grip=float(raw["grip"]) if raw.get("grip") is not None else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if len(memo.rect) == 4 and len(memo.surface_origin) == 2:
                mem._memos[memo.key] = memo
        return mem
