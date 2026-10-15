"""Actual bridge/tray callbacks with inert forms; no personal or native windows."""
import sys
import threading
from types import SimpleNamespace

import pytest
from webview.event import Event

from desktop import Bridge, _tray


def test_cleanup_waits_for_tray_callback_tail_after_quit_signal(host):
    instance = host[0]
    original = instance.quit
    tail, release = threading.Event(), threading.Event()
    def quit_with_tail():
        result = original()
        tail.set()
        assert release.wait(2)
        return result
    instance.quit = quit_with_tail
    try:
        assert instance._tray_action('quit') is True
        assert tail.wait(1) and instance._quit_done.is_set()
        assert instance._await_owned_cleanup(.01) is False
    finally:
        release.set()
    assert instance._await_owned_cleanup(1) is True
    assert all(not worker.is_alive() for worker in instance._tray_workers)


class Form:
    def __init__(self):
        self.Visible = False
        self.WindowState = 'Normal'
        self.InvokeRequired = False
        self.shows = 0
        self.activations = 0
    def Show(self):
        self.shows += 1
        self.Visible = True
    def Activate(self): self.activations += 1


@pytest.fixture
def host(monkeypatch):
    instance = Bridge(SimpleNamespace())
    form, events = Form(), []
    instance._ensure_service = lambda: instance._service
    instance._lifecycle = SimpleNamespace(quit=lambda: events.append('lifecycle'))
    instance._window = SimpleNamespace(uid='synthetic-primary',
        # The old API silently ignored a missing form and could block Invoke.
        show=lambda: pytest.fail('Open must use the bounded owned dispatcher'),
        hide=lambda: setattr(form, 'Visible', False),
        destroy=lambda: events.append('window'))
    instance._tray = SimpleNamespace(stop=lambda: events.append('tray'))
    instances = {instance._window.uid: form}
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms',
        SimpleNamespace(BrowserView=SimpleNamespace(instances=instances)))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda callback: callback))
    monkeypatch.setitem(sys.modules, 'System.Windows.Forms', SimpleNamespace(
        FormWindowState=SimpleNamespace(Normal='Normal', Maximized='Maximized', Minimized='Minimized')))
    instance._send_visibility = lambda payload: None
    return instance, form, events, instances


def idle(instance):
    # Event barriers in each fixture define the completion boundary. This
    # small condition wait is solely for the worker's final coalescing cleanup.
    deadline = threading.Event()
    for _ in range(100):
        with instance._tray_actions_lock:
            if not instance._tray_actions:
                return
        deadline.wait(.01)
    pytest.fail('Synthetic tray workers did not finish')


@pytest.mark.parametrize('mode,maximized,expected',
    [('full', True, 'Maximized'), ('full', False, 'Normal'), ('hud', True, 'Normal')])
def test_open_restores_minimized_form_through_one_owned_dispatch(host, mode, maximized, expected):
    instance, form, _, _ = host
    form.WindowState = 'Minimized'
    instance._mode, instance._full_maximized = mode, maximized
    instance.show()
    assert form.Visible and form.WindowState == expected
    assert (form.shows, form.activations) == (1, 1)
    assert instance._visibility_status()['visible']


def test_actual_pywebview_close_event_keeps_false_veto_and_does_not_run_closed(host):
    instance, form, events, _ = host
    form.Visible = True
    closing, closed = Event(instance._window, True), Event(instance._window)
    closing += instance._on_closing
    closed += instance._on_window_closed
    assert closing.set() is True  # pywebview returns True for an explicit False veto
    assert not form.Visible and not instance._closing
    assert not closed.is_set() and events == []


@pytest.mark.parametrize('failure', ['hide', 'cancel'])
def test_native_close_exception_keeps_actual_pywebview_veto_and_honest_visibility(host, caplog, failure):
    instance, form, events, _ = host
    form.Visible = True
    def unavailable(): raise RuntimeError('Synthetic private nonce and path')
    if failure == 'hide':
        instance._window.hide = unavailable
    else:
        instance._update_locked = lambda: True
        instance._update_controller = SimpleNamespace(invalidate=unavailable)
    closing = Event(instance._window, True)
    closing += instance._on_closing
    assert closing.set() is True
    assert form.Visible and instance._visibility_status()['visible'] and not instance._closing
    assert events == []
    assert 'RuntimeError' in caplog.text and 'Synthetic private' not in caplog.text


