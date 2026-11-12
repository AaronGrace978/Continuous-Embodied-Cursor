# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, session-only task memory. Reports are evidence, not executable plans."""
from __future__ import annotations

import json
from collections import deque

from .brain.agent import Report


class SessionMemory:
    def __init__(self, limit: int = 8):
        self.turns: deque[dict] = deque(maxlen=limit)
        self.completed = 0

    def remember(self, report: Report) -> None:
        actions = []
        for outcome in report.outcomes[-8:]:
            element = outcome.observed.get("element") or {}
            actions.append({
                "action": outcome.intent.describe()[:400],
                "status": outcome.status,
                "reason": outcome.reason[:200],
                "target": {key: element[key] for key in ("id", "role", "name") if key in element},
            })
        self.turns.append({"goal": report.goal[:1000], "status": report.status,
                           "reason": report.reason[:300], "actions": actions})
        self.completed += int(report.ok)

    def clear(self) -> None:
        self.turns.clear()
        self.completed = 0

    def context(self, substrates: dict | None = None) -> str:
        data: dict = {"previous_tasks": list(self.turns)}
        if substrates:
            data["substrates"] = substrates
        text = json.dumps(data, ensure_ascii=True)
        while len(text) > 12000 and data["previous_tasks"]:
            data["previous_tasks"].pop(0)
            text = json.dumps(data, ensure_ascii=True)
        return text
