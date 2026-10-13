"""Native restart admission with ephemeral proofs and inert installer bytes."""
from copy import copy, deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import io
import os
from pathlib import Path
import threading
import subprocess
from types import SimpleNamespace

import pytest

from forge_drafts import DraftManager
from forge_native_handoff import NativeAuthority, NativeBinding, HandoffRejected
import forge_update_handoff as handoff
import forge_update_proof as proof
from forge_updates import UpdateManager, load_operation, atomic_text, encode, helper_main
from test_forge_publisher_updates import fixture as publisher, sign, ready, installer, OLD_SOURCE, NEW_SOURCE


@pytest.fixture
def flow(publisher, monkeypatch):
    value = publisher
    value.manager.shutdown()
    for root, number, source in ((value.app, '5.1.3', OLD_SOURCE), (value.new_app, '5.1.4', NEW_SOURCE)):
        payload = dict(schema=1, kind='forge-build', repository=proof.REPOSITORY, platform=proof.PLATFORM,
            version=number, source_commit=source, created_at=value.now,
            files=[entry for entry in proof.tree_files(root) if entry['path'] != proof.BUILD_NAME])
        (root / proof.BUILD_NAME).write_bytes(sign(payload, value.private))
    value.payload.update(version='5.1.4', tag='v5.1.4',
        build_proof_sha256=sha256((value.new_app / proof.BUILD_NAME).read_bytes()).hexdigest())
    for artifact in value.payload['artifacts']:
        artifact['name'] = artifact['name'].replace('5.1.3', '5.1.4')
    value.raw = sign(value.payload, value.private)
    value.metadata['tag_name'] = 'v5.1.4'
    value.metadata['html_url'] = value.metadata['html_url'].replace('5.1.3', '5.1.4')
    for artifact in value.metadata['assets']:
        artifact['name'] = artifact['name'].replace('5.1.3', '5.1.4')
        artifact['browser_download_url'] = artifact['browser_download_url'].replace('5.1.3', '5.1.4')
    value.metadata['assets'][1].update(size=len(value.raw), digest='sha256:' + sha256(value.raw).hexdigest())
    service = value.manager.service
    service.admission_lock = threading.RLock()
    service.update_editor_guard = lambda: True
    service.update_native_input_guard = lambda: True
    value.manager = UpdateManager(service, '5.1.3', value.app, (), value.factory, proof_policy=value.pins)
    service.update_manager = value.manager
    value.authority = NativeAuthority()
    value.binding = NativeBinding(object(), 'a' * 64, 'synthetic-main', 1,
        'http://127.0.0.1:12345', os.getpid(), 123.0)
    value.authority.update_binding(value.binding)
    value.manager.bind_native_authority(value.authority)
    value.drafts = DraftManager(service.store, confirm_callback=value.manager.confirm_draft)
    value.scope = {'scope_kind': 'new_chat', 'project_id': None}
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', True)
    ready(value)
    yield value
    value.manager.shutdown()


def begin(value):
    value.reservation = value.manager.native_reserve(value.binding)
    value.begin = value.authority.begin(value.binding, value.reservation)
    return value.begin


def confirmation(value, text='Synthetic private draft'):
    saved = value.drafts.save({**value.scope, 'expected_revision': 0, 'content': {'text': text}})
    result = value.drafts.confirm({**value.scope, 'expected_revision': saved['revision'],
        'attempt_id': value.begin['attempt_id']})
    assert set(result) == {'ok', 'receipt'} and result['ok'] is True
    assert len(result['receipt']) == 64
    return result['receipt']


def prepare(value, receipts=None):
    if not hasattr(value, 'begin'): begin(value)
    value.ack = {**value.begin, 'workspace_revision': 7, 'participant_count': 2,
        'receipts': [] if receipts is None else receipts}
    value.ack.pop('phase'); value.ack.pop('expires_in')
    value.prepare_grant = value.authority.acknowledge(value.ack)
    result = value.manager.native_prepare(value.prepare_grant, value.ack)
    value.prepared = result
    value.second = value.authority.prepared(value.prepare_grant)
    return result


def launch(value):
    value.launch_grant = value.authority.confirm({'attempt_id': value.second['attempt_id'],
        'confirmation': value.second['confirmation'], 'workspace_revision': 7, 'participant_count': 2})
    value.bootstrap = value.manager.native_launch(value.launch_grant, value.prepared)
    return value.bootstrap


def commit(value, ready=None):
    ready = object() if ready is None else ready
    value.authority.ready(value.launch_grant, ready)
    value.commit_grant = value.authority.commit_grant(value.begin['attempt_id'], ready,
        {'ok': True, 'phase': 'prepared', 'workspace_revision': 7, 'participant_count': 2})
    assert value.manager.native_commit(value.commit_grant, value.prepared, ready) is True
    return ready


def parent(monkeypatch, value, *, exists=True, changed=False, wrong_exe=False, unrelated=False):
    # No actual process launch/termination. Both early PPID and final executable
    # checks use these synthetic process identities.
    process = SimpleNamespace(create_time=lambda: 124.0 if changed else 123.0,
        exe=lambda: str(value.app / ('other.exe' if wrong_exe else 'Forge.exe')),
        ppid=lambda: os.getpid() + 1 if unrelated else os.getpid())
    monkeypatch.setattr('forge_update_handoff.psutil.Process', lambda *args: process)
    monkeypatch.setattr('forge_update_handoff.psutil.pid_exists', lambda pid: exists)


