"""Acceptance failures remain actionable evidence, independent of tool execution."""
import json
import threading
import time

import pytest

from agent_runtime import tool_schema
from project_tools import ProjectTools
from prompt_compiler import AGENT_POLICY, durable_checkpoint, verification_outcome, verification_packet
from test_forge5_checkpoints import journal
from test_forge_core import Engine, call, finished, service


CHECK=tool_schema('quality_check','Run structured acceptance checks.',{'scope':{'type':'string'}})
CHECK['capability']='read'


def report(passed=False, label='Public contract is preserved', detail='Observed incompatible state.'):
    return {'passed':passed,'checks':[{'requirement':label,'passed':passed,'detail':'' if passed else detail}]}


def test_inspector_keeps_failed_verification_separate_from_completed_invocation(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        run,invocation,artifact=observe(svc,run,1,'quality_check',{'scope':'interaction'},report())
        data=svc.dispatch('run_context_inspect',{'run_id':run['id']})
        assert data['invocations'][0]['status']=='completed'
        failure=data['verification']['failures'][0]
        assert failure['failed'][0]['name']=='Public contract is preserved'
        assert failure['invocation_id']==invocation and failure['artifact_id']==artifact
        assert len(json.dumps(data['verification'],ensure_ascii=False).encode())<=1900
        observe(svc,run,2,'quality_check',{'scope':'interaction'},report(True))
        assert svc.dispatch('run_context_inspect',{'run_id':run['id']})['verification'] is None
    finally: svc.shutdown()


def prepared(svc,tmp_path,text='Implement the requested public contract.',mode='chat'):
    svc.jobs._launch=lambda run:None
    svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,'memory_suggestions':False})
    root=tmp_path/'project';root.mkdir()
    project=svc.create_project({'name':'Recovery fixture','path':str(root)})
    accepted=svc.jobs.start({'text':text,'project_id':project['id'],'mode':mode})
    return svc.store.update_run(accepted['id'],loaded_tools=['quality_check','write_file','read_file']),root


def observe(svc,run,round_number,name,args,result):
    run=svc.store.update_run(run['id'],rounds=round_number)
    invocation,artifact=journal(svc,run,round_number,0,name,args,result)
    schemas={s['function']['name']:s for s in ProjectTools.schemas()+[CHECK]}
    svc.jobs._check_verification(run,[(name,args,{'artifact':artifact,'result':result})],schemas)
    return svc.store.run(run['id']),invocation,artifact


@pytest.mark.parametrize('result',[
    {'passed':False}, {'enabled':False,'value':False},
    {'checks':[{'name':'flag','passed':False}]},
    {'passed':False,'checks':[]},
    {'passed':False,'checks':[{'name':'flag','passed':'false'}]},
    {'passed':True,'checks':[{'name':'flag','passed':False}]},
    {**report(),'outcome_unknown':True}, {**report(),'not_executed':True}])
def test_arbitrary_booleans_incomplete_or_unknown_results_are_not_checks(result):
    assert verification_outcome('read_file',{},result) is None


@pytest.mark.parametrize('name,result,classified',[
    ('run_command',{'argv':['test'],'exit_code':1,'output':'A test failed.'},True),
    ('command_wait',{'id':'owned','status':'completed','exit_code':2},True),
    ('command_read',{'id':'owned','status':'running','exit_code':2},False),
    ('command_wait',{'id':'owned','status':'interrupted','exit_code':2},False),
    ('run_command',{'exit_code':1,'timed_out':True},False),
    ('run_command',{'exit_code':1,'outcome_unknown':True},False),
    ('run_command',{'exit_code':True},False),
    ('read_file',{'exit_code':1},False)])
def test_commands_need_a_proven_finished_nonzero_outcome(name,result,classified):
    outcome=verification_outcome(name,{'argv':['test']},result)
    assert bool(outcome)==classified
    if classified: assert outcome['passed'] is False and outcome['kind']=='command'


