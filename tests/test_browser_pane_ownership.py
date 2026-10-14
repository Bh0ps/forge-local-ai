"""Real serialized broker and embedded methods; only UI controls/DOM are fakes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import sqlite3
import sys
import threading
import time
from types import ModuleType,SimpleNamespace

import pytest

from browser_tools import BrowserTools
from embedded_browser import EmbeddedWebView2Page
from native_browser import BrowserGuard,NativeBrowser,CLEAR_TARGET_SCRIPT


class Page(EmbeddedWebView2Page):
    def __init__(self,changed):
        self.changed=changed;self.panel_id='forge-browser-panel';self.url='about:blank'
        self.visible=False;self.disposed=False;self.bound=None;self.events=[];self.before=None;self.after=None
        self.container=SimpleNamespace(Visible=False,BringToFront=lambda:self.events.append('bind'))
        core=SimpleNamespace(ExecuteScriptAsync=lambda script:self.events.append('clear'))
        self.control=SimpleNamespace(ContainsFocus=False,CoreWebView2=core,Focus=lambda:self.events.append('focus'))
        self.owner=SimpleNamespace(Visible=True,Show=lambda:self.events.append('show'),Activate=lambda:self.events.append('activate'),
            webview=SimpleNamespace(Left=0,Top=0,ClientSize=SimpleNamespace(Width=1200,Height=800)))
        self.ui=lambda callback:callback(self.owner)
    def ready(self):pass
    def _layout(self):
        return {'visible':True,'hud':False,'modal':False,'rect':{'x':800,'y':100,'width':400,'height':600},
            'viewport':{'width':1200,'height':800}}
    def _main_evaluate(self,script):raise AssertionError('Agent cannot reopen the pane')
    def navigate(self,url,check=None):
        if self.before:self.before('navigate')
        if check:check()
        self.events.append('navigate');self.url=url;self.changed('navigation',url);self.changed('loaded')
        if self.after:self.after('navigate')
    def evaluate(self,script,check=None):
        kind='clear' if script==CLEAR_TARGET_SCRIPT else 'inspect' if 'const key=' in script else 'validate' if '"operation": "validate"' in script else 'effect'
        if self.before:self.before(kind)
        if check:check()
        if kind=='clear':return True
        if kind=='inspect':return {'url':self.url,'title':'Synthetic','text':'Synthetic','targets':[{'selector':'button','signature':['BUTTON']}]}
        if kind=='validate':return {'ok':True}
        self.events.append('effect')
        if self.after:self.after(kind)
        return {'ok':True}
    def screenshot(self,check=None):
        if check:check()
        self.events.append('capture');return b'inert fixture'
    def stop(self):self.events.append('stop')
    def dispose(self):self.disposed=True;self._hide_ui();self.changed('closed')


@pytest.fixture
def broker(tmp_path,monkeypatch):
    system=ModuleType('System');system.__path__=[]
    drawing=ModuleType('System.Drawing');drawing.Rectangle=lambda *args:args
    monkeypatch.setitem(sys.modules,'System',system);monkeypatch.setitem(sys.modules,'System.Drawing',drawing)
    value=NativeBrowser(tmp_path,view_factory=Page,session_check=lambda:True,clock=lambda:0,
        indicator_scope=lambda run,scope:run=='run-a' and scope=={'chat_id':'chat-a','run_id':'run-a'})
    value.dispatch('show',{'panel_id':'forge-browser-panel','rect':{'x':800,'y':100,'width':400,'height':600},'viewport':{'width':1200,'height':800}})
    value.view.events.clear()
    yield value
    value.shutdown()


def args(broker):
    result=broker.execute('browser_inspect',{}, {'run_id':'run-a'})
    assert result['ok'];return {'snapshot_id':result['snapshot_id'],'selector':'button'}


def wait_for(predicate):
    deadline=time.monotonic()+2
    while not predicate() and time.monotonic()<deadline:threading.Event().wait(.001)
    assert predicate(),'The bounded synthetic queue checkpoint did not arrive'


@pytest.mark.parametrize('name',['browser_click','browser_type','browser_key','browser_select','browser_scroll',
    'browser_wait','browser_navigate','browser_inspect','browser_screenshot'])
def test_hide_revokes_all_research_work_without_show_activation_or_dom(broker,name):
    data=args(broker);data.update(text='Synthetic',key='Enter',value='one',url='https://example.invalid')
    page=broker.view;generation=broker.generation
    assert broker.dispatch('hide')['ok'];assert broker.snapshot is None and broker.generation==generation
    result=broker.execute(name,data,{'run_id':'run-a'})
    assert result['not_executed'] and result['reason']=='browser_hidden'
    assert not any(event in page.events for event in ('show','activate','focus','effect','navigate','capture'))
    assert broker.view is page  # Document lifetime is retained.


def test_background_dom_action_has_no_host_focus_and_snapshot_is_consumed(broker):
    data=args(broker);assert broker.view.control.ContainsFocus is False
    assert broker.execute('browser_click',data,{'run_id':'run-a'})['ok']
    assert broker.view.events.count('effect')==1
    assert not any(event in broker.view.events for event in ('show','activate','focus'))
    assert broker.status({'chat_id':'chat-a','run_id':'run-a'})['target_indicator']=='active'
    assert broker.status({'chat_id':'other','run_id':'run-a'})['target_indicator']=='none'
    assert broker.status()['target_indicator']=='none'
    assert broker.execute('browser_click',data,{'run_id':'run-a'})['not_executed']
    assert broker.status({'chat_id':'chat-a','run_id':'run-a'})['target_indicator']=='none'


@pytest.mark.parametrize('action',['show','resize'])
def test_hide_synchronously_fences_queued_show_or_resize(broker,action):
    entered,release=threading.Event(),threading.Event()
    hold=broker._submit(lambda:(entered.set(),release.wait(3)))
    assert entered.wait(1)
    with ThreadPoolExecutor(2) as workers:
        stale=workers.submit(broker.dispatch,action,{'rect':broker.view._layout()['rect']})
        # Submission order is observed under the same counted broker lock.
        wait_for(lambda:broker.pending_io()>=2)
        hiding=workers.submit(broker.dispatch,'hide')
        wait_for(lambda:not broker._pane_allowed)
        assert not hiding.done() and broker.snapshot is None
        release.set();hold.result(1)
        with pytest.raises(BrowserGuard) as rejected:stale.result(2)
        assert rejected.value.reason=='browser_owner_changed'
        assert hiding.result(2)['ok']
    assert not any(event in broker.view.events for event in ('bind','show','activate','focus'))
    broker.dispatch('show',{'rect':broker.view._layout()['rect']})
    assert broker.execute('browser_inspect',{}, {'run_id':'run-a'})['ok']


@pytest.mark.parametrize('stage',['validate','effect'])
def test_hide_during_ui_marshalling_refuses_before_dom_dispatch(broker,stage):
    data=args(broker)
    def revoke(kind):
        if kind==stage:broker._revoke_pane()
    broker.view.before=revoke
    result=broker.execute('browser_click',data,{'run_id':'run-a'})
    assert result['not_executed'] and 'effect' not in broker.view.events


def test_hide_after_dom_dispatch_is_unknown_and_cannot_replay(broker):
    data=args(broker)
    broker.view.after=lambda kind:broker._revoke_pane() if kind=='effect' else None
    with pytest.raises(RuntimeError,match='outcome is unknown'):broker.execute('browser_click',data,{'run_id':'run-a'})
    assert broker.view.events.count('effect')==1 and broker.snapshot is None
    broker.view.after=None;broker.dispatch('show',{'rect':broker.view._layout()['rect']})
    assert broker.execute('browser_click',data,{'run_id':'run-a'})['not_executed']
    assert broker.view.events.count('effect')==1


@pytest.mark.parametrize('revocation',['hide','agent-close'])
def test_admission_revokes_a_queued_agent_before_hide_or_close_ack(broker,revocation):
    data=args(broker);entered,release=threading.Event(),threading.Event()
    hold=broker._submit(lambda:(entered.set(),release.wait(3)))
    assert entered.wait(1)
    with ThreadPoolExecutor(2) as workers:
        action=workers.submit(broker.execute,'browser_click',data,{'run_id':'run-a'})
        wait_for(lambda:broker.pending_io()>=2)
        closing=workers.submit(broker.dispatch,'hide') if revocation=='hide' else workers.submit(broker.execute,'browser_close',{}, {'run_id':'run-a'})
        wait_for(lambda:not broker._pane_allowed)
        assert not closing.done() and broker.snapshot is None
        release.set();hold.result(1)
        result=action.result(2)
        assert result['not_executed'] and result['reason']=='browser_owner_changed'
        assert closing.result(2)['ok']
    assert 'effect' not in broker.view.events and not broker.view.visible


def test_cancelled_agent_close_does_not_revoke_a_current_ui_owner(broker):
    cancelled=threading.Event();cancelled.set()
    result=broker.execute('browser_close',{}, {'run_id':'run-a','cancel':cancelled})
    assert result['not_executed'] and broker._pane_allowed and broker.view.visible
    assert args(broker)['snapshot_id']


def test_session_cancellation_after_slow_read_does_not_dispatch_dom(broker):
    data=args(broker);cancelled=threading.Event()
    def session():cancelled.set();return True
    broker.session_check=session
    result=broker.execute('browser_click',data,{'run_id':'run-a','cancel':cancelled})
    assert result['not_executed'] and 'effect' not in broker.view.events


def test_owner_hide_stop_and_screenshot_clear_the_private_indicator(broker):
    data=args(broker);broker.execute('browser_click',data,{'run_id':'run-a'})
    broker.execute('browser_screenshot',{}, {'run_id':'run-a'})
    assert broker.status({'chat_id':'chat-a','run_id':'run-a'})['target_indicator']=='none'
    data=args(broker);broker.execute('browser_click',data,{'run_id':'run-a'})
    broker.view._hide_ui()
    assert broker.snapshot is None and broker.status({'chat_id':'chat-a','run_id':'run-a'})['target_indicator']=='none'
    broker.dispatch('show',{'rect':broker.view._layout()['rect']});data=args(broker)
    broker.execute('browser_click',data,{'run_id':'run-a'});broker.stop()
    assert broker.status({'chat_id':'chat-a','run_id':'run-a'})['target_indicator']=='none'


def test_actual_service_integration_registry_share_the_hide_authority(broker):
    from forge_integrations import IntegrationHub
    from forge_runs import ToolRegistry
    from forge_service import ForgeService
    tools=BrowserTools.__new__(BrowserTools);tools.closed=False;tools.native=broker;tools.bridge=None
    hub=IntegrationHub.__new__(IntegrationHub);hub.browser=tools
    service=ForgeService.__new__(ForgeService);service.admission_lock=threading.RLock()
    service.integrations=hub;service.store=SimpleNamespace();service.stop_event=threading.Event()
    service.update_manager=service.performance_manager=None;service._native_io_pending=0
    registry=ToolRegistry(service);run={'id':'run-a','project_id':None,'settings':{'browser_tools':True}}
    snapshot=registry.execute(run,'browser_inspect',{},threading.Event())
    assert snapshot['ok']
    assert service.dispatch('browser_native_hide',{})['ok']
    result=registry.execute(run,'browser_click',{'snapshot_id':snapshot['snapshot_id'],'selector':'button'},threading.Event())
    assert result['not_executed'] and result['reason']=='browser_hidden'
    assert 'effect' not in broker.view.events and not broker.view.visible
    assert service._native_io_pending==0


@pytest.mark.parametrize('case',['direct','child','retired-child','foreign-chat','unselected','missing','cycle','too-deep'])
def test_indicator_scope_uses_only_actual_bounded_ancestry(case):
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE runs(id TEXT,chat_id TEXT,parent_id TEXT)')
    rows=[('root','chat-a',None),('child','helper-chat','root'),('retired','removed-helper-chat','root')]
    rows.extend((f'd{i}','helper-chat',f'd{i+1}' if i<16 else 'root') for i in range(17))
    rows.extend([('loop-a','chat-a','loop-b'),('loop-b','chat-a','loop-a')]);db.executemany('INSERT INTO runs VALUES(?,?,?)',rows)
    @contextmanager
    def connection(**_):yield db
    store=SimpleNamespace(_connection=connection)
    owner={'direct':'root','child':'child','retired-child':'retired','missing':'absent','cycle':'loop-a','too-deep':'d0'}.get(case,'child')
    scope={'chat_id':'other' if case=='foreign-chat' else 'chat-a','run_id':None if case=='unselected' else 'root'}
    assert BrowserTools.native_indicator_scope(store,owner,scope) is (case in ('direct','child','retired-child'))
    db.close()
