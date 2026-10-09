"""Fresh read evidence must reach an actual fake-provider turn at 8K.

Real project reads and persisted tool journals reproduce the guided Todo
recovery boundary without GPU inference, browser checks or personal state.
"""
from copy import deepcopy
import json
from pathlib import Path
import threading

import pytest

from context_window import estimated_prompt_tokens, prompt_budget, response_budget
from core import AGENT_SYSTEM_PROMPT
from forge_evals import evaluation_contract_factory, evaluation_schema_adapter, load_suite
from forge_store import encode
from prompt_compiler import PromptCompiler
from test_forge5_checkpoints import journal
from test_forge_core import Engine, call, finished, service
from tool_routing import wire_schemas


STEERING='Keep the completed inspections. Preserve task-form, task-input and task-list; the accepted selector contracts still apply.'


def staged_recovery(tmp_path, monkeypatch, *, oversized=False,task_text=None):
    snapshots=[]
    def receive(data,cancel):
        snapshots.append(deepcopy(data));svc.jobs.cancel(run['id'],pause=True)
        yield {'message':{'content':'Controlled stop after receiving the evidence.'},'done':True}
    engine=Engine([receive]);svc=service(tmp_path,engine)
    launch=svc.jobs._launch;svc.jobs._launch=lambda run:None
    svc.store.update_settings({'tokens':2048,'thinking':False,'memory_enabled':False,'memory_suggestions':False,
        'permission_profile':'full_access','web':False,'browser_tools':False,'computer_tools':False,
        'auto_delegate':False,'goal_review_enabled':False})
    task=next(value for value in load_suite()['tasks'] if value['id']=='todo-flow')
    root=tmp_path/'project';root.mkdir()
    for name,text in task['files'].items():(root/name).write_text(text,encoding='utf-8',newline='')
    if oversized:(root/'app.js').write_text('漢字🌍'*6000,encoding='utf-8')
    project=svc.create_project({'name':'8K guided recovery','path':str(root)})
    goal=svc.goal_create({'text':task['prompt'],'project_id':project['id'],
        'tasks':[{'id':'todo-public-flow','requirement_id':'todo-selector-contract','text':task_text or task['prompt']}]})
    svc.jobs.registry.schemas=evaluation_schema_adapter(svc.jobs.registry.schemas,evaluation_contract_factory(task,svc.store))
    accepted=svc.jobs.start({'text':task['prompt'],'mode':'goal','project_id':project['id'],'goal_id':goal['id'],
        'instructions':'Work only in this disposable project. Use evaluation_check for acceptance evidence and repair failures.'})
    run=svc.store.update_run(accepted['id'],rounds=1)
    svc.get_interaction().queue_steer(run['id'],STEERING,'accepted-selector-steer')
    svc.get_interaction().apply_steers(run['id'])
    # Old conversation pressure is eligible for retirement; the newer read
    # batch is protected. This exceeds 8K under the real shared estimator.
    svc.store.add_message(run['chat_id'],'assistant','Earlier diagnostic discussion. '*450)
    check_call={**call('evaluation_check',{}),'id':'failed-check-call'}
    svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[check_call])
    contract=next(s for s in svc.jobs.registry.schemas(run,['tools']) if s['function']['name']=='evaluation_check')
    result={'passed':False,'oracle_verified':True,'checks':[{'requirement':label,'passed':False,
        'detail':'Expected one active task; displayed: 0 remaining' if 'Remaining' in label else ''}
        for label in contract['verification_contract']['expected_checks']]}
    check_invocation,check_artifact=journal(svc,run,1,0,'evaluation_check',{},result)
    svc.jobs._check_verification(run,[('evaluation_check',{}, {'artifact':check_artifact,'result':result})],{'evaluation_check':contract})
    run=svc.store.update_run(run['id'],rounds=2)
    calls=[{**call('read_file',{'path':name}),'id':'fresh-'+name} for name in task['files']]
    assistant=svc.store.add_message(run['chat_id'],'assistant','',tool_calls=calls)
    results=[];artifacts=[];bodies={}
    for index,item in enumerate(calls):
        args=item['function']['arguments'];name=args['path']
        result=svc.jobs.registry.execute(run,'read_file',args,threading.Event())
        assert result['ok'] and not result['truncated']
        bodies[name]=result['content']
        _,artifact=journal(svc,run,2,index,'read_file',args,result)
        artifacts.append(artifact);results.append(('read_file',args,{'artifact':artifact,'result':result}))
    schemas=svc.jobs.registry.schemas(run,['tools'],all_tools=True)
    svc.jobs._check_verification(run,results,{s['function']['name']:s for s in schemas})
    recipe=(Path(__file__).parent/'assets/starter-library/skills/plan-and-build/SKILL.md').read_text(encoding='utf-8')
    # Pin a complete existing recipe, avoiding unrelated library installation
    # differences while exercising the production context/budget path.
    def skills(current,schemas):
        return svc.store.update_run(current['id'],skill_instructions='Source: mandatory test workflow\n'+recipe)
    monkeypatch.setattr(svc.jobs,'_skill_guidance',skills)
    run=skills(svc.store.run(run['id']),schemas)
    run=svc.jobs.workflow.refresh(run)
    return svc,engine,launch,run,goal,assistant,calls,artifacts,bodies,recipe,snapshots,check_invocation


