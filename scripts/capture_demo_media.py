"""Capture public Forge demos from synthetic data and a fresh browser profile.

Build frontend/dist first, then run this script with the development Python
environment. The fixture creates its own temporary Forge store and app. It never
opens the personal coordinator, chats, project folders or browser profile, and it
blocks requests outside its two loopback servers. No inference is performed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge_builder import BuilderManager
from forge_drafts import DraftManager
from forge_store import ForgeStore


DEMO_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tide Studio — a little room for ideas</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ee;color:#263631;font:15px/1.55 system-ui,sans-serif}
main{max-width:860px;margin:auto;padding:35px 38px}header{display:flex;align-items:center;justify-content:space-between;gap:15px}
.brand{font-weight:650;letter-spacing:-.03em;font-size:19px}.wordmark{display:inline-grid;place-items:center;background:#365a49;color:#fff;width:30px;height:30px;border-radius:9px;margin-right:9px}
.badge{background:#e3eadf;color:#365a49;padding:5px 11px;border-radius:30px;font-size:11px;text-transform:uppercase;letter-spacing:.1em}
.eyebrow{margin-top:34px;text-transform:uppercase;letter-spacing:.14em;font-size:10px;color:#69776c}
h1{font-size:clamp(28px,4vw,42px);line-height:1.13;font-weight:550;letter-spacing:-.05em;margin:9px 0 12px}p{color:#6a776f;margin:0 0 24px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{background:#fffdf8;border:1px solid #e2e6dc;border-radius:15px;padding:19px;min-height:112px}
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:#83947e}.card h2{font-size:16px;font-weight:550;margin:12px 0 4px}.card small{color:#849087}
form{display:flex;gap:8px;margin-top:24px}input{min-width:0;flex:1;padding:12px 14px;border-radius:10px;border:1px solid #d9dfd5;background:#fffdf8;font:inherit}
button{border:0;background:#365a49;color:white;border-radius:10px;padding:12px 17px;font:600 12px system-ui;cursor:pointer}
input:focus-visible,button:focus-visible{outline:3px solid #e2a56c;outline-offset:3px}
.note{border-top:1px solid #e1e6dc;padding-top:15px;font-size:11px;color:#89938b;margin-top:24px}
@media(max-width:500px){main{padding:22px}.grid{grid-template-columns:1fr}}
</style></head><body><main>
<header><div class="brand"><span class="wordmark">t</span>Tide Studio</div><span class="badge">Your idea space</span></header>
<div class="eyebrow">A little room for ideas</div><h1>Make space for<br>what comes next.</h1>
<p>Collect the sparks. Give the good ones room to grow.</p>
<div class="grid" id="ideas"><article class="card"><span class="tag">In motion</span><h2>A calmer morning routine</h2><small>Small changes. A better start.</small></article>
<article class="card"><span class="tag">To explore</span><h2>A weekend field journal</h2><small>Keep the details that matter.</small></article></div>
<form id="form"><input id="idea" aria-label="New idea" placeholder="What's on your mind?" required maxlength="80"><button>Add idea</button></form>
<div class="note">Stored in this demo tab · Designed to work with your keyboard</div>
</main><script>
document.querySelector('#form').addEventListener('submit',e=>{e.preventDefault();const input=document.querySelector('#idea');if(!input.value.trim())return;const card=document.createElement('article');card.className='card';const tag=document.createElement('span');tag.className='tag';tag.textContent='New idea';const title=document.createElement('h2');title.textContent=input.value.trim();card.append(tag,title);document.querySelector('#ideas').append(card);input.value='';input.focus();});
</script></body></html>"""

SKILLS = [
    {'id': 'ui-design', 'name': 'UI design', 'enabled': True,
     'description': 'Build clear, accessible interfaces with a consistent visual direction.'},
    {'id': 'testing', 'name': 'Testing', 'enabled': True,
     'description': 'Check the main flow, keyboard controls and small screens.'},
    {'id': 'project-discovery', 'name': 'Project discovery', 'enabled': True,
     'description': 'Understand the project before choosing the next step.'},
]


