"""Independent review protocol regressions; no credentials or cloud requests."""
from copy import deepcopy
import json
import threading

import httpx
import pytest

from forge_inference import CompatibleProvider, validated_response_format
from forge_openrouter import FREE_ROUTER, free_catalog
from test_forge_channels import env
from test_forge_openrouter import connected, model, request, sse
from test_inference_timeouts import TimedBody, transport


FORMAT = {
    'type': 'json_schema',
    'json_schema': {
        'name': 'forge_goal_verdict', 'strict': True,
        'schema': {
            'type': 'object',
            'properties': {'verdict': {'type': 'string', 'enum': ['complete', 'needs_changes', 'insufficient_evidence']}},
            'required': ['verdict'], 'additionalProperties': False,
        },
    },
}


def completion(reason='stop', content='{"verdict":"complete"}', **delta):
    return [sse({'choices': [{'delta': {'content': content, **delta}}]}),
            sse({'choices': [{'delta': {}, 'finish_reason': reason}],
                 'usage': {'prompt_tokens': 29, 'completion_tokens': 7}}),
            b'data: [DONE]\n\n']


def local_generate(monkeypatch, packets, **changes):
    captured = []
    def handler(http_request):
        captured.append(json.loads(http_request.content))
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in packets]))
    transport(monkeypatch, handler)
    provider = CompatibleProvider({'url': 'http://127.0.0.1:8080/v1'})
    return provider.generate(request(**changes), threading.Event()), captured


def test_strict_schema_reaches_free_router_without_loosening_price_or_privacy(env, monkeypatch):
    _, provider, _, client = connected(env)
    captured = []
    def handler(http_request):
        captured.append(json.loads(http_request.content))
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in completion()]))
    transport(monkeypatch, handler)
    try:
        output = list(provider.generate(request(response_format=deepcopy(FORMAT)), threading.Event()))
        assert output[-1]['done'] and output[-1]['provider_finish_reason'] == 'stop'
        assert len(captured) == 1 and captured[0]['response_format'] == FORMAT
        assert captured[0]['model'] == FREE_ROUTER
        assert captured[0]['provider'] == {
            'max_price': {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0},
            'require_parameters': True, 'data_collection': 'deny', 'allow_fallbacks': False,
        }
        assert not {'models', 'plugins', 'server_tools'} & captured[0].keys()
    finally:
        client.close()


def test_standard_compatible_generation_does_not_gain_structured_format(monkeypatch):
    stream, captured = local_generate(monkeypatch, completion(content='Ordinary response.'))
    assert list(stream)[-1]['done']
    assert 'response_format' not in captured[0]


def test_compatible_endpoint_receives_detached_strict_schema(monkeypatch):
    value = deepcopy(FORMAT)
    validated = validated_response_format(value)
    value['json_schema']['schema']['properties']['verdict']['enum'].append('unsafe_extra')
    stream, captured = local_generate(monkeypatch, completion(), response_format=validated)
    assert list(stream)[-1]['done']
    assert captured[0]['response_format'] == FORMAT


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(type='json_object'),
    lambda v: v.update(plugins=['unsafe']),
    lambda v: v['json_schema'].update(strict=False),
    lambda v: v['json_schema'].update(name='invalid name'),
    lambda v: v['json_schema'].update(url='https://untrusted.invalid'),
    lambda v: v['json_schema']['schema'].update(additionalProperties=True),
    lambda v: v['json_schema']['schema'].update(required=['missing']),
    lambda v: v['json_schema']['schema'].update(required=['verdict', 'verdict']),
    lambda v: v['json_schema']['schema'].update(unsupported_number=float('nan')),
    lambda v: v['json_schema']['schema'].update(description='🌍' * 7000),
])
def test_invalid_structured_envelopes_fail_before_http_request(monkeypatch, mutation):
    value = deepcopy(FORMAT); mutation(value)
    stream, captured = local_generate(monkeypatch, completion(), response_format=value)
    with pytest.raises(ValueError, match='structured response schema'):
        list(stream)
    assert not captured


