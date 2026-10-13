"""Bounded real anonymous-pipe fixtures; no installed host or installer runs."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from forge_native_handoff import (HandoffRejected, HelperTransport, NativeAuthority, NativeBinding,
    PipeChannel, _helper_session, matching_peer, peer_frame)


def auth():
    return {'kind': 'AUTH', 'protocol': 1, 'operation_id': 'b' * 32, 'home': 'synthetic-private-home',
        'nonce': 'c' * 64, 'session': 'd' * 64, 'intent_sha256': 'e' * 64,
        'parent_pid': os.getpid(), 'parent_created': 1.0, 'helper_version': '5.1.3', 'helper_source_commit': None}


@pytest.mark.parametrize('raw', [b'{}\n', b'{"kind":"x","kind":"y"}\n', b'{"x":NaN}\n',
    b'{"x":Infinity}\n', b'[]\n', b'\xff\n', b'\n'])
def test_read_accepts_only_strict_bounded_json_objects(raw):
    reader = io.BytesIO(raw)
    channel = PipeChannel(reader, io.BytesIO(), available=lambda stream: len(stream.getvalue()) - stream.tell())
    if raw == b'{}\n':
        assert channel.read(.1) == {}
    else:
        with pytest.raises(HandoffRejected): channel.read(.1)


def test_reads_preserve_separate_frames_and_timeout_without_abandoned_readers():
    raw = b'{"one":1}\n{"two":2}\n'
    reader = io.BytesIO(raw)
    channel = PipeChannel(reader, io.BytesIO(), available=lambda stream: len(stream.getvalue()) - stream.tell())
    assert channel.read(.1) == {'one': 1}
    assert channel.read(.1) == {'two': 2}
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(HandoffRejected): channel.read(.03)
    assert {thread.ident for thread in threading.enumerate()} == before


@pytest.mark.parametrize('field,value', [('pid', True), ('parent_pid', True), ('protocol', True),
    ('nonce', 'f' * 64), ('session', 'f' * 64), ('operation_id', 'f' * 32), ('parent_created', 2.0)])
def test_peer_frames_bind_exact_process_operation_and_session(field, value):
    frame = peer_frame(auth(), 'READY', 123)
    frame[field] = value
    assert matching_peer(frame, auth(), 'READY', 123) is False


def test_commit_admission_runs_at_actual_writer_before_any_bytes_are_written():
    output = io.BytesIO()
    channel = PipeChannel(io.BytesIO(), output)
    with pytest.raises(HandoffRejected):
        channel.write({'kind': 'COMMIT'}, timeout=.1, admit=lambda: False)
    assert output.getvalue() == b''


@pytest.fixture
def child(tmp_path):
    marker = tmp_path / 'child-result.json'
    script = tmp_path / 'pipe_fixture.py'
    root = str(Path(__file__).resolve().parents[1])
    script.write_text("""import json, os, sys
from pathlib import Path
sys.path.insert(0, ROOT)
sys.path.insert(0, SITE)
from forge_native_handoff import PipeChannel, inherited_pipe, _helper_session
if os.name == 'nt':
    sys.stdin = None
    sys.stdout = None
    reader, writer = inherited_pipe(-10, 'rb'), inherited_pipe(-11, 'wb')
else:
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
def preflight(frame):
    if frame.get('kind') != 'AUTH': raise ValueError('Synthetic refusal')
    return object()
def commit(context):
    Path(MARKER).write_text(json.dumps({'committed': True}))
    return {'synthetic_private_result': True}
def abort(context): Path(MARKER).write_text(json.dumps({'aborted': True}))
def restart(context,result): return ('synthetic-verified-restart',)
def launch(command,**kwargs):
    Path(MARKER).write_text(json.dumps({'committed': True, 'restart': command == ['synthetic-verified-restart']}))
sys.exit(_helper_session(PipeChannel(reader,writer),preflight=preflight,commit=commit,
    abort=abort,restart_command=restart,recovery_command=lambda context: None,launch=launch,timeout=2))
