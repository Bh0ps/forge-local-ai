"""Saved provider authority uses disposable workers and strictly offline HTTP."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import json
import sqlite3
import threading
import time

import httpx
import pytest

from forge_cloud_policy import CloudPolicyRejected
from forge_engine_auth import EngineAccessFailure,EngineRunBindingFailure
from forge_service import ForgeService
from forge_store import ForgeStore
from forge_updates import database_guard
from test_forge_engine_run_binding import fixture,terminal,EVIDENCE
from test_forge_ollama_auth import KEY,PRIVATE,install_http,wait_for
from test_forge_ollama_auth import deferred_workers
from forge_performance import PerformanceManager
from test_forge_openrouter import model,FREE_ROUTER
from test_forge_openrouter_continuation import DETAILS


@pytest.fixture(params=['openai-compatible','openrouter'])
def saved_provider(fixture,request):
    svc,project=fixture;kind=request.param
    if kind=='openrouter':
        svc.dispatch('openrouter_save',{'api_key':KEY,'enabled':True,'remote_consent':True})
        identifier,selected='openrouter',FREE_ROUTER
    else:
        svc.dispatch('provider_save',{'id':'engine','kind':kind,'url':'https://engine.invalid/v1',
            'preset_id':None,'api_key':KEY,'enabled':True,'capabilities':['tools'],'context_limit':32768})
        identifier,selected='engine','fixture'
    svc.store.update_settings({'provider_id':identifier,'model':selected})
    return SimpleNamespace(service=svc,project=project,kind=kind,id=identifier,model=selected)


def data(provider,**extra):
    return {'provider_id':provider.id,'model':provider.model,'context':8192,'tokens':1024,'thinking':False,
        'messages':[{'role':'user','content':'Synthetic task evidence.'}],**extra}


def completion(content='Synthetic response.',calls=None,details=None):
    delta={'content':content}
    if calls:delta['tool_calls']=[{'index':0,'id':'fixture_call','type':'function',
        'function':{'name':'read_file','arguments':json.dumps({'path':'evidence.txt'})}}]
    if details:delta['reasoning_details']=details
    return httpx.Response(200,content=('data: '+json.dumps({'model':'fixture/coder:free','choices':[{'delta':delta}]})
        +'\n\ndata: '+json.dumps({'choices':[{'delta':{},'finish_reason':'tool_calls' if calls else 'stop'}],
        'usage':{'prompt_tokens':8,'completion_tokens':4}})+'\n\ndata: [DONE]\n\n').encode())


def metadata(request):
    if request.url.path.endswith('/models'):
        return httpx.Response(200,json={'data':[model(),model('fixture/coder:free'),{'id':'fixture'}]})
    return httpx.Response(200,json={'data':{'is_free_tier':True}})


def save(provider,**changes):
    if provider.kind=='openrouter':return provider.service.dispatch('openrouter_save',changes)
    return provider.service.dispatch('provider_save',{'id':provider.id,**changes})


def start(provider):
    return provider.service.jobs.start({'text':'Read evidence.txt and report it.','project_id':provider.project['id'],
        'provider_id':provider.id,'model':provider.model,
        **({'cloud_scope':{'files':['evidence.txt']}} if provider.kind=='openrouter' else {})})


def edit(provider,change):
    if change=='key':save(provider,api_key='replacement-synthetic-key')
    elif change=='disabled':save(provider,enabled=False)
    elif change=='deleted':provider.service.store.delete_entity('providers',provider.id)
    elif change=='policy':save(provider,data_collection='allow')
    elif change=='consent':save(provider,enabled=False,remote_consent=False)
    elif change=='endpoint':save(provider,url='https://replacement.invalid/v1')
    elif change=='stop':provider.service.stop_event.set()
    elif change=='permission':provider.service.store.update_settings({'permission_profile':'deny_access'})


@pytest.mark.parametrize('boundary',['unknown_schema','transaction','committed','readback_unknown','readback_malformed',
    'referenced_elsewhere','not_owned','cleanup_failure'])
def test_failed_save_cleans_only_conclusively_uncommitted_new_key(saved_provider,monkeypatch,boundary):
    provider=saved_provider;svc=provider.service
    requests,_=install_http(monkeypatch,lambda request:pytest.fail('Saving credentials must not use HTTP'))
    previous=svc.store.entity('providers',provider.id);values=dict(svc.vault.values)
    prior_guard=database_guard(svc.store.db_path);minted=[];deleted=[]
    original_put=svc.vault.put;original_save=svc.store.save_entity;original_entities=svc.store.entities
    def put(value):
        reference=previous['credential_ref'] if boundary=='not_owned' else original_put(value)
        minted.append(reference);return reference
    def delete(reference):
        deleted.append(reference)
        if boundary=='cleanup_failure':raise RuntimeError(PRIVATE)
        svc.vault.values.pop(reference,None)
    monkeypatch.setattr(svc.vault,'put',put)
    monkeypatch.setattr(svc.vault,'delete',delete,raising=False)
    if boundary=='unknown_schema':
        with svc.store._connection(transaction='write') as db:
            db.execute('ALTER TABLE provider_authority_revisions ADD COLUMN unknown_format TEXT')
        prior_guard=database_guard(svc.store.db_path)
    elif boundary=='transaction':
        monkeypatch.setattr(svc.store,'_advance_provider_authority',lambda *args:(_ for _ in ()).throw(sqlite3.OperationalError(PRIVATE)))
    else:
        def failed(kind,profile,**kwargs):
            if boundary=='committed':original_save(kind,profile,**kwargs)
            elif boundary=='referenced_elsewhere':
                original_save('providers',{'id':'another-owner','kind':'ollama','url':'https://another.invalid/api',
                    'credential_ref':minted[-1]})
            elif boundary=='readback_unknown':
                monkeypatch.setattr(svc.store,'entities',lambda kind:(_ for _ in ()).throw(RuntimeError(PRIVATE)))
            elif boundary=='readback_malformed':monkeypatch.setattr(svc.store,'entities',lambda kind:[PRIVATE])
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(svc.store,'save_entity',failed)
    with pytest.raises(ValueError) as failure:save(provider,api_key='replacement-synthetic-key')
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value) and 'replacement-synthetic-key' not in str(failure.value)
    assert requests==[] and svc.vault.values[previous['credential_ref']]==values[previous['credential_ref']]
    if boundary=='unknown_schema':assert minted==[] and deleted==[]
    elif boundary in ('transaction','cleanup_failure'):
        assert deleted==minted and len(minted)==1
        assert (minted[0] in svc.vault.values)==(boundary=='cleanup_failure')
    else:
        assert len(minted)==1 and deleted==[] and minted[0] in svc.vault.values
    if boundary not in ('committed','referenced_elsewhere'):
        assert svc.store.entity('providers',provider.id)==previous and database_guard(svc.store.db_path)==prior_guard
    else:assert any(profile.get('credential_ref')==minted[0] for profile in original_entities('providers'))


def test_successful_save_retains_fresh_owned_key_and_existing_key(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;previous=svc.store.entity('providers',provider.id);deleted=[]
    monkeypatch.setattr(svc.vault,'delete',deleted.append,raising=False)
    requests,_=install_http(monkeypatch,lambda request:pytest.fail('Saving credentials must not use HTTP'))
    save(provider,api_key='replacement-synthetic-key')
    current=svc.store.entity('providers',provider.id)
    assert current['credential_ref']!=previous['credential_ref']
    assert svc.vault.values[current['credential_ref']]=='replacement-synthetic-key'
    assert svc.vault.values[previous['credential_ref']]==KEY and deleted==[] and requests==[]


def test_key_write_failure_is_fixed_and_does_not_touch_existing_profile(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;previous=svc.store.entity('providers',provider.id)
    values=dict(svc.vault.values);deleted=[]
    monkeypatch.setattr(svc.vault,'put',lambda key:(_ for _ in ()).throw(RuntimeError(PRIVATE)))
    monkeypatch.setattr(svc.vault,'delete',deleted.append,raising=False)
    with pytest.raises(ValueError) as failure:save(provider,api_key='replacement-synthetic-key')
    assert PRIVATE not in str(failure.value) and 'replacement-synthetic-key' not in str(failure.value)
    assert svc.store.entity('providers',provider.id)==previous and svc.vault.values==values and deleted==[]


def test_invalid_openrouter_model_is_rejected_before_key_write(fixture,monkeypatch):
    svc,_=fixture;calls=[];monkeypatch.setattr(svc.vault,'put',calls.append)
    with pytest.raises(ValueError):svc.dispatch('openrouter_save',{'api_key':'replacement-synthetic-key','model':'unsupported-model'})
    assert calls==[]


@pytest.mark.parametrize('change',['key','disabled','deleted'])
def test_actual_queue_keeps_original_authority_and_refuses_before_http(saved_provider,monkeypatch,change):
    provider=saved_provider;svc=provider.service
    requests,_=install_http(monkeypatch,lambda request:completion() if request.method=='POST' else metadata(request))
    lane=svc.providers.remote_queue if provider.kind=='openrouter' else svc.providers.queue
    with ThreadPoolExecutor(max_workers=1) as executor:
        with lane.lease(threading.Event()):
            future=executor.submit(lambda:list(svc.providers.generate(data(provider),threading.Event(),{})))
            wait_for(lambda:bool(lane.pending));edit(provider,change)
        with pytest.raises(EngineAccessFailure) as failure:future.result(timeout=3)
    assert failure.value.not_executed and requests==[]
    assert KEY not in str(failure.value) and PRIVATE not in str(failure.value)


@pytest.mark.parametrize('boundary',['vault','client'])
@pytest.mark.parametrize('change',['key','disabled','stop'])
def test_actual_post_rechecks_after_delayed_admission(saved_provider,monkeypatch,boundary,change):
    provider=saved_provider;svc=provider.service;entered,release=threading.Event(),threading.Event()
    adapter=svc.providers.provider(provider.id)
    if provider.kind=='openrouter':
        adapter.catalog=[{'name':FREE_ROUTER,'capabilities':['tools'],'context_length':131072,'reasoning':{}}]
        adapter.catalog_at=time.monotonic()
    if boundary=='vault':svc.vault.hook=lambda:(entered.set(),release.wait(3))
    requests,_=install_http(monkeypatch,lambda request:completion() if request.method=='POST' else metadata(request),
        constructor_hook=(lambda:(entered.set(),release.wait(3))) if boundary=='client' else None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(lambda:list(svc.providers.generate(data(provider),threading.Event(),{})))
        try:assert entered.wait(3);edit(provider,change)
        finally:release.set()
        with pytest.raises(EngineAccessFailure) as failure:future.result(timeout=3)
    assert failure.value.not_executed and requests==[]


@pytest.mark.parametrize('value',[None,'','non-ascii-é',RuntimeError(PRIVATE)])
def test_declared_missing_or_invalid_key_never_falls_back_to_anonymous(saved_provider,monkeypatch,value):
    provider=saved_provider;svc=provider.service;config=svc.store.entity('providers',provider.id)
    svc.vault.values[config['credential_ref']]=value
    requests,_=install_http(monkeypatch,lambda request:metadata(request))
    with pytest.raises(EngineAccessFailure) as failure:svc.providers.models(provider.id)
    assert failure.value.category=='authentication' and failure.value.not_executed and requests==[]
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)


@pytest.mark.parametrize('change',['key','disabled'])
def test_actual_two_round_worker_never_retargets_completed_evidence(saved_provider,monkeypatch,change):
    provider=saved_provider;svc=provider.service;reads=[];execute=svc.jobs.registry.execute
    def executed(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file':reads.append(name);edit(provider,change)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',executed)
    requests,_=install_http(monkeypatch,lambda request:completion('',calls=True) if request.method=='POST' else metadata(request))
    run=start(provider);saved=terminal(svc,run['id'])
    assert saved['status']=='paused' and reads==['read_file']
    posts=[request for request in requests if request.method=='POST'];assert len(posts)==1
    assert EVIDENCE not in posts[0].content.decode()
    with svc.store._connection() as db:
        rows=db.execute('SELECT status,result FROM invocations WHERE run_id=?',(run['id'],)).fetchall()
    assert len(rows)==1 and rows[0]['status']=='completed' and EVIDENCE in rows[0]['result']
    signature=svc.store._provider_run_binding(run['id'])['signature']
    assert signature not in json.dumps(svc.store.get_chat(run['chat_id']))+json.dumps(svc.jobs.poll(run['id']))


@pytest.mark.parametrize('change',['key','aba'])
def test_restart_validates_original_scope_before_reconciliation(saved_provider,monkeypatch,change):
    provider=saved_provider;svc=provider.service;execute=svc.jobs.registry.execute
    def executed(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file':svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',executed)
    requests,_=install_http(monkeypatch,lambda request:completion('',calls=True) if request.method=='POST' else metadata(request))
    run=start(provider);assert terminal(svc,run['id'])['status']=='paused'
    if change=='key':
        old_ref=svc.store.entity('providers',provider.id)['credential_ref'];edit(provider,'key')
        assert svc.store.entity('providers',provider.id)['credential_ref']!=old_ref
    elif provider.kind=='openrouter':
        edit(provider,'consent');save(provider,enabled=True,remote_consent=True)
    else:
        save(provider,enabled=False);save(provider,enabled=True)
    home,vault=svc.store.home,svc.vault;svc.shutdown();restarted=ForgeService(core=svc.core,data_dir=home)
    restarted.vault=vault;restarted.providers.vault=vault
    try:
        requests.clear();before=database_guard(restarted.store.db_path)
        monkeypatch.setattr(restarted.jobs.registry,'execute',lambda *args,**kwargs:pytest.fail('Completed action replayed'))
        with pytest.raises(EngineRunBindingFailure,match='start a fresh request'):restarted.jobs.resume(run['id'])
        assert requests==[] and database_guard(restarted.store.db_path)==before
    finally:restarted.shutdown()


def test_metadata_only_changes_do_not_replace_accepted_provider_or_authority(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service
    requests,_=install_http(monkeypatch,lambda request:completion() if request.method=='POST' else metadata(request))
    original=svc.providers.provider(provider.id);signature=svc.providers.run_binding_signature(provider.id,original)
    before=svc.store._provider_authority_snapshot(provider.id)[1]
    config=svc.store.entity('providers',provider.id)
    svc.store.save_entity('providers',{**config,'name':'Cosmetic label','connected':True,'models':[{'name':'display-only'}],
        'account':{'is_free_tier':True},'catalog_verified_at':'synthetic-time'})
    svc.providers.invalidate(provider.id)
    assert svc.providers.provider(provider.id) is original
    assert svc.providers.run_binding_signature(provider.id,original)==signature
    assert svc.store._provider_authority_snapshot(provider.id)[1]==before
    assert list(svc.providers.generate(data(provider,_admitted_provider=original),threading.Event(),{}))[-1]['done']


def test_restart_unchanged_authority_uses_saved_evidence_without_tool_replay(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;reads=[];execute=svc.jobs.registry.execute;posts=[]
    def executed(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file':reads.append(name);svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',executed)
    def handler(request):
        if request.method!='POST':return metadata(request)
        posts.append(request)
        return completion('',calls=True) if len(posts)==1 else completion('Finished from retained evidence.')
    install_http(monkeypatch,handler)
    run=start(provider);assert terminal(svc,run['id'])['status']=='paused' and reads==['read_file']
    home,vault=svc.store.home,svc.vault;svc.shutdown();restarted=ForgeService(core=svc.core,data_dir=home)
    restarted.vault=vault;restarted.providers.vault=vault
    try:
        monkeypatch.setattr(restarted.jobs.registry,'execute',lambda *args,**kwargs:pytest.fail('Completed action replayed'))
        restarted.jobs.resume(run['id']);assert terminal(restarted,run['id'])['status']=='completed'
        assert len(posts)==2 and EVIDENCE in posts[1].content.decode()
        with restarted.store._connection() as db:assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==1
    finally:restarted.shutdown()


@pytest.mark.parametrize('saved_provider',['openrouter'],indirect=True)
@pytest.mark.parametrize('change',['key','policy'])
def test_changed_cloud_authority_never_replays_original_opaque_state(saved_provider,monkeypatch,change):
    provider=saved_provider;svc=provider.service;execute=svc.jobs.registry.execute;reads=[]
    def executed(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file':reads.append(name);edit(provider,change)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',executed)
    requests,_=install_http(monkeypatch,lambda request:completion('',calls=True,details=DETAILS) if request.method=='POST' else metadata(request))
    run=start(provider);assert terminal(svc,run['id'])['status']=='paused' and reads==['read_file']
    assert len([request for request in requests if request.method=='POST'])==1
    states=svc.store.provider_turn_states(run['id'],[row['id'] for row in svc.store.get_chat(run['chat_id'])['messages']])
    assert any(state['reasoning_details']==DETAILS for state in states.values())
    public=json.dumps([svc.store.get_chat(run['chat_id']),svc.store.events(run['id']),svc.jobs.poll(run['id']),
        svc.dispatch('run_diagnostics_export',{'run_id':run['id']})])
    assert 'synthetic-opaque-state' not in public and 'fixture-signature' not in public


@pytest.mark.parametrize('status',[302,401,429,503])
def test_http_failure_preserves_specific_recovery_without_remote_private_text(saved_provider,monkeypatch,status):
    provider=saved_provider;svc=provider.service
    def handler(request):
        return httpx.Response(status,json={'error':{'message':PRIVATE+' '+KEY}},headers={'Location':'https://replacement.invalid'}) if request.method=='POST' else metadata(request)
    requests,_=install_http(monkeypatch,handler)
    with pytest.raises(ValueError) as failure:list(svc.providers.generate(data(provider),threading.Event(),{}))
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)
    assert getattr(failure.value,'not_executed',False) is False
    assert len([request for request in requests if request.method=='POST'])==1
    assert not any(request.url.host=='replacement.invalid' for request in requests)
    if status==401:assert failure.value.category=='authentication'
    if status==429:assert failure.value.category=='rate_limit'


@pytest.mark.parametrize('saved_provider',['openrouter'],indirect=True)
def test_real_connection_test_metadata_writes_do_not_revoke_original_authority(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;install_http(monkeypatch,metadata)
    original=svc.providers.provider(provider.id);signature=svc.providers.run_binding_signature(provider.id,original)
    revision=svc.store._provider_authority_snapshot(provider.id)[1]
    svc.dispatch('openrouter_test')
    assert svc.providers.provider(provider.id) is original
    assert svc.providers.run_binding_signature(provider.id,original)==signature
    assert svc.store._provider_authority_snapshot(provider.id)[1]==revision


def test_unexpected_private_final_guard_error_is_sanitized_before_post(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service
    requests,_=install_http(monkeypatch,metadata)
    adapter=svc.providers.provider(provider.id)
    if provider.kind=='openrouter':adapter.models()
    requests.clear()
    def refused():raise ValueError(PRIVATE+' '+KEY)
    with pytest.raises(EngineAccessFailure) as failure:
        list(adapter.generate(data(provider,_request_guard=refused),threading.Event()))
    assert failure.value.category=='configuration' and failure.value.not_executed and requests==[]
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)


def test_unknown_authority_schema_keeps_workspace_open_but_refuses_affected_provider(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;requests,_=install_http(monkeypatch)
    with svc.store._connection(transaction='write') as db:db.execute('ALTER TABLE provider_authority_revisions ADD COLUMN unknown_format TEXT')
    reopened=ForgeStore(svc.store.home)
    assert reopened.get_settings()['model']==provider.model
    with pytest.raises(EngineAccessFailure):svc.providers.provider(provider.id)
    assert requests==[]


@pytest.mark.parametrize('saved_provider',['openrouter'],indirect=True)
@pytest.mark.parametrize('change',['key','disabled','deleted','policy','consent','permission','agent','resource'])
def test_cloud_catalog_cannot_authorize_a_later_post_after_revocation(saved_provider,monkeypatch,change):
    provider=saved_provider;svc=provider.service
    monkeypatch.setattr(svc.jobs,'_launch',lambda run:None)
    run=svc.store.run(start(provider)['id'])
    if change=='agent':
        svc.store.save_entity('agents',{'id':'synthetic-agent','enabled':True})
        run=svc.store.update_run(run['id'],agent_id='synthetic-agent')
    def handler(request):
        assert request.method=='GET','A revoked cloud scope reached POST'
        if change=='agent':svc.store.save_entity('agents',{'id':'synthetic-agent','enabled':False})
        elif change=='resource':svc.store.update_settings({'permission_overrides':{'tool:read_file':'deny_access'}})
        else:edit(provider,change)
        return metadata(request)
    requests,_=install_http(monkeypatch,handler)
    with pytest.raises((EngineAccessFailure,CloudPolicyRejected)):
        list(svc.providers.generate(data(provider),threading.Event(),run))
    assert len(requests)==1 and requests[0].method=='GET'
    with svc.store._connection() as db:
        row=db.execute('SELECT input_tokens,output_tokens FROM usage WHERE run_id=?',(run['id'],)).fetchone()
    assert tuple(row)==(0,0)


def test_first_authority_table_creation_is_backed_up_and_global_schema_stays7(tmp_path):
    store=ForgeStore(tmp_path/'workspace');store.update_settings({'model':'fixture'})
    with store._connection(transaction='write') as db:db.execute('DROP TABLE provider_authority_revisions')
    before=database_guard(store.db_path);reopened=ForgeStore(store.home)
    backups=list((store.home/'backups').glob('pre-provider-authority-1-*/forge.sqlite3'));assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_authority_revisions'").fetchone() is None
    db.close()
    # Store reopening deliberately closes the derived queue until real recovery.
    from forge_channel_queue import InboundQueue, PRIVATE_TABLES
    closed=database_guard(reopened.db_path,before)
    assert closed!=before and 'channel_queue_protection' in closed
    assert PRIVATE_TABLES<=closed['tables'].keys()
    assert InboundQueue(reopened).maintenance()
    assert database_guard(reopened.db_path,before)==before
    with reopened._connection() as db:assert db.execute('SELECT MAX(version) FROM forge_migrations').fetchone()[0]==7
    ForgeStore(store.home);assert len(list((store.home/'backups').glob('pre-provider-authority-1-*')))==1


def test_failed_authority_backup_cannot_add_table_or_change_prior_state(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'workspace');store.update_settings({'model':'fixture'})
    with store._connection(transaction='write') as db:db.execute('DROP TABLE provider_authority_revisions')
    before=database_guard(store.db_path);connect=sqlite3.connect
    def failed(path,*args,**kwargs):
        if 'pre-provider-authority-1-' in str(path):raise OSError(PRIVATE)
        return connect(path,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',failed)
    with pytest.raises(ValueError,match='backup could not be verified') as failure:ForgeStore(store.home)
    assert PRIVATE not in str(failure.value) and database_guard(store.db_path)==before


@pytest.mark.parametrize('alias',['openai','compatible'])
def test_legacy_protocol_alias_save_models_and_queue_share_saved_authority(fixture,monkeypatch,alias):
    svc,project=fixture
    config=svc.dispatch('provider_save',{'id':'legacy','type':alias,'base_url':'https://legacy.invalid/v1',
        'api_key':KEY,'enabled':True,'context_limit':32768})
    assert config['kind']==alias
    requests,_=install_http(monkeypatch,metadata)
    assert any(item['name']=='fixture' for item in svc.providers.models('legacy'))
    assert requests[0].headers['authorization']=='Bearer '+KEY
    with ThreadPoolExecutor(max_workers=1) as executor:
        with svc.providers.queue.lease(threading.Event()):
            future=executor.submit(lambda:list(svc.providers.generate({'provider_id':'legacy','model':'fixture','context':8192,
                'tokens':1024,'thinking':False,'messages':[{'role':'user','content':'Synthetic legacy task.'}]},threading.Event(),{})))
            wait_for(lambda:bool(svc.providers.queue.pending))
            svc.dispatch('provider_save',{'id':'legacy','enabled':False})
        with pytest.raises(EngineAccessFailure):future.result(timeout=3)
    assert len(requests)==1


def test_unknown_protocol_is_rejected_before_vault_or_state_changes(fixture,monkeypatch):
    svc,project=fixture;requests,_=install_http(monkeypatch)
    before=database_guard(svc.store.db_path);secrets=deepcopy(svc.vault.values)
    with pytest.raises(ValueError,match='engine protocol'):
        svc.dispatch('provider_save',{'id':'engine','type':'unsupported-protocol','url':'https://engine.invalid/v1','api_key':KEY})
    assert requests==[] and database_guard(svc.store.db_path)==before and svc.vault.values==secrets


@pytest.mark.parametrize('saved_provider',['openai-compatible'],indirect=True)
def test_private_revision_and_profile_are_one_coherent_sql_snapshot(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;entered,release=threading.Event(),threading.Event()
    original=ForgeStore._provider_authority_revision
    expected=svc.store._provider_authority_snapshot(provider.id)
    def held(db,identifier):
        value=original(db,identifier)
        if not entered.is_set():entered.set();assert release.wait(3)
        return value
    monkeypatch.setattr(ForgeStore,'_provider_authority_revision',staticmethod(held))
    with ThreadPoolExecutor(max_workers=1) as executor:
        reader=executor.submit(svc.store._provider_authority_snapshot,provider.id)
        try:assert entered.wait(3);save(provider,url='https://replacement.invalid/v1')
        finally:release.set()
        assert reader.result(timeout=3)==expected
    current,revision=svc.store._provider_authority_snapshot(provider.id)
    assert current['url']=='https://replacement.invalid/v1' and revision==expected[1]+1


@pytest.mark.parametrize('saved_provider',['openai-compatible'],indirect=True)
def test_anonymous_endpoint_aba_across_restart_cannot_rebind_saved_work(saved_provider,monkeypatch):
    provider=saved_provider;svc=provider.service;provider.id='anonymous'
    svc.dispatch('provider_save',{'id':provider.id,'kind':provider.kind,'url':'https://engine.invalid/v1',
        'enabled':True,'capabilities':['tools'],'context_limit':32768})
    svc.store.update_settings({'provider_id':provider.id})
    execute=svc.jobs.registry.execute
    def executed(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file':svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',executed)
    requests,_=install_http(monkeypatch,lambda request:completion('',calls=True) if request.method=='POST' else metadata(request))
    run=start(provider);assert terminal(svc,run['id'])['status']=='paused'
    before=svc.store._provider_authority_snapshot(provider.id)[1]
    save(provider,url='https://replacement.invalid/v1');save(provider,url='https://engine.invalid/v1')
    config,revision=svc.store._provider_authority_snapshot(provider.id)
    assert revision==before+2 and not config.get('credential_ref')
    home,vault=svc.store.home,svc.vault;svc.shutdown();restarted=ForgeService(core=svc.core,data_dir=home)
    restarted.vault=vault;restarted.providers.vault=vault
    try:
        requests.clear();guard=database_guard(restarted.store.db_path)
        with pytest.raises(EngineRunBindingFailure):restarted.jobs.resume(run['id'])
        assert requests==[] and database_guard(restarted.store.db_path)==guard
    finally:restarted.shutdown()


@pytest.mark.parametrize('saved_provider',['openai-compatible'],indirect=True)
@pytest.mark.parametrize('change',['endpoint','key','disabled'])
@pytest.mark.parametrize('operation',['benchmark','calibrate'])
def test_deferred_compatible_performance_worker_cannot_adopt_changed_profile(saved_provider,monkeypatch,change,operation):
    provider=saved_provider;svc=provider.service
    requests,_=install_http(monkeypatch,lambda request:completion() if request.method=='POST' else metadata(request))
    workers=deferred_workers(monkeypatch);manager=PerformanceManager(svc)
    try:
        job=manager.start({},operation);assert len(workers)==1
        assert '_admitted_provider' not in json.dumps(svc.store.entities('benchmarks'))
        requests.clear();edit(provider,change)
        workers[0].target(*workers[0].args)
        saved=svc.store.entity('benchmarks',job['id'])
        assert saved['status']=='failed' and saved['stop_category']=='configuration'
        assert requests==[] and manager.jobs=={}
    finally:manager.shutdown()


@pytest.mark.parametrize('saved_provider',['openrouter'],indirect=True)
@pytest.mark.parametrize('boundary',['worker','second_probe'])
def test_existing_cloud_benchmark_keeps_its_accepted_actor(saved_provider,monkeypatch,boundary):
    provider=saved_provider;svc=provider.service;posts=[]
    def handler(request):
        if request.method!='POST':return metadata(request)
        posts.append(request)
        if boundary=='second_probe':edit(provider,'key')
        return completion('def sum_even(values):\n    return sum(x for x in values if x % 2 == 0)')
    install_http(monkeypatch,handler);workers=deferred_workers(monkeypatch);manager=PerformanceManager(svc)
    try:
        with pytest.raises(ValueError,match='local engines'):manager.start({},'calibrate')
        job=manager.start({'tools':False,'vision':False},'benchmark');assert len(workers)==1
        if boundary=='worker':edit(provider,'key')
        workers[0].target(*workers[0].args)
        saved=svc.store.entity('benchmarks',job['id'])
        assert saved['status']=='failed' and saved['stop_category']=='configuration'
        assert len(posts)==(1 if boundary=='second_probe' else 0)
        assert len(saved['results'])==(1 if boundary=='second_probe' else 0)
        assert '_admitted_provider' not in json.dumps(saved) and KEY not in json.dumps(saved)
    finally:manager.shutdown()
