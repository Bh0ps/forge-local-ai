"""Coordinator/native admission uses inert pages and disposable state only."""
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import time
from types import MethodType, SimpleNamespace

import pytest

from browser_tools import BrowserTools
from desktop import Bridge
from forge_builder import BuilderManager
from forge_integrations import IntegrationHub
from forge_native_handoff import NativeAuthority, HandoffRejected
from forge_service import ForgeService, NATIVE_IO_ACTIONS, READ_ACTIONS
from forge_store import ForgeStore
from forge_updates import UpdateManager
from native_browser import NativeBrowser
from test_forge_update_handoff import flow, begin, prepare
from test_forge_publisher_updates import fixture as publisher


class BoundedRLock:
    """Bound a failed lock-cycle regression without changing production waits."""
    def __init__(self): self.lock = threading.RLock()
    def acquire(self, blocking=True, timeout=-1):
        if not blocking: return self.lock.acquire(False)
        return self.lock.acquire(timeout=min(timeout, 2) if timeout >= 0 else 2)
    def release(self): self.lock.release()
    def __enter__(self):
        if not self.acquire(): raise TimeoutError('Synthetic admission cycle')
        return self
    def __exit__(self, *args): self.release()
    def _is_owned(self): return self.lock._is_owned()


class InertPage:
    embedded = True
    def __init__(self, bridge, changed, events):
        self.bridge, self.changed, self.events = bridge, changed, events
        self.visible = False
        self.url = 'about:blank'
    def ui(self, category):
        return self.bridge._native(lambda form: self.events.append(category))
    def ready(self): self.ui('ready')
    def show(self): self.ui('show'); self.visible = True
    def bind(self, data): self.ui('bind'); self.visible = True
    def navigate(self, url):
        self.ui('navigate'); self.url = url
        self.changed('navigation', url); self.changed('loaded', None)
    def history(self, action): self.ui(action)
    def close(self): self.ui('hide'); self.visible = False
    def stop(self): self.ui('stop')
    def focused(self): return True
    def set_viewport(self, width, height): self.ui('viewport'); return {'width': width, 'height': height}
    def evaluate(self, script):
        self.ui('evaluate')
        return {'url': self.url, 'title': 'Synthetic preview', 'text': 'Synthetic fixture', 'targets': [], 'entries': []}
    def screenshot(self): self.ui('capture'); return b'Synthetic inert image bytes'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    service = object.__new__(ForgeService)
    service.store = ForgeStore(tmp_path / 'state')
    service.admission_lock = BoundedRLock()
    service.manager_lock = threading.RLock()
    service.stop_event = threading.Event()
    service.jobs = SimpleNamespace(lock=threading.RLock(), jobs={}, cancel=lambda *args, **kwargs: None)
    service.background = service.computer_broker = service.host_dictation = None
    service.performance_manager = service.model_manager = service.setup_manager = service.runtime = None
    service.channel_manager = service._command_sessions = None
    service._native_io_pending = 0
    manager = object.__new__(UpdateManager)
    manager.service, manager.store = service, service.store
    manager.inspection_required = manager.applying = manager.shutting_down = False
    manager.lock = threading.RLock()
    service.update_manager = manager
    bridge = Bridge(service)
    bridge._window = SimpleNamespace(uid='synthetic-native-main')
    form = SimpleNamespace(InvokeRequired=False)
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms',
        SimpleNamespace(BrowserView=SimpleNamespace(instances={bridge._window.uid: form})))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda value: value))
    events = []
    factory = lambda changed: InertPage(bridge, changed, events)
    research = NativeBrowser(service.store.home, view_factory=factory, session_check=lambda: True)
    browser = object.__new__(BrowserTools)
    browser.native, browser.bridge, browser.extension_snapshots = research, None, {}
    hub = object.__new__(IntegrationHub)
    hub.browser, hub.lock = browser, threading.RLock()
    service.integrations = hub
    service.builder_manager = BuilderManager(service)
    preview = NativeBrowser(service.store.home, view_factory=factory, session_check=lambda: True,
        panel_id='forge-preview-panel', diagnostics=True)
    service.builder_manager.previews.browser = preview
    folder = tmp_path / 'project'
    folder.mkdir(); (folder / 'index.html').write_text('Synthetic fixture')
    project = service.store.create_project('Synthetic project', str(folder))
    record = service.store.save_entity('previews', {'id': 'synthetic-preview', 'project_id': project['id'],
        'status': 'ready', 'url': 'http://127.0.0.1:12345', 'builder_id': None})
    service.builder_manager.previews.active[record['id']] = {'record': record}
    value = SimpleNamespace(service=service, manager=manager, bridge=bridge, research=research,
        preview=preview, events=events, project=project, record=record, folder=folder)
    yield value
    # Pages are inert objects, not real native resources. Do not invoke the
    # simulated host after a test intentionally closes its admission.
    for native in (research, preview):
        native.view = None
        native.shutdown()