def test_completed_command_failure_is_current_until_same_command_passes(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        failed={'argv':['check','component'],'cwd':'.','exit_code':1,'output':'An assertion failed.'}
        run,invocation,_=observe(svc,run,1,'run_command',{'argv':failed['argv']},failed)
        assert verification_packet(run)['failures'][0]['invocation_id']==invocation
        run,_,_=observe(svc,run,2,'run_command',{'argv':['unrelated']},{**failed,'argv':['unrelated'],'exit_code':0})
        assert verification_packet(run)
        run,_,_=observe(svc,run,3,'run_command',{'argv':failed['argv']},{**failed,'exit_code':0})
        assert verification_packet(run) is None
        failed_session=verification_outcome('command_read',{'id':'owned'},{'id':'owned','status':'completed','exit_code':1})
        passed_session=verification_outcome('command_wait',{'id':'owned'},{'id':'owned','status':'completed','exit_code':0})
        assert failed_session['key']==passed_session['key']
    finally: svc.shutdown()


def test_only_matching_committed_current_success_clears_failure(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        run,invocation,artifact=observe(svc,run,1,'quality_check',{'scope':'one'},report())
        packet=verification_packet(run)
        assert packet['failures'][0]['invocation_id']==invocation
        assert packet['failures'][0]['artifact_id']==artifact
        run,_,_=observe(svc,run,2,'quality_check',{'scope':'two'},report(True))
        assert verification_packet(run)
        run,_,_=observe(svc,run,3,'quality_check',{'scope':'one'},report(True,label='Different contract'))
        assert verification_packet(run)
        # A successful unrelated data read is not passing acceptance evidence.
        run,_,_=observe(svc,run,4,'read_file',{'path':'data.json'},{'enabled':False,'passed':True})
        assert verification_packet(run)
        # Uncommitted results cannot clear durable failures.
        svc.jobs._check_verification(run,[('quality_check',{'scope':'one'},{'result':report(True)})],{'quality_check':CHECK})
        assert verification_packet(svc.store.run(run['id']))
        run,_,_=observe(svc,run,5,'quality_check',{'scope':'one'},report(True))
        assert verification_packet(run) is None
        with svc.store._connection() as db:
            assert db.execute('SELECT status FROM invocations WHERE id=?',(invocation,)).fetchone()[0]=='completed'
    finally: svc.shutdown()


def test_failure_repetition_resets_on_source_change_but_needs_matching_recheck(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        read={'ok':True,'path':'component.txt','sha256':'a'*64,'content':'Old.'}
        run,_,_=observe(svc,run,1,'read_file',{'path':'component.txt'},read)
        run,_,_=observe(svc,run,2,'quality_check',{},report())
        run,_,_=observe(svc,run,3,'quality_check',{},report())
        assert verification_packet(run)['repeated_unchanged']
        run,_,_=observe(svc,run,4,'read_file',{'path':'component.txt'},{**read,'sha256':'b'*64,'content':'Changed.'})
        assert verification_packet(run)['changed_since_check']
        run,_,_=observe(svc,run,5,'quality_check',{},report())
        assert verification_packet(run)['failures'][0]['repeated']==1
        run,_,_=observe(svc,run,6,'quality_check',{},report(True))
        assert verification_packet(run) is None
    finally: svc.shutdown()


def test_third_unchanged_verification_failure_pauses_without_replaying_effects(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        run,_,_=observe(svc,run,1,'quality_check',{},report())
        run,_,_=observe(svc,run,2,'quality_check',{},report())
        with pytest.raises(ValueError,match='without progress'):
            observe(svc,run,3,'quality_check',{},report())
        run=svc.store.run(run['id'])
        assert verification_packet(run)['failures'][0]['repeated']==3
        with svc.store._connection() as db:
            assert {row[0] for row in db.execute('SELECT status FROM invocations WHERE run_id=?',(run['id'],))}=={'completed'}
        text='\n'.join(message['content'] for message in svc.jobs._context(run,[CHECK]))
        assert 'Stop rereading the same evidence' in text and 'concrete blocker' in text
    finally: svc.shutdown()


def test_unchanged_source_read_loop_gets_a_bounded_guard(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        read={'ok':True,'path':'component.txt','sha256':'a'*64,'content':'Old.'}
        run,_,_=observe(svc,run,1,'read_file',{'path':'component.txt'},read)
        run,_,_=observe(svc,run,2,'quality_check',{},report())
        for round_number in range(3,6):
            run,_,_=observe(svc,run,round_number,'read_file',{'path':'component.txt'},read)
        with pytest.raises(ValueError,match='without progress'):
            observe(svc,run,6,'read_file',{'path':'component.txt'},read)
    finally: svc.shutdown()


@pytest.mark.parametrize('name',['read_file','read_files'])
def test_actual_distinct_paged_inspections_preserve_failures_and_exact_repeats_pause(tmp_path,name):
    svc=service(tmp_path)
    try:
        run,root=prepared(svc,tmp_path)
        (root/'component.txt').write_text(''.join('Line '+str(index)+' public contract detail.\n' for index in range(1,301)),encoding='utf-8')
        tools=ProjectTools(root)
        def read(start,end):
            spec={'path':'component.txt','start_line':start,'end_line':end}
            args={'files':[spec]} if name=='read_files' else spec
            result=tools.read_files(**args) if name=='read_files' else tools.read_file(**args)
            return args,result
        args,result=read(1,20)
        run,_,_=observe(svc,run,1,name,args,result)
        run,invocation,artifact=observe(svc,run,2,'quality_check',{},report())
        version=run['verification_feedback']['version']
        proof=run['verification_feedback']['sources']['component.txt']
        for index in range(5):
            args,result=read(21+index*20,40+index*20)
            run,_,_=observe(svc,run,3+index,name,args,result)
            state=run['verification_feedback'];packet=verification_packet(run)
            assert state['version']==version and state['sources']['component.txt']==proof
            assert state['unchanged_rounds']==0
            assert packet['changed_since_check'] is False
            assert packet['failures'][0]['invocation_id']==invocation
            assert packet['failures'][0]['artifact_id']==artifact
        assert len(run['verification_feedback']['inspections'])==6
        # A different region provides evidence; repeating the same returned region does not.
        for round_number in range(8,11):
            run,_,_=observe(svc,run,round_number,name,args,result)
        with pytest.raises(ValueError,match='without progress'):
            observe(svc,run,11,name,args,result)
        assert verification_packet(svc.store.run(run['id']))['failures'][0]['invocation_id']==invocation
    finally: svc.shutdown()


@pytest.mark.parametrize('text,mode',[
    ('Diagnose the failed checks and report the cause.','chat'),
    ('Inspect only; do not fix the failures.','chat'),
    ('Read-only investigation: propose improvements and changes.','chat'),
    ('Implement repairs in an ordered plan.','plan')])
def test_diagnostic_and_plan_requests_can_report_repeated_failures(tmp_path,text,mode):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path,text,mode)
        for round_number in range(1,5):
            run,_,_=observe(svc,run,round_number,'quality_check',{},report())
        assert not svc.jobs._verification_requires_repair(run)
        assert 'report the failed checks factually' in '\n'.join(m['content'] for m in svc.jobs._context(run,[CHECK]))
    finally: svc.shutdown()


@pytest.mark.parametrize('summary_fail',[False,True])
def test_covered_failure_refs_survive_model_and_deterministic_compaction(tmp_path,summary_fail):
    svc=service(tmp_path,Engine(summary_fail=summary_fail))
    try:
        run,_=prepared(svc,tmp_path)
        svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[call('quality_check',{})])
        run,invocation,artifact=observe(svc,run,1,'quality_check',{},report())
        run=svc.jobs._compact(run,[CHECK],{'cancel':threading.Event()},force=True)
        checkpoint=svc.store.entity('checkpoints',run['continuity_checkpoint_id'])
        assert checkpoint['verification_failures'][0]['invocation_id']==invocation
        packet=verification_packet(run)
        assert packet['failures'][0]['covered_checkpoint_id']==checkpoint['id']
        assert packet['failures'][0]['artifact_id']==artifact
        messages=svc.jobs._context(run,[CHECK])
        assert any('Current verification failures' in m['content'] and invocation in m['content'] for m in messages)
        # A later uncovered failure must never be attributed to an earlier boundary.
        later={**run,'verification_feedback':{'pending':{'later':{**run['verification_feedback']['pending'][next(iter(run['verification_feedback']['pending']))],
            'message_id':run['boundary']+20}},'version':0}}
        rows=[{'id':run['boundary'],'role':'assistant','content':'Covered.'}]
        assert durable_checkpoint(later,rows,[])['verification_failures']==[]
    finally: svc.shutdown()


def test_failure_packet_is_bounded_and_links_omitted_evidence():
    entries={}
    for index in range(12):
        outcome=verification_outcome('quality_check',{'scope':str(index)},
            {'passed':False,'checks':[{'name':'漢字🌍'*100,'passed':False,'detail':'漢字🌍'*100} for _ in range(10)]})
        entries[str(index)]={**outcome,'tool':'quality_check','version':0,'repeated':1,'invocation_id':'run:1:'+str(index),
            'artifact_id':'proof-'+str(index),'message_id':index+1}
    packet=verification_packet({'verification_feedback':{'pending':entries,'version':0}})
    assert len(json.dumps(packet,ensure_ascii=False).encode())<=1900
    assert packet['scope_count']==12 and packet['failures'][0]['failed_count']==10
    assert packet['failures'][0]['artifact_id']=='proof-11'


def test_failure_survives_restart_and_new_steering_can_request_diagnosis(tmp_path):
    svc=service(tmp_path)
    run,_=prepared(svc,tmp_path)
    run,_,_=observe(svc,run,1,'quality_check',{},report())
    identifier=run['id'];svc.shutdown()
    restored=service(tmp_path)
    try:
        run=restored.store.run(identifier)
        assert verification_packet(run)
        restored.jobs._launch=lambda run:None
        restored.jobs.resume(identifier)
        restored.get_interaction().queue_steer(identifier,'Diagnose only and report the exact failure.')
        assert restored.get_interaction().apply_steers(identifier)
        assert not restored.jobs._verification_requires_repair(run)
    finally: restored.shutdown()


def test_previous_run_diagnostic_steering_does_not_override_new_task(tmp_path):
    svc=service(tmp_path)
    try:
        run,_=prepared(svc,tmp_path)
        svc.get_interaction().queue_steer(run['id'],'Report only; do not repair.')
        svc.get_interaction().apply_steers(run['id'])
        assert not svc.jobs._verification_requires_repair(run)
        svc.store.update_run(run['id'],status='completed')
        accepted=svc.jobs.start({'chat_id':run['chat_id'],'project_id':run['project_id'],'text':'Implement the public contract.'})
        assert svc.jobs._verification_requires_repair(svc.store.run(accepted['id']))
    finally: svc.shutdown()


def test_steering_resets_repetition_without_clearing_unresolved_evidence(tmp_path):
    def steer(data,cancel):
        run=svc.store.runs()[0]
        svc.jobs.steer(run['id'],'Diagnose only and report the failure.')
        yield {'message':{'content':'Preparing the factual report.'},'done':True}
    svc,engine,run,_=launch_fixture(tmp_path,[{'tool_calls':[call('quality_check',{})]},
        {'tool_calls':[call('quality_check',{})]},steer,
        {'tool_calls':[call('quality_check',{})]},{'content':'The public contract check remains failed.'}],
        [report(),report(),report()])
    try:
        assert finished(svc,run)['status']=='completed'
        packet=verification_packet(svc.store.run(run['id']))
        assert packet['failures'][0]['repeated']==1
        assert any(event['type']=='steer' and event.get('status')=='applied' for event in svc.store.events(run['id']))
    finally: svc.shutdown()


def launch_fixture(tmp_path,rounds,reports,text='Implement the requested public contract.',permission_profile='full_access'):
    engine=Engine(rounds);svc=service(tmp_path,engine)
    launch=svc.jobs._launch
    run,root=prepared(svc,tmp_path,text)
    responses=iter(reports)
    svc.extra_tool_schemas=lambda run:[CHECK]
    svc.execute_extra_tool=lambda run,name,args,cancel:next(responses)
    svc.store.update_settings({'permission_profile':permission_profile})
    svc.jobs._launch=launch;launch(run)
    return svc,engine,run,root


def test_next_snapshot_drives_repair_and_matching_pass_clears_it(tmp_path):
    svc,engine,run,root=launch_fixture(tmp_path,[
        {'tool_calls':[call('quality_check',{})]},
        {'tool_calls':[call('write_file',{'path':'component.txt','content':'Corrected public contract.','expected_sha256':'missing'})]},
        {'tool_calls':[call('quality_check',{})]}, {'content':'Implemented and verified.'}], [report(),report(True)])
    try:
        assert finished(svc,run)['status']=='completed'
        assert (root/'component.txt').read_text()=='Corrected public contract.'
        feedback='\n'.join(m['content'] for m in engine.requests[1]['messages'])
        assert 'Public contract is preserved' in feedback and 'targeted authorized repair' in feedback
        contexts=[event for event in svc.store.events(run['id']) if event['type']=='context']
        assert contexts[1]['verification_failures']['failures'][0]['message_id']>0
        assert contexts[2]['verification_failures']['changed_since_check']
        assert contexts[-1]['verification_failures'] is None
        assert svc.store.run(run['id'])['workflow_stage']=='implement'
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='write_file'",(run['id'],)).fetchone()[0]==1
    finally: svc.shutdown()


def test_unresolved_failed_checks_cannot_become_false_verified_completion(tmp_path):
    svc,engine,run,root=launch_fixture(tmp_path,[{'tool_calls':[call('quality_check',{})]},
        {'content':'Everything passed.'},{'content':'Everything passed.'},{'content':'Everything passed.'}],[report()])
    try:
        result=finished(svc,run)
        assert result['status']=='paused' and 'Acceptance checks remain failed' in result['recovery']
        assert len(engine.requests)==4 and not list(root.iterdir())
    finally: svc.shutdown()


def test_diagnostic_run_can_finish_a_factual_failure_report(tmp_path):
    svc,engine,run,_=launch_fixture(tmp_path,[{'tool_calls':[call('quality_check',{})]},
        {'content':'The public contract check failed; implementation state is incompatible.'}],[report()],
        text='Diagnose the failed checks and report the cause.')
    try:
        assert finished(svc,run)['status']=='completed'
        assert verification_packet(svc.store.run(run['id']))
        assert len(engine.requests)==2
    finally: svc.shutdown()


def test_verification_feedback_never_grants_write_permission(tmp_path):
    svc,engine,run,root=launch_fixture(tmp_path,[{'tool_calls':[call('quality_check',{})]},
        {'tool_calls':[call('write_file',{'path':'component.txt','content':'Needs approval.'})]}],[report()],permission_profile='always_ask')
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if any(e['type']=='approval' for e in svc.store.events(run['id'])): break
            time.sleep(.01)
        assert any(e['type']=='approval' for e in svc.store.events(run['id']))
        assert not (root/'component.txt').exists()
        svc.jobs.cancel(run['id']);finished(svc,run)
    finally: svc.shutdown()


def test_public_contracts_take_priority_in_compact_policy():
    assert 'Explicit public selectors, state ownership and API contracts outrank generic recipe patterns' in AGENT_POLICY
