"""The browser overlay cannot trust frontend coordinates or cover other views."""
from copy import deepcopy
import threading

import pytest

from embedded_browser import validate_bounds
from native_browser import BrowserGuard, NativeBrowser
from test_native_browser import FakePage


def layout():
    return {'visible':True, 'hud':False, 'modal':False,
            'rect':{'x':800,'y':100,'width':400,'height':600},
            'viewport':{'width':1200,'height':800}}


def request():
    value = layout()
    return {'panel_id':'forge-browser-panel','rect':value['rect'],'viewport':value['viewport']}


@pytest.mark.parametrize('scale', [1,1.25,1.5,2,3])
def test_overlay_bounds_follow_actual_owner_pixels_at_different_dpi(scale):
    assert validate_bounds(request(), layout(), (0,0,1200*scale,800*scale)) == (
        round(800*scale), round(100*scale), round(400*scale), round(600*scale))


@pytest.mark.parametrize('change', ['hud','modal','hidden','outside','negative','nan','claim','viewport','zoom','panel'])
def test_overlay_rejects_stale_untrusted_or_obscured_layout(change):
    state, data, client = layout(), request(), (0,0,1200,800)
    if change == 'hud': state['hud'] = True
    if change == 'modal': state['modal'] = True
    if change == 'hidden': state['visible'] = False
    if change == 'outside': state['rect']['width'] = 500
    if change == 'negative': state['rect']['x'] = -1
    if change == 'nan': data['rect']['width'] = float('nan')
    if change == 'claim': data['rect']['x'] = 0
    if change == 'viewport': data['viewport']['width'] = 9999
    if change == 'zoom': client = (0,0,1200,1600)
    if change == 'panel': data['panel_id'] = 'unrelated-button'
    with pytest.raises(BrowserGuard): validate_bounds(data, state, client)


class EmbeddedFixture(FakePage):
    embedded = True
    visible = False
    disposed = False
    def bind(self, data):
        validate_bounds(data, layout(), (0,0,1200,800))
        self.visible = True
    def hide(self): self.visible = False
    def close(self): self.hide()
    def dispose(self): self.disposed = True; self.changed('closed')


def test_native_open_hide_and_hud_preserve_browser_state_until_shutdown(tmp_path):
    pages = []
    def factory(changed):
        view = EmbeddedFixture(changed); pages.append(view); return view
    browser = NativeBrowser(tmp_path, view_factory=factory)
    browser.dispatch('open', {'url':'https://example.org/research'})
    page = pages[0]
    assert not page.visible and page.url == 'https://example.org/research'
    browser.dispatch('show', request()); assert page.visible
    browser.dispatch('hide'); assert not page.visible and not page.disposed
    browser.dispatch('resize', request()); assert pages == [page] and page.visible
    browser.dispatch('close'); assert not page.visible and browser.status()['running']
    browser.shutdown(); assert page.disposed


def test_invalid_browser_panel_request_has_no_navigation_effect(tmp_path):
    browser = NativeBrowser(tmp_path, view_factory=lambda changed: EmbeddedFixture(changed))
    try:
        browser.dispatch('open')
        data = request(); data['rect']['x'] = 0
        with pytest.raises(BrowserGuard): browser.dispatch('show', data)
        assert not browser.view.visible and not browser.view.effects
    finally: browser.shutdown()


