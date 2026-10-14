"""Accepted source identity owns preparation; fixtures never execute Git or models."""
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

import forge_channel_queue as queue
import forge_service
from test_forge_channels import env,configured,update,inbound


class SyntheticCrash(BaseException):
    pass


def forbidden(*args,**kwargs):
    raise AssertionError('Actual home, Git, native and network access is forbidden.')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(Path,'home',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(forge_service.ForgeService,'_git',forbidden)


def project(env,name='Synthetic project'):
    folder=env.service.store.home/name
    folder.mkdir()
    return env.service.store.create_project(name,str(folder))


def test_public_request_fields_cannot_construct_internal_preparation_or_callbacks(env,monkeypatch):
    monkeypatch.setattr(env.service,'_complete_channel_preparation',forbidden)
    result=env.service.jobs.start({'text':'Synthetic normal request',
        'channel_preparation':{'state':'ready','goal_id':'0'*32,'workspace':{'project_id':'untrusted'}},
        'channel':{'prepare':'untrusted','complete':'untrusted'},'prepare':'untrusted','complete':'untrusted'})
    run=env.service.store.run(result['id'])
    assert 'channel_preparation' not in run and not run.get('goal_id') and not run.get('workspace_project_id')
    assert env.service.store.entities('goals')==[] and env.service.store.entities('channel_workspaces')==[]
    assert env.service.jobs._launch.call_count==1


def test_goal_budget_boundary_creates_nothing_until_final_admission_and_exactly_one_goal_later(env):
    recipients=list(range(42,55))
    channel=configured(env,allow_outbound=False,
        allowlist=[{'sender_id':str(value),'chat_id':str(value)} for value in recipients])
    env.manager.recovered=True
    with env.service.store._connection(transaction='write') as db:
        for number in range(149):
            recipient=recipients[number] if number<len(recipients) else 42
            text='/goal Synthetic tracked assignment' if number==12 else 'Synthetic request '+str(number)
            payload=json.dumps(update(number,sender=recipient,chat=recipient,text=text))
            db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)',
                (channel['id'],str(number),'pending',payload,f'{number:04}','synthetic'))
    env.manager.tick(poll=False)
    assert env.manager.inbound_queue.last_work['payload_work']==200
    assert env.manager.inbound_queue.last_work['payload_classifications']==149
    assert len(env.service.store.runs())==12 and env.service.store.entities('goals')==[]
    assert list((env.service.store.home/'state/goals').glob('*/TODO.md'))==[]
    assert inbound(env,channel['id'],12)['status']=='pending'
    env.manager.tick(poll=False)
    goals=env.service.store.entities('goals')
    goal_runs=[run for run in env.service.store.runs() if run.get('source_key')=='channel:'+channel['id']+':12']
    assert len(goals)==len(goal_runs)==1 and goal_runs[0]['goal_id']==goals[0]['id']
    assert goal_runs[0]['execution_mode']=='guided' and goal_runs[0]['channel_preparation']['state']=='ready'
    assert Path(goals[0]['path']).is_file()


@pytest.mark.parametrize('command',['goal','build'])
@pytest.mark.parametrize('change',['channel','permissions','profile','stop'])
def test_late_admission_fence_prevents_goal_documents_and_workspace_preparation(env,monkeypatch,command,change):
    root=project(env)
    channel=configured(env,project_id=root['id'],agent_profile_id='coder',allow_outbound=False)
    monkeypatch.setattr('forge_builder_guide.accepted_brief',lambda *args:'Synthetic accepted brief')
    accept=env.service.store.accept_request
    changed=[]
    def fenced(*args,**kwargs):
        if isinstance(kwargs.get('channel'),queue.ChannelAdmission) and not changed:
            changed.append(True)
            if change=='channel':env.service.store.save_entity('channels',{**env.service.store.entity('channels',channel['id']),'enabled':False})
            elif change=='permissions':env.service.store.update_settings({'permission_profile':'deny_access'})
            elif change=='profile':env.service.store.save_entity('agents',{**env.service.store.entity('agents','coder'),'enabled':False})
            else:env.manager.stop_event.set()
        return accept(*args,**kwargs)
    monkeypatch.setattr(env.service.store,'accept_request',fenced)
    monkeypatch.setattr(env.service,'worktree_create',forbidden)
    env.manager.receive_telegram(channel['id'],update(text='/'+command+' Synthetic assignment'))
    env.manager.tick(poll=False)
    assert changed and env.service.store.runs()==[] and env.service.store.entities('goals')==[]
    assert env.service.store.entities('channel_workspaces')==[]
    assert list((env.service.store.home/'state/goals').glob('*/TODO.md'))==[]
    assert inbound(env,channel['id'],1)['status']=='pending'


