"""Goal progress uses existing identities; every transport and workspace is synthetic."""
import builtins
from copy import deepcopy
import json
import threading
import time

import httpx
from jsonschema import Draft202012Validator
import psutil
import pytest

from core import Core
from forge_inference import CompatibleProvider
from forge_openrouter import OpenRouterProvider, OPENROUTER_URL
from forge_runs import WORKFLOW
from forge_service import ForgeService
from test_forge_core import Engine, call
from test_forge_ollama_auth import FakeVault, install_http
from test_forge_openrouter import model
from tool_routing import validate_arguments, wire_schemas


def schema(name='goal_update'):
    return next(tool for tool in WORKFLOW if tool['function']['name'] == name)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import forge_credentials
    monkeypatch.setattr(forge_credentials, 'CredentialVault', FakeVault)
    monkeypatch.setattr(psutil, 'cpu_count', lambda logical=True: 4)
    def denied(*args, **kwargs):
        pytest.fail('A goal contract fixture attempted real HTTP')
    async def async_denied(*args, **kwargs):
        pytest.fail('A goal contract fixture attempted real HTTP')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', async_denied)


@pytest.fixture
def goal_run(tmp_path, monkeypatch):
    svc = ForgeService(core=Engine(), data_dir=tmp_path / 'workspace')
    svc.store.update_settings({'provider_id': 'ollama', 'model': 'fixture', 'context': 8192,
        'permission_profile': 'full_access', 'memory_enabled': False, 'memory_suggestions': False,
        'auto_delegate': False, 'goal_review_enabled': False, 'web': False})
    monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
    goal = svc.goal_create({'text': 'Preserve the two accepted requirements.', 'tasks': [
        {'id': 'first', 'text': 'Inspect the accepted first requirement.', 'status': 'pending'},
        {'id': 'second', 'text': 'Verify the accepted second requirement.', 'status': 'pending'}]})
    created = svc.jobs.start({'text': goal['request'], 'goal_id': goal['id'], 'mode': 'goal'})
    run = svc.store.run(created['id'])
    try:
        yield svc, svc.store.goal(goal['id']), run
    finally:
        svc.shutdown()


def validate_with(mode, arguments, monkeypatch):
    tool = schema()
    if mode == 'draft2020':
        try:
            Draft202012Validator(tool['function']['parameters']).validate(arguments)
        except Exception:
            return False
        return True
    if mode == 'fallback':
        original = builtins.__import__
        def without_jsonschema(name, *args, **kwargs):
            if name == 'jsonschema' or name.startswith('jsonschema.'):
                raise ImportError('Synthetic optional-validator absence')
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', without_jsonschema)
    return validate_arguments(tool, arguments) is None


@pytest.mark.parametrize('mode', ['draft2020', 'coordinator', 'fallback'])
@pytest.mark.parametrize('identity', ['id', 'text', 'both'])
def test_advertised_schema_accepts_id_and_exact_text_forms(mode, identity, monkeypatch):
    task = {'status': 'in_progress', 'evidence': ['Synthetic evidence']}
    if identity in ('id', 'both'): task['id'] = 'existing-task'
    if identity in ('text', 'both'): task['text'] = 'Exact saved requirement.'
    assert validate_with(mode, {'tasks': [task], 'checkpoint': 'Inspected.', 'next_action': 'Verify.'}, monkeypatch)


@pytest.mark.parametrize('mode', ['draft2020', 'coordinator', 'fallback'])
@pytest.mark.parametrize('task', [
    {'status': 'in_progress'}, {'id': 'existing-task'}, {'id': 3, 'status': 'in_progress'},
    {'text': False, 'status': 'pending'}, {'id': 'existing-task', 'status': 'invented'},
    {'id': 'existing-task', 'status': 'completed', 'evidence': [False]},
])
def test_advertised_identity_status_and_types_remain_required(mode, task, monkeypatch):
    assert not validate_with(mode, {'tasks': [task], 'checkpoint': 'Inspected.', 'next_action': 'Verify.'}, monkeypatch)


