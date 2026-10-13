"""Automatic downloads use ephemeral publishers, inert bytes and offline HTTP."""
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from forge_inference import InferenceQueue
from forge_service import ForgeService
from forge_runs import RunManager
from forge_store import ForgeStore
from forge_update_downloads import MAX_ATTEMPTS, candidate_identity, retry_delay
from forge_update_monitor import UpdateMonitor
from forge_updates import UpdateManager
from test_forge_publisher_updates import fixture, replace_record, PACKAGE


@pytest.fixture
def auto(fixture):
    value = fixture
    value.manager.service.admission_lock = threading.RLock()
    value.manager.service.update_manager = value.manager
    value.manager.store.update_settings({'update_auto_download': True})
    value.monitor = UpdateMonitor(value.manager)
    return value


def check(value):
    value.monitor.tick(); value.manager.thread.join(5)
    assert not value.manager.thread.is_alive()


def download(value, now=None):
    value.monitor.tick(now=now); value.manager.thread.join(5)
    assert not value.manager.thread.is_alive()


def package_requests(value):
    return [url for url in value.requests if url.endswith('-Setup.exe')]


def test_strict_default_and_atomic_private_preference_generation(tmp_path):
    store = ForgeStore(tmp_path)
    assert store.get_settings()['update_auto_download'] is False
    assert store.update_policy()['generation'] == 0
    store.update_settings({'model': 'synthetic', 'context': 65536})
    store.update_settings({'update_auto_download': False, 'update_channel': 'preview'})
    assert store.update_policy()['generation'] == 0
    store.update_settings({'update_auto_download': True, 'update_channel': 'stable'})
    assert store.update_policy()['generation'] == 1
    before = store.get_settings()
    for invalid in (1, 0, 'true', None, [], {}):
        with pytest.raises(ValueError): store.update_settings({'update_auto_download': invalid, 'model': 'must-not-save'})
        assert store.get_settings() == before and store.update_policy()['generation'] == 1
    restarted = ForgeStore(tmp_path)
    assert restarted.get_settings()['update_auto_download'] is True
    assert restarted.update_policy()['generation'] == 1
    assert 'generation' not in restarted.get_settings()


def test_automatic_download_starts_after_owned_check_and_never_installs(auto):
    check(auto)
    assert package_requests(auto) == []
    download(auto)
    result = auto.manager.status()
    assert result['state'] == 'ready' and result['verified'] is True
    assert result['automatic_download'] == {'phase': 'ready', 'reason': 'verified', 'attempt_count': 1}
    assert result.get('operation_id') is None and auto.manager.applying is False
    for _ in range(3): auto.monitor.tick()
    assert len(package_requests(auto)) == 1


