"""MCP permission and account checks remain current through request admission."""
import concurrent.futures
import copy
import threading
import time

import pytest

from forge_integrations import IntegrationHub
from test_forge_mcp_credential_binding import MemoryVault


@pytest.fixture
def hub(tmp_path):
    instance = IntegrationHub(tmp_path / 'fixture', MemoryVault())
    result = instance.dispatch('mcp_save', {'id': 'fixture', 'url': 'https://first.example/mcp',
        'token': 'synthetic-token', 'headers': {'X-Account': 'old-account'}, 'enabled': True})
    assert result['ok']
    instance._server('fixture').update(status='ready', tools=[{'name': 'write', 'inputSchema': {}}])
    yield instance
    instance.shutdown()


def invocation(hub, kind):
    if kind == 'resource':
        return 'mcp__fixture__read_resource', {'uri': 'fixture://text'}
    return hub.tool_name('fixture', 'write'), {}


def test_cached_connection_cannot_serve_a_different_current_account_configuration(hub):
    server = hub._server('fixture')
    class Connection:
        closed = False
        config = copy.deepcopy(server)
        def shutdown(self):
            self.closed = True
    hub.connections['fixture'] = Connection()
    server['headers']['X-Account'] = 'new-account'
    result = hub.execute(hub.tool_name('fixture', 'write'), {})
    assert not result['ok'] and result['not_executed'] and not result['outcome_unknown']
    assert not hub.mcp_requests


@pytest.mark.parametrize('kind', ['tool', 'resource'])
@pytest.mark.parametrize('change', [{'headers': {'X-Account': 'new-account'}},
    {'enabled': False}, {'enabled_tools': []}])
def test_old_permission_check_cannot_send_a_request_using_a_replacement_descriptor(hub, monkeypatch, kind, change):
    old = hub._server('fixture')
    entered, release = threading.Event(), threading.Event()
    created, requests, results = [], [], []
    hook = '_server_enabled' if kind == 'resource' else '_tool_enabled'
    original = getattr(hub, hook)
    worker = None
    def paused(server, *args):
        allowed = original(server, *args)
        if server is old and threading.current_thread() is worker:
            entered.set()
            assert release.wait(5)
        return allowed
    class Connection:
        closed = False
        ready = concurrent.futures.Future()
        ready.set_result(True)
        def __init__(self, config, *args):
            created.append(config['headers'].get('X-Account'))
        def request(self, *args, **kwargs):
            requests.append(args)
            return {'content': []}
        def shutdown(self):
            self.closed = True
    monkeypatch.setattr(hub, hook, paused)
    monkeypatch.setattr('forge_integrations.MCPConnection', Connection)
    name, args = invocation(hub, kind)
    worker = threading.Thread(target=lambda: results.append(hub.execute(name, args)))
    worker.start()
    try:
        assert entered.wait(5)
        assert hub.dispatch('mcp_save', {'id': 'fixture', **change})['ok']
        release.set()
        worker.join(5)
        assert results and not results[0]['ok']
        assert results[0]['not_executed'] and not results[0]['outcome_unknown']
        assert not created and not requests and not hub.mcp_requests
    finally:
        release.set()
        worker.join(5)


@pytest.mark.parametrize('kind', ['tool', 'resource'])
def test_admitted_request_blocks_same_connection_edits_without_holding_lock_over_readiness(hub, kind):
    requests, results = [], []
    class Connection:
        closed = False
        pending = set()
        ready = concurrent.futures.Future()
        def request(self, *args, **kwargs):
            requests.append(args)
            return {'content': [{'type': 'text', 'text': 'old-account result'}]}
        def shutdown(self):
            self.closed = True
    connection = Connection()
    hub.connections['fixture'] = connection
    name, args = invocation(hub, kind)
    worker = threading.Thread(target=lambda: results.append(hub.execute(name, args)))
    worker.start()
    try:
        for _ in range(500):
            with hub.lock:
                admitted = hub.mcp_requests.get('fixture')
            if admitted:
                break
            time.sleep(.005)
        assert admitted
        # No SDK future exists yet: the coordinator claim closes that race.
        assert not connection.pending
        for update in ({'headers': {'X-Account': 'new-account'}}, {'enabled': False}, {'enabled_tools': []}):
            result = hub.dispatch('mcp_save', {'id': 'fixture', **update})
            assert not result['ok'] and 'active tool request' in result['error']
        assert hub.dispatch('mcp_save', {'id': 'unrelated', 'url': 'https://other.example/mcp'})['ok']
        connection.ready.set_result(True)
        worker.join(5)
        assert results and results[0]['ok'] and len(requests) == 1
        assert not hub.mcp_requests
        assert hub.dispatch('mcp_save', {'id': 'fixture', 'headers': {'X-Account': 'new-account'}})['ok']
    finally:
        if not connection.ready.done():
            connection.ready.set_result(True)
        worker.join(5)