def worker(operation):
    done = threading.Event(); outcome = {}
    def run():
        try: outcome['result'] = operation()
        except BaseException as exc: outcome['error'] = exc
        finally: done.set()
    thread = threading.Thread(target=run)
    thread.start()
    return thread, done, outcome


def finish(task):
    thread, done, outcome = task
    assert done.wait(3), 'Native request must not wait on its caller admission lock'
    thread.join(1)
    assert not thread.is_alive() and 'error' not in outcome
    return outcome['result']


def assert_native_drained(*browsers):
    # Future.result wakes waiters before its completion callbacks necessarily
    # finish. Readiness may remain conservatively busy for that brief interval.
    deadline = time.monotonic() + 2
    changed = threading.Event()
    while any(browser.pending_io() for browser in browsers) and time.monotonic() < deadline:
        changed.wait(.005)
    assert all(browser.pending_io() == 0 for browser in browsers)


@pytest.mark.parametrize('action', ['browser_native_open', 'browser_native_show', 'browser_native_resize',
                                   'preview_browser_show', 'preview_browser_resize'])
def test_real_dispatch_native_worker_reenters_bridge_admission_without_lock_cycle(workspace, action):
    value = workspace
    data = {'id': value.record['id'], 'rect': {'x': 0, 'y': 0, 'width': 760, 'height': 600}}
    result = finish(worker(lambda: value.service.dispatch(action, data)))
    assert result['ok'] is True and value.events
    assert value.service._native_io_pending == 0 and value.bridge._native_pending == 0
    assert_native_drained(value.research, value.preview)


def test_first_native_view_can_read_workspace_status_while_its_control_is_pending(workspace):
    value = workspace
    create = value.research.view_factory
    def factory(changed):
        assert value.service._native_io_pending == 1
        assert value.service.dispatch('browser_native_status')['available'] is True
        assert value.service.dispatch('preview_browser_status')['available'] is True
        return create(changed)
    value.research.view_factory = factory
    assert finish(worker(lambda: value.service.dispatch('browser_native_open')))['ok'] is True


def test_preview_owner_lock_wait_does_not_hold_service_gate(workspace):
    value = workspace
    entered = threading.Event()
    original = value.service.builder_manager.previews.browser_action
    def queued(*args, **kwargs): entered.set(); return original(*args, **kwargs)
    value.service.builder_manager.previews.browser_action = queued
    with value.service.builder_manager.previews.browser_lock:
        task = worker(lambda: value.service.dispatch('preview_browser_show', {'id': value.record['id']}))
        assert entered.wait(1)
        assert value.service._native_io_pending == 1
        with value.service.admission_lock:
            with pytest.raises(ValueError, match='native browser'): value.manager._idle()
        # The agent/native owner can still reenter the coordinator while a
        # frontend control waits for this actual preview ownership lock.
        assert value.bridge._native(lambda form: 'synthetic-owner') == 'synthetic-owner'
    assert finish(task)['ok'] is True