def test_failing_check_three_fresh_reads_survive_actual_next_8k_generation(tmp_path,monkeypatch):
    svc,engine,launch,run,goal,assistant,calls,artifacts,bodies,recipe,snapshots,check=staged_recovery(tmp_path,monkeypatch)
    try:
        schemas=svc.jobs.registry.schemas(run,['tools'])
        before=svc.jobs._context(run,schemas)
        budget=prompt_budget(8192,response_budget(8192,2048))
        assert estimated_prompt_tokens(before,wire_schemas(schemas),AGENT_SYSTEM_PROMPT)>budget
        original_tasks=deepcopy(svc.store.goal(goal['id'])['tasks'])
        original_scope=svc.store.goal(goal['id'])['scope_revision']
        svc.jobs._launch=launch;launch(run);finished(svc,run)
        assert len(snapshots)==len(engine.requests)==1
        data=snapshots[0];messages=data['messages']
        assert estimated_prompt_tokens(messages,data['tools'],AGENT_SYSTEM_PROMPT)<=budget
        retained=next(message for message in messages if message.get('tool_calls'))
        assert [value['id'] for value in retained['tool_calls']]==[value['id'] for value in calls]
        tool_rows=[json.loads(message['content']) for message in messages if message['role']=='tool']
        assert [value['result']['path'] for value in tool_rows]==[value['function']['arguments']['path'] for value in calls]
        assert {value['artifact'] for value in tool_rows}==set(artifacts)
        assert {value['result']['path']:value['result']['content'] for value in tool_rows}==bodies
        assert 'id="task-form"' in bodies['index.html'] and 'id="task-list"' in bodies['index.html']
        assert '// Implement the requested user flow here.' in bodies['app.js']
        system='\n'.join(message['content'] for message in messages if message['role']=='system')
        assert recipe in system and 'Expected one active task; displayed: 0 remaining' in system
        assert any(message['role']=='user' and message['content']==run['request'] for message in messages)
        assert any(message['role']=='user' and message['content']==STEERING for message in messages)
        assert 'todo-selector-contract' in system
        current=svc.store.run(run['id']);checkpoint=svc.store.entity('checkpoints',current['continuity_checkpoint_id'])
        assert checkpoint['through_message_id']==current['boundary']<assistant['id']
        assert check in encode(checkpoint) and not any(artifact in encode(checkpoint) for artifact in artifacts)
        assert checkpoint['model_summary_requests']==0 and len([e for e in svc.store.events(run['id']) if e['type']=='compacted'])==1
        assert current['context_snapshot']['token_breakdown']==PromptCompiler.metrics(messages,data['tools'])
        saved_goal=svc.store.goal(goal['id'])
        assert saved_goal['tasks']==original_tasks and saved_goal['scope_revision']==original_scope
        # Preparing unchanged continuity must reuse the retained exchange, not
        # erase it or produce a further semantic checkpoint/model request.
        for _ in range(2):
            current=svc.jobs.workflow.refresh(svc.store.run(run['id']));job={'cancel':threading.Event()}
            again=svc.jobs._compact(current,schemas,job)
            assert again['boundary']==current['boundary']
            assert [json.loads(m['content'])['result']['content'] for m in job['context_messages'] if m['role']=='tool']==list(bodies.values())
        assert len(engine.requests)==1 and len([e for e in svc.store.events(run['id']) if e['type']=='compacted'])==1
    finally:svc.shutdown()


