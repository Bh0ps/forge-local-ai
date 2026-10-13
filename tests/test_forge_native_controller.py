"""Exact native orchestration with private runtime fixtures and no real update."""
from dataclasses import replace
import os
from pathlib import Path
import threading
import time

import psutil
import pytest

from forge_native_controller import NativeController
from forge_native_handoff import HandoffRejected, HelperTransport, NativeAuthority, NativeBinding, peer_frame
from forge_update_handoff import NativeBootstrap, NativePrepared, NativeReservation


@pytest.fixture
def flow(tmp_path):
    binding = [NativeBinding(object(), 'a' * 64, 'synthetic-main', 1, 'http://127.0.0.1:12345',
        os.getpid(), psutil.Process().create_time())]
    authority = NativeAuthority()
    calls = []
    config = {'fence': True, 'commit_ack': True, 'abort': True, 'before_reserve_return': None,
              'during_commit': None, 'during_prepare': None}
    class Manager:
        def bind_native_authority(self, value): assert value is authority
        def native_reserve(self, value, action='apply'):
            calls.append('reserve')
            self.reservation = NativeReservation('b' * 32, action, value, 'c' * 64, time.monotonic() + 120)
            if config['before_reserve_return']: config['before_reserve_return']()
            return self.reservation
        def native_cancel_reservation(self, reservation):
            assert reservation is self.reservation; calls.append('cancel-reservation'); return {'not_executed': True}
        def native_prepare(self, grant, ack):
            authority.consume(grant, 'prepare', binding[0], self.reservation)
            calls.append('prepare')
            if config['during_prepare']: config['during_prepare']()
            self.prepared = NativePrepared(self.reservation, 'd' * 32, grant.attempt_id,
                tuple(ack['receipts']), ack['workspace_revision'], ack['participant_count'], 'e' * 64)
            return self.prepared
        def native_launch(self, grant, prepared):
            authority.consume(grant, 'launch', self.reservation.binding, self.reservation)
            calls.append('launch')
            return NativeBootstrap(prepared, ('synthetic-verified-Forge.exe', '--forge-update'), tmp_path,
                'f' * 64, binding[0].session, 1, '5.1.3', None, binding[0].parent_pid,
                binding[0].parent_created, prepared.intent_sha256)
        def native_commit(self, grant, prepared, ready):
            authority.consume(grant, 'commit', self.reservation.binding, self.reservation)
            self.committed = True; calls.append('backend-commit')
            if config['during_commit']: config['during_commit']()
            return True
        def native_abort(self, identifier, stopped_helper=None):
            calls.append('abort')
            if getattr(self, 'committed', False):
                assert authority.consume_stopped_helper(stopped_helper, self.reservation, self.prepared.operation_id)
            return {'not_executed': config['abort']}
    class Process:
        pid = 12345
        returncode = None
        stdin = stdout = None
        def poll(self): return self.returncode
        def wait(self, timeout): self.returncode = 0; calls.append('owned-exit'); return 0
        def terminate(self): calls.append('owned-terminate'); self.returncode = 1
    class Channel:
        def __init__(self, auth): self.auth = auth; self.writing = threading.Event(); self.reads = 0
        def write(self, value, timeout=15, admit=None):
            if admit is not None and admit() is not True: raise HandoffRejected()
            calls.append(value['kind'])
        def read(self, timeout, cancel=None):
            self.reads += 1
            if self.reads == 2 and not config['commit_ack']: raise HandoffRejected('helper_failed')
            return peer_frame(self.auth, 'READY' if self.reads == 1 else 'COMMITTED', Process.pid)
    def transport_factory(bootstrap, auth):
        transport = object.__new__(HelperTransport)
        transport.bootstrap, transport.auth, transport.timeout = bootstrap, auth, 1
        transport.cancel = threading.Event(); transport.ready = None; transport.commit_sent = False
        transport.process = Process(); transport.channel = Channel(auth)
        return transport
    def fence(request):
        calls.append('renderer-fence')
        return {'ok': config['fence'], 'workspace_revision': request['workspace_revision'],
                'participant_count': request['participant_count'], 'phase': 'prepared'}
    def depart(controller, grant, transport): calls.append('depart'); return True
    controller = NativeController(Manager(), authority, binding=lambda: binding[0], input_idle=lambda: True,
        fence=fence, depart=depart, transport_factory=transport_factory)
    return controller, authority, calls, config, binding


def prepared(flow):
    controller, _, _, _, _ = flow
    first = controller.dispatch('update_handoff_begin', {'kind': 'apply'})
    response = controller.dispatch('update_handoff_ack', {'attempt_id': first['attempt_id'], 'challenge': first['challenge'],
        'workspace_revision': 3, 'participant_count': 2, 'receipts': []})
    assert response['phase'] == 'prepared'
    return {'attempt_id': response['attempt_id'], 'confirmation': response['confirmation'],
        'workspace_revision': 3, 'participant_count': 2}


