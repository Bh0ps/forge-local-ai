"""Ordinary speech confirmations prove saved SQL scope/revision, no restart grant."""
import base64
import threading
import time
from types import SimpleNamespace

import pytest

from dictation import Dictation
from forge_native_input import NativeInputGuard
from forge_service import ForgeService
from forge_store import ForgeStore


@pytest.fixture
def saved(tmp_path):
    service = object.__new__(ForgeService)
    service.store = ForgeStore(tmp_path)
    service.manager_lock = threading.RLock(); service.admission_lock = threading.RLock()
    service.draft_manager = None; service.integrations = None; service.update_manager = None
    service.stop_event = threading.Event()
    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text=' synthetic saved speech')]), SimpleNamespace(language='en')
    speech = Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model())
    guard = NativeInputGuard(speech); service.host_dictation_guard = guard.confirm
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    with guard.operation(service.get_drafts().scope(scope)):
        speech.upload(base64.b64encode(b'synthetic audio').decode())
    deadline = time.monotonic() + 2
    while speech.status()['state'] != 'done' and time.monotonic() < deadline: time.sleep(.005)
    token = guard.decorate(speech.status())['delivery_id']
    service.dispatch('draft_save', {**scope, 'expected_revision': 0, 'content': {'text': speech.status()['text']}})
    return service, guard, scope, token


def test_ordinary_confirm_returns_only_fixed_saved_status_and_never_initializes_update_authority(saved):
    service, guard, scope, token = saved
    response = service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token]})
    assert response == {'ok': True, 'confirmed': True} and service.update_manager is None
    assert guard.idle() is True
    assert service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token]}) == response
    assert token not in str(response) and 'receipt' not in response


@pytest.mark.parametrize('revision', [0, True, '1', 2])
def test_unsaved_bool_or_stale_revision_never_consumes_delivery(saved, revision):
    service, guard, scope, token = saved
    with pytest.raises(ValueError): service.dispatch('draft_confirm', {**scope, 'expected_revision': revision, 'delivery_ids': [token]})
    assert guard.idle() is False


@pytest.mark.parametrize('ids', [[], ['f' * 64], ['f' * 64] * 17, [True], [{'private': 'synthetic'}]])
def test_unknown_malformed_or_unbounded_ids_do_not_claim_saved_work(saved, ids):
    service, guard, scope, _ = saved
    with pytest.raises(ValueError): service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': ids})
    assert guard.idle() is False


def test_correct_id_cannot_be_confirmed_against_another_saved_scope(saved):
    service, guard, _, token = saved
    chat = service.store.create_chat(None, 'Synthetic other chat', '')
    scope = {'scope_kind': 'chat', 'project_id': None, 'chat_id': chat['id']}
    service.dispatch('draft_save', {**scope, 'expected_revision': 0, 'content': {'text': 'Synthetic other work'}})
    with pytest.raises(ValueError, match='does not match'):
        service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token]})
    assert guard.idle() is False


@pytest.mark.parametrize('extra', [{'digest': 'synthetic-private'}, {'command': 'synthetic-command'}, {'ready': True}])
def test_foreign_readiness_fields_cannot_grant_ordinary_delivery_confirmation(saved, extra):
    service, guard, scope, token = saved
    with pytest.raises(ValueError): service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token], **extra})
    assert guard.idle() is False


def test_saved_confirmation_waits_for_same_admission_gate_as_updates(saved):
    service, guard, scope, token = saved
    started = threading.Event(); results = []
    def confirm():
        started.set()
        try: results.append(service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token]}))
        except ValueError: results.append('blocked')
    with service.admission_lock:
        worker = threading.Thread(target=confirm); worker.start(); assert started.wait(1)
        service.update_manager = SimpleNamespace(applying=True, shutting_down=False)
    worker.join(2)
    assert results == ['blocked'] and guard.idle() is False


def test_shutdown_never_accepts_an_ordinary_saved_delivery(saved):
    service, guard, scope, token = saved
    service.stop_event.set()
    with pytest.raises(ValueError): service.dispatch('draft_confirm', {**scope, 'expected_revision': 1, 'delivery_ids': [token]})
    assert guard.idle() is False


def test_explicit_chat_deletion_retires_only_its_bound_native_input(saved):
    service, guard, _, original = saved
    chat = service.store.create_chat(None, 'Synthetic deleted chat', '')
    scope = {'kind': 'chat', 'project_id': None, 'chat_id': chat['id']}
    with guard.operation(scope):
        guard.dictation.upload(base64.b64encode(b'synthetic speech').decode())
    deadline = time.monotonic() + 2
    while guard.dictation.status()['state'] != 'done' and time.monotonic() < deadline: time.sleep(.005)
    deleted = guard.decorate(guard.dictation.status())['delivery_id']
    assert guard.discard_chat(chat['id'])
    deadline = time.monotonic() + 2
    while guard.pending and time.monotonic() < deadline: time.sleep(.005)
    assert guard.deliveries[deleted]['consumed'] is True
    assert guard.deliveries[original]['consumed'] is False and guard.idle() is False


def test_service_calls_native_retirement_only_after_acknowledged_chat_deletion(saved, monkeypatch):
    service, _, _, _ = saved
    callbacks = []
    service.host_chat_input_deleted = callbacks.append
    monkeypatch.setattr('forge_chats.dispatch_chat', lambda host,action,data:
        {'ok': True, 'deleted': 'synthetic-confirmed-chat'})
    assert service.dispatch('chat_delete', {'id': 'synthetic-request-chat'})['ok']
    assert callbacks == ['synthetic-confirmed-chat']
    def failed(*args): raise ValueError('Synthetic deletion refused')
    monkeypatch.setattr('forge_chats.dispatch_chat', failed)
    with pytest.raises(ValueError): service.dispatch('chat_delete', {'id': 'other'})
    assert callbacks == ['synthetic-confirmed-chat']
