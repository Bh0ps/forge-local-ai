"""Credential admission uses memory-only fixtures and offline HTTP transports."""
import asyncio
from copy import deepcopy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from core import Core, OllamaRequestFailure
from forge_engine_auth import EngineAccessFailure
from forge_inference import ProviderPool
from forge_performance import PerformanceManager, calibration_facts
from forge_provider_probe import probe_provider
from forge_service import ForgeService
from forge_store import DEFAULTS
import forge_performance as performance


KEY = 'synthetic-auth-key'
PRIVATE = 'synthetic-private-credential-and-path'
BASE = 'https://engine.invalid/proxy/api'


class MemoryStore:
    def __init__(self):
        self.profile = {'id': 'engine', 'kind': 'ollama', 'name': 'Synthetic engine',
                        'url': BASE, 'credential_ref': 'reference', 'enabled': True,
                        'updated_at': 'initial', 'preset_id': 'ollama', 'context_limit': 32768}
        self.settings = {**deepcopy(DEFAULTS), 'provider_id': 'engine', 'model': 'fixture',
                         'context': 8192, 'thinking': False}
        self.saved, self.usage, self.events = {}, [], []
        self.revision, self.read_hook = 0, None
        self.lock = threading.RLock()

    def entity(self, kind, identifier):
        if self.read_hook: self.read_hook()
        with self.lock:
            if kind == 'providers' and identifier == 'engine' and self.profile is not None:
                return deepcopy(self.profile)
            if (kind, identifier) in self.saved: return deepcopy(self.saved[kind, identifier])
        raise ValueError(PRIVATE)

    def entities(self, kind):
        if kind == 'providers': return [deepcopy(self.profile)] if self.profile else []
        return [deepcopy(value) for (category, _), value in self.saved.items() if category == kind]

    def save_entity(self, kind, data):
        # This also proves that private admitted provider objects are not persisted.
        json.dumps(data)
        self.revision += 1
        result = {**deepcopy(data), 'id': data.get('id', 'job'), 'updated_at': str(self.revision)}
        with self.lock:
            if kind == 'providers': self.profile = result
            else: self.saved[kind, result['id']] = result
        return deepcopy(result)

    def get_settings(self): return deepcopy(self.settings)
    def runs(self): return []
    def record_usage(self, value): json.dumps(value); self.usage.append(deepcopy(value))
    def event(self, *args, **kwargs): json.dumps(kwargs); self.events.append(kwargs)


class FakeVault:
    def __init__(self): self.values = {'reference': KEY}; self.reads = []; self.hook = None
    def get(self, reference):
        self.reads.append(reference)
        if self.hook: self.hook()
        value = self.values.get(reference)
        if isinstance(value, Exception): raise value
        return value
    def put(self, value):
        reference = 'replacement-' + str(len(self.values))
        self.values[reference] = value
        return reference


@pytest.fixture
def engine():
    store, vault, stopped = MemoryStore(), FakeVault(), threading.Event()
    pool = ProviderPool(Core(), store, vault, stopped=stopped.is_set)
    facade = SimpleNamespace(store=store, vault=vault, providers=pool,
        stop_event=stopped, performance_manager=None, admission_lock=threading.RLock(),
        jobs=SimpleNamespace(lock=threading.RLock(), jobs={}))
    return facade


def save(engine, **changes):
    # Exercise the real saved-profile invalidation boundary, not a constructor-only fake.
    return ForgeService.provider_save(engine, {'id': 'engine', **changes})


def install_http(monkeypatch, handler=None, constructor_hook=None):
    requests, policies = [], []
    def respond(request):
        requests.append(request)
        if handler: return handler(request)
        if request.url.path.endswith('/chat'):
            return httpx.Response(200, content=(json.dumps({'message': {'content': 'Synthetic answer'},
                'done': True, 'prompt_eval_count': 8, 'eval_count': 4}) + '\n').encode())
        return httpx.Response(200, json={'models': [{'name': 'fixture'}], 'capabilities': ['tools'],
            'model_info': {'fixture.context_length': 32768}, 'version': 'synthetic-version',
            'done': True, 'prompt_eval_count': 0, 'eval_count': 0})
    client, async_client = httpx.Client, httpx.AsyncClient
    def factory(cls):
        def create(**kwargs):
            if constructor_hook: constructor_hook()
            policies.append(kwargs)
            assert kwargs['trust_env'] is False and kwargs.get('follow_redirects') is False
            return cls(transport=httpx.MockTransport(respond), **kwargs)
        return create
    monkeypatch.setattr(httpx, 'Client', factory(client))
    monkeypatch.setattr(httpx, 'AsyncClient', factory(async_client))
    return requests, policies


