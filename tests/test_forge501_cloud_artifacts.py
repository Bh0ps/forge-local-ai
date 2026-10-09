"""Cloud privacy boundaries use disposable projects, synthetic values and no inference."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from forge_cloud_policy import CloudContextPolicy, CloudPolicyRejected, cloud_artifact_allowed
from forge_collaboration import setup_specialists
from forge_goal_review import GoalReview, assignment_files, invocation_artifacts
from project_tools import ProjectTools
from test_forge50_collaboration import setup


MARKER = 'SYNTHETIC_PRIVATE_RESOURCE_MARKER'


@pytest.fixture
def privacy(tmp_path):
    svc = setup(tmp_path)
    setup_specialists(svc)
    svc.store.update_settings({'permission_profile': 'full_access', 'memory_enabled': False, 'memory_suggestions': False})
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'credentials').mkdir()
    (root / '.env').write_text(MARKER, encoding='utf-8')
    (root / 'app.py').write_text('print("safe public code")\n', encoding='utf-8')
    project = svc.create_project({'name': 'Disposable privacy fixture', 'path': str(root)})
    goal = svc.goal_create({'text': 'Preserve the public API selector #safe-button.', 'project_id': project['id'],
        'tasks': [{'id': 'stable-task', 'text': 'Preserve the public API selector #safe-button.'}]})
    run = svc.store.run(svc.jobs.start({'text': goal['request'], 'goal_id': goal['id'],
        'project_id': project['id'], 'mode': 'goal'})['id'])
    run = svc.store.update_run(run['id'], status='running', execution_mode='legacy')
    goal = svc.store.goal(goal['id'])
    svc.store.save_goal({**goal, 'tasks': [{**task, 'status': 'completed'} for task in goal['tasks']]})
    yield svc, run, root
    assert not svc.core.requests
    svc.shutdown()


def record(svc, run, name, args, result):
    artifact = svc.store.artifact({'name': name, 'arguments': args, 'result': result})
    identifier = run['id'] + ':privacy:' + artifact
    svc.store.invocation(identifier, run['id'], name, args)
    svc.store.invocation_state(identifier, 'completed', {'artifact': artifact, 'result': result})
    return artifact


def file_artifact(privacy, name, args):
    svc, run, root = privacy
    result = ProjectTools(root, svc.store.home / 'backups').execute(name, args, threading.Event())
    assert result.get('ok') is not False, result
    return record(svc, run, name, args, result)


def cloud_run(svc, parent, artifacts):
    accepted = svc.jobs.start({'provider_id': 'openrouter', 'model': 'openrouter/free', 'context': 32768,
        'text': 'Inspect only the assigned synthetic evidence.', 'project_id': parent['project_id'],
        'parent_id': parent['id'], 'goal_id': parent['goal_id'], 'readonly': True, 'mode': 'goal_review',
        'agent_tools': ['artifact_read', 'goal_read'], 'review_artifacts': artifacts,
        'cloud_scope': {'files': [], 'artifacts': artifacts, 'attachments': [], 'goal_id': parent['goal_id'], 'web': False}})
    return svc.store.run(accepted['id'])


def capture_review(svc, run, monkeypatch, answer='The safe public code is implemented.'):
    old_provider = svc.providers.provider
    monkeypatch.setattr(svc.providers, 'provider', lambda identifier: SimpleNamespace(
        capabilities=lambda model: {'capabilities': ['tools', 'structured_outputs']})
        if identifier == 'openrouter' else old_provider(identifier))
    captured = []
    def capture(data):
        captured.append(data)
        raise RuntimeError('OFFLINE_REVIEW_CAPTURE')
    with monkeypatch.context() as patch:
        patch.setattr(svc.jobs, 'start', capture)
        with pytest.raises(RuntimeError, match='OFFLINE_REVIEW_CAPTURE'):
            GoalReview(svc.jobs)._check(run, answer, {'cancel': threading.Event()}, time.monotonic())
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize('name,args', [
    ('read_file', {'path': '.env'}),
    ('write_file', {'path': '.env', 'content': MARKER + ' changed'}),
    ('write_file', {'path': 'config.json', 'content': '{"ordinary":"' + MARKER + '"}'}),
    ('write_file', {'path': 'credentials/team.txt', 'content': MARKER}),
    ('read_files', {'files': [{'path': 'app.py'}, {'path': '.env'}]}),
])
def test_protected_operations_are_excluded_before_review_prompt_and_artifact_scope(privacy, monkeypatch, name, args):
    svc, run, _ = privacy
    private = file_artifact(privacy, name, args)
    file_artifact(privacy, 'read_file', {'path': 'app.py'})
    goal = svc.store.goal(run['goal_id'])
    svc.store.save_goal({**goal, 'checkpoint': MARKER,
        'tasks': [{**task, 'evidence': [MARKER, private]} for task in goal['tasks']]})
    captured = capture_review(svc, run, monkeypatch, answer='Observed local value ' + MARKER)
    assert MARKER not in captured['text']
    assert '#safe-button' in captured['text'] and 'stable-task' in captured['text']
    assert private not in captured['review_artifacts'] and private not in captured['cloud_scope']['artifacts']
    assert 'evidence_exclusions' in captured['text']
    # Filtering never edits or deletes the authoritative local records.
    assert private in {row['artifact'] for row in invocation_artifacts(svc.store, {run['id']}, successful_only=True)}
    assert MARKER in svc.store.read_artifact(private)['text']
    assert svc.store.goal(run['goal_id'])['checkpoint'] == MARKER


@pytest.mark.parametrize('name', ['memory_search', 'memory_export', 'mcp__memory__search'])
def test_private_memory_artifacts_and_echoed_narratives_stay_local(privacy, monkeypatch, name):
    svc, run, _ = privacy
    private = record(svc, run, name, {'query': 'synthetic'}, {'context': MARKER})
    file_artifact(privacy, 'read_file', {'path': 'app.py'})
    captured = capture_review(svc, run, monkeypatch, answer=MARKER)
    assert MARKER not in captured['text'] and private not in captured['review_artifacts']
    assert not cloud_artifact_allowed(svc.store, private, run)


def test_safe_code_and_typed_check_evidence_remain_usable(privacy, monkeypatch):
    svc, run, _ = privacy
    public = file_artifact(privacy, 'read_file', {'path': 'app.py'})
    checked = record(svc, run, 'quality_check', {'scope': 'public-api'},
        {'passed': True, 'checks': [{'name': 'Public API', 'passed': True}], 'complete': True})
    captured = capture_review(svc, run, monkeypatch)
    assert 'safe public code' in captured['text']
    assert 'The safe public code is implemented.' in captured['text']
    assert 'evidence_exclusions' not in captured['text']
    assert {public, checked} <= set(captured['review_artifacts'])
    child = cloud_run(svc, run, captured['review_artifacts'])
    value = svc.jobs.registry.execute(child, 'artifact_read', {'id': public}, threading.Event())
    assert 'safe public code' in value['text']
    messages, _ = CloudContextPolicy(svc).prepare_round(child, [], [])
    assert messages[1]['content'] == child['request']


def test_default_specialist_scope_excludes_named_protected_artifact_but_keeps_public(privacy):
    svc, run, _ = privacy
    private = file_artifact(privacy, 'write_file', {'path': 'config.json', 'content': MARKER})
    public = file_artifact(privacy, 'read_file', {'path': 'app.py'})
    child = svc.store.run(svc.agent_start({'agent_id': 'openrouter-diagnosis', 'parent_id': run['id'],
        'text': 'Diagnose synthetic artifacts ' + private + ' and ' + public})['id'])
    assert private not in child['cloud_scope']['artifacts'] and public in child['cloud_scope']['artifacts']
    with pytest.raises(CloudPolicyRejected):
        svc.jobs.registry.execute(child, 'artifact_read', {'id': private}, threading.Event())
    assert 'safe public code' in svc.jobs.registry.execute(child, 'artifact_read', {'id': public}, threading.Event())['text']


def test_explicit_and_stale_artifact_ids_cannot_bypass_source_exclusion(privacy):
    svc, run, _ = privacy
    private = file_artifact(privacy, 'read_file', {'path': '.env'})
    child = cloud_run(svc, run, [private])
    policy = CloudContextPolicy(svc)
    with pytest.raises(CloudPolicyRejected): policy.execute_scoped(child, 'artifact_read', {'id': private})
    with pytest.raises(CloudPolicyRejected, match='No cloud request was sent'):
        policy.prepare_round(child, [], [])


@pytest.mark.parametrize('override', ['tool', 'project'])
def test_artifact_read_rechecks_late_source_permission_changes(privacy, override):
    svc, run, _ = privacy
    public = file_artifact(privacy, 'read_file', {'path': 'app.py'})
    child = cloud_run(svc, run, [public])
    policy = CloudContextPolicy(svc)
    assert 'safe public code' in policy.execute_scoped(child, 'artifact_read', {'id': public})['text']
    binding = 'tool:read_file' if override == 'tool' else 'project:' + run['project_id']
    svc.store.update_settings({'permission_overrides': {binding: 'deny_access'}})
    with pytest.raises(CloudPolicyRejected): policy.execute_scoped(child, 'artifact_read', {'id': public})
    with pytest.raises(CloudPolicyRejected): policy.prepare_round(child, [], [])


def test_nested_artifacts_and_returned_old_tool_context_recheck_provenance(privacy):
    svc, run, _ = privacy
    private = file_artifact(privacy, 'read_file', {'path': '.env'})
    wrapper = svc.store.artifact({'artifact_id': private, 'text': 'Synthetic wrapper'})
    child = cloud_run(svc, run, [wrapper])
    with pytest.raises(CloudPolicyRejected):
        CloudContextPolicy(svc).execute_scoped(child, 'artifact_read', {'id': wrapper})
    # A saved result from an older assignment is checked even when the current
    # scope has no references and the source message is below compaction.
    old = cloud_run(svc, run, [])
    record(svc, old, 'artifact_read', {'id': private}, {'text': svc.store.read_artifact(private)['text']})
    old = svc.store.update_run(old['id'], boundary=99999)
    with pytest.raises(CloudPolicyRejected, match='Saved cloud tool context'):
        CloudContextPolicy(svc).prepare_round(old, [], [])


def test_cloud_goal_read_preserves_contracts_and_excludes_generated_private_prose(privacy):
    svc, run, _ = privacy
    private = file_artifact(privacy, 'read_file', {'path': '.env'})
    goal = svc.store.goal(run['goal_id'])
    svc.store.save_goal({**goal, 'checkpoint': MARKER, 'blockers': MARKER,
        'tasks': [{**task, 'evidence': [MARKER, private]} for task in goal['tasks']]})
    child = cloud_run(svc, run, [])
    value = CloudContextPolicy(svc).execute_scoped(child, 'goal_read', {})
    assert MARKER not in json.dumps(value)
    assert value['tasks'][0]['id'] == 'stable-task' and '#safe-button' in value['tasks'][0]['text']


@pytest.mark.parametrize('source', ['parent', 'consumed-local-child'])
def test_known_automatic_memory_contribution_keeps_echoed_answer_local_without_changing_preferences(privacy, monkeypatch, source):
    svc, run, _ = privacy
    public = file_artifact(privacy, 'read_file', {'path': 'app.py'})
    svc.store.update_settings({'memory_enabled': True, 'memory_suggestions': True})
    if source == 'parent':
        svc.store.update_run(run['id'], private_memory_supplied=True,
            context_snapshot={'memory_provenance': {'count': 1, 'digest': 'a'*64}})
    else:
        child = svc.jobs.start({'text': 'Inspect the public code.', 'project_id': run['project_id'],
            'parent_id': run['id'], 'goal_id': run['goal_id'], 'provider_id': 'ollama'})
        svc.store.update_run(child['id'], status='completed', private_memory_supplied=True, result_consumed=True)
    # The caller's original run object is intentionally stale; snapshot must
    # obtain the actual persisted provenance rather than guessing from settings.
    assert not run.get('private_memory_supplied')
    goal = svc.store.goal(run['goal_id'])
    svc.store.save_goal({**goal, 'tasks': [{**task, 'evidence': [MARKER, public]} for task in goal['tasks']]})
    captured = capture_review(svc, run, monkeypatch, answer='Memory recalled ' + MARKER)
    assert MARKER not in captured['text'] and 'private_memory_contributed' in captured['text']
    assert '#safe-button' in captured['text'] and 'stable-task' in captured['text']
    assert public in captured['review_artifacts']
    assert svc.store.get_settings()['memory_enabled'] and svc.store.get_settings()['memory_suggestions']


@pytest.mark.parametrize('tool', ['run_command', 'command_start', 'command_read', 'command_wait'])
def test_opaque_command_artifacts_stay_local_but_exit_evidence_is_preserved(privacy, monkeypatch, tool):
    svc, run, _ = privacy
    args={'argv':['python','-c','read the synthetic .env'], 'cwd':'.'} if tool in ('run_command','command_start') else {'id':'synthetic-command'}
    private=record(svc,run,tool,args,{'status':'completed','exit_code':0,'output':MARKER,
        'argv':['python','-c','read the synthetic .env'],'cwd':'.'})
    captured=capture_review(svc,run,monkeypatch,answer='Command printed '+MARKER)
    assert MARKER not in captured['text'] and private not in captured['review_artifacts']
    packet=json.loads(captured['text'].split('\n',1)[1])
    typed=json.loads(next(item['result'] for item in packet['evidence'] if item['tool']==tool))
    assert typed['raw_output_excluded'] is True and typed['exit_code']==0 and typed['status']=='completed'
    child=cloud_run(svc,run,[private])
    with pytest.raises(CloudPolicyRejected):
        CloudContextPolicy(svc).execute_scoped(child,'artifact_read',{'id':private})


def test_known_private_local_child_result_artifact_is_excluded_but_its_code_remains_usable(privacy, monkeypatch):
    svc,run,root=privacy
    child=svc.jobs.start({'text':'Inspect public code.','project_id':run['project_id'],
        'parent_id':run['id'],'goal_id':run['goal_id'],'provider_id':'ollama'})
    svc.store.add_message(child['chat_id'],'assistant',MARKER)
    child=svc.store.update_run(child['id'],status='completed',private_memory_supplied=True,result_consumed=True)
    public_result=ProjectTools(root,svc.store.home/'backups').execute('read_file',{'path':'app.py'})
    public=record(svc,child,'read_file',{'path':'app.py'},public_result)
    returned=svc.agent_result(run,{'run_id':child['id'],'wait_seconds':0},threading.Event())
    private=record(svc,run,'agent_result',{'run_id':child['id']},returned)
    captured=capture_review(svc,run,monkeypatch,answer=MARKER)
    assert MARKER not in captured['text'] and private not in captured['review_artifacts']
    assert public in captured['review_artifacts'] and 'safe public code' in captured['text']
    assert not cloud_artifact_allowed(svc.store,private,run,svc.jobs.registry.permission)


def test_known_memory_flag_is_not_complete_taint_tracking_for_intentionally_scoped_code(privacy):
    svc,run,_=privacy
    run=svc.store.update_run(run['id'],private_memory_supplied=True)
    public=file_artifact(privacy,'write_file',{'path':'accepted-output.txt','content':'Explicit project output '+MARKER})
    # Resource allowlisting intentionally permits selected project deliverables.
    # It does not claim semantic taint tracking of every generated byte.
    child=cloud_run(svc,run,[public])
    assert cloud_artifact_allowed(svc.store,public,child,svc.jobs.registry.permission)
    assert MARKER in CloudContextPolicy(svc).execute_scoped(child,'artifact_read',{'id':public})['text']


def test_real_multi_file_patch_sources_are_reviewed_and_external_edit_invalidates_receipt(privacy, monkeypatch):
    svc,run,root=privacy
    args={'changes':[{'path':'added.py','expected_sha256':'missing','content':'print("new public file")\n'},
                     {'path':'second.py','expected_sha256':'missing','content':'print("second public file")\n'}]}
    artifact=file_artifact(privacy,'apply_patch',args)
    assert set(assignment_files(svc.store,{run['id']}))=={'added.py','second.py'}
    run=svc.store.update_run(run['id'],execution_mode='guided')
    captured=capture_review(svc,run,monkeypatch)
    assert set(captured['cloud_scope']['files'])=={'added.py','second.py'}
    snapshot=svc.store.run(run['id'])['review_candidate']['workflow_snapshot']
    assert {item['path'] for item in snapshot['source_snapshot']['files']}=={'added.py','second.py'}
    workflow=svc.jobs.workflow
    verified=workflow._store_review_receipts(run,snapshot,[{'task_id':'stable-task','evidence_ids':[artifact]}],
        'independent_review','offline-review-fixture','Synthetic structured review accepted current sources.')
    assert workflow.progress(verified)['verified_task_ids']==['stable-task']
    (root/'added.py').write_text('print("external edit after review")\n',encoding='utf-8')
    stale=workflow.refresh_freshness(verified)
    assert workflow.progress(stale)['verified_task_ids']==[]
    assert any(receipt.get('invalidated') for receipt in stale['verification_feedback']['receipts'].values())


def test_real_move_and_restore_sources_include_missing_origin_and_current_hashes(privacy):
    svc,run,root=privacy
    tools=ProjectTools(root,svc.store.home/'backups')
    (root/'origin.txt').write_text('Move this public file',encoding='utf-8')
    result=tools.execute('move_file',{'source':'origin.txt','destination':'destination.txt'})
    assert result['ok']
    record(svc,run,'move_file',{'source':'origin.txt','destination':'destination.txt'},result)
    (root/'restored.txt').write_text('Original public version',encoding='utf-8')
    changed=tools.execute('write_file',{'path':'restored.txt','content':'Changed public version'})
    restored=tools.execute('restore_file',{'path':'restored.txt','backup_id':changed['backup_id']})
    assert restored['ok'] and (root/'restored.txt').read_text(encoding='utf-8')=='Original public version'
    record(svc,run,'restore_file',{'path':'restored.txt','backup_id':changed['backup_id']},restored)
    assert set(assignment_files(svc.store,{run['id']}))=={'origin.txt','destination.txt','restored.txt'}
    snapshot=svc.jobs.workflow.review_snapshot(run,assignment_files(svc.store,{run['id']}))
    manifest={item['path']:item['sha256'] for item in snapshot['source_snapshot']['files']}
    assert manifest['origin.txt']=='missing'
    assert manifest['destination.txt']!='missing' and manifest['restored.txt']!='missing'


def test_application_json_paths_are_data_and_do_not_change_artifact_resource_scope(privacy):
    svc,run,_=privacy
    content=json.dumps({'routes':[{'path':'/home','text':'A public route'}],
        'documentation_example':{'path':'.env','content':'A literal filename example'}})
    written=file_artifact(privacy,'write_file',{'path':'routes-data.json','content':content})
    read=file_artifact(privacy,'read_file',{'path':'routes-data.json'})
    child=cloud_run(svc,run,[written,read])
    policy=CloudContextPolicy(svc)
    for artifact in (written,read):
        assert cloud_artifact_allowed(svc.store,artifact,child,svc.jobs.registry.permission)
        assert '/home' in policy.execute_scoped(child,'artifact_read',{'id':artifact})['text']
    policy.prepare_round(child,[],[])
