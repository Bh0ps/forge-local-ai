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
            self._service.host_dictation = Dictation(home)
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
            if action.startswith('dictation_'):
                return service.host_dictation.dispatch(action.removeprefix('dictation_'), data)
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
            def update(form):
                from System.Drawing import Size
                from System.Windows.Forms import Screen, FormWindowState
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
                form.WindowState = FormWindowState.Normal
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
    except Exception:
        api._tray = None
        log.exception('Tray startup failed')


def main():
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
                user.ShowWindow(hwnd, 9)
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
                    result.update(hud=api.mode('hud'), peek=api.mode('hud', True), full=api.mode('full'),
                                  pin=api.pin(True), unpin=api.pin(False), host=api.call('host_status'),
                                  projects=api.call('projects'), size=api._native(lambda form: [form.Width, form.Height]))
                    result['close_hides'] = api._on_closing() is False and not api._closing
                    result['background_api'] = api.call('projects')
                    api.show()
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
