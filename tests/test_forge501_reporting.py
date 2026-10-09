"""Final reporting uses fresh accepted-scope evidence, never another effect round."""
from copy import deepcopy
import json
import threading

import pytest

from forge_goal_review import GoalReview
from test_forge_core import Engine,call,finished
from test_forge501_cadence import cadence_case,execute,staged_run


def build_calls():
    return {'tool_calls':[call('apply_patch',{'changes':[
        {'path':'a.txt','expected_sha256':'missing','content':'#exact-selector'},
        {'path':'b.txt','expected_sha256':'missing','content':'second accepted file'},
        {'path':'c.txt','expected_sha256':'missing','content':'third accepted file'}]})]}


def prepared_report(svc,goal,schema):
    # Reach report readiness using real file effects, host checks and complete
    # protocol pairs. The fake model tests only the actual next reporting turn.
    launch=svc.jobs._launch;run=staged_run(svc,goal)
    for called in (build_calls()['tool_calls'][0],call('trusted_check',{})):
        svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[called])
        name=called['function']['name'];args=called['function']['arguments']
        run,result=execute(svc,run,name,args)
        schemas={item['function']['name']:item for item in svc.jobs.registry.schemas(run,['tools'],all_tools=True)}
        svc.jobs._check_verification(run,[(name,args,result)],schemas)
    run=svc.jobs.workflow.refresh(svc.store.run(run['id']))
    if svc.jobs.workflow.enabled(run):assert svc.jobs._guided_report_state(run)
    svc.store.update_run(run['id'],status='paused')
    svc.jobs._launch=launch
    return svc.jobs.resume(run['id'])


@pytest.mark.parametrize('extra_call',['read_file','write_file','tools_load'])
def test_verified_state_gets_tools_free_report_and_unoffered_calls_never_execute(tmp_path,monkeypatch,extra_call):
    args={'path':'a.txt'} if extra_call=='read_file' else {'path':'a.txt','content':'must not replace verified work'} if extra_call=='write_file' else {'names':['write_file']}
    engine=Engine([{'tool_calls':[call(extra_call,args)]}])
    svc,root,goal,schema=cadence_case(tmp_path,engine)
    executed=[];original=svc.jobs.registry.execute
    def trace(run,name,args,cancel,**kwargs):
        executed.append(name);return original(run,name,args,cancel,**kwargs)
    monkeypatch.setattr(svc.jobs.registry,'execute',trace)
    try:
        accepted=prepared_report(svc,goal,schema);result=finished(svc,accepted)
        assert result['status']=='completed',result.get('recovery')
        assert len(engine.requests)==1 and engine.requests[0]['tools']==[]
        instructions='\n'.join(message['content'] for message in engine.requests[0]['messages'] if message['role']=='system')
        assert 'Implementation is starting.' not in instructions and 'This turn is report-only' in instructions
        assert executed==['apply_patch','trusted_check']
        assert (root/'a.txt').read_text()=='#exact-selector'
        with svc.store._connection() as db:
            denied=json.loads(db.execute('SELECT result FROM invocations WHERE id=?',(accepted['id']+':3:0',)).fetchone()[0])
            assert denied['result']['not_executed'] and 'report-only' in denied['result']['error']
        answer=[message for message in svc.store.run_chat(accepted['chat_id'],0)['messages'] if message['role']=='assistant'][-1]
        assert 'Verified 1 accepted task: Provide the verified exact selector.' in answer['content']
        assert 'Checks and evidence are in task details.' in answer['content']
        assert next(event for event in svc.store.events(accepted['id']) if event['type']=='report_fallback')['producer']=='coordinator_verified_report'
        assert not svc.jobs.workflow.completion_issues(svc.store.run(accepted['id']))
    finally:svc.shutdown()


def test_report_without_tools_preserves_model_answer_and_current_receipt_identity(tmp_path):
    engine=Engine([{'content':'The exact selector and all three accepted files passed verification.'}])
    svc,_,goal,schema=cadence_case(tmp_path,engine)
    try:
        accepted=prepared_report(svc,goal,schema)
        assert finished(svc,accepted)['status']=='completed'
        assert engine.requests[-1]['tools']==[]
        assert not any(event['type']=='report_fallback' for event in svc.store.events(accepted['id']))
        assert svc.store.run(accepted['id'])['context_snapshot']['report_only']
    finally:svc.shutdown()


