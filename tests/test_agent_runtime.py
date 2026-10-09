"""Agent integration tests with real temporary files and durable SQLite storage.

Only model inference is scripted. Files, backups, task state, approval handling,
and saved conversation context exercise the production service and runtime.
"""
import copy
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from core import Core
from agent_runtime import safe_compaction_cut
from service import Service
from storage import Store


def call(name, **arguments):
    return {'function': {'name': name, 'arguments': arguments}}


def response(text='', calls=(), thinking=''):
    return [{'message': {'content': text, 'tool_calls': list(calls), 'thinking': thinking},
             'done': True, 'done_reason': 'stop'}]


class ScriptedCore:
    def __init__(self, *rounds, capabilities=('tools', 'thinking', 'vision'), validate=True, model_info=None):
        self.rounds = list(rounds)
        self.capabilities = list(capabilities)
        self.validate = validate
        self.requests = []
        self.dispatched = []
        self.compactions = []
        self.summary = 'Saved continuity: the project uses Python and its tests should remain green.'
        self.compaction_error = None
        self.model_info = model_info or {}

    def dispatch(self, action, data):
        self.dispatched.append((action, copy.deepcopy(data)))
        if action == 'show':
            return {'capabilities': self.capabilities, 'model_info': self.model_info}
        if action == 'research':
            return {'sources': [{'title': 'Primary docs', 'url': 'https://example.com/docs', 'snippet': 'A documented fact.'}]}
        raise ValueError('Unknown fake action')

    def stream_agent(self, data, cancelled):
        if self.validate:
            Core._agent_payload(data)
        self.requests.append(copy.deepcopy(data))
        if not self.rounds:
            raise AssertionError('Runtime requested an unexpected model round')
        script = self.rounds.pop(0)
        if callable(script):
            yield from script(data, cancelled)
        else:
            for packet in script:
                if isinstance(packet, Exception):
                    raise packet
                yield copy.deepcopy(packet)

    def compact_messages(self, model, previous_summary, messages, cancel_event=None, context=16384):
        self.compactions.append({'model': model, 'previous_summary': previous_summary,
                                 'messages': copy.deepcopy(messages), 'context': context})
        if self.compaction_error is not None:
            raise self.compaction_error
        return self.summary


@pytest.fixture
def environment(tmp_path):
    project_path = tmp_path / 'project'
    project_path.mkdir()
    store = Store(tmp_path / 'state')
    project = store.create_project('Test project', project_path)
    return store, project, project_path


def start(service, project=None, **data):
    return service.dispatch('start_chat', {'text': 'Please help with this project.', 'model': 'test-model',
                                          'project_id': project['id'] if project else None, **data})


def wait_for(service, handle, predicate, *, events=None, timeout=8):
    collected = [] if events is None else events
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.dispatch('poll', {'id': handle['id']})
        collected.extend(snapshot['events'])
        if predicate(snapshot, collected):
            return collected
        if snapshot['finished']:
            pytest.fail(f'Agent finished before expected event: {collected!r}')
        threading.Event().wait(0.005)
    service.dispatch('cancel', {'id': handle['id']})
    pytest.fail(f'Agent did not reach expected state: {collected!r}')


def finish(service, handle, *, events=None, allow_error=False):
    collected = wait_for(service, handle, lambda snapshot, _: snapshot['finished'], events=events)
    if not allow_error:
        assert not [event for event in collected if event['type'] == 'error'], collected
    assert collected[-1]['type'] == 'done'
    return collected


def tools_in(store, chat_id):
    return [json.loads(message['content']) for message in store.get_chat(chat_id)['messages']
            if message['role'] == 'tool']


