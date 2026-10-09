"""Measured prompt overhead, bounded compaction and passive read-loop recovery."""
import json
import threading

import pytest

from agent_runtime import tool_schema
from forge_evals import evaluation_schema_adapter, load_suite
from forge_store import encode
from project_tools import ProjectTools
from prompt_compiler import compile_skill_guidance, deterministic_checkpoint, static_context_allowances
from test_forge_core import Engine, call, finished, service
from test_forge5_checkpoints import journal
from test_forge_channels import env


def project(svc, tmp_path):
    root=tmp_path/'project'; root.mkdir()
    return svc.create_project({'name':'Budget fixture','path':str(root)}),root


def test_primary_recipe_is_complete_and_all_supporting_workflows_are_explicit():
    items=[{'skill':{'id':str(index),'name':'Workflow '+str(index),'path':'/long/source/path'},
            'text':'MANDATORY FIRST\n'+('recipe detail '*95)+'\nMANDATORY LAST'} for index in range(3)]
    text,active=compile_skill_guidance(items,2048)
    assert items[0]['text'] in text and 'MANDATORY LAST' in text
    assert 'skills_read id 1' in text and 'skills_read id 2' in text
    assert active==['0','1','2'] and len(text.encode())<=2048
    oversized={**items[0],'text':'MANDATORY FIRST\n'+('detail '*1000)+'\nMANDATORY LAST'}
    deferred,_=compile_skill_guidance([oversized],2048)
    assert 'Read the complete recipe' in deferred and 'MANDATORY FIRST' not in deferred


def test_real_todo_prompt_leaves_more_room_for_evidence(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        svc.store.update_settings({'tokens':2048,'memory_enabled':False,'memory_suggestions':False,
            'web':False,'browser_tools':False,'auto_delegate':False})
        task=next(t for t in load_suite()['tasks'] if t['id']=='todo-flow')
        selected,root=project(svc,tmp_path)
        for name,text in task['files'].items(): (root/name).write_text(text,encoding='utf-8')
        svc.jobs.registry.schemas=evaluation_schema_adapter(svc.jobs.registry.schemas)
        accepted=svc.jobs.start({'text':task['prompt'],'project_id':selected['id'],
            'instructions':'Work only in the disposable project. Use evaluation_check for acceptance evidence and repair failures.'})
        run=svc.store.run(accepted['id']); schemas=svc.jobs.registry.schemas(run,['tools'])
        run=svc.jobs._skill_guidance(run,schemas); schemas=svc.jobs.registry.schemas(run,['tools'])
        snapshot=svc.jobs.prompt_compiler.metrics(svc.jobs._context(run,schemas),schemas)
        static=snapshot['instructions']+snapshot['skills']+snapshot['tools']
        assert static<3000, snapshot
        assert snapshot['skills']<800 and snapshot['tools']<1500
        assert len(run['skill_instructions'].encode())<=static_context_allowances(run['settings'])['skills_bytes']
        assert {'tools_search','tools_load','read_file','write_file','evaluation_check'}<={s['function']['name'] for s in schemas}
    finally: svc.shutdown()


def test_real_8k_goal_preserves_discovery_coordination_and_loadable_helpers(env,tmp_path):
    from test_forge_agent_delegation import setup
    setup(env)
    svc=env.service; svc.jobs._launch=lambda run:None
    selected,_=project(svc,tmp_path)
    goal=svc.goal_create({'text':'Build and verify the page.','project_id':selected['id']})
    accepted=svc.jobs.start({'text':'Build the page.','project_id':selected['id'],'goal_id':goal['id'],
        'context':8192,'tokens':2048,'auto_delegate':True})
    run=svc.store.run(accepted['id'])
    names={s['function']['name'] for s in svc.jobs.registry.schemas(run,['tools'])}
    assert {'tools_search','tools_load','request_user_input','goal_read','goal_update','artifact_read'}<=names
    for helper in ('delegate_agent','agent_result'):
        result=svc.jobs.registry.execute(svc.store.run(run['id']),'tools_load',{'names':[helper]},threading.Event())
        assert result.get('loaded')==[helper],result
        assert helper in {s['function']['name'] for s in svc.jobs.registry.schemas(svc.store.run(run['id']),['tools'])}
    result=svc.jobs.registry.execute(svc.store.run(run['id']),'tools_load',{'names':['apply_patch']},threading.Event())
    assert result.get('loaded')==['apply_patch'],result
    assert {'artifact_read','apply_patch'}<={s['function']['name'] for s in svc.jobs.registry.schemas(svc.store.run(run['id']),['tools'])}
    assert not {'write_file','delegate_agent'}&{s['function']['name'] for s in svc.jobs.registry.schemas(
        {**run,'mode':'plan'},['tools'],all_tools=True)}


@pytest.mark.parametrize('denial',['profile','tool','project','agent_scope'])
def test_automatic_project_guidance_respects_actual_read_permission(tmp_path,monkeypatch,denial):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        selected,root=project(svc,tmp_path)
        (root/'AGENTS.md').write_text('PRIVATE AUTOMATIC GUIDANCE',encoding='utf-8')
        accepted=svc.jobs.start({'text':'Explain the attached task.','project_id':selected['id']})
        if denial=='profile': svc.store.update_settings({'permission_profile':'deny_access'})
        elif denial=='agent_scope': svc.store.update_run(accepted['id'],agent_tools=['web_search'])
        else: svc.store.update_settings({'permission_overrides':{
            'tool:read_file' if denial=='tool' else 'project:'+selected['id']:'deny_access'}})
        def unexpected(*args,**kwargs): raise AssertionError('Denied project guidance was read.')
        monkeypatch.setattr(svc.jobs.prompt_compiler,'project_guidance',unexpected)
        messages=svc.jobs._context(svc.store.run(accepted['id']),[])
        text='\n'.join(m['content'] for m in messages)
        assert 'PRIVATE AUTOMATIC GUIDANCE' not in text
        assert 'Budget fixture' in text and str(root) in text
    finally: svc.shutdown()


def test_compaction_has_one_model_request_across_all_budget_reductions(tmp_path):
    engine=Engine(); svc=service(tmp_path,engine)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'Keep this exact objective 漢字🌍.'})
        run=svc.store.run(accepted['id']); artifacts=[]
        for index in range(4):
            args={'path':str(index)+'.txt'}
            svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[call('read_file',args)])
            _,artifact=journal(svc,run,index+1,0,'read_file',args,{'path':args['path'],'content':'漢字🌍'*3000})
            artifacts.append(artifact)
        rows=svc.store.run_chat(run['chat_id'],0)['messages']; job={'cancel':threading.Event()}
        compacted=svc.jobs._compact(run,[],job,force=True)
        checkpoint=svc.store.entity('checkpoints',compacted['continuity_checkpoint_id'])
        assert len(engine.requests)==checkpoint['model_summary_requests']==1
        assert checkpoint['deterministic_reductions']>=1
        assert checkpoint['summary_kind']=='deterministic'
        assert checkpoint['through_message_id']==compacted['boundary']==rows[-1]['id']
        assert {e['artifact_id'] for e in checkpoint['evidence_refs']}==set(artifacts)
        assert any(m['content']==run['request'] for m in job['context_messages'])
        assert len(svc.store.run_chat(run['chat_id'],0)['messages'])==len(rows)
    finally: svc.shutdown()


