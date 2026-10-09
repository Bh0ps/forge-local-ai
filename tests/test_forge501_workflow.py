"""Stable identities and coordinator-guided continuity use real isolated journals."""
from copy import deepcopy
import json

import pytest

from agent_runtime import tool_schema
from prompt_compiler import verification_outcome, verification_packet
from test_forge5_checkpoints import journal
from test_forge_core import service
from tool_routing import wire_schemas
from forge_workflow import verification_sources


def checks(labels=('A','B'),passed=True,**extra):
    return {'passed':passed,'checks':[{'requirement':name,'passed':passed} for name in labels],**extra}


def fixture(tmp_path):
    svc=service(tmp_path); svc.jobs._launch=lambda run:None
    svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,'memory_suggestions':False})
    root=tmp_path/'project'; root.mkdir(); project=svc.create_project({'name':'Work packets','path':str(root)})
    goal=svc.goal_create({'project_id':project['id'],'text':'Implement exact public API','tasks':[{'text':'Implement selector data-add and total()','status':'pending','evidence':[]}]})
    accepted=svc.jobs.start({'goal_id':goal['id'],'project_id':project['id'],'mode':'goal','text':'Implement exact public API'})
    run=svc.store.run(accepted['id']); task=svc.store.goal(goal['id'])['tasks'][0]
    schema=tool_schema('quality_check','Registered public API check',{'scope':{'type':'string'}})
    schema['capability']='read'; schema['verification_contract']={'id':'public-api-v1','scope_keys':['scope'],'expected_checks':['A','B'],'task_ids':[task['id']],'scope_path':'.'}
    return svc,run,goal,task,schema


def observe(svc,run,round_number,result,schema,args=None,name='quality_check'):
    args=args or {'scope':'component'}
    run=svc.store.update_run(run['id'],rounds=round_number)
    invocation,artifact=journal(svc,run,round_number,0,name,args,result)
    svc.jobs._check_verification(run,[(name,args,{'artifact':artifact,'result':result})],{name:schema})
    return svc.store.run(run['id']),invocation,artifact


