# SPDX-License-Identifier: GPL-3.0-or-later
"""The world model: the agent's continuously updated picture of the screen.

It is built entirely from structured sources (accessibility trees, window
lists, the DOM), never from pixels. Perception writes to it at frame rate;
the motor loop reads from it every tick. On top of plain storage it provides
what closed-loop pointing needs:

* identity tracking, so a selector resolved once keeps following the same
  element as it moves;
* a velocity estimate per element, so the cursor can lead a moving target;
* stacking-aware hit-testing and occlusion, so a popup that lands on the
  target is noticed before the click, not after;
* a change journal, so the slow planner is woken by events instead of polling.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..geometry import ZERO, Rect, Vec2, visible_parts
from .element import SURFACE_ROLES, Role, State, UIElement
from .selector import Selector, name_score, normalise_name


_NON_CLIPPING = frozenset({Role.MENU, Role.MENU_ITEM, Role.MENU_BAR, Role.COMBO_BOX})


@dataclass(frozen=True, slots=True)
class Match:
    element: UIElement
    score: float


@dataclass(frozen=True, slots=True)
class Change:
    revision: int
    time: float
    kind: str
    """One of ``appeared``, ``vanished``, ``moved``, ``state``, ``value``, ``name``."""

    element: UIElement
    previous: UIElement | None = None


class _Tracked:
    __slots__ = ("element", "seq", "depth", "first_seen", "last_seen", "last_moved", "velocity", "streak")

    def __init__(self, element: UIElement, seq: int, t: float) -> None:
        self.element = element
        self.seq = seq
        self.depth = 0
        self.first_seen = t
        self.last_seen = t
        self.last_moved = t
        self.velocity = ZERO
        self.streak = 0


class WorldModel:
    TELEPORT_PX = 48.0
    """A single-frame displacement beyond this is a jump, not motion."""

    STILL_AFTER = 0.06
    """Seconds without movement after which an element counts as stationary.

    Longer than one perception frame, so an animation that updates at half
    the perception rate is still seen as continuous motion.
    """

    def __init__(self, journal_size: int = 4096) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, _Tracked] = {}
        self._children: dict[str | None, list[str]] = {}
        self._journal: deque[Change] = deque(maxlen=journal_size)
        self._seq = 0
        self._tree_dirty = True
        self.revision = 0
        self.time = 0.0
        self.monitors: tuple[Rect, ...] = ()

    # -- writes ----------------------------------------------------------

    def set_monitors(self, monitors: Sequence[Rect]) -> None:
        with self._lock:
            self.monitors = tuple(monitors)

    def apply_snapshot(
        self,
        elements: Iterable[UIElement],
        t: float,
        owns: Callable[[str], bool] | None = None,
    ) -> None:
        """Replace everything a source owns with a fresh full listing.

        ``owns`` says which element ids belong to the source providing the
        snapshot. Owned elements missing from ``elements`` are dropped;
        others are left alone, so several sources can share one model.
        """
        with self._lock:
            fresh = list(elements)
            keep = {e.id for e in fresh}
            gone = [i for i in self._items if i not in keep and (owns is None or owns(i))]
            self._apply(fresh, gone, t)

    def apply_patch(self, updated: Iterable[UIElement], removed: Iterable[str], t: float) -> None:
        """Update or add some elements and drop others, leaving the rest."""
        with self._lock:
            self._apply(list(updated), list(removed), t)

    def _apply(self, updated: list[UIElement], removed: list[str], t: float) -> None:
        self.time = max(self.time, t)
        for ident in removed:
            tr = self._items.pop(ident, None)
            if tr is not None:
                self._tree_dirty = True
                self._log("vanished", tr.element, None, t)
        for el in updated:
            tr = self._items.get(el.id)
            if tr is None:
                self._seq += 1
                self._items[el.id] = _Tracked(el, self._seq, t)
                self._tree_dirty = True
                self._log("appeared", el, None, t)
                continue
            old = tr.element
            dt = t - tr.last_seen
            tr.last_seen = t
            if el.bounds == old.bounds and t - tr.last_moved > self.STILL_AFTER:
                tr.streak = 0
                tr.velocity = ZERO
            if el == old:
                continue
            tr.element = el
            if el.parent != old.parent:
                self._tree_dirty = True
            if el.bounds != old.bounds:
                self._track_motion(tr, old.bounds, el.bounds, t, dt)
                self._log("moved", el, old, t)
            if el.states != old.states:
                self._log("state", el, old, t)
            if el.value != old.value:
                self._log("value", el, old, t)
            if el.name != old.name:
                self._log("name", el, old, t)

    def _track_motion(self, tr: _Tracked, old: Rect, new: Rect, t: float, dt: float) -> None:
        d = new.center - old.center
        since_move = t - tr.last_moved
        tr.last_moved = t
        if dt <= 0 or d.norm() > self.TELEPORT_PX:
            tr.streak = 0
            tr.velocity = ZERO
            return
        if tr.streak == 0 or since_move <= 0:
            # First movement after a pause: the element may have been sampled
            # several times while still, so only the last interval counts.
            tr.streak = 1
            tr.velocity = d / dt
            return
        tr.streak += 1
        tr.velocity = tr.velocity * 0.5 + (d / since_move) * 0.5

    def _log(self, kind: str, el: UIElement, prev: UIElement | None, t: float) -> None:
        self.revision += 1
        self._journal.append(Change(self.revision, t, kind, el, prev))

    def _reindex(self) -> None:
        if not self._tree_dirty:
            return
        children: dict[str | None, list[str]] = {}
        for ident, tr in self._items.items():
            parent = tr.element.parent
            children.setdefault(parent if parent in self._items else None, []).append(ident)
        for ids in children.values():
            ids.sort(key=lambda i: self._items[i].seq)
        self._children = children
        stack = [(i, 0) for i in children.get(None, [])]
        while stack:
            ident, depth = stack.pop()
            self._items[ident].depth = depth
            stack.extend((c, depth + 1) for c in children.get(ident, []))
        self._tree_dirty = False

    # -- reads -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def get(self, ident: str) -> UIElement | None:
        tr = self._items.get(ident)
        return tr.element if tr else None

    def elements(self) -> list[UIElement]:
        with self._lock:
            return [tr.element for tr in sorted(self._items.values(), key=lambda tr: tr.seq)]

    def velocity(self, ident: str) -> Vec2:
        """Estimated on-screen velocity in px/s.

        Zero until the element has been seen moving on two consecutive frames,
        so a one-off layout shift is treated as a jump rather than extrapolated.
        """
        tr = self._items.get(ident)
        if tr is None or tr.streak < 2:
            return ZERO
        return tr.velocity

    def last_seen(self, ident: str) -> float | None:
        tr = self._items.get(ident)
        return tr.last_seen if tr else None

    def parent(self, el: UIElement) -> UIElement | None:
        return self.get(el.parent) if el.parent else None

    def ancestors(self, el: UIElement) -> list[UIElement]:
        """Nearest first."""
        with self._lock:
            out: list[UIElement] = []
            seen = {el.id}
            cur = self.parent(el)
            while cur is not None and cur.id not in seen:
                out.append(cur)
                seen.add(cur.id)
                cur = self.parent(cur)
            return out

    def children(self, el: UIElement | None) -> list[UIElement]:
        with self._lock:
            self._reindex()
            ids = self._children.get(el.id if el else None, [])
            return [self._items[i].element for i in ids]

    def descendants(self, el: UIElement) -> list[UIElement]:
        with self._lock:
            self._reindex()
            out: list[UIElement] = []
            stack = list(reversed(self._children.get(el.id, [])))
            while stack:
                ident = stack.pop()
                out.append(self._items[ident].element)
                stack.extend(reversed(self._children.get(ident, [])))
            return out

    def is_within(self, el: UIElement, ancestor: UIElement) -> bool:
        return el.id == ancestor.id or any(a.id == ancestor.id for a in self.ancestors(el))

    def surface_of(self, el: UIElement) -> UIElement:
        """The window, dialog or popup that ``el`` is drawn in."""
        if el.role in SURFACE_ROLES:
            return el
        for a in self.ancestors(el):
            if a.role in SURFACE_ROLES:
                return a
        return el

    def changes_since(self, revision: int) -> list[Change]:
        with self._lock:
            return [c for c in self._journal if c.revision > revision]

    # -- queries ---------------------------------------------------------

    def find(
        self,
        selector: Selector | str | dict,
        near: Vec2 | None = None,
        visible_only: bool = True,
    ) -> list[Match]:
        """Elements matching ``selector``, best first.

        Ties on name quality go to enabled elements, then to the upper
        stacking layer, then to whatever is closest to ``near``.
        """
        sel = Selector.of(selector)
        with self._lock:
            self._reindex()
            scope: set[str] | None = None
            if sel.within is not None:
                scope = {m.element.id for m in self.find(sel.within, visible_only=visible_only)}
                if not scope:
                    return []

            matches: list[Match] = []
            for tr in self._items.values():
                el = tr.element
                if visible_only and not el.visible:
                    continue
                if sel.id is not None and el.id != sel.id:
                    continue
                if sel.role is not None and el.role != sel.role:
                    continue
                if sel.app is not None and normalise_name(sel.app) not in normalise_name(el.app):
                    continue
                if sel.states and not sel.states <= el.states:
                    continue
                score = 1.0
                if sel.name is not None:
                    score = name_score(sel.name, el.name)
                    if score <= 0.0 or (sel.exact and score < 1.0):
                        continue
                if scope is not None and not any(a.id in scope for a in self.ancestors(el)):
                    continue
                matches.append(Match(el, score))

            if sel.index is not None:
                matches.sort(key=lambda m: (round(m.element.bounds.y / 4), m.element.bounds.x, self._items[m.element.id].seq))
                return matches[sel.index : sel.index + 1] if 0 <= sel.index < len(matches) else []

            def rank(m: Match) -> tuple:
                el = m.element
                dist = el.bounds.distance_to(near) if near is not None else 0.0
                return (-m.score, not el.enabled, -el.layer, dist, el.bounds.y, el.bounds.x)

            matches.sort(key=rank)
            return matches

    def find_one(self, selector: Selector | str | dict, near: Vec2 | None = None) -> UIElement | None:
        found = self.find(selector, near=near)
        return found[0].element if found else None

    def hit_test(self, p: Vec2) -> UIElement | None:
        """The topmost visible element under ``p``."""
        with self._lock:
            self._reindex()
            best: _Tracked | None = None
            best_key: tuple[int, int, int] | None = None
            for tr in self._items.values():
                el = tr.element
                if not el.visible or not el.bounds.contains(p):
                    continue
                key = (el.layer, tr.depth, tr.seq)
                if best_key is None or key > best_key:
                    best, best_key = tr, key
            return best.element if best else None

    def occluders(self, el: UIElement) -> list[UIElement]:
        """Surfaces on a higher layer that overlap ``el``."""
        with self._lock:
            out: list[UIElement] = []
            for tr in self._items.values():
                other = tr.element
                if (
                    other.layer > el.layer
                    and other.role in SURFACE_ROLES
                    and other.visible
                    and other.bounds.intersects(el.bounds)
                    and not self.is_within(el, other)
                ):
                    out.append(other)
            return out

    def clip_of(self, el: UIElement) -> Rect:
        """``el``'s bounds cut down to what its containers let show.

        A row scrolled half out of its list still reports its full rectangle;
        only the part inside the list is really there to click. Menus are
        exempt because toolkits parent a submenu to the item that opens it,
        which it is drawn beside, not inside.
        """
        rect = el.bounds
        if el.role in SURFACE_ROLES:
            return rect
        for a in self.ancestors(el):
            if a.role not in _NON_CLIPPING and not a.bounds.empty and a.bounds.intersects(rect):
                rect = rect.intersection(a.bounds)
            if a.role in SURFACE_ROLES:
                break
        return rect

    def visible_region(self, el: UIElement, see_through: Sequence[str] = ()) -> list[Rect]:
        """The parts of ``el`` that are on a monitor and not covered.

        Surfaces whose ids are in ``see_through`` are not counted as cover.
        """
        with self._lock:
            if not el.visible or el.has(State.OBSCURED):
                return []
            clipped = self.clip_of(el)
            pieces = [clipped]
            if self.monitors:
                pieces = [clipped.intersection(m) for m in self.monitors if clipped.intersects(m)]
            covers = [o.bounds for o in self.occluders(el) if o.id not in see_through]
            out: list[Rect] = []
            for piece in pieces:
                out.extend(visible_parts(piece, covers))
            return [r for r in out if r.w >= 1.0 and r.h >= 1.0]

    def cover_at(self, el: UIElement, p: Vec2) -> UIElement | None:
        """The surface hiding point ``p`` of ``el``, if any."""
        for o in self.occluders(el):
            if o.bounds.contains(p):
                return o
        return None

    def monitor_at(self, p: Vec2) -> int | None:
        for i, m in enumerate(self.monitors):
            if m.contains(p):
                return i
        return None
