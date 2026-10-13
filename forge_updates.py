"""Fail-closed Windows updates. GitHub hashes establish integrity, not identity."""
from __future__ import annotations

from hashlib import sha256
import asyncio
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
import psutil

from forge_store import TERMINAL, atomic_text, encode

REPOSITORY = 'Bh0ps/forge-local-ai'
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
UPDATE_CHANNELS = ('stable', 'preview')
MAX_RELEASE_PAGES = 3
try:
    # Generated in the protected release build BEFORE PyInstaller/signing. This
    # module is compiled into the executable, never loaded from a user setting.
    from forge_release_policy import TRUSTED_PUBLISHERS
except ImportError:
    TRUSTED_PUBLISHERS = ()


@contextmanager
def bounded_lock(lock, description):
    if lock is None:
        yield
        return
    if not lock.acquire(timeout=12):
        raise ValueError(description + ' is still busy. Finish or stop it before updating.')
    try: yield
    finally: lock.release()


def version(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match:
        raise ValueError('Only major.minor.patch release tags are supported.')
    return tuple(map(int, match.groups()))


def update_channel(value):
    if value not in UPDATE_CHANNELS:
        raise ValueError('Choose the stable or preview update channel.')
    return value


def release_metadata(metadata):
    """Keep only the exact public installer identity; never retain release prose."""
    if not isinstance(metadata, dict):
        raise ValueError('Invalid release metadata.')
    tag = metadata.get('tag_name')
    number = '.'.join(map(str, version(tag)))
    if type(metadata.get('draft')) is not bool or type(metadata.get('prerelease')) is not bool:
        raise ValueError('Invalid release publication state.')
    if metadata['draft']:
        raise ValueError('Only published releases are supported.')
    page = 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag
    if metadata.get('html_url') != page:
        raise ValueError('Release metadata refers to a different repository.')
    filename = 'Forge-' + number + '-Setup.exe'
    assets = metadata.get('assets')
    if not isinstance(assets, list):
        raise ValueError('Invalid release assets.')
    candidates = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == filename]
    if len(candidates) != 1:
        raise ValueError('Release must contain exactly one matching Windows installer.')
    asset = candidates[0]
    expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + filename
    digest = asset.get('digest', '')
    if asset.get('browser_download_url') != expected or not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Installer URL or GitHub SHA256 digest is missing or invalid.')
    size = asset.get('size')
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES:
        raise ValueError('Invalid installer size.')
    return {'version': number, 'tag': tag, 'name': filename, 'html_url': page,
            'url': expected, 'sha256': digest[7:], 'size': size, 'prerelease': metadata['prerelease']}


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value or '\x00' in value:
        raise ValueError('Invalid package path.')
    parts = tuple(value.split('/'))
    if not parts or PurePosixPath(value).is_absolute() or any(part in ('..', '.', '') for part in parts):
        raise ValueError('Package path escapes its root.')
    if any(part.lower().rstrip(' .') != part.lower() or part.lower() in ('plugins', '.forge', '.sidekick') for part in parts):
        raise ValueError('Package contains an invalid path or executable plugin folder.')
    if any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part) for part in parts):
        raise ValueError('Reserved Windows package path.')
    return Path(*parts)


def regular_tree(root):
    root = plain_path(root)
    if not root.is_dir():
        raise ValueError('Installation folder is unavailable.')
    values, names = [], set()
    for path in root.rglob('*'):
        relative = path.relative_to(root).as_posix()
        safe_relative(relative)
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
        if path.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('Symbolic links and junctions are not permitted in update packages.')
        key = relative.casefold()
        if key in names:
            raise ValueError('Duplicate package paths.')
        names.add(key)
        if path.is_file():
            values.append({'path': relative, 'sha256': file_hash(path), 'size': path.stat().st_size})
    return sorted(values, key=lambda entry: entry['path'])


def plain_path(value):
    """Reject links in every existing component before resolving a target."""
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
            if part.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Update locations cannot contain symbolic links or junctions.')
    return path.resolve()


def verify_tree(root, manifest):
    actual = regular_tree(root)
    wanted = {entry['path']: entry for entry in manifest}
    if len(wanted) != len(manifest) or {v['path']: v for v in actual} != wanted:
        raise ValueError('Installation backup was modified or contains unexpected files.')
    return actual


ROLLBACK_DATA_FOLDERS = ('config', 'attachments', 'state/goals')


