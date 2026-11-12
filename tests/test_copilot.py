# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent task context, admission handoff, and focus ownership."""
import json
import threading

from embodied_cursor.brain.automatic import is_command
from embodied_cursor.brain.llm import render_episode
from embodied_cursor.brain.planner import Plan
from embodied_cursor.desktop import DesktopBridge
from embodied_cursor.intents import intent_from_obj
from test_desktop import Messages, request


def test_followups_receive_actual_prior_outcomes_and_clear_without_reconnect(monkeypatch):
    messages = Messages()
    bridge = DesktopBridge(messages.emit, paced=False)
    desktop = bridge, messages
    try:
        request(desktop, 1, 'connect', mode='simulation')
        session = bridge.session
        request(desktop, 2, 'run', goal='fill Notes with hello', config={'provider': 'offline'})
        episodes = []
        class FollowUp:
            def plan(self, episode):
                episodes.append(episode)
                old = json.loads(episode.context)['previous_tasks'][-1]
                assert old['goal'] == 'fill Notes with hello'
                assert old['status'] == 'done'
                ident = old['actions'][-1]['target']['id']
                assert any(el['id'] == ident for el in episode.observation.elements())
                return Plan((intent_from_obj({'action': 'type', 'target': {'id': ident}, 'text': ' again'}),), True)
        monkeypatch.setattr(bridge, '_planner', lambda config: FollowUp())
        result = request(desktop, 3, 'run', goal='add again to that note', config={'localFirst': False})
        assert result['result']['status'] == 'done'
        assert bridge.session is session
        assert len(bridge.memory.turns) == 2 and bridge.memory.completed == 2
        assert 'SESSION CONTEXT' in render_episode(episodes[0])
        notes = next(w for w in session.actuator.widgets.values() if w.name == 'Notes')
        assert notes.text == 'hello again'
        request(desktop, 4, 'clear-context')
        assert not bridge.memory.turns and bridge.session is session
        request(desktop, 5, 'run', goal='click Export', config={'provider': 'offline'})
        assert len(bridge.memory.turns) == 1
        request(desktop, 6, 'disconnect')
        assert not bridge.memory.turns
    finally:
        bridge.close()


def test_completion_reply_is_sent_only_after_next_instruction_can_be_admitted():
    messages = Messages()
    replies_while_running = []
    def emit(item):
        if item.get('id') in (2, 3):
            replies_while_running.append(bridge.running)
        messages.emit(item)
    bridge = DesktopBridge(emit, paced=False)
    desktop = bridge, messages
    try:
        request(desktop, 1, 'connect', mode='simulation')
        for ident in (2, 3):
            assert request(desktop, ident, 'run', goal='click Export', config={'provider': 'offline'})['ok']
        assert replies_while_running == [False, False]
    finally:
        bridge.close()


def test_busy_context_cannot_be_cleared_and_late_plans_do_not_click(monkeypatch):
    messages = Messages()
    bridge = DesktopBridge(messages.emit, paced=False)
    desktop = bridge, messages
    entered, release = threading.Event(), threading.Event()
    class Blocked:
        def plan(self, episode):
            entered.set()
            release.wait(2)
            return Plan((intent_from_obj({'action': 'click', 'target': 'Export'}),), True)
    monkeypatch.setattr(bridge, '_planner', lambda config: Blocked())
    try:
        request(desktop, 1, 'connect', mode='simulation')
        bridge.submit({'id': 2, 'op': 'run', 'goal': 'do it', 'config': {'localFirst': False}})
        assert entered.wait(1)
        assert not request(desktop, 3, 'clear-context')['ok']
        request(desktop, 4, 'stop')
        release.set()
        report = messages.wait(lambda item: item.get('id') == 2)['result']
        assert report['status'] == 'interrupted'
        assert bridge.session is not None
    finally:
        release.set()
        bridge.close()


def test_context_is_bounded_and_never_treated_as_an_instruction():
    from embodied_cursor.session import SessionMemory
    from embodied_cursor.brain.agent import Report
    memory = SessionMemory()
    for i in range(20):
        memory.remember(Report(str(i) + 'x' * 2000, 'failed', 'not_found'))
    assert len(memory.turns) == 8
    assert len(memory.context()) <= 12000
    assert len(json.loads(memory.context())['previous_tasks']) <= 8
    assert memory.completed == 0


def test_contextual_targets_use_planner_but_quoted_text_remains_local():
    assert not is_command('click that')
    assert not is_command('type hello into it')
    assert is_command('type "click that" into Notes')
    assert is_command('click "that"')
    assert is_command('fill Notes with go there')
    assert is_command('type go there into Notes')


