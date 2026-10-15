"""Synthetic errors and helper correction; never authenticate or infer remotely."""
from datetime import datetime, timedelta, timezone
import gzip
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from forge_ai_workflow import AIWorkflow
from forge_helper_findings import completion_issue
from forge_openrouter import OpenRouterFailure
from forge_openrouter_errors import capacity, classify, recovery_diagnostics
from forge_service import ForgeService
from test_forge_channels import env
from test_forge_openrouter import connected, pool_provider, request, sse
from test_forge_openrouter_continuation import DETAILS, helper, launch, scripted_provider
from test_forge_workspaces import ScriptedEngine, wait_finished
from test_inference_timeouts import TimedBody, transport


CANARY='synthetic-private-error-canary'


@pytest.mark.parametrize('streamed',[False,True])
@pytest.mark.parametrize('status,metadata,expected',[
    (401,{'error_type':'refusal'},'authentication'),
    (403,{'error_type':'refusal'},'refusal'),
    (403,{'error_type':'permission_denied'},'access_denied'),
    (403,{'error_type':'content_policy_violation'},'access_denied'),
    (403,{},'access_denied'),
    (429,{'error_type':'rate_limit_exceeded'},'rate_limit'),
    (429,{'error_type':'rate_limit_exceeded','limit_source':'openrouter_free_tier_daily'},'quota'),
    (429,{'error_type':'rate_limit_exceeded','limit_source':'upstream_provider_shared_pool'},'provider_overloaded'),
    (503,{'error_type':'provider_overloaded'},'provider_overloaded'),
    (402,{},'free_route'),
    (403,{'limit_source':'openrouter_key_limit'},'access_denied'),
])
def test_http_and_streamed_errors_keep_specific_safe_recovery(env,monkeypatch,streamed,status,metadata,expected):
    _,provider,_,client=connected(env);calls=[]
    error={'code':status,'message':CANARY,'metadata':{**metadata,'raw':CANARY,'provider_name':CANARY,'headers':{'Authorization':CANARY}}}
    def handle(req):
        calls.append(req)
        if streamed:return httpx.Response(200,headers={'Retry-After':'5'},stream=TimedBody([(0,sse({'model':'fixture/coder:free','error':error}))]))
        return httpx.Response(status,headers={'Retry-After':'5'},json={'error':error})
    transport(monkeypatch,handle)
    try:
        with pytest.raises(OpenRouterFailure) as raised:list(provider.generate(request(),threading.Event()))
        diagnostic=raised.value.diagnostics()
        assert diagnostic['category']==expected and diagnostic['http_status']==status
        assert diagnostic['retry_after_seconds']==5
        assert CANARY not in str(raised.value)+json.dumps(diagnostic)
        assert len(calls)==1
        if expected!='authentication':assert 'rejected the API key' not in str(raised.value)
        if expected=='rate_limit':assert 'exhausted request' not in str(raised.value)
    finally:client.close()


@pytest.mark.parametrize('value',[{},None,{'error_type':[],'limit_source':{}},
    {'error_type':CANARY,'limit_source':CANARY,'retry_after_seconds':float('inf'),'raw':CANARY},
    {'error_type':'refusal','retry_after_seconds':True}])
def test_diagnostic_projection_is_bounded_and_drops_opaque_remote_fields(value):
    safe=recovery_diagnostics(value)
    assert CANARY not in json.dumps(safe)
    assert set(safe)<={'error_type','limit_source','retry_after_seconds','cause_category'}
    assert 'retry_after_seconds' not in safe


@pytest.mark.parametrize('value',['NaN','inf','-1','0','86401','x'*101,'not a date'])
def test_invalid_retry_after_is_not_recovery_advice(value):
    category,safe=classify(429,headers={'Retry-After':value})
    assert category=='rate_limit' and 'retry_after_seconds' not in safe


def test_remote_metadata_cannot_assign_a_local_failure_cause():
    assert 'cause_category' not in classify(429,{'metadata':{'cause_category':'authentication'}})[1]


def test_authentication_status_cannot_be_rewritten_by_budget_metadata():
    assert classify(401,{'metadata':{'limit_source':'openrouter_free_tier_daily','error_type':'refusal'}})[0]=='authentication'


