"""Actual browser callbacks with inert managed tasks; no CLR or desktop access."""
import sys
import threading
from types import SimpleNamespace

import pytest

import native_browser as native
from embedded_browser import EmbeddedWebView2Page


class GenericTask:
    def __class_getitem__(cls, argument):
        return cls


class GenericAction:
    def __class_getitem__(cls, argument):
        return lambda callback: callback


@pytest.fixture
def managed(monkeypatch):
    state = SimpleNamespace(callback=True, callback_function=None, source_cancelled=False,
        source_faulted=False, source_error=None, source_reads=0, json_result='{"ok":true}',
        source_fault_observations=0, managed_fault_observations=0, observation_error=False,
        source_completed=True, source_wait_result=True, source_wait_error=False,
        source_wait_arguments=[], source_wait_delay=0.0, source_wait_gate=None,
        source_status_reads=0, source_completed_reads=0, managed_status_reads=0,
        source_status_delay=0.0, source_status_delay_after_wait=None,
        source_wait_started=threading.Event(), managed_completed=False,
        registration_error=False, callback_before_registration_error=False,
        image=b'synthetic-image', streams=[], dispose_error=False, calls=[], registrations=0,
        read_gate=None, read_started=threading.Event(),
        wait_result=True, wait_error=None, wait_failure=None, wait_gate=None,
        wait_started=threading.Event(), wait_arguments=[], cancelled=False, faulted=False,
        ui_active=False, ui_error_before=False, ui_error_after=False,
        lock=threading.Lock(), now=100.0, event_delay=0.0, wait_delay=0.0,
        status_delay=0.0, status_delay_after_wait=None, ui_delay=0.0, clocked=False)

    class Continuation:
        @property
        def IsCompleted(self):
            return state.managed_completed or state.cancelled or state.faulted

        @property
        def Exception(self):
            state.managed_fault_observations += 1
            if state.observation_error:
                raise RuntimeError('Synthetic private fault observation error')
            return FaultDetails()

        @property
        def IsCanceled(self):
            state.managed_status_reads += 1
            if state.clocked:
                state.now += state.status_delay
            return state.cancelled

        @property
        def IsFaulted(self):
            state.managed_status_reads += 1
            if state.clocked:
                state.now += state.status_delay
            return state.faulted

        def Wait(self, milliseconds):
            assert not state.ui_active
            assert state.lock.acquire(blocking=False)
            state.lock.release()
            state.wait_arguments.append(milliseconds)
            state.wait_started.set()
            if state.clocked:
                state.now += state.wait_delay
            if state.status_delay_after_wait is not None:
                state.status_delay = state.status_delay_after_wait
            if state.wait_error:
                state.cancelled = state.wait_failure == 'cancelled'
                state.faulted = state.wait_failure == 'faulted'
                raise RuntimeError('Synthetic private managed error')
            if state.wait_gate is not None:
                state.managed_completed = state.wait_gate.wait(milliseconds / 1000)
                return state.managed_completed
            state.managed_completed = state.wait_result is True
            return state.wait_result

    class SourceTask:
        @property
        def IsCompleted(self):
            state.source_completed_reads += 1
            return state.source_completed or state.source_cancelled or state.source_faulted

        def Wait(self, milliseconds):
            assert not state.ui_active
            assert state.lock.acquire(blocking=False)
            state.lock.release()
            state.source_wait_arguments.append(milliseconds)
            state.source_wait_started.set()
            if state.clocked:
                state.now += state.source_wait_delay
            if state.source_status_delay_after_wait is not None:
                state.source_status_delay = state.source_status_delay_after_wait
            if state.source_wait_error:
                raise RuntimeError('Synthetic private source wait error')
            if state.source_wait_gate is not None:
                state.source_completed = state.source_wait_gate.wait(milliseconds / 1000)
                return state.source_completed
            state.source_completed = state.source_wait_result is True
            return state.source_wait_result

        @property
        def Exception(self):
            state.source_fault_observations += 1
            if state.observation_error:
                raise RuntimeError('Synthetic private fault observation error')
            return FaultDetails()
        @property
        def IsCanceled(self):
            state.source_status_reads += 1
            if state.clocked:
                state.now += state.source_status_delay
            return state.source_cancelled
        @property
        def IsFaulted(self):
            state.source_status_reads += 1
            if state.clocked:
                state.now += state.source_status_delay
            return state.source_faulted
        @property
        def Result(self):
            state.source_reads += 1
            if state.source_error:
                raise RuntimeError('Synthetic private source error')
            return state.json_result
        def GetAwaiter(self):
            def finish():
                state.source_reads += 1
                if state.source_error:
                    raise RuntimeError('Synthetic private source error')
            return SimpleNamespace(GetResult=finish)

    class FaultDetails:
        def __str__(self):
            raise AssertionError('Private fault details must not be stringified')
        def __iter__(self):
            raise AssertionError('Private fault details must not be enumerated')

    class Operation(SourceTask):
        def ContinueWith(self, callback):
            state.registrations += 1
            state.callback_function = lambda: callback(self)
            if state.callback and (not state.registration_error or state.callback_before_registration_error):
                state.callback_function()
            if state.registration_error:
                raise RuntimeError('Synthetic private callback registration error')
            return Continuation()

    class Stream:
        def __init__(self):
            self.disposals = 0
            state.streams.append(self)
        def ToArray(self):
            state.read_started.set()
            if state.read_gate is not None:
                assert state.read_gate.wait(2)
            return state.image
        def Dispose(self):
            self.disposals += 1
            if state.dispose_error:
                raise RuntimeError('Synthetic private disposal error')

    class Core:
        def ExecuteScriptAsync(self, script):
            state.calls.append(('evaluate', script))
            return Operation()
        def CapturePreviewAsync(self, image_format, stream):
            state.calls.append(('screenshot', image_format))
            return Operation()

    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=GenericAction, String=str))
    monkeypatch.setitem(sys.modules, 'System.Threading.Tasks', SimpleNamespace(Task=GenericTask))
    monkeypatch.setitem(sys.modules, 'System.IO', SimpleNamespace(MemoryStream=Stream))
    monkeypatch.setitem(sys.modules, 'Microsoft.Web.WebView2.Core',
        SimpleNamespace(CoreWebView2CapturePreviewImageFormat=SimpleNamespace(Png='synthetic-png')))
    view = object.__new__(native.WebView2Page)
    view.control = SimpleNamespace(CoreWebView2=Core())
    def ui(operation):
        if state.clocked:
            state.now += state.ui_delay
        if state.ui_error_before:
            raise RuntimeError('Synthetic registration error')
        with state.lock:
            state.ui_active = True
            try:
                operation(None)
            finally:
                state.ui_active = False
        if state.ui_error_after:
            raise RuntimeError('Synthetic registration error')
    view.ui = ui
    state.view = view
    return state


