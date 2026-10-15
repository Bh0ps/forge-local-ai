"""Synthetic secrets and disposable static files; no live app or credentials."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from forge_host import PairingAuthority
from model_manager import create_app


PRIVATE_DETAIL = 'synthetic-secret-for-test-only C:/synthetic-owner/private.txt'


def client_for(tmp_path, failure=None):
    root = tmp_path / 'frontend'
    root.mkdir()
    (root / 'index.html').write_text('<title>Forge</title>', encoding='utf-8')
    (root / 'bundle.js').write_text('/* synthetic */', encoding='utf-8')
    def dispatch(action, data):
        if failure is not None:
            raise failure
        return {'ok': True}
    authority = PairingAuthority()
    service = SimpleNamespace(dispatch=dispatch)
    client = TestClient(create_app(service=service, auth=authority, assets_dir=root), base_url='http://127.0.0.1')
    response = client.post('/api/v1/pair', json={'code': authority.issue()['code']})
    assert response.status_code == 200
    client.headers['X-Forge-Client-Proof'] = response.json()['client_proof']
    return client, authority, root


@pytest.mark.parametrize('failure', [ValueError(PRIVATE_DETAIL), httpx.ConnectError(PRIVATE_DETAIL)])
@pytest.mark.parametrize('route', ['/api/v1/models', '/api/models'])
def test_dispatch_errors_do_not_return_exception_details(tmp_path, failure, route):
    client, _, _ = client_for(tmp_path, failure)
    response = client.post(route, json={})
    assert response.status_code == 400
    assert PRIVATE_DETAIL not in response.text
    assert 'synthetic-secret' not in response.text
    assert 'Check' in response.json()['error']


@pytest.mark.parametrize('stream', [False, True])
def test_event_errors_do_not_return_exception_details(tmp_path, stream):
    client, _, _ = client_for(tmp_path, ValueError(PRIVATE_DETAIL))
    response = client.get('/api/v1/runs/synthetic/events', headers={'Accept': 'text/event-stream'} if stream else {})
    assert response.status_code == (200 if stream else 400)
    assert 'synthetic-secret' not in response.text and 'private.txt' not in response.text
    assert 'Refresh the chat' in response.text
    if stream:
        assert 'event: error' in response.text and 'event: finished' not in response.text


def test_pair_errors_do_not_return_exception_details(tmp_path, monkeypatch):
    client, authority, _ = client_for(tmp_path)
    def fail(*args, **kwargs):
        raise ValueError(PRIVATE_DETAIL)
    monkeypatch.setattr(authority, 'pair_browser', fail)
    response = client.post('/api/v1/pair', json={'code': 'INVALID'})
    assert response.status_code == 403
    assert 'synthetic-secret' not in response.text and 'private.txt' not in response.text
    assert 'Forge Settings' in response.json()['error']


@pytest.mark.parametrize('route', ['/api/missing', '/%2e%2e/private.txt', '/%2e%2e/frontend-other/private.txt', '/..%5cprivate.txt', '/missing.js'])
def test_static_paths_cannot_escape_assets(tmp_path, route):
    client, _, _ = client_for(tmp_path)
    (tmp_path / 'private.txt').write_text(PRIVATE_DETAIL, encoding='utf-8')
    sibling = tmp_path / 'frontend-other'
    sibling.mkdir()
    (sibling / 'private.txt').write_text(PRIVATE_DETAIL, encoding='utf-8')
    response = client.get(route)
    assert response.status_code == 404
    assert response.json() == {'error': 'Not found'}
    assert PRIVATE_DETAIL not in response.text


def test_static_assets_keep_content_and_security_headers(tmp_path):
    client, _, _ = client_for(tmp_path)
    response = client.get('/bundle.js')
    assert response.status_code == 200 and response.text == '/* synthetic */'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-cache'
    assert client.get('/').status_code == 200


def test_static_asset_link_outside_root_is_rejected(tmp_path):
    client, _, root = client_for(tmp_path)
    secret = tmp_path / 'private.txt'
    secret.write_text(PRIVATE_DETAIL, encoding='utf-8')
    try:
        (root / 'linked.txt').symlink_to(secret)
    except OSError:
        pytest.skip('Creating a disposable symlink requires host privileges')
    response = client.get('/linked.txt')
    assert response.status_code == 404 and PRIVATE_DETAIL not in response.text
