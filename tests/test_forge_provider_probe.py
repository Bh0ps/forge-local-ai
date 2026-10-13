"""Draft connection tests never mutate configuration or move saved secrets."""
import httpx
import pytest

from test_forge_core import service


class Vault:
    def __init__(self):
        self.reads = []
        self.writes = []

    def get(self, reference):
        self.reads.append(reference)
        return 'synthetic-engine-key'

    def put(self, value):
        self.writes.append(value)
        return 'replacement-reference'


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handler)))


def saved_engine(svc):
    return svc.store.save_entity('providers', dict(id='engine', name='Engine',
        url='https://engine.example/v1', kind='openai-compatible',
        credential_ref='saved-reference', capabilities=['tools'], context_limit=65536,
        enabled=False, resource_id='private-lane', independent_resource=True))


@pytest.mark.parametrize('target,kind,reuse', [
    ('https://engine.example/v1/', 'openai-compatible', True),
    ('https://other.example/v1', 'openai-compatible', False),
    ('https://engine.example/other', 'openai-compatible', False),
    ('https://engine.example/v1', 'ollama', False),
])
def test_probe_keeps_configuration_and_binds_saved_key_to_exact_target(tmp_path, monkeypatch, target, kind, reuse):
    svc = service(tmp_path)
    svc.vault = Vault()
    previous = saved_engine(svc)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'id': 'fixture'}], 'models': []})
    transport(monkeypatch, handle)
    try:
        result = svc.dispatch('provider_test', {'id': 'engine', 'draft': {'url': target, 'kind': kind}})
        assert result['ok'] and set(result) == {'ok', 'model_count'}
        assert len(requests) == 1
        assert requests[0].headers.get('authorization') == ('Bearer synthetic-engine-key' if reuse else None)
        assert svc.vault.reads == (['saved-reference'] if reuse else [])
        assert svc.vault.writes == []
        assert svc.store.entity('providers', 'engine') == previous
    finally:
        svc.shutdown()


