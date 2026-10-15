"""Goal-review fixture synchronization boundaries, without inference or networking."""
import json
import threading
from types import SimpleNamespace

import pytest

import test_forge_goal_review_audit as fixture


class Store:
    def __init__(self):
        self.listeners = []
        self.subscribed = self.unsubscribed = 0
        self.rows = []

    def subscribe_events(self, listener):
        self.listeners.append(listener)
        self.subscribed += 1

    def unsubscribe_events(self, listener):
        self.listeners.remove(listener)
        self.unsubscribed += 1

    def emit(self, kind='done', run='main'):
        for listener in list(self.listeners):
            listener({'type': kind, 'run_id': run})

    def runs(self):
        return self.rows


class Event:
    def __init__(self, hook=None):
        self.latched = False
        self.timeouts = []
        self.hook = hook

    def set(self):
        self.latched = True

    def is_set(self):
        return self.latched

    def wait(self, timeout):
        self.timeouts.append(timeout)
        if self.hook:
            self.hook()
        return self.latched


def setup(monkeypatch, responses, *, clock=(100, 103), event=None):
    store = Store()
    event = event or Event()
    ticks = iter(clock)
    monkeypatch.setattr(fixture, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(fixture, 'threading', SimpleNamespace(Event=lambda: event))
    calls = []
    responses = iter(responses)

    def poll(identifier):
        assert store.listeners, 'The listener must precede the initial durable read.'
        calls.append(identifier)
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value(store, event) if callable(value) else value

    svc = SimpleNamespace(store=store, jobs=SimpleNamespace(poll=poll), core=SimpleNamespace(requests=[]))
    return svc, event, calls


PENDING = {'finished': False, 'status': 'running', 'events': []}
DONE = {'finished': True, 'status': 'completed', 'events': [{'type': 'done'}]}


def assert_removed(svc):
    assert svc.store.listeners == []
    assert svc.store.subscribed == svc.store.unsubscribed == 1


def test_historical_completion_uses_initial_durable_snapshot(monkeypatch):
    svc, event, calls = setup(monkeypatch, [DONE], clock=(100,))
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert event.timeouts == [] and calls == ['main']
    assert_removed(svc)


def test_completion_committed_during_initial_read_is_not_lost(monkeypatch):
    def initial(store, event):
        store.emit()
        return DONE
    svc, event, calls = setup(monkeypatch, [initial], clock=(100,))
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert event.latched and not event.timeouts and len(calls) == 1
    assert_removed(svc)


def test_completion_between_initial_snapshot_and_wait_remains_latched(monkeypatch):
    def initial(store, event):
        store.emit()
        return PENDING
    svc, event, calls = setup(monkeypatch, [initial, DONE])
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert event.latched and event.timeouts == [12] and calls == ['main', 'main']
    assert_removed(svc)


def test_completion_during_wait_requires_final_durable_read(monkeypatch):
    svc, event, calls = setup(monkeypatch, [PENDING, DONE])
    event.hook = lambda: svc.store.emit()
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert event.latched and len(calls) == 2
    assert_removed(svc)


@pytest.mark.parametrize('kind,identifier', [('done', 'child'), ('status', 'main'), ('done', 'unrelated')])
def test_only_exact_main_terminal_event_wakes(monkeypatch, kind, identifier):
    svc, event, calls = setup(monkeypatch, [PENDING, PENDING])
    event.hook = lambda: svc.store.emit(kind, identifier)
    with pytest.raises(AssertionError, match='terminal_wait_deadline'):
        fixture._finished_goal_review_fixture(svc, {'id': 'main'})
    assert not event.latched and event.timeouts == [12] and len(calls) == 2
    assert_removed(svc)


def test_notification_without_durable_finished_never_succeeds(monkeypatch):
    svc, event, calls = setup(monkeypatch, [PENDING, PENDING])
    event.hook = lambda: svc.store.emit()
    with pytest.raises(AssertionError, match='terminal_event_without_finished_snapshot'):
        fixture._finished_goal_review_fixture(svc, {'id': 'main'})
    assert event.latched and len(calls) == 2
    assert_removed(svc)


def test_final_durable_completion_wins_even_without_notification(monkeypatch):
    svc, event, calls = setup(monkeypatch, [PENDING, DONE])
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert not event.latched and event.timeouts == [12] and len(calls) == 2
    assert_removed(svc)


@pytest.mark.parametrize('finished', ['true', 1, False])
def test_truthy_metadata_is_not_a_finished_snapshot(monkeypatch, finished):
    invalid = {**PENDING, 'finished': finished, 'status': 'completed'}
    svc, event, _ = setup(monkeypatch, [invalid, invalid])
    with pytest.raises(AssertionError, match='terminal_wait_deadline'):
        fixture._finished_goal_review_fixture(svc, {'id': 'main'})
    assert_removed(svc)


@pytest.mark.parametrize('now,remainder', [(100, 15), (103, 12), (115, 0), (116, 0)])
def test_one_original_fifteen_second_budget_never_restarts(monkeypatch, now, remainder):
    svc, event, calls = setup(monkeypatch, [PENDING, DONE], clock=(100, now))
    assert fixture._finished_goal_review_fixture(svc, {'id': 'main'}) is DONE
    assert event.timeouts == [remainder] and len(calls) == 2
    assert_removed(svc)


@pytest.mark.parametrize('stage', ['initial', 'wait', 'final'])
def test_exception_paths_always_remove_the_listener(monkeypatch, stage):
    responses = [ValueError('Synthetic read failure')] if stage == 'initial' else [PENDING, ValueError('Synthetic read failure')]
    svc, event, _ = setup(monkeypatch, responses)
    if stage == 'wait':
        def fail(): raise ValueError('Synthetic wait failure')
        event.hook = fail
    with pytest.raises(ValueError, match='Synthetic'):
        fixture._finished_goal_review_fixture(svc, {'id': 'main'})
    assert_removed(svc)


def test_failure_diagnostics_keep_only_allowlisted_fixture_state():
    private = 'fixture-private-sentinel'
    main = {'id': 'main', 'mode': 'goal', 'status': 'running', 'phase': 'generating', 'rounds': 3,
        'request': private, 'recovery': private, 'failure': {'category': private, 'details': private},
        'settings': {'provider_id': 'ollama', 'model': 'fixture', 'context': 32768, 'tokens': 4096,
                     'thinking': True, 'adaptive_profile_ids': [private], 'api_key': private}}
    reviewer = {'id': 'child', 'parent_id': 'main', 'mode': 'goal_review', 'status': 'paused',
        'phase': 'checkpoint', 'rounds': 2, 'provider_turn_state': private,
        'settings': {'provider_id': private, 'model': private, 'context': True, 'thinking': private}}
    requests = [{'messages': [{'content': private}], 'tools': [
        {'function': {'name': 'read_file'}}, {'function': {'name': private}}]}]
    svc = SimpleNamespace(store=SimpleNamespace(runs=lambda: [main, reviewer, {'id': private}]),
        core=SimpleNamespace(requests=requests))
    snapshot = {**PENDING, 'events': [{'type': 'round', 'text': private}, {'type': private}]}
    result = fixture._goal_review_fixture_diagnostics(svc, {'id': 'main'}, snapshot,
        SimpleNamespace(requests=requests), 'terminal_wait_deadline')
    encoded = json.dumps(result)
    assert private not in encoded and 'api_key' not in encoded and 'provider_turn_state' not in encoded
    assert [row['role'] for row in result['runs']] == ['main', 'reviewer']
    assert result['runs'][0]['context'] == 32768 and result['runs'][0]['settings']['thinking'] is True
    assert result['runs'][1]['context'] is None and result['runs'][1]['model'] == 'other'
    assert result['local_offered_tools'] == result['reviewer_offered_tools'] == [['read_file']]
    assert result['event_types'] == ['round', 'other'] and result['state_read'] == 'available'


def test_unavailable_diagnostics_never_export_the_read_error():
    def fail(): raise ValueError('fixture-private-read-error')
    svc = SimpleNamespace(store=SimpleNamespace(runs=fail))
    result = fixture._goal_review_fixture_diagnostics(svc, {'id': 'main'}, PENDING, None, 'terminal_wait_deadline')
    assert result['state_read'] == 'unavailable' and 'fixture-private' not in json.dumps(result)


def test_real_finish_run_notifies_after_terminal_state_and_event_commit(tmp_path):
    svc = fixture.service(tmp_path)
    svc.jobs._launch = lambda run: None
    ready = threading.Event()
    reads, errors = [], []
    worker = None
    listener = None
    try:
        run = svc.jobs.start({'text': 'Synthetic terminal ordering.'})
        def committed(event):
            if event.get('run_id') == run['id'] and event.get('type') == 'done':
                reads.append((svc.store.run(run['id'])['status'], svc.store.events(run['id'])[-1]['type']))
        listener = committed
        svc.store.subscribe_events(listener)
        original = svc.jobs.poll
        calls = []
        def poll(identifier):
            snapshot = original(identifier)
            calls.append(snapshot['finished'])
            ready.set()
            return snapshot
        svc.jobs.poll = poll
        def complete():
            try:
                if not ready.wait(2): raise AssertionError('Synthetic initial read missing')
                svc.store.finish_run(run['id'], {'status': 'completed'}, {'status': 'completed'})
            except BaseException as error:
                errors.append(type(error).__name__)
        worker = threading.Thread(target=complete)
        worker.start()
        result = fixture._finished_goal_review_fixture(svc, run)
        worker.join(2)
        assert not worker.is_alive() and errors == []
        assert result['finished'] is True and result['status'] == 'completed'
        assert calls == [False, True] and reads == [('completed', 'done')]
    finally:
        ready.set()
        if worker: worker.join(2)
        if listener: svc.store.unsubscribe_events(listener)
        svc.shutdown()
