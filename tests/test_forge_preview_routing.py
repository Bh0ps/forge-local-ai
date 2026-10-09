"""Application preview identities cannot become extension-connected tab IDs."""
from types import SimpleNamespace
import threading
import time

import pytest

from browser_tools import BrowserTools
from native_browser import NativeBrowser
from test_forge_core import call, service
from test_native_browser import FakePage


@pytest.fixture
def preview_run(tmp_path):
    svc=service(tmp_path)
    svc.jobs._launch=lambda run: None
    svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,'memory_suggestions':False})
    folder=tmp_path/'project';folder.mkdir()
    (folder/'index.html').write_text('<button>Fixture preview</button>',encoding='utf-8')
    project=svc.create_project({'name':'Preview fixture','path':str(folder)})
    accepted=svc.jobs.start({'project_id':project['id'],'text':'Inspect this local application.'})
    run=svc.store.update_run(accepted['id'],rounds=1)
    manager=svc.get_builder()
    preview=manager.execute(run,'preview_start',{'mode':'static'})
    calls=[]
    def describe(args,context):
        calls.append(('describe',args))
        return {'not_executed':True,'error':'Connected tab is unavailable.'}
    svc.integrations=SimpleNamespace(browser=SimpleNamespace(describe_target=describe),
        execute=lambda *args,**kwargs: calls.append(('execute',args)),shutdown=lambda: None)
    yield svc,run,manager,preview,calls
    svc.shutdown()


def execute_browser(svc,run,identifier):
    schema=next(item for item in BrowserTools.schemas() if item['function']['name']=='browser_inspect')
    return svc.jobs._execute_call(run,{'cancel':threading.Event()},call('browser_inspect',{'tab_id':identifier}),
        0,{'browser_inspect':schema},{'capabilities':['tools']},time.monotonic())['result']


def test_preview_id_preflight_returns_recovery_before_any_extension_action(preview_run):
    svc,run,manager,preview,attempts=preview_run
    result=execute_browser(svc,run,preview['id'])
    assert result['not_executed'] and result['suggested_tool']=='preview_inspect'
    assert result['suggested_arguments']=={'id':preview['id']} and attempts==[]
    assert preview['id_kind']=='application_preview' and preview['next_action']=={
        'tool':'preview_inspect','arguments':{'id':preview['id']}}
    manager.previews.browser=NativeBrowser(svc.store.home,view_factory=FakePage,
        navigation_guard=manager.previews.allowed_url,panel_id='forge-preview-panel')
    inspected=manager.execute(run,result['suggested_tool'],result['suggested_arguments'])
    assert inspected['ok'] and inspected['url']==preview['url'] and inspected['snapshot_id']
    assert manager.previews.browser_preview==preview['id'] and attempts==[]


@pytest.mark.parametrize('scope',['other_project','other_run','worktree'])
def test_preview_hint_does_not_confirm_out_of_scope_identity(preview_run,tmp_path,scope):
    svc,run,manager,preview,attempts=preview_run
    if scope=='other_project':
        other=tmp_path/'other';other.mkdir()
        project=svc.create_project({'name':'Other fixture','path':str(other)})
        svc.store.save_entity('previews',{**preview,'project_id':project['id']})
    elif scope=='other_run':
        svc.store.save_entity('previews',{**preview,'run_id':'other-run'})
    else:run={**run,'workspace_project_id':'worktree-project'}
    result=execute_browser(svc,run,preview['id'])
    assert result=={'not_executed':True,'error':'Connected tab is unavailable.'}
    assert 'suggested_arguments' not in result and preview['id'] not in str(result) and preview['url'] not in str(result)
    assert len(attempts)==1 and attempts[0][0]=='describe'


def test_preview_hint_does_not_bypass_disabled_browser_permission(preview_run):
    svc,run,manager,preview,attempts=preview_run
    svc.store.update_settings({'browser_tools':False})
    result=execute_browser(svc,run,preview['id'])
    assert result['not_executed'] and 'Tool denied' in result['error'] and attempts==[]
    assert 'suggested_arguments' not in result


def test_connected_tab_routing_is_not_replaced_by_preview_tools(preview_run):
    svc,run,manager,preview,attempts=preview_run
    result=execute_browser(svc,run,'fixture-connected-tab')
    assert result=={'not_executed':True,'error':'Connected tab is unavailable.'}
    assert attempts==[('describe',{'tab_id':'fixture-connected-tab'})]


def test_preview_actions_still_reject_another_project(preview_run,tmp_path):
    svc,run,manager,preview,attempts=preview_run
    other=tmp_path/'other';other.mkdir()
    project=svc.create_project({'name':'Other fixture','path':str(other)})
    with pytest.raises(ValueError,match='another project'):
        manager.execute({**run,'project_id':project['id']},'preview_inspect',{'id':preview['id']})
    assert manager.previews.browser is None and attempts==[]


def test_hidden_preview_capture_returns_open_then_capture_recovery(preview_run):
    svc,run,manager,preview,attempts=preview_run
    class EmbeddedPage(FakePage):
        embedded=True
        visible=False
        def show(self):
            super().show();self.visible=True
    manager.previews.browser=NativeBrowser(svc.store.home,view_factory=EmbeddedPage,
        navigation_guard=manager.previews.allowed_url,panel_id='forge-preview-panel')
    blocked=manager.execute(run,'preview_screenshot',{'id':preview['id']})
    assert blocked['not_executed'] and blocked['next_action']=={'tool':'preview_open','arguments':{'id':preview['id']}}
    assert not manager.previews.browser.view.visible
    opened=manager.execute(run,blocked['next_action']['tool'],blocked['next_action']['arguments'])
    assert opened['ok'] and manager.previews.browser.view.visible
    captured=manager.execute(run,blocked['retry_action']['tool'],blocked['retry_action']['arguments'])
    assert captured['content'][0]['type']=='image' and captured['artifact'].endswith('.png')
    assert attempts==[]