def test_checker_must_really_exit_before_download_admission(auto, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = auto.manager.automatic_downloads.checked
    def hold(*args):
        original(*args); entered.set(); assert release.wait(5)
    monkeypatch.setattr(auto.manager.automatic_downloads, 'checked', hold)
    auto.monitor.tick()
    try:
        assert entered.wait(2)
        for _ in range(3): auto.monitor.tick()
        assert package_requests(auto) == []
    finally: release.set(); auto.manager.thread.join(5)
    download(auto)
    assert len(package_requests(auto)) == 1


@pytest.mark.parametrize('changes', [({'update_auto_download': False}, {'update_auto_download': True}),
    ({'update_channel': 'stable'}, {'update_channel': 'preview'}),
    ({'update_checks': False}, {'update_checks': True})])
def test_preference_aba_during_check_does_not_authorize_stale_download(auto, monkeypatch, changes):
    entered, release = threading.Event(), threading.Event()
    original = auto.manager._check_proof
    def hold(*args):
        entered.set(); assert release.wait(5); return original(*args)
    monkeypatch.setattr(auto.manager, '_check_proof', hold)
    auto.monitor.tick()
    try:
        assert entered.wait(2)
        for change in changes: auto.manager.store.update_settings(change)
    finally: release.set(); auto.manager.thread.join(5)
    assert auto.manager.automatic_downloads.candidate is None
    assert package_requests(auto) == []
    assert auto.manager.store.entities('update_trust') == []
    assert list(auto.manager.root.glob('proofs/*.json')) == []


@pytest.mark.parametrize('busy', ['worker', 'run', 'model', 'model-import', 'setup', 'performance', 'workflow', 'runtime', 'queue', 'native', 'inspection'])
def test_coordinator_and_actual_resources_defer_auto_download(auto, busy):
    check(auto); service = auto.manager.service
    if busy == 'worker': service.jobs.jobs = {'synthetic': {'thread': SimpleNamespace(is_alive=lambda: True)}}
    elif busy == 'run':
        chat = auto.manager.store.create_chat(title='Synthetic work')
        auto.manager.store.create_run({'chat_id': chat['id'], 'request': 'Synthetic task', 'settings': {}, 'mode': 'agent'})
    elif busy == 'model': service.model_manager = SimpleNamespace(active={'synthetic': {}}, import_reservations=set())
    elif busy == 'model-import': service.model_manager = SimpleNamespace(active={}, import_reservations={'synthetic'})
    elif busy == 'setup': service.setup_manager = SimpleNamespace(jobs={'synthetic': {}})
    elif busy == 'performance': service.performance_manager = SimpleNamespace(jobs={'synthetic': {}})
    elif busy == 'workflow': service.ai_workflow_manager = SimpleNamespace(jobs={'synthetic': {}})
    elif busy == 'runtime': service.runtime = SimpleNamespace(_validation={'state': 'running'})
    elif busy == 'queue':
        queue = InferenceQueue(); queue.pending.append((0, 1)); service.providers = SimpleNamespace(queue=queue)
    elif busy == 'native': auto.manager.native_handoff = SimpleNamespace(active=object())
    else: auto.manager.inspection_required = True
    auto.monitor.tick()
    assert package_requests(auto) == [] and auto.manager.automatic_downloads.active is None
    assert auto.manager.status()['automatic_download']['reason'] in ('active_work', 'inspection', 'native_handoff')
    # The fixture coordinator is not a real native authority.
    auto.manager.native_handoff = None


@pytest.mark.parametrize('change', ['disable', 'channel', 'worker', 'native'])
def test_final_rename_revalidates_revoked_preferences_and_new_work(auto, monkeypatch, change):
    import forge_updates
    check(auto); entered, release = threading.Event(), threading.Event()
    original = forge_updates.file_hash
    def hold(path):
        result = original(path)
        if path.name == 'installer.part': entered.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(forge_updates, 'file_hash', hold)
    auto.monitor.tick()
    try:
        assert entered.wait(2)
        if change == 'disable': auto.manager.store.update_settings({'update_auto_download': False})
        elif change == 'channel': auto.manager.store.update_settings({'update_channel': 'stable'})
        elif change == 'native': auto.manager.native_handoff = SimpleNamespace(active=object())
        else:
            service = auto.manager.service
            service._dispatch = lambda action, data: service.jobs.__dict__.update(jobs={'synthetic': {'thread': SimpleNamespace(is_alive=lambda: True)}}) or {'ok': True}
            assert ForgeService.dispatch(service, 'start_chat', {}) == {'ok': True}
    finally: release.set(); auto.manager.thread.join(5); auto.manager.native_handoff = None
    assert not auto.manager.status()['verified']
    assert list(auto.manager.root.glob('*/installer.exe')) == []
    assert auto.manager.applying is False


@pytest.mark.parametrize('stop', ['monitor', 'shutdown'])
def test_stop_during_download_reservation_is_bounded_and_prevents_network(auto, monkeypatch, stop):
    check(auto); entered, release = threading.Event(), threading.Event()
    original = auto.manager._automatic_save
    def hold(lease, **data):
        entered.set(); assert release.wait(5); return original(lease, **data)
    monkeypatch.setattr(auto.manager, '_automatic_save', hold)
    worker = threading.Thread(target=auto.monitor.tick); worker.start()
    try:
        assert entered.wait(2); start = time.monotonic()
        if stop == 'monitor': auto.monitor.stop()
        else: auto.manager.shutdown()
        assert time.monotonic() - start < .5
    finally: release.set(); worker.join(5)
    assert not worker.is_alive() and package_requests(auto) == [] and not auto.manager.status()['verified']


def test_user_cancel_is_durable_for_same_bytes_and_proof_renewal(auto):
    check(auto)
    auto.manager.cancel()
    assert auto.manager.automatic_downloads.history['suppressed'] is True
    auto.manager.store.update_settings({'update_auto_download': False})
    auto.manager.store.update_settings({'update_auto_download': True})
    auto.manager.shutdown()
    restarted = UpdateManager(auto.manager.service, '5.1.2', auto.app, (), auto.factory, proof_policy=auto.pins)
    auto.manager = restarted; auto.monitor = UpdateMonitor(restarted)
    payload = deepcopy(auto.payload); payload['revision'] = 2; payload['expires_at'] += 3600
    replace_record(auto, payload)
    check(auto); auto.monitor.tick()
    assert package_requests(auto) == []
    assert restarted.status()['automatic_download']['reason'] == 'user_cancelled'
    restarted.download(); restarted.thread.join(5)
    assert restarted.status()['verified'] and len(package_requests(auto)) == 1
    restarted.shutdown()


@pytest.mark.parametrize('failure', ['offline', '429', '503'])
def test_transient_download_failures_back_off_and_stop_after_four_attempts(auto, failure):
    check(auto)
    def response(request):
        auto.requests.append(str(request.url))
        if failure == 'offline': raise httpx.ConnectError('synthetic-private-key-do-not-leak', request=request)
        return httpx.Response(int(failure), text='synthetic-private-key-do-not-leak')
    auto.manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        download(auto, now=int(time.time()) + 3600 * attempt)
        assert auto.manager.status()['automatic_download']['attempt_count'] == attempt
        if attempt < MAX_ATTEMPTS:
            before = len(package_requests(auto)); auto.monitor.tick(now=int(time.time()))
            assert len(package_requests(auto)) == before
    before = len(package_requests(auto)); auto.monitor.tick(now=int(time.time()) + 10000)
    assert len(package_requests(auto)) == before
    result = auto.manager.status()
    assert result['automatic_download']['reason'] == 'retry_exhausted'
    assert 'synthetic-private-key' not in json.dumps(result)


@pytest.mark.parametrize('failure', ['401', '403', '404', 'hash'])
def test_nontransient_failure_does_not_retry_or_expose_remote_text(auto, failure):
    check(auto)
    def response(request):
        auto.requests.append(str(request.url))
        return httpx.Response(200, content=b'X' * len(PACKAGE)) if failure == 'hash' else httpx.Response(int(failure), text='synthetic-secret-never-show')
    auto.manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    download(auto)
    for _ in range(3): auto.monitor.tick()
    assert len(package_requests(auto)) == 1
    assert auto.manager.status()['automatic_download']['reason'] == 'verification_failed'
    assert 'synthetic-secret' not in json.dumps(auto.manager.status())


@pytest.mark.parametrize('header,expected', [('10', 60), ('600', 600), ('999999999', 3600), ('-5', 60), ('garbage', 60)])
def test_retry_after_is_bounded_and_malformed_values_are_ignored(header, expected):
    response = httpx.Response(429, headers={'retry-after': header}, request=httpx.Request('GET', 'https://github.com/'))
    error = httpx.HTTPStatusError('private synthetic error', request=response.request, response=response)
    assert retry_delay(error, 1, int(time.time())) == expected


@pytest.mark.parametrize('mutation', ['changed', 'missing'])
def test_fresh_check_rehashes_cached_bytes_before_reuse(auto, mutation):
    check(auto); download(auto)
    from pathlib import Path
    path = Path(auto.manager.state['downloaded'])
    if mutation == 'changed': path.write_bytes(b'X' * len(PACKAGE))
    else: path.unlink()
    auto.manager.check_background('preview', stop_event=auto.monitor.stop_event); auto.manager.thread.join(5)
    assert not auto.manager.status()['verified'] and auto.manager.status()['state'] == 'available'
    download(auto)
    assert auto.manager.status()['verified'] and len(package_requests(auto)) == 2


def test_same_verified_cache_reuse_requires_fresh_record_and_rechecks_bytes(auto):
    check(auto); download(auto)
    payload = deepcopy(auto.payload); payload['revision'] = 2; payload['expires_at'] += 3600
    replace_record(auto, payload)
    auto.manager.check_background('preview', stop_event=auto.monitor.stop_event); auto.manager.thread.join(5)
    auto.monitor.tick()
    assert auto.manager.status()['verified'] and len(package_requests(auto)) == 1
    assert auto.manager.state['release_proof']['sha256'] != auto.manager.state['signature']['proof_sha256']


def test_stopping_old_monitor_does_not_cancel_or_overwrite_manual_download(auto):
    check(auto); auto.manager.download(); auto.manager.thread.join(5)
    result = auto.manager.status(); auto.manager.cancel_event.clear()
    auto.monitor.stop()
    assert not auto.manager.cancel_event.is_set()
    assert auto.manager.status()['state'] == result['state'] == 'ready'
    assert auto.manager.automatic_downloads.history.get('suppressed') is not True


def test_background_worker_queued_before_auto_admission_takes_priority(auto):
    check(auto); jobs = auto.manager.service.jobs
    jobs.service = auto.manager.service; jobs.jobs = {}
    finish = threading.Event(); jobs._run = lambda identifier, job: finish.wait(5)
    attempted = threading.Event()
    original = jobs.lock
    class ObservedLock:
        def __enter__(self): attempted.set(); original.acquire(); return self
        def __exit__(self, *args): original.release()
    jobs.lock = ObservedLock()
    with original:
        worker = threading.Thread(target=auto.monitor.tick); worker.start()
        assert attempted.wait(2)
        # Channel/schedule workers use this actual publication path as well.
        RunManager._launch(jobs, {'id': 'synthetic-background'})
    try:
        worker.join(5)
        assert not worker.is_alive() and package_requests(auto) == []
        assert auto.manager.status()['automatic_download']['reason'] == 'active_work'
    finally:
        finish.set(); jobs.jobs['synthetic-background']['thread'].join(5)


@pytest.mark.parametrize('mode', ['automatic', 'manual'])
def test_actual_background_worker_launch_revokes_only_owned_auto_download(auto, monkeypatch, mode):
    import forge_updates
    check(auto); entered, release = threading.Event(), threading.Event()
    original = forge_updates.file_hash
    def hold(path):
        result = original(path)
        if path.name == 'installer.part': entered.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(forge_updates, 'file_hash', hold)
    if mode == 'automatic': auto.monitor.tick()
    else: auto.manager.download()
    jobs = auto.manager.service.jobs; jobs.service = auto.manager.service; jobs.jobs = {}
    finish = threading.Event(); jobs._run = lambda identifier, job: finish.wait(5)
    try:
        assert entered.wait(2)
        with jobs.lock: RunManager._launch(jobs, {'id': 'synthetic-background'})
        assert auto.manager.cancel_event.is_set() is (mode == 'automatic')
    finally:
        release.set(); auto.manager.thread.join(5); finish.set()
        if jobs.jobs: jobs.jobs['synthetic-background']['thread'].join(5)
    assert auto.manager.status()['verified'] is (mode == 'manual')


def test_stale_guard_cannot_cancel_new_manual_owner(auto):
    check(auto)
    # A finished automatic lease is stale, even if its guard wakes late.
    from forge_update_downloads import DownloadLease
    old = DownloadLease(auto.manager.automatic_downloads.candidate, threading.Event())
    auto.manager.download(); auto.manager.thread.join(5); auto.manager.cancel_event.clear()
    auto.manager.automatic_downloads.changed('stopped', old)
    assert not auto.manager.cancel_event.is_set() and auto.manager.status()['verified']


def test_expired_proof_before_promotion_never_arms_the_download(auto, monkeypatch):
    import forge_updates
    check(auto); original = forge_updates.file_hash
    def expired(path):
        result = original(path)
        if path.name == 'installer.part': monkeypatch.setattr(forge_updates.time, 'time', lambda: auto.payload['expires_at'] + 1)
        return result
    monkeypatch.setattr(forge_updates, 'file_hash', expired)
    download(auto)
    assert not auto.manager.status()['verified'] and list(auto.manager.root.glob('*/installer.exe')) == []
    assert auto.manager.status()['automatic_download']['reason'] == 'verification_failed'


def test_restarting_does_not_treat_cached_available_state_as_a_fresh_candidate(auto):
    check(auto); auto.manager.shutdown()
    restarted = UpdateManager(auto.manager.service, '5.1.2', auto.app, (), auto.factory, proof_policy=auto.pins)
    try:
        assert restarted.automatic_downloads.candidate is None
        restarted.automatic_downloads.tick(threading.Event())
        assert package_requests(auto) == [] and restarted.automatic_downloads.active is None
    finally: restarted.shutdown()


@pytest.mark.parametrize('mode', ['default-off', 'manual-check', 'unsigned', 'invalid-proof'])
def test_only_opted_in_monitor_authenticated_records_may_download(auto, mode):
    if mode == 'default-off': auto.manager.store.update_settings({'update_auto_download': False})
    elif mode == 'unsigned': auto.metadata['assets'].pop()
    elif mode == 'invalid-proof': auto.raw = b'Unauthenticated synthetic record'
    if mode == 'manual-check': auto.manager.check('preview')
    else: check(auto)
    auto.manager.automatic_downloads.tick(auto.monitor.stop_event)
    assert package_requests(auto) == [] and auto.manager.automatic_downloads.active is None


def test_a_failed_fresh_check_disarms_older_candidate_and_sanitizes_failure(auto):
    check(auto)
    auto.manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError('synthetic-secret-url-token', request=request))), trust_env=False)
    auto.manager.check_background('preview', stop_event=auto.monitor.stop_event); auto.manager.thread.join(5)
    assert auto.manager.automatic_downloads.candidate is None
    auto.manager.automatic_downloads.tick(auto.monitor.stop_event)
    assert package_requests(auto) == [] and not auto.manager.status()['verified']
    assert 'synthetic-secret' not in json.dumps(auto.manager.status())


