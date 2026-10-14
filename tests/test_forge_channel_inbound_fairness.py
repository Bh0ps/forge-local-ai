"""Actual channel/store queues with disposable state and no external effects."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

import forge_channels
from forge_channels import ChannelManager, INBOUND_DISPATCH_LIMIT, INBOUND_SCAN_LIMIT
from forge_store import ForgeStore, encode


def forbidden(*args, **kwargs):
    raise AssertionError('External/native/provider/credential I/O is forbidden in this fixture.')


class NoExternal:
    get = put = telegram = webhook = close = forbidden


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(Path, 'home', forbidden)
    monkeypatch.setattr(forge_channels, 'CredentialVault', forbidden)
    store = ForgeStore(tmp_path / 'disposable')
    started, cancelled, approved, answers = [], [], [], []
    questions = {}

    def start(data):
        started.append(dict(data))
        return store.create_run(data)

    def cancel(run_id, pause=False):
        cancelled.append((run_id, pause))
        store.update_run(run_id, status='paused' if pause else 'cancelled')

    def answer(question_id, values):
        answers.append((question_id, values))
        questions.clear()

    service = SimpleNamespace(store=store, update_manager=SimpleNamespace(applying=False),
        jobs=SimpleNamespace(start=start, cancel=cancel, approve=lambda *args: approved.append(args)),
        get_interaction=lambda: SimpleNamespace(pending=lambda chat_id: questions.get(chat_id, []), answer=answer))
    manager = ChannelManager(service, vault=NoExternal(), transport=NoExternal(), clock=lambda: 1000)
    manager.recovered = True
    return SimpleNamespace(store=store, service=service, manager=manager, started=started,
        cancelled=cancelled, approved=approved, answers=answers, questions=questions)


def config(queue, identifier='fixture-channel', recipients=('42', '43'), **extra):
    return queue.store.save_entity('channels', {'id': identifier, 'name': 'Synthetic channel',
        'kind': 'telegram', 'enabled': True, 'allow_inbound': True, 'allow_outbound': False,
        'inbound_tasks': True, 'connected': False, 'permission_profile': 'always_ask', 'owner_id': '42',
        'allowlist': [{'sender_id': str(value), 'chat_id': str(value)} for value in recipients], **extra})


def add(queue, channel, number, recipient=42, text=None, *, sender=None, payload=None, tie=False):
    if payload is None:
        payload = {'update_id': number, 'message': {'message_id': number,
            'from': {'id': recipient if sender is None else sender}, 'chat': {'id': recipient},
            'text': text if text is not None else 'Synthetic request ' + str(number)}}
    assert queue.manager._record_inbound(channel['id'], str(number), payload)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=0 if tie else number)
    with queue.store._connection(transaction='write') as db:
        db.execute('UPDATE channel_inbound SET created_at=? WHERE channel_id=? AND external_id=?',
                   (stamp.isoformat(), channel['id'], str(number)))


def row(queue, channel, number):
    with queue.store._connection() as db:
        return dict(db.execute('SELECT rowid AS queue_rowid,* FROM channel_inbound WHERE channel_id=? AND external_id=?',
                               (channel['id'], str(number))).fetchone())


def busy(queue, channel, recipient='42'):
    chat = queue.manager._route(channel, recipient)
    run = queue.store.create_run({'chat_id': chat, 'text': 'Synthetic held work'})
    queue.store.update_run(run['id'], status='running')
    return run


def restart(queue):
    queue.manager = ChannelManager(queue.service, vault=NoExternal(), transport=NoExternal(), clock=lambda: 1000)
    return queue.manager


def save_cursor(queue, channel, number):
    with queue.store._connection(transaction='write') as db:
        item=db.execute('SELECT channel_id,external_id,queue_seq,created_at,generation,lane,recipient,data_hash FROM channel_inbound_queue WHERE channel_id=? AND external_id=?',(channel['id'],str(number))).fetchone()
        queue.manager.inbound_queue.advance(db,tuple(item))


def test_busy_prefix_does_not_starve_another_route_or_pause_and_fifo_resumes(queue):
    channel = config(queue)
    held = busy(queue, channel)
    for number in range(50):
        add(queue, channel, number)
    add(queue, channel, 50, recipient=43)
    add(queue, channel, 51, text='/pause')
    queue.manager.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:50']
    assert queue.cancelled == [(held['id'], True)]
    assert all(row(queue, channel, number)['status'] == 'pending' for number in range(50))
    queue.manager.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:50', 'channel:fixture-channel:0']
    queue.manager.tick(poll=False)
    assert len(queue.started) == 2


def test_scan_rotates_past_more_than_two_hundred_busy_rows_with_bounded_work(queue, monkeypatch):
    channel = config(queue)
    held = busy(queue, channel)
    for number in range(450):
        add(queue, channel, number)
    add(queue, channel, 450, recipient=43)
    add(queue, channel, 451, text='/pause')
    dispatched = []
    dispatch = queue.manager._dispatch_inbound
    monkeypatch.setattr(queue.manager, '_dispatch_inbound', lambda item: (dispatched.append(item['external_id']), dispatch(item))[1])
    for _ in range(7):
        calls = len(dispatched)
        queue.manager.tick(poll=False)
        work=queue.manager.inbound_queue.last_work
        assert work['candidate_metadata']<=INBOUND_SCAN_LIMIT
        assert work['payload_work']<=200
        assert len(dispatched) - calls <= INBOUND_DISPATCH_LIMIT
        if row(queue,channel,450)['status']==row(queue,channel,451)['status']=='dispatched':break
    assert row(queue, channel, 450)['status'] == row(queue, channel, 451)['status'] == 'dispatched'
    assert queue.cancelled == [(held['id'], True)]
    assert queue.started[0]['source_key']=='channel:fixture-channel:450'
    queue.manager.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:450', 'channel:fixture-channel:0']


def test_dispatch_budget_keeps_later_routes_reachable_on_next_tick(queue):
    channel = config(queue, recipients=tuple(str(value) for value in range(100, 170)))
    for number in range(70):
        add(queue, channel, number, recipient=100 + number)
    queue.manager.tick(poll=False)
    assert 0<len(queue.started)<=INBOUND_DISPATCH_LIMIT
    assert queue.manager.inbound_queue.last_work['payload_work']<=200
    queue.manager.tick(poll=False)
    assert len(queue.started) == 70
    assert len({item['source_key'] for item in queue.started}) == 70


def test_pause_after_saved_cursor_does_not_let_newer_message_jump_tied_older_messages(queue):
    channel = config(queue)
    held = busy(queue, channel)
    for number in range(4):
        add(queue, channel, number, text='/pause' if number == 2 else None, tie=True)
    older = row(queue, channel, 1)
    save_cursor(queue,channel,1)
    restart(queue).tick(poll=False)
    assert queue.cancelled == [(held['id'], True)]
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:0']
    assert row(queue, channel, 3)['status'] == 'pending'
    restarted = restart(queue)
    restarted.tick(poll=False)
    assert len(queue.started) == 1
    queue.store.update_run(row(queue, channel, 0)['run_id'], status='completed')
    restarted.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:0', 'channel:fixture-channel:1']


def test_same_chat_routes_on_different_channels_share_an_ordinary_fifo(queue):
    first, second = config(queue, 'first'), config(queue, 'second')
    chat = queue.manager._route(first, '42')
    with queue.store._connection(transaction='write') as db:
        db.execute('INSERT INTO channel_routes VALUES(?,?,?,?)', (second['id'], '42', chat, None))
    add(queue, first, 0)
    add(queue, second, 1)
    older = row(queue, first, 0)
    save_cursor(queue,first,0)
    queue.manager.tick(poll=False)
    queue.manager.tick(poll=False)  # Both alias generations must be classified.
    assert [item['source_key'] for item in queue.started] == ['channel:first:0']
    queue.store.update_run(row(queue, first, 0)['run_id'], status='completed')
    queue.manager.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:first:0', 'channel:second:1']


@pytest.mark.parametrize('recipient,other', [('0042', '42'), (99999999999999999998, 99999999999999999999)],
                         ids=['leading-zero-text', 'exact-twenty-digit-integer'])
def test_recipient_identity_is_exact_and_unrounded(queue, recipient, other):
    channel = config(queue, recipients=(str(recipient), str(other)))
    add(queue, channel, 0, recipient=recipient)
    add(queue, channel, 1, recipient=recipient)
    add(queue, channel, 2, recipient=other)
    older = row(queue, channel, 0)
    save_cursor(queue,channel,0)
    queue.manager.tick(poll=False)
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:2', 'channel:fixture-channel:0']
    assert row(queue, channel, 1)['status'] == 'pending'


def test_stale_project_route_is_refused_without_blocking_other_routes(queue):
    channel = config(queue)
    chat = queue.manager._route(channel, '42')
    with queue.store._connection(transaction='write') as db:
        db.execute('UPDATE channel_routes SET project_id=? WHERE channel_id=? AND recipient=?', ('stale-project', channel['id'], '42'))
    add(queue, channel, 0)
    add(queue, channel, 1)
    add(queue, channel, 2, recipient=43)
    queue.manager.tick(poll=False)
    assert row(queue, channel, 0)['status'] == row(queue, channel, 1)['status'] == 'rejected'
    assert queue.started[0]['chat_id'] != chat and len(queue.started) == 1


def test_unauthorized_controls_and_malformed_records_reach_the_existing_refusal_paths(queue):
    channel = config(queue, recipients=('42', '43', '99'))
    held = busy(queue, channel)
    for number in range(50):
        add(queue, channel, number)
    add(queue, channel, 50, text='/pause', sender=99)
    add(queue, channel, 51, recipient=43, sender=99)
    add(queue, channel, 52, payload={'message': {'chat': {'id': True}, 'from': {'id': True}, 'text': 'Invalid'}})
    add(queue, channel, 53)
    with queue.store._connection(transaction='write') as db:
        db.execute('UPDATE channel_inbound SET data=? WHERE external_id=?', ('{malformed', '53'))
    add(queue, channel, 54, recipient=43)
    queue.manager.tick(poll=False)
    assert all(row(queue, channel, number)['status'] == 'rejected' for number in range(50, 54))
    assert not queue.cancelled and queue.store.run(held['id'])['status'] == 'running'
    assert [item['source_key'] for item in queue.started] == ['channel:fixture-channel:54']


def test_malformed_channel_and_disabled_records_do_not_poison_the_rotation(queue):
    bad, disabled, good = config(queue, 'bad'), config(queue, 'disabled', enabled=False), config(queue, 'good')
    with queue.store._connection(transaction='write') as db:
        db.execute("UPDATE entities SET data='{bad' WHERE kind='channels' AND id='bad'")
    add(queue, bad, 0)
    add(queue, bad, 1)
    add(queue, disabled, 2)
    add(queue, good, 3)
    queue.manager.tick(poll=False)
    queue.manager.tick(poll=False)
    queue.manager.tick(poll=False)
    assert row(queue, bad, 0)['status'] == row(queue, bad, 1)['status'] == 'rejected'
    assert row(queue, disabled, 2)['status'] == 'cancelled'
    assert [item['source_key'] for item in queue.started] == ['channel:good:3']


def test_pairing_and_explicit_answer_pass_busy_prefix_without_starting_another_run(queue):
    code = 'synthetic-pair-code'
    channel = config(queue, pair_hash=sha256(code.encode()).hexdigest(), pair_expires=2000)
    held = busy(queue, channel)
    queue.questions[held['chat_id']] = [{'id': 'question', 'items': [{'id': 'choice'}]}]
    for number in range(50):
        add(queue, channel, number)
    # Ordinary text keeps its FIFO; select beyond it so only explicit controls
    # answer the pending question in this tick.
    last = row(queue, channel, 49)
    save_cursor(queue,channel,49)
    add(queue, channel, 50, recipient=99, text='/pair ' + code)
    add(queue, channel, 51, text='/answer Synthetic choice')
    queue.manager.tick(poll=False)
    assert row(queue, channel, 50)['status'] == 'paired'
    assert queue.store.entities('channel_pairings')[0]['status'] == 'pending'
    assert queue.answers == [('question', {'choice': {'text': 'Synthetic choice'}})]
    assert not queue.started
    assert row(queue, channel, 0)['status'] == 'pending'


def test_approval_callback_behind_busy_messages_remains_bound_and_single_use(queue):
    channel = config(queue)
    held = busy(queue, channel)
    action = {'tool': 'read_file', 'arguments': {'path': 'synthetic.txt'}}
    digest = sha256(encode(action).encode()).hexdigest()
    with queue.store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)', ('approval', held['id'], 'invocation', digest, 'pending', encode(action)))
        db.execute('INSERT INTO channel_callbacks VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('synthetic-token', channel['id'], '42', '42', held['id'], 'approval', digest, 1, 2000, None))
    for number in range(50):
        add(queue, channel, number)
    for number in (50, 51):
        add(queue, channel, number, payload={'update_id': number, 'callback_query': {'from': {'id': 42},
            'message': {'chat': {'id': 42}}, 'data': 'forge:synthetic-token'}})
    queue.manager.tick(poll=False)
    restart(queue).tick(poll=False)
    assert queue.approved == [(held['id'], 'approval', True)]
    assert row(queue, channel, 50)['status'] == 'dispatched' and row(queue, channel, 51)['status'] == 'rejected'
    assert not queue.started


@pytest.mark.parametrize('saved', [None, {'wrong': 1}, ['bad', True], ['bad', -1], 'not-json'],
                         ids=['missing', 'wrong-shape', 'boolean-position', 'negative-position', 'corrupt-json'])
def test_invalid_scan_checkpoint_resets_safely_and_is_private(queue, saved):
    channel = config(queue)
    add(queue, channel, 0)
    if saved is not None:
        with queue.store._connection(transaction='write') as db:
            queue.manager._set_checkpoint(db, '_global', 'inbound_scan', saved)
            if saved == 'not-json':
                db.execute("UPDATE channel_checkpoints SET value='{bad' WHERE channel_id='_global' AND key='inbound_scan'")
    queue.manager.tick(poll=False)
    assert len(queue.started) == 1
    assert 'inbound_scan' not in encode(queue.manager.configurations())
    assert 'inbound_scan' not in encode(queue.manager.notifications())


@pytest.mark.parametrize('guard', ['stop', 'update'], ids=['stopped-worker', 'applying-update'])
def test_closed_admission_does_not_advance_cursor_or_dispatch(queue, guard):
    channel = config(queue)
    add(queue, channel, 0)
    if guard == 'stop':
        queue.manager.stop()
    else:
        queue.service.update_manager.applying = True
    queue.manager.tick(poll=False)
    assert not queue.started and row(queue, channel, 0)['status'] == 'pending'
    assert queue.manager._checkpoint('_global', 'inbound_scan') is None


@pytest.mark.parametrize('committed', [False, True], ids=['unknown-without-run', 'unknown-after-admission'])
def test_unknown_dispatch_outcome_never_replays_a_start_and_restart_reconciles_identity(queue, committed):
    channel = config(queue)
    add(queue, channel, 0)

    def lost_reply(data):
        queue.started.append(dict(data))
        if committed:
            queue.store.create_run(data)
        raise RuntimeError('Synthetic lost outcome')

    queue.service.jobs.start = lost_reply
    queue.manager.tick(poll=False)
    assert row(queue, channel, 0)['status'] == 'outcome_unknown'
    restart(queue).tick(poll=False)
    queue.manager.tick(poll=False)
    assert len(queue.started) == 1
    assert row(queue, channel, 0)['status'] == ('dispatched' if committed else 'outcome_unknown')
