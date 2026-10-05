"""Release regressions for event paging, atomic replay and local image storage."""
import base64
import io
import json
from pathlib import Path
from PIL import Image
import pytest
from test_forge_core import service,Engine,finished,call
from forge_runtime import RuntimeManager
from forge_runs import ToolRegistry
import threading

def test_terminal_event_replay_drains_all_pages(tmp_path):
    svc=service(tmp_path); svc.jobs._launch=lambda run:None
    run=svc.jobs.start({'text':'Synthetic event replay'})
    for i in range(1100): svc.store.event(run['id'],'token',text=str(i))
    svc.store.update_run(run['id'],status='completed')
    cursor=0; count=0
    for _ in range(3):
        page=svc.jobs.poll(run['id'],cursor);count+=len(page['events']);cursor=page['next_cursor']
        if count<1100: assert not page['finished'] and page['has_more']
    assert count==1100 and page['finished'] and not page['has_more'];svc.shutdown()

def test_tool_message_commit_is_idempotent_and_advances_cursor_atomically(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    run=svc.jobs.start({'text':'Synthetic tool checkpoint'});identifier=run['id']+':1:0'
    svc.store.invocation(identifier,run['id'],'read_file',{'path':'x'})
    svc.store.invocation_state(identifier,'completed',{'content':'Read complete'})
    svc.store.tool_message(identifier,0,'read_file','{"content":"Read complete"}')
    svc.store.tool_message(identifier,0,'read_file','{"content":"Read complete"}')
    chat=svc.store.get_chat(run['chat_id'])
    assert len([m for m in chat['messages'] if m['role']=='tool'])==1
    assert svc.store.run(run['id'])['next_tool']==1;svc.shutdown()

def test_image_request_is_pinned_but_chat_and_run_json_keep_only_references(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    buffer=io.BytesIO();Image.new('RGB',(32,32),'orange').save(buffer,'PNG');encoded=base64.b64encode(buffer.getvalue()).decode()
    run=svc.jobs.start({'text':'Look at this image','images':[encoded]})
    stored=svc.store.run(run['id']);assert stored['images'][0].startswith('forge-attachment:')
    chat=svc.store.get_chat(run['chat_id']);reference=chat['messages'][0]['images'][0]
    assert reference.startswith('forge-attachment:')
    assert svc.dispatch('attachment',{'id':reference})['image']==encoded
    context=svc.jobs._context(stored,[])
    assert context[-1]['images']==[encoded] and context[-1]['content']=='Look at this image';svc.shutdown()

def test_truncated_tool_call_is_saved_without_execution_and_can_resume(tmp_path):
    def truncated(data,cancel):
        yield {'message':{'content':'Partial','tool_calls':[call('write_file',{'path':'x','content':'x'})]},'done':True,'done_reason':'length'}
    svc=service(tmp_path,Engine([truncated,{'content':'Continuation complete'}]))
    run=svc.jobs.start({'text':'Build something'});assert finished(svc,run)['status']=='paused'
    assert not svc.store.get_chat(run['chat_id'])['messages'][-1].get('tool_calls')
    assert not svc.store.unknown_actions(run['id'])
    assert finished(svc,svc.jobs.resume(run['id']))['status']=='completed';svc.shutdown()

def test_advanced_runtime_is_bound_to_supported_flags_and_verification(tmp_path,monkeypatch):
    manager=RuntimeManager(tmp_path/'forge');executable=tmp_path/'llama-server.exe';executable.write_bytes(b'fixture')
    config={'id':'fixture','engine':'llama.cpp','executable':str(executable),'executable_sha256':manager._hash(executable),'validation':None}
    manager.configs['fixture']=config
    monkeypatch.setattr(manager,'advanced_capabilities',lambda id:{'spec_types':['none','ngram-mod','draft-mtp'],'cuda_graph':False,'fp4':False})
    args,env=manager._advanced_arguments(config,{'advanced':{'spec_type':'ngram-mod'}})
    assert args==['--spec-type','ngram-mod'] and env=={}
    with pytest.raises(ValueError,match='does not support'):manager._advanced_arguments(config,{'advanced':{'spec_type':'unavailable'}})
    with pytest.raises(ValueError,match='CUDA'):manager._advanced_arguments(config,{'advanced':{'cuda_graph_opt':True}})
    with pytest.raises(ValueError,match='Validate'):manager.start({'id':'fixture','advanced':{'spec_type':'ngram-mod'}})

def test_resume_carries_explicit_context_change_without_overwriting_profile_model(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    run=svc.jobs.start({'text':'Continue durable work','model':'profile-model','context':4096})
    svc.store.update_run(run['id'],status='paused')
    svc.store.update_settings({'context':16384})
    svc.jobs.resume(run['id'])
    saved=svc.store.run(run['id'])
    assert saved['settings']['context']==16384 and saved['settings']['model']=='profile-model'
    svc.shutdown()

def test_app_permission_uses_os_target_and_project_grant_does_not_expand_desktop(tmp_path):
    svc=service(tmp_path);svc.store.update_settings({'computer_tools':True,'permission_overrides':{
        'project:project':'full_access','app:approved.exe':'full_access','app:blocked.exe':'deny_access'}})
    run={'id':'fixture','project_id':'project','settings':svc.store.get_settings()}
    schema={'function':{'name':'computer_click'}}
    registry=ToolRegistry(svc)
    assert registry.permission(run,schema,{'app':'approved.exe'})=='ask'
    assert registry.permission(run,schema,{}, {'app':'approved.exe'})=='allow'
    assert registry.permission(run,schema,{'app':'approved.exe'}, {'app':'blocked.exe'})=='deny'
    svc.shutdown()

def test_desktop_target_change_after_approval_cannot_execute(tmp_path):
    class Broker:
        inspections=0; executions=0
        def reset(self): pass
        def schemas(self):return [{'type':'function','function':{'name':'computer_click','parameters':{'type':'object','properties':{}}}}]
        def describe_target(self,args,context):
            self.inspections+=1
            return {'app':'fixture.exe','target':'fixture window '+str(self.inspections),'window_id':1}
        def execute(self,*args):self.executions+=1;return {'ok':True}
    svc=service(tmp_path,Engine([{'tool_calls':[call('computer_click',{})]},{'content':'Stopped at changed target.'}]))
    broker=Broker();svc.computer_broker=broker
    svc.store.update_settings({'computer_tools':True})
    svc.jobs._approval=lambda *args:True
    run=svc.jobs.start({'text':'Click the connected window'})
    assert finished(svc,run)['status']=='completed' and broker.executions==0
    svc.computer_broker=None;svc.shutdown()

def test_invalid_goal_limits_are_rejected_before_journaling(tmp_path):
    svc=service(tmp_path)
    for value in ({'tokens':0},{'minutes':None},{'tools':True},{'rounds':'128'}):
        with pytest.raises(ValueError):svc.store.update_settings({'goal_limits':value})
        with pytest.raises(ValueError):svc.jobs.start({'text':'Invalid limits','goal_limits':value})
    assert not svc.store.runs();svc.shutdown()

def test_project_write_backups_stay_in_backup_directory(tmp_path):
    svc=service(tmp_path);root=tmp_path/'project';root.mkdir();(root/'example.txt').write_text('before')
    project=svc.create_project({'name':'Fixture','path':str(root)})
    svc.jobs.registry.execute({'project_id':project['id']},'write_file',{'path':'example.txt','content':'after'},threading.Event())
    assert (root/'example.txt').read_text()=='after'
    assert list((svc.store.home/'backups').glob('*.json'))
    assert not list(svc.store.home.glob('*.json'));svc.shutdown()

def test_managed_provider_capabilities_require_matching_validation(tmp_path):
    class Runtime:
        configs={'fixture':{'id':'fixture','engine':'llama.cpp','executable_sha256':'fixture-hash',
            'validation':{'promoted':True,'binding':{'model':'verified'},'executable_sha256':'fixture-hash'}}}
        def dispatch(self,action,data):return {'url':'http://127.0.0.1:8082/v1'}
        def _model_binding(self,data):return {'model':data['model']}
        def close(self):pass
    svc=service(tmp_path);svc.runtime=Runtime()
    unverified=svc.dispatch('runtime_start',{'id':'fixture','model':'different'})
    assert unverified['provider']['capabilities']==[]
    verified=svc.dispatch('runtime_start',{'id':'fixture','model':'verified'})
    assert verified['provider']['capabilities']==['tools','vision']
    svc.runtime=None;svc.shutdown()
