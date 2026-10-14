"""Synthetic main/child/HTTP and queued-native input lifecycle boundaries."""
import base64
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from desktop import Bridge
from dictation import Dictation
from forge_drafts import DraftManager
from forge_host import PairingAuthority
from forge_native_handoff import NativeBinding
from forge_native_input import NativeInputGuard
from model_manager import create_app
from test_forge_update_handoff import flow
from test_forge_publisher_updates import fixture as publisher


class Hook:
    def __init__(self): self.callbacks = []
    def __iadd__(self, callback): self.callbacks.append(callback); return self


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text=' synthetic paired speech')]), SimpleNamespace(language='en')
    service = SimpleNamespace(admission_lock=threading.RLock(),
        store=SimpleNamespace(home=tmp_path, get_settings=lambda: {'dictation_model': 'base'}),
        host_dictation=Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model()),
        get_drafts=lambda: DraftManager(None), update_manager=None)
    instance = Bridge(service)
    instance._ensure_service = lambda: service
    instance._native_input = NativeInputGuard(service.host_dictation)
    service.host_dictation_dispatch = instance._dictation_dispatch
    service.host_dictation_guard = instance._native_input.confirm
    service.update_native_input_guard = instance._update_input_idle
    instance._window = SimpleNamespace(uid='synthetic-main', _js_api=instance)
    instance._server_url = 'http://127.0.0.1:12345'
    form = SimpleNamespace(InvokeRequired=False, Visible=True, WindowState='Normal',
        VisibleChanged=Hook(), Resize=Hook(), webview=SimpleNamespace(Source=instance._server_url, NavigationStarting=Hook()))
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms',
        SimpleNamespace(BrowserView=SimpleNamespace(instances={instance._window.uid: form})))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda callback: callback))
    return instance, service, form


def _synthetic_queued_dispatch(bridge, monkeypatch):
    """Keep Python callback return distinct from the managed delegate return."""
    instance, service, form = bridge
    queued = []
    managed = SimpleNamespace(IsCompleted=False, python_done=False, end_calls=0,
        before_wait=lambda: False)
    form.InvokeRequired = True

    def begin(callback):
        queued.append(callback)
        return managed

    def end(result):
        assert result is managed and managed.IsCompleted is True
        assert managed.python_done and instance._native_pending == 1
        assert form.InvokeRequired is True
        assert not instance._native_admission._is_owned()
        assert not service.admission_lock._is_owned()
        managed.end_calls += 1

    form.BeginInvoke, form.EndInvoke = begin, end
    original = instance._NativeCall

    class Done:
        def wait(self, duration):
            assert 0 < duration <= 5
            return managed.before_wait()

        def set(self):
            managed.python_done = True

    def lifetime(*args):
        call = original(*args)
        call.done = Done()
        return call

    monkeypatch.setattr(instance, '_NativeCall', lifetime)
    return queued, managed


def test_queued_native_operation_rechecks_lock_before_executing_and_retains_pending_until_done(bridge, monkeypatch):
    instance, _, form = bridge
    queued, managed = _synthetic_queued_dispatch(bridge, monkeypatch)
    changes = []
    # Force the Python wait boundary without waiting five seconds. Callback
    # return alone cannot retire the native admission lease.
    with pytest.raises(instance._NativeCompletionError) as failure:
        instance._native(lambda form: changes.append('changed'))
    assert failure.value.reason == 'timeout'
    assert instance._native_pending == 1 and instance._update_input_idle() is False
    binding = NativeBinding(instance, 'a' * 64, instance._window.uid, 0, instance._server_url, 1, 1.0)
    instance._update_authority.update_binding(binding)
    first = instance._update_authority.begin(binding, object())
    queued.pop()()
    assert changes == [] and managed.python_done and instance._native_pending == 1
    assert managed.end_calls == 0 and instance._update_input_idle() is False
    managed.IsCompleted = True
    form.InvokeRequired = False
    assert instance._update_input_idle() is False
    assert managed.end_calls == 0 and instance._native_pending == 1
    form.InvokeRequired = True
    assert instance._retire_native_calls() is True
    assert managed.end_calls == 1 and instance._native_pending == 0
    instance._update_authority.acknowledge_abort(first['attempt_id'])
    assert instance._update_input_idle() is True
    assert managed.end_calls == 1 and changes == []


