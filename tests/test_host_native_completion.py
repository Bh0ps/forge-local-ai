"""Inert WinForms completion contracts; no CLR, desktop, vault or live state."""
import sys
import threading
from types import SimpleNamespace

import pytest

from desktop import Bridge, _smoke_tray_open


class Done:
    def __init__(self):
        self.set_value = False
        self.after_wait = lambda: None
        self.waits = []

    def set(self):
        self.set_value = True

    def wait(self, timeout):
        self.waits.append(timeout)
        self.after_wait()
        return self.set_value


class ManagedResult:
    def __init__(self, api, completed=True):
        self.api, self.completed = api, completed
        self.status_reads = self.handle_reads = self.closes = self.ends = 0
        self.waits = []
        self.wait = lambda milliseconds: self.completed
        self.end = lambda: None
        self.handle_error = self.end_error = self.close_error = False

    def unlocked(self):
        assert not self.api._native_admission._is_owned()
        assert not self.api._service.admission_lock._is_owned()

    @property
    def IsCompleted(self):
        self.status_reads += 1
        return self.completed

    @property
    def AsyncWaitHandle(self):
        self.handle_reads += 1
        if self.handle_error:
            raise RuntimeError('Synthetic private handle details')
        return self

    def WaitOne(self, milliseconds):
        self.unlocked()
        self.waits.append(milliseconds)
        return self.wait(milliseconds)

    def Close(self):
        self.unlocked()
        self.closes += 1
        if self.close_error:
            raise RuntimeError('Synthetic private close details')


@pytest.fixture
def native(monkeypatch):
    service = SimpleNamespace(admission_lock=threading.RLock(),
        update_manager=SimpleNamespace(applying=False, shutting_down=False))
    api = Bridge(service)
    api._window = SimpleNamespace(uid='synthetic-window')
    api._native_input = SimpleNamespace(idle=lambda: True)
    calls, queued, results = [], [], []
    form = SimpleNamespace(InvokeRequired=True)
    form.mode, form.created = 'immediate', None

    def begin(callback):
        queued.append(callback)
        result = form.created or ManagedResult(api)
        results.append(result)
        if form.mode == 'immediate':
            callback()
        elif form.mode == 'unknown':
            raise RuntimeError('Synthetic private registration details')
        return None if form.mode == 'missing' else result

    def end(result):
        result.unlocked()
        result.ends += 1
        result.end()
        if result.end_error:
            raise RuntimeError('Synthetic private completion details')

    form.BeginInvoke, form.EndInvoke = begin, end
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms', SimpleNamespace(
        BrowserView=SimpleNamespace(instances={api._window.uid: form})))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda callback: callback))
    original = api._NativeCall
    made = []

    def lifetime(*args):
        call = original(*args)
        call.done = Done()
        made.append(call)
        return call

    api._NativeCall = lifetime
    return SimpleNamespace(api=api, form=form, made=made, queued=queued,
        results=results, calls=calls)


def test_python_result_cannot_return_or_release_before_managed_delegate_completes(native):
    api = native.api
    managed = ManagedResult(api, False)
    native.form.created = managed
    entered, finish, returned = threading.Event(), threading.Event(), threading.Event()
    values = []

    def wait(milliseconds):
        entered.set()
        assert finish.wait(1)
        managed.completed = True
        return True

    managed.wait = wait
    def caller():
        values.append(api._native(lambda form: native.calls.append(form) or 'synthetic-value'))
        returned.set()

    worker = threading.Thread(target=caller)
    worker.start()
    try:
        assert entered.wait(1)
        assert not returned.is_set() and api._native_pending == 1
        assert api._update_input_idle() is False
        assert len(native.calls) == 1 and managed.ends == managed.closes == 0
    finally:
        finish.set()
        worker.join(2)
    assert not worker.is_alive() and values == ['synthetic-value']
    assert managed.ends == managed.closes == 1
    assert api._native_pending == 0 and api._native_calls == []
    assert api._update_input_idle() is True


