"""OpenRouter fixtures never authenticate a real key or perform cloud inference."""
import asyncio
import json
import threading
import time
from unittest.mock import Mock
from types import MethodType

import httpx
import pytest

from forge_openrouter import (FREE_ROUTER, OPENROUTER_URL, OpenRouterConnection,
                              OpenRouterProvider, free_catalog, zero_pricing)
from test_forge_channels import env
from test_inference_timeouts import TimedBody, transport
from tool_calls import ToolCallAccumulator
from forge_runs import RunManager
from test_forge_workspaces import wait_finished


ZERO = {'prompt': '0', 'completion': '0', 'request': '0', 'image': '0', 'web_search': '0',
        'internal_reasoning': '0', 'input_cache_read': '0', 'input_cache_write': '0'}


def model(name=FREE_ROUTER, pricing=None, capabilities=True):
    return {'id': name, 'name': 'Fixture free model', 'pricing': pricing or ZERO.copy(), 'context_length': 131072,
            'architecture': {'input_modalities': ['text', 'image'] if capabilities else ['text'], 'output_modalities': ['text']},
            'supported_parameters': ['tools', 'max_tokens', 'temperature'] if capabilities else ['max_tokens', 'temperature']}


class MetadataFixture:
    def __init__(self):
        self.calls = []
        self.models = [model(), model('fixture/coder:free'), model('fixture/paid', {**ZERO, 'prompt': '0.000001'})]
        self.error = None

    def __call__(self, request):
        self.calls.append(request)
        assert str(request.url).startswith(OPENROUTER_URL)
        if self.error:
            return httpx.Response(self.error, json={'error': {'message': 'private account label and synthetic_api_key_should_not_leak'}})
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': self.models})
        if request.url.path.endswith('/key'):
            return httpx.Response(200, json={'data': {'label': 'private@example.invalid', 'usage': 123, 'limit_remaining': 456,
                  'is_free_tier': True, 'free_model_daily_requests': {'used': 7, 'limit': 50, 'remaining': 43}}})
        raise AssertionError('No metadata fixture may infer.')


def connected(env, **changes):
    fixture = MetadataFixture()
    client = httpx.Client(transport=httpx.MockTransport(fixture))
    env.service.vault = env.vault
    connection = OpenRouterConnection(env.service, client)
    config = connection.save({'api_key': 'synthetic_openrouter_fixture_key', 'enabled': True, 'remote_consent': True, **changes})
    provider = OpenRouterProvider(config, env.vault, client)
    return connection, provider, fixture, client


def request(**changes):
    return {'model': FREE_ROUTER, 'context': 32768, 'tokens': 1024, 'thinking': False,
            'messages': [{'role': 'user', 'content': 'Use only this synthetic fixture.'}], **changes}


def sse(packet):
    return ('data: ' + json.dumps(packet) + '\n\n').encode()


def test_key_save_and_status_are_local_and_config_contains_reference_only(env):
    fixture = MetadataFixture(); client = httpx.Client(transport=httpx.MockTransport(fixture))
    env.service.vault = env.vault
    connection = OpenRouterConnection(env.service, client)
    assert connection.dispatch('openrouter_status')['provider']['configured'] is False
    config = connection.save({'api_key': 'synthetic_openrouter_fixture_key'})
    assert config['enabled'] is False and config['remote_consent'] is False and config['data_collection'] == 'deny'
    assert config['credential_ref'] in env.vault.values
    assert 'synthetic_openrouter_fixture_key' not in json.dumps(env.service.store.entities('providers'))
    connection.dispatch('openrouter_status')
    assert not fixture.calls
    client.close()


def test_cloud_enable_requires_explicit_consent_and_fixed_https_endpoint(env):
    env.service.vault = env.vault; connection = OpenRouterConnection(env.service)
    with pytest.raises(ValueError, match='Consent'):
        connection.save({'api_key': 'synthetic_openrouter_fixture_key', 'enabled': True})
    with pytest.raises(ValueError, match='fixed official'):
        connection.save({'url': 'https://untrusted.invalid/api/v1', 'api_key': 'synthetic_openrouter_fixture_key'})
    with pytest.raises(ValueError, match='fixed official'):
        OpenRouterProvider({'url': 'http://openrouter.ai/api/v1'}, env.vault)
    with pytest.raises(ValueError, match=':free'):
        connection.save({'api_key': 'synthetic_openrouter_fixture_key', 'model': 'fixture/paid'})