def test_proof_is_rechecked_at_the_actual_final_ready_promotion(auto, monkeypatch):
    import forge_updates
    check(auto); original = auto.manager._automatic_save
    def expire(lease, **data):
        if data.get('promote') is not None:
            monkeypatch.setattr(forge_updates.time, 'time', lambda: auto.payload['expires_at'] + 1)
        return original(lease, **data)
    monkeypatch.setattr(auto.manager, '_automatic_save', expire)
    download(auto)
    assert not auto.manager.status()['verified'] and list(auto.manager.root.glob('*/installer.exe')) == []


def test_corrupt_cancel_history_fails_closed_and_explicit_manual_retry_recovers(auto):
    auto.manager.store.save_entity('update_auto', {'id': 'current', 'candidate': 'malformed'})
    restarted = UpdateManager(auto.manager.service, '5.1.2', auto.app, (), auto.factory, proof_policy=auto.pins)
    try:
        restarted.check_background('preview', stop_event=auto.monitor.stop_event); restarted.thread.join(5)
        restarted.automatic_downloads.tick(auto.monitor.stop_event)
        assert package_requests(auto) == []
        assert restarted.status()['automatic_download']['reason'] == 'policy_error'
        restarted.download(); restarted.thread.join(5)
        assert restarted.status()['verified']
    finally: restarted.shutdown()


