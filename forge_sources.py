"""Host-owned source manifests and narrowly classified check artifacts."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from forge_xml import parse_xml

SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', '.cache', 'dist', 'build', '.forge'}


def _path(root, value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('Invalid source path.')
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Source path is outside the project.')
    candidate = root / relative
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink() or getattr(cursor, 'is_junction', lambda: False)():
            raise ValueError('Linked sources cannot establish verification currentness.')
        cursor = cursor.parent
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('Source path is outside the project.')
    return resolved


def _tracked(root):
    if not (root / '.git').exists():
        return set()
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', 'ls-files', '--cached', '-z'],
        cwd=root, capture_output=True, timeout=10,
        creationflags=0x08000000 if os.name == 'nt' else 0)
    if result.returncode:
        raise ValueError('Tracked source paths could not be inspected.')
    return {os.path.normcase(name) for name in result.stdout.decode('utf-8', errors='strict').split('\0') if name}


def generated_file(path, kind):
    """Formats identify reports/caches; extensions alone never exclude source."""
    try:
        if not path.is_file() or path.stat().st_size > 8_000_000:
            return False
        if kind == 'coverage_data':
            if path.read_bytes()[:16] != b'SQLite format 3\0':
                return False
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {'coverage_schema', 'meta', 'file', 'context', 'line_bits'} <= tables
        data = path.read_bytes()
        if kind == 'coverage_html':
            if path.suffix == '.html':
                return b'coverage.py' in data and (b'<html' in data or b'<!DOCTYPE html' in data)
            if path.name == '.gitignore': return data.strip() == b'*'
            return path.name.startswith(('style_', 'coverage_html_', 'keybd_', 'favicon_')) and path.suffix in ('.css', '.js', '.png')
        if kind in ('junit', 'coverage_xml'):
            node = parse_xml(data)
            if kind == 'junit':
                return node.tag in ('testsuite', 'testsuites') and bool(list(node.iter('testcase')))
            return node.tag == 'coverage' and 'version' in node.attrib and node.find('packages') is not None
        value = json.loads(data)
        if not isinstance(value, dict):
            return False
        if kind == 'coverage_json':
            return isinstance(value.get('meta'), dict) and 'version' in value['meta'] and isinstance(value.get('files'), dict)
        if kind == 'tsbuildinfo':
            program = value.get('program', value)
            return isinstance(value.get('version'), str) and isinstance(program, dict) and isinstance(program.get('fileNames'), list)
        if kind == 'coverage_html_status':
            return isinstance(value.get('files'), dict) and isinstance(value.get('format'), int) and isinstance(value.get('version'), str)
    except (OSError, ValueError, UnicodeError, ET.ParseError, sqlite3.Error):
        pass
    return False


def command_outputs(root, cwd, argv):
    """Infer exact destinations only from recognized report/compiler producers."""
    root, working = Path(root).resolve(), Path(cwd).resolve()
    executable = Path(argv[0]).stem.lower() if argv else ''
    tail = argv[1:]
    pytest = executable == 'pytest' or executable.startswith('python') and tail[:2] == ['-m', 'pytest']
    coverage = executable == 'coverage' or executable.startswith('python') and tail[:2] == ['-m', 'coverage']
    build = executable in ('tsc', 'vite') or executable in ('npm', 'pnpm', 'yarn', 'bun', 'npx')
    result = []
    def add(kind, value):
        destination = (working / value).resolve()
        if not destination.is_relative_to(root):
            return
        relative = destination.relative_to(root).as_posix()
        if relative == '.':
            return
        try:
            _path(root, relative)
        except ValueError:
            return
        result.append({'kind': kind, 'path': relative})
    if pytest:
        for index, argument in enumerate(tail):
            for option in ('--junitxml', '--junit-xml'):
                if argument == option and index + 1 < len(tail): add('junit', tail[index + 1])
                elif argument.startswith(option + '='): add('junit', argument.split('=', 1)[1])
    if coverage or pytest and any(arg == '--cov' or arg.startswith('--cov=') for arg in tail):
        add('coverage_data', '.coverage')
        reports = [arg.split('=', 1)[1] for arg in tail if arg.startswith('--cov-report=')]
        for index, argument in enumerate(tail):
            if argument == '--cov-report' and index + 1 < len(tail): reports.append(tail[index + 1])
        if coverage:
            action = next((arg for arg in tail if arg in ('xml', 'json', 'html')), None)
            if action:
                destination = None
                for index, argument in enumerate(tail):
                    if argument in ('-o', '--output', '-d', '--directory') and index + 1 < len(tail): destination = tail[index + 1]
                    elif argument.startswith(('--output=', '--directory=')): destination = argument.split('=', 1)[1]
                reports.append(action + (':' + destination if destination else ''))
        for report in reports:
            kind, _, value = report.partition(':')
            defaults = {'xml': ('coverage_xml', 'coverage.xml'), 'json': ('coverage_json', 'coverage.json'), 'html': ('coverage_html', 'htmlcov')}
            if kind in defaults:
                output_kind, default = defaults[kind]
                add(output_kind, value or default)
    if build:
        protected = []
        for directory, dirs, files in os.walk(working, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP]
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() == '.tsbuildinfo' and not generated_file(path, 'tsbuildinfo'):
                    protected.append(path.relative_to(root).as_posix())
        result.append({'kind': 'tsbuildinfo', 'protected_paths': protected})
    tracked = _tracked(root)
    for item in result:
        if 'path' not in item:
            continue
        path = _path(root, item['path'])
        if item['kind'] == 'coverage_html':
            valid = not path.exists() or path.is_dir() and generated_file(path / 'status.json', 'coverage_html_status') and all(
                child.name == 'status.json' or generated_file(child, 'coverage_html') for child in path.iterdir())
        else:
            valid = not path.exists() or generated_file(path, item['kind'])
        item['eligible'] = valid and os.path.normcase(item['path']) not in tracked
    return result


def source_manifest(root, scope='.', outputs=(), *, required_sources=None, permission=None,
                    max_files=5000, max_bytes=64 * 1024 * 1024, max_file_bytes=64 * 1024 * 1024):
    try:
        root = Path(root).resolve(strict=True)
        folder = _path(root, scope)
        tracked = _tracked(root) if outputs else set()
        excluded = {}
        protected = {os.path.normcase(path) for item in outputs if isinstance(item, dict) for path in item.get('protected_paths', [])}
        protected.update(os.path.normcase(item['path']) for item in outputs if isinstance(item, dict) and item.get('eligible') is False and item.get('path'))
        for item in outputs:
            if not isinstance(item, dict) or item.get('eligible') is False:
                continue
            if item.get('kind') == 'coverage_html' and item.get('path'):
                report = _path(root, item['path'])
                if generated_file(report / 'status.json', 'coverage_html_status'):
                    status = json.loads((report / 'status.json').read_text(encoding='utf-8'))
                    # Exact pages named by coverage metadata and its fixed assets.
                    names = {'index.html', 'function_index.html', 'class_index.html', 'status.json', '.gitignore'}
                    names.update(str(v['index'].get('url', '')) for v in status['files'].values() if isinstance(v, dict) and isinstance(v.get('index'), dict))
                    names.update(p.name for p in report.iterdir() if p.name.startswith(('style_', 'coverage_html_', 'keybd_', 'favicon_')) and p.suffix in ('.css', '.js', '.png'))
                    for name in names:
                        if name and Path(name).name == name:
                            candidate = report / name
                            if name == 'status.json' or generated_file(candidate, 'coverage_html'):
                                excluded[os.path.normcase(candidate.relative_to(root).as_posix())] = 'coverage_html'
            elif item.get('path'):
                excluded[os.path.normcase(item['path'])] = item['kind']
        if required_sources is not None:
            if not isinstance(required_sources, list) or len(required_sources) > max_files:
                raise ValueError('Registered source scope exceeds its file bound.')
            paths = [_path(root, value) for value in required_sources]
        elif folder.is_file() or not folder.exists(): paths = [folder]
        else:
            paths = []
            for directory, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in SKIP)
                for name in sorted(files):
                    paths.append(_path(root, (Path(directory) / name).relative_to(root).as_posix()))
                    if len(paths) > max_files:
                        raise ValueError('Registered source scope exceeds its file bound.')
        values, total, latest = [], 0, 0
        for path in sorted(set(paths), key=str):
            relative = path.relative_to(root).as_posix()
            if permission and not permission(relative):
                raise ValueError('Current permissions deny a registered verification source.')
            kind = excluded.get(os.path.normcase(relative))
            if not kind and path.suffix.lower() == '.tsbuildinfo' and any(item.get('kind') == 'tsbuildinfo' for item in outputs): kind = 'tsbuildinfo'
            if kind and os.path.normcase(relative) not in tracked | protected and (kind == 'coverage_html' or generated_file(path, kind)):
                continue
            # Preserve Builder's existing runtime/cache exclusions.
            if required_sources is None and path.suffix.lower() in ('.sqlite3', '.db', '.log', '.pyc'):
                continue
            if not path.exists():
                values.append({'path': relative, 'sha256': 'missing'}); continue
            if not path.is_file():
                raise ValueError('A registered verification source is not a file.')
            stat = path.stat(); total += stat.st_size; latest = max(latest, stat.st_mtime)
            if stat.st_size > max_file_bytes or total > max_bytes:
                raise ValueError('Registered source scope exceeds its byte bound.')
            data = path.read_bytes(); after = path.stat()
            if stat.st_size != len(data) or (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('A verification source changed while reading it.')
            values.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest()})
        digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        return {'schema_version': 1, 'available': True, 'fingerprint': digest, 'sha256': digest,
                'files': values, 'total_bytes': total, 'bytes': total, 'latest_modified': latest, 'directory': scope}
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        return {'schema_version': 1, 'available': False, 'reason': str(exc)[:300]}