def clocked(state, monkeypatch):
    state.clocked = True
    class Event:
        flag = False
        def set(self): self.flag = True
        def wait(self, timeout):
            assert 0 <= timeout <= 12
            state.now += state.event_delay
            return self.flag
    monkeypatch.setattr(native, 'threading', SimpleNamespace(Event=Event, Lock=threading.Lock))
    monkeypatch.setattr(native, 'time', SimpleNamespace(monotonic=lambda: state.now))


def invoke(state, operation):
    return state.view.evaluate('synthetic-script') if operation == 'evaluate' else state.view.screenshot()


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_usable_result_waits_for_managed_completion_outside_ui_and_admission_lock(managed, operation):
    state = managed
    state.wait_gate = threading.Event()
    findings, failures = [], []
    def work():
        try:
            findings.append(invoke(state, operation))
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=work)
    try:
        worker.start()
        assert state.wait_started.wait(1)
        assert findings == [] and worker.is_alive()
    finally:
        state.wait_gate.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []
    assert findings == ([{'ok': True}] if operation == 'evaluate' else [b'synthetic-image'])
    assert len(state.calls) == state.registrations == 1
    if operation == 'screenshot':
        assert state.streams[0].disposals == 1


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
@pytest.mark.parametrize('failure', ['cancelled', 'faulted'])
def test_source_task_failure_is_observed_and_typed_without_exposing_details(managed, operation, failure):
    state = managed
    state.source_cancelled = failure == 'cancelled'
    state.source_faulted = failure == 'faulted'
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, operation)
    assert caught.value.reason == failure and state.source_reads == 0
    assert state.source_fault_observations == int(failure == 'faulted')
    assert not isinstance(caught.value, native.BrowserGuard)
    assert len(state.calls) == state.registrations == 1
    if operation == 'screenshot':
        assert state.streams[0].disposals == 1


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
@pytest.mark.parametrize('failure', ['cancelled', 'faulted', 'protocol_error'])
def test_managed_wait_failure_has_fixed_category_and_no_safe_replay(managed, operation, failure):
    state = managed
    state.wait_error, state.wait_failure = True, failure
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, operation)
    assert caught.value.reason == failure
    assert state.managed_fault_observations >= 1 if failure == 'faulted' else state.managed_fault_observations == 0
    assert 'Synthetic private' not in str(caught.value)
    assert caught.value.__suppress_context__ and not isinstance(caught.value, native.BrowserGuard)
    assert len(state.calls) == state.registrations == 1
    if operation == 'screenshot':
        assert state.streams[0].disposals == 1


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
@pytest.mark.parametrize('value', [None, 1, 'true'])
def test_non_boolean_managed_wait_is_a_protocol_failure(managed, operation, value):
    managed.wait_result = value
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(managed, operation)
    assert caught.value.reason == 'protocol_error'


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_event_timeout_uses_original_budget_and_does_not_replay(managed, monkeypatch, operation):
    clocked(managed, monkeypatch)
    managed.callback, managed.event_delay = False, 12
    with pytest.raises(TimeoutError):
        invoke(managed, operation)
    assert managed.now == 112 and managed.wait_arguments == []
    assert len(managed.calls) == managed.registrations == 1
    if operation == 'screenshot':
        assert managed.streams[0].disposals == 0
        managed.callback_function()
        assert managed.streams[0].disposals == 1


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_event_and_managed_wait_share_one_budget_starting_after_ui_registration(managed, monkeypatch, operation):
    clocked(managed, monkeypatch)
    managed.ui_delay, managed.event_delay, managed.wait_delay = 25, 10.25, 1
    result = invoke(managed, operation)
    assert result == ({'ok': True} if operation == 'evaluate' else b'synthetic-image')
    assert managed.wait_arguments == [1750] and managed.now == 136.25


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
@pytest.mark.parametrize('delay', [12, 13])
def test_deadline_rechecked_after_event_before_managed_wait(managed, monkeypatch, operation, delay):
    clocked(managed, monkeypatch)
    managed.event_delay = delay
    with pytest.raises(TimeoutError):
        invoke(managed, operation)
    assert managed.wait_arguments == []


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_late_true_managed_completion_cannot_extend_deadline(managed, monkeypatch, operation):
    clocked(managed, monkeypatch)
    managed.event_delay, managed.wait_delay = 10, 2
    with pytest.raises(TimeoutError):
        invoke(managed, operation)
    assert managed.wait_arguments == [2000] and managed.now == 112


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_final_status_inspection_cannot_extend_deadline(managed, monkeypatch, operation):
    clocked(managed, monkeypatch)
    managed.status_delay_after_wait = 6
    with pytest.raises(TimeoutError):
        invoke(managed, operation)
    assert managed.now == 112


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
def test_managed_timeout_rejects_local_callback_result_without_replay(managed, operation):
    managed.wait_result = False
    with pytest.raises(TimeoutError):
        invoke(managed, operation)
    assert len(managed.calls) == managed.registrations == 1