def test_explicit_preview_open_requests_below_fold_pane_and_preserves_contained_layout():
    from embedded_browser import EmbeddedWebView2Page
    snapshot = layout()
    snapshot.update(present=True, displayed=True, preview_id='preview', pane_token='mount')
    snapshot['rect'].update(y=720, height=240)
    page = EmbeddedWebView2Page.__new__(EmbeddedWebView2Page)
    page.panel_id = 'forge-preview-panel'
    page._layout = lambda: deepcopy(snapshot)
    opened, bound = [], []
    def dispatch(script):
        opened.append(script)
        snapshot['visible'] = True
        snapshot['rect']['y'] = 100
        return True
    def bind(data=None, check=None):
        if check: check()
        bound.append(validate_bounds({'panel_id': page.panel_id}, snapshot,
            (0, 0, 1200, 800), page.panel_id))
    page._main_evaluate, page.bind = dispatch, bind
    page.show(activate=False)
    assert len(opened) == 1 and 'forge:preview-open' in opened[0]
    assert bound[-1] == (800, 100, 400, 240)
    page.show(activate=False)
    assert len(opened) == 1 and len(bound) == 2
    snapshot['visible'] = False
    page.show(activate=False)
    assert len(opened) == 2 and len(bound) == 3
    page.show(activate=False, request_open=True)
    assert len(opened) == 3 and len(bound) == 4


def test_preview_identity_rejects_a_previous_preview_or_pane_instance():
    from embedded_browser import EmbeddedWebView2Page
    page = EmbeddedWebView2Page.__new__(EmbeddedWebView2Page)
    page.panel_id = 'forge-preview-panel'
    page.preview_id = 'current-preview'
    state = {**layout(), 'preview_id': 'current-preview', 'pane_token': 'current-mount'}
    page._layout = lambda: state
    assert page._contained_layout()
    with pytest.raises(BrowserGuard, match='panel changed'):
        page._preview_identity(state, {'pane_token': 'previous-mount'})
    state['preview_id'] = 'previous-preview'
    assert not page._contained_layout()


@pytest.fixture
def attachment(monkeypatch):
    """Real show/bind guard paths with a fake native owner and synthetic time."""
    import sys
    from types import ModuleType, SimpleNamespace
    import embedded_browser
    from embedded_browser import EmbeddedWebView2Page
    drawing = ModuleType('System.Drawing')
    drawing.Rectangle = lambda *values: values
    monkeypatch.setitem(sys.modules, 'System.Drawing', drawing)
    clock = SimpleNamespace(now=0., after_pause=lambda: None)
    def pause(seconds):
        clock.now += seconds
        clock.after_pause()
    monkeypatch.setattr(embedded_browser, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=pause))
    good = {**layout(), 'present': True, 'displayed': True, 'contained': True,
        'preview_id': 'preview', 'pane_token': 'mount'}
    states, reads, effects = [deepcopy(good)], [], []
    page = EmbeddedWebView2Page.__new__(EmbeddedWebView2Page)
    page.panel_id, page.preview_id, page.disposed = 'forge-preview-panel', 'preview', False
    page.visible, page.bound = False, None
    page.changed = lambda kind: effects.append(kind)
    page.container = SimpleNamespace(Visible=False, BringToFront=lambda: effects.append('position'))
    page.control = SimpleNamespace(CoreWebView2=SimpleNamespace(ExecuteScriptAsync=lambda _: None),
        Focus=lambda: effects.append('focus'))
    owner = SimpleNamespace(Visible=True, Show=lambda: effects.append('show'),
        Activate=lambda: effects.append('activate'), webview=SimpleNamespace(Left=0, Top=0,
            ClientSize=SimpleNamespace(Width=1200, Height=800)))
    page.ui = lambda callback: callback(owner)
    def read():
        value = deepcopy(states[min(len(reads), len(states)-1)])
        reads.append(value)
        return value
    page._layout = read
    page._main_evaluate = lambda script: effects.append('open-event') or True
    return SimpleNamespace(page=page, owner=owner, good=good, states=states,
        reads=reads, effects=effects, clock=clock)


@pytest.mark.parametrize('geometry', ['clip', 'outside', 'zero'])
def test_same_opening_waits_for_transient_second_read_geometry_without_reopening(attachment, geometry):
    value = attachment
    pending = deepcopy(value.good)
    if geometry == 'clip': pending.update(visible=False, contained=False)
    if geometry == 'outside': pending['rect'].update(y=720, height=240)
    if geometry == 'zero': pending.update(visible=False); pending['rect']['height'] = 0
    value.states[:] = [value.good, pending, value.good, value.good]
    value.page.show(activate=False, request_open=True)
    assert value.page.visible and value.clock.now == .05
    assert value.effects.count('open-event') == value.effects.count('position') == 1
    assert not set(value.effects) & {'focus', 'show', 'activate'}
    assert len(value.reads) == 4


