# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Win32 input and UI Automation perception. No X server required."""
from __future__ import annotations

import ctypes as C
import os
import sys
from ctypes import wintypes as W
from pathlib import Path

from ..geometry import Rect, Vec2
from ..world.element import UIElement, State
from .x11 import BackendUnavailable

# Fixed-width Win32 types also allow ABI tests on non-Windows hosts.
DWORD, LONG, WORD = C.c_uint32, C.c_int32, C.c_uint16
ULONG_PTR = C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', LONG), ('dy', LONG), ('mouseData', DWORD),
                ('dwFlags', DWORD), ('time', DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', WORD), ('wScan', WORD), ('dwFlags', DWORD),
                ('time', DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [('uMsg', DWORD), ('wParamL', WORD), ('wParamH', WORD)]


class _Payload(C.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ('payload',)
    _fields_ = [('type', DWORD), ('payload', _Payload)]


class POINT(C.Structure):
    _fields_ = [('x', LONG), ('y', LONG)]


class RECT(C.Structure):
    _fields_ = [('left', LONG), ('top', LONG), ('right', LONG), ('bottom', LONG)]


def _user32():
    if sys.platform != 'win32':
        raise BackendUnavailable('the Windows backend requires Windows')
    api = C.WinDLL('user32', use_last_error=True)
    api.GetCursorPos.argtypes = [C.POINTER(POINT)]
    api.GetCursorPos.restype = W.BOOL
    api.SetCursorPos.argtypes = [C.c_int, C.c_int]
    api.SetCursorPos.restype = W.BOOL
    api.SendInput.argtypes = [C.c_uint, C.POINTER(INPUT), C.c_int]
    api.SendInput.restype = C.c_uint
    api.GetSystemMetrics.argtypes = [C.c_int]
    api.GetSystemMetrics.restype = C.c_int
    api.VkKeyScanW.argtypes = [W.WCHAR]
    api.VkKeyScanW.restype = C.c_int16
    api.GetTopWindow.argtypes = [W.HWND]
    api.GetTopWindow.restype = W.HWND
    api.GetWindow.argtypes = [W.HWND, C.c_uint]
    api.GetWindow.restype = W.HWND
    api.GetAncestor.argtypes = [W.HWND, C.c_uint]
    api.GetAncestor.restype = W.HWND
    api.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]
    api.SetThreadDpiAwarenessContext.restype = C.c_void_p
    return api


def _dpi(api):
    # Thread-local context avoids changing a host application's process policy.
    return api.SetThreadDpiAwarenessContext(C.c_void_p(-4))


_KEYS = {'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10,
         'super': 0x5b, 'win': 0x5b, 'meta': 0x5b, 'return': 0x0d,
         'enter': 0x0d, 'escape': 0x1b, 'esc': 0x1b, 'tab': 9,
         'space': 0x20, 'backspace': 8, 'delete': 0x2e, 'insert': 0x2d,
         'home': 0x24, 'end': 0x23, 'page_up': 0x21, 'page_down': 0x22,
         'pageup': 0x21, 'pagedown': 0x22, 'left': 0x25, 'up': 0x26,
         'right': 0x27, 'down': 0x28, 'caps_lock': 0x14}
_KEYS.update({f'f{i}': 0x6f + i for i in range(1, 25)})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2d, 0x2e, 0x5b}


class WindowsActuator:
    def __init__(self):
        self.api = _user32()
        self._old_dpi = _dpi(self.api)

    def _send(self, event):
        if self.api.SendInput(1, C.byref(event), C.sizeof(INPUT)) != 1:
            raise BackendUnavailable('SendInput failed; Windows blocks input into elevated apps from a non-elevated process (UIPI)')

    def position(self):
        p = POINT()
        if not self.api.GetCursorPos(C.byref(p)):
            raise BackendUnavailable('GetCursorPos failed; an interactive desktop is required')
        return Vec2(p.x, p.y)

    def move_to(self, x, y):
        if not self.api.SetCursorPos(round(x), round(y)):
            raise BackendUnavailable('SetCursorPos failed; an interactive desktop is required')

    def button(self, button, down):
        flags = {1: (0x2, 0x4), 2: (0x20, 0x40), 3: (0x8, 0x10)}
        if button not in flags:
            raise ValueError(f'unsupported mouse button: {button}')
        self._send(INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags[button][not down])))

    def scroll(self, dx, dy):
        for amount, flag in ((-dy, 0x800), (dx, 0x1000)):
            if amount:
                self._send(INPUT(type=0, mi=MOUSEINPUT(mouseData=int(amount * 120) & 0xffffffff, dwFlags=flag)))

    def key(self, key, down):
        vk = _KEYS.get(key.lower())
        if vk is None and len(key) == 1:
            # Chord modifiers are supplied separately by the caller.
            mapped = self.api.VkKeyScanW(key)
            if mapped != -1:
                vk = mapped & 0xff
        if vk is None:
            raise ValueError(f'unknown Windows key: {key}')
        flags = (0 if down else 2) | (1 if vk in _EXTENDED else 0)
        self._send(INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))

    def type_text(self, text):
        encoded = text.encode('utf-16-le')
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset:offset + 2], 'little')
            for flags in (4, 6):
                self._send(INPUT(type=1, ki=KEYBDINPUT(wScan=unit, dwFlags=flags)))

    def close(self):
        if self._old_dpi:
            self.api.SetThreadDpiAwarenessContext(self._old_dpi)