def helper(value, monkeypatch):
    parent(monkeypatch, value)
    return handoff.helper_preflight(handoff.native_bootstrap_frame(value.bootstrap),
        executable=value.bootstrap.command[0], proof_policy=value.pins, install_dir=value.app)


def test_private_receipts_saved_text_and_explicit_handoff_install(flow, monkeypatch):
    value = flow; begin(value)
    receipt = confirmation(value)
    prepare(value, [receipt]); bootstrap = launch(value)
    assert bootstrap.command == (str(Path(load_operation(value.manager.home, value.prepared.operation_id)['backup']) / 'installation/Forge.exe'), '--forge-update')
    assert all(secret not in repr(bootstrap) for secret in (receipt, bootstrap.nonce, str(bootstrap.home)))
    assert value.drafts.get(value.scope)['content']['text'] == 'Synthetic private draft'
    context = helper(value, monkeypatch)
    assert load_operation(value.manager.home, value.prepared.operation_id)['state'] == 'prepared'
    commit(value)
    parent(monkeypatch, value, exists=False)
    result = handoff.helper_commit(context, runner=installer(value), wait_seconds=0)
    assert result['installed'] and result['version'] == '5.1.4'
    with pytest.raises(HandoffRejected): handoff.helper_commit(context, runner=lambda args: pytest.fail('No replay'))


def test_no_native_default_or_public_boolean_can_open_installation(flow, monkeypatch):
    manager = flow.manager
    for action in ('update_apply', 'update_rollback'):
        with pytest.raises(ValueError, match='unavailable'):
            manager.dispatch(action, {'native_ready': True, 'receipt': 'b' * 64})
    assert manager.status()['safe_handoff_available'] is False
    assert manager.status()['automatic_install_available'] is False
    assert manager.native_status(flow.binding)['safe_handoff_available'] is True
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', False)
    with pytest.raises(HandoffRejected): manager.native_reserve(flow.binding)
    assert manager.native_status(flow.binding)['safe_handoff_available'] is False
    assert not list((manager.home / 'backups').glob('update-*'))


@pytest.mark.parametrize('guard', [None, lambda: False, lambda: 'true', lambda: 1])
def test_missing_unknown_or_paired_editor_blocks_reservation_before_backup(flow, guard):
    flow.manager.service.update_editor_guard = guard
    with pytest.raises(HandoffRejected): begin(flow)
    assert not list((flow.manager.home / 'backups').glob('update-*'))


def test_current_seed_helper_cannot_speak_protocol_even_with_enabled_authority(flow):
    flow.manager.current_version = '5.1.2'
    with pytest.raises(HandoffRejected): begin(flow)


@pytest.mark.parametrize('bad', [True, -1, '1', None])
def test_draft_confirmation_requires_exact_saved_revision(flow, bad):
    begin(flow)
    with pytest.raises(HandoffRejected):
        flow.drafts.confirm({**flow.scope, 'expected_revision': bad, 'attempt_id': flow.begin['attempt_id']})


def test_missing_scope_reference_or_disabled_skill_cannot_receive_receipt(flow):
    value = flow; begin(value)
    value.drafts.save({**value.scope, 'expected_revision': 0, 'content': {'document_ids': ['gone']}})
    with pytest.raises(HandoffRejected):
        value.drafts.confirm({**value.scope, 'expected_revision': 1, 'attempt_id': value.begin['attempt_id']})
    value.drafts.save({**value.scope, 'expected_revision': 1, 'content': {'skills': ['disabled']}})
    with pytest.raises(HandoffRejected):
        value.drafts.confirm({**value.scope, 'expected_revision': 2, 'attempt_id': value.begin['attempt_id']})


@pytest.mark.parametrize('boundary', ['prepare', 'launch', 'commit'])
def test_durable_revision_change_invalidates_receipt_at_every_fence(flow, boundary):
    value = flow; begin(value); receipt = confirmation(value)
    if boundary != 'prepare': prepare(value, [receipt])
    if boundary == 'commit': launch(value)
    value.drafts.save({**value.scope, 'expected_revision': 1, 'content': {'text': 'Later saved work'}})
    with pytest.raises(HandoffRejected):
        if boundary == 'prepare': prepare(value, [receipt])
        elif boundary == 'launch': launch(value)
        else: commit(value)
    assert value.drafts.get(value.scope)['content']['text'] == 'Later saved work'
    assert value.manager.native_abort(value.begin['attempt_id'])['not_executed']
    assert not value.manager.applying


def test_receipt_changed_after_backup_is_rejected_without_helper_command(flow, monkeypatch):
    value = flow; begin(value); receipt = confirmation(value)
    original = value.manager._prepare_apply
    def changed():
        result = original()
        value.drafts.save({**value.scope, 'expected_revision': 1, 'content': {'text': 'Concurrent edit'}})
        return result
    monkeypatch.setattr(value.manager, '_prepare_apply', changed)
    with pytest.raises(HandoffRejected): prepare(value, [receipt])
    assert value.manager.native_handoff.active.bootstrap is None
    value.manager.native_abort(value.begin['attempt_id'])
    assert load_operation(value.manager.home, value.manager.state['operation_id'])['state'] == 'cancelled'


