"""Independent release checks for plan continuity and chat lifecycle races."""
import threading

import pytest

from core import Core
from test_forge_core import Engine, call, finished, service


@pytest.mark.parametrize('operation', ['chat_archive', 'chat_move'])
def test_start_rechecks_chat_snapshot_before_creating_writer(tmp_path, monkeypatch, operation):
    svc = service(tmp_path)
    try:
        first_folder = tmp_path / 'first'
        second_folder = tmp_path / 'second'
        first_folder.mkdir()
        second_folder.mkdir()
        first = svc.create_project({'name': 'First', 'path': str(first_folder)})
        second = svc.create_project({'name': 'Second', 'path': str(second_folder)})
        chat = svc.store.create_chat(first['id'], model='fixture')
        original = svc.store.get_chat
        changed = False

        def snapshot_then_mutate(identifier, *, limit=None):
            nonlocal changed
            snapshot = original(identifier, limit=limit)
            if identifier == chat['id'] and not changed:
                changed = True
                arguments = {'id': identifier, 'archived': True} if operation == 'chat_archive' else {'id': identifier, 'project_id': second['id']}
                svc.dispatch(operation, arguments)
            return snapshot

        monkeypatch.setattr(svc.store, 'get_chat', snapshot_then_mutate)
        monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
        # Mutations now share the writer lock: a same-thread reentrant mutation
        # is an artificial stale snapshot, so reproduce the real lock boundary.
        original_start_lock = svc.jobs.lock

        class MutateBeforeLock:
            def __enter__(self):
                if not changed:
                    snapshot_then_mutate(chat['id'], limit=1)
                return original_start_lock.__enter__()

            def __exit__(self, *args):
                return original_start_lock.__exit__(*args)

        monkeypatch.setattr(svc.jobs, 'lock', MutateBeforeLock())
        monkeypatch.setattr(svc.store, 'get_chat', original)
        if operation == 'chat_archive':
            with pytest.raises(ValueError, match='archived'):
                svc.jobs.start({'chat_id': chat['id'], 'text': 'Continue'})
            assert not svc.store.runs()
        else:
            run = svc.jobs.start({'chat_id': chat['id'], 'text': 'Continue'})
            assert svc.store.run(run['id'])['project_id'] == second['id']
            assert svc.store.get_chat(chat['id'])['project_id'] == second['id']
    finally:
        svc.shutdown()


