"""Structured continuity records preserve only committed journal evidence."""
import hashlib
import json
import threading

import pytest

from forge_store import encode
from prompt_compiler import durable_checkpoint
from test_forge_core import Engine, call, service


def journal(svc, run, round_number, index, name, args, result):
    identifier = f"{run['id']}:{round_number}:{index}"
    svc.store.invocation(identifier, run['id'], name, args)
    artifact = svc.store.artifact({'name': name, 'arguments': args, 'result': result})
    wrapped = {'artifact': artifact, 'result': result}
    svc.store.invocation_state(identifier, 'completed', wrapped)
    svc.store.tool_message(identifier, index, name, encode(wrapped))
    return identifier, artifact


def prepared_run(svc, tmp_path):
    svc.jobs._launch = lambda run: None
    root = tmp_path / 'project'
    root.mkdir()
    project = svc.create_project({'name': 'Fixture', 'path': str(root)})
    goal = svc.goal_create({'text': 'Build the page.', 'project_id': project['id'], 'tasks': [
        {'id': 'page-task', 'requirement_id': 'page-requirement', 'text': 'Build the page.', 'status': 'pending'}]})
    goal = svc.store.save_goal({**goal, 'checkpoint': 'Page written; tests remain.', 'next_action': 'Run the page tests.'})
    accepted = svc.jobs.start({'text': 'Build and verify the page.', 'mode': 'goal',
                               'project_id': project['id'], 'goal_id': goal['id']})
    # These regressions pin the pre-guided conversational compaction protocol.
    run = svc.store.update_run(accepted['id'], goal_initial={'tasks': goal['tasks']},execution_mode='legacy')
    return run, goal, root


@pytest.mark.parametrize('summary_fail', [False, True])
def test_compaction_records_covered_hashes_decisions_and_evidence(tmp_path, summary_fail):
    svc = service(tmp_path, Engine(summary_fail=summary_fail))
    try:
        run, goal, root = prepared_run(svc, tmp_path)
        args = {'path': 'page.txt', 'content': 'Blue page.'}
        svc.store.add_message(run['chat_id'], 'assistant', '', tool_calls=[call('write_file', args), call('read_file', {'path': 'missing.txt'})])
        result = svc.jobs.registry.execute(run, 'write_file', args, threading.Event())
        written, write_artifact = journal(svc, run, 1, 0, 'write_file', args, result)
        failed, error_artifact = journal(svc, run, 1, 1, 'read_file', {'path': 'missing.txt'}, {'ok': False, 'error': 'File does not exist.'})
        decision = svc.store.add_message(run['chat_id'], 'user', 'Use the blue palette.')
        later_args = {'path': 'later.txt', 'content': 'Uncovered action.'}
        svc.store.add_message(run['chat_id'], 'assistant', '', tool_calls=[call('write_file', later_args)])
        later_result = svc.jobs.registry.execute(run, 'write_file', later_args, threading.Event())
        later, later_artifact = journal(svc, run, 2, 0, 'write_file', later_args, later_result)

        run = svc.jobs._compact(svc.store.run(run['id']), [], {'cancel': threading.Event()}, force=True)
        checkpoint = svc.store.entity('checkpoints', run['continuity_checkpoint_id'])
        assert checkpoint['schema_version'] == 1
        assert checkpoint['through_message_id'] == run['boundary'] == decision['id']
        assert checkpoint['summary_kind'] == ('deterministic' if summary_fail else 'model')
        assert checkpoint['goal_ref']['id'] == goal['id']
        assert checkpoint['goal_ref']['requirement_ids'] == ['page-requirement']
        assert checkpoint['current_work']['next_action'] == 'Run the page tests.'
        assert checkpoint['decision_message_ids'] == [decision['id']]
        assert {a['invocation_id'] for a in checkpoint['actions']} == {written, failed}
        assert checkpoint['changed_files'][0]['sha256'] == hashlib.sha256((root / 'page.txt').read_bytes()).hexdigest()
        assert [f['path'] for f in checkpoint['changed_files']] == ['page.txt']
        assert {e['artifact_id'] for e in checkpoint['evidence_refs']} == {write_artifact, error_artifact}
        assert checkpoint['unresolved_errors'][0]['invocation_id'] == failed
        assert later_artifact not in encode(checkpoint) and later not in encode(checkpoint)
        assert ('Deterministic checkpoint.' in run['summary']) is summary_fail
        read = svc.jobs.registry.execute(run, 'artifact_read', {'id': checkpoint['id'], 'limit': 16000}, threading.Event())
        assert json.loads(read['text']) == checkpoint
        with pytest.raises(ValueError, match='current run'):
            svc.jobs.registry.execute({**run, 'id': 'other-run'}, 'artifact_read', {'id': checkpoint['id']}, threading.Event())

        # The next record links continuity and contains only the newly covered interval.
        run = svc.jobs._compact(run, [], {'cancel': threading.Event()}, force=True)
        following = svc.store.entity('checkpoints', run['continuity_checkpoint_id'])
        assert following['previous_checkpoint_id'] == checkpoint['id']
        assert following['from_message_id'] == decision['id']
        assert [a['invocation_id'] for a in following['actions']] == [later]
        assert [f['path'] for f in following['changed_files']] == ['later.txt']
        assert len(list((svc.store.home / 'artifacts').glob('*.json'))) == 3
    finally:
        svc.shutdown()