def test_receipt_cannot_be_reused_in_another_attempt(flow):
    value = flow; begin(value); old = confirmation(value)
    value.manager.native_abort(value.begin['attempt_id'])
    value.authority.acknowledge_abort(value.begin['attempt_id'])
    begin(value)
    with pytest.raises(HandoffRejected): prepare(value, [old])
    assert not list((value.manager.home / 'backups').glob('update-*'))


@pytest.mark.parametrize('change', ['staged', 'binding', 'expiry', 'scope'])
def test_staged_binding_expiry_or_scope_changes_fail_before_backup(flow, change):
    value = flow; begin(value); receipt = confirmation(value)
    if change == 'staged': value.manager.state['release']['size'] += 1
    elif change == 'binding': value.authority.update_binding(replace(value.binding, navigation=2))
    elif change == 'expiry': value.manager.native_handoff.clock = lambda: value.reservation.expires
    else: value.scope = {'scope_kind': 'chat', 'project_id': None, 'chat_id': 'missing'}
    if change == 'scope':
        with pytest.raises(HandoffRejected):
            value.drafts.confirm({**value.scope, 'expected_revision': 0, 'attempt_id': value.begin['attempt_id']})
    else:
        with pytest.raises((HandoffRejected, ValueError)): prepare(value, [receipt])
    assert not list((value.manager.home / 'backups').glob('update-*'))


@pytest.mark.parametrize('change', ['nonce', 'session', 'intent', 'helper', 'parent-created', 'parent-exe', 'unrelated-parent'])
def test_helper_authentication_refuses_changed_inputs_without_installer(flow, monkeypatch, change):
    value = flow; prepare(value); launch(value); parent(monkeypatch, value)
    frame = handoff.native_bootstrap_frame(value.bootstrap)
    if change in ('nonce', 'session'): frame[change] = 'f' * 64
    elif change == 'intent': frame['intent_sha256'] = 'f' * 64
    elif change == 'helper': Path(value.bootstrap.command[0]).write_bytes(b'Modified retained helper')
    elif change == 'parent-created': parent(monkeypatch, value, changed=True)
    elif change == 'parent-exe': parent(monkeypatch, value, wrong_exe=True)
    else: parent(monkeypatch, value, unrelated=True)
    with pytest.raises((HandoffRejected, ValueError)):
        handoff.helper_preflight(frame, executable=value.bootstrap.command[0], proof_policy=value.pins, install_dir=value.app)
    assert load_operation(value.manager.home, value.prepared.operation_id)['state'] == 'prepared'


def test_unrelated_parent_is_rejected_before_supplied_home_is_read(flow, monkeypatch):
    value = flow; prepare(value); launch(value); parent(monkeypatch, value, unrelated=True)
    monkeypatch.setattr('forge_updates.load_operation', lambda *args: pytest.fail('No untrusted path reads'))
    with pytest.raises(HandoffRejected):
        handoff.helper_preflight(handoff.native_bootstrap_frame(value.bootstrap), executable=value.bootstrap.command[0])


def test_abort_or_eof_before_commit_never_executes_or_changes_work(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch)
    assert handoff.helper_abort(context) == {'not_executed': True}
    with pytest.raises(HandoffRejected): handoff.helper_commit(context, runner=lambda args: pytest.fail('No installer'))
    assert value.manager.native_abort(value.begin['attempt_id']) == {'not_executed': True}
    assert load_operation(value.manager.home, value.prepared.operation_id)['state'] == 'cancelled'


@pytest.mark.parametrize('change', ['no-commit', 'parent-alive', 'pid-reused', 'durable-work', 'helper', 'live-tree'])
def test_no_installer_admission_after_ready_when_commit_exit_or_snapshot_is_wrong(flow, monkeypatch, change):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch)
    if change != 'no-commit': commit(value)
    parent(monkeypatch, value, exists=change in ('parent-alive', 'pid-reused'), changed=change == 'pid-reused')
    if change == 'durable-work': value.manager.store.save_entity('notes', {'id': 'late', 'text': 'Keep this'})
    elif change == 'helper': Path(value.bootstrap.command[0]).write_bytes(b'Modified after READY')
    elif change == 'live-tree': (value.app / 'new-unknown.txt').write_text('Preserve this')
    with pytest.raises((HandoffRejected, ValueError)):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('No installer admission'), wait_seconds=0)
    data = load_operation(value.manager.home, value.prepared.operation_id)
    assert data['state'] == 'cancelled' and data['handoff']['not_executed'] is True


@pytest.mark.parametrize('phase', ['prepared', 'committed'])
def test_restart_never_replays_private_handoff_or_forgets_uncertain_commit(flow, phase):
    value = flow; prepare(value)
    if phase == 'committed': launch(value); commit(value)
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins)
    assert restarted.status()['state'] == 'interrupted' and not restarted.status()['verified']
    assert restarted.inspection_required is (phase == 'committed')
    assert restarted.native_handoff is None and not restarted.applying
    restarted.shutdown()


