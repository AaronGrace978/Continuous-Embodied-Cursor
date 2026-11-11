# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the planner is shown: the screen as a short structured listing.

No pixels are involved. The listing is built from the world model, grouped by
window, and is small enough to send to a language model on every planning
round: a few dozen lines of ``role "name"`` with states and values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..geometry import Vec2
from ..world.element import SURFACE_ROLES, Role, State, UIElement
from ..world.model import WorldModel

_CONTAINER_OPENERS = frozenset({Role.MENU, Role.COMBO_BOX})
_SHOWN_STATES = (State.CHECKED, State.SELECTED, State.EXPANDED, State.PRESSED, State.FOCUSED)


@dataclass(frozen=True, slots=True)
class Observation:
    t: float
    pointer: tuple[int, int]
    under: str
    """Description of the element under the pointer, or ''."""

    surfaces: list[dict[str, Any]] = field(default_factory=list)
    """Windows from front to back, each with the elements shown in it."""

    closed: list[str] = field(default_factory=list)
    """Paths to entries of menus that are currently closed."""

    truncated: int = 0
    """How many lower-priority elements were left out to keep this short."""

    def to_obj(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 3),
            "pointer": list(self.pointer),
            "under": self.under,
            "surfaces": self.surfaces,
            "closed": self.closed,
            "truncated": self.truncated,
        }

    def elements(self) -> list[dict[str, Any]]:
        return [e for s in self.surfaces for e in s["elements"]]

    def text(self) -> str:
        lines = [f"pointer at ({self.pointer[0]}, {self.pointer[1]})" + (f" over {self.under}" if self.under else "")]
        for surface in self.surfaces:
            x, y, w, h = surface["bounds"]
            title = f'{surface["role"]} "{surface["name"]}"' if surface["name"] else surface["role"]
            flags = " (active)" if surface.get("active") else ""
            lines.append(f"{title} at {x},{y} size {w}x{h}{flags}")
            for el in surface["elements"]:
                lines.append("  " + _line(el))
        if self.closed:
            lines.append("in closed menus: " + "; ".join(self.closed))
        if self.truncated:
            lines.append(f"({self.truncated} less relevant elements not listed)")
        return "\n".join(lines)


def _line(el: dict[str, Any]) -> str:
    parts = [f'{el["role"]} "{el["name"]}"' if el["name"] else el["role"]]
    if "value" in el:
        value = f'value={el["value"]:g}'
        if "range" in el:
            value += f' range {el["range"][0]:g}..{el["range"][1]:g}'
        parts.append(value)
    if el.get("text"):
        parts.append(f'text="{el["text"]}"')
    if el.get("states"):
        parts.append("(" + ", ".join(el["states"]) + ")")
    return " ".join(parts)


def describe(el: UIElement) -> str:
    return f'{el.role} "{el.name}"' if el.name else el.role


def _entry(el: UIElement, covered: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"role": el.role, "name": el.name, "id": el.id}
    if el.value is not None:
        out["value"] = el.value
        if el.value_min is not None and el.value_max is not None:
            out["range"] = [el.value_min, el.value_max]
    if el.text:
        out["text"] = el.text if len(el.text) <= 80 else el.text[:77] + "..."
    states = [s for s in _SHOWN_STATES if el.has(s)]
    if not el.enabled:
        states.append("disabled")
    if covered:
        states.append("covered")
    if states:
        out["states"] = states
    return out


def _worth_listing(el: UIElement) -> int:
    """0 = skip; higher = more useful to a planner."""
    if el.role in SURFACE_ROLES:
        return 0
    if el.interactive:
        return 3 if el.name or el.value is not None or el.text else 2
    if el.name and el.role in (Role.LABEL, Role.HEADING, Role.IMAGE):
        return 1
    return 0


def observe(world: WorldModel, pointer: Vec2, t: float = 0.0, limit: int = 120) -> Observation:
    """Summarise the world model for a planner."""
    elements = world.elements()
    under = world.hit_test(pointer)

    by_surface: dict[str, list[tuple[int, UIElement]]] = {}
    surfaces: dict[str, UIElement] = {}
    closed: list[str] = []
    for el in elements:
        if el.role in SURFACE_ROLES:
            if el.visible:
                surfaces.setdefault(el.id, el)
            continue
        if not el.visible:
            path = _closed_path(world, el)
            if path:
                closed.append(path)
            continue
        rank = _worth_listing(el)
        if not rank:
            continue
        surface = world.surface_of(el)
        surfaces.setdefault(surface.id, surface)
        by_surface.setdefault(surface.id, []).append((rank, el))

    listed = sorted(
        ((rank, el) for items in by_surface.values() for rank, el in items),
        key=lambda item: (-item[0], item[1].bounds.center.dist(pointer)),
    )
    keep = {el.id for _, el in listed[:limit]}
    truncated = max(len(listed) - limit, 0)

    out_surfaces: list[dict[str, Any]] = []
    for sid, surface in sorted(surfaces.items(), key=lambda kv: -kv[1].layer):
        items = [el for _, el in by_surface.get(sid, []) if el.id in keep]
        if not items and (not surface.name or _duplicated(surface, surfaces, by_surface)):
            continue
        items.sort(key=lambda el: (round(el.bounds.top / 12), el.bounds.left))
        out_surfaces.append(
            {
                "role": surface.role,
                "name": surface.name,
                "id": surface.id,
                "bounds": [round(v) for v in surface.bounds.as_tuple()],
                "active": surface.has(State.ACTIVE),
                "elements": [_entry(el, bool(world.occluders(el)) and not world.visible_region(el)) for el in items],
            }
        )

    return Observation(
        t=t,
        pointer=pointer.rounded(),
        under=describe(under) if under is not None else "",
        surfaces=out_surfaces,
        closed=sorted(set(closed))[:60],
        truncated=truncated,
    )


def _duplicated(surface: UIElement, surfaces: dict[str, UIElement], filled: dict[str, list]) -> bool:
    """Is this bare window the same thing as another surface that has contents?

    The window system and the accessibility tree each report a window; only
    the latter has anything in it.
    """
    for sid, other in surfaces.items():
        if sid == surface.id or not filled.get(sid):
            continue
        overlap = surface.bounds.intersection(other.bounds).area
        if overlap > 0.0 and overlap >= 0.8 * max(surface.bounds.area, other.bounds.area):
            return True
    return False


def _closed_path(world: WorldModel, el: UIElement) -> str:
    """``File > Export as > PDF`` for an entry of a menu that is not open."""
    if el.role not in (Role.MENU_ITEM, Role.MENU, Role.LIST_ITEM) or not el.name:
        return ""
    names = [el.name]
    for a in world.ancestors(el):
        if a.role in _CONTAINER_OPENERS:
            if a.name:
                names.append(a.name)
            if a.visible:
                return " > ".join(reversed(names))
        elif a.visible:
            break
    return ""
