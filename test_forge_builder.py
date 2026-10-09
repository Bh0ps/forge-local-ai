"""Builder tests use isolated projects, attachments, loopback servers and fake views."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import zipfile

import httpx
import pytest

from forge_builder import BuilderManager
from forge_documents import DocumentManager, extract_document
from forge_store import ForgeStore
from forge_templates import template_files
from native_browser import NativeBrowser
from test_native_browser import FakePage


@pytest.fixture
def builder(tmp_path):
    folder = tmp_path / 'project'
    folder.mkdir()
    store = ForgeStore(tmp_path / 'state')
    project = store.create_project('Fixture', str(folder))
    service = SimpleNamespace(store=store)
    manager = BuilderManager(service)
    record = manager.save({'project_id': project['id'], 'title': 'Fixture app', 'objective': 'Keep ideas locally', 'directory': '.',
                           'requirements': [{'text': 'Add and remove ideas', 'acceptance': 'The list updates without reloading.'}]})
    yield manager, record, folder, service
    manager.shutdown()


def test_revision_history_rejects_stale_edit_and_preserves_requirement_ids(builder):
    manager, record, _, _ = builder
    second = manager.save({**record, 'expected_revision': record['revision'], 'objective': 'Changed objective'})
    assert second['revision'] == 2
    assert second['requirements'][0]['id'] == record['requirements'][0]['id']
    with pytest.raises(ValueError, match='changed'):
        manager.save({**record, 'expected_revision': 1})
    assert [r['revision'] for r in manager.store.entities('builder_revisions')] == [1, 2]


def test_unchanged_brief_retains_revision_checks_and_evidence(builder):
    manager, record, folder, _ = builder
    (folder / 'index.html').write_text('Fixture', encoding='utf-8')
    manager.check({'id': record['id'], 'expected_revision': 1, 'gate': 'functional',
                   'status': 'passed', 'source': 'manual', 'note': 'Primary flow observed.'}, human=True)
    saved = manager.store.entity('builders', record['id'])
    result = manager.save({**saved, 'expected_revision': 1})
    assert result == saved
    assert len(manager.store.entities('builder_revisions')) == 1
    assert result['gates']['functional']['status'] == 'passed'
    with pytest.raises(ValueError, match='changed'):
        manager.save({**saved, 'expected_revision': 0})


def test_runtime_poll_returns_latest_failed_preview_without_hashing(builder, monkeypatch):
    manager, record, _, _ = builder
    monkeypatch.setattr(manager, 'fingerprint', lambda *_: pytest.fail('Runtime polling must not hash project files'))
    monkeypatch.setattr(manager, 'refresh_build_gate', lambda *_: pytest.fail('Runtime polling must not discover build files'))
    previews = [{'id': 'old', 'builder_id': record['id'], 'created_at': '2026-01-01', 'status': 'stopped'},
                {'id': 'new', 'builder_id': record['id'], 'created_at': '2026-01-02', 'status': 'failed', 'error': 'Port occupied'},
                {'id': 'other', 'builder_id': 'elsewhere', 'created_at': '2026-01-03', 'status': 'ready'}]
    monkeypatch.setattr(manager.previews, 'status', lambda **_: {'previews': previews})
    result = manager.runtime_get(record['id'])
    assert [item['id'] for item in result['previews']] == ['new', 'old']
    assert result['freshness'] == 'recorded'
    assert result['revision'] == record['revision']


def test_builder_start_retains_submission_identity_on_new_and_existing_goal(builder):
    manager, record, _, service = builder
    service.goal_create = lambda data: manager.store.save_goal({'id': '0123456789abcdef0123456789abcdef', 'status': 'ready',
        'request': data['text'], **data})
    submitted = []
    service.goal_resume = lambda data: submitted.append(data) or {'id': 'run', 'goal_id': data['id']}
    request = {'id': record['id'], 'expected_revision': 1, 'source_key': 'submission:fixture', 'client_submission_id': 'fixture'}
    manager.start_goal(request)
    manager.start_goal(request)
    assert submitted == [{'id': '0123456789abcdef0123456789abcdef', 'source_key': 'submission:fixture', 'client_submission_id': 'fixture'}] * 2
    assert len(manager.store.entities('goals')) == 1


def test_runtime_get_resolves_latest_admitted_parent_without_goal_run_id(builder, monkeypatch):
    manager, record, _, _ = builder
    chat = manager.store.create_chat(record['project_id'], 'Builder fixture', 'fixture')
    goal = manager.store.save_goal({'title': 'Runtime fixture', 'request': record['objective'], 'tasks': [{'text': 'Inspect app'}],
        'status': 'ready', 'chat_id': chat['id'], 'project_id': record['project_id']})
    manager.store.save_entity('builders', {**record, 'goal_id': goal['id'], 'chat_id': chat['id']})
    run, _ = manager.store.accept_request({'project_id': record['project_id'], 'settings': {'model': 'fixture'},
        'request': 'Implement the brief', 'goal_id': goal['id'], 'parent_id': None}, chat['id'])
    manager.store.update_run(run['id'], status='paused', execution_mode='guided', planner_assignment={'state': 'consumed'})
    manager.store.accept_request({'project_id': record['project_id'], 'settings': {'model': 'fixture'},
        'request': 'Read-only specialist', 'goal_id': goal['id'], 'parent_id': run['id']})
    monkeypatch.setattr(manager.store, 'runs', lambda *_, **__: pytest.fail('Runtime polling must not scan all runs'))
    result = manager.runtime_get(record['id'])
    assert 'run_id' not in manager.store.goal(goal['id'])
    assert result['run']['id'] == run['id']
    assert result['goal']['run_id'] == run['id']
    assert result['goal']['execution_mode'] == 'guided'
    assert result['goal']['planner_assignment']['state'] == 'consumed'


def test_ordinary_goal_reusing_builder_chat_does_not_inherit_other_brief_context(builder):
    from forge_service import ForgeService
    manager, record, _, service = builder
    chat = manager.store.create_chat(record['project_id'], 'Shared chat', 'fixture')
    first = manager.store.save_goal({'title': 'Builder goal', 'request': record['objective'], 'tasks': [{'text': 'Build app'}],
        'status': 'ready', 'chat_id': chat['id'], 'project_id': record['project_id']})
    manager.store.save_goal({**first, 'builder_id': record['id']})
    manager.store.save_entity('builders', {**record, 'goal_id': first['id'], 'chat_id': chat['id']})
    other = manager.store.save_goal({'title': 'Ordinary goal', 'request': 'Review this project', 'tasks': [{'text': 'Review code'}],
        'status': 'ready', 'chat_id': chat['id'], 'project_id': record['project_id']})
    service.associated_builder = lambda run: ForgeService.associated_builder(service, run)
    common = {'chat_id': chat['id'], 'project_id': record['project_id'], 'settings': {'context': 8192}}
    assert record['id'] in manager.context({**common, 'goal_id': first['id']})
    assert manager.context({**common, 'goal_id': other['id']}) == ''


def test_gate_applicability_manual_origin_and_code_change_invalidation(builder):
    manager, record, folder, _ = builder
    (folder / 'index.html').write_text('Fixture', encoding='utf-8')
    assert record['gates']['artifact']['status'] == 'not_applicable'
    assert record['gates']['build']['status'] == 'not_applicable'
    data = {'id': record['id'], 'expected_revision': 1, 'gate': 'functional', 'status': 'passed', 'source': 'manual', 'note': 'Added then removed an idea.'}
    with pytest.raises(ValueError, match='direct user'):
        manager.check(data)
    manager.check(data, human=True)
    assert manager.get(record['id'])['gates']['functional']['status'] == 'passed'
    (folder / 'index.html').write_text('Changed app', encoding='utf-8')
    current = manager.get(record['id'])
    assert current['gates']['functional']['status'] == 'pending'
    assert current['gates']['functional']['stale']


def test_existing_vite_app_requires_build_without_selecting_starter(builder, monkeypatch):
    manager, record, folder, _ = builder
    (folder / 'package.json').write_text(json.dumps({'scripts': {'build': 'tsc --noEmit && vite build'}}), encoding='utf-8')
    (folder / 'vite.config.ts').write_text('throw new Error("Configuration must never execute during discovery");', encoding='utf-8')
    monkeypatch.setattr('forge_builder.subprocess.run', lambda *args, **kwargs: pytest.fail('Build discovery executed a command.'))
    saved = manager.save({**record, 'expected_revision': 1})
    current = manager.get(saved['id'])
    assert current['template'] == 'static'
    assert current['gates']['build']['status'] == 'pending'
    assert current['gates']['build']['required']
    assert 'build script' in current['gates']['build']['applicability_reason']


def test_new_app_package_refreshes_build_gate_on_get_and_recording_other_checks(builder):
    manager, record, folder, _ = builder
    assert manager.get(record['id'])['gates']['build']['status'] == 'not_applicable'
    (folder / 'package.json').write_text(json.dumps({'scripts': {'build': 'vite build'}}), encoding='utf-8')
    # Check refresh must also work before a polling GET has observed the files.
    checked = manager.check({'id': record['id'], 'expected_revision': 1, 'gate': 'functional',
        'status': 'passed', 'source': 'manual', 'note': 'Added and removed an idea.'}, human=True)
    assert checked['gates']['build']['status'] == 'pending'
    assert checked['gates']['functional']['status'] == 'passed'
    assert not manager.get(record['id'])['complete']
    # A separate newly generated configuration must be detected by GET itself.
    second = manager.save({'project_id': record['project_id'], 'directory': 'second', 'title': 'Second app'})
    assert manager.get(second['id'])['gates']['build']['status'] == 'not_applicable'
    (folder / 'second').mkdir()
    (folder / 'second/package.json').write_text('{"scripts":{"build":"vite build"}}', encoding='utf-8')
    assert manager.get(second['id'])['gates']['build']['status'] == 'pending'


def test_build_discovery_preserves_static_backend_and_explicit_checks(builder, monkeypatch):
    manager, record, folder, _ = builder
    (folder / 'package.json').write_text('{"scripts":{"test":"node --test"}}', encoding='utf-8')
    assert manager.get(record['id'])['gates']['build']['status'] == 'not_applicable'
    (folder / 'tsconfig.json').write_text('{}', encoding='utf-8')
    assert manager.get(record['id'])['gates']['build']['status'] == 'pending'
    configured = manager.save({**record, 'expected_revision': 1, 'check_commands': {'build': ['node', 'build.cjs']}})
    assert configured['check_commands']['build'] == ['node', 'build.cjs']
    assert configured['gates']['build']['status'] == 'pending'
    backend = manager.template({'id': record['id'], 'expected_revision': 2, 'template': 'fastapi', 'directory': 'backend'})['builder']
    assert manager.get(backend['id'])['gates']['build']['status'] == 'pending'
    (folder / 'package.json').unlink()
    (folder / 'tsconfig.json').unlink()
    fresh = manager.save({'project_id': record['project_id'], 'directory': '.', 'title': 'Preview app'})
    monkeypatch.setattr(manager.previews, 'status', lambda **kwargs: {'previews': [{'builder_id': fresh['id'],
        'mode': 'vite', 'status': 'ready', 'cwd': '.'}]})
    assert manager.get(fresh['id'])['gates']['build']['status'] == 'pending'


def test_goal_requirement_ids_reconcile_after_brief_revision(builder):
    manager, record, _, service = builder
    service.goal_create = lambda data: manager.store.save_goal({'title': data['title'], 'request': data['text'],
        'tasks': data['tasks'], 'project_id': data['project_id'], 'chat_id': data['chat_id'], 'status': 'ready'})
    service.goal_resume = lambda data: {'id': 'fixture-run', 'goal_id': data['id']}
    result = manager.start_goal({'id': record['id'], 'expected_revision': 1})
    goal = manager.store.goal(result['goal_id'])
    assert goal['tasks'][0]['requirement_id'] == record['requirements'][0]['id']
    goal['tasks'][0]['status'] = 'completed'
    goal['tasks'][0]['evidence'] = ['Observed working UI']
    manager.store.save_goal(goal)
    current = manager.store.entity('builders', record['id'])
    updated = manager.save({**current, 'expected_revision': 1, 'requirements': current['requirements'] +
                          [{'text': 'Search saved ideas', 'acceptance': 'Matching ideas appear.'}]})
    goal = manager.store.goal(updated['goal_id'])
    assert goal['builder_revision'] == 2
    assert goal['tasks'][0]['status'] == 'completed' and goal['tasks'][0]['evidence']
    assert goal['tasks'][1]['status'] == 'pending'
    assert goal['tasks'][1]['requirement_id'] == updated['requirements'][1]['id']


def test_large_builder_context_is_byte_bounded_and_pages_preserve_full_criteria_and_gates(builder):
    from forge_builder import BUILDER_REFERENCE_POLICY
    manager, original, _, service = builder
    requirements = [{'id': f'requirement-{index:02}', 'text': f'Requirement {index}: ' + 'x' * 1980,
        'acceptance': f'Criterion {index}: ' + '世界' * 1490} for index in range(40)]
    chat = manager.store.create_chat(original['project_id'], 'Large brief', 'fixture')
    record = manager.save({**original, 'expected_revision': 1, 'chat_id': chat['id'], 'requirements': requirements,
        'objective': 'O' * 12000, 'audience': 'A' * 2000, 'constraints': 'C' * 6000,
        'design': {key: '風格' * 1000 for key in ('style', 'colors', 'typography', 'layout', 'reference_notes')}})
    service.goal_create = lambda data: manager.store.save_goal({'title': data['title'], 'request': data['text'],
        'tasks': data['tasks'], 'project_id': data['project_id'], 'chat_id': data['chat_id'], 'status': 'ready'})
    service.goal_resume = lambda data: {'id': 'fixture-run', 'goal_id': data['id']}
    result = manager.start_goal({'id': record['id'], 'expected_revision': 2})
    goal = manager.store.goal(result['goal_id'])
    goal['tasks'][0]['status'] = 'completed'
    manager.store.save_goal(goal)
    references = [manager.documents.upload({'name': f'Never-inject-name-{index}.txt',
        'content_base64': base64.b64encode(f'Never inject document content {index}'.encode()).decode()})['id'] for index in range(20)]
    run = {'id': 'fixture-run', 'chat_id': chat['id'], 'project_id': record['project_id'], 'document_ids': references}
    before = manager.store.entity('builders', record['id'])
    for context, host in ((1000, 4000), (4096, 4000), (8192, 4000), (32768, 4000), (32768, 1000)):
        value = manager.context({**run, 'settings': {'context': context}, 'builder_context_budget': host})
        assert len(value.encode('utf-8')) <= min(4000, max(1000, context // 3), max(1000, host))
        assert value.startswith(BUILDER_REFERENCE_POLICY)
        packet = json.loads(value[len(BUILDER_REFERENCE_POLICY):])
        assert packet['partial'] and packet['revision'] == 2 and packet['builder_id'] == record['id']
        assert packet['directory'] == '.' and packet['requirement_count'] == 40
        assert packet['current_requirement']['id'] == requirements[1]['id']
        assert requirements[1]['acceptance'].startswith(packet['current_requirement']['acceptance_excerpt'])
        assert packet['current_requirement']['acceptance_excerpt']
        assert packet['design_excerpt']
        assert packet['documents']['count'] == 20 and packet['documents']['ids'][0] == references[0]
        assert 'Never inject document content' not in value and 'Never-inject-name' not in value
        assert requirements[-1]['acceptance'] not in value
    # Tool/API default callers still receive the full durable record.
    assert manager.dispatch('builder_get', {'id': record['id']})['requirements'] == requirements
    full_read = manager.execute(run, 'builder_read', {'id': record['id']})
    assert full_read['requirements'] == requirements and full_read['document_ids'] == references
    pages, start = [], 0
    while True:
        page = manager.execute(run, 'builder_read', {'id': record['id'], 'requirement_id': requirements[1]['id'],
            'start': start, 'limit': 240})
        assert page['revision'] == 2 and len(page['text']) <= 240
        pages.append(page['text'])
        if not page['has_more']:
            break
        assert page['partial'] and page['next'] > start
        start = page['next']
    assert json.loads(''.join(pages)) == requirements[1]
    after = manager.store.entity('builders', record['id'])
    assert after['requirements'] == before['requirements'] and after['gates'] == before['gates']
    assert len(manager.store.goal(result['goal_id'])['tasks']) == 41
    schema = next(item for item in manager.schemas(run) if item['function']['name'] == 'builder_read')
    assert {'requirement_id', 'start', 'limit'} <= set(schema['function']['parameters']['properties'])


def test_builder_reference_and_page_bounds_include_encoded_ids_and_document_only_context(builder):
    manager, record, _, _ = builder
    changed = manager.save({**record, 'expected_revision': 1, 'requirements': [{'id': '\x01' * 64,
        'text': 'Task', 'acceptance': 'Observe behavior.'}]})
    value = manager.context({'project_id': record['project_id'], 'settings': {'context': 2048},
        'document_ids': ['f' * 32] * 20})
    assert len(value.encode()) <= 1000 and '\\u0001' in value
    references = ['f' * 32] * 20
    document_context = manager.context({'project_id': None, 'settings': {'context': 2048}, 'document_ids': references})
    assert len(document_context.encode()) <= 1000 and '"count":20' in document_context
    assert manager.context({'project_id': None, 'document_ids': []}) == ''
    for args in ({'start': -1}, {'start': True}, {'limit': 0}, {'limit': 48001}, {'requirement_id': 'missing'}):
        with pytest.raises(ValueError):
            manager.dispatch('builder_get', {'id': changed['id'], **args})
    page = manager.dispatch('builder_get', {'id': changed['id'], 'start': 0, 'limit': 120})
    assert page['scope'] == 'brief' and page['partial'] and page['has_more']


def test_project_disconnect_stops_preview_and_preserves_brief_and_files(builder):
    manager, record, folder, _ = builder
    manager.template({'id': record['id'], 'expected_revision': 1, 'directory': 'site'})
    preview = manager.dispatch('preview_start', {'builder_id': record['id']})
    manager.close_project(record['project_id'])
    assert manager.previews.status(preview['id'])['status'] == 'stopped'
    assert manager.store.entity('builders', record['id'])['project_missing']
    assert (folder / 'site/index.html').is_file()


def test_preview_stop_never_holds_ownership_lock_while_waiting_for_browser(builder):
    import threading
    manager, record, _, _ = builder
    manager.template({'id': record['id'], 'expected_revision': 1, 'directory': 'site'})
    preview = manager.dispatch('preview_start', {'builder_id': record['id']})
    observed = []
    class Browser:
        cancelled = threading.Event()
        def stop(self):
            complete = threading.Event()
            def navigation_guard():
                observed.append(manager.previews.allowed_url(preview['url']))
                complete.set()
            thread = threading.Thread(target=navigation_guard, daemon=True)
            thread.start()
            assert complete.wait(1), 'Stop held the lock needed by the navigation guard.'
            thread.join(1)
        def dispatch(self, action):
            assert action == 'hide'
        def shutdown(self): pass
    manager.previews.browser = Browser()
    manager.previews.browser_preview = preview['id']
    manager.previews.stop(preview['id'])
    assert observed == [False]
    assert manager.previews.status(preview['id'])['status'] == 'stopped'


def test_worktree_document_exports_never_write_original_and_preview_is_rejected(builder, tmp_path):
    manager, record, folder, _ = builder
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    project = manager.store.create_project('Worktree fixture', str(worktree))
    run = {'id': 'child', 'parent_id': 'parent', 'project_id': record['project_id'], 'workspace_project_id': project['id']}
    result = manager.execute(run, 'document_create', {'path': 'exports/report.txt', 'text': 'Worktree output', 'builder_id': record['id']})
    assert (worktree / 'exports/report.txt').read_text() == 'Worktree output'
    assert not (folder / 'exports/report.txt').exists()
    assert result['verification']['project_id'] == project['id'] and result['verification']['builder_id'] is None
    assert result['original_project_id'] == record['project_id']
    with pytest.raises(ValueError, match='Worktree helpers'):
        manager.execute(run, 'preview_start', {'builder_id': record['id']})
    assert not any(schema['function']['name'].startswith('preview_') for schema in manager.schemas(run))


def test_artifact_gate_requires_every_explicit_output(builder):
    manager, record, _, _ = builder
    current = manager.save({**record, 'expected_revision': 1, 'outputs': ['exports/a.txt', 'exports/b.txt']})
    first = manager.documents.create({'project_id': current['project_id'], 'builder_id': current['id'],
        'path': 'exports/a.txt', 'text': 'First output'})
    check = {'id': current['id'], 'expected_revision': 2, 'gate': 'artifact', 'status': 'passed',
             'source': 'artifact', 'reference': first['verification']['id']}
    with pytest.raises(ValueError, match='exports/b.txt'):
        manager.check(check)
    with pytest.raises(ValueError, match='exports/b.txt'):
        manager.check({**check, 'source': 'manual', 'note': 'Inspected the documents.'}, human=True)
    second = manager.documents.create({'project_id': current['project_id'], 'builder_id': current['id'],
        'path': 'exports/b.txt', 'text': 'Second output'})
    passed = manager.check({**check, 'reference': second['verification']['id']})
    assert len(passed['gates']['artifact']['evidence'][0]['artifacts']) == 2


def test_external_export_changes_or_deletion_revoke_artifact_gate_and_hash_cache(builder, monkeypatch):
    manager, record, folder, _ = builder
    manager.template({'id': record['id'], 'expected_revision': 1, 'directory': 'site'})
    current = manager.store.entity('builders', record['id'])
    current = manager.save({**current, 'expected_revision': 1, 'outputs': ['exports/report.txt']})
    result = manager.documents.create({'project_id': current['project_id'], 'builder_id': current['id'],
        'path': 'exports/report.txt', 'text': 'Verified original'})
    check = {'id': current['id'], 'expected_revision': 2, 'gate': 'artifact', 'status': 'passed',
             'source': 'artifact', 'reference': result['verification']['id']}
    manager.check(check)
    import forge_documents
    opened = []
    original = forge_documents.os.open
    def counted(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(forge_documents.os, 'open', counted)
    assert manager.documents.get_artifact(result['verification']['id'])['current_verified']
    assert manager.documents.get_artifact(result['verification']['id'])['current_verified']
    assert not opened, 'Unchanged receipts should reuse the metadata-bound hash.'
    (folder / 'exports/report.txt').write_text('Changed external export', encoding='utf-8')
    assert manager.dispatch('artifact_get', {'id': result['verification']['id']})['status'] == 'stale'
    assert manager.get(current['id'])['gates']['artifact']['status'] == 'pending'
    (folder / 'exports/report.txt').unlink()
    assert manager.documents.get_artifact(result['verification']['id'])['current_verified'] is False


def test_template_never_overwrites_and_static_preview_stops_owned_server(builder):
    manager, record, folder, _ = builder
    manager.template({'id': record['id'], 'expected_revision': 1, 'template': 'static', 'directory': 'site'})
    with pytest.raises(ValueError, match='empty'):
        manager.template({'id': record['id'], 'expected_revision': 1, 'directory': 'site'})
    (folder / 'site/.env').write_text('PRIVATE', encoding='utf-8')
    preview = manager.dispatch('preview_start', {'builder_id': record['id']})
    assert preview['status'] == 'ready' and preview['url'].startswith('http://127.0.0.1:')
    with httpx.Client(trust_env=False) as client:
        response = client.get(preview['url'])
        assert response.status_code == 200 and 'Fixture app' in response.text
        assert client.get(preview['url'] + '.env').status_code == 403
        assert client.get(preview['url'] + 'missing/').status_code == 404
    manager.check({'id': record['id'], 'expected_revision': 1, 'gate': 'preview', 'status': 'passed',
                   'source': 'preview', 'reference': preview['id']})
    assert manager.get(record['id'])['gates']['preview']['status'] == 'passed'
    manager.previews.stop(preview['id'])
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        httpx.get(preview['url'], timeout=.2, trust_env=False)


def test_preview_restart_records_are_never_adopted(builder):
    manager, record, _, _ = builder
    manager.store.save_entity('previews', {'id': 'old', 'project_id': record['project_id'], 'status': 'ready',
                                         'pid': 12345, 'url': 'http://127.0.0.1:1'})
    from forge_previews import PreviewManager
    restarted = PreviewManager(SimpleNamespace(store=manager.store))
    assert restarted.status('old')['status'] == 'interrupted'
    assert restarted.status('old')['url'] is None
    assert not restarted.active


def test_other_project_and_unattached_documents_are_rejected(builder, tmp_path):
    manager, record, _, _ = builder
    other = tmp_path / 'other'
    other.mkdir()
    project = manager.store.create_project('Other', str(other))
    with pytest.raises(ValueError, match='another project'):
        manager.execute({'id': 'run', 'project_id': project['id']}, 'builder_read', {'id': record['id']})
    uploaded = manager.documents.upload({'name': 'notes.txt', 'content_base64': base64.b64encode(b'Untrusted notes').decode()})
    run = {'id': 'run', 'project_id': record['project_id'], 'document_ids': []}
    with pytest.raises(ValueError, match='not attached'):
        manager.execute(run, 'document_extract', {'document_id': uploaded['id']})
    run['document_ids'] = [uploaded['id']]
    assert manager.execute(run, 'document_extract', {'document_id': uploaded['id']})['text'] == 'Untrusted notes'
    assert uploaded['id'] in manager.context(run)


def test_session_gate_rejects_echo_failed_old_and_out_of_scope_evidence(builder):
    manager, record, folder, service = builder
    (folder / 'app.py').write_text('print(1)', encoding='utf-8')
    session = {'id': 'fixture', 'project_root': str(folder), 'cwd': str(folder), 'status': 'completed',
               'created_at': time.time() + .05, 'exit_code': 0, 'argv': ['python', '-m', 'pytest', '-q']}
    service.command_sessions = SimpleNamespace(status=lambda _: dict(session))
    data = {'id': record['id'], 'expected_revision': 1, 'gate': 'functional', 'status': 'passed', 'source': 'session', 'reference': 'fixture'}
    manager.check(data)
    session['argv'] = ['python', '-c', 'print("success")']
    with pytest.raises(ValueError, match='recognized'):
        manager.check(data)
    session['argv'] = ['python', '-m', 'pytest', '-q']
    session['exit_code'] = 1
    with pytest.raises(ValueError, match='failed command'):
        manager.check(data)
    session['exit_code'] = 0
    session['created_at'] = 0
    with pytest.raises(ValueError, match='predates'):
        manager.check(data)


@pytest.mark.parametrize('argv', [
    ['pnpm', 'build'], ['pnpm.cmd', 'run', 'build'], ['yarn', 'build'], ['yarn', 'run', 'build'],
    ['bun', 'run', 'build'], ['bun', 'build', 'src/index.ts'], ['npm', 'run', 'build'],
    ['node_modules/.bin/vite.cmd', 'build'], ['node_modules/.bin/tsc.cmd', '--noEmit'],
    ['npx', '--no-install', 'vite', 'build'], ['npx', '--no-install', 'tsc', '--noEmit'],
])
def test_node_build_sessions_accept_project_package_managers_and_local_compilers(builder, argv):
    manager, record, folder, service = builder
    (folder / 'package.json').write_text('{"scripts":{"build":"tsc --noEmit && vite build"}}', encoding='utf-8')
    session = {'id': 'build-fixture', 'project_root': str(folder), 'cwd': '.', 'status': 'completed',
        'created_at': time.time() + .05, 'exit_code': 0, 'argv': argv}
    service.command_sessions = SimpleNamespace(status=lambda _: dict(session))
    result = manager.check({'id': record['id'], 'expected_revision': 1, 'gate': 'build', 'status': 'passed',
        'source': 'session', 'reference': session['id']})
    assert result['gates']['build']['status'] == 'passed'
    assert result['gates']['build']['evidence'][0]['argv'] == argv


@pytest.mark.parametrize('argv', [['pnpm', 'test'], ['pnpm', 'run', 'test'], ['yarn', 'test'],
                                  ['yarn', 'run', 'test'], ['bun', 'test'], ['bun', 'run', 'test']])
def test_functional_session_accepts_project_package_manager_tests(builder, argv):
    manager, record, folder, service = builder
    session = {'id': 'test-fixture', 'project_root': str(folder), 'cwd': '.', 'status': 'completed',
        'created_at': time.time() + .05, 'exit_code': 0, 'argv': argv}
    service.command_sessions = SimpleNamespace(status=lambda _: dict(session))
    result = manager.check({'id': record['id'], 'expected_revision': 1, 'gate': 'functional', 'status': 'passed',
        'source': 'session', 'reference': session['id']})
    assert result['gates']['functional']['status'] == 'passed'


def test_python_compile_cannot_pass_node_build_unless_brief_configures_exact_command(builder):
    manager, record, folder, service = builder
    (folder / 'package.json').write_text('{"scripts":{"build":"vite build"}}', encoding='utf-8')
    python = ['python', '-m', 'compileall', '-q', '.']
    session = {'id': 'build-fixture', 'project_root': str(folder), 'cwd': '.', 'status': 'completed',
        'created_at': time.time() + .05, 'exit_code': 0, 'argv': python}
    service.command_sessions = SimpleNamespace(status=lambda _: dict(session))
    data = {'id': record['id'], 'expected_revision': 1, 'gate': 'build', 'status': 'passed',
        'source': 'session', 'reference': session['id']}
    for argv in (python, ['npx', 'vite', 'build'], ['tsc', '--showConfig'], ['vite', 'build', '--help']):
        session['argv'] = argv
        with pytest.raises(ValueError, match='recognized'):
            manager.check(data)
    assert manager.get(record['id'])['gates']['build']['status'] == 'pending'
    # A template label cannot hide a subsequently added web build pipeline.
    manager.store.save_entity('builders', {**manager.store.entity('builders', record['id']), 'template': 'fastapi'})
    session['argv'] = python
    with pytest.raises(ValueError, match='recognized'):
        manager.check(data)
    configured = manager.save({**record, 'expected_revision': 1, 'check_commands': {'build': python}})
    data['expected_revision'] = configured['revision']
    session['created_at'] = time.time() + .05
    session['argv'] = ['pnpm', 'build']
    with pytest.raises(ValueError, match='brief-configured'):
        manager.check(data)
    session['argv'] = python
    assert manager.check(data)['gates']['build']['status'] == 'passed'


def test_document_extraction_formats_hash_verification_and_bounds(builder):
    manager, record, folder, _ = builder
    (folder / 'report.csv').write_text('Name,Count\nIdea,2\n', encoding='utf-8')
    assert 'Idea' in manager.documents.extract({'project_id': record['project_id'], 'path': 'report.csv'})['text']
    with zipfile.ZipFile(folder / 'report.docx', 'w') as package:
        package.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fixture report</w:t></w:r></w:p></w:body></w:document>')
    assert 'Fixture report' in extract_document(folder / 'report.docx')['text']
    from openpyxl import Workbook
    book = Workbook()
    book.active.append(['Name', 'Count'])
    book.active.append(['Idea', '=1+1'])
    book.save(folder / 'report.xlsx')
    extracted = extract_document(folder / 'report.xlsx')
    assert '=1+1' in extracted['text'] and extracted['sheets'] == ['Sheet']
    from pypdf import PdfWriter
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.write(folder / 'report.pdf')
    assert extract_document(folder / 'report.pdf')['pages'] == 1
    verified = manager.documents.verify({'builder_id': record['id'], 'project_id': record['project_id'], 'path': 'report.docx'})
    assert verified['status'] == 'structure_verified' and verified['visual_verified'] is False
    assert manager.store.read_artifact(verified['evidence'])['total_characters'] > 0
    with pytest.raises(ValueError, match='SHA-256'):
        manager.documents.verify({'project_id': record['project_id'], 'path': 'report.csv', 'expected_sha256': 'incorrect'})
    with pytest.raises(ValueError, match='offset'):
        extract_document(folder / 'report.csv', -1)
    with pytest.raises(ValueError):
        manager.documents.extract({'project_id': record['project_id'], 'path': '../escape.txt'})


def test_uploaded_document_tampering_and_invalid_base64_are_rejected(builder):
    manager, _, _, _ = builder
    with pytest.raises(ValueError, match='base64'):
        manager.documents.upload({'name': 'notes.txt', 'content_base64': 'not base64!'})
    item = manager.documents.upload({'name': 'notes.txt', 'content_base64': base64.b64encode(b'Original').decode()})
    path = manager.store.home / 'attachments/documents' / item['filename']
    path.write_bytes(b'Changed')
    with pytest.raises(ValueError, match='changed'):
        manager.documents.extract({'document_id': item['id']})


@pytest.mark.parametrize('suffix', ['txt', 'md', 'csv', 'tsv', 'xlsx', 'docx', 'pdf'])
def test_exports_generate_real_files_verify_hash_and_download_without_overwrite(builder, suffix):
    manager, record, folder, _ = builder
    result = manager.documents.create({'project_id': record['project_id'], 'builder_id': record['id'],
        'path': 'exports/report.' + suffix, 'title': 'Fixture report', 'text': 'Verified export contents.',
        'rows': [['Name', 'Value'], ['Fixture', 2]], 'expected_sha256': 'missing'})
    file = result['file']
    assert file['backup_id'] and file['sha256'] == hashlib.sha256((folder / file['path']).read_bytes()).hexdigest()
    assert result['verification']['visual_verified'] is False
    download = manager.documents.download({'id': result['verification']['id']})
    assert hashlib.sha256(base64.b64decode(download['content_base64'])).hexdigest() == file['sha256']
    extracted = manager.documents.extract({'project_id': record['project_id'], 'path': file['path']})
    assert 'Fixture' in extracted['text'] if suffix in ('csv', 'tsv', 'xlsx') else 'Verified export contents.' in extracted['text']
    with pytest.raises(ValueError, match='changed'):
        manager.documents.create({'project_id': record['project_id'], 'path': file['path'], 'text': 'Replacement', 'rows': []})
    (folder / file['path']).write_bytes(b'Changed file')
    with pytest.raises(ValueError, match='changed'):
        manager.documents.download({'id': result['verification']['id']})


def test_vite_typescript_template_builds_with_existing_local_toolchain(tmp_path):
    import os
    import shutil
    import subprocess
    node = shutil.which('node')
    modules = Path('frontend/node_modules').resolve()
    if not node or not modules.is_dir():
        pytest.skip('Installed frontend toolchain is required.')
    for name, content in template_files('vite', 'Fixture TS app').items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    # Copy the exact installed toolchain into the isolated fixture. No network,
    # install scripts, personal project state or coordinator service is used.
    shutil.copytree(modules, tmp_path / 'node_modules', dirs_exist_ok=True)
    options = {'cwd': tmp_path, 'capture_output': True, 'text': True, 'timeout': 30,
               'creationflags': 0x08000000 if os.name == 'nt' else 0}
    check = subprocess.run([node, str(tmp_path / 'node_modules/typescript/bin/tsc'), '--noEmit'], **options)
    assert check.returncode == 0, check.stdout + check.stderr
    build = subprocess.run([node, str(tmp_path / 'node_modules/vite/bin/vite.js'), 'build'], **options)
    assert build.returncode == 0, build.stdout + build.stderr
    assert (tmp_path / 'dist/index.html').is_file()


def test_preview_extra_actions_validate_run_snapshot_and_bounds(tmp_path):
    browser = NativeBrowser(tmp_path, view_factory=FakePage)
    try:
        browser.dispatch('open', {'url': 'http://127.0.0.1:1234/'})
        snapshot = browser.execute('browser_inspect', {}, {'run_id': 'first'})
        args = {'snapshot_id': snapshot['snapshot_id'], 'selector': snapshot['targets'][0]['selector']}
        assert browser.execute('browser_key', {**args, 'key': 'F5'}, {'run_id': 'first'})['not_executed']
        assert browser.execute('browser_select', {**args, 'value': 'x'}, {'run_id': 'other'})['not_executed']
        assert browser.execute('browser_scroll', {**args, 'y': 1501}, {'run_id': 'first'})['not_executed']
        assert browser.execute('browser_wait', {**args, 'text': 'Ready', 'timeout_ms': 0}, {'run_id': 'first'})['not_executed']
    finally:
        browser.shutdown()


def test_fastapi_sqlite_template_has_working_crud_and_no_path_routes(tmp_path):
    files = template_files('fastapi', 'Fixture')
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding='utf-8')
    namespace = {'__file__': str(tmp_path / 'app.py')}
    exec(compile(files['app.py'], str(tmp_path / 'app.py'), 'exec'), namespace)
    from fastapi.testclient import TestClient
    client = TestClient(namespace['app'], base_url='http://127.0.0.1')
    assert client.get('/').status_code == 200
    item = client.post('/api/items', json={'text': 'A fixture idea'})
    assert item.status_code == 201
    assert client.get('/api/items').json()[0]['text'] == 'A fixture idea'
    assert client.delete('/api/items/' + str(item.json()['id'])).status_code == 204
    assert client.get('/api/items').json() == []
    assert client.post('/api/items', json={'text': ' '}).status_code == 422


def test_preview_browser_has_separate_guarded_state_and_artifacts(tmp_path):
    allowed = lambda value: value == 'about:blank' or value.startswith('http://127.0.0.1:1234/')
    browser = NativeBrowser(tmp_path, view_factory=FakePage, panel_id='forge-preview-panel',
                            profile_name='native-preview-profile', navigation_guard=allowed, diagnostics=True)
    try:
        with pytest.raises(ValueError, match='loopback'):
            browser.dispatch('open', {'url': 'https://example.org'})
        browser.dispatch('open', {'url': 'http://127.0.0.1:1234/'})
        snapshot = browser.execute('browser_inspect', {}, {'run_id': 'first'})
        result = browser.execute('browser_click', {'snapshot_id': snapshot['snapshot_id'], 'selector': snapshot['targets'][0]['selector']}, {'run_id': 'other'})
        assert result['not_executed']
        artifact = browser.execute('browser_screenshot', {})
        assert 'previews' in artifact['artifact']
    finally:
        browser.shutdown()


def test_builder_rendered_browser_and_real_static_preview(builder, tmp_path):
    """Render the production UI with isolated APIs and a fresh headless profile."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import os
    import threading
    playwright = pytest.importorskip('playwright.sync_api')
    executable = os.environ.get('FORGE_TEST_BROWSER') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(executable).is_file() or not Path('frontend/dist/index.html').is_file():
        pytest.skip('Production frontend and a test browser executable are required.')
    manager, record, folder, _ = builder
    from forge_drafts import DraftManager
    drafts = DraftManager(manager.store)
    project = manager.store.get_project(record['project_id'])
    manager.template({'id': record['id'], 'expected_revision': 1, 'template': 'static', 'directory': 'site'})
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(Path('frontend/dist').resolve())))
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    errors = []
    fixture_chat = {'id': 'chat-fixture', 'project_id': project['id'], 'title': 'Repair the app', 'model': 'fixture',
                    'messages': [{'role': 'assistant', 'content': 'The app is implemented. Two checks still need repair.'}]}
    fixture_run = {'id': 'run-fixture', 'chat_id': fixture_chat['id'], 'project_id': project['id'], 'status': 'paused',
        'workflow_stage': 'repair', 'progress_summary': {'activity': 'Repair failed checks', 'phase': 'repair',
            'verified': 1, 'total': 3, 'waiting_reason': 'Two checks need current passing evidence.',
            'next_action': 'Repair keyboard focus, then rerun checks.'},
        'planner_assignment': {'state': 'consumed', 'scope_revision': 1, 'run_id': 'planner-fixture',
            'requested_at': '2026-10-08T08:00:00Z', 'consumed_at': '2026-10-08T08:00:02Z', 'guidance_hash': 'fixture-hash'},
        'review': {'status': 'needs_changes', 'summary': 'Repair keyboard focus.'}}
    try:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1050})
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            def respond(route):
                action = route.request.url.rsplit('/', 1)[-1]
                data = route.request.post_data_json or {}
                if action in ('draft_get', 'draft_save', 'draft_clear'):
                    value = drafts.dispatch(action, data)
                elif action.startswith(('builder_', 'preview_', 'document_')) or action == 'artifact_verify':
                    value = manager.dispatch(action, data)
                elif action == 'bootstrap':
                    value = {'settings': {'model': 'fixture', 'theme': 'dark'}, 'projects': [project],
                             'chats': [fixture_chat], 'runs': [fixture_run], 'capabilities': {}}
                elif action == 'get_chat':
                    value = fixture_chat
                else:
                    value = {'first_run': False, 'projects': [project], 'chats': [], 'runs': [], 'models': [], 'goals': [], 'documents': [], 'skills': [], 'spaces': [], 'actions': []}
                route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
            page.route('**/api/v1/*', respond)
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.get_by_text('Repair failed checks', exact=True).wait_for()
            page.wait_for_function("()=>!document.querySelector('.task-status button')?.disabled")
            for theme in ('light', 'dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                for width in (1440, 768, 390):
                    page.set_viewport_size({'width': width, 'height': 1050})
                    if width < 800:
                        page.get_by_role('button', name='Open sidebar', exact=True).wait_for()
                    message = page.get_by_role('textbox', name='Message Forge', exact=True)
                    message.focus()
                    assert message.evaluate('(el)=>el===document.activeElement')
                    assert page.get_by_role('button', name='Resume', exact=True).is_visible()
                    assert page.get_by_role('button', name='Resume', exact=True).is_enabled()
                    assert page.get_by_text('OpenRouter: Guidance supplied to local execution', exact=True).is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(tmp_path / f'workspace-status-{theme}-{width}.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1050})
            page.get_by_role('button', name='Open sidebar', exact=True).click()
            try:
                page.get_by_role('navigation', name='Workspace navigation').get_by_role('button', name='Builder', exact=True).click()
            except Exception:
                page.screenshot(path=str(tmp_path / 'builder-error.png'), full_page=True)
                print('Builder page errors:', errors, 'UI:', page.locator('body').inner_text()[:2500])
                raise
            page.get_by_label('Objective').wait_for()
            # The production frontend saves through the real bounded validator,
            # restores across reload, and never commits an editable draft as a
            # brief revision or completion receipt.
            draft_scope = {'scope_kind': 'builder', 'project_id': project['id'], 'builder_id': record['id']}
            page.get_by_label('Objective').fill('Unsaved browser draft, preserved after restart')
            page.get_by_text('Draft saved locally', exact=True).wait_for()
            saved_draft = drafts.get(draft_scope)
            assert saved_draft['content']['base_revision'] == 1
            assert 'base_revision' not in saved_draft['content']['brief']
            assert saved_draft['content']['brief']['objective'] == 'Unsaved browser draft, preserved after restart'
            assert manager.store.entity('builders', record['id'])['objective'] == record['objective']
            page.reload()
            page.get_by_role('navigation', name='Workspace navigation').get_by_role('button', name='Builder', exact=True).click()
            page.get_by_label('Objective').wait_for()
            page.wait_for_function("()=>document.querySelector('.builder-brief textarea')?.value==='Unsaved browser draft, preserved after restart'")
            page.get_by_role('button', name='Projects', exact=True).click()
            page.get_by_role('button', name='Stay', exact=True).click()
            assert page.get_by_label('Objective').input_value() == 'Unsaved browser draft, preserved after restart'
            page.get_by_role('button', name='Projects', exact=True).click()
            page.get_by_role('button', name='Discard & Leave', exact=True).click()
            page.get_by_role('heading', name='Projects', exact=True).wait_for()
            page.get_by_role('navigation', name='Workspace navigation').get_by_role('button', name='Builder', exact=True).click()
            page.get_by_label('Objective').wait_for()
            page.wait_for_function("(objective)=>document.querySelector('.builder-brief textarea')?.value===objective", arg=record['objective'])
            assert manager.store.entity('builders', record['id'])['revision'] == 1
            page.screenshot(path=str(tmp_path / 'builder-brief.png'), full_page=True)
            page.get_by_role('button', name='Preview', exact=True).click()
            page.get_by_role('button', name='Start preview', exact=True).click()
            frame = page.frame_locator('iframe[title="Local application preview"]')
            try:
                frame.get_by_label('New idea', exact=True).fill('Rendered browser fixture')
            except Exception:
                page.screenshot(path=str(tmp_path / 'builder-preview-error.png'), full_page=True)
                print('Preview errors:', errors, 'UI:', page.locator('body').inner_text()[:3500],
                      'Frames:', [(f.url, f.locator('body').inner_text()[:1000]) for f in page.frames])
                raise
            frame.get_by_role('button', name='Add idea', exact=True).click()
            assert frame.get_by_text('Rendered browser fixture', exact=True).count() == 1
            frame.get_by_role('button', name='Remove Rendered browser fixture', exact=True).click()
            assert frame.get_by_text('Rendered browser fixture', exact=True).count() == 0
            for theme in ('light', 'dark'):
                page.evaluate("(theme)=>document.documentElement.dataset.theme=theme", theme)
                for width in (1440, 768, 390):
                    page.set_viewport_size({'width': width, 'height': 1050})
                    if width < 800:
                        page.get_by_role('button', name='Open sidebar', exact=True).wait_for()
                    text = f'Keyboard fixture {width}'
                    field = frame.get_by_label('New idea', exact=True)
                    field.fill(text)
                    field.press('Enter')
                    assert frame.get_by_text(text, exact=True).count() == 1
                    remove = frame.get_by_role('button', name=f'Remove {text}', exact=True)
                    remove.focus()
                    remove.press('Space')
                    assert frame.get_by_text(text, exact=True).count() == 0
                    field.fill(f'Primary action {width}')
                    primary = frame.get_by_role('button', name='Add idea', exact=True)
                    primary.focus()
                    primary.press('Enter')
                    assert frame.get_by_text(f'Primary action {width}', exact=True).count() == 1
                    frame.get_by_role('button', name=f'Remove Primary action {width}', exact=True).click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'Workspace overflow at {width}px'
                    assert page.locator('.builder-page').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'), f'Builder overflow at {width}px'
                    assert frame.locator('html').evaluate('(el)=>el.scrollWidth<=innerWidth+1'), f'App overflow at {width}px'
                    page.screenshot(path=str(tmp_path / f'builder-preview-{theme}-{width}.png'), full_page=True)
            page.screenshot(path=str(tmp_path / 'builder-preview.png'), full_page=True)
            assert page.locator('.builder-page').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_production_composer_preserves_prebootstrap_text_with_real_draft_api(builder):
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import os
    import threading
    from forge_drafts import DraftManager
    playwright = pytest.importorskip('playwright.sync_api')
    executable = os.environ.get('FORGE_TEST_BROWSER') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(executable).is_file() or not Path('frontend/dist/index.html').is_file():
        pytest.skip('Production frontend and a test browser executable are required.')
    manager, _, _, _ = builder
    drafts = DraftManager(manager.store)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(Path('frontend/dist').resolve())))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pending, hold_first = [], [True]
    try:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1050})
            page.set_default_timeout(8000)
            bootstrap = {'settings': {'model': 'fixture', 'theme': 'light'}, 'projects': [], 'chats': [], 'runs': [], 'capabilities': {}}
            def respond(route):
                action = route.request.url.rsplit('/', 1)[-1]
                data = route.request.post_data_json or {}
                if action == 'bootstrap' and hold_first[0]:
                    hold_first[0] = False
                    pending.append(route)
                    return
                if action in ('draft_get', 'draft_save', 'draft_clear'):
                    value = drafts.dispatch(action, data)
                elif action == 'bootstrap':
                    value = bootstrap
                else:
                    value = {'first_run': False, 'projects': [], 'chats': [], 'runs': [], 'models': [], 'questions': [], 'skills': [], 'spaces': [], 'schedules': []}
                route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
            page.route('**/api/v1/*', respond)
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            message = page.get_by_role('textbox', name='Message Forge', exact=True)
            message.fill('Local text typed before connection finished')
            page.wait_for_timeout(350)
            assert pending, 'Bootstrap must be pending for the race fixture'
            pending[0].fulfill(status=200, content_type='application/json', body=json.dumps(bootstrap))
            page.get_by_text('Draft saved locally', exact=True).wait_for()
            assert message.input_value() == 'Local text typed before connection finished'
            assert drafts.get({'scope_kind': 'new_chat'})['content']['text'] == message.input_value()
            page.reload()
            page.wait_for_function("()=>document.querySelector('.composer textarea')?.value==='Local text typed before connection finished'")
            assert message.input_value() == 'Local text typed before connection finished'
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_production_inline_resume_uses_real_context_and_unknown_action_state(tmp_path):
    """The compiled composer must distinguish a context pause from unknown effects."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import os
    import threading
    from test_forge_core import service
    playwright = pytest.importorskip('playwright.sync_api')
    executable = os.environ.get('FORGE_TEST_BROWSER') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(executable).is_file() or not Path('frontend/dist/index.html').is_file():
        pytest.skip('Production frontend and a test browser executable are required.')
    svc = service(tmp_path)
    launches = []
    svc.jobs._launch = lambda run: launches.append(run['id'])
    svc.store.update_settings({'memory_enabled': False, 'memory_suggestions': False})
    run = svc.jobs.start({'text': 'Repair the isolated page.'})
    launches.clear()
    reason = 'A continuity checkpoint cannot fit this context. Increase Context or disable tools, then Resume. Original records remain saved.'
    svc.store.update_run(run['id'], status='paused', recovery=reason)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(Path('frontend/dist').resolve())))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with playwright.sync_playwright() as engine:
            browser = engine.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport={'width': 1200, 'height': 950})
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            real_actions = {'bootstrap', 'get_chat', 'runs', 'poll', 'unknown_actions', 'resolve_action', 'resume', 'settings',
                            'draft_get', 'draft_save', 'draft_clear'}
            def respond(route):
                action = route.request.url.rsplit('/', 1)[-1]
                data = route.request.post_data_json or {}
                try:
                    value = svc.dispatch(action, data) if action in real_actions else {
                        'first_run': False, 'projects': [], 'chats': [], 'runs': [], 'models': [], 'questions': [],
                        'skills': [], 'spaces': [], 'schedules': [], 'documents': []}
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
                except ValueError as exc:
                    route.fulfill(status=400, content_type='application/json', body=json.dumps({'error': str(exc)}))
            page.route('**/api/v1/*', respond)
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            status = page.get_by_role('region', name='Task status')
            status.get_by_text(reason, exact=True).first.wait_for()
            resume = status.get_by_role('button', name='Resume', exact=True)
            page.wait_for_function("()=>!document.querySelector('.task-status button')?.disabled")
            page.screenshot(path=str(tmp_path / 'inline-context-recovery.png'), full_page=True)
            # Change the actual saved context through the API, then use the real
            # coordinator Resume path. Inference is disabled by the launch stub.
            assert page.evaluate("async()=>{const r=await fetch('/api/v1/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({context:16384})});return r.ok;}")
            resume.click()
            page.wait_for_function("()=>document.querySelector('.task-status .badge')?.textContent==='queued'")
            assert launches == [run['id']]
            assert svc.store.run(run['id'])['settings']['context'] == 16384
            assert not svc.core.requests
            # An actual unknown invocation blocks both UI and coordinator.
            identifier = run['id'] + ':unknown-fixture'
            svc.store.invocation(identifier, run['id'], 'write_file', {'path': 'page.txt', 'content': 'Fixture'})
            svc.store.invocation_state(identifier, 'outcome_unknown', {'outcome_unknown': True})
            svc.store.update_run(run['id'], status='paused', recovery='Inspect the interrupted write before resuming.')
            with pytest.raises(ValueError, match='Inspect outcome-unknown'):
                svc.dispatch('resume', {'id': run['id']})
            page.reload()
            status.get_by_role('button', name='Inspect write_file', exact=True).wait_for()
            assert resume.is_disabled()
            status.get_by_role('button', name='Inspect write_file', exact=True).click()
            page.get_by_role('textbox', name='Inspection evidence').fill('Inspected the disposable fixture; the write did not execute.')
            page.screenshot(path=str(tmp_path / 'inline-unknown-action-inspection.png'), full_page=True)
            page.get_by_role('button', name='Verified not executed', exact=True).click()
            page.wait_for_function("()=>!document.querySelector('.task-status button')?.disabled")
            assert not svc.store.unknown_actions(run['id'])
            with svc.store._connection() as db:
                saved = json.loads(db.execute('SELECT result FROM invocations WHERE id=?', (identifier,)).fetchone()[0])
            assert saved['replay'] is False and saved['outcome'] == 'not_executed'
            resume.click()
            page.wait_for_function("()=>document.querySelector('.task-status .badge')?.textContent==='queued'")
            assert launches == [run['id'], run['id']]
            assert not svc.core.requests
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        svc.shutdown()