def test_rollback_bootstraps_current_protocol_host_and_never_old_helper(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    assert handoff.helper_commit(context, runner=installer(value), wait_seconds=0)['installed']
    service = value.manager.service
    current = UpdateManager(service, '5.1.4', value.app, (), proof_policy=value.pins)
    service.update_manager = current
    authority = NativeAuthority(); authority.update_binding(value.binding)
    current.bind_native_authority(authority)
    reservation = current.native_reserve(value.binding, 'rollback')
    first = authority.begin(value.binding, reservation)
    ack = {'attempt_id': first['attempt_id'], 'challenge': first['challenge'],
        'workspace_revision': 2, 'participant_count': 1, 'receipts': []}
    grant = authority.acknowledge(ack)
    prepared = current.native_prepare(grant, ack)
    second = authority.prepared(grant)
    launch_grant = authority.confirm({'attempt_id': first['attempt_id'], 'confirmation': second['confirmation'],
        'workspace_revision': 2, 'participant_count': 1})
    bootstrap = current.native_launch(launch_grant, prepared)
    old = Path(load_operation(current.home, prepared.operation_id)['backup']) / 'installation/Forge.exe'
    assert Path(bootstrap.command[0]) != old
    assert Path(bootstrap.command[0]).parent.name.startswith('current-helper-')
    assert bootstrap.helper_version == '5.1.4' and bootstrap.helper_source_commit == NEW_SOURCE
    parent(monkeypatch, value)
    context = handoff.helper_preflight(handoff.native_bootstrap_frame(bootstrap),
        executable=bootstrap.command[0], proof_policy=value.pins, install_dir=value.app)
    ready = object(); authority.ready(launch_grant, ready)
    commit_grant = authority.commit_grant(first['attempt_id'], ready,
        {'ok': True, 'phase': 'prepared', 'workspace_revision': 2, 'participant_count': 1})
    current.native_commit(commit_grant, prepared, ready)
    parent(monkeypatch, value, exists=False)
    result = handoff.helper_commit(context, runner=lambda args: pytest.fail('Rollback cannot execute an installer'), wait_seconds=0)
    assert result['rolled_back'] and result['version'] == '5.1.3'
    current.shutdown()


def test_reference_change_with_same_draft_revision_invalidates_receipt(flow):
    value = flow
    document = value.manager.store.save_entity('documents', {'id': 'synthetic-document', 'project_id': None, 'text': 'Original'})
    value.drafts.save({**value.scope, 'expected_revision': 0,
        'content': {'text': 'Private draft', 'document_ids': [document['id']]}})
    begin(value)
    receipt = value.drafts.confirm({**value.scope, 'expected_revision': 1,
        'attempt_id': value.begin['attempt_id']})['receipt']
    value.manager.store.save_entity('documents', {**document, 'text': 'Changed saved reference'})
    with pytest.raises(HandoffRejected): prepare(value, [receipt])
    assert not list((value.manager.home / 'backups').glob('update-*'))


@pytest.mark.parametrize('state', ['approval', 'question', 'steer', 'submission', 'command', 'import'])
def test_pending_or_unknown_work_blocks_native_before_backup(flow, state):
    value = flow
    if state == 'approval':
        with value.manager.store._connection() as db:
            db.execute("INSERT INTO approvals VALUES('synthetic','run','invocation','hash','pending','{}')")
    elif state == 'question': value.manager.store.save_entity('questions', {'id': 'pending', 'status': 'pending'})
    elif state == 'steer': value.manager.store.save_entity('steers', {'id': 'pending', 'status': 'queued'})
    elif state == 'submission': value.manager.store.save_entity('ui_submissions', {'id': 'unknown', 'state': 'unknown'})
    elif state == 'command': value.manager.service._command_sessions = SimpleNamespace(jobs={'synthetic': object()})
    else: value.manager.service.model_manager = SimpleNamespace(jobs={'synthetic': {'status': 'import_unknown'}})
    with pytest.raises(HandoffRejected): begin(value)
    assert not list((value.manager.home / 'backups').glob('update-*'))


def test_pending_dictation_can_only_clear_through_real_scope_confirmation_before_backup(flow):
    value = flow
    pending = {'c' * 64}
    value.manager.service.update_native_input_guard = lambda: not pending
    seen = []
    def consume(snapshot, deliveries):
        seen.append((snapshot['scope'], snapshot['expected_revision']))
        if snapshot['scope'] != {'kind': 'new_chat', 'project_id': None} or deliveries != ('c' * 64,): return False
        pending.clear(); return True
    value.manager.service.host_dictation_guard = consume
    begin(value)
    value.drafts.save({**value.scope, 'expected_revision': 0, 'content': {'text': 'Accepted synthetic dictation'}})
    receipt = value.drafts.confirm({**value.scope, 'expected_revision': 1,
        'attempt_id': value.begin['attempt_id'], 'delivery_ids': ['c' * 64]})['receipt']
    assert seen == [({'kind': 'new_chat', 'project_id': None}, 1)] and pending == set()
    prepare(value, [receipt])
    assert value.manager.applying


@pytest.mark.parametrize('ids', [True, ['short'], ['c' * 64, 'c' * 64], ['c' * 64] * 17])
def test_dictation_claims_are_bounded_and_never_infer_success(flow, ids):
    value = flow; begin(value)
    with pytest.raises(HandoffRejected):
        value.drafts.confirm({**value.scope, 'expected_revision': 0,
            'attempt_id': value.begin['attempt_id'], 'delivery_ids': ids})
    assert not value.manager.native_handoff.active.receipts


@pytest.mark.parametrize('guard', [None, lambda: False, lambda: 1, lambda: 'ready'])
def test_native_input_unknown_state_blocks_preparation_without_backup(flow, guard):
    value = flow; begin(value)
    value.manager.service.update_native_input_guard = guard
    with pytest.raises(HandoffRejected): prepare(value)
    assert not list((value.manager.home / 'backups').glob('update-*'))


def test_fake_stopped_helper_json_cannot_disarm_committed_operation(flow):
    value = flow; prepare(value); launch(value); commit(value)
    with pytest.raises((HandoffRejected, AttributeError)):
        value.manager.native_abort(value.begin['attempt_id'], stopped_helper={'not_executed': True})
    assert value.manager.applying
    assert load_operation(value.manager.home, value.prepared.operation_id)['handoff']['phase'] == 'committed'


def test_opaque_tokens_and_scope_content_never_enter_public_status_or_diagnostics(flow):
    value = flow; begin(value); receipt = confirmation(value); prepare(value, [receipt]); bootstrap = launch(value)
    public = json.dumps([value.manager.dispatch('update_status'), value.manager.diagnostics_export()['diagnostics']])
    for secret in (receipt, bootstrap.nonce, bootstrap.session, 'Synthetic private draft', value.begin['challenge']):
        assert secret not in public


def test_real_junction_in_retained_helper_refuses_ready_and_preserves_outside(flow, monkeypatch, tmp_path):
    value = flow; prepare(value); launch(value); parent(monkeypatch, value)
    outside = tmp_path / 'outside'; outside.mkdir()
    sentinel = outside / 'sentinel.txt'; sentinel.write_text('Keep outside data')
    link = Path(value.bootstrap.command[0]).parent / '_internal' / 'junction'
    if os.name == 'nt':
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            'New-Item -ItemType Junction -Path $env:FORGE_FIXTURE_LINK -Target $env:FORGE_FIXTURE_TARGET | Out-Null'],
            env={**os.environ, 'FORGE_FIXTURE_LINK': str(link), 'FORGE_FIXTURE_TARGET': str(outside)},
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0 and link.is_junction()
    else: link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='links|junctions'):
            handoff.helper_preflight(handoff.native_bootstrap_frame(value.bootstrap),
                executable=value.bootstrap.command[0], proof_policy=value.pins, install_dir=value.app)
        assert sentinel.read_text() == 'Keep outside data'
        assert load_operation(value.manager.home, value.prepared.operation_id)['state'] == 'prepared'
    finally:
        if os.name == 'nt': link.rmdir()
        else: link.unlink()


