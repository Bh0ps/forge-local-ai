"""Native browser tests use a disposable fake view, never another app/tab."""
import json
import threading

import pytest

from native_browser import NativeBrowser


class FakePage:
    def __init__(self, changed):
        self.changed = changed
        self.url = 'about:blank'
        self.focus = True
        self.effects = []
        self.stale = False
        self.timeout_after_click = False
    def ready(self): pass
    def show(self): self.focus = True; self.effects.append('focus')
    def focused(self): return self.focus
    def navigate(self, url):
        self.url = url
        self.effects.append('navigate')
        self.changed('navigation', url)
        self.changed('loaded')
    def evaluate(self, script):
        if 'const key=' in script:
            return {'url': self.url, 'title': 'Disposable fixture', 'text': 'Read-only fixture',
                    'targets': [{'selector': '[data-forge-native="test"]', 'signature': ['button'], 'label': 'Fixture button'}]}
        if self.stale:
            return {'ok': False, 'error': 'Browser target changed', 'not_executed': True}
        if '"operation": "validate"' in script:
            return {'ok': True}
        self.effects.append('click' if '"operation": "browser_click"' in script else 'type')
        if self.timeout_after_click:
            raise TimeoutError('Action may already have completed')
        return {'ok': True}
    def screenshot(self): return b'fixture screenshot'
    def history(self, name): self.effects.append(name)
    def stop(self): self.effects.append('stop')
    def close(self): self.changed('closed')


def browser_for(tmp_path, now=None):
    views = []
    def create(changed):
        page = FakePage(changed)
        views.append(page)
        return page
    return NativeBrowser(tmp_path, view_factory=create, clock=lambda: now[0] if now else 0), views


def snapshot(browser, run='first'):
    if browser.view is None:
        browser.dispatch('open')
        browser.view.effects.clear()
    return browser.execute('browser_inspect', {}, {'run_id': run})


def test_native_browser_uses_owned_page_and_run_bound_snapshot(tmp_path):
    browser, views = browser_for(tmp_path)
    try:
        assert browser.status()['available'] and browser.status()['engine'] == 'WebView2'
        browser.dispatch('open', {'url': 'https://example.org'})
        inspected = snapshot(browser)
        args = {'selector': inspected['targets'][0]['selector'], 'snapshot_id': inspected['snapshot_id']}
        result = browser.execute('browser_click', args, {'run_id': 'other'})
        assert result['not_executed'] and 'click' not in views[0].effects
        assert browser.execute('browser_click', args, {'run_id': 'first'})['ok']
        assert views[0].effects.count('click') == 1
        assert browser.execute('browser_click', args, {'run_id': 'first'})['not_executed']
    finally:
        browser.shutdown()


def test_native_browser_expiry_navigation_and_cancel_reject_before_effect(tmp_path):
    now = [0]
    browser, views = browser_for(tmp_path, now)
    try:
        inspected = snapshot(browser)
        args = {'selector': inspected['targets'][0]['selector'], 'snapshot_id': inspected['snapshot_id']}
        now[0] = 31
        assert browser.execute('browser_click', args, {'run_id': 'first'})['not_executed']
        now[0] = 0
        inspected = snapshot(browser)
        args['snapshot_id'] = inspected['snapshot_id']
        views[0].changed('navigation', 'https://example.org/new')
        assert browser.execute('browser_click', args, {'run_id': 'first'})['not_executed']
        cancel = threading.Event()
        cancel.set()
        assert browser.execute('browser_navigate', {'url': 'https://example.org'}, {'cancel': cancel})['not_executed']
        assert 'click' not in views[0].effects and 'navigate' not in views[0].effects
    finally:
        browser.shutdown()


