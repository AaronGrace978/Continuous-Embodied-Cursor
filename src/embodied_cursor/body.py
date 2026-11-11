# SPDX-License-Identifier: GPL-3.0-or-later
"""The body: the fast loop that owns the pointer.

The body runs at a fixed rate (120 Hz by default). On every tick it

1. reads the real pointer back and yields at once if a person has moved it,
2. lets perception refresh the attended part of the world model,
3. steps the active skill, which re-aims at the live target,
4. advances the motion controller and moves the real pointer.

It executes one intent at a time and reports an :class:`Outcome`. It never
calls a planner; the planner calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .geometry import Vec2
from .intents import Intent, Outcome
from .motor.controller import MotionController, MotionGoal, MotorConfig
from .runtime import Actuator, Clock
from .skills import SkillFailure, skill_for
from .trace import Trace
from .world.element import UIElement
from .world.memory import SpatialMemory
from .world.model import WorldModel
from .world.perception import Perception


@dataclass(frozen=True, slots=True)
class BodyConfig:
    hz: float = 120.0
    """Motor loop rate."""

    resolve_timeout: float = 2.5
    """Seconds to wait for a target that is not on screen to show up."""

    occlusion_patience: float = 1.0
    """Seconds to wait for a covered or disabled target to become usable."""

    reach_timeout: float = 10.0
    settle_time: float = 0.035
    """Seconds the pointer must rest on the target before a press."""

    press_time: float = 0.06
    click_gap: float = 0.09
    confirm_time: float = 0.08
    """Seconds an expected condition must keep holding to count as confirmed."""

    focus_delay: float = 0.08

    override: bool = True
    """Yield to a person who moves the mouse."""

    override_px: float = 4.0
    """Pointer deviation from the commanded position that counts as a person."""

    auto_reveal: bool = True
    """Replay remembered triggers to bring a hidden target on screen."""

    reveal_after: float = 0.15
    reveal_window: float = 0.6
    """Seconds after an action during which new elements are credited to it."""

    hover_reveal_dwell: float = 0.3
    scrub_iterations: int = 24
    scrub_probe: float = 6.0
    """Pixels of exploratory drag used to learn which way a control runs."""

    scrub_cross: tuple[float, ...] = (0.5, 0.75, 0.25, 0.9, 0.1)
    """Positions across a slider's thickness at which to feel for its handle."""

    scroll_rate: float = 25.0
    """Wheel notches per second."""

    scroll_limit: int = 200
    type_rate: float = 60.0
    """Characters per second."""

    memory_interval: float = 0.5


class HumanOverride(Exception):
    def __init__(self, actual: Vec2, commanded: tuple[int, int]) -> None:
        super().__init__(f"pointer at {actual.rounded()} but commanded to {commanded}")
        self.actual = actual
        self.commanded = commanded


class Cancelled(Exception):
    pass


class Context:
    """What a skill can see and do during one tick."""

    def __init__(self, body: "Body") -> None:
        self._body = body
        self.world: WorldModel = body.world
        self.memory: SpatialMemory | None = body.memory
        self.controller: MotionController = body.controller
        self.actuator: Actuator = body.actuator
        self.perception: Perception = body.perception
        self.config: BodyConfig = body.config
        self.now = 0.0
        self.dt = 1.0 / body.config.hz
        self.goal: MotionGoal | None = None
        self._hold = False
        self._attended: dict[str, None] = {}
        self._buttons: set[int] = set()
        self._keys: list[str] = []

    @property
    def pos(self) -> Vec2:
        return self.controller.pos

    def hold(self) -> None:
        """Ask the cursor to come to rest where it is."""
        self._hold = True

    def attend(self, ident: str) -> None:
        if ident not in self._attended:
            self._attended[ident] = None
            self.perception.attend(self._attended)

    def emit(self, kind: str, **data: Any) -> None:
        self._body._emit(kind, data)

    def press(self, button: int) -> None:
        self._body._before_action()
        self.actuator.button(button, True)
        self._buttons.add(button)
        self.emit("press", button=button, at=list(self.pos.rounded()))

    def release(self, button: int) -> None:
        if button in self._buttons:
            self.actuator.button(button, False)
            self._buttons.discard(button)
            self.emit("release", button=button, at=list(self.pos.rounded()))

    def key_down(self, name: str) -> None:
        self.actuator.key(name, True)
        self._keys.append(name)

    def key_up(self, name: str) -> None:
        if name in self._keys:
            self.actuator.key(name, False)
            self._keys.remove(name)

    def tap(self, name: str) -> None:
        self.actuator.key(name, True)
        self.actuator.key(name, False)

    def watch_reveal(self, trigger: UIElement, how: str) -> None:
        self._body._watch_reveal(trigger, how)

    def release_everything(self) -> None:
        for b in sorted(self._buttons):
            self.release(b)
        for k in reversed(self._keys[:]):
            self.key_up(k)

    def button_mask(self) -> int:
        return sum(1 << (b - 1) for b in self._buttons)

    def _reset(self) -> None:
        self.goal = None
        self._hold = False
        self._attended = {}
        self.perception.attend(())