def test_http_error_body_is_read_from_chunks_before_safe_projection(env,monkeypatch):
    _,provider,_,client=connected(env)
    encoded=json.dumps({'error':{'code':403,'message':CANARY,'metadata':{'error_type':'refusal','raw':CANARY}}}).encode()
    body=TimedBody([(0,encoded[:20]),(.01,encoded[20:])])
    transport(monkeypatch,lambda req:httpx.Response(403,stream=body))
    try:
        with pytest.raises(OpenRouterFailure) as error:list(provider.generate(request(),threading.Event()))
        assert error.value.category=='refusal' and CANARY not in str(error.value)+json.dumps(error.value.diagnostics())
        assert body.closed.is_set()
    finally:client.close()


@pytest.mark.parametrize('status,metadata,category',[
    (403,{'error_type':'refusal'},'refusal'),
    (429,{'error_type':'provider_overloaded','limit_source':'upstream_provider_shared_pool'},'provider_overloaded')])
def test_compressed_http_error_keeps_status_request_retry_after_and_private_projection(env,monkeypatch,status,metadata,category):
    _,provider,_,client=connected(env);calls=[]
    decoded=json.dumps({'error':{'code':status,'message':CANARY,'metadata':{**metadata,'raw':CANARY}}}).encode()
    encoded=gzip.compress(decoded)
    body=TimedBody([(0,encoded[:17]),(0,encoded[17:])])
    def handle(request):
        calls.append(request)
        return httpx.Response(status,headers={'Content-Encoding':'gzip','Content-Length':str(len(encoded)),
            'Retry-After':'5'},stream=body)
    transport(monkeypatch,handle)
    original=provider.response_error
    def projected(response):
        assert response.request is calls[0] and response.status_code==status
        assert response.headers['Retry-After']=='5'
        assert response.headers['Content-Length']==str(len(decoded))
        return original(response)
    monkeypatch.setattr(provider,'response_error',projected)
    try:
        with pytest.raises(OpenRouterFailure) as raised:list(provider.generate(request(),threading.Event()))
        diagnostic=raised.value.diagnostics()
        assert diagnostic['category']==category and diagnostic['http_status']==status
        assert diagnostic['retry_after_seconds']==5 and diagnostic['error_type']==metadata['error_type']
        assert CANARY not in str(raised.value)+json.dumps(diagnostic)
        assert len(calls)==1 and body.closed.is_set()
    finally:client.close()


def test_compressed_http_error_bound_counts_decoded_bytes(env,monkeypatch):
    from inference_stream import MAX_PACKET_BYTES
    _,provider,_,client=connected(env)
    encoded=gzip.compress(b' '*(MAX_PACKET_BYTES+1))
    body=TimedBody([(0,encoded)])
    transport(monkeypatch,lambda request:httpx.Response(429,headers={'content-encoding':'gzip'},stream=body))
    try:
        with pytest.raises(OpenRouterFailure) as raised:list(provider.generate(request(),threading.Event()))
        assert raised.value.category=='rate_limit' and raised.value.http_status==429
        assert body.closed.is_set()
    finally:client.close()


def test_oversized_streamed_http_error_keeps_only_safe_status(env,monkeypatch):
    from inference_stream import MAX_PACKET_BYTES
    _,provider,_,client=connected(env)
    body=TimedBody([(0,b'{"error":{"message":"'+CANARY.encode()),(0,b'x'*(MAX_PACKET_BYTES+1))])
    transport(monkeypatch,lambda req:httpx.Response(429,stream=body))
    try:
        with pytest.raises(OpenRouterFailure) as error:list(provider.generate(request(),threading.Event()))
        assert error.value.category=='rate_limit' and error.value.http_status==429
        assert CANARY not in str(error.value)+json.dumps(error.value.diagnostics())
        assert body.closed.is_set()
    finally:client.close()


