"""Isolated real-browser body contrast evidence for two static evaluation fixtures.

Protocol: one JSON stdin request {fixture_dir, case_id: theme|landing,
timeout_seconds: 10}, one JSON stdout result. No browser installation, fixture
mutation, user profile, native application bridge or external network is used.
This measures computed body text contrast; it is not a visual or behavior oracle.
"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer

PROBE_VERSION = 'forge-body-contrast-1'
MAX_FILES, MAX_ENTRIES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 32, 128, 256 * 1024, 1024 * 1024
MAX_OUTPUT_BYTES, MAX_DIAGNOSTICS = 65536, 16


def _result(status='unverified', detail='', **values):
    return {'status': status, 'passed': status == 'verified', 'threshold': 4.5,
            'contrast_ratio': None, 'foreground': None, 'background': None,
            'theme_body_state': None,
            'backend': {'name': 'playwright-chromium', 'browser_version': None,
                        'probe_version': PROBE_VERSION, 'verified': False},
            'diagnostics': {'detail': str(detail)[:500]},
            'cleanup': {'server_stopped': True, 'browser_closed': True, 'profile_removed': True},
            'scope': 'Computed body text contrast only; no human visual review.', **values}


def _fixture_snapshot(value):
    lexical = Path(os.path.abspath(value))
    root = lexical.resolve(strict=True)
    if not root.is_dir() or os.path.normcase(str(root)) != os.path.normcase(str(lexical)):
        raise ValueError('Use an existing fixture directory without links.')
    pending, files, entries, total = [root], {}, 0, 0
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as listing:
            for entry in listing:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError('Fixture has too many directory entries.')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Fixture links and junctions are not served.')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise ValueError('Only ordinary fixture files are served.')
                if len(files) >= MAX_FILES or info.st_size > MAX_FILE_BYTES:
                    raise ValueError('Fixture exceeds the bounded file count or size.')
                path = Path(entry.path)
                if not path.resolve().is_relative_to(root):
                    raise ValueError('Fixture path is outside its root.')
                # Windows scandir metadata may omit the file identity; query
                # the path itself before comparing it with the opened handle.
                info = path.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Fixture path changed to a link.')
                flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                with os.fdopen(os.open(path, flags), 'rb') as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError('Fixture file changed during inspection.')
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if not path.resolve().is_relative_to(root):
                    raise ValueError('Fixture path changed outside its root.')
                total += len(raw)
                if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError('Fixture exceeds the bounded byte allowance.')
                files['/' + path.relative_to(root).as_posix()] = raw
    if '/index.html' not in files:
        raise ValueError('Fixture needs index.html.')
    return files


def _browser_executable(explicit=None):
    if explicit:
        candidate = Path(explicit)
        return str(candidate.resolve()) if candidate.is_file() else None
    candidates = [os.environ.get('FORGE_TEST_BROWSER', '')]
    if os.name == 'nt':
        for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(variable)
            if base:
                candidates.extend(str(Path(base) / suffix) for suffix in (
                    'Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'))
    else:
        candidates.extend(shutil.which(name) or '' for name in ('chromium', 'chromium-browser', 'google-chrome', 'microsoft-edge'))
    return next((str(Path(candidate).resolve()) for candidate in candidates if candidate and Path(candidate).is_file()), None)


SAMPLE_SCRIPT = r"""() => {
  const body=document.body, root=document.documentElement;
  if(!body)throw new Error('Missing document body');
  const b=getComputedStyle(body),h=getComputedStyle(root);
  if([b,h].some(s=>s.backgroundImage!=='none'||s.filter!=='none'||s.backdropFilter!=='none'||s.mixBlendMode!=='normal'))
    return {unsupported:'Nonuniform background or compositing cannot be verified by a body color probe.'};
  const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
  const ctx=canvas.getContext('2d',{willReadFrequently:true,colorSpace:'srgb'});
  if(!ctx)throw new Error('Color conversion canvas is unavailable');
  const rgba=color=>{ctx.clearRect(0,0,1,1);ctx.fillStyle='rgba(0,0,0,0)';ctx.fillStyle=color;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data];};
  const system=document.createElement('span');
  system.style.setProperty('display','none','important');system.style.setProperty('color','Canvas','important');
  system.style.setProperty('color-scheme',h.colorScheme,'important');root.append(system);
  const canvasColor=getComputedStyle(system).color;system.remove();
  const over=(paint,background)=>paint.slice(0,3).map((v,i)=>v*paint[3]/255+background[i]*(1-paint[3]/255));
  const blend=(paint,background,alpha)=>paint.map((v,i)=>v*alpha+background[i]*(1-alpha));
  const base=rgba(canvasColor).slice(0,3),htmlBackground=over(rgba(h.backgroundColor),base);
  let background=over(rgba(b.backgroundColor),htmlBackground),foreground=over(rgba(b.color),background);
  foreground=blend(foreground,htmlBackground,Number(b.opacity));background=blend(background,htmlBackground,Number(b.opacity));
  foreground=blend(foreground,base,Number(h.opacity));background=blend(background,base,Number(h.opacity));
  let storage=null;try{storage=localStorage.getItem('theme')?.slice(0,64)??null;}catch{}
  return {color:b.color,backgroundColor:b.backgroundColor,rootBackgroundColor:h.backgroundColor,canvasColor,
    foreground_rgb:foreground,background_rgb:background,theme_body_state:body.dataset.theme?.slice(0,64)??null,
    local_storage_theme:storage,bridge_exposed:typeof window.pywebview!=='undefined'||typeof window.chrome?.webview!=='undefined'};
}"""


def _contrast(first, second):
    def luminance(color):
        channels = [value / 255 for value in color]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    a, b = luminance(first), luminance(second)
    return (max(a, b) + .05) / (min(a, b) + .05)


def _worker(request):
    result = _result()
    server = context = engine = worker = None
    diagnostics = {'blocked_requests': 0, 'blocked_urls': [], 'page_errors': [], 'console': [], 'extra_pages_closed': 0}
    def blocked(url):
        diagnostics['blocked_requests'] += 1
        if len(diagnostics['blocked_urls']) < MAX_DIAGNOSTICS:
            diagnostics['blocked_urls'].append(str(url)[:240])
            print(json.dumps({'event': 'blocked', 'url': str(url)[:240]}), flush=True)
    try:
        from playwright.sync_api import sync_playwright
        files = _fixture_snapshot(request['fixture_dir'])
        class Server(HTTPServer):
            def get_request(self):
                connection, address = super().get_request(); connection.settimeout(.5)
                return connection, address
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                server.requests += 1
                parsed = urlsplit(self.path)
                name = unquote(parsed.path)
                if parsed.netloc or self.headers.get('Host') != server.origin.removeprefix('http://') or server.requests > 64:
                    self.send_error(403); return
                served_name = '/index.html' if name == '/' else name
                raw = files.get(served_name)
                if raw is None:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(served_name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "object-src 'none'; worker-src 'none'; frame-src 'none'; form-action 'self'; base-uri 'self'")
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, TimeoutError): pass
        server = Server(('127.0.0.1', 0), Handler); server.requests = 0
        server.origin = f'http://127.0.0.1:{server.server_port}'
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True); worker.start()
        print(json.dumps({'event': 'server_ready', 'origin': server.origin}), flush=True)
        engine = sync_playwright().start()
        context = engine.chromium.launch_persistent_context(request['profile_dir'], executable_path=request['browser_executable'],
            headless=True, timeout=min(6500, request['timeout_seconds'] * 1000), viewport={'width': 1000, 'height': 700},
            color_scheme='light', service_workers='block', accept_downloads=False,
            proxy={'server': server.origin, 'bypass': '127.0.0.1'},
            args=['--disable-gpu', '--disable-extensions', '--disable-background-networking', '--disable-component-update',
                  '--disable-sync', '--no-first-run', '--no-default-browser-check', '--mute-audio', '--metrics-recording-only',
                  '--disable-features=OptimizationHints,MediaRouter,AutofillServerCommunication'])
        page = context.pages[0] if context.pages else context.new_page()
        context.set_default_timeout(2500); context.set_default_navigation_timeout(3000)
        def route_request(route):
            parsed = urlsplit(route.request.url)
            if parsed.scheme == 'http' and f'http://{parsed.netloc}' == server.origin and route.request.method == 'GET':
                route.continue_()
            else:
                blocked(route.request.url); route.abort('blockedbyclient')
        context.route('**/*', route_request)
        if not hasattr(context, 'route_web_socket'):
            raise RuntimeError('This Playwright runtime cannot guard WebSocket traffic.')
        def websocket(route):
            # A routed socket is disconnected unless connect_to_server is
            # explicitly called. Do not synchronously close during the browser's
            # constructor binding: some Chromium versions stall that callback.
            blocked(route.url)
            diagnostics['unsupported_websocket'] = True
            route.on_message(lambda message: None)
        context.route_web_socket('**/*', websocket)
        def extra_page(extra):
            diagnostics['extra_pages_closed'] += 1; extra.close()
        context.on('page', extra_page)
        page.on('pageerror', lambda error: diagnostics['page_errors'].append(str(error)[:300]) if len(diagnostics['page_errors']) < MAX_DIAGNOSTICS else None)
        page.on('console', lambda message: diagnostics['console'].append(message.text[:300]) if len(diagnostics['console']) < MAX_DIAGNOSTICS else None)
        page.on('dialog', lambda dialog: dialog.dismiss())
        page.on('download', lambda download: download.cancel())
        version = context.browser.version if context.browser else None
        name = 'playwright-edge' if Path(request['browser_executable']).stem.lower() == 'msedge' else 'playwright-chromium'
        result['backend'].update(name=name, browser_version=version)
        print(json.dumps({'event': 'browser_ready', 'backend': result['backend']}), flush=True)
        page.goto(server.origin + '/', wait_until='domcontentloaded')
        print(json.dumps({'event': 'page_loaded'}), flush=True)
        if request['case_id'] == 'theme':
            page.locator('#theme-button').click()
        sample = page.evaluate(SAMPLE_SCRIPT)
        print(json.dumps({'event': 'colors_sampled'}), flush=True)
        for attempt in range(4):
            page.wait_for_timeout(120)
            following = page.evaluate(SAMPLE_SCRIPT)
            if sample == following:
                break
            sample = following
        else:
            raise RuntimeError('Computed body colors did not stabilize within the probe allowance.')
        if sample.get('unsupported'):
            raise RuntimeError(sample['unsupported'])
        if diagnostics.get('unsupported_websocket'):
            raise RuntimeError('Fixture requested unsupported WebSocket traffic; the socket was never connected.')
        ratio = _contrast(sample['foreground_rgb'], sample['background_rgb'])
        valid_theme = request['case_id'] != 'theme' or sample['theme_body_state'] == 'dark'
        status = 'verified' if ratio >= 4.5 and valid_theme and not diagnostics['page_errors'] else 'failed'
        result.update(status=status, passed=status == 'verified', contrast_ratio=round(ratio, 6),
            foreground=sample['color'], background=sample['backgroundColor'], theme_body_state=sample['theme_body_state'], computed=sample)
        result['backend']['verified'] = True
        diagnostics['detail'] = ('Computed body text contrast meets 4.5:1.' if status == 'verified' else
            'The public theme toggle did not produce dark body state.' if not valid_theme else
            'Fixture script raised a browser execution error.' if diagnostics['page_errors'] else 'Computed body text contrast is below 4.5:1.')
    except Exception as error:
        result.update(status='unverified', passed=False)
        diagnostics['detail'] = str(error)[:500]
    finally:
        print(json.dumps({'event': 'closing'}), flush=True)
        if context:
            try: context.close()
            except Exception: result['cleanup']['browser_closed'] = False
        if engine:
            try: engine.stop()
            except Exception: result['cleanup']['browser_closed'] = False
        if server:
            server.shutdown(); server.server_close()
            if worker: worker.join(.5)
            result['cleanup']['server_stopped'] = not worker or not worker.is_alive()
        result['diagnostics'] = diagnostics
    return result


def probe_contrast(fixture_dir, case_id='landing', timeout_seconds=10, browser_executable=None):
    """Supervise one fresh process tree; unavailable, timeout or dirty cleanup is unverified."""
    if case_id not in ('theme', 'landing') or type(timeout_seconds) not in (int, float) or not .5 <= timeout_seconds <= 10:
        return _result(detail='Use theme or landing and a 0.5–10 second deadline.')
    executable = _browser_executable(browser_executable)
    if not executable or getattr(sys, 'frozen', False):
        return _result(detail='An installed browser and a Python worker runtime are required; no download was attempted.')
    profile = Path(tempfile.mkdtemp(prefix='forge-contrast-')).resolve()
    output, errors, process, job = bytearray(), bytearray(), None, None
    result = _result(); timed_out = False; threads = []
    def drain(stream, target, maximum):
        try:
            while chunk := stream.read(4096):
                target.extend(chunk[:max(0, maximum - len(target))])
        finally: stream.close()
    try:
        request = {'fixture_dir': str(fixture_dir), 'case_id': case_id, 'timeout_seconds': timeout_seconds,
                   'browser_executable': executable, 'profile_dir': str(profile)}
        options = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'shell': False,
                   'env': {key: value for key, value in os.environ.items() if key not in ('DEBUG', 'PWDEBUG')}}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else: options['start_new_session'] = True
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'], **options)
        if os.name == 'nt':
            # Child waits for stdin: job ownership is established before it can
            # start Playwright or a browser. Unsupported ownership fails closed.
            root = str(Path(__file__).resolve().parents[2])
            if root not in sys.path: sys.path.insert(0, root)
            from project_tools import _WindowsProcessJob
            job = _WindowsProcessJob(process)
            if job.handle is None:
                raise RuntimeError('Owned browser process tree could not be established.')
        for stream, target, maximum in ((process.stdout, output, MAX_OUTPUT_BYTES), (process.stderr, errors, 2000)):
            thread = threading.Thread(target=drain, args=(stream, target, maximum), daemon=True); thread.start(); threads.append(thread)
        process.stdin.write(json.dumps(request).encode()); process.stdin.close()
        try: process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired: timed_out = True
    except Exception as error:
        result = _result(detail=str(error))
    finally:
        cleanup_deadline = time.monotonic() + 2
        if job: job.close()
        elif process and os.name != 'nt':
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if process and process.poll() is None:
            process.kill()
        if process:
            try: process.wait(timeout=max(.01, min(1, cleanup_deadline - time.monotonic())))
            except subprocess.TimeoutExpired: pass
        for thread in threads: thread.join(max(0, min(.2, cleanup_deadline - time.monotonic())))
        events = []
        for line in output.decode('utf-8', errors='replace').splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        final = next((item for item in reversed(events) if item.get('status') in ('verified', 'failed', 'unverified')), None)
        if final and not timed_out: result = final
        elif timed_out: result = _result(detail='Owned browser contrast probe exceeded its deadline.')
        elif not result['diagnostics'].get('detail'):
            result['diagnostics']['detail'] = 'Browser worker exited without contrast evidence.'
        result['diagnostics']['timed_out'] = timed_out
        result['diagnostics']['browser_started'] = any(item.get('event') == 'browser_ready' for item in events)
        result['diagnostics']['worker_phases'] = [item['event'] for item in events if item.get('event') in (
            'server_ready', 'browser_ready', 'page_loaded', 'colors_sampled', 'closing')]
        if not final:
            ready = next((item for item in events if item.get('event') == 'browser_ready'), None)
            if ready: result['backend'] = ready['backend']
            blocked_events = [item['url'] for item in events if item.get('event') == 'blocked']
            result['diagnostics'].update(blocked_requests=len(blocked_events), blocked_urls=blocked_events[:MAX_DIAGNOSTICS])
        origin = next((item.get('origin') for item in events if item.get('event') == 'server_ready'), None)
        server_stopped = True
        if origin:
            address = urlsplit(origin)
            try:
                with socket.create_connection((address.hostname, address.port), timeout=.1): server_stopped = False
            except OSError: pass
        removed = False
        for attempt in range(8):
            if time.monotonic() >= cleanup_deadline: break
            try:
                if profile.resolve() != profile or profile.is_symlink(): break
                shutil.rmtree(profile); removed = True; break
            except OSError: time.sleep(.05)
        result['cleanup'].update(server_stopped=server_stopped,
            browser_closed=process is None or process.poll() is not None,
            profile_removed=removed, profile_name=profile.name, owned_worker_exit=process.poll() if process else None)
        if not all(result['cleanup'].get(key) for key in ('server_stopped', 'browser_closed', 'profile_removed')):
            result.update(status='unverified', passed=False)
            result['diagnostics']['detail'] = 'Owned probe cleanup could not be confirmed.'
        if errors: result['diagnostics']['worker_error'] = errors.decode('utf-8', errors='replace')[-500:]
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192: raise ValueError('Probe request exceeds 8 KiB.')
        request = json.loads(raw)
        if not isinstance(request, dict): raise ValueError('Probe request must be a JSON object.')
        if sys.argv[1:] == ['--worker']:
            result = _worker(request)
        else:
            result = probe_contrast(request.get('fixture_dir', ''), request.get('case_id', 'landing'),
                request.get('timeout_seconds', 10), request.get('browser_executable'))
    except Exception as error:
        result = _result(detail=str(error))
    # ASCII JSON escapes keep the protocol portable across Windows console
    # encodings while preserving exact Unicode diagnostic strings on decode.
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
