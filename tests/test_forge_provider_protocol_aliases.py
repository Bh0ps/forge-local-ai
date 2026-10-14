"""Protocol equivalence retains keys, never accepted-run or endpoint authority."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import httpx
import pytest

from forge_engine_auth import EngineAccessFailure, EngineRunBindingFailure
from forge_provider_metadata import ProviderProbeRevoked
from forge_service import ForgeService
from forge_updates import database_guard
from test_forge_core import Engine
from test_forge_engine_run_binding import fixture, terminal, EVIDENCE
from test_forge_ollama_auth import KEY, PRIVATE, install_http, wait_for
from test_forge_textgen import response, invocations


BASE = 'https://synthetic-alias.invalid/v1'
KINDS = ('openai', 'compatible', 'openai-compatible')


@pytest.fixture(params=KINDS)
def provider(fixture, request):
    svc, project = fixture
    saved = svc.dispatch('provider_save', {'id':'engine', 'kind':request.param,
        'name':'Synthetic alias', 'url':BASE, 'api_key':KEY, 'preset_id':'lm-studio',
        'enabled':True, 'context_limit':32768, 'capabilities':['tools'],
        'resource_id':'synthetic-lane', 'independent_resource':True})
    return svc, project, saved


def catalog(request):
    if request.url.path.endswith('/tags'):
        return httpx.Response(200, json={'models':[{'name':'fixture'}]})
    if request.url.path == '/api/v1/models':
        return httpx.Response(200, json={'models':[{'type':'llm','key':'fixture'}]})
    return httpx.Response(200, json={'data':[{'id':'fixture'}]})


def request_data():
    return {'model':'fixture','context':8192,'tokens':1024,'thinking':False,
        'messages':[{'role':'user','content':'Synthetic alias task.'}]}


@pytest.mark.parametrize('explicit_preset', [False, True])
def test_actual_canonical_probe_retains_key_and_preset_without_mutation(provider, monkeypatch, explicit_preset):
    svc, _, saved = provider
    values = deepcopy(svc.vault.values); before = database_guard(svc.store.db_path)
    requests, policies = install_http(monkeypatch, catalog)
    result = svc.dispatch('provider_test', {'id':'engine','metadata':True,
        'draft':{'kind':'openai-compatible','url':BASE + '/',
            **({'preset_id':'lm-studio'} if explicit_preset else {})}})
    assert result['ok'] is True and result['model_count'] == 1
    assert result['metadata']['state'] == 'available'
    assert [request.url.path for request in requests] == ['/v1/models','/api/v1/models']
    assert all(request.headers['authorization']=='Bearer ' + KEY for request in requests)
    assert svc.vault.reads == [saved['credential_ref']] and svc.vault.values == values
    assert svc.store.entity('providers','engine') == saved and database_guard(svc.store.db_path) == before
    assert all(policy['trust_env'] is policy['follow_redirects'] is False for policy in policies)


def test_actual_canonical_save_keeps_owned_key_preset_and_user_fields(provider, monkeypatch):
    svc, _, previous = provider
    settings = svc.store.get_settings(); values = deepcopy(svc.vault.values)
    requests, _ = install_http(monkeypatch, lambda request:catalog(request) if request.method=='GET' else response())
    saved = svc.dispatch('provider_save', {'id':'engine','name':'Edited name',
        'kind':'openai-compatible','url':BASE + '/', 'credential_ref':'untrusted-client-reference'})
    assert saved['kind'] == 'openai-compatible' and saved['url'] == BASE
    for field in ('credential_ref','preset_id','capabilities','context_limit','enabled','resource_id','independent_resource'):
        assert saved[field] == previous[field]
    assert requests == [] and svc.vault.reads == [] and svc.vault.values == values
    current = svc.providers.provider('engine')
    assert current.models()[0]['name'] == 'fixture'
    assert list(current.generate(request_data(),threading.Event()))[-1]['done'] is True
    assert all(request.headers['authorization']=='Bearer ' + KEY for request in requests)
    assert svc.store.get_settings() == settings


@pytest.mark.parametrize('target', ['https://other.invalid/v1',
    'https://synthetic-alias.invalid/other', 'https://synthetic-alias.invalid:9443/v1'])
def test_changed_endpoint_never_inherits_old_key_in_test_or_save(provider, monkeypatch, target):
    svc, _, previous = provider
    values = deepcopy(svc.vault.values)
    requests, _ = install_http(monkeypatch, catalog)
    assert svc.dispatch('provider_test', {'id':'engine',
        'draft':{'kind':'openai-compatible','url':target}})['ok'] is True
    assert len(requests)==1 and 'authorization' not in requests[0].headers and not svc.vault.reads
    saved = svc.dispatch('provider_save', {'id':'engine','kind':'openai-compatible','url':target})
    assert saved.get('credential_ref') is None and saved['url'] == target
    assert svc.vault.values == values and svc.store.get_settings()['permission_profile'] == 'full_access'


def test_actual_family_change_clears_old_preset_and_key_even_at_same_endpoint(provider, monkeypatch):
    svc, _, previous = provider
    requests, _ = install_http(monkeypatch, catalog)
    result = svc.dispatch('provider_test', {'id':'engine','metadata':True,'draft':{'kind':'ollama','url':BASE}})
    assert result['ok'] and result['metadata']['state']=='unsupported'
    assert len(requests)==1 and requests[0].url.path.endswith('/tags')
    assert 'authorization' not in requests[0].headers and svc.vault.reads == []
    saved = svc.dispatch('provider_save', {'id':'engine','kind':'ollama','url':BASE})
    assert saved.get('preset_id') is None and saved.get('credential_ref') is None
    assert saved['capabilities']==previous['capabilities'] and saved['context_limit']==previous['context_limit']


@pytest.mark.parametrize('value', [None, '', '\u2603'])
def test_declared_missing_or_invalid_key_never_becomes_anonymous_success(provider, monkeypatch, value):
    svc, _, saved = provider
    svc.vault.values[saved['credential_ref']] = value
    requests, _ = install_http(monkeypatch, lambda request:pytest.fail('Invalid saved key sent HTTP'))
    with pytest.raises(EngineAccessFailure) as captured:
        svc.dispatch('provider_test', {'id':'engine','draft':{'kind':'openai-compatible','url':BASE}})
    assert captured.value.category=='authentication' and captured.value.not_executed is True
    assert PRIVATE not in str(captured.value) and KEY not in str(captured.value) and requests == []


def test_explicit_edited_key_is_used_without_replacing_saved_owner(provider, monkeypatch):
    svc, _, saved = provider
    values = deepcopy(svc.vault.values)
    requests, _ = install_http(monkeypatch, catalog)
    assert svc.dispatch('provider_test', {'id':'engine','api_key':'synthetic-explicit-key',
        'draft':{'kind':'openai-compatible','url':'https://other.invalid/v1'}})['ok']
    assert requests[0].headers['authorization']=='Bearer synthetic-explicit-key'
    assert not svc.vault.reads and svc.vault.values == values and svc.store.entity('providers','engine') == saved


@pytest.mark.parametrize('kind', KINDS)
def test_anonymous_profiles_remain_anonymous(fixture, monkeypatch, kind):
    svc, _ = fixture
    saved = svc.dispatch('provider_save', {'id':'anonymous','kind':kind,'url':BASE,'preset_id':'lm-studio'})
    requests, _ = install_http(monkeypatch, catalog)
    result = svc.dispatch('provider_test', {'id':saved['id'],'metadata':True,
        'draft':{'kind':'openai-compatible','url':BASE}})
    assert result['ok'] and result['metadata']['state']=='available'
    assert all('authorization' not in request.headers for request in requests)
    assert svc.vault.reads == [] and saved.get('credential_ref') is None


@pytest.mark.parametrize('boundary', ['vault','catalog'])
def test_pending_probe_refuses_changed_saved_profile_before_followup_metadata(provider, monkeypatch, boundary):
    svc, _, saved = provider
    entered, release = threading.Event(), threading.Event()
    def wait(): entered.set(); assert release.wait(3)
    if boundary=='vault': svc.vault.hook=wait
    def handle(request):
        if boundary=='catalog': wait()
        return catalog(request)
    requests, _ = install_http(monkeypatch, handle)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending=executor.submit(svc.dispatch,'provider_test',{'id':'engine','metadata':True,
            'draft':{'kind':'openai-compatible','url':BASE}})
        try:
            assert entered.wait(3)
            svc.dispatch('provider_save',{'id':'engine','name':'New identity'})
        finally: release.set()
        with pytest.raises(ProviderProbeRevoked): pending.result(timeout=3)
    assert len(requests)==(0 if boundary=='vault' else 1)
    assert svc.store.entity('providers','engine')['credential_ref']==saved['credential_ref']


def test_cancellation_after_saved_vault_read_still_prevents_http(provider, monkeypatch):
    svc, _, _ = provider
    svc.vault.hook=svc.stop_event.set
    requests, _ = install_http(monkeypatch, lambda request:pytest.fail('Cancelled probe sent HTTP'))
    with pytest.raises(ProviderProbeRevoked):
        svc.dispatch('provider_test',{'id':'engine','draft':{'kind':'openai-compatible','url':BASE}})
    assert requests == []


@pytest.mark.parametrize('provider', ['openai','compatible'], indirect=True)
def test_actual_queued_worker_still_revokes_raw_kind_change_even_when_key_retained(provider, monkeypatch):
    svc, project, previous = provider
    requests, _ = install_http(monkeypatch, catalog)
    lane=svc.providers.queue_for('engine',svc.providers.provider('engine'))
    with lane.lease(threading.Event()):
        run=svc.jobs.start({'text':'Synthetic queued request.','project_id':project['id']})
        wait_for(lambda:bool(lane.pending))
        original_binding=svc.store._provider_run_binding(run['id'])
        revision=svc.store._provider_authority_snapshot('engine')[1]
        count=len(requests)
        saved=svc.dispatch('provider_save',{'id':'engine','kind':'openai-compatible','url':BASE})
        assert saved['credential_ref']==previous['credential_ref'] and saved['preset_id']=='lm-studio'
        assert svc.store._provider_authority_snapshot('engine')[1] > revision
    result=terminal(svc,run['id'])
    assert result['status']=='paused' and 'start a fresh request' in result['recovery']
    assert len(requests)==count and all(request.method=='GET' for request in requests)
    assert invocations(svc,run['id']) == [] and svc.store._provider_run_binding(run['id'])==original_binding
    with pytest.raises(EngineRunBindingFailure): svc.jobs.resume(run['id'])


@pytest.mark.parametrize('provider', ['openai-compatible'], indirect=True)
def test_unchanged_canonical_save_does_not_false_revoke_current_queue(provider, monkeypatch):
    svc, project, previous = provider
    requests, _ = install_http(monkeypatch, lambda request:catalog(request) if request.method=='GET' else response())
    lane=svc.providers.queue_for('engine',svc.providers.provider('engine'))
    with lane.lease(threading.Event()):
        run=svc.jobs.start({'text':'Synthetic queued request.','project_id':project['id']})
        wait_for(lambda:bool(lane.pending))
        revision=svc.store._provider_authority_snapshot('engine')[1]
        saved=svc.dispatch('provider_save',{'id':'engine','kind':'openai-compatible','url':BASE})
        assert saved['credential_ref']==previous['credential_ref']
        assert svc.store._provider_authority_snapshot('engine')[1]==revision
    assert terminal(svc,run['id'])['status']=='completed'
    assert len([request for request in requests if request.method=='POST'])==1


@pytest.mark.parametrize('provider', ['openai','compatible'], indirect=True)
def test_restart_preserves_completed_tool_and_refuses_old_raw_binding(provider, monkeypatch):
    svc, project, previous = provider
    reads=[]; execute=svc.jobs.registry.execute
    def once(run,name,args,cancel,**kwargs):
        result=execute(run,name,args,cancel,**kwargs)
        if name=='read_file': reads.append(name); svc.jobs.cancel(run['id'],pause=True)
        return result
    monkeypatch.setattr(svc.jobs.registry,'execute',once)
    requests, _ = install_http(monkeypatch, lambda request:catalog(request) if request.method=='GET' else response(tool=True))
    run=svc.jobs.start({'text':'Read evidence.txt and report it.','project_id':project['id']})
    assert terminal(svc,run['id'])['status']=='paused' and reads==['read_file']
    rows=invocations(svc,run['id']); binding=svc.store._provider_run_binding(run['id'])
    saved=svc.dispatch('provider_save',{'id':'engine','kind':'openai-compatible','url':BASE})
    assert saved['credential_ref']==previous['credential_ref']
    home=svc.store.home; values=deepcopy(svc.vault.values); svc.shutdown()
    restarted=ForgeService(core=Engine(),data_dir=home); restarted.vault.values.update(values)
    try:
        monkeypatch.setattr(restarted.jobs.registry,'execute',lambda *args,**kwargs:pytest.fail('Replayed completed tool'))
        before=database_guard(restarted.store.db_path); count=len(requests)
        with pytest.raises(EngineRunBindingFailure): restarted.jobs.resume(run['id'])
        assert database_guard(restarted.store.db_path)==before and len(requests)==count
        assert invocations(restarted,run['id'])==rows and EVIDENCE in json.dumps(rows)
        assert restarted.store._provider_run_binding(run['id'])==binding and reads==['read_file']
    finally: restarted.shutdown()