def test_oversized_fresh_unicode_exchange_pauses_without_retiring_its_evidence(tmp_path,monkeypatch):
    svc,engine,launch,run,goal,assistant,calls,artifacts,bodies,recipe,snapshots,check=staged_recovery(tmp_path,monkeypatch,oversized=True)
    try:
        original=svc.store.run(run['id']);schemas=svc.jobs.registry.schemas(run,['tools'])
        with pytest.raises(ValueError,match='newest tool exchange.*Increase Context'):
            svc.jobs._compact(original,schemas,{'cancel':threading.Event()})
        retained=svc.store.run(run['id'])
        assert retained.get('boundary',0)==original.get('boundary',0)
        assert retained.get('continuity_checkpoint_id')==original.get('continuity_checkpoint_id')
        assert not svc.store.entities('checkpoints') and not engine.requests
        for artifact in artifacts:
            chunks=[];start=0
            while True:
                value=svc.store.read_artifact(artifact,start=start,limit=16000);chunks.append(value['text'])
                if value['next']==value['total_characters']:break
                start=value['next']
            value=json.loads(''.join(chunks))['result']
            assert value['content']==bodies[value['path']]
        svc.jobs._launch=launch;launch(retained)
        outcome=finished(svc,run)
        assert outcome['status']=='paused' and 'Increase Context' in outcome['recovery']
        assert not engine.requests and not snapshots
        assert svc.store.run(run['id']).get('boundary',0)==original.get('boundary',0)
        assert not any(e['type']=='compacted' for e in svc.store.events(run['id']))
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name!='read_file' AND name!='evaluation_check'",(run['id'],)).fetchone()[0]==0
            count=db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]
        # A user-selected larger context resumes the same saved evidence;
        # no reread, effect or inference retry happened at the old bound.
        svc.jobs.resume(run['id'],{'context':65536});finished(svc,run)
        assert len(engine.requests)==len(snapshots)==1
        resumed=snapshots[0]
        assert resumed['context']==65536
        assert bodies['app.js'] in next(json.loads(m['content'])['result']['content'] for m in resumed['messages']
            if m['role']=='tool' and json.loads(m['content'])['result'].get('path')=='app.js')
        with svc.store._connection() as db:assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==count
    finally:svc.shutdown()


def test_late_accepted_contract_has_explicit_complete_retrieval_before_effects(tmp_path,monkeypatch):
    text='Implement the accepted task. '+('Preserve every stated behavior. '*100)+'Required late selector: data-accepted-flow-739.'
    svc,engine,launch,run,goal,assistant,calls,artifacts,bodies,recipe,snapshots,check=staged_recovery(tmp_path,monkeypatch,task_text=text)
    try:
        svc.jobs._launch=launch;launch(run);finished(svc,run)
        assert len(snapshots)==1
        messages=snapshots[0]['messages'];system='\n'.join(m['content'] for m in messages if m['role']=='system')
        assert 'Read the complete task with goal_read before implementing it' in system
        assert 'todo-selector-contract' in system and recipe in system
        assert 'goal_read' in {s['function']['name'] for s in snapshots[0]['tools']}
        current=svc.store.run(run['id'])
        complete=svc.jobs.registry.execute(current,'goal_read',{},threading.Event())
        assert complete['tasks'][0]['text']==text and complete['tasks'][0]['id']=='todo-public-flow'
        assert complete['tasks'][0]['requirement_id']=='todo-selector-contract'
        assert complete['requirement_history'][0]['tasks'][0]['text']==text
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name NOT IN ('evaluation_check','read_file')",(run['id'],)).fetchone()[0]==0
    finally:svc.shutdown()
