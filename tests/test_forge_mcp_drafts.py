"""Ephemeral setup checks cannot reconfigure a user's live MCP connection."""
import concurrent.futures
import copy
import threading

import pytest

from forge_integrations import IntegrationHub
from test_forge_mcp_credential_binding import MemoryVault, configured


@pytest.fixture
def hub(tmp_path):
    instance = IntegrationHub(tmp_path / 'fixture', MemoryVault())
    yield instance
    instance.shutdown()


@pytest.fixture
def connections(monkeypatch):
    created = []
    class Connection:
        def __init__(self, config, vault, **kwargs):
            self.config = copy.deepcopy(config)
            self.vault = vault
            self.closed = False
            self.pending = set()
            self.ready = concurrent.futures.Future()
            self.ready.set_result(True)
            created.append(self)
        def request(self, operation, arguments=None, **kwargs):
            assert operation == 'list_tools'
            return [{'name': 'lookup', 'description': 'Synthetic research tool', 'inputSchema': {}}]
        def shutdown(self):
            self.closed = True
    monkeypatch.setattr('forge_integrations.MCPConnection', Connection)
    return created, Connection


def probe(hub, **values):
    return hub.dispatch('mcp_probe', {'probe_id': 'synthetic-check', 'draft': {
        'transport': 'http', 'url': 'https://first.example/mcp', 'auth_type': 'bearer'}, **values})


def test_draft_token_is_memory_only_and_discarded_after_check(hub, connections):
    created, _ = connections
    before = hub.config_path.read_bytes()
    result = probe(hub, token='synthetic-draft-token')
    assert result['ok'] and result['tool_count'] == 1
    assert result['tools'] == [{'name': 'lookup', 'description': 'Synthetic research tool'}]
    assert hub.config_path.read_bytes() == before and not hub.config['servers']
    assert not hub.vault.values and not hub.connections and not hub.mcp_probes
    assert created[0].closed
    # The ephemeral closure releases the entered token even if a reference survives.
    assert created[0].vault.get(created[0].config['credential_ref']) is None
    with pytest.raises(ValueError, match='Save this connection'):
        created[0].vault.put('synthetic-token')


def test_check_does_not_close_or_disable_the_saved_connection(hub, connections):
    created, Connection = connections
    old = configured(hub)
    live = Connection(old, hub.vault)
    hub.connections['fixture'] = live
    before = copy.deepcopy(old), hub.config_path.read_bytes(), dict(hub.vault.values)
    assert probe(hub, id='fixture')['ok']
    assert hub._server('fixture') is old and old == before[0] and old['enabled']
    assert hub.config_path.read_bytes() == before[1] and hub.vault.values == before[2]
    assert hub.connections['fixture'] is live and not live.closed
    assert created[-1].closed and created[-1] is not live


@pytest.mark.parametrize('change', [None, {'url': 'https://second.example/mcp'},
    {'transport': 'sse'}, {'auth_type': 'none'}])
def test_saved_credentials_reused_only_for_exact_target_and_auth(hub, connections, change):
    created, _ = connections
    old = configured(hub)
    result = probe(hub, id='fixture', draft={'transport': 'http',
        'url': 'https://first.example/mcp', 'auth_type': 'bearer', **(change or {})})
    assert result['ok']
    candidate = created[0].config
    if change:
        assert not candidate.get('credential_ref') and not candidate.get('header_credentials')
        assert created[0].vault.get(old['credential_ref']) is None
    else:
        assert candidate['credential_ref'] == old['credential_ref']
        assert created[0].vault.get(old['credential_ref']) == 'synthetic-bearer'
        assert candidate['headers'] == old['headers']
    assert old['enabled']


