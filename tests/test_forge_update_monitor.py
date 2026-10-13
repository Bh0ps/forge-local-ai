"""Automatic native release detection respects preferences and host lifecycle."""
import threading
import time
from types import SimpleNamespace

import pytest

import forge_update_monitor
from forge_update_monitor import UpdateMonitor, start_native_update_monitor


class Manager:
    def __init__(self):
        self.settings = {}
        self.store = SimpleNamespace(get_settings=lambda: self.settings)
        self.value = {'state': 'idle', 'channel': 'stable'}
        self.applying = False
        self.calls = []
        self.cancelled = 0
        self.thread = None
        self.thread_stop_event = None
        self.monitor = None
        self.cancel_event = threading.Event()
        self.admission_lock = threading.Lock()
    def status(self): return dict(self.value)
    def check_background(self, channel, *, stop_event=None):
        with self.admission_lock:
            if stop_event is not None and stop_event.is_set(): raise ValueError('Stopping')
            self.calls.append(channel)
            self.thread = SimpleNamespace(is_alive=lambda: True)
            self.thread_stop_event = stop_event
            self.value.update(state='checking', channel=channel)
    def cancel(self): self.cancelled += 1
    def start_monitor(self): self.monitor = object()
    def stop_monitor(self, event):
        with self.admission_lock:
            event.set()
            if self.thread_stop_event is event and self.thread and self.thread.is_alive(): self.cancel_event.set()
    def cancel_monitor_check(self, event):
        with self.admission_lock:
            if self.thread_stop_event is event and self.thread and self.thread.is_alive(): self.cancelled += 1


def test_default_background_check_uses_preview_and_respects_six_hour_cooldown():
    manager = Manager(); monitor = UpdateMonitor(manager)
    monitor.tick(now=1_000_000)
    assert manager.calls == ['preview']
    manager.value.update(state='current', checked_at=1_000_000)
    monitor.tick(now=1_000_001)
    assert manager.calls == ['preview']
    monitor.tick(now=1_000_000 + 6 * 60 * 60)
    assert manager.calls == ['preview', 'preview']


def test_explicit_channel_change_checks_the_new_channel_without_waiting():
    manager = Manager(); monitor = UpdateMonitor(manager)
    manager.value.update(state='current', channel='preview', checked_at=1_000_000)
    manager.settings['update_channel'] = 'stable'
    monitor.tick(now=1_000_001)
    assert manager.calls == ['stable']


def test_offline_check_retries_after_five_minutes_but_cancel_and_download_failure_do_not():
    manager = Manager(); monitor = UpdateMonitor(manager)
    manager.value.update(state='error', channel='preview', checked_at=1_000_000, last_check_failed=True)
    monitor.tick(now=1_000_299)
    assert manager.calls == []
    monitor.tick(now=1_000_300)
    assert manager.calls == ['preview']
    manager.value.update(state='cancelled', checked_at=1_000_300, last_check_failed=False)
    monitor.tick(now=1_000_900)
    assert manager.calls == ['preview']
    manager.value.update(state='error', last_check_failed=False)
    monitor.tick(now=1_000_901)
    assert manager.calls == ['preview']


@pytest.mark.parametrize('condition', ['checking', 'downloading', 'prepared', 'applying', 'inspection', 'off'])
def test_background_detection_does_not_conflict_with_work_or_disabled_checks(condition):
    manager = Manager(); monitor = UpdateMonitor(manager)
    if condition == 'applying': manager.applying = True
    elif condition == 'inspection': manager.value['inspection_required'] = True
    elif condition == 'off': manager.settings['update_checks'] = False
    else: manager.value['state'] = condition
    monitor.tick(now=1_000_000)
    assert manager.calls == [] and manager.cancelled == 0


def test_disabling_checks_cancels_only_the_monitors_own_check():
    manager = Manager(); monitor = UpdateMonitor(manager)
    monitor.tick(now=1_000_000)
    manager.settings['update_checks'] = False
    monitor.tick(now=1_000_001)
    assert manager.cancelled == 1
    manager.thread = SimpleNamespace(is_alive=lambda: True)
    manager.thread_stop_event = None
    manager.value['state'] = 'downloading'
    monitor.tick(now=1_000_002)
    assert manager.cancelled == 1


def test_backward_clock_changes_cannot_suppress_checks_indefinitely():
    manager = Manager(); monitor = UpdateMonitor(manager)
    manager.value.update(state='current', channel='preview', checked_at=2_000_000)
    monitor.tick(now=1_000_000)
    assert manager.calls == ['preview']


def test_shutdown_before_startup_delay_does_not_contact_github():
    manager = Manager(); monitor = UpdateMonitor(manager, startup_delay=30)
    monitor.start(); monitor.stop()
    assert not monitor.thread.is_alive() and manager.calls == []
    monitor.tick(now=1_000_000)
    assert manager.calls == []