def test_recursive_or_overdeep_schema_is_rejected_locally():
    value = deepcopy(FORMAT)
    value['json_schema']['schema']['recursive'] = value
    with pytest.raises(ValueError, match='structured response schema'):
        validated_response_format(value)
    value = deepcopy(FORMAT); nested = value['json_schema']['schema']
    for _ in range(26):
        nested['nested'] = {}; nested = nested['nested']
    with pytest.raises(ValueError, match='structured response schema'):
        validated_response_format(value)


def test_schema_uses_the_same_context_budget_as_request(monkeypatch):
    value = deepcopy(FORMAT)
    value['json_schema']['schema']['properties']['verdict']['description'] = 'x' * 6500
    stream, captured = local_generate(monkeypatch, completion(), context=2048, response_format=value)
    with pytest.raises(ValueError, match='context budget'):
        list(stream)
    assert not captured


def test_specific_free_model_requires_advertised_structured_output(env):
    _, provider, _, client = connected(env)
    try:
        with pytest.raises(ValueError, match='does not advertise structured output'):
            provider.prepare_body(request(), {'model': 'fixture/coder:free', 'response_format': FORMAT})
    finally:
        client.close()


def test_free_catalog_exposes_structured_capability_from_metadata():
    item = model('fixture/reviewer:free')
    item['supported_parameters'].append('response_format')
    assert 'structured_outputs' in free_catalog([item])[0]['capabilities']
    assert 'structured_outputs' in free_catalog([model()])[0]['capabilities']


@pytest.mark.parametrize('reason', ['error', 'content_filter', 'refusal', 'cancelled', 'future_unknown', '', False, 123])
def test_valid_looking_verdict_with_unsuccessful_finish_never_completes(monkeypatch, reason):
    stream, captured = local_generate(monkeypatch, completion(reason))
    received = []
    with pytest.raises(ValueError, match='completion reason|did not complete'):
        for item in stream:
            received.append(item)
    assert captured and any(item.get('message', {}).get('content') for item in received)
    assert not any(item.get('done') for item in received)


def test_done_sentinel_without_explicit_finish_is_incomplete(monkeypatch):
    stream, _ = local_generate(monkeypatch, [completion()[0], b'data: [DONE]\n\n'])
    with pytest.raises(ValueError, match='ended before completion'):
        list(stream)


@pytest.mark.parametrize('reason', ['stop', 'tool_calls', 'function_call', 'length', 'max_tokens'])
def test_success_or_output_limit_reason_remains_exact(monkeypatch, reason):
    stream, _ = local_generate(monkeypatch, completion(reason))
    final = list(stream)[-1]
    assert final['done'] and final['provider_finish_reason'] == reason
    assert final['done_reason'] == reason


def test_refusal_cannot_approve_valid_looking_verdict(monkeypatch):
    stream, _ = local_generate(monkeypatch, completion(refusal='Cannot verify.'))
    with pytest.raises(ValueError, match='refused'):
        list(stream)


def test_error_after_successful_finish_still_invalidates_completion(monkeypatch):
    packets = completion()[:-1] + [sse({'choices': [{'delta': {}, 'finish_reason': 'error'}]}), b'data: [DONE]\n\n']
    stream, _ = local_generate(monkeypatch, packets)
    with pytest.raises(ValueError, match='did not complete'):
        list(stream)


def test_openrouter_scrubs_error_after_valid_looking_verdict_without_retry(env, monkeypatch):
    _, provider, _, client = connected(env)
    calls = []
    def handler(http_request):
        calls.append(http_request)
        packets = completion()[:-1] + [sse({'error': {'message': 'private synthetic_secret echoed'}})]
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in packets]))
    transport(monkeypatch, handler)
    try:
        with pytest.raises(ValueError) as result:
            list(provider.generate(request(response_format=FORMAT), threading.Event()))
        assert len(calls) == 1
        assert 'private' not in str(result.value) and 'synthetic_secret' not in str(result.value)
    finally:
        client.close()