@pytest.mark.parametrize('transition', ['lock', 'quit'])
def test_live_queued_native_operation_rechecks_guard_before_managed_completion(bridge, monkeypatch, transition):
    instance, _, form = bridge
    queued, managed = _synthetic_queued_dispatch(bridge, monkeypatch)
    events, attempt = [], []
    binding = NativeBinding(instance, 'a' * 64, instance._window.uid, 0, instance._server_url, 1, 1.0)
    instance._update_authority.update_binding(binding)
    instance._lifecycle = SimpleNamespace(quit=lambda: events.append('owned-cleanup'))
    instance._window.destroy = lambda: events.append('window-destroy')

    def changed_before_callback():
        if transition == 'lock':
            attempt.append(instance._update_authority.begin(binding, object()))
        else:
            assert instance.quit() == {'ok': True}
        queued.pop()()
        assert instance._native_pending == 1 and managed.python_done
        assert not managed.IsCompleted and managed.end_calls == 0
        managed.IsCompleted = True
        return True

    managed.before_wait = changed_before_callback
    with pytest.raises(ValueError, match='Native input is paused'):
        instance._native(lambda form: events.append('unrelated-input'))
    assert managed.end_calls == 1 and instance._native_pending == 0
    assert instance._native_calls == [] and 'unrelated-input' not in events
    if transition == 'lock':
        assert events == [] and instance._update_locked()
        instance._update_authority.acknowledge_abort(attempt[0]['attempt_id'])
        assert instance._update_input_idle() is True
    else:
        assert events == ['owned-cleanup', 'window-destroy'] and instance._closing
        assert instance._update_input_idle() is False
    assert managed.end_calls == 1


def test_only_actual_frozen_main_root_document_can_bind_native_authority(bridge, monkeypatch):
    instance, _, form = bridge
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    instance._trusted_loaded = True
    binding = instance._native_binding()
    assert binding.host is instance and binding.window_id == instance._window.uid
    for source in ('http://127.0.0.1:12345/unsafe.html', 'http://127.0.0.1:12345/?secret=synthetic', 'https://example.com/'):
        form.webview.Source = source
        assert instance._native_binding() is None
    form.webview.Source = instance._server_url
    instance._window._js_api = object()
    assert instance._native_binding() is None


def test_navigation_cancels_untrusted_same_origin_document_and_revokes_active_binding_immediately(bridge, monkeypatch):
    instance, _, form = bridge
    opened = []
    monkeypatch.setattr('webbrowser.open', opened.append)
    instance.restrict_navigation()
    guard = form.webview.NavigationStarting.callbacks[0]
    unsafe = SimpleNamespace(Uri=instance._server_url + '/unsafe.html', Cancel=False)
    guard(None, unsafe)
    assert unsafe.Cancel is True and opened == [unsafe.Uri]
    binding = NativeBinding(instance, 'a' * 64, instance._window.uid, 0, instance._server_url, 1, 1.0)
    instance._update_authority.update_binding(binding)
    instance._update_authority.begin(binding, object())
    reload = SimpleNamespace(Uri=instance._server_url, Cancel=False)
    guard(None, reload)
    assert reload.Cancel is True and instance._update_authority.binding_valid(binding) is False
    assert instance._update_authority.status()['locked'] is True


def test_hidden_retained_child_pages_block_until_explicit_disposal(bridge):
    instance, service, _ = bridge
    page = object()
    service.integrations = SimpleNamespace(browser=SimpleNamespace(native=SimpleNamespace(view=page)))
    assert instance._child_pages() == [page]
    assert instance._update_input_idle() is False
    service.integrations.browser.native.view = None
    assert instance._update_input_idle() is True
    service.builder_manager = SimpleNamespace(previews=SimpleNamespace(browser=SimpleNamespace(view=page)))
    assert instance._update_input_idle() is False


def test_locked_native_quit_cannot_destroy_window_or_cancel_input(bridge):
    instance, service, _ = bridge
    binding = NativeBinding(instance, 'a' * 64, instance._window.uid, 0, instance._server_url, 1, 1.0)
    instance._update_authority.update_binding(binding)
    instance._update_authority.begin(binding, object())
    instance._window.destroy = lambda: pytest.fail('No premature native quit')
    service.host_dictation.cancel = lambda *args: pytest.fail('No discarded input')
    result = instance.quit()
    assert result['ok'] is False and not instance._closing


