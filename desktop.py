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
_ACTIVE_SMOKE_CHECKPOINT = None
SMOKE_PHASES = frozenset({'startup-imports', 'startup-imports-ready', 'logging-start', 'logging-ready',
    'singleton-start', 'singleton-ready', 'service-start', 'service-ready', 'http-start', 'http-ready',
    'background-start', 'background-ready', 'host-hooks-start', 'host-hooks-ready', 'window-create',
    'window-created', 'webview-start', 'startup-failed', 'smoke-failed', 'smoke-complete',
    'workspace-ready', 'workspace-size', 'handoff-floor', 'library', 'host-lifecycle', 'research-start', 'research-navigate',
    'research-load', 'research-input-target', 'research-type', 'research-button-target', 'research-click',
    'research-verify', 'research-screenshot', 'builder-start', 'builder-navigation', 'builder-input-target',
    'builder-button-target', 'builder:preview_inspect', 'builder:preview_type', 'builder:preview_click',
    'builder:preview_viewport', 'builder:preview_diagnostics', 'builder:preview_screenshot',
    'standalone-start', 'standalone-open', 'standalone-layout', 'standalone-viewport',
    'standalone-input-target', 'standalone-button-target', 'standalone-screenshot'})
SMOKE_ERROR_TYPES = frozenset({'ValueError', 'RuntimeError', 'TimeoutError', 'StopIteration', 'KeyError',
    'TypeError', 'AttributeError', 'OSError', 'FileNotFoundError', 'PermissionError', 'BrowserGuard',
    'JavascriptException', 'WebViewException', 'ImportError', 'ModuleNotFoundError'})
SMOKE_STANDALONE_READINESS_FLAGS = ('view_present', 'owner_same', 'snapshot_observed', 'native_observed',
    'owner_visible', 'view_visible', 'status_visible', 'loading_complete', 'dom_visible', 'dom_hud',
    'dom_modal', 'layout_valid', 'native_bound_present', 'bounds_match', 'same_sample', 'deadline_expired')


def _smoke_preview_tool(manager, run, cancel, result, name, args):
    """Check one owned smoke call without repeating a dispatched action."""
    try:
        outcome = manager.execute(run, name, args, cancel)
    except Exception:
        result['tool_failure'] = 'outcome-unknown'
        raise
    if (not isinstance(outcome, dict) or outcome.get('ok') is not True
            or outcome.get('error') or outcome.get('outcome_unknown') is True
            or outcome.get('not_executed') is True):
        category = 'outcome-unknown'
        if isinstance(outcome, dict):
            if outcome.get('performed') or outcome.get('outcome_unknown') is True:
                category = 'outcome-unknown'
            elif outcome.get('not_executed') is True:
                category = 'not-executed'
            elif outcome.get('ok') is False:
                category = 'refused'
        result['tool_failure'] = category
        raise ValueError('Standalone preview tool did not complete successfully.')
    return outcome


def _smoke_preview_readiness_report(diagnostics, observation):
    if type(diagnostics) is dict:
        dict.__setitem__(diagnostics, 'smoke_standalone_readiness', observation.copy())


def _smoke_preview_ready(manager, identifier, *, deadline, clock=time.monotonic, pause=time.sleep, diagnostics=None):
    """Observe stable owned bounds; never show, resize, focus or repeat an action."""
    from embedded_browser import validate_bounds
    from native_browser import BrowserGuard
    browser = manager.previews.browser
    view = browser.view
    previous = None
    observation = dict.fromkeys(SMOKE_STANDALONE_READINESS_FLAGS, False)
    observation.update(view_present=view is not None, iterations=0)

    def before_deadline(value):
        observation['deadline_expired'] = value is False
        return value

    def after_deadline(value):
        observation['deadline_expired'] = value is True
        return value

    def owned():
        value = (manager.previews.browser is browser and browser.view is view and view is not None
                 and manager.previews.browser_preview == identifier)
        observation['owner_same'] = value is True
        return value

    try:
        while before_deadline(clock() < deadline):
            observation['iterations'] = min(512, observation['iterations'] + 1)
            for name in SMOKE_STANDALONE_READINESS_FLAGS:
                if name not in ('view_present', 'owner_same', 'deadline_expired'):
                    observation[name] = False
            if not owned():
                break
            state = browser.status()
            if type(state) is dict:
                observation['status_visible'] = dict.get(state, 'visible') is True
                observation['loading_complete'] = dict.get(state, 'loading') is False
            snapshot = view._layout()
            observation['snapshot_observed'] = True
            if type(snapshot) is dict:
                observation['dom_visible'] = dict.get(snapshot, 'visible') is True
                observation['dom_hud'] = dict.get(snapshot, 'hud') is True
                observation['dom_modal'] = dict.get(snapshot, 'modal') is True
            if not owned() or after_deadline(clock() >= deadline):
                break
            def native(owner):
                main = owner.webview
                return (bool(owner.Visible), bool(view.visible), view.bound,
                        (main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height))
            visible, bound_visible, bound, client = view.ui(native)
            observation.update(native_observed=True, owner_visible=visible is True,
                               view_visible=bound_visible is True, native_bound_present=bound is not None)
            if not owned() or after_deadline(clock() >= deadline):
                break
            sample = None
            try:
                bounds = validate_bounds({'panel_id': 'forge-preview-panel'}, snapshot, client, 'forge-preview-panel')
                observation['layout_valid'] = True
                if visible and bound_visible and state.get('visible') is True and state.get('loading') is False and (bounds_match := bounds == bound):
                    observation['bounds_match'] = bounds_match is True
                    sample = (bounds, client, tuple(snapshot['rect'][key] for key in ('x', 'y', 'width', 'height')),
                              tuple(snapshot['viewport'][key] for key in ('width', 'height')))
            except (BrowserGuard, TypeError, KeyError):
                pass
            if sample is not None and (same_sample := sample == previous):
                observation['same_sample'] = same_sample is True
                if owned() and before_deadline(clock() < deadline):
                    return True
            previous = sample
            remaining = deadline - clock()
            if remaining > 0:
                pause(min(.05, remaining))
        raise ValueError('The standalone preview pane did not open.')
    finally:
        try:
            _smoke_preview_readiness_report(diagnostics, observation)
        except Exception:
            pass  # Optional diagnostics cannot replace the original outcome.