def request_data():
    return {'provider_id': 'engine', 'model': 'fixture', 'context': 8192,
            'tokens': 1024, 'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    pytest.fail('Offline fixture did not reach its expected boundary')


def assert_refused(future, category='configuration'):
    with pytest.raises(EngineAccessFailure) as failure: future.result(timeout=3)
    assert failure.value.category == category and failure.value.not_executed is True
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)
    return failure.value


def test_all_named_ollama_paths_authenticate_lazily_and_keep_private_fields_out(engine, monkeypatch):
    requests, _ = install_http(monkeypatch)
    provider = engine.providers.provider('engine')
    assert engine.vault.reads == [] and requests == []
    assert probe_provider(engine, {'id': 'engine', 'draft': {'kind': 'ollama', 'url': BASE}})['ok']
    assert engine.providers.models('engine')[0]['name'] == 'fixture'
    engine.providers.capabilities('engine', 'fixture')
    for path in ('/version', '/ps'): provider.core.request('GET', path)
    assert list(engine.providers.generate(request_data(), threading.Event(), {}))[-1]['done']
    for keep in ('10m', 0):
        url = provider.core.base + '/generate'
        PerformanceManager._resident_request(url, {'model': 'fixture', 'keep_alive': keep},
            threading.Event(), headers_factory=lambda: provider.core.request_headers(url), response_error=Core._response_json)
    for action in ('pull', 'delete'): provider.core.dispatch(action, {'model': 'fixture'})
    assert len(requests) == len(engine.vault.reads) == 10
    assert all(request.headers['authorization'] == 'Bearer ' + KEY for request in requests)
    assert {request.url.path for request in requests} == {BASE.split('engine.invalid')[1] + path for path in
        ('/tags', '/show', '/version', '/ps', '/chat', '/generate', '/pull', '/delete')}
    for request in requests:
        assert KEY not in request.content.decode() and '_admitted_provider' not in request.content.decode()
    assert KEY not in json.dumps(engine.store.profile) + json.dumps(engine.store.usage)


def test_default_and_unkeyed_saved_engine_stay_anonymous(engine, monkeypatch):
    requests, _ = install_http(monkeypatch)
    engine.store.profile.pop('credential_ref')
    Core().dispatch('models', {})
    engine.providers.models('engine')
    list(engine.providers.generate(request_data(), threading.Event(), {}))
    assert len(requests) == 3 and engine.vault.reads == []
    assert all('authorization' not in request.headers for request in requests)


@pytest.mark.parametrize('value', [None, '', 123, 'non-ascii-\u00e9', 'unsafe\x7fkey', 'unsafe\r\nkey', 'x' * 8193, RuntimeError(PRIVATE)])
def test_unusable_required_key_is_fixed_auth_failure_without_anonymous_fallback(engine, monkeypatch, value):
    requests, _ = install_http(monkeypatch)
    engine.vault.values['reference'] = value
    with pytest.raises(EngineAccessFailure) as failure: engine.providers.models('engine')
    assert failure.value.category == 'authentication' and failure.value.diagnostics()['not_executed']
    assert PRIVATE not in str(failure.value) and requests == []


def test_missing_vault_refuses_but_same_reference_rotation_is_read_fresh(engine, monkeypatch):
    requests, _ = install_http(monkeypatch)
    provider = engine.providers.provider('engine')
    provider.core.request('GET', '/ps')
    engine.vault.values['reference'] = 'rotated-synthetic-key'
    provider.core.request('GET', '/ps')
    assert [request.headers['authorization'] for request in requests] == ['Bearer ' + KEY, 'Bearer rotated-synthetic-key']
    assert len(engine.vault.reads) == 2
    engine.providers.vault = None
    engine.providers.invalidate('engine')
    with pytest.raises(EngineAccessFailure): engine.providers.models('engine')
    assert len(requests) == 2


