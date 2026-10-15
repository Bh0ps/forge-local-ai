"""Forge-owned WebView2 research window; remote pages receive no native bridge."""
from __future__ import annotations

import concurrent.futures
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from browser_tools import browser_url
from computer_tools import session_available


class BrowserGuard(ValueError):
    """A rejected preflight, before any browser side effect was sent."""
    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


class BrowserCompletionError(RuntimeError):
    """A submitted callback has no usable completion; never imply safe replay."""
    _messages = {
        'cancelled': 'Browser callback was cancelled; inspect the action outcome before retrying.',
        'faulted': 'Browser callback failed; inspect the action outcome before retrying.',
        'protocol_error': 'Browser callback completion is unavailable; the submitted action outcome is unknown.',
        'pending': 'A browser capture is still pending; its submitted outcome is unknown.',
    }

    def __init__(self, reason):
        self.reason = reason if reason in self._messages else 'protocol_error'
        super().__init__(self._messages[self.reason])


def _managed_task_failure(task):
    try:
        if task.IsCanceled:
            return 'cancelled'
        if task.IsFaulted:
            try:
                task.Exception  # Observe the fault without exposing its details.
            except Exception:
                pass
            return 'faulted'
    except Exception:
        return 'protocol_error'
    return None


def _wait_managed_callback(done, continuation, timeout_message):
    # Called after UI registration, on its worker and outside native locks.
    # Event.set precedes the managed delegate's return. Retain and wait for
    # that continuation too, sharing the original twelve-second wait budget.
    deadline = time.monotonic() + 12
    failure = _managed_task_failure(continuation)
    if failure:
        raise BrowserCompletionError(failure) from None
    if not done.wait(max(0, deadline - time.monotonic())):
        failure = _managed_task_failure(continuation)
        if failure:
            raise BrowserCompletionError(failure) from None
        raise TimeoutError(timeout_message)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(timeout_message)
    try:
        completed = continuation.Wait(max(0, int(remaining * 1000)))
    except Exception:
        raise BrowserCompletionError(_managed_task_failure(continuation) or 'protocol_error') from None
    if time.monotonic() >= deadline:
        raise TimeoutError(timeout_message)
    failure = _managed_task_failure(continuation)
    if failure:
        raise BrowserCompletionError(failure) from None
    if completed is False:
        raise TimeoutError(timeout_message)
    if completed is not True:
        raise BrowserCompletionError('protocol_error')
    if time.monotonic() >= deadline:
        raise TimeoutError(timeout_message)


class _CaptureLifetime:
    """One page-owned stream, including a submitted task without a callback."""
    def __init__(self):
        self.lock = threading.Lock()
        self.stream = self.source = self.continuation = None
        self.registration_finished = self.unsubmitted = self.callback_active = False
        self.aborted = self.submission_started = False
        self.disposing = self.disposed = False

    def dispose_stream(self):
        with self.lock:
            if self.disposed:
                return True
            if self.disposing:
                return False
            self.disposing = True
            stream = self.stream
        try:
            if stream is not None:
                stream.Dispose()
        finally:
            with self.lock:
                self.disposed, self.disposing = True, False
        return True

    def retire(self, *, managed_completed=False, deadline=None):
        def expired():
            return deadline is not None and time.monotonic() >= deadline
        if expired():
            return False
        with self.lock:
            if not self.registration_finished or self.callback_active or self.disposing:
                return False
            source, continuation, unsubmitted = self.source, self.continuation, self.unsubmitted
        if not managed_completed:
            if source is None and not unsubmitted:
                return False
            for task in (source, continuation):
                if task is None:
                    continue
                if expired():
                    return False
                _managed_task_failure(task)  # Observe known faults, without reading their details.
                if expired():
                    return False
                try:
                    if task.IsCompleted is not True:
                        return False
                except Exception:
                    return False
                if expired():
                    return False
        try:
            return self.dispose_stream()
        except Exception:
            return True  # Disposal was attempted exactly once, with no usable result.

    def wait_after_registration_failure(self):
        # Never wait on the UI thread or while holding the page admission lock.
        # A failed registration still owns its source task and stream.
        deadline = time.monotonic() + 12
        task = self.continuation if self.continuation is not None else self.source
        if task is None:
            return None
        try:
            _managed_task_failure(task)
            if time.monotonic() >= deadline:
                return None
            if task.IsCompleted is not True:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    task.Wait(max(0, int(remaining * 1000)))
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        _managed_task_failure(task)
        return deadline if time.monotonic() < deadline else None


