# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
import threading
import uuid
from pathlib import Path

import pytest

from embodied_cursor.backends.sim import Button, SimDesktop, Window, make_body
from embodied_cursor.brain.heuristic import HeuristicPlanner
from embodied_cursor.daemon import Daemon, request
from embodied_cursor.geometry import Rect

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='requires Windows named pipes')


def test_native_pipe_requests_events_and_shutdown():
    from multiprocessing.connection import Client
    import json
    desk = SimDesktop()
    window = desk.add(Window('Editor', Rect(100, 100, 1000, 700)))
    button = window.add(Button('Export', Rect(400, 300, 100, 40)))
    path = Path(r'\\.\pipe\cec-test-' + uuid.uuid4().hex)
    daemon = Daemon(make_body(desk), lambda _: HeuristicPlanner(), path)
    daemon.start()
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    try:
        assert request({'op': 'status'}, path)['ok']
        with pytest.raises(RuntimeError, match='already listening'):
            Daemon(make_body(desk), lambda _: HeuristicPlanner(), path).start()
        with Client(str(path), family='AF_PIPE') as stream:
            stream.send_bytes(b'{"op":"events"}')
            assert stream.poll(5)
            assert json.loads(stream.recv_bytes())['streaming']
            reply = request({'op': 'intent', 'intent': {'action': 'click', 'target': 'Export'}, 'id': 42}, path)
            assert reply['ok'] and reply['id'] == 42
            assert button.clicks == 1
            kinds = []
            while 'outcome' not in kinds:
                assert stream.poll(5)
                kinds.append(json.loads(stream.recv_bytes())['kind'])
            assert 'press' in kinds and 'release' in kinds
        assert not request({'op': 'unknown'}, path)['ok']
    finally:
        request({'op': 'shutdown'}, path)
        thread.join(5)
        assert not thread.is_alive()
