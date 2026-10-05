"""Remote publishing traverses the real durable service approval boundary."""
import json
import threading
import time

import httpx
import pytest

from forge_github import GitHubConnection
from forge_service import ForgeService
from test_forge_core import Engine, call, finished
from test_forge_github import Vault, TOKEN, repo

ARGS={'repo':'example/project','title':'Reviewed title','body':'Reviewed body','head':'feature','base':'main'}


@pytest.fixture
def connected_service(tmp_path):
    engine=Engine()
    service=ForgeService(core=engine,data_dir=tmp_path/'forge')
    service.store.update_settings({'model':'fixture','context':32768,'permission_profile':'full_access',
                                   'browser_tools':False,'memory_suggestions':False})
    folder=tmp_path/'selected-project';folder.mkdir()
    project=service.store.create_project('Selected project',str(folder))
    vault=Vault();requests=[];route={'login':'fixture-account','write':'ok'}
    def handle(request):
        requests.append(request)
        if request.url.path=='/user':return httpx.Response(200,json={'login':route['login']})
        if request.url.path=='/repos/example/project':return httpx.Response(200,json=repo())
        if request.method=='POST':
            if route['write']=='disconnect':raise httpx.ReadError(TOKEN,request=request)
            if route['write']=='server_error':return httpx.Response(503,json={'message':TOKEN})
            if route['write']=='incomplete':return httpx.Response(201,json={'message':'Partial response'})
            return httpx.Response(201,json={'number':23,'draft':True,'title':'Reviewed title',
                                           'html_url':'https://github.com/example/project/pull/23'})
        if request.url.path.endswith('/pulls'):return httpx.Response(200,json=[])
        return httpx.Response(404,json={'message':TOKEN})
    service.github_manager=GitHubConnection(service,vault=vault,
        client_factory=lambda **kwargs:httpx.Client(transport=httpx.MockTransport(handle),**kwargs))
    service.dispatch('github_connect',{'token':TOKEN})
    service.dispatch('github_select_repo',{'full_name':'example/project','project_id':project['id']})
    yield service,engine,project,requests,route
    service.shutdown()


def pending(service,run):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        approval=next((e for e in service.jobs.poll(run['id'])['events'] if e['type']=='approval'),None)
        if approval:return approval
        time.sleep(.01)
    raise AssertionError('Fixture approval did not appear.')


def start(env,name='github__create_pull_request',args=None,**options):
    service,engine,project,_,_=env
    engine.rounds=[{'tool_calls':[call(name,ARGS if args is None else args)]},{'content':'Finished fixture.'}]
    return service.dispatch('start_chat',{'text':'Review this repository action.','project_id':project['id'],**options})


@pytest.mark.parametrize('allowed',[False,True])
def test_full_access_still_waits_for_specific_human_pr_approval(connected_service,allowed):
    service,_,_,requests,_=connected_service
    run=start(connected_service)
    approval=pending(service,run)
    assert approval['tool']=='github__create_pull_request'
    assert approval['target']=='GitHub example/project' and approval['arguments']==ARGS
    assert not [r for r in requests if r.method=='POST']
    with pytest.raises(ValueError,match='no longer pending'):
        service.dispatch('approve',{'run_id':run['id'],'approval_id':'wrong','allowed':True})
    service.dispatch('approve',{'run_id':run['id'],'approval_id':approval['id'],'allowed':allowed})
    assert finished(service,run)['status']=='completed'
    writes=[r for r in requests if r.method=='POST']
    assert len(writes)==int(allowed)
    with service.store._connection() as db:
        record=db.execute('SELECT * FROM approvals WHERE id=?',(approval['id'],)).fetchone()
        assert record['status']==('used' if allowed else 'denied')
        invocation=record['invocation_id']
    if allowed:
        assert json.loads(writes[0].content)=={**{k:v for k,v in ARGS.items() if k!='repo'},'draft':True}
        result=service.jobs.registry.execute(service.store.run(run['id']),'github__create_pull_request',ARGS,
            threading.Event(),invocation_id=invocation)
        assert result['not_executed']
        assert len([r for r in requests if r.method=='POST'])==1


def test_readonly_schema_and_selected_repository_listing_use_trusted_project_scope(connected_service):
    service,engine,project,requests,_=connected_service
    run=start(connected_service,'github__list_repos',{},readonly=True)
    assert finished(service,run)['status']=='completed'
    schemas=engine.requests[0]['tools']
    names={s['function']['name'] for s in schemas if s['function']['name'].startswith('github__')}
    assert {'github__list_repos','github__read_file','github__read_issue','github__list_pull_requests'}<=names
    assert 'github__create_pull_request' not in names
    events=service.jobs.poll(run['id'])['events']
    listing=next(e for e in events if e['type']=='tool' and e.get('state')=='done')['result']['result']
    assert listing['repositories'][0]['project_id']==project['id']
    assert not any(e['type']=='approval' for e in events)
    assert len(requests)==2  # Connection and selection only; listing is local.


def test_malformed_repository_target_is_known_not_executed(connected_service):
    service,_,_,requests,_=connected_service
    run=start(connected_service,args={**ARGS,'repo':'https://evil.example/private'})
    assert finished(service,run)['status']=='completed'
    events=service.jobs.poll(run['id'])['events']
    result=next(e for e in events if e['type']=='tool' and e.get('state')=='done')['result']['result']
    assert result['not_executed'] and 'owner/name' in result['error']
    assert not any(e['type']=='approval' for e in events)
    assert not [r for r in requests if r.method=='POST']
    assert service.store.unknown_actions(run['id'])==[]


@pytest.mark.parametrize('same_account',[False,True])
def test_reconnection_during_review_invalidates_pending_action(connected_service,same_account):
    service,_,project,requests,route=connected_service
    run=start(connected_service)
    approval=pending(service,run)
    if not same_account:route['login']='another-fixture-account'
    service.dispatch('github_connect',{'token':'replacement_fixture_token'})
    service.dispatch('github_select_repo',{'full_name':'example/project','project_id':project['id']})
    service.dispatch('approve',{'run_id':run['id'],'approval_id':approval['id'],'allowed':True})
    assert finished(service,run)['status']=='completed'
    assert not [r for r in requests if r.method=='POST']
    assert service.store.unknown_actions(run['id'])==[]


@pytest.mark.parametrize('failure',['disconnect','server_error','incomplete'])
def test_approved_remote_write_with_uncertain_outcome_pauses_and_cannot_replay(connected_service,failure):
    service,_,_,requests,route=connected_service
    route['write']=failure
    run=start(connected_service)
    approval=pending(service,run)
    service.dispatch('approve',{'run_id':run['id'],'approval_id':approval['id'],'allowed':True})
    result=finished(service,run)
    assert result['status']=='paused'
    assert len([r for r in requests if r.method=='POST'])==1
    assert service.store.unknown_actions(run['id'])
    assert TOKEN not in json.dumps(result)
    with pytest.raises(ValueError,match='unknown'):
        service.dispatch('resume',{'id':run['id']})
    assert len([r for r in requests if r.method=='POST'])==1


def test_live_deny_blocks_reviewed_pr_even_when_original_run_had_full_access(connected_service):
    service,_,_,requests,_=connected_service
    run=start(connected_service)
    approval=pending(service,run)
    service.dispatch('settings',{'permission_profile':'deny_access'})
    service.dispatch('approve',{'run_id':run['id'],'approval_id':approval['id'],'allowed':True})
    assert finished(service,run)['status']=='completed'
    assert not [r for r in requests if r.method=='POST']
