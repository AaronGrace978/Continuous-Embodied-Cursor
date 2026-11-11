# SPDX-License-Identifier: GPL-3.0-or-later
"""X11 backend: pointer actuation and the window stack.

Talks to the X server through ``ctypes`` bindings to libX11, libXtst,
libXrandr and libXext, so there is nothing to install beyond the libraries
every X desktop already has.

* :class:`X11Actuator` injects pointer and key events with the XTEST
  extension and reads the true pointer position back with ``XQueryPointer``.
* :class:`X11Windows` is a perception source for the top-level window stack:
  every mapped window's rectangle and stacking order, including the
  override-redirect popups (menus, tooltips, drag ghosts) that window
  managers do not list. It is event driven, so an unchanged desktop costs no
  server round-trips per frame.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_short, c_uint, c_ulong, c_ushort, c_void_p
from dataclasses import dataclass
from typing import Sequence

from ..geometry import Rect, Vec2
from ..world.element import DEFAULT_STATES, Role, UIElement

PREFIX = "x11:"

_IS_VIEWABLE = 2
_INPUT_ONLY = 2
_SUBSTRUCTURE_NOTIFY = 1 << 19
_PROPERTY_CHANGE = 1 << 22
_SHAPE_INPUT = 2
_ANY_PROPERTY_TYPE = 0


class BackendUnavailable(RuntimeError):
    pass


class _XWindowAttributes(Structure):
    _fields_ = [
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("border_width", c_int),
        ("depth", c_int),
        ("visual", c_void_p),
        ("root", c_ulong),
        ("c_class", c_int),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("colormap", c_ulong),
        ("map_installed", c_int),
        ("map_state", c_int),
        ("all_event_masks", c_long),
        ("your_event_mask", c_long),
        ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int),
        ("screen", c_void_p),
    ]


class _XRRMonitorInfo(Structure):
    _fields_ = [
        ("name", c_ulong),
        ("primary", c_int),
        ("automatic", c_int),
        ("noutput", c_int),
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("mwidth", c_int),
        ("mheight", c_int),
        ("outputs", c_void_p),
    ]


class _XRectangle(Structure):
    _fields_ = [("x", c_short), ("y", c_short), ("width", c_ushort), ("height", c_ushort)]


_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)


def _ignore_x_error(_display: int, _event: int) -> int:
    # Windows vanish between a listing and a query all the time. Xlib's
    # default handler would terminate the process on the resulting BadWindow.
    return 0


_handler_ref = _ERROR_HANDLER(_ignore_x_error)
_libs_lock = threading.Lock()
_libs: "_Libs | None" = None


class _Libs:
    def __init__(self) -> None:
        def load(name: str, required: bool = True) -> ctypes.CDLL | None:
            path = ctypes.util.find_library(name)
            if path is None:
                if required:
                    raise BackendUnavailable(f"lib{name} is not installed")
                return None
            return ctypes.CDLL(path)

        x = load("X11")
        assert x is not None
        self.x = x
        self.xtst = load("Xtst")
        self.xrandr = load("Xrandr", required=False)
        self.xext = load("Xext", required=False)

        x.XInitThreads()
        x.XOpenDisplay.argtypes = [c_char_p]
        x.XOpenDisplay.restype = c_void_p
        x.XCloseDisplay.argtypes = [c_void_p]
        x.XDefaultRootWindow.argtypes = [c_void_p]
        x.XDefaultRootWindow.restype = c_ulong
        x.XFlush.argtypes = [c_void_p]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XPending.argtypes = [c_void_p]
        x.XNextEvent.argtypes = [c_void_p, c_void_p]
        x.XSelectInput.argtypes = [c_void_p, c_ulong, c_long]
        x.XFree.argtypes = [c_void_p]
        x.XQueryPointer.argtypes = [
            c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
            POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_uint),
        ]
        x.XQueryPointer.restype = c_int
        x.XQueryTree.argtypes = [
            c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong), POINTER(POINTER(c_ulong)), POINTER(c_uint),
        ]
        x.XQueryTree.restype = c_int
        x.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, POINTER(_XWindowAttributes)]
        x.XGetWindowAttributes.restype = c_int
        x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        x.XInternAtom.restype = c_ulong
        x.XGetWindowProperty.argtypes = [
            c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
            POINTER(c_ulong), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p),
        ]
        x.XGetWindowProperty.restype = c_int
        x.XStringToKeysym.argtypes = [c_char_p]
        x.XStringToKeysym.restype = c_ulong
        x.XDisplayKeycodes.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        x.XGetKeyboardMapping.argtypes = [c_void_p, c_uint, c_int, POINTER(c_int)]
        x.XGetKeyboardMapping.restype = POINTER(c_ulong)
        x.XChangeKeyboardMapping.argtypes = [c_void_p, c_int, c_int, POINTER(c_ulong), c_int]
        x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
        x.XSetErrorHandler.restype = c_void_p
        x.XSetErrorHandler(_handler_ref)

        t = self.xtst
        assert t is not None
        t.XTestQueryExtension.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int)]
        t.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
        t.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
        t.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]

        if self.xrandr is not None:
            self.xrandr.XRRGetMonitors.argtypes = [c_void_p, c_ulong, c_int, POINTER(c_int)]
            self.xrandr.XRRGetMonitors.restype = POINTER(_XRRMonitorInfo)
            self.xrandr.XRRFreeMonitors.argtypes = [POINTER(_XRRMonitorInfo)]
        if self.xext is not None:
            self.xext.XShapeGetRectangles.argtypes = [c_void_p, c_ulong, c_int, POINTER(c_int), POINTER(c_int)]
            self.xext.XShapeGetRectangles.restype = POINTER(_XRectangle)
            self.xext.XShapeQueryExtension.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]


def _load() -> _Libs:
    global _libs
    with _libs_lock:
        if _libs is None:
            _libs = _Libs()
        return _libs


class _Connection:
    """One X display connection, used by one component under its own lock."""

    def __init__(self, display: str | None = None) -> None:
        self.libs = _load()
        name = display or os.environ.get("DISPLAY")
        if not name:
            raise BackendUnavailable("DISPLAY is not set; no X server to talk to")
        self.dpy = self.libs.x.XOpenDisplay(name.encode())
        if not self.dpy:
            raise BackendUnavailable(f"cannot open X display {name!r}")
        self.root = self.libs.x.XDefaultRootWindow(self.dpy)
        self.lock = threading.RLock()
        self._atoms: dict[str, int] = {}

    def close(self) -> None:
        with self.lock:
            if self.dpy:
                self.libs.x.XCloseDisplay(self.dpy)
                self.dpy = None

    def atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            a = self._atoms[name] = self.libs.x.XInternAtom(self.dpy, name.encode(), 0)
        return a

    def property(self, window: int, name: str, max_longs: int = 4096) -> tuple[int, bytes | list[int]] | None:
        """Read a window property: ``(format, value)``; lists for 32-bit formats."""
        actual_type, fmt = c_ulong(), c_int()
        nitems, after = c_ulong(), c_ulong()
        data = c_void_p()
        status = self.libs.x.XGetWindowProperty(
            self.dpy, window, self.atom(name), 0, max_longs, 0, _ANY_PROPERTY_TYPE,
            byref(actual_type), byref(fmt), byref(nitems), byref(after), byref(data),
        )
        if status != 0 or not data.value:
            return None
        try:
            n = nitems.value
            if fmt.value == 32:
                # Xlib hands 32-bit items back as sign-extended longs.
                return 32, [v & 0xFFFFFFFF for v in ctypes.cast(data, POINTER(c_ulong))[:n]]
            if fmt.value == 8:
                return 8, ctypes.string_at(data.value, n)
            return None
        finally:
            self.libs.x.XFree(data)

    def monitors(self) -> list[Rect]:
        x = self.libs
        with self.lock:
            if x.xrandr is not None:
                n = c_int()
                info = x.xrandr.XRRGetMonitors(self.dpy, self.root, 1, byref(n))
                if info:
                    try:
                        rects = [Rect(m.x, m.y, m.width, m.height) for m in info[: n.value]]
                    finally:
                        x.xrandr.XRRFreeMonitors(info)
                    if rects:
                        return rects
            attrs = _XWindowAttributes()
            x.x.XGetWindowAttributes(self.dpy, self.root, byref(attrs))
            return [Rect(0, 0, attrs.width, attrs.height)]


# -- actuator ----------------------------------------------------------------

_KEY_ALIASES = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "option": "Alt_L",
    "super": "Super_L",
    "meta": "Super_L",
    "cmd": "Super_L",
    "win": "Super_L",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


def _char_keysym(ch: str) -> int:
    cp = ord(ch)
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    if ch == "\b":
        return 0xFF08
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


class X11Actuator:
    """Moves the real pointer and presses real keys through XTEST."""

    def __init__(self, display: str | None = None) -> None:
        self._c = _Connection(display)
        libs = self._c.libs
        a, b, c, d = c_int(), c_int(), c_int(), c_int()
        if not libs.xtst.XTestQueryExtension(self._c.dpy, byref(a), byref(b), byref(c), byref(d)):
            raise BackendUnavailable("the X server does not support the XTEST extension")
        self._keymap: dict[int, tuple[int, bool]] = {}
        self._spare: list[int] = []
        self._borrowed: dict[int, int] = {}
        self._per_code = 0
        self._load_keymap()

    def close(self) -> None:
        self._restore_keys()
        self._c.close()

    # pointer

    def position(self) -> Vec2:
        c = self._c
        root, child = c_ulong(), c_ulong()
        rx, ry, wx, wy = c_int(), c_int(), c_int(), c_int()
        mask = c_uint()
        with c.lock:
            c.libs.x.XQueryPointer(c.dpy, c.root, byref(root), byref(child), byref(rx), byref(ry), byref(wx), byref(wy), byref(mask))
        return Vec2(float(rx.value), float(ry.value))

    def move_to(self, x: int, y: int) -> None:
        c = self._c
        with c.lock:
            c.libs.xtst.XTestFakeMotionEvent(c.dpy, -1, int(x), int(y), 0)
            c.libs.x.XFlush(c.dpy)

    def button(self, button: int, down: bool) -> None:
        c = self._c
        with c.lock:
            c.libs.xtst.XTestFakeButtonEvent(c.dpy, button, 1 if down else 0, 0)
            c.libs.x.XFlush(c.dpy)

    def scroll(self, dx: int, dy: int) -> None:
        c = self._c
        with c.lock:
            for amount, negative, positive in ((dy, 4, 5), (dx, 6, 7)):
                btn = positive if amount > 0 else negative
                for _ in range(abs(amount)):
                    c.libs.xtst.XTestFakeButtonEvent(c.dpy, btn, 1, 0)
                    c.libs.xtst.XTestFakeButtonEvent(c.dpy, btn, 0, 0)
            c.libs.x.XFlush(c.dpy)

    # keyboard

    def _load_keymap(self) -> None:
        c = self._c
        lo, hi, per = c_int(), c_int(), c_int()
        with c.lock:
            c.libs.x.XDisplayKeycodes(c.dpy, byref(lo), byref(hi))
            count = hi.value - lo.value + 1
            syms = c.libs.x.XGetKeyboardMapping(c.dpy, lo.value, count, byref(per))
            self._keymap.clear()
            self._spare = []
            self._per_code = per.value
            for i in range(count):
                row = [syms[i * per.value + j] for j in range(per.value)]
                code = lo.value + i
                if not any(row):
                    self._spare.append(code)
                    continue
                for level in (0, 1):
                    if level < len(row) and row[level] and row[level] not in self._keymap:
                        self._keymap[row[level]] = (code, level == 1)
            c.libs.x.XFree(syms)

    def _keycode(self, keysym: int) -> tuple[int, bool]:
        hit = self._keymap.get(keysym)
        if hit is not None:
            return hit
        # The layout has no key for this symbol: lend it an unused keycode.
        if not self._spare:
            raise ValueError(f"no free keycode to type keysym {keysym:#x}")
        code = self._spare.pop()
        c = self._c
        with c.lock:
            sym = (c_ulong * 1)(keysym)
            c.libs.x.XChangeKeyboardMapping(c.dpy, code, 1, sym, 1)
            c.libs.x.XSync(c.dpy, 0)
        self._borrowed[code] = keysym
        self._keymap[keysym] = (code, False)
        # Clients re-read the keyboard map asynchronously.
        time.sleep(0.05)
        return code, False

    def _restore_keys(self) -> None:
        c = self._c
        if not self._borrowed or not c.dpy:
            return
        with c.lock:
            for code in self._borrowed:
                sym = (c_ulong * 1)(0)
                c.libs.x.XChangeKeyboardMapping(c.dpy, code, 1, sym, 1)
            c.libs.x.XSync(c.dpy, 0)
        self._borrowed.clear()

    def _named_keysym(self, key: str) -> int:
        name = _KEY_ALIASES.get(key.lower(), key)
        if len(name) == 1:
            return _char_keysym(name)
        sym = self._c.libs.x.XStringToKeysym(name.encode())
        if not sym:
            raise ValueError(f"unknown key {key!r}")
        return sym

    def _fake_key(self, code: int, down: bool) -> None:
        c = self._c
        c.libs.xtst.XTestFakeKeyEvent(c.dpy, code, 1 if down else 0, 0)

    def key(self, key: str, down: bool) -> None:
        code, _shift = self._keycode(self._named_keysym(key))
        c = self._c
        with c.lock:
            self._fake_key(code, down)
            c.libs.x.XFlush(c.dpy)

    def type_text(self, text: str) -> None:
        c = self._c
        shift_code = self._keymap.get(0xFFE1, (None, False))[0]
        for ch in text:
            code, shift = self._keycode(_char_keysym(ch))
            with c.lock:
                if shift and shift_code:
                    self._fake_key(shift_code, True)
                self._fake_key(code, True)
                self._fake_key(code, False)
                if shift and shift_code:
                    self._fake_key(shift_code, False)
                c.libs.x.XFlush(c.dpy)

    def monitors(self) -> list[Rect]:
        return self._c.monitors()


# -- window stack --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class XWindow:
    wid: int
    """The top-level window as stacked under the root (a WM frame, usually)."""

    client: int
    """The application's own window inside the frame, or ``wid`` itself."""

    rect: Rect
    layer: int
    """1-based position in the stack; higher is closer to the viewer."""

    pid: int
    title: str
    managed: bool
    """False for override-redirect windows: menus, tooltips, drag ghosts."""


