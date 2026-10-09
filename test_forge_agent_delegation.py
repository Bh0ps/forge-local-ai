"""Cloud helpers are scoped, discoverable and waitable without live credentials."""
import json
import threading
import time
from types import MethodType

import httpx
import pytest

from test_forge_channels import env
from test_forge_openrouter import connected, completion, pool_provider, usage_rows
from test_inference_timeouts import TimedBody, transport
from test_forge_workspaces import wait_finished, tool
from forge_runs import RunManager


def setup(env):
    connection, _, _, client=connected(env)
    result=env.service.dispatch('openrouter_setup_agents')
    client.close()
    return result


def parent(env,**changes):
    result=env.service.jobs.start({'text':'Coordinate one synthetic investigation.','auto_delegate':True,**changes})
    return env.service.store.run(result['id'])


def test_cloud_setup_requires_existing_consent_and_is_atomic(env):
    before=env.service.store.entities('agents')
    with pytest.raises(ValueError): env.service.dispatch('openrouter_setup_agents')
    assert env.service.store.entities('agents')==before
    assert not env.service.store.get_settings()['auto_delegate']
    connection, _, _, client=connected(env,enabled=False,remote_consent=False)
    with pytest.raises(ValueError,match='consent'): env.service.dispatch('openrouter_setup_agents')
    assert env.service.store.entities('agents')==before
    assert not env.service.store.get_settings()['goal_review_enabled']
    client.close()


def test_cloud_setup_is_idempotent_preserves_edits_and_review_settings(env):
    result=setup(env)
    assert set(result['created'])=={'openrouter-researcher','openrouter-assistant'}
    assert result['settings']['auto_delegate'] and result['settings']['goal_review_enabled']
    profile=env.service.store.entity('agents','openrouter-researcher')
    assert profile['provider_id']=='openrouter' and profile['model']=='openrouter/free'
    assert profile['context']==32768 and profile['role']=='researcher'
    assert not {'run_command','write_file','delegate_agent','goal_update'}&set(profile['tools'])
    edited=env.service.agent_save({**profile,'name':'My researcher','instructions':'Retain this user instruction.','enabled':False})
    env.service.store.update_settings({'goal_review_context':8192,'goal_review_max_revisions':2})
    again=env.service.dispatch('openrouter_setup_agents')
    assert not again['created'] and set(again['preserved'])==set(result['created'])
    assert env.service.store.entity('agents',profile['id'])==edited
    assert again['settings']['goal_review_context']==8192 and again['settings']['goal_review_max_revisions']==2


def test_enabled_profile_ids_provider_model_and_scope_reach_local_model(env):
    setup(env)
    run=parent(env,context=4096)
    schemas=env.service.jobs.registry.schemas(run,['tools'])
    names={schema['function']['name']:schema for schema in schemas}
    assert {'delegate_agent','agent_result'}<=names.keys()
    assert 'openrouter-researcher' in names['delegate_agent']['function']['parameters']['properties']['agent_id']['enum']
    guidance=env.service.jobs._context(run,schemas)
    content='\n'.join(message['content'] for message in guidance)
    assert 'openrouter-researcher' in content and 'openrouter/free' in content and 'web_search' in content
    assert 'without consuming the local GPU' in content and 'Do not change your own provider' in content
    assert 'synthetic_openrouter_fixture_key' not in content
    env.service.agent_save({'id':'openrouter-researcher','enabled':False})
    ids=env.service.jobs.registry.schemas(run,['tools'])[1]['function'].get('parameters',{}).get('properties',{}).get('agent_id',{}).get('enum',[])
    assert 'openrouter-researcher' not in ids
    env.service.store.update_settings({'auto_delegate':False})
    assert not env.service.delegation_profiles(run)


def test_cloud_helper_inherits_project_worktree_tool_and_permission_ceiling(env,tmp_path):
    setup(env)
    project_root=tmp_path/'project'; project_root.mkdir()
    project=env.service.store.create_project('Connected fixture',str(project_root))
    workspace_root=tmp_path/'workspace'; workspace_root.mkdir(); (workspace_root/'evidence.txt').write_text('Workspace evidence.')
    workspace=env.service.store.create_project('Selected worktree',str(workspace_root))
    run=parent(env,project_id=project['id'],workspace_project_id=workspace['id'],permission_profile='always_ask',
        permission_ceiling='always_ask',agent_tools=['read_file'])
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Read evidence.txt.',
        'cloud_scope':{'files':['evidence.txt']}})
    stored=env.service.store.run(child['id'])
    assert stored['project_id']==project['id'] and stored['workspace_project_id']==workspace['id']
    assert stored['permission_ceiling']=='always_ask' and stored['agent_tools']==['read_file']
    assert stored['readonly'] and not stored['settings']['auto_delegate']
    assert env.service.jobs.registry.execute(stored,'read_file',{'path':'evidence.txt'},threading.Event())['content']=='Workspace evidence.'
    names={schema['function']['name'] for schema in env.service.jobs.registry.schemas(stored,['tools'])}
    assert not {'delegate_agent','agent_result','write_file','run_command'}&names


