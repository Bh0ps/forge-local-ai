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
    def bind():
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
