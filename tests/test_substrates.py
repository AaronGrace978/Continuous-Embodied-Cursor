# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real HTTP adapters: cache-only reads, stale state, failures, and redaction."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from embodied_cursor.substrates import SubstrateMonitor, _state


@pytest.fixture
def service():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.calls.append(self.path)
            if self.server.redirect:
                self.send_response(302)
                self.send_header('Location', '/private')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(self.server.payload).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.calls, server.redirect = [], False
    server.payload = {'tick': 3, 'affect': {'valence': .2, 'arousal': .3},
                      'memory_count': 5, 'transcript': [{'content': 'private history'}],
                      'memories': [{'content': 'private memory'}]}
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server, f'http://127.0.0.1:{server.server_port}/state'
    server.shutdown(); server.server_close(); worker.join(1)


def test_optional_reader_is_nonblocking_discards_private_text_and_expires(service):
    server, url = service
    monitor = SubstrateMonitor({'emotion': url}, interval=.02)
    assert not server.calls and not monitor.snapshot()['enabled']
    try:
        monitor.start()
        until = time.monotonic() + 2
        while not monitor.snapshot()['emotion']['connected']:
            assert time.monotonic() < until
            threading.Event().wait(.01)
        snapshot = monitor.snapshot()
        assert snapshot['emotion']['memory_count'] == 5
        assert 'private' not in json.dumps(snapshot)
        started = time.monotonic()
        for _ in range(100):
            monitor.snapshot()
        assert time.monotonic() - started < .1
        monitor.stop()
        assert not monitor.snapshot()['emotion']['connected']
        monitor.enabled = True
        monitor._latest['emotion'] = (time.monotonic() - 10, {'tick': 2})
        assert not monitor.snapshot()['emotion']['connected']
    finally:
        monitor.stop()


def test_redirects_never_fetch_another_endpoint(service):
    server, url = service
    server.redirect = True
    monitor = SubstrateMonitor({'emotion': url}, interval=.02)
    try:
        monitor.start()
        until = time.monotonic() + 2
        while not server.calls:
            assert time.monotonic() < until
            threading.Event().wait(.01)
        monitor.stop()
        assert all(path == '/state' for path in server.calls)
        assert not monitor.snapshot()['emotion']['connected']
    finally:
        monitor.stop()


@pytest.mark.parametrize('url', ['http://example.com/state', 'http://user:secret@localhost/state',
                                'http://127.0.0.1/chat', 'http://127.0.0.1/state?token=x'])
def test_only_explicit_loopback_state_endpoints_are_allowed(url):
    with pytest.raises(ValueError):
        SubstrateMonitor({'motion': url})


def test_motion_state_is_distinct_from_desktop_coordinates_and_nonfinite_values_fail():
    data = {'tick': 5, 'affect': {'valence': -.4, 'arousal': .8},
            'position': [1, 2], 'velocity': [3, 4], 'running': True}
    assert _state('motion', data)['position'] == [1, 2]
    data['position'] = [float('nan'), 2]
    with pytest.raises(ValueError):
        _state('motion', data)
