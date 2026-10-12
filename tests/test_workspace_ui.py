"""Cosmetics and journal projections never alter project files or execute work."""
import json
from uuid import uuid4

import pytest

from forge_service import ForgeService
from forge_store import ForgeStore, encode
from test_forge_workspaces import ScriptedEngine


@pytest.fixture
def service(tmp_path):
    svc = ForgeService(core=ScriptedEngine(), data_dir=tmp_path / 'forge')
    svc.store.update_settings({'model': 'fixture', 'permission_profile': 'full_access'})
    svc.jobs._launch = lambda run: None
    yield svc
    svc.shutdown()


def start(service, **kwargs):
    return service.jobs.start({'text': 'Synthetic workspace task', **kwargs})


def test_appearance_survives_restart_and_never_changes_the_project(service, tmp_path):
    folder = tmp_path / 'project'
    folder.mkdir()
    (folder / 'source.txt').write_text('preserve source')
    project = service.create_project({'path': str(folder), 'name': 'Sample'})
    settings = service.dispatch('settings', {'ui_appearance': {'name': 'My studio', 'accent': 'sage'}})
    assert settings['ui_appearance'] == {'name': 'My studio', 'accent': 'sage', 'motion': 'system'}
    saved = service.dispatch('project_appearance_save', {'project_id': project['id'], 'appearance': {'icon': 'code', 'accent': 'blue'}})
    assert saved['appearance'] == {'icon': 'code', 'accent': 'blue'}
    reopened = ForgeStore(service.store.home)
    assert reopened.get_settings()['ui_appearance'] == settings['ui_appearance']
    assert service.dispatch('projects')['projects'][0]['appearance'] == saved['appearance']
    assert (folder / 'source.txt').read_text() == 'preserve source'
    service.dispatch('project_delete', {'id': project['id']})
    assert reopened.entities('project_appearance') == []
    assert folder.exists()


@pytest.mark.parametrize('appearance', [{'name': ''}, {'name': 'a' * 61}, {'name': 'line\nbreak'}, {'accent': 'neon'}, {'motion': 'spin'}, {'path': '/tmp'}])
def test_invalid_appearance_update_is_atomic(service, appearance):
    before = service.store.get_settings()
    with pytest.raises(ValueError):
        service.dispatch('settings', {'ui_appearance': appearance, 'temperature': .1})
    assert service.store.get_settings() == before


def test_native_layout_survives_changed_origin_and_keeps_browser_clients_separate(service, tmp_path):
    folder = tmp_path / 'sample'
    folder.mkdir()
    project = service.create_project({'path': str(folder), 'name': 'Sample'})
    saved = service.dispatch('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_open': False,
        'sidebar_width': 999, 'right_panel_width': 100, 'right_panel_tab': 'files',
        'collapsed_project_ids': [project['id'], project['id'], 'deleted-project']}})
    assert saved['sidebar_width'] == 360 and saved['right_panel_width'] == 280
    assert saved['collapsed_project_ids'] == [project['id']]
    from forge_workspace_ui import WorkspaceUI
    reopened = WorkspaceUI(ForgeStore(service.store.home))
    assert reopened.layout({'client_id': 'desktop'}) == saved
    assert reopened.layout({'client_id': 'browser-a'})['sidebar_open'] is True
    reopened.layout({'client_id': 'browser-a', 'layout': {'right_panel_open': True}}, save=True)
    assert reopened.layout({'client_id': 'browser-b'})['right_panel_open'] is False
    assert reopened.layout({'client_id': 'desktop'})['right_panel_open'] is False
    service.dispatch('project_delete', {'id': project['id']})
    assert reopened.layout({'client_id': 'desktop'})['collapsed_project_ids'] == []


def test_layout_partial_saves_merge_and_invalid_updates_leave_state_intact(service):
    service.dispatch('ui_layout_save', {'client_id': 'desktop', 'layout': {'sidebar_open': False}})
    service.dispatch('ui_layout_save', {'client_id': 'desktop', 'layout': {'right_panel_tab': 'context'}})
    before = service.dispatch('ui_layout_get', {'client_id': 'desktop'})
    assert before['sidebar_open'] is False and before['right_panel_tab'] == 'context'
    for update in ({'sidebar_open': 1}, {'sidebar_width': True}, {'right_panel_tab': 'bad'}, {'collapsed_project_ids': [None]}, {'unknown': 1}):
        with pytest.raises(ValueError):
            service.dispatch('ui_layout_save', {'client_id': 'desktop', 'layout': update})
        assert service.dispatch('ui_layout_get', {'client_id': 'desktop'}) == before


def test_chat_activity_is_independent_of_global_recent_runs_limit(service):
    original = start(service)
    service.store.update_run(original['id'], status='completed')
    service.store.event(original['id'], 'status', text='Read project files')
    service.store.add_message(original['chat_id'], 'assistant', 'Saved final answer', interaction_run_id=original['id'])
    other_chat = service.store.create_chat(title='Other history')
    other_request = service.store.add_message(other_chat['id'], 'user', 'Synthetic newer task')
    with service.store._connection(transaction='write') as db:
        template = service.store.run(original['id'])
        for _ in range(210):
            identifier = uuid4().hex
            payload = {**template, 'id': identifier, 'chat_id': other_chat['id'], 'request_message_id': other_request['id'], 'created_at': '2099-01-01T00:00:00+00:00'}
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (identifier, other_chat['id'], None, None, 'completed', encode(payload), payload['created_at'], payload['created_at']))
    assert original['id'] not in {r['id'] for r in service.bootstrap()['runs']}
    chat = service.dispatch('get_chat', {'id': original['chat_id'], 'limit': 1})
    assert len(chat['activity_groups']) == 1
    assert chat['activity_groups'][0]['run_id'] == original['id']
    assert chat['activity_groups'][0]['current_step'] == 'Completed'