@pytest.mark.parametrize('bad', [None, {}, True, 'c' * 64])
def test_native_backend_rejects_public_json_claims_without_side_effects(flow, bad):
    value = flow; begin(value)
    with pytest.raises(HandoffRejected): value.manager.native_prepare(bad, {})
    with pytest.raises(HandoffRejected): value.manager.native_launch(bad, bad)
    with pytest.raises(HandoffRejected): value.manager.native_commit(bad, bad, bad)
    assert not list((value.manager.home / 'backups').glob('update-*'))


def test_shutdown_disarms_new_native_admission_even_with_live_runtime_grant(flow):
    value = flow; begin(value)
    grant = value.authority.acknowledge({'attempt_id': value.begin['attempt_id'], 'challenge': value.begin['challenge'],
        'workspace_revision': 1, 'participant_count': 1, 'receipts': []})
    value.manager.shutdown()
    with pytest.raises(HandoffRejected):
        value.manager.native_prepare(grant, {'attempt_id': value.begin['attempt_id'], 'workspace_revision': 1,
            'participant_count': 1, 'receipts': []})
    assert not list((value.manager.home / 'backups').glob('update-*'))


def test_sequential_dictation_chunks_confirm_one_saved_scope_without_dropping_pending_ids(flow):
    value = flow; begin(value)
    pending = {format(number, '064x') for number in range(20)}
    value.manager.service.update_native_input_guard = lambda: not pending
    def consume(snapshot, subset):
        if snapshot['scope'] != {'kind': 'new_chat', 'project_id': None} or any(item not in pending for item in subset): return False
        pending.difference_update(subset); return True
    value.manager.service.host_dictation_guard = consume
    saved = value.drafts.save({**value.scope, 'expected_revision': 0, 'content': {'text': 'All accepted synthetic transcripts'}})
    all_ids = sorted(pending); receipts = []
    for subset in (all_ids[:16], all_ids[16:]):
        result = value.drafts.confirm({**value.scope, 'expected_revision': saved['revision'],
            'attempt_id': value.begin['attempt_id'], 'delivery_ids': subset})
        receipts.append(result['receipt'])
    assert len(set(receipts)) == 2 and not pending
    prepare(value, receipts)
    assert value.manager.applying


def test_shutdown_during_slow_launch_preflight_has_no_post_shutdown_admission(flow, monkeypatch):
    value = flow; prepare(value)
    entered, release = threading.Event(), threading.Event()
    original = handoff.verify_bootstrap
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(handoff, 'verify_bootstrap', slow)
    outcomes = []
    def worker():
        try: outcomes.append(launch(value))
        except HandoffRejected: outcomes.append('refused')
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(2)
    value.manager.shutdown()
    release.set(); thread.join(3)
    assert not thread.is_alive() and outcomes == ['refused']
    assert value.manager.native_handoff.active.phase == 'prepared'
    assert value.authority.status()['phase'] == 'failed'


