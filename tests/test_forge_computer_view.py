"""Desktop evidence projection uses disposable journals, never desktop/vault I/O."""
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

import forge_computer_view as view
from forge_service import ForgeService, READ_ACTIONS
from forge_store import ForgeStore


@pytest.fixture
def workspace(tmp_path):
    store = ForgeStore(tmp_path / 'disposable')
    first = store.create_chat(title='Synthetic first')
    other = store.create_chat(title='Synthetic other')
    parent = store.create_run({'chat_id': first['id']})
    foreign = store.create_run({'chat_id': other['id']})
    helper_chat = store.create_chat(title='Synthetic helper')
    helper = store.create_run({'chat_id': helper_chat['id'], 'parent_id': parent['id']})
    return SimpleNamespace(store=store, chat=first['id'], parent=parent['id'], helper=helper['id'], foreign=foreign['id'])


def pixels(size=(32, 24), kind='PNG'):
    output = io.BytesIO()
    Image.new('RGB', size, (124, 96, 80)).save(output, kind)
    return base64.b64encode(output.getvalue()).decode('ascii')


def record(value, run=None, name='computer_inspect', result=None, status='completed', suffix='first'):
    run = run or value.parent
    identifier = run + ':' + suffix
    result = result if result is not None else {'snapshot_id': 'synthetic-snapshot', 'image': pixels(), 'mime_type': 'image/jpeg',
                                                'image_size': [999, 999], 'elements': [{'name': 'synthetic-private-title'}]}
    args = {'window_id': 123, 'path': 'synthetic-private-path', 'text': 'synthetic-private-secret'}
    value.store.invocation(identifier, run, name, args)
    artifact = value.store.artifact({'name': name, 'arguments': args, 'result': result})
    value.store.invocation_state(identifier, status, {'artifact': artifact, 'result': result})
    return identifier, artifact


def test_exact_chat_tree_includes_its_helper_but_excludes_other_work(workspace):
    value = workspace
    record(value)
    own, _ = record(value, run=value.helper, suffix='child')
    record(value, run=value.foreign, result={'image': pixels((70, 60)), 'title': 'synthetic-private-foreign'})
    projected = view.computer_view_state(value.store, {'chat_id': value.chat})
    assert projected['action_count'] == 2 and projected['invocation_id'] == own
    assert projected['snapshot']['run_id'] == value.helper
    assert projected['snapshot']['width'] == 32 and projected['snapshot']['height'] == 24
    assert projected['snapshot']['mime_type'] == 'image/png'
    assert 'synthetic-private' not in json.dumps(projected)
    assert set(projected['snapshot']) == {'run_id', 'invocation_id', 'data_url', 'mime_type', 'width', 'height', 'age_seconds'}
    with pytest.raises(ValueError, match='does not belong'):
        view.computer_view_state(value.store, {'chat_id': value.chat, 'run_id': value.foreign})


def test_optional_run_scope_never_uses_older_unrelated_assignment_in_same_chat(workspace):
    value = workspace
    record(value)
    value.store.update_run(value.parent, status='completed')
    next_run = value.store.create_run({'chat_id': value.chat})
    projected = view.computer_view_state(value.store, {'chat_id': value.chat, 'run_id': next_run['id']})
    assert projected['snapshot'] is None and projected['action_count'] == 0
    assert projected['scope_run_id'] == next_run['id'] and projected['status'] == 'queued'


@pytest.mark.parametrize('selected', [False, True])
def test_completed_helper_evidence_does_not_mark_its_running_parent_complete(workspace, selected):
    value = workspace
    value.store.update_run(value.parent, status='running')
    value.store.update_run(value.helper, status='completed')
    record(value, run=value.helper)
    result = view.computer_view_state(value.store, {'chat_id': value.chat, **({'run_id': value.parent} if selected else {})})
    assert result['status'] == 'running' and result['run_id'] == value.parent
    assert result['snapshot']['run_id'] == value.helper


@pytest.mark.parametrize('status', ['prepared', 'running', 'failed', 'cancelled', 'outcome_unknown'])
def test_only_acknowledged_completed_inspections_supply_pixels(workspace, status):
    record(workspace, status=status)
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['action_count'] == 0 and result['snapshot'] is None


def test_text_only_model_and_accessibility_only_evidence_are_independent(workspace):
    value = workspace
    value.store.update_run(value.parent, settings={'model': 'synthetic-text-only'}, vision_image=None)
    record(value, result={'elements': [{'name': 'synthetic-private-title'}]})
    result = view.computer_view_state(value.store, {'chat_id': value.chat})
    assert result['evidence_mode'] == 'accessibility_only' and result['snapshot'] is None
    record(value, suffix='image')
    assert view.computer_view_state(value.store, {'chat_id': value.chat})['evidence_mode'] == 'image'


