"""Real coordinator rounds exercise durable questions and safe steering."""
import json
import threading
import time

import pytest

from test_forge_core import Engine, call, finished, service


QUESTIONS = {'questions': [{'id': 'approach', 'header': 'Approach', 'question': 'Which approach should I use?',
    'options': [{'label': 'Small change', 'description': 'Preserve the current design.'},
                {'label': 'Refactor', 'description': 'Replace the parser.'}]}]}


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('Expected coordinator state was not reached.')


def pending(svc, run):
    return wait_for(lambda: svc.dispatch('pending_questions', {'run_id': run['id']})['questions'])


def test_question_waits_without_inference_and_answer_is_once_and_not_permission(tmp_path):
    folder = tmp_path / 'project'; folder.mkdir()
    engine = Engine([{'tool_calls': [call('request_user_input', QUESTIONS)]},
                     {'tool_calls': [call('write_file', {'path': 'answer.txt', 'content': 'selected'})]}, {'content': 'Done.'}])
    svc = service(tmp_path, engine)
    try:
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        run = svc.jobs.start({'text': 'Ask me questions as you go.', 'project_id': project['id']})
        question = pending(svc, run)[0]
        wait_for(lambda: svc.store.run(run['id'])['status'] == 'waiting_question')
        assert len(engine.requests) == 1
        assert question['items'][0]['options'][0]['recommended']
        reply = {'question_id': question['id'], 'answers': {'approach': {'option': 'Small change'}}}
        assert svc.dispatch('answer_question', reply)['status'] == 'answered'
        approval = wait_for(lambda: next((e for e in svc.jobs.poll(run['id'])['events'] if e['type'] == 'approval'), None))
        assert not (folder / 'answer.txt').exists()
        svc.dispatch('answer_question', reply)
        assert sum(m.get('question_id') == question['id'] for m in svc.store.get_chat(run['chat_id'])['messages']) == 1
        assert any('Small change' in m['content'] for m in engine.requests[1]['messages'] if m['role'] == 'user')
        svc.dispatch('approve', {'run_id': run['id'], 'approval_id': approval['approval_id'], 'allowed': True})
        assert finished(svc, run)['status'] == 'completed'
        assert (folder / 'answer.txt').read_text() == 'selected'
    finally:
        svc.shutdown()


def test_question_survives_pause_and_restart_without_reasking_or_repeating_tool(tmp_path):
    svc = service(tmp_path, Engine([{'tool_calls': [call('request_user_input', QUESTIONS)]}]))
    try:
        run = svc.jobs.start({'text': 'Ask for my preference.'})
        question = pending(svc, run)[0]
        wait_for(lambda: svc.store.run(run['id'])['status'] == 'waiting_question')
        svc.jobs.cancel(run['id'], pause=True)
        assert finished(svc, run)['status'] == 'paused'
        assert not svc.store.unknown_actions(run['id'])
    finally:
        svc.shutdown()
    restored = service(tmp_path, Engine([{'content': 'I will use your choice.'}]))
    try:
        assert restored.dispatch('pending_questions', {'run_id': run['id']})['questions'][0]['id'] == question['id']
        restored.dispatch('answer_question', {'question_id': question['id'], 'answers': {'approach': {'text': 'Keep Unicode 🌍 supported.'}}})
        resumed = restored.jobs.resume(run['id'])
        assert finished(restored, resumed)['status'] == 'completed'
        assert len(restored.store.entities('questions')) == 1
        assert not restored.store.unknown_actions(run['id'])
        assert any('Unicode 🌍' in m['content'] for m in restored.core.requests[0]['messages'])
    finally:
        restored.shutdown()