@pytest.mark.parametrize('action', ['resume', 'hf_retry', 'runtime_start', 'performance_calibrate', 'setup_verify', 'pull'])
def test_heavy_work_service_admission_revokes_auto_without_delaying_work(auto, action):
    check(auto)
    from forge_update_downloads import DownloadLease
    lease = DownloadLease(auto.manager.automatic_downloads.candidate, auto.manager.cancel_event)
    auto.manager.automatic_downloads.active = lease; auto.manager.thread_stop_event = lease.candidate.owner
    auto.manager.service._dispatch = lambda received, data: {'accepted': received}
    try:
        assert ForgeService.dispatch(auto.manager.service, action, {}) == {'accepted': action}
        assert lease.cancel.is_set() and lease.reason == 'active_work'
    finally: auto.manager.automatic_downloads.active = None


def test_scheduled_work_admission_revokes_only_auto_lease(auto):
    check(auto)
    from forge_update_downloads import DownloadLease
    lease = DownloadLease(auto.manager.automatic_downloads.candidate, auto.manager.cancel_event)
    service = auto.manager.service
    service.schedule_lock = threading.RLock(); service._schedule_run = lambda schedule, key: {'accepted': True}
    auto.manager.automatic_downloads.active = lease
    try:
        assert ForgeService.schedule_run(service, {}) == {'accepted': True}
        assert lease.cancel.is_set() and lease.reason == 'active_work'
    finally: auto.manager.automatic_downloads.active = None