def test_delegation_cannot_change_project_or_recursively_spawn(env,tmp_path):
    setup(env)
    root=tmp_path/'other'; root.mkdir(); project=env.service.store.create_project('Unrelated project',str(root))
    run=parent(env)
    with pytest.raises(ValueError,match='parent project'):
        env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'project_id':project['id'],'text':'No cross-project read.'})
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'One bounded helper.'})
    with pytest.raises(ValueError,match='recursively'):
        env.service.agent_start({'agent_id':'openrouter-assistant','parent_id':child['id'],'text':'No grandchild.'})
    assert len(env.service.store.runs())==2
    assert env.service.jobs.registry.execute(env.service.store.run(child['id']),'delegate_agent',
        {'agent_id':'openrouter-assistant','task':'No recursive spawn.'},threading.Event())['not_executed']


def test_disabled_or_unconsented_cloud_helpers_do_not_create_a_run(env):
    setup(env); run=parent(env)
    config=env.service.store.entity('providers','openrouter')
    env.service.store.save_entity('providers',{**config,'enabled':False,'remote_consent':False})
    assert not any(profile['cloud'] for profile in env.service.delegation_profiles(run))
    with pytest.raises(ValueError,match='consent'):
        env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Do not transmit.'})
    result=env.service.jobs.registry.execute(run,'delegate_agent',{'agent_id':'openrouter-researcher','task':'Do not transmit.'},threading.Event())
    assert result['not_executed'] and len(env.service.store.runs())==1


def test_delegate_invocation_is_idempotent_and_returns_direct_child(env):
    setup(env); run=parent(env)
    args={'agent_id':'openrouter-researcher','task':'Read only the assigned fixture.','wait':False}
    first=env.service.jobs.registry.execute(run,'delegate_agent',args,threading.Event(),invocation_id='fixture-delegation-once')
    second=env.service.jobs.registry.execute(run,'delegate_agent',args,threading.Event(),invocation_id='fixture-delegation-once')
    assert first['id']==second['id'] and second['reused']
    assert len(env.service.store.runs())==2 and env.service.jobs._launch.call_count==2
    assert first['run']['settings']['provider_id']=='openrouter' and first['waiting'] and not first['finished']


def test_blank_cloud_profile_model_uses_free_router_and_bad_tasks_fail_before_launch(env):
    setup(env); run=parent(env)
    env.service.agent_save({'id':'openrouter-researcher','model':''})
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Use the free default.'})
    assert child['settings']['model']=='openrouter/free'
    assert next(profile for profile in env.service.delegation_profiles(run) if profile['id']=='openrouter-researcher')['model']=='openrouter/free'
    result=env.service.jobs.registry.execute(run,'delegate_agent',
        {'agent_id':'openrouter-researcher','task':'   ','wait':False},threading.Event())
    assert result['not_executed'] and len(env.service.store.runs())==2
    assert env.service.jobs.registry.execute(run,'delegate_agent',{},threading.Event())['not_executed']


def test_agent_result_waits_journal_done_without_model_or_database_busy_polling(env,monkeypatch):
    setup(env); run=parent(env)
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Wait for fixture evidence.'})
    cancel=threading.Event()
    def finish():
        time.sleep(.3)
        env.service.store.add_message(child['chat_id'],'assistant','Fixture evidence returned.',thinking='Private synthetic reasoning.')
        env.service.store.finish_run(child['id'],{'status':'completed'},{'status':'completed'})
    thread=threading.Thread(target=finish); thread.start()
    env.service.jobs.jobs[child['id']]={'thread':thread,'cancel':threading.Event(),'pause':False}
    calls=[]; original=env.service.store.run
    def recorded(identifier):
        calls.append(identifier); return original(identifier)
    monkeypatch.setattr(env.service.store,'run',recorded)
    started=time.monotonic()
    result=env.service.agent_result(run,{'run_id':child['id'],'wait_seconds':2},cancel)
    thread.join(); env.service.jobs.jobs.pop(child['id'])
    assert .2<time.monotonic()-started<2
    assert result['finished'] and result['response']=='Fixture evidence returned.'
    assert len(calls)<=5 and 'Private synthetic reasoning.' not in json.dumps(result)
    assert not env.service.store._event_listeners


def test_agent_result_wait_is_cancellable_without_cancelling_child_twice(env):
    setup(env); run=parent(env)
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Cancellation fixture.'})
    cancel=threading.Event()
    thread=threading.Thread(target=lambda:(time.sleep(.1),cancel.set())); thread.start()
    env.service.jobs.jobs[child['id']]={'thread':thread,'cancel':threading.Event(),'pause':False}
    started=time.monotonic()
    result=env.service.agent_result(run,{'run_id':child['id'],'wait_seconds':300},cancel)
    thread.join(); env.service.jobs.jobs.pop(child['id'])
    assert time.monotonic()-started<.8 and result['cancelled_wait'] and result['waiting']
    assert not env.service.store._event_listeners


