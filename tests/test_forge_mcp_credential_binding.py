"""Connection edits never carry old credentials or evidence to a new target."""
import asyncio
from contextlib import asynccontextmanager
import concurrent.futures
import copy
import threading

import pytest

from forge_integrations import IntegrationHub, MCPConnection


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.reads = []
        self.serial = 0

    def put(self, value, reference=None):
        self.serial += 1
        reference = reference or f'fixture-reference-{self.serial}'
        self.values[reference] = value
        return reference

    def get(self, reference):
        self.reads.append(reference)
        return self.values.get(reference)

    def delete(self, reference):
        self.values.pop(reference, None)


@pytest.fixture
def hub(tmp_path):
    instance = IntegrationHub(tmp_path / 'fixture', MemoryVault())
    yield instance
    instance.shutdown()


def configured(hub, **options):
    saved = hub.dispatch('mcp_save', {'id': 'fixture', 'name': 'Fixture',
        'url': 'https://first.example/mcp', 'token': 'synthetic-bearer',
        'headers': {'X-API-Key': 'synthetic-header', 'Accept': 'application/json'},
        'enabled': True, **options})
    assert saved['ok'], saved
    server = hub._server('fixture')
    server.update(tools=[{'name': 'read', 'description': 'Fixture read', 'inputSchema': {}}],
        status='ready', enabled_tools=['read'], permission_profile='always_ask')
    return server


@pytest.mark.parametrize('change', [
    {'url': 'https://second.example/mcp'},
    {'url': 'https://first.example/other'},
    {'transport': 'sse'},
    {'transport': 'stdio', 'command': 'fixture-executable', 'args': []},
])
def test_target_edits_unlink_all_old_auth_and_catalog_but_keep_permissions(hub, change):
    old = configured(hub)
    old['oauth_tokens_ref'] = hub.vault.put('synthetic-oauth-token')
    old['oauth_client_ref'] = hub.vault.put('synthetic-oauth-client')
    old['oauth_redirect_uri'] = 'http://127.0.0.1:1/callback'
    before_refs = set(hub.vault.values)
    saved = hub.dispatch('mcp_save', {'id': old['id'], **change})
    assert saved['ok'], saved
    current = hub._server(old['id'])
    assert current['tools'] == [] and current['status'] == 'needs_test'
    assert current['enabled'] is False
    assert current['enabled_tools'] == ['read']
    assert current['permission_profile'] == 'always_ask'
    assert not saved['server']['authenticated']
    assert not any(current.get(key) for key in ('credential_ref', 'header_credentials',
        'env_credentials', 'oauth_tokens_ref', 'oauth_client_ref', 'oauth_redirect_uri'))
    assert before_refs <= set(hub.vault.values)
    blocked = hub.dispatch('mcp_save', {'id': old['id'], 'enabled': True})
    assert not blocked['ok'] and 'Test the changed endpoint' in blocked['error']


@pytest.mark.parametrize('change', [{'command': 'other-executable'}, {'args': ['other.py']}, {'cwd': '/other'}])
def test_stdio_credentials_bind_to_command_arguments_and_working_directory(hub, change):
    old = configured(hub, transport='stdio', command='fixture-executable', args=['fixture.py'],
        cwd='/fixture', auth_type='none', env={'API_TOKEN': 'synthetic-env', 'LANG': 'en'})
    saved = hub.dispatch('mcp_save', {'id': old['id'], **change})
    assert saved['ok'], saved
    current = hub._server(old['id'])
    assert not current.get('env_credentials')
    assert current['env'] == {}
    assert not current['enabled'] and current['tools'] == []


