"""Actual durable workers use disposable SQLite/projects and offline HTTP only."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time

import httpx
import pytest

from forge_engine_auth import EngineRunBindingFailure
from forge_service import ForgeService
from forge_store import ForgeStore, PROVIDER_BINDING_SCHEMA
from forge_updates import database_guard
from test_forge_core import Engine
from test_forge_ollama_auth import FakeVault, KEY, install_http


EVIDENCE='Synthetic completed evidence for the original engine.'


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    import forge_credentials
    import psutil
    monkeypatch.setattr(forge_credentials,'CredentialVault',FakeVault)
    monkeypatch.setattr(psutil,'cpu_count',lambda logical=True:4)
    svc=ForgeService(core=Engine(),data_dir=tmp_path/'workspace')
    svc.store.save_entity('providers',{'id':'engine','kind':'ollama','url':'https://engine.invalid/api',
        'credential_ref':'reference','enabled':True,'context_limit':32768})
    svc.store.update_settings({'model':'fixture','provider_id':'engine','context':8192,'tokens':1024,
        'thinking':False,'permission_profile':'full_access','memory_enabled':False,'memory_suggestions':False,
        'browser_tools':False,'computer_tools':False,'web':False,'guided_execution':False})
    directory=tmp_path/'project';directory.mkdir();(directory/'evidence.txt').write_text(EVIDENCE,encoding='utf-8')
    project=svc.store.create_project('Synthetic project',str(directory))
    yield svc,project
    svc.shutdown()


def terminal(svc,identifier):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        run=svc.store.run(identifier)
        if run['status'] in ('completed','paused','cancelled','failed','interrupted') and identifier not in svc.jobs.jobs:return run
        time.sleep(.01)
    pytest.fail('Offline worker did not finish')


def packet(message):
    return httpx.Response(200,content=(json.dumps({'message':message,'done':True,'done_reason':'stop',
        'prompt_eval_count':8,'eval_count':4})+'\n').encode())


def read_call():return {'tool_calls':[{'function':{'name':'read_file','arguments':{'path':'evidence.txt'}}}]}


@pytest.mark.parametrize('change',['endpoint','key','kind','endpoint_aba'])
def test_actual_two_round_worker_keeps_original_scope_and_completed_tool(fixture,monkeypatch,change):
    svc,project=fixture;entered,release=threading.Event(),threading.Event();chats=0;reads=[]
    original=svc.jobs.registry.execute
    def execute(run,name,args,cancel,**kwargs):
        if name=='read_file':reads.append(name)
        return original(run,name,args,cancel,**kwargs)
    monkeypatch.setattr(svc.jobs.registry,'execute',execute)
    def handler(request):
        nonlocal chats
        if request.url.path.endswith('/chat'):
            chats+=1
            if chats==1:entered.set();assert release.wait(3);return packet(read_call())
            return packet({'content':'Unexpected retarget'})
        return httpx.Response(200,json={'capabilities':['tools'],'model_info':{'fixture.context_length':32768}})
    requests,_=install_http(monkeypatch,handler)
    run=svc.jobs.start({'text':'Read evidence.txt and report it.','project_id':project['id']})
    try:
        assert entered.wait(3)
        if change=='endpoint':svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/api','api_key':'replacement-key'})
        elif change=='key':svc.dispatch('provider_save',{'id':'engine','api_key':'replacement-key'})
        elif change=='kind':svc.dispatch('provider_save',{'id':'engine','kind':'openai-compatible','preset_id':None})
        else:
            svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/api'})
            svc.dispatch('provider_save',{'id':'engine','url':'https://engine.invalid/api','api_key':KEY})
    finally:release.set()
    saved=terminal(svc,run['id'])
    assert saved['status']=='paused' and 'start a fresh request' in saved['recovery']
    assert chats==1 and reads==['read_file']
    assert not any(request.url.host=='replacement.invalid' for request in requests)
    with svc.store._connection() as db:
        evidence=[dict(row) for row in db.execute('SELECT * FROM invocations WHERE run_id=?',(run['id'],))]
    assert len(evidence)==1 and evidence[0]['status']=='completed' and EVIDENCE in evidence[0]['result']
    before=svc.store.get_chat(run['chat_id'])
    with pytest.raises(EngineRunBindingFailure):svc.jobs.resume(run['id'])
    assert svc.store.get_chat(run['chat_id'])==before and chats==1 and reads==['read_file']
    signature=svc.store._provider_run_binding(run['id'])['signature']
    assert signature not in json.dumps(svc.store.run(run['id']))+json.dumps(before)+json.dumps(svc.jobs.poll(run['id']))


def test_restart_same_binding_resumes_without_replaying_completed_tool(fixture,monkeypatch):
    svc,project=fixture;reads=[];chats=0
    original=svc.jobs.registry.execute
    def execute(run,name,args,cancel,**kwargs):
        result=original(run,name,args,cancel,**kwargs)
        if name=='read_file':reads.append(name);svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',execute)
    def handler(request):
        nonlocal chats
        if request.url.path.endswith('/chat'):chats+=1;return packet(read_call() if chats==1 else {'content':'Finished from saved evidence.'})
        return httpx.Response(200,json={'capabilities':['tools'],'model_info':{'fixture.context_length':32768}})
    requests,_=install_http(monkeypatch,handler)
    run=svc.jobs.start({'text':'Read evidence.txt and report it.','project_id':project['id']})
    paused=terminal(svc,run['id']);assert paused['status']=='paused' and reads==['read_file']
    signature=svc.store._provider_run_binding(run['id'])['signature'];home=svc.store.home;svc.shutdown()
    restarted=ForgeService(core=Engine(),data_dir=home)
    try:
        restarted_reads=[];restarted_execute=restarted.jobs.registry.execute
        def execute_restarted(run,name,args,cancel,**kwargs):
            if name=='read_file':restarted_reads.append(name)
            return restarted_execute(run,name,args,cancel,**kwargs)
        monkeypatch.setattr(restarted.jobs.registry,'execute',execute_restarted)
        restarted.jobs.resume(run['id']);saved=terminal(restarted,run['id'])
        assert saved['status']=='completed' and chats==2 and reads==['read_file'] and restarted_reads==[]
        assert svc.store._provider_run_binding(run['id'])['signature']==signature
        with restarted.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==1
        assert EVIDENCE in next(request.content.decode() for request in reversed(requests) if request.url.path.endswith('/chat'))
    finally:restarted.shutdown()


def queued_run(svc,project,monkeypatch):
    monkeypatch.setattr(svc.jobs,'_launch',lambda run:None)
    return svc.jobs.start({'text':'Synthetic queued request.','project_id':project['id']})


@pytest.mark.parametrize('kind',['round','completed_action','output'])
def test_legacy_saved_work_without_proof_refuses_before_resume_reconciliation(fixture,monkeypatch,kind):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    run=queued_run(svc,project,monkeypatch)
    values={'rounds':1} if kind=='round' else {'output_tokens':4} if kind=='output' else {}
    svc.store.update_run(run['id'],status='paused',**values)
    if kind=='completed_action':
        identity=run['id']+':1:0';svc.store.invocation(identity,run['id'],'read_file',{'path':'evidence.txt'})
        svc.store.invocation_state(identity,'completed',{'content':EVIDENCE})
        svc.store.update_run(run['id'],pending_calls=[read_call()['tool_calls'][0]],next_tool=0)
    before=database_guard(svc.store.db_path)
    with pytest.raises(EngineRunBindingFailure,match='Review its checkpoint and start a fresh request'):
        svc.jobs.resume(run['id'])
    assert requests==[] and database_guard(svc.store.db_path)==before
    assert svc.store._provider_run_binding(run['id']) is None


def test_no_action_legacy_run_may_bind_before_worker_admission(fixture,monkeypatch):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    launch=svc.jobs._launch;run=queued_run(svc,project,monkeypatch)
    svc.store.update_run(run['id'],status='paused');monkeypatch.setattr(svc.jobs,'_launch',launch)
    svc.jobs.resume(run['id']);assert terminal(svc,run['id'])['status']=='completed'
    assert svc.store._provider_run_binding(run['id']) is not None
    assert any(request.url.path.endswith('/chat') for request in requests)


@pytest.mark.parametrize('corruption',['schema','row'])
def test_corrupt_private_journal_leaves_workspace_open_but_blocks_affected_auth(fixture,monkeypatch,corruption):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    run=queued_run(svc,project,monkeypatch);saved=svc.store.run(run['id']);svc.jobs._bind_engine_run(saved)
    with svc.store._connection(transaction='write') as db:
        if corruption=='schema':db.execute('ALTER TABLE provider_run_bindings ADD COLUMN unknown_format TEXT')
        else:db.execute('PRAGMA ignore_check_constraints=ON');db.execute('UPDATE provider_run_bindings SET format_version=2')
    reopened=ForgeStore(svc.store.home)
    assert reopened.get_settings()['model']=='fixture'
    with pytest.raises(EngineRunBindingFailure):svc.jobs._bind_engine_run(saved)
    assert requests==[]
    with reopened._connection() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0] in ('ok','CHECK constraint failed in provider_run_bindings')


@pytest.mark.parametrize('corruption',['extra_column','missing_column','ambiguous_readable'])
def test_unknown_private_schema_refuses_bound_compatible_but_keeps_default_engine_usable(fixture,monkeypatch,corruption):
    svc,project=fixture
    svc.dispatch('provider_save',{'id':'compatible','kind':'openai-compatible','url':'https://compatible.invalid/v1','name':'Synthetic compatible','enabled':True})
    corrupt_journal(svc.store,corruption)
    def handler(request):
        return httpx.Response(200,content=('data: '+json.dumps({'choices':[{'delta':{'content':'Synthetic compatible result'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode())
    requests,_=install_http(monkeypatch,handler)
    with pytest.raises(EngineRunBindingFailure):
        svc.jobs.start({'text':'Synthetic request.','project_id':project['id'],'provider_id':'compatible'})
    assert requests==[]
    run=svc.jobs.start({'text':'Synthetic default request.','project_id':project['id'],'provider_id':'ollama'})
    assert terminal(svc,run['id'])['status']=='completed' and requests==[]


def corrupt_journal(store,corruption):
    with store._connection(transaction='write') as db:
        if corruption=='extra_column':db.execute('ALTER TABLE provider_run_bindings ADD COLUMN unknown_format TEXT')
        else:
            db.execute('ALTER TABLE provider_run_bindings RENAME COLUMN run_id TO old_run_id')
            if corruption=='ambiguous_readable':db.execute('ALTER TABLE provider_run_bindings ADD COLUMN run_id TEXT')


@pytest.mark.parametrize('corruption',['missing_column','ambiguous_readable'])
def test_unknown_saved_origin_refuses_before_missing_profile_lookup(fixture,monkeypatch,corruption):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    run=queued_run(svc,project,monkeypatch)
    svc.store.update_run(run['id'],status='paused',rounds=1)
    svc.store.delete_entity('providers','engine');svc.providers.invalidate('engine')
    corrupt_journal(svc.store,corruption);before=database_guard(svc.store.db_path)
    monkeypatch.setattr(svc.providers,'provider',lambda identifier:pytest.fail('Unknown saved origin looked up a replacement profile'))
    with pytest.raises(EngineRunBindingFailure,match='Review its checkpoint and start a fresh request'):
        svc.jobs.resume(run['id'])
    assert requests==[] and database_guard(svc.store.db_path)==before


@pytest.mark.parametrize('corruption',['missing_column','ambiguous_readable'])
def test_unknown_original_scope_cannot_retarget_saved_worker_after_kind_switch(fixture,monkeypatch,corruption):
    svc,project=fixture;original=svc.jobs.registry.execute
    def execute(run,name,args,cancel,**kwargs):
        result=original(run,name,args,cancel,**kwargs)
        if name=='read_file':svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',execute)
    def handler(request):
        if request.url.path.endswith('/chat'):return packet(read_call())
        return httpx.Response(200,json={'capabilities':['tools'],'model_info':{'fixture.context_length':32768}})
    requests,_=install_http(monkeypatch,handler)
    run=svc.jobs.start({'text':'Read evidence.txt','project_id':project['id']})
    assert terminal(svc,run['id'])['status']=='paused'
    svc.dispatch('provider_save',{'id':'engine','kind':'openai-compatible','url':'https://replacement.invalid/v1','api_key':'replacement-key'})
    corrupt_journal(svc.store,corruption);home=svc.store.home;svc.shutdown()
    restarted=ForgeService(core=Engine(),data_dir=home)
    try:
        requests.clear();before=database_guard(restarted.store.db_path)
        monkeypatch.setattr(restarted.jobs.registry,'execute',lambda *args,**kwargs:pytest.fail('Completed tool replayed'))
        with pytest.raises(EngineRunBindingFailure,match='start a fresh request'):restarted.jobs.resume(run['id'])
        assert requests==[] and database_guard(restarted.store.db_path)==before
        assert restarted.store._has_provider_run_binding(run['id']) is None
    finally:restarted.shutdown()


def test_supplemental_creation_is_backed_up_idempotent_and_keeps_schema7(tmp_path):
    store=ForgeStore(tmp_path/'workspace');store.update_settings({'model':'fixture'})
    with store._connection(transaction='write') as db:db.execute('DROP TABLE provider_run_bindings')
    before=database_guard(store.db_path)
    reopened=ForgeStore(store.home)
    backups=list((store.home/'backups').glob('pre-provider-bindings-1-*/forge.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_run_bindings'").fetchone() is None
    db.close()
    # Store reopening deliberately closes the derived queue until real recovery.
    from forge_channel_queue import InboundQueue, PRIVATE_TABLES
    closed=database_guard(reopened.db_path,before)
    assert closed!=before and 'channel_queue_protection' in closed
    assert PRIVATE_TABLES<=closed['tables'].keys()
    assert InboundQueue(reopened).maintenance()
    assert database_guard(reopened.db_path,before)==before
    ForgeStore(store.home)
    assert len(list((store.home/'backups').glob('pre-provider-bindings-1-*/forge.sqlite3')))==1
    with reopened._connection() as db:
        assert db.execute('SELECT MAX(version) FROM forge_migrations').fetchone()[0]==7
        assert db.execute('PRAGMA user_version').fetchone()[0]==1


def test_backup_failure_prevents_private_table_creation_and_preserves_work(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'workspace');store.update_settings({'model':'fixture'})
    with store._connection(transaction='write') as db:db.execute('DROP TABLE provider_run_bindings')
    before=database_guard(store.db_path);connect=sqlite3.connect
    def fail(path,*args,**kwargs):
        if 'pre-provider-bindings-1-' in str(path):raise OSError('synthetic-private-path')
        return connect(path,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',fail)
    with pytest.raises(ValueError,match='backup could not be verified') as failure:ForgeStore(store.home)
    assert 'synthetic-private-path' not in str(failure.value)
    assert database_guard(store.db_path)==before


def test_private_creation_transaction_failure_preserves_schema7_and_prior_work(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'workspace');store.update_settings({'model':'fixture'})
    with store._connection(transaction='write') as db:db.execute('DROP TABLE provider_run_bindings')
    before=database_guard(store.db_path);connection=ForgeStore._connection
    @contextmanager
    def fail(self,**kwargs):
        with connection(self,**kwargs) as db:
            yield db
            if kwargs.get('transaction')=='write' and db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_run_bindings'").fetchone():
                raise sqlite3.OperationalError('synthetic-transaction-failure')
    monkeypatch.setattr(ForgeStore,'_connection',fail)
    with pytest.raises(ValueError,match='synthetic-transaction-failure'):ForgeStore(store.home)
    assert database_guard(store.db_path)==before


def test_resume_cannot_change_bound_provider_before_reconciling_saved_actions(fixture,monkeypatch):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    run=queued_run(svc,project,monkeypatch);svc.jobs._bind_engine_run(svc.store.run(run['id']))
    svc.store.update_run(run['id'],status='paused')
    svc.store.update_settings({'provider_id':'ollama'})
    before=database_guard(svc.store.db_path)
    with pytest.raises(EngineRunBindingFailure):svc.jobs.resume(run['id'])
    assert requests==[] and database_guard(svc.store.db_path)==before


def test_binding_write_held_across_profile_aba_never_launches_a_worker(fixture,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    svc,project=fixture;requests,_=install_http(monkeypatch)
    entered,release=threading.Event(),threading.Event();bind=svc.store._bind_provider_run
    def held(*args):entered.set();assert release.wait(3);return bind(*args)
    monkeypatch.setattr(svc.store,'_bind_provider_run',held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(svc.jobs.start,{'text':'Synthetic request','project_id':project['id']})
        try:
            assert entered.wait(3)
            svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/api'})
            svc.dispatch('provider_save',{'id':'engine','url':'https://engine.invalid/api','api_key':KEY})
        finally:release.set()
        with pytest.raises(EngineRunBindingFailure):future.result(timeout=3)
    run=svc.store.runs()[0]
    assert run['status']=='paused' and svc.jobs.jobs=={} and requests==[]
    assert svc.store._provider_run_binding(run['id']) is not None


def test_restart_changed_binding_refuses_before_any_http_or_saved_tool_replay(fixture,monkeypatch):
    svc,project=fixture;original=svc.jobs.registry.execute
    def execute(run,name,args,cancel,**kwargs):
        result=original(run,name,args,cancel,**kwargs)
        if name=='read_file':svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',execute)
    def handler(request):
        if request.url.path.endswith('/chat'):return packet(read_call())
        return httpx.Response(200,json={'capabilities':['tools'],'model_info':{'fixture.context_length':32768}})
    requests,_=install_http(monkeypatch,handler)
    run=svc.jobs.start({'text':'Read evidence.txt','project_id':project['id']})
    assert terminal(svc,run['id'])['status']=='paused'
    svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/api','api_key':'replacement-key'})
    home=svc.store.home;svc.shutdown();restarted=ForgeService(core=Engine(),data_dir=home)
    try:
        requests.clear();before=database_guard(restarted.store.db_path)
        monkeypatch.setattr(restarted.jobs.registry,'execute',lambda *args,**kwargs:pytest.fail('Completed tool replayed'))
        with pytest.raises(EngineRunBindingFailure):restarted.jobs.resume(run['id'])
        assert requests==[] and database_guard(restarted.store.db_path)==before
    finally:restarted.shutdown()


def test_delayed_worker_entry_cannot_adopt_changed_profile(fixture,monkeypatch):
    svc,project=fixture;requests,_=install_http(monkeypatch);workers=[];create=threading.Thread
    class Deferred:
        def __init__(self,**kwargs):self.target=kwargs['target'];self.args=kwargs['args'];workers.append(self)
        def start(self):pass
    def thread(*args,**kwargs):
        return Deferred(**kwargs) if kwargs.get('name','').startswith('Forge-run-') else create(*args,**kwargs)
    monkeypatch.setattr(threading,'Thread',thread)
    run=svc.jobs.start({'text':'Synthetic original request','project_id':project['id']})
    assert svc.store._provider_run_binding(run['id']) is not None and len(workers)==1
    svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/api','api_key':'replacement-key'})
    workers[0].target(*workers[0].args)
    assert svc.store.run(run['id'])['status']=='paused' and requests==[]
