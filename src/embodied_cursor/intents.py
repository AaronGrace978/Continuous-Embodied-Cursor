# SPDX-License-Identifier: GPL-3.0-or-later
"""Intents: the vocabulary the slow brain uses to instruct the fast body.

An intent says *what* should happen to *which* element, in semantic terms. It
carries no coordinates and no timing. The body turns it into continuous
motion and decides for itself how to get there, when the target has been
reached, and whether the action took effect.

Every intent has a JSON form, which is what a language-model planner emits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from .world.selector import Selector


@dataclass(frozen=True, slots=True)
class Expect:
    """A condition on the world that should hold after an action."""

    appears: Selector | None = None
    vanishes: Selector | None = None
    state: str | None = None
    """With ``on``: that element must have this state (prefix ``not_`` to negate)."""

    value: float | None = None
    """With ``on``: that element's value must be within ``tolerance`` of this."""

    on: Selector | None = None
    tolerance: float = 0.0
    timeout: float = 2.0

    def __post_init__(self) -> None:
        if self.appears is None and self.vanishes is None and self.state is None and self.value is None:
            raise ValueError("an expectation needs appears, vanishes, state or value")
        if (self.state is not None or self.value is not None) and self.on is None:
            raise ValueError("state/value expectations need 'on'")

    @staticmethod
    def from_obj(obj: "dict[str, Any] | Expect | None") -> "Expect | None":
        if obj is None or isinstance(obj, Expect):
            return obj
        known = {"appears", "vanishes", "state", "value", "on", "tolerance", "timeout"}
        unknown = set(obj) - known
        if unknown:
            raise ValueError(f"unknown expect field(s): {', '.join(sorted(unknown))}")
        return Expect(
            appears=Selector.of(obj["appears"]) if obj.get("appears") else None,
            vanishes=Selector.of(obj["vanishes"]) if obj.get("vanishes") else None,
            state=obj.get("state"),
            value=float(obj["value"]) if obj.get("value") is not None else None,
            on=Selector.of(obj["on"]) if obj.get("on") else None,
            tolerance=float(obj.get("tolerance", 0.0)),
            timeout=float(obj.get("timeout", 2.0)),
        )

    def to_obj(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.appears:
            out["appears"] = self.appears.to_obj()
        if self.vanishes:
            out["vanishes"] = self.vanishes.to_obj()
        if self.state is not None:
            out["state"] = self.state
        if self.value is not None:
            out["value"] = self.value
            if self.tolerance:
                out["tolerance"] = self.tolerance
        if self.on:
            out["on"] = self.on.to_obj()
        if self.timeout != 2.0:
            out["timeout"] = self.timeout
        return out

    def describe(self) -> str:
        if self.appears:
            return f"{self.appears.describe()} appears"
        if self.vanishes:
            return f"{self.vanishes.describe()} vanishes"
        if self.state is not None:
            return f"{self.on.describe()} is {self.state}"  # type: ignore[union-attr]
        return f"{self.on.describe()} value is {self.value}"  # type: ignore[union-attr]


@dataclass(frozen=True, slots=True)
class Intent:
    action: ClassVar[str] = ""

    def to_obj(self) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> str:
        return self.action


def _avoid(obj: dict[str, Any]) -> tuple[Selector, ...]:
    raw = obj.get("avoid") or ()
    if isinstance(raw, (str, dict)):
        raw = (raw,)
    return tuple(Selector.of(a) for a in raw)


@dataclass(frozen=True, slots=True)
class Move(Intent):
    """Bring the pointer to rest on an element."""

    action: ClassVar[str] = "move"
    target: Selector
    avoid: tuple[Selector, ...] = ()

    def to_obj(self) -> dict[str, Any]:
        return _strip({"action": self.action, "target": self.target.to_obj(), "avoid": [a.to_obj() for a in self.avoid]})

    def describe(self) -> str:
        return f"move to {self.target.describe()}"


@dataclass(frozen=True, slots=True)
class Click(Intent):
    action: ClassVar[str] = "click"
    target: Selector
    button: str = "left"
    count: int = 1
    expect: Expect | None = None
    avoid: tuple[Selector, ...] = ()

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "target": self.target.to_obj(),
                "button": self.button if self.button != "left" else None,
                "count": self.count if self.count != 1 else None,
                "expect": self.expect.to_obj() if self.expect else None,
                "avoid": [a.to_obj() for a in self.avoid],
            }
        )

    def describe(self) -> str:
        kind = {1: "click", 2: "double-click", 3: "triple-click"}.get(self.count, f"{self.count}x click")
        if self.button != "left":
            kind = f"{self.button}-{kind}"
        return f"{kind} {self.target.describe()}"


