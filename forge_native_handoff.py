"""Private native update authority and bounded anonymous helper transport.

Browser/coordinator requests cannot construct these grants. Renderer messages
contain only attempt IDs, revisions, counts and coordinator-issued receipts.
The shipped capability stays closed until the packaged protocol is validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Any


HANDOFF_ENABLED = False
UNAVAILABLE = 'In-app installation is unavailable in this version. Save your work and use the manual installer.'
MAX_PARTICIPANTS = 512
MAX_RECEIPTS = 1024
MAX_FRAME = 65536
_DEFAULT_PROTOCOL_TIMEOUT = object()
TOKEN = re.compile(r'[0-9a-f]{64}\Z')
ATTEMPT = re.compile(r'[0-9a-f]{32}\Z')
REASONS = frozenset({'handoff_unavailable', 'workspace_loading', 'workspace_changed',
    'confirmation_expired', 'native_input', 'active_work', 'paired_editor', 'draft_conflict',
    'draft_save_failed', 'preparation_failed', 'helper_failed', 'protocol_error'})


class HandoffRejected(ValueError):
    def __init__(self, reason='protocol_error'):
        self.reason = reason if reason in REASONS else 'protocol_error'
        super().__init__('Restart did not start. Forge is still open.')


@dataclass(frozen=True)
class NativeBinding:
    host: object = field(repr=False)
    session: str = field(repr=False)
    window_id: str = field(repr=False)
    navigation: int
    origin: str = field(repr=False)
    parent_pid: int
    parent_created: float


@dataclass(frozen=True, repr=False)
class NativeGrant:
    """An exact runtime object, never a JSON token or persisted journal flag."""
    _issuer: object
    attempt_id: str
    stage: str
    binding: NativeBinding
    reservation: object
    expires: float


@dataclass(repr=False)
class _Attempt:
    identifier: str
    challenge: str
    binding: NativeBinding
    reservation: object
    expires: float
    phase: str = 'checking'
    revision: int | None = None
    count: int | None = None
    receipts: tuple[str, ...] = ()
    confirmation: str | None = None
    reason: str | None = None
    abort_confirmed: bool = False
    ready: object | None = None
    issued: dict[str, NativeGrant] = field(default_factory=dict)
    consumed: set[str] = field(default_factory=set)
    helper: object | None = None
    operation_id: str | None = None
    stopped_receipt: object | None = None
    stopped_consumed: bool = False


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _matching(value, expected, pattern):
    return isinstance(value, str) and bool(pattern.fullmatch(value)) and hmac.compare_digest(value, expected)


class NativeAuthority:
    """No UI, disk, backend callbacks or waits occur while this lock is held."""
    def __init__(self, *, clock=time.monotonic, lifetime=120, parent_alive=None):
        if type(lifetime) not in (int, float) or not math.isfinite(lifetime) or not 1 <= lifetime <= 120:
            raise ValueError('Invalid native authority lifetime.')
        self.clock, self.lifetime = clock, float(lifetime)
        self.lock = threading.RLock()
        self._issuer = object()
        self._binding = None
        self._attempt = None
        self._parent_alive = parent_alive or _same_parent

    def update_binding(self, binding: NativeBinding | None):
        with self.lock:
            if binding != self._binding and self._attempt and not self._attempt.abort_confirmed:
                self._fail('workspace_changed')
            self._binding = binding

    def _fail(self, reason):
        attempt = self._attempt
        if attempt:
            attempt.reason = reason if reason in REASONS else 'protocol_error'
            attempt.phase = 'failed'
            attempt.confirmation = None
            attempt.issued.clear()

    def invalidate(self, reason='workspace_changed'):
        with self.lock:
            self._fail(reason)

    def binding_valid(self, binding):
        with self.lock:
            return isinstance(binding, NativeBinding) and binding == self._binding

    def receipt_attempt(self, attempt_id, reservation):
        with self.lock:
            try:
                attempt = self._current(attempt_id)
            except HandoffRejected:
                return False
            return attempt.phase == 'checking' and attempt.reservation is reservation

    def readiness(self, grant):
        with self.lock:
            if not isinstance(grant, NativeGrant) or grant._issuer is not self._issuer:
                raise HandoffRejected()
            attempt = self._current(grant.attempt_id)
            if attempt.issued.get(grant.stage) is not grant or attempt.revision is None or attempt.count is None:
                raise HandoffRejected()
            return (attempt.revision, attempt.count, attempt.receipts)

    def _current(self, identifier=None):
        attempt = self._attempt
        if attempt is None or identifier is not None and identifier != attempt.identifier:
            raise HandoffRejected()
        if self.clock() >= attempt.expires:
            self._fail('confirmation_expired')
            raise HandoffRejected('confirmation_expired')
        if attempt.binding != self._binding or self._binding is None:
            self._fail('workspace_changed')
            raise HandoffRejected('workspace_changed')
        if attempt.phase == 'failed' or attempt.abort_confirmed:
            raise HandoffRejected(attempt.reason or 'workspace_changed')
        return attempt

    def begin(self, binding: NativeBinding, reservation: object):
        with self.lock:
            if binding is None or binding != self._binding or reservation is None:
                raise HandoffRejected('workspace_loading')
            if self._attempt and not self._attempt.abort_confirmed:
                raise HandoffRejected('active_work')
            attempt = _Attempt(secrets.token_hex(16), secrets.token_hex(32), binding,
                reservation, self.clock() + self.lifetime)
            self._attempt = attempt
            return {'attempt_id': attempt.identifier, 'challenge': attempt.challenge,
                'phase': 'checking', 'expires_in': self.lifetime}

    def _grant(self, attempt, stage):
        if stage in attempt.issued or stage in attempt.consumed:
            raise HandoffRejected()
        grant = NativeGrant(self._issuer, attempt.identifier, stage,
            attempt.binding, attempt.reservation, attempt.expires)
        attempt.issued[stage] = grant
        return grant

    def acknowledge(self, data):
        if not isinstance(data, dict) or set(data) != {'attempt_id', 'challenge',
                'workspace_revision', 'participant_count', 'receipts'}:
            raise HandoffRejected()
        revision, count, receipts = data['workspace_revision'], data['participant_count'], data['receipts']
        if not _integer(revision, 2**53 - 1) or not _integer(count, MAX_PARTICIPANTS) or count < 1:
            raise HandoffRejected()
        if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS or any(
                not isinstance(value, str) or not TOKEN.fullmatch(value) for value in receipts):
            raise HandoffRejected()
        if len(set(receipts)) != len(receipts):
            raise HandoffRejected()
        with self.lock:
            attempt = self._current(data['attempt_id'])
            if attempt.phase != 'checking' or not _matching(data['challenge'], attempt.challenge, TOKEN):
                raise HandoffRejected()
            attempt.revision, attempt.count, attempt.receipts = revision, count, tuple(receipts)
            attempt.challenge = ''
            attempt.phase = 'locked'
            return self._grant(attempt, 'prepare')

    def consume(self, grant, stage, binding, reservation):
        with self.lock:
            if not isinstance(grant, NativeGrant) or grant._issuer is not self._issuer:
                raise HandoffRejected()
            attempt = self._current(grant.attempt_id)
            phases = {'prepare': 'locked', 'launch': 'prepared', 'commit': 'ready'}
            if (stage not in phases or grant.stage != stage or attempt.phase != phases[stage]
                    or attempt.issued.get(stage) is not grant or stage in attempt.consumed
                    or grant.binding != binding or binding != self._binding
                    or grant.reservation is not reservation or attempt.reservation is not reservation):
                raise HandoffRejected()
            attempt.consumed.add(stage)
            attempt.phase = {'prepare': 'preparing', 'launch': 'launching', 'commit': 'committing'}[stage]
            return True

    def prepared(self, grant):
        with self.lock:
            attempt = self._current(grant.attempt_id)
            if attempt.issued.get('prepare') is not grant or 'prepare' not in attempt.consumed or attempt.phase != 'preparing':
                raise HandoffRejected()
            attempt.phase = 'prepared'
            attempt.confirmation = secrets.token_hex(32)
            return {'attempt_id': attempt.identifier, 'phase': 'prepared', 'confirmation': attempt.confirmation}

    def confirm(self, data):
        if not isinstance(data, dict) or set(data) != {'attempt_id', 'confirmation', 'workspace_revision', 'participant_count'}:
            raise HandoffRejected()
        with self.lock:
            attempt = self._current(data['attempt_id'])
            if (attempt.phase != 'prepared' or not _matching(data['confirmation'], attempt.confirmation or '', TOKEN)
                    or type(data['workspace_revision']) is not int or data['workspace_revision'] != attempt.revision
                    or type(data['participant_count']) is not int or data['participant_count'] != attempt.count):
                raise HandoffRejected('workspace_changed')
            attempt.confirmation = None
            return self._grant(attempt, 'launch')

    def ready(self, grant, ready):
        with self.lock:
            attempt = self._current(grant.attempt_id)
            if attempt.issued.get('launch') is not grant or 'launch' not in attempt.consumed or attempt.phase != 'launching' or ready is None:
                raise HandoffRejected()
            attempt.ready, attempt.phase = ready, 'ready'

    def commit_grant(self, identifier, ready, fence):
        with self.lock:
            attempt = self._current(identifier)
            if (attempt.phase != 'ready' or attempt.ready is not ready or not isinstance(fence, dict)
                    or set(fence) != {'ok', 'workspace_revision', 'participant_count', 'phase'}
                    or fence['ok'] is not True or fence['phase'] not in ('locked', 'prepared')
                    or type(fence['workspace_revision']) is not int or fence['workspace_revision'] != attempt.revision
                    or type(fence['participant_count']) is not int or fence['participant_count'] != attempt.count):
                self._fail('workspace_changed')
                raise HandoffRejected('workspace_changed')
            return self._grant(attempt, 'commit')

    def committed(self, grant):
        with self.lock:
            attempt = self._current(grant.attempt_id)
            if attempt.issued.get('commit') is not grant or 'commit' not in attempt.consumed or attempt.phase != 'committing':
                raise HandoffRejected()
            attempt.phase = 'committed'

    def register_helper(self, grant, prepared, transport):
        with self.lock:
            attempt = self._current(grant.attempt_id)
            if (attempt.issued.get('launch') is not grant or 'launch' not in attempt.consumed
                    or attempt.phase != 'launching' or type(transport) is not HelperTransport
                    or prepared.reservation is not attempt.reservation
                    or prepared.attempt_id != attempt.identifier
                    or not ATTEMPT.fullmatch(prepared.operation_id)
                    or transport.bootstrap.prepared is not prepared or attempt.helper is not None):
                raise HandoffRejected()
            attempt.helper, attempt.operation_id = transport, prepared.operation_id

    def pipe_admission(self, grant, transport):
        """Final current-binding fence at the transport write admission point."""
        with self.lock:
            attempt = self._current(grant.attempt_id)
            if (attempt.phase != 'committed' or attempt.issued.get('commit') is not grant
                    or 'commit' not in attempt.consumed or attempt.helper is not transport):
                raise HandoffRejected('workspace_changed')
            return True

    def stopped_helper(self, transport):
        # Process checks are outside the authority lock. Only the exact owned
        # transport registered after verified launch can provide this proof.
        with self.lock:
            attempt = self._attempt
            if attempt is None or attempt.helper is not transport or type(transport) is not HelperTransport:
                raise HandoffRejected('helper_failed')
            binding, operation, reservation = attempt.binding, attempt.operation_id, attempt.reservation
        if (transport.process is None or transport.process.poll() is None
                or self._parent_alive(binding) is not True):
            raise HandoffRejected('helper_failed')
        with self.lock:
            if self._attempt is not attempt or attempt.helper is not transport or attempt.stopped_receipt is not None:
                raise HandoffRejected('helper_failed')
            receipt = _StoppedHelper(self._issuer, transport, reservation, operation, binding)
            attempt.stopped_receipt = receipt
            return receipt

    def consume_stopped_helper(self, receipt, reservation, operation_id):
        with self.lock:
            attempt = self._attempt
            if (attempt is None or type(receipt) is not _StoppedHelper or receipt._issuer is not self._issuer
                    or attempt.stopped_receipt is not receipt or attempt.stopped_consumed
                    or receipt.transport is not attempt.helper or receipt.reservation is not reservation
                    or reservation is not attempt.reservation or operation_id != attempt.operation_id
                    or receipt.operation_id != operation_id or receipt.binding != attempt.binding):
                return False
            attempt.stopped_consumed = True
            return True

    def acknowledge_abort(self, identifier, *, stopped_helper=None):
        with self.lock:
            attempt = self._attempt
            if (attempt and 'commit' in attempt.consumed and not attempt.stopped_consumed
                    and stopped_helper is not None):
                self.consume_stopped_helper(stopped_helper, attempt.reservation, attempt.operation_id)
            if (not attempt or identifier != attempt.identifier
                    or 'commit' in attempt.consumed and not attempt.stopped_consumed):
                raise HandoffRejected()
            self._fail(attempt.reason or 'workspace_changed')
            attempt.abort_confirmed = True
            return {'attempt_id': identifier, 'phase': 'failed', 'aborted': True, 'not_executed': True,
                'reason': attempt.reason}

    def status(self, identifier=None):
        with self.lock:
            attempt = self._attempt
            if attempt is None:
                return {'phase': 'idle', 'locked': False}
            if identifier is not None and identifier != attempt.identifier:
                raise HandoffRejected()
            if self.clock() >= attempt.expires and attempt.phase != 'committed' and not attempt.abort_confirmed:
                self._fail('confirmation_expired')
            return {'attempt_id': attempt.identifier, 'phase': attempt.phase,
                'locked': not attempt.abort_confirmed, 'reason': attempt.reason,
                'aborted': attempt.abort_confirmed, 'not_executed': attempt.abort_confirmed}

    def fence_request(self, identifier):
        with self.lock:
            attempt = self._current(identifier)
            return {'attempt_id': identifier, 'workspace_revision': attempt.revision, 'participant_count': attempt.count}

    def abort_requires_stopped(self, identifier):
        with self.lock:
            attempt = self._attempt
            if attempt is None or attempt.identifier != identifier:
                raise HandoffRejected()
            return 'commit' in attempt.consumed


def unavailable():
    return {'blocked': True, 'phase': 'failed', 'reason': 'handoff_unavailable',
        'aborted': True, 'not_executed': True}


def _same_parent(binding):
    try:
        import psutil
        return binding.parent_pid == os.getpid() and psutil.Process(os.getpid()).create_time() == binding.parent_created
    except Exception:
        return False


@dataclass(frozen=True, repr=False)
class _StoppedHelper:
    _issuer: object
    transport: object
    reservation: object
    operation_id: str
    binding: NativeBinding


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffRejected()
        result[key] = value
    return result


def _invalid_constant(value):
    raise HandoffRejected()


def pipe_available(stream):
    """Peek before reading; never leave an abandoned blocking reader thread."""
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        available = wintypes.DWORD()
        handle = msvcrt.get_osfhandle(stream.fileno())
        if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            raise HandoffRejected('helper_failed')
        return available.value
    import selectors
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        return 4096 if selector.select(0) else 0


class PipeChannel:
    def __init__(self, reader, writer, *, available=pipe_available, clock=time.monotonic):
        self.reader, self.writer, self.available, self.clock = reader, writer, available, clock
        self.pending = bytearray()
        self.read_lock, self.write_lock = threading.Lock(), threading.Lock()
        self.writing = threading.Event()

    def write(self, payload, timeout=15, *, admit=None):
        try:
            raw = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
        except Exception:
            raise HandoffRejected() from None
        if not isinstance(payload, dict) or len(raw) > MAX_FRAME:
            raise HandoffRejected()
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise HandoffRejected()
        completed, result = threading.Event(), {}
        if not self.write_lock.acquire(timeout=timeout):
            raise HandoffRejected('helper_failed')
        self.writing.set()
        def write():
            try:
                if admit is not None and admit() is not True:
                    raise HandoffRejected('workspace_changed')
                count = self.writer.write(raw)
                if count != len(raw): raise HandoffRejected('helper_failed')
                self.writer.flush()
                result['ok'] = True
            except Exception:
                result['ok'] = False
            finally:
                self.writing.clear()
                self.write_lock.release()
                completed.set()
        threading.Thread(target=write, name='Forge-update-pipe-write', daemon=True).start()
        if not completed.wait(timeout) or result.get('ok') is not True:
            # The transport closes the exact owned child on timeout, which
            # breaks the anonymous pipe and releases a blocked WriteFile.
            raise HandoffRejected('helper_failed')

    def read(self, timeout=15, *, cancel=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise HandoffRejected()
        return self._read_until(self.clock() + timeout, cancel=cancel)

    def _read_ready(self, deadline, *, cancel=None):
        # Only retained-helper verification gets a longer read budget. AUTH,
        # control and COMMITTED reads retain their existing protocol bounds.
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not 0 < deadline - self.clock() <= 45):
            raise HandoffRejected('helper_failed')
        return self._read_until(deadline, cancel=cancel)

    def _read_until(self, deadline, *, cancel=None):
        with self.read_lock:
            while True:
                if cancel is not None and cancel.is_set():
                    raise HandoffRejected('helper_failed')
                if self.clock() >= deadline:
                    raise HandoffRejected('helper_failed')
                newline = self.pending.find(b'\n')
                if newline >= 0:
                    raw = bytes(self.pending[:newline])
                    del self.pending[:newline + 1]
                    if not raw or len(raw) >= MAX_FRAME:
                        raise HandoffRejected()
                    try:
                        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant)
                    except Exception:
                        raise HandoffRejected() from None
                    if not isinstance(value, dict):
                        raise HandoffRejected()
                    return value
                if len(self.pending) >= MAX_FRAME:
                    raise HandoffRejected('helper_failed')
                try:
                    available = self.available(self.reader)
                    if available:
                        raw = self.reader.read(min(available, 4096, MAX_FRAME - len(self.pending)))
                        if not raw:
                            raise HandoffRejected('helper_failed')
                        self.pending.extend(raw)
                        continue
                except HandoffRejected:
                    raise
                except Exception:
                    raise HandoffRejected('helper_failed') from None
                time.sleep(.01)


def inherited_pipe(identifier, mode):
    """Recover inherited Win32 handles even in a PyInstaller windowed process."""
    if os.name != 'nt' or identifier not in (-10, -11) or mode not in ('rb', 'wb'):
        raise HandoffRejected('helper_failed')
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    source = kernel.GetStdHandle(identifier & 0xffffffff)
    if not source or source == ctypes.c_void_p(-1).value or kernel.GetFileType(source) != 3:
        raise HandoffRejected('helper_failed')
    current, duplicated = kernel.GetCurrentProcess(), wintypes.HANDLE()
    if not kernel.DuplicateHandle(current, source, current, ctypes.byref(duplicated), 0, False, 2):
        raise HandoffRejected('helper_failed')
    try:
        descriptor = msvcrt.open_osfhandle(duplicated.value, os.O_BINARY | (os.O_RDONLY if mode == 'rb' else os.O_WRONLY))
    except Exception:
        kernel.CloseHandle(duplicated)
        raise HandoffRejected('helper_failed') from None
    return os.fdopen(descriptor, mode, buffering=0)


PEER_FIELDS = frozenset({'kind', 'protocol', 'operation_id', 'nonce', 'session',
    'parent_pid', 'parent_created', 'pid'})


def peer_frame(auth, kind, pid):
    return {'kind': kind, 'protocol': auth['protocol'], 'operation_id': auth['operation_id'],
        'nonce': auth['nonce'], 'session': auth['session'], 'parent_pid': auth['parent_pid'],
        'parent_created': auth['parent_created'], 'pid': pid}


def matching_peer(value, auth, kind, pid):
    if (not isinstance(value, dict) or set(value) != PEER_FIELDS or value['kind'] != kind
            or type(value['protocol']) is not int or value['protocol'] != 1
            or type(value['pid']) is not int or value['pid'] != pid
            or type(value['parent_pid']) is not int or value['parent_pid'] != auth['parent_pid']
            or type(value['parent_created']) not in (int, float)
            or value['parent_created'] != auth['parent_created']):
        return False
    return (all(_matching(value[key], auth[key], TOKEN) for key in ('nonce', 'session'))
        and _matching(value['operation_id'], auth['operation_id'], ATTEMPT))


@dataclass(frozen=True, repr=False)
class NativeHelperReady:
    operation_id: str
    session: str
    nonce: str
    pid: int
    transport: object


class HelperTransport:
    """Only the verified owned helper process is stopped on failed handoff."""
    def __init__(self, bootstrap, auth, *, popen=subprocess.Popen,
                 timeout=_DEFAULT_PROTOCOL_TIMEOUT, ready_timeout=None, clock=time.monotonic):
        custom_timeout = timeout is not _DEFAULT_PROTOCOL_TIMEOUT
        self.timeout = timeout if custom_timeout else 15
        # Explicit legacy timeout callers keep the same budget for both reads.
        self.ready_timeout = (self.timeout if custom_timeout else 45) if ready_timeout is None else ready_timeout
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 30
                or type(self.ready_timeout) not in (int, float) or not math.isfinite(self.ready_timeout)
                or not 0 < self.ready_timeout <= 45):
            raise HandoffRejected()
        self.bootstrap, self.auth, self.clock = bootstrap, dict(auth), clock
        from forge_update_handoff import NativeBootstrap, NativePrepared, NativeReservation
        if (type(bootstrap) is NativeBootstrap and type(bootstrap.prepared) is NativePrepared
                and type(bootstrap.prepared.reservation) is NativeReservation):
            self.ready_expires = bootstrap.prepared.reservation.expires
            if type(self.ready_expires) not in (int, float) or not math.isfinite(self.ready_expires):
                raise HandoffRejected()
            if self.clock() >= self.ready_expires:
                raise HandoffRejected('confirmation_expired')
        elif custom_timeout and self.ready_timeout <= 30:
            # Short inert transport fixtures have no native reservation. The
            # shipped default always requires the private owned expiry.
            self.ready_expires = None
        else:
            raise HandoffRejected()
        self.cancel = threading.Event()
        self.ready = None
        self.commit_sent = False
        self.process = None
        self.channel = None
        command = bootstrap.command
        if (not isinstance(command, tuple) or len(command) != 2 or command[1] != '--forge-update'
                or not isinstance(command[0], str) or not command[0]):
            raise HandoffRejected()
        try:
            self.process = popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.channel = PipeChannel(self.process.stdout, self.process.stdin, clock=self.clock)
            self.channel.write(self.auth)
        except Exception:
            self.stop()
            raise HandoffRejected('helper_failed') from None

    def await_ready(self):
        clock = getattr(self, 'clock', time.monotonic)
        expires = getattr(self, 'ready_expires', None)
        deadline = clock() + getattr(self, 'ready_timeout', self.timeout)
        if expires is not None:
            deadline = min(deadline, expires)
        try:
            if self.cancel.is_set():
                raise HandoffRejected('helper_failed')
            if expires is not None and clock() >= expires:
                raise HandoffRejected('confirmation_expired')
            if deadline - clock() > 30:
                value = self.channel._read_ready(deadline, cancel=self.cancel)
            else:
                value = self.channel.read(deadline - clock(), cancel=self.cancel)
            # Buffered or delayed replies never extend this attempt's authority.
            if self.cancel.is_set():
                raise HandoffRejected('helper_failed')
            if expires is not None and clock() >= expires:
                raise HandoffRejected('confirmation_expired')
            if clock() >= deadline or not matching_peer(value, self.auth, 'READY', self.process.pid):
                raise HandoffRejected('helper_failed')
            self.ready = NativeHelperReady(self.auth['operation_id'], self.auth['session'],
                self.auth['nonce'], self.process.pid, self)
            return self.ready
        except Exception:
            self.cancel.set()
            if expires is not None and clock() >= expires:
                raise HandoffRejected('confirmation_expired') from None
            raise

    def commit(self, ready, *, admit=None):
        if ready is not self.ready or self.cancel.is_set() or self.commit_sent:
            raise HandoffRejected()
        # A failed/partial write is conservatively committed until owned exit is
        # confirmed. The coordinator must consume a stopped-helper receipt.
        self.commit_sent = True
        self.channel.write(peer_frame(self.auth, 'COMMIT', self.process.pid), admit=admit)
        value = self.channel.read(self.timeout, cancel=self.cancel)
        if not matching_peer(value, self.auth, 'COMMITTED', self.process.pid):
            raise HandoffRejected('helper_failed')

    def stop(self):
        self.cancel.set()
        process = self.process
        if process is None:
            return True
        if process.poll() is None:
            try:
                if self.channel and not self.channel.writing.is_set():
                    self.channel.write(peer_frame(self.auth, 'ABORT', process.pid), timeout=1)
                process.wait(timeout=1)
            except Exception:
                # Popen retains the exact owned process handle; never locate or
                # stop another process by a caller-provided name/PID.
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    return False
        if process.poll() is None:
            return False
        for stream in (getattr(process, 'stdin', None), getattr(process, 'stdout', None)):
            if stream:
                try: stream.close()
                except Exception: pass
        return True


def _helper_session(channel, *, preflight=None, commit=None, abort=None,
                    restart_command=None, recovery_command=None, launch=subprocess.Popen, timeout=15):
    """Authenticated anonymous-pipe protocol, before normal application startup."""
    from forge_update_handoff import (helper_preflight, helper_commit, helper_abort,
                                     helper_restart_command, helper_recovery_command)
    preflight, commit, abort = preflight or helper_preflight, commit or helper_commit, abort or helper_abort
    restart_command = restart_command or helper_restart_command
    recovery_command = recovery_command or helper_recovery_command
    context = None
    committed = False
    try:
        auth = channel.read(timeout)
        # Preflight checks OS parent identity before consulting any supplied
        # filesystem path, then verifies the exact retained code and operation.
        context = preflight(auth)
        channel.write(peer_frame(auth, 'READY', os.getpid()))
        control = channel.read(timeout)
        if matching_peer(control, auth, 'ABORT', os.getpid()):
            abort(context)
            context = None
            return 0
        if not matching_peer(control, auth, 'COMMIT', os.getpid()):
            raise HandoffRejected()
        # ACK means authenticated control was received, not installation done.
        # Backend commit still waits for the exact parent to exit and rechecks
        # all snapshots/code before an installer can run.
        channel.write(peer_frame(auth, 'COMMITTED', os.getpid()))
        committed = True
        result = commit(context)
        command = restart_command(context, result)
        # This command is generated and reverified by the private backend
        # callback; no pipe/renderer/CLI command is accepted as restart input.
        if not isinstance(command, tuple) or len(command) != 1 or not isinstance(command[0], str):
            raise HandoffRejected('helper_failed')
        launch(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        context = None
        return 0
    except Exception:
        if context is not None and not committed:
            try: abort(context)
            except Exception: pass
        elif context is not None and committed:
            try:
                command = recovery_command(context)
                if isinstance(command, tuple) and len(command) == 1 and isinstance(command[0], str):
                    launch(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except Exception:
                pass
        return 1


def helper_entry():
    # This early gate deliberately precedes arguments, home/vault loading and
    # native/coordinator startup. Packaged validation opens it in a later step.
    if not HANDOFF_ENABLED:
        raise SystemExit(UNAVAILABLE)
    import sys
    if sys.argv != [sys.argv[0], '--forge-update'] or not getattr(sys, 'frozen', False):
        raise SystemExit('Authenticated native handoff is required.')
    try:
        reader, writer = inherited_pipe(-10, 'rb'), inherited_pipe(-11, 'wb')
    except Exception:
        raise SystemExit('Authenticated native handoff is required.') from None
    try:
        raise SystemExit(_helper_session(PipeChannel(reader, writer)))
    finally:
        reader.close()
        writer.close()


def _protocol_smoke(channel, *, timeout=15):
    """Inert packaged IPC fixture: no coordinator, paths, proof or installer."""
    import psutil
    auth = channel.read(timeout)
    fields = {'kind', 'protocol', 'operation_id', 'home', 'nonce', 'session',
        'intent_sha256', 'parent_pid', 'parent_created', 'helper_version', 'helper_source_commit'}
    if (set(auth) != fields or auth['kind'] != 'AUTH' or type(auth['protocol']) is not int or auth['protocol'] != 1
            or type(auth['parent_pid']) is not int or auth['parent_pid'] <= 0
            or type(auth['parent_created']) not in (int, float) or not math.isfinite(auth['parent_created'])
            or not isinstance(auth['operation_id'], str) or not ATTEMPT.fullmatch(auth['operation_id'])
            or any(not isinstance(auth[key], str) or not TOKEN.fullmatch(auth[key]) for key in ('nonce', 'session', 'intent_sha256'))
            or not isinstance(auth['home'], str) or len(auth['home']) > 32768
            or not isinstance(auth['helper_version'], str) or len(auth['helper_version']) > 32
            or auth['helper_source_commit'] is not None and (not isinstance(auth['helper_source_commit'], str)
                or not re.fullmatch('[0-9a-f]{40}', auth['helper_source_commit']))):
        raise HandoffRejected()
    parent = psutil.Process(auth['parent_pid'])
    if psutil.Process().ppid() != parent.pid or parent.create_time() != auth['parent_created']:
        raise HandoffRejected('helper_failed')
    channel.write(peer_frame(auth, 'READY', os.getpid()))
    control = channel.read(timeout)
    if matching_peer(control, auth, 'ABORT', os.getpid()):
        return 0
    if not matching_peer(control, auth, 'COMMIT', os.getpid()):
        raise HandoffRejected()
    channel.write(peer_frame(auth, 'COMMITTED', os.getpid()))
    return 0


def protocol_smoke_entry():
    import sys
    if (set(sys.argv[1:]) != {'--smoke-test', '--forge-update-protocol-smoke'}
            or len(sys.argv) != 3 or not getattr(sys, 'frozen', False) or not os.getenv('FORGE_DATA_DIR')):
        raise SystemExit('Disposable native protocol smoke is required.')
    reader = writer = None
    try:
        reader, writer = inherited_pipe(-10, 'rb'), inherited_pipe(-11, 'wb')
        outcome = _protocol_smoke(PipeChannel(reader, writer))
    except Exception:
        outcome = 1
    finally:
        if reader is not None: reader.close()
        if writer is not None: writer.close()
    raise SystemExit(outcome)