class Body:
    def __init__(
        self,
        actuator: Actuator,
        perception: Perception,
        clock: Clock,
        memory: SpatialMemory | None = None,
        motor: MotorConfig | None = None,
        config: BodyConfig | None = None,
        trace: Trace | None = None,
    ) -> None:
        self.actuator = actuator
        self.perception = perception
        self.world = perception.world
        self.clock = clock
        self.memory = memory
        self.config = config or BodyConfig()
        self.trace = trace if trace is not None else Trace()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.controller = MotionController(actuator.position(), motor, self.world.monitors)
        self.ctx = Context(self)
        self._period = 1.0 / self.config.hz
        self._emitted: tuple[int, int] | None = None
        self._last_tick: float | None = None
        self._next_tick = clock.now()
        self._next_memory = 0.0
        self._watch: tuple[UIElement, str, int, float] | None = None
        self.ticks = 0
        self.overruns = 0

    # -- public ----------------------------------------------------------

    def execute(self, intent: Intent, cancel: Callable[[], bool] | None = None) -> Outcome:
        """Carry out one intent, blocking until it succeeds or fails."""
        ctx = self.ctx
        ctx._reset()
        self._sync()
        started = self.clock.now()
        ctx.now = started
        self._emit("intent", {"intent": intent.to_obj()})

        status, reason, detail = "ok", "", ""
        observed: dict[str, Any] = {}
        human: HumanOverride | None = None
        gen = None
        try:
            gen = skill_for(intent)(ctx, intent)
            while True:
                self._tick_begin()
                if cancel is not None and cancel():
                    raise Cancelled()
                try:
                    next(gen)
                except StopIteration as stop:
                    observed = stop.value or {}
                    break
                self._tick_end()
        except SkillFailure as failure:
            status, reason, detail, observed = "failed", failure.reason, failure.detail, failure.observed
        except HumanOverride as override:
            status, reason, detail = "interrupted", "human_override", str(override)
            human = override
        except Cancelled:
            status, reason = "interrupted", "cancelled"
        finally:
            if gen is not None:
                gen.close()
            ctx.release_everything()

        if human is not None:
            # The person has the mouse: do not fight for it, not even to brake.
            self.controller.reset(human.actual)
            self._emitted = human.actual.rounded()
        elif status != "ok":
            self._come_to_rest()

        outcome = Outcome(intent, status, reason, detail, self.clock.now() - started, observed)
        self._emit("outcome", outcome.to_obj())
        return outcome

    def rest(self, seconds: float) -> None:
        """Keep perceiving for a while without acting."""
        self.ctx._reset()
        self._sync()
        until = self.clock.now() + seconds
        try:
            while self.clock.now() < until:
                self._tick_begin()
                self._tick_end()
        except HumanOverride as override:
            self.controller.reset(override.actual)
            self._emitted = override.actual.rounded()

    def close(self) -> None:
        self.ctx.release_everything()
        self._flush_watch(final=True)
        if self.memory is not None:
            self.memory.observe(self.world)
            self.memory.save()

    # -- the loop --------------------------------------------------------

    def _sync(self) -> None:
        """Adopt the real pointer position if it is not where we left it."""
        self.controller.bounds = self.world.monitors
        actual = self.actuator.position()
        if self._emitted is None or actual.rounded() != self._emitted:
            self.controller.reset(actual)
            self._emitted = actual.rounded()
        now = self.clock.now()
        self._last_tick = None
        self._next_tick = now

    def _tick_begin(self) -> None:
        cfg = self.config
        now = self.clock.now()
        ctx = self.ctx
        ctx.dt = self._period if self._last_tick is None else max(now - self._last_tick, 1e-6)
        ctx.now = now
        self._last_tick = now
        ctx.goal = None
        ctx._hold = False
        self.ticks += 1

        if cfg.override and self._emitted is not None:
            actual = self.actuator.position()
            if actual.dist(Vec2(*self._emitted)) > cfg.override_px:
                raise HumanOverride(actual, self._emitted)

        if not self.perception.threaded:
            self.perception.pump()
        self._flush_watch()
        if self.memory is not None and now >= self._next_memory:
            self.memory.observe(self.world)
            self._next_memory = now + cfg.memory_interval

    def _tick_end(self) -> None:
        ctx = self.ctx
        ctrl = self.controller
        if ctx.goal is not None:
            ctrl.set_goal(ctx.goal)
        elif ctx._hold and ctrl.goal is not None:
            ctrl.brake()
        ctrl.step(ctx.dt)

        xy = ctrl.pos.rounded()
        if xy != self._emitted:
            self.actuator.move_to(*xy)
            self._emitted = xy
        goal = ctrl.goal.pos if ctrl.goal is not None else None
        self.trace.sample(ctx.now, ctrl.pos, ctrl.vel, goal, ctx.button_mask())

        self._next_tick += self._period
        now = self.clock.now()
        if self._next_tick < now:
            self.overruns += 1
            self._next_tick = now
        self.clock.sleep_until(self._next_tick)

    def _come_to_rest(self) -> None:
        self.ctx.goal = None
        self.controller.brake()
        deadline = self.clock.now() + 0.5
        try:
            while self.controller.moving and self.clock.now() < deadline:
                self._tick_begin()
                self._tick_end()
        except HumanOverride as override:
            self.controller.reset(override.actual)
            self._emitted = override.actual.rounded()

    # -- affordance learning ----------------------------------------------

    def _watch_reveal(self, trigger: UIElement, how: str) -> None:
        if self.memory is None:
            return
        self._flush_watch(final=True)
        self._watch = (trigger, how, self.world.revision, self.clock.now() + self.config.reveal_window)

    def _before_action(self) -> None:
        """A new press ends the window in which changes are credited to the last one."""
        self._flush_watch(final=True)

    def _flush_watch(self, final: bool = False) -> None:
        if self._watch is None or self.memory is None:
            return
        trigger, how, revision, deadline = self._watch
        if not final and self.clock.now() < deadline:
            return
        self._watch = None
        shown: dict[str, UIElement] = {}
        for change in self.world.changes_since(revision):
            el = change.element
            became_visible = change.kind == "appeared" or (
                change.kind == "state" and change.previous is not None and not change.previous.visible
            )
            if became_visible and el.id != trigger.id:
                live = self.world.get(el.id)
                if live is not None and live.visible:
                    shown[el.id] = live
        if shown:
            self.memory.note_reveal(trigger, how, shown.values(), self.world)
            self._emit("learned", {"trigger": trigger.name, "how": how, "revealed": len(shown)})

    # -- events ----------------------------------------------------------

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        t = self.clock.now()
        self.trace.event(t, kind, **data)
        if self.listeners:
            event = {"t": round(t, 4), "kind": kind, **data}
            for fn in self.listeners:
                fn(event)