@pytest.mark.parametrize('value',[None,'','non-ascii-\u00e9',RuntimeError(PRIVATE)])
def test_probe_missing_required_saved_key_never_tests_anonymously(engine,monkeypatch,value):
    requests,_=install_http(monkeypatch)
    engine.vault.values['reference']=value
    with pytest.raises(EngineAccessFailure) as failure:
        probe_provider(engine,{'id':'engine','draft':{'kind':'ollama','url':BASE}})
    assert failure.value.category=='authentication' and failure.value.not_executed
    assert requests==[] and PRIVATE not in str(failure.value)


def test_probe_unkeyed_profile_and_explicit_draft_key_remain_compatible(engine,monkeypatch):
    requests,_=install_http(monkeypatch)
    engine.store.profile.pop('credential_ref')
    assert probe_provider(engine,{'id':'engine','draft':{'kind':'ollama','url':BASE}})['ok']
    assert probe_provider(engine,{'draft':{'kind':'ollama','url':BASE},'api_key':KEY})['ok']
    assert 'authorization' not in requests[0].headers and requests[1].headers['authorization']=='Bearer '+KEY


@pytest.mark.parametrize('edit', ['endpoint', 'kind', 'key', 'disabled', 'deleted', 'endpoint_aba', 'key_aba', 'invalidated', 'stopped'])
def test_actual_queued_stream_refuses_revoked_or_aba_binding(engine, monkeypatch, edit):
    requests, _ = install_http(monkeypatch)
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with engine.providers.queue.lease(threading.Event()):
            future = executor.submit(lambda: list(engine.providers.generate(request_data(), cancel, {})))
            wait_for(lambda: bool(engine.providers.queue.pending))
            change(engine, edit)
        assert_refused(future, 'cancelled' if edit == 'stopped' else 'configuration')
    assert requests == [] and engine.vault.reads == []
    assert len(engine.store.usage) == 1
    assert engine.store.usage[0]['input_tokens'] == engine.store.usage[0]['output_tokens'] == 0
    assert not engine.providers.queue.active and not engine.providers.queue.pending


def change(engine, edit):
    if edit == 'endpoint': save(engine, url='https://other.invalid/api')
    elif edit == 'kind': save(engine, kind='openai-compatible')
    elif edit == 'key': save(engine, api_key='new-synthetic-key')
    elif edit == 'disabled': save(engine, enabled=False)
    elif edit == 'deleted': engine.store.profile = None; engine.providers.invalidate('engine')
    elif edit == 'endpoint_aba': save(engine, url='https://other.invalid/api'); save(engine, url=BASE)
    elif edit == 'key_aba':
        original = deepcopy(engine.store.profile)
        save(engine, api_key='new-synthetic-key')
        engine.store.profile = original; engine.providers.invalidate('engine')
    elif edit == 'invalidated': engine.providers.invalidate('engine')
    elif edit == 'stopped': engine.stop_event.set()


@pytest.mark.parametrize('edit', ['endpoint_aba', 'key_aba', 'disabled', 'stopped', 'cancelled'])
def test_real_stream_rechecks_after_held_vault_read(engine, monkeypatch, edit):
    requests, _ = install_http(monkeypatch)
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    engine.vault.hook = lambda: (entered.set(), release.wait(3))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: list(engine.providers.generate(request_data(), cancel, {})))
        try:
            assert entered.wait(3)
            if edit == 'cancelled': cancel.set()
            else: change(engine, edit)
        finally: release.set()
        if edit == 'cancelled':
            # Streaming Stop wins and ends the existing generator normally.
            assert future.result(timeout=3) == []
        else: assert_refused(future, 'cancelled' if edit == 'stopped' else 'configuration')
    assert requests == [] and len(engine.vault.reads) == 1


@pytest.mark.parametrize('path', ['sync', 'stream', 'residency'])
def test_actual_http_admission_after_client_construction_rechecks_binding(engine, monkeypatch, path):
    entered, release = threading.Event(), threading.Event()
    requests, _ = install_http(monkeypatch, constructor_hook=lambda: (entered.set(), release.wait(3)))
    provider = engine.providers.provider('engine')
    def perform():
        if path == 'sync': return provider.core.request('GET', '/ps')
        if path == 'stream': return list(provider.generate(request_data(), threading.Event()))
        url = provider.core.base + '/generate'
        return PerformanceManager._resident_request(url, {'model': 'fixture'}, threading.Event(),
            headers_factory=lambda: provider.core.request_headers(url), response_error=Core._response_json)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(perform)
        try: assert entered.wait(3); change(engine, 'endpoint_aba')
        finally: release.set()
        assert_refused(future)
    assert requests == [] and engine.vault.reads == []