def test_unchanged_identity_preserves_bound_secrets_and_tool_preferences(hub):
    old = configured(hub, transport='stdio', command='fixture-executable', args=['fixture.py'],
        cwd='/fixture', auth_type='none', env={'API_TOKEN': 'synthetic-env', 'LANG': 'en'})
    references = copy.deepcopy(old)
    values = dict(hub.vault.values)
    saved = hub.dispatch('mcp_save', {'id': old['id'], 'name': 'Renamed', 'enabled_tools': []})
    assert saved['ok']
    current = hub._server(old['id'])
    assert current['env_credentials'] == references['env_credentials']
    assert current['credential_ref'] == references['credential_ref']
    assert current['header_credentials'] == references['header_credentials']
    assert current['tools'] == references['tools'] and current['status'] == 'ready'
    assert current['enabled_tools'] == []
    assert hub.vault.values == values


def test_changing_one_connection_does_not_delete_credentials_still_owned_by_another(hub):
    old = configured(hub)
    other = {**copy.deepcopy(old), 'id': 'other-fixture'}
    hub.config['servers'].append(other)
    values = dict(hub.vault.values)
    saved = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp'})
    assert saved['ok'] and not saved['server']['authenticated']
    assert hub.vault.values == values
    assert hub._public_server(other)['authenticated']


@pytest.mark.parametrize('change', [{'url': 'https://second.example/mcp'},
    {'auth_type': 'none'}, {'token': 'new-synthetic-token'}, {'clear_token': True}])
def test_mcp_edits_preserve_encrypted_credentials_owned_by_providers_and_github(hub, change):
    old = configured(hub)
    reference = old['credential_ref']
    # These owners intentionally share the operating-system vault namespace.
    provider = {'credential_ref': reference}
    github = {'credential_ref': reference}
    result = hub.dispatch('mcp_save', {'id': old['id'], **change})
    assert result['ok'], result
    assert hub.vault.get(provider['credential_ref']) == 'synthetic-bearer'
    assert hub.vault.get(github['credential_ref']) == 'synthetic-bearer'
    if 'auth_type' not in change:
        assert hub._server(old['id']).get('credential_ref') != reference


def test_restoring_configuration_backup_can_still_resolve_its_original_credentials(hub, monkeypatch):
    original = configured(hub)
    backup = copy.deepcopy(original)
    result = hub.dispatch('mcp_save', {'id': original['id'], 'url': 'https://second.example/mcp',
        'token': 'new-synthetic-token'})
    assert result['ok']
    assert hub._server(original['id'])['credential_ref'] != backup['credential_ref']
    # Restore only the configuration snapshot. The encrypted vault is unchanged;
    # a rollback must not need plaintext credentials copied into the backup.
    hub.config['servers'] = [backup]
    hub._save()
    restored = IntegrationHub(hub.root, hub.vault)
    try:
        saved = restored._server(backup['id'])
        captured = outbound(monkeypatch, saved, restored.vault)
        assert saved['url'] == 'https://first.example/mcp'
        assert captured['headers']['Authorization'] == 'Bearer synthetic-bearer'
        assert captured['headers']['X-API-Key'] == 'synthetic-header'
    finally:
        restored.shutdown()


def test_unused_credentials_created_by_the_current_save_are_cleaned_up(hub):
    old = configured(hub)
    before = dict(hub.vault.values)
    result = hub.dispatch('mcp_save', {'id': old['id'], 'token': 'new-unused-synthetic-token', 'clear_token': True})
    assert result['ok']
    assert 'credential_ref' not in hub._server(old['id'])
    assert hub.vault.values == before


@pytest.mark.parametrize('change', [{'auth_type': 'none'}, {'token': 'new-synthetic-token'},
    {'clear_token': True}, {'headers': {'X-Account': 'different-account'}}, {'env': {'ACCOUNT': 'different-account'}}])
def test_changed_authentication_or_transport_configuration_needs_fresh_catalog(hub, change):
    server = configured(hub, **({'transport': 'stdio', 'command': 'fixture-executable', 'args': []} if 'env' in change else {}))
    result = hub.dispatch('mcp_save', {'id': server['id'], **change})
    assert result['ok'], result
    current = hub._server(server['id'])
    assert current['tools'] == [] and current['status'] == 'needs_test'
    assert not current['enabled']
    assert current['enabled_tools'] == ['read']
    assert not hub.dispatch('mcp_save', {'id': server['id'], 'enabled': True})['ok']


