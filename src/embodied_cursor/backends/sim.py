# SPDX-License-Identifier: AGPL-3.0-or-later
"""A simulated desktop.

It is a clock, an actuator and a perception source in one, with a small set
of widgets that behave the way real toolkits do where it matters to a
pointer: buttons click only if pressed and released inside, sliders follow a
grabbed handle, an open menu switches when a sibling is hovered, dialogs
cover what is beneath them. Scripted events (a button that moves, a dialog
that pops up, a person bumping the mouse) are scheduled in simulated time, so
closed-loop behaviour can be tested exactly and without a display.
"""

from __future__ import annotations

import heapq
import itertools
from collections import deque
from typing import TYPE_CHECKING, Callable, Sequence

from ..geometry import Rect, Vec2, clamp_to_any
from ..runtime import SimClock
from ..world.element import DEFAULT_STATES, Role, State, UIElement

if TYPE_CHECKING:
    from ..body import Body, BodyConfig
    from ..motor.controller import MotorConfig
    from ..world.memory import SpatialMemory

PREFIX = "sim:"


class Widget:
    role = Role.UNKNOWN

    def __init__(self, name: str = "", rect: Rect = Rect(0, 0, 0, 0), layer: int | None = None) -> None:
        self.name = name
        self.rect = rect
        self.layer_override = layer
        self.visible = True
        self.enabled = True
        self.parent: Widget | None = None
        self.children: list[Widget] = []
        self.desktop: "SimDesktop" = None  # type: ignore[assignment]
        self.id = ""
        self.order = 0
        self.hovered = False
        self.enters = 0

    # -- tree ------------------------------------------------------------

    def add(self, child: "Widget") -> "Widget":
        child.parent = self
        self.children.append(child)
        if self.desktop is not None:
            self.desktop._register(child)
        return child

    @property
    def layer(self) -> int:
        if self.layer_override is not None:
            return self.layer_override
        return self.parent.layer if self.parent else 0

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1

    def shown(self) -> bool:
        w: Widget | None = self
        while w is not None:
            if not w.visible:
                return False
            w = w.parent
        return True

    def descendants(self) -> list["Widget"]:
        out: list[Widget] = []
        for c in self.children:
            out.append(c)
            out.extend(c.descendants())
        return out

    def move_by(self, d: Vec2) -> None:
        self.rect = self.rect.translate(d)
        for c in self.children:
            c.move_by(d)

    def move_to(self, x: float, y: float) -> None:
        self.move_by(Vec2(x - self.rect.x, y - self.rect.y))

    # -- perception ------------------------------------------------------

    def states(self) -> set[str]:
        s = set()
        if self.shown():
            s.add(State.VISIBLE)
        if self.enabled:
            s.add(State.ENABLED)
        return s

    def element(self) -> UIElement:
        states = frozenset(self.states())
        return UIElement(
            id=self.id,
            role=self.role,
            bounds=self.rect,
            name=self.name,
            states=DEFAULT_STATES if states == DEFAULT_STATES else states,
            parent=self.parent.id if self.parent else None,
            app="sim",
            layer=self.layer,
            **self.extra(),
        )

    def extra(self) -> dict:
        return {}

    # -- input (overridden as needed) ---------------------------------------

    def on_enter(self) -> None: ...

    def on_leave(self) -> None: ...

    def on_press(self, p: Vec2, button: int) -> None: ...

    def on_release(self, p: Vec2, button: int, inside: bool) -> None: ...

    def on_drag(self, p: Vec2) -> None: ...

    def on_scroll(self, dy: int) -> bool:
        return False

    def on_text(self, text: str) -> None: ...

    def log(self, kind: str, **data) -> None:
        self.desktop.log.append((self.desktop.clock.now(), kind, self.name, data))


class Window(Widget):
    role = Role.WINDOW


class Panel(Widget):
    role = Role.PANEL


class Label(Widget):
    role = Role.LABEL


class Dialog(Widget):
    """A surface that covers whatever is under it. Hidden until shown."""

    role = Role.DIALOG

    def __init__(self, name: str, rect: Rect, layer: int = 50) -> None:
        super().__init__(name, rect, layer)
        self.visible = False

    def show(self) -> None:
        self.visible = True
        self.log("open")

    def hide(self) -> None:
        self.visible = False
        self.log("close")


