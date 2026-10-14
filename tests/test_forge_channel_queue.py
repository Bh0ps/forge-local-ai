"""Private derived queue controls on disposable SQLite; no worker or external I/O."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

import forge_channel_queue as queued


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE entities(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE chats(id TEXT PRIMARY KEY,project_id TEXT,archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE channel_inbound(channel_id TEXT NOT NULL,external_id TEXT NOT NULL,status TEXT NOT NULL,
                data TEXT NOT NULL,run_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(channel_id,external_id));
            CREATE TABLE channel_routes(channel_id TEXT NOT NULL,recipient TEXT NOT NULL,chat_id TEXT NOT NULL,
                project_id TEXT,PRIMARY KEY(channel_id,recipient));
        ''')

    @contextmanager
    def _connection(self, transaction=None):
        if transaction == 'write':
            self.db.execute('BEGIN')
        try:
            yield self.db
        except BaseException:
            if transaction == 'write':
                self.db.rollback()
            raise
        else:
            if transaction == 'write':
                self.db.commit()


@pytest.fixture
def queue():
    store = MemoryStore()
    add_config(store.db)
    with store._connection(transaction='write') as db:
        queued.install(db)
    helper = queued.InboundQueue(store)
    try:
        yield SimpleNamespace(store=store, db=store.db, helper=helper)
    finally:
        store.db.close()


def add_config(db, channel='synthetic-channel', **extra):
    data = dict(kind='telegram', enabled=True, allow_inbound=True, project_id=None)
    data.update(extra)
    db.execute('INSERT INTO entities VALUES(?,?,?)', ('channels', channel, json.dumps(data)))


def insert(db, number, *, channel='synthetic-channel', recipient='42', text='Synthetic request', stamp=None):
    payload = {'message': {'chat': {'id': recipient}, 'from': {'id': recipient}, 'text': text}}
    db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)',
        (channel, str(number), 'pending', json.dumps(payload), stamp or f'{number:06}', 'synthetic'))


def settle(queue, rounds=20):
    counts = []
    for _ in range(rounds):
        queue.helper.maintenance()
        counts.append(queue.helper.last_work['maintenance_payloads'])
        dirty = queue.db.execute('SELECT 1 FROM channel_queue_generations WHERE ready=0 LIMIT 1').fetchone()
        if dirty is None:
            break
    assert dirty is None
    return counts


def items(queue):
    return [tuple(row) for row in queue.helper.candidates()]


@pytest.mark.parametrize('boundary',['receive','maintenance','selection'])
@pytest.mark.parametrize('body',['oversize','blob','invalid_utf8'])
def test_original_payload_validation_bounds_transfer_and_fails_closed(queue,monkeypatch,boundary,body):
    insert(queue.db,1)
    settle(queue)
    item=items(queue)[0]
    corrupt={'oversize':'x'*(queued.MAX_BODY+1),'blob':b'synthetic','invalid_utf8':b'\xff'}[body]
    expression='CAST(? AS TEXT)' if body=='invalid_utf8' else '?'
    queue.db.execute('UPDATE channel_inbound SET data='+expression+' WHERE external_id=\'1\'',(corrupt,))
    before=queue.db.execute('SELECT CAST(data AS BLOB),status FROM channel_inbound').fetchone()
    def bounded_text(value):
        assert len(value)<=queued.MAX_BODY,'An oversized original payload reached Python.'
        return value.decode('utf-8')
    queue.db.text_factory=bounded_text
    monkeypatch.setattr(queued,'classify',lambda *args:pytest.fail('An invalid original payload reached classification.'))
    with pytest.raises(queued.QueueUnavailable) as failure:
        if boundary=='receive':queued.record_insert(queue.db,'synthetic-channel','1')
        elif boundary=='maintenance':queue.helper.maintenance()
        else:queue.helper.record(queue.db,item)
    assert type(failure.value) is queued.QueueUnavailable
    assert str(failure.value)==queued.SAFE_REASONS['metadata_unavailable']
    assert tuple(queue.db.execute('SELECT CAST(data AS BLOB),status FROM channel_inbound').fetchone())==tuple(before)


def test_trigger_only_inserts_beyond_initial_ceiling_are_all_classified(queue):
    assert queue.helper.maintenance()
    insert(queue.db, 1)
    insert(queue.db, 2)
    settle(queue)
    assert [row['lane'] for row in queue.db.execute('SELECT lane FROM channel_inbound_queue ORDER BY queue_seq')] == ['ordinary', 'ordinary']


def alias_query_cost(queue, count):
    queue.db.execute("INSERT INTO chats VALUES('synthetic-chat',NULL,0)")
    for number in range(count):
        queue.db.execute('INSERT INTO channel_routes VALUES(?,?,?,NULL)',
            ('synthetic-channel', str(number), 'synthetic-chat'))
    insert(queue.db, 1, recipient='0')
    settle(queue)
    item = items(queue)[0]
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    queue.db.set_progress_handler(progress, 1)
    try:
        assert not queue.helper.eligible(queue.db, item, {})
    finally:
        queue.db.set_progress_handler(None, 0)
    return steps


def test_alias_overflow_selection_does_not_sort_or_scan_all_matching_routes():
    costs = []
    for count in (65, 1000, 10000):
        store = MemoryStore()
        try:
            add_config(store.db)
            queued.install(store.db)
            queue = SimpleNamespace(db=store.db, helper=queued.InboundQueue(store))
            costs.append(alias_query_cost(queue, count))
            assert queue.helper.last_work['alias_metadata'] == 65
        finally:
            store.db.close()
    assert max(costs) < 2500, costs
    assert max(costs) - min(costs) < 200, costs


@pytest.mark.parametrize('table', ['channel_inbound_queue', 'channel_queue_generations'])
def test_exact_ddl_with_unsupported_row_format_is_unavailable(queue, table):
    insert(queue.db, 1)
    settle(queue)
    queue.db.execute('PRAGMA ignore_check_constraints=ON')
    queue.db.execute(f'UPDATE {table} SET format_version=2')
    queue.db.execute('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(queued.QueueUnavailable) as caught:
        queued.validate_schema(queue.db)
    assert caught.value.reason == 'metadata_unavailable'


@pytest.mark.parametrize('change', ['edit', 'reenter', 'reuse_rowid'])
def test_legacy_change_behind_persisted_cursor_is_reclassified(queue, change):
    insert(queue.db, 1)
    insert(queue.db, 2)
    settle(queue)
    if change == 'edit':
        queue.db.execute("UPDATE channel_inbound SET data=? WHERE external_id='1'",
            (json.dumps({'message': {'chat': {'id': '42'}, 'from': {'id': '42'}, 'text': '/pause'}}),))
        expected = 'control'
    elif change == 'reenter':
        queue.db.execute("UPDATE channel_inbound SET status='cancelled' WHERE external_id='1'")
        queue.db.execute("UPDATE channel_inbound SET status='pending' WHERE external_id='1'")
        expected = 'ordinary'
    else:
        queue.db.execute('DELETE FROM channel_inbound')
        insert(queue.db, 3)
        assert queue.db.execute('SELECT rowid FROM channel_inbound').fetchone()[0] == 1
        expected = 'ordinary'
    state = queue.db.execute("SELECT cursor,ready FROM channel_queue_generations WHERE scope=1").fetchone()
    assert tuple(state) == (0, 0)
    settle(queue)
    assert queue.db.execute('SELECT lane FROM channel_inbound_queue ORDER BY queue_seq').fetchone()[0] == expected


def test_legacy_insert_extends_changed_configuration_generation_without_resetting_it(queue):
    insert(queue.db, 1)
    settle(queue)
    queue.db.execute("UPDATE entities SET data=? WHERE kind='channels'",
        (json.dumps(dict(kind='telegram', enabled=True, allow_inbound=True, project_id='synthetic-project')),))
    insert(queue.db, 2)
    state = queue.db.execute('SELECT generation,ceiling,ready FROM channel_queue_generations WHERE scope=1').fetchone()
    assert tuple(state) == (2, 2, 0)
    before = items(queue)
    assert not queue.helper.eligible(queue.db, before[0], {})
    settle(queue)
    assert [item[4] for item in items(queue)] == [2, 2]


def test_payload_classification_is_bounded_and_restart_resumes_backfill(queue, monkeypatch):
    assert queue.helper.maintenance()
    for number in range(450):
        insert(queue.db, number)
    decoded = []
    classify = queued.classify
    monkeypatch.setattr(queued, 'classify', lambda data, config: (decoded.append(data), classify(data, config))[1])
    queue.helper.maintenance()
    assert len(decoded) == queued.MAINTENANCE_BUDGET == 150
    queue.helper = queued.InboundQueue(queue.store)
    counts = settle(queue)
    assert counts == [150, 150] and len(decoded) == 450
    assert max(counts) <= queued.PAYLOAD_BUDGET
    assert queue.db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='ordinary'").fetchone()[0] == 450


@pytest.mark.parametrize('stop_at', [1, 2, 4])
def test_closed_admission_rolls_back_classification_and_cursor(queue, stop_at):
    assert queue.helper.maintenance()
    for number in range(3):
        insert(queue.db, number)
    calls = 0
    def admitted():
        nonlocal calls
        calls += 1
        return calls < stop_at
    with pytest.raises(queued.QueueUnavailable) as caught:
        queue.helper.maintenance(admitted=admitted)
    assert caught.value.reason == 'rebuilding'
    assert queue.db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='unclassified'").fetchone()[0] == 3
    assert tuple(queue.db.execute('SELECT cursor,ready FROM channel_queue_generations WHERE scope=1').fetchone()) == (0, 0)


@pytest.mark.parametrize('recipient', ['00042', '12345678901234567890', '-12345678901234567890'])
def test_exact_recipient_identity_and_control_fifo_survive_ties_and_restart(queue, recipient):
    insert(queue.db, 1, recipient=recipient, stamp='same')
    insert(queue.db, 2, recipient=recipient, text='Plain answer stays ordinary', stamp='same')
    insert(queue.db, 3, recipient=recipient, text='/pause', stamp='same')
    insert(queue.db, 4, recipient=recipient, text='/resume', stamp='same')
    settle(queue)
    queue.helper = queued.InboundQueue(queue.store)
    batch = items(queue)
    assert [item[6] for item in batch] == [recipient] * 4
    heads = {}
    assert [queue.helper.eligible(queue.db, item, heads) for item in batch] == [True, False, True, False]
    queue.db.execute("UPDATE channel_inbound SET status='dispatched' WHERE external_id='3'")
    heads = {}
    batch = items(queue)
    assert [queue.helper.eligible(queue.db, item, heads) for item in batch] == [True, False, True]


def test_candidate_rotation_wraps_with_fixed_metadata_limit_and_durable_identity(queue):
    for number in range(450):
        insert(queue.db, number)
    settle(queue)
    first = items(queue)
    assert len(first) == queued.CANDIDATE_BUDGET == 200
    queue.helper.advance(queue.db, first[-1])
    queue.helper = queued.InboundQueue(queue.store)
    second = items(queue)
    queue.helper.advance(queue.db, second[-1])
    third = items(queue)
    assert [item[1] for item in second] == [str(number) for number in range(200, 400)]
    assert [item[1] for item in third] == [str(number) for number in range(400, 450)] + [str(number) for number in range(150)]
    assert len({(item[0], item[1]) for item in third}) == 200


def test_alias_overflow_holds_only_its_destination_and_head_queries_use_indexes(queue):
    queue.db.execute("INSERT INTO chats VALUES('overflow-chat',NULL,0)")
    queue.db.execute("INSERT INTO chats VALUES('other-chat',NULL,0)")
    for number in range(65):
        queue.db.execute('INSERT INTO channel_routes VALUES(?,?,?,NULL)',
            ('synthetic-channel', str(number), 'overflow-chat'))
    queue.db.execute("INSERT INTO channel_routes VALUES('synthetic-channel','999','other-chat',NULL)")
    insert(queue.db, 1, recipient='0')
    insert(queue.db, 2, recipient='999')
    settle(queue)
    heads = {}
    batch = items(queue)
    assert not queue.helper.eligible(queue.db, batch[0], heads)
    assert queue.helper.reason == 'alias_limit'
    assert queue.helper.eligible(queue.db, batch[1], heads)
    plans = [row[3] for row in queue.db.execute('EXPLAIN QUERY PLAN SELECT channel_id,recipient,project_id FROM channel_routes INDEXED BY channel_queue_chat WHERE chat_id=? LIMIT ?', ('overflow-chat', 65))]
    assert any('channel_queue_chat' in detail for detail in plans) and not any('TEMP B-TREE' in detail for detail in plans)


def test_valid_cross_channel_aliases_share_one_ordinary_fifo(queue):
    add_config(queue.db, 'second-channel')
    queue.db.execute("INSERT INTO chats VALUES('shared-chat',NULL,0)")
    queue.db.execute("INSERT INTO channel_routes VALUES('synthetic-channel','42','shared-chat',NULL)")
    queue.db.execute("INSERT INTO channel_routes VALUES('second-channel','00042','shared-chat',NULL)")
    insert(queue.db, 1)
    insert(queue.db, 2, channel='second-channel', recipient='00042')
    settle(queue)
    assert [queue.helper.eligible(queue.db, item, {}) for item in items(queue)] == [True, False]


@pytest.mark.parametrize('operation', ['maintenance', 'candidates', 'eligible', 'record', 'advance', 'record_insert'])
@pytest.mark.parametrize('table', ['channel_inbound_queue', 'channel_queue_generations'])
def test_unsupported_format_blocks_every_helper_boundary_without_changing_journal(queue, table, operation):
    insert(queue.db, 1)
    settle(queue)
    item = items(queue)[0]
    before = [tuple(row) for row in queue.db.execute('SELECT * FROM channel_inbound')]
    queue.db.execute('PRAGMA ignore_check_constraints=ON')
    queue.db.execute(f'UPDATE {table} SET format_version=2')
    queue.db.execute('PRAGMA ignore_check_constraints=OFF')
    actions = dict(maintenance=lambda: queue.helper.maintenance(), candidates=lambda: queue.helper.candidates(),
        eligible=lambda: queue.helper.eligible(queue.db, item, {}), record=lambda: queue.helper.record(queue.db, item),
        advance=lambda: queue.helper.advance(queue.db, item),
        record_insert=lambda: queued.record_insert(queue.db, 'synthetic-channel', '1'))
    with pytest.raises(queued.QueueUnavailable) as caught:
        actions[operation]()
    assert caught.value.reason == 'metadata_unavailable'
    assert [tuple(row) for row in queue.db.execute('SELECT * FROM channel_inbound')] == before


def test_bad_format_probe_uses_empty_partial_index_without_scanning_valid_backlog(queue):
    for number in range(450):
        insert(queue.db, number)
    settle(queue)
    sql = "SELECT 1 FROM channel_inbound_queue INDEXED BY channel_queue_bad_format WHERE typeof(format_version)!='integer' OR format_version!=1 LIMIT 1"
    plan = [row[3] for row in queue.db.execute('EXPLAIN QUERY PLAN ' + sql)]
    assert any('channel_queue_bad_format' in detail for detail in plan)
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    queue.db.set_progress_handler(progress, 1)
    try:
        assert queue.db.execute(sql).fetchone() is None
    finally:
        queue.db.set_progress_handler(None, 0)
    assert steps < 20


@pytest.mark.parametrize('status', ['dispatching', 'outcome_unknown', 'dispatched'])
def test_original_journal_terminal_or_unknown_status_is_never_replayed_by_queue_recovery(queue, status):
    insert(queue.db, 1)
    settle(queue)
    item = items(queue)[0]
    queue.db.execute('UPDATE channel_inbound SET status=? WHERE external_id=?', (status, '1'))
    queued.record_insert(queue.db, 'synthetic-channel', '1')
    queue.helper.maintenance()
    assert items(queue) == [] and queue.helper.record(queue.db, item) is None
    assert queue.db.execute('SELECT status FROM channel_inbound').fetchone()[0] == status


@pytest.mark.parametrize('format_version', [sqlite3.Binary(b'1'), 1.5])
def test_noninteger_format_value_cannot_receive_supported_metadata_validation(queue, format_version):
    insert(queue.db, 1)
    settle(queue)
    queue.db.execute('PRAGMA ignore_check_constraints=ON')
    queue.db.execute('UPDATE channel_inbound_queue SET format_version=?', (format_version,))
    queue.db.execute('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(queued.QueueUnavailable):
        queued.validate_schema(queue.db)


def test_original_record_binding_rejects_derived_identity_tampering_and_keeps_payload_private(queue):
    text = 'Synthetic content that never enters derived queue metadata'
    insert(queue.db, 1, text=text)
    settle(queue)
    item = items(queue)[0]
    original = dict(queue.helper.record(queue.db, item))
    assert original['external_id'] == '1' and text in original['data']
    assert text not in repr([tuple(row) for row in queue.db.execute('SELECT * FROM channel_inbound_queue')])
    changed = (*item[:2], item[2] + 1, *item[3:])
    with pytest.raises(queued.QueueUnavailable):
        queue.helper.record(queue.db, changed)
    assert dict(queue.db.execute('SELECT rowid AS queue_rowid,* FROM channel_inbound').fetchone()) == original


@pytest.mark.parametrize('foreign_keys', [False, True])
def test_legacy_replace_with_nonpending_row_removes_derived_head_without_recursive_triggers(queue, foreign_keys):
    queue.db.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')
    assert queue.db.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
    insert(queue.db, 1)
    settle(queue)
    values = list(queue.db.execute('SELECT * FROM channel_inbound').fetchone())
    values[2] = 'outcome_unknown'
    queue.db.execute('INSERT OR REPLACE INTO channel_inbound VALUES(?,?,?,?,?,?,?,?)', values)
    assert items(queue) == []
    queue.helper.maintenance()
    assert queue.db.execute('SELECT status FROM channel_inbound').fetchone()[0] == 'outcome_unknown'


def test_legacy_replace_with_pending_row_rebinds_current_source_and_reclassifies(queue):
    insert(queue.db, 1)
    settle(queue)
    before = items(queue)[0]
    values = list(queue.db.execute('SELECT * FROM channel_inbound').fetchone())
    values[3] = json.dumps({'message': {'chat': {'id': '42'}, 'from': {'id': '42'}, 'text': '/pause'}})
    queue.db.execute('INSERT OR REPLACE INTO channel_inbound VALUES(?,?,?,?,?,?,?,?)', values)
    settle(queue)
    current = items(queue)[0]
    assert current[5] == 'control' and current[7] != before[7]
    assert queue.helper.record(queue.db, current)['data'] == values[3]


def test_ignored_duplicate_sql_insert_keeps_original_derived_identity_and_payload(queue):
    insert(queue.db, 1)
    settle(queue)
    before = items(queue)[0]
    queue.db.execute("INSERT OR IGNORE INTO channel_inbound VALUES('synthetic-channel','1','outcome_unknown','{}',NULL,NULL,'other','other')")
    assert items(queue) == [before]
    assert queue.helper.record(queue.db, before)['status'] == 'pending'


def test_verification_reclassification_shares_hint_classification_allowance_with_receive(queue,monkeypatch):
    # Trigger-only rows can exist above the original initialization ceiling.
    queue.db.execute("UPDATE channel_queue_generations SET ready=1,verify_pending=1,verify_cursor=0 WHERE scope=0")
    for number in range(149):insert(queue.db,number)
    calls=[]
    classify=queued.classify
    monkeypatch.setattr(queued,'classify',lambda *args:(calls.append(True),classify(*args))[1])
    budget=queued.InboundWorkBudget()
    queue.helper.maintenance(budget=budget)
    for number in range(149,199):
        insert(queue.db,number)
        queued.record_insert(queue.db,'synthetic-channel',str(number),budget)
    assert len(calls)==budget.classifications==150
    assert budget.payloads<=200
    assert queue.db.execute("SELECT COUNT(*) FROM channel_inbound_queue WHERE lane='unclassified'").fetchone()[0]==49