def test_substrate_context_is_refreshed_at_each_planning_round():
    from embodied_cursor.brain.agent import Agent
    from embodied_cursor.desktop import simulation_session
    session = simulation_session(paced=False)
    calls, seen = [], []
    def context():
        calls.append(len(calls) + 1)
        return json.dumps({'motion_tick': calls[-1]})
    class Planner:
        def plan(self, episode):
            seen.append(json.loads(episode.context)['motion_tick'])
            return (Plan(done=True) if episode.history else
                    Plan((intent_from_obj({'action': 'click', 'target': 'Export'}),)))
    try:
        assert Agent(session.body, Planner()).run('export', context=context).ok
        assert seen == [1, 2]
    finally:
        session.close()


def test_keyboard_guard_blocks_self_typing_and_releases_pressed_modifiers(monkeypatch):
    from embodied_cursor.desktop import simulation_session
    session = simulation_session(paced=False)
    bridge = DesktopBridge(lambda _: None, paced=False)
    try:
        bridge.session = session
        bridge.mode = 'live'
        bridge.controller_windows = {42}
        session.body.before_keyboard = bridge._guard_keyboard
        foreground = iter([99, 42])
        monkeypatch.setattr(session.actuator, 'foreground_window', lambda: next(foreground), raising=False)
        outcome = session.body.execute(intent_from_obj({'action': 'key', 'keys': 'ctrl+s'}))
        assert outcome.reason == 'controller_focused'
        assert session.actuator.keys == [('ctrl', True), ('ctrl', False)]
        assert not session.body.ctx._keys
        monkeypatch.setattr(session.actuator, 'foreground_window', lambda: 42)
        outcome = session.body.execute(intent_from_obj({'action': 'type', 'text': 'must not enter the dock'}))
        assert outcome.reason == 'controller_focused'
        assert not session.actuator.typed
    finally:
        bridge.close()


def test_explicit_followups_reuse_a_live_target_locally_without_cloud_credentials():
    messages = Messages()
    bridge = DesktopBridge(messages.emit, paced=False)
    desktop = bridge, messages
    try:
        request(desktop, 10, 'connect', mode='simulation')
        request(desktop, 11, 'run', goal='fill Notes with hello', config={'provider': 'offline'})
        result = request(desktop, 12, 'run', goal='fill that with goodbye!', config={'provider': 'ollama-cloud'})
        assert result['result']['status'] == 'done'
        notes = next(w for w in bridge.session.actuator.widgets.values() if w.name == 'Notes')
        assert notes.text == 'goodbye!', 'Reference resolution must preserve the typed punctuation'
        result = request(desktop, 13, 'run', goal='click that', config={'provider': 'ollama-cloud'})
        assert result['result']['status'] == 'done'
        assert result['result']['outcomes'][0]['observed']['element']['id'] == notes.id
    finally:
        bridge.close()


def test_a_stale_or_failed_previous_target_is_never_rebound_to_a_similar_name():
    from embodied_cursor.brain.automatic import resolve_followup
    from embodied_cursor.brain.observation import Observation
    from embodied_cursor.brain.planner import Episode
    context = json.dumps({'previous_tasks': [{'status': 'done', 'actions': [
        {'status': 'ok', 'target': {'id': 'sim:old', 'name': 'Save', 'role': 'button'}}]}]})
    observation = Observation(0, (0, 0), '', surfaces=[{'elements': [{'id': 'sim:new', 'name': 'Save', 'role': 'button'}]}])
    assert resolve_followup(Episode('click that', observation, context=context)) is None


def test_an_unresolved_offline_reference_does_not_click_a_literal_that_button():
    from embodied_cursor.backends.sim import Button, SimDesktop, Window, make_body
    from embodied_cursor.brain.agent import Agent
    from embodied_cursor.brain.automatic import AutomaticPlanner
    from embodied_cursor.brain.heuristic import HeuristicPlanner
    from embodied_cursor.geometry import Rect
    desktop = SimDesktop()
    window = desktop.add(Window('Editor', Rect(0, 0, 600, 400)))
    button = window.add(Button('that', Rect(100, 100, 100, 40)))
    body = make_body(desktop)
    try:
        report = Agent(body, AutomaticPlanner(HeuristicPlanner)).run('click that')
        assert report.status == 'failed'
        assert button.clicks == 0
    finally:
        body.close()
