# SPDX-License-Identifier: AGPL-3.0-or-later
"""AT-SPI backend: the Linux accessibility tree as a perception source.

AT-SPI is the same interface screen readers use. Every accessible
application publishes its widgets with a role, a name, states, screen
coordinates and, for range controls, a live value. Reading it is exact and
cheap compared with looking at pixels, but a full walk of every application
is still tens to hundreds of D-Bus round-trips. So:

* ``snapshot`` walks the tree incrementally under a time budget, skipping
  subtrees that are not showing (except menus, whose hidden entries are kept
  so that the path to a menu item can be worked out without opening it);
* ``refresh`` re-reads just the handful of attended elements, bypassing the
  client-side cache, and is what runs at frame rate;
* accessibility events mark the snapshot stale so the next walk starts at
  once instead of waiting for the periodic one.

AT-SPI knows nothing about stacking, so each element is placed in the X
window stack by geometry and owning process (see ``X11Windows.layer_for``).

Requires PyGObject and the ``Atspi-2.0`` typelib (``gir1.2-atspi-2.0`` on
Debian and Ubuntu).
"""

from __future__ import annotations

import time
from typing import Any, Generator, Sequence

from ..geometry import Rect
from ..world.element import DEFAULT_STATES, Role, State, UIElement
from .x11 import BackendUnavailable, X11Windows

PREFIX = "atspi:"

_ROLES = {
    "push button": Role.BUTTON,
    "button": Role.BUTTON,
    "toggle button": Role.TOGGLE,
    "check box": Role.CHECKBOX,
    "radio button": Role.RADIO,
    "link": Role.LINK,
    "menu bar": Role.MENU_BAR,
    "menu": Role.MENU,
    "popup menu": Role.MENU,
    "menu item": Role.MENU_ITEM,
    "check menu item": Role.MENU_ITEM,
    "radio menu item": Role.MENU_ITEM,
    "tearoff menu item": Role.MENU_ITEM,
    "page tab": Role.TAB,
    "page tab list": Role.TAB_LIST,
    "list": Role.LIST,
    "list box": Role.LIST,
    "list item": Role.LIST_ITEM,
    "tree item": Role.TREE_ITEM,
    "table": Role.TABLE,
    "tree table": Role.TABLE,
    "tree": Role.TABLE,
    "table cell": Role.CELL,
    "table row": Role.LIST_ITEM,
    "combo box": Role.COMBO_BOX,
    "text": Role.TEXT_FIELD,
    "entry": Role.TEXT_FIELD,
    "password text": Role.TEXT_FIELD,
    "editbar": Role.TEXT_FIELD,
    "terminal": Role.TEXT_FIELD,
    "slider": Role.SLIDER,
    "scroll bar": Role.SCROLL_BAR,
    "spin button": Role.SPINNER,
    "progress bar": Role.PROGRESS,
    "level bar": Role.PROGRESS,
    "label": Role.LABEL,
    "static": Role.LABEL,
    "heading": Role.HEADING,
    "image": Role.IMAGE,
    "icon": Role.IMAGE,
    "canvas": Role.CANVAS,
    "drawing area": Role.CANVAS,
    "frame": Role.WINDOW,
    "window": Role.WINDOW,
    "dialog": Role.DIALOG,
    "alert": Role.DIALOG,
    "file chooser": Role.DIALOG,
    "color chooser": Role.DIALOG,
    "font chooser": Role.DIALOG,
    "tool tip": Role.TOOLTIP,
    "tool bar": Role.TOOLBAR,
    "separator": Role.SEPARATOR,
    "document frame": Role.DOCUMENT,
    "document web": Role.DOCUMENT,
    "document text": Role.DOCUMENT,
    "panel": Role.PANEL,
    "filler": Role.PANEL,
    "scroll pane": Role.PANEL,
    "split pane": Role.PANEL,
    "viewport": Role.PANEL,
    "section": Role.PANEL,
    "form": Role.PANEL,
    "grouping": Role.PANEL,
    "layered pane": Role.PANEL,
    "root pane": Role.PANEL,
    "glass pane": Role.PANEL,
}

_VALUED = frozenset({Role.SLIDER, Role.SCROLL_BAR, Role.SPINNER, Role.PROGRESS})
_NEEDS_LABEL = frozenset({Role.SLIDER, Role.TEXT_FIELD, Role.COMBO_BOX, Role.SPINNER, Role.CHECKBOX, Role.RADIO})

# Kept in the model even while hidden, so the way to a menu entry is known
# before the menu has ever been opened.
_KEEP_HIDDEN_CHILDREN = frozenset({Role.MENU_BAR, Role.MENU, Role.COMBO_BOX})

# Drawn in their own pop-up window rather than in the window of their parent.
_FLOATING = frozenset({Role.MENU, Role.MENU_ITEM, Role.TOOLTIP})

_TOP_LEVEL = frozenset({Role.WINDOW, Role.DIALOG})