def test_checkpoint_only_and_legacy_task_fields_remain_compatible(monkeypatch):
    Draft202012Validator.check_schema(schema()['function']['parameters'])
    assert validate_arguments(schema(), {'checkpoint': 'Saved.', 'next_action': 'Continue.'}) is None
    legacy = {'id': 'existing-task', 'text': 'Exact saved requirement.', 'status': 'pending',
              'requirement_id': 'requirement', 'origin': 'initial', 'note': 'Synthetic note'}
    for mode in ('draft2020', 'coordinator', 'fallback'):
        assert validate_with(mode, {'tasks': [legacy], 'checkpoint': 'Saved.', 'next_action': 'Continue.'}, monkeypatch)


@pytest.mark.parametrize('identity', ['id', 'text', 'both'])
def test_actual_executor_updates_progress_without_rewriting_requirements(goal_run, identity):
    svc, goal, run = goal_run
    observed = svc.jobs.registry.execute(run, 'goal_read', {}, threading.Event())
    original = observed['tasks'][0]
    task = {'status': 'in_progress', 'evidence': ['Synthetic inspection result.']}
    if identity in ('id', 'both'): task['id'] = original['id']
    if identity in ('text', 'both'): task['text'] = original['text']
    args = {'tasks': [task], 'checkpoint': 'First requirement inspected.', 'next_action': 'Verify it.'}
    assert validate_arguments(schema(), args) is None
    saved = svc.jobs.registry.execute(run, 'goal_update', args, threading.Event())
    assert saved['tasks'][0]['status'] == 'in_progress'
    assert saved['tasks'][0]['evidence'] == task['evidence']
    assert saved['tasks'][1] == observed['tasks'][1]
    assert [(item['id'], item['text'], item.get('requirement_id'), item.get('origin')) for item in saved['tasks']] == [
        (item['id'], item['text'], item.get('requirement_id'), item.get('origin')) for item in observed['tasks']]
    assert saved['scope_revision'] == goal['scope_revision'] and saved['requirement_history'] == goal['requirement_history']
    assert saved['request'] == goal['request']


@pytest.mark.parametrize('bad', ['new_id', 'foreign_id', 'changed_text', 'changed_requirement', 'changed_origin', 'duplicate_id', 'ambiguous_text'])
def test_actual_executor_rejects_new_foreign_changed_or_duplicate_identity(goal_run, bad):
    svc, goal, run = goal_run
    original = goal['tasks'][0]
    update = {'id': original['id'], 'status': 'completed'}
    if bad == 'new_id': update['id'] = 'new-task'
    elif bad == 'foreign_id':
        foreign = svc.goal_create({'text': 'A separate accepted goal.', 'tasks': [{'id': 'foreign', 'text': 'Foreign requirement.'}]})
        update['id'] = foreign['tasks'][0]['id']
    elif bad == 'changed_text': update['text'] = 'A weaker replacement.'
    elif bad == 'changed_requirement': update['requirement_id'] = 'replacement-requirement'
    elif bad == 'changed_origin': update['origin'] = 'review'
    elif bad == 'ambiguous_text':
        goal = svc.store.save_goal({**goal, 'tasks': [{**item, 'text': 'Same exact requirement.'} for item in goal['tasks']]})
        update = {'text': 'Same exact requirement.', 'status': 'completed'}
    updates = [update, deepcopy(update)] if bad == 'duplicate_id' else [update]
    args = {'tasks': updates, 'checkpoint': 'Claimed progress.', 'next_action': 'Report.'}
    before = svc.store.goal(goal['id'])
    assert validate_arguments(schema(), args) is None  # Existing identity is a coordinator fact, not a schema enum.
    with pytest.raises(ValueError): svc.jobs.registry.execute(run, 'goal_update', args, threading.Event())
    assert svc.store.goal(goal['id']) == before
    if bad == 'foreign_id': assert svc.store.goal(foreign['id']) == foreign


def test_single_task_update_uses_read_revision_and_rejects_stale_revision(goal_run):
    svc, _, run = goal_run
    observed = svc.jobs.registry.execute(run, 'goal_read', {'task_id': 'first'}, threading.Event())
    args = {'task_id': observed['tasks'][0]['id'], 'expected_revision': observed['revision'], 'status': 'in_progress'}
    assert validate_arguments(schema('goal_task_update'), args) is None
    saved = svc.jobs.registry.execute(run, 'goal_task_update', args, threading.Event())
    assert saved['tasks'][0]['status'] == 'in_progress' and saved['revision'] > observed['revision']
    with pytest.raises(ValueError, match='changed|revision'):
        svc.jobs.registry.execute(run, 'goal_task_update', {**args, 'status': 'completed'}, threading.Event())
    assert svc.store.goal(saved['id']) == saved


