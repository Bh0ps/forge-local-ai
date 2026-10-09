"""The workspace can discover a preview while its native opening waits on UI."""
from concurrent.futures import ThreadPoolExecutor

from native_browser import NativeBrowser
from test_forge_core import service
from test_native_browser import FakePage


def test_preview_open_allows_workspace_status_lookup_without_a_builder(tmp_path):
    svc=service(tmp_path)
    folder=tmp_path/'project';folder.mkdir()
    (folder/'index.html').write_text('<p>Standalone fixture</p>',encoding='utf-8')
    project=svc.create_project({'name':'Standalone fixture','path':str(folder)})
    run={'id':'standalone-fixture-run','project_id':project['id']}
    manager=svc.get_builder()
    preview=manager.execute(run,'preview_start',{'mode':'static'})
    lookups=[]
    with ThreadPoolExecutor(max_workers=1) as workspace:
        class WorkspacePage(FakePage):
            embedded=True
            def show(self):
                # A real native show waits for React to find and bind the pane.
                # React reads this metadata from another coordinator worker.
                metadata=workspace.submit(manager.previews.browser_action,'status',{}).result(timeout=1)
                lookups.append(metadata)
                super().show()
        manager.previews.browser=NativeBrowser(svc.store.home,view_factory=WorkspacePage,
            navigation_guard=manager.previews.allowed_url,panel_id='forge-preview-panel')
        try:
            result=manager.execute(run,'preview_open',{'id':preview['id']})
            assert result['running'] and lookups
            assert lookups[0]['preview_id']==preview['id']
            assert lookups[0]['project_id']==project['id'] and lookups[0]['builder_id'] is None
            assert svc.store.entities('builders')==[]
        finally:
            svc.shutdown()