def test_invalid_json_is_a_fixed_protocol_failure(managed):
    managed.json_result = 'Synthetic private invalid JSON'
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(managed, 'evaluate')
    assert caught.value.reason == 'protocol_error' and 'Synthetic private' not in str(caught.value)


def test_null_json_still_requires_usable_result(managed):
    managed.json_result = 'null'
    with pytest.raises(RuntimeError, match='did not return a result'):
        invoke(managed, 'evaluate')
    assert len(managed.wait_arguments) == 1


def test_disposal_failure_sets_done_and_reports_fault_without_double_disposal(managed):
    managed.dispose_error = True
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(managed, 'screenshot')
    assert caught.value.reason == 'faulted'
    assert managed.streams[0].disposals == 1 and len(managed.wait_arguments) == 1


@pytest.mark.parametrize('after_registration', [False, True])
def test_registration_failure_disposes_stream_once_without_repeat_capture(managed, after_registration):
    managed.ui_error_before = not after_registration
    managed.ui_error_after = after_registration
    with pytest.raises(RuntimeError) as caught:
        invoke(managed, 'screenshot')
    if after_registration:
        assert isinstance(caught.value, native.BrowserCompletionError)
        assert caught.value.reason == 'protocol_error' and caught.value.__suppress_context__
    else:
        assert str(caught.value) == 'Synthetic registration error'
    assert managed.streams[0].disposals == 1
    assert len(managed.calls) == int(after_registration)