class X11Windows:
    """Perception source for the stack of top-level windows."""

    def __init__(self, display: str | None = None) -> None:
        self._c = _Connection(display)
        c = self._c
        with c.lock:
            c.libs.x.XSelectInput(c.dpy, c.root, _SUBSTRUCTURE_NOTIFY | _PROPERTY_CHANGE)
            c.libs.x.XFlush(c.dpy)
        self._event = ctypes.create_string_buffer(192)
        ext = c.libs.xext
        a, b = c_int(), c_int()
        self._has_shape = bool(ext is not None and ext.XShapeQueryExtension(c.dpy, byref(a), byref(b)))
        self._dirty = True
        self._monitors = c.monitors()
        self._windows: list[XWindow] = []
        self._frame_of: dict[int, int] = {}
        self.generation = 0
        """Bumped whenever the set, geometry or order of windows changes."""

    def close(self) -> None:
        self._c.close()

    def owns(self, ident: str) -> bool:
        return ident.startswith(PREFIX)

    def monitors(self) -> Sequence[Rect]:
        self._monitors = self._c.monitors()
        return self._monitors

    def wants_snapshot(self) -> bool:
        self._drain()
        return self._dirty

    def _drain(self) -> None:
        c = self._c
        with c.lock:
            while c.libs.x.XPending(c.dpy) > 0:
                c.libs.x.XNextEvent(c.dpy, self._event)
                self._dirty = True

    # -- listing -----------------------------------------------------------

    def windows(self) -> list[XWindow]:
        """The current stack, bottom first. Re-read only if something changed."""
        self._drain()
        if self._dirty:
            self._dirty = False
            fresh = self._list()
            if fresh != self._windows:
                self._windows = fresh
                self.generation += 1
        return self._windows

    def _top_level_of(self, client: int) -> int:
        """Walk up from a client window to its ancestor directly under the root."""
        c = self._c
        w = client
        for _ in range(8):
            root, parent = c_ulong(), c_ulong()
            kids = POINTER(c_ulong)()
            n = c_uint()
            if not c.libs.x.XQueryTree(c.dpy, w, byref(root), byref(parent), byref(kids), byref(n)):
                return 0
            if kids:
                c.libs.x.XFree(kids)
            if parent.value in (0, c.root):
                return w
            w = parent.value
        return 0

    def _list(self) -> list[XWindow]:
        c = self._c
        x = c.libs.x
        with c.lock:
            root, parent = c_ulong(), c_ulong()
            kids = POINTER(c_ulong)()
            n = c_uint()
            if not x.XQueryTree(c.dpy, c.root, byref(root), byref(parent), byref(kids), byref(n)):
                return []
            stack = list(kids[: n.value])
            if kids:
                x.XFree(kids)

            clients_prop = c.property(c.root, "_NET_CLIENT_LIST")
            clients = clients_prop[1] if clients_prop and clients_prop[0] == 32 else []
            assert isinstance(clients, list)
            current = c.property(c.root, "_NET_CURRENT_DESKTOP")
            desktop = current[1][0] if current and current[0] == 32 and current[1] else None

            frames: dict[int, int] = {}
            for client in clients:
                frame = self._frame_of.get(client)
                if frame is None or frame not in stack:
                    frame = self._top_level_of(client)
                if frame:
                    frames[frame] = client
            self._frame_of = {client: frame for frame, client in frames.items()}

            out: list[XWindow] = []
            screens = self._monitors
            attrs = _XWindowAttributes()
            for wid in stack:
                if not x.XGetWindowAttributes(c.dpy, wid, byref(attrs)):
                    continue
                if attrs.map_state != _IS_VIEWABLE or attrs.c_class == _INPUT_ONLY:
                    continue
                rect = Rect(attrs.x, attrs.y, attrs.width + 2 * attrs.border_width, attrs.height + 2 * attrs.border_width)
                if rect.w < 2 or rect.h < 2 or not any(rect.intersects(m) for m in screens):
                    continue
                client = frames.get(wid, wid)
                managed = wid in frames
                if not managed and not attrs.override_redirect:
                    # Neither a client nor a popup: WM scaffolding.
                    continue
                if managed and desktop is not None:
                    on = c.property(client, "_NET_WM_DESKTOP")
                    if on and on[0] == 32 and on[1] and on[1][0] not in (desktop, 0xFFFFFFFF):
                        continue
                rect = self._input_bounds(wid, rect)
                if rect is None:
                    continue
                out.append(
                    XWindow(wid, client, rect, len(out) + 1, self._pid(client), self._title(client), managed)
                )
            return out

    def _input_bounds(self, wid: int, rect: Rect) -> Rect | None:
        """Shrink ``rect`` to the part of the window that takes pointer input.

        Docks and overlays often own a large, mostly click-through window.
        Returns ``None`` when the window takes no pointer input at all.
        """
        ext = self._c.libs.xext
        if ext is None or not self._has_shape:
            return rect
        count, ordering = c_int(), c_int()
        rects = ext.XShapeGetRectangles(self._c.dpy, wid, _SHAPE_INPUT, byref(count), byref(ordering))
        if not rects:
            # NULL means an empty input region, or a window that has just gone.
            return None
        try:
            if count.value == 0:
                return None
            left = min(r.x for r in rects[: count.value])
            top = min(r.y for r in rects[: count.value])
            right = max(r.x + r.width for r in rects[: count.value])
            bottom = max(r.y + r.height for r in rects[: count.value])
        finally:
            self._c.libs.x.XFree(rects)
        shaped = Rect.from_ltrb(rect.x + left, rect.y + top, rect.x + right, rect.y + bottom)
        clipped = shaped.intersection(rect)
        return clipped if not clipped.empty else None

    def _pid(self, wid: int) -> int:
        p = self._c.property(wid, "_NET_WM_PID")
        if p and p[0] == 32 and p[1]:
            return int(p[1][0])  # type: ignore[index]
        return 0

    def _title(self, wid: int) -> str:
        for name in ("_NET_WM_NAME", "WM_NAME"):
            p = self._c.property(wid, name)
            if p and p[0] == 8:
                assert isinstance(p[1], bytes)
                return p[1].decode("utf-8", "replace")
        return ""

    # -- perception source -------------------------------------------------

    def snapshot(self, budget: float | None = None) -> list[UIElement]:
        return [self._element(w) for w in self.windows()]

    def refresh(self, ids: Sequence[str]) -> tuple[list[UIElement], list[str]]:
        by_id = {f"{PREFIX}{w.wid}": w for w in self.windows()}
        return [self._element(by_id[i]) for i in ids if i in by_id], [i for i in ids if i not in by_id]

    @staticmethod
    def _element(w: XWindow) -> UIElement:
        return UIElement(
            id=f"{PREFIX}{w.wid}",
            role=Role.WINDOW if w.managed else Role.POPUP,
            bounds=w.rect,
            name=w.title,
            states=DEFAULT_STATES,
            layer=w.layer,
            app=f"pid:{w.pid}" if w.pid else "",
        )

    def layer_for(self, rect: Rect, pid: int = 0) -> int:
        """Stacking layer of the window that best matches ``rect``.

        Used to place accessibility elements, which carry no z-order, into the
        window stack. Prefers a window of the same process; falls back to the
        topmost window containing the rectangle's centre.
        """
        best_layer, best_score = 0, 0.0
        for w in self.windows():
            overlap = w.rect.intersection(rect).area
            if overlap <= 0:
                continue
            score = overlap / max(rect.area, 1.0) + overlap / max(w.rect.area, 1.0)
            if pid and w.pid == pid:
                score += 2.0
            if score >= best_score:
                best_layer, best_score = w.layer, score
        return best_layer