@pytest.mark.parametrize('change', [None, {'command': 'other'}, {'args': ['other.py']}, {'cwd': '/other'}])
def test_stdio_checks_bind_environment_to_exact_executable(hub, connections, change):
    created, _ = connections
    old = configured(hub, transport='stdio', command='synthetic-executable', args=['fixture.py'],
        cwd='/fixture', auth_type='none', env={'API_TOKEN': 'synthetic-env'})
    result = hub.dispatch('mcp_probe', {
        'probe_id': 'stdio-check', 'id': 'fixture', 'draft': {'transport': 'stdio',
        'command': 'synthetic-executable', 'args': ['fixture.py'], 'cwd': '/fixture', 'auth_type': 'none', **(change or {})}})
    assert result['ok']
    refs = created[0].config.get('env_credentials', {})
    assert bool(refs) is (change is None)
    assert bool(created[0].vault.get(old['env_credentials']['API_TOKEN'])) is (change is None)


@pytest.mark.parametrize('draft,token', [({}, None), ({'url': 'https://user:synthetic@example.com'}, None),
    ({'url': 'https://example.com?api_key=synthetic'}, None), ({'url': 'https://example.com', 'auth_type': 'oauth'}, None),
    ({'transport': 'stdio', 'command': '', 'args': []}, None),
    ({'url': 'https://example.com'}, 'synthetic\nheader'), ({'url': 'https://example.com'}, 123)])
def test_invalid_drafts_fail_before_sdk_creation(hub, connections, draft, token):
    created, _ = connections
    result = probe(hub, draft=draft, token=token)
    assert not result['ok'] and 'synthetic' not in result['error']
    assert not created and not hub.mcp_probes and not hub.vault.values


@pytest.mark.parametrize('failure', [ValueError('synthetic-secret https://private.example?token=synthetic'),
    FileNotFoundError('synthetic-secret'), concurrent.futures.TimeoutError()])
def test_check_remote_errors_are_sanitized_and_cleanup_is_complete(hub, connections, failure):
    created, Connection = connections
    Connection.request = lambda *args, **kwargs: (_ for _ in ()).throw(failure)
    result = probe(hub, token='synthetic-only')
    assert not result['ok'] and 'synthetic' not in result['error'] and 'private.example' not in result['error']
    assert created[0].closed and not hub.mcp_probes and not hub.vault.values


def test_cancel_before_rpc_admission_never_opens_a_connection(hub, connections):
    created, _ = connections
    assert hub.dispatch('mcp_probe_cancel', {'probe_id': 'synthetic-check'})['ok']
    result = probe(hub)
    assert not result['ok'] and 'cancelled' in result['error']
    assert not created


def test_cancel_active_check_closes_only_ephemeral_connection(hub, connections):
    created, Connection = connections
    old = configured(hub)
    live = Connection(old, hub.vault)
    hub.connections['fixture'] = live
    entered, release = threading.Event(), threading.Event()
    def request(self, *args, cancel=None, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancel.is_set()
        return []
    Connection.request = request
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(probe, hub, id='fixture')
        assert entered.wait(3)
        assert hub.dispatch('mcp_probe_cancel', {'probe_id': 'synthetic-check'})['ok']
        release.set()
        result = future.result(timeout=3)
    assert not result['ok'] and 'cancelled' in result['error']
    assert not live.closed and old['enabled'] and created[-1].closed and not hub.mcp_probes


@pytest.mark.parametrize('duplicate', [False, True])
def test_active_check_admission_is_bounded(hub, connections, duplicate):
    created, _ = connections
    for index in range(1 if duplicate else 8):
        hub.mcp_probes['synthetic-check' if duplicate else str(index)] = {'cancel': threading.Event(), 'connection': None}
    result = probe(hub)
    assert not result['ok'] and 'already running' in result['error'] and not created


@pytest.fixture
def flows(monkeypatch):
    created = []
    class Flow:
        redirect_uri = 'http://127.0.0.1:1/callback'
        authorization_url = 'https://identity.example/signin'
        closed = False
        def __init__(self):
            created.append(self)
        def shutdown(self):
            self.closed = True
    monkeypatch.setattr('forge_integrations.OAuthFlow', Flow)
    return created


def test_stale_auth_cancel_and_status_cannot_touch_new_flow(hub, connections, flows):
    configured(hub)
    first = hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'first'})
    second = hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'second'})
    assert first['flow_id'] == 'first' and second['flow_id'] == 'second'
    current = hub.connections['fixture']
    assert hub.dispatch('mcp_auth_cancel', {'id': 'fixture', 'flow_id': 'first'}) == {'ok': True, 'stale': True}
    status = hub.dispatch('mcp_auth_status', {'id': 'fixture', 'flow_id': 'first'})
    assert not status['ok'] and hub.connections['fixture'] is current and not current.closed
    assert not flows[-1].closed
    assert hub.dispatch('mcp_auth_cancel', {'id': 'fixture', 'flow_id': 'second'})['ok']
    assert current.closed and flows[-1].closed


