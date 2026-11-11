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

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Generator, Sequence

from .geometry import ZERO, Rect, Vec2
from .intents import Click, Drag, Expect, Hover, Intent, Key, Move, Scroll, Scrub, Type, Wait
from .motor.controller import MotionGoal
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
) -> Generator[None, None, Aim]:
    """Fly the cursor onto ``target`` and hold it there until settled."""
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

        ctx.goal = MotionGoal(
            aim.pos,
            aim.width,
            aim.vel,
            obstacles_for(ctx, aim.element, avoid),
            duration,
        )

        if _arrived(ctx, aim):
            if on_since is None:
                on_since = ctx.now
            if ctx.now - on_since >= cfg.settle_time:
                return aim
        else:
            on_since = None

        if ctx.now - started > cfg.reach_timeout:
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

    # Pixels of pointer travel per unit of value. This first guess assumes a
    # linear control whose low end is at the left or top; it is corrected
    # from the control's actual response below.
    gain = travel / span

    if abs(want - start_value) <= min(tolerance, 0.5 / abs(gain)):
        return {"value": start_value, "wanted": want, "started_at": start_value}

    def read() -> Generator[None, None, UIElement]:
        live = yield from _fresh_value(ctx, ident, ctx.now)
        if live is None or live.value is None:
            raise SkillFailure("not_found", "the control disappeared while being dragged")
        return live

    # -- take hold ---------------------------------------------------------
    # The accessibility tree gives the control's box, not where its handle
    # is. Along the track the handle is where the value says; across it, the
    # box may include a label or readout. So feel for it: press, nudge, and
    # if the value does not respond, let go and try a little off-centre.
    live = el
    grab = ctx.pos
    grab_bounds = el.bounds
    engaged = False
    remembered = ctx.memory.grip(el) if ctx.memory is not None else None
    # Quick nudges everywhere first; longer ones (which a stepped control
    # needs before it clicks over) only once those have all come up empty.
    attempts = [(c, (1.0,)) for c in cfg.scrub_cross[:3]]
    attempts += [(c, (3.0, 9.0)) for c in cfg.scrub_cross[:3]]
    attempts += [(c, (1.0, 3.0)) for c in cfg.scrub_cross[3:]]
    if remembered is not None:
        attempts.insert(0, (remembered, (1.0, 3.0, 9.0)))
    for cross, scales in attempts:

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
        if abs(live.value - before) > eps and abs(after - (1.0 - along)) < abs(after - along) - 0.1:  # type: ignore[operator]
            # The press landed on the track, not the handle, and the value
            # jumped to the mirror image of where we pressed: the control's
            # high end is at the left or top.
            gain = -gain

        error = want - live.value  # type: ignore[operator]
        toward = 1.0 if gain * error >= 0 else -1.0
        s = 0.0
        # Small nudges first; a stepped control needs a longer one to click over.
        offsets = [toward * sign * cfg.scrub_probe * scale for scale in scales for sign in (1.0, -1.0)]
        for offset in offsets:
            ref_v = live.value
            nudge = offset - s
            s = offset
            yield from reach(ctx, PointTarget(grab + axis * s, width=4.0))
            live = yield from read()
            dv = live.value - ref_v  # type: ignore[operator]
            if abs(dv) > eps:
                seen = nudge / dv
                gain = seen if seen * gain < 0 else gain * (0.5 + 0.5 * min(max(seen / gain, 0.125), 8.0))
                engaged = True
                break
        if engaged or abs(live.value - before) > eps:  # type: ignore[operator]
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
    try:
        swept = False
        best = float("inf")
        stale = 0
        stuck = 0
        for _ in range(cfg.scrub_iterations):
            error = want - live.value  # type: ignore[operator]
            if abs(error) <= min(tolerance, 0.5 / abs(gain)):
                break
            if abs(error) < best - eps:
                best, stale = abs(error), 0
            else:
                stale += 1
                if stale >= 4:
                    break
            step = gain * error
            if abs(step) < 0.5:
                break
            if abs(step) < 1.0:
                step = 1.0 if step > 0 else -1.0

            shift = Vec2(live.bounds.x - grab_bounds.x, live.bounds.y - grab_bounds.y)
            s_now = (ctx.pos - grab - shift).dot(axis)
            # Never chase a mis-estimated gain off the end of the control.
            origin = (grab + shift).dot(axis)
            low = (live.bounds.left if horizontal else live.bounds.top) - origin - pad
            high = (live.bounds.right if horizontal else live.bounds.bottom) - origin + pad
            s_next = min(max(s_now + step, low), high)

            def dest(c: "Context", s_next: float = s_next) -> Vec2:
                # Follow the control if it moves under the pointer mid-drag.
                now = (c.world.get(ident) or el).bounds
                return grab + Vec2(now.x - grab_bounds.x, now.y - grab_bounds.y) + axis * s_next

            ref_v = live.value
            yield from reach(
                ctx,
                PointTarget(dest, width=min(max(abs(s_next - s_now) / 2.0, 2.0), 12.0)),
                duration=None if swept else intent.duration,
            )
            swept = True
            live = yield from read()

            shift = Vec2(live.bounds.x - grab_bounds.x, live.bounds.y - grab_bounds.y)
            ds = (ctx.pos - grab - shift).dot(axis) - s_now
            dv = live.value - ref_v  # type: ignore[operator]
            if abs(ds) >= 1.0 and abs(dv) > eps:
                seen = ds / dv
                if seen * gain < 0:
                    # The control runs the other way round; adopt that outright.
                    gain = seen
                else:
                    gain *= 0.5 + 0.5 * min(max(seen / gain, 0.125), 8.0)
                stuck = 0
            elif abs(ds) >= 8.0:
                stuck += 1
                if stuck >= 3:
                    break
        ctx.emit("scrubbed", value=live.value, wanted=want, px_per_unit=round(gain, 4))
    finally:
        ctx.release(1)

    final = yield from _fresh_value(ctx, ident, ctx.now)
    value = final.value if final is not None else None
    observed = {"value": value, "wanted": want, "started_at": start_value}
    if value is None or abs(value - want) > tolerance:
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
