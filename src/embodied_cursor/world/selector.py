# SPDX-License-Identifier: GPL-3.0-or-later
"""Semantic selectors.

The planner never emits pixel coordinates. It names what it wants
(``button "Export"``, ``slider "Volume" in dialog "Sound"``) and the world
model resolves that against the live element tree on every perception frame.
That indirection is what lets a target move while the cursor is in flight.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .element import ALL_ROLES

_ROLE_ALIASES = {
    "btn": "button",
    "check": "checkbox",
    "check_box": "checkbox",
    "radio_button": "radio",
    "toggle_button": "toggle",
    "switch": "toggle",
    "menuitem": "menu_item",
    "menubar": "menu_bar",
    "textbox": "text_field",
    "text_box": "text_field",
    "entry": "text_field",
    "input": "text_field",
    "field": "text_field",
    "combobox": "combo_box",
    "dropdown": "combo_box",
    "scrollbar": "scroll_bar",
    "listitem": "list_item",
    "frame": "window",
    "range": "slider",
    "scale": "slider",
}


def canonical_role(role: str) -> str:
    r = role.strip().lower().replace(" ", "_").replace("-", "_")
    return _ROLE_ALIASES.get(r, r)


def normalise_name(name: str) -> str:
    """Fold case, accents, mnemonics and trailing ellipses out of a label."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("_", " ").replace("&", " ")
    s = s.replace("\u2026", "").replace("...", "")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s.rstrip(":").strip()


def name_score(query: str, candidate: str) -> float:
    """How well ``candidate`` answers a request for ``query``, in ``[0, 1]``.

    Matching is deliberately conservative: whole words only, no edit-distance
    guessing. Clicking ``Row 1`` when asked for ``Row 41`` is far worse than
    reporting that ``Row 41`` is not on screen.
    """
    q, c = normalise_name(query), normalise_name(candidate)
    if not q:
        return 1.0
    if not c:
        return 0.0
    if q == c:
        return 1.0
    if c.startswith(q) and (len(c) == len(q) or not c[len(q)].isalnum()):
        return 0.85
    if re.search(rf"(?<![^\W_]){re.escape(q)}(?![^\W_])", c):
        return 0.75
    return 0.0


@dataclass(frozen=True, slots=True)
class Selector:
    role: str | None = None
    name: str | None = None
    exact: bool = False
    """Require the normalised name to match exactly rather than fuzzily."""

    id: str | None = None
    app: str | None = None
    within: "Selector | None" = None
    """Some ancestor must match this selector."""

    index: int | None = None
    """Pick the n-th match (0-based) in reading order instead of the best."""

    states: frozenset[str] = frozenset()
    """States the element must have, e.g. ``checked``."""

    def __post_init__(self) -> None:
        if self.role is not None:
            object.__setattr__(self, "role", canonical_role(self.role))
        if self.role is None and self.name is None and self.id is None:
            raise ValueError("a selector needs at least a role, a name or an id")

    # -- construction ----------------------------------------------------

    @staticmethod
    def of(spec: "Selector | str | dict[str, Any]") -> "Selector":
        if isinstance(spec, Selector):
            return spec
        if isinstance(spec, str):
            return Selector.parse(spec)
        if isinstance(spec, dict):
            return Selector.from_obj(spec)
        raise TypeError(f"cannot make a selector from {type(spec).__name__}")

    @staticmethod
    def from_obj(obj: dict[str, Any]) -> "Selector":
        known = {"role", "name", "exact", "id", "app", "within", "in", "index", "states"}
        unknown = set(obj) - known
        if unknown:
            raise ValueError(f"unknown selector field(s): {', '.join(sorted(unknown))}")
        within = obj.get("within", obj.get("in"))
        states = obj.get("states") or ()
        if isinstance(states, str):
            states = (states,)
        return Selector(
            role=obj.get("role") or None,
            name=obj.get("name") if obj.get("name") not in ("", None) else None,
            exact=bool(obj.get("exact", False)),
            id=obj.get("id") or None,
            app=obj.get("app") or None,
            within=Selector.of(within) if within else None,
            index=int(obj["index"]) if obj.get("index") is not None else None,
            states=frozenset(states),
        )

    _TOKEN = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|\[(\d+)\]|(#\S+)|(\S+))')

    @staticmethod
    def parse(text: str) -> "Selector":
        """Parse the compact form: ``[role] ["name"] [[n]] [in <selector>]``.

        Examples: ``button "Export"``, ``"Export"``, ``slider``,
        ``menu_item "Copy" [1] in menu "Edit"``, ``#atspi:42``. A bare word
        that is not a known role is taken as a name.
        """
        tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = Selector._TOKEN.match(text, pos)
            if not m:
                break
            pos = m.end()
            dq, sq, idx, ident, word = m.groups()
            if dq is not None:
                tokens.append(("name", re.sub(r"\\(.)", r"\1", dq)))
            elif sq is not None:
                tokens.append(("name", re.sub(r"\\(.)", r"\1", sq)))
            elif idx is not None:
                tokens.append(("index", idx))
            elif ident is not None:
                tokens.append(("id", ident[1:]))
            elif word is not None:
                tokens.append(("word", word))
        if not tokens:
            raise ValueError("empty selector")

        split = next(
            (i for i, (kind, val) in enumerate(tokens) if kind == "word" and val.lower() == "in" and i > 0),
            None,
        )
        head, tail = (tokens, []) if split is None else (tokens[:split], tokens[split + 1 :])
        within = None
        if split is not None:
            if not tail:
                raise ValueError("'in' must be followed by a selector")
            rest = " ".join(
                f'"{v}"' if k == "name" else f"[{v}]" if k == "index" else f"#{v}" if k == "id" else v
                for k, v in tail
            )
            within = Selector.parse(rest)

        role = name = ident = None
        index = None
        words: list[str] = []
        for kind, val in head:
            if kind == "name":
                name = val
            elif kind == "index":
                index = int(val)
            elif kind == "id":
                ident = val
            else:
                words.append(val)
        if words:
            first = canonical_role(words[0])
            if first in ALL_ROLES:
                role = first
                words = words[1:]
            if words:
                if name is not None:
                    raise ValueError(f"unexpected word(s) in selector: {' '.join(words)}")
                name = " ".join(words)
        return Selector(role=role, name=name, id=ident, within=within, index=index)

    # -- presentation ----------------------------------------------------

    def to_obj(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.role:
            out["role"] = self.role
        if self.name is not None:
            out["name"] = self.name
        if self.exact:
            out["exact"] = True
        if self.id:
            out["id"] = self.id
        if self.app:
            out["app"] = self.app
        if self.index is not None:
            out["index"] = self.index
        if self.states:
            out["states"] = sorted(self.states)
        if self.within:
            out["within"] = self.within.to_obj()
        return out

    def describe(self) -> str:
        parts: list[str] = []
        if self.id:
            parts.append(f"#{self.id}")
        if self.role:
            parts.append(self.role)
        if self.name is not None:
            parts.append(f'"{self.name}"')
        if self.index is not None:
            parts.append(f"[{self.index}]")
        if self.within:
            parts.append(f"in {self.within.describe()}")
        return " ".join(parts)
