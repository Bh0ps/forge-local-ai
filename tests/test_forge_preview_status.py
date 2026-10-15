"""The workspace can discover a preview while its native opening waits on UI."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

import native_browser

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


def test_explicit_preview_open_budgets_navigation_after_initialization_and_does_not_wait_for_rect_binds(tmp_path, monkeypatch):
    # Initialization can take longer than the old smoke's twenty-second polling
    # budget. Synthetic time avoids starting a visible desktop or waiting here.
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(native_browser, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    svc = service(tmp_path)
    folder = tmp_path/'project'; folder.mkdir()
    (folder/'index.html').write_text('<p>Opening fixture</p>', encoding='utf-8')
    project = svc.create_project({'name': 'Opening fixture', 'path': str(folder)})
    run = {'id': 'opening-fixture-run', 'project_id': project['id']}
    manager = svc.get_builder()
    preview = manager.execute(run, 'preview_start', {'mode': 'static'})
    class SlowPage(FakePage):
        embedded = True
        visible = False
        def ready(self): clock.now += 25
        def navigate(self, url):
            self.url = url
            self.effects.append('navigate')
            self.changed('navigation', url)
        def show(self):
            self.visible = True
            super().show()
        def bind(self, data):
            self.visible = True
            self.effects.append('bind')
        def preflight(self):
            assert self.visible
    browser = manager.previews.browser = NativeBrowser(svc.store.home, view_factory=SlowPage,
        navigation_guard=manager.previews.allowed_url, panel_id='forge-preview-panel')
    def complete(seconds):
        clock.now += seconds
        browser.view.changed('loaded')
        return False
    browser.cancelled.wait = complete
    try:
        opened = manager.execute(run, 'preview_open', {'id': preview['id']})
        assert opened['ok'] and opened['visible'] and not opened['loading']
        assert clock.now == 25.05 and browser.view.effects.count('navigate') == 1
        assert manager.execute(run, 'preview_inspect', {'id': preview['id']})['ok']
        browser.view.changed('navigation', preview['url'])
        # Geometry updates must not hold up the frontend waiting for navigation.
        monkeypatch.setattr(browser, 'wait_ready', lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('Frontend rectangle binds cannot wait for navigation.')))
        bound = manager.previews.browser_action('show', {'id': preview['id'], 'rect': {}})
        assert bound['ok'] and bound['loading'] and browser.view.effects.count('bind') == 1
        assert clock.now == 25.05
    finally:
        svc.shutdown()


@pytest.mark.parametrize('revoke', ['run-cancel', 'owner-change', 'preview-stop', 'browser-stop'])
def test_pending_preview_attachment_rechecks_run_and_manager_ownership_before_position(tmp_path, revoke):
    from native_browser import BrowserGuard
    svc = service(tmp_path)
    folder = tmp_path/'project'; folder.mkdir()
    (folder/'index.html').write_text('<p>Pending fixture</p>', encoding='utf-8')
    project = svc.create_project({'name': 'Pending fixture', 'path': str(folder)})
    manager = svc.get_builder()
    run = {'id': 'pending-fixture-run', 'project_id': project['id']}
    preview = manager.execute(run, 'preview_start', {'mode': 'static'})
    cancel, positions = threading.Event(), []
    active = manager.previews.active[preview['id']]
    class PendingPage(FakePage):
        embedded = supports_pane_guard = True
        visible = False
        def navigate(self, url, check=None):
            if check: check()
            super().navigate(url)
        def open_pane(self, check=None):
            check()
            if revoke == 'run-cancel': cancel.set()
            elif revoke == 'owner-change': manager.previews.browser_preview = 'other-preview'
            elif revoke == 'preview-stop': manager.previews.active.pop(preview['id'])
            else: browser.stop()
            check()  # Same check passed into real bind before native positioning.
            positions.append(True)
            self.visible = True
    browser = manager.previews.browser = NativeBrowser(svc.store.home, view_factory=PendingPage,
        navigation_guard=manager.previews.allowed_url, panel_id='forge-preview-panel')
    try:
        with pytest.raises(BrowserGuard, match='cancelled|changed'):
            manager.execute(run, 'preview_open', {'id': preview['id']}, cancel)
        assert not positions and not browser.view.visible
        assert browser.view.effects.count('navigate') == 1
    finally:
        manager.previews.active[preview['id']] = active
        manager.previews.browser_preview = preview['id']
        svc.shutdown()


def test_cancelled_preview_open_never_creates_a_view_or_navigates(tmp_path):
    from native_browser import BrowserGuard
    svc = service(tmp_path)
    folder = tmp_path/'project'; folder.mkdir()
    (folder/'index.html').write_text('<p>Cancelled fixture</p>', encoding='utf-8')
    project = svc.create_project({'name': 'Cancelled fixture', 'path': str(folder)})
    manager = svc.get_builder()
    run = {'id': 'cancelled-fixture-run', 'project_id': project['id']}
    preview = manager.execute(run, 'preview_start', {'mode': 'static'})
    created = []
    manager.previews.browser = NativeBrowser(svc.store.home,
        view_factory=lambda changed: created.append(True) or FakePage(changed),
        navigation_guard=manager.previews.allowed_url, panel_id='forge-preview-panel')
    cancel = threading.Event(); cancel.set()
    try:
        with pytest.raises(BrowserGuard, match='cancelled'):
            manager.previews.browser_action('show', {'id': preview['id']}, {'run_id': run['id'], 'cancel': cancel})
        assert not created and manager.previews.browser.view is None
    finally:
        svc.shutdown()


def test_cancelled_queued_open_cannot_clear_a_previous_browser_stop(tmp_path):
    from native_browser import BrowserGuard
    created = []
    browser = NativeBrowser(tmp_path, view_factory=lambda changed: created.append(True) or FakePage(changed))
    browser.stop()
    cancel = threading.Event(); cancel.set()
    try:
        with pytest.raises(BrowserGuard, match='cancelled'):
            browser.dispatch('show', context={'cancel': cancel})
        assert browser.cancelled.is_set() and not created
    finally:
        browser.shutdown()
