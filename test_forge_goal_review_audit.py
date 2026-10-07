"""Independent goal-review acceptance and trust-boundary fixtures, fully offline."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from forge_goal_review import (GoalReview, REVIEW_TOOLS, RESPONSE_FORMAT,
                               invocation_artifacts, parse_verdict)
from test_forge_core import Engine, call, finished, service


TASKS = [{'id': 'first', 'text': 'Inspect implementation.', 'status': 'completed'},
         {'id': 'second', 'text': 'Verify results.', 'status': 'completed'}]


def verdict(tasks=TASKS, evidence='fixture-evidence', status='complete', feedback=None):
    return {'verdict': status, 'summary': 'Evidence supports the requested work.' if status == 'complete' else 'Verification needs a repair.',
            'feedback': feedback or ([] if status == 'complete' else ['Inspect the missing implementation evidence.']),
            'verified_tasks': [{'task_id': task['id'], 'evidence_ids': [evidence]} for task in tasks]}


class ReviewerFixture:
    remote_inference = True

    def __init__(self, responses):
        self.responses = list(responses); self.requests = []

    def capabilities(self, model):
        return {'capabilities': ['tools', 'structured_outputs'], 'model_info': {'fixture.context_length': 262144}}

    def estimate_tokens(self, messages, tools):
        return 100

    def generate(self, data, cancel):
        self.requests.append(deepcopy(data))
        response=data.get('response_format')
        assert response and response['type']==RESPONSE_FORMAT['type'] and response['json_schema']['strict']
        assert response['json_schema']['schema']['required']==RESPONSE_FORMAT['json_schema']['schema']['required']
        response = self.responses.pop(0)
        if callable(response):
            yield from response(data, cancel)
            return
        if isinstance(response, BaseException):
            raise response
        yield {'message': response, 'done': True, 'done_reason': 'stop',
               'eval_count': 11, 'prompt_eval_count': 200, 'eval_duration': 1_000_000_000}


def configure(svc, reviewer):
    svc.store.save_entity('providers', {'id': 'openrouter', 'kind': 'openrouter',
        'url': 'https://openrouter.ai/api/v1', 'enabled': True, 'remote_consent': True,
        'credential_ref': 'synthetic-review-fixture'})
    previous = svc.providers.provider
    svc.providers.provider = lambda identifier: reviewer if identifier == 'openrouter' else previous(identifier)
    svc.store.update_settings({'goal_review_enabled': True, 'permission_profile': 'full_access', 'context': 32768})


def mark_complete(svc):
    def response(data, cancel):
        goal = svc.store.entities('goals')[0]
        tasks = [{**task, 'status': 'completed', 'evidence': ['Fixture progress.']} for task in goal['tasks']]
        yield {'message': {'tool_calls': [call('goal_update', {'tasks': tasks,
            'checkpoint': 'Fixture tasks completed.', 'next_action': 'Review evidence.'})]},
            'done': True, 'done_reason': 'stop', 'eval_count': 8, 'prompt_eval_count': 150}
    return response


def reviewer_complete(svc, evidence=None):
    def response(data, cancel):
        goal = svc.store.entities('goals')[0]
        child = next(run for run in svc.store.runs() if run.get('mode') == 'goal_review' and run['status'] == 'running')
        artifact_ids = [record['artifact'] for record in invocation_artifacts(svc.store, {child['id']}, successful_only=True) if record['artifact']]
        identifier = evidence or artifact_ids[-1]
        yield {'message': {'content': json.dumps(verdict(goal['tasks'], identifier))},
            'done': True, 'done_reason': 'stop', 'eval_count': 11, 'prompt_eval_count': 200}
    return response


def suspended_goal(svc):
    svc.jobs._launch = lambda run: None
    goal = svc.goal_create({'text': 'Inspect implementation.\nVerify results.'})
    accepted = svc.jobs.start({'text': 'Start the original objective.', 'goal_id': goal['id'], 'mode': 'goal'})
    goal = svc.store.goal(goal['id'])
    svc.store.save_goal({**goal, 'tasks': [{**task, 'status': 'completed'} for task in goal['tasks']], 'status': 'running'})
    return svc.store.update_run(accepted['id'], rounds=2, status='running')


def record(svc, run, name='read_file', result=None, status='completed'):
    artifact = svc.store.artifact({'fixture': result or {'content': 'Verified fixture.'}})
    identifier = run['id'] + ':audit:' + str(len(svc.store.events(run['id']))) + ':' + artifact
    svc.store.invocation(identifier, run['id'], name, {})
    svc.store.invocation_state(identifier, status, {'artifact': artifact, 'result': result or {'content': 'Verified fixture.'}})
    return artifact


def test_valid_complete_verdict_requires_every_known_task():
    assert parse_verdict(json.dumps(verdict()), TASKS, {'fixture-evidence'})['verdict'] == 'complete'


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(verdict='done'),
    lambda v: v.update(summary=None),
    lambda v: v.update(summary=''),
    lambda v: v.update(extra='ignore'),
    lambda v: v.update(feedback=['More changes are required.']),
    lambda v: v.update(verified_tasks=[]),
    lambda v: v['verified_tasks'].pop(),
    lambda v: v['verified_tasks'][0].update(task_id='invented'),
    lambda v: v['verified_tasks'][1].update(task_id='first'),
    lambda v: v['verified_tasks'][0].update(evidence_ids=['unrelated-artifact']),
    lambda v: v['verified_tasks'][0].update(evidence_ids=[]),
    lambda v: v['verified_tasks'][0].update(evidence_ids='fixture-evidence'),
    lambda v: v['verified_tasks'][0].update(extra=True),
    lambda v: v.update(verdict='needs_changes', feedback=[]),
    lambda v: v.update(verdict='insufficient_evidence', feedback=[]),
])
def test_malformed_or_unsupported_verdict_never_approves(mutation):
    value = verdict(); mutation(value)
    with pytest.raises(ValueError):
        parse_verdict(json.dumps(value), TASKS, {'fixture-evidence'})


@pytest.mark.parametrize('text', [
    '{"verdict":"needs_changes","verdict":"complete","summary":"x","feedback":[],"verified_tasks":[]}',
    '{"verdict":"complete","summary":NaN,"feedback":[],"verified_tasks":[]}',
    '```json\n' + json.dumps(verdict()) + '\n```',
    json.dumps(verdict()) + '\nAdditional approval.',
    '',
])
def test_duplicate_fields_nonfinite_or_wrapped_json_never_approves(text):
    with pytest.raises(ValueError):
        parse_verdict(text, TASKS, {'fixture-evidence'})


def test_empty_checklist_cannot_be_verified():
    with pytest.raises(ValueError, match='every task'):
        parse_verdict(json.dumps(verdict([])), [], {'fixture-evidence'})


@pytest.mark.parametrize('identity', [None, '', '  ', 123, False, {'invented': 'identity'}, 'first'])
def test_missing_nonstring_or_duplicate_task_identity_cannot_approve(identity):
    tasks = deepcopy(TASKS); tasks[1]['id'] = identity
    with pytest.raises(ValueError, match='unique and nonempty'):
        parse_verdict(json.dumps(verdict(TASKS[:1])), tasks, {'fixture-evidence'})


@pytest.mark.parametrize('field', ['summary', 'feedback'])
def test_blank_summary_or_feedback_is_not_a_concrete_verdict(field):
    value = verdict(status='needs_changes')
    value[field] = ' \n\t' if field == 'summary' else [' \n\t']
    with pytest.raises(ValueError):
        parse_verdict(json.dumps(value), TASKS, {'fixture-evidence'})


def test_snapshot_retains_original_steps_when_local_agent_drops_one(tmp_path):
    svc = service(tmp_path)
    try:
        run = suspended_goal(svc)
        initial = deepcopy(run['goal_initial'])
        goal = svc.store.goal(run['goal_id'])
        svc.store.save_goal({**goal, 'tasks': goal['tasks'][:1]})
        _, packet, _, _ = GoalReview(svc.jobs).snapshot(run, 'All work finished.')
        assert packet['initial'] == initial
        assert len(packet['initial']['tasks']) == 2 and len(packet['tasks']) == 1
        assert packet['initial']['request'] == 'Inspect implementation.\nVerify results.'
    finally:
        svc.shutdown()


def test_reviewer_retrieval_is_limited_to_supplied_and_own_successful_artifacts(tmp_path):
    svc = service(tmp_path)
    try:
        main = suspended_goal(svc)
        supplied = record(svc, main)
        unrelated = svc.jobs.start({'text': 'Unrelated isolated chat.'})
        forbidden = record(svc, unrelated)
        child = svc.jobs.start({'text': 'Review only the supplied evidence.', 'parent_id': main['id'],
            'goal_id': main['goal_id'], 'mode': 'goal_review', 'readonly': True,
            'review_artifacts': [supplied], 'agent_tools': list(REVIEW_TOOLS)})
        child = svc.store.run(child['id'])
        own = record(svc, child)
        for artifact in (supplied, own):
            assert svc.jobs.registry.execute(child, 'artifact_read', {'id': artifact}, threading.Event())['artifact'] == artifact
        with pytest.raises(ValueError, match='outside the goal review'):
            svc.jobs.registry.execute(child, 'artifact_read', {'id': forbidden}, threading.Event())
    finally:
        svc.shutdown()


def test_error_denied_and_unknown_tool_records_are_not_successful_evidence(tmp_path):
    svc = service(tmp_path)
    try:
        run = suspended_goal(svc)
        good = record(svc, run)
        for result in ({'error': 'Denied.'}, {'not_executed': True}, {'cancelled': True},
                       {'timed_out': True}, {'outcome_unknown': True}):
            record(svc, run, result=result)
        record(svc, run, status='outcome_unknown')
        assert [row['artifact'] for row in invocation_artifacts(svc.store, {run['id']}, successful_only=True)] == [good]
    finally:
        svc.shutdown()


def test_goal_journal_and_memory_claims_are_not_completion_proof(tmp_path):
    svc = service(tmp_path)
    try:
        run = suspended_goal(svc)
        for name in ('goal_update', 'goal_read', 'memory_propose', 'memory_search', 'search_memory', 'request_user_input'):
            record(svc, run, name=name, result={'claim': 'All implementation works.'})
        assert invocation_artifacts(svc.store, {run['id']}, successful_only=True) == []
    finally:
        svc.shutdown()


def test_external_checklist_edits_and_live_children_prevent_review(tmp_path):
    svc = service(tmp_path)
    try:
        run = suspended_goal(svc)
        child = svc.jobs.start({'text': 'Research evidence.', 'parent_id': run['id'], 'goal_id': run['goal_id'], 'readonly': True})
        with pytest.raises(ValueError, match='Wait for delegated agents'):
            GoalReview(svc.jobs).snapshot(run, 'Done.')
        svc.store.update_run(child['id'], status='completed')
        path = Path(svc.store.goal(run['goal_id'])['path'])
        path.write_text(path.read_text(encoding='utf-8') + '\nExternal change.\n', encoding='utf-8')
        with pytest.raises(ValueError, match='externally edited'):
            GoalReview(svc.jobs).snapshot(run, 'Done.')
    finally:
        svc.shutdown()


def test_failed_review_child_does_not_overwrite_parent_goal_checkpoint(tmp_path):
    svc = service(tmp_path)
    try:
        reviewer = ReviewerFixture([ValueError('Synthetic unavailable free capacity.')]); configure(svc, reviewer)
        run = suspended_goal(svc)
        previous = svc.store.goal(run['goal_id'])
        from types import MethodType
        from forge_runs import RunManager
        svc.jobs._launch = MethodType(RunManager._launch, svc.jobs)
        child = svc.jobs.start({'text': 'Review evidence.', 'parent_id': run['id'], 'goal_id': run['goal_id'],
            'provider_id': 'openrouter', 'model': 'openrouter/free', 'mode': 'goal_review', 'readonly': True,
            'memory_enabled': False, 'memory_suggestions': False, 'agent_tools': list(REVIEW_TOOLS)})
        assert finished(svc, child)['status'] == 'paused'
        current = svc.store.goal(run['goal_id'])
        assert current['status'] == previous['status'] == 'running'
        assert current['checkpoint'] == previous['checkpoint']
    finally:
        svc.shutdown()


def test_actual_reviewer_reads_project_and_usage_is_separate_once(tmp_path):
    svc = service(tmp_path)
    try:
        folder = tmp_path / 'project'; folder.mkdir()
        (folder / 'result.txt').write_text('Expected implementation.\n', encoding='utf-8')
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        reviewer = ReviewerFixture([{'tool_calls': [call('read_file', {'path': 'result.txt'})]}, reviewer_complete(svc)])
        configure(svc, reviewer)
        svc.core.rounds = [mark_complete(svc), {'content': 'Implementation completed.'}]
        goal = svc.goal_create({'text': 'Inspect implementation.\nVerify results.', 'project_id': project['id']})
        run = svc.goal_resume({'id': goal['id']})
        result = finished(svc, run)
        assert result['status'] == 'completed', result
        saved = svc.store.goal(goal['id'])
        assert saved['review']['verdict'] == 'complete' and saved['status'] == 'completed'
        children = [child for child in svc.store.runs() if child.get('parent_id') == run['id']]
        assert len(children) == 1 and children[0]['mode'] == 'goal_review'
        assert not children[0]['settings']['memory_enabled'] and not children[0]['settings']['auto_delegate']
        assert not children[0].get('active_skills')
        allowed = {schema['function']['name'] for schema in reviewer.requests[0]['tools']}
        assert 'read_file' in allowed and allowed <= set(REVIEW_TOOLS)
        with svc.store._connection() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM usage')]
        assert len([row for row in rows if row['purpose'] == 'goal_review']) == 2
        assert len([row for row in rows if row['purpose'] == 'main']) == 2
    finally:
        svc.shutdown()


def test_unknown_evidence_pauses_main_and_never_completes_goal(tmp_path):
    svc = service(tmp_path)
    try:
        def bad(data, cancel):
            tasks = svc.store.entities('goals')[0]['tasks']
            yield {'message': {'content': json.dumps(verdict(tasks, 'invented-artifact'))},
                'done': True, 'done_reason': 'stop', 'eval_count': 11}
        reviewer = ReviewerFixture([bad]); configure(svc, reviewer)
        svc.core.rounds = [mark_complete(svc), {'content': 'Completed.'}]
        goal = svc.goal_create({'text': 'Inspect implementation.'})
        run = svc.goal_resume({'id': goal['id']})
        result = finished(svc, run)
        assert result['status'] == 'paused' and 'missing evidence' in result['recovery']
        assert svc.store.goal(goal['id'])['status'] != 'completed'
        assert not any(event['type'] == 'complete' for event in result['events'])
        assert len(reviewer.requests) == 1
    finally:
        svc.shutdown()


def test_pause_cancels_cloud_review_and_preserves_parent_request(tmp_path):
    svc = service(tmp_path); entered = threading.Event(); stopped = threading.Event()
    def stalled(data, cancel):
        entered.set()
        while not cancel.wait(.01):
            pass
        stopped.set()
        if False: yield {}
    try:
        reviewer = ReviewerFixture([stalled]); configure(svc, reviewer)
        svc.core.rounds = [mark_complete(svc), {'content': 'Completed.'}]
        goal = svc.goal_create({'text': 'Inspect implementation.'})
        run = svc.goal_resume({'id': goal['id']})
        assert entered.wait(3)
        original = svc.store.run(run['id'])['request']
        svc.jobs.cancel(run['id'], pause=True)
        assert finished(svc, run)['status'] == 'paused' and stopped.wait(2)
        assert svc.store.run(run['id'])['request'] == original
        assert svc.store.goal(goal['id'])['status'] != 'completed'
        assert len(reviewer.requests) == 1
    finally:
        svc.shutdown()


def completed_review_child(svc, run, answer, status='complete'):
    reviewer = GoalReview(svc.jobs)
    goal, packet, fingerprint, artifacts = reviewer.snapshot(run, answer)
    child = svc.jobs.start({'text': 'Review the fixture evidence.', 'parent_id': run['id'],
        'goal_id': run['goal_id'], 'provider_id': 'openrouter', 'model': 'openrouter/free',
        'mode': 'goal_review', 'readonly': True, 'agent_tools': list(REVIEW_TOOLS),
        'memory_enabled': False, 'memory_suggestions': False})
    evidence = artifacts[-1] if artifacts else packet['candidate_answer']['id']
    value = verdict(goal['tasks'], evidence, status)
    svc.store.add_message(child['chat_id'], 'assistant', json.dumps(value))
    svc.store.update_run(child['id'], status='completed')
    pending = {'fingerprint': fingerprint, 'answer': answer, 'review_run_id': child['id'],
        'attempt': 1, 'evidence_artifact': svc.store.artifact(packet), 'verdict': value,
        'revision': 1, 'review': {'status': status, 'attempt': 1, 'review_run_id': child['id']}}
    return svc.store.update_run(run['id'], review_candidate=pending, review_pending=True,
        review_attempt=1, status='paused'), child


def test_completed_verdict_survives_restart_without_local_or_cloud_generation(tmp_path):
    svc = service(tmp_path)
    try:
        configure(svc, ReviewerFixture([]))
        run = suspended_goal(svc)
        record(svc, run, name='write_file', result={'written': True, 'content': 'Finished fixture.'})
        run, child = completed_review_child(svc, svc.store.run(run['id']), 'Finished fixture.')
        original = run['request']; initial = deepcopy(run['goal_initial'])
    finally:
        svc.shutdown()
    restored = service(tmp_path)
    try:
        cloud = ReviewerFixture([]); configure(restored, cloud)
        resumed = restored.jobs.resume(run['id'])
        assert finished(restored, resumed)['status'] == 'completed'
        assert not cloud.requests and not restored.core.requests
        assert restored.store.run(run['id'])['request'] == original
        assert restored.store.run(run['id'])['goal_initial'] == initial
        assert len([item for item in restored.store.runs() if item.get('parent_id') == run['id']]) == 1
        with restored.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE name='write_file'").fetchone()[0] == 1
        assert restored.store.goal(run['goal_id'])['status'] == 'completed'
    finally:
        restored.shutdown()


def test_feedback_application_can_replay_crash_window_without_duplicate_task(tmp_path):
    svc = service(tmp_path)
    try:
        configure(svc, ReviewerFixture([]))
        run = suspended_goal(svc)
        run, child = completed_review_child(svc, run, 'Needs a fixture repair.', 'needs_changes')
        initial = deepcopy(run['goal_initial'])
        pending = deepcopy(run['review_candidate'])
        reviewer = GoalReview(svc.jobs)
        assert reviewer.check(run, pending['answer'], {'cancel': threading.Event()}, time.monotonic(), resume=True) == 'continue'
        once = svc.store.goal(run['goal_id'])
        assert len(once['tasks']) == 3 and svc.store.run(run['id'])['review_revisions'] == 1
        # Reproduce a journal replay after TODO.md was replaced but before the
        # run's applied marker reached durable storage.
        replay = svc.store.update_run(run['id'], review_pending=True, review_candidate=pending, review_revisions=0)
        assert reviewer.check(replay, pending['answer'], {'cancel': threading.Event()}, time.monotonic(), resume=True) == 'continue'
        twice = svc.store.goal(run['goal_id'])
        assert [task['id'] for task in twice['tasks']] == [task['id'] for task in once['tasks']]
        assert svc.store.run(run['id'])['review_revisions'] == 1
        assert svc.store.run(run['id'])['goal_initial'] == initial
        assert [task['text'] for task in twice['tasks'][:2]] == [task['text'] for task in initial['tasks']]
    finally:
        svc.shutdown()


def test_parent_steering_invalidates_finished_review_before_application(tmp_path):
    svc = service(tmp_path)
    try:
        configure(svc, ReviewerFixture([]))
        run = suspended_goal(svc)
        run, _ = completed_review_child(svc, run, 'Previously completed fixture.')
        svc.store.add_message(run['chat_id'], 'user', 'Also verify the Unicode path.', interaction_run_id=run['id'])
        outcome = GoalReview(svc.jobs).check(svc.store.run(run['id']), 'Previously completed fixture.',
            {'cancel': threading.Event()}, time.monotonic(), resume=True)
        assert outcome == 'continue'
        assert not svc.store.run(run['id'])['review_pending']
        assert svc.store.goal(run['goal_id'])['status'] != 'completed'
    finally:
        svc.shutdown()


def test_review_resume_cannot_reactivate_memory_or_delegation(tmp_path):
    svc = service(tmp_path)
    try:
        configure(svc, ReviewerFixture([]))
        main = suspended_goal(svc)
        child = svc.jobs.start({'text': 'Read-only review.', 'parent_id': main['id'], 'goal_id': main['goal_id'],
            'mode': 'goal_review', 'provider_id': 'openrouter', 'model': 'openrouter/free', 'readonly': True,
            'memory_enabled': False, 'memory_suggestions': False, 'auto_delegate': False,
            'agent_tools': list(REVIEW_TOOLS)})
        svc.store.update_run(child['id'], status='paused')
        svc.jobs.resume(child['id'], {'memory_enabled': True, 'memory_suggestions': True,
            'auto_delegate': True, 'provider_id': 'ollama', 'model': 'fixture'})
        child = svc.store.run(child['id'])
        assert not child['settings']['memory_enabled'] and not child['settings']['memory_suggestions']
        assert not child['settings']['auto_delegate'] and child['settings']['provider_id'] == 'openrouter'
        schemas = svc.jobs.registry.schemas(child, ['tools'])
        assert {schema['function']['name'] for schema in schemas} <= set(REVIEW_TOOLS)
    finally:
        svc.shutdown()


def test_review_admission_crash_reuses_child_from_durable_intent(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        remote = ReviewerFixture([]); configure(svc, remote)
        main = suspended_goal(svc)
        reviewer = GoalReview(svc.jobs)
        original = svc.jobs.start
        launched = []
        def crash_after_admission(data):
            accepted = original(data); launched.append(accepted)
            raise RuntimeError('Synthetic crash after child request admission.')
        monkeypatch.setattr(svc.jobs, 'start', crash_after_admission)
        with pytest.raises(RuntimeError, match='Synthetic crash'):
            reviewer.check(main, 'Text-only implementation verified.', {'cancel': threading.Event()}, time.monotonic())
        saved = svc.store.run(main['id'])
        assert saved['review_pending'] and saved['review_candidate']['attempt'] == 1
        assert not saved['review_candidate'].get('review_run_id')
        assert len(launched) == 1
        child = svc.store.run(launched[0]['id'])
        value = verdict(svc.store.goal(main['goal_id'])['tasks'], 'answer:' + main['id'] + ':' + str(main['rounds']))
        svc.store.add_message(child['chat_id'], 'assistant', json.dumps(value))
        svc.store.update_run(child['id'], status='completed')
        monkeypatch.setattr(svc.jobs, 'start', original)
        assert reviewer.check(svc.store.run(main['id']), 'Text-only implementation verified.',
            {'cancel': threading.Event()}, time.monotonic(), resume=True) == 'complete'
        children = [run for run in svc.store.runs() if run.get('parent_id') == main['id']]
        assert len(children) == 1 and children[0]['id'] == child['id']
        assert not remote.requests and not svc.core.requests
    finally:
        svc.shutdown()


def test_disabled_review_does_not_resume_a_cloud_request(tmp_path):
    svc = service(tmp_path)
    try:
        remote = ReviewerFixture([]); configure(svc, remote)
        main = suspended_goal(svc)
        main, child = completed_review_child(svc, main, 'Candidate work finished.')
        svc.store.update_run(child['id'], status='paused')
        svc.store.update_settings({'goal_review_enabled': False})
        assert GoalReview(svc.jobs).check(svc.store.run(main['id']), 'Candidate work finished.',
            {'cancel': threading.Event()}, time.monotonic(), resume=True) == 'continue'
        assert not svc.store.run(main['id'])['review_pending'] and not remote.requests
        assert svc.store.run(child['id'])['status'] == 'paused'
        assert svc.store.goal(main['goal_id'])['review']['status'] == 'disabled'
    finally:
        svc.shutdown()


def test_goal_tools_survive_small_project_context_loading(tmp_path):
    svc = service(tmp_path)
    try:
        folder = tmp_path / 'project'; folder.mkdir()
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        svc.jobs._launch = lambda run: None
        goal = svc.goal_create({'text': 'Inspect implementation.', 'project_id': project['id']})
        accepted = svc.jobs.start({'text': 'Implement the saved goal.', 'goal_id': goal['id'], 'project_id': project['id']})
        run = svc.store.run(accepted['id'])
        assert run['settings']['context'] == 8192
        names = {schema['function']['name'] for schema in svc.jobs.registry.schemas(run, ['tools'])}
        assert {'goal_read', 'goal_update', 'artifact_read'} <= names
    finally:
        svc.shutdown()


def test_review_budget_pause_retains_valid_verdict_for_explicit_resume(tmp_path):
    svc = service(tmp_path)
    try:
        remote = ReviewerFixture([]); configure(svc, remote)
        main = suspended_goal(svc)
        main, _ = completed_review_child(svc, main, 'Text-only work completed.')
        svc.store.record_usage({'id': 'audit-budget-request', 'run_id': main['id'],
            'provider': 'ollama', 'model': 'fixture', 'purpose': 'main', 'output_tokens': 11})
        settings = {**main['settings'], 'goal_limits': {'minutes': 60, 'tokens': 10, 'rounds': 128, 'tools': 256}}
        main = svc.store.update_run(main['id'], settings=settings)
        with pytest.raises(ValueError, match='Shared tokens limit.*review is saved'):
            GoalReview(svc.jobs).check(main, 'Text-only work completed.', {'cancel': threading.Event()}, time.monotonic(), resume=True)
        saved = svc.store.run(main['id'])
        assert saved['review_pending'] and saved['review_candidate']['verdict']['verdict'] == 'complete'
        assert not saved['review_candidate'].get('invalid')
        svc.store.update_run(main['id'], status='paused')
        from types import MethodType
        from forge_runs import RunManager
        svc.jobs._launch = MethodType(RunManager._launch, svc.jobs)
        resumed = svc.jobs.resume(main['id'])
        assert finished(svc, resumed)['status'] == 'completed'
        assert not remote.requests and not svc.core.requests
        assert len([run for run in svc.store.runs() if run.get('parent_id') == main['id']]) == 1
    finally:
        svc.shutdown()


def test_crash_after_complete_verdict_before_parent_completion_never_repeats_write(tmp_path, monkeypatch):
    svc = service(tmp_path)
    try:
        folder = tmp_path / 'project'; folder.mkdir()
        project = svc.create_project({'name': 'Fixture', 'path': str(folder)})
        remote = ReviewerFixture([{'tool_calls': [call('read_file', {'path': 'result.txt'})]}, reviewer_complete(svc)])
        configure(svc, remote)
        svc.core.rounds = [
            {'tool_calls': [call('write_file', {'path': 'result.txt', 'content': 'Write once and verify.'})]},
            mark_complete(svc), {'content': 'Created and verified result.txt.'},
        ]
        goal = svc.goal_create({'text': 'Create result.txt with the fixture output.', 'project_id': project['id']})
        previous_save = svc.store.save_goal
        crashed = []
        def crash_on_completion(data, **kwargs):
            if data.get('status') == 'completed' and data.get('review', {}).get('status') == 'complete' and not crashed:
                crashed.append(True)
                raise ValueError('Synthetic interruption before parent completion commit.')
            return previous_save(data, **kwargs)
        monkeypatch.setattr(svc.store, 'save_goal', crash_on_completion)
        run = svc.goal_resume({'id': goal['id']})
        result = finished(svc, run)
        assert result['status'] == 'paused' and 'Synthetic interruption' in result['recovery']
        assert svc.store.run(run['id'])['review_pending']
        assert svc.store.run(run['id'])['review_candidate']['verdict']['verdict'] == 'complete'
        counts = (len(svc.core.requests), len(remote.requests))
        monkeypatch.setattr(svc.store, 'save_goal', previous_save)
        resumed = svc.jobs.resume(run['id'])
        assert finished(svc, resumed)['status'] == 'completed'
        assert not svc.store.run(run['id'])['review_pending']
        assert (len(svc.core.requests), len(remote.requests)) == counts
        assert (folder / 'result.txt').read_text(encoding='utf-8') == 'Write once and verify.'
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE name='write_file'").fetchone()[0] == 1
        assert len([child for child in svc.store.runs() if child.get('parent_id') == run['id']]) == 1
    finally:
        svc.shutdown()