def database_guard(path, baseline=None):
    """Hash logical user state, tolerating additive schema-only migrations.

    Updater bookkeeping is deliberately excluded; invocation/message/run rows,
    settings and all other user state remain covered. No row content is retained
    in the operation journal.
    """
    def quoted(value): return '"' + value.replace('"', '""') + '"'
    tables = {}
    with sqlite3.connect(path) as db:
        names = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='forge_migrations'"))
        wanted = (baseline or {}).get('tables', {})
        for name in sorted(set(names) | set(wanted)):
            if name not in names:
                tables[name] = {'missing': True}
                continue
            present = [row[1] for row in db.execute('PRAGMA table_info(' + quoted(name) + ')')]
            columns = wanted[name]['columns'] if name in wanted else present
            if any(column not in present for column in columns):
                tables[name] = {'missing_columns': True}
                continue
            select = ','.join(quoted(column) for column in columns)
            sql = 'SELECT ' + select + ' FROM ' + quoted(name)
            if name == 'entities': sql += " WHERE kind!='updates'"
            sql += ' ORDER BY ' + select
            digest = sha256(); count = 0
            for row in db.execute(sql):
                values = [{'blob': value.hex()} if isinstance(value, bytes) else value for value in row]
                digest.update(encode(values).encode('utf-8')); digest.update(b'\n'); count += 1
            # A newly introduced empty table is an additive migration, not work.
            if baseline is not None and name not in wanted and not count: continue
            tables[name] = {'columns': columns, 'rows': count, 'sha256': digest.hexdigest()}
    return {'version': 1, 'tables': tables}


def retain_rollback_state(home, data):
    """Take a current consistent snapshot before deciding whether rewind is safe."""
    backup = plain_path(data['backup'])
    current = backup / ('post-update-state-' + uuid4().hex)
    current.mkdir()
    with sqlite3.connect(home / 'state/forge.sqlite3') as source, sqlite3.connect(current / 'forge.sqlite3') as destination:
        source.backup(destination)
    for folder in ROLLBACK_DATA_FOLDERS:
        source = home / folder
        if source.exists():
            regular_tree(source)
            shutil.copytree(source, current / 'data' / folder)
    before = data.get('rollback_guard') or database_guard(backup / 'forge.sqlite3')
    database_changed = database_guard(current / 'forge.sqlite3', before) != before
    files_changed = any(
        (regular_tree(current / 'data' / folder) if (current / 'data' / folder).exists() else []) !=
        (regular_tree(backup / 'data' / folder) if (backup / 'data' / folder).exists() else [])
        for folder in ROLLBACK_DATA_FOLDERS)
    if database_changed or files_changed:
        data.update(state='rollback_blocked', current_snapshot=str(current),
                    error='Rollback would rewind newer user work. Current code and state were preserved. Inspect the retained snapshot and repair manually; no prior actions were resumed.')
        atomic_text(home / 'runtimes/updates/operations' / (data['id'] + '.json'), encode(data))
        raise ValueError(data['error'] + ' Snapshot: ' + str(current))
    return current


def authenticode(path):
    if os.name != 'nt':
        raise ValueError('Windows Authenticode verification is required before installation.')
    # A fixed program reads the path from an environment variable; no shell
    # interpolation, URLs, user-supplied arguments or execution of the package.
    script = "$s=Get-AuthenticodeSignature -LiteralPath $env:FORGE_VERIFY_FILE; $v=(Get-Item -LiteralPath $env:FORGE_VERIFY_FILE).VersionInfo; @{status=[string]$s.Status; publisher=$s.SignerCertificate.Subject; thumbprint=$s.SignerCertificate.Thumbprint; issuer=$s.SignerCertificate.Issuer; version=$v.ProductVersion; timestamped=($null -ne $s.TimeStamperCertificate)}|ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env={**os.environ, 'FORGE_VERIFY_FILE': str(Path(path).resolve())}, capture_output=True,
        text=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('Windows could not verify this installer signature.')
    return json.loads(result.stdout)


def require_trust(path, expected_version, publishers, verifier=authenticode):
    if not publishers:
        raise ValueError('Automatic installation is disabled until a verified publisher is embedded in a signed Forge release.')
    result = verifier(path)
    if result.get('status') != 'Valid' or result.get('publisher') not in publishers or not result.get('timestamped'):
        raise ValueError('Installer has no valid timestamped signature from the trusted Forge publisher.')
    reported = str(result.get('version', '')).strip()
    if reported.endswith('.0') and len(reported.split('.')) == 4:
        reported = reported[:-2]
    if version(reported) != version(expected_version):
        raise ValueError('Signed installer version differs from release metadata.')
    return {key: result.get(key) for key in ('status', 'publisher', 'issuer', 'thumbprint', 'version', 'timestamped')}


def _client():
    return httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,
        headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})