def owned_browser_cleanup(instance, service, tmp_path, events):
    """Actual teardown chain with raw page methods and inert UI controls."""
    from browser_tools import BrowserTools
    from forge_host import HostLifecycle
    from forge_integrations import IntegrationHub
    from native_browser import NativeBrowser, WebView2Page
    native = NativeBrowser(tmp_path, ui_dispatch=instance._native, session_check=lambda: True)
    page = object.__new__(WebView2Page)
    page.ui = instance._native
    page.control = SimpleNamespace(CoreWebView2=SimpleNamespace(Stop=lambda: events.append('browser-stop')))
    def closed():
        events.append('browser-close')
        native._changed('closed')
    page.form = SimpleNamespace(Close=closed)
    native.view = page
    browser = object.__new__(BrowserTools)
    browser.native, browser.bridge, browser.extension_snapshots = native, None, {}
    hub = SimpleNamespace(browser=browser)
    service.emergency_stop = lambda: IntegrationHub.stop(hub)
    def shutdown():
        events.append('service-shutdown')
        native.shutdown()
    service.shutdown = shutdown
    instance._lifecycle = HostLifecycle(service, instance._pairing)
    instance._window.destroy = lambda: events.append('window-destroy')
    return native


def test_owned_quit_stops_and_closes_retained_browser_before_destroying_window(bridge, tmp_path):
    instance, service, _ = bridge
    events = []
    native = owned_browser_cleanup(instance, service, tmp_path, events)
    try:
        assert instance.quit() == {'ok': True}
        assert events == ['browser-stop', 'service-shutdown', 'browser-close', 'window-destroy']
        assert native.closed and native.view is None
        assert instance._native_pending == 0 and not instance._owned_teardown.active
        assert instance.quit() == {'ok': True}
        assert events.count('window-destroy') == 1
    finally:
        native.view = None
        native.shutdown()


def test_owned_quit_does_not_admit_concurrent_native_input(bridge, tmp_path):
    instance, service, _ = bridge
    events, failures = [], []
    native = owned_browser_cleanup(instance, service, tmp_path, events)
    stop = service.emergency_stop
    def unrelated():
        try:
            instance._native(lambda form: events.append('unrelated-input'))
        except Exception as exc:
            failures.append(type(exc))
    def emergency_stop():
        worker = threading.Thread(target=unrelated)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        stop()
    service.emergency_stop = emergency_stop
    try:
        assert instance.quit() == {'ok': True}
        assert failures == [ValueError] and 'unrelated-input' not in events
        assert 'window-destroy' in events and instance._native_pending == 0
    finally:
        native.view = None
        native.shutdown()


def test_native_callback_queued_before_quit_cannot_execute_during_teardown(bridge, monkeypatch):
    instance, _, form = bridge
    queued, managed = _synthetic_queued_dispatch(bridge, monkeypatch)
    events = []
    with pytest.raises(instance._NativeCompletionError) as failure:
        instance._native(lambda form: events.append('queued-input'))
    assert failure.value.reason == 'timeout'
    assert instance._native_pending == 1
    form.InvokeRequired = False
    instance._lifecycle = SimpleNamespace(quit=lambda: queued.pop()())
    instance._window.destroy = lambda: events.append('window-destroy')
    assert instance.quit() == {'ok': True}
    assert events == ['window-destroy'] and managed.python_done
    assert instance._native_pending == 1 and managed.end_calls == 0
    managed.IsCompleted = True
    assert instance._update_input_idle() is False
    assert instance._native_pending == 1 and managed.end_calls == 0
    form.InvokeRequired = True
    assert instance._update_input_idle() is False
    assert instance._native_pending == 0 and managed.end_calls == 1
    assert instance._native_calls == [] and events == ['window-destroy']
    assert instance._update_input_idle() is False and managed.end_calls == 1


def test_owned_teardown_scope_clears_when_custom_lifecycle_cleanup_raises(bridge):
    instance, _, _ = bridge
    def unavailable():
        raise RuntimeError('Synthetic teardown failure')
    instance._lifecycle = SimpleNamespace(quit=unavailable)
    destroyed = []
    instance._window.destroy = lambda: destroyed.append(True)
    result = instance.quit()
    assert result == {'ok': False, 'error': 'Forge could not complete owned cleanup.'}
    assert destroyed == [True] and instance.quit() == result and destroyed == [True]
    assert not instance._owned_teardown.active
    with pytest.raises(ValueError):
        instance._native(lambda form: pytest.fail('Failed teardown cannot grant further native input'))