def test_one_opening_allows_initial_render_but_never_adopts_a_remounted_pane(attachment):
    value = attachment
    value.states[:] = [{'present': False, 'visible': False}, value.good, value.good]
    value.page.show(activate=False, request_open=True)
    assert value.clock.now == .05 and value.effects.count('position') == 1
    value.reads.clear(); value.effects.clear(); value.clock.now = 0
    pending = {**value.good, 'visible': False, 'contained': False}
    remounted = {**value.good, 'pane_token': 'new-mount'}
    value.states[:] = [value.good, pending, remounted]
    with pytest.raises(BrowserGuard, match='panel changed'):
        value.page.show(activate=False, request_open=True)
    assert not value.page.visible and value.effects.count('position') == 0
    assert value.effects.count('open-event') == 1 and value.clock.now == .05


@pytest.mark.parametrize('blocker', ['modal', 'hud', 'css-hidden', 'ancestor-hidden', 'missing', 'preview', 'token', 'owner', 'cancel'])
def test_second_read_blockers_cannot_be_retried_as_transient_geometry(attachment, blocker):
    value = attachment
    blocked = deepcopy(value.good)
    if blocker in ('modal', 'hud'): blocked[blocker] = True
    if blocker in ('css-hidden', 'ancestor-hidden'): blocked.update(visible=False, displayed=False)
    if blocker == 'missing': blocked = {'present': False, 'visible': False}
    if blocker == 'preview': blocked['preview_id'] = 'foreign-preview'
    if blocker == 'token': blocked['pane_token'] = 'foreign-mount'
    if blocker in ('owner', 'cancel'): blocked.update(visible=False, contained=False)
    value.states[:] = [value.good, blocked, value.good]
    def check():
        if len(value.reads) >= 2:
            if blocker == 'owner': value.owner.Visible = False
            if blocker == 'cancel': raise BrowserGuard('Browser action cancelled.')
    with pytest.raises(BrowserGuard):
        value.page.show(activate=False, check=check, request_open=True)
    assert not value.page.visible and value.clock.now == 0
    assert value.effects.count('open-event') == 1 and value.effects.count('position') == 0


@pytest.mark.parametrize('end', ['deadline', 'cancel', 'closed', 'modal', 'hud'])
def test_pending_geometry_retains_opening_deadline_and_security_guards(attachment, end):
    value = attachment
    pending = {**value.good, 'visible': False, 'contained': False}
    value.states[:] = [value.good, pending]
    cancelled = threading.Event()
    def change():
        if end == 'cancel': cancelled.set()
        elif end == 'closed': value.states.append({'present': False, 'visible': False})
        elif end in ('modal', 'hud'): value.states.append({**pending, end: True})
    value.clock.after_pause = change
    def check():
        if cancelled.is_set(): raise BrowserGuard('Browser action cancelled.')
    with pytest.raises(TimeoutError if end == 'deadline' else BrowserGuard):
        value.page.show(activate=False, check=check, request_open=True)
    assert not value.page.visible and value.effects.count('position') == 0
    assert value.effects.count('open-event') == 1
    assert value.clock.now == (10 if end == 'deadline' else .05)


def test_late_native_attachment_cannot_position_after_deadline(attachment):
    value = attachment
    def ui(callback):
        if callback.__name__ == 'position': value.clock.now = 10
        return callback(value.owner)
    value.page.ui = ui
    with pytest.raises(TimeoutError, match='opening timeout'):
        value.page.show(activate=False, request_open=True)
    assert value.effects.count('position') == 0 and not value.page.visible
