"""Synthetic native authority regressions; no installed state or WebView2."""
from dataclasses import replace
import json
import threading

import pytest

from forge_native_handoff import HandoffRejected, NativeAuthority, NativeBinding, NativeGrant


@pytest.fixture
def flow():
    now = [100.0]
    authority = NativeAuthority(clock=lambda: now[0])
    binding = NativeBinding(object(), 'a' * 64, 'synthetic-main-window', 1,
        'http://127.0.0.1:12345', 100, 1.0)
    authority.update_binding(binding)
    reservation = object()
    begin = authority.begin(binding, reservation)
    ack = {'attempt_id': begin['attempt_id'], 'challenge': begin['challenge'],
        'workspace_revision': 5, 'participant_count': 3, 'receipts': ['b' * 64]}
    return authority, binding, reservation, begin, ack, now


def prepare(flow):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    authority.consume(grant, 'prepare', binding, reservation)
    prepared = authority.prepared(grant)
    second = {'attempt_id': prepared['attempt_id'], 'confirmation': prepared['confirmation'],
        'workspace_revision': ack['workspace_revision'], 'participant_count': ack['participant_count']}
    return grant, second


def test_only_exact_runtime_objects_authorize_each_stage_once(flow):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    assert authority.consume(grant, 'prepare', binding, reservation) is True
    with pytest.raises(HandoffRejected):
        authority.consume(grant, 'prepare', binding, reservation)
    prepared = authority.prepared(grant)
    launch = authority.confirm({'attempt_id': prepared['attempt_id'], 'confirmation': prepared['confirmation'],
        'workspace_revision': 5, 'participant_count': 3})
    authority.consume(launch, 'launch', binding, reservation)
    ready = object()
    authority.ready(launch, ready)
    commit = authority.commit_grant(ack['attempt_id'], ready,
        {'ok': True, 'workspace_revision': 5, 'participant_count': 3, 'phase': 'prepared'})
    authority.consume(commit, 'commit', binding, reservation)
    authority.committed(commit)
    assert authority.status()['phase'] == 'committed'
    with pytest.raises(HandoffRejected):
        authority.acknowledge_abort(ack['attempt_id'])


@pytest.mark.parametrize('spoof', [None, {}, {'ready': True, 'native_ready_token': 'synthetic'}, 'a' * 64])
def test_json_or_boolean_claims_cannot_construct_native_authority(flow, spoof):
    authority, binding, reservation, *_ = flow
    with pytest.raises(HandoffRejected):
        authority.consume(spoof, 'prepare', binding, reservation)


def test_copy_of_real_grant_is_not_authority(flow):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    copied = replace(grant)
    with pytest.raises(HandoffRejected):
        authority.consume(copied, 'prepare', binding, reservation)
    assert authority.consume(grant, 'prepare', binding, reservation)


@pytest.mark.parametrize('field,value', [('session', 'c' * 64), ('window_id', 'synthetic-child'),
    ('navigation', 2), ('origin', 'http://127.0.0.1:12346'), ('parent_pid', 101), ('parent_created', 2.0), ('host', object())])
def test_host_session_navigation_and_parent_changes_revoke_grant(flow, field, value):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    changed = replace(binding, **{field: value})
    authority.update_binding(changed)
    with pytest.raises(HandoffRejected):
        authority.consume(grant, 'prepare', binding, reservation)
    assert authority.status()['locked'] is True
    assert authority.status()['reason'] == 'workspace_changed'


def test_equal_looking_operation_cannot_replace_reserved_object(flow):
    authority, binding, _, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    with pytest.raises(HandoffRejected):
        authority.consume(grant, 'prepare', binding, object())


@pytest.mark.parametrize('mutation', [
    {'challenge': '0' * 64}, {'attempt_id': '0' * 32}, {'workspace_revision': True},
    {'workspace_revision': -1}, {'workspace_revision': 2**53}, {'participant_count': True},
    {'participant_count': 0}, {'participant_count': 513}, {'receipts': ['synthetic-private-draft']},
    {'receipts': ['b' * 64, 'b' * 64]}, {'private_text': 'synthetic-private-text'},
])
def test_ack_bounds_and_privacy_reject_untrusted_payloads(flow, mutation):
    authority, _, _, _, ack, _ = flow
    ack.update(mutation)
    with pytest.raises(HandoffRejected) as rejected:
        authority.acknowledge(ack)
    assert 'synthetic-private' not in str(rejected.value)
    assert 'synthetic-private' not in json.dumps(authority.status())


@pytest.mark.parametrize('offset', [120, 121])
def test_expired_attempt_never_prepares_or_unlocks_without_abort_receipt(flow, offset):
    authority, _, _, _, ack, now = flow
    now[0] += offset
    with pytest.raises(HandoffRejected, match='Restart did not start'):
        authority.acknowledge(ack)
    status = authority.status()
    assert status['locked'] and not status['aborted']
    assert status['reason'] == 'confirmation_expired'
    assert authority.acknowledge_abort(ack['attempt_id'])['not_executed'] is True
    assert not authority.status()['locked']


def test_new_attempt_requires_confirmed_abort_and_revokes_previous_grants(flow):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    authority.invalidate()
    with pytest.raises(HandoffRejected):
        authority.begin(binding, object())
    authority.acknowledge_abort(ack['attempt_id'])
    authority.begin(binding, object())
    with pytest.raises(HandoffRejected):
        authority.consume(grant, 'prepare', binding, reservation)


@pytest.mark.parametrize('mutation', [{'workspace_revision': 6}, {'participant_count': 4},
    {'confirmation': '0' * 64}, {'private_text': 'synthetic-private-text'}])
def test_second_confirmation_rejects_changed_membership_or_revision(flow, mutation):
    authority, *_ = flow
    _, second = prepare(flow)
    second.update(mutation)
    with pytest.raises(HandoffRejected):
        authority.confirm(second)


@pytest.mark.parametrize('mutation', [{'ok': False}, {'phase': 'checking'}, {'workspace_revision': 6},
    {'participant_count': 4}, {'private_text': 'synthetic-private-text'}])
def test_post_ready_fence_rejects_stale_renderer_before_commit(flow, mutation):
    authority, binding, reservation, *_ = flow
    _, second = prepare(flow)
    grant = authority.confirm(second)
    authority.consume(grant, 'launch', binding, reservation)
    ready = object()
    authority.ready(grant, ready)
    fence = {'ok': True, 'workspace_revision': 5, 'participant_count': 3, 'phase': 'locked'}
    fence.update(mutation)
    with pytest.raises(HandoffRejected):
        authority.commit_grant(second['attempt_id'], ready, fence)
    assert authority.status()['phase'] == 'failed'


def test_duplicate_concurrent_consumption_has_one_winner(flow):
    authority, binding, reservation, _, ack, _ = flow
    grant = authority.acknowledge(ack)
    barrier = threading.Barrier(3)
    outcomes = []
    def consume():
        barrier.wait()
        try:
            outcomes.append(authority.consume(grant, 'prepare', binding, reservation))
        except HandoffRejected:
            outcomes.append(False)
    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(timeout=2)
    assert sorted(outcomes) == [False, True]