def test_connection_test_uses_account_and_catalog_only_and_redacts_account_labels(env):
    connection, _, fixture, client = connected(env)
    result = connection.test()
    assert [r.url.path for r in fixture.calls] == ['/api/v1/key', '/api/v1/models']
    assert result['account']['free_model_daily_requests'] == {'used': 7, 'limit': 50, 'remaining': 43}
    serialized = json.dumps(result)
    assert 'private@example.invalid' not in serialized and 'limit_remaining' not in serialized and 'synthetic_openrouter_fixture_key' not in serialized
    assert [m['name'] for m in result['models']] == [FREE_ROUTER, 'fixture/coder:free']
    client.close()


@pytest.mark.parametrize('pricing', [{**ZERO, 'request': '0.01'}, {**ZERO, 'image': '0.001'}, {**ZERO, 'internal_reasoning': '0.1'}, {**ZERO, 'prompt': '-1'}, {**ZERO, 'completion': 'NaN'}, {**ZERO, 'overrides': [{'min_prompt_tokens': 50000, 'prompt': '0.01'}]}, {'prompt': '0'}])
def test_catalog_rejects_any_paid_unknown_or_conditional_pricing(pricing):
    assert not zero_pricing(pricing)
    assert free_catalog([model('fixture/unsafe:free', pricing)]) == []


def test_catalog_accepts_all_zero_overrides_but_never_paid_id():
    pricing = {**ZERO, 'overrides': [{'min_prompt_tokens': 50000, 'prompt': '0', 'completion': '0'}]}
    assert zero_pricing(pricing)
    assert [m['name'] for m in free_catalog([model('fixture/free:free', pricing), model('fixture/paid', ZERO)])] == ['fixture/free:free']


def test_real_free_router_catalog_omits_unbilled_optional_price_fields():
    advertised = {'id': 'openrouter/free', 'name': 'Free Models Router', 'context_length': 200000,
                  'pricing': {'prompt': '0', 'completion': '0'}, 'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
                  'supported_parameters': ['tools', 'tool_choice', 'max_tokens', 'temperature']}
    assert zero_pricing(advertised['pricing'])
    assert free_catalog([advertised])[0]['name'] == FREE_ROUTER
    assert free_catalog([{**advertised, 'id': 'fixture/paid-trial'}]) == []
    assert free_catalog([{**advertised, 'pricing': {**advertised['pricing'], 'image': '0.01'}}]) == []


def test_body_enforces_zero_all_price_caps_privacy_and_no_paid_fallback(env):
    _, provider, _, client = connected(env)
    body = {'model': FREE_ROUTER, 'messages': [], 'stream': True, 'provider': {'max_price': {'prompt': 100}, 'data_collection': 'allow'}, 'ignored': 'unsafe_extra'}
    safe = provider.prepare_body(request(), body)
    assert safe['provider'] == {'max_price': {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0}, 'require_parameters': True, 'data_collection': 'deny', 'allow_fallbacks': False}
    assert 'ignored' not in safe
    for field in ('models', 'plugins', 'server_tools', 'preset', 'transforms'):
        with pytest.raises(ValueError, match='fallbacks, plugins or server tools'):
            provider.prepare_body(request(**{field: []}), body)
    with pytest.raises(ValueError, match='unavailable'):
        provider.prepare_body(request(model='fixture/paid'), {**body, 'model': 'fixture/paid'})
    with pytest.raises(ValueError, match='function tools only'):
        provider.prepare_body(request(), {**body, 'tools': [{'type': 'web_search'}]})
    client.close()


def test_disabled_cloud_consent_stops_before_any_metadata_or_prompt_leaves(env):
    _, provider, fixture, client = connected(env, enabled=False, remote_consent=False)
    with pytest.raises(ValueError, match='enabled cloud consent'):
        list(provider.generate(request(), threading.Event()))
    assert fixture.calls == []
    client.close()


def test_data_collection_allow_requires_explicit_configuration(env):
    _, provider, _, client = connected(env, data_collection='allow')
    assert provider.prepare_body(request(), {'model': FREE_ROUTER})['provider']['data_collection'] == 'allow'
    client.close()