@pytest.mark.parametrize('failure', ['cancelled', 'faulted'])
def test_completed_managed_failure_without_callback_releases_stream_once(managed, failure):
    managed.callback = False
    managed.cancelled = failure == 'cancelled'
    managed.faulted = failure == 'faulted'
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(managed, 'screenshot')
    assert caught.value.reason == failure and managed.streams[0].disposals == 1


def test_embedded_research_and_preview_inherit_both_managed_completion_barriers():
    assert EmbeddedWebView2Page.evaluate is native.WebView2Page.evaluate
    assert EmbeddedWebView2Page.screenshot is native.WebView2Page.screenshot


@pytest.mark.parametrize('operation', ['evaluate', 'screenshot'])
@pytest.mark.parametrize('source_failure', [False, True])
def test_fault_observation_failure_never_exposes_details_or_changes_known_fault_category(managed, operation, source_failure):
    managed.observation_error = True
    managed.source_faulted = source_failure
    managed.wait_error, managed.wait_failure = not source_failure, 'faulted'
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(managed, operation)
    assert caught.value.reason == 'faulted' and 'Synthetic private' not in str(caught.value)
    assert managed.source_fault_observations + managed.managed_fault_observations >= 1


@pytest.mark.parametrize('view_type', [native.WebView2Page, EmbeddedWebView2Page])
def test_pending_registration_retains_source_and_stream_and_blocks_duplicate_capture(managed, view_type):
    state = managed
    state.view.__class__ = view_type  # Both real initializers are intentionally bypassed; ownership must be lazy.
    state.registration_error, state.source_completed, state.source_wait_result = True, False, False
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error' and 'Synthetic private' not in str(caught.value)
    lifetime = state.view._capture_lifetime
    assert lifetime.source is not None and lifetime.continuation is None
    assert lifetime.stream is state.streams[0] and lifetime.stream.disposals == 0
    with pytest.raises(native.BrowserCompletionError) as blocked:
        invoke(state, 'screenshot')
    assert blocked.value.reason == 'pending' and 'retry' not in str(blocked.value)
    assert state.view._capture_lifetime is lifetime
    assert len(state.streams) == len(state.calls) == state.registrations == 1


@pytest.mark.parametrize('failure', [None, 'faulted', 'cancelled'])
def test_late_source_completion_retires_failed_registration_before_new_capture(managed, failure):
    state = managed
    state.registration_error, state.source_completed, state.source_wait_result = True, False, False
    with pytest.raises(native.BrowserCompletionError):
        invoke(state, 'screenshot')
    lifetime = state.view._capture_lifetime
    state.source_completed = True
    state.source_faulted, state.source_cancelled = failure == 'faulted', failure == 'cancelled'
    assert state.view._retire_capture(lifetime)
    assert lifetime.stream.disposals == 1 and state.view._capture_lifetime is None
    assert state.source_fault_observations >= 1 if failure == 'faulted' else state.source_fault_observations == 0
    state.source_faulted = state.source_cancelled = state.registration_error = False
    assert invoke(state, 'screenshot') == b'synthetic-image'
    assert len(state.calls) == state.registrations == len(state.streams) == 2
    assert [stream.disposals for stream in state.streams] == [1, 1]