def test_error_after_reasoning_preserves_safety_and_original_cause_without_payload(env,monkeypatch):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env,['web_search'])
    packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS}}]}),
        sse({'model':'fixture/coder:free','error':{'code':429,'message':CANARY,'metadata':{
            'error_type':'rate_limit_exceeded','limit_source':'upstream_provider_shared_pool','raw':CANARY}}})]
    transport(monkeypatch,lambda req:httpx.Response(200,headers={'Retry-After':'5'},stream=TimedBody([(0,p) for p in packets])))
    launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Bounded synthetic question.'})
        assert wait_finished(env.service,child,timeout=45)['status']=='paused'
        failure=env.service.store.run(child['id'])['failure']
        assert failure=={'provider_id':'openrouter','category':'incomplete_provider_state','http_status':429,
            'error_type':'rate_limit_exceeded','limit_source':'upstream_provider_shared_pool','retry_after_seconds':5,
            'cause_category':'provider_overloaded'}
        result=env.service.agent_result(parent,{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert not result['successful'] and not result['retry_available']
        diagnostic=env.service.dispatch('run_diagnostics_export',{'run_id':child['id']})
        assert diagnostic['cause_category']=='provider_overloaded' and diagnostic['retry_after_seconds']==5
        assert CANARY not in json.dumps([result,diagnostic,env.service.store.events(child['id'])])
        assert 'synthetic-opaque-state' not in json.dumps([result,diagnostic])
    finally:client.close()


@pytest.mark.parametrize('signed',[False,True])
def test_streamed_explicit_refusal_offers_fresh_assignment_instead_of_key_replacement(env,monkeypatch,signed):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env)
    packet=sse({'model':'fixture/coder:free','error':{'code':403,'message':CANARY,'metadata':{'error_type':'refusal'}}})
    packets=([sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS}}]})] if signed else [])+[packet]
    transport(monkeypatch,lambda req:httpx.Response(200,stream=TimedBody([(0,p) for p in packets])));launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Original bounded question.'})
        assert wait_finished(env.service,child,timeout=45)['status']=='paused'
        result=env.service.agent_result(parent,{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert result['retry_available'] and result['retries_remaining']==3
        assert result['run']['failure']['category']=='refusal'
        assert CANARY not in json.dumps(result)
    finally:client.close()


def test_oversized_metadata_error_is_bounded_without_persisting_remote_body(env):
    from forge_openrouter import OpenRouterProvider
    _,provider,_,client=connected(env)
    stream=httpx.ByteStream(b'{"error":{"message":"'+CANARY.encode()+b'x'*65536)
    probe=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(403,stream=stream)))
    provider.http_client=probe
    try:
        with pytest.raises(OpenRouterFailure) as error:provider.account()
        assert error.value.category=='metadata' and CANARY not in str(error.value)
    finally:
        probe.close();client.close()