def test_new_target_discards_all_inherited_maps_but_keeps_explicit_new_values(hub):
    old = configured(hub)
    old['headers']['X-Unknown-Account'] = 'old-synthetic-sensitive-value'
    old['env'] = {'UNCLASSIFIED': 'old-synthetic-sensitive-value'}
    result = hub.dispatch('mcp_save', {'id': old['id'], 'transport': 'stdio', 'command': 'fixture-executable',
        'args': [], 'headers': {'X-New-Account': 'new-synthetic-value'}, 'env': {'NEW_SETTING': 'new-synthetic-value'}})
    assert result['ok'], result
    current = hub._server(old['id'])
    assert current['headers'] == {'X-New-Account': 'new-synthetic-value'}
    assert current['env'] == {'NEW_SETTING': 'new-synthetic-value'}
    assert current['permission_profile'] == 'always_ask'


def outbound(monkeypatch, config, vault):
    """Capture the actual headers/environment passed to the official SDK transport."""
    import mcp
    import mcp.client.sse
    import mcp.client.stdio
    import mcp.client.streamable_http
    import httpx2

    captured = {}
    connection = MCPConnection.__new__(MCPConnection)
    connection.config = copy.deepcopy(config)
    connection.vault = vault
    connection.oauth_flow = None
    connection.ready = concurrent.futures.Future()
    connection.pending = set()
    connection.closed = False

    @asynccontextmanager
    async def streams(*args, **kwargs):
        if config['transport'] == 'stdio':
            captured['env'] = args[0].env
        elif config['transport'] == 'sse':
            captured['headers'] = kwargs['headers']
        yield (object(), object())

    class Client:
        def __init__(self, **kwargs):
            captured['headers'] = kwargs['headers']

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            connection.queue.put_nowait(None)

    monkeypatch.setattr(mcp, 'ClientSession', Session)
    monkeypatch.setattr(mcp.client.sse, 'sse_client', streams)
    monkeypatch.setattr(mcp.client.stdio, 'stdio_client', streams)
    monkeypatch.setattr(mcp.client.streamable_http, 'streamable_http_client', streams)
    monkeypatch.setattr(httpx2, 'AsyncClient', Client)
    asyncio.run(connection._serve())
    return captured


@pytest.mark.parametrize('transport', ['http', 'sse'])
def test_none_never_forwards_automatic_or_authorization_credentials(hub, monkeypatch, transport):
    server = configured(hub, transport=transport)
    server['header_credentials']['Authorization'] = hub.vault.put('synthetic-legacy-authorization')
    server['headers']['Authorization'] = 'synthetic-legacy-plaintext'
    saved = hub.dispatch('mcp_save', {'id': server['id'], 'auth_type': 'none'})
    assert saved['ok']
    current = hub._server(server['id'])
    hub.vault.reads.clear()
    captured = outbound(monkeypatch, current, hub.vault)
    assert 'Authorization' not in captured['headers']
    # Explicit API-key headers remain usable for the same bound target.
    assert captured['headers']['X-API-Key'] == 'synthetic-header'
    assert current['credential_ref'] not in hub.vault.reads
    assert current['header_credentials']['Authorization'] not in hub.vault.reads


def test_none_still_supports_explicitly_bound_stdio_environment(hub, monkeypatch):
    server = configured(hub, transport='stdio', command='fixture-executable', args=[],
        auth_type='none', env={'API_TOKEN': 'synthetic-env'})
    captured = outbound(monkeypatch, server, hub.vault)
    assert captured['env'] == {'API_TOKEN': 'synthetic-env'}


