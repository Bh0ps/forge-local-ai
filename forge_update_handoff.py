"""Private coordinator admission for an explicit, authenticated native restart.

Receipts and grants are runtime capabilities, not settings or journal booleans.
Public dispatcher paths cannot prepare or execute an update. No renderer wait,
network request or helper wait occurs under a coordinator admission lock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import threading
import time

import psutil

from forge_native_handoff import HandoffRejected, NativeGrant


PROTOCOL = 1
TOKEN = re.compile(r'[0-9a-f]{64}\Z')
ATTEMPT = re.compile(r'[0-9a-f]{32}\Z')
LIFETIME = 120
MAX_RECEIPTS = 1024


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class NativeReservation:
    identifier: str
    action: str
    binding: object
    staged_identity: str
    expires: float


@dataclass(frozen=True, repr=False)
class NativePrepared:
    reservation: NativeReservation
    operation_id: str
    attempt_id: str
    receipts: tuple[str, ...]
    workspace_revision: int
    participant_count: int
    intent_sha256: str


@dataclass(frozen=True, repr=False)
class NativeBootstrap:
    prepared: NativePrepared
    command: tuple[str, ...]
    home: Path
    nonce: str
    session: str
    protocol: int
    helper_version: str
    helper_source_commit: str | None
    parent_pid: int
    parent_created: float
    intent_sha256: str


@dataclass(repr=False)
class _Active:
    reservation: NativeReservation
    phase: str = 'checking'
    prepared: NativePrepared | None = None
    bootstrap: NativeBootstrap | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    receipts: dict = field(default_factory=dict)
    stopped_helper_receipt: object | None = None


class HandoffCoordinator:
    def __init__(self, manager, authority, *, clock=time.monotonic):
        self.manager, self.authority, self.clock = manager, authority, clock
        self.lock = threading.RLock()
        self.active = None
        self.cancelled_reservations = []

    def _retire_reservation(self, reservation):
        with self.lock:
            if not any(item is reservation for item in self.cancelled_reservations):
                self.cancelled_reservations.append(reservation)
                del self.cancelled_reservations[:-32]

    def _current(self, reservation=None):
        with self.lock:
            active = self.active
            if (self.manager.shutting_down or active is None
                    or reservation is not None and active.reservation is not reservation):
                raise HandoffRejected()
            if self.clock() >= active.reservation.expires:
                raise HandoffRejected('confirmation_expired')
            return active

    def _editors(self):
        guard = getattr(self.manager.service, 'update_editor_guard', None)
        try:
            if not callable(guard) or guard() is not True:
                raise HandoffRejected('active_work')
        except HandoffRejected:
            raise
        except Exception:
            raise HandoffRejected('workspace_loading') from None

    def _idle(self, *, confirmed=True):
        self._editors()
        try:
            inputs = getattr(self.manager.service, 'update_native_input_guard', None)
            if confirmed and (not callable(inputs) or inputs() is not True):
                raise HandoffRejected('native_input')
            self.manager._idle()
            sessions = getattr(self.manager.service, '_command_sessions', None)
            if sessions and getattr(sessions, 'jobs', {}):
                raise HandoffRejected('active_work')
            models = getattr(self.manager.service, 'model_manager', None)
            if models and any(item.get('status') == 'import_unknown' for item in getattr(models, 'jobs', {}).values()):
                raise HandoffRejected('active_work')
            store = self.manager.store
            with store._connection() as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'approvals' in tables and db.execute("SELECT 1 FROM approvals WHERE status='pending' LIMIT 1").fetchone():
                    raise HandoffRejected('active_work')
                if 'entities' in tables:
                    for row in db.execute("SELECT kind,data FROM entities WHERE kind IN ('questions','steers','ui_submissions')"):
                        item = json.loads(row['data'])
                        if ((row['kind'] == 'questions' and item.get('status') == 'pending')
                                or (row['kind'] == 'steers' and item.get('status') == 'queued')
                                or (row['kind'] == 'ui_submissions' and item.get('state') in ('admitting', 'unknown'))):
                            raise HandoffRejected('active_work')
        except HandoffRejected:
            raise
        except Exception:
            raise HandoffRejected('active_work') from None

    def _staged(self, action):
        from forge_updates import load_operation
        manager = self.manager
        if action == 'apply':
            release, package = manager._package()
            return _digest([action, manager.current_version, str(manager.install_dir), release,
                            manager.state.get('release_proof'), str(package)])
        data = load_operation(manager.home, manager.state.get('operation_id'))
        if data['state'] != 'installed':
            raise HandoffRejected('preparation_failed')
        return _digest([action, manager.current_version, str(manager.install_dir), data])

    def reserve(self, binding, action='apply'):
        from forge_updates import version
        if action not in ('apply', 'rollback') or not self.authority.binding_valid(binding):
            raise HandoffRejected('workspace_loading')
        if version(self.manager.current_version) < (5, 1, 3):
            raise HandoffRejected('handoff_unavailable')
        with self.manager._quiesce():
            with self.lock:
                if self.active is not None or self.manager.applying or self.manager.shutting_down:
                    raise HandoffRejected('active_work')
            # The checking phase may confirm an already-delivered transcript.
            # Capturing/transcribing still blocks in manager._idle; the strict
            # unconsumed-input fence closes before preparation can make a backup.
            self._idle(confirmed=False)
            if not self.manager.install_dir:
                raise HandoffRejected('handoff_unavailable')
            staged = self._staged(action)
            reservation = NativeReservation(secrets.token_hex(16), action, binding,
                staged, self.clock() + LIFETIME)
            with self.lock:
                if self.manager.shutting_down or not self.authority.binding_valid(binding):
                    raise HandoffRejected('workspace_changed')
                self.active = _Active(reservation)
            return reservation

    def confirm_draft(self, drafts, data):
        identifier = data.get('attempt_id')
        if not isinstance(identifier, str) or not ATTEMPT.fullmatch(identifier):
            raise HandoffRejected()
        deliveries = data.get('delivery_ids', [])
        if (not isinstance(deliveries, list) or len(deliveries) > 16
                or any(not isinstance(item, str) or not TOKEN.fullmatch(item) for item in deliveries)
                or len(set(deliveries)) != len(deliveries)):
            raise HandoffRejected()
        gate = self.manager.service.admission_lock
        with gate:
            active = self._current()
            if active.phase != 'checking' or not self.authority.receipt_attempt(identifier, active.reservation):
                raise HandoffRejected('workspace_changed')
            try:
                snapshot = drafts.confirmation_state(data)
            except Exception:
                raise HandoffRejected('draft_conflict') from None
            if deliveries:
                guard = getattr(self.manager.service, 'host_dictation_guard', None)
                try:
                    if not callable(guard) or guard(snapshot, tuple(deliveries)) is not True:
                        raise HandoffRejected('native_input')
                except HandoffRejected:
                    raise
                except Exception:
                    raise HandoffRejected('native_input') from None
            receipt = secrets.token_hex(32)
            with self.lock:
                if self.active is not active or active.phase != 'checking' or len(active.receipts) >= MAX_RECEIPTS:
                    raise HandoffRejected('workspace_changed')
                active.receipts[receipt] = (identifier, snapshot, drafts)
                active.attempt_id = identifier
            return {'ok': True, 'receipt': receipt}

    def _receipts(self, active, identifier, receipts):
        if len(receipts) > MAX_RECEIPTS or len(set(receipts)) != len(receipts):
            raise HandoffRejected()
        for receipt in receipts:
            if not isinstance(receipt, str) or not TOKEN.fullmatch(receipt):
                raise HandoffRejected()
            with self.lock:
                saved = active.receipts.get(receipt)
            if not saved or saved[0] != identifier:
                raise HandoffRejected('workspace_changed')
            _, snapshot, drafts = saved
            try:
                latest = drafts.confirmation_state({**snapshot['scope'], 'expected_revision': snapshot['expected_revision']})
            except Exception:
                raise HandoffRejected('draft_conflict') from None
            if latest != snapshot:
                raise HandoffRejected('workspace_changed')

    def prepare(self, grant, ack):
        from forge_updates import atomic_text, encode, file_hash, load_operation, regular_tree
        manager = self.manager
        if not isinstance(grant, NativeGrant):
            raise HandoffRejected()
        active = self._current(grant.reservation)
        revision, count, receipts = self.authority.readiness(grant)
        if (not isinstance(ack, dict) or ack.get('attempt_id') != grant.attempt_id
                or ack.get('workspace_revision') != revision or type(ack.get('workspace_revision')) is not int
                or ack.get('participant_count') != count or type(ack.get('participant_count')) is not int
                or ack.get('receipts') != list(receipts)):
            raise HandoffRejected()
        self.authority.consume(grant, 'prepare', active.reservation.binding, active.reservation)
        active.attempt_id = grant.attempt_id
        with manager._quiesce():
            self._current(active.reservation)
            self._idle()
            self._receipts(active, grant.attempt_id, receipts)
            if self._staged(active.reservation.action) != active.reservation.staged_identity:
                raise HandoffRejected('workspace_changed')
            active.phase = 'preparing'
            try:
                result = (manager._prepare_apply if active.reservation.action == 'apply' else manager._prepare_rollback)()
                active.operation_id = result['operation_id']
                data = load_operation(manager.home, result['operation_id'])
                if active.reservation.action == 'apply':
                    helper_root = Path(data['backup']) / 'installation'
                    helper_source = data.get('previous_source_commit')
                else:
                    # Rollback runs the CURRENT protocol-capable host, never a
                    # retained older host that may refuse the new protocol.
                    helper_root = Path(data['backup']) / ('current-helper-' + secrets.token_hex(16))
                    current = regular_tree(manager.install_dir)
                    shutil.copytree(manager.install_dir, helper_root)
                    from forge_updates import verify_tree
                    verify_tree(helper_root, current)
                    helper_source = data.get('source_commit')
                helper_files = regular_tree(helper_root)
                self._current(active.reservation)
                self._receipts(active, grant.attempt_id, receipts)
                self._idle()
                nonce = secrets.token_hex(32)
                binding = active.reservation.binding
                data.update(parent_pid=binding.parent_pid, parent_created=binding.parent_created)
                data['handoff'] = {'protocol': PROTOCOL, 'attempt_id': grant.attempt_id,
                    'session_sha256': sha256(binding.session.encode()).hexdigest(),
                    'nonce_sha256': sha256(nonce.encode()).hexdigest(), 'phase': 'prepared',
                    'created_at': int(time.time()), 'expires_at': int(time.time()) + LIFETIME,
                    'helper_root': str(helper_root), 'helper_version': manager.current_version,
                    'helper_source_commit': helper_source, 'helper_files': helper_files,
                    'parent_executable': str(manager.install_dir / 'Forge.exe')}
                path = manager.root / 'operations' / (data['id'] + '.json')
                atomic_text(path, encode(data))
                prepared = NativePrepared(active.reservation, data['id'], grant.attempt_id,
                    tuple(receipts), revision, count, file_hash(path))
                bootstrap = NativeBootstrap(prepared, (str(helper_root / 'Forge.exe'), '--forge-update'),
                    manager.home, nonce, binding.session, PROTOCOL, manager.current_version,
                    helper_source, binding.parent_pid, binding.parent_created, prepared.intent_sha256)
                with self.lock:
                    active.prepared, active.bootstrap, active.phase = prepared, bootstrap, 'prepared'
                return prepared
            except BaseException:
                # Failed preparation never owns an executable admission. The
                # native owner confirms abort before unlocking its workspace.
                active.phase = 'failed'
                manager.applying = False
                raise

    def _snapshot(self, active):
        from forge_updates import load_operation
        data = load_operation(self.manager.home, active.prepared.operation_id)
        verify_saved_work(self.manager.home, data)
        return data

    def launch(self, grant, prepared):
        from forge_updates import file_hash
        if not isinstance(grant, NativeGrant) or not isinstance(prepared, NativePrepared):
            raise HandoffRejected()
        active = self._current(prepared.reservation)
        if active.prepared is not prepared or active.phase != 'prepared':
            raise HandoffRejected()
        self.authority.consume(grant, 'launch', active.reservation.binding, active.reservation)
        with self.manager._quiesce():
            self._idle()
            self._receipts(active, prepared.attempt_id, prepared.receipts)
            self._snapshot(active)
            path = self.manager.root / 'operations' / (prepared.operation_id + '.json')
            if file_hash(path) != prepared.intent_sha256:
                raise HandoffRejected('workspace_changed')
            # This preflight is also done in the launched helper. Parent-side
            # verification guarantees no changed/unknown retained helper runs.
            frame = native_bootstrap_frame(active.bootstrap)
            verify_bootstrap(frame, executable=active.bootstrap.command[0],
                proof_policy=self.manager.proof_keys, publishers=self.manager.publishers,
                verifier=self.manager.verifier, install_dir=self.manager.install_dir,
                inspect_parent=False)
            # Long proof/tree reads stay outside the shutdown admission lock.
            # The final memory-only reservation shares its shutdown boundary.
            with self.manager.admission_lock:
                self._current(prepared.reservation)
                if not self.authority.binding_valid(active.reservation.binding):
                    raise HandoffRejected('workspace_changed')
                with self.lock:
                    active.phase = 'launched'
            return active.bootstrap

    def commit(self, grant, prepared, ready):
        from forge_updates import atomic_text, encode
        if not isinstance(grant, NativeGrant) or not isinstance(prepared, NativePrepared):
            raise HandoffRejected()
        active = self._current(prepared.reservation)
        if active.prepared is not prepared or active.phase != 'launched' or ready is None:
            raise HandoffRejected()
        self.authority.consume(grant, 'commit', active.reservation.binding, active.reservation)
        with self.manager._quiesce():
            self._idle()
            self._receipts(active, prepared.attempt_id, prepared.receipts)
            data = self._snapshot(active)
            self._current(prepared.reservation)
            if not self.authority.binding_valid(active.reservation.binding):
                raise HandoffRejected('workspace_changed')
            data['handoff']['phase'] = 'committed'
            atomic_text(self.manager.root / 'operations' / (prepared.operation_id + '.json'), encode(data))
            with self.lock:
                active.phase = 'committed'
            return True

    def abort(self, identifier, *, stopped_helper=None):
        from forge_updates import atomic_text, encode, load_operation
        with self.manager._quiesce():
            if not isinstance(identifier, str) or not ATTEMPT.fullmatch(identifier):
                raise HandoffRejected()
            active = self.active
            if active is None:
                return {'not_executed': True}
            if (active.attempt_id is not None and identifier != active.attempt_id
                    or self.authority.status(identifier).get('attempt_id') != identifier):
                raise HandoffRejected()
            if active.phase == 'committed' or stopped_helper is not None:
                retained = active.stopped_helper_receipt
                if stopped_helper is None or retained is not None and stopped_helper is not retained:
                    raise HandoffRejected('helper_failed')
                if retained is None:
                    if not self.authority.consume_stopped_helper(
                            stopped_helper, active.reservation, active.operation_id):
                        raise HandoffRejected('helper_failed')
                    # Authority remains one-use. Retain the exact consumed proof
                    # only on this active reservation so a transient journal or
                    # status write can be retried without minting a new grant.
                    active.stopped_helper_receipt = stopped_helper
                # A retained receipt never replaces current OS parent checks.
                parent = psutil.Process(active.reservation.binding.parent_pid)
                if (parent.create_time() != active.reservation.binding.parent_created
                        or Path(parent.exe()).resolve() != self.manager.install_dir / 'Forge.exe'):
                    raise HandoffRejected('helper_failed')
            if active.operation_id:
                data = load_operation(self.manager.home, active.operation_id)
                already_cancelled = (data['state'] in ('cancelled', 'installed')
                    and data.get('handoff', {}).get('phase') == 'aborted'
                    and data.get('handoff', {}).get('not_executed') is True
                    and data.get('handoff', {}).get('attempt_id') == identifier)
                if data['state'] not in ('prepared', 'rollback_prepared') and not already_cancelled:
                    raise HandoffRejected('helper_failed')
                if 'handoff' in data: data['handoff'].update(phase='aborted', not_executed=True)
                data['state'] = 'cancelled' if active.reservation.action == 'apply' else 'installed'
                atomic_text(self.manager.root / 'operations' / (data['id'] + '.json'), encode(data))
                self.manager._save(state='ready' if active.reservation.action == 'apply' else 'installed')
            self.manager.applying = False
            with self.lock:
                self.active = None
            self._retire_reservation(active.reservation)
            return {'not_executed': True}

    def cancel_reservation(self, reservation):
        """Native begin failed before issuing an attempt; no disk work exists."""
        with self.manager._quiesce():
            active = self.active
            if active is None and any(item is reservation for item in self.cancelled_reservations):
                return {'not_executed': True}
            if (active is None or active.reservation is not reservation
                    or active.phase != 'checking' or active.attempt_id is not None):
                raise HandoffRejected()
            # An issued native attempt must take the normal acknowledged abort
            # path, even if it has not yet confirmed any drafts.
            if self.authority.status().get('phase') not in ('idle', 'failed'):
                raise HandoffRejected()
            with self.lock:
                self.active = None
            self._retire_reservation(reservation)
            return {'not_executed': True}


def native_bootstrap_frame(bootstrap):
    if not isinstance(bootstrap, NativeBootstrap):
        raise HandoffRejected()
    return {'kind': 'AUTH', 'protocol': bootstrap.protocol, 'operation_id': bootstrap.prepared.operation_id,
        'home': str(bootstrap.home), 'nonce': bootstrap.nonce, 'session': bootstrap.session,
        'intent_sha256': bootstrap.intent_sha256, 'parent_pid': bootstrap.parent_pid,
        'parent_created': bootstrap.parent_created, 'helper_version': bootstrap.helper_version,
        'helper_source_commit': bootstrap.helper_source_commit}


def verify_saved_work(home, data):
    from forge_updates import database_guard, plain_path, regular_tree, ROLLBACK_DATA_FOLDERS
    home = plain_path(home)
    baseline = data['rollback_guard']
    if database_guard(home / 'state/forge.sqlite3', baseline) != baseline:
        raise HandoffRejected('workspace_changed')
    backup = plain_path(data['backup'])
    for folder in ROLLBACK_DATA_FOLDERS:
        current, saved = home / folder, backup / 'data' / folder
        if (regular_tree(current) if current.exists() else []) != (regular_tree(saved) if saved.exists() else []):
            raise HandoffRejected('workspace_changed')


def verify_bootstrap(frame, *, executable=None, proof_policy=None, publishers=None,
                     verifier=None, install_dir=None, inspect_parent=True):
    """No execution, journal mutation, coordinator or migration before READY."""
    from forge_updates import (load_operation, plain_path, file_hash, helper_preflight,
        verify_tree, require_trust, TRUSTED_PUBLISHERS, TRUSTED_UPDATE_KEYS, authenticode, version)
    from forge_update_proof import BUILD_NAME, read_record, verify_build, normalize_keys
    import sys
    fields = {'kind', 'protocol', 'operation_id', 'home', 'nonce', 'session', 'intent_sha256',
              'parent_pid', 'parent_created', 'helper_version', 'helper_source_commit'}
    if (not isinstance(frame, dict) or set(frame) != fields or frame['kind'] != 'AUTH'
            or type(frame['protocol']) is not int or frame['protocol'] != PROTOCOL
            or not isinstance(frame['operation_id'], str) or not ATTEMPT.fullmatch(frame['operation_id'])
            or any(not isinstance(frame[key], str) or not TOKEN.fullmatch(frame[key])
                   for key in ('nonce', 'session', 'intent_sha256'))
            or type(frame['parent_pid']) is not int or frame['parent_pid'] < 1
            or type(frame['parent_created']) not in (int, float) or not math.isfinite(frame['parent_created'])
            or not isinstance(frame['home'], str) or len(frame['home']) > 32768):
        raise HandoffRejected()
    # Reject bare/manual or unrelated parent launches before touching any path
    # supplied by the pipe. The executable match follows authenticated intent.
    if inspect_parent:
        parent = psutil.Process(frame['parent_pid'])
        if parent.create_time() != frame['parent_created'] or psutil.Process().ppid() != frame['parent_pid']:
            raise HandoffRejected()
    if not Path(frame['home']).is_absolute():
        raise HandoffRejected()
    home = plain_path(frame['home'])
    operation = home / 'runtimes/updates/operations' / (frame['operation_id'] + '.json')
    if plain_path(operation).stat().st_size > 16 * 1024 * 1024:
        raise HandoffRejected()
    if file_hash(plain_path(operation)) != frame['intent_sha256']:
        raise HandoffRejected()
    data = load_operation(home, frame['operation_id'])
    if version(frame['helper_version']) < (5, 1, 3):
        raise HandoffRejected('handoff_unavailable')
    handoff = data.get('handoff') or {}
    if (handoff.get('protocol') != PROTOCOL or handoff.get('phase') != 'prepared'
            or type(handoff.get('expires_at')) is not int or type(handoff.get('created_at')) is not int
            or not handoff['created_at'] <= time.time() < handoff['expires_at']
            or handoff['expires_at'] - handoff['created_at'] > LIFETIME
            or not hmac.compare_digest(handoff.get('nonce_sha256', ''), sha256(frame['nonce'].encode()).hexdigest())
            or not hmac.compare_digest(handoff.get('session_sha256', ''), sha256(frame['session'].encode()).hexdigest())
            or data['parent_pid'] != frame['parent_pid'] or data['parent_created'] != frame['parent_created']
            or handoff.get('helper_version') != frame['helper_version']
            or handoff.get('helper_source_commit') != frame['helper_source_commit']):
        raise HandoffRejected()
    helper = plain_path(executable or sys.executable)
    root = plain_path(handoff.get('helper_root', ''))
    backup = plain_path(data['backup'])
    if (helper != root / 'Forge.exe' or not root.is_relative_to(backup)
            or root == backup or root != backup / 'installation' and not re.fullmatch('current-helper-[0-9a-f]{32}', root.name)):
        raise HandoffRejected()
    verify_tree(root, handoff['helper_files'])
    pins = normalize_keys(TRUSTED_UPDATE_KEYS if proof_policy is None else proof_policy)
    verify = verifier or authenticode
    if data.get('trust_mode') == 'ed25519':
        verify_build(read_record(root / BUILD_NAME), pins, root=root,
            expected_version=frame['helper_version'], expected_source=frame['helper_source_commit'], installed=True)
    else:
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
            require_trust(root / name, frame['helper_version'],
                TRUSTED_PUBLISHERS if publishers is None else publishers, verify)
    if inspect_parent:
        parent = psutil.Process(frame['parent_pid'])
        if (parent.create_time() != frame['parent_created']
                or plain_path(parent.exe()) != plain_path(handoff['parent_executable'])
                or psutil.Process().ppid() != frame['parent_pid']):
            raise HandoffRejected()
    plan = helper_preflight(home, frame['operation_id'], frame['parent_pid'],
        install_dir=install_dir, proof_policy=pins, publishers=publishers, verifier=verify)
    verify_saved_work(home, data)
    return home, data, plan


@dataclass(frozen=True, repr=False)
class HelperReadyContext:
    frame: dict
    executable: str
    home: Path
    operation: dict
    plan: object
    proof_policy: object
    publishers: object
    verifier: object
    install_dir: object


_helper_lock = threading.Lock()
_helper_contexts = {}
_helper_completed = {}
_helper_recoverable = {}
_helper_admissions = {}


def helper_preflight(frame, *, executable=None, proof_policy=None, publishers=None,
                     verifier=None, install_dir=None):
    import sys
    from forge_updates import authenticode
    home, data, plan = verify_bootstrap(frame, executable=executable, proof_policy=proof_policy,
        publishers=publishers, verifier=verifier, install_dir=install_dir)
    context = HelperReadyContext(dict(frame), str(executable or sys.executable), home,
        data, plan, proof_policy, publishers, verifier or authenticode, install_dir)
    with _helper_lock:
        if _helper_contexts:
            raise HandoffRejected()
        _helper_contexts[id(context)] = context
    return context


def _consume_context(context):
    with _helper_lock:
        if not isinstance(context, HelperReadyContext) or _helper_contexts.pop(id(context), None) is not context:
            raise HandoffRejected()


def helper_abort(context):
    _consume_context(context)
    # The coordinator owns journal disarming. A helper abort performs no file,
    # database or install mutation and cannot rewind user work.
    return {'not_executed': True}


def helper_commit(context, *, runner=None, wait_seconds=60):
    """Called only after the pipe transport authenticates matching COMMIT."""
    admission = _InstallerAdmission(context)
    try:
        result = _helper_commit(context, runner=runner, wait_seconds=wait_seconds, admission=admission)
        with _helper_lock:
            _helper_completed[id(context)] = (context, result)
        return result
    except BaseException:
        if _not_executed(context, admission):
            with _helper_lock:
                _helper_recoverable[id(context)] = context
        raise
    finally:
        admission.release()


class _InstallerAdmission:
    """Exact private intent binding through the actual installer admission."""
    def __init__(self, context):
        _consume_context(context)
        self.context = context
        self.expected = {**context.operation,
            'handoff': {**context.operation['handoff'], 'phase': 'committed'}}
        self.admitted = False
        self.applying_written = False
        with _helper_lock:
            _helper_admissions[id(context)] = self

    def require_current(self):
        with _helper_lock:
            if _helper_admissions.get(id(self.context)) is not self or self.admitted:
                raise HandoffRejected()

    def release(self):
        with _helper_lock:
            if _helper_admissions.get(id(self.context)) is self:
                del _helper_admissions[id(self.context)]

    def wrote_applying(self, data):
        self.require_current()
        if self.admitted or self.applying_written or data != {**self.expected, 'state': 'applying'}:
            raise HandoffRejected()
        self.applying_written = True

    def check(self, data, *, applying=False, admit=False):
        from forge_updates import load_operation
        self.require_current()
        expected = {**self.expected, 'state': 'applying'} if applying else self.expected
        if self.admitted or data != expected:
            raise HandoffRejected('workspace_changed')
        if admit:
            if not applying or not self.applying_written:
                raise HandoffRejected()
            # User state may change during foundation preflight. Recheck it
            # before the final journal/parent fence and actual admission.
            verify_saved_work(self.context.home, expected)
        if (load_operation(self.context.home, self.context.frame['operation_id']) != expected
                or time.time() >= expected['handoff']['expires_at']):
            raise HandoffRejected('workspace_changed')
        # PID reuse is a refusal too; this never substitutes a new process for
        # the authenticated parent's observed exit.
        if psutil.pid_exists(self.context.frame['parent_pid']):
            raise HandoffRejected('helper_failed')
        if admit:
            with _helper_lock:
                if _helper_admissions.get(id(self.context)) is not self or self.admitted:
                    raise HandoffRejected()
                self.admitted = True
                del _helper_admissions[id(self.context)]


def _not_executed(context, admission):
    """Disarm only the unchanged authenticated intent before runner admission.

    Changed journals and unknown applying/installed outcomes stay untouched.
    Only this helper's own proved applying write before runtime admission may
    be disarmed; an already-admitted installer is never safe to replay/rewind.
    """
    from forge_updates import load_operation, atomic_text, encode
    try:
        if type(admission) is not _InstallerAdmission or admission.context is not context or admission.admitted:
            return False
        admission.require_current()
        data = load_operation(context.home, context.frame['operation_id'])
        original = context.operation
        committed = {**original, 'handoff': {**original['handoff'], 'phase': 'committed'}}
        applying = {**committed, 'state': 'applying'}
        if data not in (original, committed) and not (admission.applying_written and data == applying):
            return False
        data['handoff'].update(phase='aborted', not_executed=True)
        data['state'] = 'cancelled' if original['state'] == 'prepared' else 'installed'
        data['error'] = 'Restart stopped before installation. Your work was retained.'
        atomic_text(context.home / 'runtimes/updates/operations' / (data['id'] + '.json'), encode(data))
        return True
    except Exception:
        # Retain a journal that cannot be authenticated; never repair it from
        # speculative state or obscure a possible actual installer admission.
        return False


def _verified_restart(context, data, number, source):
    from forge_updates import plain_path, require_trust, regular_tree, TRUSTED_PUBLISHERS, TRUSTED_UPDATE_KEYS
    from forge_update_proof import BUILD_NAME, read_record, verify_build, normalize_keys
    # Never reopen while the exact parent is still alive or its PID is reused.
    if psutil.pid_exists(context.frame['parent_pid']):
        raise HandoffRejected('helper_failed')
    if any(data.get(key) != context.operation.get(key) for key in (
            'id', 'home', 'backup', 'install_dir', 'previous_version', 'version', 'trust_mode',
            'previous_source_commit', 'source_commit', 'parent_pid', 'parent_created')):
        raise HandoffRejected('workspace_changed')
    root = plain_path(data['install_dir'])
    if root != context.plan['expected']:
        raise HandoffRejected('workspace_changed')
    if data.get('trust_mode') == 'ed25519':
        verify_build(read_record(root / BUILD_NAME), normalize_keys(
            TRUSTED_UPDATE_KEYS if context.proof_policy is None else context.proof_policy), root=root,
            expected_version=number, expected_source=source, installed=True)
    else:
        regular_tree(root)
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
            require_trust(root / name, number, TRUSTED_PUBLISHERS if context.publishers is None else context.publishers,
                context.verifier)
    return (str(root / 'Forge.exe'),)


def helper_restart_command(context, result):
    """One generated command from the exact private completed helper result."""
    from forge_updates import load_operation
    with _helper_lock:
        saved = _helper_completed.get(id(context))
        if not saved or saved[0] is not context or saved[1] is not result:
            raise HandoffRejected()
        del _helper_completed[id(context)]
    data = load_operation(context.home, context.frame['operation_id'])
    if data['state'] == 'installed' and result.get('installed') is True:
        number, source = data['version'], data.get('source_commit')
    elif data['state'] == 'rolled_back' and result.get('rolled_back') is True:
        number, source = data['previous_version'], data.get('previous_source_commit')
    else:
        raise HandoffRejected('helper_failed')
    if result.get('version') != number:
        raise HandoffRejected('helper_failed')
    return _verified_restart(context, data, number, source)


def helper_recovery_command(context):
    """Reopen intact current code only after a proved pre-install refusal."""
    from forge_updates import load_operation
    with _helper_lock:
        if _helper_recoverable.pop(id(context), None) is not context:
            raise HandoffRejected()
    data = load_operation(context.home, context.frame['operation_id'])
    if (data['state'] not in ('cancelled', 'installed') or data.get('handoff', {}).get('phase') != 'aborted'
            or data.get('handoff', {}).get('not_executed') is not True):
        raise HandoffRejected('helper_failed')
    rollback = context.operation['state'] == 'rollback_prepared'
    number = data['version'] if rollback else data['previous_version']
    source = data.get('source_commit') if rollback else data.get('previous_source_commit')
    return _verified_restart(context, data, number, source)


def _helper_commit(context, *, runner=None, wait_seconds=60, admission):
    from forge_updates import helper_main, plain_path, load_operation, verify_tree, require_trust, TRUSTED_PUBLISHERS
    from forge_update_proof import verify_build, read_record, BUILD_NAME, normalize_keys
    deadline = time.monotonic() + min(wait_seconds, 60)
    while psutil.pid_exists(context.frame['parent_pid']):
        try:
            parent = psutil.Process(context.frame['parent_pid'])
            if parent.create_time() != context.frame['parent_created']:
                raise HandoffRejected('helper_failed')
            if plain_path(parent.exe()) != plain_path(context.operation['handoff']['parent_executable']):
                raise HandoffRejected('helper_failed')
        except psutil.NoSuchProcess:
            break
        if time.monotonic() >= deadline:
            raise HandoffRejected('helper_failed')
        time.sleep(.05)
    data = load_operation(context.home, context.frame['operation_id'])
    expected = dict(context.operation)
    expected['handoff'] = {**expected['handoff'], 'phase': 'committed'}
    if data != expected or time.time() >= data['handoff']['expires_at']:
        raise HandoffRejected('workspace_changed')
    root = plain_path(data['handoff']['helper_root'])
    verify_tree(root, data['handoff']['helper_files'])
    if data.get('trust_mode') == 'ed25519':
        from forge_updates import TRUSTED_UPDATE_KEYS
        verify_build(read_record(root / BUILD_NAME), normalize_keys(
            TRUSTED_UPDATE_KEYS if context.proof_policy is None else context.proof_policy), root=root,
            expected_version=data['handoff']['helper_version'],
            expected_source=data['handoff']['helper_source_commit'], installed=True)
    else:
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
            require_trust(root / name, data['handoff']['helper_version'],
                TRUSTED_PUBLISHERS if context.publishers is None else context.publishers, context.verifier)
    verify_saved_work(context.home, data)
    # Revalidate snapshot/code/artifacts after actual parent exit. Legacy helper
    # tests retain their isolated API; this native path never bypasses READY.
    return helper_main(context.home, data['id'], data['parent_pid'], install_dir=context.install_dir,
        proof_policy=context.proof_policy, publishers=context.publishers,
        verifier=context.verifier, runner=runner, wait_seconds=0, _native_admission=admission)
