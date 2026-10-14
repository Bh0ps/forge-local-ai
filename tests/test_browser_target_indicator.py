"""Actual shipping DOM scripts in an isolated headless browser; no OS cursor."""
import json
import pytest
from native_browser import ACTION_SCRIPT,INSPECT_SCRIPT,CLEAR_TARGET_SCRIPT
from test_native_browser_keys import browser

HTML='''<style>body{padding:20px}button,input,select{margin:8px;padding:12px}#offscreen{position:absolute;top:1600px}</style>
<input id="reader" value="Synthetic reader"><button id="target">Synthetic action</button><input id="field">
<select id="select"><option value="one">One</option><option value="two">Two</option></select>
<input id="password" type="password"><input id="file" type="file"><input id="readonly" readonly>
<button id="disabled" disabled>Disabled</button><button id="offscreen">Outside</button>
<script>window.effects=0;window.moves=0;for(const name of ['click','input','change'])document.addEventListener(name,()=>effects++);
for(const name of ['pointermove','mousemove'])document.addEventListener(name,()=>moves++);</script>'''


def action(page,selector,operation='browser_click',**extra):
    inspected=page.evaluate(INSPECT_SCRIPT.replace('__FORGE_KEY__',json.dumps('synthetic-key')))
    marker=page.locator(selector).get_attribute('data-forge-native')
    target=next(t for t in inspected['targets'] if t['selector']=='[data-forge-native="'+marker+'"]')
    data={'url':inspected['url'],'selector':target['selector'],'signature':target['signature'],
        'operation':operation,'text':'Synthetic typed input','key':'Enter','value':'two','indicate':True,'preserve_focus':True,**extra}
    return page.evaluate(ACTION_SCRIPT.replace('__FORGE_DATA__',json.dumps(data)))


@pytest.mark.parametrize('operation,selector',[('browser_click','#target'),('browser_type','#field'),
    ('browser_key','#target'),('browser_select','#select')])
def test_valid_target_uses_numeric_noninteractive_ring_without_forcing_focus(browser,operation,selector):
    context=browser.new_context(viewport={'width':480,'height':420},reduced_motion='reduce')
    page=context.new_page();external=[];page.route('**/*',lambda route:(external.append(True),route.abort()))
    try:
        page.set_content(HTML);page.locator('#reader').focus()
        assert action(page,selector,operation)['ok']
        marker=page.locator('[data-forge-ai-target]');assert marker.count()==1
        assert marker.get_attribute('aria-hidden')=='true'
        observed=marker.evaluate('(el)=>({text:el.textContent,focusable:el.tabIndex>=0,pointer:getComputedStyle(el).pointerEvents,animation:getComputedStyle(el).animationName,html:el.outerHTML})')
        assert observed['text']=='' and not observed['focusable'] and observed['pointer']=='none' and observed['animation']=='none'
        assert 'Synthetic typed input' not in observed['html'] and 'synthetic-key' not in observed['html']
        assert page.evaluate('document.activeElement.id')=='reader'
        assert page.evaluate('window.moves')==0 and external==[]
        assert page.evaluate(CLEAR_TARGET_SCRIPT) is True and marker.count()==0
    finally:context.close()


@pytest.mark.parametrize('selector,operation,extra',[('#target','validate',{'action':'browser_click'}),
    ('#target','browser_click',{'signature':['changed']}),('#password','browser_type',{}),('#file','browser_click',{}),
    ('#readonly','browser_type',{}),('#disabled','browser_click',{}),('#select','browser_select',{'value':'absent'}),
    ('#target','browser_type',{})])
def test_rejected_or_validation_only_calls_never_paint_or_deliver(browser,selector,operation,extra):
    context=browser.new_context();page=context.new_page()
    try:
        page.set_content(HTML);page.locator('#reader').focus()
        result=action(page,selector,operation,**extra)
        assert result.get('not_executed') is True or operation=='validate' and result['ok']
        assert page.locator('[data-forge-ai-target]').count()==0
        assert page.evaluate('window.effects')==0 and page.evaluate('document.activeElement.id')=='reader'
    finally:context.close()


def test_offscreen_target_omits_ring_and_finite_lifetime_does_not_follow_pointer(browser):
    context=browser.new_context(viewport={'width':360,'height':420});page=context.new_page()
    try:
        page.clock.install();page.set_content(HTML)
        assert action(page,'#offscreen')['ok'];assert page.locator('[data-forge-ai-target]').count()==0
        assert action(page,'#target')['ok'];assert page.locator('[data-forge-ai-target]').count()==1
        page.clock.fast_forward(3001);assert page.locator('[data-forge-ai-target]').count()==0
        assert page.evaluate('window.moves')==0
    finally:context.close()