def test_exact_stopped_owned_helper_disarms_failed_commit_while_parent_stays_open(flow, monkeypatch):
    from forge_native_handoff import HelperTransport
    value = flow; prepare(value); bootstrap = launch(value)
    exited = [None]
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), pid=9876, poll=lambda: exited[0])
    transport = HelperTransport(bootstrap, handoff.native_bootstrap_frame(bootstrap), popen=lambda *args, **kwargs: process)
    value.authority.register_helper(value.launch_grant, value.prepared, transport)
    commit(value)
    parent(monkeypatch, value)
    with pytest.raises(HandoffRejected): value.authority.stopped_helper(transport)
    exited[0] = 1
    proof = value.authority.stopped_helper(transport)
    with pytest.raises(HandoffRejected):
        value.manager.native_abort(value.begin['attempt_id'], stopped_helper=replace(proof))
    assert value.manager.applying
    assert value.manager.native_abort(value.begin['attempt_id'], stopped_helper=proof)['not_executed']
    assert value.authority.acknowledge_abort(value.begin['attempt_id'])['aborted']
    assert not value.manager.applying
    assert load_operation(value.manager.home, value.prepared.operation_id)['state'] == 'cancelled'
    assert value.authority.consume_stopped_helper(proof, value.reservation, value.prepared.operation_id) is False


@pytest.mark.parametrize('failure', ['journal_write', 'status_save'])
def test_stopped_helper_abort_retries_transient_io_with_exact_consumed_proof(flow, monkeypatch, failure):
    from forge_native_handoff import HelperTransport
    import forge_updates
    value = flow; prepare(value); bootstrap = launch(value)
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), pid=9876, poll=lambda: 1)
    transport = HelperTransport(bootstrap, handoff.native_bootstrap_frame(bootstrap), popen=lambda *args, **kwargs: process)
    value.authority.register_helper(value.launch_grant, value.prepared, transport)
    commit(value); parent(monkeypatch, value)
    receipt = value.authority.stopped_helper(transport)
    failed = [False]
    if failure == 'journal_write':
        original = forge_updates.atomic_text
        def write(path, text):
            if not failed[0] and path.parent.name == 'operations':
                failed[0] = True
                raise OSError('Synthetic transient journal failure')
            return original(path, text)
        monkeypatch.setattr(forge_updates, 'atomic_text', write)
    else:
        original = value.manager._save
        def save(**changes):
            if not failed[0]:
                failed[0] = True
                raise OSError('Synthetic transient status failure')
            return original(**changes)
        monkeypatch.setattr(value.manager, '_save', save)
    with pytest.raises(OSError): value.manager.native_abort(value.begin['attempt_id'], stopped_helper=receipt)
    assert value.manager.applying
    with pytest.raises(HandoffRejected):
        value.manager.native_abort(value.begin['attempt_id'], stopped_helper=replace(receipt))
    parent(monkeypatch, value, changed=True)
    with pytest.raises(HandoffRejected): value.manager.native_abort(value.begin['attempt_id'], stopped_helper=receipt)
    assert value.manager.applying
    parent(monkeypatch, value)
    assert value.manager.native_abort(value.begin['attempt_id'], stopped_helper=receipt)['not_executed']
    assert value.authority.acknowledge_abort(value.begin['attempt_id'])['aborted']
    assert not value.manager.applying
    data = load_operation(value.manager.home, value.prepared.operation_id)
    assert data['state'] == 'cancelled' and data['handoff']['not_executed'] is True
    assert value.authority.consume_stopped_helper(receipt, value.reservation, value.prepared.operation_id) is False
    begin(value)
    with pytest.raises(HandoffRejected): value.manager.native_abort(value.begin['attempt_id'], stopped_helper=receipt)
    assert value.manager.native_handoff.active.reservation is value.reservation


@pytest.mark.parametrize('boundary', ['saved_work', 'helper_preflight', 'applying_write'])
@pytest.mark.parametrize('phase', ['prepared', 'aborted', 'unknown'])
def test_changed_native_intent_never_reaches_actual_installer_admission(flow, monkeypatch, boundary, phase):
    import forge_updates
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    changed = []
    def mutate():
        data = load_operation(value.manager.home, value.prepared.operation_id)
        data['handoff'].update(phase=phase, nonce_sha256='f' * 64)
        atomic_text(value.manager.root / 'operations' / (data['id'] + '.json'), encode(data))
        changed.append(deepcopy(data))
    if boundary == 'saved_work':
        original = handoff.verify_saved_work
        def saved(home, data): original(home, data); mutate()
        monkeypatch.setattr(handoff, 'verify_saved_work', saved)
    elif boundary == 'helper_preflight':
        original = forge_updates.helper_preflight
        def preflight(*args, **kwargs):
            plan = original(*args, **kwargs); mutate(); return plan
        monkeypatch.setattr(forge_updates, 'helper_preflight', preflight)
    else:
        original = forge_updates.atomic_text
        def write(path, text):
            original(path, text)
            if path.parent.name == 'operations' and json.loads(text)['state'] == 'applying': mutate()
        monkeypatch.setattr(forge_updates, 'atomic_text', write)
    with pytest.raises(HandoffRejected):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('Changed intent cannot admit an installer'), wait_seconds=0)
    assert len(changed) == 1
    assert load_operation(value.manager.home, value.prepared.operation_id) == changed[0]
    with pytest.raises(HandoffRejected): handoff.helper_recovery_command(context)


