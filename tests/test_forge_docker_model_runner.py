"""Docker setup and inference use disposable state and offline HTTP only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import httpx
import pytest

from forge_engine_auth import EngineAccessFailure
from forge_inference import CompatibleProvider, ProviderStreamFailure
from forge_provider_metadata import (DockerModelRunnerMetadataInvalid, MAX_JSON_BYTES,
    MAX_METADATA_MODELS, docker_model_runner_catalog, fetch_provider_metadata)
from forge_provider_presets import provider_preset
from forge_service import ForgeService
from forge_store import ForgeStore
from test_forge_core import Engine
from test_forge_engine_run_binding import fixture, terminal, EVIDENCE
from test_forge_ollama_auth import KEY, install_http, wait_for
from test_forge_textgen import response, invocations


BASE = 'http://127.0.0.1:12434/engines/llama.cpp/v1'
MODEL = 'ai/fixture:Q4_K_M'
FILENAME = 'fixture-Q4_K_M.gguf'
PRIVATE = 'synthetic-private-response-and-credential'


def entry(model=MODEL, **changes):
    return {'id': model, 'object': 'model', 'created': 0, 'owned_by': 'docker',
        'dmr': {'context_window': 131072, 'architecture': 'fixture', 'parameters': '3B',
            'quantization': 'Q4_K_M', 'size': '2 GB'}, **changes}


def catalog(entries=None):
    return {'object': 'list', 'data': [entry()] if entries is None else entries}


def answer(**kwargs):
    return response(model=FILENAME, **kwargs)


def data():
    return {'model': MODEL, 'context': 8192, 'tokens': 1024, 'thinking': True,
        'num_thread': 4, 'keep_alive': '5m', 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}


@pytest.fixture
def dmr(fixture):
    svc, project = fixture
    svc.dispatch('provider_save', {'id': 'engine', 'name': 'Synthetic Docker',
        'kind': 'openai-compatible', 'preset_id': 'docker-model-runner', 'url': BASE,
        'api_key': KEY, 'enabled': True, 'context_limit': 32768, 'capabilities': ['tools']})
    svc.store.update_settings({'model': MODEL})
    return svc, project


def test_preset_and_actual_save_are_editable_without_automatic_engine_actions(dmr, monkeypatch):
    svc, _ = dmr
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Save performed HTTP'))
    preset = provider_preset('docker-model-runner')
    assert preset['url'] == BASE and preset['metadata_strategy'] == 'dmr'
    saved = svc.dispatch('provider_save', {'name': 'Edited Docker connection', 'kind': preset['kind'],
        'preset_id': preset['id'], 'url': 'http://127.0.0.1:23456/engines/llama.cpp/v1'})
    assert saved['url'].startswith('http://127.0.0.1:23456/')
    assert saved.get('capabilities') is None and saved.get('context_limit') is None
    assert requests == [] and ForgeStore(svc.store.home).entity('providers', saved['id']) == saved


def test_probe_keeps_package_hints_unknown_and_user_preferences_unchanged(dmr, monkeypatch):
    svc, _ = dmr
    before = svc.store.entity('providers', 'engine'); settings = svc.store.get_settings()
    requests, policies = install_http(monkeypatch, lambda request: httpx.Response(200, json=catalog()))
    result = svc.dispatch('provider_test', {'id': 'engine', 'metadata': True,
        'draft': {'kind': 'openai-compatible', 'url': BASE}})
    assert result == {'ok': True, 'model_count': 1, 'metadata': {
        'state': 'available', 'source': 'advertised', 'models': [{
            'model': MODEL, 'max_context_length': None, 'loaded_context_lengths': [],
            'loaded_instances': [], 'vision': None, 'trained_for_tool_use': None, 'reasoning_options': []}]}}
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == BASE + '/models'
    assert requests[0].headers['authorization'] == 'Bearer ' + KEY
    assert all(policy['trust_env'] is policy['follow_redirects'] is False for policy in policies)
    assert svc.store.entity('providers', 'engine') == before and svc.store.get_settings() == settings


@pytest.mark.parametrize('url', [
    'https://foreign.invalid/engines/llama.cpp/v1', 'http://127.0.0.1:12434/engines/v1',
    'http://127.0.0.1:12434/engines/vllm/v1', 'http://127.0.0.1:12434/proxy/engines/llama.cpp/v1',
    'http://127.0.0.1.foreign.invalid:12434/engines/llama.cpp/v1',
], ids=['foreign-host', 'multiplexing-route', 'other-engine', 'proxy-route', 'lookalike-host'])
def test_edited_endpoint_cannot_manufacture_local_scope(dmr, monkeypatch, url):
    svc, _ = dmr
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Invalid local scope sent HTTP'))
    saved = svc.dispatch('provider_save', {'id': 'engine', 'url': url})
    assert saved['url'] == url  # Never replace the user's entered endpoint.
    with pytest.raises(DockerModelRunnerMetadataInvalid):
        svc.dispatch('provider_test', {'id': 'engine', 'draft': {'kind': 'openai-compatible', 'url': url}})
    provider = svc.providers.provider('engine')
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate(data(), threading.Event()))
    assert captured.value.category == 'metadata' and requests == []


@pytest.mark.parametrize('url', ['http://localhost:23456/engines/llama.cpp/v1',
    'http://[::1]:23456/engines/llama.cpp/v1'])
def test_explicit_other_loopback_ports_keep_exact_configured_route(monkeypatch, url):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, json=catalog()))
    result = fetch_provider_metadata({'kind': 'openai-compatible', 'preset_id': 'docker-model-runner', 'url': url})
    assert result['models'][0]['model'] == MODEL
    assert len(requests) == 1 and str(requests[0].url) == url + '/models'


BAD_CATALOGS = [
    {'object': 'foreign', 'data': [entry()]}, {'object': 'list', 'data': {}},
    catalog([entry(), entry()]), catalog([entry(model='')]), catalog([entry(model=False)]),
    catalog([entry(model='unsafe\nmodel')]), catalog([entry(model='unsafe\x7fmodel')]),
    catalog([entry(owned_by='openrouter')]), catalog([entry(object='cloud_model')]),
    catalog([entry(recipe='cloud', url='https://foreign.invalid')]),
    catalog([entry(created=True)]), catalog([entry(dmr={'context_window': True})]),
    catalog([entry(dmr={'context_window': '8192'})]), catalog([entry(dmr={'context_window': -2**31-1})]),
    catalog([entry(dmr={'context_window': 2**31})]), catalog([entry(dmr={'architecture': ['cloud']})]),
    catalog([entry(dmr={'quantization': 'private\nvalue'})]), catalog([entry(dmr={'url': 'https://foreign.invalid'})]),
    catalog([entry(dmr=None)]), catalog([entry(model='m' * 513)]),
    catalog([entry(model='fixture-' + str(index)) for index in range(MAX_METADATA_MODELS + 1)]),
]


@pytest.mark.parametrize('value', BAD_CATALOGS, ids=[
    'top-object', 'not-list', 'duplicate', 'empty-id', 'boolean-id', 'control-id', 'del-id',
    'foreign-owner', 'cloud-object', 'cloud-descriptor', 'boolean-date', 'boolean-context',
    'string-context', 'below-int32-context', 'large-context', 'non-string-hint', 'control-hint',
    'routing-hint', 'null-hints', 'long-id', 'too-many-models'])
def test_bad_or_foreign_catalog_has_fixed_metadata_recovery_and_never_posts(monkeypatch, value):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, json=value))
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'docker-model-runner'})
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate(data(), threading.Event()))
    assert captured.value.category == 'metadata'
    assert 'local catalog' in str(captured.value) and 'foreign.invalid' not in str(captured.value)
    assert len(requests) == 1 and requests[0].method == 'GET'


@pytest.mark.parametrize('body', [
    b'{"object":"list","data":[],"data":[]}',
    b'{"object":"list","data":[{"id":"x","object":"model","created":NaN,"owned_by":"docker"}]}',
    b' ' * (MAX_JSON_BYTES + 1),
], ids=['duplicate-json-key', 'non-finite-json', 'byte-limit'])
def test_unparseable_or_oversized_wire_is_refused_before_post(monkeypatch, body):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, content=body))
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'docker-model-runner'})
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate(data(), threading.Event()))
    assert captured.value.category == 'metadata' and len(requests) == 1


@pytest.mark.parametrize('context', [-1, 0, 8192])
def test_valid_package_context_never_becomes_serving_context(context):
    assert docker_model_runner_catalog(catalog([entry(dmr={'context_window': context})])) == [{'id': MODEL}]


@pytest.mark.parametrize('model', [None, 'ai/other:Q4', MODEL.upper()])
def test_actual_worker_requires_selected_exact_local_catalog_entry(dmr, monkeypatch, model):
    svc, project = dmr
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200,
        json=catalog([] if model is None else [entry(model)])))
    run = svc.jobs.start({'text': 'Read evidence.txt.', 'project_id': project['id']})
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert requests and all(request.method == 'GET' for request in requests)
    assert invocations(svc, run['id']) == []
    public = json.dumps(svc.dispatch('poll', {'id': run['id']}))
    assert 'local catalog' in public


def test_actual_two_round_worker_accepts_distinct_filename_without_alias_or_new_permissions(dmr, monkeypatch):
    svc, project = dmr
    before = svc.store.get_settings(); posts = []
    def handle(request):
        if request.method == 'GET': return httpx.Response(200, json=catalog())
        body = json.loads(request.content); posts.append(body)
        assert body['model'] == MODEL
        assert set(body) <= {'model', 'messages', 'stream', 'stream_options', 'max_tokens', 'temperature', 'tools'}
        assert not set(body) & {'reasoning', 'reasoning_effort', 'think', 'enable_thinking', 'options',
            'num_ctx', 'context_size', 'num_thread', 'keep_alive', 'agent', 'mcp', 'tool_choice'}
        return answer(tool=len(posts) == 1)
    requests, _ = install_http(monkeypatch, handle)
    run = svc.jobs.start({'text': 'Read evidence.txt and report it.', 'project_id': project['id']})
    saved = terminal(svc, run['id'])
    assert saved['status'] == 'completed' and len(posts) == 2
    assert all(str(request.url) in (BASE + '/models', BASE + '/chat/completions') for request in requests)
    rows = invocations(svc, run['id'])
    assert len(rows) == 1 and rows[0]['name'] == 'read_file' and rows[0]['status'] == 'completed'
    assert EVIDENCE in json.dumps(posts[1]['messages'])
    assert svc.store.get_settings() == before


def test_direct_stream_keeps_requested_tag_and_reported_filename_separate(monkeypatch):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, json=catalog())
        if request.method == 'GET' else answer())
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'docker-model-runner'})
    packets = list(provider.generate(data(), threading.Event()))
    assert packets[-1]['provider_model'] == FILENAME
    assert json.loads(requests[-1].content)['model'] == MODEL
    assert not set(json.loads(requests[-1].content)) & {'think', 'reasoning', 'reasoning_effort',
        'num_ctx', 'context_size', 'num_thread', 'keep_alive'}
    assert packets[-1]['done'] is True


def test_changed_stream_identity_keeps_existing_refusal_and_never_executes_tools(dmr, monkeypatch):
    svc, project = dmr
    def handle(request):
        if request.method == 'GET': return httpx.Response(200, json=catalog())
        prefix = ('data: ' + json.dumps({'model': FILENAME, 'choices': [{'delta': {}}]}) + '\n\n').encode()
        return httpx.Response(200, content=prefix + response(model=PRIVATE, tool=True, private=True).content)
    install_http(monkeypatch, handle)
    run = svc.jobs.start({'text': 'Read evidence.txt.', 'project_id': project['id']})
    assert terminal(svc, run['id'])['status'] == 'paused' and invocations(svc, run['id']) == []
    assert PRIVATE not in json.dumps(svc.dispatch('poll', {'id': run['id']}))


def test_catalog_refresh_after_completed_tool_and_restart_preserves_evidence_without_replay(dmr, monkeypatch):
    svc, project = dmr
    models = [entry()]; posts = []; reads = []
    execute = svc.jobs.registry.execute
    def once(run, name, args, cancel, **kwargs):
        result = execute(run, name, args, cancel, **kwargs)
        if name == 'read_file': reads.append(name); models.clear()
        return result
    monkeypatch.setattr(svc.jobs.registry, 'execute', once)
    def handle(request):
        if request.method == 'GET': return httpx.Response(200, json=catalog(deepcopy(models)))
        posts.append(request); return answer(tool=len(posts) == 1)
    install_http(monkeypatch, handle)
    run = svc.jobs.start({'text': 'Read evidence.txt and report it.', 'project_id': project['id']})
    assert terminal(svc, run['id'])['status'] == 'paused' and len(posts) == 1 and reads == ['read_file']
    rows = invocations(svc, run['id']); assert len(rows) == 1 and EVIDENCE in rows[0]['result']
    home = svc.store.home; saved_keys = deepcopy(svc.vault.values)
    svc.shutdown(); restarted = ForgeService(core=Engine(), data_dir=home)
    restarted.vault.values.update(saved_keys)  # The disposable vault survives this simulated restart.
    try:
        restarted_execute = restarted.jobs.registry.execute
        def no_replay(run, name, args, cancel, **kwargs):
            if name == 'read_file': pytest.fail('Restart replayed completed tool')
            return restarted_execute(run, name, args, cancel, **kwargs)
        monkeypatch.setattr(restarted.jobs.registry, 'execute', no_replay)
        restarted.jobs.resume(run['id'])
        assert terminal(restarted, run['id'])['status'] == 'paused' and len(posts) == 1
        models.append(entry()); restarted.jobs.resume(run['id'])
        saved = terminal(restarted, run['id'])
        assert saved['status'] == 'completed' and len(posts) == 2, (saved.get('recovery'), len(posts))
        assert EVIDENCE in posts[-1].content.decode() and invocations(restarted, run['id']) == rows
    finally:
        restarted.shutdown()


@pytest.mark.parametrize('change', ['key', 'endpoint', 'disable', 'preset', 'cancel'])
def test_actual_catalog_io_cannot_authorize_changed_profile_or_cancelled_work(dmr, monkeypatch, change):
    svc, project = dmr
    def handle(request):
        assert request.method == 'GET'
        if change == 'key': svc.dispatch('provider_save', {'id': 'engine', 'api_key': 'replacement-key'})
        elif change == 'endpoint': svc.dispatch('provider_save', {'id': 'engine', 'url': 'http://127.0.0.1:23456/engines/llama.cpp/v1'})
        elif change == 'disable': svc.dispatch('provider_save', {'id': 'engine', 'enabled': False})
        elif change == 'preset': svc.dispatch('provider_save', {'id': 'engine', 'preset_id': 'llama-cpp'})
        else:
            for identifier in tuple(svc.jobs.jobs): svc.jobs.cancel(identifier, pause=True)
        return httpx.Response(200, json=catalog())
    requests, _ = install_http(monkeypatch, handle)
    run = svc.jobs.start({'text': 'Read evidence.txt.', 'project_id': project['id']})
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert requests and all(request.method == 'GET' for request in requests)
    assert invocations(svc, run['id']) == []


def test_declared_missing_key_refuses_before_any_dmr_request(dmr, monkeypatch):
    svc, _ = dmr
    profile = svc.store.entity('providers', 'engine'); svc.vault.values[profile['credential_ref']] = None
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Missing key sent HTTP'))
    with pytest.raises(EngineAccessFailure) as captured:
        list(svc.providers.provider('engine').generate(data(), threading.Event()))
    assert captured.value.category == 'authentication' and requests == []


@pytest.mark.parametrize('change', ['endpoint', 'key', 'disable'])
def test_actual_held_inference_queue_rejects_revoked_dmr_before_catalog(dmr, monkeypatch, change):
    svc, _ = dmr
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Revoked queued request sent HTTP'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with svc.providers.queue.lease(threading.Event()):
            pending = executor.submit(lambda:list(svc.providers.generate(
                {**data(), 'provider_id':'engine'}, threading.Event(), {})))
            wait_for(lambda:bool(svc.providers.queue.pending))
            changes = ({'url':'http://127.0.0.1:23456/engines/llama.cpp/v1'} if change=='endpoint' else
                {'api_key':'replacement-key'} if change=='key' else {'enabled':False})
            svc.dispatch('provider_save', {'id':'engine', **changes})
        with pytest.raises(EngineAccessFailure) as captured:
            pending.result(timeout=3)
    assert captured.value.not_executed is True and requests == []


def test_probe_and_discovery_refuse_cloud_descriptor_without_granting_or_saving_facts(dmr, monkeypatch):
    svc, _ = dmr; before = svc.store.entity('providers', 'engine')
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200,
        json=catalog([entry(recipe='cloud', url='https://foreign.invalid')])))
    with pytest.raises(DockerModelRunnerMetadataInvalid):
        svc.dispatch('provider_test', {'id':'engine', 'metadata':True,
            'draft':{'kind':'openai-compatible','url':BASE}})
    with pytest.raises(ProviderStreamFailure) as captured:
        svc.providers.provider('engine').models()
    assert captured.value.category == 'metadata' and all(request.method=='GET' for request in requests)
    assert svc.store.entity('providers','engine') == before


def test_catalog_redirect_is_not_followed_and_cannot_authorize_a_post(monkeypatch):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(307,
        headers={'location':'https://foreign.invalid/private'}, text=PRIVATE))
    provider = CompatibleProvider({'kind':'openai-compatible','url':BASE,'preset_id':'docker-model-runner'})
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate(data(),threading.Event()))
    assert captured.value.category=='metadata' and captured.value.http_status==307
    assert PRIVATE not in str(captured.value) and len(requests)==1


@pytest.mark.parametrize('status', [401, 403, 429])
def test_catalog_failure_retains_specific_status_without_response_content(monkeypatch, status):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(status, text=PRIVATE))
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'docker-model-runner'})
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate(data(), threading.Event()))
    assert captured.value.category == ('authentication' if status in (401, 403) else 'metadata')
    assert captured.value.http_status == status and PRIVATE not in str(captured.value) and len(requests) == 1


@pytest.mark.parametrize('failure', [401, 403, 'transport', 'constructor'])
def test_actual_show_sanitizes_metadata_failure_and_preserves_authentication_status(dmr, monkeypatch, failure):
    svc, _ = dmr
    def handle(request):
        if failure == 'transport': raise httpx.ConnectError(PRIVATE, request=request)
        return httpx.Response(failure, text=PRIVATE)
    def constructor():
        if failure == 'constructor': raise RuntimeError(PRIVATE)
    requests, _ = install_http(monkeypatch, handle, constructor_hook=constructor)
    with pytest.raises(ProviderStreamFailure) as captured:
        svc.dispatch('show', {'provider_id':'engine','model':MODEL})
    assert captured.value.category == ('authentication' if failure in (401,403) else 'metadata')
    assert captured.value.http_status == (failure if type(failure) is int else None)
    assert PRIVATE not in str(captured.value) and len(requests) == (0 if failure == 'constructor' else 1)


@pytest.mark.parametrize('failure', ['constructor', 'nested-json'])
def test_actual_discovery_refuses_unexpected_transport_and_deep_json_as_metadata(dmr, monkeypatch, failure):
    svc, _ = dmr
    def constructor():
        if failure == 'constructor': raise RuntimeError(PRIVATE)
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200,
        content=b'{"object":"list","data":' + b'['*2000 + b']'*2000 + b'}'),
        constructor_hook=constructor)
    with pytest.raises(ProviderStreamFailure) as captured:
        svc.dispatch('provider_test', {'id':'engine'})
    assert captured.value.category=='metadata' and PRIVATE not in str(captured.value)
    assert len(requests)==(0 if failure=='constructor' else 1)


def test_dmr_package_does_not_enable_residency_tuning_or_automatic_profile_application(dmr, monkeypatch):
    from forge_performance import PerformanceManager, calibration_facts, adaptive_settings
    svc, _ = dmr; settings = svc.store.get_settings()
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, json=catalog())
        if request.method == 'GET' and str(request.url) == BASE + '/models' else
        pytest.fail('Unsupported control sent a management or inference request'))
    monkeypatch.setattr('forge_performance.hardware', lambda *args, **kwargs: pytest.fail('Queried real hardware'))
    manager = PerformanceManager(svc)
    for operation in ('warm', 'release', 'tune'):
        with pytest.raises(ValueError): manager.start({}, operation=operation)
    with pytest.raises(ValueError, match='unsupported_engine'):
        calibration_facts(svc.store, svc.providers, settings, telemetry={})
    assert adaptive_settings(svc.store, {}, settings, svc.providers) == settings
    assert all(request.method == 'GET' for request in requests) and svc.store.get_settings() == settings