async def _get_bytes(client, url, limit, cancel=None, progress=None, consume=None):
    # Follow GitHub's asset redirect only to its official download CDN; hashes
    # and publisher validation still apply to the final bytes.
    async def fetch():
      target_url = url
      for _ in range(4):
        async with client.stream('GET', target_url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                next_url = httpx.URL(response.headers.get('location', ''))
                if next_url.scheme != 'https' or next_url.host not in (
                    'github.com', 'api.github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                    raise ValueError('Release download redirected to an untrusted host.')
                target_url = str(next_url)
                continue
            response.raise_for_status()
            count = 0
            async for chunk in response.aiter_bytes(1024 * 128):
                if cancel is not None and cancel.is_set():
                    raise ValueError('Update download cancelled.')
                count += len(chunk)
                if count > limit:
                    raise ValueError('Release payload exceeds its allowed size.')
                if progress: progress(count)
                if consume: consume(chunk)
            return count
      raise ValueError('Too many release download redirects.')
    task = asyncio.create_task(fetch())
    async def cancelled():
        while cancel is None or not cancel.is_set(): await asyncio.sleep(.05)
    watcher = asyncio.create_task(cancelled()) if cancel is not None else None
    try:
        if watcher:
            await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if cancel.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ValueError('Update download cancelled.')
        return await task
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


class UpdateManager:
    def __init__(self, service, current_version='5.1.1', install_dir=None, trust_policy=None,
                 client_factory=None, verifier=authenticode):
        self.service, self.store = service, service.store
        self.home = self.store.home
        self.root = self.home / 'runtimes/updates'
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_version = current_version
        version(current_version)
        self.install_dir = Path(install_dir).resolve() if install_dir else (
            Path(sys.executable).parent.resolve() if getattr(sys, 'frozen', False) else None)
        self.publishers = tuple(TRUSTED_PUBLISHERS if trust_policy is None else trust_policy)
        self.client_factory = client_factory or _client
        self.verifier = verifier
        self.lock, self.cancel_event = threading.RLock(), threading.Event()
        # Only admission flags/thread starts use this lock. SQLite/filesystem
        # work stays outside it so host shutdown never waits on slow state I/O.
        self.admission_lock = threading.RLock()
        self.shutting_down = False
        self.thread = None
        self.thread_stop_event = None
        self.monitor = None
        self.applying = False
        self.inspection_required = False
        try: self.state = self.store.entity('updates', 'current')
        except ValueError: self.state = {'id': 'current', 'state': 'idle'}
        if self.state.get('state') in ('checking', 'downloading'):
            self._save(state='interrupted', verified=False,
                       error='The previous update operation stopped. Check again.')
        if self.state.get('operation_id'):
            try:
                operation = load_operation(self.home, self.state['operation_id'])
                if operation['state'] in ('installed', 'rolled_back'):
                    self._save(state=operation['state'], verified=False)
                elif operation['state'] in ('applying', 'rollback_blocked'):
                    self.inspection_required = True
                    self._save(state='interrupted', verified=False,
                        error=operation.get('error') or 'The previous installer outcome is unknown. Inspect the retained installer and backup, then repair Forge manually. Automatic retry is blocked.')
            except (ValueError, OSError, KeyError):
                self.inspection_required = True
                self._save(state='error', error='Update journal could not be verified. Inspect the retained backups.')
        if self.state.get('state') in ('available', 'ready'):
            try:
                if version((self.state.get('release') or {}).get('version')) <= version(self.current_version):
                    self._save(state='current', release=None, downloaded=None, signature=None,
                               verified=False, received_bytes=0)
            except ValueError:
                self._save(state='error', verified=False, error='Stored release metadata is invalid. Check again.')

    def _save(self, **data):
        with self.lock:
            self.state = self.store.save_entity('updates', {**self.state, **data, 'id': 'current'})
            return self.status()

    def status(self):
        with self.lock:
            return {**self.state, 'current_version': self.current_version, 'repository': REPOSITORY,
                'channel': self.state.get('channel') if self.state.get('channel') in UPDATE_CHANNELS else 'stable',
                'inspection_required': self.inspection_required,
                'automatic_install_available': bool(self.publishers and self.install_dir and not self.inspection_required),
                'signing_setup_required': not bool(self.publishers),
                'message': 'The previous installer outcome is unknown. Inspect the retained backup and repair Forge manually before updating.' if self.inspection_required else
                    'Updates require a valid timestamped Windows signature from the approved publisher.' if self.publishers else
                    'Signing setup is required. Unsigned releases can be installed manually; automatic installation stays disabled.'}

    def _begin_check(self, channel):
        with self.lock:
            with self.admission_lock:
                if self.shutting_down: raise ValueError('The updater is shutting down.')
            if self.applying or self.state.get('state') in ('checking', 'downloading') or self.thread and self.thread.is_alive(): raise ValueError('An update operation is already active.')
            previous = dict(self.state)
            self.cancel_event.clear()
            self._save(state='checking', error=None, channel=channel, last_check_failed=False)
            return previous

    def check(self, channel='stable'):
        channel = update_channel(channel)
        return self._check(channel, self._begin_check(channel))

    def check_background(self, channel='stable', *, stop_event=None):
        """Reserve the operation before returning; HTTP never blocks the UI call."""
        channel = update_channel(channel)
        with self.lock:
            with self.admission_lock:
                if self.shutting_down or stop_event is not None and stop_event.is_set():
                    raise ValueError('The update check is stopping.')
            previous = self._begin_check(channel)
            def worker():
                try: self._check(channel, previous)
                except Exception: pass  # Sanitized failure is already persisted.
            with self.admission_lock:
                stopped = self.shutting_down or stop_event is not None and stop_event.is_set()
                if not stopped:
                    self.thread = threading.Thread(target=worker, daemon=True, name='Forge-update-check')
                    self.thread_stop_event = stop_event
                    self.thread.start()
            if stopped:
                self._save(state='cancelled', verified=False, error='Update check cancelled.')
                raise ValueError('The update check is stopping.')
            return self.status()

    def _check(self, channel, previous):
        try:
            if self.shutting_down or self.cancel_event.is_set(): raise ValueError('Update check cancelled.')
            async def fetch_metadata():
                async with self.client_factory() as client:
                    base = 'https://api.github.com/repos/' + REPOSITORY + '/releases'
                    if channel == 'stable':
                        chunks = []
                        await _get_bytes(client, base + '/latest', 1024 * 1024,
                                         self.cancel_event, consume=chunks.append)
                        metadata = json.loads(b''.join(chunks))
                        if not isinstance(metadata, dict): raise ValueError('Invalid release metadata.')
                        if metadata.get('draft') or metadata.get('prerelease'): raise ValueError('Only published stable releases are supported.')
                        version(metadata.get('tag_name'))
                        return metadata
                    candidates = []
                    for page in range(1, MAX_RELEASE_PAGES + 1):
                        chunks = []
                        await _get_bytes(client, base + '?per_page=100&page=' + str(page), 1024 * 1024,
                                         self.cancel_event, consume=chunks.append)
                        values = json.loads(b''.join(chunks))
                        if not isinstance(values, list) or len(values) > 100:
                            raise ValueError('Invalid release feed.')
                        for value in values:
                            if not isinstance(value, dict): raise ValueError('Invalid release metadata.')
                            if value.get('draft') is True: continue
                            try: version(value.get('tag_name'))
                            except ValueError: continue  # Other tag formats are never executable candidates.
                            candidates.append(value)
                        if len(values) < 100:
                            if not candidates: return None
                            newest = max(version(value['tag_name']) for value in candidates)
                            matching = [value for value in candidates if version(value['tag_name']) == newest]
                            if len(matching) != 1: raise ValueError('Release feed has ambiguous version identities.')
                            return matching[0]
                    raise ValueError('Release history exceeds the safe check limit. View the public releases instead.')
            metadata = asyncio.run(fetch_metadata())
            if metadata is not None:
                if type(metadata.get('draft')) is not bool or type(metadata.get('prerelease')) is not bool or metadata['draft']:
                    raise ValueError('Invalid release publication state.')
                if metadata.get('html_url') != 'https://github.com/' + REPOSITORY + '/releases/tag/' + metadata['tag_name']:
                    raise ValueError('Release metadata refers to a different repository.')
            if metadata is None or version(metadata.get('tag_name')) <= version(self.current_version):
                return self._finish_check(state='current', release=None, downloaded=None, signature=None,
                                          verified=False, received_bytes=0)
            release = release_metadata(metadata)
            # A periodic check of the same release preserves its verified bytes.
            identity = ('version', 'tag', 'name', 'url', 'sha256', 'size')
            same = all((previous.get('release') or {}).get(key) == release[key] for key in identity)
            staged = previous.get('state') == 'ready' and previous.get('verified') is True and same
            return self._finish_check(state='ready' if staged else 'available', release=release,
                downloaded=previous.get('downloaded') if staged else None,
                signature=previous.get('signature') if staged else None,
                verified=staged, received_bytes=release['size'] if staged else 0)
        except Exception as exc:
            # A failed feed check must not leave an old Install button armed.
            cancelled = self.cancel_event.is_set()
            self._save(state='cancelled' if cancelled else 'error',
                       error='Update check cancelled.' if cancelled else str(exc)[:400], verified=False,
                       last_check_failed=not cancelled, checked_at=int(time.time())); raise

    def _finish_check(self, **data):
        with self.lock:
            if self.cancel_event.is_set(): raise ValueError('Update check cancelled.')
            return self._save(**data, last_check_failed=False, checked_at=int(time.time()))

    def download(self):
        with self.lock:
            with self.admission_lock:
                if self.shutting_down: raise ValueError('The updater is shutting down.')
            if self.inspection_required: raise ValueError('Inspect and repair the previous update before downloading another automatic installer.')
            if not self.publishers: raise ValueError('Automatic download is disabled until a verified publisher is embedded. Install unsigned previews manually.')
            if self.applying or self.state.get('state') == 'checking' or self.thread and self.thread.is_alive(): raise ValueError('An update operation is already active.')
            release = self._release()
            self.cancel_event.clear()
            self._save(state='downloading', received_bytes=0, error=None, verified=False)
            with self.admission_lock:
                stopped = self.shutting_down
                if not stopped:
                    self.thread = threading.Thread(target=self._download, args=(release,), daemon=True, name='Forge-update-download')
                    self.thread_stop_event = None
                    self.thread.start()
            if stopped:
                self._save(state='cancelled', verified=False, error='Update download cancelled.')
                raise ValueError('The updater is shutting down.')
            return self.status()

    def _download(self, release):
        directory = self.root / uuid4().hex
        directory.mkdir()
        partial, complete = directory / 'installer.part', directory / 'installer.exe'
        try:
            with partial.open('xb') as target:
                async def fetch_package():
                    async with self.client_factory() as client:
                        await _get_bytes(client, release['url'], release['size'], self.cancel_event,
                                         lambda count: self._save(received_bytes=count), target.write)
                asyncio.run(fetch_package())
                target.flush(); os.fsync(target.fileno())
            if self.cancel_event.is_set(): raise ValueError('Update download cancelled.')
            if partial.stat().st_size != release['size'] or file_hash(partial) != release['sha256']:
                raise ValueError('Installer failed SHA256 or size verification. Nothing was installed.')
            signature = require_trust(partial, release['version'], self.publishers, self.verifier)
            with self.lock:
                if self.cancel_event.is_set(): raise ValueError('Update download cancelled.')
                os.replace(partial, complete)
                self._save(state='ready', downloaded=str(complete), verified=True, signature=signature)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            self._save(state='cancelled' if self.cancel_event.is_set() else 'error', error=str(exc)[:400], downloaded=None, verified=False)

    def cancel(self):
        with self.lock:
            self.cancel_event.set()
        return {'ok': True}

    def _idle(self):
        if self.inspection_required:
            raise ValueError('The previous update outcome needs inspection. Repair Forge manually before another install or rollback.')
        if any(getattr(job.get('thread'), 'is_alive', lambda: False)() for job in getattr(self.service.jobs, 'jobs', {}).values()):
            raise ValueError('Wait for agent and memory workers to finish before updating.')
        for run in self.store.runs():
            if run['status'] not in TERMINAL: raise ValueError('Pause active chats and agents before updating.')
            if self.store.unknown_actions(run['id']): raise ValueError('Resolve unknown action outcomes before updating.')
        for name in ('performance_manager', 'model_manager', 'setup_manager'):
            manager = getattr(self.service, name, None)
            # ModelManager.jobs is durable history; active owns its workers.
            active = getattr(manager, 'active', {}) if name == 'model_manager' else getattr(manager, 'jobs', {})
            if active: raise ValueError('Finish active model operations before updating.')
        runtime = getattr(self.service, 'runtime', None)
        if runtime and getattr(runtime, '_validation', {}).get('state') == 'running':
            raise ValueError('Finish runtime profile validation before updating.')
        with self.store._connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('channel_inbound','channel_outbox'):
                if table in tables and db.execute('SELECT 1 FROM ' + table + " WHERE status IN ('sending','outcome_unknown') LIMIT 1").fetchone():
                    raise ValueError('Inspect unknown connected-channel outcomes before updating.')
        dictation = getattr(self.service, 'host_dictation', None)
        if dictation and dictation.status()['state'] in ('starting', 'recording', 'stopping', 'transcribing', 'installing'):
            raise ValueError('Finish dictation before updating.')

    @contextmanager
    def _quiesce(self):
        # Channel ticks hold channel.lock before jobs.lock. Never invert that
        # order while draining external delivery and ingress before snapshot.
        with ExitStack() as locks:
            locks.enter_context(bounded_lock(getattr(self.service, 'admission_lock', None), 'Workspace'))
            channel = getattr(self.service, 'channel_manager', None)
            locks.enter_context(bounded_lock(getattr(channel, 'lock', None), 'Connected channel'))
            locks.enter_context(bounded_lock(self.service.jobs.lock, 'Agent coordinator'))
            locks.enter_context(bounded_lock(self.lock, 'Updater'))
            yield

    def _package(self):
        if self.state.get('state') in ('checking', 'downloading') or self.thread and self.thread.is_alive():
            raise ValueError('An update operation is already active.')
        release = self._release()
        path = plain_path(self.state.get('downloaded') or '')
        if not self.state.get('verified') or not path.is_relative_to(self.root.resolve()) or path.name != 'installer.exe' or not path.is_file():
            raise ValueError('Download and verify the installer first.')
        if version(release.get('version')) <= version(self.current_version): raise ValueError('A stale release cannot replace this version.')
        if file_hash(path) != release['sha256'] or path.stat().st_size != release['size']: raise ValueError('Staged installer was modified.')
        require_trust(path, release['version'], self.publishers, self.verifier)
        return release, path

    def _release(self):
        release = dict(self.state.get('release') or {})
        number = '.'.join(map(str, version(release.get('version'))))
        if version(number) <= version(self.current_version): raise ValueError('Check for a newer release first.')
        tag = release.get('tag')
        if version(tag) != version(number): raise ValueError('Stored release tag differs from its version.')
        name = 'Forge-' + number + '-Setup.exe'
        expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + name
        if release.get('name') != name or release.get('url') != expected or not re.fullmatch(r'[0-9a-f]{64}', str(release.get('sha256', ''))):
            raise ValueError('Stored release metadata is invalid.')
        if type(release.get('size')) is not int or not 0 < release['size'] <= MAX_PACKAGE_BYTES: raise ValueError('Invalid installer size.')
        return release

    def apply(self):
        with self._quiesce():
            if self.applying: raise ValueError('An update is already prepared.')
            self._idle(); release, package = self._package()
            if not self.install_dir or not (self.install_dir / 'Forge.exe').is_file(): raise ValueError('Install Forge on Windows before using automatic updates.')
            for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
                require_trust(self.install_dir / name, self.current_version, self.publishers, self.verifier)
            # Quiesce guard is shared with the service dispatcher/run admission.
            self.applying = True
            try:
                identifier = uuid4().hex
                backup = self.home / 'backups' / ('update-' + identifier)
                backup.mkdir()
                previous = regular_tree(self.install_dir)
                shutil.copytree(self.install_dir, backup / 'installation')
                verify_tree(backup / 'installation', previous)
                with sqlite3.connect(self.store.db_path) as source, sqlite3.connect(backup / 'forge.sqlite3') as destination:
                    source.backup(destination)
                for folder in ROLLBACK_DATA_FOLDERS:
                    original = self.home / folder
                    if original.exists():
                        regular_tree(original)
                        shutil.copytree(original, backup / 'data' / folder)
                backup_files = regular_tree(backup)
                data = {'id': identifier, 'home': str(self.home.resolve()), 'install_dir': str(self.install_dir),
                    'previous_version': self.current_version, 'version': release['version'], 'installer': str(package),
                    'sha256': release['sha256'], 'size': release['size'], 'backup': str(backup),
                    'previous_files': previous, 'backup_files': backup_files,
                    'rollback_guard': database_guard(backup / 'forge.sqlite3'),
                    'state': 'prepared', 'parent_pid': os.getpid(),
                    'parent_created': psutil.Process().create_time()}
                intent = self.root / 'operations' / (identifier + '.json')
                atomic_text(intent, encode(data))
                self._save(state='prepared', operation_id=identifier)
                return {'prepared': True, 'operation_id': identifier, 'restart_required': True,
                    'command': [str(backup / 'installation/Forge.exe'), '--forge-update', identifier,
                        '--forge-home', str(self.home), '--forge-parent-pid', str(os.getpid())]}
            except BaseException:
                self.applying = False
                raise

    def rollback(self):
        # Rollback is an explicit user decision, and uses the same helper/quiesce
        # path. A committed rollback never executes a downloaded older installer.
        with self._quiesce():
            if self.applying: raise ValueError('An update is already prepared.')
            if self.state.get('state') in ('checking', 'downloading') or self.thread and self.thread.is_alive():
                raise ValueError('An update operation is already active.')
            self._idle()
            identifier = self.state.get('operation_id')
            data = load_operation(self.home, identifier)
            if data['state'] != 'installed': raise ValueError('No completed update is available to roll back.')
            verify_tree(Path(data['backup']) / 'installation', data['previous_files'])
            try: retain_rollback_state(self.home, data)
            except ValueError:
                if data.get('state') == 'rollback_blocked':
                    self.inspection_required = True
                    self._save(state='rollback_blocked', error=data['error'], current_snapshot=data['current_snapshot'])
                raise
            data['state'] = 'rollback_prepared'; data['parent_pid'] = os.getpid()
            data['parent_created'] = psutil.Process().create_time()
            atomic_text(self.root / 'operations' / (identifier + '.json'), encode(data))
            self.applying = True
            return {'prepared': True, 'operation_id': identifier, 'restart_required': True,
                'command': [str(Path(data['backup']) / 'installation/Forge.exe'), '--forge-update', identifier,
                    '--forge-home', str(self.home), '--forge-parent-pid', str(os.getpid())]}

    def diagnostics_export(self):
        # Allowlist rather than attempting to redact arbitrary logs or prompts.
        from datetime import datetime, timezone
        import platform
        settings = self.store.get_settings()
        data = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'version': self.current_version,
            'platform': {'system': platform.system(), 'release': platform.release(), 'architecture': platform.machine(),
                         'python': '.'.join(map(str, sys.version_info[:2]))},
            'configuration': {'context': settings.get('context') if type(settings.get('context')) is int else None,
                'tokens': settings.get('tokens') if type(settings.get('tokens')) is int else None,
                'thinking': settings.get('thinking') if type(settings.get('thinking')) is bool else None,
                'permission_profile': settings.get('permission_profile') if settings.get('permission_profile') in ('always_ask', 'full_access', 'deny_access') else None,
                'performance': settings.get('performance') if settings.get('performance') in ('balanced', 'fast', 'quality', 'custom') else None},
            'counts': {'projects': len(self.store.list_projects()), 'chats': len(self.store.list_chats()),
                       'runs': len(self.store.runs()), 'providers': len(self.store.entities('providers'))},
            'updates': {'state': self.state.get('state') if self.state.get('state') in ('idle', 'checking', 'downloading', 'interrupted', 'error', 'available', 'current', 'ready', 'cancelled', 'prepared', 'applying', 'installed', 'rolled_back') else 'unknown', 'publisher_configured': bool(self.publishers)},
            'privacy': 'No chats, prompts, attachments, identifiers, paths, hostnames, URLs, credentials or raw logs are included.'}
        artifact = self.store.artifact(data)
        return {'artifact': artifact, 'diagnostics': data}

    def dispatch(self, action, data=None):
        if action == 'update_status':
            result = self.status()
            result['operation_error'] = result.pop('error', None)
            return result
        if action == 'update_check':
            payload = data or {}
            background = payload.get('background', False)
            if type(background) is not bool: raise ValueError('Background checking must be enabled or disabled.')
            return (self.check_background if background else self.check)(channel=payload.get('channel', 'stable'))
        if action == 'update_download': return self.download()
        if action == 'update_cancel': return self.cancel()
        if action == 'update_apply': return self.apply()
        if action == 'update_rollback': return self.rollback()
        if action == 'diagnostics_export': return self.diagnostics_export()
        raise ValueError('Unknown update action.')

    def shutdown(self):
        with self.admission_lock:
            self.shutting_down = True
            self.cancel_event.set()
        if self.monitor: self.monitor.stop()
        if self.thread: self.thread.join(timeout=2)

    def start_monitor(self):
        with self.lock:
            with self.admission_lock:
                if self.shutting_down: raise ValueError('The updater is shutting down.')
            if self.monitor is None:
                from forge_update_monitor import UpdateMonitor
                self.monitor = UpdateMonitor(self)
            self.monitor.start()

    def stop_monitor(self, stop_event):
        with self.admission_lock:
            stop_event.set()
            self.cancel_monitor_check(stop_event)

    def cancel_monitor_check(self, stop_event):
        with self.admission_lock:
            if self.thread_stop_event is stop_event and self.thread and self.thread.is_alive():
                self.cancel_event.set()


def load_operation(home, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier): raise ValueError('Invalid update operation.')
    home = plain_path(home)
    path = plain_path(home / 'runtimes/updates/operations' / (identifier + '.json'))
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('id') != identifier or Path(data.get('home', '')).resolve() != home: raise ValueError('Update operation belongs to another workspace.')
    backup = plain_path(data.get('backup', ''))
    if backup != home / 'backups' / ('update-' + identifier): raise ValueError('Invalid rollback backup location.')
    package = plain_path(data.get('installer', ''))
    if not package.is_relative_to(home / 'runtimes/updates') or package.name != 'installer.exe': raise ValueError('Invalid update package location.')
    return data


def helper_main(home, operation_id, parent_pid, *, install_dir=None, publishers=None, verifier=authenticode,
                runner=None, wait_seconds=60):
    """Native early-entry helper. Does not create a coordinator or migrate state."""
    home = plain_path(home); data = load_operation(home, operation_id)
    if install_dir is not None:
        expected = plain_path(install_dir)  # explicit isolated helper/test host
    else:
        programs = plain_path(Path(os.environ['LOCALAPPDATA']) / 'Programs')
        expected = plain_path(data['install_dir'])
        if expected not in (programs / 'Forge4', programs / 'Forge5'):
            raise ValueError('Update target differs from the per-user Forge installation.')
    if Path(data['install_dir']).resolve() != expected or expected == home or home.is_relative_to(expected):
        raise ValueError('Update target differs from the per-user Forge installation.')
    if type(parent_pid) is not int or parent_pid != data['parent_pid'] or parent_pid <= 0:
        raise ValueError('Update parent process differs from its prepared intent.')
    deadline = time.monotonic() + wait_seconds
    while psutil.pid_exists(parent_pid):
        try:
            if psutil.Process(parent_pid).create_time() != data.get('parent_created'):
                break  # PID was reused; never wait for or stop an unrelated app.
        except psutil.NoSuchProcess:
            break
        if time.monotonic() >= deadline: raise ValueError('Forge did not quit. No update was applied.')
        time.sleep(.1)
    backup = Path(data['backup'])
    previous = backup / 'installation'
    verify_tree(previous, data['previous_files'])
    policy = TRUSTED_PUBLISHERS if publishers is None else publishers
    for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
        require_trust(previous / name, data['previous_version'], policy, verifier)
    # A later failed-installation directory is retained outside the immutable
    # snapshot manifest; verify every original file without accepting changes.
    for entry in data['backup_files']:
        source = plain_path(backup / safe_relative(entry['path']))
        if not source.is_file() or source.is_symlink() or file_hash(source) != entry['sha256'] or source.stat().st_size != entry['size']:
            raise ValueError('Rollback data snapshot was modified.')
    rollback = data['state'] == 'rollback_prepared'
    if data['state'] not in ('prepared', 'rollback_prepared'): raise ValueError('This update intent has already been consumed.')
    if not rollback:
        package = plain_path(data['installer'])
        if file_hash(package) != data['sha256'] or package.stat().st_size != data['size']: raise ValueError('Staged installer was modified.')
        require_trust(package, data['version'], policy, verifier)
    operation = home / 'runtimes/updates/operations' / (operation_id + '.json')
    rollback_snapshot = retain_rollback_state(home, data) if rollback else None
    data['state'] = 'applying'; atomic_text(operation, encode(data))
    try:
        if not rollback:
            command = [str(package), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/DIR=' + str(expected)]
            result = (runner or (lambda args: subprocess.run(args, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)))(command)
            if result.returncode not in (0, 3010): raise ValueError('Installer failed; previous files will be restored.')
            regular_tree(expected)
            for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
                require_trust(expected / name, data['version'], policy, verifier)
        else:
            raise _Rollback()
        data['state'] = 'installed'; atomic_text(operation, encode(data))
        return {'installed': True, 'version': data['version'], 'restart_command': [str(expected / 'Forge.exe')]}
    except BaseException as exc:
        # Preserve post-upgrade journals before any code/data move. Rewinding
        # a completed or unknown effect to a pending invocation could replay it.
        current = rollback_snapshot or retain_rollback_state(home, data)
        # Targets were resolved and compared to the exact installation BEFORE
        # these recursive moves. Keep failed bytes for inspection, never execute.
        if expected.exists():
            regular_tree(expected)
            failed = backup / ('failed-installation-' + uuid4().hex)
            shutil.move(str(expected), str(failed))
        shutil.copytree(previous, expected)
        verify_tree(expected, data['previous_files'])
        latest = current / 'forge.sqlite3'
        with sqlite3.connect(backup / 'forge.sqlite3') as source, sqlite3.connect(home / 'state/forge.sqlite3') as destination:
            source.backup(destination)
        from forge_channels import quarantine_after_rollback
        with sqlite3.connect(home / 'state/forge.sqlite3') as restored, sqlite3.connect(latest) as current:
            data['channel_recovery'] = quarantine_after_rollback(restored, current)
        for folder in ROLLBACK_DATA_FOLDERS:
            saved = backup / 'data' / folder
            target = home / folder
            if saved.exists():
                if target.exists():
                    regular_tree(target)
                    retained = backup / 'post-update-data' / folder
                    retained.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(retained))
                shutil.copytree(saved, target)
        data['state'] = 'rolled_back'; data['reason'] = 'User requested rollback.' if isinstance(exc, _Rollback) else type(exc).__name__
        atomic_text(operation, encode(data))
        return {'installed': False, 'rolled_back': True, 'version': data['previous_version'],
                'restart_command': [str(expected / 'Forge.exe')]}


class _Rollback(Exception):
    pass
