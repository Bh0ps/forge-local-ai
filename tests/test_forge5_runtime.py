"""Real coordinator contracts for selective tools, snapshots and read batches."""
import json
import threading
import time

import pytest

from agent_runtime import tool_schema
from prompt_compiler import PromptCompiler, deterministic_checkpoint, compact_goal_context
from test_forge_core import Engine, call, finished, service
from tool_routing import validate_arguments, relevance


def project(svc,tmp_path):
    root=tmp_path/'project'; root.mkdir()
    return svc.create_project({'name':'Fixture','path':str(root)}),root


def test_scoped_project_instructions_refresh_and_cannot_escape(tmp_path):
    root=tmp_path/'project'; root.mkdir(); child=root/'src'; child.mkdir()
    (root/'AGENTS.md').write_text('Root convention.',encoding='utf-8')
    (root/'CLAUDE.md').write_text('Superseded root convention.',encoding='utf-8')
    (child/'CLAUDE.md').write_text('Scoped frontend convention.',encoding='utf-8')
    outside=tmp_path/'CLAUDE.md'; outside.write_text('Private unrelated convention.',encoding='utf-8')
    compiler=PromptCompiler()
    text=compiler.project_guidance(root,['src/App.tsx','../CLAUDE.md'])
    assert text['sources']==['AGENTS.md','src/CLAUDE.md']
    assert 'Private' not in text['text'] and 'Superseded' not in text['text']
    (root/'AGENTS.md').write_text('Updated root convention.',encoding='utf-8')
    changed=compiler.project_guidance(root,['src/App.tsx'])
    assert changed['digest']!=text['digest'] and 'Updated root' in changed['text']


def test_round_snapshot_recalls_memory_once_and_reaches_provider(tmp_path,monkeypatch):
    engine=Engine([{'tool_calls':[call('read_file',{'path':'a.txt'})]},{'content':'Checked.'}])
    svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path); (root/'a.txt').write_text('Evidence.')
        (root/'AGENTS.md').write_text('Use the established names.',encoding='utf-8')
        memory=svc.get_memory(); recalls=[]; original=memory.recall
        def recall(*args,**kwargs): recalls.append(args); return original(*args,**kwargs)
        monkeypatch.setattr(memory,'recall',recall)
        run=svc.jobs.start({'text':'Read a.txt and explain it.','project_id':selected['id']})
        assert finished(svc,run)['status']=='completed'
        assert len(recalls)==2 and len(engine.requests)==2
        assert 'Use the established names.' in '\n'.join(m['content'] for m in engine.requests[0]['messages'])
        assert engine.requests[0]['token_breakdown']['version']=='forge-5.0.3'
        assert svc.store.run(run['id'])['context_snapshot']['round']==2
    finally: svc.shutdown()


def test_discovered_tool_loads_next_round_without_scope_expansion(tmp_path):
    engine=Engine([{'tool_calls':[call('tools_search',{'query':'needle specialist'})]},
        {'tool_calls':[call('tools_load',{'names':['fixture_needle']})]},
        {'tool_calls':[call('fixture_needle',{'marker':'checked'})]},{'content':'Used checked evidence.'}])
    svc=service(tmp_path,engine)
    svc.extra_tool_schemas=lambda run:[tool_schema('fixture_unused_'+str(n),'Unrelated capability '+('x'*1200),{}) for n in range(12)]+[
        {**tool_schema('fixture_needle','Needle specialist fixture. '+('q'*1400),{'marker':{'type':'string'}},['marker']),'capability':'read'}]
    svc.execute_extra_tool=lambda run,name,args,cancel:{'marker':args['marker']}
    try:
        run=svc.jobs.start({'text':'Investigate the connected fixture.','context':8192})
        assert finished(svc,run)['status']=='completed'
        assert 'fixture_needle' not in {s['function']['name'] for s in engine.requests[0]['tools']}
        assert 'fixture_needle' in {s['function']['name'] for s in engine.requests[2]['tools']}
        assert any('checked' in m['content'] for m in engine.requests[3]['messages'] if m['role']=='tool')
        assert svc.store.run(run['id'])['settings']['permission_profile']=='always_ask'
    finally: svc.shutdown()