def test_saved_tool_associations_and_identical_calls_remain_distinct(service):
    run = start(service)
    for index in range(2):
        identifier = uuid4().hex
        service.store.invocation(identifier, run['id'], 'read_file', {'path': 'same.txt'})
        service.store.invocation_state(identifier, 'completed', {'content': 'same'})
        service.store.tool_message(identifier, index, 'read_file', '{"content":"same"}')
        service.store.event(run['id'], 'tool', invocation_id=identifier, name='read_file', state='done', result={'content': 'same'})
    chat = service.dispatch('get_chat', {'id': run['chat_id']})
    tools = [m for m in chat['messages'] if m['role'] == 'tool']
    assert len({m['invocation_id'] for m in tools}) == 2
    assert {m['run_id'] for m in tools} == {run['id']}
    assert chat['activity_groups'][0]['tool_count'] == 2


def test_activity_pages_filter_tokens_and_private_fields_without_replay(service):
    run = start(service)
    service.store.event(run['id'], 'token', text='private transcript')
    service.store.event(run['id'], 'thinking', text='private reasoning')
    first = service.store.event(run['id'], 'tool', name='read_file', state='running', invocation_id='one', reasoning_details=[{'data': 'secret opaque state'}])
    second = service.store.event(run['id'], 'tool', name='read_file', state='done', invocation_id='one')
    service.store.event(run['id'], 'done')
    page = service.dispatch('run_activity', {'run_id': run['id'], 'limit': 1})
    assert page['next_cursor'] == first['seq'] and page['has_more']
    assert page['events'][0]['created_at']
    assert 'private' not in json.dumps(page) and 'reasoning_details' not in json.dumps(page)
    next_page = service.dispatch('run_activity', {'run_id': run['id'], 'after': page['next_cursor'], 'limit': 2})
    assert next_page['events'][0]['seq'] == second['seq'] and not next_page['has_more']
    assert service.core.requests == []


def test_helper_retries_project_one_logical_assignment_and_diagnostics_omit_content(service):
    parent = start(service)
    initial = start(service, parent_id=parent['id'])
    service.store.update_run(initial['id'], status='paused', assignment_id=initial['id'], attempt=1, failure={'category': 'model_refusal'})
    retry = start(service, parent_id=parent['id'])
    service.store.update_run(retry['id'], status='completed', assignment_id=initial['id'], attempt=2, retry_count=1,
        returned_model_last='provider/free-model', response_provider='fixture-provider', last_finish_reason='stop',
        request='Never export this prompt', recovery='Never export this raw error', failure=None)
    group = service.dispatch('get_chat', {'id': parent['chat_id']})['activity_groups'][0]
    assert group['helper_count'] == 1 and group['children'][0]['run_id'] == retry['id']
    assert group['children'][0]['attempts'][0]['run_id'] == initial['id']
    diagnostic = service.dispatch('run_diagnostics_export', {'run_id': retry['id']})
    assert diagnostic['retry_count'] == 1 and diagnostic['finish_reason'] == 'stop'
    assert 'Never export' not in json.dumps(diagnostic)


@pytest.mark.parametrize('data', [{'after': -1}, {'after': True}, {'limit': 201}, {'limit': 0}])
def test_activity_cursor_validation(service, data):
    run = start(service)
    with pytest.raises(ValueError):
        service.dispatch('run_activity', {'run_id': run['id'], **data})


@pytest.mark.parametrize('outcome, expected', [('outcome_unknown', 'Inspect interrupted action'), ('prepared', 'Preparing run command'), ('failed', 'Failed run command')])
def test_activity_never_calls_an_uncertain_action_finished(service, outcome, expected):
    run = start(service)
    service.store.invocation('synthetic-action', run['id'], 'run_command', {'argv': ['fixture']})
    service.store.invocation_state('synthetic-action', outcome)
    group = service.dispatch('get_chat', {'id': run['chat_id']})['activity_groups'][0]
    assert group['current_step'] == expected


def test_terminal_activity_and_failure_projection_are_truthful_and_allowlisted(service):
    run = start(service)
    service.store.event(run['id'], 'status', text='Working…')
    service.store.update_run(run['id'], status='completed', failure={'category': 'refusal', 'provider_id': 'openrouter', 'http_status': 400, 'raw_error': 'Never expose this', 'reasoning_details': [{'data': 'opaque secret'}]})
    group = service.dispatch('get_chat', {'id': run['chat_id']})['activity_groups'][0]
    assert group['current_step'] == 'Completed'
    assert group['failure'] == {'category': 'refusal', 'provider_id': 'openrouter', 'http_status': 400}
    assert 'Never expose' not in json.dumps(group) and 'opaque secret' not in json.dumps(group)