def test_real_file_read_edit_read_loop_and_reopened_chat(environment):
    store, project, folder = environment
    target = folder / 'main.py'
    target.write_bytes(b'answer = 41\n')

    def edit_after_read(data, cancelled):
        read = json.loads(data['messages'][-1]['content'])
        assert read['content'] == 'answer = 41\n'
        yield from response(calls=[call('write_file', path='main.py', content='answer = 42\n',
                                       expected_sha256=read['sha256'])])

    core = ScriptedCore(response(calls=[call('read_file', path='main.py')]), edit_after_read,
                        response(calls=[call('read_file', path='main.py')]), response('Updated and checked.'))
    service = Service(core=core, store=store)
    handle = start(service, project, allow_edits=True, thinking=True)
    events = finish(service, handle)
    assert target.read_text(encoding='utf-8') == 'answer = 42\n'
    saved = Store(store.data_dir).get_chat(handle['chat_id'])
    assert [message['role'] for message in saved['messages']] == [
        'user', 'assistant', 'tool', 'assistant', 'tool', 'assistant', 'tool', 'assistant']
    results = tools_in(store, handle['chat_id'])
    assert all(result['ok'] for result in results)
    assert results[-1]['content'] == 'answer = 42\n'
    assert Path(results[1]['backup_path']).read_bytes() == b'answer = 41\n'
    assert len([event for event in events if event['type'] == 'tool' and event['state'] == 'done']) == 3
    continuation = ScriptedCore(response('I remember the change.'))
    resumed = Service(core=continuation, store=Store(store.data_dir))
    next_handle = start(resumed, project, chat_id=handle['chat_id'], text='What did we change?')
    finish(resumed, next_handle)
    assert next_handle['chat_id'] == handle['chat_id']
    context = continuation.requests[0]['messages']
    assert any(message['content'] == 'Updated and checked.' for message in context)
    assert context[-1]['content'] == 'What did we change?'


@pytest.mark.parametrize('allowed', [False, True], ids=['denied', 'approved'])
def test_command_requires_exact_pending_approval(environment, monkeypatch, allowed):
    store, project, _ = environment
    argv = [sys.executable, '-c', "print('sidekick-approved-command')"]
    core = ScriptedCore(response(calls=[call('run_command', argv=argv, cwd='.', timeout=5)]),
                        response('Command step finished.'))
    service = Service(core=core, store=store)
    # A denied command must not even start a child process.
    if not allowed:
        def forbidden(*args, **kwargs):
            pytest.fail('Denied command spawned a process')
        monkeypatch.setattr('project_tools.subprocess.Popen', forbidden)
    handle = start(service, project)
    events = wait_for(service, handle, lambda _, items: any(e['type'] == 'approval' for e in items))
    approval = next(event for event in events if event['type'] == 'approval')
    assert approval['arguments']['argv'] == argv
    assert approval['arguments']['cwd'] == '.'
    assert tools_in(store, handle['chat_id']) == []
    with pytest.raises(ValueError, match='no longer pending'):
        service.dispatch('approve', {'job_id': handle['id'], 'approval_id': 'wrong', 'allowed': True})
    service.dispatch('approve', {'job_id': handle['id'], 'approval_id': approval['id'], 'allowed': allowed})
    finish(service, handle, events=events)
    result = tools_in(store, handle['chat_id'])[0]
    if allowed:
        assert result['ok'] and result['exit_code'] == 0
        assert result['output'].strip() == 'sidekick-approved-command'
    else:
        assert 'not approved' in result['error']
    with pytest.raises(ValueError, match='no longer pending'):
        service.dispatch('approve', {'job_id': handle['id'], 'approval_id': approval['id'], 'allowed': True})


@pytest.mark.parametrize('tool, arguments', [
    ('write_file', {'path': 'main.txt', 'content': 'changed'}),
    ('edit_file', {'path': 'main.txt', 'old_text': 'original', 'new_text': 'changed'}),
    ('make_directory', {'path': 'new-dir'}),
    ('move_file', {'source': 'main.txt', 'destination': 'moved.txt'}),
    ('restore_file', {'path': 'main.txt', 'backup_id': '0' * 32}),
])
def test_disabled_edits_block_every_mutating_file_tool(environment, tool, arguments):
    store, project, folder = environment
    target = folder / 'main.txt'
    target.write_text('original', encoding='utf-8')
    core = ScriptedCore(response(calls=[call(tool, **arguments)]), response('Edits are disabled.'))
    service = Service(core=core, store=store)
    handle = start(service, project, allow_edits=False)
    finish(service, handle)
    assert 'edits are disabled' in tools_in(store, handle['chat_id'])[0]['error']
    assert target.read_text(encoding='utf-8') == 'original'
    assert sorted(path.name for path in folder.iterdir()) == ['main.txt']
    assert not (store.data_dir / 'backups').exists()


