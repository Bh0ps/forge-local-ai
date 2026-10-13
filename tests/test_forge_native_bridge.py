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


def test_queued_native_operation_rechecks_lock_before_executing_and_retains_pending_until_done(bridge, monkeypatch):
    instance, _, form = bridge
    queued, changes = [], []
    form.InvokeRequired = True
    form.BeginInvoke = queued.append
    # Exercise the actual timeout path without waiting five seconds in a unit
    # test. A queued operation still counts until its native callback finishes.
    class Event:
        def wait(self, duration): return False
        def set(self): pass
    monkeypatch.setattr('desktop.threading.Event', Event)
    with pytest.raises(TimeoutError): instance._native(lambda form: changes.append('changed'))
    assert instance._native_pending == 1 and instance._update_input_idle() is False
    binding = NativeBinding(instance, 'a' * 64, instance._window.uid, 0, instance._server_url, 1, 1.0)
    instance._update_authority.update_binding(binding)
    first = instance._update_authority.begin(binding, object())
    queued.pop()()
    assert changes == [] and instance._native_pending == 0
    instance._update_authority.acknowledge_abort(first['attempt_id'])
    assert instance._update_input_idle() is True


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


def test_default_handoff_gate_refuses_before_service_window_or_home_loading(bridge, monkeypatch):
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
    manager = SimpleNamespace(home='synthetic-private-home', current_version='5.1.4', native_status=lambda value: dict(status))
    monkeypatch.setattr('forge_updates.load_operation', lambda *args:
        {'state': state, 'trust_mode': mode, 'version': version})
    result = instance._native_update_status(SimpleNamespace(manager=manager), object())
    assert result['safe_rollback_available'] is wanted
    assert 'synthetic-private-home' not in str(result)
    status['inspection_required'] = True
    assert instance._native_update_status(SimpleNamespace(manager=manager), object())['safe_rollback_available'] is False