def test_closed_event_stops_orphan_off_callback_and_removes_tray_before_slow_cleanup(host):
    instance, _, events, instances = host
    callback_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    cleanup_threads = []
    def cleanup():
        cleanup_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        events.append('lifecycle')
    instance._lifecycle.quit = cleanup
    instances.clear()
    try:
        instance._on_window_closed()
        assert entered.wait(1)
        assert instance._window_closed and not instance._trusted_loaded
        assert instance._closing and events == ['tray']
        assert cleanup_threads == [cleanup_threads[0]] and cleanup_threads[0] != callback_thread
    finally:
        release.set()
        idle(instance)
    assert events == ['tray', 'lifecycle'] and instance._quit_complete is True


def test_missing_primary_open_stops_orphan_instead_of_silent_success(host):
    instance, form, events, instances = host
    instances.clear()
    instance.show()
    idle(instance)
    assert instance._window_closed and instance._closing and instance._quit_complete
    assert form.shows == form.activations == 0
    assert events == ['tray', 'lifecycle']


def test_blocked_open_is_coalesced_and_does_not_block_tray_quit(host):
    instance, _, events, _ = host
    entered, release = threading.Event(), threading.Event()
    def blocked_open():
        entered.set()
        assert release.wait(2)
    instance.show = blocked_open
    try:
        assert instance._tray_action('open') is True
        assert entered.wait(1)
        assert all(instance._tray_action('open') is False for _ in range(20))
        assert instance._tray_action('quit') is True
        for _ in range(100):
            if instance._quit_complete is not None: break
            threading.Event().wait(.01)
        assert instance._quit_complete and events == ['tray', 'lifecycle', 'window']
        assert instance._tray_action('open') is False
    finally:
        release.set()
        idle(instance)


def test_slow_stop_does_not_block_quit_and_unknown_actions_spawn_nothing(host):
    instance, _, events, _ = host
    entered, release = threading.Event(), threading.Event()
    instance._emergency_stop = lambda: (entered.set(), release.wait(2))
    try:
        assert instance._tray_action('unknown') is False
        assert instance._tray_action('stop') is True and entered.wait(1)
        assert instance._tray_action('quit') is True
        for _ in range(100):
            if instance._quit_complete is not None: break
            threading.Event().wait(.01)
        assert instance._quit_complete and events.count('tray') == 1
    finally:
        release.set()
        idle(instance)


def test_concurrent_quit_has_one_cleanup_owner_and_closed_event_does_not_repeat(host):
    instance, _, events, _ = host
    entered, release = threading.Event(), threading.Event()
    def cleanup():
        events.append('lifecycle')
        entered.set()
        assert release.wait(2)
    instance._lifecycle.quit = cleanup
    results = []
    first = threading.Thread(target=lambda: results.append(instance.quit()))
    try:
        first.start()
        assert entered.wait(1)
        followers = [threading.Thread(target=lambda: results.append(instance.quit())) for _ in range(20)]
        for thread in followers: thread.start()
        for thread in followers: thread.join(1); assert not thread.is_alive()
        instance._on_window_closed()
        assert events == ['tray', 'lifecycle']
    finally:
        release.set()
        first.join(2)
        idle(instance)
    assert events == ['tray', 'lifecycle']
    assert results.count({'ok': True}) == 1
    assert instance.quit() == {'ok': True}
    assert events.count('window') == 0


def test_tray_quit_keeps_native_update_guard_but_closed_window_revokes_attempt(host):
    instance, _, events, instances = host
    cancelled = []
    instance._update_locked = lambda: True
    instance._update_controller = SimpleNamespace(invalidate=lambda: cancelled.append(True))
    assert instance._tray_action('quit') is True
    idle(instance)
    assert not instance._closing and events == [] and cancelled == [True]
    instances.clear()
    instance._on_window_closed()
    idle(instance)
    assert instance._closing and instance._quit_complete and cancelled == [True, True]
    assert events == ['tray', 'lifecycle']