def test_deterministic_continuity_uses_record_link_without_nested_summary():
    text=deterministic_checkpoint({'continuity_checkpoint_id':'previous-record','summary':'old summary '*1000},
                                  [{'id':7,'role':'assistant','content':'Progress.'}])
    assert 'previous-record' in text and 'old summary' not in text and 'prior_summary' not in text


def test_unchanged_successful_reads_nudge_once_until_progress(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'Inspect relevant files.'}); run=svc.store.run(accepted['id'])
        schemas={s['function']['name']:s for s in ProjectTools.schemas()}
        def read(path='a.txt',proof='a'*64,artifact='first'):
            return [('read_file',{'path':path},{'artifact':artifact,'result':{'path':path,'sha256':proof,'content':'Evidence.'}})]
        svc.jobs._check_read_progress(run,read(),schemas)
        svc.jobs._check_read_progress(run,read(artifact='second'),schemas)
        svc.jobs._check_read_progress(run,read(artifact='third'),schemas)
        assert len([e for e in svc.store.events(run['id']) if e['type']=='progress_nudge'])==1
        assert 'specific unresolved question' in svc.store.run(run['id'])['read_progress']['nudge']
        # Observed new evidence changes the approach; attempted writes invalidate old proof.
        svc.jobs._check_read_progress(run,read(proof='b'*64),schemas)
        assert not svc.store.run(run['id'])['read_progress']['nudge']
        svc.jobs._check_read_progress(run,[('write_file',{'path':'a.txt'}, {'result':{'outcome_unknown':True}})],schemas)
        svc.jobs._check_read_progress(run,read(proof='b'*64),schemas)
        assert not svc.store.run(run['id'])['read_progress']['nudge']
        # Failed partial batch reads are not treated as successful evidence.
        failed=[('read_files',{'files':[{'path':'missing'}]}, {'result':{'files':[{'ok':False,'error':'Missing'}]}})]
        svc.jobs._check_read_progress(run,failed,schemas); svc.jobs._check_read_progress(run,failed,schemas)
        svc.jobs._check_read_progress(run,[('read_file',{'path':'a.txt'},None)],schemas)
        assert len([e for e in svc.store.events(run['id']) if e['type']=='progress_nudge'])==1
    finally: svc.shutdown()


def test_steering_clears_read_nudge_before_next_action(tmp_path):
    engine=Engine(); svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path); (root/'a.txt').write_text('Evidence.')
        read={'tool_calls':[call('read_file',{'path':'a.txt'})]}
        def steer(data,cancel):
            run=svc.store.runs()[0]
            svc.jobs.steer(run['id'],'Inspect the specific remaining question.')
            yield {'message':{'content':'Steered partial response.'},'done':True}
        engine.rounds=[read,read,steer,read,{'content':'Evidence checked.'}]
        accepted=svc.jobs.start({'text':'Read a.txt.','project_id':selected['id']})
        assert finished(svc,accepted)['status']=='completed'
        assert len([e for e in svc.store.events(accepted['id']) if e['type']=='progress_nudge'])==1
        assert not svc.store.run(accepted['id'])['read_progress']['nudge']
    finally: svc.shutdown()