class NativeBrowser:
    def __init__(self, home, ui_dispatch=None, view_factory=None, clock=time.monotonic, session_check=None,
                 panel_id='forge-browser-panel', profile_name='native-browser-profile', navigation_guard=None, diagnostics=False,
                 indicator_scope=None):
        self.home = Path(home)
        self.ui_dispatch = ui_dispatch
        self.view_factory = view_factory
        self.panel_id, self.profile_name = panel_id, profile_name
        self.navigation_guard, self.capture_diagnostics = navigation_guard, diagnostics
        self.indicator_scope = indicator_scope
        self._indicator = None
        self.clock = clock
        self.session_check = session_check or (lambda: bool(self.view_factory) or session_available())
        self.view = None
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='forge-native-browser')
        self._pending_io = 0
        self.cancelled = threading.Event()
        self.snapshot = None
        self.generation = 0
        self._pane_generation = 0
        self._pane_allowed = False
        self.closed = False
        self.state = dict(running=False, loading=False, url='about:blank', title='', error=None)

    def pending_io(self):
        with self.lock:
            return self._pending_io

    def _submit(self, operation, *args):
        # A caller timeout does not finish its queued/running Future. Keep the
        # owned work visible to restart admission until actual completion.
        with self.lock:
            self._pending_io += 1
        try:
            future = self.pool.submit(operation, *args)
        except BaseException:
            with self.lock:
                self._pending_io -= 1
            raise
        def completed(_):
            with self.lock:
                self._pending_io -= 1
        future.add_done_callback(completed)
        return future

    @staticmethod
    def _run(context):
        return str((context or {}).get('run_id') or 'interactive')

    def _check(self, context=None):
        cancel = (context or {}).get('cancel')
        if self.closed or self.cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
            raise BrowserGuard('Browser action cancelled.')
        if not self.session_check():
            raise BrowserGuard('Windows desktop session is unavailable. Unlock Windows before continuing browser work.')
        if self.closed or self.cancelled.is_set() or (hasattr(cancel, 'is_set') and cancel.is_set()):
            raise BrowserGuard('Browser action cancelled.')

    def _revoke_pane(self):
        with self.lock:
            self._pane_generation += 1
            self._pane_allowed = False
            self.snapshot = None
            self._indicator = None

    def _clear_indicator(self):
        with self.lock:
            self._indicator = None
            view = self.view
        if view is not None and hasattr(view, 'clear_indicator'):
            try: view.clear_indicator()
            except Exception: pass  # Decoration cannot change action/replay outcomes.

    def _pane_check(self, generation, context=None, *, require=True):
        self._check(context)
        if self.panel_id != 'forge-browser-panel':
            return
        with self.lock:
            if generation != self._pane_generation:
                raise BrowserGuard('The Browser pane changed. Open Browser and inspect again.', 'browser_owner_changed')
            if require and not self._pane_allowed:
                raise BrowserGuard('Open the Browser pane before continuing browser work.', 'browser_hidden')

    def _view_call(self, name, *args, check=None, effect=False):
        # Real hosts recheck on their UI thread after marshaling. Legacy injected
        # test views retain their call shape; no native lock spans that wait.
        if check: check()
        method = getattr(self.view, name)
        sent = False
        def admission():
            nonlocal sent
            if check: check()
            sent = True
        try:
            if check and getattr(self.view, 'supports_pane_guard', False) is True:
                result = method(*args, check=admission if effect else check)
            else:
                sent = True
                result = method(*args)
        except BrowserGuard as exc:
            if effect and sent:
                raise RuntimeError('Browser action outcome is unknown. Inspect before retrying.') from exc
            raise
        try:
            if check: check()
        except BrowserGuard as exc:
            if effect and sent and not (isinstance(result, dict) and result.get('not_executed') is True):
                raise RuntimeError('Browser action outcome is unknown. Inspect before retrying.') from exc
            raise
        return result

    def _changed(self, kind, value=None):
        with self.lock:
            if kind == 'navigation':
                self.generation += 1
                self.snapshot = None
                self.state.update(url=str(value), loading=True, error=None)
                self._indicator = None
            elif kind == 'loaded':
                self.state.update(loading=False, error=value)
            elif kind == 'title':
                self.state['title'] = str(value or '')[:500]
            elif kind == 'closed':
                self.generation += 1
                self.snapshot = None
                self._indicator = None
                self.state.update(running=False, loading=False)
                self.view = None
            if kind in ('closed', 'hidden') and self.panel_id == 'forge-browser-panel':
                self._pane_generation += 1
                self._pane_allowed = False
                self.snapshot = None

    def status(self, scope=None):
        def eligible(indicator):
            return bool(indicator is not None and indicator is self._indicator and not self.closed
                and self._pane_allowed and not self.cancelled.is_set()
                and 0 <= self.clock()-indicator['time'] < 3 and indicator['pane'] == self._pane_generation
                and indicator['generation'] == self.generation and not self.state['loading']
                and getattr(self.view, 'visible', False))
        with self.lock:
            available = not self.closed and bool(self.view_factory or os.name == 'nt' and self.ui_dispatch)
            indicator = self._indicator
            # Expired or revoked cues need no ancestry query. Recheck after the
            # query as its read may overlap Hide, Stop, navigation or expiry.
            candidate = eligible(indicator)
            result = dict(available=available, engine='WebView2', **self.state,
                embedded=True, visible=bool(getattr(self.view, 'visible', False)),
                message=self.state['error'] or ('Ready — uses the Windows web runtime' if available else 'Open Forge desktop to use the built-in browser'),
                capabilities=['navigate', 'inspect', 'click', 'type', 'screenshot', 'back', 'forward', 'reload'])
        active = False
        if candidate and self.indicator_scope and self.panel_id == 'forge-browser-panel':
            try: active = self.indicator_scope(indicator['run'], scope or {}) is True
            except Exception: active = False
        with self.lock:
            active = bool(active and eligible(indicator))
        result['target_indicator'] = 'active' if active else 'none'
        return result

    def _start(self):
        if self.view is not None:
            return
        if not self.status()['available']:
            raise BrowserGuard('The built-in browser requires Forge desktop on Windows.')
        if self.view_factory:
            view = self.view_factory(self._changed)
        else:
            from embedded_browser import EmbeddedWebView2Page
            view = self.ui_dispatch(lambda owner: EmbeddedWebView2Page(owner, self.home, self.ui_dispatch, self._changed,
                panel_id=self.panel_id, profile_name=self.profile_name, navigation_guard=self.navigation_guard,
                diagnostics=self.capture_diagnostics))
        with self.lock:
            self.view = view
            self.state.update(running=True, error=None)
        try:
            view.ready()
        except Exception as exc:
            try:
                (getattr(view, 'dispose', None) or view.close)()
            finally:
                with self.lock:
                    self.view = None
                    self.state.update(running=False, loading=False, error=str(exc)[:400])
            raise

    def dispatch(self, action, data=None):
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise ValueError('Expected browser controls object.')
        if action == 'status':
            return self.status(data)
        if action not in ('open', 'show', 'resize', 'hide', 'navigate', 'back', 'forward', 'reload', 'close'):
            raise ValueError('Unknown native browser control.')
        if action in ('hide', 'close') and self.panel_id == 'forge-browser-panel':
            self._revoke_pane()
        with self.lock:
            generation = self._pane_generation
        return self._submit(self._manual, action, deepcopy(data), generation).result(timeout=45)

    def wait_ready(self, context=None, *, check=None, timeout=20):
        """Observe the opened document; never navigate, focus or repeat an action."""
        if type(timeout) not in (int, float) or not 0 < timeout <= 20:
            raise ValueError('Browser readiness requires a timeout of at most 20 seconds.')
        deadline = time.monotonic() + timeout
        with self.lock:
            view, pane = self.view, self._pane_generation
        navigation = getattr(view, '_navigation_id', None)

        def guard():
            nonlocal navigation
            self._pane_check(pane, context)
            if check: check()
            with self.lock:
                if view is None or self.view is not view or not self.state['running']:
                    raise BrowserGuard('Browser view changed while opening. Open the preview again.')
                current = getattr(view, '_navigation_id', None)
                if navigation is None:
                    navigation = current  # First Starting may arrive after Navigate returns.
                elif current != navigation:
                    raise BrowserGuard('Browser navigation changed while opening. Inspect the preview again.')

        def wait():
            while True:
                guard()
                if time.monotonic() >= deadline:
                    raise TimeoutError('Browser navigation did not become ready within its opening timeout.')
                with self.lock:
                    loading, error, generation = self.state['loading'], self.state['error'], self.generation
                if error:
                    raise BrowserGuard('Browser navigation failed. Check the preview and reload it.')
                if not loading:
                    if getattr(view, 'embedded', False) and not getattr(view, 'visible', True):
                        raise BrowserGuard('The Builder preview is hidden. Open its pane before using preview tools.')
                    if hasattr(view, 'preflight'):
                        self._view_call('preflight', check=guard)
                    guard()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Browser navigation did not become ready within its opening timeout.')
                    with self.lock:
                        ready = generation == self.generation and not self.state['loading'] and not self.state['error']
                    if ready:
                        return {'ok': True, **self.status()}
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    # Stop wakes this wait without waiting for another UI round.
                    self.cancelled.wait(min(.05, remaining))

        return self._submit(wait).result(timeout=timeout + 5)

    def _manual(self, action, data, generation):
        if action in ('close', 'hide'):
            self._clear_indicator()
            if self.view:
                self.view.close()
            return {'ok': True, **self.status()}
        # A new direct user command may continue after Stop; old agent requests
        # retain their own cancellation event and cannot reuse a cleared snapshot.
        if self.panel_id == 'forge-browser-panel':
            with self.lock:
                if generation != self._pane_generation:
                    raise BrowserGuard('The Browser pane changed. Open Browser and inspect again.', 'browser_owner_changed')
        self.cancelled.clear()
        self._check()
        self._start()
        self._clear_indicator()
        guard = lambda: self._pane_check(generation, require=False)
        guard()
        if action in ('show', 'resize') and data.get('rect') is not None:
            if not hasattr(self.view, 'bind'): raise BrowserGuard('This browser does not support an embedded pane.')
            self._view_call('bind', data, check=guard)
        elif action != 'open' or not getattr(self.view, 'embedded', False):
            explicit_preview = action == 'show' and self.panel_id == 'forge-preview-panel' and hasattr(self.view, 'open_pane')
            self._view_call('open_pane' if explicit_preview else 'show', check=guard)
        with self.lock:
            if self.panel_id == 'forge-browser-panel' and generation == self._pane_generation:
                if action in ('show', 'resize') or not getattr(self.view, 'embedded', False):
                    if not getattr(self.view, 'visible', True):
                        raise BrowserGuard('Open the Browser pane in the visible Forge workspace.', 'browser_hidden')
                    self._pane_allowed = True
        guard()
        if action in ('open', 'navigate') and (action == 'navigate' or data.get('url')):
            url = browser_url(data.get('url', 'about:blank'))
            if self.navigation_guard and not self.navigation_guard(url):
                raise BrowserGuard('Preview navigation must stay on its owned loopback origin.')
            self._view_call('navigate', url, check=guard)
        elif action in ('back', 'forward', 'reload'):
            self._view_call('history', action, check=guard)
        return {'ok': True, **self.status()}

    def describe_target(self, args=None, context=None):
        try:
            self._check(context)
            if (args or {}).get('snapshot_id'):
                self._snapshot(args, context, focus=False)
            status = self.status()
            return {'app': 'forge:native-browser', 'target': str((args or {}).get('url') or status['url']),
                    'backend': 'native'}
        except BrowserGuard as exc:
            return {'ok': False, 'error': str(exc), 'not_executed': True}

    def execute(self, name, args, context=None):
        context = dict(context or {})
        try:
            if name == 'browser_close' and self.panel_id == 'forge-browser-panel':
                self._check(context)
                self._revoke_pane()
            with self.lock:
                generation = self._pane_generation
                self._indicator = None
            return self._submit(self._perform, name, deepcopy(args), context, generation).result(timeout=45)
        except BrowserGuard as exc:
            return {'ok': False, 'error': str(exc), 'not_executed': True,
                    **({'reason': exc.reason} if exc.reason in ('browser_hidden', 'browser_owner_changed') else {})}

    def _snapshot(self, args, context, focus=True, pane=None):
        self._check(context)
        if self.panel_id == 'forge-browser-panel':
            self._pane_check(self._pane_generation if pane is None else pane, context)
        with self.lock:
            snapshot = self.snapshot
            if not snapshot or args.get('snapshot_id') != snapshot['id'] or snapshot['run'] != self._run(context):
                raise BrowserGuard('Stale browser snapshot. Inspect this page again.')
            if self.clock() - snapshot['time'] > 30 or snapshot['generation'] != self.generation:
                raise BrowserGuard('Browser page changed or snapshot expired. Inspect again.')
            if self.state['loading'] or snapshot['url'] != self.state['url'] or not self.view:
                raise BrowserGuard('Browser page is navigating or closed. Inspect again.')
            if self.panel_id == 'forge-browser-panel' and snapshot.get('pane') != self._pane_generation:
                raise BrowserGuard('The Browser pane changed. Inspect again.', 'browser_owner_changed')
        if focus and not self.view.focused():
            raise BrowserGuard('Forge browser is not focused. Open its window, then inspect again.')
        return snapshot

    def _perform(self, name, data, context, pane):
        self._check(context)
        if name == 'browser_close':
            guard = lambda: self._pane_check(pane, context, require=False)
            guard()
            self._clear_indicator()
            if self.view:
                if getattr(self.view, 'supports_pane_guard', False) is True:
                    self.view.close(check=guard)
                else:
                    self.view.close()
            return {'ok': True}
        guard = lambda: self._pane_check(pane, context)
        guard()
        self._clear_indicator()
        if name == 'browser_navigate':
            try:
                url = browser_url(data.get('url', ''))
            except ValueError as exc:
                raise BrowserGuard(str(exc)) from None
            if self.navigation_guard and not self.navigation_guard(url):
                raise BrowserGuard('Preview navigation must stay on its owned loopback origin.')
            if self.panel_id != 'forge-browser-panel': self._start()
            elif self.view is None:
                raise BrowserGuard('Open the Browser pane before navigating.', 'browser_hidden')
            try:
                self._check(context)
            except BrowserGuard as exc:
                # Starting an owned window is already a completed host action.
                raise RuntimeError('Browser window opened; navigation was cancelled before dispatch.') from exc
            if self.panel_id != 'forge-browser-panel': self.view.show()
            self._view_call('navigate', url, check=guard, effect=True)
            return {'ok': True, 'url': url, 'loading': True, 'next_action': 'Use browser_inspect after navigation finishes.'}
        if name not in ('browser_inspect', 'browser_screenshot', 'browser_click', 'browser_type',
                        'browser_select', 'browser_key', 'browser_scroll', 'browser_wait'):
            raise BrowserGuard('Unsupported native browser tool.')
        if self.view is None:
            raise BrowserGuard('Open Forge Browser or use browser_navigate before inspecting this page.')
        if name == 'browser_inspect':
            return self._inspect(context, pane)
        if name == 'browser_screenshot':
            if self.panel_id == 'forge-preview-panel' and getattr(self.view, 'embedded', False) and not getattr(self.view, 'visible', False):
                raise BrowserGuard('Open the Builder preview pane before capturing visual evidence.')
            if hasattr(self.view, 'preflight'):
                self._view_call('preflight', check=guard)
            with self.lock:
                generation = self.generation
            self._check(context)
            if self.panel_id == 'forge-browser-panel':
                cleared = self._view_call('evaluate', CLEAR_TARGET_SCRIPT, check=guard)
                if cleared is not True:
                    raise BrowserGuard('The browser target indicator could not be cleared before capture.')
            raw = self._view_call('screenshot', check=guard)
            with self.lock:
                if generation != self.generation:
                    raise BrowserGuard('Browser navigated while capturing. Inspect again.')
            directory = self.home / ('artifacts/previews' if self.panel_id == 'forge-preview-panel' else 'artifacts/browser')
            directory.mkdir(parents=True, exist_ok=True)
            artifact = directory / (uuid4().hex + '.png')
            artifact.write_bytes(raw)
            return {'ok': True, 'artifact': str(artifact), 'mimeType': 'image/png', 'url': self.state['url'],
                'content': [{'type': 'image', 'artifact': str(artifact), 'mimeType': 'image/png'}]}
        snapshot = self._snapshot(data, context, focus=False, pane=pane)
        if hasattr(self.view, 'preflight'): self._view_call('preflight', check=guard)
        if name == 'browser_wait':
            text, timeout = data.get('text'), data.get('timeout_ms', 5000)
            if not isinstance(text, str) or not 1 <= len(text) <= 2000 or type(timeout) is not int or not 100 <= timeout <= 15000:
                raise BrowserGuard('Wait for text up to 2000 characters with a 100–15000 ms timeout.')
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                self._snapshot(data, context, focus=False, pane=pane)
                payload = json.dumps({'url': snapshot['url'], 'text': text})
                result = self._view_call('evaluate', "(() => {const d=" + payload + ";return {same_url:location.href===d.url,matched:(document.body?.innerText||'').includes(d.text)};})()", check=guard)
                if not result.get('same_url'):
                    raise BrowserGuard('Preview navigated while waiting. Inspect again.')
                if result.get('matched'):
                    return {'ok': True, 'matched': True, 'inspect_again': True}
                time.sleep(.1)
            return {'ok': True, 'matched': False, 'timed_out': True, 'inspect_again': True}
        if name == 'browser_scroll':
            x, y = data.get('x', 0), data.get('y', 0)
            if type(x) is not int or type(y) is not int or max(abs(x), abs(y)) > 1500:
                raise BrowserGuard('Scroll by integer offsets of at most 1500 CSS pixels.')
            payload = json.dumps({'url': snapshot['url'], 'x': x, 'y': y})
            self._check(context)
            with self.lock:
                self.snapshot = None
            result = self._view_call('evaluate', "(() => {const d=" + payload + ";if(location.href!==d.url)return {ok:false,not_executed:true,error:'Preview navigated.'};window.scrollBy(d.x,d.y);return {ok:true,inspect_again:true};})()", check=guard, effect=True)
            return result
        selector = data.get('selector')
        if selector not in snapshot['signatures']:
            raise BrowserGuard('Choose a selector returned by browser_inspect.')
        text = data.get('text')
        if name == 'browser_type' and (not isinstance(text, str) or len(text) > 12000):
            raise BrowserGuard('Browser text must contain at most 12000 characters.')
        if name == 'browser_select' and (not isinstance(data.get('value'), str) or len(data['value']) > 2000):
            raise BrowserGuard('Select an option value of at most 2000 characters.')
        if name == 'browser_key' and data.get('key') not in ('Enter', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' ', 'Backspace', 'Delete', 'Home', 'End'):
            raise BrowserGuard('This preview key is unsupported.')
        expected = snapshot['signatures'][selector]
        self._check(context)
        # Validation and DOM action share one script: no host round-trip race.
        payload = json.dumps(dict(selector=selector, signature=expected, operation=name, text=text,
                                  value=data.get('value'), key=data.get('key'),
                                  preserve_focus=self.panel_id == 'forge-browser-panel',
                                  indicate=self.panel_id == 'forge-browser-panel',
                                  url=snapshot['url']), ensure_ascii=True)
        check = json.dumps(dict(selector=selector, signature=expected, operation='validate', action=name, key=data.get('key'), text=None,
                                url=snapshot['url']), ensure_ascii=True)
        preflight = self._view_call('evaluate', ACTION_SCRIPT.replace('__FORGE_DATA__', check), check=guard)
        if preflight.get('not_executed'):
            return preflight
        self._snapshot(data, context, focus=False, pane=pane)
        focused_effect = self.panel_id != 'forge-browser-panel' and not self.view.focused()
        if focused_effect:
            self.view.show()
        if self.panel_id != 'forge-browser-panel' and not self.view.focused():
            raise RuntimeError('Owned browser focus changed after focusing. Inspect before retrying.')
        try:
            self._check(context)
            if hasattr(self.view, 'preflight'): self._view_call('preflight', check=guard)
        except BrowserGuard as exc:
            if focused_effect:
                raise RuntimeError('Browser focused, then action cancelled before dispatch.') from exc
            raise
        guard()
        with self.lock:
            self.snapshot = None
            if self.panel_id == 'forge-browser-panel' and self._pane_allowed and self._pane_generation == pane:
                self._indicator = dict(run=self._run(context), pane=pane, generation=self.generation, time=self.clock())
        result = self._view_call('evaluate', ACTION_SCRIPT.replace('__FORGE_DATA__', payload), check=guard, effect=True)
        if result.get('not_executed'):
            self._clear_indicator()
            if focused_effect:
                result = dict(result)
                result.pop('not_executed', None)
                result['performed'] = 'focused_browser'
            return result
        # Exceptions/timeout after ExecuteScriptAsync may follow a dispatched click;
        # do not turn them into a safe rejection or repeat them automatically.
        with self.lock:
            self.snapshot = None
        return {'ok': True, 'inspect_again': True, 'backend': 'native'}

    def _inspect(self, context, pane):
        self._check(context)
        self._pane_check(pane, context)
        with self.lock:
            if self.state['loading']:
                raise BrowserGuard('Browser page is still loading. Inspect again when navigation completes.')
            generation = self.generation
        key = uuid4().hex
        result = self._view_call('evaluate', INSPECT_SCRIPT.replace('__FORGE_KEY__', json.dumps(key)), check=lambda: self._pane_check(pane, context))
        if not isinstance(result, dict) or not result.get('url'):
            raise BrowserGuard('Browser page could not be inspected.')
        self._check(context)
        self._pane_check(pane, context)
        with self.lock:
            if generation != self.generation or self.state['loading'] or result['url'] != self.state['url']:
                raise BrowserGuard('Browser navigated while inspecting. Inspect again.')
            if self.panel_id == 'forge-browser-panel' and (pane != self._pane_generation or not self._pane_allowed):
                raise BrowserGuard('The Browser pane changed. Inspect again.', 'browser_owner_changed')
            signatures = {target['selector']: target.pop('signature') for target in result['targets']}
            self.snapshot = dict(id=key, run=self._run(context), time=self.clock(), generation=generation, pane=pane,
                                 url=result['url'], signatures=signatures)
        return dict(ok=True, snapshot_id=key, backend='native', **result)

    def viewport(self, data):
        def update():
            self._check()
            if not self.view or not hasattr(self.view, 'set_viewport'):
                raise BrowserGuard('Open the embedded preview first.')
            width, height = data.get('width'), data.get('height')
            if width is not None and (type(width) is not int or type(height) is not int or
                                      not 240 <= width <= 1920 or not 240 <= height <= 2160):
                raise BrowserGuard('Viewport dimensions must be 240–1920 by 240–2160 CSS pixels.')
            result = self.view.set_viewport(width, height)
            with self.lock:
                self.generation += 1
                self.snapshot = None
            return {'ok': True, **result}
        return self._submit(update).result(timeout=20)

    def diagnostics(self):
        def inspect():
            self._check()
            if not self.view or not self.capture_diagnostics:
                raise BrowserGuard('Diagnostics are available only in the preview browser.')
            result = self.view.evaluate("(() => ({url:location.href,entries:(Array.isArray(window.__forgePreviewDiagnostics)?window.__forgePreviewDiagnostics:[]).slice(-100).map(entry=>({kind:String(entry?.kind||'').slice(0,64),message:String(entry?.message||'').slice(0,1600),timestamp:String(entry?.timestamp||'').slice(0,64)})),untrusted:true}))()")
            if self.navigation_guard and not self.navigation_guard(result.get('url', '')):
                raise BrowserGuard('Preview diagnostics changed origin.')
            return {'ok': True, **result}
        return self._submit(inspect).result(timeout=20)

    def stop(self):
        self.cancelled.set()
        if self.panel_id == 'forge-browser-panel': self._revoke_pane()
        self._clear_indicator()
        with self.lock:
            self.snapshot = None
        if self.view:
            self.view.stop()

    def reset(self):
        self.cancelled.clear()

    def shutdown(self):
        if self.closed:
            return
        self.cancelled.set()
        if self.panel_id == 'forge-browser-panel': self._revoke_pane()
        self._clear_indicator()
        if self.view:
            (getattr(self.view, 'dispose', None) or self.view.close)()
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


