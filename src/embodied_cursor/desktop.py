# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private JSON-lines stdio bridge for Electron. No listening port or shell.

Session lifetime and all motor work belong to one engine thread. The stdin
thread can always cancel it, including while an LLM request is in flight.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .brain.agent import Agent
from .brain.heuristic import HeuristicPlanner
from .brain.observation import observe
from .brain.ollama import OllamaPlanner, endpoint, list_models
from .brain.planner import PlannerError


@dataclass
class SimulationSession:
    body: Any
    notes: list[str]

    @property
    def world(self):
        return self.body.world

    @property
    def perception(self):
        return self.body.perception

    @property
    def actuator(self):
        return self.body.actuator

    def close(self):
        self.body.close()


def simulation_session(paced: bool = True) -> SimulationSession:
    from .backends.sim import Button, Label, SimDesktop, Slider, TextField, Window, make_body
    from .geometry import Rect, Vec2
    from .runtime import SimClock

    class PacedClock(SimClock):
        def sleep_until(self, t):
            if paced:
                time.sleep(max(0, t - self.now()))
            super().sleep_until(t)

    desk = SimDesktop(clock=PacedClock(), monitors=[Rect(0, 0, 1000, 640)], pointer=Vec2(220, 340))
    win = desk.add(Window("Cursor playground", Rect(40, 40, 920, 560)))
    win.add(Label("A little space to try big ideas", Rect(80, 95, 560, 36)))
    win.add(TextField("Notes", Rect(80, 170, 520, 100)))
    win.add(Slider("Volume", Rect(80, 330, 480, 24), value=35))
    win.add(Button("Word wrap", Rect(80, 405, 160, 40), checkable=True))
    win.add(Button("Export", Rect(680, 465, 190, 52)))
    return SimulationSession(make_body(desk), ["Simulation: the system pointer is never moved."])


