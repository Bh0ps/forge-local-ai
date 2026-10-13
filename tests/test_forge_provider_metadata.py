"""Offline declared metadata never becomes validated inference authority."""
import json
import threading

import httpx
import pytest

from forge_provider_metadata import (MAX_JSON_BYTES, ProviderProbeRevoked,
    fetch_json, fetch_provider_metadata)


CONFIG = {'kind': 'openai-compatible', 'preset_id': 'lm-studio',
    'url': 'https://engine.example/v1'}


def factory(handler, options=None):
    def create(**kwargs):
        if options is not None:
            options.append(kwargs)
        return httpx.Client(**kwargs, transport=httpx.MockTransport(handler))
    return create


def entry(**changes):
    return {'type': 'llm', 'key': 'catalog-model', 'max_context_length': 262144,
        'loaded_instances': [{'id': 'loaded-model', 'config': {'context_length': 4096}}],
        'capabilities': {'vision': True, 'trained_for_tool_use': True,
            'reasoning': {'allowed_options': ['off', 'high', 'low']}}, **changes}


def read(entries, model=None):
    return fetch_provider_metadata(CONFIG, model=model,
        client_factory=factory(lambda request: httpx.Response(200, json={'models': entries})))


def test_metadata_binds_exact_origin_and_keeps_loaded_context_separate():
    requests, options = [], []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'models': [entry(),
            {'type': 'embedding', 'key': 'not-an-llm'}]})
    result = fetch_provider_metadata(CONFIG, token='synthetic-key',
        client_factory=factory(handle, options))
    assert len(requests) == 1
    assert str(requests[0].url) == 'https://engine.example/api/v1/models'
    assert requests[0].headers['authorization'] == 'Bearer synthetic-key'
    assert options[0]['trust_env'] is options[0]['follow_redirects'] is False
    assert result == {'state': 'available', 'source': 'advertised', 'models': [{
        'model': 'catalog-model', 'max_context_length': 262144,
        'loaded_context_lengths': [4096],
        'loaded_instances': [{'model': 'loaded-model', 'context_length': 4096}],
        'vision': True, 'trained_for_tool_use': True,
        'reasoning_options': ['off', 'low', 'high']}]}
    assert 'tools' not in result['models'][0]


@pytest.mark.parametrize('model', ['catalog-model', 'loaded-model'])
def test_metadata_lookup_accepts_only_exact_catalog_or_loaded_id(model):
    assert read([entry()], model)['state'] == 'available'


@pytest.mark.parametrize('model', ['catalog', 'loaded', 'Catalog-model', 'other/model'])
def test_metadata_lookup_does_not_guess_aliases(model):
    assert read([entry()], model) == {'state': 'unknown', 'source': 'unknown', 'models': []}


def test_multiple_loaded_instances_remain_distinct_and_ambiguous_names_are_unknown():
    item = entry(loaded_instances=[{'id': 'small', 'config': {'context_length': 4096}},
        {'id': 'large', 'config': {'context_length': 8192}}])
    result = read([item], 'catalog-model')['models'][0]
    assert result['loaded_context_lengths'] == [4096, 8192]
    assert result['loaded_instances'] == [{'model': 'small', 'context_length': 4096},
        {'model': 'large', 'context_length': 8192}]
    assert read([entry(), entry(key='other')], 'loaded-model')['state'] == 'unknown'
    assert read([entry(), entry()], 'catalog-model')['state'] == 'unknown'


def test_duplicate_loaded_id_cannot_assert_a_context():
    item = entry(loaded_instances=[{'id': 'repeated', 'config': {'context_length': 4096}},
        {'id': 'repeated', 'config': {'context_length': 8192}}])
    fact = read([item])['models'][0]
    assert fact['loaded_instances'] == fact['loaded_context_lengths'] == []
    assert read([item], 'repeated')['state'] == 'unknown'


@pytest.mark.parametrize('value', [None, False, 0, -1, 2**31, 4096.0, '4096'])
def test_missing_or_invalid_context_is_unknown_not_defaulted(value):
    fact = read([entry(max_context_length=value,
        loaded_instances=[{'id': 'loaded-model', 'config': {'context_length': value}}])])['models'][0]
    assert fact['max_context_length'] is None
    assert fact['loaded_instances'][0]['context_length'] is None
    assert fact['loaded_context_lengths'] == []


def test_absent_capabilities_and_unrecognized_reasoning_controls_remain_unknown():
    fact = read([entry(capabilities={'vision': 'yes', 'trained_for_tool_use': 1,
        'reasoning': {'allowed_options': ['auto', 'off']}})])['models'][0]
    assert fact['vision'] is fact['trained_for_tool_use'] is None
    assert fact['reasoning_options'] == []
    fact = read([{'type': 'llm', 'key': 'plain'}])['models'][0]
    assert fact['max_context_length'] is None and fact['loaded_instances'] == []
    assert fact['reasoning_options'] == []


