"""Bounded project file tools and caller-approved process execution.

File tools enforce the selected project boundary. ``run_command`` is deliberately
NOT a filesystem sandbox: its caller must obtain approval for each argv/cwd pair.
The desktop service, not a model-supplied argument, is the permission authority.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_READ_CHARS = 48_000
MAX_LIST_ENTRIES = 400
MAX_SCAN_ENTRIES = 5_000
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_BYTES = 12 * 1024 * 1024
MAX_COMMAND_OUTPUT = 65_536
MAX_DIFF_CHARS = 12_000
SKIP_DIRECTORIES = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache'}
MUTATING_TOOLS = frozenset({'write_file', 'edit_file', 'make_directory', 'move_file', 'restore_file'})


class _WindowsProcessJob:
    """Kill inherited child processes when a command finishes or is cancelled.

    If Windows refuses job assignment (for example an older host job policy),
    callers still use taskkill /T on cancellation. This is lifecycle management,
    not a permission boundary or a process sandbox.
    """
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                        ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcesses', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD),
                        ('Scheduling', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('Basic', BasicLimits), ('Io', IoCounters),
                        ('ProcessMemory', ctypes.c_size_t), ('JobMemory', ctypes.c_size_t),
                        ('PeakProcessMemory', ctypes.c_size_t), ('PeakJobMemory', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process_handle = None
        try:
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return
            process_handle = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
            if process_handle and kernel.AssignProcessToJobObject(handle, process_handle):
                self.handle = handle
        finally:
            if process_handle:
                kernel.CloseHandle(process_handle)
            if self.handle is None:
                kernel.CloseHandle(handle)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _schema(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(required), 'additionalProperties': False}}}


def _string(description):
    return {'type': 'string', 'description': description}


class ProjectTools:
    def __init__(self, root, data_dir=None):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Project root must be an existing directory.')
        if data_dir is None:
            state = Path(os.environ.get('LOCALAPPDATA') or
                         os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
            data_dir = state / 'Sidekick' / 'backups'
        self.data_dir = Path(data_dir).expanduser().resolve()
        if self._inside(self.data_dir, self.root):
            raise ValueError('Backups must be stored outside the selected project.')
        self._lock = threading.RLock()

    @staticmethod
    def _inside(path, root):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def schemas():
        path = _string('File or directory path within the selected project; defaults to . where optional.')
        digest = _string('Optional SHA-256 from read_file to prevent overwriting changes; use missing for a new file.')
        return [
            _schema('list_files', 'List project files. Recursive scans skip dependency caches and .git; results are bounded.',
                    {'path': path, 'recursive': {'type': 'boolean'}}),
            _schema('read_file', 'Read a UTF-8 project text file (up to 2 MiB), with numbered-line range and SHA-256. Output is bounded.',
                    {'path': path, 'start_line': {'type': 'integer', 'minimum': 1},
                     'end_line': {'type': 'integer', 'minimum': 1}}, ('path',)),
            _schema('search_files', 'Find literal text in UTF-8 project files. Returns bounded matches with line numbers.',
                    {'query': _string('Literal, case-sensitive text to find.'), 'path': path}, ('query',)),
            _schema('write_file', 'Create or replace a UTF-8 project file atomically. Saves an undo backup; read existing files first.',
                    {'path': path, 'content': _string('Complete UTF-8 file content.'), 'expected_sha256': digest}, ('path', 'content')),
            _schema('edit_file', 'Replace exactly one occurrence of old_text. Fails if absent or ambiguous. Saves an undo backup.',
                    {'path': path, 'old_text': _string('Exact existing text; must occur once.'),
                     'new_text': _string('Replacement text.'), 'expected_sha256': digest}, ('path', 'old_text', 'new_text')),
            _schema('make_directory', 'Create a directory and any missing parent directories inside the project.',
                    {'path': path}, ('path',)),
            _schema('move_file', 'Move a regular project file to an unused project path. Does not overwrite a destination.',
                    {'source': path, 'destination': path}, ('source', 'destination')),
            _schema('restore_file', 'Restore the prior version saved by a file tool using its backup_id. Creates an undo backup of the current version.',
                    {'path': path, 'backup_id': _string('Backup ID returned by a prior file operation.')}, ('path', 'backup_id')),
            _schema('run_command', 'Run an executable after explicit approval for this exact command. This process has normal user permissions, not a sandbox. Maximum 60 seconds.',
                    {'argv': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 128},
                     'cwd': path, 'timeout': {'type': 'number', 'minimum': 0.1, 'maximum': 60}}, ('argv',)),
        ]

    def execute(self, name, args, cancel_event=None):
        """Return JSON data, including errors, so one failed tool cannot crash a chat."""
        try:
            if cancel_event is not None and cancel_event.is_set():
                return {'ok': False, 'error': 'Operation cancelled.', 'cancelled': True}
            known = {schema['function']['name'] for schema in self.schemas()}
            if name not in known:
                raise ValueError('Unknown project tool.')
            if not isinstance(args, dict):
                raise ValueError('Tool arguments must be a JSON object.')
            if name == 'run_command':
                return {'ok': True, **self.run_command(**args, cancel_event=cancel_event)}
            with self._lock:
                result = getattr(self, name)(**args)
            return {'ok': True, **result}
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, subprocess.SubprocessError) as exc:
            return {'ok': False, 'error': str(exc)[:2000]}

    def _path(self, value='.', *, directory=False):
        if not isinstance(value, str) or not value or '\x00' in value or len(value) > 4096:
            raise ValueError('A valid project path is required.')
        windows = PureWindowsPath(value)
        if value.startswith(('\\\\', '//')) or (windows.drive and not windows.root):
            raise ValueError('UNC, device and drive-relative paths are not allowed.')
        if windows.root and not windows.drive and os.name == 'nt' and value.startswith('\\'):
            raise ValueError('Use a project-relative path or a full local drive path.')
        # Reject ADS and Windows ambiguous names on every platform, including Docker.
        parts = re.split(r'[\\/]', value)
        if parts and re.fullmatch(r'[A-Za-z]:', parts[0]):
            parts = parts[1:]
        for part in parts:
            if part in ('', '.'):
                continue
            if part == '..' or ':' in part or part.endswith((' ', '.')):
                raise ValueError('Parent traversal, alternate streams and ambiguous paths are not allowed.')
            if part.casefold() == '.git':
                raise ValueError('.git internals are protected.')
            if re.fullmatch(r'(?i)(con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?', part):
                raise ValueError('Windows device names are not valid project files.')
        candidate = Path(value)
        if os.name != 'nt' and windows.drive:
            raise ValueError('Windows drive paths are not available in this runtime.')
        if not candidate.is_absolute():
            candidate = self.root / candidate
        lexical = Path(os.path.abspath(candidate))
        if not self._inside(lexical, self.root):
            raise ValueError('Path is outside the selected project.')
        current = self.root
        for component in lexical.relative_to(self.root).parts:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Symbolic links and junctions are not available to project tools.')
        resolved = lexical.resolve()
        if not self._inside(resolved, self.root):
            raise ValueError('Path resolves outside the selected project.')
        if directory and not resolved.is_dir():
            raise ValueError('Directory does not exist in the selected project.')
        return resolved

    def _relative(self, path):
        return path.relative_to(self.root).as_posix()

    def _read_bytes(self, path, *, missing=False):
        path = self._path(str(path))
        try:
            info = path.stat()
        except FileNotFoundError:
            if missing:
                return None
            raise ValueError('File does not exist.')
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('A regular file is required.')
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError('File exceeds the 2 MiB text limit.')
        if info.st_nlink > 1:
            raise ValueError('Hard-linked files are not available to project tools.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('A regular file is required.')
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('File exceeds the 2 MiB text limit.')
        self._decode(data)
        return data

    @staticmethod
    def _decode(data):
        if b'\x00' in data:
            raise ValueError('Binary files are not supported. Use an approved command if needed.')
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('File is not UTF-8 text.') from exc

    @staticmethod
    def _encode(content):
        if not isinstance(content, str) or '\x00' in content:
            raise ValueError('Content must be UTF-8 text without null bytes.')
        data = content.encode('utf-8')
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('Content exceeds the 2 MiB text limit.')
        return data

    def list_files(self, path='.', recursive=False):
        if not isinstance(recursive, bool):
            raise ValueError('recursive must be true or false.')
        start = self._path(path, directory=True)
        entries, truncated = [], False
        pending = [start]
        visited = 0
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as children:
                for entry in children:
                    visited += 1
                    if visited > MAX_SCAN_ENTRIES:
                        truncated = True
                        break
                    if entry.name.casefold() in SKIP_DIRECTORIES:
                        continue
                    try:
                        item = self._path(entry.path)
                        is_dir = item.is_dir()
                        if not is_dir and not item.is_file():
                            continue
                        entries.append({'path': self._relative(item), 'type': 'directory' if is_dir else 'file',
                                        'size': None if is_dir else item.stat().st_size})
                        if recursive and is_dir:
                            pending.append(item)
                    except (OSError, ValueError):
                        continue
                    if len(entries) >= MAX_LIST_ENTRIES:
                        truncated = True
                        break
            if truncated:
                break
        entries.sort(key=lambda item: (item['type'] != 'directory', item['path'].casefold()))
        return {'path': self._relative(start), 'entries': entries, 'truncated': truncated}

    def read_file(self, path, start_line=1, end_line=None):
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ValueError('start_line must be a positive integer.')
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line):
            raise ValueError('end_line must be an integer at least start_line.')
        target = self._path(path)
        data = self._read_bytes(target)
        lines = self._decode(data).splitlines(keepends=True)
        selected = ''.join(lines[start_line - 1:end_line])
        content = selected[:MAX_READ_CHARS]
        return {'path': self._relative(target), 'content': content, 'start_line': start_line,
                'end_line': min(end_line or len(lines), len(lines)), 'total_lines': len(lines),
                'sha256': _sha(data), 'truncated': len(selected) > MAX_READ_CHARS}

    def search_files(self, query, path='.'):
        if not isinstance(query, str) or not query or len(query) > 1000 or '\x00' in query:
            raise ValueError('Search query must contain 1–1000 characters.')
        start = self._path(path)
        matches, scanned, byte_count, visited = [], 0, 0, 0
        pending = [start]
        truncated = False
        while pending and not truncated:
            item = pending.pop()
            visited += 1
            if visited > MAX_SCAN_ENTRIES:
                truncated = True
                break
            try:
                item = self._path(str(item))
                if item.is_dir():
                    with os.scandir(item) as children:
                        for entry in children:
                            if entry.name.casefold() in SKIP_DIRECTORIES:
                                continue
                            if len(pending) + visited >= MAX_SCAN_ENTRIES:
                                truncated = True
                                break
                            pending.append(Path(entry.path))
                    continue
                data = self._read_bytes(item)
            except (OSError, ValueError):
                continue
            byte_count += len(data)
            if byte_count > MAX_SEARCH_BYTES:
                truncated = True
                break
            scanned += 1
            for line_number, line in enumerate(self._decode(data).splitlines(), 1):
                column = line.find(query)
                if column >= 0:
                    offset = max(0, column - 160)
                    matches.append({'path': self._relative(item), 'line': line_number,
                                    'text': line[offset:offset + 600]})
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        truncated = True
                        break
        return {'matches': matches, 'files_searched': scanned, 'truncated': truncated}

    @staticmethod
    def _diff(old, new, path):
        before, after = (old or b'').decode('utf-8'), (new or b'').decode('utf-8')
        if len(before) + len(after) > 200_000:
            return '(Diff omitted for a large file; use read_file to inspect.)'
        output, count = [], 0
        for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                         fromfile=f'a/{path}', tofile=f'b/{path}', n=3):
            output.append(line[:MAX_DIFF_CHARS - count])
            count += len(line)
            if count >= MAX_DIFF_CHARS:
                output.append('\n…diff truncated…\n')
                break
        return ''.join(output)

    @staticmethod
    def _check_expected(old, expected_sha256):
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-fA-F0-9]{64}|missing', expected_sha256):
                raise ValueError('expected_sha256 must be a SHA-256 digest or missing.')
            if (_sha(old) or 'missing') != expected_sha256.lower():
                raise ValueError('File changed since it was read. Read it again before editing.')

    def _backup(self, target, old, new):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.data_dir.resolve() != self.data_dir or self._inside(self.data_dir.resolve(), self.root):
            raise ValueError('Backup directory no longer resolves to trusted storage.')
        identifier = uuid.uuid4().hex
        payload = self.data_dir / (identifier + '.bin')
        manifest = self.data_dir / (identifier + '.json')
        metadata = {'version': 1, 'root': str(self.root), 'path': self._relative(target),
                    'existed': old is not None, 'sha256': _sha(old), 'new_sha256': _sha(new),
                    'created_at': time.time()}
        with payload.open('xb') as handle:
            handle.write(old or b'')
            handle.flush()
            os.fsync(handle.fileno())
        with manifest.open('x', encoding='utf-8') as handle:
            json.dump(metadata, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return {'backup_id': identifier, 'backup_path': str(payload)}

    def _commit(self, target, old, new):
        """Check for intervening edits, stage locally, then atomically replace."""
        self._path(str(target))
        if not target.parent.is_dir():
            raise ValueError('Parent directory does not exist. Use make_directory first.')
        backup = self._backup(target, old, new)
        staging = None
        try:
            if new is not None:
                fd, staging = tempfile.mkstemp(prefix='.sidekick-', suffix='.tmp', dir=target.parent)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(new)
                    handle.flush()
                    os.fsync(handle.fileno())
                if old is not None:
                    os.chmod(staging, stat.S_IMODE(target.stat().st_mode))
            self._path(str(target))
            if self._read_bytes(target, missing=True) != old:
                raise ValueError('File changed while preparing this edit. Read it again before editing.')
            if new is None:
                if target.exists():
                    target.unlink()
            elif old is None:
                # Link the staging inode exclusively: unlike replace(), this cannot
                # overwrite a file created after the missing-file check.
                os.link(staging, target)
                os.unlink(staging)
                staging = None
            else:
                os.replace(staging, target)
                staging = None
        finally:
            if staging is not None:
                try:
                    os.unlink(staging)
                except OSError:
                    pass
        relative = self._relative(target)
        return {'path': relative, 'sha256': _sha(new), 'previous_sha256': _sha(old),
                'bytes': len(new) if new is not None else 0, 'deleted': new is None,
                'diff': self._diff(old, new, relative), **backup}

    def write_file(self, path, content, expected_sha256=None):
        target = self._path(path)
        new = self._encode(content)
        old = self._read_bytes(target, missing=True)
        self._check_expected(old, expected_sha256)
        return self._commit(target, old, new)

    def edit_file(self, path, old_text, new_text, expected_sha256=None):
        if not isinstance(old_text, str) or not old_text:
            raise ValueError('old_text must be nonempty exact text.')
        if not isinstance(new_text, str):
            raise ValueError('new_text must be text.')
        target = self._path(path)
        old = self._read_bytes(target)
        self._check_expected(old, expected_sha256)
        content = self._decode(old)
        count = content.count(old_text)
        if count != 1:
            raise ValueError(f'old_text must match exactly once; found {count} matches.')
        return self._commit(target, old, self._encode(content.replace(old_text, new_text, 1)))

    def make_directory(self, path):
        target = self._path(path)
        existed = target.is_dir()
        target.mkdir(parents=True, exist_ok=True)
        self._path(str(target), directory=True)
        return {'path': self._relative(target), 'created': not existed}

    def move_file(self, source, destination):
        origin, target = self._path(source), self._path(destination)
        old = self._read_bytes(origin)
        if target.exists():
            raise ValueError('Destination already exists; move_file never overwrites it.')
        if not target.parent.is_dir():
            raise ValueError('Destination directory does not exist. Use make_directory first.')
        backup = self._backup(origin, old, None)
        self._path(str(origin))
        self._path(str(target))
        if self._read_bytes(origin) != old:
            raise ValueError('Source changed while preparing the move.')
        # An exclusive hard link followed by unlink preserves bytes/mode without
        # POSIX rename's overwrite behavior. Cross-volume moves fail safely.
        os.link(origin, target)
        try:
            origin.unlink()
        except OSError:
            target.unlink()
            raise
        return {'source': self._relative(origin), 'destination': self._relative(target),
                'sha256': _sha(old), **backup,
                'note': 'Restoring this backup restores the original path; the moved copy remains at the destination.'}

    def restore_file(self, path, backup_id):
        if not isinstance(backup_id, str) or not re.fullmatch(r'[a-f0-9]{32}', backup_id):
            raise ValueError('Invalid backup ID.')
        target = self._path(path)
        manifest = self.data_dir / (backup_id + '.json')
        payload = self.data_dir / (backup_id + '.bin')
        for item in (manifest, payload):
            if item.resolve() != item or not item.is_file() or item.is_symlink():
                raise ValueError('Backup is missing or is not trusted.')
            if item.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('Invalid backup size.')
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(metadata, dict) or metadata.get('root') != str(self.root) or metadata.get('path') != self._relative(target):
            raise ValueError('Backup does not belong to this project file.')
        restored = payload.read_bytes() if metadata.get('existed') is True else None
        if _sha(restored) != metadata.get('sha256'):
            raise ValueError('Backup integrity check failed.')
        if restored is not None:
            self._decode(restored)
        old = self._read_bytes(target, missing=True)
        result = self._commit(target, old, restored)
        return {**result, 'restored_backup_id': backup_id}

    @staticmethod
    def _terminate_process(process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            taskkill = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe')
            try:
                subprocess.run([taskkill, '/PID', str(process.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def run_command(self, argv, cwd='.', timeout=60, cancel_event=None):
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(
                not isinstance(arg, str) or '\x00' in arg or len(arg) > 16_384 for arg in argv):
            raise ValueError('argv must be a list of 1–128 valid command arguments.')
        if not argv[0].strip() or sum(map(len, argv)) > 65_536:
            raise ValueError('Executable is missing or command arguments are too long.')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 60:
            raise ValueError('Command timeout must be between 0.1 and 60 seconds.')
        working = self._path(cwd, directory=True)
        if cancel_event is not None and cancel_event.is_set():
            return {'cancelled': True, 'exit_code': None, 'output': ''}
        options = {'cwd': str(working), 'shell': False, 'stdin': subprocess.DEVNULL,
                   'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        process = subprocess.Popen(argv, **options)
        process_job = _WindowsProcessJob(process)
        output, byte_count = bytearray(), [0]

        def drain():
            try:
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    byte_count[0] += len(chunk)
                    output.extend(chunk[:max(0, MAX_COMMAND_OUTPUT - len(output))])
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=drain, daemon=True, name='sidekick-command-output')
        reader.start()
        deadline = time.monotonic() + timeout
        cancelled = timed_out = False
        while process.poll() is None:
            cancelled = cancel_event is not None and cancel_event.is_set()
            timed_out = time.monotonic() >= deadline
            if cancelled or timed_out:
                process_job.close()
                self._terminate_process(process)
                break
            if cancel_event is not None:
                cancel_event.wait(0.04)
            else:
                time.sleep(0.04)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process_job.close()
            self._terminate_process(process)
            process.wait(timeout=5)
        process_job.close()
        if os.name != 'nt':
            # A command is a bounded operation: do not leave background workers
            # holding its pipes or project files after the parent has exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        reader.join(timeout=1)
        # Avoid blocking the UI on descendants that inherited the output pipe.
        if not reader.is_alive():
            process.stdout.close()
        return {'argv': argv, 'cwd': self._relative(working), 'exit_code': process.returncode,
                'output': bytes(output).decode('utf-8', errors='replace'),
                'output_truncated': byte_count[0] > MAX_COMMAND_OUTPUT or reader.is_alive(),
                'cancelled': cancelled, 'timed_out': timed_out}
