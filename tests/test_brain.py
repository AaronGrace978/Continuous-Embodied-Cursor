# SPDX-License-Identifier: GPL-3.0-or-later
"""The slow half: observation, planners, and the agent that joins them to the body."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from embodied_cursor.backends.sim import (
    Button,
    Dialog,
    Menu,
    MenuBar,
    SimDesktop,
    Slider,
    TextField,
    Window,
    make_body,
)
from embodied_cursor.body import BodyConfig
from embodied_cursor.brain.agent import Agent
from embodied_cursor.brain.heuristic import HeuristicPlanner, Unparseable, parse_clause, parse_target, split_clauses
from embodied_cursor.brain.llm import LLMPlanner, parse_reply, render_episode
from embodied_cursor.brain.observation import observe
from embodied_cursor.brain.planner import Episode, Plan, PlannerError
from embodied_cursor.geometry import Rect, Vec2
from embodied_cursor.intents import Click, Key, Scrub, Type, intent_from_obj
from embodied_cursor.world.selector import Selector


@pytest.fixture
def app():
    d = SimDesktop(pointer=Vec2(200, 800))
    win = d.add(Window("Editor", Rect(100, 100, 1400, 800)))
    bar = win.add(MenuBar("", Rect(100, 100, 1400, 30)))
    file = bar.add(Menu("File", Rect(100, 100, 60, 30)))
    file.item("New")
    export = file.submenu("Export as")
    file.item("Close")
    d.pdf = export.item("PDF document")
    export.item("PNG image")
    d.wrap = win.add(Button("Wrap lines", Rect(300, 300, 140, 28), checkable=True))
    d.volume = win.add(Slider("Volume", Rect(300, 400, 400, 20), value=20))
    d.search = win.add(TextField("Search", Rect(300, 500, 300, 30)))
    d.dialog = d.add(Dialog("Unsaved changes", Rect(500, 300, 500, 300)))
    d.discard = d.dialog.add(Button("Discard", Rect(820, 540, 120, 36), on_click=lambda b: d.dialog.hide()))
    d.delete = win.add(Button("Delete", Rect(900, 300, 100, 30)))
    d.win = win
    return d


# -- observation --------------------------------------------------------------


def test_observation_lists_the_screen_as_text_not_pixels(app):
    body = make_body(app)
    obs = observe(body.world, app.pointer)
    text = obs.text()
    assert 'window "Editor"' in text
    assert 'slider "Volume" value=20 range 0..100' in text
    assert 'checkbox "Wrap lines"' in text
    assert "File > Export as > PDF document" in text
    assert "Discard" not in text  # the dialog is not open
    assert len(text) < 1500


def test_observation_reports_state(app):
    app.wrap.checked = True
    app.dialog.show()
    body = make_body(app)
    obs = observe(body.world, Vec2(370, 314))
    text = obs.text()
    assert 'over checkbox "Wrap lines"' in text
    assert 'checkbox "Wrap lines" (checked)' in text
    assert text.index('dialog "Unsaved changes"') < text.index('window "Editor"')
    delete = next(e for e in obs.elements() if e["name"] == "Delete")
    assert "covered" in delete["states"]


# -- command language ---------------------------------------------------------


def test_clauses_split_outside_quotes():
    assert split_clauses('click Export; type "a; then b" into Search, then press enter.') == [
        "click Export",
        'type "a; then b" into Search',
        "press enter",
    ]


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("Export", Selector(name="Export")),
        ("the Export button", Selector(role="button", name="Export")),
        ('button "Export"', Selector(role="button", name="Export")),
        ('the "Save as" menu item', Selector(role="menu_item", name="Save as")),
        ("Volume slider", Selector(role="slider", name="Volume")),
        ("Search field", Selector(role="text_field", name="Search")),
        ("OK in the Settings dialog", Selector(name="OK", within=Selector(role="dialog", name="Settings"))),
        ("#atspi:42", Selector(id="atspi:42")),
    ],
)
def test_target_phrases(phrase, expected):
    assert parse_target(phrase) == expected


def test_clause_parsing_covers_the_command_language():
    obs = None
    assert parse_clause("press ctrl+s").build(obs) == [Key("ctrl+s")]
    assert parse_clause("press the Export button").build(obs) == [Click(Selector(role="button", name="Export"))]
    assert parse_clause("double-click Report").build(obs) == [Click(Selector(name="Report"), count=2)]
    assert parse_clause("right click Report").build(obs) == [Click(Selector(name="Report"), button="right")]
    assert parse_clause("set Volume to 70").build(obs) == [Scrub(Selector(name="Volume"), value=70.0)]
    assert parse_clause("set the Volume slider to 25% over 2s").build(obs) == [
        Scrub(Selector(role="slider", name="Volume"), fraction=0.25, duration=2.0)
    ]
    assert parse_clause('type "hi there" into Search and press enter').build(obs) == [
        Type("hi there", Selector(name="Search"), True)
    ]
    assert parse_clause("type hello").build(obs) == [Type("hello", None, False)]
    with pytest.raises(Unparseable):
        parse_clause("make the report look nicer")


# -- agent with the offline planner ------------------------------------------


def test_agent_runs_a_multi_step_goal(app):
    body = make_body(app, snapshot_hz=60)
    events = []
    agent = Agent(body, HeuristicPlanner(), on_event=lambda kind, data: events.append(kind))
    report = agent.run(
        'open File > Export as > PDF document; set Volume to 70; check Wrap lines; '
        'type "q3 report" into Search and press enter'
    )
    assert report.ok, report.describe()
    assert app.pdf.activated == 1
    assert app.volume.value == pytest.approx(70, abs=1.0)
    assert app.wrap.checked
    assert app.search.submitted == ["q3 report"]
    # One decision was enough; everything else was the body's business.
    assert report.rounds == 1
    assert events.count("plan") == 1


def test_steps_that_already_hold_are_skipped(app):
    app.wrap.checked = True
    body = make_body(app)
    report = Agent(body, HeuristicPlanner()).run("check Wrap lines")
    assert report.ok
    assert report.outcomes == []
    assert app.wrap.clicks == 0


def test_agent_replans_when_a_dialog_interrupts(app):
    # Deleting pops a confirmation over the rest of the window.
    app.delete.on_click = lambda b: app.dialog.show()

    class Scripted:
        def __init__(self):
            self.episodes = []

        def plan(self, episode):
            self.episodes.append(episode)
            if len(self.episodes) == 1:
                return Plan((intent_from_obj({"action": "click", "target": "Delete"}),
                             intent_from_obj({"action": "click", "target": "Wrap lines"})), done=True)
            assert "Unsaved changes" in episode.surprise
            assert 'button "Discard"' in episode.observation.text()
            return Plan((intent_from_obj({"action": "click", "target": "Discard",
                                          "expect": {"vanishes": 'dialog "Unsaved changes"'}}),
                         intent_from_obj({"action": "click", "target": "Wrap lines"})), done=True)

    planner = Scripted()
    body = make_body(app, snapshot_hz=60)
    report = Agent(body, planner).run("delete, then turn on wrapping")
    assert report.ok, report.describe()
    assert len(planner.episodes) == 2
    assert app.wrap.checked and not app.dialog.visible


def test_agent_falls_back_when_a_path_is_not_in_the_tree(app):
    """A > B that is not a menu path is retried as separate clicks."""
    panel = app.win.add(Button("Advanced", Rect(300, 600, 120, 30)))
    hidden = app.win.add(Button("Reset", Rect(500, 600, 120, 30)))
    hidden.visible = False
    panel.on_click = lambda b: setattr(hidden, "visible", True)
    body = make_body(app, snapshot_hz=60, config=BodyConfig(resolve_timeout=0.5))
    report = Agent(body, HeuristicPlanner()).run("click Advanced > Reset")
    assert report.ok, report.describe()
    assert hidden.clicks == 1
    assert report.rounds == 2
    assert report.outcomes[0].reason == "not_found"


def test_agent_reports_what_it_could_not_do(app):
    body = make_body(app, config=BodyConfig(resolve_timeout=0.3))
    report = Agent(body, HeuristicPlanner()).run("click Frobnicate")
    assert report.status == "failed"
    assert "not_found" in report.reason
    assert Agent(body, HeuristicPlanner()).run("tidy up my desktop").status == "failed"


def test_agent_stops_for_a_human_instead_of_replanning(app):
    app.at(0.15, lambda: app.bump_pointer(80, -50))
    body = make_body(app)
    planner = HeuristicPlanner()
    report = Agent(body, planner).run("click Delete; check Wrap lines")
    assert report.status == "interrupted" and report.reason == "human_override"
    assert report.rounds == 1
    assert app.delete.clicks == 0 and not app.wrap.checked


# -- LLM planner against a local stand-in -------------------------------------


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, dict(self.headers), body))
        reply = self.server.replies.pop(0)
        if isinstance(reply, int):
            self.send_response(reply)
            self.end_headers()
            self.wfile.write(b'{"error": "response_format is not supported"}')
            return
        payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    server.requests, server.replies = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}/v1"
    yield server
    server.shutdown()
    server.server_close()


def test_reply_parsing_is_strict_about_intents_and_lenient_about_wrapping():
    plan = parse_reply('Sure!\n```json\n{"intents": [{"action": "click", "target": "button \\"OK\\""}], "done": true}\n```')
    assert plan.done and plan.intents == (Click(Selector(role="button", name="OK")),)
    assert parse_reply('{"give_up": "nothing to click"}').give_up == "nothing to click"
    with pytest.raises(ValueError, match="intent 1 is invalid"):
        parse_reply('{"intents": [{"action": "click", "x": 10, "y": 20}]}')
    with pytest.raises(ValueError):
        parse_reply('{"intents": [], "done": false}')
    with pytest.raises(ValueError):
        parse_reply("I clicked it for you.")


def test_llm_planner_drives_the_body_through_an_openai_style_api(app, stub):
    stub.replies = [
        json.dumps({
            "intents": [
                {"action": "click", "target": 'menu_item "PDF document"'},
                {"action": "scrub", "target": 'slider "Volume"', "value": 55},
            ],
            "done": True,
            "note": "export, then set the level",
        })
    ]
    body = make_body(app, snapshot_hz=60)
    planner = LLMPlanner(stub.url, "test-model", api_key="sk-test")
    report = Agent(body, planner).run("export as PDF and set the volume to 55")
    assert report.ok, report.describe()
    assert app.pdf.activated == 1
    assert app.volume.value == pytest.approx(55, abs=1.0)

    path, headers, request = stub.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert request["model"] == "test-model"
    user = request["messages"][1]["content"]
    assert "GOAL: export as PDF" in user and 'slider "Volume" value=20' in user
    # Text only: nothing resembling an image goes to the model.
    assert all(isinstance(m["content"], str) for m in request["messages"])


def test_llm_planner_repairs_an_unusable_reply_and_sees_failures(app, stub):
    stub.replies = [
        '{"intents": [{"action": "click", "x": 640, "y": 480}]}',
        '{"intents": [{"action": "click", "target": "Frobnicate"}], "done": true}',
        '{"intents": [{"action": "click", "target": "Wrap lines", "expect": {"on": "Wrap lines", "state": "checked"}}], "done": true}',
    ]
    body = make_body(app, config=BodyConfig(resolve_timeout=0.3))
    report = Agent(body, LLMPlanner(stub.url, "m")).run("turn on wrapping")
    assert report.ok, report.describe()
    assert app.wrap.checked
    assert len(stub.requests) == 3
    # The coordinate-based reply was rejected and explained back to the model.
    repair = stub.requests[1][2]["messages"][-1]["content"]
    assert "could not be used" in repair and "does not take" in repair
    # After the failed click the model was told why.
    assert "[not_found]" in stub.requests[2][2]["messages"][1]["content"]


def test_llm_planner_copes_without_json_mode_and_reports_dead_endpoints(app, stub):
    stub.replies = [400, '{"intents": [], "done": true}']
    planner = LLMPlanner(stub.url, "m")
    body = make_body(app)
    episode = Episode("nothing", observe(body.world, app.pointer))
    assert planner.plan(episode).done
    assert "response_format" in stub.requests[0][2] and "response_format" not in stub.requests[1][2]

    dead = LLMPlanner("http://127.0.0.1:9/v1", "m", timeout=2.0)
    with pytest.raises(PlannerError, match="could not reach"):
        dead.plan(episode)
    report = Agent(body, dead).run("anything")
    assert report.status == "failed" and report.reason.startswith("planner:")


def test_rendered_episode_flags_a_click_that_probably_missed(app):
    body = make_body(app)
    out = body.execute(intent_from_obj({"action": "click", "target": "Delete"}))
    out.observed["target_moved_away"] = True
    text = render_episode(Episode("g", observe(body.world, app.pointer), [out], "dialog \"X\" appeared"))
    assert "probably missed" in text and "UNEXPECTED" in text