@pytest.mark.parametrize('status', [401, 403, 402, 429, 503])
def test_metadata_errors_do_not_expose_remote_account_message_or_retry(env, status):
    connection, _, fixture, client = connected(env)
    fixture.error = status
    with pytest.raises(ValueError) as error:
        connection.test()
    assert 'private account' not in str(error.value) and 'synthetic_api_key' not in str(error.value)
    assert len(fixture.calls) == 1
    client.close()


def test_split_long_tool_calls_preserve_call_identity_usage_and_zero_price_request(env, monkeypatch):
    _, provider, _, client = connected(env)
    arguments = json.dumps({'query': 'synthetic research ' + 'x'*18000})
    packets = [sse({'model': 'fixture/coder:free', 'provider': 'Fixture Host', 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function'}]}}]})]
    packets += [sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'web_search' if n == 0 else '', 'arguments': arguments[n:n+400]}}]}}]}) for n in range(0, len(arguments), 400)]
    packets += [sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 37, 'completion_tokens': 15, 'total_tokens': 52}}), b'data: [DONE]\n\n']
    calls = []
    def handler(http_request):
        calls.append(http_request)
        payload = json.loads(http_request.content)
        assert payload['provider']['max_price'] == {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0}
        assert payload['provider']['data_collection'] == 'deny' and payload['model'] == FREE_ROUTER
        assert http_request.headers['Authorization'] == 'Bearer synthetic_openrouter_fixture_key'
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in packets]))
    transport(monkeypatch, handler)
    result = list(provider.generate(request(tools=[{'type': 'function', 'function': {'name': 'web_search', 'description': 'Read-only local research tool.', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]), threading.Event()))
    accumulator = ToolCallAccumulator()
    for item in result:
        accumulator.add(item.get('message', {}).get('tool_calls', []))
    tool_calls = accumulator.finish()
    assert tool_calls[0]['id'] == 'call_fixture' and tool_calls[0]['function']['arguments']['query'].endswith('x'*18000)
    assert result[-1]['done'] and result[-1]['usage']['prompt_tokens'] == 37
    assert result[-1]['provider_model'] == 'fixture/coder:free' and result[-1]['response_provider'] == 'Fixture Host'
    assert len(calls) == 1
    client.close()


def test_stop_interrupts_cloud_stream_socket_without_retry(env, monkeypatch):
    _, provider, _, client = connected(env)
    entered = threading.Event(); body = TimedBody([], stall=True); calls = []
    def handler(http_request):
        calls.append(http_request); entered.set(); return httpx.Response(200, stream=body)
    transport(monkeypatch, handler)
    cancel = threading.Event(); result = []; errors = []
    def consume():
        try: result.extend(provider.generate(request(), cancel))
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=consume); thread.start(); assert entered.wait(2)
    began = time.monotonic(); cancel.set(); thread.join(2)
    assert not thread.is_alive() and time.monotonic() - began < 1
    assert len(calls) == 1 and body.closed.is_set() and not result and not errors
    client.close()


@pytest.mark.parametrize('status', [401, 429])
def test_inference_auth_rate_limit_error_redacted_and_no_automatic_retry(env, monkeypatch, status):
    _, provider, _, client = connected(env)
    calls = []
    def handler(http_request):
        calls.append(http_request); return httpx.Response(status, json={'error': {'message': 'synthetic_api_key private prompt data'}})
    transport(monkeypatch, handler)
    with pytest.raises(ValueError) as error:
        list(provider.generate(request(), threading.Event()))
    assert 'synthetic_api_key' not in str(error.value) and 'private prompt' not in str(error.value)
    assert len(calls) == 1
    client.close()


def test_sse_error_body_is_scrubbed_and_incomplete_calls_do_not_complete(env, monkeypatch):
    _, provider, _, client = connected(env)
    body = TimedBody([(0, sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'bad_call', 'function': {'name': 'unknown_callback', 'arguments': '{'}}]}}]})),
                      (0, sse({'error': {'message': 'synthetic_openrouter_fixture_key private tool result'}}))])
    transport(monkeypatch, lambda http_request: httpx.Response(200, stream=body))
    seen = []
    with pytest.raises(ValueError) as error:
        seen.extend(provider.generate(request(), threading.Event()))
    assert 'synthetic_openrouter_fixture_key' not in str(error.value) and 'private tool result' not in str(error.value)
    assert not any(packet.get('done') for packet in seen)
    client.close()