@pytest.mark.parametrize('edit', ['invalidated', 'stopped'])
def test_last_profile_read_cannot_hide_generation_or_stop_change(engine, monkeypatch, edit):
    requests, _ = install_http(monkeypatch)
    provider = engine.providers.provider('engine')
    entered, release = threading.Event(), threading.Event()
    reads = 0
    def read():
        nonlocal reads
        reads += 1
        if reads == 2: entered.set(); assert release.wait(3)
    engine.store.read_hook = read
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider.core.request, 'GET', '/ps')
        try: assert entered.wait(3); change(engine, edit)
        finally: release.set()
        assert_refused(future, 'cancelled' if edit == 'stopped' else 'configuration')
    assert requests == [] and len(engine.vault.reads) == 1


@pytest.mark.parametrize('path', ['sync', 'stream'])
def test_mutable_core_base_never_routes_bound_key_elsewhere(engine, monkeypatch, path):
    requests, _ = install_http(monkeypatch)
    provider = engine.providers.provider('engine')
    engine.vault.hook = lambda: setattr(provider.core, 'base', 'https://other.invalid/api')
    if path == 'sync': provider.core.request('GET', '/ps')
    else: list(provider.generate(request_data(), threading.Event()))
    assert len(requests) == 1 and requests[0].url.host == 'engine.invalid'
    with pytest.raises(EngineAccessFailure): provider.core.request('GET', '/ps')
    assert len(requests) == 1


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 429, 500])
@pytest.mark.parametrize('path', ['sync', 'stream', 'residency'])
def test_http_redirect_and_failure_never_echo_key_or_claim_nonexecution(engine, monkeypatch, status, path):
    requests, _ = install_http(monkeypatch, handler=lambda request: httpx.Response(status,
        headers={'location': 'https://other.invalid/private'}, content=b'' if status<400 else (PRIVATE+KEY).encode()))
    provider=engine.providers.provider('engine')
    with pytest.raises(OllamaRequestFailure) as failure:
        if path=='sync':provider.core.dispatch('delete',{'model':'fixture'})
        elif path=='stream':list(provider.generate(request_data(),threading.Event()))
        else:
            url=provider.core.base+'/generate'
            PerformanceManager._resident_request(url,{'model':'fixture'},threading.Event(),
                headers_factory=lambda:provider.core.request_headers(url),response_error=Core._response_json)
    assert len(requests) == 1 and requests[0].url.host == 'engine.invalid'
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)
    assert failure.value.http_status==status
    assert 'not_executed' not in failure.value.diagnostics()


def test_empty_success_remains_valid_for_management(engine,monkeypatch):
    requests,_=install_http(monkeypatch,handler=lambda request:httpx.Response(200))
    assert engine.providers.provider('engine').core.dispatch('delete',{'model':'fixture'})=={}
    assert len(requests)==1


@pytest.mark.parametrize('invalid', ['exception','unicode','shape'])
def test_private_header_callback_failure_is_sanitized_before_http(monkeypatch,invalid):
    requests,_=install_http(monkeypatch)
    def headers(url,cancel):
        if invalid=='exception':raise RuntimeError(PRIVATE+KEY)
        return {'Authorization':'Bearer '+PRIVATE+'\u00e9'} if invalid=='unicode' else {'Authorization':{PRIVATE:KEY}}
    core=Core(base=BASE,request_headers=headers)
    with pytest.raises(EngineAccessFailure) as failure:core.request('GET','/ps')
    assert failure.value.category=='authentication' and requests==[]
    assert PRIVATE not in str(failure.value) and KEY not in str(failure.value)


