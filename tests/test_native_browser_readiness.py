"""Navigation readiness observes one owned opening without another native action."""
import threading
from types import SimpleNamespace

import pytest

import native_browser
from native_browser import BrowserGuard, NativeBrowser
from test_native_browser import event_browser


class Clock:
    def __init__(self):
        self.now = 0.
        self.pauses = []
        self.after_pause = lambda: None

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        self.pauses.append(seconds)
        self.after_pause()
        return False


@pytest.fixture
def opening(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(native_browser, 'time', SimpleNamespace(monotonic=clock.monotonic))
    browser = event_browser(tmp_path)
    page = browser.view
    browser.execute('browser_navigate', {'url': 'https://example.org/fixture'})
    page.starting(1, 'https://example.org/fixture')
    browser.cancelled.wait = clock.wait
    yield browser, page, clock
    browser.view = page
    browser.shutdown()


def test_opening_waits_for_current_navigation_without_inspection_or_action_replay(opening):
    browser, page, clock = opening
    assert browser.execute('browser_inspect', {})['not_executed']
    page.preflight = lambda: None
    clock.after_pause = lambda: page.completed(1) if clock.now >= .15 else None
    result = browser.wait_ready(timeout=1)
    assert result['ok'] and not result['loading']
    assert clock.now == pytest.approx(.15) and len(clock.pauses) == 3
    assert page.effects.count(('requested', 'https://example.org/fixture')) == 1
    assert browser.snapshot is None and not any(effect in page.effects for effect in ('click', 'type'))
    assert browser.execute('browser_inspect', {})['ok']


def test_opening_timeout_keeps_loading_refusal_and_never_repeats_navigation(opening):
    browser, page, clock = opening
    with pytest.raises(TimeoutError, match='opening timeout'):
        browser.wait_ready(timeout=.2)
    assert clock.now == pytest.approx(.2)
    assert browser.execute('browser_inspect', {})['not_executed']
    assert page.effects.count(('requested', 'https://example.org/fixture')) == 1


@pytest.mark.parametrize('source', ['stop', 'run'])
def test_opening_honors_stop_and_run_cancellation_before_ready(opening, source):
    browser, page, clock = opening
    cancel = threading.Event()
    def stop():
        (browser.cancelled if source == 'stop' else cancel).set()
        page.completed(1)
    clock.after_pause = stop
    with pytest.raises(BrowserGuard, match='cancelled'):
        browser.wait_ready({'cancel': cancel}, timeout=1)
    assert clock.now == .05 and browser.snapshot is None


@pytest.mark.parametrize('replacement', ['view', 'preview'])
def test_opening_rejects_changed_view_or_preview_ownership(opening, replacement):
    browser, page, clock = opening
    owner = [True]
    def owned():
        if not owner[0]:
            raise BrowserGuard('Synthetic preview owner changed.')
    def replace():
        if replacement == 'view': browser.view = object()
        else: owner[0] = False
    clock.after_pause = replace
    with pytest.raises(BrowserGuard, match='changed'):
        browser.wait_ready(check=owned, timeout=1)
    assert clock.now == .05 and browser.snapshot is None


def test_navigation_failure_is_sanitized_and_is_not_successful_readiness(opening):
    browser, page, clock = opening
    clock.after_pause = lambda: browser._changed('loaded', 'Synthetic private page detail')
    with pytest.raises(BrowserGuard, match='navigation failed') as caught:
        browser.wait_ready(timeout=1)
    assert 'Synthetic private' not in str(caught.value) and clock.now == .05


def test_late_layout_observation_cannot_complete_after_opening_deadline(opening):
    browser, page, clock = opening
    page.completed(1)
    def late_preflight():
        clock.now = .2
    page.preflight = late_preflight
    with pytest.raises(TimeoutError, match='opening timeout'):
        browser.wait_ready(timeout=.2)
    assert clock.now == .2 and clock.pauses == [] and browser.snapshot is None


def test_replaced_navigation_during_layout_observation_cannot_be_adopted(opening):
    browser, page, clock = opening
    page.completed(1)
    calls = []
    def preflight():
        calls.append(True)
        if len(calls) == 1: page.starting(2, 'https://example.org/fixture#next')
    page.preflight = preflight
    with pytest.raises(BrowserGuard, match='navigation changed'):
        browser.wait_ready(timeout=1)
    assert len(calls) == 1 and clock.now == 0 and browser.snapshot is None


def test_redirects_for_the_same_navigation_do_not_confuse_generation_invalidation(opening):
    browser, page, clock = opening
    def redirect_then_complete():
        page.starting(1, 'https://example.org/redirected')
        page.source_changed('https://example.org/redirected', new_document=True)
        page.completed(1)
    clock.after_pause = redirect_then_complete
    result = browser.wait_ready(timeout=1)
    assert result['ok'] and result['url'].endswith('/redirected')
    assert clock.now == .05 and browser.snapshot is None


def test_navigation_events_complete_readiness_without_a_queued_frontend_bind(opening):
    browser, page, clock = opening
    binds, queued = [], []
    page.bind = lambda data: binds.append(data)
    def loaded_while_resize_queued():
        queued.append(browser._submit(browser._manual, 'resize', {'rect': {}}, browser._pane_generation))
        assert not queued[0].done()
        page.completed(1)  # WebView2 callbacks run on the UI, not the native queue.
    clock.after_pause = loaded_while_resize_queued
    assert browser.wait_ready(timeout=1)['ok']
    assert queued[0].result(timeout=1)['ok'] and binds == [{'rect': {}}]
    assert clock.now == .05


@pytest.mark.parametrize('timeout', [True, False, 0, -1, 21, float('inf'), float('nan'), None, '20'])
def test_opening_readiness_timeout_is_bounded_before_admission(opening, timeout):
    browser, _, clock = opening
    with pytest.raises(ValueError, match='at most 20 seconds'):
        browser.wait_ready(timeout=timeout)
    assert clock.now == 0 and browser.pending_io() == 0