def test_invalid_flow_identity_does_not_close_live_connection_or_create_listener(hub, connections, flows):
    _, Connection = connections
    old = configured(hub)
    live = Connection(old, hub.vault)
    hub.connections['fixture'] = live
    assert not hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'invalid/id'})['ok']
    assert not live.closed and not flows and old['enabled']


def test_client_draft_cannot_supply_vault_links_or_copy_private_transport_fields(hub, connections):
    created, _ = connections
    reference = hub.vault.put('synthetic-unrelated-provider-key')
    result = probe(hub, draft={'transport': 'http', 'url': 'https://first.example/mcp',
        'auth_type': 'bearer', 'credential_ref': reference, 'header_credentials': {'X-API-Key': reference},
        'headers': {'Authorization': 'synthetic-old-key'}, 'env': {'API_KEY': 'synthetic-old-key'}})
    assert result['ok']
    assert not any(created[0].config.get(key) for key in ('credential_ref', 'header_credentials', 'headers', 'env'))
    assert created[0].vault.get(reference) is None and not hub.vault.reads


def test_auth_constructor_failure_releases_listener_and_sanitizes_private_details(hub, connections, flows, monkeypatch):
    configured(hub)
    monkeypatch.setattr('forge_integrations.MCPConnection', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('synthetic-private-key')))
    result = hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'first'})
    assert not result['ok'] and 'synthetic' not in result['error']
    assert flows[0].closed and not hub.auth_flows and not hub.connections
    assert not hub._server('fixture')['enabled'] and hub._server('fixture')['status'] == 'error'


@pytest.mark.parametrize('action', ['mcp_test', 'mcp_auth_status'])
def test_explicit_setup_requests_never_echo_sdk_exception_secrets(hub, connections, flows, action):
    _, Connection = connections
    configured(hub)
    if action == 'mcp_auth_status':
        assert hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'first'})['ok']
    Connection.request = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('synthetic-credential'))
    result = hub.dispatch(action, {'id': 'fixture'})
    assert not result['ok'] and 'synthetic' not in result['error']


@pytest.mark.parametrize('change', [{'url': 'https://second.example/mcp'}, {'headers': {'X-Account': 'new-account'}},
    {'token': 'synthetic-new-account'}, {'name': 'New revision'}, {'enabled_tools': []}])
def test_delayed_enable_cannot_activate_a_new_verified_revision(hub, connections, change):
    configured(hub)
    first = hub.dispatch('mcp_test', {'id': 'fixture'})
    assert first['ok'] and first['setup_token']
    assert hub.dispatch('mcp_save', {'id': 'fixture', **change, 'enabled': False})['ok']
    second = hub.dispatch('mcp_test', {'id': 'fixture'})
    assert second['ok'] and second['setup_token'] != first['setup_token']
    newer = hub._server('fixture')
    assert newer['status'] == 'ready' and not newer['enabled']
    before = hub.config_path.read_bytes()
    stale = hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': True, 'setup_token': first['setup_token']})
    assert not stale['ok'] and 'changed during setup' in stale['error']
    assert hub._server('fixture') is newer and not newer['enabled'] and hub.config_path.read_bytes() == before
    valid = hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': True, 'setup_token': second['setup_token']})
    assert valid['ok'] and valid['server']['enabled']
    assert 'setup_token' not in hub.config_path.read_text() and not hub.mcp_setup_tokens


def test_setup_receipt_expires_and_requires_a_new_check(hub, connections):
    configured(hub)
    tested = hub.dispatch('mcp_test', {'id': 'fixture'})
    hub.mcp_setup_tokens['fixture']['expires'] = 0
    assert not hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': True, 'setup_token': tested['setup_token']})['ok']
    assert hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': False})['ok']
    assert hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': True})['ok']  # Existing explicit list toggles remain compatible.


