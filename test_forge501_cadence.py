"""Real disposable effects and native fake-provider snapshots exercise cadence."""
from copy import deepcopy
import json
import sys
import threading
import time

import pytest

from agent_runtime import tool_schema
from forge_cadence import FILE_MUTATIONS
from forge_store import encode
from test_forge_core import Engine,call,finished,service


def cadence_case(tmp_path,engine=None):
    svc=service(tmp_path,engine)
    svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,
        'memory_suggestions':False,'goal_cloud_guidance':False,'goal_review_enabled':False})
    root=tmp_path/'project';root.mkdir(exist_ok=True)
    project=svc.create_project({'name':'Cadence fixture','path':str(root)})
    goal=svc.goal_create({'text':'Build three exact files and verify the expected selector.',
        'project_id':project['id'],'tasks':[{'id':'exact-files','text':'Provide the verified exact selector.'}]})
    schema=tool_schema('trusted_check','Verify the actual exact selector and required files.',{})
    schema['capability']='read'
    schema['verification_contract']={'id':'exact-selector-v1','expected_checks':['Expected selector'],
        'task_ids':['exact-files'],'scope_path':'.'}
    previous_schemas=svc.extra_tool_schemas;previous_execute=svc.execute_extra_tool
    svc.extra_tool_schemas=lambda run:[schema]+previous_schemas(run)
    def check(run,name,args,cancel):
        if name!='trusted_check':return previous_execute(run,name,args,cancel)
        passed=all((root/name).is_file() for name in ('a.txt','b.txt','c.txt')) and (root/'a.txt').read_text()=='#exact-selector'
        return {'passed':passed,'checks':[{'requirement':'Expected selector','passed':passed,
            'detail':'Inspect a.txt and preserve #exact-selector.' if not passed else 'Actual files and selector passed.'}]}
    svc.execute_extra_tool=check
    return svc,root,goal,schema


def staged_run(svc,goal):
    svc.jobs._launch=lambda run:None
    accepted=svc.jobs.start({'text':goal['request'],'project_id':goal['project_id'],'goal_id':goal['id'],'mode':'goal'})
    return svc.store.update_run(accepted['id'],status='running')


def execute(svc,run,name,args,index=0,round_number=None):
    if round_number is None:round_number=run['rounds']+1
    run=svc.store.update_run(run['id'],rounds=round_number)
    schemas=svc.jobs.registry.schemas(run,['tools'],all_tools=True)
    names={schema['function']['name']:schema for schema in schemas}
    called=call(name,args)
    result=svc.jobs._execute_call(run,{'cancel':threading.Event()},called,index,names,{'capabilities':['tools']},time.monotonic())
    svc.jobs._tool_result(run,called,index,result)
    return svc.store.run(run['id']),result


def three_writes(svc,run):
    for index,name in enumerate(('a.txt','b.txt','c.txt')):
        run,_=execute(svc,run,'write_file',{'path':name,'content':'initial '+name})
    return run


def test_native_sequence_blocks_fourth_write_then_failed_check_permits_verified_repair(tmp_path):
    engine=Engine([
        {'tool_calls':[call('write_file',{'path':name,'content':'initial '+name}) for name in ('a.txt','b.txt','c.txt')]},
        {'tool_calls':[call('write_file',{'path':'fourth.txt','content':'must not execute'})]},
        {'tool_calls':[call('trusted_check',{})]},
        {'tool_calls':[call('write_file',{'path':'a.txt','content':'#exact-selector'})]},
        {'tool_calls':[call('trusted_check',{})]},
        {'content':'The exact files and selector passed the registered check.'}])
    svc,root,goal,_=cadence_case(tmp_path,engine)
    try:
        accepted=svc.jobs.start({'text':goal['request'],'project_id':goal['project_id'],'goal_id':goal['id'],'mode':'goal'})
        final=finished(svc,accepted)
        assert final['status']=='completed',final.get('recovery')
        assert not (root/'fourth.txt').exists() and (root/'a.txt').read_text()=='#exact-selector'
        assert len(engine.requests)==6
        sent=engine.requests[1]
        assert 'trusted_check' in {schema['function']['name'] for schema in sent['tools']}
        assert any('Verification cadence: three file mutations' in message['content'] for message in sent['messages'])
        assert all(set(schema)=={'type','function'} for schema in sent['tools'])
        assert sent['token_breakdown']==svc.jobs.prompt_compiler.metrics(sent['messages'],sent['tools'])
        with svc.store._connection() as db:
            denied=json.loads(db.execute('SELECT result FROM invocations WHERE id=?',(accepted['id']+':2:0',)).fetchone()[0])
            assert denied['result']['not_executed'] and denied['result']['next_check']=='trusted_check'
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='trusted_check' AND status='completed'",(accepted['id'],)).fetchone()[0]==2
        current=svc.store.run(accepted['id'])
        assert current['verification_cadence']['mutations_since_check']==0
        assert svc.jobs.workflow.completion_issues(current)==[]
        assert not svc.store.unknown_actions(accepted['id'])
    finally:svc.shutdown()


