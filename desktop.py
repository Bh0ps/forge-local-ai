"""Sidekick native host: UI-thread window updates and cancellable work."""
import base64
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import time
from core import Core, BASE_DIR
from runtime import Jobs

log = logging.getLogger('sidekick')

class Bridge:
    def __init__(self):
        self._core = Core()
        self._jobs = Jobs(self._core)
        self._service = None
        self._service_lock = threading.Lock()
        self._window = None
        self._mode = 'full'
        self._full_bounds = None
        self._pin = False
        self._closing = False
        self._capture_lock = threading.Lock()

    def call(self, action, data=None):
        data = data or {}
        try:
            if self._closing: raise ValueError('App is closing.')
            if self._service is None:
                with self._service_lock:
                    if self._service is None:
                        from service import Service
                        self._service = Service(self._core)
                        self._jobs = self._service.jobs
            return self._service.dispatch(action, data)
        except Exception as exc:
            log.warning('Action %s failed: %s', action, type(exc).__name__)
            return {'error': str(exc)[:1000]}

    def _native(self, operation):
        """pywebview bridge methods run off-thread; marshal all Form access."""
        from webview.platforms.winforms import BrowserView
        from System import Action
        form = BrowserView.instances.get(self._window.uid)
        if form is None or self._closing: raise RuntimeError('Window is unavailable.')
        result, done = {}, threading.Event()
        def run():
            try: result['value'] = operation(form)
            except Exception as exc: result['error'] = exc
            finally: done.set()
        if form.InvokeRequired:
            form.BeginInvoke(Action(run))
            if not done.wait(4): raise TimeoutError('Window update timed out.')
        else: run()
        if 'error' in result: raise result['error']
        return result.get('value')

    def pin(self, value):
        try:
            self._pin = bool(value)
            self._native(lambda form: setattr(form, 'TopMost', self._pin or self._mode == 'hud'))
            return {'ok': True}
        except Exception as exc: return {'error': str(exc)}

    def mode(self, mode, expanded=False):
        if mode not in ('full', 'hud'): return {'error': 'Unknown window mode'}
        try:
            def update(form):
                from System.Windows.Forms import Screen, FormWindowState
                if form.WindowState != FormWindowState.Normal: form.WindowState = FormWindowState.Normal
                area = Screen.FromControl(form).WorkingArea
                scale = float(getattr(form, '_scale', 1))
                if mode == 'hud':
                    if self._mode != 'hud':
                        self._full_bounds = (form.Left, form.Top, form.Width, form.Height)
                    width, height = round(620 * scale), round((430 if expanded else 210) * scale)
                    x, y = form.Left, form.Top
                else:
                    x, y, width, height = self._full_bounds or (form.Left, form.Top, round(1000*scale), round(760*scale))
                width, height = min(width, area.Width), min(height, area.Height)
                x, y = max(area.Left, min(x, area.Right-width)), max(area.Top, min(y, area.Bottom-height))
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
        except Exception as exc: return {'error': str(exc)}

    def choose_project(self):
        try:
            import webview
            paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            if not paths: return {'cancelled': True}
            path = Path(paths[0])
            return self.call('create_project', {'path': str(path), 'name': path.name})
        except Exception as exc: return {'error': str(exc)}

    def screenshot(self):
        if not self._capture_lock.acquire(blocking=False): return {'error': 'Capture is already running.'}
        try:
            from PIL import ImageGrab
            self._window.hide()
            time.sleep(0.2)
            picture = ImageGrab.grab()
            picture.thumbnail((1600, 1600))
            buffer = io.BytesIO()
            picture.convert('RGB').save(buffer, 'JPEG', quality=80)
            return {'image': base64.b64encode(buffer.getvalue()).decode()}
        except Exception as exc: return {'error': str(exc)}
        finally:
            if not self._closing:
                try: self._window.show()
                except Exception: log.exception('Restore after capture failed')
            self._capture_lock.release()

    def _close(self):
        self._closing = True
        self._jobs.cancel()
        log.info('Window closed normally')

def setup_logging():
    directory = Path(os.getenv('SIDEKICK_DATA_DIR', str(Path(os.getenv('LOCALAPPDATA', BASE_DIR))/'Sidekick')))
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory/'sidekick.log', maxBytes=500000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    logging.getLogger('pywebview').addHandler(handler)
    threading.excepthook = lambda args: log.error('Unhandled worker failure', exc_info=(args.exc_type,args.exc_value,args.exc_traceback))
    return directory

