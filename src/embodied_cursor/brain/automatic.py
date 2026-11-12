# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep explicit commands local; use a model only for open-ended goals."""
import json
import re
from dataclasses import replace
from .heuristic import HeuristicPlanner, Unparseable, parse_clause, split_clauses
from .planner import Episode, Plan, Planner


def resolve_followup(episode: Episode) -> str | None:
    """Ground a small set of explicit references in the last successful target.

    Only a target still present in this observation can be reused. Never replay
    a prior plan, infer a coordinate, or use substrate memories as UI evidence.
    """
    goal = episode.goal.strip()
    move = re.fullmatch(r'(click|double[- ]click|right[- ]click|hover|move to|go to)\s+(it|that|there)[.!]?', goal, re.I)
    fill = re.fullmatch(r'fill\s+(it|that|there)\s+with\s+(.+)', goal, re.I | re.S)
    if not (move or fill):
        return None
    try:
        previous = json.loads(episode.context)['previous_tasks'][-1]
        action = previous['actions'][-1]
        target = action['target']
        ident = target['id']
        if previous['status'] != 'done' or action['status'] != 'ok':
            return None
        if not isinstance(ident, str) or not re.fullmatch(r'[\w:.-]+', ident):
            return None
        live = next((item for item in episode.observation.elements() if item['id'] == ident), None)
        if live is None or (fill and live['role'] != 'text_field'):
            return None
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if fill:
        return f'fill #{ident} with {fill.group(2)}'
    verb = move.group(1).lower().replace('go to', 'move to')
    return f'{verb} #{ident}'


def needs_context(goal: str) -> bool:
    # A grammatical command can still need conversation context to identify its target.
    masked = re.sub(r'''(["']).*?\1''', '<literal>', goal)
    for clause in split_clauses(masked):
        if re.match(r'^(?:click|double[- ]click|right[- ]click|press|hover(?: over)?|move to|go to|point to|set|fill|replace|open|drag)\s+(?:it|that|there)\b', clause, re.I):
            return True
        # Fill/replace's remaining words are literal text, not another target.
        if re.match(r'^(?:fill|replace)\b', clause, re.I):
            continue
        if re.search(r'\b(?:into|onto|over|in|on)\s+(?:it|that|there)(?:\s+and\s+(?:press\s+)?enter)?[.!]?$', clause, re.I):
            return True
        if re.match(r'^drag\b.*\bto\s+(?:it|that|there)[.!]?$', clause, re.I):
            return True
    return False


def is_command(goal: str) -> bool:
    if needs_context(goal):
        return False
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
        self._resolved_goal: str | None = None

    def plan(self, episode: Episode):
        if not episode.history or self._planner is None:
            self._resolved_goal = resolve_followup(episode)
            local = self._resolved_goal or episode.goal
            self._planner = HeuristicPlanner() if is_command(local) else self.fallback()
            if needs_context(local) and isinstance(self._planner, HeuristicPlanner):
                return Plan(give_up="I cannot resolve that reference locally. Name a visible control or configure a model for this instruction")
        return self._planner.plan(replace(episode, goal=self._resolved_goal) if self._resolved_goal else episode)