def test_signed_intent_correction_pins_exact_model_and_preserves_opaque_state(env,monkeypatch):
    _,_,_,client=connected(env);pool_provider(env,client);parent,profile=helper(env);requests=[]
    def handle(req):
        body=json.loads(req.content);requests.append(body)
        if len(requests)==1:
            packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'reasoning_details':DETAILS,'content':'I will inspect the project.'},'finish_reason':'stop'}]})]
        else:
            assert body['model']=='fixture/coder:free'
            assistant=next(message for message in body['messages'] if message['role']=='assistant')
            assert assistant['reasoning_details']==DETAILS
            packets=[sse({'model':'fixture/coder:free','choices':[{'delta':{'content':'The supplied function returns 42.'},'finish_reason':'stop'}]})]
        return httpx.Response(200,stream=TimedBody([(0,p) for p in packets]))
    transport(monkeypatch,handle);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Answer the bounded synthetic question.'})
        assert wait_finished(env.service,child,timeout=45)['status']=='completed' and len(requests)==2
        saved=env.service.store.run(child['id'])
        assert saved['continuation_model']=='fixture/coder:free' and saved['helper_result_corrections']==1
        result=env.service.agent_result(parent,{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert result['successful'] and 'synthetic-opaque-state' not in json.dumps(result)
    finally:client.close()


@pytest.mark.parametrize('age,state',[(0,'available'),(300,'available'),(301,'stale'),(2592000,'stale'),(-1,'stale')])
def test_capacity_snapshot_expires_without_claiming_account_exhaustion(age,state):
    now=datetime(2026,10,16,12,tzinfo=timezone.utc)
    config={'account_checked_at':(now-timedelta(seconds=age)).isoformat(),
        'account':{'free_model_daily_requests':{'used':4,'limit':50,'remaining':46}}}
    value=capacity(config,now=now)
    assert value['state']==state
    if state=='stale':assert value['remaining'] is None


def test_capacity_expires_at_utc_reset_and_unknown_metadata_stays_unknown():
    checked=datetime(2026,10,15,23,59,40,tzinfo=timezone.utc)
    config={'account_checked_at':checked.isoformat(),'account':{'free_model_daily_requests':{'used':50,'limit':50,'remaining':0}}}
    fresh=capacity(config,now=checked)
    assert fresh['state']=='exhausted' and fresh['expires_at']=='2026-10-16T00:00:00+00:00'
    assert capacity(config,now=checked+timedelta(seconds=30))['state']=='stale'
    assert capacity({'account':[]})['state']=='unknown'
    assert capacity({**config,'account_checked_at':'not a timestamp'})['state']=='unknown'
    assert capacity({**config,'account_checked_at':CANARY})['checked_at'] is None
    assert capacity({**config,'account_checked_at':'x'*10000})['checked_at'] is None


def test_stale_daily_zero_does_not_gate_inference_or_claim_current_exhaustion(env,monkeypatch):
    _,provider,_,client=connected(env)
    config=env.service.store.entity('providers','openrouter')
    env.service.store.save_entity('providers',{**config,'account_checked_at':'2000-01-01T00:00:00Z',
        'account':{'free_model_daily_requests':{'used':50,'limit':50,'remaining':0}}})
    transport(monkeypatch,lambda req:httpx.Response(200,stream=TimedBody([(0,sse({'model':'fixture/coder:free','choices':[{'delta':{'content':'42'},'finish_reason':'stop'}]}))])))
    try:
        assert AIWorkflow(env.service).status()['free_capacity']['state']=='stale'
        assert list(provider.generate(request(),threading.Event()))[-1]['done']
    finally:client.close()


@pytest.mark.parametrize('text',[
    'I will inspect the project and report my findings.', "Sure, I'll review the source.",
    'I will investigate. I will report back.', 'Let me check the logs.', 'Understood!', 'On it.', 'Done.',
    'I will inspect app.py and report my findings.', 'I will review the failed check.',
])
def test_acknowledgments_and_intent_only_responses_are_unusable(text):
    assert completion_issue(text)=='unusable_result'


@pytest.mark.parametrize('text',[
    '42', 'Use a bounded queue.', 'I found no issues in the supplied code.',
    'The function returns None for an empty list.', 'I will recommend a lock because the update races.',
    '```python\nprint(42)\n```', 'The source confirms it: https://example.invalid/fixture',
])
def test_concise_direct_answers_and_findings_do_not_require_tool_calls(text):
    assert completion_issue(text) is None


@pytest.mark.parametrize('answer,assignment',[('OK','Reply only OK.'),('Done','Say exactly "Done".'),
    ('Understood!','Please respond with Understood!')])
def test_literal_response_assignments_can_use_a_brief_acknowledgment(answer,assignment):
    assert completion_issue(answer,assignment) is None


def test_one_correction_reuses_completed_evidence_and_counts_rounds(env):
    _,_,_,client=connected(env);parent,profile=helper(env,['artifact_read'])
    artifact=env.service.store.artifact({'content':'Synthetic fixture evidence.'})
    requests=scripted_provider(env,[{'tool_calls':[{'id':'fixture-call','function':{'name':'artifact_read','arguments':{'id':artifact}}}]},
        {'content':'I will inspect the project and report my findings.'}, {'content':'Found a bounded queue in the supplied evidence.'}]);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Inspect this synthetic evidence.',
            'cloud_scope':{'artifacts':[artifact]}})
        assert wait_finished(env.service,child,timeout=45)['status']=='completed'
        run=env.service.store.run(child['id'])
        assert run['helper_result_corrections']==1 and run['rounds']==3 and run['tools']==1
        assert len(requests)==3 and any(m['role']=='tool' for m in requests[-1]['messages'])
        with env.service.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='artifact_read'",(child['id'],)).fetchone()[0]==1
    finally:client.close()


