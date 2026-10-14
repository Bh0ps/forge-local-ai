"""Textgen connection suggestions use disposable state and offline HTTP only."""
import json
import threading

import httpx
import pytest

from forge_engine_auth import EngineAccessFailure
from forge_inference import CompatibleProvider, ProviderStreamFailure
from forge_provider_presets import provider_preset
from forge_store import ForgeStore
from test_forge_engine_run_binding import fixture, terminal, EVIDENCE
from test_forge_ollama_auth import KEY, install_http


BASE = 'https://textgen.invalid/proxy/v1'
PRIVATE = 'synthetic-private-model-and-response'


@pytest.fixture
def textgen(fixture):
    svc, project = fixture
    svc.dispatch('provider_save', {'id': 'engine', 'name': 'Synthetic Textgen',
        'kind': 'openai-compatible', 'preset_id': 'textgen', 'url': BASE,
        'api_key': KEY, 'enabled': True, 'context_limit': 32768,
        # Explicit fixture configuration, never inferred from the engine name.
        'capabilities': ['tools']})
    return svc, project


def catalog(model='fixture'):
    return httpx.Response(200, json={'data': [] if model is None else [{'id': model}]})


def response(*, model='fixture', tool=False, complete=True, private=False):
    delta = {'content': '' if tool else 'Synthetic answer.'}
    if tool:
        delta['tool_calls'] = [{'index': 0, 'id': 'synthetic_call', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'evidence.txt'})}}]
    if private:
        delta.update(content=PRIVATE, reasoning_content=PRIVATE)
    packet = {'choices': [{'delta': delta}]}
    if model is not None:
        packet['model'] = model
    packets = [packet]
    if complete:
        final = {'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 4}}
        if model is not None:
            final['model'] = model
        packets.append(final)
    content = ''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets)
    return httpx.Response(200, content=(content + 'data: [DONE]\n\n').encode(),
        headers={'content-type': 'text/event-stream'})


def invocations(svc, identifier):
    with svc.store._connection() as db:
        return [dict(row) for row in db.execute('SELECT name,status,result FROM invocations WHERE run_id=?', (identifier,))]


def start(svc, project):
    return svc.jobs.start({'text': 'Read evidence.txt and report it.', 'project_id': project['id']})


def test_preset_is_editable_and_save_does_not_grant_capabilities_or_infer(textgen, monkeypatch):
    svc, _ = textgen
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Saving a preset performed HTTP'))
    preset = provider_preset('textgen')
    assert preset['url'] == 'http://127.0.0.1:5000/v1'
    saved = svc.dispatch('provider_save', {'name': 'Custom Textgen', 'kind': preset['kind'],
        'preset_id': 'textgen', 'url': BASE})
    assert saved['url'] == BASE and saved['preset_id'] == 'textgen'
    assert saved.get('capabilities') is None and saved.get('context_limit') is None
    assert requests == []
    reopened = ForgeStore(svc.store.home)
    assert reopened.entity('providers', saved['id']) == saved


@pytest.mark.parametrize('loaded', [None, 'fixture'])
def test_probe_reads_only_loaded_catalog_and_never_invents_native_metadata(textgen, monkeypatch, loaded):
    svc, _ = textgen
    before = svc.store.entity('providers', 'engine')
    requests, _ = install_http(monkeypatch, lambda request: catalog(loaded))
    result = svc.dispatch('provider_test', {'id': 'engine', 'metadata': True,
        'draft': {'kind': 'openai-compatible', 'url': BASE}})
    assert result == {'ok': True, 'model_count': int(loaded is not None),
        'metadata': {'state': 'unsupported', 'source': 'unknown', 'models': []}}
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == BASE + '/models'
    assert requests[0].headers['authorization'] == 'Bearer ' + KEY
    assert svc.store.entity('providers', 'engine') == before


@pytest.mark.parametrize('entries', [
    [{'id': 'fixture'}, {'id': 'other'}], [{'id': 'fixture'}, {'id': 'fixture'}],
    [{'id': ''}], [{'id': False}], [{'id': 'unsafe\nmodel'}],
], ids=['multiple-loaded', 'duplicate', 'empty-id', 'boolean-id', 'control-id'])
def test_ambiguous_or_invalid_loaded_catalog_is_refused_before_inference(textgen, monkeypatch, entries):
    svc, project = textgen
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(200, json={'data': entries}))
    run = start(svc, project)
    saved = terminal(svc, run['id'])
    assert saved['status'] == 'paused'
    assert all(request.method == 'GET' for request in requests)
    assert invocations(svc, run['id']) == []


