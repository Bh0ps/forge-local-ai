"""Windows tools. The coordinator's permission registry is the authority.

This broker never approves its own calls. Targeted mutations use a recent
same-run snapshot. Pointer/key input also requires the target foreground window;
the explicit UIA value action requests neither focus nor pointer/key input.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from concurrent.futures import CancelledError, ThreadPoolExecutor
import hashlib
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
                 session_check=session_available, foreground=None, process_identity=None, capture=None):
        self.data_dir = Path(data_dir or Path.home() / '.forge')
        self.desktop_factory = desktop_factory
        self.clock = clock
        self.session_check = session_check
        self.foreground = foreground
        self.process_identity = process_identity
        self.capture = capture
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
            _tool('computer_inspect', 'Inspect fresh accessibility targets. Request a target-rendered image when supported; false captures no pixels. Unavailable images do not permit coordinates.',
                  {'window_id': {'type': 'integer'}, 'include_image': {'type': 'boolean', 'default': True}}, ['window_id']),
            _tool('computer_focus', 'Focus the window in a fresh snapshot.',
                  {'snapshot_id': {'type': 'string'}}, ['snapshot_id']),
            _tool('computer_click', 'Click one accessibility target from a fresh snapshot.', target,
                  ['snapshot_id', 'element_id']),
            _tool('computer_type', 'Type literal text into the focused target from a fresh snapshot.',
                  {**target, 'text': {'type': 'string', 'maxLength': 12000}}, ['snapshot_id', 'element_id', 'text']),
            _tool('computer_set_value', 'Set a writable UIA ValuePattern without Forge pointer, key or focus input. App behavior may vary; no desktop isolation.',
                  {**target, 'value': {'type': 'string', 'maxLength': 12000}}, ['snapshot_id', 'element_id', 'value']),
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
        authority=(context or {}).get('authority_check')
        if authority is not None:
            try: admitted=callable(authority) and authority() is True
            except Exception: admitted=False
            if not admitted:
                self._guard('Computer permission changed. Review the exact action before continuing.')
            # Authority reads may wait. Do not let a lock or cancellation which
            # arrived during that read authorize the next native effect.
            if not self.session_check():
                self._guard('Desktop session is unavailable. Unlock Windows and resume the run.')
            if self._cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
                self._guard('Computer action cancelled.')

    def _guard(self, message):
        raise _GuardFailure(message, not_executed=not self._effect_started) from None

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

    def _action_target(self, args, context, *, focused=True, element=False):
        # Permissions may wait on storage. Validate the target after that read,
        # rather than sending input to whichever app became foreground meanwhile.
        self._check(context)
        snapshot, window = self._snapshot(args, context, focused=focused)
        control = self._element(args, snapshot) if element else None
        if element:
            self._snapshot(args, context, focused=focused)
        if not self.session_check():
            self._guard('Desktop session is unavailable. Unlock Windows and resume the run.')
        cancel = (context or {}).get('cancel')
        if self._cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
            self._guard('Computer action cancelled.')
        if focused and self._active_handle() != window.handle:
            self._guard('Window focus changed before input. Inspect the target before continuing.')
        return snapshot, window, control

    def _set_value(self, args, context, snapshot, window):
        value = args.get('value')
        if not isinstance(value, str) or len(value) > 12000:
            self._guard('Value is limited to 12000 characters.')
        element = self._element(args, snapshot)
        try:
            pattern = element.iface_value
            readonly = pattern.CurrentIsReadOnly
            setter = pattern.SetValue
        except Exception:
            self._guard('This control does not expose a readable UI Automation ValuePattern. Inspect another control.')
        if type(readonly) not in (bool, int) or readonly not in (0, 1) or not callable(setter):
            self._guard('Cannot confirm a writable UI Automation ValuePattern. Inspect another control.')
        if readonly:
            self._guard('This accessibility control is read-only. Choose a writable control.')
        # Pattern discovery can cross slow COM reads. Revalidate the exact
        # snapshot afterwards; a changed target must never receive the value.
        snapshot, window = self._snapshot(args, context, focused=False)
        element = self._element(args, snapshot)
        runtime = getattr(element.element_info, 'runtime_id', None)
        runtime = runtime() if callable(runtime) else runtime
        window_runtime = snapshot['identity'][3]
        if (not isinstance(runtime, (tuple, list)) or not runtime
                or not window_runtime or any(type(value) is not int for value in (*window_runtime, *runtime))):
            self._guard('Accessibility identity is unavailable. Inspect a uniquely identified control.')
        box = element.rectangle()
        left, top, right, bottom = snapshot['rect']
        if not (left <= box.left < box.right <= right and top <= box.top < box.bottom <= bottom):
            self._guard('Accessibility control bounds are outside the inspected window. Inspect it again.')
        if int(element.process_id()) != snapshot['identity'][1]:
            self._guard('Accessibility control belongs to a different app. Inspect it again.')
        # The bounds/process reads are also native calls. Close their read gap
        # against the original identity and fingerprint before the provider call.
        self._action_target(args, context, focused=False, element=True)
        # An attempted provider call may already have changed the app when it
        # raises. Retire its snapshot before admission, including unknown effects.
        self._snapshots.pop(args['snapshot_id'], None)
        self._effect_started = True
        try:
            setter(value)
        except Exception:
            self._guard('Accessible value request has an unknown outcome. Inspect the target before continuing.')
        self._check(context)
        return {'ok': True, 'inspect_again': True}

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
        except Exception:
            return {'ok': False, 'error': 'Computer target could not be identified. Inspect Windows apps before continuing.',
                    'not_executed': True}

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
            future = self._executor.submit(self._execute_checked, name, deepcopy(args), dict(context or {}))
        except Exception:
            return {'ok': False, 'error': 'Computer tools are unavailable. Review the run before continuing.',
                    'not_executed': True}
        try:
            return future.result()
        except _GuardFailure as exc:
            if exc.not_executed:
                return {'ok': False, 'error': str(exc), 'not_executed': True}
            raise
        except CancelledError:
            return {'ok': False, 'error': 'Computer action cancelled before dispatch.', 'not_executed': True}
        except Exception:
            raise _GuardFailure('Computer action outcome is unknown. Inspect the target before continuing.', False) from None

    def _execute_checked(self, name, args, context):
        # Keep final admission on the same UIA worker and effect identity. This
        # also guards list/clipboard reads, which may wait inside native code.
        with self._lock:
            try:
                result = self._execute(name, args, context)
                self._check(context)
                return result
            except _GuardFailure:
                if self._effect_started and isinstance(args, dict): self._snapshots.pop(args.get('snapshot_id'), None)
                raise
            except Exception:
                if self._effect_started and isinstance(args, dict): self._snapshots.pop(args.get('snapshot_id'), None)
                self._guard('Computer action outcome is unknown. Inspect the target before continuing.'
                            if self._effect_started else 'Computer action could not start. Inspect the target before continuing.')

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
                include_image = args.get('include_image', True)
                if type(include_image) is not bool:
                    self._guard('include_image must be true or false.')
                window = self._find(args.get('window_id'))
                identity = self._identity(window)
                rect = window.rectangle()
                bounds = (rect.left, rect.top, rect.right, rect.bottom)
                if not window.is_visible():
                    self._guard('Target window is unavailable. Inspect Windows apps again.')
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
                image_fields = {'image_available': False}
                image_size = original_size = None
                if include_image:
                    from forge_window_capture import capture_window
                    captured = (self.capture or capture_window)(identity[0], identity[1], bounds,
                        cancel=(context or {}).get('cancel'), check=lambda: self._check(context))
                    image_fields['capture_reason'] = captured.reason
                    if captured.available:
                        original_size, image_size = captured.original_size, captured.image_size
                        image_fields = {'image': base64.b64encode(captured.png).decode(),
                                        'mime_type': 'image/png', 'image_size': list(image_size),
                                        'image_available': True, 'image_source': 'window_render'}
                # UIA traversal and image capture are separate OS reads. Bind
                # them to the same target rather than blessing its new identity
                # after capture, especially for coordinate-only actions.
                self._check(context)
                current_rect = window.rectangle()
                if (self._identity(window) != identity or not window.is_visible()
                        or (current_rect.left, current_rect.top, current_rect.right, current_rect.bottom) != bounds):
                    self._guard('Target window changed during inspection. Inspect it again.')
                key = secrets.token_urlsafe(18)
                self._snapshots = {k: v for k, v in self._snapshots.items() if self.clock() - v['time'] < 30}
                if len(self._snapshots) >= 32:
                    self._snapshots.pop(next(iter(self._snapshots)))
                self._snapshots[key] = {'run': self._run(context), 'time': self.clock(), 'identity': identity,
                                        'rect': bounds, 'elements': elements,
                                        'has_image': bool(image_fields['image_available']), 'image_size': image_size, 'original_size': original_size}
                return {'snapshot_id': key, 'window_id': window.handle, 'focused': self._active_handle() == window.handle,
                        'elements': entries, **image_fields, 'expires_in': 30}
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
            if name not in {'computer_focus', 'computer_click', 'computer_type', 'computer_set_value', 'computer_key', 'computer_coordinate_click'}:
                self._guard('Unknown computer tool.')
            if name == 'computer_set_value':
                try:
                    snapshot, window = self._snapshot(args, context, focused=False)
                    self._check(context)
                    return self._set_value(args, context, snapshot, window)
                except _GuardFailure:
                    raise
                except Exception:
                    self._guard('Accessibility value target could not be safely validated. Inspect it again.')
            snapshot, window, _ = self._action_target(args, context, focused=name != 'computer_focus')
            if name == 'computer_focus':
                self._effect_started = True
                window.set_focus()
            elif name == 'computer_click':
                _, window, element = self._action_target(args, context, element=True)
                self._effect_started = True
                element.click_input()
            elif name == 'computer_type':
                text = args.get('text')
                if not isinstance(text, str) or len(text) > 12000:
                    self._guard('Text is limited to 12000 characters.')
                from pywinauto.keyboard import send_keys
                escaped = ''.join('{' + ch + '}' if ch in '{}+^%~()' else ch for ch in text)
                _, window, element = self._action_target(args, context, element=True)
                self._effect_started = True
                element.set_focus()
                self._action_target(args, context, element=True)
                # VK_PACKET sends literal Unicode, avoiding brace/key interpolation.
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
                self._action_target(args, context)
                self._effect_started = True
                send_keys(allowed[key])
            else:
                if not snapshot.get('has_image'):
                    self._guard('Coordinate input requires an image snapshot. Inspect again with include_image true.')
                x, y = args.get('x'), args.get('y')
                width, height = snapshot['image_size']
                if type(x) is not int or type(y) is not int or not 0 <= x < width or not 0 <= y < height:
                    self._guard('Coordinates must be inside the snapshot image.')
                from pywinauto import mouse
                rect = snapshot['rect']
                self._action_target(args, context)
                self._effect_started = True
                mouse.click(coords=(rect[0] + round(x * snapshot['original_size'][0] / width),
                                    rect[1] + round(y * snapshot['original_size'][1] / height)))
            self._snapshots.pop(args['snapshot_id'], None)
            return {'ok': True, 'inspect_again': True}
