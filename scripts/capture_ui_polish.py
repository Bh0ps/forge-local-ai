"""Verify the production GUI using disposable synthetic data and isolated Chrome.

No personal Forge coordinator, model, browser profile, or external service is used.
All browser requests are restricted to this script's randomly allocated loopback
static server; API responses are fulfilled by DemoFixture's temporary store.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from capture_demo_media import DemoFixture, MEDIA_VERSION, ROOT, production_server
from forge_drafts import DraftManager


OUTPUT = ROOT.parent / 'forge-native-private/ui-524/visual'
LAYOUT = {'sidebar_open': True, 'sidebar_width': 260, 'right_panel_open': False,
          'right_panel_width': 320, 'right_panel_tab': 'activity',
          'collapsed_project_ids': [], 'browser_mode': 'browser'}
SKILLS = [
    {'id': 'native-cpp', 'name': 'Windows C++', 'bundle': 'build', 'category': 'coding',
     'description': 'Use CMake presets and verify native Windows builds.', 'enabled': True, 'automatic': True},
    {'id': 'dotnet', 'name': 'Windows .NET', 'bundle': 'development', 'category': 'coding',
     'description': 'Build and test a .NET solution with clear acceptance checks.', 'enabled': True, 'automatic': True},
    {'id': 'ui-design', 'name': 'Interface design', 'bundle': 'design', 'category': 'design',
     'description': 'Preserve the design system, keyboard access and readable hierarchy.', 'enabled': True, 'automatic': True},
    {'id': 'testing', 'name': 'Release checks', 'bundle': 'review', 'category': 'testing',
     'description': 'Check normal use, narrow screens and failure recovery.', 'enabled': True, 'automatic': False},
]


class PolishFixture(DemoFixture):
    def __init__(self, home: Path):
        super().__init__(home)
        self.actions = []
        self.layout = dict(LAYOUT)
        self.settings.update({'thinking': True, 'web': True, 'tokens': 4096,
                              'temperature': .3, 'allow_edits': True,
                              'ui_appearance': {'name': 'Forge', 'accent': 'copper', 'motion': 'system'}})
        self.project['appearance'] = {'icon': 'code', 'accent': 'copper'}
        self.spaces = [{'id': 'studio-direction', 'name': 'Studio direction',
                        'description': 'Warm colours, clear controls and keyboard focus.',
                        'project_ids': [self.project['id']]}]
        self.drafts = DraftManager(self.store, skill_catalog=lambda _project: SKILLS)
        for number in range(1, 10):
            self.store.add_message(self.chat['id'], 'user',
                                   f'Iteration {number}: refine the idea board and its everyday controls.')
            self.store.add_message(self.chat['id'], 'assistant',
                                   f'The iteration {number} keeps the same visual direction.\n\n'
                                   'The interface has a clear hierarchy, useful controls and a quiet workspace. '
                                   'Keyboard focus remains visible and the layout adapts to a smaller window.\n\n'
                                   '- Keep the next action easy to find.\n- Reveal supporting detail when needed.\n'
                                   '- Preserve local work while changing views.',
                                   thinking='Reviewed the layout, keyboard sequence and responsive behaviour.\n'
                                   'The conversation remains the primary reading surface.')
        self.store.add_message(self.chat['id'], 'assistant',
                               'The interface is ready for another careful review.\n\n'
                               '```ts\nconst direction = "quiet, clear, local";\n```\n\n'
                               'We can keep refining the details without losing the character of Forge.')
        self.chat = self.store.get_chat(self.chat['id'])
        image = Image.new('RGB', (320, 180), '#e9e3d9')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((24, 24, 296, 156), radius=12, fill='#faf8f3', outline='#c6b8a6')
        draw.text((42, 48), 'Synthetic layout reference', fill='#534635')
        draw.line((42, 85, 260, 85), fill='#b76337', width=4)
        stream = io.BytesIO(); image.save(stream, format='JPEG')
        self.image = base64.b64encode(stream.getvalue()).decode('ascii')

    def respond(self, route):
        action = urlsplit(route.request.url).path.rsplit('/', 1)[-1]
        self.actions.append(action)
        data = route.request.post_data_json or {}
        result = None
        if action == 'skills':
            result = {'skills': SKILLS}
        elif action == 'catalog_discover':
            result = {'entries': [{**skill, 'id': 'starter:' + skill['id'], 'skill_id': skill['id'],
                                   'kind': 'skill', 'installed': True, 'builtin': True,
                                   'catalog_name': 'Forge starters', 'license': 'MIT',
                                   'compatibility': 'SKILL.md'} for skill in SKILLS],
                      'sources': [{'id': 'forge-starter', 'name': 'Forge starters', 'builtin': True, 'count': len(SKILLS)}]}
        elif action == 'plugins':
            result = {'plugins': []}
        elif action == 'show':
            result = {'capabilities': ['tools', 'thinking'], 'model_info': {'fixture.context_length': 32768}}
        elif action == 'ui_layout_get':
            result = self.layout
        elif action == 'ui_layout_save':
            self.layout = data['layout']; result = self.layout
        elif action == 'skill_preview':
            result = {'text': '# Synthetic skill\nRead the project, preserve local work and verify the result.'}
        if result is None:
            return super().respond(route)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))


def geometry(page):
    return page.evaluate('''() => ({width:innerWidth,height:innerHeight,
        documentWidth:document.documentElement.scrollWidth,
        bodyWidth:document.body.scrollWidth,
        appWidth:document.querySelector('.app')?.getBoundingClientRect().width})''')


def settle(page):
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def follow_observations(page):
    """Read scroll geometry across real layout frames without forcing a scroll."""
    return page.evaluate('''async () => {
        const samples=[];
        for(let frame=0;frame<=32;frame++) {
            if([0,1,2,4,8,16,32].includes(frame)) {
                const node=document.querySelector('.chat-scroll');
                samples.push({frame,top:node.scrollTop,height:node.scrollHeight,client:node.clientHeight,
                    distance:node.scrollHeight-node.clientHeight-node.scrollTop,
                    jumpVisible:Boolean(document.querySelector('.jump-to-latest')),
                    fonts:document.fonts.status});
            }
            await new Promise(resolve=>requestAnimationFrame(resolve));
        }
        return samples;
    }''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--browser', default=os.environ.get('FORGE_TEST_BROWSER'))
    args = parser.parse_args()
    output = args.output.resolve()
    # Captures may contain diagnostic paths on failure. Keep them private until
    # reviewed; the release process explicitly selects safe synthetic pictures.
    if output != OUTPUT.resolve():
        raise SystemExit(f'Output must be the private capture directory: {OUTPUT}')
    output.mkdir(parents=True, exist_ok=True)
    executable = args.browser or (str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) /
                                      'Google/Chrome/Application/chrome.exe') if os.name == 'nt' else None)
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise SystemExit('Build frontend/dist before running this capture.')
    report = {'version': MEDIA_VERSION, 'synthetic': True, 'passed': False, 'checks': [],
              'screenshots': [], 'page_errors': [], 'blocked_requests': [],
              'initial_follow_observations': [],
              'production_index_sha256': hashlib.sha256((ROOT / 'frontend/dist/index.html').read_bytes()).hexdigest(),
              'zoom_method': '150% physical scaling: 960x626 CSS viewport at device scale factor 1.5'}
    current_page = None
    try:
        with tempfile.TemporaryDirectory(prefix='forge-ui-polish-') as temporary, production_server() as server:
            fixture = PolishFixture(Path(temporary))
            try:
                with sync_playwright() as engine:
                    browser = engine.chromium.launch(headless=True, executable_path=executable,
                        args=['--disable-gpu', '--disable-background-networking', '--no-first-run',
                              '--disable-sync', '--disable-default-apps'])
                    origin = f'http://127.0.0.1:{server.server_port}'

                    def context(width=1440, height=940, scale=1):
                        fixture.layout = dict(LAYOUT)
                        ctx = browser.new_context(viewport={'width': width, 'height': height},
                            device_scale_factor=scale, locale='en-GB', timezone_id='UTC', reduced_motion='reduce')
                        def route(request):
                            parsed = urlsplit(request.request.url)
                            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != server.server_port:
                                report['blocked_requests'].append(request.request.url); request.abort(); return
                            if parsed.path.startswith('/api/v1/'):
                                fixture.respond(request)
                            else:
                                request.continue_()
                        ctx.route('**/*', route)
                        ctx.add_init_script('''(() => { window.pywebview={api:{
                            call:async(action,data={})=>{const response=await fetch('/api/v1/'+action,{method:'POST',
                                headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await response.json();
                                if(!response.ok)throw Error(result.error || 'Synthetic request failed');return result;},
                            mode:async()=>({ok:true}),copy:async()=>({ok:true}),
                            screenshot:async()=>({image:''' + json.dumps(fixture.image) + '''})}};})();''')
                        page = ctx.new_page(); page.set_default_timeout(12000)
                        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
                        page.goto(origin); page.get_by_role('textbox', name='Message Forge', exact=True).wait_for()
                        page.get_by_text('We can keep refining the details', exact=False).wait_for()
                        settle(page)
                        return ctx, page

                    def check(name, condition, detail=None):
                        report['checks'].append({'name': name, 'passed': bool(condition), 'detail': detail})
                        if not condition:
                            raise AssertionError(name + ': ' + str(detail))

                    def capture(page, name):
                        settle(page); bounds = geometry(page)
                        check(name + ': no page horizontal overflow', bounds['documentWidth'] <= bounds['width'] + 1
                              and bounds['bodyWidth'] <= bounds['width'] + 1, bounds)
                        path = output / f'forge-{MEDIA_VERSION}-{name}.png'
                        page.screenshot(path=str(path)); report['screenshots'].append(path.name)

                    ctx, page = context(); current_page = page
                    samples = follow_observations(page)
                    report['initial_follow_observations'].append({'viewport': 'desktop', 'samples': samples})
                    check('Desktop initially follows latest without reader input',
                        all(sample['distance'] <= 3 and not sample['jumpVisible'] for sample in samples), samples)
                    capture(page, 'chat-dark')
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    capture(page, 'chat-light')
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    settings = page.get_by_role('button', name='Settings', exact=True)
                    settings.click(); dialog = page.get_by_role('dialog', name='Settings', exact=True)
                    dialog.wait_for(); page.get_by_role('combobox', name='Appearance', exact=True).wait_for()
                    check('Settings focus remains inside modal', dialog.evaluate('(node)=>node.contains(document.activeElement)'))
                    page.keyboard.press('Tab')
                    check('Settings keyboard focus remains inside modal', dialog.evaluate('(node)=>node.contains(document.activeElement)'))
                    capture(page, 'settings-dark')
                    reduced = page.locator('.settings-dialog').evaluate('(node)=>({animation:getComputedStyle(node).animationName,transition:getComputedStyle(node).transitionDuration})')
                    check('Reduced motion disables settings animation', reduced['animation'] == 'none', reduced)
                    page.keyboard.press('Escape')
                    check('Settings returns keyboard focus', settings.evaluate('(node)=>document.activeElement===node'))
                    page.get_by_role('button', name='Skills and notes', exact=True).click()
                    page.get_by_role('button', name='Coding skills', exact=True).click()
                    choice = page.get_by_role('checkbox', name='Windows C++', exact=True)
                    choice.focus(); page.keyboard.press('Space')
                    remove = page.get_by_role('button', name='Remove Windows C++', exact=True); remove.wait_for()
                    check('Context selection transfers keyboard focus', remove.evaluate('(node)=>node===document.activeElement'))
                    page.get_by_role('button', name='Notes', exact=True).click()
                    # Selected choices move into Attached items, retiring their
                    # checkbox. Verify the new control rather than asking
                    # Playwright to keep observing a now-removed checkbox.
                    page.get_by_role('checkbox', name='Studio direction', exact=True).click()
                    page.get_by_role('button', name='Remove Studio direction', exact=True).wait_for()
                    capture(page, 'context-dark')
                    page.get_by_role('button', name='Close workspace panel', exact=True).click()
                    page.get_by_role('button', name='Tools', exact=True).click()
                    page.get_by_role('menuitem', name='Library', exact=True).click()
                    page.get_by_role('heading', name='Windows C++', exact=True).wait_for()
                    check('Library groups normalized Coding bundle', page.get_by_role('button', name='Coding bundle', exact=True).count() == 1)
                    entry = page.get_by_role('button', name='Windows C++', exact=True)
                    entry.focus(); page.keyboard.press('Tab')
                    check('Library keyboard focus has a visible outline', page.evaluate('''() => {
                        const n=document.activeElement,s=getComputedStyle(n),p=getComputedStyle(n,'::after');
                        return n!==document.body && (parseFloat(s.outlineWidth)>0 || parseFloat(p.outlineWidth)>0);
                    }'''))
                    capture(page, 'library-dark')
                    page.get_by_role('button', name='A small studio for ideas', exact=True).click()
                    prompt = page.get_by_role('textbox', name='Message Forge', exact=True)
                    prompt.fill('\n'.join(f'Line {number}: preserve the layout and verify keyboard behaviour.' for number in range(1, 10)))
                    page.get_by_role('button', name='Attach screenshot', exact=True).click()
                    page.get_by_role('img', name='Attachment 1', exact=True).wait_for()
                    page.locator('.chat-scroll').evaluate('(node)=>{node.scrollTop=80;node.dispatchEvent(new Event("scroll",{bubbles:true}));}')
                    jump = page.get_by_role('button', name='Jump to latest', exact=True); jump.wait_for()
                    jump_bounds, composer_bounds = jump.bounding_box(), page.locator('.composer-area').bounding_box()
                    check('Tall composer and attachment do not overlap Jump to latest',
                        jump_bounds['y'] + jump_bounds['height'] <= composer_bounds['y'] + 1,
                        {'jump': jump_bounds, 'composer': composer_bounds})
                    check('Multiline text and attachment make a tall composer', composer_bounds['height'] >= 240
                          and prompt.evaluate('(node)=>node.scrollHeight>node.clientHeight'), composer_bounds)
                    capture(page, 'tall-composer-dark')
                    # The production input currently has a fixed minimum height.
                    # Exercise its declared CSS maximum as a separate, explicitly
                    # synthetic geometry stress case, not as a product screenshot.
                    maximum = prompt.evaluate('(node)=>getComputedStyle(node).maxHeight')
                    prompt.evaluate('(node,height)=>node.style.height=height', maximum)
                    settle(page)
                    jump_bounds, composer_bounds = jump.bounding_box(), page.locator('.composer-area').bounding_box()
                    check('Maximum textarea-height stress still clears Jump to latest',
                        jump_bounds['y'] + jump_bounds['height'] <= composer_bounds['y'] + 1,
                        {'synthetic_textarea_height': maximum, 'jump': jump_bounds, 'composer': composer_bounds})
                    capture(page, 'composer-max-height-stress')
                    prompt.evaluate('(node)=>node.style.height=""')
                    jump.click(); check('Jump to latest reaches transcript end', page.locator('.chat-scroll').evaluate('(n)=>n.scrollHeight-n.clientHeight-n.scrollTop<3'))
                    page.get_by_role('button', name='Remove attachment 1', exact=True).click(); prompt.fill('')
                    page.get_by_role('button', name='Collapse sidebar', exact=True).click()
                    page.set_viewport_size({'width':480,'height':980}); settle(page)
                    capture(page, 'chat-narrow-480')
                    page.set_viewport_size({'width':1440,'height':940})
                    page.get_by_role('button', name='Minimal HUD', exact=True).click()
                    page.get_by_role('button', name='Full workspace', exact=True).wait_for()
                    page.set_viewport_size({'width':620,'height':260}); capture(page, 'hud-compact')
                    page.get_by_role('button', name='Show reply', exact=True).click()
                    page.set_viewport_size({'width':620,'height':620}); capture(page, 'hud-expanded')
                    ctx.close(); current_page = None
                    ctx, page = context(960, 626, 1.5); current_page = page
                    samples = follow_observations(page)
                    report['initial_follow_observations'].append({'viewport': '150-percent', 'samples': samples})
                    check('150-percent initially follows latest without reader input',
                        all(sample['distance'] <= 3 and not sample['jumpVisible'] for sample in samples), samples)
                    page.get_by_role('button', name='Collapse sidebar', exact=True).click()
                    capture(page, 'chat-150-percent')
                    page.get_by_role('button', name='Open sidebar', exact=True).click()
                    page.get_by_role('button', name='Settings', exact=True).click()
                    page.get_by_role('combobox', name='Appearance', exact=True).wait_for()
                    capture(page, 'settings-150-percent')
                    ctx.close(); current_page = None; browser.close()
                    check('No JavaScript page errors', not report['page_errors'], report['page_errors'])
                    check('No external requests attempted', not report['blocked_requests'], report['blocked_requests'])
                    report['actions'] = sorted(set(fixture.actions))
                    report['passed'] = True
            finally:
                fixture.manager.shutdown()
    except Exception as error:
        report['error'] = str(error)
        if current_page and not current_page.is_closed():
            try:
                current_page.screenshot(path=str(output / 'failure.png'))
            except Exception:
                pass
    finally:
        (output / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']),
                      'screenshots': report['screenshots'], 'error': report.get('error'),
                      'report': str(output / 'verification.json')}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
