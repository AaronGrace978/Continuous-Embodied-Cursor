# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long-lived local service that owns the cursor.

Perception keeps running between requests, so the world model is already
warm when an intent arrives and nothing has to be rediscovered. Clients talk
JSON lines over a Unix socket that only the owning user can open:

    {"op": "intent", "intent": {"action": "click", "target": "button \\"Export\\""}}
    {"op": "goal", "goal": "open File > Export as > PDF document", "planner": "heuristic"}
    {"op": "observe"}                       what a planner would be shown
    {"op": "find", "selector": "slider"}    matching elements
    {"op": "cancel"}                        stop whatever is running
    {"op": "events"}                        stream body events until disconnect
    {"op": "status"} / {"op": "shutdown"}

Every request gets one JSON line back with ``"ok": true|false``. Only one
request moves the cursor at a time; others wait their turn.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

from .body import Body
from .brain.agent import Agent
from .brain.observation import observe
from .brain.planner import Planner, PlannerError
from .intents import intent_from_obj
from .world.selector import Selector

PlannerFactory = Callable[[str], Planner]


def default_socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/embodied-cursor-{os.getuid()}"
    return Path(base) / "embodied-cursor.sock"


class Daemon:
    def __init__(self, body: Body, planners: PlannerFactory, socket_path: Path | str | None = None) -> None:
        self.body = body
        self.planners = planners
        self.path = Path(socket_path) if socket_path else default_socket_path()
        self._motor = threading.Lock()
        self._cancel = threading.Event()
        self._busy = ""
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._subscribers_lock = threading.Lock()
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        body.listeners.append(self._publish)

    # -- serving ---------------------------------------------------------

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            if _alive(self.path):
                raise RuntimeError(f"another daemon is already listening on {self.path}")
            self.path.unlink()
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("a request must be a JSON object")
                    except ValueError as exc:
                        self._send({"ok": False, "error": f"bad request: {exc}"})
                        continue
                    if request.get("op") == "events":
                        self._stream()
                        return
                    reply = daemon.handle(request)
                    if "id" in request:
                        reply["id"] = request["id"]
                    self._send(reply)
                    if request.get("op") == "shutdown":
                        return

            def _send(self, obj: dict[str, Any]) -> bool:
                try:
                    self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

            def _stream(self) -> None:
                q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
                with daemon._subscribers_lock:
                    daemon._subscribers.append(q)
                try:
                    self._send({"ok": True, "streaming": True})
                    while daemon._server is not None:
                        try:
                            event = q.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        if not self._send(event):
                            break
                finally:
                    with daemon._subscribers_lock:
                        daemon._subscribers.remove(q)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        old = os.umask(0o177)
        try:
            self._server = Server(str(self.path), Handler)
        finally:
            os.umask(old)

    def serve_forever(self) -> None:
        if self._server is None:
            self.start()
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.server_close()
            try:
                self.path.unlink()
            except OSError:
                pass

    def _publish(self, event: dict[str, Any]) -> None:
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    # -- requests --------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        try:
            if op == "intent":
                intent = intent_from_obj(request.get("intent"))
                with self._moving(intent.describe()):
                    outcome = self.body.execute(intent, cancel=self._cancel.is_set)
                return {"ok": outcome.ok, "outcome": outcome.to_obj()}
            if op == "goal":
                goal = request.get("goal")
                if not isinstance(goal, str) or not goal.strip():
                    raise ValueError("'goal' must be a non-empty string")
                planner = self.planners(str(request.get("planner", "heuristic")))
                with self._moving(goal):
                    report = Agent(self.body, planner).run(goal, cancel=self._cancel)
                return {"ok": report.ok, "report": report.to_obj()}
            if op == "observe":
                obs = observe(self.body.world, self.body.actuator.position(), self.body.clock.now(), int(request.get("limit", 120)))
                return {"ok": True, "observation": obs.to_obj(), "text": obs.text()}
            if op == "find":
                selector = Selector.of(request.get("selector"))
                matches = self.body.world.find(selector, visible_only=not request.get("hidden", False))
                return {"ok": True, "matches": [{**m.element.to_obj(), "score": m.score} for m in matches[:50]]}
            if op == "cancel":
                was = self._busy
                if was:
                    self._cancel.set()
                return {"ok": True, "cancelled": was}
            if op == "status":
                stats = self.body.perception.stats
                return {
                    "ok": True,
                    "busy": self._busy,
                    "pointer": list(self.body.actuator.position().rounded()),
                    "elements": len(self.body.world),
                    "ticks": self.body.ticks,
                    "overruns": self.body.overruns,
                    "perception": {
                        "frames": stats.frames,
                        "snapshots": stats.snapshots,
                        "last_frame_ms": round(stats.last_frame_s * 1000, 2),
                        "worst_frame_ms": round(stats.worst_frame_s * 1000, 2),
                        "errors": stats.errors,
                    },
                }
            if op == "shutdown":
                self._cancel.set()
                threading.Thread(target=self._shutdown, daemon=True).start()
                return {"ok": True}
            raise ValueError(f"unknown op {op!r}")
        except (ValueError, TypeError, KeyError, PlannerError) as exc:
            return {"ok": False, "error": str(exc)}

    def _shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _moving(self, what: str) -> "_Turn":
        return _Turn(self, what)


class _Turn:
    """Exclusive use of the cursor for one request."""

    def __init__(self, daemon: Daemon, what: str) -> None:
        self.daemon, self.what = daemon, what

    def __enter__(self) -> None:
        self.daemon._motor.acquire()
        self.daemon._cancel.clear()
        self.daemon._busy = self.what

    def __exit__(self, *exc: object) -> None:
        self.daemon._busy = ""
        self.daemon._motor.release()


def _alive(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
        return True
    except OSError:
        return False


def request(obj: dict[str, Any], socket_path: Path | str | None = None, timeout: float | None = 300.0) -> dict[str, Any]:
    """Send one request to a running daemon and return its reply."""
    path = Path(socket_path) if socket_path else default_socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf.strip():
        raise ConnectionError("the daemon closed the connection without answering")
    return json.loads(buf.decode("utf-8"))