def test_dense_checkpoint_is_bounded_and_keeps_durable_references():
    run = {'id': 'run', 'goal_id': 'goal', 'boundary': 2, 'request_message_id': 1}
    actions = [{'id': str(index), 'name': 'write_file', 'status': 'completed', 'message_id': index + 3,
        'arguments': {'content': 'never-copy-raw-content'},
        'result': {'artifact': f'{index:032x}', 'result': {'path': f'page-{index}.txt', 'sha256': 'a' * 64}}}
        for index in range(500)]
    checkpoint = durable_checkpoint(run, [{'id': 502, 'role': 'assistant'}], actions,
                                    {'tasks': []}, maximum_bytes=4096)
    assert len(encode(checkpoint).encode('utf-8')) <= 4096 - 256
    assert checkpoint['truncated'] and checkpoint['counts']['changed_files'] == 500
    assert checkpoint['goal_ref']['id'] == 'goal' and checkpoint['through_message_id'] == 502
    assert 'never-copy-raw-content' not in encode(checkpoint)
    assert checkpoint['evidence_refs']


def test_resolved_identical_error_is_removed_and_failed_writes_have_no_hash():
    actions = [
        {'id': 'failed', 'name': 'write_file', 'status': 'completed', 'arguments': {'path': 'page.txt'},
         'result': {'artifact': 'failed-evidence', 'result': {'ok': False, 'error': 'Conflict.', 'path': 'page.txt', 'sha256': 'a' * 64}}},
        {'id': 'succeeded', 'name': 'write_file', 'status': 'completed', 'arguments': {'path': 'page.txt'},
         'result': {'artifact': 'success-evidence', 'result': {'ok': True, 'path': 'page.txt', 'sha256': 'b' * 64}}},
        {'id': 'failed-other', 'name': 'write_file', 'status': 'outcome_unknown', 'arguments': {'path': 'other.txt'},
         'result': {'result': {'path': 'other.txt', 'sha256': 'c' * 64}}}]
    checkpoint = durable_checkpoint({'id': 'run'}, [{'id': 9, 'role': 'assistant'}], actions)
    assert [e['invocation_id'] for e in checkpoint['unresolved_errors']] == ['failed-other']
    assert checkpoint['changed_files'] == [{'path': 'page.txt', 'sha256': 'b' * 64, 'previous_sha256': None,
        'deleted': False, 'invocation_id': 'succeeded', 'artifact_id': 'success-evidence'}]


def test_checkpoint_save_cancellation_never_advances_boundary(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        run, _, _ = prepared_run(svc, tmp_path)
        svc.store.add_message(run['chat_id'], 'assistant', 'Current progress.')
        cancelled = threading.Event()
        original = svc.store.save_entity
        def save(kind, value):
            saved = original(kind, value)
            if kind == 'checkpoints':
                cancelled.set()
            return saved
        monkeypatch.setattr(svc.store, 'save_entity', save)
        with pytest.raises(ValueError, match='boundary commit'):
            svc.jobs._compact(run, [], {'cancel': cancelled}, force=True)
        retained = svc.store.run(run['id'])
        assert retained.get('boundary', 0) == run.get('boundary', 0)
        assert not retained.get('continuity_checkpoint_id')
        assert not any(e['type'] == 'compacted' for e in svc.store.events(run['id']))
    finally:
        svc.shutdown()


def test_failed_summary_is_not_retried_and_records_actual_covered_evidence(tmp_path):
    class RetryEngine(Engine):
        def __init__(self):
            super().__init__()
            self.attempts = []
        def stream_agent(self, data, cancel):
            if any('continuity summary' in m['content'] for m in data['messages'] if m['role'] == 'system'):
                self.attempts.append(json.loads(next(m['content'] for m in data['messages'] if m['role'] == 'user')))
                if len(self.attempts) == 1:
                    raise ValueError('Summary unavailable.')
                yield {'message': {'content': 'Earlier decisions.'}, 'done': True}
                return
            yield from super().stream_agent(data, cancel)
    engine = RetryEngine()
    svc = service(tmp_path, engine)
    try:
        run, _, _ = prepared_run(svc, tmp_path)
        args = {'path': 'page.txt', 'content': 'Actual bytes.'}
        svc.store.add_message(run['chat_id'], 'assistant', '', tool_calls=[call('write_file', args)])
        result = svc.jobs.registry.execute(run, 'write_file', args, threading.Event())
        _, artifact = journal(svc, run, 1, 0, 'write_file', args, result)
        svc.store.add_message(run['chat_id'], 'user', 'Continue verification.')
        svc.store.add_message(run['chat_id'], 'assistant', 'Checking the page.')
        rows = svc.store.run_chat(run['chat_id'], 0)['messages']
        run = svc.jobs._compact(svc.store.run(run['id']), [], {'cancel': threading.Event()}, force=True)
        checkpoint = svc.store.entity('checkpoints', run['continuity_checkpoint_id'])
        covered = len(engine.attempts[-1]['transcript'])
        assert len(engine.attempts) == 1 and covered == 3
        assert checkpoint['through_message_id'] == run['boundary'] == rows[covered - 1]['id']
        assert checkpoint['summary_kind'] == 'deterministic' and checkpoint['model_summary_requests'] == 1
        assert checkpoint['changed_files'][0]['path'] == 'page.txt'
        assert checkpoint['evidence_refs'][0]['artifact_id'] == artifact
    finally:
        svc.shutdown()


def test_nonzero_command_exit_remains_an_unresolved_error():
    checkpoint = durable_checkpoint({'id': 'run'}, [{'id': 1, 'role': 'assistant'}], [
        {'id': 'test-command', 'name': 'command_wait', 'status': 'completed', 'arguments': {'id': 'command'},
         'result': {'artifact': 'command-evidence', 'result': {'exit_code': 2}}}])
    assert checkpoint['unresolved_errors'][0]['error'] == 'Exit code 2'