@dataclass(frozen=True, slots=True)
class Hover(Intent):
    """Rest on an element, e.g. to open a hover menu or tooltip."""

    action: ClassVar[str] = "hover"
    target: Selector
    dwell: float = 0.4
    expect: Expect | None = None
    avoid: tuple[Selector, ...] = ()

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "target": self.target.to_obj(),
                "dwell": self.dwell if self.dwell != 0.4 else None,
                "expect": self.expect.to_obj() if self.expect else None,
                "avoid": [a.to_obj() for a in self.avoid],
            }
        )

    def describe(self) -> str:
        return f"hover {self.target.describe()}"


@dataclass(frozen=True, slots=True)
class Drag(Intent):
    """Press on one element and release on another, or after an offset."""

    action: ClassVar[str] = "drag"
    target: Selector
    to: Selector | None = None
    offset: tuple[float, float] | None = None
    duration: float | None = None
    """Seconds for the carrying movement. Default: the movement-time law."""

    expect: Expect | None = None

    def __post_init__(self) -> None:
        if (self.to is None) == (self.offset is None):
            raise ValueError("a drag needs exactly one of 'to' or 'offset'")

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "target": self.target.to_obj(),
                "to": self.to.to_obj() if self.to else None,
                "offset": list(self.offset) if self.offset else None,
                "duration": self.duration,
                "expect": self.expect.to_obj() if self.expect else None,
            }
        )

    def describe(self) -> str:
        dest = self.to.describe() if self.to else f"by {self.offset}"
        return f"drag {self.target.describe()} to {dest}"


@dataclass(frozen=True, slots=True)
class Scrub(Intent):
    """Drag a slider, scroll bar or timeline until it reads a given value.

    The loop is closed on the value the control reports, not on pixels, so it
    works for non-linear and reversed controls.
    """

    action: ClassVar[str] = "scrub"
    target: Selector
    value: float | None = None
    fraction: float | None = None
    """Alternative to ``value``: position within the range, 0 to 1."""

    duration: float | None = None
    """Sweep slowly, taking this many seconds, instead of going straight there."""

    tolerance: float | None = None
    """Acceptable error in value units. Default: 1% of the range."""

    def __post_init__(self) -> None:
        if (self.value is None) == (self.fraction is None):
            raise ValueError("a scrub needs exactly one of 'value' or 'fraction'")

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "target": self.target.to_obj(),
                "value": self.value,
                "fraction": self.fraction,
                "duration": self.duration,
                "tolerance": self.tolerance,
            }
        )

    def describe(self) -> str:
        goal = f"{self.value:g}" if self.value is not None else f"{self.fraction:.0%}"
        return f"scrub {self.target.describe()} to {goal}"


@dataclass(frozen=True, slots=True)
class Scroll(Intent):
    """Turn the wheel over an element, by an amount or until something shows."""

    action: ClassVar[str] = "scroll"
    target: Selector | None = None
    notches: int | None = None
    """Wheel notches; positive scrolls down."""

    until: Selector | None = None
    direction: str = "down"

    def __post_init__(self) -> None:
        if (self.notches is None) == (self.until is None):
            raise ValueError("a scroll needs exactly one of 'notches' or 'until'")
        if self.direction not in ("down", "up"):
            raise ValueError("scroll direction must be 'down' or 'up'")

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "target": self.target.to_obj() if self.target else None,
                "notches": self.notches,
                "until": self.until.to_obj() if self.until else None,
                "direction": self.direction if self.until and self.direction != "down" else None,
            }
        )

    def describe(self) -> str:
        where = f" over {self.target.describe()}" if self.target else ""
        if self.until:
            return f"scroll {self.direction}{where} until {self.until.describe()} shows"
        return f"scroll {self.notches} notches{where}"


@dataclass(frozen=True, slots=True)
class Type(Intent):
    """Type text, optionally clicking a field first to focus it."""

    action: ClassVar[str] = "type"
    text: str
    target: Selector | None = None
    enter: bool = False

    def to_obj(self) -> dict[str, Any]:
        return _strip(
            {
                "action": self.action,
                "text": self.text,
                "target": self.target.to_obj() if self.target else None,
                "enter": True if self.enter else None,
            }
        )

    def describe(self) -> str:
        where = f" into {self.target.describe()}" if self.target else ""
        return f"type {self.text!r}{where}"


@dataclass(frozen=True, slots=True)
class Key(Intent):
    """Press a key or chord such as ``Escape`` or ``ctrl+s``."""

    action: ClassVar[str] = "key"
    keys: str

    def __post_init__(self) -> None:
        if not self.keys.strip():
            raise ValueError("a key intent needs 'keys'")

    def to_obj(self) -> dict[str, Any]:
        return {"action": self.action, "keys": self.keys}

    def describe(self) -> str:
        return f"press {self.keys}"


@dataclass(frozen=True, slots=True)
class Wait(Intent):
    """Do nothing until a condition holds."""

    action: ClassVar[str] = "wait"
    expect: Expect

    def to_obj(self) -> dict[str, Any]:
        return {"action": self.action, "expect": self.expect.to_obj()}

    def describe(self) -> str:
        return f"wait until {self.expect.describe()}"