INSPECT_SCRIPT = r'''(() => {
 const key=__FORGE_KEY__;
 const sig=el=>[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,el.tagName==='SELECT'?[el.value,[...el.options].slice(0,200).map(o=>[o.value.slice(0,300),o.text.slice(0,300),o.disabled])]:el.type!=='password'&&['INPUT','TEXTAREA'].includes(el.tagName)?el.value:null];
 const targets=[...document.querySelectorAll('a,button,input,textarea,select,[role=button]')]
 .filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=='hidden';}).slice(0,200)
 .map((el,i)=>{const id=key+'-'+i;el.setAttribute('data-forge-native',id);return {
 selector:'[data-forge-native="'+id+'"]',tag:el.tagName.toLowerCase(),type:el.getAttribute('type'),
 label:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||'').slice(0,300),
 disabled:el.disabled===true,password:el.type==='password',signature:sig(el)};});
 return {url:location.href,title:document.title.slice(0,500),text:(document.body?.innerText||'').slice(0,24000),targets};
})()'''

CLEAR_TARGET_SCRIPT = r'''(() => {document.querySelectorAll('[data-forge-ai-target]').forEach(el=>el.remove());return true;})()'''

ACTION_SCRIPT = r'''(() => {
 const d=__FORGE_DATA__;
 const fail=error=>({ok:false,error,not_executed:true});
 const mark=()=>{if(d.indicate!==true)return;try{
  const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
  if(![left,top,right,bottom].every(Number.isFinite)||right<=left||bottom<=top)return;
  document.querySelectorAll('[data-forge-ai-target]').forEach(node=>node.remove());
  const node=document.createElement('div');node.setAttribute('data-forge-ai-target','');node.setAttribute('aria-hidden','true');node.inert=true;
  node.style.cssText='position:fixed!important;pointer-events:none!important;z-index:2147483647!important;border:2px solid #bc683f!important;border-radius:5px!important;box-sizing:border-box!important;background:transparent!important;';
  for(const [name,value] of [['left',left],['top',top],['width',right-left],['height',bottom-top]])node.style.setProperty(name,value+'px','important');
  document.documentElement.append(node);setTimeout(()=>node.remove(),3000);
 }catch{}};
 if(location.href!==d.url)return fail('Browser navigated. Inspect again.');
 const matches=document.querySelectorAll(d.selector);if(matches.length!==1)return fail('Browser target changed. Inspect again.');
 const el=matches[0],r=el.getBoundingClientRect();
 const signature=[el.tagName,el.getAttribute('type'),el.getAttribute('href'),el.getAttribute('aria-label'),el.textContent.slice(0,200),el.disabled===true,el.readOnly===true,el.tagName==='SELECT'?[el.value,[...el.options].slice(0,200).map(o=>[o.value.slice(0,300),o.text.slice(0,300),o.disabled])]:el.type!=='password'&&['INPUT','TEXTAREA'].includes(el.tagName)?el.value:null];
 if(JSON.stringify(signature)!==JSON.stringify(d.signature)||!el.isConnected||!r.width||!r.height||getComputedStyle(el).visibility==='hidden')return fail('Browser target changed. Inspect again.');
 if(el.type==='password'||el.type==='file')return fail('Password and file fields require direct user interaction.');
 if(el.disabled||el.readOnly)return fail('Target is disabled or read-only.');
  const button=el.tagName==='BUTTON'||el.tagName==='INPUT'&&['button','submit','reset','image'].includes(el.type);
  const checkable=el.tagName==='INPUT'&&['checkbox','radio'].includes(el.type);
  const textInput=el.tagName==='INPUT'&&['text','search','email','url','tel','number',''].includes(el.type||'');
  const textEntry=el.tagName==='TEXTAREA'||el.tagName==='INPUT'&&['text','search','url','tel',''].includes(el.type||'');
  if((d.action||d.operation)==='browser_key'&&['Enter',' '].includes(d.key)){
   if(d.key==='Enter'&&!button&&!textInput&&el.tagName!=='TEXTAREA'&&el.tagName!=='A')return fail('Enter default action is unsupported for this target. Use browser_click or another supported action.');
   if(d.key===' '&&!button&&!checkable&&!textEntry)return fail('Space default action is unsupported for this target. Use browser_click or browser_type.');
  }
  if(d.operation==='validate')return {ok:true};
 if(d.operation==='browser_select'){
  if(el.tagName!=='SELECT'||el.options.length>200)return fail('Select a dropdown with at most 200 options from the snapshot.');
  const option=[...el.options].find(option=>option.value===d.value&&!option.disabled);if(!option)return fail('Option changed or is disabled. Inspect again.');
  const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')?.set;if(!setter)return fail('Dropdown cannot be edited.');
  mark();setter.call(el,d.value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true};
 }
 if(d.operation==='browser_key'){
  mark();if(d.preserve_focus!==true)el.focus();const proceed=el.dispatchEvent(new KeyboardEvent('keydown',{key:d.key,bubbles:true,cancelable:true}));
   if(proceed&&d.key==='Enter'){
    if(button||el.tagName==='A')el.click();
    else if(el.tagName==='TEXTAREA'){
     el.setRangeText('\n',el.selectionStart,el.selectionEnd,'end');el.dispatchEvent(new Event('input',{bubbles:true}));
    }else if(textInput&&el.form){
     const submitter=[...document.querySelectorAll('button,input')].find(control=>control.form===el.form&&
      (control.tagName==='BUTTON'&&control.type==='submit'||control.tagName==='INPUT'&&['submit','image'].includes(control.type)));
     if(submitter)submitter.click();
     else if([...el.form.elements].filter(control=>control.tagName==='INPUT'&&['text','search','tel','url','email','password','date','month','week','time','datetime-local','number'].includes(control.type)).length<=1)
      HTMLFormElement.prototype.requestSubmit.call(el.form);
    }
   }else if(proceed&&d.key===' '&&textEntry){
    el.setRangeText(' ',el.selectionStart,el.selectionEnd,'end');el.dispatchEvent(new Event('input',{bubbles:true}));
   }
   el.dispatchEvent(new KeyboardEvent('keyup',{key:d.key,bubbles:true}));
   if(proceed&&d.key===' '&&(button||checkable))el.click();
   return {ok:true};
 }
 if(d.operation==='browser_type'&&!['INPUT','TEXTAREA'].includes(el.tagName))return fail('Select an editable text field.');
 if(d.operation==='browser_type'&&!['text','search','email','url','tel','number',''].includes(el.type||''))return fail('Unsupported input type.');
 if(d.operation==='browser_click'){mark();el.click();return {ok:true};}
 const prototype=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
 const setter=Object.getOwnPropertyDescriptor(prototype,'value')?.set;if(!setter)return fail('This field cannot be edited.');
 mark();if(d.preserve_focus!==true)el.focus();setter.call(el,d.text);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true};
})()'''