def test_plan_can_ask_and_mixed_followup_action_does_not_run_before_answer(tmp_path):
    folder = tmp_path / 'project'; folder.mkdir()
    engine = Engine([{'tool_calls': [call('request_user_input', QUESTIONS),
                                   call('write_file', {'path': 'wrong.txt', 'content': 'no'})]},
                     {'content': '1. Inspect the parser.\n2. Preserve the selected approach.'}])
    svc = service(tmp_path, engine)
    try:
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        run = svc.command({'text': '/plan Ask which design I prefer.', 'project_id': project['id']})
        question = pending(svc, run)[0]
        assert svc.store.entities('plans') == []
        svc.dispatch('answer_question', {'question_id': question['id'], 'answers': {'approach': {'option': 'Refactor'}}})
        assert finished(svc, run)['status'] == 'completed'
        assert not (folder / 'wrong.txt').exists()
        assert svc.store.entities('plans')[0]['status'] == 'ready'
    finally:
        svc.shutdown()


@pytest.mark.parametrize('answers', [{}, {'approach': {'option': 'Invented'}},
    {'approach': {'text': ''}}, {'approach': {'option': 'Refactor', 'text': 'both'}},
    {'approach': {'text': 'x' * 6001}}, {'approach': {'text': 'valid', 'allowed': True}}])
def test_invalid_answers_leave_question_pending(tmp_path, answers):
    svc = service(tmp_path, Engine([{'tool_calls': [call('request_user_input', QUESTIONS)]}]))
    try:
        run = svc.jobs.start({'text': 'Ask me.'}); question = pending(svc, run)[0]
        with pytest.raises(ValueError):
            svc.dispatch('answer_question', {'question_id': question['id'], 'answers': answers})
        assert svc.dispatch('pending_questions', {'run_id': run['id']})['questions'][0]['status'] == 'pending'
    finally:
        svc.shutdown()


def test_steer_interrupts_generation_keeps_partial_and_never_executes_partial_call(tmp_path):
    generating = threading.Event()
    def slow(data, cancel):
        yield {'message': {'content': 'Original partial response.', 'tool_calls': [call('write_file', {'path': 'wrong.txt', 'content': 'wrong'})]}, 'done': False}
        generating.set()
        assert cancel.wait(8)
    engine = Engine([slow, {'content': 'Updated response.'}])
    svc = service(tmp_path, engine)
    try:
        folder = tmp_path / 'project'; folder.mkdir()
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        svc.store.update_settings({'permission_profile': 'full_access'})
        run = svc.jobs.start({'text': 'Original request.', 'project_id': project['id']})
        assert generating.wait(8)
        reply = svc.dispatch('run_steer', {'run_id': run['id'], 'text': 'Use the smaller change instead.', 'client_id': 'once'})
        assert reply['status'] == 'queued'
        assert finished(svc, run)['status'] == 'completed'
        assert not (folder / 'wrong.txt').exists()
        assert svc.store.run(run['id'])['request'] == 'Original request.'
        assert any(m['role'] == 'user' and m['content'] == 'Use the smaller change instead.' for m in engine.requests[1]['messages'])
        messages = svc.store.get_chat(run['chat_id'])['messages']
        assert sum(m['content'] == 'Original partial response.' for m in messages) == 1
        assert sum(m['content'] == 'Use the smaller change instead.' for m in messages) == 1
        replay = svc.get_interaction().queue_steer(run['id'], 'Use the smaller change instead.', 'once')
        assert replay['id'] == reply['id'] and replay['status'] == 'applied'
        with pytest.raises(ValueError):
            svc.dispatch('run_steer', {'run_id': run['id'], 'text': 'Too late.'})
    finally:
        svc.shutdown()


def test_steer_waits_for_started_action_then_skips_remainder(tmp_path, monkeypatch):
    folder = tmp_path / 'project'; folder.mkdir()
    started = threading.Event(); release = threading.Event(); executions = []
    engine = Engine([{'tool_calls': [call('write_file', {'path': 'first.txt', 'content': 'first'}),
                                   call('write_file', {'path': 'second.txt', 'content': 'second'})]}, {'content': 'Changed direction.'}])
    svc = service(tmp_path, engine)
    original = svc.jobs.registry.execute
    def execute(run, name, args, cancel, **kwargs):
        if name == 'write_file':
            executions.append(args['path']); started.set()
            assert release.wait(8)
        return original(run, name, args, cancel, **kwargs)
    monkeypatch.setattr(svc.jobs.registry, 'execute', execute)
    try:
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        svc.store.update_settings({'permission_profile': 'full_access'})
        run = svc.jobs.start({'text': 'Create files.', 'project_id': project['id']})
        assert started.wait(8)
        svc.dispatch('run_steer', {'run_id': run['id'], 'text': 'Keep only the first file.'})
        release.set()
        assert finished(svc, run)['status'] == 'completed'
        assert executions == ['first.txt']
        assert (folder / 'first.txt').exists() and not (folder / 'second.txt').exists()
        assert not svc.store.unknown_actions(run['id'])
    finally:
        release.set(); svc.shutdown()