def test_goal_and_run_insert_share_writer_and_crash_before_commit_leaves_neither(env,monkeypatch):
    channel=configured(env,allow_outbound=False)
    save=env.service.store.save_entity
    observed=[]
    def interrupted(kind,data,*,connection=None):
        result=save(kind,data,connection=connection)
        if kind=='goals':
            observed.append(connection.in_transaction)
            assert not Path(result['path']).exists()
            raise SyntheticCrash()
        return result
    monkeypatch.setattr(env.service.store,'save_entity',interrupted)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    with pytest.raises(SyntheticCrash):env.manager.tick(poll=False)
    assert observed==[True] and env.service.store.runs()==[] and env.service.store.entities('goals')==[]
    assert inbound(env,channel['id'],1)['status']=='pending'
    monkeypatch.setattr(env.service.store,'save_entity',save)
    env.manager.tick(poll=False)
    assert len(env.service.store.runs())==len(env.service.store.entities('goals'))==1


@pytest.mark.parametrize('stage',['after_accept','after_todo'])
def test_crash_after_admission_never_automatically_prepares_or_launches_on_restart(env,monkeypatch,stage):
    channel=configured(env,allow_outbound=False)
    complete=env.service._complete_channel_preparation
    write=forge_service._publish_channel_goal_document
    calls=[]
    if stage=='after_accept':
        def crash(*args):
            calls.append(True)
            raise SyntheticCrash()
        monkeypatch.setattr(env.service,'_complete_channel_preparation',crash)
    else:
        def crash(*args):
            write(*args)
            calls.append(True)
            raise SyntheticCrash()
        monkeypatch.setattr(forge_service,'_publish_channel_goal_document',crash)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    with pytest.raises(SyntheticCrash):env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert len(env.service.store.entities('goals'))==1 and run['channel_preparation']['state'] in ('pending','preparing')
    assert not env.service.jobs._launch.called and calls==[True]
    env.service.store.recover()
    monkeypatch.setattr(env.service,'_complete_channel_preparation',complete)
    same=env.service.channel_start({'channel_command':'goal'},channel['id'],'1')
    assert same['id']==run['id'] and calls==[True]
    with pytest.raises(ValueError,match='Channel setup did not finish'):env.service.jobs.resume(run['id'])
    assert not env.service.jobs._launch.called and len(env.service.store.entities('goals'))==1


def test_todo_failure_pauses_one_accepted_identity_and_reports_no_raw_error(env,monkeypatch):
    channel=configured(env,allow_outbound=False)
    writes=[]
    def fail(*args):
        writes.append(True)
        raise OSError('synthetic-private-detail')
    monkeypatch.setattr(forge_service,'_publish_channel_goal_document',fail)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert run['status']=='paused' and run['channel_preparation']['state']=='unknown'
    assert 'synthetic-private-detail' not in json.dumps(run)
    monkeypatch.setattr(env.service.store,'_goal_record',forbidden)
    assert env.service.channel_start({'channel_command':'goal'},channel['id'],'1')['id']==run['id']
    assert writes==[True] and not env.service.jobs._launch.called


@pytest.mark.parametrize('change',['cancelled','goal_edited','document_edited'])
def test_preparation_preserves_post_acceptance_cancellation_and_user_goal_edits(env,monkeypatch,change):
    channel=configured(env,allow_outbound=False)
    accept=env.service.store.accept_request
    def changed(*args,**kwargs):
        result=accept(*args,**kwargs)
        run=result[0]
        goal=env.service.store.entity('goals',run['goal_id'])
        if change=='cancelled':env.service.store.update_run(run['id'],status='cancelled')
        elif change=='goal_edited':env.service.store.save_goal({**goal,'title':'Synthetic user edit'},reconcile=True)
        else:
            path=Path(goal['path'])
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('Synthetic external edit',encoding='utf-8')
        return result
    monkeypatch.setattr(env.service.store,'accept_request',changed)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    goal=env.service.store.entity('goals',run['goal_id'])
    assert not env.service.jobs._launch.called
    if change=='cancelled':
        assert run['status']=='cancelled' and not Path(goal['path']).exists()
    else:
        assert run['status']=='paused' and run['channel_preparation']['state']=='unknown'
        assert ('Synthetic user edit' if change=='goal_edited' else 'Synthetic external edit') in Path(goal['path']).read_text(encoding='utf-8')
    assert len(env.service.store.entities('goals'))==len(env.service.store.runs())==1