class _SmokeCheckpoint:
    """Fixed smoke-only labels; this writer never receives exception text or state."""
    def __init__(self, home, report):
        self.root = self.path = self.home = None
        try:
            if not isinstance(home, str) or not home or not isinstance(report, str) or not report:
                return
            root = Path(os.path.abspath(report)).parent
            data = Path(os.path.abspath(home))
            if data.parent != root or data.name != 'state' or not self._plain(root):
                return
            try:
                data.lstat()
            except FileNotFoundError:
                pass
            else:
                if not self._plain(data): return
            self.root, self.path, self.home = root, root / 'native-smoke-checkpoint.json', data
        except Exception:
            pass

    @staticmethod
    def _plain(path, *, file=False):
        import stat
        current = path
        first = True
        while True:
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not (first and file):
                    return False
            else:
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    return False
                if first and file:
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        return False
                elif not stat.S_ISDIR(info.st_mode):
                    return False
            if current.parent == current:
                return True
            first = False
            current = current.parent

    def write(self, phase, *, error_type=None, error_category='none'):
        temporary = None
        try:
            if (self.path is None or phase not in SMOKE_PHASES
                    or error_category not in ('none', 'startup-failure', 'smoke-failure')
                    or not self._plain(self.path, file=True)):
                return False
            try:
                self.home.lstat()
            except FileNotFoundError:
                pass
            else:
                if not self._plain(self.home): return False
            payload = {'schema': 1, 'phase': phase, 'error_type':
                None if error_type is None else error_type if error_type in SMOKE_ERROR_TYPES else 'unclassified',
                'error_category': error_category}
            raw = (json.dumps(payload, separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')
            if len(raw) > 2048:
                return False
            import tempfile
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.smoke-checkpoint-', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            if not self._plain(self.path, file=True) or not self._plain(temporary, file=True):
                return False
            os.replace(temporary, self.path)
            temporary = None
            return True
        except Exception:
            return False
        finally:
            if temporary is not None:
                try:
                    if self._plain(temporary, file=True): temporary.unlink(missing_ok=True)
                except Exception:
                    pass


def _smoke_checkpoint(phase, *, error_type=None, error_category='none'):
    if ('--smoke-test' in sys.argv and '--forge-update' not in sys.argv
            and '--forge-update-protocol-smoke' not in sys.argv and _ACTIVE_SMOKE_CHECKPOINT is not None):
        _ACTIVE_SMOKE_CHECKPOINT.write(phase, error_type=error_type, error_category=error_category)


def _smoke_native_through(arguments, environment):
    """Private isolation selector; standard smoke always exercises every stage."""
    selected = [value for value in arguments
                if value == '--smoke-native-through' or value.startswith('--smoke-native-through=')]
    if not selected:
        return 'full'
    if (len(selected) != 1 or selected[0] not in
            ('--smoke-native-through=research', '--smoke-native-through=builder')
            or '--smoke-test' not in arguments or '--native-browser-smoke' not in arguments
            or not environment.get('FORGE_DATA_DIR')):
        raise ValueError('Native stage selection requires a disposable browser smoke.')
    return selected[0].split('=', 1)[1]


def _smoke_cleanup_marker(step, phase):
    """Fixed, best-effort teardown evidence only for an active disposable smoke."""
    if (_ACTIVE_SMOKE_CHECKPOINT is None
            or step not in ('tray', 'dictation', 'pairing', 'emergency-stop', 'service',
                            'server', 'lifecycle', 'hotkey', 'window', 'main-wait')
            or phase not in ('started', 'completed')):
        return
    try:
        log.info('Native smoke cleanup %s %s', step, phase)
    except Exception:
        pass  # Diagnostics cannot alter cleanup or its actual outcome.


def _smoke_handoff_pristine(api):
    """Read owned disposable evidence; never changes native update authority."""
    from forge_native_controller import NativeController
    from forge_native_handoff import NativeAuthority
    from forge_update_handoff import HandoffCoordinator
    from forge_updates import UpdateManager
    try:
        configured = os.environ.get('FORGE_DATA_DIR')
        service = api._service
        home = Path(os.path.abspath(service.store.home))
        if (not configured or home != Path(os.path.abspath(configured)) or not _SmokeCheckpoint._plain(home)
                or type(api._update_authority) is not NativeAuthority or api._closing is not False
                or api._quit_complete is not None):
            raise ValueError()
        with service.admission_lock:
            authority = api._update_authority
            with authority.lock:
                if authority._attempt is not None or authority.status() != {'phase': 'idle', 'locked': False}:
                    raise ValueError()
            controller = api._update_controller
            manager = service.update_manager
            if type(manager) is not UpdateManager or manager.service is not service or manager.home != service.store.home:
                raise ValueError()
            with manager.lock:
                if (manager.applying is not False or manager.shutting_down is not False or manager.inspection_required is not False
                        or manager.state.get('state') != 'idle'
                        or manager.thread is not None and manager.thread.is_alive() is not False
                        or manager.state.get('verified') is not None and manager.state.get('verified') is not False
                        or any(manager.state.get(key) is not None for key in ('downloaded', 'signature', 'operation_id'))):
                    raise ValueError()
            if controller is not None and (type(controller) is not NativeController or controller.manager is not manager
                    or controller.authority is not authority or any(getattr(controller, key) is not None
                        for key in ('attempt', 'reservation', 'prepared', 'transport', 'commit', 'stopped_receipt', 'abort_worker'))):
                raise ValueError()
            native = manager.native_handoff
            if native is not None and (type(native) is not HandoffCoordinator or native.manager is not manager
                    or native.authority is not authority or native.active is not None):
                raise ValueError()
            root = home / 'runtimes' / 'updates'
            if manager.root != root or not _SmokeCheckpoint._plain(root):
                raise ValueError()
            operations = root / 'operations'
            try:
                operations.lstat()
            except FileNotFoundError:
                pass
            else:
                if not _SmokeCheckpoint._plain(operations) or any(operations.iterdir()):
                    raise ValueError()
            backups = home / 'backups'
            if not _SmokeCheckpoint._plain(backups) or any(path.name.startswith('update-') for path in backups.iterdir()):
                raise ValueError()
        return {'no_attempt': True, 'no_preparation': True, 'no_helper': True, 'no_restart': True}
    except Exception:
        raise ValueError('Handoff smoke requires pristine un-staged disposable state.') from None


def _smoke_handoff_setup(api):
    """Disable public update checks only in a fresh, explicit disposable smoke home."""
    try:
        service = api._service
        configured = os.environ.get('FORGE_DATA_DIR')
        home = Path(os.path.abspath(service.store.home))
        if not configured or home != Path(os.path.abspath(configured)) or not _SmokeCheckpoint._plain(home):
            raise ValueError()
        with service.admission_lock:
            if service.background is not None or service.store.entities('updates'):
                raise ValueError()
            with service.store._connection() as db:
                # Presence queries inspect no conversation content or names.
                if any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
                        for table in ('chats', 'runs', 'projects')):
                    raise ValueError()
            service.dispatch('update_status', {})
            _smoke_handoff_pristine(api)
            service.dispatch('settings', {'update_checks': False})
    except Exception:
        raise ValueError('Handoff smoke requires pristine un-staged disposable state.') from None


def _smoke_handoff_floor(api):
    """The opt-in smoke uses normal main-window admission, with no staged release."""
    import forge_native_handoff
    _smoke_handoff_pristine(api)
    try:
        enabled = forge_native_handoff.HANDOFF_ENABLED
        if type(enabled) is not bool or api._native_binding() is None:
            raise ValueError()
        fence = api._window.evaluate_js("(()=>{const f=window.__forgeUpdateReadiness;if(!f)return null;const value=f.check({attempt_id:'" +
            '0' * 32 + "',workspace_revision:0,participant_count:0});return {installed:true,invalid_attempt_rejected:value.ok===false,phase:value.phase,participants:value.participant_count};})()")
        if (not isinstance(fence, dict) or fence.get('installed') is not True
                or fence.get('invalid_attempt_rejected') is not True or fence.get('phase') != 'idle'
                or type(fence.get('participants')) is not int or not 0 <= fence['participants'] <= forge_native_handoff.MAX_PARTICIPANTS):
            raise ValueError()
        blocked = api.call('update_handoff_begin', {})
        reason = 'helper_failed' if enabled else 'handoff_unavailable'
        if (not isinstance(blocked, dict) or blocked.get('reason') != reason or blocked.get('phase') != 'failed'
                or any(blocked.get(key) is not True for key in ('blocked', 'aborted', 'not_executed'))
                or any(key in blocked for key in ('error', 'attempt_id', 'challenge', 'confirmation'))):
            raise ValueError()
        unchanged = _smoke_handoff_pristine(api)
        if api._native_binding() is None:
            raise ValueError()
        return {'feature_available': enabled, 'trusted_main': True, 'fence_installed': True,
            'invalid_attempt_rejected': True, 'phase': 'idle', 'participant_count': fence['participants'],
            'typed_block': True, 'trust_floor_blocked': True, 'reason': reason, **unchanged}
    except Exception:
        raise ValueError('Native handoff trust-floor smoke failed.') from None


class _WindowUnavailable(RuntimeError):
    """The owned primary form has disappeared, rather than merely being hidden."""


def _smoke_tray_open(api, *, clock=time.monotonic, pause=time.sleep):
    """Exercise the real asynchronous tray action before observing its result."""
    if api._tray_action('open') is not True:
        raise ValueError('Native tray Open was not admitted.')
    deadline = clock() + 5
    while True:
        with api._tray_actions_lock:
            pending = 'open' in api._tray_actions
        if not pending:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            raise ValueError('Native tray Open did not finish before its deadline.')
        pause(min(.01, remaining))
    return api._native(lambda form: bool(form.Visible and not api._closing and not api._window_closed),
                       _update_safe=True)


def _smoke_workspace_state(window, timeout=25):
    """Wait for the real page/bridge before probing, within one startup budget."""
    deadline = time.monotonic() + timeout
    for event, message in ((window.events.loaded, 'Workspace page load timed out.'),
                           (window.events._pywebviewready, 'Workspace bridge initialization timed out.')):
        if not event.wait(max(0, deadline - time.monotonic())):
            raise ValueError(message)
    while time.monotonic() < deadline:
        result = {'ready': window.evaluate_js('document.readyState'),
                  'title': window.evaluate_js('document.title'),
                  'ui': window.evaluate_js("({composer:!!document.querySelector('.composer'), textarea:!!document.querySelector('.composer textarea'), navigation:document.querySelectorAll('nav[aria-label=\"Workspace navigation\"] button').length})")}
        if (result['ready'] == 'complete' and result['title'] == 'Forge'
                and result['ui'].get('composer') and result['ui'].get('textarea')):
            return result
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    raise ValueError('React workspace did not render its composer.')


class Bridge:
    class _NativeCompletionError(RuntimeError):
        """A submitted operation has no usable, confirmed completion result."""
        def __init__(self, reason):
            self.reason = reason if reason in ('timeout', 'protocol_error', 'completion_unknown', 'pending') else 'protocol_error'
            super().__init__('Window update completion is unknown. Inspect the current state before taking another action.')

    class _NativeCall:
        def __init__(self, form, release, kind):
            self.form, self.release = form, release
            self.kind = kind
            self.done, self.result = threading.Event(), {}
            self.lock = threading.Lock()
            self.async_result = None
            self.wait_handle = None
            self.registering = self.observing = True
            self.ended = self.end_attempted = self.abandoned = self.started = False

    def __init__(self, service=None, pairing=None):
        self._service = service
        self._service_lock = threading.Lock()
        self._window = None
        self._mode = 'full'
        self._full_bounds = None
        self._full_maximized = True
        self._pin = False
        self._closing = False
        self._quit_complete = None
        self._quit_done = threading.Event()
        self._quit_owner = None
        self._window_closed = False
        self._tray_actions_lock = threading.Lock()
        self._tray_actions = set()
        self._tray_workers = set()
        self._owned_teardown = threading.local()
        self._capture_lock = threading.Lock()
        self._pairing = pairing or PairingAuthority()
        self._lifecycle = None
        self._tray = None
        self._tray_started = False
        self._tray_ready = None
        self._tray_startup_done = None
        self._tray_thread = None
        self._tray_failure = False
        self._hotkey = None
        self._server_url = None
        self._navigation_guard = None
        self._visibility_lock = threading.Lock()
        self._visible = True
        self._visibility_seq = 0
        self._visibility_guard = None
        from forge_native_handoff import NativeAuthority
        self._update_authority = NativeAuthority()
        self._update_controller = None
        self._update_controller_lock = threading.Lock()
        self._native_input = None
        self._native_admission = threading.RLock()
        self._native_pending = 0
        self._native_calls_lock = threading.Lock()
        self._native_calls = []
        self._native_session = None
        self._navigation_generation = 0
        self._trusted_loaded = False

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
            from browser_tools import BrowserTools
            integrations.browser.native = NativeBrowser(home, ui_dispatch=self._native,
                indicator_scope=lambda owner, scope: BrowserTools.native_indicator_scope(self._service.store, owner, scope))
        if hasattr(self._service, 'get_builder'):
            self._service.get_builder().previews.attach_browser(self._native)
        if self._lifecycle is None:
            self._lifecycle = HostLifecycle(self._service, self._pairing)
        admission = getattr(self._service, 'admission_lock', None)
        if admission is not None and hasattr(self._pairing, 'bind_update_admission'):
            self._pairing.bind_update_admission(admission, self._pairing_allowed)
            self._service.update_editor_guard = self._pairing.no_active_editors
        with self._native_admission:
            if self._native_input is None:
                from forge_native_input import NativeInputGuard
                self._native_input = NativeInputGuard(self._service.host_dictation)
        self._service.host_dictation_guard = self._native_input.confirm
        self._service.update_native_input_guard = self._update_input_idle
        self._service.host_dictation_dispatch = self._dictation_dispatch
        self._service.host_chat_input_deleted = self._native_input.discard_chat
        return self._service

    def _pairing_allowed(self):
        manager = getattr(self._service, 'update_manager', None)
        if manager is None:
            return not self._closing and not self._update_locked()
        return (not self._closing and not self._update_locked() and getattr(manager, 'applying', None) is False
                and getattr(manager, 'shutting_down', None) is False)

    def _update_locked(self):
        return self._update_authority.status().get('locked') is True

    def _admit_native(self, authorize=None):
        from contextlib import nullcontext
        gate = getattr(self._service, 'admission_lock', None)
        with gate if gate is not None else nullcontext():
            with self._native_admission:
                if authorize is not None and (not callable(authorize) or authorize() is not True):
                    raise ValueError('Pair this browser in Forge Settings.')
                if self._closing or self._update_locked():
                    raise ValueError('Native input is paused while Forge confirms a restart.')
                manager = getattr(self._service, 'update_manager', None)
                if manager and (manager.applying or manager.shutting_down):
                    raise ValueError('Native input is paused while Forge confirms a restart.')
                self._native_pending += 1
        released = False
        def release():
            nonlocal released
            with self._native_admission:
                if not released:
                    released = True
                    self._native_pending -= 1
        return release

    def _native_operation(self, scope=None, authorize=None):
        from contextlib import contextmanager, nullcontext
        @contextmanager
        def operation():
            release = self._admit_native(authorize)
            try:
                with self._native_input.operation(scope) if self._native_input else nullcontext():
                    yield
            finally:
                release()
        return operation()

    def _dictation_dispatch(self, action, data, *, authorize=None):
        # The HTTP host uses this same private callback. Authorization is
        # rechecked at admission, so an already-queued revoked browser request
        # cannot start a late native producer after explicit disconnection.
        from contextlib import nullcontext
        service = self._service
        speech = service.host_dictation
        if action == 'dictation_status':
            gate = getattr(service, 'admission_lock', None)
            with gate if gate is not None else nullcontext():
                if authorize is not None and authorize() is not True:
                    raise ValueError('Pair this browser in Forge Settings.')
                result = speech.status()
            return self._native_input.decorate(result) if self._native_input else result
        scope = None
        if action in ('dictation_start', 'dictation_upload') and data.get('draft_scope') is not None:
            scope = service.get_drafts().scope(data['draft_scope'])
        with self._native_operation(scope, authorize):
            state = speech.status()['state']
            if action in ('dictation_start', 'dictation_upload') and state not in (
                    'starting', 'recording', 'stopping', 'transcribing', 'installing') and hasattr(service.store, 'get_settings'):
                speech.select_model(service.store.get_settings().get('dictation_model', 'base'))
            result = speech.dispatch(action.removeprefix('dictation_'), data)
            if action == 'dictation_install' and data.get('model'):
                service.dispatch('settings', {'dictation_model': data['model']})
        return self._native_input.decorate(result) if self._native_input else result

    def _child_pages(self):
        pages = []
        browser = getattr(getattr(getattr(self._service, 'integrations', None), 'browser', None), 'native', None)
        if browser and getattr(browser, 'view', None) is not None:
            pages.append(browser.view)
        builder = getattr(self._service, 'builder', None)
        if builder is None:
            builder = getattr(self._service, 'builder_manager', None)
        previews = getattr(builder, 'previews', None)
        preview_browser = getattr(previews, 'browser', None)
        if preview_browser and getattr(preview_browser, 'view', None) is not None:
            pages.append(preview_browser.view)
        return pages

    def _update_input_idle(self):
        try:
            if not self._retire_native_calls():
                return False
            with self._native_admission:
                if self._closing or self._native_pending != 0:
                    return False
            if self._child_pages():
                return False
            return self._native_input is not None and self._native_input.idle() is True
        except Exception:
            return False

    def _native_binding(self):
        from forge_native_handoff import NativeBinding
        import psutil
        import secrets
        if (not getattr(sys, 'frozen', False) or self._closing or not self._trusted_loaded
                or self._window is None or getattr(self._window, '_js_api', None) is not self):
            return None
        with self._native_admission:
            generation = self._navigation_generation
            if self._native_session is None:
                self._native_session = secrets.token_hex(32)
        try:
            source = self._native(lambda form: str(form.webview.Source), _update_safe=True)
            target, expected = urlsplit(source), urlsplit(self._server_url)
            if ((target.scheme, target.hostname, target.port) != (expected.scheme, expected.hostname, expected.port)
                    or target.path not in ('', '/') or target.query or target.fragment):
                return None
            parent = psutil.Process(os.getpid())
            with self._native_admission:
                if generation != self._navigation_generation or not self._trusted_loaded or self._closing:
                    return None
                return NativeBinding(self, self._native_session, str(self._window.uid), generation,
                    self._server_url, os.getpid(), parent.create_time())
        except Exception:
            return None

    def _renderer_fence(self, request):
        result, completed = {}, threading.Event()
        def check():
            try:
                script = 'window.__forgeUpdateReadiness?.check(' + json.dumps(request) + ')'
                result['value'] = self._window.evaluate_js(script)
            except Exception:
                pass
            finally:
                completed.set()
        threading.Thread(target=check, name='Forge-update-renderer-fence', daemon=True).start()
        if not completed.wait(5):
            return None
        return result.get('value')

    def _emit_handoff(self, value):
        def emit():
            try:
                if self._window is not None and not self._closing:
                    self._window.evaluate_js("window.dispatchEvent(new CustomEvent('forge:update-handoff',{detail:" +
                        json.dumps(value) + '}));true')
            except Exception:
                pass
        threading.Thread(target=emit, name='Forge-update-handoff-status', daemon=True).start()

    def _handoff_call(self, action, data):
        import forge_native_handoff
        if not forge_native_handoff.HANDOFF_ENABLED:
            return forge_native_handoff.unavailable()
        self._ensure_service()
        if action == 'update_handoff_close_children':
            return self._close_update_children(data)
        if action == 'update_handoff_disconnect_editors':
            return self._disconnect_update_editors(data)
        self._ensure_update_controller()
        if action == 'update_handoff_begin' and self._child_pages():
            return {**forge_native_handoff.unavailable(), 'reason': 'native_input'}
        return self._update_controller.dispatch(action, data)

    def _ensure_update_controller(self):
        with self._update_controller_lock:
            if self._update_controller is None:
                self._service.dispatch('update_status', {})
                from forge_native_controller import NativeController
                self._update_controller = NativeController(self._service.update_manager, self._update_authority,
                    binding=self._native_binding, input_idle=self._update_input_idle,
                    fence=self._renderer_fence, depart=self._handoff_depart, emit=self._emit_handoff)
        return self._update_controller

    def _native_update_status(self, controller, binding):
        result = dict(controller.manager.native_status(binding))
        # Match update_status dispatch: an operation failure is renderable
        # state, rather than a failed bridge request.
        result['operation_error'] = result.pop('error', None)
        available = False
        if (result.get('safe_handoff_available') is True and not self._closing
                and result.get('inspection_required') is not True):
            try:
                from forge_updates import load_operation, plain_path
                from forge_update_proof import BUILD_NAME, read_record, validated_build_manifest
                data = load_operation(controller.manager.home, result.get('operation_id'))
                available = (data['state'] == 'installed' and data.get('trust_mode', 'authenticode') in ('ed25519', 'authenticode')
                    and data['version'] == controller.manager.current_version)
                if available:
                    current = plain_path(controller.manager.install_dir)
                    previous = plain_path(data['backup']) / 'installation'
                    if (plain_path(data['install_dir']) != current or plain_path(data['backup']) !=
                            plain_path(controller.manager.home / 'backups' / ('update-' + data['id']))):
                        available = False
                    else:
                        # Authenticate the bounded records for this offer;
                        # actual tree bytes are checked again before execution.
                        validated_build_manifest(read_record(current / BUILD_NAME), controller.manager.proof_keys,
                            expected_version=data['version'], expected_source=data.get('source_commit'))
                        validated_build_manifest(read_record(previous / BUILD_NAME), controller.manager.proof_keys,
                            expected_version=data['previous_version'], expected_source=data.get('previous_source_commit'))
            except Exception:
                available = False
        # An offer is not authority. Native reservation/prepare verifies the
        # retained/current publisher proofs and exact operation before action.
        return {**result, 'safe_rollback_available': available}

    def _close_update_children(self, data):
        from forge_native_handoff import HandoffRejected
        if data or self._native_binding() is None or self._update_locked():
            raise HandoffRejected('native_input')
        def close(form):
            from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon, DialogResult
            pages = self._child_pages()
            if not pages:
                return {'closed': True, 'cancelled': False}
            answer = MessageBox.Show(form,
                'Close the open browser and preview pages? Unsaved page input will be discarded. Your Forge drafts stay open.',
                'Close browser work', MessageBoxButtons.YesNo, MessageBoxIcon.Warning)
            if answer != DialogResult.Yes:
                return {'closed': False, 'cancelled': True}
            for page in pages:
                page.dispose()
            return {'closed': not self._child_pages(), 'cancelled': False}
        return self._native(close)

    def _disconnect_update_editors(self, data):
        from forge_native_handoff import HandoffRejected
        if data or self._native_binding() is None or self._update_locked():
            raise HandoffRejected('paired_editor')
        def disconnect(form):
            from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon, DialogResult
            answer = MessageBox.Show(form,
                'Save any work in paired browser windows first. Disconnect all paired windows now? Their unsaved input is not saved by this action.',
                'Disconnect paired windows', MessageBoxButtons.YesNo, MessageBoxIcon.Warning)
            if answer != DialogResult.Yes:
                return {'disconnected': False, 'cancelled': True}
            # Session revocation is atomic with new pairing admission. No
            # token, origin, editor identity or browser content is returned.
            with self._service.admission_lock:
                self._pairing.close()
            return {'disconnected': True, 'cancelled': False}
        return self._native(disconnect)

    def _handoff_depart(self, controller, grant, transport):
        # This private callback cannot be requested through bridge JSON. The
        # owned helper has acknowledged READY and COMMITTED; admission is final.
        with self._native_admission:
            if (self._closing or controller is not self._update_controller or self._native_pending
                    or not self._trusted_loaded or self._navigation_generation != grant.binding.navigation
                    or self._native_session != grant.binding.session or self._window is None
                    or getattr(self._window, '_js_api', None) is not self
                    or str(self._window.uid) != grant.binding.window_id):
                return False
            self._update_authority.pipe_admission(grant, transport)
            self._closing = True
            self._quit_owner = threading.get_ident()
            self._quit_done.clear()
        if self._quit_owned() is not True:
            from forge_native_handoff import HandoffRejected
            raise HandoffRejected('helper_failed')
        return True

    def _emergency_stop(self):
        if self._update_controller is not None:
            self._update_controller.invalidate()
        return self._lifecycle.emergency_stop()

    def _checking_persistence(self, service, binding):
        """Only the current native checking lease may flush retained UI work.

        Call under the service admission gate. The native document lookup is
        completed beforehand; these private identity checks do not wait on UI.
        Service dispatch still owns the draft CAS and the preparation fence.
        """
        import forge_native_handoff
        from forge_native_controller import NativeController
        controller = self._update_controller
        if (not forge_native_handoff.HANDOFF_ENABLED or binding is None
                or type(controller) is not NativeController
                or controller.authority is not self._update_authority
                or controller.manager is not getattr(service, 'update_manager', None)
                or not self._update_authority.binding_valid(binding)):
            return False
        reservation = controller.reservation
        if (reservation is None or reservation.binding != binding
                or not self._update_authority.receipt_attempt(controller.attempt, reservation)):
            return False
        coordinator = controller.manager.native_handoff
        try:
            with coordinator.lock:
                return coordinator._current(reservation).phase == 'checking'
        except Exception:
            return False

    def call(self, action, data=None):
        try:
            if self._closing:
                raise ValueError('Forge is shutting down.')
            data = {} if data is None else data
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            if action in ('update_apply', 'update_rollback'):
                # Publisher verification does not prove that window-only work
                # is durable. Only the native handshake can acknowledge it.
                raise ValueError('In-app installation and restore are unavailable in this version. Save your work and use the public installer for a manual update.')
            if action.startswith('update_handoff_'):
                return self._handoff_call(action, data)
            service = self._ensure_service()
            if action == 'update_status':
                import forge_native_handoff
                if forge_native_handoff.HANDOFF_ENABLED and self._native_binding() is not None:
                    controller = self._ensure_update_controller()
                    return self._native_update_status(controller, self._native_binding())
            if self._update_locked() and action not in ('host_status', 'update_status', 'dictation_status',
                    'draft_get', 'draft_confirm', 'emergency_stop'):
                from forge_service import READ_ACTIONS
                if action not in READ_ACTIONS:
                    if action in ('draft_save', 'ui_layout_save'):
                        binding = self._native_binding()
                        controller = self._update_controller
                        from forge_native_controller import NativeController
                        if type(controller) is NativeController and controller.operation_lock.acquire(blocking=False):
                            try:
                                with service.admission_lock:
                                    if self._update_controller is controller and self._checking_persistence(service, binding):
                                        return service.dispatch(action, data)
                            finally:
                                controller.operation_lock.release()
                    raise ValueError('Forge is confirming a restart. Cancel the restart before changing work.')
            if action == 'artifact_save_as':
                # Only an explicit user download opens this dialog. Agent tools
                # cannot choose arbitrary filesystem destinations.
                import webview
                artifact = service.get_builder().documents.download(data)
                with self._native_operation():
                    destination = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=artifact['name'])
                    if not destination:
                        return {'cancelled': True}
                    target = Path(destination[0] if isinstance(destination, (tuple, list)) else destination)
                    raw = base64.b64decode(artifact['content_base64'], validate=True)
                    target.write_bytes(raw)
                return {'ok': True, 'path': str(target), 'sha256': artifact['sha256']}
            if action.startswith('dictation_'):
                return self._dictation_dispatch(action, data)
            if action == 'host_status':
                return {'native': True, 'mode': self._mode, 'pinned': self._pin,
                        **self._visibility_status(),
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
                return self._emergency_stop()
            if action == 'settings' and data.get('emergency_shortcut'):
                EmergencyHotkey.parse(data['emergency_shortcut'])
            result = service.dispatch(action, data)
            if action == 'settings' and data.get('emergency_shortcut') and self._hotkey:
                self._hotkey.stop()
                self._hotkey = EmergencyHotkey(self._emergency_stop, data['emergency_shortcut'])
                self._hotkey.start()
            return result
        except Exception as exc:
            log.warning('Action %s failed: %s', action, type(exc).__name__)
            return {'error': str(exc)[:1000]}

    def _end_native_call(self, call):
        # IsCompleted has already been confirmed outside every native/admission
        # lock. EndInvoke therefore observes this exact delegate without waiting.
        with call.lock:
            if call.end_attempted:
                raise self._NativeCompletionError('completion_unknown')
            call.end_attempted = True
        try:
            call.form.EndInvoke(call.async_result)
        except Exception:
            raise self._NativeCompletionError('completion_unknown') from None
        with call.lock:
            call.ended = True
        failed = False
        try:
            if call.wait_handle is not None:
                call.wait_handle.Close()
        except Exception:
            failed = True
        finally:
            call.release()
            with self._native_calls_lock:
                if call in self._native_calls:
                    self._native_calls.remove(call)
        if failed:
            raise self._NativeCompletionError('protocol_error') from None

    def _retire_native_calls(self):
        # A later existing worker probe may observe an abandoned completion. No
        # timer, thread, WaitOne or action retry is introduced by this cleanup.
        with self._native_calls_lock:
            calls = tuple(self._native_calls)
        for call in calls:
            with call.lock:
                if call.registering or call.observing or call.ended or call.end_attempted:
                    continue
                call.observing = True
            try:
                if call.form.InvokeRequired is not True or call.async_result is None:
                    continue  # Never observe a queued call from its UI thread.
                if call.async_result.IsCompleted is not True:
                    continue
                self._end_native_call(call)
            except Exception:
                pass  # Unknown completion keeps its lease; no details escape.
            finally:
                with call.lock:
                    call.observing = False
        with self._native_calls_lock:
            return not self._native_calls

    def _wait_native_call(self, call):
        from time import monotonic
        deadline = monotonic() + 5
        def remaining():
            value = deadline - monotonic()
            if value <= 0:
                raise self._NativeCompletionError('timeout')
            return value
        try:
            if call.async_result is None:
                raise self._NativeCompletionError('protocol_error')
            if call.done.wait(remaining()) is not True:
                raise self._NativeCompletionError('timeout')
            remaining()
            if call.async_result is None:
                raise self._NativeCompletionError('protocol_error')
            try:
                completed = call.async_result.IsCompleted
                remaining()
                if completed is not True:
                    if completed is not False:
                        raise self._NativeCompletionError('protocol_error')
                    handle = call.async_result.AsyncWaitHandle
                    call.wait_handle = handle
                    milliseconds = max(0, int(remaining() * 1000))
                    waited = handle.WaitOne(milliseconds)
                    remaining()
                    if waited is False:
                        raise self._NativeCompletionError('timeout')
                    if waited is not True:
                        raise self._NativeCompletionError('protocol_error')
                    completed = call.async_result.IsCompleted
                    remaining()
                    if completed is not True:
                        raise self._NativeCompletionError('protocol_error')
            except self._NativeCompletionError:
                raise
            except Exception:
                raise self._NativeCompletionError('protocol_error') from None
            remaining()
            self._end_native_call(call)
            remaining()  # A delayed observation never returns stale usable output.
        finally:
            with call.lock:
                if not call.ended:
                    call.abandoned = True
                call.observing = False

    def _native(self, operation, *, _update_safe=False):
        """Bridge calls run on workers; marshal WinForms operations to its UI thread."""
        from webview.platforms.winforms import BrowserView
        from System import Action
        if self._window is None:
            raise _WindowUnavailable('Window is unavailable.')
        form = BrowserView.instances.get(self._window.uid)
        if form is None:
            raise _WindowUnavailable('Window is unavailable.')
        self._retire_native_calls()
        # Only the owned quit thread may marshal its synchronous stop/dispose
        # calls after admission closes. Other workers and already queued input
        # retain their normal admission checks.
        teardown = self._closing and getattr(self._owned_teardown, 'active', False) is True
        _update_safe = _update_safe or teardown
        release = (lambda: None) if _update_safe else self._admit_native()
        try:
            marshalled = bool(form.InvokeRequired)
        except Exception:
            release()  # No delegate was submitted.
            raise self._NativeCompletionError('protocol_error') from None
        kind = 'teardown' if teardown else 'control' if _update_safe else 'input'
        call = self._NativeCall(form, release, kind)
        def run():
            try:
                with call.lock:
                    if call.abandoned:
                        raise self._NativeCompletionError('timeout')
                    call.started = True
                if not _update_safe and (self._closing or self._update_locked()):
                    raise ValueError('Native input is paused while Forge confirms a restart.')
                call.result['value'] = operation(form)
            except Exception as exc:
                call.result['error'] = exc
            finally:
                if not marshalled:
                    release()
                call.done.set()
        if marshalled:
            with self._native_calls_lock:
                # Separate finite slots keep Open and owned Quit cleanup usable
                # after an unknown input result without an unbounded registry.
                peers = [item for item in self._native_calls if item.kind == kind]
                rejected = len(peers) >= {'input': 16, 'control': 8, 'teardown': 4}[kind]
                if kind == 'input' and any(item.abandoned for item in peers):
                    rejected = True
                if not rejected:
                    self._native_calls.append(call)
            if rejected:
                release()
                raise self._NativeCompletionError('pending')
            try:
                call.async_result = form.BeginInvoke(Action(run))
            except Exception:
                # A registration failure cannot establish that no callback was
                # queued. Keep the private lease and report an unknown outcome.
                with call.lock:
                    call.registering = call.observing = False
                    call.abandoned = True
                raise self._NativeCompletionError('protocol_error') from None
            with call.lock:
                call.registering = False
            self._wait_native_call(call)
        else:
            run()
        if 'error' in call.result:
            raise call.result['error']
        if 'value' not in call.result:
            raise self._NativeCompletionError('protocol_error')
        return call.result['value']

    def pin(self, value):
        try:
            if self._update_locked():
                raise ValueError('Cancel the restart before changing the window.')
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
            if self._update_locked():
                raise ValueError('Cancel the restart before changing the window.')
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
            with self._native_operation():
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
            with self._native_operation():
                paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            return {'path': paths[0]} if paths else {'cancelled': True}
        except Exception as exc:
            return {'error': str(exc)}

    def dictation(self, action, data=None):
        return self.call('dictation_' + str(action), data)

    def screenshot(self):
        if self._update_locked():
            return {'error': 'Cancel the restart before taking a screenshot.'}
        try:
            release = self._admit_native()
        except Exception as exc:
            return {'error': str(exc)}
        if not self._capture_lock.acquire(blocking=False):
            release()
            return {'error': 'Capture is already running.'}
        try:
            from PIL import ImageGrab
            self._window.hide()
            self._publish_visibility(False)
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
                    self._refresh_visibility()
                except Exception:
                    log.exception('Restore after capture failed')
            self._capture_lock.release()
            release()

    def show(self):
        try:
            def focus(form):
                from System.Windows.Forms import FormWindowState
                if self._closing or self._window_closed:
                    return
                if form.WindowState == FormWindowState.Minimized:
                    form.WindowState = (FormWindowState.Maximized
                                        if self._mode == 'full' and self._full_maximized
                                        else FormWindowState.Normal)
                form.Show()
                form.Activate()
                self._publish_visibility(bool(form.Visible and str(form.WindowState) != 'Minimized'))
            self._native(focus, _update_safe=True)
        except _WindowUnavailable:
            # A stale pywebview window silently ignores show(uid) when its form
            # is gone. Do not keep a headless coordinator and an inert tray.
            self._on_window_closed()
            log.warning('Workspace is unavailable; stopping the owned coordinator')
        except Exception:
            log.warning('Could not show Forge', exc_info=True)

    def _tray_action(self, action):
        """Keep pystray's message pump free, with at most one worker per action."""
        callbacks = {'open': self.show, 'stop': self._emergency_stop,
                     'quit': self.quit, 'closed': self._quit_closed_window}
        if action not in callbacks:
            return False
        with self._tray_actions_lock:
            if action in self._tray_actions or self._closing:
                return False
            self._tray_actions.add(action)
        def run():
            try:
                if action in ('open', 'stop') and self._closing:
                    return
                result = callbacks[action]()
                if (action == 'quit' and isinstance(result, dict) and
                        result.get('error') == 'Cancel the restart and confirm cancellation before quitting Forge.'):
                    if self._tray is not None:
                        try:
                            self._tray.notify('Cancel the pending restart in Forge before quitting.', 'Forge')
                        except Exception as exc:
                            log.warning('Tray restart notice failed: %s', type(exc).__name__)
                    self._tray_action('open')
            except Exception as exc:
                log.warning('Tray action %s failed: %s', action, type(exc).__name__)
            finally:
                with self._tray_actions_lock:
                    self._tray_actions.discard(action)
        try:
            worker = threading.Thread(target=run, name='Forge-tray-' + action, daemon=True)
            with self._tray_actions_lock:
                self._tray_workers = {thread for thread in self._tray_workers if thread.is_alive()}
                self._tray_workers.add(worker)
                worker.start()
        except Exception:
            with self._tray_actions_lock:
                self._tray_actions.discard(action)
                if 'worker' in locals():
                    self._tray_workers.discard(worker)
            raise
        return True

    def _on_window_closed(self):
        # pywebview's closed event runs after the primary form is removed from
        # BrowserView.instances. No shutdown or native wait belongs in this
        # callback; an independent worker removes the tray and owned services.
        with self._native_admission:
            self._window_closed = True
            if not self._closing:
                self._trusted_loaded = False
                self._update_authority.update_binding(None)
        try:
            return self._tray_action('closed')
        except Exception as exc:
            log.warning('Closed workspace cleanup worker failed: %s', type(exc).__name__)
            return False

    def _quit_closed_window(self):
        # There is no document left to confirm an update cancellation. Revoke
        # the owned attempt and close the orphan host without reusing its bridge.
        with self._native_admission:
            if self._closing:
                return
            self._closing = True
            self._quit_owner = threading.get_ident()
            self._quit_done.clear()
            self._update_authority.update_binding(None)
        if self._update_controller is not None:
            try:
                self._update_controller.invalidate()
            except Exception as exc:
                log.warning('Closed workspace update cancellation failed: %s', type(exc).__name__)
        self._quit_owned()

    def restrict_navigation(self):
        """Never expose the authenticated native bridge to a third-party page."""
        def attach(form):
            if self._visibility_guard is None:
                def visibility_changed(sender, args):
                    # Native callbacks run on the WinForms thread. Capture the
                    # state here, but never synchronously evaluate JavaScript.
                    self._publish_visibility(bool(form.Visible and str(form.WindowState) != 'Minimized'))
                self._visibility_guard = visibility_changed
                form.VisibleChanged += visibility_changed
                form.Resize += visibility_changed
            if self._navigation_guard:
                return
            expected = urlsplit(self._server_url)
            def guard(sender, args):
                try:
                    with self._native_admission:
                        self._navigation_generation += 1
                        self._trusted_loaded = False
                        self._update_authority.update_binding(None)
                    if self._update_locked():
                        args.Cancel = True
                        if self._update_controller:
                            self._update_controller.invalidate()
                        else:
                            self._update_authority.invalidate()
                        return
                    target = urlsplit(str(args.Uri))
                    if ((target.scheme, target.hostname, target.port) != (expected.scheme, expected.hostname, expected.port)
                            or target.path not in ('', '/') or target.query):
                        args.Cancel = True
                        if target.scheme in ('http', 'https'):
                            import webbrowser
                            webbrowser.open(str(args.Uri))
                except ValueError:
                    args.Cancel = True
            self._navigation_guard = guard
            form.webview.NavigationStarting += guard
        return self._native(attach, _update_safe=True)

    def _visibility_status(self):
        with self._visibility_lock:
            return {'visible': self._visible, 'visibility_seq': self._visibility_seq}

    def _send_visibility(self, payload):
        try:
            if self._window is not None and not self._closing and not self._window_closed:
                self._window.evaluate_js("window.dispatchEvent(new CustomEvent('forge:visibility',{detail:" + json.dumps(payload) + "}));true")
        except Exception:
            # The loaded callback republishes the current snapshot if startup
            # has not yet installed the bridge or React listener.
            log.debug('Native visibility event unavailable', exc_info=True)

    def _publish_visibility(self, visible, snapshot=False):
        with self._visibility_lock:
            changed = self._visible != visible
            if changed:
                self._visible = visible
                self._visibility_seq += 1
            payload = {'visible': self._visible, 'seq': self._visibility_seq}
        if changed or snapshot:
            threading.Thread(target=self._send_visibility, args=(payload,),
                name='Forge-visibility', daemon=True).start()
        return payload

    def _refresh_visibility(self, snapshot=False):
        # Publish inside the marshaled operation so a stale native read cannot
        # overwrite a later hide/restore transition with a newer sequence.
        try:
            if self._closing or self._window_closed:
                return
            def snapshot_visibility(form):
                if self._closing or self._window_closed:
                    return
                return self._publish_visibility(
                    bool(form.Visible and str(form.WindowState) != 'Minimized'), snapshot=snapshot)
            return self._native(snapshot_visibility, _update_safe=True)
        except Exception:
            log.debug('Native visibility snapshot unavailable', exc_info=True)

    def _on_loaded(self):
        with self._native_admission:
            if self._closing or self._window_closed:
                return
        self.restrict_navigation()
        with self._native_admission:
            if self._closing or self._window_closed:
                return
            self._trusted_loaded = True
        binding = self._native_binding()
        with self._native_admission:
            if self._closing or self._window_closed:
                return
            self._update_authority.update_binding(binding)
        self._refresh_visibility(snapshot=True)

    def _on_shown(self):
        with self._native_admission:
            if self._closing or self._window_closed or self._tray_started:
                return
        self.restrict_navigation()
        # pystray must not expose Open/Quit before pywebview has created its
        # primary form. Hidden Windows startup still fires shown (Show→Hide).
        # Serialize registration against Quit so a late shown callback cannot
        # create an icon after owned cleanup has already removed it.
        with self._native_admission:
            if self._closing or self._window_closed or self._tray_started:
                return
            self._tray_started = True
            _tray(self)

    def _on_visibility_event(self):
        self._refresh_visibility()

    def _on_closing(self):
        if self._closing:
            return True
        try:
            if self._update_controller is not None and self._update_locked():
                self._update_controller.invalidate()
            if self._tray is not None:
                self._window.hide()
                self._publish_visibility(False)
                log.info('Workspace hidden; coordinator remains active')
            else:
                log.warning('Tray is unavailable; use Quit Forge to stop the coordinator')
        except Exception as exc:
            # pywebview swallows exceptions from this native event. An escaping
            # hide/cancellation error would remove our explicit False veto and
            # permit disposal of the primary form while its coordinator lives.
            log.warning('Close-to-tray failed: %s', type(exc).__name__)
        return False

    def _close(self):
        # Kept as a compatibility callback, but normal close now hides to tray.
        return self._on_closing()

    def quit(self):
        # Share the admission lock with native update departure. Only one
        # caller may claim cleanup; never hold it while waiting on native UI.
        with self._native_admission:
            if self._closing:
                return ({'ok': True} if self._quit_complete is True else
                    {'ok': False, 'error': 'Forge could not complete owned cleanup.'})
            if self._update_locked():
                if self._update_controller is not None:
                    self._update_controller.invalidate()
                return {'ok': False, 'error': 'Cancel the restart and confirm cancellation before quitting Forge.'}
            self._closing = True
            self._quit_owner = threading.get_ident()
            self._quit_done.clear()
            self._update_authority.update_binding(None)
        complete = self._quit_owned()
        return ({'ok': True} if complete else
            {'ok': False, 'error': 'Forge could not complete owned cleanup.'})

    def _quit_owned(self):
        previous = getattr(self._owned_teardown, 'active', False)
        self._owned_teardown.active = True
        complete = True
        def cleanup(name, callback):
            nonlocal complete
            _smoke_cleanup_marker(name, 'started')
            try:
                callback()
            except Exception as exc:
                complete = False
                log.warning('Owned cleanup %s failed: %s', name, type(exc).__name__)
            else:
                _smoke_cleanup_marker(name, 'completed')
        try:
            # Remove the icon before slow model/service cleanup. Tray callbacks
            # are workers, so this cannot strand the notification message pump.
            tray = self._tray
            if tray:
                def stop_tray():
                    # run_detached returns before pystray is running; its stop
                    # method otherwise silently ignores this startup race.
                    startup = self._tray_startup_done or self._tray_ready
                    if startup is not None and not startup.wait(5):
                        raise TimeoutError('Tray startup did not finish.')
                    if self._tray_ready is not None and not self._tray_ready.is_set():
                        raise RuntimeError('Tray startup failed.')
                    tray.stop()
                    loop = self._tray_thread
                    if loop is not None and loop is not threading.current_thread():
                        loop.join(timeout=5)
                        if loop.is_alive():
                            raise TimeoutError('Tray shutdown did not finish.')
                cleanup('tray', stop_tray)
            if self._service and getattr(self._service, 'host_dictation', None):
                cleanup('dictation', self._service.host_dictation.cancel)
            if self._lifecycle:
                if type(self._lifecycle) is HostLifecycle:
                    # Preserve the owned lifecycle order, while a failing stop
                    # cannot skip service shutdown or closing the HTTP host.
                    self._lifecycle.shutting_down = True
                    cleanup('pairing', self._lifecycle.pairing.close)
                    cleanup('emergency-stop', self._lifecycle.emergency_stop)
                    shutdown = getattr(self._lifecycle.service, 'shutdown', None)
                    if shutdown:
                        cleanup('service', shutdown)
                    if self._lifecycle.server:
                        cleanup('server', lambda: setattr(self._lifecycle.server, 'should_exit', True))
                else:
                    cleanup('lifecycle', self._lifecycle.quit)
            if self._hotkey:
                cleanup('hotkey', self._hotkey.stop)
            if self._window and not self._window_closed:
                cleanup('window', self._window.destroy)
            self._quit_complete = complete and not self._tray_failure
            return self._quit_complete
        finally:
            self._owned_teardown.active = previous
            if self._quit_complete is None:
                self._quit_complete = False
            self._quit_owner = None
            self._quit_done.set()

    def _await_owned_cleanup(self, timeout=30):
        # webview.start returns as soon as the last form closes. Its worker can
        # still be unwinding synchronous pythonnet calls; the main thread must
        # not finalize the interpreter before that owner has finished cleanup.
        if self._quit_owner == threading.get_ident():
            return self._quit_done.is_set() and self._quit_complete is True
        deadline = time.monotonic() + timeout
        if not self._quit_done.wait(max(0, deadline - time.monotonic())) or self._quit_complete is not True:
            return False
        # The completion event is set inside quit(). The tray callback still
        # owns a Python frame and may be unwinding a managed call afterwards.
        # Join owned callbacks outside native/UI locks, using the same budget.
        if not self._tray_actions_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            workers = tuple(self._tray_workers)
        finally:
            self._tray_actions_lock.release()
        for worker in workers:
            if worker is threading.current_thread():
                return False
            worker.join(max(0, deadline - time.monotonic()))
            if worker.is_alive():
                return False
        return True

    def _finish_native_host(self, timeout=30):
        # Called only after the GUI loop has returned or startup has failed.
        # A live-window restart refusal must not leave a headless tray behind
        # once there is no renderer available to confirm its cancellation.
        scheduled = self._on_window_closed()
        if not scheduled and not self._closing:
            # Thread creation failed, and the GUI loop has already ended. The
            # main thread may finish the orphan cleanup without dispatching to
            # the missing primary form. Owned child/service cleanup still runs.
            self._quit_closed_window()
        return self._await_owned_cleanup(timeout)


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
        menu = pystray.Menu(pystray.MenuItem('Open Forge', lambda icon, item: api._tray_action('open'), default=True),
                            pystray.MenuItem('Stop all agents', lambda icon, item: api._tray_action('stop')),
                            pystray.MenuItem('Quit Forge', lambda icon, item: api._tray_action('quit')))
        api._tray = pystray.Icon('Forge', picture, 'Forge — local AI workspace', menu)
        icon = api._tray
        ready = api._tray_ready = threading.Event()
        startup = api._tray_startup_done = threading.Event()
        def setup(icon):
            # pystray invokes setup only after _mark_ready sets _running. A
            # delayed startup can now be stopped even after Quit was admitted.
            ready.set()
            startup.set()
            with api._native_admission:
                retiring = api._closing or api._window_closed or api._tray is not icon
                if not retiring:
                    icon.visible = True
            if retiring:
                icon.stop()
        if os.name == 'nt':
            def run_tray():
                try:
                    icon.run(setup)
                except Exception as exc:
                    api._tray_failure = True
                    log.warning('Tray loop failed: %s', type(exc).__name__)
                finally:
                    startup.set()
                    with api._native_admission:
                        if api._tray is icon:
                            if not api._closing and not api._window_closed:
                                api._tray_failure = True
                            api._tray = None
            # CLR event callbacks enter Python through daemon dummy threads.
            # Explicit ownership preserves the old non-daemon Windows pump.
            api._tray_thread = threading.Thread(target=run_tray, name='Forge-tray-loop', daemon=False)
            api._tray_thread.start()
        else:
            icon.run_detached(setup)
        def notify(kind,summary):
            if not api._tray or api._closing: return
            message={'approval':'A tool action needs your approval.','outcome_unknown':'Inspect an interrupted action in Forge.'}.get(kind,
                'Your Forge run '+str(summary.get('status','finished'))+'.')
            try: api._tray.notify(message,'Forge')
            except Exception: pass
        api._service.desktop_notify=notify
    except Exception as exc:
        api._tray_failure = True
        api._tray = None
        if api._tray_startup_done is not None:
            api._tray_startup_done.set()
        log.warning('Tray startup failed: %s', type(exc).__name__)


def main():
    if '--forge-window-capture' in sys.argv:
        from forge_window_capture import helper_entry
        return helper_entry()
    if '--forge-update' in sys.argv:
        from forge_native_handoff import helper_entry
        return helper_entry()
    if '--forge-update-protocol-smoke' in sys.argv:
        from forge_native_handoff import protocol_smoke_entry
        return protocol_smoke_entry()
    native_through = _smoke_native_through(sys.argv, os.environ)
    if '--smoke-test' in sys.argv and not os.getenv('FORGE_DATA_DIR'):
        raise ValueError('Native smoke requires an explicit disposable FORGE_DATA_DIR.')
    if '--smoke-test' in sys.argv:
        global _ACTIVE_SMOKE_CHECKPOINT
        _ACTIVE_SMOKE_CHECKPOINT = _SmokeCheckpoint(os.getenv('FORGE_DATA_DIR'), os.getenv('LOCAL_AI_SMOKE_PATH'))
        _smoke_checkpoint('startup-imports')
    import webview
    import uvicorn
    from model_manager import create_app
    _smoke_checkpoint('startup-imports-ready')
    _smoke_checkpoint('logging-start')
    directory = setup_logging()
    _smoke_checkpoint('logging-ready')
    _smoke_checkpoint('singleton-start')
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
    _smoke_checkpoint('singleton-ready')
    api = Bridge()
    sock = None
    try:
        _smoke_checkpoint('service-start')
        service = api._ensure_service()
        if '--smoke-test' in sys.argv and '--handoff-smoke' in sys.argv:
            _smoke_handoff_setup(api)
        _smoke_checkpoint('service-ready')
        _smoke_checkpoint('http-start')
        app = create_app(service=service, auth=api._pairing)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', int(os.getenv('FORGE_PORT', '0'))))
        sock.listen(128)
        api._server_url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        api._lifecycle.server = server
        server_thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name='forge-coordinator-http', daemon=True)
        server_thread.start()
        _smoke_checkpoint('http-ready')
        _smoke_checkpoint('background-start')
        background = getattr(service, 'start_background', None)
        if background:
            background()
        _smoke_checkpoint('background-ready')
        _smoke_checkpoint('host-hooks-start')
        shortcut = service.dispatch('settings', {}).get('emergency_shortcut', 'Ctrl+Alt+Shift+S')
        api._hotkey = EmergencyHotkey(api._emergency_stop, shortcut)
        api._hotkey.start()
        from forge_update_monitor import start_native_update_monitor
        start_native_update_monitor(service)
        _smoke_checkpoint('host-hooks-ready')
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
        _smoke_checkpoint('window-create')
        api._window = webview.create_window('Forge', url=api._server_url, js_api=api,
                                           width=1180, height=820, min_size=(360, 150),
                                           maximized=True,
                                           hidden='--background' in sys.argv,
                                           background_color='#171717', text_select=True)
        _smoke_checkpoint('window-created')
        api._visible = '--background' not in sys.argv
        api._window.events.closing += api._on_closing
        api._window.events.closed += api._on_window_closed
        api._window.events.shown += api._on_shown
        api._window.events.loaded += api._on_loaded
        api._window.events.minimized += api._on_visibility_event
        api._window.events.restored += api._on_visibility_event
        api._window.events.maximized += api._on_visibility_event
        icon = BASE_DIR / 'assets' / 'forge.ico'
        options = dict(gui='edgechromium', private_mode=False, storage_path=str(directory / 'webview'))
        if icon.exists():
            options['icon'] = str(icon)
        if '--smoke-test' in sys.argv:
            def smoke():
                result = {'smoke_phase': 'workspace-ready'}
                _smoke_checkpoint('workspace-ready')
                try:
                    result.update(_smoke_workspace_state(api._window))
                    result['smoke_phase'] = 'workspace-size'
                    _smoke_checkpoint(result['smoke_phase'])
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
                    if '--handoff-smoke' in sys.argv:
                        result['smoke_phase'] = 'handoff-floor'
                        _smoke_checkpoint(result['smoke_phase'])
                        result['handoff_seed'] = _smoke_handoff_floor(api)
                    result['smoke_phase'] = 'library'
                    _smoke_checkpoint(result['smoke_phase'])
                    starters = [skill for skill in api.call('skills')['skills'] if skill.get('builtin')]
                    if len(starters) != 28 or not all(skill['enabled'] and skill['automatic'] for skill in starters):
                        raise ValueError('The packaged starter library was not prepared correctly.')
                    api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Tools');button?.click();return true;})()")
                    time.sleep(.1)
                    opened = api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('[role=menuitem],nav[aria-label=\"Workspace navigation\"] button')].find(e=>e.textContent.trim()==='Library');if(button){button.click();return true;}return false;})()")
                    if not opened: raise ValueError('The Library navigation is missing.')
                    deadline = time.monotonic() + 10
                    cards = 0
                    while cards < 24 and time.monotonic() < deadline:
                        time.sleep(.05)
                        cards = api._window.evaluate_js("document.querySelectorAll('.library-card').length")
                    if cards < 24: raise ValueError('The packaged Discover library did not render.')
                    result['library'] = {'starter_skills': len(starters), 'discover_cards': cards, 'rendered': True}
                    api._window.evaluate_js("document.querySelector('.new-chat').click()")
                    result['smoke_phase'] = 'host-lifecycle'
                    _smoke_checkpoint(result['smoke_phase'])
                    result.update(hud=api.mode('hud'), peek=api.mode('hud', True), full=api.mode('full'),
                                  pin=api.pin(True), unpin=api.pin(False), host=api.call('host_status'),
                                  projects=api.call('projects'), size=api._native(lambda form: [form.Width, form.Height]))
                    result['full_restores_maximized'] = api._native(lambda form: str(form.WindowState) == 'Maximized')
                    if not result['full_restores_maximized']:
                        raise ValueError('Expand did not restore the maximized workspace.')
                    def close_to_tray(form):
                        from webview.platforms.winforms import BrowserView
                        form.Close()
                        return (BrowserView.instances.get(api._window.uid) is form
                            and not form.Visible and not api._closing and not api._window_closed)
                    result['close_hides'] = api._native(close_to_tray, _update_safe=True)
                    result['background_api'] = api.call('projects')
                    result['close_hides'] = result['close_hides'] and _smoke_tray_open(api)
                    if not result['close_hides']:
                        raise ValueError('Native close-to-tray did not preserve and restore the primary workspace.')
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
                        result['smoke_phase'] = 'research-start'
                        _smoke_checkpoint(result['smoke_phase'])
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
                                    result['tool_failure'] = ('not-executed' if outcome.get('not_executed') is True else
                                        'outcome-unknown' if outcome.get('outcome_unknown') is True else 'refused')
                                    result['research_view_created'] = getattr(browser.native, 'view', None) is not None
                                    raise ValueError(outcome.get('error', 'Native browser fixture failed'))
                                return outcome
                            # An agent may not reopen a hidden research pane.
                            # This explicit smoke UI Show owns the pane first.
                            result['smoke_phase'] = 'research-start'
                            _smoke_checkpoint(result['smoke_phase'])
                            browser.native.dispatch('show')
                            result['smoke_phase'] = 'research-navigate'
                            _smoke_checkpoint(result['smoke_phase'])
                            tool('browser_navigate', {'url': url})
                            result['smoke_phase'] = 'research-load'
                            _smoke_checkpoint(result['smoke_phase'])
                            deadline = time.monotonic() + 20
                            while browser.native.status()['loading'] and time.monotonic() < deadline:
                                time.sleep(.1)
                            inspected = tool('browser_inspect', {})
                            result['smoke_phase'] = 'research-input-target'
                            _smoke_checkpoint(result['smoke_phase'])
                            field = next(t for t in inspected['targets'] if t['tag'] == 'input')
                            result['smoke_phase'] = 'research-type'
                            _smoke_checkpoint(result['smoke_phase'])
                            tool('browser_type', {'snapshot_id': inspected['snapshot_id'], 'selector': field['selector'], 'text': 'Native WebView2 works'})
                            inspected = tool('browser_inspect', {})
                            result['smoke_phase'] = 'research-button-target'
                            _smoke_checkpoint(result['smoke_phase'])
                            button = next(t for t in inspected['targets'] if t['tag'] == 'button')
                            result['smoke_phase'] = 'research-click'
                            _smoke_checkpoint(result['smoke_phase'])
                            tool('browser_click', {'snapshot_id': inspected['snapshot_id'], 'selector': button['selector']})
                            inspected = tool('browser_inspect', {})
                            result['smoke_phase'] = 'research-verify'
                            _smoke_checkpoint(result['smoke_phase'])
                            if 'Native WebView2 works' not in inspected['text']:
                                raise ValueError('Native form edit/click did not update the fixture.')
                            result['smoke_phase'] = 'research-screenshot'
                            _smoke_checkpoint(result['smoke_phase'])
                            capture = tool('browser_screenshot', {})
                            result['native_browser'] = dict(engine='WebView2', bridge_exposed=browser.native.view.evaluate('typeof window.pywebview'),
                                fixture_updated=True, screenshot_bytes=Path(capture['artifact']).stat().st_size,
                                same_page_url=inspected['url'] == url)
                            browser.native.dispatch('close')
                        finally:
                            fixture.shutdown()
                            fixture.server_close()
                        if native_through == 'research':
                            result['smoke_native_through'] = 'research'
                            return
                        # Exercise the separately owned preview controller, not
                        # merely the research browser's shared DOM tools.
                        result['smoke_phase'] = 'builder-start'
                        _smoke_checkpoint(result['smoke_phase'])
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
                            # HUD resizing may have collapsed the sidebar. Restore it
                            # before looking for Builder, then wait for React to render.
                            api._window.evaluate_js("document.querySelector('button[aria-label=\"Close workspace panel\"]')?.click();document.querySelector('button[aria-label=\"Open sidebar\"]')?.click();true")
                            result['smoke_phase'] = 'builder-navigation'
                            _smoke_checkpoint(result['smoke_phase'])
                            opened = False; deadline = time.monotonic()+10
                            api._window.evaluate_js("(()=>{const button=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Tools');button?.click();return true;})()")
                            while not opened and time.monotonic()<deadline:
                                opened = api._window.evaluate_js("(()=>{const b=[...document.querySelectorAll('[role=menuitem],nav[aria-label=\"Workspace navigation\"] button')].find(e=>e.textContent.trim()==='Builder');if(b){b.click();return true;}return false;})()")
                                if not opened: time.sleep(.05)
                            if not opened: raise ValueError('Builder navigation is missing.')
                            deadline = time.monotonic()+10
                            while not api._window.evaluate_js("!!document.querySelector('nav[aria-label=\"Builder sections\"]')") and time.monotonic()<deadline:
                                time.sleep(.05)
                            api._window.evaluate_js("(()=>{const b=[...document.querySelectorAll('nav[aria-label=\"Builder sections\"] button')].find(e=>e.textContent.trim()==='Preview');b?.click();document.querySelector('button[aria-label=\"Collapse sidebar\"]')?.click();return true;})()")
                            deadline = time.monotonic()+10
                            while not api._window.evaluate_js("!!document.getElementById('forge-preview-panel')") and time.monotonic()<deadline:
                                time.sleep(.05)
                            # On short desktops the controls can put the pane
                            # below its native owner's viewport. Scroll as a user
                            # would; the frontend still validates/binds real bounds.
                            api._window.evaluate_js("document.getElementById('forge-preview-panel')?.scrollIntoView({block:'start',inline:'nearest',behavior:'instant'});true")
                            # The actual frontend sizes and binds the child to
                            # host-inspected viewport bounds.
                            deadline = time.monotonic()+20
                            while (not manager.previews.browser.status().get('visible') or manager.previews.browser.status().get('loading')) and time.monotonic()<deadline:
                                time.sleep(.1)
                            preview_run = {'id': 'preview-native-smoke', 'project_id': project['id'], 'settings': service.store.get_settings()}
                            def preview_tool(name, arguments):
                                result['smoke_phase'] = 'builder:' + name
                                _smoke_checkpoint(result['smoke_phase'])
                                try:
                                    outcome = manager.execute(preview_run, name, {'id': preview['id'], **arguments}, cancel)
                                    if outcome.get('ok') is False: raise ValueError(outcome.get('error', 'Preview fixture failed.'))
                                    return outcome
                                except Exception:
                                    if name == 'preview_inspect':
                                        try:
                                            state = manager.previews.browser.status()
                                            result['smoke_preview_state'] = {
                                                'available': True, 'running': bool(state.get('running')),
                                                'loading': bool(state.get('loading')), 'visible': bool(state.get('visible')),
                                                'view_present': manager.previews.browser.view is not None,
                                                'record_ready': manager.previews.status(preview['id']).get('status') == 'ready'}
                                            ui = api._window.evaluate_js("(()=>{const p=document.getElementById('forge-preview-panel'),r=p?.getBoundingClientRect(),s=p&&getComputedStyle(p);return {builder:!!document.querySelector('nav[aria-label=\"Builder sections\"]'),pane:!!p,pane_visible:!!r&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden',modal:!!document.querySelector('dialog[open],.modal-backdrop')};})()")
                                            if isinstance(ui, dict):
                                                result['smoke_preview_state'].update({
                                                    'builder': bool(ui.get('builder')), 'pane': bool(ui.get('pane')),
                                                    'pane_visible': bool(ui.get('pane_visible')), 'modal': bool(ui.get('modal'))})
                                        except Exception:
                                            result.setdefault('smoke_preview_state', {'available': False})
                                    raise
                            inspected = preview_tool('preview_inspect', {})
                            result['smoke_phase'] = 'builder-input-target'
                            _smoke_checkpoint(result['smoke_phase'])
                            target = next(t for t in inspected['targets'] if t['tag']=='input')
                            preview_tool('preview_type', {'snapshot_id': inspected['snapshot_id'], 'selector': target['selector'], 'text': 'Isolated preview works'})
                            inspected = preview_tool('preview_inspect', {})
                            result['smoke_phase'] = 'builder-button-target'
                            _smoke_checkpoint(result['smoke_phase'])
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
                        if native_through == 'builder':
                            result['smoke_native_through'] = 'builder'
                            return
                        # Plain plans can start previews without a Builder brief.
                        # The actual workspace must discover that identity while
                        # preview_open waits for its guarded native pane to appear.
                        result['smoke_phase'] = 'standalone-start'
                        _smoke_checkpoint(result['smoke_phase'])
                        standalone_run = {'id':'standalone-native-smoke','project_id':project['id'],
                            'settings':service.store.get_settings()}
                        standalone = manager.execute(standalone_run,'preview_start',{'cwd':'site','mode':'static'},cancel)
                        try:
                            api._window.evaluate_js("document.querySelector('button.new-chat')?.click();window.dispatchEvent(new Event('pywebviewready'));document.querySelector('button[aria-label=\"Open sidebar\"]')?.click();true")
                            deadline=time.monotonic()+10
                            while not api._window.evaluate_js("document.body.innerText.includes('Disposable preview fixture')") and time.monotonic()<deadline:
                                time.sleep(.05)
                            result['smoke_phase'] = 'standalone-open'
                            _smoke_checkpoint(result['smoke_phase'])
                            manager.execute(standalone_run,'preview_open',{'id':standalone['id']},cancel)
                            result['smoke_phase'] = 'standalone-layout'
                            _smoke_checkpoint(result['smoke_phase'])
                            deadline=time.monotonic()+10
                            _smoke_preview_ready(manager,standalone['id'],deadline=deadline,diagnostics=result)
                            result['smoke_phase'] = 'standalone-viewport'
                            _smoke_checkpoint(result['smoke_phase'])
                            _smoke_preview_tool(manager,standalone_run,cancel,result,'preview_viewport',{'id':standalone['id']})
                            inspected=_smoke_preview_tool(manager,standalone_run,cancel,result,'preview_inspect',{'id':standalone['id']})
                            result['smoke_phase'] = 'standalone-input-target'
                            _smoke_checkpoint(result['smoke_phase'])
                            target=next(t for t in inspected['targets'] if t['tag']=='input')
                            _smoke_preview_tool(manager,standalone_run,cancel,result,'preview_type',{'id':standalone['id'],'snapshot_id':inspected['snapshot_id'],
                                'selector':target['selector'],'text':'Standalone preview works'})
                            inspected=_smoke_preview_tool(manager,standalone_run,cancel,result,'preview_inspect',{'id':standalone['id']})
                            result['smoke_phase'] = 'standalone-button-target'
                            _smoke_checkpoint(result['smoke_phase'])
                            target=next(t for t in inspected['targets'] if t['tag']=='button')
                            _smoke_preview_tool(manager,standalone_run,cancel,result,'preview_click',{'id':standalone['id'],'snapshot_id':inspected['snapshot_id'],
                                'selector':target['selector']})
                            inspected=_smoke_preview_tool(manager,standalone_run,cancel,result,'preview_inspect',{'id':standalone['id']})
                            if 'Standalone preview works' not in inspected['text']:
                                raise ValueError('Standalone preview controls did not update the fixture.')
                            result['smoke_phase'] = 'standalone-screenshot'
                            _smoke_checkpoint(result['smoke_phase'])
                            capture=_smoke_preview_tool(manager,standalone_run,cancel,result,'preview_screenshot',{'id':standalone['id']})
                            result['native_standalone_preview']={'pane_visible':True,'fixture_updated':True,
                                'without_builder':standalone.get('builder_id') is None,
                                'bridge_exposed':manager.previews.browser.view.evaluate('typeof window.pywebview'),
                                'screenshot_bytes':Path(capture['artifact']).stat().st_size}
                        finally:
                            manager.previews.stop(standalone['id'])
                except Exception as exc:
                    result['error_type'] = type(exc).__name__
                    result['error'] = str(exc)
                    _smoke_checkpoint('smoke-failed', error_type=type(exc).__name__, error_category='smoke-failure')
                finally:
                    destination = os.getenv('LOCAL_AI_SMOKE_PATH')
                    if destination:
                        Path(destination).write_text(json.dumps(result), encoding='utf-8')
                    if 'error' not in result:
                        _smoke_checkpoint('smoke-complete')
                    api._tray_action('quit')
            _smoke_checkpoint('webview-start')
            webview.start(smoke, **options)
        else:
            webview.start(**options)
        server.should_exit = True
        server_thread.join(timeout=5)
    finally:
        try:
            _smoke_cleanup_marker('main-wait', 'started')
            if not api._finish_native_host():
                raise RuntimeError('Forge could not complete owned cleanup.')
            _smoke_cleanup_marker('main-wait', 'completed')
        finally:
            if sock:
                sock.close()
            singleton.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        _smoke_checkpoint('startup-failed', error_type=type(exc).__name__, error_category='startup-failure')
        log.exception('Fatal startup failure')
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, 'Forge could not start. Details were saved in .forge/logs/forge.log.', 'Forge', 16)
        else:
            raise
