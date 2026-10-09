"""Coordinator-owned command processes, bounded output and restart-safe identities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from uuid import uuid4

from project_tools import ProjectTools, _WindowsProcessJob

MAX_LOG_BYTES = 16 * 1024 * 1024
LIVE_LIMIT = 8


class CommandSessionManager:
    def __init__(self, data_dir):
        self.home = Path(data_dir).resolve()
        self.root = self.home / 'state' / 'commands'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self._volatile_status = {}
        self.closed = False
        for path in self.root.glob('*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record.get('status') in ('starting', 'running', 'stopping'):
                    record.update(status='interrupted', recovery='Coordinator restarted; prior process identity is not adopted. Inspect effects before starting again.')
                    self._save(record)
            except (OSError, ValueError):
                continue

    def _save(self, record):
        record['updated_at'] = time.time()
        path = self.root / (record['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)

    def _record(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('Invalid command session ID.')
        try:
            return json.loads((self.root / (identifier + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('Command session was not found.') from None

    def start(self, project_root, argv, cwd='.', run_id='', purpose='command', owner_id='', max_seconds=3600,
              source_key=None, cancel=None):
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(not isinstance(a, str) or '\x00' in a or len(a) > 16384 for a in argv):
            raise ValueError('Provide an executable argv array with 1–128 text arguments.')
        if not argv[0].strip() or sum(map(len, argv)) > 65536:
            raise ValueError('Command is empty or too large.')
        if type(max_seconds) not in (int, float) or not .1 <= max_seconds <= 86400:
            raise ValueError('Command lifetime must be 0.1–86,400 seconds.')
        if purpose not in ('command', 'preview'):
            raise ValueError('Unknown command purpose.')
        project = ProjectTools(project_root, self.home / 'backups')
        working = project._path(cwd, directory=True)
        with self.lock:
            if self.closed:
                raise ValueError('Command manager has stopped.')
            if cancel is not None and cancel.is_set():
                return {'not_executed': True, 'cancelled': True, 'error': 'Command cancelled before launch.'}
            if source_key:
                for path in self.root.glob('*.json'):
                    try:
                        previous = json.loads(path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    if previous.get('source_key') == source_key:
                        if previous.get('project_root') != str(project.root) or previous.get('argv') != argv:
                            raise ValueError('Command identity is already bound to another action.')
                        return {**previous, 'reused': True}
            if len(self.jobs) >= LIVE_LIMIT:
                raise ValueError('Eight command sessions are already active; wait or stop one.')
            identifier = uuid4().hex
            record = {'id': identifier, 'version': 1, 'run_id': run_id, 'owner_id': owner_id,
                'project_root': str(project.root), 'cwd': str(working), 'argv': list(argv), 'purpose': purpose,
                'max_seconds': max_seconds, 'source_key': source_key, 'status': 'starting', 'created_at': time.time(),
                'exit_code': None, 'output_bytes': 0, 'truncated': False}
            self._save(record)
            options = {'cwd': str(working), 'shell': False, 'stdin': subprocess.DEVNULL,
                'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options['start_new_session'] = True
            try:
                process = subprocess.Popen(argv, **options)
            except OSError as exc:
                record.update(status='failed', error=str(exc)[:1000])
                self._save(record)
                return {**record, 'not_executed': True}
            try:
                process_job = _WindowsProcessJob(process)
            except Exception as exc:
                ProjectTools._terminate_process(process)
                record.update(status='interrupted',error='Process ownership could not be established.',outcome_unknown=True)
                try: self._save(record)
                except OSError: self._volatile_status[identifier]=dict(record)
                return dict(record)
            record.update(status='running', pid=process.pid)
            job = {'process': process, 'process_job': process_job, 'stop': threading.Event(),
                   'done': threading.Event(), 'record': record, 'bytes': 0}
            try:
                self._save(record)
            except OSError:
                self._terminate(job)
                record.update(status='interrupted',error='Command started but its running state could not be saved.',outcome_unknown=True,persistence_error=True)
                self._volatile_status[identifier]=dict(record)
                return dict(record)
            self.jobs[identifier] = job
            thread = threading.Thread(target=self._watch, args=(identifier, job), daemon=True, name='Forge-command-' + identifier[:8])
            job['thread'] = thread
            try:
                thread.start()
            except RuntimeError:
                self._terminate(job); self.jobs.pop(identifier,None)
                record.update(status='interrupted',error='Command monitoring could not start.',outcome_unknown=True)
                try: self._save(record)
                except OSError: self._volatile_status[identifier]=dict(record)
                return dict(record)
            return dict(record)

    @staticmethod
    def _terminate(job):
        process = job['process']
        job['process_job'].close()
        if os.name=='nt' and process.poll() is None:
            ProjectTools._terminate_process(process)
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill()
            return
        if os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        if os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _watch(self, identifier, job):
        log = self.root / (identifier + '.log')
        def drain():
            try:
                with log.open('wb') as handle:
                    while True:
                        chunk = os.read(job['process'].stdout.fileno(), 4096)
                        if not chunk:
                            break
                        written = min(len(chunk), max(0, MAX_LOG_BYTES - job['bytes']))
                        if written:
                            handle.write(chunk[:written])
                            handle.flush()
                        job['bytes'] += len(chunk)
            except (OSError, ValueError):
                pass
        reader = threading.Thread(target=drain, daemon=True, name='Forge-command-output')
        reader.start()
        deadline = time.monotonic() + job['record']['max_seconds']
        timed_out = False
        try:
            while job['process'].poll() is None:
                if job['stop'].wait(.05) or time.monotonic() >= deadline:
                    timed_out = not job['stop'].is_set()
                    self._terminate(job)
                    break
            job['process'].wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._terminate(job)
        finally:
            # The manager owns descendants even if their parent exited first.
            self._terminate(job)
            reader.join(timeout=5)
            if not reader.is_alive(): job['process'].stdout.close()
            with self.lock:
                record = job['record']
                record.update(status='timed_out' if timed_out else 'stopped' if job['stop'].is_set() else 'completed',
                    exit_code=job['process'].poll(), output_bytes=job['bytes'], truncated=job['bytes'] > MAX_LOG_BYTES,
                    ended_at=time.time())
                try:
                    self._save(record)
                except OSError:
                    record['persistence_error']=True
                    self._volatile_status[identifier]=dict(record)
                finally:
                    self.jobs.pop(identifier, None)
                    job['done'].set()

    def status(self, identifier):
        with self.lock:
            record = dict(self._volatile_status[identifier]) if identifier in self._volatile_status else self._record(identifier)
            job = self.jobs.get(identifier)
            if job:
                record.update(output_bytes=job['bytes'], truncated=job['bytes'] > MAX_LOG_BYTES)
            return record

    def read(self, identifier, start=0, limit=8000):
        record = self.status(identifier)
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 64000:
            raise ValueError('Output start must be nonnegative and limit 1–64,000 bytes.')
        path = self.root / (identifier + '.log')
        data = b''
        if path.is_file():
            with path.open('rb') as handle:
                handle.seek(start)
                data = handle.read(limit)
        return {'id': identifier, 'status': record['status'], 'output': data.decode('utf-8', errors='replace'),
                'start': start, 'next': start + len(data), 'next_start': start + len(data), 'has_more': min(record['output_bytes'], MAX_LOG_BYTES) > start + len(data),
                'exit_code': record['exit_code'], 'truncated': record['truncated']}

    def wait(self, identifier, timeout=10, cancel=None):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError('Wait must be 0–30 seconds.')
        with self.lock:
            self._record(identifier)
            job = self.jobs.get(identifier)
        deadline = time.monotonic() + timeout
        while job and not job['done'].is_set() and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                break
            job['done'].wait(min(.1, max(0, deadline-time.monotonic())))
        return self.status(identifier)

    def stop(self, identifier):
        with self.lock:
            self._record(identifier)
            job = self.jobs.get(identifier)
            if job:
                job['stop'].set()
        if job:
            job['done'].wait(8)
        return self.status(identifier)

    def shutdown(self):
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
            for job in jobs:
                job['stop'].set()
        for job in jobs:
            job['done'].wait(8)
