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
from dataclasses import dataclass, replace
from typing import Any, Callable

from .brain.agent import Agent
from .brain.automatic import AutomaticPlanner
from .brain.heuristic import HeuristicPlanner
from .brain.observation import observe
from .brain.ollama import OllamaPlanner, endpoint, list_models
from .brain.planner import PlannerError
from .session import SessionMemory
from .substrates import SubstrateMonitor


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
        self.memory = SessionMemory()
        self.substrates = SubstrateMonitor()
        self.connected_at = None
        self.detail = "Connect a desktop to begin a session"
        self.current_action = ""
        self.controller_windows: set[int] = set()
        self._pointer = [0, 0]
        self.cancel = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.running = False
        self.hand_sample = None
        self.hand_received = 0.0
        self.input_epoch = 0
        self.inputs: queue.Queue = queue.Queue(maxsize=24)
        self.jobs: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="cec-desktop-engine", daemon=True)
        self.worker.start()
        self.telemetry = threading.Thread(target=self._telemetry, name="cec-desktop-status", daemon=True)
        self.telemetry.start()

    def _reply(self, ident, result=None, error=None):
        self.emit({"id": ident, "ok": error is None, **({"error": error} if error else {"result": result})})

    def _state(self, phase):
        if self.cancel.is_set() and self.running and phase in ("planning", "moving", "camera"):
            phase = "stopping"
        with self.lock:
            self.phase = phase
        self.emit({"event": "phase", "data": {"phase": phase, "mode": self.mode, "goal": self.goal}})

    def submit(self, message):
        ident = message.get("id")
        op = message.get("op")
        if op == "stop":
            with self.lock:
                self.input_epoch += 1
                self.cancel.set()
            if self.running:
                self._state("stopping")
            self._reply(ident, {"stopping": self.running})
            return
        if op == "keyboard-input":
            try:
                self._validate_input(message)
                with self.lock:
                    if not self.session or (self.running and self.cancel.is_set()):
                        raise ValueError("Connect an idle desktop or enable hand control first")
                    message = {**message, "_input_epoch": self.input_epoch}
                    if self.running:
                        if self.phase != "camera":
                            raise ValueError("Stop the current task before typing")
                        self.inputs.put_nowait(message)
                        return
            except (ValueError, queue.Full) as exc:
                self._reply(ident, error=str(exc) or "Keyboard input queue is full")
                return
        if op == "hand":
            try:
                from .hand import HandSample
                sample = HandSample.parse(message.get("sample"))
                with self.lock:
                    if not self.running or self.phase != "camera":
                        raise ValueError("Hand control is not active")
                    self.hand_sample = sample
                    self.hand_received = time.monotonic()
                self._reply(ident, {"received": True})
            except ValueError as exc:
                self._reply(ident, error=str(exc))
            return
        if op not in ("connect", "disconnect", "run", "camera", "models", "test-provider", "trace", "keyboard-input", "clear-context", "configure-substrates"):
            self._reply(ident, error="Unsupported desktop operation")
            return
        with self.lock:
            if self.running and op not in ("disconnect",):
                self._reply(ident, error="Stop the current task before starting another operation")
                return
            if op in ("run", "camera"):
                if not self.session:
                    self._reply(ident, error="Connect a desktop or simulation first")
                    return
                if op == "run" and (not isinstance(message.get("goal"), str) or not 0 < len(message["goal"].strip()) <= 4000):
                    self._reply(ident, error="Enter a goal of up to 4,000 characters")
                    return
                self.cancel.clear()
                self.running = True
            elif op == "disconnect":
                self.input_epoch += 1
                self.cancel.set()
        self.jobs.put(message)

    @staticmethod
    def _validate_input(message):
        text, key = message.get("text"), message.get("key")
        if (text is None) == (key is None):
            raise ValueError("Choose one keyboard input")
        if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= 8 or any(ord(c) < 32 for c in text)):
            raise ValueError("Invalid keyboard text")
        if key is not None and key not in ("BackSpace", "Tab", "Return", "Escape", "Left", "Right", "Up", "Down", "Delete"):
            raise ValueError("Unsupported keyboard key")

    def _keyboard_input(self, message):
        self._validate_input(message)
        if message.get("_input_epoch") != self.input_epoch or self.closed.is_set() or (self.running and self.cancel.is_set()):
            raise ValueError("Keyboard input was cancelled")
        actuator = self.session.actuator
        if "text" in message:
            actuator.type_text(message["text"])
        else:
            key = message["key"]
            try:
                actuator.key(key, True)
            finally:
                actuator.key(key, False)
        return {"typed": True}

    def _drain_inputs(self, cancelled=False):
        # Called only by the owner engine thread, never by stdin/telemetry.
        for _ in range(24):
            try:
                message = self.inputs.get_nowait()
            except queue.Empty:
                break
            try:
                if cancelled or self.cancel.is_set():
                    raise ValueError("Keyboard input was cancelled")
                self._reply(message["id"], self._keyboard_input(message))
            except Exception:
                self._reply(message["id"], error="Keyboard input was cancelled" if cancelled or self.cancel.is_set() else "Keyboard input failed")

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
                result, error = None, None
                try:
                    result = self._dispatch(op, message)
                except Exception as exc:
                    # Do not echo arbitrary HTTP bodies, credentials or request payloads.
                    from .backends.x11 import BackendUnavailable
                    error = str(exc) if isinstance(exc, (ValueError, PlannerError, BackendUnavailable)) else f"Desktop operation failed ({type(exc).__name__})"
                    key = message.get("config", {}).get("apiKey", "")
                    if key:
                        error = error.replace(key, "[redacted]")
                    self.detail = error
                    if op in ("connect", "run", "camera"):
                        self._state("ready" if self.session else "disconnected")
                finally:
                    if op in ("run", "camera"):
                        with self.lock:
                            self.running = False
                        self.snapshot()
                # Reopen admission before acknowledging completion. An immediate follow-up
                # must not race the previous task's running flag.
                self._reply(ident, result, error)
        finally:
            if self.session:
                self.session.close()
                self.session = None

    def _dispatch(self, op, message):
        if op == "configure-substrates":
            if not isinstance(message.get("enabled"), bool):
                raise ValueError("Choose whether to connect the substrates")
            if message["enabled"]:
                self.substrates.start()
            else:
                self.substrates.stop()
            self.snapshot()
            return self.substrates.snapshot()
        if op == "clear-context":
            self.memory.clear()
            self.last_report = None
            self.goal = ""
            self.detail = "Context cleared. Ready for a new instruction"
            self.snapshot()
            return {"cleared": True}
        if op == "keyboard-input":
            return self._keyboard_input(message)
        if op == "connect":
            mode = message.get("mode", "live")
            if mode not in ("live", "simulation"):
                raise ValueError("Choose live desktop or simulation")
            if self.session:
                self.session.close()
                self.session = None
            self.cancel.clear()
            self.memory.clear()
            self.last_report = None
            self.current_action = ""
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
            self.connected_at = time.monotonic()
            self.detail = "Session active. Ready for your first instruction"
            self._state("ready")
            self.snapshot()
            return {"notes": self.session.notes, "mode": mode}
        if op == "disconnect":
            if self.session:
                self.session.close()
                self.session = None
            self.goal = ""
            self.memory.clear()
            self.substrates.stop()
            self.connected_at = None
            self.detail = "Disconnected"
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
            stats = self.session.perception.stats
            diagnostics = {"platform": sys.platform, "mode": self.mode,
                           "motorTicks": self.session.body.ticks,
                           "motorOverruns": self.session.body.overruns,
                           "perceptionErrors": stats.errors,
                           "lastPerceptionMs": round(stats.last_frame_s * 1000, 2),
                           "worstPerceptionMs": round(stats.worst_frame_s * 1000, 2)}
            return {**self.session.body.trace.to_obj(), "scene": scene_of(self.session.world),
                    "report": self.last_report, "diagnostics": diagnostics}
        if op == "camera":
            return self._camera()
        if op == "run":
            self.goal = message["goal"].strip()
            config = message.get("config", {})
            planner = AutomaticPlanner(lambda: self._planner(config)) if config.get("localFirst", True) else self._planner(config)
            speed = config.get("speed", "responsive")
            if speed not in ("careful", "responsive", "fast"):
                raise ValueError("Choose a supported movement speed")
            body = self.session.body
            handles = message.get("controllerWindows", [])
            if not isinstance(handles, list) or len(handles) > 8:
                raise ValueError("Invalid controller windows")
            self.controller_windows = {int(handle) for handle in handles}
            body.before_keyboard = self._guard_keyboard
            tempo, type_rate = {"careful": (1.15, 60), "responsive": (0.8, 240), "fast": (0.6, 720)}[speed]
            body.config = replace(body.config, type_rate=type_rate)
            body.ctx.config = body.config
            body.controller.config = replace(body.controller.config, tempo=tempo)
            self._state("planning")
            self.detail = "Refreshing the desktop before planning"
            if hasattr(self.session, "settle"):
                self.session.settle(2.0)
            agent = Agent(self.session.body, planner, max_rounds=24, max_intents=160, on_event=self._agent_event)
            report = agent.run(self.goal, cancel=self.cancel,
                               context=lambda: self.memory.context(self.substrates.snapshot()))
            if self.cancel.is_set():
                report.status, report.reason = "interrupted", "cancelled"
            self.last_report = report.to_obj()
            self.memory.remember(report)
            self.current_action = ""
            self.detail = ("Task finished. Session stays active — what next?" if report.ok
                           else f"Paused: {report.reason or report.status}. Session stays connected")
            self.emit({"event": "report", "data": self.last_report})
            self._pointer = list(self.session.actuator.position().rounded())
            self._state("ready")
            return self.last_report
        raise ValueError("Unsupported operation")

    def _guard_keyboard(self):
        if self.mode != "live" or not self.controller_windows:
            return
        foreground = getattr(self.session.actuator, "foreground_window", None)
        if foreground is not None and foreground() in self.controller_windows:
            from .skills import SkillFailure
            raise SkillFailure("controller_focused", "The copilot has keyboard focus. Name the target field, e.g. fill Notes with hello, or focus the target app first")

    def _camera(self):
        from .hand import HandController
        actuator = self.session.actuator
        monitors = self.session.world.monitors
        actual = actuator.position()
        bounds = next((rect for rect in monitors if rect.contains(actual)), monitors[0] if monitors else None)
        if bounds is None:
            raise ValueError("No monitor available for hand control")
        controller = HandController(bounds)
        self.goal = "Hand control · point, pinch, two fingers to scroll, three to summon keyboard"
        with self.lock:
            self.hand_sample = None
            self.hand_received = 0
        self._state("camera")
        commanded = actual.rounded()
        last_status = ""
        reason = "cancelled"
        try:
            while not self.cancel.wait(1 / 60) and not self.closed.is_set():
                if self.mode == "simulation":
                    self.session.body.clock.advance(1 / 60)
                if not self.session.perception.threaded:
                    self.session.perception.pump()
                actual = actuator.position()
                if actual.dist(type(actual)(*commanded)) > self.session.body.config.override_px:
                    reason = "human_override"
                    break
                with self.lock:
                    received = self.hand_received
                    sample = self.hand_sample if time.monotonic() - received < 0.25 else None
                # Debounce advances only with new camera evidence. Reusing a single
                # pinch frame at motor rate must not turn a stalled stream into a click.
                action = controller.update(sample, received if sample is not None else time.monotonic())
                position, click, status = action[:3]
                self._drain_inputs()
                if not self.cancel.is_set():
                    if any(action.scroll):
                        actuator.scroll(*action.scroll)
                    if action.keyboard:
                        self.emit({"event": "keyboard-toggle", "data": {}})
                if position is not None:
                    commanded = position.rounded()
                    actuator.move_to(*commanded)
                if click and not self.cancel.is_set():
                    try:
                        actuator.button(1, True)
                    finally:
                        actuator.button(1, False)
                self._pointer = list(actuator.position().rounded())
                if status != last_status:
                    self.emit({"event": "hand-status", "data": {"status": status}})
                    last_status = status
        except Exception:
            reason = "input_error"
            raise
        finally:
            # Close admission before draining, including on actuator failures.
            self._state("ready")
            self._drain_inputs(cancelled=True)
            self.session.body.controller.reset(actuator.position())
            self.goal = ""
            self.emit({"event": "hand-stopped", "data": {"reason": reason}})
        return {"reason": reason}

    def _agent_event(self, kind, data):
        if kind == "thinking":
            self.detail = f"Planning step {data.get('round', 1)}"
            self._state("planning")
        elif kind == "plan":
            self._state("moving" if data.get("intents") else "planning")
        elif kind == "surprise":
            self._state("planning")
        self.emit({"event": "activity", "data": {"kind": kind, **data}})

    def _body_event(self, event):
        if event.get("kind") == "intent":
            self.current_action = event.get("intent", {}).get("action", "")
            self.detail = f"Executing {self.current_action}"
            self._state("moving")
        elif event.get("kind") == "waiting":
            self.detail = event.get("detail") or event.get("reason", "Waiting for a target")
        elif event.get("kind") in ("locked", "acquired"):
            self.detail = f"Executing {self.current_action}"
        self.emit({"event": "activity", "data": event})

    def snapshot(self):
        with self.lock:
            session, phase, running = self.session, self.phase, self.running
        data = {"phase": phase, "mode": self.mode, "running": running, "goal": self.goal,
                "connected": session is not None, "detail": self.detail, "action": self.current_action,
                "contextTurns": len(self.memory.turns), "completedTasks": self.memory.completed,
                "sessionSeconds": round(time.monotonic() - self.connected_at) if self.connected_at else 0,
                "substrates": self.substrates.snapshot()}
        if session:
            try:
                pointer = session.body.controller.pos.rounded() if running and phase != "camera" else self._pointer
                from .geometry import Vec2
                observation = observe(session.world, Vec2(*pointer), session.body.clock.now()).to_obj()
                data.update(pointer=list(pointer), elements=[el.to_obj() for el in session.world.elements() if el.visible],
                            monitors=[list(rect.as_tuple()) for rect in session.world.monitors],
                            observation=observation, ticks=session.body.ticks,
                            perceptionMs=round(session.perception.stats.last_frame_s * 1000, 2),
                            perceptionErrors=session.perception.stats.errors,
                            motorOverruns=session.body.overruns)
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
        self.substrates.stop()


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