def test_registration_failure_wait_uses_one_post_ui_budget_and_retains_unfinished_task(managed, monkeypatch):
    state = managed
    clocked(state, monkeypatch)
    state.registration_error, state.source_completed, state.source_wait_result = True, False, False
    state.ui_delay, state.source_wait_delay = 25, 12
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error'
    assert state.source_wait_arguments == [12000] and state.now == 137
    assert state.streams[0].disposals == 0 and state.view._capture_lifetime.source is not None


def test_registration_failure_can_release_source_after_worker_wait_observes_completion(managed):
    state = managed
    state.registration_error, state.source_completed = True, False
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error'
    assert len(state.source_wait_arguments) == 1 and state.streams[0].disposals == 1
    assert state.view._capture_lifetime is None and len(state.calls) == 1


def test_registration_failure_wait_error_is_sanitized_and_retains_pending_stream(managed):
    state = managed
    state.registration_error, state.source_completed, state.source_wait_error = True, False, True
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error' and caught.value.__suppress_context__
    assert 'Synthetic private' not in str(caught.value)
    assert state.streams[0].disposals == 0 and state.view._capture_lifetime is not None


def test_genuine_preflight_guard_disposes_unsubmitted_stream_and_preserves_guard(managed):
    def reject():
        raise native.BrowserGuard('Synthetic preflight rejection', 'browser_hidden')
    with pytest.raises(native.BrowserGuard) as caught:
        managed.view.screenshot(check=reject)
    assert caught.value.reason == 'browser_hidden'
    assert managed.calls == [] and managed.streams[0].disposals == 1
    assert managed.view._capture_lifetime is None


def test_concurrent_capture_cannot_replace_owned_callback_before_managed_completion(managed):
    state = managed
    state.wait_gate = threading.Event()
    findings, failures = [], []
    def work():
        try: findings.append(invoke(state, 'screenshot'))
        except Exception as exc: failures.append(exc)
    worker = threading.Thread(target=work)
    try:
        worker.start()
        assert state.wait_started.wait(1)
        lifetime = state.view._capture_lifetime
        with pytest.raises(native.BrowserCompletionError) as caught:
            invoke(state, 'screenshot')
        assert caught.value.reason == 'pending' and state.view._capture_lifetime is lifetime
        assert len(state.streams) == len(state.calls) == 1
    finally:
        state.wait_gate.set()
        worker.join(2)
    assert not worker.is_alive() and findings == [b'synthetic-image'] and failures == []
    assert state.view._capture_lifetime is None and state.streams[0].disposals == 1


def test_retirement_cannot_dispose_during_callback_read_and_later_disposes_exactly_once(managed):
    state = managed
    state.registration_error, state.source_completed, state.source_wait_result = True, False, False
    with pytest.raises(native.BrowserCompletionError):
        invoke(state, 'screenshot')
    lifetime = state.view._capture_lifetime
    state.source_completed, state.read_gate = True, threading.Event()
    callback = threading.Thread(target=state.callback_function)
    try:
        callback.start()
        assert state.read_started.wait(1)
        with pytest.raises(native.BrowserCompletionError) as caught:
            invoke(state, 'screenshot')
        assert caught.value.reason == 'pending' and lifetime.stream.disposals == 0
    finally:
        state.read_gate.set()
        callback.join(2)
    assert not callback.is_alive() and lifetime.stream.disposals == 1
    assert state.view._retire_capture(lifetime)
    assert state.view._capture_lifetime is None and lifetime.stream.disposals == 1