def pool_provider(env, client):
    env.service.providers.vault = env.vault
    provider = env.service.providers.provider('openrouter')
    provider.http_client = client
    return provider


def completion(text='Synthetic fixture answer.', model_name='fixture/coder:free'):
    return [sse({'model': model_name, 'provider': 'Fixture Host', 'choices': [{'delta': {'content': text}}]}),
            sse({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 37, 'completion_tokens': 15}}),
            b'data: [DONE]\n\n']


def usage_rows(env, run_id):
    with env.service.store._connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM usage WHERE run_id=?', (run_id,))]


def test_real_pool_cloud_bypasses_held_gpu_lease_and_records_actual_model_once(env, monkeypatch):
    _, _, _, client = connected(env)
    pool_provider(env, client)
    calls = []
    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in completion()]))
    transport(monkeypatch, handler)
    run = env.service.jobs.start({'text': 'Queue fixture.', 'model': FREE_ROUTER, 'provider_id': 'openrouter'})
    run = env.service.store.run(run['id'])
    result = []; errors = []
    def cloud():
        try: result.extend(env.service.providers.generate(request(provider_id='openrouter'), threading.Event(), run))
        except Exception as exc: errors.append(exc)
    with env.service.providers.queue.lease(threading.Event()):
        thread = threading.Thread(target=cloud); thread.start(); thread.join(2)
        assert not thread.is_alive(), 'Cloud inference incorrectly waited for the held local GPU lease.'
    assert not errors and result[-1]['done'] and len(calls) == 1
    rows = usage_rows(env, run['id'])
    assert len(rows) == 1 and rows[0]['provider'] == 'openrouter' and rows[0]['model'] == 'fixture/coder:free'
    assert rows[0]['input_tokens'] == 37 and rows[0]['output_tokens'] == 15 and not rows[0]['estimated']
    events = [event for event in env.service.store.events(run['id']) if event['type'] == 'usage']
    assert len(events) == 1 and events[0]['requested_model'] == FREE_ROUTER and events[0]['actual_model'] == 'fixture/coder:free'
    assert events[0]['response_provider'] == 'Fixture Host'
    client.close()


def test_actual_cloud_research_child_returns_local_tool_result_to_parent_once(env, monkeypatch):
    _, _, _, client = connected(env)
    pool_provider(env, client)
    parent = env.service.jobs.start({'text': 'Parent stays local.', 'auto_delegate': True})
    profile = env.service.agent_save({'name': 'Cloud researcher fixture', 'role': 'researcher',
             'provider_id': 'openrouter', 'model': FREE_ROUTER, 'tools': ['web_search', 'web_fetch'], 'instructions': 'Research the synthetic question.'})
    calls = []; research_calls = []
    original_dispatch = env.service.core.dispatch
    def research(action, data):
        if action == 'research':
            research_calls.append(data)
            return {'sources': [{'title': 'Fixture source', 'url': 'https://fixture.invalid/source', 'snippet': 'Synthetic public evidence.'}]}
        return original_dispatch(action, data)
    env.service.core.dispatch = research
    def handler(http_request):
        payload = json.loads(http_request.content); calls.append(payload)
        assert payload['model'] == FREE_ROUTER and all(value == 0 for value in payload['provider']['max_price'].values())
        assert not any(field in payload for field in ('models', 'plugins', 'server_tools'))
        if len(calls) == 1:
            packets = [sse({'model': 'fixture/coder:free', 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'research_fixture_call', 'type': 'function',
                      'function': {'name': 'web_search', 'arguments': json.dumps({'query': 'fixture research'})}}]}}]}),
                       sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 37, 'completion_tokens': 15}})]
        else:
            tool_result = next(message for message in payload['messages'] if message['role'] == 'tool')
            assert tool_result['tool_call_id'] == 'research_fixture_call' and 'Synthetic public evidence.' in tool_result['content']
            packets = completion('Research complete: https://fixture.invalid/source')
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in packets]))
    transport(monkeypatch, handler)
    env.service.jobs._launch = MethodType(RunManager._launch, env.service.jobs)
    child = env.service.agent_start({'agent_id': profile['id'], 'parent_id': parent['id'], 'text': 'Find the fixture evidence.'})
    finished = wait_finished(env.service, child)
    assert finished['status'] == 'completed' and len(calls) == 2 and research_calls == [{'query': 'fixture research'}]
    result = env.service.jobs.registry.execute(env.service.store.run(parent['id']), 'agent_result', {'run_id': child['id']}, threading.Event())
    assert result['run']['readonly'] and result['run']['settings']['provider_id'] == 'openrouter'
    assert result['messages'][-1]['content'] == 'Research complete: https://fixture.invalid/source'
    rows = usage_rows(env, child['id'])
    assert len(rows) == 2 and all(row['purpose'] == 'child' for row in rows)
    assert not usage_rows(env, parent['id'])
    client.close()