def test_stale_authenticated_metadata_reply_cannot_restore_old_cache(engine,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    def handler(request):
        if len(engine.vault.reads)==1:entered.set();assert release.wait(3)
        return httpx.Response(200,json={'models':[{'name':'fixture'}]})
    requests,_=install_http(monkeypatch,handler=handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(engine.providers.models,'engine')
        try:assert entered.wait(3);change(engine,'key')
        finally:release.set()
        with pytest.raises(ValueError,match='configuration changed'):future.result(timeout=3)
    assert engine.providers.model_cache=={}
    assert engine.providers.models('engine')[0]['name']=='fixture'
    assert len(requests)==2 and requests[-1].headers['authorization']=='Bearer new-synthetic-key'


def test_final_calibration_facts_refuse_old_worker_instead_of_retargeting(engine,monkeypatch):
    requests,_=install_http(monkeypatch)
    provider=engine.providers.provider('engine')
    settings={**engine.store.get_settings(),'_admitted_provider':provider}
    change(engine,'endpoint')
    with pytest.raises(EngineAccessFailure):calibration_facts(engine.store,engine.providers,settings)
    assert requests==[] and engine.vault.reads==[]


def test_pinned_provider_lookup_failure_is_fixed_before_http(engine,monkeypatch):
    requests,_=install_http(monkeypatch)
    provider=engine.providers.provider('engine');engine.store.profile=None
    with pytest.raises(EngineAccessFailure) as failure:
        list(engine.providers.generate({**request_data(),'_admitted_provider':provider},threading.Event(),{}))
    assert requests==[] and failure.value.category=='configuration' and failure.value.not_executed
    assert PRIVATE not in str(failure.value)


def deferred_workers(monkeypatch):
    workers = []
    real = threading.Thread
    class Deferred:
        def __init__(self, **kwargs): self.target, self.args = kwargs.get('target'), kwargs.get('args', ())
        def start(self): pass
        def join(self, timeout=None): pass
    def factory(*args, **kwargs):
        if kwargs.get('name') == 'Forge-performance':
            worker = Deferred(**kwargs); workers.append(worker); return worker
        if kwargs.get('name') == 'Forge-performance-memory': return Deferred(**kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(threading, 'Thread', factory)
    monkeypatch.setattr(performance, 'hardware', lambda: pytest.fail('Auth fixture must not query hardware'))
    return workers


@pytest.mark.parametrize('operation', ['warm', 'release', 'benchmark', 'calibrate'])
def test_delayed_real_performance_worker_keeps_original_admitted_engine(engine, monkeypatch, operation):
    requests, _ = install_http(monkeypatch)
    workers = deferred_workers(monkeypatch)
    manager = PerformanceManager(engine)
    try:
        job = manager.start({}, operation)
        assert len(workers) == 1 and len(requests) == 1
        saved = json.dumps(engine.store.entities('benchmarks'))
        assert '_admitted_provider' not in saved and KEY not in saved
        requests.clear(); engine.vault.reads.clear()
        change(engine, 'endpoint')
        workers[0].target(*workers[0].args)
        finished = engine.store.entity('benchmarks', job['id'])
        assert finished['status'] == 'failed' and finished['stop_category'] == 'configuration'
        assert requests == [] and engine.vault.reads == [] and manager.jobs == {}
    finally: manager.shutdown()


@pytest.mark.parametrize('operation', ['warm', 'release'])
def test_actual_queued_residency_rechecks_original_binding(engine, monkeypatch, operation):
    requests, _ = install_http(monkeypatch)
    workers = deferred_workers(monkeypatch)
    manager = PerformanceManager(engine)
    try:
        job = manager.start({}, operation)
        requests.clear(); engine.vault.reads.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with engine.providers.queue.lease(threading.Event()):
                future = executor.submit(workers[0].target, *workers[0].args)
                wait_for(lambda: bool(engine.providers.queue.pending))
                change(engine, 'endpoint_aba')
            future.result(timeout=3)
        assert engine.store.entity('benchmarks', job['id'])['stop_category'] == 'configuration'
        assert requests == [] and engine.vault.reads == [] and len(engine.store.usage) == 1
    finally: manager.shutdown()


@pytest.mark.parametrize('operation', ['warm', 'release'])
def test_authenticated_performance_worker_completes_without_persisting_private_snapshot(engine, monkeypatch, operation):
    requests, _ = install_http(monkeypatch)
    workers = deferred_workers(monkeypatch)
    manager = PerformanceManager(engine)
    try:
        job = manager.start({}, operation); workers[0].target(*workers[0].args)
        assert engine.store.entity('benchmarks', job['id'])['status'] == 'completed'
        assert len(requests) == 2 and all(request.headers['authorization'] == 'Bearer ' + KEY for request in requests)
        assert len(engine.store.usage) == 1 and '_admitted_provider' not in requests[-1].content.decode()
    finally: manager.shutdown()