@pytest.mark.parametrize('failure', ['dictation', 'pairing', 'browser-stop', 'service', 'hotkey', 'tray', 'window'])
def test_failed_owned_cleanup_attempts_all_independent_steps_and_remains_honest(bridge, tmp_path, caplog, failure):
    instance, service, _ = bridge
    events = []
    native = owned_browser_cleanup(instance, service, tmp_path, events)
    def step(name):
        events.append(name)
        if name == failure:
            raise RuntimeError('Synthetic private cleanup exception and nonce')
    service.host_dictation.cancel = lambda: step('dictation')
    instance._lifecycle.pairing = SimpleNamespace(close=lambda: step('pairing'))
    native.view.control.CoreWebView2.Stop = lambda: step('browser-stop')
    def shutdown():
        step('service')
        native.shutdown()
    service.shutdown = shutdown
    server = SimpleNamespace(should_exit=False)
    instance._lifecycle.server = server
    instance._hotkey = SimpleNamespace(stop=lambda: step('hotkey'))
    instance._tray = SimpleNamespace(stop=lambda: step('tray'))
    instance._window.destroy = lambda: step('window')
    try:
        result = instance.quit()
        assert result == {'ok': False, 'error': 'Forge could not complete owned cleanup.'}
        assert all(name in events for name in ('dictation', 'pairing', 'browser-stop', 'service', 'hotkey', 'tray', 'window'))
        assert server.should_exit and instance._closing and not instance._owned_teardown.active
        count = len(events)
        assert instance.quit() == result and len(events) == count
        assert 'Synthetic private cleanup exception and nonce' not in caplog.text
        assert 'RuntimeError' in caplog.text
        with pytest.raises(ValueError):
            instance._native(lambda form: pytest.fail('No reopening after failed owned cleanup'))
    finally:
        native.view = None
        native.shutdown()


def test_incomplete_native_handoff_departure_reports_failure_without_minting_stop_proof(bridge, monkeypatch):
    from forge_native_handoff import HandoffRejected
    instance, _, _ = bridge
    instance._trusted_loaded = True
    instance._update_controller = controller = object()
    grant = SimpleNamespace(binding=SimpleNamespace(navigation=0, session=None, window_id=instance._window.uid))
    admitted = []
    monkeypatch.setattr(instance._update_authority, 'pipe_admission', lambda *args: admitted.append(args))
    monkeypatch.setattr(instance._update_authority, 'stopped_helper', lambda *args: pytest.fail('Cleanup is not helper stop proof'))
    instance._lifecycle = SimpleNamespace(quit=lambda: (_ for _ in ()).throw(RuntimeError('Synthetic owned failure')))
    destroyed = []
    instance._window.destroy = lambda: destroyed.append(True)
    transport = object()
    with pytest.raises(HandoffRejected) as rejected:
        instance._handoff_depart(controller, grant, transport)
    assert rejected.value.reason == 'helper_failed'
    assert admitted == [(grant, transport)] and destroyed == [True]
    assert instance._closing and instance._quit_complete is False and not instance._owned_teardown.active


def test_owned_teardown_preserves_the_lifecycle_service_and_custom_quit_override(bridge):
    from forge_host import HostLifecycle
    instance, service, _ = bridge
    events = []
    service.shutdown = lambda: pytest.fail('Owned lifecycle service is authoritative')
    owner = SimpleNamespace(emergency_stop=lambda: events.append('stop'), shutdown=lambda: events.append('shutdown'))
    instance._lifecycle = HostLifecycle(owner, instance._pairing)
    instance._window.destroy = lambda: events.append('window')
    assert instance.quit() == {'ok': True}
    assert events == ['stop', 'shutdown', 'window']
    class CustomLifecycle(HostLifecycle):
        def quit(self): events.append('custom-quit')
    instance._lifecycle = CustomLifecycle(owner, instance._pairing)
    # An internal custom lifecycle keeps its own override; no public input is
    # reopened and the bridge's closing guard stays in force.
    assert instance._quit_owned() is True
    assert events[-2:] == ['custom-quit', 'window']


@pytest.mark.parametrize('state,error', [('error', 'Check for updates again.'),
    ('interrupted', 'Inspect the previous update before continuing.'), ('available', None)])
