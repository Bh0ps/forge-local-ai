"""Synthetic cloud continuation/retry fixtures never contact a real model."""
import json
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from forge_cloud_policy import CloudContextPolicy, CloudPolicyRejected
from forge_openrouter import FREE_ROUTER, OpenRouterFailure
from forge_provider_state import reasoning_details, MAX_PROVIDER_STATE_BYTES
from forge_runs import RunManager
from forge_service import ForgeService
from forge_store import ForgeStore
from test_forge_channels import env
from test_forge_openrouter import connected, pool_provider, sse, completion, request
from test_forge_workspaces import wait_finished, ScriptedEngine
from test_inference_timeouts import TimedBody, transport
from core import Core
from inference_stream import InferenceTimeouts


DETAILS = [
    {'type':'reasoning.text','text':'Synthetic reasoning.','signature':'fixture-signature','index':0,'format':'google-gemini-v1'},
    {'type':'reasoning.encrypted','data':'synthetic-opaque-state','id':'fixture_call','index':1,'format':'google-gemini-v1'},
]


def helper(env, tools=(), required=True):
    parent=env.service.jobs.start({'text':'Original parent objective.','auto_delegate':True})
    profile=env.service.agent_save({'name':'Synthetic cloud helper','role':'researcher','provider_id':'openrouter',
        'model':FREE_ROUTER,'tools':list(tools),'instructions':'Return bounded evidence.'})
    return parent,profile


def test_each_helper_attempt_starts_with_free_router_without_changing_saved_model_choices(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        env.service.agent_save({'id':profile['id'],'model':'fixture/coder:free'})
        config=env.service.store.entity('providers','openrouter')
        env.service.store.save_entity('providers',{**config,'model':'fixture/coder:free'})
        before=env.service.store.get_settings()
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Fresh bounded assignment.'})
        assert child['settings']['model']==FREE_ROUTER
        assert env.service.store.entity('agents',profile['id'])['model']=='fixture/coder:free'
        assert env.service.store.entity('providers','openrouter')['model']=='fixture/coder:free'
        assert env.service.store.get_settings()==before
        review=env.service.jobs.start({'text':'Independent review fixture.','provider_id':'openrouter',
            'model':'fixture/coder:free','mode':'goal_review','readonly':True})
        assert review['settings']['model']=='fixture/coder:free'
    finally:client.close()


def tool_packets(name,args,*,details=None,model='fixture/coder:free'):
    packets=[]
    for detail in details or []:
        packets.append(sse({'id':'gen-synthetic','model':model,'choices':[{'delta':{'reasoning_details':[detail]}}]}))
    packets.append(sse({'id':'gen-synthetic','model':model,'choices':[{'delta':{'tool_calls':[{
        'id':'fixture_call','index':0,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}))
    packets.append(sse({'choices':[{'delta':{},'finish_reason':'tool_calls'}]}))
    return packets


def launch(env):
    env.service.jobs._launch=MethodType(RunManager._launch,env.service.jobs)


def private_states(store,child):
    return store.provider_turn_states(child['id'],[row['id'] for row in store.get_chat(child['chat_id'])['messages']])


def assert_partial_private_state(env,child):
    states=private_states(env.service.store,child)
    assert len(states)==1
    state=next(iter(states.values()))
    assert state['reasoning_details']==DETAILS and state['model_id']=='fixture/coder:free' and not state['complete']
    chat=env.service.store.get_chat(child['chat_id'])
    assert chat['messages'][-1]['status']=='partial' and not chat['messages'][-1].get('tool_calls')
    with env.service.store._connection() as db:
        usage=[dict(row) for row in db.execute('SELECT * FROM usage WHERE run_id=?',(child['id'],))]
    public=[chat,env.service.store.events(child['id']),usage,
        env.service.dispatch('run_diagnostics_export',{'run_id':child['id']})]
    assert 'synthetic-opaque-state' not in json.dumps(public) and 'fixture-signature' not in json.dumps(public)
    assert env.service.store.run(child['id'])['tools']==0
    return state


def test_disconnected_opaque_state_survives_restart_and_blocks_unsafe_resume(env,monkeypatch):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env,['web_search'])
    calls=[]
    packets=tool_packets('web_search',{'query':'fixture'},details=DETAILS)[:-1]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,stream=TimedBody([(0,p) for p in packets]))
    transport(monkeypatch,handler);launch(env)
    fresh=None
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Bounded disconnect fixture.'})
        assert wait_finished(env.service,child)['status']=='paused'
        assert_partial_private_state(env,child)
        assert env.service.store.run(child['id'])['failure']['category']=='incomplete_provider_state'
        fresh=ForgeService(core=ScriptedEngine(),data_dir=env.service.store.home)
        pool_provider(SimpleNamespace(service=fresh,vault=env.vault),client)
        assert next(iter(private_states(fresh.store,child).values()))['reasoning_details']==DETAILS
        resumed=fresh.jobs.resume(child['id'])
        assert wait_finished(fresh,resumed)['status']=='paused'
        assert fresh.store.run(child['id'])['failure']['category']=='incomplete_provider_state'
        assert len(calls)==1 and fresh.store.run(child['id'])['tools']==0
    finally:
        if fresh:fresh.shutdown()
        client.close()


@pytest.mark.parametrize('ending',['length','tool_calls'])
def test_truncated_or_malformed_opaque_exchange_preserves_state_and_sends_no_continuation(env,monkeypatch,ending):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env,['web_search'])
    packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':[detail]}}]}) for detail in DETAILS]
    packets.extend([sse({'choices':[{'delta':{'tool_calls':[{'id':'fixture_call','index':0,'function':{'name':'web_search','arguments':'{'}}]}}]}),
        sse({'choices':[{'delta':{},'finish_reason':ending}]})])
    calls=[]
    def handler(req):
        calls.append(req);return httpx.Response(200,stream=TimedBody([(0,p) for p in packets]))
    transport(monkeypatch,handler);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Bounded incomplete fixture.'})
        assert wait_finished(env.service,child)['status']=='paused'
        assert_partial_private_state(env,child)
        assert len(calls)==1 and env.service.store.run(child['id'])['failure']['category']=='incomplete_provider_state'
        result=env.service.agent_result(env.service.store.run(parent['id']),{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert not result['retry_available']
    finally:client.close()


@pytest.mark.parametrize('pause',[True,False])
def test_pause_or_cancel_archives_partial_opaque_state_without_executing_tools(env,monkeypatch,pause):
    import forge_runs
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env,['web_search'])
    received=threading.Event();original=forge_runs.reasoning_details
    def observed(values):
        result=original(values)
        if result==DETAILS:received.set()
        return result
    monkeypatch.setattr(forge_runs,'reasoning_details',observed)
    packet=sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS}}]})
    body=TimedBody([(0,packet)],stall=True);calls=[]
    def handler(req):
        calls.append(req);return httpx.Response(200,stream=body)
    transport(monkeypatch,handler);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Bounded pause fixture.'})
        assert received.wait(3)
        env.service.jobs.cancel(child['id'],pause=pause)
        assert wait_finished(env.service,child)['status']==('paused' if pause else 'cancelled')
        assert_partial_private_state(env,child)
        assert len(calls)==1 and body.closed.is_set()
    finally:client.close()


