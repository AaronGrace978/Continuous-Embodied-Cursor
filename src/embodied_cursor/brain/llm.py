# SPDX-License-Identifier: AGPL-3.0-or-later
"""A planner backed by a language model behind an OpenAI-compatible API.

Works with anything that speaks ``POST /chat/completions``: a hosted API or a
model running on the local GPU behind llama.cpp, Ollama, vLLM and the like.
Only the standard library is used.

The model is shown the screen as text and answers with intents as JSON. It is
never shown a screenshot and never asked for a coordinate, which is what keeps
its latency out of the motion: a slow answer delays the next decision, not the
cursor.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..intents import ACTIONS, Intent, intent_from_obj
from .planner import Episode, Plan, PlannerError

SYSTEM_PROMPT = """\
You operate a desktop through an embodied cursor. You decide WHAT to do; a \
separate real-time controller decides HOW to move. You never see pixels and \
never give coordinates: you name UI elements and the controller finds them, \
follows them if they move, waits if they are covered, and reports back.

Each turn you get the goal, a listing of what is on screen, and the outcomes \
of what was tried. Answer with ONE JSON object and nothing else:

{"intents": [<intent>, ...], "done": <bool>, "note": "<short reason>"}

"done" is true when the goal will be complete once these intents succeed (or \
is already complete, with an empty list). To give up, answer \
{"give_up": "<why>"}.

Intents:
{"action":"click","target":T,"button":"left|middle|right","count":1,"expect":E}
{"action":"hover","target":T,"dwell":0.4,"expect":E}
{"action":"move","target":T}
{"action":"drag","target":T,"to":T,"expect":E}        (or "offset":[dx,dy])
{"action":"scrub","target":T,"value":N}               (or "fraction":0..1; optional "duration" seconds)
{"action":"scroll","target":T,"direction":"down|up|left|right","until":T}   (or "notches":N)
{"action":"type","text":"...","target":T,"enter":false,"clear":false}   (clear replaces what the field holds)
{"action":"key","keys":"ctrl+s"}
{"action":"wait","expect":E}

T is a selector string: role "name", for example  button "Export"  or \
menu_item "PDF document" in menu "File"  or just  "Export". Use names exactly \
as listed. Roles: button, checkbox, radio, toggle, link, menu, menu_item, tab, \
list_item, text_field, combo_box, slider, window, dialog, label.
E is optional: {"appears":T} | {"vanishes":T} | {"on":T,"state":"checked"} | \
{"on":T,"value":N,"tolerance":N}. Add one when the next step depends on it.

Entries listed under "in closed menus" can be clicked directly; the \
controller opens the menus on the way. Plan several intents at once when the \
later ones do not depend on something you cannot see yet. If an outcome says \
"failed", read its reason and try something different rather than the same \
thing again.
"""


def _extract_json(text: str) -> Any:
    """The first JSON object in ``text``, tolerating code fences and chatter."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def parse_reply(text: str) -> Plan:
    """Validate a model reply into a :class:`Plan`; raises ``ValueError``."""
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        raise ValueError("the reply must be a JSON object")
    if obj.get("give_up"):
        return Plan(give_up=str(obj["give_up"]))
    raw = obj.get("intents", [])
    if not isinstance(raw, list):
        raise ValueError("'intents' must be a list")
    intents: list[Intent] = []
    for i, item in enumerate(raw):
        try:
            intents.append(intent_from_obj(item))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"intent {i + 1} is invalid: {exc}") from None
    done = bool(obj.get("done", False))
    if not intents and not done:
        raise ValueError("no intents, and not done: give intents, set done, or give_up")
    return Plan(tuple(intents), done, str(obj.get("note", "")))


def render_episode(episode: Episode, history_limit: int = 12) -> str:
    lines = [f"GOAL: {episode.goal}", "", "SCREEN:", episode.observation.text()]
    if episode.history:
        lines += ["", "TRIED SO FAR:"]
        skipped = len(episode.history) - history_limit
        if skipped > 0:
            lines.append(f"  ({skipped} earlier actions omitted)")
        for outcome in episode.history[-history_limit:]:
            line = "  " + outcome.describe()
            if outcome.observed.get("target_moved_away"):
                line += " (but the target moved away during the click; it probably missed)"
            lines.append(line)
    if episode.surprise:
        lines += ["", f"UNEXPECTED: {episode.surprise}. The rest of the last plan was not run."]
    return "\n".join(lines)


@dataclass
class LLMPlanner:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 60.0
    temperature: float = 0.0
    repairs: int = 2
    """How many times to show the model its own invalid reply and ask again."""

    json_mode: bool = True
    """Ask for ``response_format: json_object``; retried without if refused."""

    extra_headers: dict[str, str] = field(default_factory=dict)
    last_exchange: list[dict[str, str]] = field(default_factory=list, repr=False)

    @staticmethod
    def from_env() -> "LLMPlanner":
        base = os.environ.get("CEC_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        key = os.environ.get("CEC_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        model = os.environ.get("CEC_LLM_MODEL", "")
        if not model:
            raise PlannerError("set CEC_LLM_MODEL (and CEC_LLM_BASE_URL / CEC_LLM_API_KEY) to use the LLM planner")
        return LLMPlanner(base, model, key)

    def plan(self, episode: Episode) -> Plan:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_episode(episode)},
        ]
        problem = ""
        for _ in range(self.repairs + 1):
            reply = self._complete(messages)
            messages.append({"role": "assistant", "content": reply})
            try:
                plan = parse_reply(reply)
            except ValueError as exc:
                problem = str(exc)
                messages.append(
                    {
                        "role": "user",
                        "content": f"That reply could not be used: {problem}. "
                        f"Valid actions are {', '.join(ACTIONS)}. Answer again with one JSON object only.",
                    }
                )
                continue
            self.last_exchange = messages
            return plan
        self.last_exchange = messages
        raise PlannerError(f"the model kept answering in a form that cannot be used ({problem})")

    def _complete(self, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            data = self._post(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if self.json_mode and exc.code in (400, 404, 422) and "response_format" in detail:
                self.json_mode = False
                return self._complete(messages)
            raise PlannerError(f"HTTP {exc.code} from {self.base_url}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlannerError(f"could not reach {self.base_url}: {exc}") from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise PlannerError(f"unexpected response shape: {str(data)[:200]}") from None
        if not isinstance(content, str) or not content.strip():
            raise PlannerError("the model returned an empty reply")
        return content

    def _post(self, body: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