def test_new_draft_tests_entered_key_in_memory_without_saving_it(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.vault = Vault()
    before = svc.store.entities('providers')
    def handle(request):
        assert request.headers['authorization'] == 'Bearer new-synthetic-key'
        return httpx.Response(200, json={'data': [{'id': 'fixture'}, {'id': 'other'}]})
    transport(monkeypatch, handle)
    try:
        assert svc.dispatch('provider_test', {'draft': {'kind': 'openai-compatible', 'url': 'https://new.example/v1'},
            'api_key': 'new-synthetic-key'}) == {'ok': True, 'model_count': 2}
        assert svc.store.entities('providers') == before
        assert svc.vault.writes == svc.vault.reads == []
    finally:
        svc.shutdown()


@pytest.mark.parametrize('status,expected', [(401, 'Authentication'), (403, 'Authentication'),
    (404, 'base URL'), (500, 'status')])
def test_probe_errors_do_not_echo_remote_body_or_credentials(tmp_path, monkeypatch, status, expected):
    svc = service(tmp_path)
    transport(monkeypatch, lambda request: httpx.Response(status, text='synthetic-engine-key private response'))
    try:
        with pytest.raises(ValueError, match=expected) as captured:
            svc.dispatch('provider_test', {'draft': {'kind': 'openai-compatible', 'url': 'https://engine.example/v1'},
                'api_key': 'synthetic-engine-key'})
        assert 'synthetic' not in str(captured.value) and 'private response' not in str(captured.value)
    finally:
        svc.shutdown()


@pytest.mark.parametrize('url', ['https://user:secret@engine.example/v1',
    'https://engine.example/v1?api_key=secret', 'https://engine.example/v1#secret',
    'https://engine.example:99999/v1', 'file:///local', 'http://engine.example\r\n/v1'])
def test_bad_draft_endpoints_never_send_requests(tmp_path, monkeypatch, url):
    svc = service(tmp_path)
    transport(monkeypatch, lambda request: pytest.fail('Invalid draft made a request'))
    try:
        with pytest.raises(ValueError):
            svc.dispatch('provider_test', {'draft': {'kind': 'openai-compatible', 'url': url}})
        assert svc.store.entities('providers') == []
    finally:
        svc.shutdown()


@pytest.mark.parametrize('changed', [False, True])
def test_engine_edit_retains_trusted_metadata_but_never_moves_old_key(tmp_path, changed):
    svc = service(tmp_path)
    svc.vault = Vault()
    previous = saved_engine(svc)
    try:
        saved = svc.dispatch('provider_save', {'id': 'engine', 'name': 'Renamed',
            'kind': 'openai-compatible', 'url': 'https://other.example/v1' if changed else previous['url'],
            'credential_ref': 'untrusted-client-reference'})
        for key in ('capabilities', 'context_limit', 'enabled', 'resource_id', 'independent_resource'):
            assert saved[key] == previous[key]
        assert saved.get('credential_ref') == (None if changed else 'saved-reference')
        assert not svc.vault.writes
    finally:
        svc.shutdown()


def test_legacy_saved_provider_test_remains_compatible(tmp_path):
    svc = service(tmp_path)
    try:
        assert svc.dispatch('provider_test', {'id': 'ollama'}) == {
            'ok': True, 'models': [{'name': 'fixture'}]}
    finally:
        svc.shutdown()


def test_changed_target_uses_only_explicit_replacement_key(tmp_path):
    svc = service(tmp_path)
    svc.vault = Vault()
    saved_engine(svc)
    try:
        saved = svc.dispatch('provider_save', {'id': 'engine', 'url': 'https://other.example/v1',
            'api_key': 'new-synthetic-key'})
        assert saved['credential_ref'] == 'replacement-reference'
        assert svc.vault.writes == ['new-synthetic-key'] and not svc.vault.reads
    finally:
        svc.shutdown()


@pytest.mark.parametrize('url', ['http://engine.example:99999/v1', 'http://engine.example\r\n/v1'])
def test_saved_and_tested_endpoints_share_validation(tmp_path, url):
    svc = service(tmp_path)
    try:
        with pytest.raises(ValueError):
            svc.dispatch('provider_save', {'url': url})
        assert svc.store.entities('providers') == []
    finally:
        svc.shutdown()


def test_probe_catalog_does_not_forward_authentication_to_redirects(tmp_path, monkeypatch):
    svc = service(tmp_path)
    client = httpx.Client
    options, requests = [], []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://other.example/models'})
    def factory(**kwargs):
        options.append(kwargs)
        return client(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, 'Client', factory)
    try:
        with pytest.raises(ValueError):
            svc.dispatch('provider_test', {'draft': {'kind': 'openai-compatible', 'url': 'https://engine.example/v1'},
                'api_key': 'synthetic-engine-key'})
        assert len(requests) == 1 and requests[0].url.host == 'engine.example'
        assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False
    finally:
        svc.shutdown()


def test_vault_failure_does_not_expose_private_exception_text(tmp_path, monkeypatch):
    svc = service(tmp_path)
    saved_engine(svc)
    svc.vault = Vault()
    def fail(*args):
        raise RuntimeError('private-local-path synthetic-engine-key')
    monkeypatch.setattr(svc.vault, 'get', fail)
    try:
        with pytest.raises(ValueError, match='could not be read') as captured:
            svc.dispatch('provider_test', {'id': 'engine', 'draft': {
                'kind': 'openai-compatible', 'url': 'https://engine.example/v1'}})
        assert 'private' not in str(captured.value) and 'synthetic' not in str(captured.value)
        monkeypatch.setattr(svc.vault, 'put', fail)
        with pytest.raises(ValueError, match='could not be saved') as captured:
            svc.dispatch('provider_save', {'id': 'engine', 'api_key': 'new-synthetic-key'})
        assert 'private' not in str(captured.value) and 'synthetic' not in str(captured.value)
        assert svc.store.entity('providers', 'engine')['credential_ref'] == 'saved-reference'
    finally:
        svc.shutdown()


@pytest.mark.parametrize('key', [123, ['invalid'], 'unsafe\r\nheader', 'x' * 8193])
def test_test_and_save_reject_invalid_keys_before_vault_or_network(tmp_path, monkeypatch, key):
    svc = service(tmp_path)
    svc.vault = Vault()
    transport(monkeypatch, lambda request: pytest.fail('Invalid key sent a request'))
    try:
        with pytest.raises(ValueError, match='valid API key'):
            svc.dispatch('provider_test', {'draft': {'kind': 'openai-compatible', 'url': 'https://engine.example/v1'},
                'api_key': key})
        with pytest.raises(ValueError, match='valid API key'):
            svc.dispatch('provider_save', {'url': 'https://engine.example/v1', 'api_key': key})
        assert not svc.vault.writes and not svc.store.entities('providers')
    finally:
        svc.shutdown()