def test_oauth_readiness_issues_the_same_activation_precondition(hub, connections, flows):
    configured(hub)
    assert hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'first'})['ok']
    ready = hub.dispatch('mcp_auth_status', {'id': 'fixture', 'flow_id': 'first'})
    assert ready['ok'] and ready['state'] == 'ready' and ready['setup_token']
    assert hub.dispatch('mcp_save', {'id': 'fixture', 'enabled': True, 'setup_token': ready['setup_token']})['ok']


@pytest.mark.parametrize('action', ['mcp_servers', 'mcp_auth_status'])
def test_oauth_credential_status_never_echoes_vault_failure_values(hub, connections, flows, monkeypatch, action):
    configured(hub)
    assert hub.dispatch('mcp_auth_start', {'id': 'fixture', 'flow_id': 'first'})['ok']
    monkeypatch.setattr(hub.vault, 'get', lambda *args: (_ for _ in ()).throw(ValueError('synthetic-credential-value')))
    result = hub.dispatch(action, {'id': 'fixture', 'flow_id': 'first'})
    assert not result['ok'] and 'synthetic' not in result['error']
    assert 'Check credential storage' in result['error']


@pytest.mark.parametrize('stop', ['shutdown', 'cancel'])
def test_late_validation_cannot_start_a_connection_after_shutdown_or_cancel(hub, connections, monkeypatch, stop):
    import forge_integrations
    created, _ = connections
    entered, release = threading.Event(), threading.Event()
    original = forge_integrations._safe_url
    def validate(value):
        entered.set()
        assert release.wait(3)
        return original(value)
    monkeypatch.setattr(forge_integrations, '_safe_url', validate)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(probe, hub)
        assert entered.wait(3)
        if stop == 'shutdown': hub.shutdown()
        else: assert hub.dispatch('mcp_probe_cancel', {'probe_id': 'synthetic-check'})['ok']
        release.set()
        result = future.result(timeout=3)
    assert not result['ok'] and not created and not hub.mcp_probes


@pytest.mark.parametrize('stop', ['shutdown', 'cancel'])
def test_admitted_probe_is_published_before_shutdown_or_cancel_can_return(hub, connections, monkeypatch, stop):
    created, Connection = connections
    entered, release = threading.Event(), threading.Event()
    waiting, stopped = threading.Event(), threading.Event()
    original = Connection.__init__
    def construct(self, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        original(self, *args, **kwargs)
    monkeypatch.setattr(Connection, '__init__', construct)
    def halt():
        waiting.set()
        if stop == 'shutdown': hub.shutdown()
        else: hub.dispatch('mcp_probe_cancel', {'probe_id': 'synthetic-check'})
        stopped.set()
    with concurrent.futures.ThreadPoolExecutor() as executor:
        checked = executor.submit(probe, hub)
        assert entered.wait(3)
        stopping = executor.submit(halt)
        assert waiting.wait(3)
        assert not stopped.wait(.05)
        release.set()
        stopping.result(timeout=3)
        checked.result(timeout=3)
    assert created and created[0].closed and stopped.is_set() and not hub.mcp_probes


def test_probe_is_rejected_before_validation_on_an_already_closed_hub(hub, connections, monkeypatch):
    created, _ = connections
    hub.shutdown()
    monkeypatch.setattr('forge_integrations._safe_url', lambda *args: pytest.fail('No validation or SDK work after shutdown'))
    assert not probe(hub)['ok'] and not created


@pytest.mark.parametrize('action', ['mcp_test', 'mcp_auth_start', 'mcp_resources'])
def test_closed_hub_cannot_admit_other_queued_setup_sessions(hub, connections, flows, action):
    created, _ = connections
    configured(hub)
    hub.shutdown()
    result = hub.dispatch(action, {'id': 'fixture', 'flow_id': 'late-start'})
    assert not result['ok'] and 'Forge is closing' in result['error']
    assert not created and not flows and not hub.connections and not hub.auth_flows