def test_queued_ui_capture_cannot_submit_after_dispatch_timeout_and_stream_retirement(managed):
    queued = []
    def queue_and_timeout(operation):
        queued.append(operation)
        raise TimeoutError('Synthetic UI dispatch timeout')
    managed.view.ui = queue_and_timeout
    with pytest.raises(TimeoutError, match='Synthetic UI dispatch timeout'):
        invoke(managed, 'screenshot')
    assert managed.view._capture_lifetime is None and managed.streams[0].disposals == 1
    queued[0](None)
    assert managed.calls == [] and managed.registrations == 0
    assert managed.streams[0].disposals == 1


def test_ui_timeout_after_submission_started_retains_stream_until_late_registration_finishes(managed):
    state = managed
    started, release = threading.Event(), threading.Event()
    core_capture = state.view.control.CoreWebView2.CapturePreviewAsync
    def slow_submission(image_format, stream):
        started.set()
        assert release.wait(2)
        return core_capture(image_format, stream)
    state.view.control.CoreWebView2.CapturePreviewAsync = slow_submission
    workers = []
    def dispatch_and_timeout(operation):
        worker = threading.Thread(target=operation, args=(None,))
        workers.append(worker)
        worker.start()
        assert started.wait(1)
        raise TimeoutError('Synthetic UI dispatch timeout')
    state.view.ui = dispatch_and_timeout
    try:
        with pytest.raises(native.BrowserCompletionError) as caught:
            invoke(state, 'screenshot')
        assert caught.value.reason == 'protocol_error'
        lifetime = state.view._capture_lifetime
        assert lifetime.submission_started and lifetime.source is None and lifetime.aborted
        assert lifetime.stream.disposals == 0
        with pytest.raises(native.BrowserCompletionError) as blocked:
            invoke(state, 'screenshot')
        assert blocked.value.reason == 'pending' and len(state.streams) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert lifetime.registration_finished and lifetime.stream.disposals == 1
    state.managed_completed = True
    assert state.view._retire_capture(lifetime)
    assert state.view._capture_lifetime is None and lifetime.stream.disposals == 1


@pytest.mark.parametrize('wait_result', [False, True])
def test_registration_failure_performs_no_status_or_retirement_after_wait_budget_expires(managed, monkeypatch, wait_result):
    state = managed
    clocked(state, monkeypatch)
    state.registration_error, state.source_completed = True, False
    state.source_wait_result, state.source_wait_delay = wait_result, 12
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error' and state.now == 112
    assert state.source_wait_arguments == [12000]
    assert state.source_status_reads == 2 and state.source_completed_reads == 1
    assert state.streams[0].disposals == 0 and state.view._capture_lifetime is not None


def test_post_registration_dispatch_error_has_no_post_deadline_continuation_inspection(managed, monkeypatch):
    state = managed
    clocked(state, monkeypatch)
    state.ui_error_after, state.wait_delay = True, 12
    with pytest.raises(native.BrowserCompletionError) as caught:
        invoke(state, 'screenshot')
    assert caught.value.reason == 'protocol_error' and state.now == 112
    assert state.managed_status_reads == 2 and state.source_completed_reads == 0
    assert state.streams[0].disposals == 1 and state.view._capture_lifetime is not None


def test_registration_failure_status_expiry_prevents_wait_and_retirement(managed, monkeypatch):
    state = managed
    clocked(state, monkeypatch)
    state.registration_error, state.source_completed, state.source_status_delay = True, False, 6
    with pytest.raises(native.BrowserCompletionError):
        invoke(state, 'screenshot')
    assert state.now == 112 and state.source_wait_arguments == []
    assert state.source_status_reads == 2 and state.source_completed_reads == 0
    assert state.streams[0].disposals == 0 and state.view._capture_lifetime is not None


def test_registration_failure_retirement_uses_remaining_original_budget(managed, monkeypatch):
    state = managed
    clocked(state, monkeypatch)
    state.registration_error, state.source_completed = True, False
    state.source_status_delay_after_wait = 3
    with pytest.raises(native.BrowserCompletionError):
        invoke(state, 'screenshot')
    assert state.now == 112 and len(state.source_wait_arguments) == 1
    assert state.source_status_reads == 6 and state.source_completed_reads == 1
    assert state.streams[0].disposals == 0 and state.view._capture_lifetime is not None
