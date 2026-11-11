# SPDX-License-Identifier: GPL-3.0-or-later
"""``cec``: drive the embodied cursor from a shell.

    cec demo                               simulated scenarios, no display needed
    cec look                               what a planner would be shown, as text
    cec find 'slider "Volume"'             elements matching a selector
    cec do 'click Export; set Volume to 70'    plain commands, planned offline
    cec run 'export this as a PDF'         open-ended goal, planned by an LLM
    cec intent '{"action": "click", "target": "button \\"Export\\""}'
    cec daemon                             keep perception warm and serve a socket
    cec send '{"op": "status"}'            talk to the daemon
    cec render trace.json -o trace.svg     draw a recorded run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .brain.planner import Planner, PlannerError


def _planner(kind: str) -> Planner:
    if kind == "llm":
        from .brain.llm import LLMPlanner

        return LLMPlanner.from_env()
    if kind == "heuristic":
        from .brain.heuristic import HeuristicPlanner

        return HeuristicPlanner()
    raise ValueError(f"unknown planner {kind!r}; expected 'heuristic' or 'llm'")


def _session(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from .backends.linux import open_session
    from .backends.x11 import BackendUnavailable
    from .motor.controller import MotorConfig

    try:
        session = open_session(
            display=args.display,
            use_memory=not args.no_memory,
            motor=MotorConfig(tempo=args.tempo),
        )
    except BackendUnavailable as exc:
        raise SystemExit(f"cec: cannot reach the desktop: {exc}") from None
    for note in session.notes:
        print(f"note: {note}", file=sys.stderr)
    session.settle()
    return session


def _save_trace(session: Any, path: str | None, title: str) -> None:
    if not path:
        return
    from .render import render_svg, scene_of

    out = Path(path)
    if out.suffix == ".svg":
        out.write_text(render_svg(session.body.trace, scene_of(session.world), title), encoding="utf-8")
    else:
        obj = session.body.trace.to_obj()
        obj["scene"] = scene_of(session.world)
        obj["title"] = title
        out.write_text(json.dumps(obj), encoding="utf-8")
    print(f"trace written to {out}", file=sys.stderr)


def _print_event(kind: str, data: dict[str, Any]) -> None:
    if kind == "plan":
        steps = "; ".join(data["intents"]) or "(nothing to do)"
        note = f"  [{data['note']}]" if data.get("note") else ""
        print(f"plan {data['round']}: {steps}{note}", file=sys.stderr)
    elif kind == "surprise":
        print(f"  unexpected: {data['what']}; asking the planner again", file=sys.stderr)


# -- commands -----------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import SCENARIOS, run_all

    if args.scenario and args.scenario not in SCENARIOS:
        raise SystemExit(f"cec: unknown scenario {args.scenario!r}; choose from {', '.join(SCENARIOS)}")
    results = run_all(args.out, args.scenario)
    for r in results:
        print(f"{'ok  ' if r.ok else 'FAIL'} {r.title}")
        for fact in r.facts:
            print(f"       {fact}")
    if args.out:
        print(f"\ntraces written to {Path(args.out).resolve()}/")
    return 0 if all(r.ok for r in results) else 1


def cmd_look(args: argparse.Namespace) -> int:
    from .brain.observation import observe

    with _session(args) as session:
        obs = observe(session.world, session.actuator.position(), limit=args.limit)
        if args.json:
            print(json.dumps(obs.to_obj(), indent=2))
        else:
            print(obs.text())
            stats = session.perception.stats
            print(
                f"\n{len(session.world)} elements from {', '.join(type(s).__name__ for s in session.sources)}; "
                f"last attended-set refresh {stats.last_frame_s * 1000:.1f} ms",
                file=sys.stderr,
            )
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    with _session(args) as session:
        matches = session.world.find(args.selector, visible_only=not args.hidden)
        for m in matches[:40]:
            el = m.element
            x, y, w, h = (round(v) for v in el.bounds.as_tuple())
            extra = f" value={el.value:g}" if el.value is not None else ""
            shown = "" if el.visible else " (hidden)"
            print(f'{m.score:.2f}  {el.role} "{el.name}"  at {x},{y} {w}x{h}{extra}{shown}  #{el.id}')
        if not matches:
            print("no match", file=sys.stderr)
            return 1
    return 0


def cmd_intent(args: argparse.Namespace) -> int:
    from .intents import intent_from_obj

    try:
        intent = intent_from_obj(json.loads(args.json))
    except ValueError as exc:
        raise SystemExit(f"cec: bad intent: {exc}") from None
    with _session(args) as session:
        outcome = session.body.execute(intent)
        _save_trace(session, args.trace, intent.describe())
    print(json.dumps(outcome.to_obj(), indent=2) if args.json_out else outcome.describe())
    return 0 if outcome.ok else 1


def _run_goal(args: argparse.Namespace, planner_kind: str) -> int:
    from .brain.agent import Agent

    try:
        planner = _planner(planner_kind)
    except PlannerError as exc:
        raise SystemExit(f"cec: {exc}") from None
    with _session(args) as session:
        agent = Agent(session.body, planner, on_event=None if args.json_out else _print_event)
        report = agent.run(args.goal)
        _save_trace(session, args.trace, args.goal)
    if args.json_out:
        print(json.dumps(report.to_obj(), indent=2))
    else:
        print(report.describe())
        print(
            f"{report.rounds} planning round(s): {report.planning_time:.2f} s thinking, {report.acting_time:.2f} s moving",
            file=sys.stderr,
        )
    return 0 if report.ok else 1


def cmd_do(args: argparse.Namespace) -> int:
    return _run_goal(args, "heuristic")


def cmd_run(args: argparse.Namespace) -> int:
    return _run_goal(args, args.planner)


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon

    with _session(args) as session:
        daemon = Daemon(session.body, _planner, args.socket)
        try:
            daemon.start()
        except RuntimeError as exc:
            raise SystemExit(f"cec: {exc}") from None
        print(f"listening on {daemon.path}", file=sys.stderr)
        try:
            daemon.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            daemon.close()
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    from .daemon import default_socket_path, request

    try:
        obj = json.loads(args.json)
    except ValueError as exc:
        raise SystemExit(f"cec: bad request: {exc}") from None
    try:
        reply = request(obj, args.socket)
    except (OSError, ConnectionError) as exc:
        path = args.socket or default_socket_path()
        raise SystemExit(f"cec: no daemon answering on {path} ({exc}); start one with 'cec daemon'") from None
    if "text" in reply and not args.json_out:
        print(reply["text"])
    else:
        print(json.dumps(reply, indent=2))
    return 0 if reply.get("ok") else 1


def cmd_render(args: argparse.Namespace) -> int:
    from .render import render_svg

    obj = json.loads(Path(args.trace).read_text(encoding="utf-8"))
    out = Path(args.output or Path(args.trace).with_suffix(".svg"))
    out.write_text(render_svg(obj, obj.get("scene", ()), args.title or obj.get("title", "")), encoding="utf-8")
    print(out)
    return 0


# -- argument parsing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cec", description="Continuous embodied cursor.")
    parser.add_argument("--version", action="version", version=f"cec {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def live(p: argparse.ArgumentParser) -> None:
        p.add_argument("--display", help="X display to drive (default: $DISPLAY)")
        p.add_argument("--tempo", type=float, default=1.0, help="movement duration multiplier; above 1 is slower")
        p.add_argument("--no-memory", action="store_true", help="do not read or write spatial memory")

    def acting(p: argparse.ArgumentParser) -> None:
        live(p)
        p.add_argument("--trace", metavar="FILE", help="save the run as .svg (drawing) or .json (raw samples)")
        p.add_argument("--json", dest="json_out", action="store_true", help="print the result as JSON")

    p = sub.add_parser("demo", help="run the simulated scenarios; needs no display")
    p.add_argument("scenario", nargs="?", help="run just one scenario")
    p.add_argument("--out", metavar="DIR", help="write an SVG trace of each scenario here")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("look", help="print the screen as the planner sees it")
    live(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=120, help="most elements to list")
    p.set_defaults(func=cmd_look)

    p = sub.add_parser("find", help="list elements matching a selector")
    live(p)
    p.add_argument("selector", help="for example: 'button \"Export\"' or 'slider'")
    p.add_argument("--hidden", action="store_true", help="include elements that are not showing")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("do", help="carry out plainly worded commands (offline planner)")
    acting(p)
    p.add_argument("goal", help="for example: 'open File > Export as > PDF document; set Volume to 70'")
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("run", help="pursue an open-ended goal with a language-model planner")
    acting(p)
    p.add_argument("goal")
    p.add_argument("--planner", choices=("llm", "heuristic"), default="llm")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("intent", help="execute one intent given as JSON")
    acting(p)
    p.add_argument("json", help='for example: \'{"action": "scrub", "target": "slider", "fraction": 0.5}\'')
    p.set_defaults(func=cmd_intent)

    p = sub.add_parser("daemon", help="serve intents and goals on a Unix socket")
    live(p)
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("send", help="send one JSON request to a running daemon")
    p.add_argument("json", help='for example: \'{"op": "goal", "goal": "click Export"}\'')
    p.add_argument("--socket", metavar="PATH")
    p.add_argument("--json", dest="json_out", action="store_true", help="always print the raw reply")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("render", help="draw a saved .json trace as SVG")
    p.add_argument("trace")
    p.add_argument("-o", "--output")
    p.add_argument("--title")
    p.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