_ROLES = dict(enumerate([
    'button', 'calendar', 'checkbox', 'combo_box', 'text_field', 'link',
    'image', 'list_item', 'list', 'menu', 'menu_bar', 'menu_item',
    'progress', 'radio', 'scroll_bar', 'slider', 'spinner', 'status_bar',
    'tab', 'tab_list', 'label', 'toolbar', 'tooltip', 'tree', 'tree_item',
    'unknown', 'group', 'thumb', 'table', 'cell', 'document', 'window',
    'panel', 'header', 'heading', 'table', 'title_bar', 'separator'], 50000))


class WindowsSource:
    """UIA is created on the perception thread, which owns its COM apartment."""
    def __init__(self):
        try:
            import comtypes.client
        except ImportError as exc:
            raise BackendUnavailable('Windows perception requires: pip install -e ".[windows]"') from exc
        self._client = comtypes.client
        self._uia = None
        self._elements = {}
        self._pending = None

    def _connect(self):
        if self._uia is None:
            import comtypes
            comtypes.CoInitializeEx(0)
            self._client.GetModule('UIAutomationCore.dll')
            from comtypes.gen import UIAutomationClient as UIA
            self._uia = self._client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
            self._walker = self._uia.ControlViewWalker
            self._api = _user32()
            self._old_dpi = _dpi(self._api)

    def owns(self, ident):
        return ident.startswith('uia:')

    def monitors(self):
        api = _user32()
        old = _dpi(api)
        result = []
        callback_type = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(W.BOOL, W.HANDLE, W.HDC, C.POINTER(RECT), W.LPARAM)
        def visit(_monitor, _dc, rect, _data):
            r = rect.contents
            result.append(Rect(r.left, r.top, r.right-r.left, r.bottom-r.top))
            return True
        callback = callback_type(visit)
        api.EnumDisplayMonitors.argtypes = [W.HDC, C.POINTER(RECT), callback_type, W.LPARAM]
        api.EnumDisplayMonitors.restype = W.BOOL
        try:
            if not api.EnumDisplayMonitors(None, None, callback, 0):
                raise BackendUnavailable('EnumDisplayMonitors failed')
        finally:
            if old:
                api.SetThreadDpiAwarenessContext(old)
        return result

    def _read(self, el, parent=None, layer=0):
        rid = el.GetRuntimeId()
        ident = 'uia:' + '.'.join(str(n) for n in rid)
        r = el.CurrentBoundingRectangle
        states = set()
        if not el.CurrentIsOffscreen:
            states.add(State.VISIBLE)
        if el.CurrentIsEnabled:
            states.add(State.ENABLED)
        if el.CurrentHasKeyboardFocus:
            states.add(State.FOCUSED)
        value = low = high = None
        try:
            from comtypes.gen import UIAutomationClient as UIA
            pattern = el.GetCurrentPattern(10003)
            if pattern:
                pattern = pattern.QueryInterface(UIA.IUIAutomationRangeValuePattern)
                value, low, high = pattern.CurrentValue, pattern.CurrentMinimum, pattern.CurrentMaximum
        except Exception:
            pass  # The element need not support RangeValue.
        try:
            hwnd = el.CurrentNativeWindowHandle
            if hwnd:
                root = self._api.GetAncestor(hwnd, 2) or hwnd
                layer = self._layers.get(root, layer)
            elif parent in self._elements:
                layer = self._elements[parent][2]
        except Exception:
            pass
        text = None
        try:
            from comtypes.gen import UIAutomationClient as UIA
            for pattern_id, interface, attribute, state, expected in (
                (10015, UIA.IUIAutomationTogglePattern, 'CurrentToggleState', State.CHECKED, 1),
                (10010, UIA.IUIAutomationSelectionItemPattern, 'CurrentIsSelected', State.SELECTED, True),
                (10005, UIA.IUIAutomationExpandCollapsePattern, 'CurrentExpandCollapseState', State.EXPANDED, 1),
            ):
                try:
                    pattern = el.GetCurrentPattern(pattern_id)
                    if pattern and getattr(pattern.QueryInterface(interface), attribute) == expected:
                        states.add(state)
                except Exception:
                    pass
            if not el.CurrentIsPassword:
                pattern = el.GetCurrentPattern(10002)
                if pattern:
                    pattern = pattern.QueryInterface(UIA.IUIAutomationValuePattern)
                    text = pattern.CurrentValue
                    if not pattern.CurrentIsReadOnly:
                        states.add(State.EDITABLE)
        except Exception:
            pass
        self._elements[ident] = (el, parent, layer)
        return UIElement(ident, _ROLES.get(el.CurrentControlType, 'unknown'),
                         Rect(r.left, r.top, r.right-r.left, r.bottom-r.top),
                         name=el.CurrentName or '', states=frozenset(states),
                         parent=parent, layer=layer, value=value, value_min=low, value_max=high, text=text)

    def snapshot(self, budget=None):
        import time
        self._connect()
        if self._pending is None:
            stack = []
            hwnd = self._api.GetTopWindow(None)
            while hwnd and hwnd not in stack:
                stack.append(hwnd)
                hwnd = self._api.GetWindow(hwnd, 2)  # GW_HWNDNEXT, front to back
            self._layers = {hwnd: len(stack) - i for i, hwnd in enumerate(stack)}
            self._pending = [(self._uia.GetRootElement(), None, 0)]
            self._snapshot = []
        deadline = time.monotonic() + budget if budget is not None else float('inf')
        while self._pending:
            el, parent, layer = self._pending.pop()
            try:
                item = self._read(el, parent, layer)
                self._snapshot.append(item)
                children = []
                child = self._walker.GetFirstChildElement(el)
                while child:
                    children.append((child, item.id, layer + 1))
                    child = self._walker.GetNextSiblingElement(child)
                self._pending.extend(reversed(children))
            except Exception:
                if parent is None:
                    self._pending = None
                    raise  # A broken desktop root is not an empty successful snapshot.
                # UIA elements can vanish while walking.
            if self._pending and time.monotonic() >= deadline:
                return None
        self._pending = None
        result = self._snapshot
        live = {el.id for el in result}
        self._elements = {k: v for k, v in self._elements.items() if k in live}
        return result

    def refresh(self, ids):
        current, vanished = [], []
        for ident in ids:
            try:
                current.append(self._read(*self._elements[ident]))
            except Exception:
                vanished.append(ident)
        return current, vanished

    def close_thread(self):
        if self._uia is not None:
            import comtypes
            self._elements.clear()
            self._pending = None
            self._walker = None
            self._uia = None
            if self._old_dpi:
                self._api.SetThreadDpiAwarenessContext(self._old_dpi)
            comtypes.CoUninitialize()

    def wants_snapshot(self):
        return False  # Periodic snapshots are scheduled by Perception.


def open_session(display=None, memory_path=None, use_memory=True, motor=None,
                 config=None, perception_hz=60.0, snapshot_hz=1.0, extra_sources=None):
    from .linux import Session
    from ..body import Body
    from ..runtime import RealClock
    from ..world.model import WorldModel
    from ..world.perception import Perception
    from ..world.memory import SpatialMemory
    if display:
        raise BackendUnavailable('--display is only supported by the X11 backend')
    actuator = WindowsActuator()
    perception = None
    try:
        source = WindowsSource()
        world, clock = WorldModel(), RealClock()
        sources = [source, *(extra_sources or [])]
        perception = Perception(sources, world, clock, perception_hz, snapshot_hz)
        perception.start()
        if perception.stats.errors:
            perception.stop()
            raise BackendUnavailable(f'Windows UI Automation failed: {perception.stats.last_error}')
        path = memory_path or Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'embodied-cursor' / 'memory.json'
        memory = SpatialMemory.load(path) if use_memory else None
        body = Body(actuator, perception, clock, memory=memory, motor=motor, config=config)
        return Session(body, perception, world, actuator, sources, [])
    except Exception:
        if perception is not None:
            perception.stop()
        actuator.close()
        raise