@pytest.mark.parametrize('bad',[{'path':3},{'path':'a.txt','extra':True},{}])
def test_invalid_complete_call_repairs_before_execution(tmp_path,bad):
    engine=Engine([{'tool_calls':[call('read_file',bad)]},
                  {'tool_calls':[call('read_file',{'path':'a.txt'})]},{'content':'Verified.'}])
    svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path); (root/'a.txt').write_text('Evidence.')
        run=svc.jobs.start({'text':'Read a.txt.','project_id':selected['id']})
        assert finished(svc,run)['status']=='completed'
        events=svc.store.events(run['id'])
        assert any(e['type']=='tool_validation' and e['executed'] is False for e in events)
        assert len([e for e in events if e['type']=='tool' and e.get('state')=='running'])==1
        assert svc.store.run(run['id'])['tool_repairs']==1
    finally: svc.shutdown()


def test_invalid_batch_never_executes_valid_write_and_repair_is_bounded(tmp_path):
    invalid=[call('write_file',{'path':'new.txt','content':'Must not appear'}),call('read_file',{'path':17})]
    svc=service(tmp_path,Engine([{'tool_calls':invalid},{'tool_calls':invalid}]))
    try:
        selected,root=project(svc,tmp_path)
        run=svc.jobs.start({'text':'Build the file.','project_id':selected['id'],'permission_profile':'full_access'})
        result=finished(svc,run)
        assert result['status']=='paused' and 'one repair' in result['recovery']
        assert not (root/'new.txt').exists()
        assert not any(e['type']=='approval' for e in result['events'])
    finally: svc.shutdown()


def test_malformed_streamed_arguments_can_repair_without_protocol_action(tmp_path):
    svc=service(tmp_path,Engine([{'tool_calls':[call('read_file','{"path":')]},{'content':'No action executed.'}]))
    try:
        selected,_=project(svc,tmp_path)
        run=svc.jobs.start({'text':'Read the fixture.','project_id':selected['id']})
        assert finished(svc,run)['status']=='completed'
        with svc.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==0
    finally: svc.shutdown()


def test_parallel_reads_preserve_protocol_order_and_budget(tmp_path,monkeypatch):
    svc=service(tmp_path,Engine([{'tool_calls':[call('read_file',{'path':str(n)+'.txt'}) for n in range(4)]},{'content':'Checked.'}]))
    try:
        selected,root=project(svc,tmp_path)
        for n in range(4): (root/(str(n)+'.txt')).write_text('File '+str(n))
        original=svc.jobs.registry.execute; active=0; peak=0; lock=threading.Lock(); barrier=threading.Barrier(4)
        def execute(run,name,args,cancel,**kwargs):
            nonlocal active,peak
            if name=='read_file':
                with lock: active+=1; peak=max(peak,active)
                try:
                    barrier.wait(timeout=5)
                    time.sleep(.04*(4-int(args['path'][0])))
                    return original(run,name,args,cancel,**kwargs)
                finally:
                    with lock: active-=1
            return original(run,name,args,cancel,**kwargs)
        monkeypatch.setattr(svc.jobs.registry,'execute',execute)
        run=svc.jobs.start({'text':'Read all four fixture files.','project_id':selected['id']})
        assert finished(svc,run)['status']=='completed'
        assert peak==4 and svc.store.run(run['id'])['tools']==4
        messages=svc.store.run_chat(run['chat_id'],0)['messages']
        results=[json.loads(m['content'])['result']['path'] for m in messages if m['role']=='tool']
        assert results==['0.txt','1.txt','2.txt','3.txt']
    finally: svc.shutdown()


def test_schema_nested_validation_rejects_types_without_echoing_secrets():
    schema=tool_schema('nested','Fixture.',{'items':{'type':'array','items':{'type':'object','properties':{
        'count':{'type':'integer','minimum':1}},'required':['count'],'additionalProperties':False}}},['items'])
    assert validate_arguments(schema,{'items':[{'count':True}]})
    assert validate_arguments(schema,{'items':[{'count':2}]}) is None
    issue=validate_arguments(schema,{'items':[{'count':'secret-do-not-echo'}]})
    assert 'secret-do-not-echo' not in issue