@pytest.mark.parametrize('reference', ['../outside', '/absolute/private', 'z' * 32, 'a' * 33])
def test_arbitrary_artifact_references_are_refused_without_read(workspace, monkeypatch, reference):
    identifier, _ = record(workspace)
    workspace.store.invocation_state(identifier, 'completed', {'artifact': reference, 'result': {'image': pixels()}})
    monkeypatch.setattr(view, '_record', lambda _: pytest.fail('No path read for invalid reference'))
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['failure'] == 'evidence_unavailable' and result['snapshot'] is None


def test_artifact_must_match_the_acknowledged_tool_result(workspace):
    identifier, artifact = record(workspace)
    path = workspace.store.home / 'artifacts' / (artifact + '.json')
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved['result']['snapshot_id'] = 'changed-snapshot'
    path.write_text(json.dumps(saved), encoding='utf-8')
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['failure'] == 'evidence_unavailable' and result['snapshot'] is None


@pytest.mark.parametrize('image', ['synthetic-private-invalid-image', 'A' * 8_000_001], ids=['invalid-encoding', 'oversize-encoding'])
def test_invalid_or_oversize_pixels_have_a_fixed_failure(workspace, image):
    record(workspace, result={'image': image, 'error_detail': 'synthetic-private-error'})
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['failure'] == 'evidence_unavailable' and result['snapshot'] is None
    assert 'synthetic-private' not in json.dumps(result)


def test_bounded_file_and_dimension_checks_are_enforced(workspace, monkeypatch):
    record(workspace)
    monkeypatch.setattr(view, 'MAX_ARTIFACT_BYTES', 8)
    assert view.computer_view_state(workspace.store, {'chat_id': workspace.chat})['failure'] == 'evidence_unavailable'
    monkeypatch.undo()
    record(workspace, result={'image': pixels((5000, 4100))}, suffix='large-pixels')
    assert view.computer_view_state(workspace.store, {'chat_id': workspace.chat})['snapshot'] is None


def test_latest_refusal_does_not_misrepresent_an_older_image_as_current(workspace):
    record(workspace)
    record(workspace, result={'not_executed': True, 'error': 'synthetic-private-refusal'}, suffix='refused')
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['failure'] == 'inspection_refused' and result['snapshot'] is None
    assert 'synthetic-private' not in json.dumps(result)


def test_clipboard_result_and_raw_errors_are_never_read_as_view_content(workspace, monkeypatch):
    record(workspace, name='computer_clipboard_read', result={'text': 'synthetic-private-clipboard'})
    monkeypatch.setattr(view, '_record', lambda _: pytest.fail('Clipboard artifact must not be read'))
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['action_count'] == 1 and result['tool'] == 'computer_clipboard_read'
    assert 'synthetic-private' not in json.dumps(result)


def test_value_pattern_step_is_counted_and_keeps_inspection_as_earlier_evidence(workspace):
    first, _ = record(workspace)
    latest, _ = record(workspace, name='computer_set_value', result={'ok': True, 'inspect_again': True}, suffix='value')
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['tool'] == 'computer_set_value' and result['invocation_id'] == latest
    assert result['action_count'] == 2 and result['snapshot']['invocation_id'] == first


def test_journal_replay_updates_the_same_invocation_and_only_current_result_counts(workspace):
    identifier, artifact = record(workspace)
    saved = json.loads((workspace.store.home / 'artifacts' / (artifact + '.json')).read_text(encoding='utf-8'))
    workspace.store.invocation_state(identifier, 'completed', {'artifact': artifact, 'result': saved['result']})
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['action_count'] == 1 and result['snapshot']['invocation_id'] == identifier


def test_hardlinked_evidence_is_not_read(workspace, tmp_path):
    import os
    _, artifact = record(workspace)
    path = workspace.store.home / 'artifacts' / (artifact + '.json')
    os.link(path, tmp_path / 'linked-evidence')
    result = view.computer_view_state(workspace.store, {'chat_id': workspace.chat})
    assert result['failure'] == 'evidence_unavailable' and result['snapshot'] is None


@pytest.mark.parametrize('data', [{}, {'chat_id': '../private'}, {'chat_id': 'missing'}, {'chat_id': 'synthetic', 'path': 'private'}])
def test_invalid_scope_is_fixed_and_read_only(workspace, data):
    with pytest.raises(ValueError): view.computer_view_state(workspace.store, data)


def test_service_uses_only_read_dispatch_during_update_and_never_initializes_broker(workspace):
    record(workspace)
    service = object.__new__(ForgeService)
    service.store = workspace.store
    service.update_manager = SimpleNamespace(applying=True)
    service.computer_broker = None
    assert 'computer_view_state' in READ_ACTIONS
    result = service.dispatch('computer_view_state', {'chat_id': workspace.chat})
    assert result['snapshot'] and service.computer_broker is None