def test_native_update_status_preserves_renderable_operation_failures(bridge, flow, state, error):
    instance, _, _ = bridge
    flow.manager._save(state=state, error=error)
    expected = flow.manager.dispatch('update_status', {})
    result = instance._native_update_status(SimpleNamespace(manager=flow.manager), flow.binding)
    assert 'error' not in result and result['operation_error'] == expected['operation_error'] == error
    assert result['state'] == expected['state'] == state
    assert result['current_version'] == expected['current_version']
    assert flow.manager.state['error'] == error


def test_explicitly_closed_handoff_gate_refuses_before_service_window_or_home_loading(bridge, monkeypatch):
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', False)
    instance, _, _ = bridge
    instance._ensure_service = lambda: pytest.fail('Closed capability cannot touch coordinator state')
    for action in ('update_handoff_begin', 'update_handoff_close_children', 'update_handoff_disconnect_editors'):
        assert instance.call(action, {})['reason'] == 'handoff_unavailable'


def paired_client(instance, service):
    pairing = PairingAuthority()
    client = TestClient(create_app(service=service, auth=pairing), base_url='http://127.0.0.1')
    response = client.post('/api/v1/pair', json={'code': pairing.issue()['code']})
    assert response.status_code == 200
    client.headers['x-forge-client-proof'] = response.json()['client_proof']
    return pairing, client


def test_http_dictation_uses_original_scope_private_delivery_and_same_admission(bridge):
    instance, service, _ = bridge
    _, client = paired_client(instance, service)
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    response = client.post('/api/v1/dictation_upload', json={'audio': base64.b64encode(b'synthetic audio').decode(), 'draft_scope': scope})
    assert response.status_code == 200
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = client.post('/api/v1/dictation_status', json={}).json()
        if result['state'] == 'done': break
        time.sleep(.005)
    assert result['text'] == 'synthetic paired speech' and len(result['delivery_id']) == 64
    assert instance._native_input.unknown is False
    assert instance._native_input.confirm({'scope': {'kind': 'new_chat', 'project_id': None}}, (result['delivery_id'],))
    assert instance._update_input_idle() is True


def test_queued_http_dictation_is_refused_after_pairing_revocation_without_admitting_job(bridge, monkeypatch):
    instance, service, _ = bridge
    pairing, client = paired_client(instance, service)
    entered = threading.Event(); results = []
    original = instance._admit_native
    def admitted(authorize=None): entered.set(); return original(authorize)
    monkeypatch.setattr(instance, '_admit_native', admitted)
    with service.admission_lock:
        worker = threading.Thread(target=lambda: results.append(client.post('/api/v1/dictation_upload',
            json={'audio': base64.b64encode(b'synthetic audio').decode(), 'draft_scope': {'scope_kind': 'new_chat'}})))
        worker.start(); assert entered.wait(1)
        pairing.close()
    worker.join(2)
    assert results[0].status_code == 400
    assert service.host_dictation.status()['state'] == 'idle'
    assert instance._native_input.unknown is False and instance._native_pending == 0


def test_http_native_producer_cannot_bypass_prepared_update(bridge):
    instance, service, _ = bridge
    _, client = paired_client(instance, service)
    service.update_manager = SimpleNamespace(applying=True, shutting_down=False)
    response = client.post('/api/v1/dictation_upload', json={'audio': base64.b64encode(b'audio').decode(),
        'draft_scope': {'scope_kind': 'new_chat'}})
    assert response.status_code == 400 and service.host_dictation.status()['state'] == 'idle'
    assert instance._native_pending == 0


@pytest.mark.parametrize('state,mode,version,wanted', [('installed', 'ed25519', '5.1.4', True),
    ('installed', 'authenticode', '5.1.4', True), ('prepared', 'ed25519', '5.1.4', False),
    ('rolled_back', 'ed25519', '5.1.4', False), ('installed', 'manual', '5.1.4', False),
    ('installed', 'ed25519', '5.1.3', False)])