_STRUCTURE_EVENTS = ("object:children-changed", "window:", "object:state-changed:showing", "object:state-changed:visible")


class _Meta:
    __slots__ = ("acc", "parent", "app", "pid", "layer")

    def __init__(self, acc: Any, parent: str | None, app: str, pid: int, layer: int) -> None:
        self.acc = acc
        self.parent = parent
        self.app = app
        self.pid = pid
        self.layer = layer
        """Layer inherited from the parent when the tree was last walked."""


class AtspiSource:
    def __init__(
        self,
        windows: X11Windows | None = None,
        max_nodes: int = 20000,
        max_children: int = 400,
        call_timeout_ms: int = 300,
    ) -> None:
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi, GLib
        except (ImportError, ValueError) as exc:
            raise BackendUnavailable(
                "AT-SPI bindings not found; install PyGObject and the Atspi-2.0 typelib "
                "(Debian/Ubuntu: python3-gi gir1.2-atspi-2.0)"
            ) from exc
        self._atspi = Atspi
        self._glib = GLib
        self.windows = windows
        self.max_nodes = max_nodes
        self.max_children = max_children
        Atspi.init()
        Atspi.set_timeout(call_timeout_ms, call_timeout_ms)

        S = Atspi.StateType
        self._states = (
            (S.FOCUSED, State.FOCUSED),
            (S.CHECKED, State.CHECKED),
            (S.SELECTED, State.SELECTED),
            (S.EXPANDED, State.EXPANDED),
            (S.PRESSED, State.PRESSED),
            (S.EDITABLE, State.EDITABLE),
            (S.MODAL, State.MODAL),
            (S.ACTIVE, State.ACTIVE),
        )

        self._nodes: dict[str, _Meta] = {}
        self._crawl_gen: Generator[None, None, list[UIElement]] | None = None
        self._dirty = False
        self._window_generation = -1
        self.last_node_count = 0

        # Applications only emit the events some client has asked for, and
        # the client library only keeps its cache current from those events.
        self._listener = Atspi.EventListener.new(self._on_event)
        for name in ("object:state-changed", "object:children-changed", "object:property-change", "window"):
            try:
                self._listener.register(name)
            except Exception:
                pass

    def close(self) -> None:
        for name in ("object:state-changed", "object:children-changed", "object:property-change", "window"):
            try:
                self._listener.deregister(name)
            except Exception:
                pass

    # -- events ------------------------------------------------------------

    def _on_event(self, event: Any) -> None:
        kind = event.type
        if kind.startswith(_STRUCTURE_EVENTS):
            self._dirty = True

    def _pump(self) -> None:
        ctx = self._glib.MainContext.default()
        for _ in range(500):
            if not ctx.pending():
                break
            ctx.iteration(False)

    # -- perception source -------------------------------------------------

    def owns(self, ident: str) -> bool:
        return ident.startswith(PREFIX)

    def monitors(self) -> Sequence[Rect]:
        return self.windows.monitors() if self.windows else ()

    def wants_snapshot(self) -> bool:
        self._pump()
        if self.windows is not None and self.windows.generation != self._window_generation:
            return True
        return self._dirty

    def snapshot(self, budget: float | None = None) -> list[UIElement] | None:
        self._pump()
        if self._crawl_gen is None:
            self._dirty = False
            if self.windows is not None:
                self.windows.windows()
                self._window_generation = self.windows.generation
            self._crawl_gen = self._crawl()
        deadline = None if budget is None else time.monotonic() + budget
        try:
            while True:
                next(self._crawl_gen)
                if deadline is not None and time.monotonic() >= deadline:
                    return None
        except StopIteration as stop:
            self._crawl_gen = None
            return stop.value

    def refresh(self, ids: Sequence[str]) -> tuple[list[UIElement], list[str]]:
        self._pump()
        current: list[UIElement] = []
        gone: list[str] = []
        for ident in ids:
            meta = self._nodes.get(ident)
            el = None
            if meta is not None:
                try:
                    meta.acc.clear_cache_single()
                except Exception:
                    pass
                el = self._read(ident, meta, meta.layer)
            if el is None:
                gone.append(ident)
                self._nodes.pop(ident, None)
            else:
                current.append(el)
        return current, gone

    # -- reading -----------------------------------------------------------

    @staticmethod
    def _ident(acc: Any) -> str:
        return f"{PREFIX}{hash(acc) & 0xFFFFFFFFFFFF:x}"

    def _floating_layer(self, rect: Rect, pid: int, inherited: int) -> int:
        """Layer of the pop-up window an open menu entry is drawn in."""
        if self.windows is None or rect.empty:
            return inherited
        centre = rect.center
        best = inherited
        for w in self.windows.windows():
            if not w.managed and w.layer > best and w.rect.contains(centre) and (not pid or not w.pid or w.pid == pid):
                best = w.layer
        return best

    def _read(self, ident: str, meta: _Meta, layer: int) -> UIElement | None:
        """Translate one accessible. ``None`` if it is gone or unreadable."""
        A = self._atspi
        acc = meta.acc
        try:
            role = _ROLES.get(acc.get_role_name(), Role.UNKNOWN)
            states = acc.get_state_set()
            S = A.StateType
            if states.contains(S.DEFUNCT):
                return None
            showing = states.contains(S.SHOWING) and states.contains(S.VISIBLE)
            name = acc.get_name() or ""

            bounds = Rect(0, 0, 0, 0)
            if showing:
                try:
                    e = acc.get_extents(A.CoordType.SCREEN)
                    if e.width > 0 and e.height > 0 and e.x > -100000 and e.y > -100000:
                        bounds = Rect(e.x, e.y, e.width, e.height)
                except Exception:
                    pass

            out = {State.VISIBLE} if showing and not bounds.empty else set()
            if states.contains(S.ENABLED) or states.contains(S.SENSITIVE):
                out.add(State.ENABLED)
            for native, ours in self._states:
                if states.contains(native):
                    out.add(ours)

            value = lo = hi = None
            if role in _VALUED:
                try:
                    iface = acc.get_value_iface()
                    if iface is not None:
                        value = A.Value.get_current_value(iface)
                        lo = A.Value.get_minimum_value(iface)
                        hi = A.Value.get_maximum_value(iface)
                except Exception:
                    value = lo = hi = None

            text = None
            if role == Role.TEXT_FIELD and showing:
                try:
                    iface = acc.get_text_iface()
                    if iface is not None:
                        n = A.Text.get_character_count(iface)
                        # Called through the interface class: on the object
                        # itself the name resolves to a deprecated accessor.
                        text = A.Text.get_text(iface, 0, min(n, 500)) if n > 0 else ""
                except Exception:
                    text = None  # an unreadable field is still a field

            if not name and role in _NEEDS_LABEL:
                name = self._label(acc)

            if role in _FLOATING and showing:
                layer = self._floating_layer(bounds, meta.pid, layer)
        except Exception:
            return None

        return UIElement(
            id=ident,
            role=role,
            bounds=bounds,
            name=name,
            states=DEFAULT_STATES if out == DEFAULT_STATES else frozenset(out),
            value=value,
            value_min=lo,
            value_max=hi,
            text=text,
            parent=meta.parent,
            app=meta.app,
            layer=layer,
        )

    def _label(self, acc: Any) -> str:
        A = self._atspi
        try:
            for rel in acc.get_relation_set() or ():
                if rel.get_relation_type() == A.RelationType.LABELLED_BY and rel.get_n_targets() > 0:
                    return rel.get_target(0).get_name() or ""
        except Exception:
            pass
        return ""

    # -- walking -----------------------------------------------------------

    def _crawl(self) -> Generator[None, None, list[UIElement]]:
        A = self._atspi
        out: list[UIElement] = []
        nodes: dict[str, _Meta] = {}
        desktop = A.get_desktop(0)
        try:
            n_apps = desktop.get_child_count()
        except Exception:
            n_apps = 0

        for i in range(n_apps):
            try:
                app = desktop.get_child_at_index(i)
                if app is None:
                    continue
                app_name = app.get_name() or ""
                pid = app.get_process_id()
                n_tops = app.get_child_count()
            except Exception:
                continue
            yield

            for j in range(n_tops):
                try:
                    top = app.get_child_at_index(j)
                except Exception:
                    top = None
                if top is None:
                    continue
                # (accessible, parent id, inherited layer, keep even if hidden)
                stack: list[tuple[Any, str | None, int, bool]] = [(top, None, 0, False)]
                while stack and len(out) < self.max_nodes:
                    acc, parent, layer, keep_hidden = stack.pop()
                    ident = self._ident(acc)
                    is_top = parent is None
                    meta = _Meta(acc, parent, app_name, pid, layer)
                    el = self._read(ident, meta, layer)
                    if el is None:
                        continue
                    if is_top and self.windows is not None and el.visible:
                        meta.layer = self.windows.layer_for(el.bounds, pid)
                        el = el.with_(layer=meta.layer)
                    if not el.visible and not keep_hidden:
                        if not is_top:
                            continue
                    nodes[ident] = meta
                    out.append(el)
                    if len(out) % 24 == 0:
                        yield

                    descend_hidden = el.role in _KEEP_HIDDEN_CHILDREN
                    if not el.visible and not descend_hidden:
                        continue
                    try:
                        count = min(acc.get_child_count(), self.max_children)
                    except Exception:
                        continue
                    kids = []
                    for k in range(count):
                        try:
                            child = acc.get_child_at_index(k)
                        except Exception:
                            child = None
                        if child is not None:
                            kids.append((child, ident, el.layer, descend_hidden))
                    stack.extend(reversed(kids))
                yield

        self._nodes = nodes
        self.last_node_count = len(out)
        return out
