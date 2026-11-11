# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real native controls and real pointer; run on an interactive Windows desktop."""
import ctypes as C
from ctypes import wintypes as W
import sys
import threading
import time
import uuid

import pytest

pytestmark = [pytest.mark.desktop, pytest.mark.skipif(sys.platform != 'win32', reason='requires interactive Windows')]


@pytest.fixture
def controls():
    ready, stop = threading.Event(), threading.Event()
    data = {}
    def run():
        api = C.WinDLL('user32', use_last_error=True)
        api.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
        api.CreateWindowExW.restype = W.HWND
        api.DestroyWindow.argtypes = [W.HWND]
        api.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        api.SendMessageW.restype = C.c_ssize_t
        api.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        api.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        api.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
        api.DispatchMessageW.restype = C.c_ssize_t
        title = 'CEC native test ' + uuid.uuid4().hex
        parent = api.CreateWindowExW(0, 'STATIC', title, 0x10cf0000, 200, 200, 500, 350, None, None, None, None)
        button = api.CreateWindowExW(0, 'BUTTON', 'CEC checkbox', 0x50010003, 30, 50, 180, 40, parent, None, None, None)
        edit = api.CreateWindowExW(0x200, 'EDIT', '', 0x50010000, 30, 120, 250, 40, parent, None, None, None)
        data.update(api=api, parent=parent, button=button, edit=edit, title=title)
        ready.set()
        try:
            msg = W.MSG()
            while not stop.is_set():
                while api.PeekMessageW(C.byref(msg), None, 0, 0, 1):
                    api.TranslateMessage(C.byref(msg))
                    api.DispatchMessageW(C.byref(msg))
                stop.wait(0.005)
        finally:
            api.DestroyWindow(parent)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10), 'native controls did not start'
    assert data['parent'] and data['button'] and data['edit']
    yield data
    stop.set()
    thread.join(5)


def test_native_discovery_motion_click_and_unicode(controls):
    from embodied_cursor.backends.windows import open_session
    from embodied_cursor.intents import intent_from_obj
    with open_session(use_memory=False) as session:
        before = session.actuator.position()
        try:
            target = session.world.find_one('checkbox "CEC checkbox"')
            assert target is not None, 'UIA did not discover the native checkbox'
            result = session.body.execute(intent_from_obj({'action': 'click', 'target': 'checkbox "CEC checkbox"'}))
            assert result.ok, result.describe()
            assert controls['api'].SendMessageW(controls['button'], 0xf0, 0, 0) == 1
            actual = session.actuator.position()
            assert target.bounds.contains(actual)
            # Direct native typing, checked through the target application's API.
            edit = next(el for el in session.world.elements()
                        if el.role == 'text_field' and el.parent == target.parent)
            center = edit.bounds.center
            session.actuator.move_to(center.x, center.y)
            session.actuator.button(1, True)
            session.actuator.button(1, False)
            session.actuator.type_text('Windows Ω 😀')
            deadline = time.monotonic() + 3
            text = C.create_unicode_buffer(200)
            while time.monotonic() < deadline:
                controls['api'].GetWindowTextW(controls['edit'], text, 200)
                if text.value == 'Windows Ω 😀':
                    break
                time.sleep(0.02)
            assert text.value == 'Windows Ω 😀'
        finally:
            session.actuator.move_to(before.x, before.y)