def test_steer_supersedes_pending_approval_without_authorizing_edit(tmp_path):
    folder = tmp_path / 'project'; folder.mkdir()
    svc = service(tmp_path, Engine([{'tool_calls': [call('write_file', {'path': 'wrong.txt', 'content': 'wrong'})]}, {'content': 'Okay.'}]))
    try:
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        run = svc.jobs.start({'text': 'Write a file.', 'project_id': project['id']})
        wait_for(lambda: svc.store.run(run['id'])['status'] == 'awaiting_approval')
        svc.dispatch('run_steer', {'run_id': run['id'], 'text': 'Do not edit. Explain instead.'})
        assert finished(svc, run)['status'] == 'completed'
        assert not (folder / 'wrong.txt').exists()
        with svc.store._connection() as db:
            assert db.execute('SELECT status FROM approvals WHERE run_id=?', (run['id'],)).fetchone()[0] == 'superseded'
    finally:
        svc.shutdown()


def test_steer_dismisses_question_and_delete_removes_interaction_content(tmp_path):
    svc = service(tmp_path, Engine([{'tool_calls': [call('request_user_input', QUESTIONS)]}, {'content': 'Using your direction.'}]))
    try:
        run = svc.jobs.start({'text': 'Ask me.'}); question = pending(svc, run)[0]
        svc.dispatch('run_steer', {'run_id': run['id'], 'text': 'Use the small change; continue.'})
        assert finished(svc, run)['status'] == 'completed'
        assert svc.store.entity('questions', question['id'])['status'] == 'dismissed'
        svc.dispatch('chat_delete', {'id': run['chat_id']})
        assert svc.store.entities('questions') == [] and svc.store.entities('steers') == []
        assert svc.dispatch('pending_questions', {})['questions'] == []
    finally:
        svc.shutdown()


def test_start_and_resume_return_the_same_durable_request_identity(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
    try:
        started = svc.jobs.start({'text': 'Keep the exact request.'})
        assert started['request_message_id'] == svc.store.run(started['id'])['request_message_id']
        svc.store.update_run(started['id'], status='paused')
        resumed = svc.jobs.resume(started['id'])
        assert resumed['request_message_id'] == started['request_message_id']
        assert resumed['request'] == started['request']
    finally:
        svc.shutdown()


def test_latest_exact_steer_and_answers_survive_compaction_boundary(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(svc.jobs, '_launch', lambda run: None)
    try:
        run = svc.jobs.start({'text': 'Original objective.'})
        invocation = run['id'] + ':1:0'
        svc.store.invocation(invocation, run['id'], 'request_user_input', QUESTIONS)
        result = svc.get_interaction().ask(svc.store.run(run['id']), QUESTIONS, invocation)
        svc.store.tool_message(invocation, 0, 'request_user_input', json.dumps(result))
        svc.get_interaction().publish_pending(run['id'])
        svc.get_interaction().answer(result['question_id'], {'approach': {'text': 'Preserve accents é and Unicode 🌍.'}})
        svc.get_interaction().queue_steer(run['id'], 'Keep the existing API exactly.', 'retained')
        svc.get_interaction().apply_steers(run['id'])
        rows = svc.store.get_chat(run['chat_id'])['messages']
        accepted = [m['content'] for m in rows if m.get('interaction_run_id') == run['id']]
        compacted = svc.store.update_run(run['id'], boundary=rows[-1]['id'], summary='A deliberately incomplete summary.')
        context = svc.jobs._context(compacted, [])
        assert [m['content'] for m in context if m['role'] == 'user'] == ['Original objective.', *accepted]
    finally:
        svc.shutdown()
