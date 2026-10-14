"""Read-only guard controls on disposable databases, never installed state."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
import sqlite3

import pytest

import forge_channel_queue as queue
import forge_channel_queue_guard as guard
import forge_updates as updates
from forge_store import ForgeStore


def forbidden(*args, **kwargs):
    raise AssertionError('Home, native and external access is forbidden.')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(Path, 'home', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def settle(store):
    helper = queue.InboundQueue(store)
    for _ in range(30):
        helper.maintenance()
        with store._connection() as db:
            if queue.ready(db) and db.execute('SELECT 1 FROM channel_queue_generations WHERE ready=0').fetchone() is None:
                return
    raise AssertionError('Synthetic recovery did not finish.')


def create(tmp_path, *, populated=True):
    store = ForgeStore(tmp_path / 'synthetic-workspace')
    if populated:
        store.save_entity('channels', {'id':'synthetic-channel','kind':'telegram','enabled':True,'allow_inbound':True})
        with store._connection(transaction='write') as db:
            for number in range(3):
                data = json.dumps({'message':{'chat':{'id':'42'},'from':{'id':'42'},'text':'Synthetic request'}})
                db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)', ('synthetic-channel',str(number),'pending',data,str(number),'synthetic'))
        settle(store)
    return store


def remove_group(store):
    with store._connection(transaction='write') as db:
        for kind in ('trigger','index','table'):
            for name, in db.execute('SELECT name FROM sqlite_master WHERE type=?', (kind,)).fetchall():
                if name in queue.SCHEMA:
                    db.execute('DROP ' + kind + ' ' + guard.quoted(name))


def mark_ready(db):
    db.execute('UPDATE channel_queue_generations SET ready=1,cursor=ceiling,verify_pending=0,verify_cursor=0')


@pytest.mark.parametrize('populated', [False, True])
def test_exact_ready_schema_excludes_only_cache_tables(tmp_path, populated):
    store = create(tmp_path, populated=populated)
    result = updates.database_guard(store.db_path)
    assert result['version'] == 2 and 'channel_queue_protection' not in result
    assert not queue.PRIVATE_TABLES & result['tables'].keys()
    assert {'channel_inbound','channel_routes','channel_callbacks','runs','messages','entities','settings'} <= result['tables'].keys()
    assert result['capabilities']['channel_queue']['required'] is True
    assert updates.database_guard(store.db_path, result) == result


def test_legacy_absent_and_ready_addition_keep_exact_projection(tmp_path):
    store = create(tmp_path, populated=False)
    remove_group(store)
    fresh = updates.database_guard(store.db_path)
    legacy = {'version':1,'tables':fresh['tables']}
    assert fresh['capabilities']['channel_queue']['required'] is False
    assert updates.database_guard(store.db_path, legacy) == legacy
    with store._connection(transaction='write') as db:
        queue.install(db)
    assert updates.database_guard(store.db_path, legacy) == legacy
    assert updates.database_guard(store.db_path, fresh) == fresh


def test_required_cache_disappearance_is_protected(tmp_path):
    store = create(tmp_path, populated=False)
    before = updates.database_guard(store.db_path)
    remove_group(store)
    after = updates.database_guard(store.db_path, before)
    assert after != before and after['channel_queue_protection']['state'] == 'missing'


def test_optional_capability_cannot_accept_closed_addition(tmp_path):
    store = create(tmp_path, populated=False)
    remove_group(store)
    before = updates.database_guard(store.db_path)
    with store._connection(transaction='write') as db:
        queue.install(db)
        queue.reset_recovery(db)
    protected = updates.database_guard(store.db_path, before)
    assert protected != before and protected['capabilities'] == before['capabilities']
    assert queue.PRIVATE_TABLES <= protected['tables'].keys()
    settle(store)
    assert updates.database_guard(store.db_path, before) == before


@pytest.mark.parametrize('mutation', [
    'closed','missing_global','missing_index','unknown_trigger','unknown_reference_trigger',
    'future_table','uppercase_table','future_view','generation_namespace','parent_column',
    'rowid_shadow','source_rowid','source_stamp','source_hash','missing_head','extra_head',
    'stale_generation','bad_format','bad_scalar','bad_cross_scalar','unclassified',
    'bad_config','unknown_kind','oversize_config','oversize_source',
])
def test_unknown_or_incomplete_state_never_receives_exclusion(tmp_path, mutation):
    store = create(tmp_path)
    before = updates.database_guard(store.db_path)
    with store._connection(transaction='write') as db:
        if mutation == 'closed': queue.reset_recovery(db)
        elif mutation == 'missing_global': db.execute('DELETE FROM channel_queue_generations WHERE scope=0')
        elif mutation == 'missing_index': db.execute('DROP INDEX channel_queue_route')
        elif mutation == 'unknown_trigger': db.execute('CREATE TRIGGER other_name AFTER INSERT ON channel_inbound_queue BEGIN SELECT 1; END')
        elif mutation == 'unknown_reference_trigger': db.execute('CREATE TRIGGER other_name AFTER INSERT ON messages BEGIN SELECT COUNT(*) FROM channel_inbound_queue; END')
        elif mutation == 'future_table': db.execute('CREATE TABLE channel_queue_future(unknown TEXT)')
        elif mutation == 'uppercase_table': db.execute('CREATE TABLE CHANNEL_QUEUE_FUTURE(unknown TEXT)')
        elif mutation == 'future_view': db.execute('CREATE VIEW differently_named AS SELECT * FROM channel_inbound_queue')
        elif mutation == 'generation_namespace': db.execute('CREATE TABLE channel_generation_future(unknown TEXT)')
        elif mutation == 'parent_column': db.execute('ALTER TABLE channel_routes ADD COLUMN unknown TEXT')
        elif mutation == 'rowid_shadow': db.execute('ALTER TABLE channel_inbound ADD COLUMN rowid INTEGER')
        else:
            db.execute('PRAGMA ignore_check_constraints=ON')
            if mutation == 'source_rowid': db.execute('UPDATE channel_inbound_queue SET queue_seq=queue_seq+100')
            elif mutation == 'source_stamp': db.execute("UPDATE channel_inbound_queue SET created_at='different'")
            elif mutation == 'source_hash': db.execute('UPDATE channel_inbound_queue SET data_hash=?', ('0'*64,))
            elif mutation == 'missing_head': db.execute('DELETE FROM channel_inbound_queue WHERE external_id=\'0\'')
            elif mutation == 'extra_head':
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute("UPDATE channel_inbound SET status='complete'")
                db.execute("INSERT INTO channel_inbound_queue VALUES('synthetic-channel','0',1,1,'0',1,'ordinary','42',?)", ('0'*64,))
            elif mutation == 'stale_generation': db.execute('UPDATE channel_inbound_queue SET generation=3')
            elif mutation == 'bad_format': db.execute('UPDATE channel_queue_generations SET format_version=2')
            elif mutation == 'bad_scalar': db.execute('UPDATE channel_queue_generations SET scan_seq=-1')
            elif mutation == 'bad_cross_scalar': db.execute("UPDATE channel_queue_generations SET scan_stamp='unpaired',scan_seq=0")
            elif mutation == 'unclassified': db.execute("UPDATE channel_inbound_queue SET lane='unclassified',recipient=NULL,data_hash=NULL")
            elif mutation in ('bad_config','unknown_kind','oversize_config'):
                value = '{invalid' if mutation == 'bad_config' else (json.dumps({'kind':'future'}) if mutation == 'unknown_kind' else ' ' * (queue.MAX_BODY+1))
                db.execute("UPDATE entities SET data=? WHERE kind='channels'", (value,))
                db.execute('UPDATE channel_queue_generations SET generation=1 WHERE scope=1')
            elif mutation == 'oversize_source': db.execute('UPDATE channel_inbound SET data=?', (' '*(queue.MAX_BODY+1),))
            mark_ready(db)
    after = updates.database_guard(store.db_path, before)
    assert after != before and 'channel_queue_protection' in after
    assert queue.PRIVATE_TABLES <= after['tables'].keys()
    assert not any('Synthetic request' in str(value) for value in after.values())


def test_unknown_empty_sql_group_cannot_use_additive_empty_table_exception(tmp_path):
    store = create(tmp_path, populated=False)
    remove_group(store)
    before = updates.database_guard(store.db_path)
    legacy = {'version':1,'tables':before['tables']}
    with store._connection(transaction='write') as db:
        db.execute('CREATE TABLE CHANNEL_QUEUE_FUTURE(private_data TEXT)')
        db.execute('CREATE TRIGGER channel_queue_future AFTER INSERT ON messages BEGIN SELECT 1; END')
    for baseline in (before, legacy):
        after = updates.database_guard(store.db_path, baseline)
        assert after != baseline and after['tables']['CHANNEL_QUEUE_FUTURE']['rows'] == 0
        assert set(after['channel_queue_protection']) == {'state','sql_sha256'}
        assert 'CREATE' not in str(after)


def test_protected_baseline_is_never_promoted_into_exclusion(tmp_path):
    store = create(tmp_path)
    with store._connection(transaction='write') as db: queue.reset_recovery(db)
    before = updates.database_guard(store.db_path)
    assert queue.PRIVATE_TABLES <= before['tables'].keys()
    settle(store)
    after = updates.database_guard(store.db_path, before)
    assert after != before and queue.PRIVATE_TABLES <= after['tables'].keys()
    assert 'channel_queue_protection' in after


def test_legacy_explicit_cache_and_selected_columns_remain_protected(tmp_path):
    store = create(tmp_path)
    with store._connection() as db:
        rows = [row[0] for row in db.execute('SELECT channel_id FROM channel_inbound_queue ORDER BY channel_id')]
    digest = sha256()
    for value in rows: digest.update(updates.encode([value]).encode()); digest.update(b'\n')
    legacy = {'version':1,'tables':{'channel_inbound_queue':{'columns':['channel_id'],'rows':len(rows),'sha256':digest.hexdigest()}}}
    result = updates.database_guard(store.db_path, legacy)
    assert result['tables']['channel_inbound_queue'] == legacy['tables']['channel_inbound_queue']
    assert result['channel_queue_protection']['state'] == 'legacy_uncertified'
    assert updates.database_guard(store.db_path, result) == result


def test_baseline_missing_table_and_columns_remain_visible(tmp_path):
    store = create(tmp_path, populated=False)
    baseline = {'version':1,'tables':{
        'synthetic_removed':{'columns':['original'],'rows':1,'sha256':'0'*64},
        'settings':{'columns':['removed_column'],'rows':1,'sha256':'0'*64}}}
    after = updates.database_guard(store.db_path, baseline)
    assert after['tables']['synthetic_removed'] == {'missing':True}
    assert after['tables']['settings'] == {'missing_columns':True}


@pytest.mark.parametrize('mutation', ['version','capability_version','hash','required','unknown_capability','explicit_without_protection'])
def test_unknown_or_inconsistent_capability_fails_with_fixed_reason(tmp_path, mutation):
    store = create(tmp_path)
    before = updates.database_guard(store.db_path)
    if mutation == 'version': before['version'] = 3
    elif mutation == 'capability_version': before['capabilities']['channel_queue']['version'] = True
    elif mutation == 'hash': before['capabilities']['channel_queue']['schema_sha256'] = '0'*64
    elif mutation == 'required': before['capabilities']['channel_queue']['required'] = 1
    elif mutation == 'unknown_capability': before['capabilities']['future'] = {}
    else: before['tables']['channel_inbound_queue'] = {'columns':['channel_id'],'rows':0,'sha256':'0'*64}
    with pytest.raises(guard.GuardFailure, match='^'+re_escape(guard.FAILURE)+'$'):
        updates.database_guard(store.db_path, before)


def re_escape(value):
    import re
    return re.escape(value)


@pytest.mark.parametrize('table,sql', [
    ('messages', "INSERT INTO messages(chat_id,role,content,created_at) VALUES('synthetic','user','private synthetic text','now')"),
    ('settings', "INSERT INTO settings VALUES('synthetic',1)"),
    ('entities', "INSERT INTO entities VALUES('providers','synthetic','{}')"),
    ('channel_checkpoints', "INSERT INTO channel_checkpoints VALUES('synthetic','offset','1')"),
    ('channel_replays', "INSERT INTO channel_replays VALUES('synthetic','nonce',1)"),
    ('channel_outbox', "INSERT INTO channel_outbox VALUES('synthetic','synthetic','42','reply','{}','pending',0,0,NULL,NULL,'now','now')"),
    ('channel_notifications', "INSERT INTO channel_notifications VALUES('synthetic',NULL,'reply','{}',1,'now')"),
    ('channel_event_journal', "INSERT INTO channel_event_journal(run_id,seq) VALUES('synthetic',1)"),
])
def test_authoritative_state_is_hashed_even_when_cache_ready(tmp_path, table, sql):
    store = create(tmp_path, populated=False)
    before = updates.database_guard(store.db_path)
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute(sql)
    after = updates.database_guard(store.db_path, before)
    assert after != before and after['tables'][table] != before['tables'][table]


def test_only_existing_updater_bookkeeping_is_excluded(tmp_path):
    store = create(tmp_path, populated=False)
    before = updates.database_guard(store.db_path)
    store.save_entity('updates', {'id':'synthetic','state':'installed'})
    store.save_entity('update_trust', {'id':'synthetic','state':'fixture'})
    assert updates.database_guard(store.db_path, before) == before


def test_internal_table_prefix_is_literal_and_cannot_hide_user_work(tmp_path):
    store = create(tmp_path, populated=False)
    before = updates.database_guard(store.db_path)
    with store._connection(transaction='write') as db:
        db.execute('CREATE TABLE sqliteX_user_work(content TEXT)')
        db.execute("INSERT INTO sqliteX_user_work VALUES('synthetic newly created work')")
    after = updates.database_guard(store.db_path, before)
    assert after != before and after['tables']['sqliteX_user_work']['rows'] == 1
    legacy = {'version':1,'tables':before['tables']}
    assert updates.database_guard(store.db_path, legacy) != legacy
    assert 'sqlite_sequence' not in after['tables'] and 'forge_migrations' not in after['tables']


@pytest.mark.parametrize('table', ['projects','chats','runs','run_events','invocations','approvals','usage','occurrences',
    'request_keys','provider_turn_state','provider_run_bindings','provider_authority_revisions','channel_callbacks','channel_routes'])
def test_remaining_authoritative_families_cannot_be_mistaken_for_cache(tmp_path, table):
    store = create(tmp_path, populated=False)
    before = updates.database_guard(store.db_path)
    # Synthetic scalar rows deliberately bypass application validation: the guard
    # must preserve even malformed authoritative work, rather than excluding it.
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        columns = db.execute('PRAGMA table_info(' + table + ')').fetchall()
        values = [1 if row[2] in ('INTEGER','REAL') else 'synthetic' for row in columns]
        db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in values)+')', values)
    after = updates.database_guard(store.db_path, before)
    assert after != before and after['tables'][table] != before['tables'][table]


def test_raw_sql_change_alone_changes_protected_descriptor(tmp_path):
    store = create(tmp_path, populated=False)
    with store._connection(transaction='write') as db:
        db.execute('CREATE TRIGGER differently_named AFTER INSERT ON channel_inbound_queue BEGIN SELECT 1; END')
    before = updates.database_guard(store.db_path)
    with store._connection(transaction='write') as db:
        db.execute('DROP TRIGGER differently_named')
        db.execute('CREATE TRIGGER differently_named AFTER INSERT ON channel_inbound_queue BEGIN SELECT 2; END')
    after = updates.database_guard(store.db_path, before)
    assert after['tables'] == before['tables']
    assert after['channel_queue_protection'] != before['channel_queue_protection']


def test_scalar_audit_reads_rows_instead_of_trusting_partial_index_contents(tmp_path):
    store = create(tmp_path)
    before = updates.database_guard(store.db_path)
    # A damaged partial index can advertise the expected DDL while omitting a
    # bad row. The lightweight runtime validator is deliberately insufficient.
    with store._connection(transaction='write') as db:
        db.execute('DROP INDEX channel_generation_bad_scalar')
        db.execute('CREATE INDEX channel_generation_bad_scalar ON channel_queue_generations(format_version) WHERE 0')
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE channel_queue_generations SET scan_seq=-1 WHERE scope=0')
        db.execute('PRAGMA writable_schema=ON')
        db.execute("UPDATE sqlite_master SET sql=? WHERE name='channel_generation_bad_scalar'", (queue.SCHEMA['channel_generation_bad_scalar'],))
        version = db.execute('PRAGMA schema_version').fetchone()[0]
        db.execute('PRAGMA schema_version='+str(version+1))
    with store._connection() as db: queue.validate_schema(db)
    after = updates.database_guard(store.db_path, before)
    assert after != before and 'channel_queue_protection' in after


def test_reader_is_read_only_one_snapshot_and_closes(tmp_path, monkeypatch):
    store = create(tmp_path)
    connect = sqlite3.connect
    seen, connections = [], []
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE,
              sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH}
    def read_only(path, **kwargs):
        assert path.endswith('?mode=ro') and kwargs == {'uri':True,'timeout':0}
        db = connect(path, **kwargs)
        db.set_trace_callback(seen.append)
        db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
        connections.append(db)
        return db
    monkeypatch.setattr(updates.sqlite3, 'connect', read_only)
    updates.database_guard(store.db_path)
    assert seen.count('BEGIN') == 1
    with pytest.raises(sqlite3.ProgrammingError): connections[0].execute('SELECT 1')


def test_source_semantics_stream_in_batches_of_64(tmp_path, monkeypatch):
    store = create(tmp_path)
    with store._connection(transaction='write') as db:
        data = db.execute('SELECT data FROM channel_inbound LIMIT 1').fetchone()[0]
        for number in range(3, 133):
            db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)', ('synthetic-channel',str(number),'pending',data,str(number),'synthetic'))
    settle(store)
    connect, sizes, classifications = sqlite3.connect, [], []
    classify = queue.classify
    class Cursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.source = sql.startswith('SELECT h.queue_seq,')
            return super().execute(sql, parameters)
        def fetchmany(self, size=1):
            if self.source: sizes.append(size)
            return super().fetchmany(size)
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=Cursor).execute(sql, parameters)
    monkeypatch.setattr(updates.sqlite3, 'connect', lambda path, **kwargs:connect(path, factory=Connection, **kwargs))
    def classification(*args):
        classifications.append(1)
        return classify(*args)
    monkeypatch.setattr(queue, 'classify', classification)
    result = updates.database_guard(store.db_path)
    assert 'channel_queue_protection' not in result
    assert sizes == [64,64,64,64] and len(classifications) == 133


@pytest.mark.parametrize('stop', ['cancel','deadline'])
def test_public_guard_interrupts_sql_inside_semantic_join(tmp_path, monkeypatch, stop):
    store = create(tmp_path)
    connect, active, clock, checks = sqlite3.connect, [False], [0.0], [0]
    monkeypatch.setattr(guard.time, 'monotonic', lambda:clock[0])
    def cancel():
        if active[0]:
            checks[0] += 1
            if checks[0] >= 2:
                if stop == 'cancel': return True
                clock[0] = 5
        return False
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('SELECT h.queue_seq,'):
                active[0] = True
                super().execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(a.x+b.x) FROM n a,n b').fetchone()
            return super().execute(sql, parameters)
    monkeypatch.setattr(updates.sqlite3, 'connect', lambda path, **kwargs:connect(path, factory=Connection, **kwargs))
    with pytest.raises(guard.GuardFailure) as failure:
        updates.database_guard(store.db_path, cancel=cancel, deadline=5 if stop == 'deadline' else None)
    assert active[0] and str(failure.value) == guard.FAILURE


def test_missing_path_is_not_created(tmp_path):
    path = tmp_path/'missing.sqlite3'
    with pytest.raises(guard.GuardFailure): updates.database_guard(path)
    assert not path.exists()


@pytest.mark.parametrize('target', ['channel_inbound','entities'])
def test_invalid_utf8_never_leaks_raw_decoder_details(tmp_path, target):
    store = create(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute('UPDATE '+target+" SET data=CAST(x'80' AS TEXT)")
        mark_ready(db)
    with pytest.raises(guard.GuardFailure) as failure: updates.database_guard(store.db_path)
    assert str(failure.value) == guard.FAILURE


@pytest.mark.parametrize('target', ['channel_inbound','entities'])
def test_deep_json_is_protected_without_decoder_escape(tmp_path, monkeypatch, target):
    store = create(tmp_path)
    before = updates.database_guard(store.db_path)
    nested = '['*1100+'0'+']'*1100
    with store._connection(transaction='write') as db:
        db.execute('UPDATE '+target+' SET data=?', (nested,))
        data = db.execute('SELECT data FROM channel_inbound LIMIT 1').fetchone()[0]
        db.execute("UPDATE channel_inbound_queue SET lane='ordinary',recipient='42',data_hash=?", (sha256(data.encode()).hexdigest(),))
        db.execute('UPDATE channel_queue_generations SET generation=1 WHERE scope=1')
        mark_ready(db)
    loads, seen = json.loads, []
    def decode(value, *args, **kwargs):
        seen.append(value == nested)
        return loads(value, *args, **kwargs)
    monkeypatch.setattr(guard.json, 'loads', decode)
    after = updates.database_guard(store.db_path, before)
    assert after != before and 'channel_queue_protection' in after and any(seen)


def test_locked_database_fails_closed_without_busy_wait(tmp_path):
    path = tmp_path/'synthetic-locked.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,content TEXT)')
        db.commit()
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(guard.GuardFailure) as failure: updates.database_guard(path)
        assert str(failure.value) == guard.FAILURE


def test_sql_progress_cancels_long_join_with_fixed_reason(tmp_path):
    store = create(tmp_path, populated=False)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls > 30
    budget = guard.GuardBudget(cancel)
    with closing(sqlite3.connect(store.db_path)) as db:
        db.set_progress_handler(budget.progress, 10)
        with pytest.raises(sqlite3.OperationalError):
            db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(a.x+b.x) FROM n a,n b').fetchone()
    with pytest.raises(guard.GuardFailure): budget.check()


def test_default_audit_deadline_does_not_extend_explicit_deadline():
    clock = [100.0]
    budget = guard.GuardBudget(deadline=102, clock=lambda:clock[0])
    assert budget.audit_deadline == 102
    budget.auditing = True; clock[0] = 102
    with pytest.raises(guard.GuardFailure): budget.check()


def test_default_deadline_applies_to_whole_semantic_audit(tmp_path, monkeypatch):
    store = create(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(guard.time, 'monotonic', lambda:clock[0])
    classify = queue.classify
    def slow(*args):
        clock[0] = 11
        return classify(*args)
    monkeypatch.setattr(queue, 'classify', slow)
    with pytest.raises(guard.GuardFailure): updates.database_guard(store.db_path)


def test_absent_group_discovery_must_finish_within_audit_budget(tmp_path, monkeypatch):
    store = create(tmp_path, populated=False)
    remove_group(store)
    clock = [0.0]
    monkeypatch.setattr(guard.time, 'monotonic', lambda:clock[0])
    objects = guard._objects
    def slow_discovery(*args):
        result = objects(*args)
        clock[0] = 11
        return result
    monkeypatch.setattr(guard, '_objects', slow_discovery)
    with pytest.raises(guard.GuardFailure): updates.database_guard(store.db_path)


@pytest.mark.parametrize('stop', ['cancel','deadline','default_only'])
def test_later_authoritative_hashing_honors_caller_limits(tmp_path, monkeypatch, stop):
    store = create(tmp_path)
    clock, stopped = [0.0], [False]
    monkeypatch.setattr(guard.time, 'monotonic', lambda:clock[0])
    encode = updates.encode
    def hash_row(value):
        clock[0] = 20
        stopped[0] = True
        return encode(value)
    monkeypatch.setattr(updates, 'encode', hash_row)
    kwargs = {'cancel':lambda:stopped[0]} if stop == 'cancel' else ({'deadline':5} if stop == 'deadline' else {})
    if stop == 'default_only':
        assert updates.database_guard(store.db_path, **kwargs)['version'] == 2
    else:
        with pytest.raises(guard.GuardFailure): updates.database_guard(store.db_path, **kwargs)