@pytest.mark.parametrize('intervention',['external-edit','human-steer'])
def test_changed_verification_during_reporting_denies_old_calls_and_requires_fresh_check(tmp_path,intervention):
    holder={}
    def change(data,cancel):
        svc=holder['svc'];root=holder['root']
        assert data['tools']==[]
        if intervention=='external-edit':(root/'b.txt').write_text('Human changed this source during reporting.')
        else:
            run=next(run for run in svc.store.runs() if not run.get('parent_id'))
            svc.jobs.steer(run['id'],'Accepted clarification: keep the exact selector and verify all current files.','during-report')
        yield {'message':{'tool_calls':[call('read_file',{'path':'b.txt'})]},'done':True,'eval_count':12}
    engine=Engine([change,
        {'tool_calls':[call('tools_load',{'names':['trusted_check']})]},
        {'tool_calls':[call('trusted_check',{})]}, {'content':'Current sources passed the fresh registered check.'}])
    svc,root,goal,schema=cadence_case(tmp_path,engine);holder.update(svc=svc,root=root)
    try:
        accepted=prepared_report(svc,goal,schema);result=finished(svc,accepted)
        assert result['status']=='completed',result.get('recovery')
        assert len(engine.requests)==4 and engine.requests[0]['tools']==[] and engine.requests[1]['tools']
        assert 'trusted_check' in {schema['function']['name'] for schema in engine.requests[2]['tools']}
        assert not any(event['type']=='report_fallback' for event in svc.store.events(accepted['id']))
        with svc.store._connection() as db:
            rows=db.execute('SELECT name,result FROM invocations WHERE run_id=?',(accepted['id'],)).fetchall()
        checks=[row for row in rows if row['name']=='trusted_check']
        assert len(checks)==2 and all(json.loads(row['result'])['result']['passed'] for row in checks)
        if intervention=='external-edit':
            denial=next(json.loads(row['result'])['result'] for row in rows if row['name']=='read_file')
            assert denial['not_executed']
        else:assert not any(row['name']=='read_file' for row in rows)
        assert not svc.jobs.workflow.completion_issues(svc.store.run(accepted['id']))
    finally:svc.shutdown()


def test_report_only_candidate_does_not_bypass_required_independent_review(tmp_path,monkeypatch):
    engine=Engine([{'tool_calls':[call('read_file',{'path':'a.txt'})]}])
    svc,_,goal,schema=cadence_case(tmp_path,engine);seen=[]
    svc.store.update_settings({'cloud_required_review':True,'goal_review_enabled':True})
    def review(self,run,answer,job,started,**kwargs):
        seen.append((run['id'],answer))
        raise ValueError('Required independent review remains pending in this fixture.')
    monkeypatch.setattr(GoalReview,'check',review)
    try:
        accepted=prepared_report(svc,goal,schema);result=finished(svc,accepted)
        assert result['status']=='paused' and 'Required independent review remains pending' in result['recovery']
        assert engine.requests[-1]['tools']==[] and len(seen)==1
        assert 'Verified 1 accepted task' in seen[0][1] and 'independent review verified' not in seen[0][1].lower()
        assert svc.store.goal(goal['id'])['status']!='completed'
    finally:svc.shutdown()


@pytest.mark.parametrize('blocker',['unconsumed-child','failed-child','input','unknown','prepared','missing-original'])
def test_report_readiness_requires_children_inputs_interruptions_and_original_scope(tmp_path,blocker):
    svc,_,goal,schema=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        run,_=execute(svc,run,'apply_patch',build_calls()['tool_calls'][0]['function']['arguments'])
        run,result=execute(svc,run,'trusted_check',{})
        svc.jobs._check_verification(run,[('trusted_check',{},result)],{'trusted_check':schema})
        run=svc.jobs.workflow.refresh(svc.store.run(run['id']))
        assert svc.jobs._guided_report_state(run)
        if blocker in ('unconsumed-child','failed-child'):
            child=svc.jobs.start({'text':'Required child fixture.','parent_id':run['id'],'project_id':run['project_id']})
            svc.store.update_run(child['id'],status='completed' if blocker=='unconsumed-child' else 'failed',result_consumed=blocker=='failed-child')
        elif blocker=='input':
            run,_=execute(svc,run,'request_user_input',{'questions':[{'id':'choice','question':'Choose the accepted layout.','options':[{'label':'One'},{'label':'Two'}]}]})
        elif blocker in ('unknown','prepared'):
            identifier=run['id']+':unresolved'
            svc.store.invocation(identifier,run['id'],'write_file',{'path':'unresolved.txt','content':'pending'})
            if blocker=='unknown':svc.store.invocation_state(identifier,'outcome_unknown',{'error':'Interrupted after starting.'})
        else:
            original=deepcopy(run.get('goal_initial') or {})
            original['tasks']=original.get('tasks',[])+[{'id':'lost-original','text':'Accepted original requirement.'}]
            svc.store.update_run(run['id'],goal_initial=original)
        assert svc.jobs._guided_report_state(svc.store.run(run['id'])) is None
        assert not svc.core.requests
    finally:svc.shutdown()