def test_user_cancel_between_tick_and_actual_download_admission_cannot_be_undone(auto, monkeypatch):
    check(auto); entered, release = threading.Event(), threading.Event()
    original = auto.manager._automatic_download
    def hold(*args): entered.set(); assert release.wait(5); return original(*args)
    monkeypatch.setattr(auto.manager, '_automatic_download', hold)
    worker = threading.Thread(target=auto.monitor.tick); worker.start()
    try:
        assert entered.wait(2); auto.manager.cancel()
        assert auto.manager.automatic_downloads.history['suppressed'] is True
    finally: release.set(); worker.join(5)
    assert not worker.is_alive() and package_requests(auto) == []
    assert auto.manager.automatic_downloads.history['suppressed'] is True
    assert auto.manager.status()['automatic_download']['reason'] == 'user_cancelled'


@pytest.mark.parametrize('boundary', ['proof', 'rename', 'sql', 'publication'])
def test_monitor_stop_during_final_admission_cannot_publish_ready(auto, monkeypatch, boundary):
    import forge_updates
    check(auto); entered, release = threading.Event(), threading.Event()
    proof = auto.manager._proof; replace = forge_updates.os.replace; save = auto.manager.store.save_entity
    in_promotion = False; original = auto.manager._automatic_save
    def promotion(lease, **data):
        nonlocal in_promotion
        in_promotion = data.get('promote') is not None
        return original(lease, **data)
    monkeypatch.setattr(auto.manager, '_automatic_save', promotion)
    def held_proof(*args, **kwargs):
        result = proof(*args, **kwargs)
        if boundary == 'proof' and in_promotion: entered.set(); assert release.wait(5)
        return result
    def held_replace(source, target):
        if boundary == 'rename' and source.name == 'installer.part': entered.set(); assert release.wait(5)
        return replace(source, target)
    def held_save(kind, data, **kwargs):
        result = save(kind, data, **kwargs)
        if boundary == 'sql' and kind == 'updates' and data.get('state') == 'ready': entered.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(auto.manager, '_proof', held_proof)
    monkeypatch.setattr(forge_updates.os, 'replace', held_replace)
    monkeypatch.setattr(auto.manager.store, 'save_entity', held_save)
    validate = auto.manager.automatic_downloads.validate
    def held_validate(lease, **kwargs):
        result = validate(lease, **kwargs)
        if boundary == 'publication' and in_promotion and kwargs.get('resources') is False and 'connection' not in kwargs:
            entered.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(auto.manager.automatic_downloads, 'validate', held_validate)
    auto.monitor.tick()
    try:
        assert entered.wait(2); start = time.monotonic(); auto.monitor.stop()
        assert time.monotonic() - start < .5
    finally: release.set(); auto.manager.thread.join(5)
    assert not auto.manager.thread.is_alive() and not auto.manager.status()['verified']
    assert not auto.manager.store.entity('updates', 'current').get('verified')
    assert list(auto.manager.root.glob('*/installer.exe')) == []
    assert auto.manager.status()['automatic_download']['phase'] == 'cancelled'