PREVIEW_DIAGNOSTICS_SCRIPT = r'''(() => {
 const entries=[];Object.defineProperty(window,'__forgePreviewDiagnostics',{value:entries});
 const push=(kind,message)=>{entries.push({kind,message:String(message).slice(0,1600),timestamp:new Date().toISOString()});if(entries.length>100)entries.shift();};
 for(const kind of ['warn','error']){const original=console[kind].bind(console);console[kind]=(...args)=>{push('console_'+kind,args.map(a=>String(a)).join(' '));original(...args);};}
 addEventListener('error',event=>push('page_error',event.message||'Resource could not load'),true);
 addEventListener('unhandledrejection',event=>push('unhandled_rejection',event.reason));
 const fetchOriginal=window.fetch.bind(window);window.fetch=async(...args)=>{try{const response=await fetchOriginal(...args);if(!response.ok)push('network',response.status+' '+response.url);return response;}catch(error){push('network',String(args[0])+' '+error);throw error;}};
 const open=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(method,url,...args){this.addEventListener('loadend',()=>{if(this.status===0||this.status>=400)push('network',this.status+' '+String(url));});return open.call(this,method,url,...args);};
})()'''


class WebView2Page:
    """Raw control: no pywebview JS API, host objects, downloads or extension profile."""
    supports_pane_guard = True
    _capture_admission_lock = threading.Lock()

    def _retire_capture(self, lifetime, *, managed_completed=False, deadline=None):
        if not lifetime.retire(managed_completed=managed_completed, deadline=deadline):
            return False
        with self._capture_admission_lock:
            if getattr(self, '_capture_lifetime', None) is lifetime:
                self._capture_lifetime = None
        return True

    def _reserve_capture(self):
        # Lazy ownership also covers EmbeddedWebView2Page's separate initializer.
        while True:
            with self._capture_admission_lock:
                previous = getattr(self, '_capture_lifetime', None)
                if previous is None:
                    lifetime = _CaptureLifetime()
                    self._capture_lifetime = lifetime
                    return lifetime
            if not self._retire_capture(previous):
                raise BrowserCompletionError('pending') from None

    def __init__(self, owner, home, ui_dispatch, changed):
        from webview.platforms.edgechromium import WebView2, CoreWebView2CreationProperties
        from System.Drawing import Color, Size, Font
        from System.Windows.Forms import Form, Panel, Button, TextBox, DockStyle, Keys, FlatStyle, Screen
        self.ui = ui_dispatch
        self.changed = changed
        self.event = threading.Event()
        self.error = None
        self.disposed = False
        scale = float(getattr(owner, 'DeviceDpi', 96)) / 96
        px = lambda value: round(value * scale)
        area = Screen.FromControl(owner).WorkingArea
        self.form = Form()
        self.form.Text = 'Forge Browser'
        self.form.Size = Size(min(px(1100), area.Width), min(px(780), area.Height))
        self.form.MinimumSize = Size(min(px(620), area.Width), min(px(440), area.Height))
        self.form.BackColor = Color.FromArgb(25, 25, 27)
        self.form.Font = Font('Segoe UI', 10)
        self.form.Icon = owner.Icon
        self.form.Owner = owner
        self.control = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = str(Path(home) / 'state/native-browser-profile')
        self.control.CreationProperties = props
        self.control.Dock = DockStyle.Fill
        self.form.Controls.Add(self.control)
        toolbar = Panel()
        toolbar.Dock = DockStyle.Top
        toolbar.Height = px(46)
        self.address = TextBox()
        self.address.Left, self.address.Top, self.address.Height = px(200), px(12), px(24)
        self.address.Width = px(730)
        self.address.BackColor = Color.FromArgb(38, 38, 40)
        self.address.ForeColor = Color.WhiteSmoke
        self.address.AccessibleName = 'Browser address'
        toolbar.Controls.Add(self.address)
        for label, left, operation in [('‹', 10, 'back'), ('›', 54, 'forward'), ('↻', 98, 'reload'), ('Go', 148, 'go')]:
            button = Button()
            button.Text, button.Left, button.Top, button.Width, button.Height = label, px(left), px(8), px(42), px(30)
            button.FlatStyle = FlatStyle.Flat
            button.FlatAppearance.BorderSize = 0
            button.BackColor = Color.FromArgb(237, 129, 59) if operation == 'go' else Color.FromArgb(38, 38, 40)
            button.ForeColor = Color.Black if operation == 'go' else Color.WhiteSmoke
            button.AccessibleName = {'back':'Back','forward':'Forward','reload':'Reload','go':'Navigate'}[operation]
            button.Click += lambda sender, args, name=operation: self._toolbar(name)
            toolbar.Controls.Add(button)
        self.address.KeyDown += lambda sender, args: self._address_key(args, Keys.Enter)
        self.form.Resize += lambda sender, args: setattr(self.address, 'Width', max(px(220), self.form.ClientSize.Width - px(210)))
        self.form.Controls.Add(toolbar)
        toolbar.BringToFront()
        self.control.CoreWebView2InitializationCompleted += self._initialized
        self.form.FormClosed += self._closed
        self.form.Show()
        self.control.EnsureCoreWebView2Async(None)

    def _address_key(self, args, enter):
        if args.KeyCode == enter:
            args.SuppressKeyPress = True
            self._toolbar('go')

    def _toolbar(self, action):
        try:
            if action == 'go':
                value = self.address.Text.strip()
                if '://' not in value:
                    value = 'https://' + value
                self.navigate(browser_url(value))
            else:
                self.history(action)
        except Exception as exc:
            self.changed('loaded', str(exc)[:400])

    def _initialized(self, sender, args):
        try:
            if not args.IsSuccess:
                raise RuntimeError('Windows WebView2 could not initialize.')
            core = self.control.CoreWebView2
            core.Settings.AreHostObjectsAllowed = False
            core.Settings.IsWebMessageEnabled = False
            core.Settings.AreDevToolsEnabled = False
            self._navigation_id = None
            core.NavigationStarting += self._navigation
            core.NavigationCompleted += self._loaded
            core.SourceChanged += self._source_changed
            core.DocumentTitleChanged += lambda sender, args: self.changed('title', core.DocumentTitle)
            core.NewWindowRequested += self._popup
            core.DownloadStarting += lambda sender, args: setattr(args, 'Cancel', True)
            core.PermissionRequested += self._permission
            if getattr(self, 'capture_diagnostics', False):
                from System import Action, String
                from System.Threading.Tasks import Task
                self.diagnostics_ready = threading.Event()
                def registered(task):
                    try:
                        str(task.Result)
                    except Exception as exc:
                        self.error = exc
                    finally:
                        self.diagnostics_ready.set()
                core.AddScriptToExecuteOnDocumentCreatedAsync(PREVIEW_DIAGNOSTICS_SCRIPT).ContinueWith(Action[Task[String]](registered))
            core.Navigate('about:blank')
        except Exception as exc:
            self.error = exc
        finally:
            self.event.set()

    def _navigation(self, sender, args):
        if getattr(self, 'disposed', False):
            args.Cancel = True
            return
        self._navigation_id = getattr(args, 'NavigationId', None)
        try:
            url = browser_url(str(args.Uri))
            guard = getattr(self, 'navigation_guard', None)
            if guard and not guard(url):
                raise ValueError('Preview navigation must stay on its owned loopback origin.')
            try: self.clear_indicator()
            except Exception: pass
            self.address.Text = url
            self.changed('navigation', url)
        except ValueError:
            args.Cancel = True
            self.changed('loaded', 'Only HTTP and HTTPS pages can open in Forge Browser.')

    def _loaded(self, sender, args):
        identifier = getattr(args, 'NavigationId', None)
        if (getattr(self, 'disposed', False) or identifier is None or
                identifier != getattr(self, '_navigation_id', None)):
            return
        self.changed('loaded', None if args.IsSuccess else 'Navigation failed: ' + str(args.WebErrorStatus))

    def _source_changed(self, sender, args):
        if getattr(self, 'disposed', False):
            return
        # SPA/history/fragment changes need the same invalidation as full loads.
        source = str(self.control.CoreWebView2.Source)
        try: self.clear_indicator()
        except Exception: pass
        self.address.Text = source
        self.changed('navigation', source)
        if not args.IsNewDocument:
            self.changed('loaded')

    def _closed(self, sender, args):
        self.disposed = True
        self._navigation_id = None
        self.changed('closed')

    def _popup(self, sender, args):
        args.Handled = True
        try:
            url = browser_url(str(args.Uri))
            guard = getattr(self, 'navigation_guard', None)
            if not guard or guard(url):
                self.control.CoreWebView2.Navigate(url)
        except ValueError:
            pass

    @staticmethod
    def _permission(sender, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState
        args.State = CoreWebView2PermissionState.Deny

    def ready(self):
        if not self.event.wait(20):
            raise TimeoutError('Windows WebView2 initialization timed out.')
        diagnostics_ready = getattr(self, 'diagnostics_ready', None)
        if diagnostics_ready and not diagnostics_ready.wait(20):
            raise TimeoutError('Preview diagnostics initialization timed out.')
        if self.error:
            raise self.error

    def show(self, check=None):
        def reveal(_):
            if check: check()
            self.form.Show(); self.form.Activate()
        self.ui(reveal)

    def focused(self):
        return self.ui(lambda _: bool(self.form.ContainsFocus and self.form.Visible))

    def navigate(self, url, check=None):
        # Navigate schedules WebView2 events asynchronously. Mark loading before
        # returning, and reject old completions while the next Starting is pending.
        def request(_):
            if check: check()
            self._navigation_id = None
            self.changed('navigation', url)
            self.control.CoreWebView2.Navigate(url)
        self.ui(request)

    def history(self, action, check=None):
        def change(_):
            if check: check()
            core = self.control.CoreWebView2
            if action == 'back' and core.CanGoBack:
                operation = core.GoBack
            elif action == 'forward' and core.CanGoForward:
                operation = core.GoForward
            elif action == 'reload':
                operation = core.Reload
            else:
                return
            self._navigation_id = None
            self.changed('navigation', str(core.Source))
            operation()
        self.ui(change)

    def evaluate(self, script, check=None):
        from System import Action, String
        from System.Threading.Tasks import Task
        done, result = threading.Event(), {}
        def complete(task):
            try:
                failure = _managed_task_failure(task)
                if failure:
                    raise BrowserCompletionError(failure)
                result['value'] = json.loads(str(task.Result))
            except BrowserCompletionError as exc:
                result['error'] = exc
            except (ValueError, TypeError):
                result['error'] = BrowserCompletionError('protocol_error')
            except Exception:
                result['error'] = BrowserCompletionError('faulted')
            finally:
                done.set()
        def execute(_):
            if check: check()
            result['continuation'] = self.control.CoreWebView2.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](complete))
        self.ui(execute)
        _wait_managed_callback(done, result.get('continuation'),
            'Browser script timed out; inspect the action outcome before retrying.')
        if 'error' in result:
            raise result['error']
        if result.get('value') is None:
            raise RuntimeError('Browser script did not return a result; inspect the page before retrying.')
        return result['value']

    def screenshot(self, check=None):
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        from System import Action
        from System.IO import MemoryStream
        from System.Threading.Tasks import Task
        lifetime = self._reserve_capture()
        done, result = threading.Event(), {}
        def complete(task):
            with lifetime.lock:
                lifetime.callback_active = True
            try:
                failure = _managed_task_failure(task)
                if failure:
                    raise BrowserCompletionError(failure)
                task.GetAwaiter().GetResult()
                result['value'] = bytes(lifetime.stream.ToArray())
            except BrowserCompletionError as exc:
                result['error'] = exc
            except Exception:
                result['error'] = BrowserCompletionError('faulted')
            finally:
                try:
                    lifetime.dispose_stream()
                except Exception:
                    result['error'] = BrowserCompletionError('faulted')
                finally:
                    with lifetime.lock:
                        lifetime.callback_active = False
                    done.set()
        def capture(_):
            with lifetime.lock:
                if lifetime.aborted:
                    return
            if check: check()
            with lifetime.lock:
                if lifetime.aborted:
                    return
                lifetime.submission_started = True
            try:
                lifetime.source = self.control.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, lifetime.stream)
                lifetime.continuation = lifetime.source.ContinueWith(Action[Task](complete))
            finally:
                with lifetime.lock:
                    lifetime.registration_finished = True
        try:
            lifetime.stream = MemoryStream()
            self.ui(capture)
        except BaseException:
            with lifetime.lock:
                lifetime.aborted = True
                started = lifetime.submission_started
                if not started:
                    lifetime.unsubmitted = lifetime.registration_finished = True
            if not started:
                self._retire_capture(lifetime)
                raise
            deadline = lifetime.wait_after_registration_failure()
            if deadline is not None:
                self._retire_capture(lifetime, deadline=deadline)
            raise BrowserCompletionError('protocol_error') from None
        try:
            _wait_managed_callback(done, lifetime.continuation, 'Browser screenshot timed out.')
        except TimeoutError:
            raise  # Keep ownership; do not extend the expired budget with retirement.
        except BaseException:
            self._retire_capture(lifetime)
            raise
        self._retire_capture(lifetime, managed_completed=True)
        if 'error' in result:
            raise result['error']
        return result['value']

    def stop(self):
        self.ui(lambda _: self.control.CoreWebView2.Stop())

    def clear_indicator(self):
        def clear(_):
            try:self.control.CoreWebView2.ExecuteScriptAsync(CLEAR_TARGET_SCRIPT)
            except Exception:pass
        self.ui(clear)

    def close(self, check=None):
        def close(_):
            if check: check()
            self.form.Close()
        self.ui(close)