def test_registered_identity_clears_shortened_early_failure(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        run,failed,_=observe(svc,run,1,checks(('Required file is inspectable',),False),schema)
        assert verification_packet(run)
        run,passed,_=observe(svc,run,2,checks(),schema)
        assert verification_packet(run) is None
        assert next(iter(run['verification_feedback']['receipts'].values()))['complete']
        run=svc.jobs.workflow.refresh(run)
        updated=svc.store.goal(goal['id'])['tasks'][0]
        assert updated['id']==task['id'] and updated['status']=='completed'
        assert 'verification:'+passed in updated['evidence']
        assert svc.jobs.workflow.completion_issues(run)==[]
        assert run['workflow']['phase']=='report' and run['progress_summary']['verified']==1
    finally: svc.shutdown()


@pytest.mark.parametrize('result',[checks(('A',)),checks(stale=True),checks(unavailable=True),checks(partial=True),checks(available=False)])
def test_partial_unavailable_or_stale_success_cannot_clear_failure(tmp_path,result):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        run,_,_=observe(svc,run,1,checks(('File missing',),False),schema)
        run,_,_=observe(svc,run,2,result,schema)
        assert verification_packet(run)
        svc.jobs.workflow.refresh(run)
        assert svc.store.goal(goal['id'])['tasks'][0]['status']=='pending'
    finally: svc.shutdown()


def test_unrelated_contract_or_scope_does_not_supersede(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        run,_,_=observe(svc,run,1,checks(('File missing',),False),schema)
        run,_,_=observe(svc,run,2,checks(),schema,{'scope':'other'})
        assert verification_packet(run)
        other=deepcopy(schema); other['verification_contract']['id']='different-contract'
        run,_,_=observe(svc,run,3,checks(),other)
        assert verification_packet(run)
    finally: svc.shutdown()


def test_pass_receipt_becomes_stale_after_effect_or_steering(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        run,_,_=observe(svc,run,1,checks(),schema)
        run=svc.jobs.workflow.refresh(run)
        assert svc.jobs.workflow.completion_issues(run)==[]
        svc.jobs._reset_verification_progress(run['id'])
        run=svc.jobs.workflow.refresh(svc.store.run(run['id']))
        assert 'current complete registered verification' in svc.jobs.workflow.completion_issues(run)[0]
        run,_,_=observe(svc,run,2,checks(),schema)
        assert svc.jobs.workflow.completion_issues(run)==[]
    finally: svc.shutdown()


def test_goal_progress_preserves_ids_order_and_requirements(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        prior=svc.store.goal(goal['id'])
        current=svc.store.update_goal_progress(goal['id'],{'tasks':[{'text':task['text'],'status':'completed','evidence':['Explicit fixture review']}]})
        assert current['tasks'][0]['id']==task['id']
        assert current['scope_revision']==prior['scope_revision']
        assert current['revision']>prior['revision']
        for updates in ([{'text':'Replace original work','status':'completed'}],[{'id':task['id'],'text':'New text','status':'completed'}],[{'id':'unknown','status':'completed'}]):
            with pytest.raises(ValueError): svc.store.update_goal_progress(goal['id'],{'tasks':updates})
        assert svc.store.goal(goal['id'])['tasks']==current['tasks']
        assert current['requirement_history'][0]['tasks'][0]['id']==task['id']
    finally: svc.shutdown()


def test_targeted_task_update_revision_conflict_and_ambiguity(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        current=svc.store.goal(goal['id']); revision=current['revision']
        result=svc.store.update_goal_task(goal['id'],task['id'],revision,'in_progress',[], 'Inspecting public selector')
        assert result['tasks'][0]['status']=='in_progress'
        with pytest.raises(ValueError,match='Goal changed'): svc.store.update_goal_task(goal['id'],task['id'],revision,'completed',[])
        second=svc.store.save_goal({**result,'tasks':result['tasks']+[{'id':'second','text':task['text'],'status':'pending'}]})
        with pytest.raises(ValueError,match='ambiguous'): svc.store.update_goal_progress(goal['id'],{'tasks':[{'text':task['text'],'status':'completed'}]})
        assert second['scope_revision']==current['scope_revision']+1
        assert len(second['requirement_history'])==2
    finally: svc.shutdown()


def test_saved_receipts_and_work_packets_survive_restart_without_replay(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    run,invocation,artifact=observe(svc,run,1,checks(),schema)
    run=svc.jobs.workflow.refresh(run); packet=deepcopy(run['work_packet']); svc.shutdown()
    replacement=service(tmp_path)
    try:
        restored=replacement.jobs.workflow.refresh(replacement.store.run(run['id']))
        assert restored['work_packet']['identity']==packet['identity']
        assert replacement.jobs.workflow.completion_issues(restored)==[]
        with replacement.store._connection() as db:
            row=db.execute('SELECT status,result FROM invocations WHERE id=?',(invocation,)).fetchone()
            assert row['status']=='completed' and json.loads(row['result'])['artifact']==artifact
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==1
    finally: replacement.shutdown()


def test_old_runs_without_execution_mode_keep_legacy_behavior(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        legacy=svc.store.update_run(run['id'],execution_mode=None)
        assert not svc.jobs.workflow.enabled(legacy)
        assert svc.jobs.workflow.context(legacy) is None
        assert svc.jobs.workflow.completion_issues(legacy)==[]
    finally: svc.shutdown()


def test_guided_compaction_reconstructs_checkpoint_without_model_summary(tmp_path):
    import threading
    from test_forge_core import call
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        svc.jobs.workflow.refresh(run)
        svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[call('quality_check',{'scope':'component'})])
        run,_,_=observe(svc,run,1,checks(('Missing file',),False),schema)
        svc.store.add_message(run['chat_id'],'user','Preserve the public data-add selector.')
        svc.store.add_message(run['chat_id'],'assistant','Inspecting the missing file.')
        run=svc.jobs._compact(run,[],{'cancel':threading.Event()},force=True)
        checkpoint=svc.store.entity('checkpoints',run['continuity_checkpoint_id'])
        assert checkpoint['summary_kind']=='deterministic' and checkpoint['model_summary_requests']==0
        assert verification_packet(run)
        packet=svc.jobs.workflow.refresh(run)['work_packet']
        assert packet['task']['id']==task['id'] and 'data-add' in packet['task']['text']
    finally: svc.shutdown()


def test_model_receipt_fields_cannot_register_or_complete_contract():
    value=checks(('A',),contract_id='invented',complete=True,task_ids=['forged'])
    outcome=verification_outcome('quality_check',{},value)
    assert 'schema_version' not in outcome and 'task_ids' not in outcome
    schema=tool_schema('quality_check','Check',{}); schema['verification_contract']={'id':'private','task_ids':['sensitive']}
    assert 'verification_contract' not in wire_schemas([schema])[0]
    assert 'sensitive' not in json.dumps(wire_schemas([schema]))


def test_external_source_change_invalidates_receipt_without_a_tool_effect(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        root=tmp_path/'project'; (root/'component.js').write_text('original')
        run,_,_=observe(svc,run,1,checks(),schema)
        run=svc.jobs.workflow.refresh(run)
        original_version=run['verification_feedback']['version']
        assert svc.jobs.workflow.completion_issues(run)==[]
        (root/'component.js').write_text('external edit')
        issues=svc.jobs.workflow.completion_issues(run)
        assert issues and 'current complete' in issues[0]
        assert svc.store.run(run['id'])['verification_feedback']['version']==original_version
        run,_,_=observe(svc,svc.store.run(run['id']),2,checks(),schema)
        assert svc.jobs.workflow.completion_issues(run)==[]
    finally: svc.shutdown()


def test_source_hash_bounds_traversal_and_permissions(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        assert not verification_sources(svc.store,run,{'required_sources':['../private.txt']})['available']
        (tmp_path/'project'/'source.txt').write_text('important')
        denied=verification_sources(svc.store,run,{'required_sources':['source.txt']},lambda path:False)
        assert not denied['available'] and 'permissions' in denied['reason']
        valid=verification_sources(svc.store,run,{'required_sources':['source.txt','missing.txt']},lambda path:True)
        assert valid['available'] and valid['files'][0]['sha256']=='missing'
        oversized=tmp_path/'project'/'large.bin'
        with oversized.open('wb') as file: file.truncate(8_000_001)
        assert not verification_sources(svc.store,run,{'required_sources':['large.bin']})['available']
    finally: svc.shutdown()


def test_unambiguous_legacy_failure_reconciles_only_with_registered_full_pass(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        old=deepcopy(schema); old.pop('verification_contract')
        run,invocation,_=observe(svc,run,1,checks(('Missing file is inspectable',),False),old)
        run,_,_=observe(svc,run,2,checks(),schema)
        assert verification_packet(run) is None
        assert any(event.get('migration')=='registered_contract' and event.get('cleared_invocation_id')==invocation for event in svc.store.events(run['id']))
    finally: svc.shutdown()


def test_builder_gate_contract_covers_only_explicit_gate_mapping():
    contract={'id':'builder-check-v1','builder_id':'builder','result_adapter':'builder_gate','scope_keys':['id','gate'],
        'tasks_by_gate':{'functional':['functional-task']},'requirements_by_gate':{'functional':['interaction-requirement']}}
    args={'id':'builder','gate':'functional','expected_revision':3}
    evidence={'builder_id':'builder','brief_revision':3,'scope_sha256':'a'*64}
    record={'id':'builder','revision':3,'gates':{'functional':{'status':'passed','evidence':[evidence],'fingerprint':'a'*64}}}
    receipt=verification_outcome('builder_check',args,record,contract)
    assert receipt['complete'] and receipt['task_ids']==['functional-task'] and receipt['requirement_ids']==['interaction-requirement']
    assert verification_outcome('builder_check',{**args,'expected_revision':2},record,contract) is None
    assert verification_outcome('builder_check',{**args,'gate':'build'},record,contract) is None
    assert verification_outcome('builder_check',args,record) is None
    assert verification_outcome('builder_check',args,record,{**contract,'builder_id':'another-builder'}) is None


def test_contract_check_multiplicity_prevents_shortened_pass():
    contract={'id':'schema-v1','expected_checks':['Valid row','Schema rejects invalid row','Schema rejects invalid row']}
    partial=verification_outcome('quality_check',{},checks(('Valid row','Schema rejects invalid row')),contract)
    full=verification_outcome('quality_check',{},checks(('Valid row','Schema rejects invalid row','Schema rejects invalid row')),contract)
    assert not partial['complete'] and full['complete']


def test_accepted_requirement_revision_needs_fresh_full_successor_and_retains_audit(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        run,original,artifact=observe(svc,run,1,checks(('File missing',),False),schema)
        prior=svc.store.goal(goal['id'])
        updated=svc.store.save_goal({**prior,'request':'Accepted public API correction','tasks':[{**prior['tasks'][0],'text':'Implement corrected public data-add and total()'}]},reconcile=True)
        assert updated['scope_revision']==prior['scope_revision']+1
        run,_,_=observe(svc,run,2,checks(('A',)),schema)
        assert verification_packet(run)
        run,_,_=observe(svc,run,3,checks(stale=True),schema)
        assert verification_packet(run)
        run,new,_=observe(svc,run,4,checks(),schema)
        assert verification_packet(run) is None
        event=next(v for v in svc.store.events(run['id']) if v.get('reason')=='scope_revised')
        assert event['superseded_invocation_id']==original and event['superseded_artifact_id']==artifact
        assert event['invocation_id']==new and event['scope_revision']==updated['scope_revision']
        run=svc.jobs.workflow.refresh(run)
        assert svc.store.goal(goal['id'])['tasks'][0]['status']=='completed'
        assert svc.jobs.workflow.completion_issues(run)==[]
        assert any(r.get('superseded') for r in run['verification_feedback']['receipts'].values())
        with svc.store._connection() as db: assert db.execute('SELECT status FROM invocations WHERE id=?',(original,)).fetchone()[0]=='completed'
    finally:svc.shutdown()


def test_progress_cannot_forge_scope_history_or_drop_requirements(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        original=svc.store.goal(goal['id'])
        for request in ({'requirement_history':[]},{'scope_revision':100},{'tasks':[{'id':task['id'],'text':'different requirement','status':'completed'}]},
                {'tasks':[{'id':task['id'],'status':'completed','origin':'review'}]}):
            with pytest.raises(ValueError):svc.store.update_goal_progress(goal['id'],request)
        unchanged=svc.store.update_goal_progress(goal['id'],{'tasks':[]})
        assert unchanged['tasks']==original['tasks']
        assert unchanged['scope_revision']==original['scope_revision'] and unchanged['requirement_history']==original['requirement_history']
    finally:svc.shutdown()


def test_paused_unknown_effect_offers_inspection_before_resume(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        invocation=run['id']+':1:0'
        svc.store.invocation(invocation,run['id'],'write_file',{'path':'result.txt','content':'unknown effect'})
        svc.store.invocation_state(invocation,'outcome_unknown',{'error':'Process interrupted.'})
        run=svc.store.update_run(run['id'],status='paused',recovery='Inspect the saved effect before resuming.')
        assert svc.jobs.workflow.progress(run)['available_actions']==['inspect_interruption']
        with pytest.raises(ValueError,match='Inspect and reconcile'):
            svc.jobs.workflow.accept_human(run,[task['id']],svc.store.goal(goal['id'])['revision'],'I accept the result.')
        with pytest.raises(ValueError,match='outcome-unknown'):svc.jobs.resume(run['id'])
        with svc.store._connection() as db:assert db.execute('SELECT status FROM invocations WHERE id=?',(invocation,)).fetchone()[0]=='outcome_unknown'
    finally:svc.shutdown()


def test_guided_narrative_evidence_is_candidate_not_verified_completion(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        current=svc.store.goal(goal['id'])
        svc.store.update_goal_task(goal['id'],task['id'],current['revision'],'completed',['The file exists and everything passed.'])
        assert 'narrative evidence' in svc.jobs.workflow.completion_issues(run)[0]
        assert svc.jobs.workflow.completion_issues(run,for_review=True)==[]
        assert svc.jobs.workflow.progress(run)['verified_task_ids']==[]
        legacy=svc.store.update_run(run['id'],execution_mode='legacy')
        assert svc.jobs.workflow.completion_issues(legacy)==[]
    finally:svc.shutdown()


def test_direct_human_acceptance_is_typed_and_stale_sources_need_new_review(tmp_path):
    from project_tools import ProjectTools
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        path=tmp_path/'project'/'component.js';path.write_text('Reviewed content')
        read=ProjectTools(tmp_path/'project',svc.store.home/'backups').read_file('component.js')
        read_schema=next(item for item in ProjectTools.schemas() if item['function']['name']=='read_file')
        run,_,artifact=observe(svc,run,1,read,read_schema,args={'path':'component.js'},name='read_file')
        current=svc.store.goal(goal['id'])
        run=svc.jobs.workflow.accept_human(run,[task['id']],current['revision'],'I inspected this component and accepted its public API.',[artifact])
        receipts=run['verification_feedback']['receipts'].values()
        assert any(value['producer_type']=='human_review' and value['complete'] for value in receipts)
        assert svc.jobs.workflow.completion_issues(run)==[]
        assert svc.jobs.workflow.progress(run)['verified_task_ids']==[task['id']]
        assert svc.store.goal(goal['id'])['tasks'][0]['status']=='completed'
        path.write_text('External change after acceptance')
        assert svc.jobs.workflow.completion_issues(run)
        assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']),for_review=True)==[]
        assert not any(s['function']['name']=='goal_task_accept' for s in svc.jobs.registry.schemas(run,['tools'],all_tools=True))
        with pytest.raises(ValueError,match='Goal changed'):svc.jobs.workflow.accept_human(run,[task['id']],current['revision'],'stale acceptance')
    finally:svc.shutdown()


def test_review_admission_cannot_bypass_known_partial_or_stale_checks(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        current=svc.store.goal(goal['id']);svc.store.update_goal_task(goal['id'],task['id'],current['revision'],'completed',['A narrative claim.'])
        run,_,_=observe(svc,run,1,checks(('A',)),schema)
        assert svc.jobs.workflow.completion_issues(run,for_review=True)
        run,_,_=observe(svc,run,2,checks(),schema)
        assert not svc.jobs.workflow.completion_issues(run,for_review=True)
        svc.jobs._reset_verification_progress(run['id'])
        assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']),for_review=True)
    finally:svc.shutdown()


def test_paused_guided_goals_remain_visible_for_acceptance(tmp_path):
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        current=svc.store.goal(goal['id']);svc.store.save_goal({**current,'chat_id':run['chat_id']})
        svc.store.update_run(run['id'],status='paused')
        listed=svc.store.active_goals()
        assert listed[0]['id']==goal['id'] and listed[0]['execution_mode']=='guided'
        svc.store.update_run(run['id'],execution_mode='legacy')
        assert svc.store.active_goals()==[]
    finally:svc.shutdown()


def test_poll_status_events_and_cursor_share_one_read_snapshot(tmp_path,monkeypatch):
    import threading
    from contextlib import contextmanager
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        svc.store.finish_run(run['id'],{'status':'paused'}, {'status':'paused','paused':True})
        original=svc.store._connection; triggered=[]; failures=[]
        class SnapshotConnection:
            def __init__(self,db):self.db=db
            def execute(self,sql,parameters=()):
                cursor=self.db.execute(sql,parameters)
                if sql=='SELECT data FROM runs WHERE id=?' and not triggered:
                    triggered.append(True)
                    def finish():
                        try:svc.store.finish_run(run['id'],{'status':'completed'},{'status':'completed'})
                        except Exception as exc:failures.append(exc)
                    thread=threading.Thread(target=finish);thread.start();thread.join(3)
                    assert not thread.is_alive()
                return cursor
        @contextmanager
        def connection(*,transaction=None):
            with original(transaction=transaction) as db:yield SnapshotConnection(db) if transaction=='read' else db
        monkeypatch.setattr(svc.store,'_connection',connection)
        before=svc.jobs.poll(run['id'])
        assert triggered and not failures and before['status']=='paused'
        assert [event['status'] for event in before['events'] if event['type']=='done']==['paused']
        assert not before['has_more']
        after=svc.jobs.poll(run['id'],before['next_cursor'])
        assert after['status']=='completed' and after['finished']
        assert after['events'][-1]['status']=='completed' and not after['has_more']
    finally:svc.shutdown()


def test_real_structured_independent_review_creates_task_receipts_for_text(tmp_path):
    from test_forge_goal_review import environment,complete_tasks,verdict
    from test_forge_core import finished
    svc,remote=environment(tmp_path,lambda data,cancel:verdict(data))
    try:
        goal=svc.goal_create({'text':'Compose a greeting.','tasks':[{'text':'Compose a short greeting.'}]})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello, welcome to Forge.'}]
        started=svc.jobs.start({'text':'Compose the requested greeting.','mode':'goal','goal_id':goal['id']})
        assert finished(svc,started)['status']=='completed'
        run=svc.store.run(started['id']);receipts=list(run['verification_feedback']['receipts'].values())
        receipt=next(value for value in receipts if value.get('producer_type')=='independent_review')
        assert receipt['task_ids']==[goal['tasks'][0]['id']] and receipt['evidence_ids'][0].startswith('answer:'+run['id'])
        assert svc.jobs.workflow.completion_issues(run)==[]
        assert svc.jobs.workflow.progress(run)['verified_task_ids']==receipt['task_ids']
        assert len(remote.requests)==1
    finally:svc.shutdown()


@pytest.mark.parametrize('decision',['insufficient_evidence','invalid_complete'])
def test_partial_or_invalid_independent_review_never_creates_completion_receipt(tmp_path,decision):
    from test_forge_goal_review import environment,complete_tasks,verdict
    from test_forge_core import finished
    def reply(data,cancel):
        if decision=='invalid_complete':return json.dumps({'verdict':'complete','summary':'No task coverage.','feedback':[],'verified_tasks':[]})
        return verdict(data,'insufficient_evidence',feedback=['The output needs actual inspection.'])
    svc,remote=environment(tmp_path,reply)
    try:
        goal=svc.goal_create({'text':'Compose a greeting.'});svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        started=svc.jobs.start({'text':'Compose the greeting.','goal_id':goal['id']})
        assert finished(svc,started)['status']=='paused'
        run=svc.store.run(started['id'])
        assert not any(value.get('producer_type')=='independent_review' for value in (run.get('verification_feedback') or {}).get('receipts',{}).values())
        assert svc.jobs.workflow.completion_issues(run)
        assert len(remote.requests)==1
    finally:svc.shutdown()


def test_saved_review_snapshot_cannot_certify_changed_sources(tmp_path):
    from project_tools import ProjectTools
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        path=tmp_path/'project'/'component.js';path.write_text('Before review')
        snapshot=svc.jobs.workflow.review_snapshot(run,['component.js'])
        path.write_text('After review')
        with pytest.raises(ValueError,match='changed after review'):
            svc.jobs.workflow._validate_review_snapshot(run,snapshot,[task['id']])
        assert not (svc.store.run(run['id']).get('verification_feedback') or {}).get('receipts')
    finally:svc.shutdown()


def test_optional_cloud_outage_keeps_unsupported_checks_pending_and_local_permissions(tmp_path,monkeypatch):
    import threading
    from forge_goal_review import GoalReview
    from project_tools import ProjectTools
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        builder=svc.get_builder().save({'project_id':run['project_id'],'chat_id':run['chat_id'],'objective':'Implement the public API','directory':'.','requirements':['Public API']})
        current=svc.store.goal(goal['id'])
        svc.store.save_goal({**current,'builder_id':builder['id'],'builder_revision':builder['revision'],
            'tasks':[{**current['tasks'][0],'requirement_id':builder['requirements'][0]['id'],'status':'completed','evidence':['A narrative claim.']}]})
        for gate in builder['gates']:
            svc.get_builder().check({'id':builder['id'],'expected_revision':builder['revision'],'gate':gate,'status':'passed','source':'manual','note':'Human fixture gate inspection.'},human=True)
        reviewer=GoalReview(svc.jobs)
        monkeypatch.setattr(reviewer,'_check',lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('OpenRouter free quota unavailable')))
        with pytest.raises(ValueError,match='Local checks are incomplete'):reviewer.check(svc.store.run(run['id']),'Done',{'cancel':threading.Event()},0)
        assert svc.store.goal(goal['id'])['status']!='completed'
        assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']))
        write=next(value for value in ProjectTools.schemas() if value['function']['name']=='write_file')
        assert svc.jobs.registry.permission(run,write,{'path':'repair.js','content':'authorized local work'})=='allow'
        assert not svc.store.run(run['id']).get('review_unavailable')
    finally:svc.shutdown()


def test_manual_delegation_off_preserves_only_current_planner_result(tmp_path):
    import threading
    from test_forge50_collaboration import setup
    from forge_collaboration import CloudCollaboration,setup_specialists
    svc=setup(tmp_path)
    try:
        setup_specialists(svc);svc.store.update_settings({'auto_delegate':False,'goal_cloud_guidance':True})
        goal=svc.goal_create({'text':'Implement the guided goal.','tasks':[{'text':'Implement the task.'}]})
        accepted=svc.jobs.start({'text':'Implement it.','goal_id':goal['id'],'mode':'goal'})
        run=CloudCollaboration(svc).prepare(svc.store.run(accepted['id']),threading.Event())
        child=run['planner_assignment']['run_id']
        schemas={s['function']['name']:s for s in svc.jobs.registry.schemas(run,['tools'],all_tools=True)}
        assert 'delegate_agent' not in schemas and schemas['agent_result']['function']['parameters']['properties']['run_id']['enum']==[child]
        from forge_runs import WORKFLOW
        delegation=next(item for item in WORKFLOW if item['function']['name']=='delegate_agent')
        args={'agent_id':'openrouter-planner','task':'Unauthorized new assignment.'}
        assert svc.jobs.registry.permission(run,delegation,args)=='deny'
        assert svc.jobs.registry.execute(run,'delegate_agent',args,threading.Event())['not_executed']
        routed={s['function']['name'] for s in svc.jobs.registry.schemas(run,['tools'])}
        assert 'agent_result' in routed
        result=svc.jobs.registry.execute(run,'agent_result',{'run_id':child,'wait_seconds':0},threading.Event())
        assert not result.get('not_executed')
        denied=svc.jobs.registry.execute(run,'agent_result',{'run_id':run['id'],'wait_seconds':0},threading.Event())
        assert denied['not_executed']
        stranger={**run,'planner_assignment':{'run_id':run['id']}}
        assert 'agent_result' not in {s['function']['name'] for s in svc.jobs.registry.schemas(stranger,['tools'],all_tools=True)}
    finally:svc.shutdown()


def test_planner_arriving_before_effect_requires_new_context_without_initial_write(tmp_path):
    import threading
    import time
    from project_tools import ProjectTools
    from test_forge_core import call
    svc,run,goal,task,schema=fixture(tmp_path)
    try:
        svc.before_tool_execution=lambda *args:{'not_executed':True,'error':'New planner guidance needs a fresh model round.'}
        run=svc.store.update_run(run['id'],rounds=1)
        write=next(item for item in ProjectTools.schemas() if item['function']['name']=='write_file')
        action=call('write_file',{'path':'result.txt','content':'drafted before advice'})
        job={'cancel':threading.Event(),'approval':None}
        result=svc.jobs._execute_call(run,job,action,0,{'write_file':write},{'capabilities':[]},time.monotonic())
        assert result['result']['not_executed'] and not (tmp_path/'project'/'result.txt').exists()
        svc.before_tool_execution=lambda *args:None
        run=svc.store.update_run(run['id'],rounds=2)
        second=svc.jobs._execute_call(run,job,call('write_file',{'path':'result.txt','content':'revised using advice'}),0,{'write_file':write},{'capabilities':[]},time.monotonic())
        assert not second['result'].get('not_executed')
        assert (tmp_path/'project'/'result.txt').read_text()=='revised using advice'
    finally: svc.shutdown()