@pytest.mark.parametrize('value', [None, 'synthetic-value'])
def test_synchronous_ui_path_never_registers_waits_or_retires_a_managed_call(native, value):
    native.form.InvokeRequired = False
    assert native.api._native(lambda form: value) == value
    assert not native.queued and not native.api._native_calls
    assert native.api._native_pending == 0 and native.made[0].done.waits == []


def test_operation_failure_is_preserved_only_after_endinvoke(native):
    managed = ManagedResult(native.api)
    native.form.created = managed
    def denied(form):
        assert managed.ends == 0
        raise ValueError('Synthetic permission refusal')
    with pytest.raises(ValueError, match='Synthetic permission refusal'):
        native.api._native(denied)
    assert managed.ends == 1 and native.api._native_pending == 0


def test_deadline_is_shared_without_post_deadline_status_or_wait(native, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    managed = ManagedResult(native.api, False)
    native.form.created = managed
    original = native.api._NativeCall
    def lifetime(*args):
        call = original(*args)
        call.done.after_wait = lambda: clock.__setitem__(0, 5.0)
        return call
    native.api._NativeCall = lifetime
    with pytest.raises(native.api._NativeCompletionError) as failure:
        native.api._native(lambda form: 'not-usable')
    assert failure.value.reason == 'timeout'
    assert managed.status_reads == managed.handle_reads == managed.ends == 0
    assert native.api._native_pending == 1 and len(native.api._native_calls) == 1


def test_remaining_managed_wait_cannot_extend_five_seconds_and_later_probe_retires(native, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    managed = ManagedResult(native.api, False)
    native.form.created = managed
    original = native.api._NativeCall
    def lifetime(*args):
        call = original(*args)
        call.done.after_wait = lambda: clock.__setitem__(0, 4.8)
        return call
    native.api._NativeCall = lifetime
    def delayed(milliseconds):
        managed.completed = True
        clock[0] = 5.1
        return True
    managed.wait = delayed
    with pytest.raises(native.api._NativeCompletionError) as failure:
        native.api._native(lambda form: 'not-usable')
    assert failure.value.reason == 'timeout'
    assert managed.status_reads == 1 and len(managed.waits) == 1
    assert 0 <= managed.waits[0] <= 200
    assert managed.ends == managed.closes == 0 and native.api._native_pending == 1
    assert native.api._update_input_idle() is True
    assert managed.ends == managed.closes == 1 and native.api._native_pending == 0


def test_unstarted_timed_out_callback_is_aborted_and_never_replayed(native):
    native.form.mode = 'queued'
    with pytest.raises(native.api._NativeCompletionError) as failure:
        native.api._native(lambda form: native.calls.append('action'))
    assert failure.value.reason == 'timeout' and native.api._native_pending == 1
    native.queued[0]()
    assert native.calls == []
    native.results[0].completed = True
    assert native.api._update_input_idle() is True
    assert native.results[0].ends == 1 and native.calls == []


def test_unknown_registration_keeps_lease_but_open_and_owned_quit_keep_separate_capacity(native):
    api = native.api
    native.form.mode = 'unknown'
    with pytest.raises(api._NativeCompletionError) as failure:
        api._native(lambda form: native.calls.append('unconfirmed'))
    assert failure.value.reason == 'protocol_error' and 'private' not in str(failure.value)
    assert api._native_pending == 1 and api._update_input_idle() is False
    native.queued[0]()
    assert native.calls == []
    before = len(native.queued)
    with pytest.raises(api._NativeCompletionError) as pending:
        api._native(lambda form: native.calls.append('duplicate'))
    assert pending.value.reason == 'pending' and len(native.queued) == before
    native.form.mode = 'immediate'
    assert api._native(lambda form: native.calls.append('open'), _update_safe=True) is None
    api._closing, api._owned_teardown.active = True, True
    assert api._native(lambda form: native.calls.append('owned-quit')) is None
    assert native.calls == ['open', 'owned-quit'] and api._native_pending == 1


def test_unknown_call_storage_has_finite_separate_control_and_teardown_slots(native):
    api = native.api
    native.form.mode = 'missing'
    for _ in range(8):
        with pytest.raises(api._NativeCompletionError):
            api._native(lambda form: None, _update_safe=True)
    with pytest.raises(api._NativeCompletionError) as blocked:
        api._native(lambda form: None, _update_safe=True)
    assert blocked.value.reason == 'pending' and len(native.queued) == 8
    api._closing, api._owned_teardown.active = True, True
    for _ in range(4):
        with pytest.raises(api._NativeCompletionError):
            api._native(lambda form: None)
    with pytest.raises(api._NativeCompletionError) as blocked:
        api._native(lambda form: None)
    assert blocked.value.reason == 'pending'
    assert len(native.queued) == len(api._native_calls) == 12
    assert api._update_input_idle() is False


@pytest.mark.parametrize('boundary', ['handle', 'end', 'close'])
def test_managed_failures_are_fixed_and_do_not_repeat_endinvoke_or_close(native, boundary):
    api = native.api
    managed = ManagedResult(api, boundary != 'handle')
    managed.handle_error = boundary == 'handle'
    managed.end_error = boundary == 'end'
    managed.close_error = boundary == 'close'
    if boundary == 'close':
        managed.completed = False
        managed.wait = lambda milliseconds: setattr(managed, 'completed', True) or True
    native.form.created = managed
    with pytest.raises(api._NativeCompletionError) as failure:
        api._native(lambda form: 'not-usable')
    assert 'private' not in str(failure.value)
    ends, closes = managed.ends, managed.closes
    if boundary == 'handle':
        managed.completed = True
        managed.handle_error = False
        assert api._update_input_idle() is True
        assert managed.ends == 1 and managed.closes == 0
    else:
        api._update_input_idle()
        api._update_input_idle()
        assert (managed.ends, managed.closes) == (ends, closes)
        assert (api._native_pending == 1) is (boundary == 'end')


def test_concurrent_idle_reapers_have_one_endinvoke_close_and_release_owner(native):
    api = native.api
    managed = ManagedResult(api, False)
    managed.wait = lambda milliseconds: False
    native.form.created = managed
    with pytest.raises(api._NativeCompletionError):
        api._native(lambda form: None)
    managed.completed = True
    entered, finish = threading.Event(), threading.Event()
    def end():
        entered.set()
        assert finish.wait(1)
    managed.end = end
    first = threading.Thread(target=api._update_input_idle)
    first.start()
    try:
        assert entered.wait(1)
        assert api._update_input_idle() is False
        assert managed.ends == 1 and managed.closes == 0
    finally:
        finish.set()
        first.join(2)
    assert not first.is_alive()
    assert managed.ends == managed.closes == 1
    assert api._native_pending == 0 and api._native_calls == []


def test_packaged_restore_fixture_waits_for_actual_open_worker_before_native_observation():
    clock, actions, observations = [0.0], [], []
    api = SimpleNamespace(_tray_actions_lock=threading.Lock(),_tray_actions=set(),
        _closing=False,_window_closed=False)
    form = SimpleNamespace(Visible=False)
    def action(name):
        actions.append(name)
        api._tray_actions.add(name)
        return True
    def pause(seconds):
        clock[0] += seconds
        form.Visible = True
        api._tray_actions.remove('open')
    def native_read(callback, **kwargs):
        assert not api._tray_actions and kwargs == {'_update_safe':True}
        observations.append(True)
        return callback(form)
    api._tray_action,api._native = action,native_read
    assert _smoke_tray_open(api,clock=lambda:clock[0],pause=pause) is True
    assert actions == ['open'] and observations == [True]


@pytest.mark.parametrize('admitted',[False,True])
def test_packaged_restore_fixture_cannot_accept_rejected_or_stalled_open(admitted):
    clock=[0.0]
    api=SimpleNamespace(_tray_actions_lock=threading.Lock(),_tray_actions={'open'})
    api._tray_action=lambda action:admitted
    api._native=lambda *args,**kwargs:pytest.fail('No native observation before successful Open completion')
    def pause(seconds):clock[0]+=seconds
    with pytest.raises(ValueError,match='Native tray Open'):
        _smoke_tray_open(api,clock=lambda:clock[0],pause=pause)
    assert clock[0] <= 5