def test_cancelled_stream_never_executes_a_proposed_file_write(environment):
    store, project, folder = environment
    streamed, release = threading.Event(), threading.Event()

    def pending_tool(data, cancelled):
        yield {'message': {'content': 'Preparing an edit.', 'tool_calls': [
            call('write_file', path='should-not-exist.txt', content='unexpected')]}}
        streamed.set()
        assert release.wait(3)
        yield {'done': True}

    service = Service(core=ScriptedCore(pending_tool), store=store)
    handle = start(service, project, allow_edits=True)
    try:
        assert streamed.wait(3)
        service.dispatch('cancel', {'id': handle['id']})
    finally:
        release.set()
    events = finish(service, handle)
    assert events[-1]['cancelled'] is True
    assert not (folder / 'should-not-exist.txt').exists()
    messages = store.get_chat(handle['chat_id'])['messages']
    assert messages[-1]['role'] == 'assistant'
    assert messages[-1]['status'] == 'cancelled'
    assert not any(message['role'] == 'tool' for message in messages)


def test_cancel_while_awaiting_approval_never_starts_a_process(environment, monkeypatch):
    store, project, _ = environment
    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled command spawned a process')
    monkeypatch.setattr('project_tools.subprocess.Popen', forbidden)
    core = ScriptedCore(response(calls=[call('run_command', argv=[sys.executable, '-c', "print('unused')"])]))
    service = Service(core=core, store=store)
    handle = start(service, project)
    events = wait_for(service, handle, lambda _, items: any(event['type'] == 'approval' for event in items))
    service.dispatch('cancel', {'id': handle['id']})
    events = finish(service, handle, events=events)
    assert events[-1]['cancelled'] is True
    assert 'not approved' in tools_in(store, handle['chat_id'])[0]['error']


def test_existing_chat_keeps_its_project_scope_even_if_request_changes_project(environment, tmp_path):
    store, project, folder = environment
    other_path = tmp_path / 'other-project'
    other_path.mkdir()
    other = store.create_project('Other', other_path)
    chat = store.create_chat(project['id'])
    core = ScriptedCore(response(calls=[call('write_file', path='scope.txt', content='owned by original')]),
                        response('Saved.'))
    service = Service(core=core, store=store)
    handle = start(service, other, chat_id=chat['id'], allow_edits=True)
    finish(service, handle)
    assert (folder / 'scope.txt').read_text(encoding='utf-8') == 'owned by original'
    assert not (other_path / 'scope.txt').exists()
    assert store.get_chat(chat['id'])['project_id'] == project['id']


def test_out_of_project_file_access_is_a_recoverable_tool_error(environment, tmp_path):
    store, project, _ = environment
    outside = tmp_path / 'outside.txt'
    outside.write_text('PRIVATE', encoding='utf-8')
    core = ScriptedCore(response(calls=[call('read_file', path=str(outside))]), response('That file is outside this project.'))
    service = Service(core=core, store=store)
    handle = start(service, project)
    finish(service, handle)
    result = tools_in(store, handle['chat_id'])[0]
    assert not result['ok'] and 'outside' in result['error']
    assert 'PRIVATE' not in json.dumps(core.requests)


def test_auto_compaction_saves_summary_and_preserves_complete_history(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    original = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'Historical turn {index}')
                for index in range(20)]
    core = ScriptedCore(response('Continuing with saved context.'))
    service = Service(core=core, store=store)
    handle = start(service, project, chat_id=chat['id'], text='Continue the implementation.')
    events = finish(service, handle)
    restored = Store(store.data_dir).get_chat(chat['id'])
    assert restored['summary'] == core.summary
    assert restored['compacted_through'] > 0
    assert restored['messages'][:20] == original
    assert len(restored['messages']) == 22
    assert len(core.compactions) == 1
    assert any(event['type'] == 'memory' for event in events)
    assert core.summary in core.requests[0]['messages'][0]['content']
    assert not any(message['content'] == 'Historical turn 0' for message in core.requests[0]['messages'])


def test_auto_compaction_handles_fewer_than_seven_large_messages(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    original = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant',
                                  f'Historical turn {index}: ' + 'x' * 16_000)
                for index in range(4)]
    core = ScriptedCore(response('Large history compacted.'))
    service = Service(core=core, store=store)
    handle = start(service, project, chat_id=chat['id'], text='Continue.')
    finish(service, handle)
    saved = store.get_chat(chat['id'])
    assert core.compactions
    assert saved['compacted_through'] > 0
    assert saved['messages'][:4] == original


