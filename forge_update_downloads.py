"""Monitor-owned, opt-in verified downloads. This module never installs code."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from email.utils import parsedate_to_datetime
import json
import threading
import time
import re

import httpx

from forge_store import encode

MAX_ATTEMPTS = 4
BACKOFF = (60, 300, 900)
FRESH_SECONDS = 30 * 60
MAX_RETRY_DELAY = 60 * 60
REASONS = frozenset(('disabled', 'checks_disabled', 'fresh_check_required', 'active_work',
    'inspection', 'native_handoff', 'stopped', 'policy_changed', 'candidate_changed',
    'publisher_required', 'user_cancelled', 'offline', 'rate_limited', 'server_unavailable',
    'verification_failed', 'retry_exhausted', 'policy_error', 'verified'))


class AutomaticRejected(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in REASONS else 'verification_failed'
        super().__init__('Automatic download deferred. Check the update status.')


def candidate_identity(release, channel):
    # Renewing the proof or changing a preference cannot undo a user's Cancel.
    return sha256(encode([channel, *[release.get(key) for key in
        ('version', 'tag', 'name', 'url', 'sha256', 'size')]]).encode()).hexdigest()


def failure_category(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429: return 'rate_limited'
        if 500 <= exc.response.status_code <= 599: return 'server_unavailable'
    if isinstance(exc, (httpx.TransportError, TimeoutError)): return 'offline'
    return 'verification_failed'


def retry_delay(exc, attempt, now):
    delay = BACKOFF[min(len(BACKOFF) - 1, max(0, attempt - 1))]
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
        value = exc.response.headers.get('retry-after', '')
        try:
            seconds = int(value) if value.isdecimal() else int(parsedate_to_datetime(value).timestamp()) - now
            if seconds > 0: delay = max(delay, min(MAX_RETRY_DELAY, seconds))
        except (ValueError, TypeError, OverflowError): pass
    return delay


@dataclass(repr=False)
class Candidate:
    identity: str
    release: dict
    proof: dict
    policy: dict
    owner: threading.Event
    observed: float


@dataclass(repr=False)
class DownloadLease:
    candidate: Candidate
    cancel: threading.Event
    reason: str | None = None
    finished: threading.Event = field(default_factory=threading.Event)


class AutomaticDownloads:
    def __init__(self, manager):
        self.manager = manager
        self.candidate = None
        self.active = None
        self.phase, self.reason = 'off', 'disabled'
        self.needs_check = False
        with manager.store._connection() as db:
            row = db.execute("SELECT data FROM entities WHERE kind='update_auto' AND id='current'").fetchone()
        try: self.history = json.loads(row['data']) if row else {}
        except (ValueError, TypeError): self.history = {'corrupt': True}
        self.history_valid = isinstance(self.history, dict) and (not self.history or not (
                not isinstance(self.history.get('candidate'), str) or
                not re.fullmatch('[0-9a-f]{64}', self.history['candidate']) or
                type(self.history.get('attempt_count')) is not int or not 0 <= self.history['attempt_count'] <= MAX_ATTEMPTS or
                type(self.history.get('not_before')) is not int or self.history['not_before'] < 0 or
                type(self.history.get('suppressed')) is not bool or
                'terminal' in self.history and type(self.history['terminal']) is not bool))
        if not self.history_valid:
            self.history = {}
            self.phase, self.reason = 'unavailable', 'policy_error'

    def status(self):
        return {'phase': self.phase, 'reason': self.reason,
                'attempt_count': min(MAX_ATTEMPTS, max(0, self.history.get('attempt_count', 0)))
                    if type(self.history.get('attempt_count', 0)) is int else 0}

    def _history(self, **data):
        self.history = self.manager.store.save_entity('update_auto', {**self.history, **data, 'id': 'current'})

    def checking(self, policy):
        with self.manager.lock:
            self.candidate = None; self.needs_check = False
            self.phase, self.reason = ('checking', None) if policy['update_auto_download'] else ('off', 'disabled')

    def check_failed(self, policy, error):
        with self.manager.lock:
            self.candidate = None
            self.phase, self.reason = ('unavailable', failure_category(error)) if policy['update_auto_download'] else ('off', 'disabled')

    def checked(self, owner, policy, result):
        current = self.manager.store.update_policy()
        with self.manager.lock:
            if (owner.is_set() or self.manager.shutting_down or policy != current or
                    self.manager.thread_stop_event is not owner): return
            release = result.get('release') or {}
            if (not policy['update_checks'] or not policy['update_auto_download'] or
                    result.get('last_check_failed') or result.get('state') not in ('available', 'ready') or
                    result.get('channel') != policy['update_channel'] or release.get('trust_mode') != 'ed25519'):
                self.candidate = None
                self.phase, self.reason = 'off', 'disabled' if not policy['update_auto_download'] else 'publisher_required'
                return
            self.candidate = Candidate(candidate_identity(release, policy['update_channel']), dict(release),
                dict(self.manager.state.get('release_proof') or {}), dict(policy), owner, time.monotonic())
            self.needs_check = False
            self.phase, self.reason = 'waiting', 'active_work'

    @contextmanager
    def admission(self):
        # Same order as service mutations/native handoff. No network wait occurs
        # here, and the small shutdown lock never spans disk or SQLite work.
        gate = getattr(self.manager.service, 'admission_lock', None)
        if gate is None: gate = threading.RLock()  # Isolated foundation fixtures.
        with gate, self.manager.service.jobs.lock, self.manager.lock:
            yield

    def validate_owner(self, lease):
        manager = self.manager; candidate = lease.candidate
        if manager.shutting_down or candidate.owner.is_set(): raise AutomaticRejected('stopped')
        if self.active is not lease or manager.thread_stop_event is not candidate.owner:
            raise AutomaticRejected('candidate_changed')
        if lease.cancel.is_set(): raise AutomaticRejected(lease.reason or 'stopped')

    def validate(self, lease, *, resources=True, connection=None):
        self.validate_owner(lease)
        manager = self.manager; candidate = lease.candidate
        policy = manager.store._update_policy(connection) if connection is not None else manager.store.update_policy()
        if policy != candidate.policy: raise AutomaticRejected('policy_changed')
        if not policy['update_checks']: raise AutomaticRejected('checks_disabled')
        if not policy['update_auto_download']: raise AutomaticRejected('disabled')
        if (manager.state.get('release') != candidate.release or manager.state.get('release_proof') != candidate.proof or
                manager.state.get('channel') != policy['update_channel'] or manager.state.get('last_check_failed') is True):
            raise AutomaticRejected('candidate_changed')
        if time.monotonic() - candidate.observed > FRESH_SECONDS: raise AutomaticRejected('fresh_check_required')
        if resources: self._idle()

    def _idle(self):
        manager = self.manager
        if manager.applying: raise AutomaticRejected('native_handoff')
        if manager.inspection_required: raise AutomaticRejected('inspection')
        coordinator = manager.native_handoff
        if coordinator and getattr(coordinator, 'active', None) is not None: raise AutomaticRejected('native_handoff')
        try: manager._idle()
        except Exception: raise AutomaticRejected('active_work') from None
        service = manager.service
        if getattr(getattr(service, 'ai_workflow_manager', None), 'jobs', {}): raise AutomaticRejected('active_work')
        models = getattr(service, 'model_manager', None)
        if getattr(models, 'import_reservations', set()): raise AutomaticRejected('active_work')
        providers = getattr(service, 'providers', None)
        queues = [getattr(providers, name, None) for name in ('queue', 'remote_queue')]
        queues += list(getattr(providers, 'resource_queues', {}).values())
        queues.append(getattr(getattr(service, 'runtime', None), 'inference_queue', None))
        for queue in queues:
            if queue is None: continue
            with queue.condition:
                if queue.active or queue.pending: raise AutomaticRejected('active_work')

    def changed(self, reason='policy_changed', lease=None):
        # Called under service admission by preference/foreground-work starts.
        # It never cancels an unrelated manual check/download.
        with self.manager.admission_lock:
            current = self.active
            if current is not None and (lease is None or current is lease):
                current.reason = reason; current.cancel.set()

    def preferences_changed(self):
        policy = self.manager.store.update_policy()
        with self.manager.lock:
            if self.active and self.active.candidate.policy != policy: self.changed()
            checking = self.manager.check_policy
            if checking and checking[1] != policy:
                with self.manager.admission_lock:
                    if self.manager.thread_stop_event is checking[0]: self.manager.cancel_event.set()

    def stop(self, owner):
        with self.manager.admission_lock:
            if self.active and self.active.candidate.owner is owner:
                self.active.reason = 'stopped'; self.active.cancel.set()

    def user_cancel(self):
        with self.manager.lock:
            lease = self.active; candidate = lease.candidate if lease else self.candidate
            if candidate and self.manager.thread_stop_event is candidate.owner:
                if lease: lease.reason = 'user_cancelled'; lease.cancel.set()
                self._history(candidate=candidate.identity,
                    attempt_count=self.history.get('attempt_count', 0), not_before=0, suppressed=True)
                self.phase, self.reason = 'cancelled', 'user_cancelled'

    def manual_retry(self, release, channel):
        with self.manager.lock:
            identity = candidate_identity(release, channel)
            owned = (not self.history_valid or self.history.get('candidate') == identity or
                self.candidate is not None and self.candidate.identity == identity)
            self.candidate = None
            if owned:
                self._history(candidate=identity, attempt_count=0, not_before=0, suppressed=False, terminal=False)
                self.history_valid = True
                self.phase, self.reason = 'waiting', 'fresh_check_required'

    def tick(self, owner, now=None):
        now = int(time.time()) if now is None else int(now)
        try: policy = self.manager.store.update_policy()
        except Exception:
            self.changed('policy_error'); self.phase, self.reason = 'unavailable', 'policy_error'; return
        with self.manager.lock:
            candidate = self.candidate
            if not self.history_valid:
                self.phase, self.reason = 'unavailable', 'policy_error'; return
            if self.active:
                if policy != self.active.candidate.policy: self.changed()
                return
            if not policy['update_checks'] or not policy['update_auto_download']:
                self.candidate = None; self.phase, self.reason = 'off', 'checks_disabled' if not policy['update_checks'] else 'disabled'; return
            if candidate is None: return
            if candidate.owner is not owner or owner.is_set(): return
            if candidate.policy != policy or time.monotonic() - candidate.observed > FRESH_SECONDS:
                self.candidate = None; self.needs_check = True; self.phase, self.reason = 'waiting', 'fresh_check_required'; return
            # The checker is still the operation owner until its thread exits.
            if self.manager.thread and self.manager.thread.is_alive(): return
            if self.history.get('candidate') == candidate.identity:
                if self.history.get('suppressed'):
                    self.phase, self.reason = 'cancelled', 'user_cancelled'; return
                if self.history.get('attempt_count', 0) >= MAX_ATTEMPTS:
                    self.phase, self.reason = 'unavailable', 'retry_exhausted'; return
                if self.history.get('terminal') is True:
                    self.phase, self.reason = 'unavailable', 'verification_failed'; return
                if self.history.get('not_before', 0) - now > MAX_RETRY_DELAY:
                    self._history(not_before=now + MAX_RETRY_DELAY)
                if now < self.history.get('not_before', 0):
                    self.phase = 'retry_wait'; return
        try: self.manager._automatic_download(candidate, now)
        except AutomaticRejected as exc:
            with self.manager.lock:
                if self.candidate is not candidate and self.manager.state.get('verified') is True: return
                self.phase = 'cancelled' if exc.reason == 'user_cancelled' else 'unavailable' if exc.reason in ('retry_exhausted', 'verification_failed', 'policy_error') else 'waiting'
                self.reason = exc.reason
        except Exception:
            with self.manager.lock: self.phase, self.reason = 'unavailable', 'verification_failed'

    def watch(self, lease):
        while not lease.finished.wait(.5):
            try:
                with self.admission(): self.validate(lease)
            except AutomaticRejected as exc:
                self.changed(exc.reason, lease); return
            except Exception:
                self.changed('policy_error', lease); return

    def finished(self, lease, error=None):
        lease.finished.set()
        with self.manager.lock:
            if self.active is not lease: return
            self.active = None
            if error is None:
                self.phase, self.reason = ('cancelled', lease.reason or 'stopped') if lease.cancel.is_set() else ('ready', 'verified')
                if self.candidate is lease.candidate: self.candidate = None
                return
            reason = lease.reason if lease.cancel.is_set() else failure_category(error)
            if reason not in REASONS: reason = 'verification_failed'
            attempts = self.history.get('attempt_count', 0)
            if reason in ('offline', 'rate_limited', 'server_unavailable') and attempts < MAX_ATTEMPTS:
                now = int(time.time())
                self._history(not_before=now + retry_delay(error, attempts, now))
                self.phase = 'retry_wait'
            else:
                if not lease.cancel.is_set(): self._history(terminal=True)
                self.phase = 'cancelled' if lease.cancel.is_set() else 'unavailable'
            self.reason = reason