@pytest.mark.parametrize('transport', ['http', 'sse'])
def test_new_target_transport_receives_only_fresh_explicit_credentials(hub, monkeypatch, transport):
    old = configured(hub)
    old_refs = set(hub.vault.values)
    saved = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp',
        'transport': transport, 'token': 'new-synthetic-bearer',
        'headers': {'X-API-Key': 'new-synthetic-header'}})
    assert saved['ok'], saved
    current = hub._server(old['id'])
    captured = outbound(monkeypatch, current, hub.vault)
    assert captured['headers'] == {'Authorization': 'Bearer new-synthetic-bearer', 'X-API-Key': 'new-synthetic-header'}
    assert old_refs <= set(hub.vault.values)
    assert not old_refs.intersection(hub.vault.reads)
    assert not current['enabled']


def test_active_request_blocks_edits_without_losing_credentials_or_configuration(hub):
    old = configured(hub)
    snapshot = copy.deepcopy(old)
    values = dict(hub.vault.values)
    class BusyConnection:
        closed = False
        pending = {concurrent.futures.Future()}
        def shutdown(self):
            pytest.fail('An active request was interrupted by an endpoint edit')
    hub.connections[old['id']] = BusyConnection()
    try:
        result = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp', 'token': 'new-synthetic'})
        assert not result['ok'] and 'active tool request' in result['error']
        assert hub._server(old['id']) == snapshot
        assert hub.vault.values == values
    finally:
        hub.connections.pop(old['id'])


def test_target_edit_cancels_oauth_before_closing_old_connection(hub):
    old = configured(hub)
    calls = []
    class Flow:
        def shutdown(self):
            calls.append('auth cancelled')
    class Connection:
        closed = False
        pending = set()
        def shutdown(self):
            calls.append('connection closed')
    hub.auth_flows[old['id']] = Flow()
    hub.connections[old['id']] = Connection()
    saved = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp'})
    assert saved['ok']
    assert calls == ['auth cancelled', 'connection closed']
    assert old['id'] not in hub.auth_flows and old['id'] not in hub.connections
    with pytest.raises(ValueError, match='endpoint changed'):
        hub._connection(old)


def test_shutdown_also_cancels_authentication_that_has_no_connection_yet(hub):
    calls = []
    class Flow:
        def shutdown(self):
            calls.append('auth cancelled')
    hub.auth_flows['fixture'] = Flow()
    hub.shutdown()
    assert calls == ['auth cancelled'] and not hub.auth_flows


def test_new_oauth_sign_in_discards_old_account_catalog_until_ready(hub, monkeypatch):
    old = configured(hub)
    class Flow:
        redirect_uri = 'http://127.0.0.1:1/callback'
        closed = False
        def shutdown(self):
            self.closed = True
    class Connection:
        closed = False
        pending = set()
        ready = concurrent.futures.Future()
        ready.set_result(True)
        def __init__(self, server, *args, **kwargs):
            assert not server['enabled'] and server['tools'] == []
        def request(self, *args):
            return [{'name': 'new_account_tool'}]
        def shutdown(self):
            self.closed = True
    monkeypatch.setattr('forge_integrations.OAuthFlow', Flow)
    monkeypatch.setattr('forge_integrations.MCPConnection', Connection)
    assert hub.dispatch('mcp_auth_start', {'id': old['id']})['ok']
    assert not old['enabled'] and old['tools'] == [] and old['status'] == 'authenticating'
    assert not hub.dispatch('mcp_save', {'id': old['id'], 'enabled': True})['ok']
    ready = hub.dispatch('mcp_auth_status', {'id': old['id']})
    assert ready['state'] == 'ready'
    assert [tool['name'] for tool in ready['server']['tools']] == ['new_account_tool']
    assert hub.dispatch('mcp_save', {'id': old['id'], 'enabled': True})['server']['enabled']