def test_guarded_tray_quit_shows_fixed_safe_notice_and_requests_existing_window(host):
    instance, form, events, _ = host
    notices = []
    instance._tray.notify = lambda message, title: notices.append((message, title))
    instance._update_locked = lambda: True
    instance._update_controller = SimpleNamespace(invalidate=lambda: None)
    assert instance._tray_action('quit') is True
    idle(instance)
    assert notices == [('Cancel the pending restart in Forge before quitting.', 'Forge')]
    assert form.Visible and form.shows == 1 and not instance._closing and events == []


def test_main_completion_waits_for_owned_destroy_and_reports_timeout_honestly(host):
    instance, _, events, _ = host
    entered, release = threading.Event(), threading.Event()
    def destroy():
        entered.set()
        assert release.wait(2)
        events.append('window')
    instance._window.destroy = destroy
    owner = threading.Thread(target=instance.quit)
    try:
        owner.start()
        assert entered.wait(1)
        assert instance.quit()['ok'] is False
        assert instance._await_owned_cleanup(.01) is False
        assert not instance._quit_done.is_set()
    finally:
        release.set()
        owner.join(2)
    assert instance._await_owned_cleanup(.01) is True
    assert instance._quit_done.is_set() and events == ['tray', 'lifecycle', 'window']


def test_owned_cleanup_never_waits_on_itself_and_failed_cleanup_completion_is_false(host):
    instance, _, events, _ = host
    waits = []
    def destroy():
        waits.append(instance._await_owned_cleanup(.01))
        raise RuntimeError('Synthetic owned failure')
    instance._window.destroy = destroy
    assert instance.quit()['ok'] is False
    assert waits == [False] and instance._quit_done.is_set()
    assert instance._await_owned_cleanup(.01) is False
    assert events == ['tray', 'lifecycle']


def test_ended_gui_host_cancels_locked_update_and_finishes_without_waiting_for_live_confirmation(host):
    instance, _, events, _ = host
    cancelled = []
    instance._update_locked = lambda: True
    instance._update_controller = SimpleNamespace(invalidate=lambda: cancelled.append(True))
    assert instance.quit()['ok'] is False and not instance._closing
    assert instance._finish_native_host(1) is True
    idle(instance)
    assert instance._window_closed and instance._quit_complete
    assert cancelled == [True, True] and events == ['tray', 'lifecycle']


def test_main_finalization_waits_for_an_existing_destroy_owner_instead_of_duplicating_it(host):
    instance, _, events, _ = host
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def destroy():
        entered.set()
        assert release.wait(2)
        events.append('window')
    instance._window.destroy = destroy
    owner = threading.Thread(target=instance.quit)
    results = []
    finalizer = threading.Thread(target=lambda: (results.append(instance._finish_native_host(1)), finished.set()))
    try:
        owner.start()
        assert entered.wait(1)
        finalizer.start()
        assert finished.wait(.02) is False
    finally:
        release.set()
        owner.join(2)
        finalizer.join(2)
        idle(instance)
    assert results == [True] and events == ['tray', 'lifecycle', 'window']


def test_authorized_handoff_departure_keeps_its_admitted_binding_during_closed_callback(host, monkeypatch):
    instance, _, events, _ = host
    controller, transport = object(), object()
    instance._update_controller = controller
    instance._trusted_loaded = True
    instance._window._js_api = instance
    grant = SimpleNamespace(binding=SimpleNamespace(navigation=0, session=None, window_id=instance._window.uid))
    monkeypatch.setattr(instance._update_authority, 'pipe_admission',
        lambda received, helper: events.append('admitted') if (received is grant and helper is transport) else pytest.fail('Wrong grant'))
    monkeypatch.setattr(instance._update_authority, 'update_binding',
        lambda binding: pytest.fail('Committed departure must not revoke its admitted authority'))
    def destroy():
        events.append('window')
        instance._on_window_closed()
    instance._window.destroy = destroy
    assert instance._handoff_depart(controller, grant, transport) is True
    assert events == ['admitted', 'tray', 'lifecycle', 'window']
    assert instance._finish_native_host(.01) is True
    assert instance._quit_done.is_set() and instance._quit_complete