@pytest.mark.parametrize('existing',['different','identical'])
def test_fresh_todo_publication_never_replaces_a_file_created_at_the_publication_boundary(env,monkeypatch,existing):
    channel=configured(env,allow_outbound=False)
    publish=forge_service.os.link
    observed=[]
    def raced(source,destination):
        expected=source.read_bytes()
        content=b'Synthetic editor content' if existing=='different' else expected
        destination.write_bytes(content)
        observed.append(content)
        return publish(source,destination)
    monkeypatch.setattr(forge_service.os,'link',raced)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    goal=env.service.store.entities('goals')[0]
    assert Path(goal['path']).read_bytes()==observed[0]
    assert list(Path(goal['path']).parent.glob('.*.tmp'))==[]
    assert len(env.service.store.runs())==len(env.service.store.entities('goals'))==1
    if existing=='different':
        assert run['status']=='paused' and run['channel_preparation']['state']=='unknown'
        assert not env.service.jobs._launch.called
        env.service.channel_start({},channel['id'],'1')
        assert len(observed)==1
    else:
        assert run['channel_preparation']['state']=='ready' and env.service.jobs._launch.call_count==1


def test_unsupported_exclusive_publication_pauses_without_fallback_or_document_replacement(env,monkeypatch):
    channel=configured(env,allow_outbound=False)
    def unsupported(*args):raise OSError('Synthetic unsupported filesystem')
    monkeypatch.setattr(forge_service.os,'link',unsupported)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    goal=env.service.store.entities('goals')[0]
    assert run['status']=='paused' and run['channel_preparation']['state']=='unknown'
    assert not Path(goal['path']).exists() and not env.service.jobs._launch.called
    assert list(Path(goal['path']).parent.glob('.*.tmp'))==[]


@pytest.mark.parametrize('change',['channel','permissions','profile'])
def test_revocation_after_acceptance_blocks_physical_preparation_without_a_second_assignment(env,monkeypatch,change):
    root=project(env)
    channel=configured(env,project_id=root['id'],agent_profile_id='coder',allow_outbound=False)
    accept=env.service.store.accept_request
    def revoked(*args,**kwargs):
        result=accept(*args,**kwargs)
        if change=='channel':env.service.store.save_entity('channels',{**env.service.store.entity('channels',channel['id']),'enabled':False})
        elif change=='permissions':env.service.store.update_settings({'permission_profile':'deny_access'})
        else:env.service.store.save_entity('agents',{**env.service.store.entity('agents','coder'),'enabled':False})
        return result
    monkeypatch.setattr(env.service.store,'accept_request',revoked)
    monkeypatch.setattr(env.service,'worktree_create',forbidden)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert run['status']=='paused' and run['channel_preparation']['state']=='unknown'
    assert len(env.service.store.entities('goals'))==1 and not Path(env.service.store.entities('goals')[0]['path']).exists()
    assert not env.service.jobs._launch.called and env.service.store.entities('channel_workspaces')==[]