def test_unknown_applying_outcome_is_preserved_without_reopen_claim(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    original = handoff.verify_saved_work
    changed = []
    def saved(home, data):
        original(home, data)
        latest = load_operation(home, data['id'])
        latest['state'] = 'applying'
        atomic_text(value.manager.root / 'operations' / (data['id'] + '.json'), encode(latest))
        changed.append(deepcopy(latest))
    monkeypatch.setattr(handoff, 'verify_saved_work', saved)
    with pytest.raises(ValueError):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('Unknown admission cannot run an installer'), wait_seconds=0)
    assert load_operation(value.manager.home, value.prepared.operation_id) == changed[0]
    with pytest.raises(HandoffRejected): handoff.helper_recovery_command(context)


def test_user_work_changed_during_preflight_is_retained_without_installation(flow, monkeypatch):
    import forge_updates
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    original = forge_updates.helper_preflight
    def preflight(*args, **kwargs):
        plan = original(*args, **kwargs)
        value.manager.store.save_entity('documents', {'id': 'late-document', 'text': 'Synthetic later saved work'})
        return plan
    monkeypatch.setattr(forge_updates, 'helper_preflight', preflight)
    with pytest.raises(HandoffRejected):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('New work cannot admit a stale restart'), wait_seconds=0)
    assert value.manager.store.entity('documents', 'late-document')['text'] == 'Synthetic later saved work'
    data = load_operation(value.manager.home, value.prepared.operation_id)
    assert data['state'] == 'cancelled' and data['handoff']['not_executed'] is True


@pytest.mark.parametrize('refusal', ['expired', 'reused_parent'])
def test_native_refusal_after_applying_write_is_proved_not_executed(flow, monkeypatch, refusal):
    import forge_updates
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    original = forge_updates.atomic_text
    def write(path, text):
        original(path, text)
        if path.parent.name == 'operations' and json.loads(text)['state'] == 'applying':
            if refusal == 'expired':
                monkeypatch.setattr(handoff.time, 'time', lambda: context.operation['handoff']['expires_at'])
            else:
                monkeypatch.setattr(handoff.psutil, 'pid_exists', lambda pid: True)
    monkeypatch.setattr(forge_updates, 'atomic_text', write)
    with pytest.raises(HandoffRejected):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('Refused admission cannot run the installer'), wait_seconds=0)
    data = load_operation(value.manager.home, value.prepared.operation_id)
    assert data['state'] == 'cancelled' and data['handoff']['not_executed'] is True
    if refusal == 'reused_parent':
        with pytest.raises(HandoffRejected): handoff.helper_recovery_command(context)
    else:
        assert handoff.helper_recovery_command(context) == (str(value.app / 'Forge.exe'),)


def test_changed_rollback_intent_cannot_move_current_installation(flow, monkeypatch):
    import forge_updates
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    assert handoff.helper_commit(context, runner=installer(value), wait_seconds=0)['installed']
    value.manager.shutdown()
    service = value.manager.service
    value.manager = UpdateManager(service, '5.1.4', value.app, (), proof_policy=value.pins)
    service.update_manager = value.manager
    value.authority = NativeAuthority(); value.authority.update_binding(value.binding)
    value.manager.bind_native_authority(value.authority)
    value.reservation = value.manager.native_reserve(value.binding, 'rollback')
    value.begin = value.authority.begin(value.binding, value.reservation)
    prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    original = forge_updates.retain_rollback_state
    changed = []
    def snapshot(home, data):
        current = original(home, data)
        latest = load_operation(home, data['id'])
        latest['handoff'].update(phase='aborted', nonce_sha256='f' * 64)
        atomic_text(value.manager.root / 'operations' / (data['id'] + '.json'), encode(latest))
        changed.append(deepcopy(latest))
        return current
    monkeypatch.setattr(forge_updates, 'retain_rollback_state', snapshot)
    monkeypatch.setattr(forge_updates.shutil, 'move', lambda *args, **kwargs: pytest.fail('Changed rollback cannot move code or data'))
    with pytest.raises(HandoffRejected):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('Rollback never runs an installer'), wait_seconds=0)
    assert load_operation(value.manager.home, value.prepared.operation_id) == changed[0]
    proof.verify_build(proof.read_record(value.app / proof.BUILD_NAME), value.pins, root=value.app,
        expected_version='5.1.4', expected_source=NEW_SOURCE, installed=True)
    with pytest.raises(HandoffRejected): handoff.helper_recovery_command(context)


@pytest.mark.parametrize('claim', [True, {}, object()])
def test_public_claim_cannot_replace_private_installer_admission(flow, claim):
    with pytest.raises(ValueError, match='Authenticated native installer admission'):
        helper_main(flow.manager.home, 'f' * 32, os.getpid(), _native_admission=claim,
            runner=lambda args: pytest.fail('Public claims cannot start an installer'))
    assert not list((flow.manager.home / 'backups').glob('update-*'))


