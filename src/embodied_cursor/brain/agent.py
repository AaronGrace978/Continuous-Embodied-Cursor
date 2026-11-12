# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two speeds: a planner that thinks in goals, a body that moves in real time.

The planner is consulted at decision points only: at the start, when an
intent fails, when something unexpected opens, and when its last plan runs
out. Between those points the body executes on its own, tracking, re-planning
its trajectory and waiting out occlusions hundreds of times a second without
asking anyone.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..body import Body
from ..intents import Intent, Outcome
from ..world.element import Role, State
from .observation import describe, observe
from .planner import Episode, Plan, Planner, PlannerError

_INTERRUPTING = frozenset({Role.DIALOG, Role.WINDOW})
_CONSEQUENTIAL = frozenset({"click", "drag", "key", "type"})


@dataclass
class Report:
    goal: str
    status: str
    """``done``, ``failed``, ``interrupted`` or ``exhausted``."""

    reason: str = ""
    outcomes: list[Outcome] = field(default_factory=list)
    rounds: int = 0
    planning_time: float = 0.0
    """Seconds spent waiting for the planner."""

    acting_time: float = 0.0
    """Seconds the body spent executing intents."""

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def to_obj(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "reason": self.reason,
            "rounds": self.rounds,
            "planning_time": round(self.planning_time, 3),
            "acting_time": round(self.acting_time, 3),
            "outcomes": [o.to_obj() for o in self.outcomes],
        }

    def describe(self) -> str:
        head = f"{self.status}: {self.goal}"
        if self.reason:
            head += f" [{self.reason}]"
        return "\n".join([head, *("  " + o.describe() for o in self.outcomes)])


class Agent:
    """Runs a goal to completion by alternating planning and embodied execution."""

    def __init__(
        self,
        body: Body,
        planner: Planner,
        max_rounds: int = 8,
        max_intents: int = 40,
        settle: float = 0.12,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.body = body
        self.planner = planner
        self.max_rounds = max_rounds
        self.max_intents = max_intents
        self.settle = settle
        self.on_event = on_event

    def _say(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)

    def run(self, goal: str, cancel: threading.Event | None = None, context: str | Callable[[], str] = "") -> Report:
        body = self.body
        clock = body.clock
        report = Report(goal, "exhausted")
        surprise = ""
        for round_no in range(1, self.max_rounds + 1):
            report.rounds = round_no
            if cancel is not None and cancel.is_set():
                report.status, report.reason = "interrupted", "cancelled"
                return report

            observation = observe(body.world, body.actuator.position(), clock.now())
            started = clock.now()
            self._say("thinking", round=round_no)
            try:
                live_context = context() if callable(context) else context
                plan: Plan = self.planner.plan(Episode(goal, observation, list(report.outcomes), surprise, live_context))
            except PlannerError as exc:
                report.status, report.reason = "failed", f"planner: {exc}"
                return report
            report.planning_time += clock.now() - started
            if cancel is not None and cancel.is_set():
                report.status, report.reason = "interrupted", "cancelled"
                return report
            self._say("plan", round=round_no, intents=[i.describe() for i in plan.intents], done=plan.done, note=plan.note)

            if plan.give_up:
                report.status, report.reason = "failed", plan.give_up
                return report
            if not plan.intents:
                if plan.done:
                    report.status = "done"
                    return report
                report.status, report.reason = "failed", "the planner had nothing to try"
                return report

            surprise = ""
            cut_short = False
            for position, intent in enumerate(plan.intents):
                if len(report.outcomes) >= self.max_intents:
                    report.reason = f"gave up after {self.max_intents} actions"
                    return report
                revision = body.world.revision
                started = clock.now()
                outcome = body.execute(intent, cancel=cancel.is_set if cancel is not None else None)
                report.acting_time += clock.now() - started
                report.outcomes.append(outcome)
                self._say("outcome", **outcome.to_obj())

                if outcome.status == "interrupted":
                    # Someone took the mouse, or asked us to stop. Either way
                    # the right move is to get out of the way, not to re-plan.
                    report.status, report.reason = "interrupted", outcome.reason
                    return report
                if not outcome.ok or outcome.observed.get("target_moved_away"):
                    recent = report.outcomes[-3:]
                    if len(recent) == 3 and all(
                        not item.ok and item.reason == outcome.reason and item.intent == outcome.intent
                        for item in recent
                    ):
                        report.status = "failed"
                        report.reason = f"stopped after the same action failed three times ({outcome.reason})"
                        return report
                    cut_short = True
                    break
                if position < len(plan.intents) - 1:
                    if getattr(intent, "expect", None) is None and intent.action in _CONSEQUENTIAL:
                        # Give the consequences of the action a moment to show
                        # up before committing to the next one.
                        requested = clock.now()
                        body.perception.request_snapshot()
                        # A fresh snapshot can finish well before the fallback
                        # pause. Do not spend the rest of it doing nothing.
                        deadline = clock.now() + self.settle
                        while clock.now() < deadline:
                            if cancel is not None and cancel.is_set():
                                report.status, report.reason = "interrupted", "cancelled"
                                return report
                            body.rest(min(1 / body.config.hz, deadline - clock.now()))
                            if body.perception.fresh_since(requested) and self._effect_visible(revision):
                                break
                    surprise = self._surprise(revision, intent)
                    if surprise:
                        self._say("surprise", what=surprise)
                        cut_short = True
                        break
            if not cut_short and plan.done:
                report.status = "done"
                return report
        report.reason = report.reason or f"no result after {self.max_rounds} planning rounds"
        return report

    def _effect_visible(self, revision: int) -> bool:
        """Focus/press echoes alone do not prove the application has reacted."""
        transient = {State.FOCUSED, State.PRESSED, State.ACTIVE}
        for change in self.body.world.changes_since(revision):
            if change.kind in ("appeared", "vanished", "value", "name"):
                return True
            if change.kind == "state" and change.previous is not None:
                if (change.element.states ^ change.previous.states) - transient:
                    return True
        return False

    def _surprise(self, revision: int, intent: Intent) -> str:
        """Did something open that the plan did not ask for?

        A dialog appearing in the middle of a plan usually invalidates the
        rest of it, so the planner gets to look again. Popups are left alone:
        menus and tooltips come and go as a normal part of acting.
        """
        expect = getattr(intent, "expect", None)
        expected = expect.appears if expect is not None else None
        world = self.body.world
        for change in world.changes_since(revision):
            el = change.element
            if el.role not in _INTERRUPTING or not el.name:
                continue
            shown = change.kind == "appeared" or (
                change.kind == "state" and change.previous is not None and not change.previous.visible
            )
            if not shown:
                continue
            live = world.get(el.id)
            if live is None or not live.visible:
                continue
            if expected is not None and any(m.element.id == el.id for m in world.find(expected)):
                continue
            return f"{describe(el)} appeared"
        return ""