@contextmanager
def production_server(directory=ROOT / 'frontend/dist'):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
        QuietHandler, directory=str(directory)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class DemoFixture:
    def __init__(self, home: Path):
        self.allowed_ports = set()
        app = home / 'demo-workspace/site'
        app.mkdir(parents=True)
        (app / 'index.html').write_text(DEMO_HTML, encoding='utf-8')
        self.store = ForgeStore(home / 'state')
        project = self.store.create_project('Tide Studio', str(app.parent))
        # The browser receives a generic path. Actual temporary paths never
        # enter the public UI or screenshots.
        self.project = {**project, 'path': './demo-workspace', 'branch': 'main'}
        chat = self.store.create_chat(project['id'], 'A small studio for ideas', 'local-model')
        self.store.add_message(chat['id'], 'user',
            'Build a calm little studio board for my ideas. Keep it simple, local and easy to use with a keyboard.')
        self.store.add_message(chat['id'], 'assistant',
            'Let’s start with a clear direction.\n\n'
            '**Tide Studio** gives each idea a little room to grow: a quiet board, a quick way to add a thought, '
            'and a layout that feels at home on a small screen.\n\n'
            '- A warm, uncluttered interface with clear focus states.\n'
            '- Add ideas without leaving the page.\n'
            '- Keep the brief, local preview and checks together in Builder.\n\n'
            'The demonstration preview is ready to explore. We can refine the details from here.')
        self.chat = self.store.get_chat(chat['id'])
        self.spaces = [{'id': 'studio-direction', 'name': 'Studio direction',
                        'project_ids': [project['id']], 'notes': 'Calm layouts, warm neutral colors and clear keyboard focus.'}]
        self.store.save_entity('spaces', self.spaces[0])
        self.settings = {'model': 'local-model', 'theme': 'dark', 'context': 32768,
                         'permission_profile': 'always_ask', 'timezone': 'UTC'}
        self.manager = BuilderManager(SimpleNamespace(store=self.store))
        self.drafts = DraftManager(self.store, skill_catalog=lambda _project: SKILLS)
        self.brief = self.manager.save({
            'project_id': project['id'], 'chat_id': chat['id'], 'title': 'Tide Studio',
            'objective': 'A small, calm board for collecting ideas and trying the next one.',
            'audience': 'People who like to keep their ideas close.',
            'constraints': 'Local data, responsive layout and visible keyboard focus.',
            'directory': 'site', 'template': 'static',
            'design': {'style': 'Warm neutrals, forest green and generous space.'},
            'requirements': [
                {'text': 'Add a new idea', 'acceptance': 'The card appears immediately without reloading.'},
                {'text': 'Use the board with a keyboard', 'acceptance': 'Enter adds an idea and focus returns to the field.'},
            ]})

    def respond(self, route):
        action = urlsplit(route.request.url).path.rsplit('/', 1)[-1]
        data = route.request.post_data_json or {}
        try:
            if action in ('draft_get', 'draft_save', 'draft_clear'):
                result = self.drafts.dispatch(action, data)
            elif action.startswith(('builder_', 'preview_')):
                result = self.manager.dispatch(action, data)
                if action == 'preview_start' and result.get('url'):
                    self.allowed_ports.add(urlsplit(result['url']).port)
            elif action == 'bootstrap':
                result = {'settings': self.settings, 'projects': [self.project], 'chats': [self.chat],
                          'runs': [], 'capabilities': {}}
            elif action == 'get_chat':
                result = self.chat
            elif action == 'settings':
                self.settings.update(data)
                result = self.settings
            elif action == 'models':
                result = {'models': [{'name': 'local-model', 'context_length': 32768, 'provider': 'ollama'}]}
            elif action == 'skills':
                result = {'skills': SKILLS}
            elif action == 'spaces':
                result = {'spaces': self.spaces}
            else:
                result = {'first_run': False, 'projects': [self.project], 'chats': [self.chat],
                          'runs': [], 'goals': [], 'documents': [], 'actions': [], 'questions': [], 'schedules': []}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
        except ValueError as exc:
            route.fulfill(status=400, content_type='application/json', body=json.dumps({'error': str(exc)}))


def pixels(page) -> Image.Image:
    return Image.open(io.BytesIO(page.screenshot())).convert('RGB')


def png(page, target: Path):
    pixels(page).save(target, optimize=True)


def gif(frames, target: Path):
    # A shared palette reduces flicker. Sparse UI snapshots keep these short
    # demos small while preserving the actual rendered clicks and states.
    size = (1120, round(frames[0].height * 1120 / frames[0].width))
    scaled = [frame.resize(size, Image.Resampling.LANCZOS) for frame in frames]
    palette = scaled[-1].quantize(colors=160)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in scaled]
    indexed[0].save(target, save_all=True, append_images=indexed[1:],
                    duration=[1300] * len(indexed), loop=0, optimize=True, disposal=1)