def test_copied_private_admission_cannot_consume_the_exact_ready_context(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    admission = handoff._InstallerAdmission(context)
    before = load_operation(value.manager.home, value.prepared.operation_id)
    try:
        with pytest.raises(HandoffRejected):
            helper_main(value.manager.home, value.prepared.operation_id, value.binding.parent_pid,
                install_dir=value.app, proof_policy=value.pins, _native_admission=copy(admission),
                runner=lambda args: pytest.fail('Copied authority cannot admit an installer'))
        assert load_operation(value.manager.home, value.prepared.operation_id) == before
        result = helper_main(value.manager.home, value.prepared.operation_id, value.binding.parent_pid,
            install_dir=value.app, proof_policy=value.pins, _native_admission=admission, runner=installer(value))
        assert result['installed']
        with pytest.raises(HandoffRejected): admission.require_current()
        with pytest.raises(HandoffRejected): handoff._InstallerAdmission(context)
    finally:
        admission.release()


def test_verified_success_restarts_generated_current_host_once(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    result = handoff.helper_commit(context, runner=installer(value), wait_seconds=0)
    with pytest.raises(HandoffRejected): handoff.helper_restart_command(context, dict(result))
    result['restart_command'] = ['Untrusted supplied command ignored']
    assert handoff.helper_restart_command(context, result) == (str(value.app / 'Forge.exe'),)
    with pytest.raises(HandoffRejected): handoff.helper_restart_command(context, result)


def test_changed_new_host_after_install_never_receives_restart_execution(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    result = handoff.helper_commit(context, runner=installer(value), wait_seconds=0)
    (value.app / 'Forge.exe').write_bytes(b'Changed installed host')
    with pytest.raises(ValueError): handoff.helper_restart_command(context, result)


def test_known_pre_install_refusal_reopens_only_intact_current_code(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    value.manager.store.save_entity('notes', {'id': 'late', 'text': 'Retained new user work'})
    with pytest.raises(HandoffRejected):
        handoff.helper_commit(context, runner=lambda args: pytest.fail('No installer'), wait_seconds=0)
    assert handoff.helper_recovery_command(context) == (str(value.app / 'Forge.exe'),)
    assert value.manager.store.entity('notes', 'late')['text'] == 'Retained new user work'
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins)
    assert restarted.status()['state'] == 'cancelled' and not restarted.inspection_required
    restarted.shutdown()


def test_changed_journal_is_retained_and_cannot_claim_known_nonexecution(flow, monkeypatch):
    value = flow; prepare(value); launch(value); context = helper(value, monkeypatch); commit(value)
    parent(monkeypatch, value, exists=False)
    path = value.manager.root / 'operations' / (value.prepared.operation_id + '.json')
    data = load_operation(value.manager.home, value.prepared.operation_id)
    data['unexpected'] = 'Keep modified evidence'
    atomic_text(path, encode(data)); before = path.read_bytes()
    with pytest.raises(HandoffRejected): handoff.helper_commit(context, runner=lambda args: pytest.fail('No installer'), wait_seconds=0)
    assert path.read_bytes() == before
    with pytest.raises(HandoffRejected): handoff.helper_recovery_command(context)


def test_wrong_attempt_cannot_abort_current_native_readiness(flow):
    value = flow; begin(value)
    with pytest.raises(HandoffRejected): value.manager.native_abort('f' * 32)
    assert value.manager.native_handoff.active.reservation is value.reservation
    assert value.manager.native_abort(value.begin['attempt_id'])['not_executed']


@pytest.mark.parametrize('malformed', [None, True, [], 'not an object'])
def test_malformed_handoff_journal_restarts_fail_closed_without_raw_crash(flow, malformed):
    value = flow; prepare(value)
    data = load_operation(value.manager.home, value.prepared.operation_id)
    data['handoff'] = malformed
    path = value.manager.root / 'operations' / (data['id'] + '.json')
    atomic_text(path, encode(data)); before = path.read_bytes()
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins)
    assert restarted.inspection_required and restarted.status()['state'] == 'error'
    assert path.read_bytes() == before
    restarted.shutdown()


def test_pre_authority_binding_loss_cancels_exact_reservation_idempotently(flow):
    value = flow
    reservation = value.manager.native_reserve(value.binding)
    value.authority.update_binding(None)
    with pytest.raises(HandoffRejected): value.authority.begin(value.binding, reservation)
    assert value.manager.native_cancel_reservation(reservation) == {'not_executed': True}
    assert value.manager.native_cancel_reservation(reservation) == {'not_executed': True}
    with pytest.raises(HandoffRejected): value.manager.native_cancel_reservation(replace(reservation))
    value.authority.update_binding(value.binding)
    replacement = value.manager.native_reserve(value.binding)
    assert replacement is not reservation
    # A stale cleanup cannot remove a newer reservation.
    with pytest.raises(HandoffRejected): value.manager.native_cancel_reservation(reservation)
    assert value.manager.native_handoff.active.reservation is replacement


def test_issued_checking_attempt_requires_normal_acknowledged_abort(flow):
    value = flow; begin(value)
    with pytest.raises(HandoffRejected): value.manager.native_cancel_reservation(value.reservation)
    assert value.manager.native_handoff.active.reservation is value.reservation
    assert value.manager.native_abort(value.begin['attempt_id'])['not_executed']
    assert value.authority.acknowledge_abort(value.begin['attempt_id'])['aborted']
