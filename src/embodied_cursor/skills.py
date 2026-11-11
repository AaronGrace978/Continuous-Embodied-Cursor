# SPDX-License-Identifier: GPL-3.0-or-later
"""Motor skills: the closed-loop routines that carry out intents.

Each skill is a generator that the body steps once per motor tick. On every
step it looks at the freshest world model, decides where the cursor should be
heading, and yields. The body then advances the motion controller and moves
the real pointer. Because the target is re-located on every tick, anything
that changes mid-flight (the element moves, gets covered, disappears, or the
slider reports an unexpected value) is answered on the next tick, with no
round-trip to the planner.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Generator, Sequence

from .geometry import ZERO, Rect, Vec2
from .intents import Click, Drag, Expect, Hover, Intent, Key, Move, Scroll, Scrub, Type, Wait
from .motor.controller import MotionGoal
from .motor.minjerk import s_curve
from .runtime import BUTTONS
from .world.element import SURFACE_ROLES, Role, UIElement
from .world.memory import RevealStep
from .world.model import WorldModel
from .world.selector import Selector

if TYPE_CHECKING:
    from .body import Context

Skill = Generator[None, None, Any]

_MENU_ROLES = frozenset({Role.MENU, Role.MENU_ITEM})
_OPENERS = frozenset({Role.MENU, Role.COMBO_BOX})


class SkillFailure(Exception):
    def __init__(self, reason: str, detail: str = "", observed: dict[str, Any] | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.observed = observed or {}


# -- targets -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Aim:
    pos: Vec2
    width: float
    rect: Rect
    """Region in which the pointer counts as being on target."""

    vel: Vec2 = ZERO
    element: UIElement | None = None
    slipped: bool = False
    """The target moved out from under the pointer while it was being clicked."""


@dataclass(frozen=True, slots=True)
class Pending:
    """Why a target cannot be aimed at right now."""

    reason: str
    detail: str = ""
    hint: Rect | None = None
    """Where memory says the target usually is."""

    observed: dict[str, Any] = field(default_factory=dict)


class ElementTarget:
    """A selector, resolved afresh each tick but sticky to one element.

    Once an element has been chosen the target keeps following that element
    by identity for as long as it stays on screen, so a re-layout that would
    make another element the better textual match does not steal the reach.
    """

    def __init__(
        self,
        selector: Selector,
        anchor: tuple[float, float] | Callable[[UIElement], tuple[float, float]] = (0.5, 0.5),
        see_through: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self.selector = selector
        self.anchor = anchor
        self._see_through = see_through
        self.id: str | None = None
        self.ambiguity = 0

    def element(self, ctx: "Context") -> UIElement | None:
        return ctx.world.get(self.id) if self.id else None

    def locate(self, ctx: "Context") -> Aim | Pending:
        world = ctx.world
        el = world.get(self.id) if self.id else None
        if el is None or not el.visible:
            found = world.find(self.selector, near=ctx.pos)
            if not found:
                self.id = None
                hint = None
                if ctx.memory is not None:
                    rec = ctx.memory.recall(self.selector, world)
                    if rec is not None and rec.confidence >= 0.25:
                        hint = rec.rect
                return Pending("not_found", f"nothing on screen matches {self.selector.describe()}", hint)
            el = found[0].element
            if el.id != self.id:
                self.id = el.id
                self.ambiguity = sum(1 for m in found if m.score == found[0].score) - 1
                ctx.attend(el.id)
                ctx.emit("locked", element=el.id, name=el.name, role=el.role, alternatives=self.ambiguity)

        if not el.enabled:
            return Pending("disabled", f"{el.role} {el.name!r} is disabled")

        see_through = self._see_through() if self._see_through else ()
        region = world.visible_region(el, see_through)
        if not region:
            covers = [o for o in world.occluders(el) if o.id not in see_through]
            if covers:
                top = max(covers, key=lambda o: o.layer)
                return Pending(
                    "occluded",
                    f"{el.role} {el.name!r} is covered by {top.role} {top.name!r}",
                    observed={"occluder": top.to_obj()},
                )
            if el.has("obscured"):
                return Pending("occluded", f"{el.role} {el.name!r} is covered by another element")
            return Pending("offscreen", f"{el.role} {el.name!r} is outside the visible area")

        anchor = self.anchor(el) if callable(self.anchor) else self.anchor
        want = el.bounds.point_at(*anchor)
        piece = next((r for r in region if r.contains(want)), None)
        if piece is None:
            piece = max(region, key=lambda r: r.area)
            want = piece.center
        return Aim(want, min(piece.w, piece.h), piece, world.velocity(el.id), el)


class PointTarget:
    """A bare screen position, possibly recomputed every tick."""

    def __init__(self, point: Vec2 | Callable[["Context"], Vec2], width: float = 4.0) -> None:
        self._point = point
        self.width = max(width, 2.0)
        self.selector = None

    def locate(self, ctx: "Context") -> Aim | Pending:
        p = self._point(ctx) if callable(self._point) else self._point
        half = self.width / 2.0
        return Aim(p, self.width, Rect(p.x - half, p.y - half, self.width, self.width))


Target = ElementTarget | PointTarget


# -- shared pieces -----------------------------------------------------------


def menu_hazards(world: WorldModel, el: UIElement) -> list[Rect]:
    """Menu entries that would steal the menu if the pointer crossed them.

    For a target nested in open menus, every sibling of each menu on the way
    down is a hazard: hovering it switches or collapses the branch the target
    lives in. Siblings of the target itself are harmless.
    """
    out: list[Rect] = []
    for branch in world.ancestors(el):
        if branch.role not in _MENU_ROLES:
            continue
        for sib in world.children(world.parent(branch)):
            if sib.id != branch.id and sib.role in _MENU_ROLES and sib.visible:
                out.append(sib.bounds)
    return out


def structural_reveal(world: WorldModel, selector: Selector) -> list[RevealStep]:
    """Menus to open, outermost first, to bring a hidden menu entry on screen.

    Works from the element tree alone: toolkits list a menu's entries even
    while it is closed, so the path ``File > Export as > PDF`` can be read off
    the ancestors of the hidden entry without ever having seen it open.
    """
    for match in world.find(selector, visible_only=False):
        el = match.element
        if el.visible:
            continue
        chain: list[UIElement] = []
        anchored = False
        for a in world.ancestors(el):
            if a.role in _OPENERS:
                chain.append(a)
            elif not a.visible:
                break
            if a.visible:
                anchored = a.role in _OPENERS
                break
        if anchored and chain:
            return [RevealStep(Selector(id=a.id), "click") for a in reversed(chain)]
    return []


def obstacles_for(ctx: "Context", el: UIElement | None, avoid: Sequence[Selector]) -> tuple[Rect, ...]:
    rects: list[Rect] = []
    for sel in avoid:
        rects.extend(m.element.bounds for m in ctx.world.find(sel))
    if el is not None:
        rects.extend(menu_hazards(ctx.world, el))
    return tuple(rects)


def pause(ctx: "Context", seconds: float) -> Skill:
    """Let time pass without changing where the cursor is heading."""
    until = ctx.now + seconds
    while ctx.now < until:
        yield


def _arrived(ctx: "Context", aim: Aim) -> bool:
    ctrl = ctx.controller
    if ctrl.remaining > ctrl.config.tau_min + 1e-9:
        return False
    if (ctrl.vel - aim.vel).norm() >= 40.0:
        return False
    # Either right on the aim point (which may itself hug an edge), or, for a
    # target being chased, comfortably inside it.
    if ctx.pos.dist(aim.pos) <= 1.0 and aim.rect.inflate(0.5).contains(ctx.pos):
        return True
    margin = min(2.0, aim.rect.w / 4.0, aim.rect.h / 4.0)
    return aim.rect.contains(ctx.pos, margin)


def reach(
    ctx: "Context",
    target: Target,
    avoid: Sequence[Selector] = (),
    duration: float | None = None,
    reveal: bool = True,
    allow: float = 0.0,
) -> Generator[None, None, Aim]:
    """Fly the cursor onto ``target`` and hold it there until settled.

    ``allow`` extends the time limit for targets that are meant to take a
    while to get to, such as the end of a slow sweep.
    """
    cfg = ctx.config
    started = ctx.now
    waiting_since: float | None = None
    waiting_for = ""
    on_since: float | None = None
    tried_reveal = False

    while True:
        aim = target.locate(ctx)

        if isinstance(aim, Pending):
            if waiting_since is None or aim.reason != waiting_for:
                waiting_since, waiting_for = ctx.now, aim.reason
                ctx.emit("waiting", reason=aim.reason, detail=aim.detail)
            on_since = None
            waited = ctx.now - waiting_since

            if aim.reason == "not_found":
                if (
                    reveal
                    and cfg.auto_reveal
                    and not tried_reveal
                    and target.selector is not None
                    and waited >= cfg.reveal_after
                ):
                    tried_reveal = True
                    steps = structural_reveal(ctx.world, target.selector)
                    if not steps and ctx.memory is not None:
                        steps = ctx.memory.reveal_chain(target.selector, ctx.world)
                    if steps:
                        ctx.emit("revealing", via=[s.selector.describe() for s in steps])
                        for step in steps:
                            trigger = ElementTarget(step.selector)
                            if step.how == "hover":
                                yield from _hover(ctx, trigger, cfg.hover_reveal_dwell, None, (), reveal=False)
                            else:
                                yield from _click(ctx, trigger, 1, 1, (), reveal=False)
                        waiting_since = ctx.now
                        continue
                if aim.hint is not None:
                    # Start moving towards where it usually is; the path is
                    # corrected as soon as the element really shows up.
                    ctx.goal = MotionGoal(aim.hint.center, max(aim.hint.w, aim.hint.h, 24.0))
                else:
                    ctx.hold()
                patience = cfg.resolve_timeout
            else:
                ctx.hold()
                patience = cfg.occlusion_patience

            if waited > patience:
                raise SkillFailure(aim.reason, aim.detail, aim.observed)
            yield
            continue

        if waiting_since is not None:
            ctx.emit("acquired", after=round(ctx.now - waiting_since, 3))
            waiting_since = None

        # A requested duration is a budget for the whole reach, so a target
        # that keeps shifting does not restart the clock.
        left = None if duration is None else started + duration - ctx.now
        ctx.goal = MotionGoal(
            aim.pos,
            aim.width,
            aim.vel,
            obstacles_for(ctx, aim.element, avoid),
            left if left is not None and left > 0.0 else None,
        )

        if _arrived(ctx, aim):
            if on_since is None:
                on_since = ctx.now
            if ctx.now - on_since >= cfg.settle_time:
                return aim
        else:
            on_since = None

        if ctx.now - started > cfg.reach_timeout + (duration or 0.0) + allow:
            raise SkillFailure("timeout", f"could not settle on the target within {cfg.reach_timeout:g}s")
        yield


def holds(expect: Expect, world: WorldModel) -> bool:
    if expect.appears is not None:
        return bool(world.find(expect.appears))
    if expect.vanishes is not None:
        return not world.find(expect.vanishes)
    el = world.find_one(expect.on) if expect.on else None
    if el is None:
        return False
    if expect.state is not None:
        negate = expect.state.startswith("not_")
        name = expect.state[4:] if negate else expect.state
        return el.has(name) != negate
    if el.value is None or expect.value is None:
        return False
    span = (el.value_max - el.value_min) if el.value_min is not None and el.value_max is not None else 1.0
    return abs(el.value - expect.value) <= max(expect.tolerance, abs(span) * 1e-9)


def await_expect(ctx: "Context", expect: Expect | None, track: Target | None = None) -> Skill:
    """Wait for ``expect`` to hold, staying on ``track`` meanwhile."""
    if expect is None:
        return
    since = ctx.now
    deadline = since + expect.timeout
    held_since: float | None = None
    while True:
        # Only observations made after the action count, and the condition
        # has to stay true for a moment: a control that was already in the
        # expected state, or that flickers through it, is not a confirmation.
        if expect.on is not None:
            el = ctx.world.find_one(expect.on)
            if el is not None:
                ctx.attend(el.id)
            seen = ctx.world.last_seen(el.id) if el is not None else None
            fresh = seen is not None and seen > since
        else:
            ctx.perception.request_snapshot()
            fresh = ctx.perception.fresh_since(since)
        if fresh and holds(expect, ctx.world):
            if held_since is None:
                held_since = ctx.now
            if ctx.now - held_since >= ctx.config.confirm_time:
                ctx.emit("confirmed", expect=expect.describe())
                return
        else:
            held_since = None
        if ctx.now >= deadline:
            raise SkillFailure(
                "expectation_failed",
                f"{expect.describe()} did not happen within {expect.timeout:g}s",
            )
        if track is not None:
            _follow(ctx, track)
        yield


def _follow(ctx: "Context", target: Target) -> None:
    aim = target.locate(ctx)
    if isinstance(aim, Aim):
        ctx.goal = MotionGoal(aim.pos, aim.width, aim.vel)


# -- skills ------------------------------------------------------------------


def _click(
    ctx: "Context",
    target: ElementTarget,
    button: int,
    count: int,
    avoid: Sequence[Selector],
    reveal: bool = True,
) -> Generator[None, None, Aim]:
    cfg = ctx.config
    aim = yield from reach(ctx, target, avoid, reveal=reveal)
    pressed_at = ctx.now
    for i in range(count):
        if i:
            yield from pause(ctx, cfg.click_gap)
        ctx.press(button)
        if i == 0 and aim.element is not None:
            ctx.watch_reveal(aim.element, "click")
        yield from pause(ctx, cfg.press_time)
        ctx.release(button)
    ctx.emit("clicked", element=target.id, at=list(ctx.pos.rounded()), count=count)
    if aim.element is not None:
        if (yield from _slipped(ctx, aim.element, pressed_at)):
            aim = replace(aim, slipped=True)
    return aim


def _slipped(ctx: "Context", el: UIElement, since: float) -> Generator[None, None, bool]:
    """Did the target move out from under the pointer while it was being clicked?

    Perception always trails the screen a little, so a target that teleports
    in the instant before the press can still be missed. That cannot be
    prevented, but it can be noticed: wait for one observation newer than the
    press and see whether the element is still under the pointer.
    """
    deadline = ctx.now + 0.12
    ctx.attend(el.id)
    while ctx.world.last_seen(el.id) <= since and ctx.now < deadline:
        ctx.hold()
        yield
    live = ctx.world.get(el.id)
    if live is None or not live.visible or live.bounds == el.bounds:
        return False
    if live.bounds.contains(ctx.pos):
        return False
    ctx.emit("slipped", element=el.id, now_at=list(live.bounds.as_tuple()))
    return True


def _hover(
    ctx: "Context",
    target: ElementTarget,
    dwell: float,
    expect: Expect | None,
    avoid: Sequence[Selector],
    reveal: bool = True,
) -> Generator[None, None, Aim]:
    aim = yield from reach(ctx, target, avoid, reveal=reveal)
    if aim.element is not None:
        ctx.watch_reveal(aim.element, "hover")
    if expect is not None:
        yield from await_expect(ctx, expect, track=target)
    else:
        until = ctx.now + dwell
        while ctx.now < until:
            _follow(ctx, target)
            yield
    return aim


def _observed(aim: Aim, ctx: "Context", target: ElementTarget | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"at": list(ctx.pos.rounded())}
    if aim.element is not None:
        out["element"] = aim.element.to_obj()
    if target is not None and target.ambiguity:
        out["equally_good_matches"] = target.ambiguity
    if aim.slipped:
        out["target_moved_away"] = True
    return out


def move(ctx: "Context", intent: Move) -> Skill:
    target = ElementTarget(intent.target)
    aim = yield from reach(ctx, target, intent.avoid)
    return _observed(aim, ctx, target)


def click(ctx: "Context", intent: Click) -> Skill:
    target = ElementTarget(intent.target)
    aim = yield from _click(ctx, target, BUTTONS[intent.button], intent.count, intent.avoid)
    yield from await_expect(ctx, intent.expect)
    return _observed(aim, ctx, target)


def hover(ctx: "Context", intent: Hover) -> Skill:
    target = ElementTarget(intent.target)
    aim = yield from _hover(ctx, target, intent.dwell, intent.expect, intent.avoid)
    return _observed(aim, ctx, target)


def drag(ctx: "Context", intent: Drag) -> Skill:
    cfg = ctx.config
    source = ElementTarget(intent.target)
    aim = yield from reach(ctx, source)
    grab = ctx.pos
    carried = aim.element.id if aim.element else None

    ctx.press(1)
    pressed_at = ctx.world.revision

    def carried_surfaces() -> list[str]:
        # Whatever is being carried rides above the drop zone, often as a
        # ghost window that only exists during the drag. Neither it nor the
        # dragged element itself counts as covering the destination.
        ids = [carried] if carried else []
        for change in ctx.world.changes_since(pressed_at):
            if change.kind == "appeared" and change.element.role in SURFACE_ROLES:
                ids.append(change.element.id)
        return ids

    dest: Target
    if intent.to is not None:
        dest = ElementTarget(intent.to, see_through=carried_surfaces)
    else:
        dest = PointTarget(grab + Vec2(*intent.offset))  # type: ignore[misc]

    try:
        yield from pause(ctx, cfg.press_time)
        landed = yield from reach(ctx, dest, duration=intent.duration, reveal=False)
        yield from pause(ctx, cfg.press_time)
    finally:
        ctx.release(1)
    ctx.emit("dropped", at=list(ctx.pos.rounded()))
    yield from await_expect(ctx, intent.expect)
    out = _observed(aim, ctx, source)
    out["from"] = list(grab.rounded())
    if landed.element is not None:
        out["onto"] = landed.element.to_obj()
    return out


def _acquire(ctx: "Context", target: ElementTarget) -> Generator[None, None, UIElement]:
    """Wait, without moving, until ``target`` is on screen and usable."""
    cfg = ctx.config
    since = ctx.now
    while True:
        aim = target.locate(ctx)
        if isinstance(aim, Aim):
            assert aim.element is not None
            return aim.element
        patience = cfg.resolve_timeout if aim.reason == "not_found" else cfg.occlusion_patience
        if ctx.now - since > patience:
            raise SkillFailure(aim.reason, aim.detail, aim.observed)
        yield


def _settled_value(
    ctx: "Context", ident: str, quiet: float, lag_seen: list[float]
) -> Generator[None, None, UIElement | None]:
    """Hold still until the control's reading has stopped changing.

    An application takes a moment to react to the pointer, and the reading
    takes another to come back, so the first few observations after stopping
    can still describe the motion. The reading counts once it has been
    observed afresh and has not changed for ``quiet`` seconds, or for longer
    if this control has been seen to trail further behind than that:
    ``lag_seen[0]`` keeps the longest such trail.
    """
    began = ctx.now
    changed = ctx.now
    first = ctx.world.get(ident)
    last_value = first.value if first is not None else None
    fresh = False
    rested_at, rested_since = ctx.pos, ctx.now
    while ctx.now - began < 1.5:
        ctx.hold()
        yield
        el = ctx.world.get(ident)
        stamp = ctx.world.last_seen(ident)
        if el is None or stamp is None:
            return None
        if ctx.pos.dist(rested_at) > 0.25:
            rested_at, rested_since = ctx.pos, ctx.now
        if el.value != last_value:
            last_value, changed = el.value, ctx.now
            lag_seen[0] = max(lag_seen[0], min(ctx.now - rested_since, 0.5))
        fresh = fresh or stamp > began
        # The quiet interval starts only once the pointer is actually still.
        # A value that has not changed while the pointer was still moving says
        # nothing: the application's report of that last movement may not have
        # arrived yet.
        if fresh and ctx.now - max(changed, rested_since) >= max(quiet, 1.5 * lag_seen[0] + 0.03):
            return el
    return ctx.world.get(ident)


def _fresh_value(ctx: "Context", ident: str, after: float) -> Generator[None, None, UIElement | None]:
    """Wait for the control's reading to catch up with the pointer.

    Returns once two consecutive observations made after ``after`` agree, or
    after a short timeout, so a value that is still animating is not trusted.
    """
    deadline = ctx.now + 0.35
    last_stamp: float | None = None
    last_value: float | None = None
    agree = 0
    while ctx.now < deadline:
        yield
        el = ctx.world.get(ident)
        stamp = ctx.world.last_seen(ident)
        if el is None or stamp is None:
            return None
        if stamp <= after or stamp == last_stamp:
            continue
        agree = agree + 1 if el.value == last_value else 1
        last_stamp, last_value = stamp, el.value
        if agree >= 2:
            return el
    return ctx.world.get(ident)


# A sweep may aim only this far, as a fraction of the value still to go, past
# the latest reading. The rest is closed as readings catch up, so a response
# that steepens while a reading is in flight cannot run away.
_SWEEP_CAUTION = 0.5

# The aim point may move this many times faster than the sweep was timed for.
# Past that, a late reading makes the control look as though it needs ever
# more travel, and the aim chases its own delay.
_SWEEP_PACE = 2.0

# A delay guessed from the first response is believed only if the value keeps
# changing afterwards. A stepped control answers once, when a step is crossed,
# and then sits still however prompt it is.
_LAG_CHANGES = 2
_LAG_LOOKS = 8


class Response:
    """What a draggable control has been seen to do: pointer offset -> value.

    Every observation of the control during a drag adds a reading. From
    those, :meth:`solve` answers "where must the pointer be for the control to
    read *v*?" by interpolating between readings that bracket *v*, or by
    extending the local slope a short way when nothing brackets it yet. No
    shape is assumed beyond the response being monotonic, so linear, curved,
    reversed and stepped controls are all handled by the same code.
    """

    def __init__(self, gain: float, span: float) -> None:
        self.gain = gain
        """Pixels of pointer travel per unit of value; the sign is the direction."""

        self._first = gain

        self.sign_known = False
        """Set once the direction has been felt directly; readings no longer overturn it."""

        self._eps = span * 1e-9
        self._quantum = span * 1e-6
        self._table: dict[int, tuple[float, float]] = {}
        # Where each distinct value was first met. On a stepped control these
        # are the step edges, which give its true pitch; single readings do not.
        self._levels: dict[int, tuple[float, float]] = {}
        self._settled: list[tuple[float, float]] = []
        self._rest_gain: float | None = None

    def __len__(self) -> int:
        return len(self._table)

    def clear(self) -> None:
        self._table.clear()
        self._levels.clear()
        self._settled.clear()
        self._rest_gain = None

    def note(self, offset: float, value: float) -> None:
        """Add a reading taken while the pointer may have been moving.

        Such readings trail the pointer by however long the application
        takes to react, so they are good for steering a sweep and for the
        slope, but not for deciding where exactly to stop.
        """
        self._table[round(offset * 2.0)] = (offset, value)
        self._levels.setdefault(round(value / self._quantum), (offset, value))

    def settle(self, offset: float, value: float) -> None:
        """Add a reading taken with the pointer at rest: the ground truth."""
        self.note(offset, value)
        # A newer reading wins over older ones it cannot be reconciled with.
        kept = []
        for s, v in self._settled:
            if abs(s - offset) < 0.75:
                continue
            if abs(v - value) > self._eps and ((s - offset) * self.gain > 0) != (v > value):
                continue
            kept.append((s, v))
        if kept:
            # The freshest slope there is: two readings, both at rest.
            s, v = min(kept, key=lambda p: abs(p[0] - offset))
            if abs(s - offset) >= 2.0 and abs(v - value) > self._eps and (s - offset) / (v - value) * self.gain > 0:
                slope = (s - offset) / (v - value)
                if abs(s - offset) > 40.0:
                    # A chord across a long stretch overstates the slope here
                    # if the response curves the wrong way. Lean towards the
                    # smaller estimate, but not below half the chord.
                    size = max(min(abs(self.gain), abs(slope)), 0.5 * abs(slope))
                    slope = size if slope > 0 else -size
                self.gain = self._rest_gain = slope
        kept.append((offset, value))
        self._settled = sorted(kept)

    def home(self, value: float, near_offset: float) -> float:
        """Like :meth:`solve`, but from at-rest readings only."""
        eps = self._eps
        pts = self._settled
        if not pts:
            return self.solve(value, near_offset)
        best: float | None = None
        for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
            if min(v0, v1) - eps <= value <= max(v0, v1) + eps:
                if abs(v0 - value) <= eps and abs(v1 - value) <= eps:
                    cand = (s0 + s1) / 2.0
                elif abs(v1 - v0) <= eps:
                    continue
                else:
                    cand = s0 + (value - v0) / (v1 - v0) * (s1 - s0)
                    # Somewhere not tried yet, if there is anywhere between.
                    if abs(s1 - s0) >= 2.0:
                        cand = min(max(cand, s0 + 1.0), s1 - 1.0)
                if best is None or abs(cand - near_offset) < abs(best - near_offset):
                    best = cand
        if best is not None:
            return best
        # No bracket yet: step on from the nearest reading, and from the one
        # furthest along if several read the same.
        closest = min(abs(p[1] - value) for p in pts)
        ties = [p for p in pts if abs(p[1] - value) <= closest + eps]
        forward = self.gain * (value - ties[0][1]) >= 0
        anchor = max(ties) if forward else min(ties)
        gain = self._rest_gain if self._rest_gain is not None and self._rest_gain * self.gain > 0 else self.gain
        return anchor[0] + gain * (value - anchor[1])

    def pinch(self, value: float) -> tuple[float, float] | None:
        """The better of two at-rest readings a pixel apart that straddle ``value``.

        When there is such a pair, the pointer has no position in between
        to try: the closer of the two is the best this control can do.
        """
        for (s0, v0), (s1, v1) in zip(self._settled, self._settled[1:]):
            if s1 - s0 <= 1.5 and min(v0, v1) < value < max(v0, v1):
                return (s0, v0) if abs(v0 - value) <= abs(v1 - value) else (s1, v1)
        return None

    def grid(self) -> tuple[float, float] | None:
        """``(origin, step)`` if the control only ever reports values on a grid."""
        values = sorted(v for _, v in self._levels.values())
        if len(values) < 4:
            return None
        step = min(b - a for a, b in zip(values, values[1:]))
        if step <= self._quantum * 10:
            return None
        for v in values:
            k = (v - values[0]) / step
            if abs(k - round(k)) > 1e-6:
                return None
        # Steps a pixel or so wide are the pointer's own resolution showing
        # through, not a property of the control.
        pts = sorted(self._table.values())
        rise = abs(pts[-1][1] - pts[0][1])
        if rise <= 0.0 or step * (pts[-1][0] - pts[0][0]) / rise < 2.0:
            return None
        return values[0], step

    def solve(self, value: float, near_offset: float) -> float:
        """Pointer offset at which the control should read ``value``."""
        eps = self._eps
        pts = sorted(self._table.values())
        if not pts:
            return near_offset
        same = [p[0] for p in pts if abs(p[1] - value) <= eps]
        if same:
            # Seen reading exactly this: aim for the middle of where, which on
            # a stepped control is away from the edges of the step.
            return (min(same) + max(same)) / 2.0
        best: float | None = None
        for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
            if min(v0, v1) - eps <= value <= max(v0, v1) + eps:
                cand = (s0 + s1) / 2.0 if abs(v1 - v0) <= eps else s0 + (value - v0) / (v1 - v0) * (s1 - s0)
                if best is None or abs(cand - near_offset) < abs(best - near_offset):
                    best = cand
        if best is not None:
            return best

        # Not bracketed yet: extend the local slope from the nearest reading,
        # taking the one furthest along if several read the same (a stepped
        # control reports one value across a whole stretch of track).
        self._refit(pts)
        closest = min(abs(p[1] - value) for p in pts)
        ties = [p for p in pts if abs(p[1] - value) <= closest + eps]
        forward = self.gain * (value - ties[0][1]) >= 0
        anchor = max(ties) if forward else min(ties)
        # While the response is still being discovered, do not aim far past
        # what has been explored: commit further as the evidence grows.
        lead = max(48.0, 0.75 * (pts[-1][0] - pts[0][0]))
        step = self.gain * (value - anchor[1])
        return anchor[0] + min(max(step, -lead), lead)

    def _refit(self, pts: list[tuple[float, float]]) -> None:
        """Update the slope estimate from the readings at the explored frontier."""
        eps = self._eps
        frontier = pts[-1] if self.gain >= 0 else pts[0]
        if len(pts) >= 2 and abs(pts[-1][0] - pts[0][0]) >= 4.0:
            # Which end is "ahead" depends on which way the drag has gone.
            newest = max(self._levels.values(), key=lambda p: abs(p[0]))
            frontier = min(pts, key=lambda p: abs(p[0] - newest[0]))
        # Two estimates of pixels per unit of value around the frontier: a fit
        # over the recent stretch, and the spacing of the last two value
        # levels. The smaller one is used, because falling short costs a
        # correction while overshooting sets a value nobody asked for.
        slopes: list[float] = []
        reach_back = max(24.0, 0.3 * (pts[-1][0] - pts[0][0]))
        window = [p for p in pts if abs(p[0] - frontier[0]) <= reach_back]
        if len(window) >= 3:
            ms = sum(p[0] for p in window) / len(window)
            mv = sum(p[1] for p in window) / len(window)
            var = sum((p[1] - mv) ** 2 for p in window)
            if var > eps * eps:
                slopes.append(sum((p[0] - ms) * (p[1] - mv) for p in window) / var)
        entry = self._levels.get(round(frontier[1] / self._quantum), frontier)
        earlier = [p for p in self._levels.values() if abs(p[0] - entry[0]) >= 6.0 and abs(p[1] - entry[1]) > eps]
        if earlier:
            ref = min(earlier, key=lambda p: abs(p[0] - entry[0]))
            slopes.append((ref[0] - entry[0]) / (ref[1] - entry[1]))
        if not slopes:
            return
        agreeing = [k for k in slopes if k * self.gain > 0]
        if agreeing:
            # Bounded against the first guess, not the last estimate, so a
            # run of skewed readings cannot walk the slope away.
            first = self._first if self._first * self.gain > 0 else -self._first
            self.gain = first * min(max(min(agreeing, key=abs) / first, 0.125), 8.0)
        elif abs(pts[-1][0] - pts[0][0]) >= 4.0 and not self.sign_known:
            # Every measurement says the control runs the other way round.
            self.gain = min(slopes, key=abs)


class _Sweep:
    """Aim point for a drag whose reported value follows a smooth reference.

    The value, not the pointer, is what moves along a minimum-jerk profile
    from where it is to where it should be. Each tick the pointer is sent to
    wherever the control is expected to read the reference value, so the
    value changes smoothly even when the control's response is curved.
    """

    selector = None

    def __init__(
        self,
        ctx: "Context",
        response: Response,
        observe: Callable[["Context"], tuple[UIElement, float]],
        point: Callable[[UIElement, float], Vec2],
        limits: Callable[[UIElement], tuple[float, float]],
        v_from: float,
        v_to: float,
        duration: float,
        tolerance: float,
        pace: float,
    ) -> None:
        self.response, self.observe, self.point, self.limits = response, observe, point, limits
        self.v_from, self.v_to, self.duration, self.tolerance = v_from, v_to, max(duration, 1e-3), tolerance
        self.started = ctx.now
        self.pace = max(pace, 30.0)
        self._goal: tuple[float, float] | None = None
        self._last: tuple[float, Vec2] | None = None
        self._vel = ZERO

    def locate(self, ctx: "Context") -> Aim:
        now, here = self.observe(ctx)
        u = min((ctx.now - self.started) / self.duration, 1.0)
        reference = self.v_from + (self.v_to - self.v_from) * s_curve(u)
        if now.value is not None:
            # Never ask for more than a part of what is known to remain. The
            # latest reading is the last thing known for certain; beyond it
            # the response may steepen, and a reading that arrives late
            # cannot warn of that in time.
            ahead = now.value + _SWEEP_CAUTION * (self.v_to - now.value)
            reference = min(reference, ahead) if self.v_to >= self.v_from else max(reference, ahead)
        low, high = self.limits(now)
        goal = self.response.solve(reference, here)
        if u >= 1.0 and abs(goal - here) < 1.0 and abs(self.v_to - (now.value or 0.0)) > self.tolerance:
            goal = here + (1.0 if goal >= here else -1.0)
        goal = min(max(goal, low), high)
        if self._goal is not None:
            # The aim point itself moves no faster than a brisk version of
            # the planned sweep. Readings that trail the pointer make the
            # control seem to need more travel the faster the pointer goes;
            # without a limit that feeds on itself.
            reach_ = _SWEEP_PACE * self.pace * (ctx.now - self._goal[0])
            goal = min(max(goal, self._goal[1] - reach_), self._goal[1] + reach_)
        self._goal = (ctx.now, goal)
        p = self.point(now, goal)

        # Feed the aim point's own velocity forward so the cursor tracks it
        # rather than trailing behind.
        if self._last is not None and ctx.now > self._last[0] and u < 1.0:
            raw = (p - self._last[1]) / (ctx.now - self._last[0])
            self._vel = self._vel * 0.7 + raw * 0.3
        elif u >= 1.0:
            self._vel = ZERO
        self._last = (ctx.now, p)

        # Only the end of the sweep counts as arriving.
        end = self.point(now, min(max(self.response.solve(self.v_to, here), low), high)) if u < 1.0 else p
        # Loose while following the reference, tight for the landing.
        width = 48.0 if u < 1.0 else 4.0
        return Aim(p, width, Rect(end.x - 2.0, end.y - 2.0, 4.0, 4.0), self._vel if u < 1.0 else ZERO)


def scrub(ctx: "Context", intent: Scrub) -> Skill:
    cfg = ctx.config
    target = ElementTarget(intent.target)

    # Find the control first; it has to report a value to servo on.
    el = yield from _acquire(ctx, target)
    if el.value is None or el.value_min is None or el.value_max is None or el.value_max <= el.value_min:
        raise SkillFailure("not_adjustable", f"{el.role} {el.name!r} does not report a value range")
    ident = el.id
    lo, hi = el.value_min, el.value_max
    span = hi - lo
    want = intent.value if intent.value is not None else lo + span * intent.fraction  # type: ignore[operator]
    want = min(max(want, lo), hi)
    tolerance = intent.tolerance if intent.tolerance is not None else 0.01 * span
    start_value = el.value
    eps = span * 1e-9

    horizontal = el.bounds.w >= el.bounds.h
    axis = Vec2(1.0, 0.0) if horizontal else Vec2(0.0, 1.0)
    length = el.bounds.w if horizontal else el.bounds.h
    pad = min(el.bounds.w, el.bounds.h) / 2.0
    travel = max(length - 2.0 * pad, 1.0)

    # First guess: a linear control whose low end is at the left or top. It
    # is corrected from the control's actual response as soon as there is one.
    response = Response(travel / span, span)

    def allowed() -> float:
        """How far from the requested value is acceptable."""
        grid = response.grid()
        if intent.tolerance is None and grid is not None:
            # A control that moves in steps can and must land on the step
            # nearest to what was asked for, not merely somewhere close.
            origin, step = grid
            nearest = origin + round((want - origin) / step) * step
            return abs(nearest - want) + 0.01 * step
        return tolerance

    def close_enough(value: float) -> bool:
        return abs(want - value) <= min(allowed(), 0.5 / abs(response.gain))

    if close_enough(start_value):
        return {"value": start_value, "wanted": want, "started_at": start_value}

    live = el
    grab = ctx.pos
    grab_bounds = el.bounds
    recent: deque[tuple[float, float]] = deque(maxlen=64)
    seen_stamp = 0.0
    lag_seen = [0.0]
    lag = [cfg.scrub_lag or 0.0]
    trial: tuple[float, float, int, int] | None = None
    held_value = start_value

    def read() -> Generator[None, None, UIElement]:
        """A reading with the pointer at rest and the control caught up."""
        fresh = yield from _settled_value(ctx, ident, cfg.scrub_quiet, lag_seen)
        if fresh is None or fresh.value is None:
            raise SkillFailure("not_found", "the control disappeared while being dragged")
        return fresh

    def shift_of(bounds: Rect) -> Vec2:
        """How far the control itself has moved since it was grabbed."""
        return Vec2(bounds.x - grab_bounds.x, bounds.y - grab_bounds.y)

    def offset_of(bounds: Rect) -> float:
        return (ctx.pos - grab - shift_of(bounds)).dot(axis)

    def point_at(now: UIElement, offset: float) -> Vec2:
        return grab + shift_of(now.bounds) + axis * offset

    def limits(now: UIElement) -> tuple[float, float]:
        """Offsets beyond which the pointer would leave the control."""
        origin = (grab + shift_of(now.bounds)).dot(axis)
        low = (now.bounds.left if horizontal else now.bounds.top) - origin - pad
        high = (now.bounds.right if horizontal else now.bounds.bottom) - origin + pad
        return low, high

    def offset_at(then: float) -> float:
        return next((o for t, o in reversed(recent) if t <= then + 1e-9), recent[0][1] if recent else 0.0)

    def observe(c: "Context") -> tuple[UIElement, float]:
        """Record a fresh reading against where the pointer was when it was taken."""
        nonlocal seen_stamp, trial
        now = c.world.get(ident) or live
        here = offset_of(now.bounds)
        recent.append((c.now, here))
        stamp = c.world.last_seen(ident) or 0.0
        if stamp > seen_stamp and now.value is not None:
            seen_stamp = stamp
            if trial is not None:
                # A delay measured from the first response only means what it
                # seems to if the control answers every little movement. One
                # that moves in steps answers when a step is crossed, however
                # promptly, and so shows no further change for a while.
                claimed, last_value, looks, changes = trial
                changes += now.value != last_value
                if changes >= _LAG_CHANGES:
                    lag[0], trial = claimed, None
                    response.clear()
                    response.settle(0.0, held_value)
                elif looks >= _LAG_LOOKS:
                    trial = None
                else:
                    trial = (claimed, now.value, looks + 1, changes)
            # The reading reflects where the pointer was a moment before it
            # was taken: the application needs time to react. Erring on the
            # long side makes the sweep fall short rather than run past.
            then = stamp - lag[0]
            response.note(offset_at(then), now.value)
        return now, here

    # -- take hold ---------------------------------------------------------
    # The accessibility tree gives the control's box, not where its handle
    # is. Along the track the handle is where the value says; across it, the
    # box may include a label or readout. So feel for it: press, lean on it,
    # and if the value does not respond, let go and try a little off-centre.
    engaged = False
    remembered = ctx.memory.grip(el) if ctx.memory is not None else None
    crossings = list(cfg.scrub_cross)
    if remembered is not None:
        crossings = [remembered] + [c for c in crossings if c != remembered]
    for cross in crossings:

        def handle(now: UIElement, cross: float = cross) -> tuple[float, float]:
            along = (pad + (now.value_fraction or 0.0) * travel) / length
            return (along, cross) if horizontal else (cross, along)

        target.anchor = handle
        yield from reach(ctx, target)
        before = (ctx.world.get(ident) or el).value
        ctx.press(1)
        yield from pause(ctx, cfg.press_time)
        live = yield from read()
        grab = ctx.pos
        grab_bounds = live.bounds

        track_lo = (grab_bounds.left if horizontal else grab_bounds.top) + pad
        along = ((grab.x if horizontal else grab.y) - track_lo) / travel
        after = live.value_fraction or 0.0
        jumped = abs(live.value - before) > eps  # type: ignore[operator]
        if jumped and abs(after - (1.0 - along)) < abs(after - along) - 0.1:
            # The press landed on the track, not the handle, and the value
            # jumped to the mirror image of where we pressed: the control's
            # high end is at the left or top.
            response.gain = -abs(response.gain)

        # Lean on it in the direction of the goal and watch for the first
        # response. Long enough to click a stepped control over, but never
        # further than the goal is thought to be.
        error = want - live.value  # type: ignore[operator]
        toward = 1.0 if response.gain * error >= 0 else -1.0
        lean = min(cfg.scrub_probe * 9.0, max(abs(response.gain * error), cfg.scrub_probe))
        ref_v = held_value = live.value  # type: ignore[assignment]
        response.clear()
        recent.clear()
        trial = None
        seen_stamp = ctx.world.last_seen(ident) or 0.0
        response.settle(0.0, ref_v)  # type: ignore[arg-type]
        responded = jumped
        budged: float | None = None
        for _ in reach(ctx, PointTarget(grab + axis * (toward * lean), width=4.0), duration=0.12 + lean / 160.0):
            now, here = observe(ctx)
            if budged is None and abs(here) >= 0.5:
                budged = ctx.now
            if now.value is not None and abs(now.value - ref_v) > eps:
                # It moved: keep going, straight into the sweep below.
                live, responded = now, True
                if cfg.scrub_lag is None and budged is not None:
                    # How long the control took to notice is how far its
                    # readings trail the pointer, if it is the kind that
                    # answers every movement; `observe` finds that out.
                    trial = (min(max(seen_stamp - budged, 0.0), cfg.scrub_latency_max), now.value, 0, 0)
                break
            yield
        else:
            live = yield from read()
            response.settle(offset_of(live.bounds), live.value)  # type: ignore[arg-type]
            responded = responded or abs(live.value - ref_v) > eps  # type: ignore[operator]
        if abs(live.value - ref_v) > eps:  # type: ignore[operator]
            # Which way it went when leaned on is which way it runs.
            sign = toward if live.value > ref_v else -toward  # type: ignore[operator]
            response.gain = sign * abs(response.gain)
            response.sign_known = True
        if responded:
            engaged = True
            if ctx.memory is not None:
                ctx.memory.note_grip(el, cross, ctx.world)
            ctx.emit("gripped", across=cross)
            break
        ctx.release(1)
        yield from pause(ctx, cfg.click_gap)

    if not engaged:
        raise SkillFailure(
            "not_adjustable",
            f"dragging {el.role} {el.name!r} had no effect on its value",
            {"value": live.value, "wanted": want, "started_at": start_value},
        )

    # -- servo on the reported value -----------------------------------------
    # Two phases, as in a hand's own aimed movements. A sweep covers most of
    # the distance with the value following a smooth reference, and stops
    # deliberately short: readings taken on the move lag the pointer by an
    # unknown amount, so the sweep cannot know exactly where to stop. Homing
    # then closes the gap from readings taken at rest, which are exact.
    try:
        s_now = offset_of(live.bounds)
        distance = abs(response.solve(want, s_now) - s_now)
        error = want - live.value  # type: ignore[operator]
        if distance >= 16.0 and not close_enough(live.value):  # type: ignore[arg-type]
            fitts = ctx.controller.config.fitts
            width = max(2.0 * abs(response.gain) * tolerance, 4.0)
            duration = intent.duration or 1.3 * fitts.movement_time(max(distance, abs(response.gain * error)), width)
            # Short by about as far as the pointer travels, at its fastest,
            # in the time a sluggish application takes to report back.
            peak = 1.875 * distance / duration
            unsure = cfg.scrub_latency if trial is not None or lag[0] == 0.0 else 0.015 + 0.25 * lag[0]
            short = min(max(unsure * 0.6 * peak, 4.0), 0.25 * distance)
            stop_at = want - error * (short / distance)
            sweep = _Sweep(ctx, response, observe, point_at, limits, live.value, stop_at, duration, allowed(), peak)  # type: ignore[arg-type]
            yield from reach(ctx, sweep, allow=duration)

        best_error = float("inf")
        stale = 0
        last: tuple[float, float] | None = None  # the previous homing step and the value before it
        for _ in range(cfg.scrub_iterations):
            live = yield from read()
            s_now = offset_of(live.bounds)
            response.settle(s_now, live.value)  # type: ignore[arg-type]
            if close_enough(live.value):  # type: ignore[arg-type]
                # Hold a little longer before believing it. The report can
                # still belong to where the pointer was a moment ago, and on
                # a stepped control that moment can be a different step.
                sure = yield from _settled_value(ctx, ident, cfg.scrub_confirm, lag_seen)
                if sure is None or sure.value is None:
                    raise SkillFailure("not_found", "the control disappeared while being dragged")
                if abs(sure.value - live.value) > eps:  # type: ignore[operator]
                    live = sure
                    s_now = offset_of(live.bounds)
                    response.settle(s_now, live.value)  # type: ignore[arg-type]
                else:
                    live = sure
                if close_enough(live.value):  # type: ignore[arg-type]
                    break
            error = abs(want - live.value)  # type: ignore[operator]
            if error < best_error - eps:
                best_error, stale = error, 0
            else:
                stale += 1
                if stale >= 7:
                    break

            finest = response.pinch(want)
            if finest is not None:
                # The goal lies between two neighbouring pixels. Settle for
                # the nearer one; the caller's tolerance decides if it will do.
                if abs(finest[0] - s_now) < 0.75:
                    break
                goal = finest[0]
            else:
                goal = response.home(want, s_now)
                if abs(goal - s_now) < 1.0:
                    # Closer than a pixel by the numbers, yet not there: nudge.
                    goal = s_now + (1.0 if response.gain * (want - live.value) >= 0 else -1.0)  # type: ignore[operator]
            if last is not None and abs(live.value - last[1]) <= eps and (goal - s_now) * last[0] > 0:  # type: ignore[operator]
                # The last step changed nothing, so this is a flat stretch of
                # the response. Stride out until something gives.
                stride = 2.0 * abs(last[0])
                if abs(goal - s_now) < stride:
                    goal = s_now + (stride if goal > s_now else -stride)
            last = (goal - s_now, live.value)  # type: ignore[assignment]
            low, high = limits(live)
            goal = min(max(goal, low), high)
            if abs(goal - s_now) < 0.5:
                break  # pinned against the end of the control

            def dest(c: "Context", goal: float = goal) -> Vec2:
                # Follows the control if it moves under the pointer mid-drag.
                return point_at(c.world.get(ident) or live, goal)

            yield from reach(ctx, PointTarget(dest, width=4.0))
        ctx.emit(
            "scrubbed",
            value=live.value,
            wanted=want,
            px_per_unit=round(response.gain, 4),
            readings=len(response),
            lag=round(max(lag[0], lag_seen[0]), 3),
        )
    finally:
        ctx.release(1)

    final = yield from _fresh_value(ctx, ident, ctx.now)
    value = final.value if final is not None else None
    observed = {"value": value, "wanted": want, "started_at": start_value}
    if value is None or abs(value - want) > allowed() + eps:
        raise SkillFailure(
            "value_mismatch",
            f"{el.role} {el.name!r} reads {value} after scrubbing towards {want:g}",
            observed,
        )
    return observed


def scroll(ctx: "Context", intent: Scroll) -> Skill:
    cfg = ctx.config
    target = None
    if intent.target is not None:
        target = ElementTarget(intent.target)
        yield from reach(ctx, target)
    gap = 1.0 / cfg.scroll_rate

    if intent.notches is not None:
        sign = 1 if intent.notches > 0 else -1
        for _ in range(abs(intent.notches)):
            ctx.actuator.scroll(0, sign)
            yield from pause(ctx, gap)
        return {"notches": intent.notches}

    assert intent.until is not None
    sign = 1 if intent.direction == "down" else -1
    turned = 0
    while True:
        for m in ctx.world.find(intent.until):
            if ctx.world.visible_region(m.element):
                ctx.emit("scrolled_into_view", element=m.element.id, notches=turned)
                return {"notches": turned * sign, "element": m.element.to_obj()}
        if turned >= cfg.scroll_limit:
            raise SkillFailure(
                "not_found",
                f"{intent.until.describe()} did not come into view after {turned} notches",
            )
        ctx.actuator.scroll(0, sign)
        turned += 1
        # Give the application and the perception stream a frame or two to
        # show what the notch brought in before deciding on the next one.
        yield from pause(ctx, max(gap, 2.5 * ctx.perception.period))


def type_text(ctx: "Context", intent: Type) -> Skill:
    cfg = ctx.config
    observed: dict[str, Any] = {}
    if intent.target is not None:
        target = ElementTarget(intent.target)
        aim = yield from _click(ctx, target, 1, 1, ())
        observed = _observed(aim, ctx, target)
        yield from pause(ctx, cfg.focus_delay)
    if intent.clear:
        ctx.key_down("ctrl")
        yield
        ctx.tap("a")
        yield
        ctx.key_up("ctrl")
        yield
    budget = 0.0
    i = 0
    while i < len(intent.text):
        budget += cfg.type_rate * ctx.dt
        n = int(budget)
        if n:
            budget -= n
            ctx.actuator.type_text(intent.text[i : i + n])
            i += n
        yield
    if intent.target is not None and aim.element is not None:
        # Report what the field actually holds now, for whoever checks.
        ctx.attend(aim.element.id)
        sent_at = ctx.now
        while (ctx.world.last_seen(aim.element.id) or 0.0) <= sent_at + 0.03 and ctx.now < sent_at + 0.3:
            yield
        field = ctx.world.get(aim.element.id)
        if field is not None and field.text is not None:
            observed["text"] = field.text
    if intent.enter:
        ctx.tap("Return")
        yield
    observed["typed"] = len(intent.text)
    return observed


def key(ctx: "Context", intent: Key) -> Skill:
    parts = [p.strip() for p in intent.keys.split("+") if p.strip()]
    if not parts:
        raise SkillFailure("invalid", "no key given")
    for name in parts:
        ctx.key_down(name)
        yield
    yield from pause(ctx, ctx.config.press_time)
    for name in reversed(parts):
        ctx.key_up(name)
        yield
    return {"keys": "+".join(parts)}


def wait(ctx: "Context", intent: Wait) -> Skill:
    yield from await_expect(ctx, intent.expect)
    return {}


SKILLS: dict[type, Callable[["Context", Any], Skill]] = {
    Move: move,
    Click: click,
    Hover: hover,
    Drag: drag,
    Scrub: scrub,
    Scroll: scroll,
    Type: type_text,
    Key: key,
    Wait: wait,
}


def skill_for(intent: Intent) -> Callable[["Context", Any], Skill]:
    try:
        return SKILLS[type(intent)]
    except KeyError:
        raise SkillFailure("invalid", f"no skill for {type(intent).__name__}") from None