def test_native_rollback_offer_uses_completed_operation_not_update_feed_state(bridge, monkeypatch, state, mode, version, wanted):
    instance, _, _ = bridge
    status = {'safe_handoff_available': True, 'inspection_required': False,
        'state': 'available', 'operation_id': 'a' * 32}
    home = Path(__file__).resolve().parent / 'synthetic-private-home'
    app = home.parent / 'synthetic-private-app'
    manager = SimpleNamespace(home=home, install_dir=app, proof_keys={},
        current_version='5.1.4', native_status=lambda value: dict(status))
    monkeypatch.setattr('forge_updates.load_operation', lambda *args:
        {'id': 'a' * 32, 'state': state, 'trust_mode': mode, 'version': version,
         'previous_version': '5.1.3', 'install_dir': str(app), 'backup': str(home / 'backups' / ('update-' + 'a' * 32))})
    monkeypatch.setattr('forge_update_proof.read_record', lambda path: b'synthetic signed record')
    validated = []
    monkeypatch.setattr('forge_update_proof.validated_build_manifest', lambda *args,**kwargs: validated.append(kwargs))
    result = instance._native_update_status(SimpleNamespace(manager=manager), object())
    assert result['safe_rollback_available'] is wanted
    assert len(validated) == (2 if wanted else 0)
    assert 'synthetic-private-home' not in str(result)
    status['inspection_required'] = True
    assert instance._native_update_status(SimpleNamespace(manager=manager), object())['safe_rollback_available'] is False


@pytest.mark.parametrize('failure', ['wrong-installation', 'foreign-backup', 'current-proof', 'previous-proof'])
def test_rollback_offer_refuses_mismatched_installation_or_unverified_build_headers(bridge, monkeypatch, failure):
    instance, _, _ = bridge
    home = Path(__file__).resolve().parent / 'synthetic-private-home'
    app = home.parent / 'synthetic-private-app'
    data = {'id': 'a' * 32, 'state': 'installed', 'trust_mode': 'ed25519', 'version': '5.1.4', 'previous_version': '5.1.3',
        'install_dir': str(app), 'backup': str(home / 'backups' / ('update-' + 'a' * 32)),
        'source_commit': 'b' * 40, 'previous_source_commit': 'c' * 40}
    if failure == 'wrong-installation': data['install_dir'] = str(app.parent / 'other-app')
    if failure == 'foreign-backup': data['backup'] = str(app.parent / 'other-backup')
    manager = SimpleNamespace(home=home, install_dir=app, proof_keys={}, current_version='5.1.4',
        native_status=lambda value: {'safe_handoff_available': True, 'operation_id': 'a' * 32})
    monkeypatch.setattr('forge_updates.load_operation', lambda *args: data)
    monkeypatch.setattr('forge_update_proof.read_record', lambda path: b'synthetic record')
    def validate(raw, pins, **kwargs):
        if failure == 'current-proof' and kwargs['expected_version'] == '5.1.4': raise ValueError('Synthetic refusal')
        if failure == 'previous-proof' and kwargs['expected_version'] == '5.1.3': raise ValueError('Synthetic refusal')
    monkeypatch.setattr('forge_update_proof.validated_build_manifest', validate)
    assert instance._native_update_status(SimpleNamespace(manager=manager), object())['safe_rollback_available'] is False


@pytest.fixture
def checking_persistence(bridge, flow, monkeypatch):
    """Actual native authority/controller, SQL/CAS service and inert updater."""
    from forge_native_controller import NativeController
    from forge_service import ForgeService
    instance, _, _ = bridge
    value = flow
    service = object.__new__(ForgeService)
    service.__dict__.update(value.manager.service.__dict__)
    service.manager_lock = threading.RLock()
    service.stop_event = threading.Event()
    service.integrations = None
    service.draft_manager = None
    from forge_updates import UpdateManager
    value.manager.shutdown()
    value.manager = UpdateManager(service, '5.1.3', value.app, (), value.factory, proof_policy=value.pins)
    service.update_manager = value.manager
    instance._service = service
    instance._ensure_service = lambda: service
    instance._trusted_loaded = True
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    instance._update_controller = NativeController(value.manager, instance._update_authority,
        binding=instance._native_binding, input_idle=lambda: True,
        fence=lambda request: None, depart=lambda *args: pytest.fail('No helper is launched'))
    initial = instance.call('update_handoff_begin', {})
    assert initial['phase'] == 'checking'
    yield instance, service, initial
    instance._update_controller.dispatch('update_handoff_abort', {'attempt_id': initial['attempt_id']})


