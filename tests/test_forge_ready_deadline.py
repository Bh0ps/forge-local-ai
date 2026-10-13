"""Inert READY deadlines use the owned authority expiry; no helper is launched."""
from dataclasses import replace
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import forge_native_handoff as native
from forge_update_handoff import NativeBootstrap, NativePrepared, NativeReservation, native_bootstrap_frame


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += 1


class OwnedProcess:
    pid = 12345
    def __init__(self, raw):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO(raw)
        self.exit = None
        self.calls = []
    def poll(self): return self.exit
    def wait(self, timeout): self.calls.append(('wait', timeout)); self.exit = 0; return 0
    def terminate(self): self.calls.append(('terminate',)); self.exit = 1


@pytest.fixture
def flow(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(native.time, 'sleep', clock.sleep)
    binding = native.NativeBinding(object(), 'a' * 64, 'synthetic-main', 1,
        'http://127.0.0.1:12345', 10, 1.0)
    authority = native.NativeAuthority(clock=clock, parent_alive=lambda value: True)
    authority.update_binding(binding)
    reservation = NativeReservation('b' * 32, 'apply', binding, 'c' * 64, clock() + 120)
    begin = authority.begin(binding, reservation)
    ack = {'attempt_id': begin['attempt_id'], 'challenge': begin['challenge'],
        'workspace_revision': 1, 'participant_count': 1, 'receipts': []}
    grant = authority.acknowledge(ack)
    authority.consume(grant, 'prepare', binding, reservation)
    prepared_ack = authority.prepared(grant)
    launch = authority.confirm({'attempt_id': begin['attempt_id'],
        'confirmation': prepared_ack['confirmation'], 'workspace_revision': 1, 'participant_count': 1})
    authority.consume(launch, 'launch', binding, reservation)
    prepared = NativePrepared(reservation, 'd' * 32, begin['attempt_id'], (), 1, 1, 'e' * 64)
    bootstrap = NativeBootstrap(prepared, ('synthetic-owned-Forge.exe', '--forge-update'),
        Path('synthetic-disposable-home'), 'f' * 64, binding.session, 1, '5.1.3', None,
        binding.parent_pid, binding.parent_created, prepared.intent_sha256)
    auth = native_bootstrap_frame(bootstrap)
    ready = json.dumps(native.peer_frame(auth, 'READY', OwnedProcess.pid)).encode() + b'\n'
    committed = json.dumps(native.peer_frame(auth, 'COMMITTED', OwnedProcess.pid)).encode() + b'\n'
    value = SimpleNamespace(clock=clock, binding=binding, authority=authority, launch=launch,
        bootstrap=bootstrap, auth=auth, ready=ready, committed=committed, starts=[])
    def transport(*, ready_at=None, committed_at=None, **kwargs):
        process = OwnedProcess(ready + committed)
        def popen(command, **options):
            assert command == list(bootstrap.command)
            value.starts.append(command)
            return process
        result = native.HelperTransport(bootstrap, auth, popen=popen, clock=clock, **kwargs)
        def available(reader):
            if reader.tell() < len(ready):
                return len(ready) - reader.tell() if ready_at is not None and clock() >= ready_at else 0
            return len(committed) if committed_at is not None and clock() >= committed_at else 0
        result.channel.available = available
        authority.register_helper(launch, prepared, result)
        return result
    value.transport = transport
    return value


def assert_no_ready(transport, reason):
    with pytest.raises(native.HandoffRejected) as rejected:
        transport.await_ready()
    assert rejected.value.reason == reason
    assert transport.ready is None and transport.commit_sent is False and transport.cancel.is_set()


def test_default_ready_accepts_slow_verified_peer_within_separate_budget(flow):
    # Simulated preparation and parent preflight already consumed 52 seconds.
    flow.clock.now += 52
    transport = flow.transport(ready_at=flow.clock() + 34)
    ready = transport.await_ready()
    flow.authority.ready(flow.launch, ready)
    assert flow.clock() == 186 and flow.authority.status()['phase'] == 'ready'
    assert transport.ready is ready and not transport.cancel.is_set() and not transport.commit_sent
    # AUTH remains the exact existing private wire frame, without a new expiry.
    assert json.loads(transport.process.stdin.getvalue()) == flow.auth


@pytest.mark.parametrize('timeout', [2, 15])
def test_explicit_protocol_timeout_keeps_legacy_ready_budget(flow, timeout):
    transport = flow.transport(ready_at=flow.clock() + timeout + 1, timeout=timeout)
    before = flow.clock()
    assert_no_ready(transport, 'helper_failed')
    assert flow.clock() == before + timeout


def test_explicit_ready_budget_does_not_extend_committed_or_write_timeout(flow, monkeypatch):
    writes = []
    write = native.PipeChannel.write
    def record_write(channel, payload, timeout=15, **kwargs):
        writes.append((payload['kind'], timeout))
        return write(channel, payload, timeout=timeout, **kwargs)
    monkeypatch.setattr(native.PipeChannel, 'write', record_write)
    transport = flow.transport(ready_at=flow.clock() + 40, committed_at=flow.clock() + 56, ready_timeout=45)
    ready = transport.await_ready()
    before = flow.clock()
    with pytest.raises(native.HandoffRejected) as rejected:
        transport.commit(ready, admit=lambda: True)
    assert rejected.value.reason == 'helper_failed' and flow.clock() == before + 15
    assert writes == [('AUTH', 15), ('COMMIT', 15)] and transport.commit_sent is True
    assert not transport.process.calls


def test_ready_budget_is_clipped_to_remaining_exact_reservation_expiry(flow):
    flow.clock.now += 111
    transport = flow.transport(ready_at=flow.clock() + 10)
    assert_no_ready(transport, 'confirmation_expired')
    assert flow.clock() == flow.bootstrap.prepared.reservation.expires
    assert flow.authority.status()['reason'] == 'confirmation_expired'
    assert flow.authority.status()['phase'] == 'failed'


def test_expired_reservation_is_rejected_before_any_owned_process_start(flow):
    flow.clock.now += 120
    with pytest.raises(native.HandoffRejected) as rejected:
        flow.transport(ready_at=flow.clock())
    assert rejected.value.reason == 'confirmation_expired' and flow.starts == []


def test_expiry_between_auth_and_ready_wait_refuses_without_reading_buffered_reply(flow):
    transport = flow.transport(ready_at=flow.clock())
    transport.channel.pending.extend(flow.ready)
    flow.clock.now += 120
    assert_no_ready(transport, 'confirmation_expired')
    assert transport.channel.pending == flow.ready


@pytest.mark.parametrize('change', ['expiry', 'cancel'])
def test_authority_and_cancellation_are_rechecked_after_ready_read_before_publication(flow, monkeypatch, change):
    transport = flow.transport(ready_at=flow.clock())
    read = transport.channel._read_ready
    def read_then_change(*args, **kwargs):
        result = read(*args, **kwargs)
        if change == 'expiry': flow.clock.now = flow.bootstrap.prepared.reservation.expires
        else: transport.cancel.set()
        return result
    monkeypatch.setattr(transport.channel, '_read_ready', read_then_change)
    assert_no_ready(transport, 'confirmation_expired' if change == 'expiry' else 'helper_failed')


def test_reply_read_at_ready_deadline_is_not_accepted_from_pending_buffer(flow):
    transport = flow.transport()
    deadline = flow.clock() + 45
    def available(reader):
        flow.clock.now = deadline
        return len(flow.ready)
    transport.channel.available = available
    assert_no_ready(transport, 'helper_failed')
    assert transport.channel.pending == flow.ready


def test_timeout_permanently_revokes_late_ready_and_commit_on_same_owned_transport(flow):
    transport = flow.transport(ready_at=flow.clock() + 46)
    assert_no_ready(transport, 'helper_failed')
    flow.clock.now += 1
    assert_no_ready(transport, 'helper_failed')
    with pytest.raises(native.HandoffRejected): transport.commit(None, admit=lambda: True)
    assert json.loads(transport.process.stdin.getvalue()) == flow.auth


@pytest.mark.parametrize('when', ['before', 'waiting'])
def test_cancellation_refuses_ready_without_consuming_future_peer_frame(flow, when):
    transport = flow.transport(ready_at=flow.clock() + 1)
    if when == 'before': transport.cancel.set()
    else:
        def cancel(reader): transport.cancel.set(); return 0
        transport.channel.available = cancel
    before = flow.clock()
    assert_no_ready(transport, 'helper_failed')
    assert flow.clock() <= before + 1 and transport.process.stdout.tell() == 0


@pytest.mark.parametrize('delta', [0, -1, 46, float('inf'), float('nan')])
def test_private_ready_read_has_hard_finite_45_second_bound(flow, delta):
    reader = io.BytesIO(flow.ready)
    channel = native.PipeChannel(reader, io.BytesIO(), clock=flow.clock, available=lambda stream: len(flow.ready))
    with pytest.raises(native.HandoffRejected): channel._read_ready(flow.clock() + delta)
    assert reader.tell() == 0


def test_ordinary_protocol_read_keeps_30_second_maximum(flow):
    channel = native.PipeChannel(io.BytesIO(flow.ready), io.BytesIO(), clock=flow.clock)
    with pytest.raises(native.HandoffRejected): channel.read(31)
    assert channel.reader.tell() == 0


@pytest.mark.parametrize('field,value', [('timeout', True), ('timeout', 31), ('ready_timeout', True),
    ('ready_timeout', 46), ('ready_timeout', float('nan'))])
def test_invalid_deadline_configuration_never_starts_owned_helper(flow, field, value):
    with pytest.raises(native.HandoffRejected): flow.transport(**{field: value})
    assert flow.starts == []


@pytest.mark.parametrize('expires', [True, float('inf'), float('nan')])
def test_unknown_private_expiry_never_starts_owned_helper(flow, expires):
    prepared = replace(flow.bootstrap.prepared, reservation=replace(flow.bootstrap.prepared.reservation, expires=expires))
    bootstrap = replace(flow.bootstrap, prepared=prepared)
    with pytest.raises(native.HandoffRejected):
        native.HelperTransport(bootstrap, flow.auth, clock=flow.clock,
            popen=lambda *args, **kwargs: pytest.fail('Invalid private expiry cannot start a helper'))


def test_default_long_ready_budget_requires_owned_bootstrap_not_wire_expiry(flow):
    fake = SimpleNamespace(command=flow.bootstrap.command, expires_at=flow.clock() + 120)
    with pytest.raises(native.HandoffRejected):
        native.HelperTransport(fake, {**flow.auth, 'expires_at': flow.clock() + 120}, clock=flow.clock,
            popen=lambda *args, **kwargs: pytest.fail('Wire metadata cannot supply READY authority'))


def test_owned_stop_interrupts_default_ready_wait_promptly_without_real_process():
    clock = time.monotonic
    reservation = NativeReservation('b' * 32, 'apply', object(), 'c' * 64, clock() + 120)
    prepared = NativePrepared(reservation, 'd' * 32, 'e' * 32, (), 1, 1, 'f' * 64)
    bootstrap = NativeBootstrap(prepared, ('synthetic-owned-Forge.exe', '--forge-update'), Path('synthetic-home'),
        'a' * 64, 'b' * 64, 1, '5.1.3', None, 10, 1.0, prepared.intent_sha256)
    process = OwnedProcess(b'')
    transport = native.HelperTransport(bootstrap, native_bootstrap_frame(bootstrap),
        popen=lambda *args, **kwargs: process)
    entered = threading.Event()
    transport.channel.available = lambda stream: entered.set() or 0
    failures = []
    def wait():
        try: transport.await_ready()
        except native.HandoffRejected as error: failures.append(error.reason)
    worker = threading.Thread(target=wait)
    worker.start()
    try:
        assert entered.wait(1)
        assert transport.stop() is True
        worker.join(.5)
        assert not worker.is_alive() and failures == ['helper_failed']
        assert transport.ready is None and transport.commit_sent is False
        assert process.calls == [('wait', 1)]
    finally:
        transport.cancel.set()
        worker.join(1)