def test_failed_closed_worker_creation_stays_nonblocking_and_main_falls_back_without_native_calls(host, monkeypatch, caplog):
    instance, _, events, _ = host
    def failed_start(thread): raise RuntimeError('Synthetic private resource details')
    monkeypatch.setattr('desktop.threading.Thread.start', failed_start)
    assert instance._on_window_closed() is False
    assert instance._window_closed and not instance._closing and events == []
    assert instance._finish_native_host(.01) is True
    assert events == ['tray', 'lifecycle'] and instance._quit_done.is_set()
    assert 'RuntimeError' in caplog.text and 'Synthetic private' not in caplog.text


def test_late_queued_open_cannot_reshow_window_after_quit_claim(host):
    instance, form, events, _ = host
    queued, ready = [], threading.Event()
    form.InvokeRequired = True
    form.BeginInvoke = lambda callback: (queued.append(callback), ready.set())
    try:
        assert instance._tray_action('open') is True and ready.wait(1)
        assert instance.quit() == {'ok': True}
        queued.pop()()
    finally:
        idle(instance)
    assert form.shows == form.activations == 0 and not form.Visible
    assert events == ['tray', 'lifecycle', 'window']


def test_real_menu_items_dispatch_without_waiting_for_handlers(host, monkeypatch):
    import pystray
    instance, _, _, _ = host
    stopped = threading.Event()
    class Icon:
        def __init__(self, name, picture, title, menu): self.menu = menu
        def run(self, setup=None):
            setup(self)
            assert stopped.wait(2)
        def stop(self): stopped.set()
    monkeypatch.setattr(pystray, 'Icon', Icon)
    submitted = []
    instance._tray_action = submitted.append
    try:
        _tray(instance)
        assert instance._tray_ready.wait(1)
        icon = instance._tray
        for item in icon.menu.items: item(icon)
    finally:
        stopped.set()
        instance._tray_thread.join(2)
    assert submitted == ['open', 'stop', 'quit']


@pytest.mark.parametrize('visible', [True, False])
def test_startup_tray_is_only_registered_after_owned_form_exists_including_background(host, monkeypatch, visible):
    instance, form, events, instances = host
    instance._tray = None
    form.Visible = visible
    registered = []
    instance.restrict_navigation = lambda: registered.append('guard')
    def tray(owner):
        assert instances.get(owner._window.uid) is form
        assert not owner._closing and not owner._window_closed
        registered.append('tray')
        owner._tray = SimpleNamespace(stop=lambda: events.append('tray'))
    monkeypatch.setattr('desktop._tray', tray)
    assert instance._tray is None and not instance._tray_started
    instance._on_shown()
    instance._on_shown()
    assert registered == ['guard', 'tray'] and instance._tray_started
    assert form.Visible is visible
    assert instance._tray_action('open') is True
    idle(instance)
    assert form.Visible
    assert instance._tray_action('quit') is True
    idle(instance)
    assert instance._quit_complete and events == ['tray', 'lifecycle', 'window']


def test_late_shown_callback_after_startup_quit_cannot_leave_new_tray(host, monkeypatch):
    instance, _, events, _ = host
    instance._tray = None
    instance.restrict_navigation = lambda: pytest.fail('Late shown cannot dispatch to a stopped primary')
    monkeypatch.setattr('desktop._tray', lambda owner: pytest.fail('No tray after owned quit'))
    assert instance.quit() == {'ok': True}
    instance._on_shown()
    assert not instance._tray_started and instance._tray is None
    assert events == ['lifecycle', 'window']


def test_pystray_delayed_ready_cannot_show_an_icon_after_quit_was_admitted(host, monkeypatch):
    import pystray
    instance, _, events, _ = host
    setup_callbacks, ready_to_stop = [], threading.Event()
    pump_entered, stopped = threading.Event(), threading.Event()
    class Icon:
        visible = False
        running = False
        stops = 0
        def __init__(self, name, picture, title, menu): self.menu = menu
        def run(self, setup=None):
            setup_callbacks.append(setup)
            pump_entered.set()
            assert stopped.wait(2)
        def stop(self):
            if self.running:
                self.stops += 1
                ready_to_stop.set()
                stopped.set()
    monkeypatch.setattr(pystray, 'Icon', Icon)
    instance._ensure_service = lambda: pytest.fail('Tray registration uses its already prepared coordinator')
    _tray(instance)
    assert pump_entered.wait(1)
    icon = instance._tray
    assert not icon.running and not icon.visible and not instance._tray_ready.is_set()
    assert instance._tray_action('quit') is True
    try:
        for _ in range(100):
            if instance._closing: break
            threading.Event().wait(.01)
        assert instance._closing and instance._quit_complete is None
        assert not ready_to_stop.is_set() and events == []
        icon.running = True  # exact pystray _mark_ready boundary before setup
        setup_callbacks[0](icon)
        assert ready_to_stop.wait(1)
    finally:
        instance._tray_ready.set()
        instance._tray_startup_done.set()
        stopped.set()
        idle(instance)
    assert not icon.visible and icon.stops >= 1 and instance._quit_complete
    assert events == ['lifecycle', 'window']