def test_compaction_failure_preserves_original_summary_and_history(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    original = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'History {index}')
                for index in range(22)]
    store.set_summary(chat['id'], 'Existing summary', original[1]['id'])
    core = ScriptedCore()
    core.compaction_error = ValueError('Model unavailable while summarizing')
    service = Service(core=core, store=store)
    handle = start(service, project, chat_id=chat['id'])
    events = finish(service, handle, allow_error=True)
    saved = store.get_chat(chat['id'])
    assert any(event['type'] == 'error' and 'summarizing' in event['text'] for event in events)
    assert saved['summary'] == 'Existing summary'
    assert saved['compacted_through'] == original[1]['id']
    assert saved['messages'][:22] == original


def test_large_file_tool_arguments_compact_before_next_model_round(environment):
    store, project, folder = environment
    content = 'A' * 50_000
    core = ScriptedCore(response(calls=[call('write_file', path='large.txt', content=content)]),
                        response('The large file was saved.'))
    service = Service(core=core, store=store)
    handle = start(service, project, allow_edits=True)
    finish(service, handle)
    assert (folder / 'large.txt').read_text(encoding='utf-8') == content
    saved = store.get_chat(handle['chat_id'])
    assert saved['messages'][-1]['content'] == 'The large file was saved.'
    assert saved['messages'][1]['tool_calls'][0]['function']['arguments']['content'] == content
    assert core.compactions


def test_tool_failures_are_saved_and_returned_to_model_for_recovery(environment):
    store, project, _ = environment
    core = ScriptedCore(response(calls=[call('read_file', path='missing.txt')]),
                        response(calls=[call('not_a_real_tool', path='anything')]), response('Both tool failures were handled.'))
    service = Service(core=core, store=store)
    handle = start(service, project)
    finish(service, handle)
    results = tools_in(store, handle['chat_id'])
    assert len(results) == 2
    assert all(not result['ok'] and result['error'] for result in results)
    assert 'does not exist' in results[0]['error']
    assert 'Unknown' in results[1]['error']
    assert json.loads(core.requests[1]['messages'][-1]['content']) == results[0]


@pytest.mark.parametrize('standalone', [False, True], ids=['project-memory', 'standalone-memory'])
def test_search_memory_does_not_leak_other_project_conversations(environment, tmp_path, standalone):
    store, project, _ = environment
    other_path = tmp_path / 'other-project'
    other_path.mkdir()
    other = store.create_project('Other', other_path)
    scope = None if standalone else project
    own = store.create_chat(scope['id'] if scope else None, title='Own prior chat')
    store.add_message(own['id'], 'assistant', 'ORCHID means the parser should preserve whitespace.')
    foreign = store.create_chat(other['id'], title='Confidential other project')
    store.add_message(foreign['id'], 'assistant', 'ORCHID is the secret deployment name elsewhere.')
    if not standalone:
        unscoped = store.create_chat()
        store.add_message(unscoped['id'], 'assistant', 'ORCHID standalone secret.')
    core = ScriptedCore(response(calls=[call('search_memory', query='ORCHID')]), response('Found the earlier parser decision.'))
    service = Service(core=core, store=store)
    handle = start(service, scope, text='Find the earlier decision.')
    finish(service, handle)
    matches = tools_in(store, handle['chat_id'])[0]['matches']
    assert [match['chat_id'] for match in matches] == [own['id']]
    assert 'preserve whitespace' in matches[0]['snippet']
    assert 'secret' not in json.dumps(core.requests)


def test_service_project_and_task_management_and_container_mount_scope(environment, tmp_path, monkeypatch):
    store, project, folder = environment
    service = Service(core=ScriptedCore(), store=store)
    assert service.dispatch('projects')['projects'] == [project]
    task = service.dispatch('create_task', {'project_id': project['id'], 'title': 'Verify parsing'})
    updated = service.dispatch('update_task', {'project_id': project['id'], 'id': task['id'], 'status': 'completed'})
    assert updated['status'] == 'completed'
    assert service.dispatch('tasks', {'project_id': project['id']})['tasks'] == [updated]
    monkeypatch.setenv('SIDEKICK_PROJECTS_ROOT', str(folder))
    with pytest.raises(ValueError, match='mounted workspace'):
        service.dispatch('create_project', {'path': str(tmp_path), 'name': 'Outside'})
    inside = folder / 'nested'
    inside.mkdir()
    selected = service.dispatch('create_project', {'path': str(inside), 'name': 'Inside'})
    assert selected['path'] == str(inside.resolve())


