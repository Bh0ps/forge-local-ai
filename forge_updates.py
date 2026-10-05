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
        raise ValueError('Only stable major.minor.patch releases are supported.')
    return tuple(map(int, match.groups()))


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
    def __init__(self, service, current_version='4.2.0', install_dir=None, trust_policy=None,
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
        self.thread = None
        self.applying = False
        self.inspection_required = False
        try: self.state = self.store.entity('updates', 'current')
        except ValueError: self.state = {'id': 'current', 'state': 'idle'}
        if self.state.get('state') in ('checking', 'downloading'):
            self._save(state='interrupted', error='The previous update operation stopped. Check again.')
        if self.state.get('operation_id'):
            try:
                operation = load_operation(self.home, self.state['operation_id'])
                if operation['state'] in ('installed', 'rolled_back'):
                    self._save(state=operation['state'], verified=False)
                elif operation['state'] == 'applying':
                    self.inspection_required = True
                    self._save(state='interrupted', verified=False,
                        error='The previous installer outcome is unknown. Inspect the retained installer and backup, then repair Forge manually. Automatic retry is blocked.')
            except (ValueError, OSError, KeyError):
                self.inspection_required = True
                self._save(state='error', error='Update journal could not be verified. Inspect the retained backups.')

    def _save(self, **data):
        with self.lock:
            self.state = self.store.save_entity('updates', {**self.state, **data, 'id': 'current'})
            return self.status()

    def status(self):
        with self.lock:
            return {**self.state, 'current_version': self.current_version, 'repository': REPOSITORY,
                'inspection_required': self.inspection_required,
                'automatic_install_available': bool(self.publishers and self.install_dir and not self.inspection_required),
                'signing_setup_required': not bool(self.publishers),
                'message': 'The previous installer outcome is unknown. Inspect the retained backup and repair Forge manually before updating.' if self.inspection_required else
                    'Updates require a valid timestamped Windows signature from the approved publisher.' if self.publishers else
                    'Signing setup is required. Unsigned releases can be installed manually; automatic installation stays disabled.'}

    def check(self):
        with self.lock:
            if self.applying or self.state.get('state') == 'checking' or self.thread and self.thread.is_alive(): raise ValueError('An update operation is already active.')
            self._save(state='checking', error=None)
        try:
            chunks = []
            async def fetch_metadata():
                async with self.client_factory() as client:
                    await _get_bytes(client, 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest', 1024 * 1024, consume=chunks.append)
            asyncio.run(fetch_metadata())
            raw = b''.join(chunks)
            metadata = json.loads(raw)
            tag = metadata['tag_name']; newer = version(tag) > version(self.current_version)
            if metadata.get('draft') or metadata.get('prerelease'): raise ValueError('Only published stable releases are supported.')
            if metadata.get('html_url') != 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag:
                raise ValueError('Release metadata refers to a different repository.')
            if not newer: return self._save(state='current', release=None)
            number = '.'.join(map(str, version(tag)))
            filename = 'Forge-' + number + '-Setup.exe'
            candidates = [asset for asset in metadata.get('assets', []) if asset.get('name') == filename]
            if len(candidates) != 1: raise ValueError('Release must contain exactly one matching Windows installer.')
            asset = candidates[0]
            expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + filename
            digest = asset.get('digest', '')
            if asset.get('browser_download_url') != expected or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                raise ValueError('Installer URL or GitHub SHA256 digest is missing or invalid.')
            size = asset.get('size')
            if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES: raise ValueError('Invalid installer size.')
            return self._save(state='available', release={'version': number, 'tag': tag, 'name': filename,
                'url': expected, 'sha256': digest[7:], 'size': size}, downloaded=None, verified=False)
        except Exception as exc:
            self._save(state='error', error=str(exc)[:400]); raise

    def download(self):
        with self.lock:
            if self.inspection_required: raise ValueError('Inspect and repair the previous update before downloading another automatic installer.')
            if not self.publishers: raise ValueError('Automatic download is disabled until a verified publisher is embedded. Install unsigned previews manually.')
            if self.applying or self.state.get('state') == 'checking' or self.thread and self.thread.is_alive(): raise ValueError('An update operation is already active.')
            release = self._release()
            self.cancel_event.clear()
            self._save(state='downloading', received_bytes=0, error=None, verified=False)
            self.thread = threading.Thread(target=self._download, args=(release,), daemon=True, name='Forge-update-download')
            self.thread.start()
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
            os.replace(partial, complete)
            self._save(state='ready', downloaded=str(complete), verified=True, signature=signature)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            self._save(state='cancelled' if self.cancel_event.is_set() else 'error', error=str(exc)[:400], downloaded=None, verified=False)

    def cancel(self):
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
            if manager and getattr(manager, 'jobs', {}): raise ValueError('Finish active model operations before updating.')
            if manager and getattr(manager, 'active', None): raise ValueError('Finish the active performance operation before updating.')
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
                for folder in ('config', 'attachments', 'state/goals'):
                    original = self.home / folder
                    if original.exists():
                        regular_tree(original)
                        shutil.copytree(original, backup / 'data' / folder)
                backup_files = regular_tree(backup)
                data = {'id': identifier, 'home': str(self.home.resolve()), 'install_dir': str(self.install_dir),
                    'previous_version': self.current_version, 'version': release['version'], 'installer': str(package),
                    'sha256': release['sha256'], 'size': release['size'], 'backup': str(backup),
                    'previous_files': previous, 'backup_files': backup_files, 'state': 'prepared', 'parent_pid': os.getpid(),
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
            self._idle()
            identifier = self.state.get('operation_id')
            data = load_operation(self.home, identifier)
            if data['state'] != 'installed': raise ValueError('No completed update is available to roll back.')
            verify_tree(Path(data['backup']) / 'installation', data['previous_files'])
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
        if action == 'update_status': return self.status()
        if action == 'update_check': return self.check()
        if action == 'update_download': return self.download()
        if action == 'update_cancel': return self.cancel()
        if action == 'update_apply': return self.apply()
        if action == 'update_rollback': return self.rollback()
        if action == 'diagnostics_export': return self.diagnostics_export()
        raise ValueError('Unknown update action.')

    def shutdown(self):
        self.cancel()
        if self.thread: self.thread.join(timeout=2)


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
    expected = plain_path(install_dir or Path(os.environ['LOCALAPPDATA']) / 'Programs/Forge4')
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
    # A later failed-installation directory is retained outside the immutable
    # snapshot manifest; verify every original file without accepting changes.
    for entry in data['backup_files']:
        source = plain_path(backup / safe_relative(entry['path']))
        if not source.is_file() or source.is_symlink() or file_hash(source) != entry['sha256'] or source.stat().st_size != entry['size']:
            raise ValueError('Rollback data snapshot was modified.')
    rollback = data['state'] == 'rollback_prepared'
    if data['state'] not in ('prepared', 'rollback_prepared'): raise ValueError('This update intent has already been consumed.')
    policy = TRUSTED_PUBLISHERS if publishers is None else publishers
    if not rollback:
        package = plain_path(data['installer'])
        if file_hash(package) != data['sha256'] or package.stat().st_size != data['size']: raise ValueError('Staged installer was modified.')
        require_trust(package, data['version'], policy, verifier)
    operation = home / 'runtimes/updates/operations' / (operation_id + '.json')
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
        # Targets were resolved and compared to the exact installation BEFORE
        # these recursive moves. Keep failed bytes for inspection, never execute.
        if expected.exists():
            regular_tree(expected)
            failed = backup / ('failed-installation-' + uuid4().hex)
            shutil.move(str(expected), str(failed))
        shutil.copytree(previous, expected)
        verify_tree(expected, data['previous_files'])
        latest = backup / ('post-update-state-' + uuid4().hex + '.sqlite3')
        with sqlite3.connect(home / 'state/forge.sqlite3') as source, sqlite3.connect(latest) as destination:
            source.backup(destination)
        with sqlite3.connect(backup / 'forge.sqlite3') as source, sqlite3.connect(home / 'state/forge.sqlite3') as destination:
            source.backup(destination)
        from forge_channels import quarantine_after_rollback
        with sqlite3.connect(home / 'state/forge.sqlite3') as restored, sqlite3.connect(latest) as current:
            data['channel_recovery'] = quarantine_after_rollback(restored, current)
        for folder in ('config', 'attachments', 'state/goals'):
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