@pytest.mark.parametrize('action', ['preview_browser_inspect', 'preview_browser_capture', 'preview_browser_diagnostics'])
def test_read_native_io_keeps_read_scope_but_counts_until_callback_finishes(workspace, action):
    value = workspace
    assert action in READ_ACTIONS
    value.service.dispatch('preview_browser_show', {'id': value.record['id']})
    entered, release = threading.Event(), threading.Event()
    original = value.preview.view.ui
    def paused(category):
        entered.set()
        assert release.wait(3)
        return original(category)
    value.preview.view.ui = paused
    task = worker(lambda: value.service.dispatch(action, {'id': value.record['id']}))
    try:
        assert entered.wait(1) and value.service._native_io_pending == 1
        with value.service.admission_lock:
            with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    finally: release.set()
    assert finish(task)['ok'] is True
    assert value.service._native_io_pending == 0
    assert_native_drained(value.preview)


@pytest.mark.parametrize('state', ['applying', 'shutting_down', 'stopped'])
@pytest.mark.parametrize('action', ['browser_native_open', 'preview_browser_inspect', 'preview_stop', 'project_delete'])
def test_closed_admission_rejects_native_io_before_dispatch(workspace, state, action, monkeypatch):
    value = workspace
    if state == 'stopped': value.service.stop_event.set()
    else: setattr(value.manager, state, True)
    monkeypatch.setattr(value.service, '_dispatch', lambda *args: pytest.fail('No dispatch after admission closes'))
    with pytest.raises(ValueError, match='paused'): value.service.dispatch(action, {'id': value.record['id']})
    assert value.service._native_io_pending == 0 and value.research.pending_io() == value.preview.pending_io() == 0


def test_checking_bridge_authority_refusal_releases_service_and_future_ownership(workspace):
    value = workspace
    monkey_status = value.bridge._update_authority.status
    value.bridge._update_authority.status = lambda *args: {'locked': True, 'phase': 'checking'}
    try:
        result = value.service.dispatch('browser_native_open')
        assert result['ok'] is False and 'paused' in result['error']
        assert value.service._native_io_pending == value.bridge._native_pending == 0
        assert_native_drained(value.research)
        assert value.events == []
    finally: value.bridge._update_authority.status = monkey_status


def test_reentrant_native_dispatch_balances_nested_leases_and_supports_legacy_instances(workspace, monkeypatch):
    service = workspace.service
    del service._native_io_pending
    original = service._dispatch
    observed = []
    def dispatch(action, data):
        observed.append(service._native_io_pending)
        if action == 'browser_native_show':
            return service.dispatch('browser_native_resize')
        return original('browser_native_open', data)
    monkeypatch.setattr(service, '_dispatch', dispatch)
    assert finish(worker(lambda: service.dispatch('browser_native_show')))['ok'] is True
    assert observed == [1, 2] and service._native_io_pending == 0


@pytest.mark.parametrize('action', ['browser_native_unknown', 'preview_browser_unknown', 'preview_browser_open'])
def test_unknown_reserved_controls_cannot_start_or_touch_a_view(workspace, action, monkeypatch):
    value = workspace
    monkeypatch.setattr(value.service, '_dispatch', lambda *args: pytest.fail('Unsupported control has no dispatch effects'))
    with pytest.raises(ValueError, match='Unknown native browser'): value.service.dispatch(action, {'id': value.record['id']})
    assert value.service._native_io_pending == 0 and value.events == []


def test_ordinary_mutations_and_unknown_actions_retain_the_coordinator_gate(workspace):
    value = workspace
    calls = []
    value.service.core = SimpleNamespace(dispatch=lambda action, data:
        calls.append((action, value.service.admission_lock._is_owned())) or {'ok': True})
    assert value.service.dispatch('synthetic_unknown')['ok'] is True
    assert calls == [('synthetic_unknown', True)]
    value.manager.applying = True
    with pytest.raises(ValueError, match='preparing an update'): value.service.dispatch('synthetic_unknown')
    assert len(calls) == 1


