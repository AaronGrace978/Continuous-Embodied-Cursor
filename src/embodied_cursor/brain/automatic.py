# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep explicit commands local; use a model only for open-ended goals."""
from .heuristic import HeuristicPlanner, Unparseable, parse_clause, split_clauses
from .planner import Episode, Planner


def is_command(goal: str) -> bool:
    clauses = split_clauses(goal)
    if not clauses:
        return False
    try:
        for clause in clauses:
            parse_clause(clause)
    except Unparseable:
        return False
    return True


class AutomaticPlanner:
    def __init__(self, fallback):
        self.fallback = fallback
        self._planner: Planner | None = None

    def plan(self, episode: Episode):
        if not episode.history or self._planner is None:
            self._planner = HeuristicPlanner() if is_command(episode.goal) else self.fallback()
        return self._planner.plan(episode)
