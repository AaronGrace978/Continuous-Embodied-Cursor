# SPDX-License-Identifier: GPL-3.0-or-later
"""The socket protocol, served from a simulated desktop."""

import json
import os
import socket
import stat
import threading

import pytest

from embodied_cursor.backends.sim import Button, SimDesktop, Slider, Window, make_body
from embodied_cursor.brain.heuristic import HeuristicPlanner
from embodied_cursor.daemon import Daemon, request
from embodied_cursor.geometry import Rect, Vec2


@pytest.fixture
def served(tmp_path):
    desk = SimDesktop(pointer=Vec2(200, 800))
    win = desk.add(Window("Editor", Rect(100, 100, 1400, 800)))
    desk.export = win.add(Button("Export", Rect(1200, 300, 90, 32)))
    desk.volume = win.add(Slider("Volume", Rect(300, 400, 400, 20), value=20))
    body = make_body(desk)
    path = tmp_path / "cec.sock"
    daemon = Daemon(body, lambda kind: HeuristicPlanner(), path)
    daemon.start()
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    yield desk, path
    try:
        request({"op": "shutdown"}, path, timeout=5)
    except OSError:
        pass
    thread.join(timeout=5)


def test_socket_is_private_to_the_user(served):
    _, path = served
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o077 == 0


def test_intent_goal_and_queries(served):
    desk, path = served
    reply = request({"op": "intent", "intent": {"action": "click", "target": 'button "Export"'}, "id": 7}, path)
    assert reply["ok"] and reply["id"] == 7
    assert reply["outcome"]["status"] == "ok"
    assert desk.export.clicks == 1

    reply = request({"op": "goal", "goal": "set Volume to 60; click Export"}, path)
    assert reply["ok"], reply
    assert reply["report"]["status"] == "done"
    assert desk.volume.value == pytest.approx(60, abs=1.0)
    assert desk.export.clicks == 2

    reply = request({"op": "observe"}, path)
    assert 'slider "Volume" value=' in reply["text"]

    reply = request({"op": "find", "selector": "slider"}, path)
    assert [m["name"] for m in reply["matches"]] == ["Volume"]

    status = request({"op": "status"}, path)
    assert status["ok"] and status["busy"] == "" and status["ticks"] > 0


def test_bad_requests_are_answered_not_dropped(served):
    _, path = served
    assert "unknown op" in request({"op": "dance"}, path)["error"]
    assert "does not take" in request({"op": "intent", "intent": {"action": "click", "x": 1, "y": 2}}, path)["error"]
    assert not request({"op": "goal", "goal": "   "}, path)["ok"]
    failed = request({"op": "goal", "goal": "juggle"}, path)
    assert not failed["ok"] and failed["report"]["status"] == "failed"

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(path))
        s.sendall(b"this is not json\n")
        assert "bad request" in json.loads(s.makefile().readline())["error"]


def test_events_are_streamed_to_subscribers(served):
    _, path = served
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(path))
        s.sendall(b'{"op": "events"}\n')
        lines = s.makefile()
        assert json.loads(lines.readline())["streaming"] is True
        request({"op": "intent", "intent": {"action": "click", "target": "Export"}}, path)
        kinds = []
        while "outcome" not in kinds:
            kinds.append(json.loads(lines.readline())["kind"])
    assert kinds[0] == "intent" and "press" in kinds and "release" in kinds


def test_a_second_daemon_refuses_to_take_over_a_live_socket(served):
    desk, path = served
    with pytest.raises(RuntimeError, match="already listening"):
        Daemon(make_body(desk), lambda kind: HeuristicPlanner(), path).start()