def test_emergency_stop_remains_admitted_while_closing_and_counts_its_cleanup(workspace, monkeypatch):
    value = workspace
    value.manager.applying = value.manager.shutting_down = True
    value.service.stop_event.set()
    original = value.service.emergency_stop
    def stop():
        assert value.service._native_io_pending == 1
        assert not value.service.admission_lock._is_owned()
        with value.service.admission_lock:
            with pytest.raises(ValueError, match='native browser'): value.manager._idle()
        return original()
    monkeypatch.setattr(value.service, 'emergency_stop', stop)
    assert value.service.dispatch('emergency_stop')['ok'] is True
    assert value.research.cancelled.is_set() and value.preview.cancelled.is_set()
    assert value.service._native_io_pending == 0


def test_update_and_native_admission_have_one_atomic_boundary(workspace):
    value = workspace
    with value.manager._quiesce():
        value.manager._idle()
        value.manager.applying = True
        task = worker(lambda: value.service.dispatch('browser_native_open'))
    _, done, outcome = task
    assert done.wait(2)
    task[0].join(1)
    assert isinstance(outcome.get('error'), ValueError) and 'paused' in str(outcome['error'])
    assert not value.events and value.service._native_io_pending == 0


def test_queued_future_caller_timeout_does_not_retire_actual_native_work(workspace, monkeypatch):
    value = workspace
    entered, release = threading.Event(), threading.Event()
    blocker = value.research._submit(lambda: (entered.set(), release.wait(3)))
    assert entered.wait(1)
    futures = []
    submit = value.research._submit
    def immediate_timeout(operation, *args):
        future = submit(operation, *args); futures.append(future)
        def timeout(**kwargs): raise TimeoutError('Synthetic caller deadline')
        return SimpleNamespace(result=timeout)
    monkeypatch.setattr(value.research, '_submit', immediate_timeout)
    try:
        result = value.service.dispatch('browser_native_show')
        assert result['ok'] is False and value.service._native_io_pending == 0
        assert value.research.pending_io() == 2 and not value.research.view
        with value.service.admission_lock:
            with pytest.raises(ValueError, match='native browser'): value.manager._idle()
        assert futures[0].cancel()
        assert value.research.pending_io() == 1 and value.events == []
    finally: release.set()
    blocker.result(2)
    assert_native_drained(value.research)
    value.manager._idle()


def test_active_future_completion_error_and_submit_failure_release_exactly_once(workspace):
    value = workspace
    entered, release = threading.Event(), threading.Event()
    def delayed_error():
        entered.set(); assert release.wait(3)
        raise ValueError('Synthetic owned failure')
    future = value.research._submit(delayed_error)
    try:
        assert entered.wait(1)
        with pytest.raises(TimeoutError): future.result(timeout=.001)
        assert value.research.pending_io() == 1
        with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    finally: release.set()
    with pytest.raises(ValueError, match='Synthetic owned failure'): future.result(2)
    assert_native_drained(value.research)
    for _ in range(30): assert value.research._submit(lambda: True).result(1) is True
    assert_native_drained(value.research)
    value.research.view = None; value.research.shutdown()
    with pytest.raises(RuntimeError): value.research._submit(lambda: pytest.fail('No post-shutdown callback'))
    assert_native_drained(value.research)


def test_native_shutdown_cancels_queued_future_but_counts_active_until_it_finishes(workspace):
    value = workspace
    entered, release = threading.Event(), threading.Event()
    active = value.research._submit(lambda: (entered.set(), release.wait(3)))
    assert entered.wait(1)
    queued = value.research._submit(lambda: pytest.fail('Cancelled queued work cannot execute'))
    try:
        assert value.research.pending_io() == 2
        value.research.shutdown()
        assert queued.cancelled() and value.research.pending_io() == 1
        with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    finally: release.set()
    active.result(2)
    assert_native_drained(value.research)


@pytest.mark.parametrize('pending', [True, -1, None, 'unknown'])
def test_unknown_pending_ownership_cannot_be_treated_as_idle(workspace, pending):
    value = workspace
    value.research._pending_io = pending
    with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    value.research._pending_io = 0
    value.service._native_io_pending = pending
    with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    value.service._native_io_pending = 0


