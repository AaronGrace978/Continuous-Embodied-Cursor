# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contract between the slow brain and the fast body.

A planner never sees pixels and never produces coordinates. It is shown an
:class:`Observation` and the outcomes of what has been tried so far, and
answers with the next few :class:`Intent` objects. How long it takes to
answer does not matter to the motion: the body runs its own loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..intents import Intent, Outcome
from .observation import Observation


class PlannerError(RuntimeError):
    """The planner could not produce a usable answer."""


@dataclass(frozen=True, slots=True)
class Plan:
    intents: tuple[Intent, ...] = ()
    done: bool = False
    """True if the goal will be complete once ``intents`` have succeeded."""

    note: str = ""
    give_up: str = ""
    """Non-empty when the planner sees no way to reach the goal."""


@dataclass
class Episode:
    """Everything a planner may look at when asked for the next plan."""

    goal: str
    observation: Observation
    history: list[Outcome] = field(default_factory=list)
    surprise: str = ""
    """Why the previous plan was cut short, if it was not a failure."""

    context: str = ""
    """Bounded session history and cached substrate readings, never a new goal."""


class Planner(Protocol):
    def plan(self, episode: Episode) -> Plan: ...