def test_four_calls_in_one_batch_cannot_execute_the_fourth_before_check(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        for index in range(4):
            run,result=execute(svc,run,'write_file',{'path':str(index)+'.txt','content':'complete valid edit'},index=index,round_number=1)
        assert result['result']['not_executed'] and not (root/'3.txt').exists()
        assert all((root/(str(index)+'.txt')).exists() for index in range(3))
        assert not svc.core.requests
    finally:svc.shutdown()


@pytest.mark.parametrize('result',[
    {'not_executed':True,'error':'Permission denied.'},
    {'error':'Host checker unavailable.'},
    {'passed':True,'checks':[{'requirement':'Expected selector','passed':True}],'unavailable':True},
    {'passed':True,'checks':[{'requirement':'Expected selector','passed':True}],'partial':True},
    {'passed':True,'checks':[{'requirement':'Unrelated label','passed':True}]},
    {'passed':True,'checks':[{'requirement':'Expected selector','passed':False}]},
])
def test_invalid_unexecuted_or_unavailable_checks_do_not_restore_mutation_allowance(tmp_path,result):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=three_writes(svc,staged_run(svc,goal))
        svc.execute_extra_tool=lambda *args:deepcopy(result)
        run,_=execute(svc,run,'trusted_check',{})
        run,blocked=execute(svc,run,'write_file',{'path':'fourth.txt','content':'not executed'})
        assert blocked['result']['not_executed'] and not (root/'fourth.txt').exists()
        assert run['verification_cadence']['mutations_since_check']==3
        assert not svc.core.requests
    finally:svc.shutdown()


def test_real_missing_file_failure_resets_cadence_for_repairs_without_verifying_task(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=three_writes(svc,staged_run(svc,goal))
        svc.execute_extra_tool=lambda *args:{'passed':False,'checks':[{'requirement':'Required file is inspectable','passed':False}]}
        run,_=execute(svc,run,'trusted_check',{})
        run,allowed=execute(svc,run,'write_file',{'path':'repair.txt','content':'authorized repair'})
        assert allowed['result']['ok'] and (root/'repair.txt').exists()
        assert run['verification_cadence']['mutations_since_check']==0
        assert svc.jobs.workflow.completion_issues(run)
    finally:svc.shutdown()


@pytest.mark.parametrize('mode',['legacy','no-check','denied-check','oversized-check','child'])
def test_legacy_unavailable_check_and_child_runs_keep_existing_mutation_behavior(tmp_path,mode):
    svc,root,goal,schema=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        if mode=='legacy':run=svc.store.update_run(run['id'],execution_mode='legacy')
        elif mode=='no-check':svc.extra_tool_schemas=lambda run:[]
        elif mode=='denied-check':svc.store.update_settings({'permission_overrides':{'tool:trusted_check':'deny_access'}})
        elif mode=='oversized-check':schema['function']['description']='Large non-loadable schema '*2000
        elif mode=='child':
            parent=svc.jobs.start({'text':'Independent parent fixture.'})
            svc.store.update_run(parent['id'],status='paused')
            run=svc.store.update_run(run['id'],parent_id=parent['id'])
        for index in range(4):run,result=execute(svc,run,'write_file',{'path':str(index)+'.txt','content':'allowed edit'})
        assert result['result']['ok'] and (root/'3.txt').exists()
        assert not svc.core.requests
    finally:svc.shutdown()


def test_batch_patch_counts_once_and_discovery_cannot_bypass_execution_guard(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        args={'changes':[{'path':name,'expected_sha256':'missing','content':'initial '+name} for name in ('a.txt','b.txt','c.txt')]}
        run,_=execute(svc,run,'apply_patch',args)
        schemas=svc.jobs.registry.schemas(run,['tools'],all_tools=True)
        state=svc.jobs.cadence.refresh(run,schemas)
        assert state['mutations_since_check']==1
        run,_=execute(svc,run,'write_file',{'path':'a.txt','content':'second edit'})
        run,_=execute(svc,run,'write_file',{'path':'b.txt','content':'third edit'})
        run,loaded=execute(svc,run,'tools_load',{'names':['write_file']})
        assert loaded['result']['loaded']==['write_file']
        selected=svc.jobs.registry.schemas(run,['tools'])
        assert 'trusted_check' in {item['function']['name'] for item in selected}
        run,blocked=execute(svc,run,'write_file',{'path':'c.txt','content':'blocked fourth edit'})
        assert blocked['result']['not_executed'] and (root/'c.txt').read_text()=='initial c.txt'
    finally:svc.shutdown()


def test_restart_and_checkbox_updates_do_not_clear_cadence_but_accepted_steering_does(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    run=three_writes(svc,staged_run(svc,goal))
    svc.jobs.registry.schemas(run,['tools'])
    svc.store.update_run(run['id'],status='paused')
    svc.shutdown()
    restored=service(tmp_path)
    try:
        restored.jobs._launch=lambda run:None
        schema=tool_schema('trusted_check','Check the exact selector.',{});schema['capability']='read'
        schema['verification_contract']={'id':'exact-selector-v1','expected_checks':['Expected selector'],'task_ids':['exact-files'],'scope_path':'.'}
        restored.extra_tool_schemas=lambda run:[schema]
        current=restored.store.run(run['id'])
        restored.jobs.resume(run['id']);current=restored.store.update_run(run['id'],status='running')
        progress=restored.store.goal(goal['id'])
        restored.store.update_goal_task(goal['id'],'exact-files',progress['revision'],'completed',['Checkbox update only.'])
        current,blocked=execute(restored,current,'write_file',{'path':'fourth.txt','content':'blocked'})
        assert blocked['result']['not_executed'] and not (root/'fourth.txt').exists()
        restored.get_interaction().queue_steer(run['id'],'Accepted correction: implement the additional fifth file now.','accepted-scope')
        assert restored.get_interaction().apply_steers(run['id'])
        current,allowed=execute(restored,current,'write_file',{'path':'fifth.txt','content':'authorized changed scope'})
        assert allowed['result']['ok'] and (root/'fifth.txt').exists()
        assert not restored.core.requests
        with restored.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='write_file'",(run['id'],)).fetchone()[0]==5
    finally:restored.shutdown()


def test_accepted_requirement_revision_resets_cadence_without_model_scope_changes(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=three_writes(svc,staged_run(svc,goal))
        svc.jobs.registry.schemas(run,['tools'])
        old=svc.store.goal(goal['id'])
        svc.store.save_goal({**old,'request':'Accepted revised result.','tasks':[{**old['tasks'][0],'text':'Provide the accepted revised selector.'}]},reconcile=True)
        run,result=execute(svc,run,'write_file',{'path':'revised.txt','content':'accepted structural revision'})
        assert result['result']['ok'] and (root/'revised.txt').exists()
        assert not svc.core.requests
    finally:svc.shutdown()


def test_actual_question_answer_and_final_narrative_cannot_clear_verification_cadence(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=three_writes(svc,staged_run(svc,goal))
        run,result=execute(svc,run,'request_user_input',{'questions':[{'id':'layout','header':'Layout',
            'question':'Which accepted layout should be used?','options':[{'label':'Compact'},{'label':'Roomy'}]}]})
        interaction=svc.get_interaction();interaction.publish_pending(run['id'])
        interaction.answer(result['result']['question_id'],{'layout':{'option':'Compact'}})
        svc.jobs._reset_verification_progress(run['id'])
        svc.store.add_message(run['chat_id'],'assistant','Everything was checked and is complete.')
        run,blocked=execute(svc,run,'write_file',{'path':'fourth.txt','content':'must still wait'})
        assert blocked['result']['not_executed'] and not (root/'fourth.txt').exists()
        assert run['verification_cadence']['mutations_since_check']==3
        assert not svc.core.requests
    finally:svc.shutdown()


def test_due_cadence_keeps_real_commands_available_but_exit_zero_is_not_the_registered_check(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=three_writes(svc,staged_run(svc,goal))
        run,command=execute(svc,run,'run_command',{'argv':[sys.executable,'-c','print("command remains available")'],'cwd':'.'})
        assert command['result']['exit_code']==0 and 'command remains available' in command['result']['output']
        run,blocked=execute(svc,run,'write_file',{'path':'fourth.txt','content':'still requires the registered checker'})
        assert blocked['result']['not_executed'] and not (root/'fourth.txt').exists()
        assert not svc.core.requests
    finally:svc.shutdown()


def test_failed_file_mutation_does_not_consume_a_committed_mutation_allowance(tmp_path):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        run,failed=execute(svc,run,'write_file',{'path':'missing-parent/result.txt','content':'no parent exists'})
        assert failed['result']['ok'] is False
        run=three_writes(svc,run)
        run,blocked=execute(svc,run,'write_file',{'path':'fourth.txt','content':'not executed'})
        assert blocked['result']['not_executed'] and run['verification_cadence']['mutations_since_check']==3
        assert not (root/'missing-parent').exists() and not (root/'fourth.txt').exists()
    finally:svc.shutdown()


def test_unknown_effect_still_requires_inspection_and_is_never_replayed(tmp_path,monkeypatch):
    svc,root,goal,_=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        for name in ('a.txt','b.txt'):run,_=execute(svc,run,'write_file',{'path':name,'content':'owned file'})
        original=svc.jobs.registry.execute
        def unknown(current,name,args,cancel,**kwargs):
            value=original(current,name,args,cancel,**kwargs)
            assert (root/'unknown.txt').exists()
            raise RuntimeError('Fixture interrupted after the effect committed.')
        monkeypatch.setattr(svc.jobs.registry,'execute',unknown)
        with pytest.raises(ValueError,match='outcome is unknown'):
            execute(svc,run,'write_file',{'path':'unknown.txt','content':'actual owned side effect'})
        svc.store.update_run(run['id'],status='paused')
        assert len(svc.store.unknown_actions(run['id']))==1
        with pytest.raises(ValueError,match='outcome-unknown'):svc.jobs.resume(run['id'])
        assert (root/'unknown.txt').read_text()=='actual owned side effect'
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='write_file'",(run['id'],)).fetchone()[0]==3
        assert not svc.core.requests
    finally:svc.shutdown()