def test_agent_result_returns_user_attention_and_bounded_timeout(env):
    setup(env); run=parent(env)
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Wait for permission fixture.'})
    def attention():
        time.sleep(.1)
        env.service.store.update_run(child['id'],status='awaiting_approval')
        env.service.store.event(child['id'],'approval',id='fixture-approval')
    thread=threading.Thread(target=attention); thread.start()
    env.service.jobs.jobs[child['id']]={'thread':thread,'cancel':threading.Event(),'pause':False}
    result=env.service.agent_result(run,{'run_id':child['id'],'wait_seconds':2},threading.Event())
    thread.join()
    assert result['needs_attention'] and result['waiting'] and not result['finished']
    env.service.store.update_run(child['id'],status='running')
    started=time.monotonic()
    result=env.service.agent_result(run,{'run_id':child['id'],'wait_seconds':.1},threading.Event())
    assert .08<time.monotonic()-started<.8 and result['waiting']
    env.service.jobs.jobs.pop(child['id'])
    assert not env.service.store._event_listeners


def test_cloud_helpers_share_parent_goal_budgets(env,tmp_path):
    setup(env)
    folder=tmp_path/'goal_project'; folder.mkdir()
    project=env.service.store.create_project('Goal fixture',str(folder))
    goal=env.service.goal_create({'project_id':project['id'],'text':'Research one bounded goal.'})
    run=parent(env,project_id=project['id'],goal_id=goal['id'],goal_limits={'tokens':10})
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Bounded research.'})
    stored=env.service.store.run(child['id'])
    assert stored['goal_id']==goal['id'] and stored['settings']['goal_limits']['tokens']==10
    env.service.store.record_usage(dict(id='fixture-child-usage',run_id=child['id'],project_id=project['id'],
        provider='openrouter',model='openrouter/free',purpose='child',input_tokens=10,cached_input_tokens=0,
        output_tokens=10,estimated=False,decode_seconds=1,total_seconds=1,cancelled=False))
    assert env.service.jobs._limits(stored,time.monotonic())=='Shared tokens limit reached.'


def test_agent_result_excludes_other_chats_and_later_runs_in_child_chat(env):
    setup(env); run=parent(env)
    child=env.service.agent_start({'agent_id':'openrouter-researcher','parent_id':run['id'],'text':'Assigned child request.'})
    env.service.store.add_message(child['chat_id'],'assistant','Scoped child result.')
    env.service.store.finish_run(child['id'],{'status':'completed'},{'status':'completed'})
    env.service.jobs.start({'chat_id':child['chat_id'],'text':'A later unrelated question in the same chat.'})
    env.service.store.add_message(child['chat_id'],'assistant','A later unrelated response.')
    other=env.service.jobs.start({'text':'A private unrelated chat.'})
    env.service.store.add_message(other['chat_id'],'assistant','A private unrelated answer.')
    result=env.service.agent_result(run,{'run_id':child['id'],'wait_seconds':0},threading.Event())
    assert result['response']=='Scoped child result.' and 'unrelated' not in json.dumps(result)
    with pytest.raises(ValueError,match='not a child'):
        env.service.agent_result(run,{'run_id':other['id'],'wait_seconds':0},threading.Event())


def test_local_model_itself_spawns_cloud_helper_and_receives_evidence_in_next_round(env,monkeypatch):
    _, _, _, client=connected(env)
    env.service.dispatch('openrouter_setup_agents')
    pool_provider(env,client)
    env.service.core.responses=[{'message':{'tool_calls':[tool('delegate_agent',
        {'agent_id':'openrouter-researcher','task':'Explain the bounded synthetic fixture.','wait_seconds':5})]}},
        {'message':{'content':'Used the cloud helper evidence to finish the local task.'}}]
    requests=[]
    def remote(request):
        payload=json.loads(request.content); requests.append(payload)
        assert payload['model']=='openrouter/free'
        assert all(value==0 for value in payload['provider']['max_price'].values())
        assert 'Coordinate the synthetic fixture locally.' not in json.dumps(payload['messages'])
        assert not {'delegate_agent','agent_result','write_file','run_command'}&{tool['function']['name'] for tool in payload.get('tools',[])}
        return httpx.Response(200,stream=TimedBody([(0,packet) for packet in completion('Independent helper evidence: fixture verified.')]))
    transport(monkeypatch,remote)
    env.service.jobs._launch=MethodType(RunManager._launch,env.service.jobs)
    main=env.service.jobs.start({'text':'Coordinate the synthetic fixture locally.'})
    assert wait_finished(env.service,main)['status']=='completed'
    children=[run for run in env.service.store.runs() if run.get('parent_id')==main['id']]
    assert len(children)==1 and children[0]['status']=='completed'
    assert children[0]['source_key'].startswith('delegate:'+main['id']+':')
    assert len(requests)==1 and len(env.service.core.requests)==2
    evidence=next(message for message in env.service.core.requests[1]['messages'] if message['role']=='tool')
    assert 'Independent helper evidence: fixture verified.' in evidence['content']
    assert usage_rows(env,children[0]['id'])[0]['purpose']=='child'
    assert all(row['provider']=='ollama' for row in usage_rows(env,main['id']))
    client.close()
