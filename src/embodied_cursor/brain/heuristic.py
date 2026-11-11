# SPDX-License-Identifier: AGPL-3.0-or-later
"""An offline planner for plainly worded instructions.

It understands a small command language ("click Export", "open File > Export
as > PDF", "set Volume to 70%", "type "hello" into Search and press enter")
and nothing else. It exists so that the whole stack can be driven, tested and
demonstrated without a language model; anything open-ended needs the LLM
planner instead.

It still plans against what is on screen: steps that are already satisfied
are skipped, and a step that fails is retried or approached differently
before the planner gives up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..intents import Click, Drag, Expect, Hover, Intent, Key, Move, Scroll, Scrub, Type, Wait
from ..world.element import ALL_ROLES
from ..world.selector import Selector, canonical_role, name_score, roles_matching
from .observation import Observation
from .planner import Episode, Plan

_ROLE_WORDS = {
    "button": "button",
    "checkbox": "checkbox",
    "check box": "checkbox",
    "slider": "slider",
    "menu": "menu",
    "menu item": "menu_item",
    "item": None,
    "option": None,
    "entry": None,
    "link": "link",
    "tab": "tab",
    "field": "text_field",
    "text field": "text_field",
    "text box": "text_field",
    "box": "text_field",
    "input": "text_field",
    "toggle": "toggle",
    "switch": "toggle",
    "window": "window",
    "dialog": "dialog",
    "radio button": "radio",
    "label": "label",
    "dropdown": "combo_box",
    "combo box": "combo_box",
    "list": "list",
    "row": None,
}
_ROLE_PATTERN = "|".join(sorted((re.escape(w) for w in _ROLE_WORDS), key=len, reverse=True))

_KEY_NAMES = {
    "enter", "return", "escape", "esc", "tab", "space", "backspace", "delete", "del", "insert",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown", "page_up", "page_down",
    "ctrl", "control", "alt", "shift", "super", "meta", "cmd",
}  # fmt: skip
_RETRIABLE = {"occluded", "timeout", "expectation", "slipped", "not_found", "unreachable"}

Builder = Callable[[Observation], "list[Intent]"]


@dataclass
class _Step:
    text: str
    build: Builder
    fallback: Builder | None = None
    tries: int = 0
    used_fallback: bool = False
    emitted: int = 0
    succeeded: int = 0


class Unparseable(ValueError):
    pass


def split_clauses(goal: str) -> list[str]:
    """Split on ``;``, newlines and "then", leaving quoted text alone."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(goal):
        ch = goal[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'" and (i == 0 or not goal[i - 1].isalnum()):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        m = re.match(r"(?:\s*[;\n]\s*|,?\s+(?:and\s+)?then\s+)", goal[i:], re.I)
        if m:
            parts.append("".join(buf))
            buf = []
            i += m.end()
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    cleaned = []
    for part in parts:
        part = re.sub(r"^(?:please|and|now)\s+", "", part.strip(), flags=re.I).rstrip(".").strip()
        if part:
            cleaned.append(part)
    return cleaned


def parse_target(phrase: str) -> Selector:
    """``the "Export" button``, ``Export button``, ``button "Export"``, ``Export``."""
    phrase = phrase.strip().rstrip(".").strip()
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.I)
    if not phrase:
        raise Unparseable("nothing to aim at")
    if phrase.startswith("#"):
        return Selector(id=phrase[1:])

    within = None
    m = re.match(r"^(.*?)\s+(?:in|inside|of|on)\s+(?:the\s+)?(.+\s(?:window|dialog|menu|tab|list))$", phrase, re.I)
    if m and m.group(1).strip():
        within = parse_target(m.group(2))
        phrase = m.group(1).strip()

    quoted = re.search(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', phrase)
    if quoted:
        name = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
        rest = (phrase[: quoted.start()] + " " + phrase[quoted.end() :]).strip().lower()
        role = None
        if rest:
            role = _ROLE_WORDS[rest] if rest in _ROLE_WORDS else canonical_role(rest)
            if role is not None and role not in ALL_ROLES:
                raise Unparseable(f"'{rest}' is not a kind of element I know")
        return Selector(role=role, name=name, within=within)

    m = re.match(rf"^(.+?)\s+({_ROLE_PATTERN})$", phrase, re.I)
    if m:
        return Selector(role=_ROLE_WORDS[m.group(2).lower()], name=m.group(1).strip(), within=within)
    m = re.match(rf"^({_ROLE_PATTERN})\s+(.+)$", phrase, re.I)
    if m and m.group(1).lower() not in ("list", "box", "item", "option", "entry", "row", "input", "switch", "label"):
        return Selector(role=_ROLE_WORDS[m.group(1).lower()], name=m.group(2).strip(), within=within)
    return Selector(name=phrase, within=within)


def _looks_like_keys(text: str) -> bool:
    parts = [p.strip().lower() for p in text.split("+")]
    if not all(parts):
        return False
    if len(parts) == 1:
        return parts[0] in _KEY_NAMES or bool(re.fullmatch(r"f\d{1,2}", parts[0]))
    return all(p in _KEY_NAMES or len(p) == 1 or re.fullmatch(r"f\d{1,2}", p) for p in parts)


def _find(obs: Observation, selector: Selector) -> dict | None:
    """Best match for a selector among what the observation lists."""
    best, best_score = None, 0.0
    for el in obs.elements():
        if selector.id is not None:
            if el["id"] == selector.id:
                return el
            continue
        if selector.role is not None and el["role"] not in roles_matching(selector.role):
            continue
        score = name_score(selector.name, el["name"]) if selector.name is not None else 1.0
        if score > best_score:
            best, best_score = el, score
    return best


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


_PATH_SEP = re.compile(r"\s*(?:>|->|→|›|»)\s*")
_QUOTED = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''


def parse_clause(clause: str) -> _Step:
    """Turn one instruction into a step, or raise :class:`Unparseable`."""
    c = clause.strip()

    m = re.match(r"^(?:press|hit)\s+(?:the\s+)?(.+?)(?:\s+keys?)?$", c, re.I)
    if m and _looks_like_keys(m.group(1)):
        keys = m.group(1).lower().replace(" ", "")
        return _Step(c, lambda obs: [Key(keys)])

    m = re.match(
        rf"^(?:type|enter|write)\s+({_QUOTED}|.+?)"
        r"(?:\s+(?:in|into|in to)\s+(.+?))?"
        r"(\s+(?:and|then)\s+(?:press\s+|hit\s+)?(?:enter|return))?$",
        c,
        re.I,
    )
    if m:
        text = _unquote(m.group(1))
        target = parse_target(m.group(2)) if m.group(2) else None
        enter = bool(m.group(3))
        return _Step(c, lambda obs: [Type(text, target, enter)])

    m = re.match(
        rf"^(?:fill|replace)\s+(.+?)\s+with\s+({_QUOTED}|.+?)(\s+(?:and|then)\s+(?:press\s+|hit\s+)?(?:enter|return))?$",
        c,
        re.I,
    )
    if m:
        target = parse_target(m.group(1))
        text = _unquote(m.group(2))
        enter = bool(m.group(3))
        return _Step(c, lambda obs: [Type(text, target, enter, clear=True)])

    m = re.match(
        r"^(?:set|slide|scrub|adjust|change|drag|move)\s+(.+?)\s+to\s+(-?\d+(?:\.\d+)?)\s*(%|percent)?"
        r"(?:\s+(slowly)|\s+(?:over|in)\s+(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?)?$",
        c,
        re.I,
    )
    if m:
        target = parse_target(m.group(1))
        number = float(m.group(2))
        duration = 2.5 if m.group(4) else float(m.group(5)) if m.group(5) else None
        if m.group(3):
            intent: Intent = Scrub(target, fraction=number / 100.0, duration=duration)
        else:
            intent = Scrub(target, value=number, duration=duration)
        return _Step(c, lambda obs: [intent])

    m = re.match(r"^drag\s+(.+?)\s+(?:to|onto|on to|into)\s+(.+)$", c, re.I)
    if m:
        source, dest = parse_target(m.group(1)), parse_target(m.group(2))
        return _Step(c, lambda obs: [Drag(source, to=dest)])

    m = re.match(r"^(check|tick|enable|turn on|uncheck|untick|disable|turn off)\s+(.+)$", c, re.I)
    if m:
        want = m.group(1).lower() in ("check", "tick", "enable", "turn on")
        target = parse_target(m.group(2))

        def build_check(obs: Observation) -> list[Intent]:
            el = _find(obs, target)
            if el is not None and ("checked" in el.get("states", ()) or "pressed" in el.get("states", ())) == want:
                return []
            state = "checked" if want else "not_checked"
            return [Click(target, expect=Expect(state=state, on=target))]

        return _Step(c, build_check)

    m = re.match(r"^hover\s+(?:over\s+|on\s+)?(.+)$", c, re.I)
    if m:
        target = parse_target(m.group(1))
        return _Step(c, lambda obs: [Hover(target)])

    m = re.match(r"^(?:move|go|point)\s+(?:to|at|over)\s+(.+)$", c, re.I)
    if m:
        target = parse_target(m.group(1))
        return _Step(c, lambda obs: [Move(target)])

    m = re.match(
        r"^scroll(?:\s+(up|down|left|right))?(?:\s+(\d+)(?:\s*(?:notch(?:es)?|lines?|times?|steps?))?)?"
        r"(?:\s+(?:in|on)\s+(.+?))?(?:\s+(?:until|to)\s+(.+?)(?:\s+(?:is visible|appears|shows|is shown))?)?$",
        c,
        re.I,
    )
    if m and (m.group(2) or m.group(4)):
        direction = (m.group(1) or "down").lower()
        notches = int(m.group(2)) if m.group(2) else None
        where = parse_target(m.group(3)) if m.group(3) else None
        until = parse_target(m.group(4)) if m.group(4) else None

        def build_scroll(obs: Observation) -> list[Intent]:
            if until is not None and _find(obs, until) is not None and notches is None:
                return []
            return [Scroll(where, notches, until, direction)]

        return _Step(c, build_scroll)

    m = re.match(
        r"^wait\s+(?:for|until)\s+(.+?)"
        r"(?:\s+(appears|to appear|is visible|shows|vanishes|disappears|to disappear|is gone|closes|to close))?$",
        c,
        re.I,
    )
    if m:
        target = parse_target(m.group(1))
        gone = (m.group(2) or "").lower() in ("vanishes", "disappears", "to disappear", "is gone", "closes", "to close")
        expect = Expect(vanishes=target, timeout=10.0) if gone else Expect(appears=target, timeout=10.0)
        return _Step(c, lambda obs: [Wait(expect)])

    m = re.match(
        r"^(double[- ]click|right[- ]click|middle[- ]click|click|press|tap|select|choose|open|activate|pick)"
        r"\s+(?:on\s+)?(.+)$",
        c,
        re.I,
    )
    if m:
        verb = re.sub(r"[- ]", " ", m.group(1).lower())
        button = "right" if verb.startswith("right") else "middle" if verb.startswith("middle") else "left"
        count = 2 if verb.startswith("double") else 1
        names = [n for n in _PATH_SEP.split(m.group(2)) if n.strip()]
        if len(names) > 1:
            hops = [parse_target(n) for n in names]
            nested = None
            for hop in hops:
                nested = Selector(role=hop.role, name=hop.name, id=hop.id, within=nested)
            leaf = nested
            assert leaf is not None

            def build_path(obs: Observation) -> list[Intent]:
                return [Click(leaf, button, count)]

            def build_explicit(obs: Observation) -> list[Intent]:
                out: list[Intent] = []
                for i, hop in enumerate(hops):
                    last = i == len(hops) - 1
                    if not last and _find(obs, hops[i + 1]) is not None:
                        continue  # the next level is already showing
                    expect = None if last else Expect(appears=hops[i + 1])
                    out.append(Click(hop, button if last else "left", count if last else 1, expect))
                return out

            return _Step(c, build_path, fallback=build_explicit)
        target = parse_target(m.group(2))
        return _Step(c, lambda obs: [Click(target, button, count)])

    raise Unparseable(
        f"I do not understand {clause!r}. The offline planner knows: click / double-click / right-click X, "
        "open A > B > C, hover X, move to X, set X to N or N%, drag X onto Y, check / uncheck X, "
        'type "text" into X, press ctrl+s, scroll down until X, wait for X.'
    )


@dataclass
class HeuristicPlanner:
    """Rule-based planner for the command language described in this module."""

    max_tries: int = 3
    _goal: str | None = field(default=None, repr=False)
    _steps: list[_Step] = field(default_factory=list, repr=False)
    _cursor: int = field(default=0, repr=False)
    _pending: list[int] = field(default_factory=list, repr=False)
    _seen: int = field(default=0, repr=False)

    def plan(self, episode: Episode) -> Plan:
        if episode.goal != self._goal or len(episode.history) < self._seen:
            try:
                self._steps = [parse_clause(c) for c in split_clauses(episode.goal)]
            except Unparseable as exc:
                self._goal = None
                return Plan(give_up=str(exc))
            self._goal = episode.goal
            self._cursor, self._pending, self._seen = 0, [], len(episode.history)
            if not self._steps:
                return Plan(give_up="there is nothing to do in that goal")

        problem = self._digest(episode)
        if problem:
            return Plan(give_up=problem)

        intents: list[Intent] = []
        self._pending = []
        notes: list[str] = []
        for index in range(self._cursor, len(self._steps)):
            step = self._steps[index]
            builder = step.fallback if step.used_fallback and step.fallback else step.build
            built = builder(episode.observation)
            if not built:
                notes.append(f"'{step.text}' already holds")
            step.emitted, step.succeeded = len(built), 0
            intents.extend(built)
            self._pending.extend([index] * len(built))
        if not intents:
            self._cursor = len(self._steps)
        return Plan(tuple(intents), done=True, note="; ".join(notes))

    def _digest(self, episode: Episode) -> str:
        """Fold the outcomes of the last plan into step progress."""
        fresh = episode.history[self._seen :]
        self._seen = len(episode.history)
        for index, outcome in zip(self._pending, fresh):
            step = self._steps[index]
            slipped = outcome.ok and outcome.observed.get("target_moved_away")
            if outcome.ok and not slipped:
                step.succeeded += 1
                if step.succeeded >= step.emitted:
                    self._cursor = index + 1
                continue
            reason = "slipped" if slipped else outcome.reason
            self._cursor = index
            step.tries += 1
            if reason == "not_found" and step.fallback is not None and not step.used_fallback:
                step.used_fallback = True
                return ""
            if reason in _RETRIABLE and step.tries < self.max_tries:
                return ""
            detail = f": {outcome.detail}" if outcome.detail else ""
            return f"'{step.text}' did not work ({reason}{detail})"
        # Intents that never ran because the plan was cut short are planned
        # again from the current step; steps that already hold build nothing.
        return ""