@pytest.mark.parametrize('loaded', [None, 'other', 'Fixture'], ids=['unloaded', 'different', 'case-alias'])
def test_each_admitted_request_requires_exact_loaded_model_before_post(textgen, monkeypatch, loaded):
    svc, project = textgen
    requests, _ = install_http(monkeypatch, lambda request: catalog(loaded))
    run = start(svc, project)
    saved = terminal(svc, run['id'])
    assert saved['status'] == 'paused'
    assert len(requests) == 1 and requests[0].url.path.endswith('/models')
    assert invocations(svc, run['id']) == []


def test_actual_two_round_worker_refreshes_loaded_model_and_executes_tool_once(textgen, monkeypatch):
    svc, project = textgen
    posts = []
    def handle(request):
        if request.method == 'GET':
            return catalog()
        assert str(request.url) == BASE + '/chat/completions'
        body = json.loads(request.content)
        posts.append(body)
        assert body['model'] == 'fixture' and body['stream'] is True
        assert body['stream_options'] == {'include_usage': True}
        assert 'num_thread' not in body and 'keep_alive' not in body
        return response(tool=len(posts) == 1)
    requests, _ = install_http(monkeypatch, handle)
    run = start(svc, project)
    saved = terminal(svc, run['id'])
    assert saved['status'] == 'completed' and len(posts) == 2
    assert [request.method for request in requests] == ['GET', 'POST', 'GET', 'POST']
    rows = invocations(svc, run['id'])
    assert len(rows) == 1 and rows[0]['name'] == 'read_file'
    assert rows[0]['status'] == 'completed'
    tools = [message for message in posts[1]['messages'] if message['role'] == 'tool']
    assert len(tools) == 1 and tools[0]['tool_call_id'] == 'synthetic_call'
    assert EVIDENCE in tools[0]['content']


@pytest.mark.parametrize('reported', [None, PRIVATE], ids=['missing-model', 'different-model'])
def test_returned_replacement_model_cannot_publish_or_execute_tools(textgen, monkeypatch, reported):
    svc, project = textgen
    def handle(request):
        return catalog() if request.method == 'GET' else response(model=reported, tool=True, private=True)
    requests, _ = install_http(monkeypatch, handle)
    run = start(svc, project)
    saved = terminal(svc, run['id'])
    assert saved['status'] == 'paused' and len(requests) == 2
    assert invocations(svc, run['id']) == []
    public = json.dumps(svc.dispatch('poll', {'id': run['id']}))
    assert PRIVATE not in public and 'selected model identity' in public


def test_later_identity_change_cannot_publish_wrong_content_reasoning_or_tools(textgen, monkeypatch):
    svc, project = textgen
    def handle(request):
        if request.method == 'GET':
            return catalog()
        valid = 'data: ' + json.dumps({'model': 'fixture', 'choices': [{'delta': {}}]}) + '\n\n'
        invalid = response(model=PRIVATE, tool=True, private=True)
        return httpx.Response(200, content=valid.encode() + invalid.content,
            headers={'content-type': 'text/event-stream'})
    install_http(monkeypatch, handle)
    run = start(svc, project)
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert invocations(svc, run['id']) == []
    assert PRIVATE not in json.dumps(svc.dispatch('poll', {'id': run['id']}))


