# SPDX-License-Identifier: GPL-3.0-or-later
"""The unit of perception: one on-screen UI element.

Every backend (AT-SPI, the browser DOM, the simulator) translates its native
objects into this one shape, so the rest of the system never sees
toolkit-specific roles or coordinate systems.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..geometry import Rect


class Role:
    """Canonical role vocabulary. Backends map native roles onto these."""

    WINDOW = "window"
    DIALOG = "dialog"
    POPUP = "popup"
    TOOLTIP = "tooltip"
    PANEL = "panel"
    TOOLBAR = "toolbar"
    BUTTON = "button"
    TOGGLE = "toggle"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    LINK = "link"
    MENU_BAR = "menu_bar"
    MENU = "menu"
    MENU_ITEM = "menu_item"
    TAB = "tab"
    TAB_LIST = "tab_list"
    LIST = "list"
    LIST_ITEM = "list_item"
    TREE_ITEM = "tree_item"
    TABLE = "table"
    CELL = "cell"
    COMBO_BOX = "combo_box"
    TEXT_FIELD = "text_field"
    SLIDER = "slider"
    SCROLL_BAR = "scroll_bar"
    SPINNER = "spinner"
    PROGRESS = "progress"
    LABEL = "label"
    HEADING = "heading"
    IMAGE = "image"
    CANVAS = "canvas"
    DOCUMENT = "document"
    SEPARATOR = "separator"
    UNKNOWN = "unknown"


ALL_ROLES = frozenset(v for k, v in vars(Role).items() if k.isupper())

INTERACTIVE_ROLES = frozenset(
    {
        Role.BUTTON,
        Role.TOGGLE,
        Role.CHECKBOX,
        Role.RADIO,
        Role.LINK,
        Role.MENU,
        Role.MENU_ITEM,
        Role.TAB,
        Role.LIST_ITEM,
        Role.TREE_ITEM,
        Role.CELL,
        Role.COMBO_BOX,
        Role.TEXT_FIELD,
        Role.SLIDER,
        Role.SCROLL_BAR,
        Role.SPINNER,
    }
)

SURFACE_ROLES = frozenset({Role.WINDOW, Role.DIALOG, Role.POPUP, Role.TOOLTIP})
"""Roles that open their own stacking layer above whatever they cover."""


class State:
    VISIBLE = "visible"
    ENABLED = "enabled"
    FOCUSED = "focused"
    CHECKED = "checked"
    SELECTED = "selected"
    EXPANDED = "expanded"
    PRESSED = "pressed"
    EDITABLE = "editable"
    MODAL = "modal"
    ACTIVE = "active"
    OBSCURED = "obscured"
    """The backend hit-tested the element and something else is on top."""


DEFAULT_STATES = frozenset({State.VISIBLE, State.ENABLED})


@dataclass(frozen=True, slots=True)
class UIElement:
    id: str
    """Stable for as long as the backend's underlying object lives."""

    role: str
    bounds: Rect
    """Global desktop pixels."""

    name: str = ""
    states: frozenset[str] = DEFAULT_STATES
    value: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    text: str | None = None
    parent: str | None = None
    app: str = ""
    layer: int = 0
    """Stacking layer. A higher layer is drawn over a lower one."""

    description: str = ""

    @property
    def visible(self) -> bool:
        return State.VISIBLE in self.states and not self.bounds.empty

    @property
    def enabled(self) -> bool:
        return State.ENABLED in self.states

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES

    @property
    def value_fraction(self) -> float | None:
        """Value mapped onto ``[0, 1]`` across its range, if it has one."""
        if self.value is None or self.value_min is None or self.value_max is None:
            return None
        span = self.value_max - self.value_min
        if span <= 0:
            return None
        return min(max((self.value - self.value_min) / span, 0.0), 1.0)

    def has(self, state: str) -> bool:
        return state in self.states

    def with_(self, **changes: Any) -> "UIElement":
        return replace(self, **changes)

    def to_obj(self) -> dict[str, Any]:
        """Compact JSON-ready form, omitting fields at their defaults."""
        out: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "bounds": [round(v) for v in self.bounds.as_tuple()],
        }
        extra = sorted(self.states - DEFAULT_STATES)
        missing = sorted(DEFAULT_STATES - self.states)
        if extra or missing:
            out["states"] = extra + [f"not_{s}" for s in missing]
        if self.value is not None:
            out["value"] = self.value
            if self.value_min is not None and self.value_max is not None:
                out["range"] = [self.value_min, self.value_max]
        if self.text:
            out["text"] = self.text
        if self.app:
            out["app"] = self.app
        return out
