# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows named-pipe transport; JSON bytes only, never pickle payloads.

The pipe uses the Windows default DACL (owner, administrators and SYSTEM).
"""
import json
import queue
import threading
from multiprocessing.connection import Client, Listener


def _send(connection, obj):
    connection.send_bytes(json.dumps(obj).encode('utf-8'))


def request(obj, path, timeout=300.0):
    with Client(str(path), family='AF_PIPE') as connection:
        _send(connection, obj)
        if not connection.poll(timeout):
            raise TimeoutError('Windows daemon did not answer before the deadline')
        return json.loads(connection.recv_bytes().decode('utf-8'))


class PipeServer:
    def __init__(self, daemon):
        self.daemon = daemon
        self.listener = Listener(str(daemon.path), family='AF_PIPE')
        self.stopped = threading.Event()

    def serve_forever(self, poll_interval=0.2):
        while not self.stopped.is_set():
            try:
                connection = self.listener.accept()
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            if self.stopped.is_set():
                connection.close()
                break
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        try:
            with connection:
                while not self.stopped.is_set():
                    try:
                        obj = json.loads(connection.recv_bytes().decode('utf-8'))
                        if not isinstance(obj, dict):
                            raise ValueError('a request must be a JSON object')
                    except (ValueError, UnicodeError) as exc:
                        _send(connection, {'ok': False, 'error': f'bad request: {exc}'})
                        continue
                    if obj.get('op') == 'events':
                        self._stream(connection)
                        return
                    reply = self.daemon.handle(obj)
                    if 'id' in obj:
                        reply['id'] = obj['id']
                    _send(connection, reply)
                    if obj.get('op') == 'shutdown':
                        return
        except (OSError, EOFError):
            pass

    def _stream(self, connection):
        events = queue.Queue(maxsize=2000)
        with self.daemon._subscribers_lock:
            self.daemon._subscribers.append(events)
        try:
            _send(connection, {'ok': True, 'streaming': True})
            while not self.stopped.is_set():
                try:
                    _send(connection, events.get(timeout=0.5))
                except queue.Empty:
                    continue
        finally:
            with self.daemon._subscribers_lock:
                self.daemon._subscribers.remove(events)

    def shutdown(self):
        if not self.stopped.is_set():
            self.stopped.set()
            # Wake the blocking accept. No request is processed on this connection.
            try:
                with Client(str(self.daemon.path), family='AF_PIPE'):
                    pass
            except OSError:
                pass

    def server_close(self):
        self.shutdown()
        self.listener.close()