def test_actual_child_cancel_closes_cloud_socket_and_records_one_cancelled_request(env, monkeypatch):
    _, _, _, client = connected(env)
    pool_provider(env, client)
    parent = env.service.jobs.start({'text': 'Parent fixture.'})
    profile = env.service.agent_save({'name': 'Cloud cancellation fixture', 'role': 'researcher', 'provider_id': 'openrouter', 'model': FREE_ROUTER})
    body = TimedBody([], stall=True); entered = threading.Event(); calls = []
    def handler(http_request):
        calls.append(http_request); entered.set(); return httpx.Response(200, stream=body)
    transport(monkeypatch, handler)
    env.service.jobs._launch = MethodType(RunManager._launch, env.service.jobs)
    child = env.service.agent_start({'agent_id': profile['id'], 'parent_id': parent['id'], 'text': 'Cancellation fixture.'})
    assert entered.wait(2)
    env.service.jobs.cancel(child['id'])
    finished = wait_finished(env.service, child)
    assert finished['status'] == 'cancelled' and body.closed.is_set() and len(calls) == 1
    rows = usage_rows(env, child['id'])
    assert len(rows) == 1 and rows[0]['cancelled'] and rows[0]['purpose'] == 'child'
    assert not env.service.providers.remote_queue.active
    client.close()


def test_trusted_looking_remote_error_cannot_bypass_redaction(env, monkeypatch):
    _, provider, _, client = connected(env)
    body = TimedBody([(0, sse({'error': {'message': 'OpenRouter returned synthetic_openrouter_fixture_key private tool results'}}))])
    transport(monkeypatch, lambda http_request: httpx.Response(200, stream=body))
    with pytest.raises(ValueError) as error:
        list(provider.generate(request(), threading.Event()))
    assert 'synthetic_openrouter_fixture_key' not in str(error.value) and 'private tool results' not in str(error.value)
    client.close()


def test_remote_compaction_rate_limit_is_not_automatically_retried(env, monkeypatch):
    _, _, _, client = connected(env)
    pool_provider(env, client)
    calls = []
    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(429, json={'error': {'message': 'Synthetic quota exhausted.'}})
    transport(monkeypatch, handler)
    run = env.service.jobs.start({'text': 'Synthetic compaction request.', 'model': FREE_ROUTER, 'provider_id': 'openrouter', 'context': 4096})
    run = env.service.store.run(run['id'])
    compacted = env.service.jobs._compact(run, [], {'cancel': threading.Event()}, force=True)
    assert compacted['summary'].startswith('Deterministic checkpoint.')
    assert len(calls) == 1, 'A remote quota failure must not consume additional automatic compaction requests.'
    assert len(usage_rows(env, run['id'])) == 1
    client.close()


def test_malformed_remote_cached_usage_still_records_one_failed_request(env, monkeypatch):
    _, _, _, client = connected(env)
    pool_provider(env, client)
    packets = [sse({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 37, 'completion_tokens': 15, 'prompt_tokens_details': 'invalid fixture details'}})]
    transport(monkeypatch, lambda http_request: httpx.Response(200, stream=TimedBody([(0, packet) for packet in packets])))
    run = env.service.jobs.start({'text': 'Usage validation fixture.', 'provider_id': 'openrouter', 'model': FREE_ROUTER})
    with pytest.raises(ValueError, match='OpenRouter'):
        list(env.service.providers.generate(request(provider_id='openrouter'), threading.Event(), env.service.store.run(run['id'])))
    rows = usage_rows(env, run['id'])
    assert len(rows) == 1 and rows[0]['estimated']
    client.close()