def main():
    import webview
    directory = setup_logging()
    log.info('Starting Sidekick 3.1, pid=%s', os.getpid())
    api = Bridge()
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
    api._window = webview.create_window('Sidekick', html=(BASE_DIR/'frontend/index.html').read_text(encoding='utf-8'),
        js_api=api, width=1000, height=760, min_size=(400,190), background_color='#131715', text_select=True)
    api._window.events.closed += api._close
    # Dedicated browser cache; conversation data lives in SQLite beside it.
    options = dict(gui='edgechromium', icon=str(BASE_DIR/'assets/sidekick.ico'), private_mode=False, storage_path=str(directory/'webview'))
    if '--smoke-test' in sys.argv:
        def smoke():
            result = {}
            try:
                deadline = time.monotonic()+20
                while time.monotonic()<deadline:
                    ready=api._window.evaluate_js("document.getElementById('connection').textContent")
                    if ready in ('Connected','Offline'): break
                    time.sleep(0.2)
                result['ready']=ready
                result['hud']=api.mode('hud')
                result['pin']=api.pin(True)
                result['peek']=api.mode('hud',True)
                result['full']=api.mode('full')
                result['unpin']=api.pin(False)
                result['title']=api._window.evaluate_js('document.title')
                result['model']=api._window.evaluate_js("document.getElementById('model').value")
                result['size']=api._native(lambda f: [f.Width, f.Height])
                result['projects_api']=api.call('projects',{})
                if os.getenv('SIDEKICK_SMOKE_AGENT')=='1':
                    if not os.getenv('SIDEKICK_DATA_DIR'): raise ValueError('Agent smoke test needs an isolated data directory.')
                    folder=directory/'smoke-project'
                    folder.mkdir(exist_ok=True)
                    (folder/'smoke.txt').write_text('SIDEKICK_NATIVE_OK',encoding='utf-8')
                    project=api.call('create_project',{'path':str(folder),'name':'Native validation'})
                    job=api.call('start_chat',{'project_id':project['id'],'model':'qwen3.5:9b',
                        'text':'Read smoke.txt using read_file and reply with its exact contents. Do not use other tools.',
                        'thinking':False,'tokens':1024,'context':32768,'allow_edits':False})
                    events=[]
                    deadline=time.monotonic()+90
                    while time.monotonic()<deadline:
                        batch=api.call('poll',{'id':job['id']})
                        events.extend(batch.get('events',[]))
                        if batch.get('finished'): break
                        time.sleep(.1)
                    else:
                        api.call('cancel',{'id':job['id']})
                        raise ValueError('Native agent smoke test timed out.')
                    saved=api.call('get_chat',{'id':job['chat_id'],'limit':100})
                    result['agent']={'tools':[e.get('name') for e in events if e.get('type')=='tool' and e.get('state')=='done'],
                        'errors':[e.get('text') for e in events if e.get('type')=='error'],
                        'context':[e for e in events if e.get('type')=='context'],
                        'answer':saved['messages'][-1]['content'],'saved_messages':saved['total_messages']}
                    if result['agent']['errors'] or 'read_file' not in result['agent']['tools'] or 'SIDEKICK_NATIVE_OK' not in result['agent']['answer']:
                        raise ValueError('Native agent smoke test failed.')
                time.sleep(float(os.getenv('SIDEKICK_SMOKE_IDLE','3')))
                result['responsive']=api._window.evaluate_js('document.readyState')
            except Exception as exc: result['error']=str(exc)
            finally:
                Path(os.environ['LOCAL_AI_SMOKE_PATH']).write_text(json.dumps(result),encoding='utf-8')
                api._window.destroy()
        webview.start(smoke, **options)
    else: webview.start(**options)

if __name__ == '__main__':
    # Windows mutex prevents several heavy WebView2 instances on repeated launches.
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, 'Local\\SidekickDesktopV2')
    duplicate = ctypes.get_last_error() == 183
    try:
        if not duplicate: main()
        else:
            user = ctypes.windll.user32
            user.FindWindowW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p]
            user.FindWindowW.restype=ctypes.c_void_p
            hwnd=user.FindWindowW(None,'Sidekick')
            if hwnd:
                user.ShowWindow.argtypes=[ctypes.c_void_p,ctypes.c_int]
                user.SetForegroundWindow.argtypes=[ctypes.c_void_p]
                user.ShowWindow(hwnd,9)
                user.SetForegroundWindow(hwnd)
    except Exception:
        log.exception('Fatal startup failure')
        ctypes.windll.user32.MessageBoxW(0,'Sidekick could not start. Details were saved to LocalAppData/Sidekick/sidekick.log.','Sidekick',16)
    finally:
        if handle: kernel.CloseHandle(handle)