def test_checking_native_flush_saves_real_cas_draft_and_layout_then_prepares_same_receipt(checking_persistence):
    instance, service, initial = checking_persistence
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    saved = instance.call('draft_save', {**scope, 'expected_revision': 0,
        'content': {'text': 'Synthetic retained draft before restart'}})
    assert saved['revision'] == 1
    assert instance.call('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_width': 300}})['sidebar_width'] == 300
    receipt = instance.call('draft_confirm', {**scope, 'expected_revision': saved['revision'],
        'attempt_id': initial['attempt_id']})
    assert set(receipt) == {'ok', 'receipt'} and len(receipt['receipt']) == 64
    prepared = instance.call('update_handoff_ack', {key: initial[key] for key in ('attempt_id', 'challenge')} |
        {'workspace_revision': 7, 'participant_count': 4, 'receipts': [receipt['receipt']]})
    assert prepared['phase'] == 'prepared' and service.update_manager.applying is True
    assert 'error' in instance.call('draft_save', {**scope, 'expected_revision': 1, 'content': {'text': 'Cannot change prepared work'}})
    assert 'error' in instance.call('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_width': 320}})
    assert service.get_drafts().get(scope)['content']['text'] == 'Synthetic retained draft before restart'
    assert service.dispatch('ui_layout_get', {'client_id': 'desktop'})['sidebar_width'] == 300


def test_checking_flush_retains_coordinator_cas_and_applying_checks(checking_persistence):
    instance, service, _ = checking_persistence
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    assert instance.call('draft_save', {**scope, 'expected_revision': 0, 'content': {'text': 'Synthetic newer draft'}})['revision'] == 1
    stale = instance.call('draft_save', {**scope, 'expected_revision': 0, 'content': {'text': 'Cannot overwrite newer draft'}})
    assert 'another window' in stale['error']
    service.update_manager.applying = True
    assert 'error' in instance.call('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_width': 300}})
    service.update_manager.applying = False
    assert service.get_drafts().get(scope)['content']['text'] == 'Synthetic newer draft'


def test_native_ack_cannot_advance_during_admitted_checking_cas(checking_persistence, monkeypatch):
    instance, service, initial = checking_persistence
    entered, release = threading.Event(), threading.Event()
    drafts = service.get_drafts()
    original = drafts.save
    def delayed(data):
        entered.set()
        assert release.wait(2)
        return original(data)
    monkeypatch.setattr(drafts, 'save', delayed)
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    result = []
    worker = threading.Thread(target=lambda: result.append(instance.call('draft_save',
        {**scope, 'expected_revision': 0, 'content': {'text': 'Synthetic pending CAS'}})))
    worker.start()
    try:
        assert entered.wait(2)
        blocked = instance.call('update_handoff_ack', {key: initial[key] for key in ('attempt_id', 'challenge')} |
            {'workspace_revision': 7, 'participant_count': 4, 'receipts': []})
        assert blocked['blocked'] is True and blocked['phase'] == 'checking'
        assert service.update_manager.applying is False
        assert not list((service.store.home / 'backups').glob('update-*'))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and result[0]['revision'] == 1
    assert drafts.get(scope)['content']['text'] == 'Synthetic pending CAS'


@pytest.mark.parametrize('boundary', ['untrusted-document', 'missing-controller', 'copied-reservation',
    'backend-reservation', 'backend-phase', 'expired', 'aborting', 'locked', 'disabled'])
def test_checking_persistence_never_uses_stale_untrusted_or_advanced_authority(checking_persistence, monkeypatch, boundary):
    from dataclasses import replace
    instance, service, initial = checking_persistence
    controller = instance._update_controller
    if boundary == 'untrusted-document': instance._trusted_loaded = False
    elif boundary == 'missing-controller': instance._update_controller = None
    elif boundary == 'copied-reservation': controller.reservation = replace(controller.reservation)
    elif boundary == 'backend-reservation': service.update_manager.native_handoff.active.reservation = replace(controller.reservation)
    elif boundary == 'backend-phase': service.update_manager.native_handoff.active.phase = 'preparing'
    elif boundary == 'expired': instance._update_authority.clock = lambda: controller.reservation.expires + 1
    elif boundary == 'aborting': instance._update_authority.invalidate()
    elif boundary == 'locked': instance._update_authority.acknowledge({key: initial[key] for key in ('attempt_id', 'challenge')} |
        {'workspace_revision': 7, 'participant_count': 4, 'receipts': []})
    elif boundary == 'disabled': monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', False)
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    try:
        assert 'error' in instance.call('draft_save', {**scope, 'expected_revision': 0,
            'content': {'text': 'Cannot establish authority with client booleans'}, 'native_ready': True})
        assert 'error' in instance.call('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_width': 300}})
        assert service.get_drafts().get(scope)['revision'] == 0
    finally:
        instance._update_controller = controller