def test_preview_stop_drains_native_cleanup_outside_gate(workspace):
    value = workspace
    value.service.dispatch('preview_browser_show', {'id': value.record['id']})
    result = finish(worker(lambda: value.service.dispatch('preview_stop', {'id': value.record['id']})))
    assert result['status'] == 'stopped' and value.record['id'] not in value.service.builder_manager.previews.active
    assert value.service._native_io_pending == 0
    assert_native_drained(value.preview)


def test_project_delete_commits_revocation_before_native_cleanup_and_prevents_new_preview(workspace, monkeypatch):
    value = workspace
    saved = value.service.store.save_entity('builders', {'id': 'synthetic-builder', 'project_id': value.project['id'], 'status': 'ready'})
    value.service.dispatch('preview_browser_show', {'id': value.record['id']})
    entered, release = threading.Event(), threading.Event()
    close = value.service.builder_manager.close_project
    def paused(project_id):
        assert not value.service.admission_lock._is_owned()
        entered.set(); assert release.wait(3)
        return close(project_id)
    monkeypatch.setattr(value.service.builder_manager, 'close_project', paused)
    task = worker(lambda: value.service.dispatch('project_delete', {'id': value.project['id']}))
    try:
        assert entered.wait(1) and value.service._native_io_pending == 1
        with pytest.raises(ValueError): value.service.store.get_project(value.project['id'])
        with pytest.raises(ValueError): value.service.dispatch('preview_start', {'project_id': value.project['id'], 'mode': 'static'})
        assert len(value.service.builder_manager.previews.active) == 1
        with value.service.admission_lock:
            with pytest.raises(ValueError, match='native browser'): value.manager._idle()
    finally: release.set()
    assert finish(task)['ok'] is True
    assert value.folder.is_dir() and (value.folder / 'index.html').read_text() == 'Synthetic fixture'
    builder = value.service.store.entity('builders', saved['id'])
    assert builder['project_id'] is None and builder['detached_project_id'] == value.project['id']
    assert builder['project_missing'] and builder['status'] == 'project_missing'
    assert not value.service.builder_manager.previews.active
    assert value.service._native_io_pending == 0
    assert_native_drained(value.preview)


@pytest.mark.parametrize('blocker', ['worktree', 'unknown'])
def test_refused_project_delete_does_not_cleanup_previews_or_detach_registration(workspace, monkeypatch, blocker):
    value = workspace
    monkeypatch.setattr(value.service.builder_manager, 'close_project', lambda *args: pytest.fail('No cleanup before authoritative deletion'))
    if blocker == 'worktree':
        value.service.store.save_entity('worktrees', {'id': 'synthetic-worktree', 'parent_project_id': value.project['id'], 'status': 'active'})
    else:
        monkeypatch.setattr(value.service.store, 'runs', lambda: [{'id': 'synthetic-run', 'chat_id': 'synthetic-chat',
            'project_id': value.project['id'], 'status': 'completed'}])
        monkeypatch.setattr(value.service.store, 'unknown_actions', lambda identifier: [{'status': 'outcome_unknown'}])
    with pytest.raises(ValueError): value.service.dispatch('project_delete', {'id': value.project['id']})
    assert value.service.store.get_project(value.project['id'])['id'] == value.project['id']
    assert value.record['id'] in value.service.builder_manager.previews.active
    assert value.service._native_io_pending == 0


@pytest.mark.parametrize('phase', ['reserve', 'prepare'])
def test_real_native_handoff_refuses_pending_service_lease_before_backup_or_grant(flow, phase):
    value = flow
    service = value.manager.service
    service.stop_event = threading.Event()
    service._check_native_io_admission = MethodType(ForgeService._check_native_io_admission, service)
    if phase == 'prepare': begin(value)
    with ForgeService._native_io_lease(service, 'preview_browser_inspect'):
        with pytest.raises(HandoffRejected) as refused:
            if phase == 'reserve': value.manager.native_reserve(value.binding)
            else: prepare(value)
        assert refused.value.reason == 'active_work'
    assert service._native_io_pending == 0 and value.manager.applying is False
    assert not list((value.manager.root / 'operations').glob('*.json'))
    assert not list((value.manager.home / 'backups').glob('update-*'))
