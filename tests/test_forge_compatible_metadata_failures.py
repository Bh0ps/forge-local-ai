"""Current metadata ownership and fixed failures with disposable HTTP/vaults."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ssl
import threading

import httpx
import pytest

from forge_engine_auth import EngineAccessFailure
from forge_inference import ProviderStreamFailure
from test_forge_engine_run_binding import fixture
from test_forge_ollama_auth import KEY, PRIVATE, install_http


BASE = 'https://engine.invalid/v1'
CHANGED = 'Provider configuration changed during metadata loading. Retry with the current engine.'


@pytest.fixture
def engine(fixture):
    svc, _ = fixture
    svc.dispatch('provider_save', {'id':'engine','kind':'openai-compatible','url':BASE,
        'preset_id':'lm-studio','api_key':KEY,'context_limit':32768,'capabilities':['tools']})
    return svc


def facts(context=4096):
    return {'models':[{'type':'llm','key':'fixture','max_context_length':131072,
        'loaded_instances':[{'id':'fixture','config':{'context_length':context}}],
        'capabilities':{'reasoning':{'allowed_options':['off','on']}}}]}


def show(svc):
    return svc.dispatch('show',{'provider_id':'engine','model':'fixture'})


def accurate_failure(exc):
    assert type(exc) is ValueError and str(exc)==CHANGED
    assert not hasattr(exc,'not_executed') and 'No request' not in str(exc)
    assert PRIVATE not in str(exc) and KEY not in str(exc)


@pytest.mark.parametrize('change',['endpoint','key','preset','deleted','invalid_current','metadata_invalidation'])
def test_show_rejects_old_facts_after_real_owner_change_without_automatic_retry(engine,monkeypatch,change):
    svc=engine
    def handle(request):
        if change=='endpoint':svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/v1'})
        elif change=='key':svc.dispatch('provider_save',{'id':'engine','api_key':'replacement-synthetic-key'})
        elif change=='preset':svc.dispatch('provider_save',{'id':'engine','preset_id':None})
        elif change=='deleted':svc.store.delete_entity('providers','engine')
        elif change=='invalid_current':
            def refuse(identifier):raise EngineAccessFailure('configuration')
            monkeypatch.setattr(svc.store,'_provider_authority_snapshot',refuse)
        else:svc.providers.invalidate('engine')
        return httpx.Response(200,json=facts())
    requests,_=install_http(monkeypatch,handle)
    with pytest.raises(ValueError) as captured:show(svc)
    accurate_failure(captured.value)
    assert len(requests)==1 and requests[0].method=='GET'
    assert not svc.providers.capability_cache and not svc.providers.metadata_pending


def test_explicit_retry_after_endpoint_edit_reads_only_current_exact_origin(engine,monkeypatch):
    svc=engine
    def handle(request):
        if request.url.host=='engine.invalid':
            svc.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/v1'})
            return httpx.Response(200,json=facts())
        return httpx.Response(200,json=facts(8192))
    requests,policies=install_http(monkeypatch,handle)
    with pytest.raises(ValueError) as captured:show(svc)
    accurate_failure(captured.value)
    assert len(requests)==1
    result=show(svc)
    assert result['context_limit_source']=='loaded_instance' and result['model_info']['local.context_length']==8192
    assert [request.url.host for request in requests]==['engine.invalid','replacement.invalid']
    assert all(request.method=='GET' for request in requests)
    assert all(policy['trust_env'] is policy['follow_redirects'] is False for policy in policies)


@pytest.mark.parametrize('failure',['connect','timeout','401','403','malformed','oversized_shape'])
def test_optional_lm_show_failure_retains_bounded_unknown_and_configured_limit(engine,monkeypatch,failure):
    before=deepcopy(engine.store.entity('providers','engine'))
    def handle(request):
        if failure=='connect':raise httpx.ConnectError(PRIVATE,request=request)
        if failure=='timeout':raise httpx.ReadTimeout(PRIVATE,request=request)
        if failure in ('401','403'):return httpx.Response(int(failure),text=PRIVATE+KEY)
        if failure=='malformed':return httpx.Response(200,content=b'not-json')
        return httpx.Response(200,json={'models':[facts()['models'][0]]*257})
    requests,_=install_http(monkeypatch,handle)
    result=show(engine)
    assert result=={'capabilities':['tools'],'model_info':{'local.context_length':32768},
        'context_limit_source':'configured','metadata_state':'unknown'}
    assert len(requests)==1 and PRIVATE not in str(result) and KEY not in str(result)
    assert engine.store.entity('providers','engine')==before and not engine.providers.metadata_pending


def test_show_keeps_exact_loaded_facts_and_fresh_reads_even_when_pool_has_a_cache(engine,monkeypatch):
    count=[]
    def handle(request):
        count.append(1);return httpx.Response(200,json=facts(4096*len(count)))
    requests,_=install_http(monkeypatch,handle)
    first=show(engine);second=show(engine)
    assert first['model_info']['local.context_length']==4096 and second['model_info']['local.context_length']==8192
    assert len(requests)==2 and second['reasoning']['control_supported'] is False


def test_current_service_show_coalesces_owned_concurrent_reads(engine,monkeypatch):
    entered=threading.Event();release=threading.Event();following=threading.Event()
    def handle(request):entered.set();assert release.wait(3);return httpx.Response(200,json=facts())
    requests,_=install_http(monkeypatch,handle)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner=executor.submit(show,engine);assert entered.wait(2)
        with engine.providers.lock:
            pending=next(iter(engine.providers.metadata_pending.values()));event=pending['event']
            class Wait:
                def set(self):event.set()
                def wait(self,timeout):following.set();return event.wait(timeout)
            pending['event']=Wait()
        follower=executor.submit(show,engine);assert following.wait(2);release.set()
        results=[owner.result(3),follower.result(3)]
    assert results[0]==results[1] and len(requests)==1 and not engine.providers.metadata_pending
    results[0]['advertised_metadata']['loaded_instances'].clear()
    assert results[1]['advertised_metadata']['loaded_instances']


def test_default_ollama_show_shape_and_refresh_contract_are_preserved(engine):
    expected=engine.core.dispatch('show',{'model':'fixture'})
    assert engine.dispatch('show',{'provider_id':'ollama','name':'fixture'})==expected
    assert engine.dispatch('show',{'provider_id':'ollama','model':'fixture'})==expected


@pytest.mark.parametrize('boundary',['vault','revoked','cancelled'])
def test_pre_get_auth_refusal_keeps_specific_nonexecuted_contract(engine,monkeypatch,boundary):
    def hook():
        if boundary=='vault':raise OSError(PRIVATE+KEY)
        if boundary=='revoked':engine.dispatch('provider_save',{'id':'engine','url':'https://replacement.invalid/v1'})
        else:engine.stop_event.set()
    engine.vault.hook=hook
    requests,_=install_http(monkeypatch,lambda request:pytest.fail('Refused show sent HTTP'))
    with pytest.raises(EngineAccessFailure) as captured:show(engine)
    assert captured.value.category=={'vault':'authentication','revoked':'configuration','cancelled':'cancelled'}[boundary]
    assert captured.value.not_executed is True and requests==[]
    assert PRIVATE not in str(captured.value) and KEY not in str(captured.value)


@pytest.mark.parametrize('exception',[FileNotFoundError,PermissionError])
def test_real_client_constructor_oserror_has_fixed_catalog_recovery(engine,monkeypatch,exception):
    attempts=[]
    monkeypatch.setattr(ssl,'create_default_context',lambda *args,**kwargs:(_ for _ in ()).throw(exception(PRIVATE+KEY)))
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',lambda *args:attempts.append(1))
    with pytest.raises(ProviderStreamFailure) as captured:
        engine.dispatch('models',{'provider_id':'engine','refresh':True})
    assert captured.value.category=='metadata' and captured.value.http_status is None
    assert not hasattr(captured.value,'not_executed') and attempts==[]
    assert PRIVATE not in str(captured.value) and KEY not in str(captured.value)


def test_dmr_catalog_oserror_keeps_its_existing_specific_recovery(engine,monkeypatch):
    from forge_provider_metadata import DockerModelRunnerMetadataInvalid
    engine.dispatch('provider_save',{'id':'engine','preset_id':'docker-model-runner',
        'url':'http://127.0.0.1:12434/engines/llama.cpp/v1'})
    monkeypatch.setattr(ssl,'create_default_context',lambda *args,**kwargs:(_ for _ in ()).throw(OSError(PRIVATE)))
    with pytest.raises(ProviderStreamFailure) as captured:
        engine.dispatch('models',{'provider_id':'engine','refresh':True})
    assert str(captured.value)==str(DockerModelRunnerMetadataInvalid()) and captured.value.category=='metadata'


def test_oserror_read_after_get_does_not_claim_metadata_request_was_unexecuted(engine,monkeypatch):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):raise OSError(PRIVATE+KEY);yield b''
    requests,_=install_http(monkeypatch,lambda request:httpx.Response(200,stream=Broken()))
    with pytest.raises(ProviderStreamFailure) as captured:
        engine.dispatch('models',{'provider_id':'engine','refresh':True})
    assert captured.value.category=='metadata' and len(requests)==1
    assert not hasattr(captured.value,'not_executed') and PRIVATE not in str(captured.value) and KEY not in str(captured.value)


@pytest.mark.parametrize('status',[401,403])
def test_catalog_http_authentication_category_and_status_remain_specific(engine,monkeypatch,status):
    requests,_=install_http(monkeypatch,lambda request:httpx.Response(status,text=PRIVATE+KEY))
    with pytest.raises(ProviderStreamFailure) as captured:
        engine.dispatch('models',{'provider_id':'engine','refresh':True})
    assert captured.value.category=='authentication' and captured.value.http_status==status and len(requests)==1
    assert not hasattr(captured.value,'not_executed') and PRIVATE not in str(captured.value)


def test_deleted_shared_owner_lookup_is_fixed_for_owner_and_follower(engine,monkeypatch):
    entered=threading.Event();release=threading.Event();following=threading.Event()
    def handle(request):entered.set();assert release.wait(3);return httpx.Response(200,json=facts())
    requests,_=install_http(monkeypatch,handle)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner=executor.submit(show,engine);assert entered.wait(2)
        with engine.providers.lock:
            pending=next(iter(engine.providers.metadata_pending.values()));event=pending['event']
            class Wait:
                def set(self):event.set()
                def wait(self,timeout):following.set();return event.wait(timeout)
            pending['event']=Wait()
        follower=executor.submit(show,engine);assert following.wait(2)
        engine.store.delete_entity('providers','engine');release.set()
        for future in [owner,follower]:
            with pytest.raises(ValueError) as captured:future.result(3)
            accurate_failure(captured.value)
    assert len(requests)==1 and not engine.providers.metadata_pending and not engine.providers.capability_cache