def test_resuming_old_run_after_chat_move_cannot_write_previous_project(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        first_folder = tmp_path / 'first'
        second_folder = tmp_path / 'second'
        first_folder.mkdir()
        second_folder.mkdir()
        first = svc.create_project({'name': 'First', 'path': str(first_folder)})
        second = svc.create_project({'name': 'Second', 'path': str(second_folder)})
        run = svc.jobs.start({'project_id': first['id'], 'text': 'Inspect files'})
        finished(svc, run)
        svc.dispatch('chat_move', {'id': run['chat_id'], 'project_id': second['id']})
        svc.store.update_run(run['id'], status='paused')
        monkeypatch.setattr(svc.jobs, '_launch', lambda saved: None)
        with pytest.raises(ValueError, match='project|moved'):
            svc.jobs.resume(run['id'])
        assert svc.store.run(run['id'])['status'] == 'paused'
    finally:
        svc.shutdown()


def test_plan_manual_resume_retains_last_limited_fragment(tmp_path):
    def fragment(number):
        def response(data, cancel):
            yield {'message': {'content': f'{number}. Step {number}.'}, 'done': True,
                   'done_reason': 'length', 'eval_count': 8}
        return response

    svc = service(tmp_path, Engine([fragment(1), fragment(2), fragment(3), {'content': '4. Step 4.'}]))
    try:
        run = svc.command({'text': '/plan Prepare four ordered steps'})
        assert finished(svc, run)['status'] == 'paused'
        assert not svc.store.entities('plans')
        resumed = svc.jobs.resume(run['id'])
        assert finished(svc, resumed)['status'] == 'completed'
        plan = svc.store.entity('plans', run['id'])
        assert [task['text'] for task in plan['tasks']] == [f'Step {n}.' for n in range(1, 5)]
    finally:
        svc.shutdown()


def test_repeated_concurrent_build_creates_one_goal_and_one_writer(tmp_path):
    svc = service(tmp_path, Engine([{'content': '1. Inspect the source.\n2. Verify the result.'}]))
    try:
        planned = svc.command({'text': '/plan Review source'})
        assert finished(svc, planned)['status'] == 'completed'
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def build():
            barrier.wait(timeout=5)
            try:
                results.append(svc.plan_build({'id': planned['id']}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=build) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        assert not errors
        assert len(results) == 2 and results[0]['id'] == results[1]['id']
        assert len(svc.store.entities('goals')) == 1
        assert len([r for r in svc.store.runs() if r.get('goal_id')]) == 1
        finished(svc, results[0])
    finally:
        svc.shutdown()


def test_latest_chat_window_keeps_newest_messages_in_chronological_order(tmp_path):
    svc = service(tmp_path)
    try:
        chat = svc.store.create_chat(model='fixture')
        for number in range(265):
            svc.store.add_message(chat['id'], 'user' if number % 2 == 0 else 'assistant', str(number))
        saved = svc.dispatch('get_chat', {'id': chat['id'], 'limit': 200})
        assert saved['total_messages'] == 265
        assert [message['content'] for message in saved['messages']] == [str(n) for n in range(65, 265)]
    finally:
        svc.shutdown()


def test_single_leading_system_retains_plan_scope_and_exact_request():
    payload = Core._agent_payload({'model': 'fixture', 'context': 8192, 'tokens': 1024,
        'messages': [{'role': 'system', 'content': 'Project scope: connected source'},
                     {'role': 'user', 'content': 'Earlier request'},
                     {'role': 'assistant', 'content': 'Earlier reply'},
                     {'role': 'system', 'content': 'Plan only; inspect files without editing'},
                     {'role': 'user', 'content': 'Implement Unicode 🌍 handling'}], 'tools': []})
    systems = [m for m in payload['messages'] if m['role'] == 'system']
    assert len(systems) == 1 and payload['messages'][0] is systems[0]
    assert 'Project scope: connected source' in systems[0]['content']
    assert 'Plan only; inspect files without editing' in systems[0]['content']
    assert payload['messages'][-1]['content'] == 'Implement Unicode 🌍 handling'


@pytest.mark.parametrize('missing', [{'eval_count': None}, {'usage': None}])
def test_completed_round_with_nullable_usage_counters_is_not_paused(tmp_path, missing):
    def response(data, cancel):
        yield {'message': {'content': 'A complete visible answer.'}, 'done': True,
               'done_reason': 'stop', **missing}

    svc = service(tmp_path, Engine([response]))
    try:
        run = svc.jobs.start({'text': 'Answer the question'})
        assert finished(svc, run)['status'] == 'completed'
        usage = svc.store.usage()
        assert usage['totals']['requests'] == 1
        assert usage['totals']['output_tokens'] > 0
        assert usage['totals']['estimated_requests'] == 1
    finally:
        svc.shutdown()


def test_goal_journal_cannot_mutate_immutable_request_or_workspace_identity(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        folder = tmp_path / 'original-project'
        folder.mkdir()
        project = svc.create_project({'name': 'Original', 'path': str(folder)})
        chat = svc.store.create_chat(project['id'], model='fixture')
        other = svc.store.create_chat(model='fixture')
        goal = svc.goal_create({'text': 'Inspect source and validate behavior.',
                                'project_id': project['id'], 'chat_id': chat['id']})
        monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
        created = svc.jobs.start({'text': goal['request'], 'goal_id': goal['id'],
                                 'project_id': project['id'], 'chat_id': chat['id']})
        run = svc.store.run(created['id'])
        try:
            svc.jobs.registry.execute(run, 'goal_update', {
                'checkpoint': 'Reviewed source.', 'next_action': 'Validate behavior.',
                'project_id': None, 'chat_id': other['id'], 'request': 'Different request',
                'instructions': 'Arbitrary replacement instructions'}, threading.Event())
        except ValueError:
            pass  # Rejecting undeclared journal arguments is also safe.
        saved = svc.store.goal(goal['id'])
        assert saved['project_id'] == project['id']
        assert saved['chat_id'] == chat['id']
        assert saved['request'] == goal['request']
        assert 'instructions' not in saved
    finally:
        svc.shutdown()


def test_legacy_plan_recovery_uses_its_own_message_interval(tmp_path, monkeypatch):
    svc = service(tmp_path, Engine([{'content': '1. Inspect original source.\n2. Validate original result.'},
                                   {'content': '1. Later conversation task.'}]))
    try:
        planned = svc.command({'text': '/plan Review original behavior'})
        assert finished(svc, planned)['status'] == 'completed'
        svc.store.delete_entity('plans', planned['id'])
        later = svc.jobs.start({'chat_id': planned['chat_id'], 'text': 'A later unrelated question'})
        assert finished(svc, later)['status'] == 'completed'
        monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
        built = svc.plan_build({'id': planned['id']})
        plan = svc.store.entity('plans', planned['id'])
        assert [task['text'] for task in plan['tasks']] == ['Inspect original source.', 'Validate original result.']
        assert 'Later conversation task' not in plan['markdown']
        assert svc.store.goal(built['goal_id'])['request'] == 'Review original behavior'
    finally:
        svc.shutdown()


def test_legacy_empty_plan_does_not_borrow_later_assistant_answer(tmp_path):
    svc = service(tmp_path, Engine([{'content': ''}, {'content': '1. Later conversation task.'}]))
    try:
        planned = svc.command({'text': '/plan Review original behavior'})
        assert finished(svc, planned)['status'] == 'paused'
        # The older installation could mark a reasoning-only plan completed.
        svc.store.update_run(planned['id'], status='completed')
        later = svc.jobs.start({'chat_id': planned['chat_id'], 'text': 'A later unrelated question'})
        assert finished(svc, later)['status'] == 'completed'
        with pytest.raises(ValueError, match='visible saved plan'):
            svc.plan_build({'id': planned['id']})
        assert not svc.store.entities('plans')
        assert not svc.store.entities('goals')
    finally:
        svc.shutdown()