def test_delayed_second_tick_cannot_download_completed_candidate_again(auto, monkeypatch):
    check(auto); entered, release = threading.Event(), threading.Event(); count = 0
    original = auto.manager._automatic_download
    def hold(*args):
        nonlocal count
        count += 1
        if count == 1: entered.set(); assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(auto.manager, '_automatic_download', hold)
    worker = threading.Thread(target=auto.monitor.tick); worker.start()
    try:
        assert entered.wait(2); download(auto)
        assert auto.manager.status()['verified']
    finally: release.set(); worker.join(5)
    assert not worker.is_alive() and len(package_requests(auto)) == 1
    assert auto.manager.status()['automatic_download']['attempt_count'] == 1


def test_opt_in_change_during_old_check_is_not_consumed_by_the_cooldown(auto, monkeypatch):
    auto.manager.store.update_settings({'update_auto_download': False})
    entered, release = threading.Event(), threading.Event(); original = auto.manager._check_proof
    def hold(*args): entered.set(); assert release.wait(5); return original(*args)
    monkeypatch.setattr(auto.manager, '_check_proof', hold)
    auto.monitor.tick()
    try:
        assert entered.wait(2)
        auto.manager.store.update_settings({'update_auto_download': True})
        auto.monitor.tick()  # The old checker is still active.
    finally: release.set(); auto.manager.thread.join(5)
    monkeypatch.setattr(auto.manager, '_check_proof', original)
    before = len(auto.requests); check(auto)
    assert len(auto.requests) > before and auto.manager.automatic_downloads.candidate is not None
    download(auto); assert auto.manager.status()['verified']