@pytest.mark.parametrize('status', [401, 403])
def test_catalog_authentication_failure_stays_specific_and_content_free(monkeypatch, status):
    requests, _ = install_http(monkeypatch, lambda request: httpx.Response(status, text=PRIVATE))
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'textgen'})
    with pytest.raises(ProviderStreamFailure) as captured:
        list(provider.generate({'model': 'fixture', 'context': 8192, 'tokens': 1024,
            'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}, threading.Event()))
    assert captured.value.category == 'authentication' and captured.value.http_status == status
    assert PRIVATE not in str(captured.value) and len(requests) == 1


@pytest.mark.parametrize('code', [401, 403, 429, 500])
def test_model_less_error_packet_retains_specific_status_without_publishing(monkeypatch, code):
    def handle(request):
        if request.method == 'GET':
            return catalog()
        packet = {'error': {'code': code, 'message': PRIVATE}}
        return httpx.Response(200, content=('data: ' + json.dumps(packet) + '\n\n').encode())
    requests, _ = install_http(monkeypatch, handle)
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'textgen'})
    published = []
    with pytest.raises(ProviderStreamFailure) as captured:
        for packet in provider.generate({'model': 'fixture', 'context': 8192, 'tokens': 1024,
                'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}, threading.Event()):
            published.append(packet)
    assert captured.value.category == 'provider_error' and captured.value.http_status == code
    assert PRIVATE not in str(captured.value) and published == []
    assert [request.method for request in requests] == ['GET', 'POST']


def test_incomplete_stream_keeps_tool_unexecuted(textgen, monkeypatch):
    svc, project = textgen
    install_http(monkeypatch, lambda request: catalog() if request.method == 'GET' else response(tool=True, complete=False))
    run = start(svc, project)
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert invocations(svc, run['id']) == []


def test_loaded_model_change_between_rounds_keeps_completed_tool_without_replay(textgen, monkeypatch):
    svc, project = textgen
    loaded = ['fixture']; original = svc.jobs.registry.execute
    def execute(run, name, args, cancel, **kwargs):
        result = original(run, name, args, cancel, **kwargs)
        if name == 'read_file':
            loaded[0] = 'replacement'
        return result
    monkeypatch.setattr(svc.jobs.registry, 'execute', execute)
    requests, _ = install_http(monkeypatch, lambda request: catalog(loaded[0]) if request.method == 'GET' else response(tool=True))
    run = start(svc, project)
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert [request.method for request in requests] == ['GET', 'POST', 'GET']
    rows = invocations(svc, run['id'])
    assert len(rows) == 1 and rows[0]['status'] == 'completed'
    assert EVIDENCE in json.dumps(rows[0]['result'])


@pytest.mark.parametrize('change', ['key', 'endpoint', 'endpoint_aba', 'disable', 'delete'])
def test_catalog_io_cannot_authorize_a_replaced_or_disabled_saved_profile(textgen, monkeypatch, change):
    svc, project = textgen
    def handle(request):
        assert request.method == 'GET'
        if change == 'key':
            svc.dispatch('provider_save', {'id': 'engine', 'api_key': 'replacement-synthetic-key'})
        elif change == 'endpoint':
            svc.dispatch('provider_save', {'id': 'engine', 'url': 'https://replacement.invalid/v1'})
        elif change == 'endpoint_aba':
            svc.dispatch('provider_save', {'id': 'engine', 'url': 'https://replacement.invalid/v1'})
            svc.dispatch('provider_save', {'id': 'engine', 'url': BASE, 'api_key': KEY})
        elif change == 'disable':
            svc.dispatch('provider_save', {'id': 'engine', 'enabled': False})
        else:
            svc.store.delete_entity('providers', 'engine')
        return catalog()
    requests, _ = install_http(monkeypatch, handle)
    run = start(svc, project)
    assert terminal(svc, run['id'])['status'] == 'paused'
    assert len(requests) == 1 and invocations(svc, run['id']) == []


def test_cancel_during_catalog_retains_cancel_category_and_never_posts(monkeypatch):
    cancel = threading.Event()
    def handle(request):
        cancel.set()
        return catalog()
    requests, _ = install_http(monkeypatch, handle)
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE, 'preset_id': 'textgen'})
    with pytest.raises(EngineAccessFailure) as captured:
        list(provider.generate({'model': 'fixture', 'context': 8192, 'tokens': 1024,
            'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}, cancel))
    assert captured.value.category == 'cancelled'
    assert len(requests) == 1 and requests[0].method == 'GET'


def test_missing_saved_key_is_refused_before_catalog_http(textgen, monkeypatch):
    svc, _ = textgen
    config = svc.store.entity('providers', 'engine')
    svc.vault.values[config['credential_ref']] = None
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail('Missing key sent HTTP'))
    provider = svc.providers.provider('engine')
    with pytest.raises(EngineAccessFailure) as captured:
        list(provider.generate({'model': 'fixture', 'context': 8192, 'tokens': 1024,
            'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}, threading.Event()))
    assert captured.value.category == 'authentication' and requests == []


def test_generic_provider_does_not_gain_textgen_preflight_or_identity_rules(monkeypatch):
    requests, _ = install_http(monkeypatch, lambda request: response(model='generic-reported-model'))
    provider = CompatibleProvider({'kind': 'openai-compatible', 'url': BASE})
    packets = list(provider.generate({'model': 'generic-requested-model', 'context': 8192,
        'tokens': 1024, 'thinking': False, 'messages': [{'role': 'user', 'content': 'Synthetic task'}]}, threading.Event()))
    assert len(requests) == 1 and requests[0].method == 'POST'
    assert packets[-1]['provider_model'] == 'generic-reported-model'