def test_oauth_sign_in_does_not_interrupt_an_active_tool_request(hub):
    old = configured(hub)
    snapshot = copy.deepcopy(old)
    class Connection:
        closed = False
        pending = {concurrent.futures.Future()}
        def shutdown(self):
            pytest.fail('OAuth interrupted an active request')
    hub.connections[old['id']] = Connection()
    try:
        result = hub.dispatch('mcp_auth_start', {'id': old['id']})
        assert not result['ok'] and 'active tool request' in result['error']
        assert hub._server(old['id']) == snapshot and not hub.auth_flows
    finally:
        hub.connections.pop(old['id'])


@pytest.mark.parametrize('failure', [False, True])
def test_cancelled_old_test_never_changes_or_closes_a_replacement_connection(hub, monkeypatch, failure):
    configured(hub)
    started, finish = threading.Event(), threading.Event()
    class Connection:
        closed = False
        pending = set()
        def __init__(self, *args, **kwargs):
            pass
        def request(self, *args):
            started.set()
            assert finish.wait(5)
            if failure:
                raise RuntimeError('Old fixture failure')
            return [{'name': 'old-test-tool'}]
        def shutdown(self):
            self.closed = True
    monkeypatch.setattr('forge_integrations.MCPConnection', Connection)
    result = []
    worker = threading.Thread(target=lambda: result.append(hub.dispatch('mcp_test', {'id': 'fixture'})))
    worker.start()
    try:
        assert started.wait(5)
        assert hub.dispatch('mcp_auth_cancel', {'id': 'fixture'})['ok']
        assert hub.dispatch('mcp_save', {'id': 'fixture', 'url': 'https://second.example/mcp'})['ok']
        replacement = Connection()
        hub.connections['fixture'] = replacement
        finish.set()
        worker.join(5)
        assert result and not result[0]['ok'] and 'changed' in result[0]['error']
        assert hub._server('fixture')['tools'] == []
        assert hub._server('fixture')['status'] == 'needs_test'
        assert hub.connections['fixture'] is replacement and not replacement.closed
    finally:
        finish.set()
        worker.join(5)


def test_failed_save_rolls_back_new_vault_entries_without_changing_old_links(hub, monkeypatch):
    old = configured(hub)
    snapshot = copy.deepcopy(old)
    values = dict(hub.vault.values)
    monkeypatch.setattr(hub, '_save', lambda: (_ for _ in ()).throw(RuntimeError('Private path and secret detail')))
    result = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp', 'token': 'new-synthetic'})
    assert not result['ok'] and 'securely' in result['error']
    assert 'Private' not in result['error']
    assert hub._server(old['id']) == snapshot and hub.vault.values == values


@pytest.mark.parametrize('token', ['synthetic\r\ninjection', 7, 'x' * 8193])
def test_invalid_tokens_never_reach_vault_or_change_existing_target(hub, token):
    old = configured(hub)
    values = dict(hub.vault.values)
    snapshot = copy.deepcopy(old)
    result = hub.dispatch('mcp_save', {'id': old['id'], 'url': 'https://second.example/mcp', 'token': token})
    assert not result['ok'] and 'valid access token' in result['error']
    assert hub._server(old['id']) == snapshot and hub.vault.values == values


@pytest.mark.parametrize('error_type', [ValueError, FileNotFoundError])
def test_remote_sdk_errors_never_echo_private_detail_into_ready_or_pending_results(error_type):
    connection = MCPConnection.__new__(MCPConnection)
    connection.ready = concurrent.futures.Future()
    pending = concurrent.futures.Future()
    connection.pending = {pending}
    connection.closed = False
    async def fail():
        raise error_type('synthetic-secret-value private-path')
    connection._serve = fail
    connection._thread_main()
    for future in (connection.ready, pending):
        with pytest.raises(RuntimeError) as captured:
            future.result()
        assert 'synthetic-secret-value' not in str(captured.value)
        assert 'private-path' not in str(captured.value)
        assert error_type.__name__ in str(captured.value)