def capture_extension(executable, output: Path):
    """Render the shipped popup against synthetic Chrome API responses only."""
    errors, blocked = [], []
    with production_server(ROOT / 'browser-extension') as server, sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, executable_path=executable or None)
        context = browser.new_context(viewport={'width': 336, 'height': 500},
                                      device_scale_factor=2, locale='en-GB')
        def allow_loopback(route):
            url = urlsplit(route.request.url)
            if url.scheme == 'http' and url.hostname == '127.0.0.1' and url.port == server.server_port:
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()
        context.route('**/*', allow_loopback)
        context.add_init_script("""(() => {
            let selected = false;
            window.demoActions = [];
            window.chrome = {
                tabs: {query: async () => [{id: 7, title: 'Reference page', url: 'https://example.org/'}]},
                runtime: {
                    id: 'abcdefghijklmnopabcdefghijklmnop',
                    sendMessage: async request => {
                        window.demoActions.push(request.action);
                        if (request.action === 'connect') selected = true;
                        if (request.action === 'disconnect') selected = false;
                        return {ok: true, selected, connected: selected, eligible: true,
                            bridge: selected ? 'connected' : 'disconnected', error: null,
                            title: 'Reference page', origin: 'https://example.org'};
                    }
                }
            };
        })();""")
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}/popup.html')
        page.get_by_role('status').get_by_text('This tab is not connected', exact=True).wait_for()
        connect = page.get_by_role('button', name='Connect this tab', exact=True)
        disconnect = page.get_by_role('button', name='Disconnect', exact=True)
        assert connect.is_enabled() and connect.is_visible()
        assert not disconnect.is_visible()
        assert not page.get_by_role('button', name='Reconnect Forge', exact=True).is_visible()
        assert page.get_by_text('Reference page', exact=True).is_visible()
        assert page.get_by_text('https://example.org', exact=True).is_visible()
        target = output / 'forge-5.0.3-extension-popup.png'
        Image.open(io.BytesIO(page.locator('body').screenshot())).save(target, optimize=True)
        connect.click()
        page.get_by_role('status').get_by_text('Connected to Forge', exact=True).wait_for()
        assert not connect.is_visible() and disconnect.is_enabled()
        disconnect.click()
        page.get_by_role('status').get_by_text('This tab is not connected', exact=True).wait_for()
        assert connect.is_enabled() and not disconnect.is_visible()
        assert page.evaluate("window.demoActions.includes('connect') && window.demoActions.includes('disconnect')")
        assert not errors and not blocked
        context.close()
        browser.close()
    print(f'{target.name}: {target.stat().st_size:,} bytes')
    print('Synthetic popup capture passed; Connect/Disconnect states verified with no external requests.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', default=os.environ.get('FORGE_TEST_BROWSER'),
                        help='Chromium executable; defaults to the installed test browser on Windows.')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/images')
    parser.add_argument('--extension-only', action='store_true',
                        help='Capture only the browser popup; keep workspace images and GIFs unchanged.')
    args = parser.parse_args()
    executable = args.browser
    if not executable and os.name == 'nt':
        executable = str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) /
                         'Google/Chrome/Application/chrome.exe')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.extension_only:
        capture_extension(executable, args.output)
        return
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise SystemExit('Build the production frontend before capturing media.')
    errors = []
    blocked_requests = []
    with tempfile.TemporaryDirectory(prefix='forge-public-demo-') as temporary, production_server() as server:
        fixture = DemoFixture(Path(temporary))
        try:
            with sync_playwright() as engine:
                browser = engine.chromium.launch(headless=True, executable_path=executable or None)
                context = browser.new_context(viewport={'width': 1440, 'height': 900},
                                              locale='en-GB', timezone_id='UTC', reduced_motion='reduce')
                origin = f'http://127.0.0.1:{server.server_port}'
                allowed_ports = fixture.allowed_ports
                allowed_ports.add(server.server_port)
                def allow_loopback(route):
                    url = urlsplit(route.request.url)
                    if url.scheme == 'http' and url.hostname == '127.0.0.1' and url.port in allowed_ports:
                        route.continue_()
                    else:
                        blocked_requests.append(route.request.url)
                        route.abort()
                context.route('**/*', allow_loopback)
                page = context.new_page()
                page.route('**/api/v1/*', fixture.respond)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.set_default_timeout(10000)
                page.goto(origin)
                page.get_by_role('textbox', name='Message Forge', exact=True).wait_for()
                page.get_by_text('The demonstration preview is ready to explore.', exact=False).wait_for()
                page.wait_for_timeout(300)
                frames = [pixels(page)]
                page.get_by_role('button', name='Skills and notes', exact=True).click()
                page.get_by_role('heading', name='Make it your own', exact=True).wait_for()
                frames.append(pixels(page))
                page.get_by_role('checkbox', name='UI design', exact=False).check()
                frames.append(pixels(page))
                page.get_by_role('checkbox', name='Testing', exact=False).check()
                frames.append(pixels(page))
                page.get_by_role('checkbox', name='Studio direction', exact=False).check()
                page.get_by_role('textbox', name='Message Forge', exact=True).fill(
                    'Add keyboard controls and keep the layout calm.')
                page.get_by_text('Draft saved locally', exact=True).wait_for()
                frames.append(pixels(page))
                png(page, args.output / 'forge-5.0.3-workspace-dark.png')
                page.get_by_role('button', name='Skills and notes', exact=True).click()
                frames.append(pixels(page))
                page.get_by_role('button', name='Skills and notes', exact=True).click()
                frames.append(pixels(page))
                gif(frames, args.output / 'forge-5.0.3-context.gif')

                page.get_by_role('button', name='Skills and notes', exact=True).click()
                page.set_viewport_size({'width': 1440, 'height': 1050})
                page.evaluate("document.documentElement.dataset.theme='light'")
                page.get_by_role('navigation', name='Workspace navigation').get_by_role(
                    'button', name='Builder', exact=True).click()
                page.get_by_label('Objective').wait_for()
                frames = [pixels(page)]
                page.get_by_role('button', name='Preview', exact=True).click()
                frames.append(pixels(page))
                # The real preview_start API admits its chosen loopback port
                # before the browser receives the URL. No duplicate start.
                page.get_by_role('button', name='Start preview', exact=True).click()
                frame = page.frame_locator('iframe[title="Local application preview"]')
                frame.get_by_role('heading', name='Make space for what comes next.', exact=False).wait_for()
                frames.append(pixels(page))
                png(page, args.output / 'forge-5.0.3-builder-light.png')
                field = frame.get_by_role('textbox', name='New idea', exact=True)
                field.fill('A small creative habit')
                frames.append(pixels(page))
                field.press('Enter')
                frame.get_by_role('heading', name='A small creative habit', exact=True).wait_for()
                assert field.evaluate('(element)=>element===document.activeElement')
                frames.append(pixels(page))
                # Record only observed checks in the disposable fixture.
                current = fixture.manager.get(fixture.brief['id'])
                fixture.manager.check({'id': current['id'], 'expected_revision': current['revision'],
                    'gate': 'functional', 'status': 'passed', 'source': 'manual',
                    'note': 'Demo capture: Enter added an idea and returned focus to the field.'}, human=True)
                fixture.manager.check({'id': current['id'], 'expected_revision': current['revision'],
                    'gate': 'preview', 'status': 'passed', 'source': 'preview',
                    'reference': current['previews'][0]['id'],
                    'note': 'Isolated static app loaded and was exercised in the preview.'})
                page.get_by_role('button', name='Checks', exact=True).click()
                page.get_by_role('button', name='Refresh check freshness', exact=True).click()
                page.locator('.builder-gates').get_by_text('passed', exact=True).first.wait_for()
                frames.append(pixels(page))
                page.get_by_role('button', name='Preview', exact=True).click()
                frame.get_by_role('heading', name='Make space for what comes next.', exact=False).wait_for()
                frames.append(pixels(page))
                gif(frames, args.output / 'forge-5.0.3-builder.gif')
                assert not errors, errors
                assert not blocked_requests, 'The demo attempted a request outside its isolated loopback servers.'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                context.close()
                browser.close()
        finally:
            fixture.manager.shutdown()
    for path in sorted(args.output.glob('forge-5.0.3-*')):
        print(f'{path.name}: {path.stat().st_size:,} bytes')
    print('Synthetic production-UI capture passed; no page errors or external requests.')


if __name__ == '__main__':
    main()