@pytest.mark.parametrize('mode', ['source', 'disposable', 'legacy-disposable', 'smoke', 'browser-smoke', 'dictation-smoke'])
def test_native_boot_excludes_source_and_disposable_hosts(monkeypatch, mode):
    monkeypatch.setattr(forge_update_monitor.sys, 'frozen', mode != 'source', raising=False)
    monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe'])
    monkeypatch.delenv('FORGE_DATA_DIR', raising=False)
    monkeypatch.delenv('SIDEKICK_DATA_DIR', raising=False)
    if mode == 'disposable': monkeypatch.setenv('FORGE_DATA_DIR', 'disposable-fixture')
    if mode == 'legacy-disposable': monkeypatch.setenv('SIDEKICK_DATA_DIR', 'disposable-fixture')
    if mode == 'smoke': monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe', '--smoke-test'])
    if mode == 'browser-smoke': monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe', '--native-browser-smoke'])
    if mode == 'dictation-smoke': monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe', '--dictation-smoke'])
    service = SimpleNamespace(manager_lock=threading.RLock(), update_manager=None)
    monkeypatch.setattr(forge_update_monitor, 'UpdateManager', lambda value: pytest.fail('must not initialize updater'))
    assert start_native_update_monitor(service) is None and service.update_manager is None


def test_native_boot_reuses_the_coordinator_owned_manager(monkeypatch):
    monkeypatch.setattr(forge_update_monitor.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe', '--background'])
    monkeypatch.delenv('FORGE_DATA_DIR', raising=False)
    monkeypatch.delenv('SIDEKICK_DATA_DIR', raising=False)
    manager = Manager()
    service = SimpleNamespace(manager_lock=threading.RLock(), update_manager=manager)
    monkeypatch.setattr(forge_update_monitor, 'UpdateManager', lambda value: pytest.fail('must reuse manager'))
    assert start_native_update_monitor(service) is manager.monitor


def test_native_boot_claims_a_single_manager_under_the_service_lock(monkeypatch):
    monkeypatch.setattr(forge_update_monitor.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(forge_update_monitor.sys, 'argv', ['Forge.exe'])
    monkeypatch.delenv('FORGE_DATA_DIR', raising=False)
    monkeypatch.delenv('SIDEKICK_DATA_DIR', raising=False)
    service = SimpleNamespace(manager_lock=threading.RLock(), update_manager=None)
    calls = []
    def create(value):
        assert value.manager_lock._is_owned()
        calls.append(value)
        return Manager()
    monkeypatch.setattr(forge_update_monitor, 'UpdateManager', create)
    start_native_update_monitor(service); start_native_update_monitor(service)
    assert calls == [service]


@pytest.mark.parametrize('read', ['settings', 'status'])
def test_stop_during_slow_read_prevents_later_check_admission(read):
    manager = Manager(); monitor = UpdateMonitor(manager)
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set(); assert release.wait(5)
        return {} if read == 'settings' else dict(manager.value)
    if read == 'settings': manager.store.get_settings = blocked
    else: manager.status = blocked
    worker = threading.Thread(target=lambda: monitor.tick(now=1_000_000))
    worker.start()
    try:
        assert entered.wait(2)
        before = time.monotonic(); monitor.stop()
        assert time.monotonic() - before < .5
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive() and manager.calls == []


@pytest.mark.parametrize('permanent', [False, True])
def test_real_updater_shutdown_during_slow_reservation_never_starts_network(tmp_path, monkeypatch, permanent):
    import httpx
    from forge_store import ForgeStore
    from forge_updates import UpdateManager
    store = ForgeStore(tmp_path / 'data')
    service = SimpleNamespace(store=store, jobs=SimpleNamespace(lock=threading.RLock()))
    contacted = []
    def factory():
        contacted.append(True)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    manager = UpdateManager(service, current_version='5.1.0', client_factory=factory)
    monitor = UpdateMonitor(manager)
    entered, release = threading.Event(), threading.Event()
    original = manager._save
    def save(**data):
        if data.get('state') == 'checking':
            entered.set(); assert release.wait(5)
        return original(**data)
    monkeypatch.setattr(manager, '_save', save)
    worker = threading.Thread(target=lambda: monitor.tick(now=1_000_000)); worker.start()
    try:
        assert entered.wait(2)
        before = time.monotonic()
        if permanent: manager.shutdown()
        else: monitor.stop()
        assert time.monotonic() - before < .5
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive() and contacted == [] and manager.thread is None
    assert manager.status()['state'] == 'cancelled' and not manager.status()['verified']
    monkeypatch.setattr(manager, '_save', original)
    if permanent:
        with pytest.raises(ValueError, match='stopping|shutting down'): manager.check_background('preview')
        with pytest.raises(ValueError, match='shutting down'): manager.download()
    else:
        # Stopping automatic checks must leave the explicit user control usable.
        manager.check_background('preview'); manager.thread.join(2)
        assert contacted == [True] and manager.status()['state'] == 'current'
        manager.shutdown()


def test_real_monitor_cancellation_does_not_cancel_a_newer_manual_operation(tmp_path):
    import httpx
    from forge_store import ForgeStore
    from forge_updates import UpdateManager
    store = ForgeStore(tmp_path / 'data')
    service = SimpleNamespace(store=store, jobs=SimpleNamespace(lock=threading.RLock()))
    manager = UpdateManager(service, current_version='5.1.0', client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))))
    monitor = UpdateMonitor(manager)
    manager.check_background('preview'); manager.thread.join(2)
    assert manager.thread_stop_event is None
    manager.thread = SimpleNamespace(is_alive=lambda: True)
    manager.cancel_event.clear()
    monitor.stop()
    assert not manager.cancel_event.is_set() and not manager.shutting_down
