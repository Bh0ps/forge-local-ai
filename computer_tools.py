"""Windows tools. The coordinator's permission registry is the authority.

This broker never approves its own calls. All mutating calls use a recent
snapshot from the same run and fail if the target or foreground window changed.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import secrets
import threading
import time


def _tool(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {},
                           'required': required or [], 'additionalProperties': False}}}


def session_available():
    if os.name != 'nt':
        return False
    import ctypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    user.OpenInputDesktop.restype = ctypes.c_void_p
    user.CloseDesktop.argtypes = [ctypes.c_void_p]
    user.CloseDesktop.restype = ctypes.c_bool
    user.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                             ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
    user.GetUserObjectInformationW.restype = ctypes.c_bool
    handle = user.OpenInputDesktop(0, False, 0x0100)
    if not handle:
        return False
    try:
        # The locked desktop can be opened by some processes: require Default.
        size = ctypes.c_uint()
        user.GetUserObjectInformationW(handle, 2, None, 0, ctypes.byref(size))
        name = ctypes.create_unicode_buffer(max(32, size.value // 2 + 1))
        if not user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(size)):
            return False
        return name.value.casefold() == 'default'
    finally:
        user.CloseDesktop(handle)


class _GuardFailure(ValueError):
    def __init__(self, message, not_executed):
        super().__init__(message)
        self.not_executed = not_executed


class ComputerBroker:
    def __init__(self, data_dir=None, desktop_factory=None, clock=time.monotonic,
                 session_check=session_available, foreground=None, process_identity=None):
        self.data_dir = Path(data_dir or Path.home() / '.forge')
        self.desktop_factory = desktop_factory
        self.clock = clock
        self.session_check = session_check
        self.foreground = foreground
        self.process_identity = process_identity
        self._snapshots = {}
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._effect_started = False
        # UIA COM interfaces must be created and consumed on one thread.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='forge-computer')

    def schemas(self):
        target = {'snapshot_id': {'type': 'string'}, 'element_id': {'type': 'integer'}}
        return [
            _tool('computer_windows', 'List visible Windows apps. Requires desktop permission.'),
            _tool('computer_inspect', 'Inspect a window and receive fresh accessibility targets and image.',
                  {'window_id': {'type': 'integer'}}, ['window_id']),
            _tool('computer_focus', 'Focus the window in a fresh snapshot.',
                  {'snapshot_id': {'type': 'string'}}, ['snapshot_id']),
            _tool('computer_click', 'Click one accessibility target from a fresh snapshot.', target,
                  ['snapshot_id', 'element_id']),
            _tool('computer_type', 'Type literal text into the focused target from a fresh snapshot.',
                  {**target, 'text': {'type': 'string', 'maxLength': 12000}}, ['snapshot_id', 'element_id', 'text']),
            _tool('computer_key', 'Press a bounded key combination in the focused snapshot window.',
                  {'snapshot_id': {'type': 'string'}, 'key': {'type': 'string'}}, ['snapshot_id', 'key']),
            _tool('computer_coordinate_click', 'Vision fallback: click snapshot-relative coordinates in a focused window.',
                  {'snapshot_id': {'type': 'string'}, 'x': {'type': 'integer'}, 'y': {'type': 'integer'}},
                  ['snapshot_id', 'x', 'y']),
            _tool('computer_clipboard_read', 'Read the system clipboard text.'),
            _tool('computer_clipboard_write', 'Copy text to the system clipboard.',
                  {'text': {'type': 'string', 'maxLength': 12000}}, ['text']),
        ]

    def _desktop(self):
        if self.desktop_factory:
            return self.desktop_factory()
        if os.name != 'nt':
            self._guard('Computer tools require the Forge Windows host.')
        try:
            from pywinauto import Desktop
        except ImportError:
            self._guard('Install requirements-host.txt to enable Windows computer tools.')
        return Desktop(backend='uia')

    @staticmethod
    def _identity(window):
        runtime = getattr(window.element_info, 'runtime_id', None)
        runtime = runtime() if callable(runtime) else runtime
        return (int(window.handle), int(window.process_id()), str(window.window_text()),
                tuple(runtime) if runtime is not None else None)

    @staticmethod
    def _password(element):
        info = element.element_info
        if getattr(info, 'is_password', False):
            return True
        underlying = getattr(info, 'element', None)
        try:
            return bool(underlying.CurrentIsPassword) if underlying is not None else False
        except Exception:
            # A failed UIA read must not turn a password field into an editable target.
            return True

    @staticmethod
    def _fingerprint(element):
        info = element.element_info
        runtime = getattr(info, 'runtime_id', None)
        runtime = runtime() if callable(runtime) else runtime
        rect = element.rectangle()
        value = (runtime, getattr(info, 'automation_id', ''), getattr(info, 'control_type', ''),
                 element.window_text(), rect.left, rect.top, rect.right, rect.bottom)
        return hashlib.sha256(repr(value).encode()).hexdigest()

    @staticmethod
    def _run(context):
        return str((context or {}).get('run_id') or (context or {}).get('id') or 'interactive')

    def _check(self, context):
        if not self.session_check():
            self._guard('Desktop session is unavailable. Unlock Windows and resume the run.')
        cancel = (context or {}).get('cancel')
        if self._cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
            self._guard('Computer action cancelled.')

    def _guard(self, message):
        raise _GuardFailure(message, not_executed=not self._effect_started)

    def stop(self):
        self._cancelled.set()
        with self._lock:
            self._snapshots.clear()

    def reset(self):
        self._cancelled.clear()

    def close(self):
        self.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)

    shutdown = close

    def _find(self, handle):
        if type(handle) is not int or handle <= 0:
            self._guard('Select a window_id from computer_windows.')
        try:
            return self._desktop().window(handle=handle).wrapper_object()
        except _GuardFailure:
            raise
        except Exception:
            self._guard('Target window is unavailable. Inspect Windows apps again.')

    def _active_handle(self):
        if self.foreground:
            return int(self.foreground())
        import ctypes
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)

    def _snapshot(self, args, context, focused=True):
        key = args.get('snapshot_id')
        if not isinstance(key, str) or not key:
            self._guard('Choose a snapshot_id from computer_inspect.')
        snapshot = self._snapshots.get(key)
        if not snapshot or snapshot['run'] != self._run(context):
            self._guard('Unknown snapshot. Inspect the target window again.')
        if self.clock() - snapshot['time'] > 30:
            self._guard('Snapshot expired. Inspect the target window again.')
        window = self._find(snapshot['identity'][0])
        if self._identity(window) != snapshot['identity'] or not window.is_visible():
            self._guard('Target window changed. Inspect it again.')
        rect = window.rectangle()
        if (rect.left, rect.top, rect.right, rect.bottom) != snapshot['rect']:
            self._guard('Window moved. Inspect it again before acting.')
        if focused and self._active_handle() != window.handle:
            self._guard('Target is not focused. Use computer_focus and inspect again.')
        return snapshot, window

    def _element(self, args, snapshot):
        index = args.get('element_id')
        if type(index) is not int or not 0 <= index < len(snapshot['elements']):
            self._guard('Choose an element_id from the current snapshot.')
        element, fingerprint = snapshot['elements'][index]
        if not element.is_visible() or not element.is_enabled() or self._fingerprint(element) != fingerprint:
            self._guard('Accessibility target changed. Inspect the window again.')
        if self._password(element):
            self._guard('Password fields cannot be controlled by Forge.')
        return element

    def describe_target(self, args=None, context=None):
        """Resolve approval identity on the UIA thread without changing focus.

        App identities use the canonical executable path, not a title or basename.
        A rejected stale target returns a proven pre-effect failure; callers must
        not fall back to a broader project/app permission for that request.
        """
        try:
            return self._executor.submit(self._describe_target, args, context).result()
        except _GuardFailure as exc:
            return {'ok': False, 'error': str(exc), 'not_executed': True}

    def _describe_target(self, args=None, context=None):
        self._effect_started = False
        args = {} if args is None else args
        if not isinstance(args, dict):
            self._guard('Expected tool arguments object.')
        with self._lock:
            self._check(context)
            if 'snapshot_id' in args:
                _, window = self._snapshot(args, context, focused=False)
            elif 'window_id' in args:
                window = self._find(args['window_id'])
                if not window.is_visible():
                    self._guard('Target window is unavailable. Inspect Windows apps again.')
            else:
                return {'app': 'windows:desktop', 'target': 'Windows desktop and clipboard', 'window_id': None}
            identity = self._identity(window)
            try:
                if self.process_identity:
                    executable = self.process_identity(identity[1])
                else:
                    import psutil
                    executable = psutil.Process(identity[1]).exe()
                executable = str(Path(executable).resolve()) if executable else ''
            except Exception:
                self._guard('Cannot identify the target app. Inspect Windows apps again.')
            if not executable:
                self._guard('Cannot identify the target app. Inspect Windows apps again.')
            if self._identity(window) != identity:
                self._guard('Target window changed while identifying the app. Inspect it again.')
            self._check(context)
            return {'app': os.path.normcase(executable), 'target': executable + ': ' + identity[2][:200],
                    'window_id': identity[0]}

    def execute(self, name, args=None, context=None):
        try:
            return self._executor.submit(self._execute, name, args, context).result()
        except _GuardFailure as exc:
            if exc.not_executed:
                return {'ok': False, 'error': str(exc), 'not_executed': True}
            raise

    def _execute(self, name, args=None, context=None):
        self._effect_started = False
        args = args or {}
        if not isinstance(args, dict):
            self._guard('Expected tool arguments object.')
        with self._lock:
            self._check(context)
            if name == 'computer_windows':
                windows = []
                for window in self._desktop().windows(visible_only=True)[:100]:
                    try:
                        identity = self._identity(window)
                        if identity[2]:
                            windows.append({'window_id': identity[0], 'process_id': identity[1], 'title': identity[2][:200]})
                    except Exception:
                        continue
                return {'windows': windows}
            if name == 'computer_inspect':
                window = self._find(args.get('window_id'))
                rect = window.rectangle()
                entries, elements = [], []
                for element in [window] + window.descendants()[:300]:
                    try:
                        if not element.is_visible():
                            continue
                        info, box = element.element_info, element.rectangle()
                        password = self._password(element)
                        entries.append({'element_id': len(elements), 'name': '' if password else element.window_text()[:500],
                                        'type': str(getattr(info, 'control_type', '')), 'enabled': element.is_enabled(),
                                        'password': password,
                                        'bounds': [box.left - rect.left, box.top - rect.top,
                                                   box.right - rect.left, box.bottom - rect.top]})
                        elements.append((element, self._fingerprint(element)))
                    except Exception:
                        continue
                picture = window.capture_as_image()
                original_size = picture.size
                picture.thumbnail((1600, 1600))
                image = io.BytesIO()
                picture.convert('RGB').save(image, 'JPEG', quality=80)
                key = secrets.token_urlsafe(18)
                self._snapshots = {k: v for k, v in self._snapshots.items() if self.clock() - v['time'] < 30}
                if len(self._snapshots) >= 32:
                    self._snapshots.pop(next(iter(self._snapshots)))
                self._snapshots[key] = {'run': self._run(context), 'time': self.clock(), 'identity': self._identity(window),
                                        'rect': (rect.left, rect.top, rect.right, rect.bottom), 'elements': elements,
                                        'image_size': picture.size, 'original_size': original_size}
                return {'snapshot_id': key, 'window_id': window.handle, 'focused': self._active_handle() == window.handle,
                        'elements': entries, 'image': base64.b64encode(image.getvalue()).decode(),
                        'mime_type': 'image/jpeg', 'image_size': list(picture.size), 'expires_in': 30}
            if name.startswith('computer_clipboard_'):
                import pyperclip
                if name == 'computer_clipboard_read':
                    return {'text': str(pyperclip.paste())[:12000]}
                if name == 'computer_clipboard_write':
                    text = args.get('text')
                    if not isinstance(text, str) or len(text) > 12000:
                        self._guard('Clipboard text is limited to 12000 characters.')
                    self._effect_started = True
                    pyperclip.copy(text)
                    return {'ok': True}
            if name not in {'computer_focus', 'computer_click', 'computer_type', 'computer_key', 'computer_coordinate_click'}:
                self._guard('Unknown computer tool.')
            snapshot, window = self._snapshot(args, context, focused=name != 'computer_focus')
            self._check(context)
            if name == 'computer_focus':
                self._effect_started = True
                window.set_focus()
            elif name == 'computer_click':
                element = self._element(args, snapshot)
                self._check(context)
                self._effect_started = True
                element.click_input()
            elif name == 'computer_type':
                text = args.get('text')
                if not isinstance(text, str) or len(text) > 12000:
                    self._guard('Text is limited to 12000 characters.')
                element = self._element(args, snapshot)
                self._check(context)
                self._effect_started = True
                element.set_focus()
                self._check(context)
                if self._active_handle() != window.handle:
                    self._guard('Window focus changed before typing.')
                # VK_PACKET sends literal Unicode, avoiding brace/key interpolation.
                from pywinauto.keyboard import send_keys
                escaped = ''.join('{' + ch + '}' if ch in '{}+^%~()' else ch for ch in text)
                send_keys(escaped, with_spaces=True, with_tabs=True, with_newlines=True, vk_packet=True, pause=0.01)
            elif name == 'computer_key':
                allowed = {'enter': '{ENTER}', 'escape': '{ESC}', 'tab': '{TAB}', 'backspace': '{BACKSPACE}',
                           'delete': '{DELETE}', 'up': '{UP}', 'down': '{DOWN}', 'left': '{LEFT}', 'right': '{RIGHT}',
                           'ctrl+a': '^a', 'ctrl+c': '^c', 'ctrl+v': '^v', 'ctrl+z': '^z', 'ctrl+s': '^s',
                           'shift+tab': '+{TAB}', 'pageup': '{PGUP}', 'pagedown': '{PGDN}'}
                key = str(args.get('key', '')).lower()
                if key not in allowed:
                    self._guard('Unsupported key. Use enter, escape, tab, arrows or Ctrl+A/C/V/Z/S.')
                from pywinauto.keyboard import send_keys
                self._check(context)
                self._effect_started = True
                send_keys(allowed[key])
            else:
                x, y = args.get('x'), args.get('y')
                width, height = snapshot['image_size']
                if type(x) is not int or type(y) is not int or not 0 <= x < width or not 0 <= y < height:
                    self._guard('Coordinates must be inside the snapshot image.')
                from pywinauto import mouse
                rect = snapshot['rect']
                self._check(context)
                self._effect_started = True
                mouse.click(coords=(rect[0] + round(x * snapshot['original_size'][0] / width),
                                    rect[1] + round(y * snapshot['original_size'][1] / height)))
            self._snapshots.pop(args['snapshot_id'], None)
            return {'ok': True, 'inspect_again': True}