@pytest.mark.parametrize('config,state', [({**CONFIG, 'url': 'https://engine.example/proxy/v1'}, 'unknown'),
    ({**CONFIG, 'preset_id': None}, 'unsupported'),
    ({**CONFIG, 'preset_id': 'jan-desktop'}, 'unsupported')])
def test_no_root_endpoint_guess_for_custom_or_other_presets(config, state):
    result = fetch_provider_metadata(config,
        client_factory=lambda **kwargs: pytest.fail('Unsupported metadata performed I/O'))
    assert result == {'state': state, 'source': 'unknown', 'models': []}


@pytest.mark.parametrize('body', [b'{"models":[],"models":[]}', b'{"models":NaN}',
    b'[{"models":[]}]', b'not JSON', b'\xff'])
def test_metadata_malformed_response_is_bounded_unknown(body):
    result = fetch_provider_metadata(CONFIG,
        client_factory=factory(lambda request: httpx.Response(200, content=body)))
    assert result == {'state': 'unknown', 'source': 'unknown', 'models': []}


@pytest.mark.parametrize('entries', [None, {}, [entry()] * 257])
def test_missing_or_over_limit_metadata_catalog_is_unknown(entries):
    assert read(entries)['state'] == 'unknown'


@pytest.mark.parametrize('status', [302, 401, 404, 500])
def test_metadata_errors_never_follow_urls_or_echo_private_response(status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={'location': 'https://other.example/private'},
            text='private-path synthetic-key')
    result = fetch_provider_metadata(CONFIG, token='synthetic-key', client_factory=factory(handle))
    assert result == {'state': 'unknown', 'source': 'unknown', 'models': []}
    assert len(requests) == 1 and requests[0].url.host == 'engine.example'


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, after=None):
        self.chunks, self.after = chunks, after
    def __iter__(self):
        for chunk in self.chunks:
            yield chunk
            if self.after:
                self.after()


def test_json_stream_limit_applies_without_content_length():
    def handle(request):
        return httpx.Response(200, stream=Chunks([b' ' * 4096] * (MAX_JSON_BYTES // 4096 + 1)))
    with pytest.raises(ValueError, match='Invalid engine metadata'):
        fetch_json('https://engine.example/v1/models', client_factory=factory(handle))


def test_declared_body_limit_does_not_consume_oversized_stream():
    class NeverRead(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('Oversized declared response was consumed')
            yield b''
    with pytest.raises(ValueError):
        fetch_json('https://engine.example/v1/models', client_factory=factory(lambda request:
            httpx.Response(200, headers={'content-length': str(MAX_JSON_BYTES + 1)}, stream=NeverRead())))


@pytest.mark.parametrize('cancelled', [False, True])
def test_revocation_during_stream_is_not_downgraded_to_unknown(cancelled):
    cancel, alive = threading.Event(), [True]
    def revoke():
        cancel.set() if cancelled else alive.__setitem__(0, False)
    def handle(request):
        return httpx.Response(200, stream=Chunks([b' ' * 4096,
            json.dumps({'models': [entry()]}).encode()], revoke))
    with pytest.raises(ProviderProbeRevoked):
        fetch_provider_metadata(CONFIG, cancel=cancel, current=lambda: alive[0], client_factory=factory(handle))


def test_cancelled_probe_never_opens_client():
    cancel = threading.Event(); cancel.set()
    with pytest.raises(ProviderProbeRevoked, match='cancelled'):
        fetch_provider_metadata(CONFIG, cancel=cancel,
            client_factory=lambda **kwargs: pytest.fail('Cancelled metadata opened client'))


def test_failed_identity_callback_is_sanitized_and_never_opens_client():
    def current():
        raise RuntimeError('private-path synthetic-key')
    with pytest.raises(ProviderProbeRevoked, match='configuration changed') as captured:
        fetch_provider_metadata(CONFIG, current=current,
            client_factory=lambda **kwargs: pytest.fail('Unverifiable metadata opened client'))
    assert 'private' not in str(captured.value) and 'synthetic' not in str(captured.value)


def test_total_deadline_rejects_response_completed_after_budget(monkeypatch):
    import forge_provider_metadata
    tick = [0.0]
    monkeypatch.setattr(forge_provider_metadata.time, 'monotonic', lambda: tick[0])
    def handle(request):
        tick[0] = 10.0
        return httpx.Response(200, json={'models': [entry()]})
    assert fetch_provider_metadata(CONFIG, client_factory=factory(handle))['state'] == 'unknown'