@pytest.mark.parametrize('state', ['closing', 'closed'])
def test_delayed_loaded_visibility_and_shown_events_do_not_touch_ended_primary(host, state):
    instance, _, _, _ = host
    instance._closing = state == 'closing'
    instance._window_closed = state == 'closed'
    instance._native = lambda *args, **kwargs: pytest.fail('No dispatch to an ended primary')
    instance.restrict_navigation = lambda: pytest.fail('No late native navigation registration')
    instance._on_loaded()
    instance._on_shown()
    instance._on_visibility_event()
    assert not instance._trusted_loaded and not instance._tray_started


def test_loaded_callback_rechecks_quit_after_native_registration_before_reviving_trust(host):
    instance, _, _, _ = host
    instance.restrict_navigation = lambda: setattr(instance, '_closing', True)
    instance._on_loaded()
    assert not instance._trusted_loaded


def test_loaded_callback_cannot_revoke_admitted_departure_after_its_binding_read(host, monkeypatch):
    instance, _, _, _ = host
    instance.restrict_navigation = lambda: None
    instance._native_binding = lambda: setattr(instance, '_closing', True)
    monkeypatch.setattr(instance._update_authority, 'update_binding',
        lambda binding: pytest.fail('An admitted departure owns its terminal binding'))
    instance._on_loaded()
    assert instance._closing


def test_external_pywebview_callback_starts_explicit_nondaemon_pump_and_quit_waits_for_its_removal(host, monkeypatch):
    import _thread
    import pystray
    instance, _, events, _ = host
    entered, stop_requested, allow_removed, removed = (threading.Event() for _ in range(4))
    observed = []
    class Icon:
        def __init__(self, name, picture, title, menu): self.menu = menu
        def run(self, setup=None):
            observed.append(threading.current_thread().daemon)
            setup(self)
            entered.set()
            assert stop_requested.wait(2)
            assert allow_removed.wait(2)
            removed.set()
        def stop(self): stop_requested.set()
    monkeypatch.setattr(pystray, 'Icon', Icon)
    event = Event(instance._window)
    event += lambda: _tray(instance)
    _thread.start_new_thread(event.set, ())
    try:
        assert entered.wait(1)
        assert observed == [False]
        assert instance._tray_action('quit') is True
        assert stop_requested.wait(1)
        assert instance._quit_complete is None and events == []
        assert instance._await_owned_cleanup(.01) is False
    finally:
        stop_requested.set()
        allow_removed.set()
        idle(instance)
        if instance._tray_thread is not None:
            instance._tray_thread.join(2)
    assert removed.is_set() and instance._quit_complete
    assert events == ['lifecycle', 'window'] and not instance._tray_thread.is_alive()


def test_owned_pump_startup_failure_clears_only_failed_icon_and_preserves_visible_close_veto(host, monkeypatch, caplog):
    import pystray
    instance, form, events, _ = host
    form.Visible = True
    class Icon:
        def __init__(self, *args): pass
        def run(self, setup=None): raise RuntimeError('Synthetic private tray error')
    monkeypatch.setattr(pystray, 'Icon', Icon)
    _tray(instance)
    instance._tray_thread.join(1)
    assert instance._tray is None and instance._tray_startup_done.is_set()
    assert not instance._tray_ready.is_set()
    assert instance._on_closing() is False and form.Visible
    assert not instance._closing and events == []
    assert 'RuntimeError' in caplog.text and 'Synthetic private' not in caplog.text
    assert instance._tray_failure
    assert instance.quit()['ok'] is False
    assert instance._quit_done.is_set() and instance._await_owned_cleanup(.01) is False
    assert events == ['lifecycle', 'window']