def _strip(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if v not in (None, [], ())}


def _need(obj: dict[str, Any], key: str) -> Any:
    if obj.get(key) in (None, ""):
        raise ValueError(f"'{obj.get('action')}' needs '{key}'")
    return obj[key]


_FIELDS: dict[str, set[str]] = {
    "move": {"target", "avoid"},
    "click": {"target", "button", "count", "expect", "avoid"},
    "hover": {"target", "dwell", "expect", "avoid"},
    "drag": {"target", "to", "offset", "duration", "expect"},
    "scrub": {"target", "value", "fraction", "duration", "tolerance"},
    "scroll": {"target", "notches", "until", "direction"},
    "type": {"text", "target", "enter"},
    "key": {"keys"},
    "wait": {"expect"},
}

ACTIONS = tuple(_FIELDS)


def intent_from_obj(obj: "dict[str, Any] | Intent") -> Intent:
    """Build an intent from its JSON form, rejecting anything malformed."""
    if isinstance(obj, Intent):
        return obj
    if not isinstance(obj, dict):
        raise ValueError("an intent must be a JSON object")
    action = obj.get("action")
    if action not in _FIELDS:
        raise ValueError(f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
    unknown = set(obj) - _FIELDS[action] - {"action"}
    if unknown:
        raise ValueError(f"'{action}' does not take {', '.join(sorted(unknown))}")

    def sel(key: str, required: bool = True) -> Selector | None:
        if obj.get(key) in (None, ""):
            if required:
                raise ValueError(f"'{action}' needs '{key}'")
            return None
        return Selector.of(obj[key])

    expect = Expect.from_obj(obj.get("expect"))
    if action == "move":
        return Move(sel("target"), _avoid(obj))  # type: ignore[arg-type]
    if action == "click":
        button = obj.get("button", "left")
        if button not in ("left", "middle", "right"):
            raise ValueError(f"unknown button {button!r}")
        count = int(obj.get("count", 1))
        if not 1 <= count <= 3:
            raise ValueError("click count must be 1, 2 or 3")
        return Click(sel("target"), button, count, expect, _avoid(obj))  # type: ignore[arg-type]
    if action == "hover":
        return Hover(sel("target"), float(obj.get("dwell", 0.4)), expect, _avoid(obj))  # type: ignore[arg-type]
    if action == "drag":
        offset = obj.get("offset")
        if offset is not None:
            if not isinstance(offset, (list, tuple)) or len(offset) != 2:
                raise ValueError("drag offset must be [dx, dy]")
            offset = (float(offset[0]), float(offset[1]))
        duration = obj.get("duration")
        return Drag(
            sel("target"),  # type: ignore[arg-type]
            sel("to", required=False),
            offset,
            float(duration) if duration is not None else None,
            expect,
        )
    if action == "scrub":
        return Scrub(
            sel("target"),  # type: ignore[arg-type]
            float(obj["value"]) if obj.get("value") is not None else None,
            float(obj["fraction"]) if obj.get("fraction") is not None else None,
            float(obj["duration"]) if obj.get("duration") is not None else None,
            float(obj["tolerance"]) if obj.get("tolerance") is not None else None,
        )
    if action == "scroll":
        return Scroll(
            sel("target", required=False),
            int(obj["notches"]) if obj.get("notches") is not None else None,
            sel("until", required=False),
            obj.get("direction", "down"),
        )
    if action == "type":
        if not isinstance(obj.get("text"), str):
            raise ValueError("'type' needs 'text'")
        return Type(obj["text"], sel("target", required=False), bool(obj.get("enter", False)))
    if action == "key":
        return Key(str(_need(obj, "keys")))
    if expect is None:
        raise ValueError("'wait' needs 'expect'")
    return Wait(expect)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the body reports back after attempting an intent."""

    intent: Intent
    status: str
    """``ok``, ``failed`` or ``interrupted``."""

    reason: str = ""
    """Machine-readable cause when not ok: ``not_found``, ``occluded``,
    ``disabled``, ``timeout``, ``expectation_failed``, ``value_mismatch``,
    ``not_adjustable``, ``human_override``, ``cancelled``."""

    detail: str = ""
    duration: float = 0.0
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_obj(self) -> dict[str, Any]:
        out: dict[str, Any] = {"intent": self.intent.to_obj(), "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        out["duration"] = round(self.duration, 3)
        if self.observed:
            out["observed"] = self.observed
        return out

    def describe(self) -> str:
        if self.ok:
            return f"ok: {self.intent.describe()} ({self.duration:.2f}s)"
        tail = f": {self.detail}" if self.detail else ""
        return f"{self.status}: {self.intent.describe()} [{self.reason}]{tail}"