@pytest.mark.parametrize('restriction', ['helper', 'readonly', 'plan'])
def test_coordinator_refuses_goal_bookkeeping_for_readonly_or_helper_context(goal_run, restriction):
    svc, goal, run = goal_run
    restricted = {**run, **({'parent_id': 'synthetic-parent'} if restriction == 'helper' else
                           {'readonly': True} if restriction == 'readonly' else {'mode': 'plan'})}
    tool = schema()
    assert svc.jobs.registry.permission(restricted, tool, {}) == 'deny'
    assert 'goal_update' not in {item['function']['name'] for item in svc.jobs.registry.schemas(restricted, ['tools'], all_tools=True)}
    args = {'tasks': [{'id': 'first', 'status': 'completed'}], 'checkpoint': 'Attempted.', 'next_action': 'Report.'}
    wrapped = svc.jobs._execute_call(restricted, {'cancel': threading.Event()}, call('goal_update', args), 0,
        {'goal_update': tool}, {'capabilities': ['tools']}, time.monotonic())
    assert wrapped['result']['not_executed'] is True and wrapped['result']['coordinator_refusal'] is True
    assert svc.store.goal(goal['id']) == goal


def test_model_status_update_cannot_satisfy_coordinator_verification(goal_run):
    svc, goal, run = goal_run
    run = svc.store.update_run(run['id'], execution_mode='guided')
    args = {'tasks': [{'id': item['id'], 'status': 'completed', 'evidence': ['Model assertion.']} for item in goal['tasks']],
            'checkpoint': 'Claimed complete.', 'next_action': 'Report.'}
    svc.jobs.registry.execute(run, 'goal_update', args, threading.Event())
    assert svc.jobs.workflow.enabled(run) and svc.jobs.workflow.completion_issues(run)
    saved = svc.store.goal(goal['id'])
    assert not saved.get('verified_task_ids') and saved['requirement_history'] == goal['requirement_history']


def test_legacy_goal_context_prefers_current_task_revision_without_new_section(goal_run):
    svc, _, run = goal_run
    run = svc.store.update_run(run['id'], execution_mode='legacy')
    messages = svc.jobs._context(run, [schema(), schema('goal_task_update')])
    packets = [message['content'] for message in messages if message['role'] == 'system' and message['content'].startswith('Goal checkpoint (saved task data):')]
    assert len(packets) == 1
    assert 'goal_task_update' in packets[0] and 'expected_revision' in packets[0] and 'goal_read' in packets[0]
    assert 'when available' in packets[0] and 'exact saved task text' in packets[0]
    assert 'accepted requirements cannot be added or replaced' in packets[0]
    helper = svc.store.update_run(run['id'], parent_id='synthetic-parent')
    context = svc.jobs._context(helper, [])
    helper_packet = next(message['content'] for message in context if message['content'].startswith('Goal checkpoint (saved task data):'))
    assert 'the main agent maintains overall tasks' in helper_packet and 'Prefer goal_task_update' not in helper_packet


@pytest.mark.parametrize('context', [8192, 16384])
@pytest.mark.parametrize('phase', ['initial', 'repair'])
def test_real_goal_schema_keeps_write_read_and_coordination_available(goal_run, tmp_path, context, phase):
    svc, _, _ = goal_run
    folder = tmp_path / 'project'; folder.mkdir()
    project = svc.create_project({'name': 'Schema availability fixture', 'path': str(folder)})
    goal = svc.goal_create({'text': 'Write a small change and verify the result.', 'project_id': project['id']})
    accepted = svc.jobs.start({'text': goal['request'], 'goal_id': goal['id'], 'project_id': project['id'],
        'context': context, 'tokens': 2048, 'mode': 'goal'})
    run = svc.store.run(accepted['id'])
    if phase == 'repair':
        run = svc.store.update_run(run['id'], loaded_tools=['write_file', 'read_file'], workflow_stage='repair')
    names = {tool['function']['name'] for tool in svc.jobs.registry.schemas(run, ['tools'])}
    assert {'write_file', 'read_file', 'tools_search', 'tools_load', 'request_user_input',
            'goal_read', 'goal_update', 'artifact_read'} <= names


