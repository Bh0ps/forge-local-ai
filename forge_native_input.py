"""Memory-only native input readiness; no transcript, audio or draft digest."""
from contextlib import contextmanager
import re
import secrets
import threading


TOKEN = re.compile(r'[0-9a-f]{64}\Z')
ACTIVE = frozenset({'starting', 'recording', 'stopping', 'transcribing', 'installing'})


class NativeInputGuard:
    def __init__(self, dictation, *, limit=4096):
        self.dictation, self.limit = dictation, limit
        self.lock = threading.RLock()
        self.local = threading.local()
        self.pending = 0
        self.unknown = False
        self.jobs = {}
        self.deliveries = {}
        self.observer = self._observe
        dictation.bind_job_observer(self.observer)

    @contextmanager
    def operation(self, scope=None):
        # Caller admits this producer under service.admission_lock before
        # releasing that lock for native/model work. Backend probes only read.
        with self.lock:
            self.pending += 1
        previous = getattr(self.local, 'scope', None)
        self.local.scope = dict(scope) if isinstance(scope, dict) else None
        try:
            yield
        finally:
            self.local.scope = previous
            with self.lock:
                self.pending -= 1

    def _observe(self, event, job, nonempty=False):
        # Dictation holds its own callback lock. This callback is memory-only;
        # no method holding this lock calls back into Dictation.
        with self.lock:
            if event == 'admitted':
                if len(self.jobs) >= self.limit:
                    self.unknown = True
                    raise ValueError('Native input limit reached.')
                self.jobs[job] = {'scope': getattr(self.local, 'scope', None), 'token': None, 'consumed': False}
            elif event == 'completed' and nonempty:
                record = self.jobs.get(job)
                if record is None or record['scope'] is None:
                    self.unknown = True
                    return
                token = secrets.token_hex(32)
                record['token'] = token
                self.deliveries[token] = record
            elif event == 'discarded' and job in self.jobs:
                self.jobs[job]['consumed'] = True

    def decorate(self, result):
        value = dict(result)
        with self.lock:
            record = self.jobs.get(value.get('id'))
            if value.get('state') == 'done' and record and record['token']:
                value['delivery_id'] = record['token']
        return value

    def confirm(self, snapshot, identifiers):
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('scope'), dict)
                or not isinstance(identifiers, tuple) or len(identifiers) > 16
                or len(set(identifiers)) != len(identifiers)
                or any(not isinstance(token, str) or not TOKEN.fullmatch(token) for token in identifiers)):
            return False
        with self.lock:
            records = [self.deliveries.get(token) for token in identifiers]
            if any(record is None or record['scope'] != snapshot['scope'] for record in records):
                return False
            # Atomic subset confirmation: a bad ID never consumes a good one.
            for record in records:
                record['consumed'] = True
            return True

    def idle(self):
        if self.dictation.native_idle() is not True:
            return False
        with self.lock:
            return (self.pending == 0 and not self.unknown
                    and all(record['consumed'] for record in self.deliveries.values()))

    def discard_chat(self, chat_id):
        """Called only after the coordinator commits an explicit chat deletion."""
        if not isinstance(chat_id, str) or not chat_id:
            return False
        with self.lock:
            jobs = tuple(job for job, record in self.jobs.items()
                if record['scope'] is not None and record['scope'].get('kind') == 'chat'
                and record['scope'].get('chat_id') == chat_id)
            for job in jobs:
                self.jobs[job]['consumed'] = True
            if not jobs:
                return True
            # Admit this owned stop before releasing the coordinator gate. Its
            # capture join runs off that gate; readiness remains false until
            # the exact matching job stop has finished.
            self.pending += 1
        def cancel():
            try:
                for job in jobs:
                    self.dictation.cancel(job)
            except Exception:
                with self.lock: self.unknown = True
            finally:
                with self.lock: self.pending -= 1
        threading.Thread(target=cancel, name='Forge-deleted-chat-dictation', daemon=True).start()
        return True
