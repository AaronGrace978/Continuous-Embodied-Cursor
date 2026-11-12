# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desktop IPC and native Ollama protocol, without live input or paid API calls."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from embodied_cursor.desktop import DesktopBridge
from embodied_cursor.brain.ollama import OllamaPlanner, endpoint, list_models
from embodied_cursor.brain.planner import Plan, PlannerError
from embodied_cursor.intents import intent_from_obj


class Messages:
    def __init__(self):
        self.items = []
        self.condition = threading.Condition()

    def emit(self, item):
        with self.condition:
            self.items.append(item)
            self.condition.notify_all()

    def wait(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        with self.condition:
            while True:
                found = next((item for item in self.items if predicate(item)), None)
                if found is not None:
                    return found
                remaining = until - time.monotonic()
                assert remaining > 0, 'Desktop bridge did not respond'
                self.condition.wait(remaining)


@pytest.fixture
def desktop():
    messages = Messages()
    bridge = DesktopBridge(messages.emit, paced=False)
    yield bridge, messages
    bridge.close()


def request(desktop, ident, op, **data):
    bridge, messages = desktop
    bridge.submit({'id': ident, 'op': op, **data})
    return messages.wait(lambda item: item.get('id') == ident)


def test_connect_run_observe_trace_and_disconnect(desktop):
    bridge, messages = desktop
    assert request(desktop, 1, 'connect', mode='simulation')['ok']
    snapshot = messages.wait(lambda item: item.get('event') == 'snapshot' and item['data'].get('connected'))['data']
    assert snapshot['mode'] == 'simulation'
    assert any(el['name'] == 'Export' for el in snapshot['elements'])
    result = request(desktop, 2, 'run', goal='click Export', config={'provider': 'offline'})
    assert result['ok'] and result['result']['status'] == 'done'
    export = next(widget for widget in bridge.session.actuator.widgets.values() if widget.name == 'Export')
    assert export.clicks == 1
    trace = request(desktop, 3, 'trace')
    assert trace['ok'] and trace['result']['report']['status'] == 'done'
    assert any(event['kind'] == 'press' for event in trace['result']['events'])
    assert request(desktop, 4, 'disconnect')['ok']
    assert bridge.session is None


def test_no_task_without_explicit_connection_and_valid_goal(desktop):
    assert not request(desktop, 1, 'run', goal='click Export')['ok']
    assert not request(desktop, 2, 'connect', mode='untrusted')['ok']
    assert request(desktop, 3, 'connect', mode='simulation')['ok']
    assert not request(desktop, 4, 'run', goal='   ')['ok']
    assert not request(desktop, 5, 'run', goal='x' * 4001)['ok']
    assert not request(desktop, 6, 'not-an-operation')['ok']


def test_stop_during_inflight_model_prevents_late_clicks(desktop, monkeypatch):
    bridge, messages = desktop
    entered, release = threading.Event(), threading.Event()
    class BlockedPlanner:
        def plan(self, episode):
            entered.set()
            assert release.wait(3)
            return Plan((intent_from_obj({'action': 'click', 'target': 'Export'}),), True)
    monkeypatch.setattr(bridge, '_planner', lambda config: BlockedPlanner())
    request(desktop, 1, 'connect', mode='simulation')
    bridge.submit({'id': 2, 'op': 'run', 'goal': 'click Export', 'config': {'localFirst': False}})
    assert entered.wait(2)
    started = time.monotonic()
    assert request(desktop, 3, 'stop')['ok']
    assert time.monotonic() - started < 0.5
    assert not request(desktop, 4, 'run', goal='click Export')['ok']
    release.set()
    report = messages.wait(lambda item: item.get('id') == 2)['result']
    assert report['status'] == 'interrupted'
    export = next(widget for widget in bridge.session.actuator.widgets.values() if widget.name == 'Export')
    assert export.clicks == 0
    assert not bridge.session.body.ctx._buttons and not bridge.session.body.ctx._keys


def test_connection_failure_is_recoverable(desktop):
    bridge, _ = desktop
    from embodied_cursor.backends.x11 import BackendUnavailable
    def unavailable():
        raise BackendUnavailable('No interactive desktop')
    bridge.session_factory = unavailable
    reply = request(desktop, 1, 'connect', mode='live')
    assert not reply['ok'] and 'interactive desktop' in reply['error']
    assert request(desktop, 2, 'connect', mode='simulation')['ok']


def test_local_first_works_without_cloud_credentials_and_applies_speed(desktop):
    bridge, _ = desktop
    request(desktop, 1, 'connect', mode='simulation')
    result = request(desktop, 2, 'run', goal='click Export', config={'provider': 'ollama-cloud', 'speed': 'fast'})
    assert result['result']['status'] == 'done'
    assert bridge.session.body.config.type_rate == 720
    assert bridge.session.body.controller.config.tempo == 0.6


def test_camera_has_exclusive_control_and_stop_is_responsive(desktop):
    bridge, messages = desktop
    assert not request(desktop, 1, 'camera')['ok']
    request(desktop, 2, 'connect', mode='simulation')
    original = bridge.session.actuator.position()
    bridge.submit({'id': 3, 'op': 'camera'})
    messages.wait(lambda item: item.get('event') == 'phase' and item['data']['phase'] == 'camera')
    assert request(desktop, 4, 'hand', sample={'x': 0.5, 'y': 0.5, 'pinch': 0.2})['ok']
    assert not request(desktop, 5, 'run', goal='click Export')['ok']
    assert not request(desktop, 6, 'hand', sample={'x': float('nan'), 'y': 0.5, 'pinch': 1})['ok']
    assert request(desktop, 7, 'stop')['ok']
    assert messages.wait(lambda item: item.get('id') == 3)['result']['reason'] == 'cancelled'
    assert bridge.session.actuator.position() == original
    assert request(desktop, 8, 'run', goal='click Export')['result']['status'] == 'done'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), None))
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'models': [{'name': 'test-model'}, {'name': 'test-model'}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.server.status); self.end_headers()
        if self.server.status == 200:
            self.wfile.write(json.dumps({'message': {'content': self.server.reply}}).encode())
        else:
            self.wfile.write(b'{"error":"do not expose raw provider responses"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests, server.status = [], 200
    server.reply = '{"intents":[],"done":true}'
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    server.url = f'http://127.0.0.1:{server.server_port}'
    yield server
    server.shutdown(); server.server_close(); thread.join(2)


def test_ollama_model_catalog_and_native_chat(ollama):
    assert list_models(ollama.url, 'test-key') == ['test-model']
    planner = OllamaPlanner(ollama.url, 'test-model', api_key='test-key', json_mode=False)
    assert planner._complete([{'role': 'user', 'content': 'test'}]) == ollama.reply
    path, headers, body = ollama.requests[-1]
    assert path == '/api/chat' and headers['Authorization'] == 'Bearer test-key'
    assert body['stream'] is False and body['model'] == 'test-model'
    assert 'format' not in body
    assert 'test-key' not in json.dumps(body)
    planner.json_mode = True; planner._complete([])
    assert ollama.requests[-1][2]['format'] == 'json'


@pytest.mark.parametrize('status, match', [(401,'key was rejected'),(403,'Access was denied'),(429,'rate limit')])
def test_provider_failures_are_actionable_without_raw_body(ollama, status, match):
    ollama.status = status
    with pytest.raises(PlannerError, match=match) as error:
        OllamaPlanner(ollama.url, 'test-model')._complete([])
    assert 'do not expose' not in str(error.value)


@pytest.mark.parametrize('url', ['http://example.com:11434', 'https://user:password@localhost',
                                'http://localhost/api', 'file:///etc/passwd', 'http://localhost?token=x'])
def test_local_endpoint_cannot_redirect_a_cloud_key(url):
    assert endpoint('ollama-cloud', url) == 'https://ollama.com'
    with pytest.raises(ValueError):
        endpoint('ollama-local', url)


def test_virtual_keyboard_types_into_focused_app_during_camera_without_pointer_motion(desktop):
    bridge, messages = desktop
    assert not request(desktop, 100, 'keyboard-input', text='x')['ok']
    request(desktop, 101, 'connect', mode='simulation')
    request(desktop, 102, 'run', goal='click Notes')
    origin = bridge.session.actuator.position()
    bridge.submit({'id': 103, 'op': 'camera'})
    messages.wait(lambda item: item.get('event') == 'phase' and item['data']['phase'] == 'camera')
    assert request(desktop, 104, 'keyboard-input', text='Hello')['ok']
    assert request(desktop, 105, 'keyboard-input', key='Return')['ok']
    notes = next(w for w in bridge.session.actuator.widgets.values() if w.name == 'Notes')
    assert notes.text == 'Hello'
    assert bridge.session.actuator.position() == origin
    assert bridge.session.actuator.keys[-2:] == [('Return', True), ('Return', False)]
    request(desktop, 106, 'stop')
    messages.wait(lambda item: item.get('id') == 103)
    assert request(desktop, 107, 'keyboard-input', text='!')['ok']
    assert notes.text == 'Hello!'


@pytest.mark.parametrize('payload', [dict(text='x', key='Return'), dict(text=''), dict(text='x'*9),
    dict(key='Super'), dict(text='\n'), dict(text=True), dict()])
def test_keyboard_rejects_invalid_input(desktop, payload):
    request(desktop, 200, 'connect', mode='simulation')
    assert not request(desktop, 201, 'keyboard-input', **payload)['ok']


def test_stopped_keyboard_queue_cannot_leak_into_next_session(desktop):
    bridge, _ = desktop
    request(desktop, 300, 'connect', mode='simulation')
    message = {'id': 301, 'op': 'keyboard-input', 'text': 'x', '_input_epoch': bridge.input_epoch}
    request(desktop, 302, 'stop')
    with pytest.raises(ValueError, match='cancelled'):
        bridge._keyboard_input(message)
    assert bridge.session.actuator.typed == ''


def test_keyboard_releases_native_key_after_press_failure(desktop, monkeypatch):
    bridge, _ = desktop
    request(desktop, 400, 'connect', mode='simulation')
    events = []
    def failing_key(key, down):
        events.append((key, down))
        if down:
            raise RuntimeError('native input failed')
    monkeypatch.setattr(bridge.session.actuator, 'key', failing_key)
    assert not request(desktop, 401, 'keyboard-input', key='Return')['ok']
    assert events == [('Return', True), ('Return', False)]