def wait_abort(controller):
    if controller.abort_worker:
        controller.abort_worker.join(2)
        assert not controller.abort_worker.is_alive()
    return controller.authority.status()


def test_native_handoff_requires_helper_ready_renderer_fence_commit_ack_before_depart(flow):
    controller, _, calls, _, _ = flow
    response = controller.dispatch('update_handoff_commit', prepared(flow))
    assert response['phase'] == 'committed'
    assert calls == ['reserve', 'prepare', 'launch', 'renderer-fence', 'backend-commit', 'COMMIT', 'depart']


def test_binding_change_before_attempt_publication_cancels_exact_private_reservation(flow):
    controller, authority, calls, config, _ = flow
    config['before_reserve_return'] = lambda: authority.update_binding(None)
    result = controller.dispatch('update_handoff_begin', {})
    assert result['aborted'] is True and calls == ['reserve', 'cancel-reservation']
    assert controller.attempt is None


def test_binding_change_just_after_authority_begin_schedules_acknowledged_abort(flow, monkeypatch):
    controller, authority, calls, _, _ = flow
    original = authority.begin
    def begin(*args):
        result = original(*args); authority.invalidate(); return result
    monkeypatch.setattr(authority, 'begin', begin)
    controller.dispatch('update_handoff_begin', {})
    assert wait_abort(controller)['aborted'] is True
    assert calls == ['reserve', 'abort']


@pytest.mark.parametrize('failure', ['fence', 'commit_ack', 'during_commit'])
def test_stale_fence_or_ack_failure_stops_only_owned_helper_and_never_quits(flow, failure):
    controller, _, calls, config, binding = flow
    second = prepared(flow)
    if failure == 'during_commit':
        config[failure] = lambda: binding.__setitem__(0, replace(binding[0], navigation=2))
    else:
        config[failure] = False
    response = controller.dispatch('update_handoff_commit', second)
    status = wait_abort(controller)
    assert response['blocked'] is True and 'depart' not in calls
    assert status['aborted'] is True and status['not_executed'] is True and status['locked'] is False
    assert 'owned-exit' in calls and 'abort' in calls
    if failure != 'commit_ack': assert 'COMMIT' not in calls


def test_abort_without_known_nonexecuted_proof_retains_workspace_lock(flow):
    controller, _, _, config, _ = flow
    second = prepared(flow); config['abort'] = False
    result = controller.abort({'attempt_id': second['attempt_id']})
    assert result['locked'] is True and result['aborted'] is False and result['not_executed'] is False


def test_abort_during_preparation_revokes_confirmation_then_finishes_off_ui_thread(flow):
    controller, _, calls, config, _ = flow
    entered, proceed = threading.Event(), threading.Event()
    def pause(): entered.set(); assert proceed.wait(2)
    config['during_prepare'] = pause
    first = controller.begin({})
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.dispatch('update_handoff_ack',
        {'attempt_id': first['attempt_id'], 'challenge': first['challenge'], 'workspace_revision': 3,
         'participant_count': 2, 'receipts': []})))
    worker.start(); assert entered.wait(1)
    controller.invalidate()
    assert controller.authority.status()['locked'] is True
    proceed.set(); worker.join(2)
    assert wait_abort(controller)['aborted'] is True and 'launch' not in calls and 'depart' not in calls
    assert results[0]['blocked'] is True


def test_stale_abort_worker_cannot_disarm_a_newer_assignment(flow):
    controller, _, calls, _, _ = flow
    first = controller.begin({})
    assert controller.abort({'attempt_id': first['attempt_id']})['aborted']
    second = controller.begin({})
    before = list(calls)
    controller._abort_owned(first['attempt_id'])
    assert calls == before and controller.status({'attempt_id': second['attempt_id']})['phase'] == 'checking'
    controller.abort({'attempt_id': second['attempt_id']})


def test_unknown_wire_fields_never_expose_private_errors_or_construct_grants(flow):
    controller, _, calls, _, _ = flow
    response = controller.dispatch('update_handoff_begin', {'command': 'synthetic-private-command'})
    assert response['blocked'] and response['not_executed'] and calls == []
    assert 'synthetic-private' not in str(response)


@pytest.mark.parametrize('action', ['update_handoff_ack', 'update_handoff_commit'])
def test_stale_renderer_messages_cannot_invalidate_a_newer_native_attempt(flow, action):
    controller, _, calls, _, _ = flow
    first = controller.begin({})
    assert controller.abort({'attempt_id': first['attempt_id']})['aborted']
    second = prepared(flow)
    before = list(calls)
    response = controller.dispatch(action, {'attempt_id': first['attempt_id']})
    assert response['blocked'] and controller.authority.status()['phase'] == 'prepared'
    assert calls == before
    assert controller.dispatch('update_handoff_commit', second)['phase'] == 'committed'