@pytest.mark.parametrize('kind', ['core', 'compatible', 'openrouter'])
def test_fake_http_wire_preserves_anyof_for_core_and_compatible_protocols(monkeypatch, kind):
    tool = deepcopy(schema()); tool['coordinator_private_marker'] = 'Never send coordinator metadata.'
    selected = 'fixture/coder:free' if kind == 'openrouter' else 'fixture'
    def response(request):
        if request.method == 'GET': return httpx.Response(200, json={'data': [model(selected)]})
        if kind == 'core':
            return httpx.Response(200, content=(json.dumps({'message': {'content': 'Synthetic answer.'}, 'done': True})+'\n').encode())
        return httpx.Response(200, content=('data: '+json.dumps({'choices': [{'delta': {'content': 'Synthetic answer.'}}]})+'\n\n'
            +'data: '+json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})+'\n\ndata: [DONE]\n\n').encode())
    requests, _ = install_http(monkeypatch, response)
    # RunManager supplies the coordinator's wire projection to every adapter.
    # Core preserves that input rather than accepting coordinator-only fields.
    data = {'model': selected, 'context': 8192, 'tokens': 1024, 'thinking': False,
            'messages': [{'role': 'user', 'content': 'Synthetic request.'}], 'tools': wire_schemas([tool])}
    if kind == 'core': packets = list(Core(base='https://engine.invalid/api').stream_agent(data, threading.Event()))
    elif kind == 'compatible': packets = list(CompatibleProvider({'kind': 'openai-compatible', 'url': 'https://engine.invalid/v1',
        'capabilities': ['tools'], 'context_limit': 32768}).generate(data, threading.Event()))
    else: packets = list(OpenRouterProvider({'id': 'openrouter', 'kind': 'openrouter', 'url': OPENROUTER_URL,
        'credential_ref': 'reference', 'enabled': True, 'remote_consent': True, 'data_collection': 'deny'}, FakeVault()).generate(data, threading.Event()))
    assert packets[-1]['done'] is True
    posts = [request for request in requests if request.method == 'POST']; assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body['tools'] == wire_schemas([tool]) and 'coordinator_private_marker' not in json.dumps(body)
    wire = body['tools'][0]; assert 'strict' not in wire['function']
    task = wire['function']['parameters']['properties']['tasks']['items']
    assert task['required'] == ['status'] and task['anyOf'] == [{'required': ['id']}, {'required': ['text']}]
    for identity in ({'id': 'first'}, {'text': 'Exact saved task text.'}):
        Draft202012Validator(wire['function']['parameters']).validate({
            'tasks': [{**identity, 'status': 'in_progress'}], 'checkpoint': 'Saved.', 'next_action': 'Continue.'})


def test_completed_legacy_goal_invocation_is_not_replayed_after_reopen(goal_run, monkeypatch):
    svc, goal, run = goal_run
    old = deepcopy(schema()); item = old['function']['parameters']['properties']['tasks']['items']
    item['required'] = ['text', 'status']; item.pop('anyOf', None)
    args = {'tasks': [{'text': goal['tasks'][0]['text'], 'status': 'in_progress'}], 'checkpoint': 'Saved.', 'next_action': 'Verify.'}
    assert validate_arguments(old, args) is None
    first = svc.jobs._execute_call(run, {'cancel': threading.Event()}, call('goal_update', args), 0,
        {'goal_update': old}, {'capabilities': ['tools']}, time.monotonic())
    saved = svc.store.goal(goal['id']); home, core = svc.store.home, svc.core
    svc.shutdown(); reopened = ForgeService(core=core, data_dir=home)
    try:
        monkeypatch.setattr(reopened.jobs.registry, 'execute', lambda *args, **kwargs: pytest.fail('Completed goal update replayed'))
        assert validate_arguments(schema(), args) is None
        replay = reopened.jobs._execute_call(reopened.store.run(run['id']), {'cancel': threading.Event()}, call('goal_update', args), 0,
            {'goal_update': schema()}, {'capabilities': ['tools']}, time.monotonic())
        assert replay == first and reopened.store.goal(goal['id']) == saved
        with reopened.store._connection() as db:
            rows = db.execute('SELECT name,status FROM invocations WHERE run_id=?', (run['id'],)).fetchall()
        assert [(row['name'], row['status']) for row in rows] == [('goal_update', 'completed')]
    finally:
        reopened.shutdown()
