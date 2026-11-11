# SPDX-License-Identifier: AGPL-3.0-or-later
"""Win32 ABI and event semantics, runnable without a Windows desktop."""
import ctypes as C
import sys
from unittest.mock import Mock

import pytest

from embodied_cursor.backends import windows as win
from embodied_cursor.geometry import Vec2


@pytest.fixture
def actuator(monkeypatch):
    api = Mock()
    api.SetThreadDpiAwarenessContext.return_value = 123
    api.SendInput.return_value = 1
    api.SetCursorPos.return_value = 1
    monkeypatch.setattr(win, '_user32', lambda: api)
    obj = win.WindowsActuator()
    obj.events = []
    def send(count, pointer, size):
        obj.events.append(win.INPUT.from_buffer_copy(C.string_at(pointer, size)))
        return 1
    api.SendInput.side_effect = send
    return obj


def test_input_abi():
    assert C.sizeof(win.INPUT) == (40 if C.sizeof(C.c_void_p) == 8 else 28)
    assert C.sizeof(win.POINT) == 8
    assert C.sizeof(win.RECT) == 16


def test_real_pointer_readback_and_negative_monitor_coordinates(actuator):
    def position(pointer):
        pointer._obj.x, pointer._obj.y = -1200, 300
        return 1
    actuator.api.GetCursorPos.side_effect = position
    assert actuator.position() == Vec2(-1200, 300)
    actuator.move_to(-900, 450)
    actuator.api.SetCursorPos.assert_called_once_with(-900, 450)


@pytest.mark.parametrize('button,flags', [(1, [2, 4]), (2, [32, 64]), (3, [8, 16])])
def test_buttons(actuator, button, flags):
    actuator.button(button, True)
    actuator.button(button, False)
    assert [e.mi.dwFlags for e in actuator.events] == flags


def test_wheel_directions(actuator):
    actuator.scroll(2, 3)
    assert [(e.mi.dwFlags, C.c_int32(e.mi.mouseData).value) for e in actuator.events] == [(0x800, -360), (0x1000, 240)]


def test_unicode_including_surrogate_pairs(actuator):
    actuator.type_text('A😀')
    assert [(e.ki.wScan, e.ki.dwFlags) for e in actuator.events] == [
        (65, 4), (65, 6), (0xd83d, 4), (0xd83d, 6), (0xde00, 4), (0xde00, 6)]


def test_chords_extended_keys_and_unknown_keys(actuator):
    actuator.key('ctrl', True)
    actuator.key('Delete', True)
    actuator.key('Delete', False)
    actuator.key('ctrl', False)
    assert [(e.ki.wVk, e.ki.dwFlags) for e in actuator.events] == [(17, 0), (46, 1), (46, 3), (17, 2)]
    with pytest.raises(ValueError):
        actuator.key('not-a-key', True)


def test_input_failure_is_reported(actuator):
    actuator.api.SendInput.side_effect = None
    actuator.api.SendInput.return_value = 0
    with pytest.raises(win.BackendUnavailable, match='UIPI'):
        actuator.button(1, True)
    actuator.api.SetCursorPos.return_value = 0
    with pytest.raises(win.BackendUnavailable, match='SetCursorPos'):
        actuator.move_to(1, 2)


def test_dpi_context_restored(actuator):
    actuator.close()
    actuator.api.SetThreadDpiAwarenessContext.assert_called_with(123)


def test_native_dispatch(monkeypatch):
    from embodied_cursor.backends import desktop, linux
    for platform, module in [('win32', win), ('linux', linux)]:
        native = Mock(return_value='session')
        monkeypatch.setattr(sys, 'platform', platform)
        monkeypatch.setattr(module, 'open_session', native)
        assert desktop.open_session(use_memory=False) == 'session'
        native.assert_called_once_with(use_memory=False)


def test_windows_pipe_path_does_not_need_getuid(monkeypatch):
    from embodied_cursor import daemon
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert str(daemon.default_socket_path()).startswith('\\\\.\\pipe\\embodied-cursor-')


def test_uia_refresh_removes_stale_elements():
    source = object.__new__(win.WindowsSource)
    source._elements = {'uia:1': ('element', None, 1)}
    source._read = Mock(return_value='updated')
    assert source.refresh(['uia:1', 'uia:2']) == (['updated'], ['uia:2'])
    source._read.assert_called_once_with('element', None, 1)