class DesktopBridge:
    def __init__(self, emit: Callable[[dict], None], session_factory=None, paced=True):
        self.emit = emit
        self.session_factory = session_factory
        self.paced = paced
        self.session = None
        self.mode = "live"
        self.phase = "disconnected"
        self.goal = ""
        self.last_report = None
        self._pointer = [0, 0]
        self.cancel = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.running = False
        self.jobs: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="cec-desktop-engine", daemon=True)
        self.worker.start()
        self.telemetry = threading.Thread(target=self._telemetry, name="cec-desktop-status", daemon=True)
        self.telemetry.start()

    def _reply(self, ident, result=None, error=None):
        self.emit({"id": ident, "ok": error is None, **({"error": error} if error else {"result": result})})

    def _state(self, phase):
        if self.cancel.is_set() and self.running and phase in ("planning", "moving"):
            phase = "stopping"
        with self.lock:
            self.phase = phase
        self.emit({"event": "phase", "data": {"phase": phase, "mode": self.mode, "goal": self.goal}})

    def submit(self, message):
        ident = message.get("id")
        op = message.get("op")
        if op == "stop":
            self.cancel.set()
            if self.running:
                self._state("stopping")
            self._reply(ident, {"stopping": self.running})
            return
        if op not in ("connect", "disconnect", "run", "models", "test-provider", "trace"):
            self._reply(ident, error="Unsupported desktop operation")
            return
        with self.lock:
            if self.running and op not in ("disconnect",):
                self._reply(ident, error="Stop the current task before starting another operation")
                return
            if op == "run":
                if not self.session:
                    self._reply(ident, error="Connect a desktop or simulation first")
                    return
                if not isinstance(message.get("goal"), str) or not 0 < len(message["goal"].strip()) <= 4000:
                    self._reply(ident, error="Enter a goal of up to 4,000 characters")
                    return
                self.cancel.clear()
                self.running = True
            elif op == "disconnect":
                self.cancel.set()
        self.jobs.put(message)

    def _planner(self, config):
        provider = config.get("provider", "offline")
        if provider == "offline":
            return HeuristicPlanner()
        base = endpoint(provider, config.get("localUrl", "http://127.0.0.1:11434"))
        model = config.get("model", "").strip()
        key = config.get("apiKey", "") if provider == "ollama-cloud" else ""
        if provider == "ollama-cloud" and not key:
            raise PlannerError("Add your Ollama Cloud API key in settings")
        if not model:
            raise PlannerError("Choose an Ollama model in settings")
        return OllamaPlanner(base, model, key, timeout=45, json_mode=provider == "ollama-local")

    def _work(self):
        try:
            while not self.closed.is_set():
                try:
                    message = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    if self.session:
                        try:
                            if not self.session.perception.threaded:
                                self.session.perception.pump()
                            self._pointer = list(self.session.actuator.position().rounded())
                        except Exception:
                            self._state("disconnected")
                            session, self.session = self.session, None
                            session.close()
                    continue
                if message is None:
                    break
                ident, op = message.get("id"), message["op"]
                try:
                    result = self._dispatch(op, message)
                    self._reply(ident, result)
                except Exception as exc:
                    # Do not echo arbitrary HTTP bodies, credentials or request payloads.
                    from .backends.x11 import BackendUnavailable
                    error = str(exc) if isinstance(exc, (ValueError, PlannerError, BackendUnavailable)) else f"Desktop operation failed ({type(exc).__name__})"
                    key = message.get("config", {}).get("apiKey", "")
                    if key:
                        error = error.replace(key, "[redacted]")
                    self._reply(ident, error=error)
                    if op in ("connect", "run"):
                        self._state("ready" if self.session else "disconnected")
                finally:
                    if op == "run":
                        with self.lock:
                            self.running = False
                        self.snapshot()
        finally:
            if self.session:
                self.session.close()
                self.session = None

    def _dispatch(self, op, message):
        if op == "connect":
            mode = message.get("mode", "live")
            if mode not in ("live", "simulation"):
                raise ValueError("Choose live desktop or simulation")
            if self.session:
                self.session.close()
                self.session = None
            self.mode = mode
            self._state("connecting")
            if mode == "simulation":
                self.session = simulation_session(self.paced)
            else:
                if self.session_factory:
                    self.session = self.session_factory()
                else:
                    from .backends.desktop import open_session
                    self.session = open_session()
            self.session.body.listeners.append(self._body_event)
            self._pointer = list(self.session.actuator.position().rounded())
            self._state("ready")
            self.snapshot()
            return {"notes": self.session.notes, "mode": mode}
        if op == "disconnect":
            if self.session:
                self.session.close()
                self.session = None
            self.goal = ""
            self._state("disconnected")
            self.snapshot()
            return {"disconnected": True}
        if op in ("models", "test-provider"):
            config = message.get("config", {})
            base = endpoint(config.get("provider", "ollama-cloud"), config.get("localUrl", "http://127.0.0.1:11434"))
            key = config.get("apiKey", "") if config.get("provider") == "ollama-cloud" else ""
            if op == "models":
                return {"models": list_models(base, key)}
            planner = self._planner(config)
            started = time.monotonic()
            planner._complete([{"role": "user", "content": 'Reply with only the JSON object {"ok":true}.'}])
            return {"latencyMs": round((time.monotonic() - started) * 1000), "model": planner.model}
        if op == "trace":
            if not self.session:
                raise ValueError("Connect a desktop before exporting a trace")
            from .render import scene_of
            return {**self.session.body.trace.to_obj(), "scene": scene_of(self.session.world), "report": self.last_report}
        if op == "run":
            self.goal = message["goal"].strip()
            planner = self._planner(message.get("config", {}))
            self._state("planning")
            if hasattr(self.session, "settle"):
                self.session.settle(2.0)
            agent = Agent(self.session.body, planner, on_event=self._agent_event)
            report = agent.run(self.goal, cancel=self.cancel)
            if self.cancel.is_set():
                report.status, report.reason = "interrupted", "cancelled"
            self.last_report = report.to_obj()
            self.emit({"event": "report", "data": self.last_report})
            self._pointer = list(self.session.actuator.position().rounded())
            self._state("ready")
            return self.last_report
        raise ValueError("Unsupported operation")

    def _agent_event(self, kind, data):
        if kind == "plan":
            self._state("moving" if data.get("intents") else "planning")
        elif kind in ("surprise", "outcome"):
            self._state("planning")
        self.emit({"event": "activity", "data": {"kind": kind, **data}})

    def _body_event(self, event):
        if event.get("kind") == "intent":
            self._state("moving")
        self.emit({"event": "activity", "data": event})

    def snapshot(self):
        with self.lock:
            session, phase, running = self.session, self.phase, self.running
        data = {"phase": phase, "mode": self.mode, "running": running, "goal": self.goal,
                "connected": session is not None}
        if session:
            try:
                pointer = session.body.controller.pos.rounded() if running else self._pointer
                from .geometry import Vec2
                observation = observe(session.world, Vec2(*pointer), session.body.clock.now()).to_obj()
                data.update(pointer=list(pointer), elements=[el.to_obj() for el in session.world.elements() if el.visible],
                            monitors=[list(rect.as_tuple()) for rect in session.world.monitors],
                            observation=observation, ticks=session.body.ticks,
                            perceptionMs=round(session.perception.stats.last_frame_s * 1000, 2),
                            perceptionErrors=session.perception.stats.errors)
            except Exception:
                return
        self.emit({"event": "snapshot", "data": data})

    def _telemetry(self):
        while not self.closed.wait(0.2):
            self.snapshot()

    def close(self):
        self.cancel.set()
        self.closed.set()
        self.jobs.put(None)
        self.worker.join(timeout=3)
        self.telemetry.join(timeout=1)


def main():
    output_lock = threading.Lock()
    def emit(message):
        with output_lock:
            print(json.dumps(message, ensure_ascii=True), flush=True)
    bridge = DesktopBridge(emit)
    emit({"event": "ready", "data": {"version": 1, "platform": sys.platform}})
    try:
        for line in sys.stdin:
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("Request too large")
                message = json.loads(line)
                if not isinstance(message, dict) or not isinstance(message.get("id"), int):
                    raise ValueError("A request needs an integer id")
                bridge.submit(message)
            except (ValueError, TypeError):
                emit({"id": None, "ok": False, "error": "Invalid desktop request"})
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