def test_legacy_verified_goal_still_executes_its_requested_read_and_bookkeeping(tmp_path):
    holder={}
    def bookkeeping(data,cancel):
        svc=holder['svc'];goal=svc.store.goal(holder['goal']['id'])
        tasks=[{**task,'status':'completed','evidence':['Real host selector check passed.']} for task in goal['tasks']]
        yield {'message':{'tool_calls':[call('goal_update',{'tasks':tasks,'checkpoint':'Verified legacy fixture.','next_action':'Report.'})]},'done':True,'eval_count':12}
    engine=Engine([{'tool_calls':[call('read_file',{'path':'a.txt'})]},bookkeeping,
        {'content':'The legacy exact selector passed.'}])
    svc,_,goal,schema=cadence_case(tmp_path,engine);holder.update(svc=svc,goal=goal)
    svc.store.update_settings({'guided_execution':False})
    try:
        accepted=prepared_report(svc,goal,schema);result=finished(svc,accepted)
        assert result['status']=='completed',result.get('recovery')
        assert engine.requests[0]['tools']
        with svc.store._connection() as db:
            result=json.loads(db.execute("SELECT result FROM invocations WHERE run_id=? AND name='read_file'",(accepted['id'],)).fetchone()[0])
        assert result['result']['content']=='#exact-selector' and not result['result'].get('not_executed')
        assert not svc.store.run(accepted['id'])['context_snapshot']['report_only']
    finally:svc.shutdown()


def test_report_denial_crash_reconciles_protocol_without_reading_or_replaying_effects(tmp_path,monkeypatch):
    engine=Engine([{'content':'The accepted tasks remain verified after recovery.'}])
    svc,root,goal,schema=cadence_case(tmp_path,engine)
    try:
        launch=svc.jobs._launch;run=staged_run(svc,goal)
        for called in (build_calls()['tool_calls'][0],call('trusted_check',{})):
            svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[called])
            name=called['function']['name'];args=called['function']['arguments']
            run,result=execute(svc,run,name,args)
            names={item['function']['name']:item for item in svc.jobs.registry.schemas(run,['tools'],all_tools=True)}
            svc.jobs._check_verification(run,[(name,args,result)],names)
        run=svc.jobs.workflow.refresh(svc.store.run(run['id']))
        run=svc.store.update_run(run['id'],rounds=3)
        called=call('read_file',{'path':'a.txt'})
        svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[called])
        original=svc.jobs._tool_result
        def crash(*args):raise RuntimeError('Fixture crash before the protocol response published.')
        monkeypatch.setattr(svc.jobs,'_tool_result',crash)
        with pytest.raises(RuntimeError,match='Fixture crash'):svc.jobs._deny_report_calls(run,[called])
        saved=svc.store.run(run['id'])
        assert saved['pending_calls']==[called] and saved['next_tool']==0
        monkeypatch.setattr(svc.jobs,'_tool_result',original)
        svc.store.update_run(run['id'],status='paused')
        svc.jobs._launch=launch
        resumed=svc.jobs.resume(run['id'])
        assert finished(svc,resumed)['status']=='completed'
        assert len(engine.requests)==1 and engine.requests[0]['tools']==[]
        assert (root/'a.txt').read_text()=='#exact-selector'
        with svc.store._connection() as db:
            rows=db.execute("SELECT result,message_id FROM invocations WHERE run_id=? AND name='read_file'",(run['id'],)).fetchall()
            assert len(rows)==1 and rows[0]['message_id'] is not None and json.loads(rows[0]['result'])['result']['not_executed']
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='apply_patch'",(run['id'],)).fetchone()[0]==1
    finally:svc.shutdown()