def test_native_dom_preflight_precedes_focus_and_ambiguous_effect_is_unknown(tmp_path):
    browser, views = browser_for(tmp_path)
    try:
        inspected = snapshot(browser)
        args = {'selector': inspected['targets'][0]['selector'], 'snapshot_id': inspected['snapshot_id']}
        page = views[0]
        page.focus = False
        page.stale = True
        assert browser.execute('browser_click', args, {'run_id': 'first'})['not_executed']
        assert not page.effects
        page.stale = False
        page.timeout_after_click = True
        with pytest.raises(TimeoutError, match='already have completed'):
            browser.execute('browser_click', args, {'run_id': 'first'})
        assert page.effects == ['focus', 'click']
    finally:
        browser.shutdown()


def test_native_browser_capture_is_artifact_and_manual_history_is_same_window(tmp_path):
    browser, views = browser_for(tmp_path)
    try:
        browser.dispatch('open')
        browser.dispatch('back')
        browser.dispatch('forward')
        browser.dispatch('reload')
        assert len(views) == 1
        assert views[0].effects == ['focus', 'focus', 'back', 'focus', 'forward', 'focus', 'reload']
        captured = browser.execute('browser_screenshot', {})
        assert captured['content'][0]['type'] == 'image'
        assert captured['artifact'].endswith('.png')
        browser.dispatch('close')
        assert browser.status()['running'] is False
    finally:
        browser.shutdown()


def test_native_locked_session_is_rejected_and_stop_cannot_replay_old_snapshot(tmp_path):
    available = [False]
    browser = NativeBrowser(tmp_path, view_factory=FakePage, session_check=lambda: available[0])
    try:
        blocked = browser.execute('browser_navigate', {'url': 'https://example.org'})
        assert blocked['not_executed'] and 'Unlock Windows' in blocked['error']
        assert browser.view is None
        available[0] = True
        inspected = snapshot(browser)
        old = {'selector': inspected['targets'][0]['selector'], 'snapshot_id': inspected['snapshot_id']}
        browser.stop()
        browser.dispatch('show')
        assert browser.execute('browser_click', old, {'run_id': 'first'})['not_executed']
        assert 'click' not in browser.view.effects
    finally:
        browser.shutdown()


def test_missing_page_and_invalid_url_reject_without_creating_window(tmp_path):
    browser, views = browser_for(tmp_path)
    try:
        for name, data in [('browser_inspect', {}), ('browser_screenshot', {}),
                           ('browser_click', {'snapshot_id': 'missing', 'selector': 'button'}),
                           ('browser_navigate', {'url': 'file:///private/local.txt'})]:
            assert browser.execute(name, data)['not_executed']
        assert views == [] and browser.view is None
    finally:
        browser.shutdown()


def test_cancel_after_approved_focus_prevents_click_without_false_no_effect_claim(tmp_path):
    browser, views = browser_for(tmp_path)
    cancel = threading.Event()
    try:
        inspected = snapshot(browser)
        page = views[0]
        page.focus = False
        show = page.show
        def focus_then_cancel():
            show()
            cancel.set()
        page.show = focus_then_cancel
        with pytest.raises(RuntimeError, match='focused, then action cancelled'):
            browser.execute('browser_click', {'snapshot_id': inspected['snapshot_id'],
                'selector': inspected['targets'][0]['selector']}, {'run_id': 'first', 'cancel': cancel})
        assert page.effects == ['focus']
    finally:
        browser.shutdown()


def test_native_status_never_starts_playwright_until_explicit_diagnostics(tmp_path, monkeypatch):
    from browser_tools import BrowserTools
    import browser_tools
    calls = []
    monkeypatch.setattr(browser_tools.importlib.util, 'find_spec', lambda name: object())
    tools = BrowserTools(tmp_path)
    native, _ = browser_for(tmp_path)
    tools.native = native
    tools._check_installation = lambda: calls.append('diagnostic') or True
    try:
        for _ in range(3):
            status = tools.status()
            assert status['native']['available'] and status['default_backend'] == 'native'
        assert calls == [] and native.view is None and tools.playwright is None
        status = tools.status(backend='isolated')
        assert calls == ['diagnostic'] and status['isolated']['available']
        assert native.view is None and tools.playwright is None
        with pytest.raises(ValueError, match='diagnostics'):
            tools.status(backend='unknown')
    finally:
        tools.shutdown()