def test_successful_workspace_preparation_closes_writer_before_git_and_commits_scope_before_launch(env,monkeypatch):
    root=project(env)
    child=project(env,'Synthetic child')
    channel=configured(env,project_id=root['id'],agent_profile_id='coder',allow_outbound=False)
    calls=[]
    def no_writer():
        with sqlite3.connect(env.service.store.db_path,timeout=0) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
    def git(*args,**kwargs):
        no_writer()
        calls.append('git_check')
        assert env.service.store.runs()[0]['channel_preparation']['state']=='preparing'
        return SimpleNamespace(returncode=0)
    def create(*args):
        no_writer()
        calls.append('worktree_create')
        return {'project':child,'worktree':{'id':'synthetic-worktree'}}
    monkeypatch.setattr(env.service,'_git',git)
    monkeypatch.setattr(env.service,'worktree_create',create)
    env.manager.receive_telegram(channel['id'],update(text='Synthetic implementation'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert run['channel_preparation']['state']=='ready' and run['workspace_project_id']==child['id']
    assert run['worktree_id']=='synthetic-worktree' and calls==['git_check','worktree_create']
    assert run['channel_preparation']['model']==run['settings']['model']
    assert env.service.jobs._launch.call_count==1
    assert env.service.jobs._launch.call_args[0][0]['workspace_project_id']==child['id']
    assert json.loads(json.dumps(run))==run
    env.service.channel_start({},channel['id'],'1')
    assert calls==['git_check','worktree_create'] and env.service.jobs._launch.call_count==1


@pytest.mark.parametrize('goal',[False,True])
def test_cloud_scope_binds_deferred_goal_without_creating_a_readonly_workspace(env,monkeypatch,goal):
    from forge_cloud_policy import cloud_policy
    root=project(env)
    env.service.store.save_entity('providers',dict(id='openrouter',kind='openrouter',enabled=True,
        remote_consent=True,credential_ref='synthetic-reference'))
    channel=configured(env,project_id=root['id'],agent_profile_id='coder',provider_id='openrouter',allow_outbound=False)
    monkeypatch.setattr(env.service,'resolve_run_settings',lambda values,preferences:{**preferences,**values})
    monkeypatch.setattr(env.service,'worktree_create',forbidden)
    env.manager.receive_telegram(channel['id'],update(text='/goal Synthetic assignment' if goal else 'Synthetic assignment'))
    env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert run['readonly'] is True and not run.get('workspace_project_id')
    assert run['settings']['provider_id']=='openrouter' and env.service.jobs._launch.call_count==1
    assert env.service.store.entities('channel_workspaces')==[]
    if goal:
        assert run['cloud_scope']['goal_id']==run['goal_id']
        assert run['channel_preparation']['workspace'] is None and run['channel_preparation']['state']=='ready'
        cloud_policy(env.service).guard(run,'goal_read',{})
    else:assert 'channel_preparation' not in run


@pytest.mark.parametrize('stage',['git_check','worktree_create','workspace_record'])
@pytest.mark.parametrize('outcome',['error','crash'])
def test_workspace_preparation_claim_is_durable_and_uncertain_attempts_never_replay(env,monkeypatch,stage,outcome):
    root=project(env)
    child=project(env,'Synthetic child')
    channel=configured(env,project_id=root['id'],agent_profile_id='coder',allow_outbound=False)
    calls=[]
    def fail():
        if outcome=='crash':raise SyntheticCrash()
        raise OSError('synthetic-private-detail')
    def git(*args,**kwargs):
        run=env.service.store.runs()[0]
        assert run['channel_preparation']['state']=='preparing'
        calls.append('git_check')
        if stage=='git_check':fail()
        return SimpleNamespace(returncode=0)
    def create(*args):
        calls.append('worktree_create')
        if stage=='worktree_create':fail()
        return {'project':child,'worktree':{'id':'synthetic-worktree'}}
    save=env.service.store.save_entity
    def saved(kind,data,**kwargs):
        result=save(kind,data,**kwargs)
        if kind=='channel_workspaces' and stage=='workspace_record':fail()
        return result
    monkeypatch.setattr(env.service,'_git',git)
    monkeypatch.setattr(env.service,'worktree_create',create)
    monkeypatch.setattr(env.service.store,'save_entity',saved)
    env.manager.receive_telegram(channel['id'],update(text='Synthetic implementation'))
    if outcome=='crash':
        with pytest.raises(SyntheticCrash):env.manager.tick(poll=False)
    else:env.manager.tick(poll=False)
    run=env.service.store.runs()[0]
    assert run['channel_preparation']['state'] in ('preparing','unknown')
    assert not env.service.jobs._launch.called
    previous=list(calls)
    env.service.store.recover()
    assert env.service.channel_start({},channel['id'],'1')['id']==run['id']
    with pytest.raises(ValueError,match='Channel setup did not finish'):env.service.jobs.resume(run['id'])
    assert calls==previous and len(env.service.store.runs())==1 and not env.service.jobs._launch.called
