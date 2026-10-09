"""Forge Windows host: one coordinator, same-window HUD, and tray lifecycle."""
from __future__ import annotations

import base64
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from core import BASE_DIR
from forge_host import (EmergencyHotkey, HostLifecycle, PairingAuthority,
                        SingleCoordinator, data_directory, set_startup)

log = logging.getLogger('forge')


class Bridge:
    def __init__(self, service=None, pairing=None):
        self._service = service
        self._service_lock = threading.Lock()
        self._window = None
        self._mode = 'full'
        self._full_bounds = None
        self._full_maximized = True
        self._pin = False
        self._closing = False
        self._capture_lock = threading.Lock()
        self._pairing = pairing or PairingAuthority()
        self._lifecycle = None
        self._tray = None
        self._hotkey = None
        self._server_url = None
        self._navigation_guard = None

    def _ensure_service(self):
        if self._service is None:
            with self._service_lock:
                if self._service is None:
                    from forge_service import ForgeService
                    self._service = ForgeService()
        from computer_tools import ComputerBroker
        from dictation import Dictation
        home = getattr(self._service.store, 'home', data_directory())
        if not getattr(self._service, 'computer_broker', None):
            self._service.computer_broker = ComputerBroker(home)
        if not getattr(self._service, 'host_dictation', None):
            selected = self._service.store.get_settings().get('dictation_model', 'base') if hasattr(self._service.store, 'get_settings') else 'base'
            self._service.host_dictation = Dictation(home, model=selected)
        integrations = getattr(self._service, 'integrations', None)
        if integrations and not getattr(integrations.browser, 'native', None):
            from native_browser import NativeBrowser
            integrations.browser.native = NativeBrowser(home, ui_dispatch=self._native)
        if hasattr(self._service, 'get_builder'):
            self._service.get_builder().previews.attach_browser(self._native)
        if self._lifecycle is None:
            self._lifecycle = HostLifecycle(self._service, self._pairing)
        return self._service

    def call(self, action, data=None):
        try:
            if self._closing:
                raise ValueError('Forge is shutting down.')
            data = {} if data is None else data
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            service = self._ensure_service()
            if action == 'artifact_save_as':
                # Only an explicit user download opens this dialog. Agent tools
                # cannot choose arbitrary filesystem destinations.
                import webview
                artifact = service.get_builder().documents.download(data)
                destination = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=artifact['name'])
                if not destination:
                    return {'cancelled': True}
                target = Path(destination[0] if isinstance(destination, (tuple, list)) else destination)
                raw = base64.b64decode(artifact['content_base64'], validate=True)
                target.write_bytes(raw)
                return {'ok': True, 'path': str(target), 'sha256': artifact['sha256']}
            if action.startswith('dictation_'):
                state = service.host_dictation.status()['state']
                sync = (action in ('dictation_start', 'dictation_upload') and
                        state not in ('starting', 'recording', 'stopping', 'transcribing', 'installing') or
                        action == 'dictation_status' and state in ('idle', 'ready', 'error', 'cancelled'))
                if sync and hasattr(service.store, 'get_settings'):
                    service.host_dictation.select_model(service.store.get_settings().get('dictation_model', 'base'))
                result = service.host_dictation.dispatch(action.removeprefix('dictation_'), data)
                if action == 'dictation_install' and data.get('model'):
                    service.dispatch('settings', {'dictation_model': data['model']})
                return result
            if action == 'host_status':
                return {'native': True, 'mode': self._mode, 'pinned': self._pin,
                        'url': self._server_url, 'tray': self._tray is not None,
                        'emergency_shortcut': self._hotkey.shortcut if self._hotkey else 'Ctrl+Alt+Shift+S',
                        'shortcut_error': self._hotkey.error if self._hotkey else None}
            if action == 'pair_browser':
                return {**self._pairing.issue(), 'url': self._server_url}
            if action == 'startup':
                result = set_startup(data.get('enabled'))
                service.dispatch('settings', {'startup': result['enabled']})
                return result
            if action == 'quit':
                return self.quit()
            if action == 'emergency_stop':
                return self._lifecycle.emergency_stop()
            if action == 'settings' and data.get('emergency_shortcut'):
                EmergencyHotkey.parse(data['emergency_shortcut'])
            result = service.dispatch(action, data)
            if action in ('update_apply','update_rollback') and result.get('prepared'):
                if not getattr(sys,'frozen',False):
                    service.update_manager.applying=False
                    raise ValueError('Apply updates from the installed Windows app.')
                import subprocess
                try:
                    subprocess.Popen(result['command'],creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
                except OSError:
                    service.update_manager.applying=False
                    raise ValueError('The update helper could not start. The prepared backup is retained; retry from Updates.') from None
                threading.Timer(.3,self.quit).start()
            if action == 'settings' and data.get('emergency_shortcut') and self._hotkey:
                self._hotkey.stop()
                self._hotkey = EmergencyHotkey(self._lifecycle.emergency_stop, data['emergency_shortcut'])
                self._hotkey.start()
            return result
        except Exception as exc:
            log.warning('Action %s failed: %s', action, type(exc).__name__)
            return {'error': str(exc)[:1000]}

    def _native(self, operation):
        """Bridge calls run on workers; marshal WinForms operations to its UI thread."""
        from webview.platforms.winforms import BrowserView
        from System import Action
        if self._window is None:
            raise RuntimeError('Window is unavailable.')
        form = BrowserView.instances.get(self._window.uid)
        if form is None:
            raise RuntimeError('Window is unavailable.')
        result, done = {}, threading.Event()
        def run():
            try:
                result['value'] = operation(form)
            except Exception as exc:
                result['error'] = exc
            finally:
                done.set()
        if form.InvokeRequired:
            form.BeginInvoke(Action(run))
            if not done.wait(5):
                raise TimeoutError('Window update timed out.')
        else:
            run()
        if 'error' in result:
            raise result['error']
        return result.get('value')

    def pin(self, value):
        try:
            if type(value) is not bool:
                raise ValueError('Pin expects a boolean.')
            self._pin = value
            self._native(lambda form: setattr(form, 'TopMost', self._pin or self._mode == 'hud'))
            return {'ok': True, 'pinned': self._pin}
        except Exception as exc:
            return {'error': str(exc)}

    def mode(self, mode, expanded=False):
        if mode not in ('full', 'hud'):
            return {'error': 'Unknown window mode'}
        try:
            if mode == 'hud':
                browser = getattr(getattr(getattr(self._service, 'integrations', None), 'browser', None), 'native', None)
                if browser and browser.view and hasattr(browser.view, 'hide'):
                    browser.view.hide()
            def update(form):
                from System.Drawing import Size
                from System.Windows.Forms import Screen, FormWindowState
                # Keep the workspace state before restoring the same form for HUD.
                # Capturing bounds while maximized would otherwise make Expand
                # restore a border-sized window rather than the maximized workspace.
                if self._mode == 'full' and mode == 'hud':
                    if form.WindowState != FormWindowState.Minimized:
                        self._full_maximized = form.WindowState == FormWindowState.Maximized
                elif self._mode == 'full' and mode == 'full' and form.WindowState != FormWindowState.Minimized:
                    self._full_maximized = form.WindowState == FormWindowState.Maximized
                if form.WindowState != FormWindowState.Normal:
                    form.WindowState = FormWindowState.Normal
                area = Screen.FromControl(form).WorkingArea
                scale = float(getattr(form, 'DeviceDpi', 96)) / 96
                # Change minimum bounds before shrinking the same native form.
                form.MinimumSize = Size(round((360 if mode == 'hud' else 760) * scale),
                                        round((150 if mode == 'hud' else 560) * scale))
                if mode == 'hud':
                    if self._mode != 'hud':
                        self._full_bounds = (form.Left, form.Top, form.Width, form.Height)
                    width, height = round(620 * scale), round((440 if expanded else 188) * scale)
                    x, y = form.Left, form.Top
                else:
                    x, y, width, height = self._full_bounds or (form.Left, form.Top, round(1180 * scale), round(820 * scale))
                width, height = min(width, area.Width), min(height, area.Height)
                x = max(area.Left, min(x, area.Right - width))
                y = max(area.Top, min(y, area.Bottom - height))
                form.SetBounds(x, y, width, height)
                if mode == 'full' and self._full_maximized:
                    form.WindowState = FormWindowState.Maximized
                form.TopMost = self._pin or mode == 'hud'
                self._mode = mode
                return {'mode': mode, 'expanded': bool(expanded)}
            return self._native(update)
        except Exception as exc:
            log.exception('Window mode update failed')
            return {'error': str(exc)}

    def copy(self, text):
        try:
            import pyperclip
            pyperclip.copy(str(text))
            return {'ok': True}
        except Exception as exc:
            return {'error': str(exc)}

    def choose_project(self):
        try:
            import webview
            paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            if not paths:
                return {'cancelled': True}
            path = Path(paths[0])
            return self.call('create_project', {'path': str(path), 'name': path.name})
        except Exception as exc:
            return {'error': str(exc)}

    def choose_plugin(self):
        try:
            import webview
            paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            return {'path': paths[0]} if paths else {'cancelled': True}
        except Exception as exc:
            return {'error': str(exc)}

    def dictation(self, action, data=None):
        return self.call('dictation_' + str(action), data)

    def screenshot(self):
        if not self._capture_lock.acquire(blocking=False):
            return {'error': 'Capture is already running.'}
        try:
            from PIL import ImageGrab
            self._window.hide()
            time.sleep(.2)
            picture = ImageGrab.grab()
            picture.thumbnail((1600, 1600))
            buffer = io.BytesIO()
            picture.convert('RGB').save(buffer, 'JPEG', quality=80)
            return {'image': base64.b64encode(buffer.getvalue()).decode(), 'mime_type': 'image/jpeg'}
        except Exception as exc:
            return {'error': str(exc)}
        finally:
            if not self._closing:
                try:
                    self._window.show()
                except Exception:
                    log.exception('Restore after capture failed')
            self._capture_lock.release()

    def show(self):
        try:
            self._window.show()
            def focus(form):
                from System.Windows.Forms import FormWindowState
                if form.WindowState == FormWindowState.Minimized:
                    form.WindowState = (FormWindowState.Maximized
                                        if self._mode == 'full' and self._full_maximized
                                        else FormWindowState.Normal)
                form.Activate()
            self._native(focus)
        except Exception:
            log.exception('Could not show Forge')

    def restrict_navigation(self):
        """Never expose the authenticated native bridge to a third-party page."""
        def attach(form):
            if self._navigation_guard:
                return
            expected = urlsplit(self._server_url)
            def guard(sender, args):
                try:
                    target = urlsplit(str(args.Uri))
                    if (target.scheme, target.hostname, target.port) != (expected.scheme, expected.hostname, expected.port):
                        args.Cancel = True
                        if target.scheme in ('http', 'https'):
                            import webbrowser
                            webbrowser.open(str(args.Uri))
                except ValueError:
                    args.Cancel = True
            self._navigation_guard = guard
            form.webview.NavigationStarting += guard
        return self._native(attach)

    def _on_closing(self):
        if self._closing:
            return True
        if self._tray is not None:
            self._window.hide()
            log.info('Workspace hidden; coordinator remains active')
        else:
            log.warning('Tray is unavailable; use Quit Forge to stop the coordinator')
        return False

    def _close(self):
        # Kept as a compatibility callback, but normal close now hides to tray.
        return self._on_closing()

    def quit(self):
        if self._closing:
            return {'ok': True}
        self._closing = True
        if self._service and getattr(self._service, 'host_dictation', None):
            self._service.host_dictation.cancel()
        if self._lifecycle:
            self._lifecycle.quit()
        if self._hotkey:
            self._hotkey.stop()
        if self._tray:
            self._tray.stop()
        if self._window:
            self._window.destroy()
        return {'ok': True}


def setup_logging():
    directory = data_directory()
    log_dir = directory / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'forge.log', maxBytes=500000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    logging.getLogger('pywebview').addHandler(handler)
    threading.excepthook = lambda args: log.error('Unhandled worker failure', exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    return directory


def _tray(api):
    try:
        import pystray
        from PIL import Image, ImageDraw
        icon_path = BASE_DIR / 'assets' / 'forge.ico'
        if icon_path.exists():
            picture = Image.open(icon_path)
        else:
            picture = Image.new('RGBA', (64, 64), '#171717')
            ImageDraw.Draw(picture).polygon([(32, 7), (55, 32), (32, 57), (9, 32)], fill='#ed813b')
        menu = pystray.Menu(pystray.MenuItem('Open Forge', lambda icon, item: api.show(), default=True),
                            pystray.MenuItem('Stop all agents', lambda icon, item: api._lifecycle.emergency_stop()),
                            pystray.MenuItem('Quit Forge', lambda icon, item: api.quit()))
        api._tray = pystray.Icon('Forge', picture, 'Forge — local AI workspace', menu)
        api._tray.run_detached()
        def notify(kind,summary):
            if not api._tray or api._closing: return
            message={'approval':'A tool action needs your approval.','outcome_unknown':'Inspect an interrupted action in Forge.'}.get(kind,
                'Your Forge run '+str(summary.get('status','finished'))+'.')
            try: api._tray.notify(message,'Forge')
            except Exception: pass
        api._ensure_service().desktop_notify=notify
    except Exception:
        api._tray = None
        log.exception('Tray startup failed')


def main():
    if '--smoke-test' in sys.argv and not os.getenv('FORGE_DATA_DIR'):
        raise ValueError('Native smoke requires an explicit disposable FORGE_DATA_DIR.')
    if '--forge-update' in sys.argv:
        import argparse
        import subprocess
        from forge_updates import helper_main
        parser=argparse.ArgumentParser()
        parser.add_argument('--forge-update',required=True)
        parser.add_argument('--forge-home',required=True)
        parser.add_argument('--forge-parent-pid',required=True,type=int)
        args=parser.parse_args()
        result=helper_main(args.forge_home,args.forge_update,args.forge_parent_pid)
        if result.get('restart_command'):
            subprocess.Popen(result['restart_command'],creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        return
    import webview
    import uvicorn
    from model_manager import create_app
    directory = setup_logging()
    singleton = SingleCoordinator(directory)
    if not singleton.acquire():
        if os.name == 'nt':
            import ctypes
            user = ctypes.windll.user32
            user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            user.FindWindowW.restype = ctypes.c_void_p
            hwnd = user.FindWindowW(None, 'Forge')
            if hwnd:
                user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                user.IsIconic.argtypes = [ctypes.c_void_p]
                user.IsIconic.restype = ctypes.c_int
                # Showing a hidden maximized workspace must not restore it to
                # windowed bounds. A currently minimized window still needs Restore.
                user.ShowWindow(hwnd, 9 if user.IsIconic(hwnd) else 5)
                user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                user.SetForegroundWindow(hwnd)
        singleton.close()
        return
    api = Bridge()
    sock = None
    try:
        service = api._ensure_service()
        app = create_app(service=service, auth=api._pairing)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', int(os.getenv('FORGE_PORT', '0'))))
        sock.listen(128)
        api._server_url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        api._lifecycle.server = server
        server_thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name='forge-coordinator-http', daemon=True)
        server_thread.start()
        background = getattr(service, 'start_background', None)
        if background:
            background()
        shortcut = service.dispatch('settings', {}).get('emergency_shortcut', 'Ctrl+Alt+Shift+S')
        api._hotkey = EmergencyHotkey(api._lifecycle.emergency_stop, shortcut)
        api._hotkey.start()
        _tray(api)
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
        api._window = webview.create_window('Forge', url=api._server_url, js_api=api,
                                           width=1180, height=820, min_size=(360, 150),
                                           maximized=True,
                                           hidden='--background' in sys.argv,
                                           background_color='#171717', text_select=True)
        api._window.events.closing += api._on_closing
        api._window.events.shown += api.restrict_navigation
        icon = BASE_DIR / 'assets' / 'forge.ico'
        options = dict(gui='edgechromium', private_mode=False, storage_path=str(directory / 'webview'))
        if icon.exists():
            options['icon'] = str(icon)
        if '--smoke-test' in sys.argv:
            def smoke():
                result = {}
                try:
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        result['ready'] = api._window.evaluate_js('document.readyState')
                        result['title'] = api._window.evaluate_js('document.title')
                        result['ui'] = api._window.evaluate_js("({composer:!!document.querySelector('.composer'), textarea:!!document.querySelector('.composer textarea'), navigation:document.querySelectorAll('nav[aria-label=\"Workspace navigation\"] button').length})")
                        if result['ready'] == 'complete' and result['title'] == 'Forge' and result['ui']['composer']:
                            break
                        time.sleep(.2)
                    if not result.get('ui', {}).get('composer') or not result['ui']['textarea']:
                        raise ValueError('React workspace did not render its composer.')
                    result['launch_maximized'] = api._native(lambda form: str(form.WindowState) == 'Maximized')
                    if not result['launch_maximized']:
                        raise ValueError('Forge did not launch with its workspace maximized.')
                    # Use the real first-run dismissal; modal browser guards
                    # must stay active in the production path and this fixture.
                    result['setup_dismissed'] = api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('dialog[open] button')].find(e=>e.textContent.trim()==='Skip for now');if(button){button.click();return true;}return false;})()")
                    if not result['setup_dismissed'] and api.call('setup_status').get('first_run'):
                        deadline = time.monotonic() + 5
                        while not result['setup_dismissed'] and time.monotonic() < deadline:
                            time.sleep(.05)
                            result['setup_dismissed'] = api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('dialog[open] button')].find(e=>e.textContent.trim()==='Skip for now');if(button){button.click();return true;}return false;})()")
                    deadline = time.monotonic() + 5
                    while api._window.evaluate_js("!!document.querySelector('dialog[open]')") and time.monotonic() < deadline:
                        time.sleep(.05)
                    starters = [skill for skill in api.call('skills')['skills'] if skill.get('builtin')]
                    if len(starters) != 24 or not all(skill['enabled'] and skill['automatic'] for skill in starters):
                        raise ValueError('The packaged starter library was not prepared correctly.')
                    opened = api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('nav[aria-label=\"Workspace navigation\"] button')].find(e=>e.textContent.trim()==='Library');if(button){button.click();return true;}return false;})()")
                    if not opened: raise ValueError('The Library navigation is missing.')
                    deadline = time.monotonic() + 10
                    cards = 0
                    while cards < 24 and time.monotonic() < deadline:
                        time.sleep(.05)
                        cards = api._window.evaluate_js("document.querySelectorAll('.library-card').length")
                    if cards < 24: raise ValueError('The packaged Discover library did not render.')
                    result['library'] = {'starter_skills': len(starters), 'discover_cards': cards, 'rendered': True}
                    api._window.evaluate_js("document.querySelector('.new-chat').click()")
                    result.update(hud=api.mode('hud'), peek=api.mode('hud', True), full=api.mode('full'),
                                  pin=api.pin(True), unpin=api.pin(False), host=api.call('host_status'),
                                  projects=api.call('projects'), size=api._native(lambda form: [form.Width, form.Height]))
                    result['full_restores_maximized'] = api._native(lambda form: str(form.WindowState) == 'Maximized')
                    if not result['full_restores_maximized']:
                        raise ValueError('Expand did not restore the maximized workspace.')
                    result['close_hides'] = api._on_closing() is False and not api._closing
                    result['background_api'] = api.call('projects')
                    api.show()
                    if '--dictation-smoke' in sys.argv:
                        # SAPI makes a disposable phrase in memory. No microphone
                        # input or user's spoken audio is captured by this test.
                        import wave
                        import win32com.client
                        voice = win32com.client.Dispatch('SAPI.SpVoice')
                        speech = win32com.client.Dispatch('SAPI.SpMemoryStream')
                        speech.Format.Type = 22  # 22.05 kHz, 16-bit mono PCM
                        voice.AudioOutputStream = speech
                        voice.Speak('Hello Forge. Create a new project.', 0)
                        audio = io.BytesIO()
                        with wave.open(audio, 'wb') as writer:
                            writer.setnchannels(1)
                            writer.setsampwidth(2)
                            writer.setframerate(22050)
                            writer.writeframes(bytes(speech.GetData()))
                        current = api.call('dictation_upload', {'audio': base64.b64encode(audio.getvalue()).decode()})
                        deadline = time.monotonic() + 45
                        while current.get('state') == 'transcribing' and time.monotonic() < deadline:
                            time.sleep(.1)
                            current = api.call('dictation_status')
                        audio.close()
                        text = current.get('text', '').lower()
                        if current.get('state') != 'done' or not all(word in text for word in ('forge', 'project')):
                            raise ValueError('Dictation fixture failed: ' + str(current.get('error') or current.get('state')))
                        result['dictation'] = {'state': 'done', 'fixture_transcribed': True,
                            'device': current['device'], 'compute_type': current['compute_type'],
                            'model': current['model'], 'audio_memory_only': True}
                    if '--native-browser-smoke' in sys.argv:
                        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
                        class Fixture(BaseHTTPRequestHandler):
                            def do_GET(self):
                                body = b'''<!doctype html><title>Forge disposable browser fixture</title>
                                <input id="name" placeholder="Fixture name"><button onclick="document.querySelector('#result').innerText=document.querySelector('#name').value">Apply fixture</button><p id="result">Before</p>'''
                                self.send_response(200)
                                self.send_header('Content-Type', 'text/html; charset=utf-8')
                                self.end_headers()
                                self.wfile.write(body)
                            def log_message(self, *args):
                                pass
                        fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
                        threading.Thread(target=fixture.serve_forever, daemon=True).start()
                        try:
                            browser = service.integrations.browser
                            url = 'http://127.0.0.1:' + str(fixture.server_address[1]) + '/'
                            run = {'id': 'native-smoke', 'settings': service.store.get_settings(), 'project_id': None}
                            schema = next(s for s in browser.schemas() if s['function']['name'] == 'browser_navigate')
                            result['browser_permission'] = service.jobs.registry.permission(run, schema, {'url': url})
                            cancel = threading.Event()
                            def tool(name, arguments):
                                outcome = service.jobs.registry.execute(run, name, arguments, cancel)
                                if outcome.get('ok') is False:
                                    raise ValueError(outcome.get('error', 'Native browser fixture failed'))
                                return outcome
                            tool('browser_navigate', {'url': url})
                            deadline = time.monotonic() + 20
                            while browser.native.status()['loading'] and time.monotonic() < deadline:
                                time.sleep(.1)
                            inspected = tool('browser_inspect', {})
                            field = next(t for t in inspected['targets'] if t['tag'] == 'input')
                            tool('browser_type', {'snapshot_id': inspected['snapshot_id'], 'selector': field['selector'], 'text': 'Native WebView2 works'})
                            inspected = tool('browser_inspect', {})
                            button = next(t for t in inspected['targets'] if t['tag'] == 'button')
                            tool('browser_click', {'snapshot_id': inspected['snapshot_id'], 'selector': button['selector']})
                            inspected = tool('browser_inspect', {})
                            if 'Native WebView2 works' not in inspected['text']:
                                raise ValueError('Native form edit/click did not update the fixture.')
                            capture = tool('browser_screenshot', {})
                            result['native_browser'] = dict(engine='WebView2', bridge_exposed=browser.native.view.evaluate('typeof window.pywebview'),
                                fixture_updated=True, screenshot_bytes=Path(capture['artifact']).stat().st_size,
                                same_page_url=inspected['url'] == url)
                            browser.native.dispatch('close')
                        finally:
                            fixture.shutdown()
                            fixture.server_close()
                        # Exercise the separately owned preview controller, not
                        # merely the research browser's shared DOM tools.
                        manager = service.get_builder()
                        project = service.create_project({'name': 'Disposable preview fixture', 'git': False})
                        brief = manager.save({'project_id': project['id'], 'title': 'Native preview fixture',
                            'objective': 'Verify the owned WebView2 preview', 'directory': 'site',
                            'requirements': [{'text': 'Edit the local fixture', 'acceptance': 'A click updates visible text.'}]})
                        from project_tools import ProjectTools
                        files = ProjectTools(project['path'], service.store.home / 'backups')
                        files.make_directory('site')
                        files.write_file('site/index.html', '''<!doctype html><meta charset="utf-8"><title>Forge preview fixture</title>
                        <input placeholder="Preview name"><button onclick="document.querySelector('#result').innerText=document.querySelector('input').value">Apply preview</button>
                        <p id="result">Before</p><script>console.error('FORGE_PREVIEW_DIAGNOSTIC');fetch('/missing-diagnostic').catch(()=>{});</script>''', 'missing')
                        preview = manager.dispatch('preview_start', {'builder_id': brief['id'], 'mode': 'static'})
                        try:
                            opened = api._window.evaluate_js("(()=>{document.querySelector('button[aria-label=\"Close workspace panel\"]')?.click();const b=[...document.querySelectorAll('nav[aria-label=\"Workspace navigation\"] button')].find(e=>e.textContent.trim()==='Builder');if(b){b.click();return true;}return false;})()")
                            if not opened: raise ValueError('Builder navigation is missing.')
                            deadline = time.monotonic()+10
                            while not api._window.evaluate_js("!!document.querySelector('nav[aria-label=\"Builder sections\"]')") and time.monotonic()<deadline:
                                time.sleep(.05)
                            api._window.evaluate_js("(()=>{const b=[...document.querySelectorAll('nav[aria-label=\"Builder sections\"] button')].find(e=>e.textContent.trim()==='Preview');b?.click();document.querySelector('button[aria-label=\"Collapse sidebar\"]')?.click();return true;})()")
                            deadline = time.monotonic()+10
                            while not api._window.evaluate_js("!!document.getElementById('forge-preview-panel')") and time.monotonic()<deadline:
                                time.sleep(.05)
                            # The actual frontend sizes and binds the child to
                            # host-inspected viewport bounds.
                            deadline = time.monotonic()+20
                            while (not manager.previews.browser.status().get('visible') or manager.previews.browser.status().get('loading')) and time.monotonic()<deadline:
                                time.sleep(.1)
                            preview_run = {'id': 'preview-native-smoke', 'project_id': project['id'], 'settings': service.store.get_settings()}
                            def preview_tool(name, arguments):
                                outcome = manager.execute(preview_run, name, {'id': preview['id'], **arguments}, cancel)
                                if outcome.get('ok') is False: raise ValueError(outcome.get('error', 'Preview fixture failed.'))
                                return outcome
                            inspected = preview_tool('preview_inspect', {})
                            target = next(t for t in inspected['targets'] if t['tag']=='input')
                            preview_tool('preview_type', {'snapshot_id': inspected['snapshot_id'], 'selector': target['selector'], 'text': 'Isolated preview works'})
                            inspected = preview_tool('preview_inspect', {})
                            target = next(t for t in inspected['targets'] if t['tag']=='button')
                            arguments = {'id': preview['id'], 'snapshot_id': inspected['snapshot_id'], 'selector': target['selector']}
                            rejected = manager.execute({**preview_run,'id':'other-preview-run'}, 'preview_click', arguments, cancel)
                            if not rejected.get('not_executed'): raise ValueError('Preview accepted another run snapshot.')
                            preview_tool('preview_click', arguments)
                            inspected = preview_tool('preview_inspect', {})
                            if 'Isolated preview works' not in inspected['text']: raise ValueError('Preview click did not update the fixture.')
                            viewport = preview_tool('preview_viewport', {'width':240,'height':240})
                            diagnostics = preview_tool('preview_diagnostics', {})
                            capture = preview_tool('preview_screenshot', {})
                            controller = manager.previews.browser
                            research_profile = browser.native.view.control.CreationProperties.UserDataFolder
                            preview_profile = controller.view.control.CreationProperties.UserDataFolder
                            exposed = controller.view.evaluate('typeof window.pywebview')
                            captured = any('FORGE_PREVIEW_DIAGNOSTIC' in item.get('message','') for item in diagnostics['entries'])
                            if exposed!='undefined' or not captured or str(research_profile)==str(preview_profile):
                                raise ValueError('Preview profile, bridge isolation or diagnostics failed.')
                            result['native_preview'] = {'engine':'WebView2','separate_controller':controller is not browser.native,
                                'separate_profile':str(research_profile)!=str(preview_profile), 'bridge_exposed':exposed,
                                'fixture_updated':True,'snapshot_guarded':True,'diagnostics_captured':captured,
                                'viewport_verified':viewport.get('width')==240 and viewport.get('height')==240,
                                'screenshot_bytes':Path(capture['artifact']).stat().st_size}
                        finally:
                            manager.previews.stop(preview['id'])
                        result['native_preview']['stopped'] = manager.previews.status(preview['id'])['status']=='stopped' and preview['id'] not in manager.previews.active
                except Exception as exc:
                    result['error'] = str(exc)
                finally:
                    destination = os.getenv('LOCAL_AI_SMOKE_PATH')
                    if destination:
                        Path(destination).write_text(json.dumps(result), encoding='utf-8')
                    api.quit()
            webview.start(smoke, **options)
        else:
            webview.start(**options)
        server.should_exit = True
        server_thread.join(timeout=5)
    finally:
        api.quit()
        if sock:
            sock.close()
        singleton.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log.exception('Fatal startup failure')
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, 'Forge could not start. Details were saved in .forge/logs/forge.log.', 'Forge', 16)
        else:
            raise
