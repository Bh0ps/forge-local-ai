"""Owned loopback previews, separate from research browsing and user processes."""
from __future__ import annotations

from collections import deque
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx

from project_tools import ProjectTools
from forge_store import encode
from storage import _now


class _StaticHandler(SimpleHTTPRequestHandler):
    def _safe_path(self, value):
        parts = Path(unquote(urlsplit(value).path).lstrip('/')).parts
        if any(part.startswith('.') or part in ('..', '__pycache__', 'node_modules') for part in parts):
            return None
        current = Path(self.directory)
        for part in parts:
            current /= part
            if current.is_symlink() or current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                return None
        path = current.resolve()
        if not path.is_relative_to(Path(self.directory).resolve()):
            return None
        return str(path)

    def translate_path(self, value):
        return self._safe_path(value) or str(Path(self.directory) / ('forge-blocked-' + uuid4().hex))

    def send_head(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403, 'Preview accepts its loopback host only')
            return None
        if self._safe_path(self.path) is None:
            self.send_error(403, 'Path is not available in this preview')
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(403, 'Directory listing disabled')

    def log_message(self, format, *args):
        self.server.preview_logs.append((format % args)[:1500])

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()


class PreviewManager:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.lock = threading.RLock()
        self.browser_lock = threading.RLock()
        self.active = {}
        self.closed = threading.Event()
        self.browser = None
        self.browser_preview = None
        # PIDs in persisted records are evidence, never process ownership.
        for record in self.store.entities('previews'):
            if record.get('status') in ('starting', 'ready', 'running', 'stopping'):
                self._patch_status(record, {'status': 'interrupted', 'url': None,
                    'error': 'Coordinator restarted. Start a fresh preview.'},
                    states=('starting', 'ready', 'running', 'stopping'))

    def _patch_status(self, expected, changes, *, states=None, detached=True):
        """Patch an existing operation without replaying its stale scope record."""
        if set(changes) - {'status', 'url', 'error'}:
            raise ValueError('Only preview status fields may be patched.')
        with self.store._connection(transaction='write') as db:
            row = db.execute("SELECT data FROM entities WHERE kind='previews' AND id=?", (expected['id'],)).fetchone()
            if row is None:
                return None
            current = json.loads(row[0])
            identity = ('id', 'session_id', 'run_id', 'builder_id', 'mode', 'cwd', 'url')
            if any(current.get(key) != expected.get(key) for key in identity):
                return None
            owner = expected.get('project_id')
            same_owner = current.get('project_id') == owner
            if same_owner and current.get('detached_project_id') != expected.get('detached_project_id'):
                return None
            detached_owner = (detached and owner is not None and current.get('project_id') is None
                and current.get('detached_project_id') == owner)
            if not (same_owner or detached_owner) or states is not None and current.get('status') not in states:
                return None
            result = {**current, **changes, 'updated_at': _now()}
            db.execute("UPDATE entities SET data=? WHERE kind='previews' AND id=?", (encode(result), expected['id']))
        return result

    def attach_browser(self, ui_dispatch):
        with self.lock:
            if self.browser is None:
                from native_browser import NativeBrowser
                self.browser = NativeBrowser(self.store.home, ui_dispatch=ui_dispatch,
                    panel_id='forge-preview-panel', profile_name='native-preview-profile',
                    navigation_guard=self.allowed_url, diagnostics=True)

    def allowed_url(self, url):
        parsed = urlsplit(url)
        if url == 'about:blank':
            return True
        with self.lock:
            current = self.active.get(self.browser_preview)
            record = current.get('record') if current else None
        origin = urlsplit(record['url']) if record and record.get('url') else None
        return bool(origin and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                    and parsed.port == origin.port and not parsed.username and not parsed.password)

    def _project(self, data):
        project = self.store.get_project(data['project_id'])
        tools = ProjectTools(project['path'], self.store.home / 'backups')
        return project, tools._path(data.get('cwd', '.'), directory=True)

    @staticmethod
    def _port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def start(self, data):
        project, folder = self._project(data)
        mode = data.get('mode', 'auto')
        if mode == 'auto':
            mode = 'vite' if (folder / 'package.json').is_file() else 'fastapi' if (folder / 'app.py').is_file() else 'static'
        if mode not in ('static', 'vite', 'fastapi'):
            raise ValueError('Choose static, vite or fastapi preview.')
        if self.closed.is_set():
            raise ValueError('Preview manager has stopped.')
        identifier = uuid4().hex
        record = {'id': identifier, 'project_id': project['id'], 'builder_id': data.get('builder_id'),
                  'mode': mode, 'cwd': str(folder.relative_to(Path(project['path']).resolve())),
                  'status': 'starting', 'url': None, 'run_id': data.get('run_id'), 'session_id': None}
        with self.lock:
            if any(item['record'].get('builder_id') == data.get('builder_id') and
                   item['record']['project_id'] == project['id'] for item in self.active.values()):
                raise ValueError('Stop the existing preview before starting another for this builder.')
            if mode == 'static':
                if not (folder / 'index.html').is_file():
                    raise ValueError('Static preview needs index.html in the selected directory.')
                handler = functools.partial(_StaticHandler, directory=str(folder))
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
                server.daemon_threads = True
                server.preview_logs = deque(maxlen=300)
                thread = threading.Thread(target=server.serve_forever, daemon=True, name='Forge-static-preview')
                record.update(status='ready', url=f'http://127.0.0.1:{server.server_port}/')
                record = self.store.save_entity('previews', record)
                self.active[identifier] = {'record': record, 'server': server, 'thread': thread}
                thread.start()
            else:
                port = self._port()
                if mode == 'vite':
                    node = shutil.which('node')
                    entry = folder / 'node_modules/vite/bin/vite.js'
                    if not node or not entry.is_file():
                        raise ValueError('Vite preview needs Node.js and installed project dependencies. Install them explicitly, then retry.')
                    argv = [node, str(entry), '--host', '127.0.0.1', '--port', str(port), '--strictPort']
                else:
                    local = folder / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
                    python = str(local) if local.is_file() else sys.executable if not getattr(sys, 'frozen', False) else shutil.which('python')
                    entrypoint = data.get('entrypoint', 'app:app')
                    if not python or not isinstance(entrypoint, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', entrypoint):
                        raise ValueError('FastAPI preview needs Python and an entrypoint such as app:app.')
                    argv = [python, '-m', 'uvicorn', entrypoint, '--host', '127.0.0.1', '--port', str(port)]
                sessions = getattr(self.service, 'command_sessions', None)
                if sessions is None:
                    raise ValueError('Managed command sessions are unavailable.')
                session = sessions.start(project['path'], argv, cwd=record['cwd'],
                    run_id=data.get('run_id', ''), purpose='preview', owner_id=identifier, max_seconds=86400)
                record.update(session_id=session['id'], url=f'http://127.0.0.1:{port}/', argv=argv)
                record = self.store.save_entity('previews', record)
                self.active[identifier] = {'record': record, 'port': port}
                threading.Thread(target=self._ready, args=(identifier,), daemon=True, name='Forge-preview-readiness').start()
        return record

    def _ready(self, identifier):
        deadline = time.monotonic() + 30
        error = 'Preview did not become ready within 30 seconds. Check its logs.'
        while not self.closed.is_set() and time.monotonic() < deadline:
            with self.lock:
                item = self.active.get(identifier)
                if not item:
                    return
                record = item['record']
            status = self.service.command_sessions.status(record['session_id'])
            if status.get('status') in ('completed', 'failed', 'cancelled', 'stopped', 'interrupted', 'timed_out'):
                error = 'Preview process exited. Check its logs.'
                break
            try:
                # A responding unrelated service is never adopted after a port race.
                import psutil
                process = psutil.Process(status['pid'])
                owned = any(connection.laddr and connection.laddr.port == item['port'] and
                            connection.status == psutil.CONN_LISTEN
                            for child in [process] + process.children(recursive=True)
                            for connection in child.net_connections(kind='tcp'))
                if owned:
                    with httpx.Client(timeout=1, trust_env=False) as client:
                        response = client.get(record['url'], follow_redirects=False)
                    if response.status_code < 500:
                        with self.lock:
                            if self.active.get(identifier) is item:
                                updated = self._patch_status(record, {'status': 'ready'}, states=('starting',), detached=False)
                                if updated is not None:
                                    item['record'] = updated
                        return
            except (OSError, KeyError, httpx.HTTPError, psutil.Error):
                pass
            self.closed.wait(.2)
        with self.lock:
            item = self.active.get(identifier)
            record = dict(item['record']) if item else None
        if item:
            self.service.command_sessions.stop(record['session_id'])
            with self.lock:
                if self.active.get(identifier) is item:
                    self._patch_status(record, {'status': 'failed', 'url': None, 'error': error},
                        states=('starting', 'ready', 'running'))
                    self.active.pop(identifier, None)

    def status(self, identifier=None, project_id=None):
        records = self.store.entities('previews')
        if identifier:
            record = self.store.entity('previews', identifier)
            with self.lock:
                item = self.active.get(identifier)
                owned = item is not None
                owned_record = dict(item['record']) if item else None
            if owned and owned_record.get('session_id'):
                session = self.service.command_sessions.status(owned_record['session_id'])
                if session['status'] not in ('starting', 'running', 'stopping'):
                    with self.lock:
                        if self.active.get(identifier) is item:
                            self._patch_status(owned_record, {'status': 'failed', 'url': None,
                                'error': 'Preview process exited. Inspect logs, then start a fresh preview.'},
                                states=('starting', 'ready', 'running'))
                            self.active.pop(identifier, None)
            if not owned:
                with self.lock:
                    if identifier not in self.active:
                        self._patch_status(record, {'status': 'interrupted', 'url': None}, states=('starting', 'ready'))
            return self.store.entity('previews', identifier)
        records = [r for r in records if not project_id or r['project_id'] == project_id][-30:][::-1]
        return {'previews': [self.status(r['id']) if r['id'] in self.active else r for r in records]}

    def logs(self, identifier, start=0, limit=8000):
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError('Use a nonnegative log offset and limit from 1 to 16000.')
        record = self.store.entity('previews', identifier)
        if record.get('session_id'):
            return self.service.command_sessions.read(record['session_id'], start=start, limit=min(limit, 16000))
        with self.lock:
            item = self.active.get(identifier)
            text = '\n'.join(item['server'].preview_logs) if item else ''
        return {'id': identifier, 'output': text[start:start + limit], 'next_start': min(len(text), start + limit)}

    def stop(self, identifier):
        with self.lock:
            item = self.active.pop(identifier, None)
            try:
                record = self.store.entity('previews', identifier)
            except ValueError:
                if item is None:
                    raise
                record = dict(item['record'])
            owned_record = dict(item['record']) if item else record
            browser = None
            if self.browser_preview == identifier:
                self.browser_preview = None
                browser = self.browser
            # Publish revocation before any blocking host/process calls.
            stopping = self._patch_status(owned_record, {'status': 'stopping', 'url': None})
        errors = []
        if browser and hasattr(browser, 'cancelled'):
            browser.cancelled.set()
        if item:
            try:
                if item.get('server'):
                    item['server'].shutdown()
                    item['server'].server_close()
                elif owned_record.get('session_id'):
                    self.service.command_sessions.stop(owned_record['session_id'])
            except Exception as exc:
                errors.append(str(exc)[:400])
        if browser:
            try:
                # Serialize pane changes separately. Navigation guards acquire
                # only the ownership lock, which is never held here.
                with self.browser_lock:
                    if self.browser_preview is None:
                        browser.stop()
                        browser.dispatch('hide')
            except Exception as exc:
                errors.append(str(exc)[:400])
        if stopping is not None:
            self._patch_status(stopping, {'status': 'stopped', 'url': None,
                **({'error': 'Preview stopped; browser cleanup reported: ' + '; '.join(errors)} if errors else {})},
                states=('stopping', 'failed', 'interrupted', 'stopped'))
        return self.store.entity('previews', identifier)

    def browser_action(self, action, data, context=None):
        # Opening a native pane waits for the workspace to render it. That
        # workspace must be able to read the current preview identity while
        # the opening action owns the pane lock.
        if action == 'status':
            return self._browser_action(action, data, context)
        with self.browser_lock:
            return self._browser_action(action, data, context)

    def _browser_action(self, action, data, context=None):
        if self.browser is None:
            if action == 'status':
                return {'available': False, 'message': 'Open Forge desktop for the isolated preview panel.'}
            raise ValueError('Preview inspection requires Forge desktop.')
        identifier = data.get('id') or data.get('preview_id')
        token = data.get('pane_token')
        if token is not None:
            if not isinstance(token, str) or not 1 <= len(token) <= 96 or not identifier:
                raise ValueError('Expected a scoped preview pane identity.')
            view = getattr(self.browser, 'view', None)
            layout = view._layout() if view is not None and hasattr(view, '_layout') else {}
            stale = (action == 'hide' and identifier != self.browser_preview)
            if action == 'hide' and layout.get('pane_token'):
                stale = stale or layout['pane_token'] != token
            if action in ('show', 'resize') and view is not None and hasattr(view, '_layout'):
                stale = layout.get('preview_id') != identifier or layout.get('pane_token') != token
            if stale:
                return {'ok': True, 'not_executed': True}
        if action in ('hide', 'status'):
            result = self.browser.dispatch(action)
            if action == 'status':
                with self.lock:
                    identifier = self.browser_preview
                    current = self.active.get(identifier)
                    record = dict(current['record']) if current else {}
                result = {**result, 'preview_id': identifier,
                          'builder_id': record.get('builder_id'),
                          'project_id': record.get('project_id')}
            return result
        record = self.status(identifier)
        if record['status'] != 'ready' or identifier not in self.active:
            raise ValueError('Start a ready preview before inspecting it.')
        changed = self.browser_preview != identifier
        self.browser_preview = identifier
        browser = self.browser
        from native_browser import BrowserGuard
        def owned():
            with self.lock:
                if self.browser is not browser or self.browser_preview != identifier or identifier not in self.active:
                    raise BrowserGuard('Browser view changed while opening. Open the preview again.')
        if changed:
            self.browser.stop()
            self.browser.reset()
            self.browser.dispatch('open', {'url': record['url']}, context=context, check=owned)
        if getattr(self.browser, 'view', None) is not None:
            self.browser.view.preview_id = identifier
        args = {k: v for k, v in data.items() if k not in ('id', 'preview_id')}
        if action in ('show', 'reload', 'resize'):
            result = browser.dispatch(action, args, context=context, check=owned)
            if action == 'show' and args.get('rect') is None:
                # An explicit preview_open must finish its one admitted opening
                # before an agent can inspect. Frontend bounds binds stay short
                # and asynchronous; they are not navigation-readiness requests.
                return browser.wait_ready(context, check=owned)
            return result
        if action == 'viewport':
            return self.browser.viewport(args)
        if action == 'diagnostics':
            return self.browser.diagnostics()
        if action not in ('inspect', 'click', 'type', 'screenshot', 'key', 'select', 'scroll', 'wait'):
            raise ValueError('Unsupported preview browser operation.')
        return self.browser.execute('browser_' + action, args, context)

    def shutdown(self):
        self.closed.set()
        for identifier in list(self.active):
            self.stop(identifier)
        if self.browser:
            self.browser.shutdown()
