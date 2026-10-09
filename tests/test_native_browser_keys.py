"""Shipping DOM scripts must match real browser Enter/Space activation defaults."""
import json
import os
from pathlib import Path

import pytest

from native_browser import ACTION_SCRIPT,INSPECT_SCRIPT


@pytest.fixture(scope='module')
def browser():
    runtime=pytest.importorskip('playwright.sync_api')
    executable=os.environ.get('FORGE_TEST_BROWSER') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
    if not Path(executable).is_file():pytest.skip('An isolated headless test browser is required.')
    with runtime.sync_playwright() as engine:
        instance=engine.chromium.launch(executable_path=executable,headless=True)
        yield instance
        instance.close()


def content(markup):
    return markup+'''<script>
window.clicked=[];window.submitted=0;window.submitter=null;window.submittedValue=null;window.resets=0;
for(const control of document.querySelectorAll('button,input'))control.addEventListener('click',()=>clicked.push(control.id));
for(const form of document.forms){form.addEventListener('submit',event=>{event.preventDefault();submitted++;submitter=event.submitter?.id||null;submittedValue=event.submitter?.value||null;});form.addEventListener('reset',()=>resets++);}
</script>'''


def observe(page):
    return page.evaluate('''() => ({clicked,submitted,submitter,submittedValue,resets,
      value:document.getElementById('target').value,checked:document.getElementById('target').checked})''')


def scripted_key(page,backend,key):
    if backend=='extension':
        source=(Path(__file__).resolve().parents[1]/'browser-extension/background.js').read_text(encoding='utf-8')
        function='function pageOperation(operation,args) {'+source.split('function pageOperation(operation,args) {',1)[1]
        page.evaluate('crypto.randomUUID=()=>"fixture-snapshot"')
        inspected=page.evaluate('()=>{'+function+';return pageOperation("inspect",{});}')
        marker=page.locator('#target').get_attribute('data-forge-target')
        return page.evaluate('()=>{'+function+';return pageOperation("key",'+json.dumps({
            'snapshot_id':inspected['snapshot_id'],'selector':'[data-forge-target="'+marker+'"]','key':key})+');}')
    inspected=page.evaluate(INSPECT_SCRIPT.replace('__FORGE_KEY__',json.dumps('fixture-snapshot')))
    marker=page.locator('#target').get_attribute('data-forge-native')
    target=next(item for item in inspected['targets'] if item['selector']=='[data-forge-native="'+marker+'"]')
    data={'url':inspected['url'],'selector':target['selector'],'signature':target['signature'],'operation':'browser_key','key':key}
    return page.evaluate(ACTION_SCRIPT.replace('__FORGE_DATA__',json.dumps(data)))


CASES=[
    ('cancel_enter','<form><button id="target" type="button">Cancel</button></form>','Enter'),
    ('cancel_space','<form><button id="target" type="button">Cancel</button></form>',' '),
    ('submit_enter','<form><button id="target" type="submit" name="action" value="save">Save</button></form>','Enter'),
    ('submit_space','<form><button id="target" type="submit" name="action" value="save">Save</button></form>',' '),
    ('reset_enter','<form><button id="target" type="reset">Reset</button></form>','Enter'),
    ('reset_space','<form><button id="target" type="reset">Reset</button></form>',' '),
    ('checkbox_space','<form><input id="target" type="checkbox"></form>',' '),
    ('radio_space','<form><input id="target" type="radio"></form>',' '),
    ('default_submitter','<form><input id="target"><button id="save" name="action" value="save">Save</button><button id="delete" name="action" value="delete">Delete</button></form>','Enter'),
    ('disabled_first','<form><input id="target"><button id="disabled" disabled>Disabled</button><button id="delete" name="action" value="delete">Delete</button></form>','Enter'),
    ('external_default','<button id="save" form="form" name="action" value="save">Save</button><form id="form"><input id="target"><button id="delete" name="action" value="delete">Delete</button></form>','Enter'),
    ('no_default_single','<form><input id="target"></form>','Enter'),
    ('no_default_multiple','<form><input id="target"><input id="second"></form>','Enter'),
    ('textarea_newline','<form><textarea id="target">Initial text</textarea><button id="save">Save</button></form>','Enter'),
    ('text_space','<form><input id="target" value="Initial text"></form>',' '),
]


@pytest.mark.parametrize('backend',['native','extension'])
@pytest.mark.parametrize('case,markup,key',CASES,ids=[item[0] for item in CASES])
def test_scripted_key_matches_native_browser_default(browser,backend,case,markup,key):
    context=browser.new_context();page=context.new_page()
    try:
        page.set_content(content(markup));page.locator('#target').press('Space' if key==' ' else key)
        expected=observe(page)
        page.set_content(content(markup))
        result=scripted_key(page,backend,key)
        assert result['ok'],result
        assert observe(page)==expected,{'case':case,'expected_native':expected,'actual_script':observe(page)}
    finally:context.close()


@pytest.mark.parametrize('backend',['native','extension'])
@pytest.mark.parametrize('markup,key',[
    ('<form><input id="target" type="range"><button>Save</button></form>','Enter'),
    ('<form><input id="target" type="password" value="synthetic-value"><button>Save</button></form>','Enter'),
    ('<form><button id="target" disabled>Disabled</button></form>',' '),
])
def test_unsupported_or_private_key_target_is_rejected_before_focus(browser,backend,markup,key):
    context=browser.new_context();page=context.new_page()
    try:
        page.set_content(content(markup));before=observe(page)
        result=scripted_key(page,backend,key)
        assert result['not_executed'],result
        assert observe(page)==before and page.evaluate('document.activeElement.tagName')=='BODY'
    finally:context.close()