@pytest.mark.parametrize('refusal_form',['finish_reason','same_packet_delta'])
def test_signed_refusal_remains_eligible_for_fresh_retry_without_state_replay(env,monkeypatch,refusal_form):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env,['web_search'])
    bodies=[]
    def handler(req):
        body=json.loads(req.content);bodies.append(body)
        assert body['model']==FREE_ROUTER
        if len(bodies)==1:
            if refusal_form=='same_packet_delta':
                packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS,'refusal':'Synthetic provider refusal.'}}]})]
            else:
                packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS}}]}),
                    sse({'choices':[{'delta':{},'finish_reason':'content_filter'}]})]
        else:
            assert 'reasoning_details' not in json.dumps(body) and 'synthetic-opaque-state' not in json.dumps(body)
            assert not any(message['role']=='tool' for message in body['messages'])
            packets=completion('Fresh, concrete synthetic findings.')
        return httpx.Response(200,stream=TimedBody([(0,p) for p in packets]))
    transport(monkeypatch,handler);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Original bounded refusal fixture.'})
        assert wait_finished(env.service,child)['status']=='paused'
        assert_partial_private_state(env,child)
        assert env.service.store.run(child['id'])['failure']['category']=='refusal'
        resumed=env.service.jobs.resume(child['id'])
        assert wait_finished(env.service,resumed)['status']=='paused' and len(bodies)==1
        assert env.service.store.run(child['id'])['failure']['category']=='refusal'
        result=env.service.agent_result(env.service.store.run(parent['id']),{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert result['retry_available']
        retried=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':child['id'],'text':'Clearer wording of the same bounded task.'})
        assert wait_finished(env.service,retried)['status']=='completed' and len(bodies)==2
        assert private_states(env.service.store,retried)=={}
        assert env.service.store.run(retried['id'])['retry_count']==1
    finally:client.close()


def test_signed_tool_continuation_pins_exact_free_model_and_keeps_state_private(env,monkeypatch):
    _,_,_,client=connected(env);pool_provider(env,client)
    parent,profile=helper(env,['web_search'])
    original=env.service.core.dispatch
    env.service.core.dispatch=lambda action,data: {'sources':[{'snippet':'Synthetic source'}]} if action=='research' else original(action,data)
    bodies=[]
    def handler(req):
        body=json.loads(req.content);bodies.append(body)
        if len(bodies)==1:
            assert body['model']==FREE_ROUTER
            packets=tool_packets('web_search',{'query':'fixture'},details=DETAILS)
        else:
            assert body['model']=='fixture/coder:free'
            assistant=next(m for m in body['messages'] if m['role']=='assistant')
            assert assistant['reasoning_details']==DETAILS
            assert '_provider_state' not in json.dumps(body)
            tool=next(m for m in body['messages'] if m['role']=='tool')
            assert tool['tool_call_id']=='fixture_call'
            packets=completion('Concrete synthetic findings.')
        return httpx.Response(200,stream=TimedBody([(0,p) for p in packets]))
    transport(monkeypatch,handler);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Original bounded assignment.'})
        assert wait_finished(env.service,child)['status']=='completed'
        saved=env.service.store.run(child['id'])
        assert saved['settings']['model']==FREE_ROUTER and saved['continuation_model']=='fixture/coder:free'
        assert saved['provider_generation_id'] is None or saved['provider_generation_id']=='gen-synthetic'
        with env.service.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM provider_turn_state WHERE run_id=?',(child['id'],)).fetchone()[0]==1
        public=[env.service.store.get_chat(child['chat_id']),env.service.store.run_chat(child['chat_id'],0),
            env.service.store.events(child['id']),env.service.agent_result(env.service.store.run(parent['id']),{'run_id':child['id'],'wait_seconds':0},threading.Event())]
        assert 'synthetic-opaque-state' not in json.dumps(public) and 'fixture-signature' not in json.dumps(public)
        assert len(bodies)==2
    finally:client.close()


@pytest.mark.parametrize('returned', ['fixture/unlisted','fixture/coder',FREE_ROUTER])
def test_unpinnable_router_identity_pauses_before_any_tool_execution(env,monkeypatch,returned):
    _,_,_,client=connected(env);pool_provider(env,client)
    parent,profile=helper(env,['web_search']);calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,stream=TimedBody([(0,p) for p in tool_packets('web_search',{'query':'fixture'},details=DETAILS,model=returned)]))
    transport(monkeypatch,handler);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Bounded fixture.'})
        assert wait_finished(env.service,child)['status']=='paused'
        saved=env.service.store.run(child['id'])
        assert saved['tools']==0 and saved['failure']['category']=='continuation_unavailable' and len(calls)==1
        assert not saved.get('continuation_model')
    finally:client.close()


def test_pin_validates_current_model_context_and_capabilities(env):
    _,provider,fixture,client=connected(env)
    try:
        fixture.models[1]['context_length']=8192
        with pytest.raises(OpenRouterFailure,match='Context'):
            provider.validate_continuation('fixture/coder:free',32768,['tools'])
        fixture.models[1]['context_length']=131072;fixture.models[1]['supported_parameters']=[]
        with pytest.raises(OpenRouterFailure,match='capability'):
            provider.validate_continuation('fixture/coder:free',32768,['tools'])
        fixture.models[1]['pricing']['prompt']='0.001'
        with pytest.raises(OpenRouterFailure,match='exact advertised free'):
            provider.validate_continuation('fixture/coder:free',32768,['tools'])
    finally:client.close()


def scripted_provider(env,rounds):
    info={'capabilities':['tools'],'model_info':{'openrouter.context_length':131072}}
    env.service.providers.provider=lambda _:SimpleNamespace(capabilities=lambda _:info)
    env.service.providers.capabilities=lambda *_:info
    requests=[]
    def generate(data,*args,**kwargs):
        requests.append(data)
        yield {'message':rounds.pop(0),'done':True,'done_reason':'stop','usage':{'completion_tokens':5}}
    env.service.providers.generate=generate
    return requests


def test_refused_artifact_read_is_projected_and_helper_can_correct_it(env):
    _,_,_,client=connected(env);parent,profile=helper(env,['artifact_read'])
    requests=scripted_provider(env,[{'tool_calls':[{'id':'fixture_call','function':{'name':'artifact_read','arguments':{'id':'not-assigned'}}}]},
        {'content':'The assigned artifact was unavailable; here is the concrete blocker.'}])
    launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Inspect assigned evidence.'})
        assert wait_finished(env.service,child)['status']=='completed' and len(requests)==2
        tool=next(m for m in requests[1]['messages'] if m['role']=='tool')
        assert json.loads(tool['content'])['not_executed'] and 'artifact' not in json.loads(tool['content'])
        assert env.service.store.run(child['id'])['tools']==1
    finally:client.close()


def test_empty_helper_result_is_not_successful_or_consumed(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    scripted_provider(env,[{'content':'','thinking':'Only hidden fixture thinking.'}]);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Return visible findings.'})
        assert wait_finished(env.service,child)['status']=='paused'
        result=env.service.agent_result(env.service.store.run(parent['id']),{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert result['response']=='' and not result['successful'] and not result['result_consumed']
        assert result['retry_available'] and result['run']['failure']['category']=='empty_result'
    finally:client.close()


def test_strict_review_refusal_is_not_projected(env):
    _,_,_,client=connected(env)
    try:
        child=env.service.jobs.start({'text':'Independent review.','provider_id':'openrouter','model':FREE_ROUTER,'mode':'goal_review'})
        invocation=child['id']+':1:0'
        env.service.store.invocation(invocation,child['id'],'artifact_read',{'id':'not-assigned'})
        env.service.store.invocation_state(invocation,'completed',{'result':{'not_executed':True,'coordinator_refusal':True}})
        with pytest.raises(CloudPolicyRejected,match='Saved cloud tool context'):
            CloudContextPolicy(env.service).prepare_round(env.service.store.run(child['id']),[],[])
    finally:client.close()


def test_private_state_survives_restart_without_public_metadata_and_save_is_atomic(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'State fixture.'})
        store=env.service.store
        row=store.save_assistant_turn(child['id'],'Synthetic progress.',provider_state={'provider_id':'openrouter','model_id':'fixture/coder:free','reasoning_details':DETAILS})
        reopened=ForgeStore(store.home)
        assert reopened.provider_turn_states(child['id'],[row['id']])[row['id']]['reasoning_details']==DETAILS
        assert reopened.provider_turn_states(parent['id'],[row['id']])=={}
        assert 'reasoning_details' not in json.dumps(reopened.get_chat(child['chat_id']))
        count=len(store.get_chat(child['chat_id'])['messages'])
        with pytest.raises(ValueError):
            store.save_assistant_turn(child['id'],'Do not save.',provider_state={'provider_id':'ollama','model_id':'fixture','reasoning_details':DETAILS})
        assert len(store.get_chat(child['chat_id'])['messages'])==count
    finally:client.close()


def test_private_state_binding_and_request_size_are_enforced(env):
    _,provider,_,client=connected(env)
    try:
        private={'provider_id':'openrouter','model_id':'fixture/coder:free','reasoning_details':DETAILS}
        data=request(messages=[{'role':'assistant','content':'','_provider_state':private},{'role':'user','content':'Continue.'}])
        body={'model':FREE_ROUTER,'messages':[{'role':'assistant','content':''},{'role':'user','content':'Continue.'}]}
        with pytest.raises(OpenRouterFailure,match='another model'):provider.prepare_body(data,body)
        huge=[{'type':'reasoning.encrypted','data':'x'*40000}]
        data=request(model='fixture/coder:free',context=2048,messages=[{'role':'assistant','content':'','_provider_state':{**private,'reasoning_details':huge}}])
        with pytest.raises(OpenRouterFailure,match='Context|size limit'):
            provider.prepare_body(data,{'model':'fixture/coder:free','messages':[{'role':'assistant','content':''}]})
        with pytest.raises(ValueError,match='size limit'):reasoning_details([{'data':'x'*MAX_PROVIDER_STATE_BYTES}])
    finally:client.close()


def test_inference_400_has_safe_typed_diagnostics_and_no_retry(env,monkeypatch):
    _,provider,_,client=connected(env);calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(400,json={'error':{'message':'private prompt synthetic-credential'}})
    transport(monkeypatch,handler)
    try:
        with pytest.raises(OpenRouterFailure) as error:list(provider.generate(request(),threading.Event()))
        assert error.value.diagnostics()=={'provider_id':'openrouter','category':'invalid_request','http_status':400}
        assert 'inference request' in str(error.value) and 'metadata' not in str(error.value)
        assert 'private prompt' not in str(error.value) and len(calls)==1
    finally:client.close()


def test_reasoning_control_honors_explicit_free_router_setting(env):
    _,provider,_,client=connected(env)
    try:
        for enabled in (True,False):
            safe=provider.prepare_body(request(thinking=enabled),{'model':FREE_ROUTER,'messages':[]})
            assert safe['reasoning']=={'enabled':enabled}
    finally:client.close()


def test_opaque_reasoning_chunks_count_as_progress_without_exposing_blobs(env,monkeypatch):
    _,provider,_,client=connected(env)
    monkeypatch.setattr(Core,'_inference_timeouts',lambda **kwargs:InferenceTimeouts(first_response=.2,generation_idle=.12,total=1))
    chunks=[(.08,sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':[DETAILS[1]]}}]})) for _ in range(3)]
    chunks.append((.08,sse({'choices':[{'delta':{'content':'Visible result.'},'finish_reason':'stop'}]})))
    transport(monkeypatch,lambda req:httpx.Response(200,stream=TimedBody(chunks)))
    try:
        packets=list(provider.generate(request(),threading.Event()))
        assert packets[-1]['done'] and packets[-1]['_provider_reasoning_details']==[DETAILS[1]]*3
        assert all('synthetic-opaque-state' not in json.dumps(p.get('message',{})) for p in packets)
    finally:client.close()


def test_filtered_finish_is_a_sanitized_refusal(env,monkeypatch):
    _,provider,_,client=connected(env)
    transport(monkeypatch,lambda req:httpx.Response(200,stream=TimedBody([(0,sse({'choices':[{'delta':{},'finish_reason':'content_filter'}]}))])))
    try:
        with pytest.raises(OpenRouterFailure) as error:list(provider.generate(request(),threading.Event()))
        assert error.value.category=='refusal'
    finally:client.close()


def test_retry_is_fresh_scoped_idempotent_and_limited_to_three_extra_attempts(env):
    _,_,_,client=connected(env);parent,profile=helper(env,['artifact_read'])
    try:
        first=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Preserve this original objective.','required':False})
        env.service.store.update_run(first['id'],status='paused',failure={'provider_id':'openrouter','category':'refusal'})
        previous=first
        for attempt in range(2,5):
            args={'agent_id':profile['id'],'parent_id':parent['id'],'text':'A clearer phrasing of the same objective.',
                'retry_of':previous['id'],'source_key':'retry-fixture-'+str(attempt),'cloud_scope':{'files':['.env']}}
            retried=env.service.agent_start(args)
            repeated=env.service.agent_start(args)
            assert retried['id']==repeated['id'] and repeated['reused']
            saved=env.service.store.run(retried['id'])
            assert saved['assignment_id']==first['id'] and saved['retry_count']==attempt-1 and saved['attempt']==attempt
            assert saved['settings']['model']==FREE_ROUTER and not saved.get('continuation_model')
            assert saved['readonly'] is True
            assert saved['cloud_scope']['files']==[] and not saved['assignment_required']
            assert saved['assignment_original_request']=='Preserve this original objective.'
            assert len(env.service.store.get_chat(retried['chat_id'])['messages'])==1
            env.service.store.update_run(retried['id'],status='paused',failure={'provider_id':'openrouter','category':'empty_result' if attempt%2 else 'refusal'})
            previous=retried
        with pytest.raises(ValueError,match='three extra attempts'):
            env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':previous['id'],'text':'Another phrasing.'})
        assert env.service.jobs._launch.call_count==5  # parent + initial + three fresh attempts
    finally:client.close()


@pytest.mark.parametrize('provider_id',['ollama','synthetic-compatible'])
def test_retry_rejects_profile_provider_changes_before_admission(env,provider_id):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        first=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Preserve this scoped objective.'})
        env.service.store.update_run(first['id'],status='paused',failure={'category':'refusal'})
        original=env.service.store.run(first['id'])
        if provider_id!='ollama':
            env.service.store.save_entity('providers',{'id':provider_id,'kind':'compatible','enabled':True})
        changed=env.service.agent_save({'id':profile['id'],'provider_id':provider_id,'role':'coder','model':'fixture'})
        before=len(env.service.store.runs());launches=env.service.jobs._launch.call_count
        with pytest.raises(ValueError,match='profile provider changed'):
            env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':first['id'],
                'text':'A clearer phrasing.','source_key':'changed-provider-retry'})
        assert len(env.service.store.runs())==before and env.service.jobs._launch.call_count==launches
        assert env.service.store.entity('agents',profile['id'])==changed
        assert env.service.store.run(first['id'])==original
        with env.service.store._connection() as db:
            assert db.execute('SELECT 1 FROM request_keys WHERE source_key=?',('changed-provider-retry',)).fetchone() is None
    finally:client.close()


def test_successful_retry_resolves_failed_predecessor_and_keeps_required_contract(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        first=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Required original objective.'})
        env.service.store.update_run(first['id'],status='paused',failure={'category':'empty_result'})
        with pytest.raises(ValueError,match='required'):
            env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':first['id'],'required':False,'text':'Clearer.'})
        second=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':first['id'],'text':'Clearer.'})
        env.service.store.add_message(second['chat_id'],'assistant','Concrete successful evidence.')
        env.service.store.update_run(second['id'],status='completed')
        result=env.service.agent_result(env.service.store.run(parent['id']),{'run_id':second['id'],'wait_seconds':0},threading.Event())
        assert result['successful'] and result['required']
        assert env.service.store.run(first['id'])['result_consumed']
        assert env.service.store.run(first['id'])['assignment_result_id']==second['id']
    finally:client.close()


@pytest.mark.parametrize('category', ['rate_limit','quota','network','protocol','continuation_unavailable',
    'authentication','configuration','consent','timeout','metadata','context','incomplete_provider_state',None])
def test_other_failure_categories_have_no_retry_admission_or_retry_offer(env,category):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Preserve the original assignment.'})
        env.service.store.update_run(child['id'],status='paused',failure={'provider_id':'openrouter','category':category})
        result=env.service.agent_result(env.service.store.run(parent['id']),{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert not result['retry_available']
        before=len(env.service.store.runs());launches=env.service.jobs._launch.call_count
        with pytest.raises(ValueError,match='Only refused or empty'):
            env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':child['id'],
                'text':'A reworded task cannot fix this failure.','source_key':'forbidden-retry-fixture'})
        assert len(env.service.store.runs())==before and env.service.jobs._launch.call_count==launches
        with env.service.store._connection() as db:
            assert db.execute('SELECT 1 FROM request_keys WHERE source_key=?',('forbidden-retry-fixture',)).fetchone() is None
    finally:client.close()


@pytest.mark.parametrize('status', ['running','awaiting_approval','waiting_question','completed','cancelled'])
def test_eligible_category_cannot_retry_active_cancelled_or_completed_runs(env,status):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Original assignment.'})
        env.service.store.update_run(child['id'],status=status,failure={'category':'refusal'})
        before=len(env.service.store.runs())
        with pytest.raises(ValueError,match='Only a failed or interrupted'):
            env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':child['id'],'text':'Clearer phrasing.'})
        assert len(env.service.store.runs())==before
    finally:client.close()


@pytest.mark.parametrize('terminal', ['completed','cancelled'])
def test_pause_and_cancel_preserve_terminal_runs_even_if_completion_races(env,monkeypatch,terminal):
    run=env.service.jobs.start({'text':'Lifecycle fixture.'})
    original=env.service.store.run
    once=[False]
    def stale(identifier):
        value=original(identifier)
        if identifier==run['id'] and not once[0]:
            once[0]=True
            env.service.store.update_run(identifier,status=terminal)
        return value
    monkeypatch.setattr(env.service.store,'run',stale)
    assert env.service.jobs.cancel(run['id'],pause=True)['unchanged']
    assert original(run['id'])['status']==terminal
    assert env.service.jobs.cancel(run['id'])['unchanged']


def test_pause_preserves_suspended_failure_and_cancel_can_close_it(env):
    run=env.service.jobs.start({'text':'Suspended fixture.'})
    env.service.store.update_run(run['id'],status='failed')
    assert env.service.jobs.cancel(run['id'],pause=True)['unchanged']
    assert env.service.store.run(run['id'])['status']=='failed'
    env.service.jobs.cancel(run['id'])
    assert env.service.store.run(run['id'])['status']=='cancelled'