def test_checkpoint_retains_decision_and_action_artifact():
    text=deterministic_checkpoint({'request_message_id':1,'checkpoint':'Tests pending.'},[
        {'id':1,'role':'user','content':'Original task.'},{'id':9,'role':'user','content':'Use the blue palette.'}],
        [{'id':'inv-1','name':'apply_patch','status':'completed','result':json.dumps({'artifact':'artifact-1','result':{'path':'src/App.tsx'}})}])
    assert 'Use the blue palette.' in text and 'artifact-1' in text and '"covered_through": 9' in text


def test_compaction_failure_uses_one_request_then_exact_deterministic_coverage(tmp_path):
    class RetryEngine(Engine):
        def __init__(self): super().__init__(); self.summary_attempts=[]
        def stream_agent(self,data,cancel):
            if any('continuity summary' in m['content'] for m in data['messages'] if m['role']=='system'):
                packet=json.loads(next(m['content'] for m in data['messages'] if m['role']=='user'))
                self.summary_attempts.append(packet)
                if len(self.summary_attempts)==1: raise ValueError('First summary unavailable')
                yield {'message':{'content':'Observed earlier decisions retained.'},'done':True,'done_reason':'stop'}
                return
            yield from super().stream_agent(data,cancel)
    engine=RetryEngine(); svc=service(tmp_path,engine)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'Keep the original request exact.'})
        for n in range(15): svc.store.add_message(accepted['chat_id'],'user','Decision '+str(n)+': '+('x'*1780))
        rows=svc.store.run_chat(accepted['chat_id'],0)['messages']
        compacted=svc.jobs._compact(svc.store.run(accepted['id']),[],{'cancel':threading.Event()})
        assert len(engine.summary_attempts)==1
        checkpoint=svc.store.entity('checkpoints',compacted['continuity_checkpoint_id'])
        assert checkpoint['summary_kind']=='deterministic' and checkpoint['model_summary_requests']==1
        assert checkpoint['through_message_id']==compacted['boundary']<=rows[-1]['id']
        retained=svc.jobs._context(compacted,[])
        assert any(m['content']=='Keep the original request exact.' for m in retained)
        assert checkpoint['decision_count']>0
    finally: svc.shutdown()


def test_async_completed_child_is_consumed_before_parent_finishes(tmp_path):
    engine=Engine(); svc=service(tmp_path,engine); child_ids=[]
    def response(data,cancel):
        parent=next(r for r in svc.store.runs() if not r.get('parent_id'))
        chat=svc.store.create_chat()
        child=svc.store.create_run({'chat_id':chat['id'],'parent_id':parent['id'],'settings':parent['settings'],
            'request':'Bounded advisory fixture','request_message_id':0,'rounds':0,'tools':0,'output_tokens':0})
        svc.store.add_message(chat['id'],'assistant','Helper evidence must be considered.')
        svc.store.finish_run(child['id'],{'status':'completed'},{'status':'completed'})
        child_ids.append(child['id'])
        yield {'message':{'content':'Initial final attempt.'},'done':True,'done_reason':'stop'}
    engine.rounds=[response,{'content':'Used helper evidence.'}]
    try:
        accepted=svc.jobs.start({'text':'Coordinate the bounded task.'})
        assert finished(svc,accepted)['status']=='completed'
        assert len(engine.requests)==2 and svc.store.run(child_ids[0])['result_consumed']
        assert any('Helper evidence must be considered.' in m['content'] for m in engine.requests[-1]['messages'])
    finally: svc.shutdown()