class Button(Widget):
    role = Role.BUTTON

    def __init__(
        self,
        name: str,
        rect: Rect,
        on_click: Callable[["Button"], None] | None = None,
        checkable: bool = False,
    ) -> None:
        super().__init__(name, rect)
        self.on_click = on_click
        self.checkable = checkable
        self.checked = False
        self.clicks = 0
        self._armed = False
        if checkable:
            self.role = Role.CHECKBOX

    def states(self) -> set[str]:
        s = super().states()
        if self.checked:
            s.add(State.CHECKED)
        if self._armed:
            s.add(State.PRESSED)
        return s

    def on_press(self, p: Vec2, button: int) -> None:
        self._armed = button == 1 and self.enabled

    def on_release(self, p: Vec2, button: int, inside: bool) -> None:
        if self._armed and inside and button == 1:
            self.clicks += 1
            if self.checkable:
                self.checked = not self.checked
            self.log("click", at=p.rounded())
            if self.on_click:
                self.on_click(self)
        self._armed = False


class Slider(Widget):
    """A draggable handle on a track.

    ``curve`` maps handle position (0..1 along the track) to value fraction,
    and ``inverse`` maps back, to model non-linear controls such as a
    logarithmic volume fader. ``reverse`` puts the maximum at the left/top.
    """

    role = Role.SLIDER

    def __init__(
        self,
        name: str,
        rect: Rect,
        lo: float = 0.0,
        hi: float = 100.0,
        value: float = 0.0,
        step: float | None = None,
        reverse: bool = False,
        curve: Callable[[float], float] | None = None,
        inverse: Callable[[float], float] | None = None,
        handle: float | None = None,
        band: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        super().__init__(name, rect)
        self.lo, self.hi, self.value = lo, hi, value
        # Part of the thickness that actually responds; the rest is dead space
        # such as a value readout drawn inside the control's bounds.
        self.band = band
        self.step = step
        self.reverse = reverse
        self.curve = curve or (lambda f: f)
        self.inverse = inverse or (lambda f: f)
        self.horizontal = rect.w >= rect.h
        self.handle_size = handle if handle is not None else min(rect.w, rect.h)
        self._grab: float | None = None
        self.grabs = 0

    def extra(self) -> dict:
        return {"value": self.value, "value_min": self.lo, "value_max": self.hi}

    def _travel(self) -> tuple[float, float]:
        start = (self.rect.left if self.horizontal else self.rect.top) + self.handle_size / 2
        length = (self.rect.w if self.horizontal else self.rect.h) - self.handle_size
        return start, max(length, 1.0)

    def _pos_fraction(self) -> float:
        f = self.inverse((self.value - self.lo) / (self.hi - self.lo))
        return 1.0 - f if self.reverse else f

    def handle_center(self) -> Vec2:
        start, length = self._travel()
        along = start + self._pos_fraction() * length
        c = self.rect.center
        return Vec2(along, c.y) if self.horizontal else Vec2(c.x, along)

    def handle_rect(self) -> Rect:
        c = self.handle_center()
        h = self.handle_size / 2
        return Rect(c.x - h, c.y - h, self.handle_size, self.handle_size)

    def _set_from(self, along: float) -> None:
        start, length = self._travel()
        f = min(max((along - start) / length, 0.0), 1.0)
        if self.reverse:
            f = 1.0 - f
        v = self.lo + self.curve(f) * (self.hi - self.lo)
        if self.step:
            v = self.lo + round((v - self.lo) / self.step) * self.step
        v = min(max(v, self.lo), self.hi)
        if v != self.value:
            self.value = v
            self.log("value", value=v)

    def on_press(self, p: Vec2, button: int) -> None:
        if button != 1 or not self.enabled:
            return
        along = p.x if self.horizontal else p.y
        across = (p.y - self.rect.top) / self.rect.h if self.horizontal else (p.x - self.rect.left) / self.rect.w
        self.grabs += 1
        if not self.band[0] <= across <= self.band[1]:
            return
        if self.handle_rect().contains(p):
            c = self.handle_center()
            self._grab = along - (c.x if self.horizontal else c.y)
        else:
            # Clicking the track jumps the handle under the pointer.
            self._grab = 0.0
            self._set_from(along)

    def on_drag(self, p: Vec2) -> None:
        if self._grab is not None:
            self._set_from((p.x if self.horizontal else p.y) - self._grab)

    def on_release(self, p: Vec2, button: int, inside: bool) -> None:
        self._grab = None


class TextField(Widget):
    role = Role.TEXT_FIELD

    def __init__(self, name: str, rect: Rect) -> None:
        super().__init__(name, rect)
        self.text = ""
        self.submitted: list[str] = []

    def states(self) -> set[str]:
        s = super().states()
        s.add(State.EDITABLE)
        if self.desktop.focus is self:
            s.add(State.FOCUSED)
        return s

    def extra(self) -> dict:
        return {"text": self.text}

    def on_press(self, p: Vec2, button: int) -> None:
        self.desktop.focus = self

    def select_all(self) -> None:
        self._selected = True

    def on_text(self, text: str) -> None:
        for ch in text:
            if getattr(self, "_selected", False) and ch != "\n":
                self.text = ""
            self._selected = False
            if ch == "\n":
                self.submitted.append(self.text)
            elif ch == "\b":
                self.text = self.text[:-1]
            else:
                self.text += ch


class MenuBar(Widget):
    role = Role.MENU_BAR

    def open_menu(self) -> "Menu | None":
        return next((m for m in self.children if isinstance(m, Menu) and m.open), None)

    def close_all(self) -> None:
        for m in self.children:
            if isinstance(m, Menu):
                m.close()


class Menu(Widget):
    """A menu, either a top-level entry in a bar or a submenu inside another menu.

    Its items are its children, drawn in a popup on a higher layer. As in real
    toolkits, hovering a sibling entry takes the menu away from this one.
    """

    role = Role.MENU
    ITEM_H = 28.0
    ITEM_W = 190.0

    def __init__(self, name: str, rect: Rect = Rect(0, 0, 0, 0), open_delay: float = 0.12) -> None:
        super().__init__(name, rect)
        self.open = False
        self.open_delay = open_delay
        self.popup: Widget | None = None
        self.opens = 0
        self._hover_serial = 0

    @property
    def top_level(self) -> bool:
        return isinstance(self.parent, MenuBar)

    def states(self) -> set[str]:
        s = super().states()
        if self.open:
            s.add(State.EXPANDED)
        return s

    def item(self, name: str) -> "MenuItem":
        return self.add(MenuItem(name))  # type: ignore[return-value]

    def submenu(self, name: str) -> "Menu":
        sub = Menu(name)
        sub.visible = False
        return self.add(sub)  # type: ignore[return-value]

    def _layout(self) -> None:
        if self.top_level:
            x, y = self.rect.left, self.rect.bottom
        else:
            x, y = self.rect.right, self.rect.top
        layer = self.layer + 10
        n = len(self.children)
        if self.popup is None:
            self.popup = self.desktop.add(Widget("", Rect(0, 0, 0, 0), layer))
            self.popup.role = Role.POPUP
        self.popup.rect = Rect(x, y, self.ITEM_W, self.ITEM_H * n)
        self.popup.layer_override = layer
        for i, c in enumerate(self.children):
            c.rect = Rect(x, y + i * self.ITEM_H, self.ITEM_W, self.ITEM_H)
            c.layer_override = layer

    def show(self) -> None:
        if self.open:
            return
        self._layout()
        self.open = True
        self.opens += 1
        assert self.popup is not None
        self.popup.visible = True
        for c in self.children:
            c.visible = True
        self.log("open")

    def close(self) -> None:
        if not self.open:
            return
        for c in self.children:
            if isinstance(c, Menu):
                c.close()
            c.visible = False
        self.open = False
        if self.popup is not None:
            self.popup.visible = False
        self.log("close")

    def on_enter(self) -> None:
        _close_sibling_menus(self)
        if self.top_level:
            bar = self.parent
            assert isinstance(bar, MenuBar)
            if any(m.open for m in bar.children if isinstance(m, Menu) and m is not self):
                bar.close_all()
                self.show()
            return
        self._hover_serial += 1
        serial = self._hover_serial

        def maybe_open() -> None:
            if self.hovered and serial == self._hover_serial and self.shown():
                self.show()

        self.desktop.after(self.open_delay, maybe_open)

    def on_press(self, p: Vec2, button: int) -> None:
        if button != 1:
            return
        if self.top_level:
            if self.open:
                self.close()
            else:
                bar = self.parent
                assert isinstance(bar, MenuBar)
                bar.close_all()
                self.show()
        else:
            self.show()


class MenuItem(Widget):
    role = Role.MENU_ITEM

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.visible = False
        self.activated = 0

    def on_enter(self) -> None:
        _close_sibling_menus(self)

    def on_release(self, p: Vec2, button: int, inside: bool) -> None:
        if inside and button == 1:
            self.activated += 1
            self.log("activate")
            self.desktop.close_menus()


def _close_sibling_menus(entry: Widget) -> None:
    if entry.parent is None or isinstance(entry.parent, MenuBar):
        return
    for sib in entry.parent.children:
        if sib is not entry and isinstance(sib, Menu):
            sib.close()


class Draggable(Widget):
    """Something that can be picked up. A ghost surface follows the pointer."""

    role = Role.LIST_ITEM

    def __init__(self, name: str, rect: Rect) -> None:
        super().__init__(name, rect)
        self.dropped_on: str | None = None
        self._offset: Vec2 | None = None
        self._ghost: Widget | None = None

    def on_press(self, p: Vec2, button: int) -> None:
        if button == 1:
            self._offset = p - Vec2(self.rect.x, self.rect.y)

    def on_drag(self, p: Vec2) -> None:
        if self._offset is None:
            return
        if self._ghost is None:
            self._ghost = self.desktop.add(Widget(f"dragging {self.name}", self.rect, 90))
            self._ghost.role = Role.POPUP
        corner = p - self._offset
        self._ghost.rect = Rect(corner.x, corner.y, self.rect.w, self.rect.h)

    def on_release(self, p: Vec2, button: int, inside: bool) -> None:
        if self._offset is None:
            return
        self._offset = None
        if self._ghost is None:
            return
        self.desktop.remove(self._ghost)
        self._ghost = None
        zone = self.desktop.topmost_at(p, lambda w: isinstance(w, DropZone))
        if isinstance(zone, DropZone):
            self.dropped_on = zone.name
            zone.received.append(self.name)
            self.rect = Rect(p.x - self.rect.w / 2, p.y - self.rect.h / 2, self.rect.w, self.rect.h)
            self.log("drop", onto=zone.name)


class DropZone(Widget):
    role = Role.PANEL

    def __init__(self, name: str, rect: Rect) -> None:
        super().__init__(name, rect)
        self.received: list[str] = []


class ScrollList(Widget):
    """A clipped, wheel-scrolled column of rows."""

    role = Role.LIST
    ROW_H = 30.0
    NOTCH = 45.0

    def __init__(self, name: str, rect: Rect, rows: Sequence[str]) -> None:
        super().__init__(name, rect)
        self.offset = 0.0
        self._rows = list(rows)

    def populate(self) -> None:
        for name in self._rows:
            self.add(Button(name, Rect(0, 0, 0, 0))).role = Role.LIST_ITEM
        self._layout()

    def _layout(self) -> None:
        for i, row in enumerate(self.children):
            y = self.rect.top + i * self.ROW_H - self.offset
            row.rect = Rect(self.rect.left, y, self.rect.w, self.ROW_H)
            row.visible = row.rect.intersects(self.rect)

    def on_scroll(self, dy: int) -> bool:
        limit = max(0.0, len(self.children) * self.ROW_H - self.rect.h)
        new = min(max(self.offset + dy * self.NOTCH, 0.0), limit)
        if new != self.offset:
            self.offset = new
            self._layout()
        return True

    def move_by(self, d: Vec2) -> None:
        self.rect = self.rect.translate(d)
        self._layout()


class _Animation:
    def __init__(self, widget: Widget, delta: Vec2, start: float, duration: float, linear: bool) -> None:
        self.widget = widget
        self.delta = delta
        self.start = start
        self.duration = max(duration, 1e-9)
        self.linear = linear
        self.done = Vec2(0.0, 0.0)
        self.finished = False


class SimDesktop:
    def __init__(
        self,
        clock: SimClock | None = None,
        monitors: Sequence[Rect] = (Rect(0, 0, 1920, 1080),),
        pointer: Vec2 = Vec2(960, 540),
        latency: float = 0.0,
    ) -> None:
        self.clock = clock or SimClock()
        self.clock.subscribe(self._advance)
        self._monitors = tuple(monitors)
        self.pointer = pointer
        self.latency = latency
        self.roots: list[Widget] = []
        self.widgets: dict[str, Widget] = {}
        self.focus: Widget | None = None
        self.log: list[tuple[float, str, str, dict]] = []
        self.keys: list[tuple[str, bool]] = []
        self.typed = ""
        self._ids = itertools.count(1)
        self._grab: Widget | None = None
        self._hover: Widget | None = None
        self._down: set[int] = set()
        self._timers: list[tuple[float, int, Callable[[], None]]] = []
        self._timer_seq = itertools.count()
        self._animations: list[_Animation] = []
        self._history: deque[tuple[float, dict[str, UIElement]]] = deque()
        self.moves = 0

    # -- building ---------------------------------------------------------

    def add(self, widget: Widget) -> Widget:
        self.roots.append(widget)
        self._register(widget)
        return widget

    def _register(self, widget: Widget) -> None:
        widget.desktop = self
        if not widget.id:
            n = next(self._ids)
            widget.id = f"{PREFIX}{n}"
            widget.order = n
        self.widgets[widget.id] = widget
        for c in widget.children:
            self._register(c)
        if isinstance(widget, ScrollList) and not widget.children:
            widget.populate()

    def remove(self, widget: Widget) -> None:
        for w in [widget, *widget.descendants()]:
            self.widgets.pop(w.id, None)
            if self._hover is w:
                self._hover = None
            if self._grab is w:
                self._grab = None
        if widget.parent is not None:
            widget.parent.children.remove(widget)
        elif widget in self.roots:
            self.roots.remove(widget)

    def find(self, name: str) -> Widget:
        for w in self.widgets.values():
            if w.name == name:
                return w
        raise KeyError(name)

    # -- scripting ---------------------------------------------------------

    def at(self, t: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._timers, (t, next(self._timer_seq), fn))

    def after(self, delay: float, fn: Callable[[], None]) -> None:
        self.at(self.clock.now() + delay, fn)

    def animate(self, widget: Widget, delta: Vec2, duration: float, linear: bool = False) -> None:
        """Slide ``widget`` by ``delta`` over ``duration`` seconds, eased unless ``linear``."""
        self._animations.append(_Animation(widget, delta, self.clock.now(), duration, linear))

    def bump_pointer(self, dx: float, dy: float) -> None:
        """A person moves the mouse, behind the actuator's back."""
        self.pointer = self._clamp(self.pointer + Vec2(dx, dy))
        self._pointer_moved()

    def close_menus(self) -> None:
        for w in list(self.widgets.values()):
            if isinstance(w, Menu) and w.top_level:
                w.close()

    def events(self, kind: str, name: str | None = None) -> list[tuple[float, str, str, dict]]:
        return [e for e in self.log if e[1] == kind and (name is None or e[2] == name)]

    def _advance(self, now: float) -> None:
        while self._timers and self._timers[0][0] <= now + 1e-12:
            _, _, fn = heapq.heappop(self._timers)
            fn()
        if self._animations:
            for anim in self._animations:
                f = min(max((now - anim.start) / anim.duration, 0.0), 1.0)
                want = anim.delta * (f if anim.linear else f * f * (3.0 - 2.0 * f))
                anim.widget.move_by(want - anim.done)
                anim.done = want
                anim.finished = f >= 1.0
            self._animations = [a for a in self._animations if not a.finished]
            self._pointer_moved()
        if self.latency > 0:
            self._history.append((now, {w.id: w.element() for w in self.widgets.values()}))
            while len(self._history) > 2 and self._history[1][0] <= now - self.latency:
                self._history.popleft()

    # -- actuator ----------------------------------------------------------

    def _clamp(self, p: Vec2) -> Vec2:
        return clamp_to_any(p, self._monitors)

    def position(self) -> Vec2:
        return self.pointer

    def move_to(self, x: int, y: int) -> None:
        self.pointer = self._clamp(Vec2(float(x), float(y)))
        self.moves += 1
        self._pointer_moved()

    def topmost_at(self, p: Vec2, accept: Callable[[Widget], bool] | None = None) -> Widget | None:
        best: Widget | None = None
        best_key: tuple[int, int, int] | None = None
        for w in self.widgets.values():
            if not w.shown() or not w.rect.contains(p):
                continue
            if accept is not None and not accept(w):
                continue
            key = (w.layer, w.depth, w.order)
            if best_key is None or key > best_key:
                best, best_key = w, key
        return best

    def _pointer_moved(self) -> None:
        if self._grab is not None:
            self._grab.on_drag(self.pointer)
        top = self.topmost_at(self.pointer, lambda w: not w.name.startswith("dragging "))
        if top is not self._hover:
            if self._hover is not None:
                self._hover.hovered = False
                self._hover.on_leave()
            self._hover = top
            if top is not None:
                top.hovered = True
                top.enters += 1
                self.log.append((self.clock.now(), "enter", top.name, {}))
                top.on_enter()

    def button(self, button: int, down: bool) -> None:
        if down:
            self._down.add(button)
            target = self.topmost_at(self.pointer)
            if not isinstance(target, (Menu, MenuItem)):
                self.close_menus()
            if target is None:
                return
            self._grab = target
            self.log.append((self.clock.now(), "press", target.name, {"at": self.pointer.rounded()}))
            if target.enabled:
                target.on_press(self.pointer, button)
        else:
            self._down.discard(button)
            target, self._grab = self._grab, None
            if target is None:
                return
            inside = target.shown() and target.rect.contains(self.pointer) and self.topmost_at(self.pointer) in (
                target,
                *target.descendants(),
            )
            self.log.append((self.clock.now(), "release", target.name, {"at": self.pointer.rounded()}))
            if target.enabled:
                target.on_release(self.pointer, button, inside)
            self._pointer_moved()

    def scroll(self, dx: int, dy: int) -> None:
        w = self.topmost_at(self.pointer)
        while w is not None:
            if w.on_scroll(dy):
                self._pointer_moved()
                return
            w = w.parent

    def key(self, key: str, down: bool) -> None:
        self.keys.append((key, down))
        held = {k.lower() for k, d in self.keys if d} - {k.lower() for k, d in self.keys if not d}
        if down and key in ("Return", "Enter") and self.focus is not None:
            self.focus.on_text("\n")
        if down and key.lower() == "a" and held & {"ctrl", "control"} and isinstance(self.focus, TextField):
            self.focus.select_all()
        if down and key == "Escape":
            self.close_menus()

    def type_text(self, text: str) -> None:
        self.typed += text
        if self.focus is not None:
            self.focus.on_text(text)

    # -- perception source ---------------------------------------------------

    def owns(self, ident: str) -> bool:
        return ident.startswith(PREFIX)

    def monitors(self) -> Sequence[Rect]:
        return self._monitors

    def wants_snapshot(self) -> bool:
        return False

    def _view(self) -> dict[str, UIElement]:
        if self.latency > 0 and self._history:
            cutoff = self.clock.now() - self.latency
            chosen = self._history[0][1]
            for t, view in self._history:
                if t <= cutoff + 1e-12:
                    chosen = view
                else:
                    break
            return chosen
        return {w.id: w.element() for w in self.widgets.values()}

    def snapshot(self, budget: float | None = None) -> list[UIElement]:
        view = self._view()
        return sorted(view.values(), key=lambda e: int(e.id[len(PREFIX):]))

    def refresh(self, ids: Sequence[str]) -> tuple[list[UIElement], list[str]]:
        if self.latency > 0 and self._history:
            view = self._view()
            return [view[i] for i in ids if i in view], [i for i in ids if i not in view]
        current: list[UIElement] = []
        gone: list[str] = []
        for i in ids:
            w = self.widgets.get(i)
            if w is None:
                gone.append(i)
            else:
                current.append(w.element())
        return current, gone


def make_body(
    desktop: SimDesktop,
    memory: "SpatialMemory | None" = None,
    motor: "MotorConfig | None" = None,
    config: "BodyConfig | None" = None,
    perception_hz: float = 60.0,
    snapshot_hz: float = 10.0,
) -> "Body":
    """Wire a body to a simulated desktop, sharing the desktop's clock."""
    from ..body import Body
    from ..world.model import WorldModel
    from ..world.perception import Perception

    perception = Perception([desktop], WorldModel(), desktop.clock, perception_hz, snapshot_hz)
    perception.prime()
    return Body(desktop, perception, desktop.clock, memory=memory, motor=motor, config=config)