def test_inadequate_result_cannot_extend_round_limits(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    env.service.agent_save({'id':profile['id'],'rounds':1})
    requests=scripted_provider(env,[{'content':'I will inspect the project.'}]);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Return concrete findings.'})
        assert wait_finished(env.service,child,timeout=45)['status']=='paused'
        assert env.service.store.run(child['id'])['helper_result_corrections']==1 and len(requests)==1
        assert 'round limit reached' in env.service.store.run(child['id'])['recovery']
    finally:client.close()


def test_correction_counter_and_request_roll_back_together_then_commit_once(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Inspect synthetic evidence.'})
        env.service.store.update_run(child['id'],status='running')
        with env.service.store._connection(transaction='write') as db:
            db.execute("CREATE TRIGGER reject_synthetic_correction BEFORE INSERT ON messages WHEN NEW.role='user' AND NEW.content LIKE 'Your last response stated an intention%' BEGIN SELECT RAISE(ABORT,'Synthetic correction write interruption'); END")
        with pytest.raises(ValueError,match='Synthetic correction write interruption'):
            env.service.store.request_helper_correction(child['id'])
        assert env.service.store.run(child['id'])['helper_result_corrections']==0
        messages=env.service.store.get_chat(child['chat_id'])['messages']
        assert len(messages)==1
        with env.service.store._connection(transaction='write') as db:db.execute('DROP TRIGGER reject_synthetic_correction')
        assert env.service.store.request_helper_correction(child['id']) is True
        assert env.service.store.request_helper_correction(child['id']) is False
        assert env.service.store.run(child['id'])['helper_result_corrections']==1
        messages=env.service.store.get_chat(child['chat_id'])['messages']
        assert len(messages)==2 and messages[-1]['interaction_run_id']==child['id']
    finally:client.close()


def test_explicit_blocker_is_reported_but_does_not_satisfy_required_work(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    scripted_provider(env,[{'content':'Blocked: the assigned source file is unavailable, so I cannot verify the requested behavior.'}]);launch(env)
    try:
        child=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Verify the assigned source.'})
        assert wait_finished(env.service,child,timeout=45)['status']=='paused'
        result=env.service.agent_result(parent,{'run_id':child['id'],'wait_seconds':0},threading.Event())
        assert result['response'].startswith('Blocked:') and result['required']
        assert not result['successful'] and not result['result_consumed'] and not result['retry_available']
        assert result['run']['failure']['category']=='assignment_blocked'
    finally:client.close()


def test_unusable_result_correction_survives_restart_and_three_fresh_attempt_limit(env):
    _,_,_,client=connected(env);parent,profile=helper(env)
    scripted_provider(env,[{'content':'I will inspect the project.'},{'content':'I will report my findings.'}]);launch(env)
    fresh=None
    try:
        first=env.service.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'text':'Original bounded task.'})
        assert wait_finished(env.service,first,timeout=45)['status']=='paused'
        assert env.service.store.run(first['id'])['failure']['category']=='unusable_result'
        fresh=ForgeService(core=ScriptedEngine(),data_dir=env.service.store.home)
        fresh.vault=env.vault;fresh.providers.vault=env.vault;fresh.jobs._launch=Mock()
        fresh.jobs.resume(parent['id'])
        previous=first
        for retry in range(1,4):
            wrapped=SimpleNamespace(service=fresh)
            requests=scripted_provider(wrapped,[{'content':'I will inspect the project.'}]);launch(wrapped)
            child=fresh.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':previous['id'],
                'text':'Clearer wording of the original bounded task.'})
            assert wait_finished(fresh,child,timeout=45)['status']=='paused'
            run=fresh.store.run(child['id'])
            assert run['helper_result_corrections']==1 and run['retry_count']==retry and run['assignment_id']==first['id']
            assert len(requests)==1 and run['rounds']==1
            previous=child
        with pytest.raises(ValueError,match='three extra attempts'):
            fresh.agent_start({'agent_id':profile['id'],'parent_id':parent['id'],'retry_of':previous['id'],'text':'Another wording.'})
    finally:
        if fresh:fresh.shutdown()
        client.close()