def test_stream_error_saves_partial_answer_but_never_runs_its_tools(environment):
    store, project, folder = environment
    core = ScriptedCore([
        {'message': {'content': 'Partial answer', 'tool_calls': [call('write_file', path='wrong.txt', content='wrong')]}},
        ValueError('Connection interrupted before completion')])
    service = Service(core=core, store=store)
    handle = start(service, project, allow_edits=True)
    events = finish(service, handle, allow_error=True)
    assert any(event['type'] == 'error' and 'interrupted' in event['text'] for event in events)
    saved = store.get_chat(handle['chat_id'])
    assert saved['messages'][-1]['content'] == 'Partial answer'
    assert saved['messages'][-1]['status'] == 'partial'
    assert not (folder / 'wrong.txt').exists()
    assert tools_in(store, handle['chat_id']) == []


def test_incremental_tool_arguments_are_assembled_before_execution(environment):
    store, project, folder = environment
    core = ScriptedCore([
        {'message': {'tool_calls': [{'index': 0, 'function': {
            'name': 'write_file', 'arguments': '{"path":"chunked.txt",'}}]}},
        {'message': {'tool_calls': [{'index': 0, 'function': {'arguments': '"content":"assembled"}'}}]}},
        {'done': True, 'done_reason': 'stop'}], response('Saved the assembled call.'))
    service = Service(core=core, store=store)
    handle = start(service, project, allow_edits=True)
    finish(service, handle)
    assert (folder / 'chunked.txt').read_text(encoding='utf-8') == 'assembled'
    assert len(tools_in(store, handle['chat_id'])) == 1
    assert tools_in(store, handle['chat_id'])[0]['ok']


def test_thinking_is_streamed_when_enabled_and_saved(environment):
    store, project, _ = environment
    core = ScriptedCore(response('Answer', thinking='Model-provided reasoning'))
    service = Service(core=core, store=store)
    handle = start(service, project, thinking=True)
    events = finish(service, handle)
    assert any(event['type'] == 'thinking' and event['text'] == 'Model-provided reasoning' for event in events)
    assert store.get_chat(handle['chat_id'])['messages'][-1]['thinking'] == 'Model-provided reasoning'


@pytest.mark.parametrize('enabled', [False, True], ids=['web-disabled', 'web-enabled'])
def test_agent_web_search_obeys_toggle_and_saves_sources(environment, enabled):
    store, project, _ = environment
    core = ScriptedCore(response(calls=[call('web_search', query='Python official documentation')]),
                        response('Research step completed.'))
    service = Service(core=core, store=store)
    handle = start(service, project, web=enabled)
    finish(service, handle)
    result = tools_in(store, handle['chat_id'])[0]
    research_calls = [entry for entry in core.dispatched if entry[0] == 'research']
    names = [schema['function']['name'] for schema in core.requests[0]['tools']]
    if enabled:
        assert len(research_calls) == 1
        assert result['sources'][0]['url'] == 'https://example.com/docs'
        assert 'web_search' in names
    else:
        assert not research_calls
        assert 'disabled' in result['error']
        assert 'web_search' not in names


def test_double_submit_does_not_add_duplicate_user_turn(environment):
    store, project, _ = environment
    entered, release = threading.Event(), threading.Event()
    def waiting(data, cancelled):
        entered.set()
        assert release.wait(3)
        yield from response('Only one turn.')
    service = Service(core=ScriptedCore(waiting), store=store)
    handle = start(service, project)
    try:
        assert entered.wait(3)
        with pytest.raises(ValueError, match='already running'):
            start(service, project, chat_id=handle['chat_id'])
        assert len(store.get_chat(handle['chat_id'])['messages']) == 1
    finally:
        release.set()
    finish(service, handle)
    assert len(store.get_chat(handle['chat_id'])['messages']) == 2