def test_corrupt_policy_does_not_block_unrelated_settings_or_promise_false_repair(tmp_path):
    store = ForgeStore(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute('INSERT INTO forge_settings VALUES(?,?)', ('update_auto_download', '1'))
    assert store.update_settings({'model': 'synthetic', 'context': 65536})['model'] == 'synthetic'
    with pytest.raises(ValueError, match='Inspect update recovery'):
        store.update_settings({'update_auto_download': True})


def test_manual_retry_clears_only_owned_automatic_stop_metadata_and_preserves_primary_state(auto):
    check(auto); auto.manager.cancel()
    assert auto.manager.status()['automatic_download']['reason'] == 'user_cancelled'
    auto.manager.download(); auto.manager.thread.join(5)
    result = auto.manager.status()
    assert result['state'] == 'ready' and result['verified'] is True
    assert result['automatic_download'] == {'phase': 'waiting', 'reason': 'fresh_check_required', 'attempt_count': 0}
    assert auto.manager.automatic_downloads.history['suppressed'] is False


def test_manual_only_download_does_not_invent_automatic_history_or_pending_metadata(auto):
    auto.manager.store.update_settings({'update_auto_download': False})
    check(auto)
    assert auto.manager.automatic_downloads.history == {}
    auto.manager.download(); auto.manager.thread.join(5)
    result = auto.manager.status()
    assert result['state'] == 'ready' and result['verified'] is True
    assert result['automatic_download'] == {'phase': 'off', 'reason': 'disabled', 'attempt_count': 0}
    assert auto.manager.automatic_downloads.history == {}