def test_run_admission_retains_selected_context_and_chat_skills(tmp_path,monkeypatch):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        selected,_=project(svc,tmp_path)
        chat=svc.store.create_chat(selected['id'])
        skill=svc.integrations.discover_skills()[0]
        svc.store.save_entity('chat_skills',{'id':chat['id'],'skills':[skill['id']]})
        space=svc.store.save_entity('spaces',{'name':'Attached decisions','project_ids':[selected['id']],'notes':'Use blue.'})
        document=svc.store.save_entity('documents',{'name':'Explicit reference','project_id':selected['id'],'sha256':'0'*64})
        calls=[]
        def resolve(data,current):
            calls.append(data)
            return {**current,**data,'context':16384,'adaptive_applied_profile':'approved-profile'}
        monkeypatch.setattr(svc,'resolve_run_settings',resolve)
        accepted=svc.jobs.start({'text':'Read the attached decisions.','chat_id':chat['id'],
                                'space_ids':[space['id']],'document_ids':[document['id']]})
        run=svc.store.run(accepted['id'])
        assert calls and run['settings']['context']==16384 and run['settings']['adaptive_applied_profile']=='approved-profile'
        assert run['space_ids']==[space['id']] and run['document_ids']==[document['id']] and skill['id'] in run['skills']
        assert svc.store.get_settings()['context']==8192
        with pytest.raises(ValueError): svc.jobs.start({'text':'Bad reference.','document_ids':['unknown']})
    finally: svc.shutdown()


def test_long_goal_focuses_current_work_and_task_paging(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        tasks=[{'id':'task-'+str(n),'text':'Requirement '+str(n)+': '+('detail '*100),
                'status':'completed' if n<70 else 'pending','evidence':['Saved result '+str(n)] if n<70 else []} for n in range(100)]
        goal=svc.goal_create({'text':'Implement the required app.','tasks':tasks})
        packet=compact_goal_context(svc.store.goal(goal['id']),8192)
        assert 'task-70' in packet and '"completed_tasks": 70' in packet and len(packet.encode())<4500
        run=svc.jobs.start({'text':'Continue the app.','goal_id':goal['id']})
        details=svc.jobs.registry.execute(svc.store.run(run['id']),'goal_read',{'task_id':'task-70'},threading.Event())
        assert len(details['tasks'])==1 and details['tasks'][0]['id']=='task-70' and details['total_tasks']==100
        child={**svc.store.run(run['id']),'parent_id':'parent'}
        assert 'goal_update' not in {s['function']['name'] for s in svc.jobs.registry.schemas(child,['tools'])}
    finally: svc.shutdown()


def test_skill_context_passes_structured_failures_without_forcing_implementation(tmp_path,monkeypatch):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'Research the sources and compare findings.'})
        run=svc.store.run(accepted['id']); captured=[]
        def select(project,explicit,query='',context=None): captured.append(context); return []
        monkeypatch.setattr(svc.integrations,'active_skill_instructions',select)
        svc.store.invocation('failed-fixture',run['id'],'read_file',{'path':'missing'})
        svc.store.invocation_state('failed-fixture','completed',{'result':{'ok':False,'error':'Missing file'}})
        svc.jobs._skill_guidance(run,[])
        assert captured[0]['phase']=='research'
        assert captured[0]['outcomes']==[{'name':'read_file','status':'failed','ok':False}]
        from forge_library import task_context
        assert task_context(run['request'],captured[0])['phase']=='research'
        assert 'debug' in task_context(run['request'],captured[0])['intents']
    finally: svc.shutdown()


def test_large_skill_defers_whole_recipe_instead_of_cutting_mandatory_steps(tmp_path,monkeypatch):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'Use this workflow.'})
        def select(project,explicit,query='',context=None):
            return [{'skill':{'id':'long-fixture','name':'Complete recipe','path':'fixture/SKILL.md'},
                     'text':'MANDATORY FIRST STEP\n'+('detail '*3000)+'\nMANDATORY LAST STEP'}]
        monkeypatch.setattr(svc.integrations,'active_skill_instructions',select)
        result=svc.jobs._skill_guidance(svc.store.run(accepted['id']),[])
        assert 'skills_read id long-fixture' in result['skill_instructions']
        assert 'MANDATORY FIRST STEP' not in result['skill_instructions']
        assert result['active_skills']==['long-fixture']
    finally: svc.shutdown()


def test_verification_routes_command_tools_without_requesting_implementation():
    command=tool_schema('command_start','Start a managed command.',{})
    write=tool_schema('write_file','Write a file.',{})
    assert relevance(command,'Run pytest and verify the tests')>relevance(write,'Run pytest and verify the tests')