def test_switching_to_text_model_strips_historical_images_without_deleting_them(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'], model='vision-model')
    attachment = 'original-saved-image-attachment'
    original = store.add_message(chat['id'], 'user', 'Describe this diagram.', images=[attachment])
    store.add_message(chat['id'], 'assistant', 'The diagram shows a parsing pipeline.')
    core = ScriptedCore(response('I can continue from the earlier description.'),
                        capabilities=('tools', 'thinking'))
    service = Service(core=core, store=store)
    handle = start(service, project, chat_id=chat['id'], text='Implement the pipeline.', model='text-model')
    finish(service, handle)
    assert len(core.requests) == 1
    assert not any(message.get('images') for message in core.requests[0]['messages'])
    assert any(message['content'] == 'The diagram shows a parsing pipeline.'
               for message in core.requests[0]['messages'])
    saved = Store(store.data_dir).get_chat(chat['id'])
    assert saved['messages'][0] == original
    assert saved['messages'][0]['images'] == [attachment]


def test_new_image_is_rejected_for_text_only_model_and_attachment_stays_saved(environment):
    store, project, _ = environment
    core = ScriptedCore(capabilities=('tools', 'thinking'))
    service = Service(core=core, store=store)
    handle = start(service, project, images=['new-image-attachment'], model='text-model')
    events = finish(service, handle, allow_error=True)
    assert any(event['type'] == 'error' and 'vision' in event['text'].lower() for event in events)
    assert core.requests == []
    saved = store.get_chat(handle['chat_id'])
    assert len(saved['messages']) == 1
    assert saved['messages'][0]['images'] == ['new-image-attachment']


def test_vision_model_receives_only_newest_historical_image_and_keeps_originals(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    first = store.add_message(chat['id'], 'user', 'First image', images=['first-image'])
    store.add_message(chat['id'], 'assistant', 'First image description')
    second = store.add_message(chat['id'], 'user', 'Second image', images=['second-image'])
    store.add_message(chat['id'], 'assistant', 'Second image description')
    core = ScriptedCore(response('Continuing with the newest image.'))
    service = Service(core=core, store=store)
    handle = start(service, project, chat_id=chat['id'], text='Continue from the second diagram.')
    finish(service, handle)
    assert [message['images'] for message in core.requests[0]['messages'] if message.get('images')] == [['second-image']]
    saved = store.get_chat(chat['id'])
    assert saved['messages'][0] == first
    assert saved['messages'][2] == second


def test_compaction_can_archive_all_complete_tool_evidence_without_splitting_pending_calls():
    messages = [
        {'role': 'user', 'content': 'Inspect two files.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [call('read_file', path='a.py'), call('read_file', path='b.py')]},
        {'role': 'tool', 'content': '{"content":"a"}', 'tool_name': 'read_file'},
        {'role': 'tool', 'content': '{"content":"b"}', 'tool_name': 'read_file'},
    ]
    assert safe_compaction_cut(messages, keep=0) == 4
    assert safe_compaction_cut(messages, keep=1) == 1
    assert safe_compaction_cut(messages[:3], keep=0) == 1


@pytest.mark.parametrize('context', [2048, 6144, 12288, 32768, 65536, 131072, 262144])
def test_selected_context_is_reported_and_passed_without_clamping(environment, context):
    store, _, _ = environment
    core = ScriptedCore(response('Hello.'), model_info={'general.architecture': 'test', 'test.context_length': 262144})
    service = Service(core=core, store=store)
    handle = start(service, context=context)
    events = finish(service, handle)
    assert core.requests[0]['context'] == context
    payload = Core._agent_payload(core.requests[0])
    assert payload['options']['num_ctx'] == context
    assert payload['options']['num_predict'] == min(2048, context // 4)
    event = next(event for event in events if event['type'] == 'context')
    assert event == {'type': 'context', 'requested': context, 'effective': context,
                     'response_tokens': min(2048, context // 4), 'model_limit': 262144}


def test_every_tool_round_uses_selected_context(environment):
    store, project, _ = environment
    core = ScriptedCore(response(calls=[call('list_tasks')]), response('Tasks checked.'))
    service = Service(core=core, store=store)
    finish(service, start(service, project, context=24576))
    assert [request['context'] for request in core.requests] == [24576, 24576]
    assert [action for action, _ in core.dispatched] == ['show']


@pytest.mark.parametrize('context', [None, False, True, 8192.0, '32768', 0, 2049, 264192])
def test_invalid_context_never_saves_a_user_turn_or_changes_chat_model(environment, context):
    store, project, _ = environment
    chat = store.create_chat(project['id'], model='original-model')
    original = store.get_chat(chat['id'])
    core = ScriptedCore()
    service = Service(core=core, store=store)
    with pytest.raises(ValueError, match='Context must'):
        start(service, project, chat_id=chat['id'], context=context, model='new-model')
    assert store.get_chat(chat['id']) == original
    assert core.dispatched == []


def test_model_advertised_maximum_rejects_larger_selection_before_saving(environment):
    store, _, _ = environment
    core = ScriptedCore(model_info={'general.architecture': 'small', 'small.context_length': 16384})
    service = Service(core=core, store=store)
    with pytest.raises(ValueError, match='maximum context of 16,384'):
        start(service, context=32768)
    assert store.list_chats() == []
    assert core.requests == []


def test_primary_architecture_limit_wins_over_unrelated_encoder_metadata(environment):
    store, _, _ = environment
    core = ScriptedCore(response('Ready.'), model_info={
        'general.architecture': 'main', 'main.context_length': 32768, 'vision.context_length': 256})
    service = Service(core=core, store=store)
    finish(service, start(service, context=32768))
    assert core.requests[0]['context'] == 32768


def test_tiny_project_context_fails_before_saving_instead_of_overflowing_tools(environment):
    store, project, _ = environment
    service = Service(core=ScriptedCore(), store=store)
    with pytest.raises(ValueError, match='Increase Context'):
        start(service, project, context=2048)
    assert store.list_chats() == []


@pytest.mark.parametrize(('context', 'compacts'), [(8192, True), (131072, False)])
def test_large_history_compaction_threshold_tracks_selected_window(environment, context, compacts):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    original = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant',
                                  f'History {index}: ' + 'x' * 6000) for index in range(16)]
    core = ScriptedCore(response('Continuing.'))
    service = Service(core=core, store=store)
    finish(service, start(service, project, chat_id=chat['id'], context=context))
    assert bool(core.compactions) is compacts
    assert store.get_chat(chat['id'])['messages'][:16] == original
    if not compacts:
        assert sum(len(message['content']) for message in core.requests[0]['messages']) > 60000
    else:
        assert all(compaction['context'] <= 16384 for compaction in core.compactions)


def test_many_short_turns_use_larger_message_threshold_at_high_context(environment):
    store, _, _ = environment
    chat = store.create_chat()
    for index in range(80):
        store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'Turn {index}')
    core = ScriptedCore(response('All history retained.'))
    service = Service(core=core, store=store)
    finish(service, start(service, chat_id=chat['id'], context=65536))
    assert not core.compactions
    assert len(core.requests[0]['messages']) == 81


def test_lowering_window_compacts_beyond_eight_batches_until_history_fits(environment):
    store, project, _ = environment
    chat = store.create_chat(project['id'])
    for index in range(24):
        store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'Turn {index}: ' + 'x' * 16000)
    core = ScriptedCore(response('Finished shrinking context.'))
    service = Service(core=core, store=store)
    finish(service, start(service, project, chat_id=chat['id'], context=8192))
    assert len(core.compactions) > 8
    assert len(store.get_chat(chat['id'])['messages']) == 26
    assert core.requests[0]['context'] == 8192


def test_low_limit_model_uses_bounded_summary_context(environment):
    store, _, _ = environment
    chat = store.create_chat()
    for index in range(20):
        store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'Turn {index}')
    core = ScriptedCore(response('Continuing.'), model_info={'small.context_length': 4096})
    service = Service(core=core, store=store)
    finish(service, start(service, chat_id=chat['id'], context=4096))
    assert core.compactions
    assert all(compaction['context'] == 4096 for compaction in core.compactions)


@pytest.mark.parametrize(('context', 'length'), [(32768, 60000), (262144, 200000)])
def test_large_new_prompt_is_accepted_with_a_large_context(environment, context, length):
    store, project, _ = environment
    text = 'Review this code:\n' + 'x' * length
    core = ScriptedCore(response('Reviewed.'))
    service = Service(core=core, store=store)
    handle = start(service, project, text=text, context=context)
    finish(service, handle)
    assert core.requests[0]['messages'][-1]['content'] == text
    assert store.get_chat(handle['chat_id'])['messages'][0]['content'] == text


@pytest.mark.parametrize('context', [2048, 8192, 32768, 262144])
def test_prompt_above_scaled_text_limit_is_not_saved(environment, context):
    store, _, _ = environment
    core = ScriptedCore()
    service = Service(core=core, store=store)
    with pytest.raises(ValueError, match='characters for the selected context'):
        start(service, text='x' * (18000 * context // 8192 + 1), context=context)
    assert store.list_chats() == []
    assert core.dispatched == []
