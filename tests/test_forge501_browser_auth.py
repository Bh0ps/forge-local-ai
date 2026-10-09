"""Synthetic browser sessions; never access personal Forge state or models."""
import asyncio
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from forge_host import PairingAuthority
from model_manager import create_app


class Service:
    def __init__(self, folder):
        self.store = SimpleNamespace(home=folder)
        self.calls = []

    def dispatch(self, action, data):
        self.calls.append((action, data))
        return {'ok': True, 'events': [], 'finished': True, 'status': 'completed'}


@pytest.fixture
def coordinator(tmp_path):
    auth, service = PairingAuthority(), Service(tmp_path)
    app = create_app(service=service, auth=auth, assets_dir=tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1:31001')
    try:
        yield app, client, auth, service
    finally:
        client.close()
        auth.close()


def pair(client, auth):
    response = client.post('/api/v1/pair', json={'code': auth.issue()['code']})
    assert response.status_code == 200
    return response.json()['client_proof']


def test_cookie_replay_without_client_proof_cannot_dispatch_or_revoke(coordinator):
    _, client, auth, service = coordinator
    proof = pair(client, auth)
    cookie = client.cookies.get('forge_session')
    assert auth.valid(cookie)  # This is a real, current session, not a bogus token.
    for headers in ({}, {'Origin': 'http://127.0.0.1:31001'}, {'Authorization': 'Bearer ' + cookie}):
        for path in ('/api/v1/synthetic_check', '/api/synthetic_check', '/api/v1/unpair'):
            assert client.post(path, json={}, headers=headers).status_code == 401
        assert client.get('/api/v1/runs/run/events', headers=headers).status_code == 401
    assert service.calls == [] and auth.valid(cookie)
    assert client.post('/api/v1/synthetic_check', json={}, headers={'X-Forge-Client-Proof': proof}).status_code == 200
    assert len(service.calls) == 1
    assert client.post('/api/v1/unpair', json={}, headers={'X-Forge-Client-Proof': proof}).status_code == 200
    assert client.cookies.get('forge_session') == cookie
    assert not auth.valid(cookie)


def test_client_proof_is_bound_to_session_and_exact_origin_including_port(coordinator):
    app, client, auth, service = coordinator
    first_proof = pair(client, auth)
    first_cookie = client.cookies.get('forge_session')
    second_proof = pair(client, auth)
    second_cookie = client.cookies.get('forge_session')
    assert client.post('/api/v1/synthetic_check', json={}, headers={'X-Forge-Client-Proof': first_proof}).status_code == 401
    for origin in ('http://127.0.0.1:31002', 'http://localhost:31001'):
        with TestClient(app, base_url=origin) as other:
            headers = {'Cookie': 'forge_session=' + second_cookie, 'X-Forge-Client-Proof': second_proof}
            assert other.post('/api/v1/synthetic_check', json={}, headers=headers).status_code == 401
            # Pairing both accepted hosts cannot mix one session's cookie with another proof.
            third_proof = pair(other, auth)
            headers['X-Forge-Client-Proof'] = third_proof
            assert other.post('/api/v1/synthetic_check', json={}, headers=headers).status_code == 401
    assert not service.calls
    assert client.post('/api/v1/synthetic_check', json={}, headers={
        'Cookie': 'forge_session=' + first_cookie, 'X-Forge-Client-Proof': first_proof}).status_code == 200


def test_proof_expiry_restart_and_legacy_cookie_require_repair():
    now = [0]
    auth = PairingAuthority(clock=lambda: now[0])
    legacy = auth.pair(auth.issue()['code'])
    assert auth.valid(legacy) and not auth.valid_browser(legacy, 'x' * 64, 'http://127.0.0.1:31001')
    credentials = auth.pair_browser(auth.issue()['code'], 'fixture', 'http://127.0.0.1:31001')
    assert auth.valid_browser(credentials['session'], credentials['client_proof'], 'http://127.0.0.1:31001')
    # No raw proof is retained by the authority.
    assert credentials['client_proof'] not in repr(auth._sessions)
    now[0] = 86401
    assert not auth.valid_browser(credentials['session'], credentials['client_proof'], 'http://127.0.0.1:31001')
    auth.close()
    assert not auth.valid(credentials['session'])


def test_preview_cookie_disclosure_cannot_replay_forge_actions_in_real_browser(tmp_path):
    """Reproduce port-unscoped cookies, then prove the additional proof blocks replay."""
    playwright = pytest.importorskip('playwright.sync_api')
    executable = os.getenv('FORGE_TEST_BROWSER') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(executable).is_file():
        pytest.skip('An isolated headless test browser is required.')
    import uvicorn
    auth, service = PairingAuthority(), Service(tmp_path)
    received = []

    class Preview(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get('Cookie', '')))
            body = b'<script>fetch("/api/probe").then(()=>window.done=true)</script>' if self.path == '/' else b'{}'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/json')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    preview = ThreadingHTTPServer(('127.0.0.1', 0), Preview)
    preview_thread = threading.Thread(target=preview.serve_forever, daemon=True)
    preview_thread.start()
    preview_url = 'http://127.0.0.1:' + str(preview.server_port)
    (tmp_path / 'index.html').write_text('<!doctype html><iframe sandbox="allow-scripts allow-forms allow-same-origin" src="' + preview_url + '/"></iframe>', encoding='utf-8')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    app = create_app(service=service, auth=auth, assets_dir=tmp_path)
    unpair_entered, release_unpair = threading.Event(), threading.Event()

    @app.middleware('http')
    async def delay_unpair_response(request, call_next):
        response = await call_next(request)
        if request.url.path == '/api/v1/unpair':
            unpair_entered.set()
            await asyncio.to_thread(release_unpair.wait, 10)
        return response

    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=executable, headless=True)
            try:
                page = browser.new_page()
                page.goto(base)
                credentials = page.evaluate('''async code => {
                    const response = await fetch('/api/v1/pair', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code})});
                    const result = await response.json();
                    localStorage.setItem('forge.browser-client-proof.v1', JSON.stringify({origin:location.origin,value:result.client_proof}));
                    return result;
                }''', auth.issue()['code'])
                proof = credentials['client_proof']
                page.reload()
                frame = next(frame for frame in page.frames if frame.url.startswith(preview_url))
                frame.wait_for_function('window.done === true')
                cookies = SimpleCookie()
                cookies.load(next(cookie for path, cookie in received if path == '/api/probe' and cookie))
                cookie = cookies['forge_session'].value
                assert auth.valid(cookie)
                assert all(not cookie for path, cookie in received if path == '/')
                # Same host, different port: the frame cannot read the coordinator's stored proof.
                assert frame.evaluate('''() => {try {parent.localStorage.getItem('forge.browser-client-proof.v1');return false;} catch {return true;}}''')
                with httpx.Client(trust_env=False, timeout=5) as attacker:
                    headers = {'Cookie': 'forge_session=' + cookie}
                    assert attacker.post(base + '/api/v1/synthetic_check', json={}, headers=headers).status_code == 401
                    assert attacker.post(base + '/api/v1/synthetic_check', json={}, headers={**headers, 'Origin': base}).status_code == 401
                    assert attacker.get(base + '/api/v1/runs/run/events', headers=headers).status_code == 401
                assert service.calls == []
                assert page.evaluate('''async proof => (await fetch('/api/v1/synthetic_check', {method:'POST',headers:{'Content-Type':'application/json','X-Forge-Client-Proof':proof},body:'{}'})).status''', proof) == 200
                page.reload()
                assert page.evaluate('''async () => (await fetch('/api/v1/synthetic_check', {method:'POST',headers:{'Content-Type':'application/json','X-Forge-Client-Proof':JSON.parse(localStorage.getItem('forge.browser-client-proof.v1')).value},body:'{}'})).status''') == 200
                assert len(service.calls) == 2
                # An older tab's unpair response must not delete a newly paired cookie.
                page.evaluate('''proof => {window.unpairFinished=false; fetch('/api/v1/unpair', {method:'POST',headers:{'Content-Type':'application/json','X-Forge-Client-Proof':proof},body:'{}'}).then(()=>window.unpairFinished=true);}''', proof)
                assert unpair_entered.wait(5)
                assert not auth.valid(cookie)
                replacement = page.evaluate('''async code => {
                    const response=await fetch('/api/v1/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
                    const result=await response.json();localStorage.setItem('forge.browser-client-proof.v1',JSON.stringify({origin:location.origin,value:result.client_proof}));return result;
                }''', auth.issue()['code'])
                replacement_cookie = next(item['value'] for item in page.context.cookies(base + '/api/') if item['name'] == 'forge_session')
                assert replacement_cookie != cookie and auth.valid(replacement_cookie)
                release_unpair.set()
                page.wait_for_function('window.unpairFinished === true')
                assert next(item['value'] for item in page.context.cookies(base + '/api/') if item['name'] == 'forge_session') == replacement_cookie
                assert page.evaluate('''async proof => (await fetch('/api/v1/synthetic_check',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-Client-Proof':proof},body:'{}'})).status''', replacement['client_proof']) == 200
                assert len(service.calls) == 3
            finally:
                browser.close()
    finally:
        release_unpair.set()
        server.should_exit = True
        thread.join(10)
        sock.close()
        preview.shutdown()
        preview.server_close()
        preview_thread.join(5)
        auth.close()