@pytest.mark.parametrize('change', [
    {'headers': {'X-Account': 'new-account'}},
    {'env': {'ACCOUNT': 'new-account'}},
    {'header_credentials': {'X-New-Key': 'same-private-reference'}},
    {'env_credentials': {'NEW_API_KEY': 'same-private-reference'}},
])
def test_readiness_revalidates_complete_account_binding_including_mapped_credential_names(hub, change):
    server = hub._server('fixture')
    server['header_credentials'] = {'X-Old-Key': 'same-private-reference'}
    server['env_credentials'] = {'OLD_API_KEY': 'same-private-reference'}
    requests, results = [], []
    class Connection:
        closed = False
        ready = concurrent.futures.Future()
        def request(self, *args, **kwargs):
            requests.append(args)
            return {'content': []}
        def shutdown(self):
            self.closed = True
    connection = Connection()
    hub.connections['fixture'] = connection
    worker = threading.Thread(target=lambda: results.append(hub.execute(hub.tool_name('fixture', 'write'), {})))
    worker.start()
    try:
        for _ in range(500):
            with hub.lock:
                admitted = hub.mcp_requests.get('fixture')
            if admitted:
                break
            time.sleep(.005)
        assert admitted
        # An in-place backend update must also fail closed, not just a replaced object.
        with hub.lock:
            server.update(copy.deepcopy(change))
        connection.ready.set_result(True)
        worker.join(5)
        assert results and results[0]['not_executed'] and not results[0]['outcome_unknown']
        assert not requests and not hub.mcp_requests
    finally:
        if not connection.ready.done():
            connection.ready.set_result(True)
        worker.join(5)


@pytest.mark.parametrize('kind', ['tool', 'resource'])
def test_plugin_permission_revocation_while_waiting_for_readiness_prevents_dispatch(hub, kind):
    hub.config['plugins'].append({'id': 'owner', 'enabled': True, 'server_ids': ['fixture']})
    results, requests = [], []
    class Connection:
        closed = False
        ready = concurrent.futures.Future()
        def request(self, *args, **kwargs):
            requests.append(args)
            return {'content': []}
        def shutdown(self):
            self.closed = True
    connection = Connection()
    hub.connections['fixture'] = connection
    name, args = invocation(hub, kind)
    worker = threading.Thread(target=lambda: results.append(hub.execute(name, args)))
    worker.start()
    try:
        for _ in range(500):
            with hub.lock:
                admitted = hub.mcp_requests.get('fixture')
            if admitted:
                break
            time.sleep(.005)
        assert admitted
        assert hub.dispatch('plugin_toggle', {'id': 'owner', 'enabled': False})['ok']
        connection.ready.set_result(True)
        worker.join(5)
        assert results and results[0]['not_executed'] and not results[0]['outcome_unknown']
        assert not requests and not hub.mcp_requests
    finally:
        if not connection.ready.done():
            connection.ready.set_result(True)
        worker.join(5)


@pytest.mark.parametrize('kind', ['tool', 'resource'])
@pytest.mark.parametrize('stage', ['ready', 'request'])
@pytest.mark.parametrize('error_type', [ValueError, FileNotFoundError, concurrent.futures.TimeoutError])
def test_remote_errors_are_private_and_failures_distinguish_pre_dispatch_from_unknown_effect(hub, kind, stage, error_type):
    requests = []
    class Connection:
        closed = False
        ready = concurrent.futures.Future()
        def request(self, *args, **kwargs):
            requests.append(args)
            raise error_type('synthetic-secret private-account /private/path')
        def shutdown(self):
            self.closed = True
    connection = Connection()
    if stage == 'ready':
        connection.ready.set_exception(error_type('synthetic-secret private-account /private/path'))
    else:
        connection.ready.set_result(True)
    hub.connections['fixture'] = connection
    name, args = invocation(hub, kind)
    result = hub.execute(name, args)
    assert not result['ok'] and 'synthetic-secret' not in result['error']
    assert 'private-account' not in result['error'] and '/private/path' not in result['error']
    assert result.get('not_executed', False) is (stage == 'ready')
    assert result['outcome_unknown'] is (stage == 'request')
    assert len(requests) == (stage == 'request')
    assert not hub.mcp_requests
    if error_type is concurrent.futures.TimeoutError:
        assert ('No tool request was sent' in result['error']) is (stage == 'ready')
        assert ('Inspect its outcome' in result['error']) is (stage == 'request')