""".replace('ROOT', repr(root)).replace('SITE', repr(str(Path(sys.prefix) / 'Lib' / 'site-packages')))
        .replace('MARKER', repr(str(marker))), encoding='utf-8')
    # A venv redirector may spawn another process; use the actual interpreter
    # so the authenticated peer PID is the exact owned Popen process.
    python = str(Path(sys._base_executable).with_name('pythonw.exe')) if os.name == 'nt' else sys.executable
    assert Path(python).is_file()
    processes = []
    def popen(command, **kwargs):
        assert command[1:] == ['--forge-update']
        process = subprocess.Popen([python, str(script)], **kwargs)
        processes.append(process)
        return process
    yield popen, marker
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


def test_real_windowed_inherited_pipes_ready_commit_and_private_restart(child):
    popen, marker = child
    transport = HelperTransport(SimpleNamespace(command=('synthetic-verified-host', '--forge-update')), auth(), popen=popen, timeout=2)
    ready = transport.await_ready()
    transport.commit(ready, admit=lambda: True)
    assert transport.process.wait(timeout=3) == 0
    assert json.loads(marker.read_text()) == {'committed': True, 'restart': True}
    assert transport.stop() is True


def test_real_windowed_abort_never_commits_or_restarts(child):
    popen, marker = child
    transport = HelperTransport(SimpleNamespace(command=('synthetic-verified-host', '--forge-update')), auth(), popen=popen, timeout=2)
    transport.await_ready()
    assert transport.stop() is True
    assert json.loads(marker.read_text()) == {'aborted': True}


def test_stopped_receipt_is_exact_one_use_and_cannot_be_copied_or_minted_from_json(child):
    popen, _ = child
    authority = NativeAuthority(parent_alive=lambda binding: True)
    binding = NativeBinding(object(), 'd' * 64, 'synthetic-main', 1, 'http://127.0.0.1:12345', os.getpid(), 1.0)
    authority.update_binding(binding)
    reservation = object()
    begin = authority.begin(binding, reservation)
    ack = {'attempt_id': begin['attempt_id'], 'challenge': begin['challenge'],
        'workspace_revision': 1, 'participant_count': 1, 'receipts': []}
    prepare = authority.acknowledge(ack)
    authority.consume(prepare, 'prepare', binding, reservation)
    prepared_ack = authority.prepared(prepare)
    launch = authority.confirm({'attempt_id': begin['attempt_id'], 'confirmation': prepared_ack['confirmation'],
        'workspace_revision': 1, 'participant_count': 1})
    authority.consume(launch, 'launch', binding, reservation)
    prepared = SimpleNamespace(reservation=reservation, attempt_id=begin['attempt_id'], operation_id='b' * 32)
    bootstrap = SimpleNamespace(command=('synthetic-verified-host', '--forge-update'), prepared=prepared)
    transport = HelperTransport(bootstrap, auth(), popen=popen, timeout=2)
    authority.register_helper(launch, prepared, transport)
    transport.await_ready()
    with pytest.raises(HandoffRejected): authority.stopped_helper(transport)
    assert transport.stop() is True
    receipt = authority.stopped_helper(transport)
    assert authority.consume_stopped_helper({'pid': transport.process.pid, 'stopped': True}, reservation, prepared.operation_id) is False
    assert authority.consume_stopped_helper(replace(receipt), reservation, prepared.operation_id) is False
    assert authority.consume_stopped_helper(receipt, object(), prepared.operation_id) is False
    assert authority.consume_stopped_helper(receipt, reservation, prepared.operation_id) is True
    assert authority.consume_stopped_helper(receipt, reservation, prepared.operation_id) is False


class MemoryProtocol:
    def __init__(self, control): self.control = control; self.writes = []; self.reads = 0
    def read(self, timeout):
        self.reads += 1
        if self.reads == 1: return auth()
        if isinstance(self.control, Exception): raise self.control
        return self.control
    def write(self, value): self.writes.append(value['kind'])


@pytest.mark.parametrize('control', [HandoffRejected(), {}, peer_frame(auth(), 'ABORT', os.getpid())])
def test_helper_timeout_or_protocol_failure_aborts_preflight_context(control):
    channel = MemoryProtocol(control); calls = []
    outcome = _helper_session(channel, preflight=lambda frame: object(),
        commit=lambda context: calls.append('commit'), abort=lambda context: calls.append('abort'),
        restart_command=lambda context,result: ('ignored',), recovery_command=lambda context: None)
    assert outcome in (0, 1) and calls == ['abort'] and channel.writes == ['READY']


def test_helper_known_nonexecuted_recovery_launches_only_private_reverified_command():
    channel = MemoryProtocol(peer_frame(auth(), 'COMMIT', os.getpid())); launched = []
    def refused(context): raise HandoffRejected('workspace_changed')
    assert _helper_session(channel, preflight=lambda frame: object(), commit=refused,
        abort=lambda context: pytest.fail('Committed context is not a preflight abort'),
        recovery_command=lambda context: ('synthetic-reverified-current-host',),
        launch=lambda command,**kwargs: launched.append(command)) == 1
    assert launched == [['synthetic-reverified-current-host']]


def test_inert_packaged_protocol_uses_exact_os_parent_without_loading_paths(monkeypatch):
    from forge_native_handoff import _protocol_smoke
    import psutil
    frame = auth()
    # Metadata-only fake OS identity; the supplied home is never consulted.
    monkeypatch.setattr(psutil, 'Process', lambda pid=None: SimpleNamespace(
        pid=frame['parent_pid'], ppid=lambda: frame['parent_pid'], create_time=lambda: frame['parent_created']))
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: pytest.fail('No home or intent reads in inert IPC'))
    channel = MemoryProtocol(peer_frame(frame, 'COMMIT', os.getpid()))
    assert _protocol_smoke(channel) == 0 and channel.writes == ['READY', 'COMMITTED']
    monkeypatch.setattr(psutil, 'Process', lambda pid=None: SimpleNamespace(
        pid=frame['parent_pid'], ppid=lambda: frame['parent_pid'] + 1, create_time=lambda: frame['parent_created']))
    with pytest.raises(HandoffRejected):
        _protocol_smoke(MemoryProtocol(peer_frame(frame, 'COMMIT', os.getpid())))


@pytest.mark.parametrize('arguments', [[], ['--forge-update-protocol-smoke'],
    ['--smoke-test', '--forge-update-protocol-smoke', '--forge-home', 'synthetic-private']])
def test_inert_smoke_cannot_be_used_as_a_bare_or_argument_based_helper(monkeypatch, arguments):
    from forge_native_handoff import protocol_smoke_entry
    monkeypatch.setattr(sys, 'argv', ['Forge.exe', *arguments])
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('FORGE_DATA_DIR', 'synthetic-disposable-no-read')
    monkeypatch.setattr('forge_native_handoff.inherited_pipe', lambda *args: pytest.fail('No invalid smoke transport'))
    with pytest.raises(SystemExit, match='Disposable native protocol smoke'):
        protocol_smoke_entry()
