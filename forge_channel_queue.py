"""Private, derived pending-channel index; it grants no dispatch authority.

Only Store installs this additive schema before workers start. SQL triggers use
built-ins so schema7 clients can still write the original journal after rollback.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import sqlite3

PAYLOAD_BUDGET = 200
MAINTENANCE_BUDGET = 150
CANDIDATE_BUDGET = 200
DISPATCH_BUDGET = 50
ALIAS_LIMIT = 64
MAX_BODY = 65536
MAX_INT = 9223372036854775807
# Validate the original type/byte size inside SQLite before transferring a body.
# The source journal remains intact when a legacy or damaged row is unusable.
_SOURCE_DATA = f"CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<={MAX_BODY} THEN data END AS data"
PRIVATE_TABLES = frozenset({'channel_inbound_queue', 'channel_queue_generations'})
SAFE_REASONS = {
    'metadata_unavailable': 'Channel queue metadata is unavailable. Pending messages are retained; repair or restore the verified workspace before continuing.',
    'alias_limit': 'This chat has too many connected channel routes. Reduce its routes locally before continuing; pending messages are retained.',
    'rebuilding': 'Channel queue metadata is being rebuilt. Pending messages are retained.',
}


class QueueUnavailable(ValueError):
    def __init__(self, reason='metadata_unavailable'):
        self.reason = reason if reason in SAFE_REASONS else 'metadata_unavailable'
        super().__init__(SAFE_REASONS[self.reason])


class QueueDeferred(QueueUnavailable):
    def __init__(self):
        super().__init__('rebuilding')


class InboundWorkBudget:
    """Counts original payload work, including repeated decodes and final claims."""
    def __init__(self):
        self.payloads = self.classifications = self.dispatches = 0
        self.by_kind = {}

    def payload(self, kind, *, classification=False):
        if self.payloads >= PAYLOAD_BUDGET or classification and self.classifications >= MAINTENANCE_BUDGET:
            raise QueueDeferred()
        self.payloads += 1
        self.classifications += int(classification)
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def dispatch(self):
        if self.dispatches >= DISPATCH_BUDGET:
            raise QueueDeferred()
        self.dispatches += 1


class ChannelAdmission(tuple):
    """In-process only; never stored in request data or constructed from JSON."""
    def __new__(cls, channel, external, validator, *, prepare=None, complete=None):
        value = super().__new__(cls, (channel, external))
        value.validate = validator
        value.prepare, value.complete = prepare, complete
        return value


SCHEMA = {
    'channel_queue_generations': """CREATE TABLE channel_queue_generations(
        scope INTEGER NOT NULL CHECK(typeof(scope)='integer' AND scope IN (0,1)),
        channel_id TEXT NOT NULL CHECK(typeof(channel_id)='text' AND
            ((scope=0 AND channel_id='') OR (scope=1 AND length(channel_id) BETWEEN 1 AND 65536))),
        format_version INTEGER NOT NULL CHECK(typeof(format_version)='integer' AND format_version=1),
        generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 9223372036854775807),
        cursor INTEGER NOT NULL CHECK(typeof(cursor)='integer' AND cursor BETWEEN 0 AND 9223372036854775807),
        ceiling INTEGER NOT NULL CHECK(typeof(ceiling)='integer' AND ceiling BETWEEN 0 AND 9223372036854775807),
        ready INTEGER NOT NULL CHECK(typeof(ready)='integer' AND ready IN (0,1)),
        scan_stamp TEXT NOT NULL CHECK(typeof(scan_stamp)='text' AND length(scan_stamp)<=100),
        scan_seq INTEGER NOT NULL CHECK(typeof(scan_seq)='integer' AND scan_seq BETWEEN 0 AND 9223372036854775807),
        maintenance_channel TEXT NOT NULL CHECK(typeof(maintenance_channel)='text' AND length(maintenance_channel)<=65536),
        verify_pending INTEGER NOT NULL DEFAULT 0 CHECK(typeof(verify_pending)='integer' AND verify_pending IN (0,1)),
        verify_cursor INTEGER NOT NULL DEFAULT 0 CHECK(typeof(verify_cursor)='integer' AND verify_cursor BETWEEN 0 AND 9223372036854775807),
        CHECK(cursor<=ceiling),
        CHECK(scope=0 OR (verify_pending=0 AND verify_cursor=0)),
        PRIMARY KEY(scope,channel_id))""",
    'channel_inbound_queue': """CREATE TABLE channel_inbound_queue(
        channel_id TEXT NOT NULL CHECK(typeof(channel_id)='text' AND length(channel_id) BETWEEN 1 AND 65536),
        external_id TEXT NOT NULL CHECK(typeof(external_id)='text' AND length(external_id) BETWEEN 1 AND 65536),
        format_version INTEGER NOT NULL CHECK(typeof(format_version)='integer' AND format_version=1),
        queue_seq INTEGER NOT NULL UNIQUE CHECK(typeof(queue_seq)='integer' AND queue_seq BETWEEN 1 AND 9223372036854775807),
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at) BETWEEN 1 AND 100),
        generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 9223372036854775807),
        lane TEXT NOT NULL CHECK(lane IN ('unclassified','invalid','ordinary','control')),
        recipient TEXT CHECK(recipient IS NULL OR (typeof(recipient)='text' AND length(recipient) BETWEEN 1 AND 21)),
        data_hash TEXT CHECK(data_hash IS NULL OR (typeof(data_hash)='text' AND length(data_hash)=64 AND data_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK((lane IN ('unclassified','invalid') AND recipient IS NULL) OR
              (lane IN ('ordinary','control') AND recipient IS NOT NULL)),
        CHECK((lane='unclassified' AND data_hash IS NULL) OR (lane!='unclassified' AND data_hash IS NOT NULL)),
        PRIMARY KEY(channel_id,external_id),
        FOREIGN KEY(channel_id,external_id) REFERENCES channel_inbound(channel_id,external_id) ON DELETE CASCADE)""",
    'channel_queue_scan': 'CREATE INDEX channel_queue_scan ON channel_inbound_queue(created_at,queue_seq)',
    'channel_queue_route': 'CREATE INDEX channel_queue_route ON channel_inbound_queue(channel_id,recipient,generation,created_at,queue_seq)',
    'channel_queue_control': 'CREATE INDEX channel_queue_control ON channel_inbound_queue(channel_id,recipient,generation,lane,created_at,queue_seq)',
    'channel_queue_rebuild': 'CREATE INDEX channel_queue_rebuild ON channel_queue_generations(scope,ready,channel_id)',
    'channel_queue_pending': 'CREATE INDEX channel_queue_pending ON channel_inbound(status)',
    'channel_queue_channel_pending': 'CREATE INDEX channel_queue_channel_pending ON channel_inbound(channel_id,status)',
    'channel_queue_chat': 'CREATE INDEX channel_queue_chat ON channel_routes(chat_id)',
    'channel_queue_bad_format': "CREATE INDEX channel_queue_bad_format ON channel_inbound_queue(format_version) WHERE typeof(format_version)!='integer' OR format_version!=1",
    'channel_generation_bad_format': "CREATE INDEX channel_generation_bad_format ON channel_queue_generations(format_version) WHERE typeof(format_version)!='integer' OR format_version!=1",
}

_STATE_INSERT = """INSERT INTO channel_queue_generations
    (scope,channel_id,format_version,generation,cursor,ceiling,ready,scan_stamp,scan_seq,maintenance_channel)
    VALUES(1,NEW.channel_id,1,1,0,NEW.rowid,0,'',0,'')
    ON CONFLICT(scope,channel_id) DO UPDATE SET cursor=MIN(cursor,NEW.rowid-1),
        ceiling=MAX(ceiling,NEW.rowid),ready=0;"""
_QUEUE_INSERT = """INSERT INTO channel_inbound_queue VALUES(
    NEW.channel_id,NEW.external_id,1,NEW.rowid,NEW.created_at,
    (SELECT generation FROM channel_queue_generations WHERE scope=1 AND channel_id=NEW.channel_id),
    'unclassified',NULL,NULL);"""
SCHEMA.update({
    'channel_queue_insert': "CREATE TRIGGER channel_queue_insert AFTER INSERT ON channel_inbound WHEN NEW.status='pending' BEGIN " + _STATE_INSERT + _QUEUE_INSERT + ' END',
    'channel_queue_nonpending_insert': """CREATE TRIGGER channel_queue_nonpending_insert AFTER INSERT ON channel_inbound
        WHEN NEW.status!='pending' BEGIN
        DELETE FROM channel_inbound_queue WHERE channel_id=NEW.channel_id AND external_id=NEW.external_id; END""",
    'channel_queue_exit': """CREATE TRIGGER channel_queue_exit AFTER UPDATE OF status ON channel_inbound
        WHEN NEW.status!='pending' BEGIN
        DELETE FROM channel_inbound_queue WHERE channel_id=OLD.channel_id AND external_id=OLD.external_id; END""",
    'channel_queue_delete': """CREATE TRIGGER channel_queue_delete AFTER DELETE ON channel_inbound BEGIN
        DELETE FROM channel_inbound_queue WHERE channel_id=OLD.channel_id AND external_id=OLD.external_id; END""",
    'channel_queue_edit': """CREATE TRIGGER channel_queue_edit AFTER UPDATE OF data,created_at,channel_id,external_id,status ON channel_inbound
        WHEN NEW.status='pending' AND (OLD.status!='pending' OR NEW.data!=OLD.data OR NEW.created_at!=OLD.created_at OR
            NEW.channel_id!=OLD.channel_id OR NEW.external_id!=OLD.external_id) BEGIN
        DELETE FROM channel_inbound_queue WHERE channel_id=OLD.channel_id AND external_id=OLD.external_id; """ + _STATE_INSERT + _QUEUE_INSERT + ' END',
})
for _event, _reference in (('insert', 'NEW'), ('update', 'NEW'), ('delete_config', 'OLD')):
    _verb = 'DELETE' if _event == 'delete_config' else _event.upper()
    _name = 'channel_queue_' + _event + '_config'
    SCHEMA[_name] = f"""CREATE TRIGGER {_name} AFTER {_verb} ON entities WHEN {_reference}.kind='channels' BEGIN
        INSERT INTO channel_queue_generations(scope,channel_id,format_version,generation,cursor,ceiling,ready,scan_stamp,scan_seq,maintenance_channel) VALUES(1,{_reference}.id,1,1,0,
            COALESCE((SELECT MAX(rowid) FROM channel_inbound WHERE channel_id={_reference}.id AND status='pending'),0),0,'',0,'')
        ON CONFLICT(scope,channel_id) DO UPDATE SET generation=generation+1,cursor=0,
            ceiling=excluded.ceiling,ready=0; END"""

_GEN_VALID = """typeof(scope)='integer' AND scope IN(0,1) AND typeof(channel_id)='text' AND
    ((scope=0 AND channel_id='') OR (scope=1 AND length(channel_id) BETWEEN 1 AND 65536)) AND
    typeof(format_version)='integer' AND format_version=1 AND typeof(generation)='integer' AND generation BETWEEN 1 AND 9223372036854775807 AND
    typeof(cursor)='integer' AND cursor BETWEEN 0 AND ceiling AND typeof(ceiling)='integer' AND ceiling BETWEEN 0 AND 9223372036854775807 AND
    typeof(ready)='integer' AND ready IN(0,1) AND typeof(scan_stamp)='text' AND length(scan_stamp)<=100 AND
    (ready=0 OR cursor=ceiling) AND (scope=1 OR generation=1) AND
    typeof(scan_seq)='integer' AND scan_seq BETWEEN 0 AND 9223372036854775807 AND
    ((scan_seq=0 AND scan_stamp='') OR (scan_seq>0 AND length(scan_stamp)>0)) AND
    typeof(maintenance_channel)='text' AND length(maintenance_channel)<=65536 AND
    typeof(verify_pending)='integer' AND verify_pending IN(0,1) AND typeof(verify_cursor)='integer' AND verify_cursor BETWEEN 0 AND 9223372036854775807 AND
    (scope=0 OR (verify_pending=0 AND verify_cursor=0))"""
_QUEUE_VALID = """typeof(channel_id)='text' AND length(channel_id) BETWEEN 1 AND 65536 AND typeof(external_id)='text' AND length(external_id) BETWEEN 1 AND 65536 AND
    typeof(format_version)='integer' AND format_version=1 AND typeof(queue_seq)='integer' AND queue_seq BETWEEN 1 AND 9223372036854775807 AND
    typeof(created_at)='text' AND length(created_at) BETWEEN 1 AND 100 AND typeof(generation)='integer' AND generation BETWEEN 1 AND 9223372036854775807 AND
    typeof(lane)='text' AND lane IN('unclassified','invalid','ordinary','control') AND
    (recipient IS NULL OR (typeof(recipient)='text' AND length(recipient) BETWEEN 1 AND 21)) AND
    (data_hash IS NULL OR (typeof(data_hash)='text' AND length(data_hash)=64 AND data_hash NOT GLOB '*[^0-9a-f]*')) AND
    ((lane IN('unclassified','invalid') AND recipient IS NULL) OR (lane IN('ordinary','control') AND recipient IS NOT NULL)) AND
    ((lane='unclassified' AND data_hash IS NULL) OR (lane!='unclassified' AND data_hash IS NOT NULL))"""
for _table, _predicate, _index in (('channel_queue_generations',_GEN_VALID,'channel_generation_bad_scalar'),('channel_inbound_queue',_QUEUE_VALID,'channel_queue_bad_scalar')):
    SCHEMA[_index] = f'CREATE INDEX {_index} ON {_table}(format_version) WHERE NOT COALESCE(({_predicate}),0)'
for _event,_refs in (('insert',('NEW',)),('update',('OLD','NEW')),('delete',('OLD',))):
    # A changed/deleted hint cannot hide an older original source head. Normal
    # journal exits already removed pending authority and need no prefix rewind.
    _body=[]
    for _ref in _refs:
        _source=f"(SELECT rowid FROM channel_inbound WHERE channel_id={_ref}.channel_id AND external_id={_ref}.external_id AND status='pending')"
        _body.append(f"""UPDATE channel_queue_generations SET cursor=MIN(cursor,{_source}-1),ceiling=MAX(ceiling,{_source}),ready=0
            WHERE scope=1 AND channel_id={_ref}.channel_id AND EXISTS(SELECT 1 FROM channel_inbound
                WHERE channel_id={_ref}.channel_id AND external_id={_ref}.external_id AND status='pending');""")
    _name='channel_queue_hint_'+_event
    SCHEMA[_name]=f'CREATE TRIGGER {_name} AFTER {_event.upper()} ON channel_inbound_queue BEGIN '+''.join(_body)+' END'


def _normal_sql(value):
    return ' '.join(value.strip().rstrip(';').split())


def validate_schema(db):
    """Lightweight exact DDL check, not a scan of queued payloads."""
    rows = db.execute("SELECT name,sql,type,tbl_name FROM sqlite_master WHERE name IN (" +
                      ','.join('?' for _ in SCHEMA) + ") OR tbl_name IN ('channel_inbound_queue','channel_queue_generations') OR "
                      "(type='trigger' AND tbl_name IN ('channel_inbound','entities'))", tuple(SCHEMA)).fetchall()
    found = {}
    for name, sql, kind, table in rows:
        if name.startswith('sqlite_autoindex_') and kind == 'index' and table in PRIVATE_TABLES:
            continue
        if name not in SCHEMA or not isinstance(sql, str) or _normal_sql(sql) != _normal_sql(SCHEMA[name]):
            raise QueueUnavailable()
        found[name] = True
    if set(found) != set(SCHEMA):
        raise QueueUnavailable()
    _validate_formats(db)


def _validate_formats(db):
    # Exact partial indexes contain only unsupported rows. Checking an empty
    # valid index does not scan the pending backlog, including after a legacy
    # writer or a corrupt database bypassed CHECK constraints.
    for table, index in (('channel_inbound_queue', 'channel_queue_bad_format'),
                         ('channel_queue_generations', 'channel_generation_bad_format')):
        try:
            unsupported = db.execute(f"SELECT 1 FROM {table} INDEXED BY {index} WHERE typeof(format_version)!='integer' OR format_version!=1 LIMIT 1").fetchone()
        except sqlite3.Error:
            raise QueueUnavailable() from None
        if unsupported:
            raise QueueUnavailable()
    for table,index,predicate in (('channel_queue_generations','channel_generation_bad_scalar',_GEN_VALID),('channel_inbound_queue','channel_queue_bad_scalar',_QUEUE_VALID)):
        try:
            invalid = db.execute(f'SELECT 1 FROM {table} INDEXED BY {index} WHERE NOT COALESCE(({predicate}),0) LIMIT 1').fetchone()
        except sqlite3.Error:
            raise QueueUnavailable() from None
        if invalid:
            raise QueueUnavailable()
    if db.execute("SELECT 1 FROM channel_queue_generations WHERE scope=0 AND channel_id=''").fetchone() is None:
        raise QueueUnavailable()


def install(db):
    for sql in SCHEMA.values():
        db.execute(sql)
    ceiling = db.execute("SELECT COALESCE(MAX(rowid),0) FROM channel_inbound WHERE status='pending'").fetchone()[0]
    db.execute("INSERT INTO channel_queue_generations VALUES(0,'',1,1,0,?,?,'',0,'',?,0)", (ceiling, int(ceiling == 0), int(ceiling != 0)))
    validate_schema(db)


def reset_recovery(db):
    """Exclusive coordinator startup; no body scan or original-state mutation."""
    validate_schema(db)
    ceiling = db.execute("SELECT COALESCE(MAX(rowid),0) FROM channel_inbound WHERE status='pending'").fetchone()[0]
    db.execute("UPDATE channel_queue_generations SET cursor=0,ceiling=?,ready=0,verify_pending=1,verify_cursor=0 WHERE scope=0 AND channel_id=''", (ceiling,))


def ready(db):
    row = db.execute("SELECT ready,verify_pending FROM channel_queue_generations WHERE scope=0 AND channel_id=''").fetchone()
    if row is None:
        raise QueueUnavailable()
    return tuple(row) == (1, 0)


def _recipient(value):
    if isinstance(value, bool) or not re.fullmatch(r'-?\d{1,20}', str(value)):
        raise ValueError()
    return str(value)


def classify(data, config):
    """Pure routing hint; actual dispatch repeats all authorization checks."""
    try:
        if type(data) is not str or len(data.encode('utf-8')) > MAX_BODY:
            return 'invalid', None
        payload = json.loads(data)
        if not isinstance(payload, dict) or not isinstance(config, dict):
            return 'invalid', None
        if config.get('kind') == 'webhook':
            return ('ordinary', 'webhook') if isinstance(payload.get('text'), str) else ('invalid', None)
        if config.get('kind') != 'telegram':
            return 'invalid', None
        callback = payload.get('callback_query')
        if 'callback_query' in payload:
            return 'control', _recipient(callback['message']['chat']['id'])
        message = payload.get('message') or {}
        recipient = _recipient(message['chat']['id'])
        _recipient(message['from']['id'])
        text = message.get('text')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 16000:
            return 'invalid', None
        command = text.split()[0].split('@', 1)[0].lower() if text.startswith('/') else ''
        argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ''
        conversation = command in ('/builder','/build','/plan','/goal') and not (command == '/builder' and argument.lower() == 'off')
        return ('control' if text.startswith('/') and not conversation else 'ordinary'), recipient
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'invalid', None


def _config(db, identifier):
    row = db.execute("SELECT data FROM entities WHERE kind='channels' AND id=?", (identifier,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value,dict) else {}
    except (ValueError, TypeError):
        return {}


def _index_row(db, row, config):
    try:
        _write_index_row(db,row,config)
    except (sqlite3.Error,UnicodeError):
        raise QueueUnavailable() from None


def _source_read(db,query,parameters,*,many=False):
    try:
        cursor=db.execute(query,parameters)
        return cursor.fetchall() if many else cursor.fetchone()
    except (sqlite3.Error,UnicodeError):
        raise QueueUnavailable() from None


def _write_index_row(db, row, config):
    channel, external, seq, stamp, data = row
    if type(data) is not str or len(data.encode('utf-8'))>MAX_BODY:
        raise QueueUnavailable()
    db.execute("INSERT OR IGNORE INTO channel_queue_generations(scope,channel_id,format_version,generation,cursor,ceiling,ready,scan_stamp,scan_seq,maintenance_channel) VALUES(1,?,1,1,0,?,0,'',0,'')", (channel, seq))
    generation = db.execute('SELECT generation FROM channel_queue_generations WHERE scope=1 AND channel_id=?', (channel,)).fetchone()[0]
    lane, recipient = classify(data, config)
    signature = sha256(data.encode('utf-8')).hexdigest()
    db.execute('INSERT INTO channel_inbound_queue VALUES(?,?,1,?,?,?,?,?,?) ON CONFLICT(channel_id,external_id) DO UPDATE SET '
               'queue_seq=excluded.queue_seq,created_at=excluded.created_at,generation=excluded.generation,lane=excluded.lane,recipient=excluded.recipient,data_hash=excluded.data_hash',
               (channel, external, seq, stamp, generation, lane, recipient, signature))
    _advance_prefix(db,channel,seq)


def _advance_prefix(db,channel,seq):
    state=db.execute('SELECT cursor,ceiling FROM channel_queue_generations WHERE scope=1 AND channel_id=?',(channel,)).fetchone()
    if state is None:raise QueueUnavailable()
    if seq<state[0]:return
    older=db.execute("SELECT 1 FROM channel_inbound WHERE channel_id=? AND status='pending' AND rowid>? AND rowid<? LIMIT 1",(channel,state[0],seq)).fetchone()
    if older is None:
        db.execute('UPDATE channel_queue_generations SET cursor=?,ready=? WHERE scope=1 AND channel_id=?',(seq,int(state[1]==seq),channel))


def record_insert(db, identifier, external_id, budget=None):
    """Called inside the existing journal transaction; duplicate IDs stay untouched."""
    validate_schema(db)
    if budget is not None:
        try:
            budget.payload('receive', classification=True)
        except QueueDeferred:
            return False  # Its atomic SQL trigger retains the unclassified row.
    row = _source_read(db,"SELECT channel_id,external_id,rowid,created_at,"+_SOURCE_DATA+" FROM channel_inbound WHERE channel_id=? AND external_id=? AND status='pending'", (identifier, external_id))
    if row:
        _index_row(db, tuple(row), _config(db, identifier))
        # A fresh immediate classification may extend an already proved prefix.
        # The indexed source probe also notices missing/unclassified legacy rows.
    return row is not None


class InboundQueue:
    def __init__(self, store):
        self.store = store
        self.reason = None
        self.last_work = dict(maintenance_payloads=0,candidate_metadata=0,dispatch_payloads=0,alias_metadata=0)

    def maintenance(self, admitted=lambda: True, budget=None):
        """At most150 persisted payload classifications; restart resumes the cursor."""
        self.reason = None
        with self.store._connection(transaction='write') as db:
            validate_schema(db)
            state = db.execute("SELECT cursor,ceiling,ready,maintenance_channel,verify_pending,verify_cursor FROM channel_queue_generations WHERE scope=0 AND channel_id=''").fetchone()
            if state is None:
                raise QueueUnavailable()
            cursor, ceiling, is_ready, previous, verify_pending, verify_cursor = state
            limit = min(MAINTENANCE_BUDGET, MAINTENANCE_BUDGET-(budget.classifications if budget else 0), PAYLOAD_BUDGET-(budget.payloads if budget else 0))
            if limit <= 0:
                if not admitted():raise QueueDeferred()
                self.last_work = dict(maintenance_payloads=0,candidate_metadata=0,dispatch_payloads=0,alias_metadata=0)
                return ready(db)
            channel = None
            if is_ready and not verify_pending:
                dirty = db.execute('SELECT channel_id,cursor,ceiling FROM channel_queue_generations WHERE scope=1 AND ready=0 AND channel_id>? ORDER BY channel_id LIMIT 1', (previous,)).fetchone()
                if not dirty:
                    dirty = db.execute('SELECT channel_id,cursor,ceiling FROM channel_queue_generations WHERE scope=1 AND ready=0 ORDER BY channel_id LIMIT 1').fetchone()
                if dirty:
                    channel, cursor, ceiling = dirty
            if is_ready and channel is None:
                rows = []
            else:
                clause = ' AND channel_id=?' if channel is not None else ''
                parameters = (cursor, ceiling, channel, limit) if channel is not None else (cursor, ceiling, limit)
                rows = _source_read(db,"SELECT channel_id,external_id,rowid,created_at,"+_SOURCE_DATA+" FROM channel_inbound WHERE status='pending' AND rowid>? AND rowid<=?" + clause + ' ORDER BY rowid LIMIT ?', parameters,many=True)
                configs = {}
                for row in rows:
                    if not admitted():
                        raise QueueDeferred()
                    identifier = row[0]
                    if identifier not in configs:
                        configs[identifier] = _config(db, identifier)
                    if budget:
                        budget.payload('maintenance', classification=True)
                    _index_row(db, tuple(row), configs[identifier])
                complete = len(rows) < limit or (rows and rows[-1][2] == ceiling)
                next_cursor = ceiling if complete else rows[-1][2]
                db.execute('UPDATE channel_queue_generations SET cursor=?,ready=? WHERE scope=? AND channel_id=?',
                           (next_cursor, int(complete), 1 if channel is not None else 0, channel or ''))
                if channel is not None:
                    db.execute("UPDATE channel_queue_generations SET maintenance_channel=? WHERE scope=0 AND channel_id=''", (channel,))
            if not admitted():
                raise QueueDeferred()
            global_state = db.execute("SELECT ready,verify_pending FROM channel_queue_generations WHERE scope=0 AND channel_id=''").fetchone()
            if tuple(global_state) == (1, 1):
                # Bounded source audit/pruning; no persisted hint is trusted on restart.
                available = min(limit-len(rows), PAYLOAD_BUDGET-(budget.payloads if budget else 0))
                verify = db.execute('SELECT channel_id,external_id,queue_seq,created_at,generation,lane,recipient,data_hash FROM channel_inbound_queue WHERE queue_seq>? ORDER BY queue_seq LIMIT ?', (verify_cursor, available)).fetchall() if available else []
                for item in verify:
                    if not admitted():
                        raise QueueDeferred()
                    state=db.execute('SELECT generation FROM channel_queue_generations WHERE scope=1 AND channel_id=?',(item[0],)).fetchone()
                    reclassify=item[5]=='unclassified' or state is not None and state[0]!=item[4]
                    record = self.record(db, tuple(item), budget=budget, semantic=not reclassify, classification=reclassify)
                    if record is None:
                        db.execute('DELETE FROM channel_inbound_queue WHERE channel_id=? AND external_id=?', (item[0],item[1]))
                    elif reclassify:
                        _index_row(db, (record['channel_id'],record['external_id'],record['queue_rowid'],record['created_at'],record['data']), _config(db,item[0]))
                    verify_cursor = item[2]
                if available:
                    done = len(verify)<available
                    db.execute("UPDATE channel_queue_generations SET verify_cursor=?,verify_pending=? WHERE scope=0 AND channel_id=''", (verify_cursor,int(not done)))
            global_ready = ready(db)
            if not admitted():raise QueueDeferred()
            self.last_work = {'maintenance_payloads': len(rows), 'candidate_metadata': 0, 'dispatch_payloads': 0, 'alias_metadata': 0}
            if not global_ready:
                self.reason = 'rebuilding'
            return global_ready

    def candidates(self):
        with self.store._connection() as db:
            validate_schema(db)
            state = db.execute("SELECT scan_stamp,scan_seq FROM channel_queue_generations WHERE scope=0 AND channel_id=''").fetchone()
            if state is None:
                raise QueueUnavailable()
            query = 'SELECT channel_id,external_id,queue_seq,created_at,generation,lane,recipient,data_hash FROM channel_inbound_queue'
            rows = db.execute(query+' WHERE (created_at,queue_seq)>(?,?) ORDER BY created_at,queue_seq LIMIT ?', (*state, CANDIDATE_BUDGET)).fetchall()
            if len(rows) < CANDIDATE_BUDGET:
                rows += db.execute(query+' WHERE (created_at,queue_seq)<=(?,?) ORDER BY created_at,queue_seq LIMIT ?', (*state, CANDIDATE_BUDGET-len(rows))).fetchall()
            self.last_work['candidate_metadata'] = len(rows)
            return rows

    def eligible(self, db, item, heads):
        _validate_formats(db)
        if not ready(db):
            self.reason = 'rebuilding'
            return False
        channel, external, seq, stamp, generation, lane, recipient, signature = item
        current = db.execute('SELECT generation,ready FROM channel_queue_generations WHERE scope=1 AND channel_id=?', (channel,)).fetchone()
        if current is not None and (generation>current[0] or current[1]==1 and (generation!=current[0] or lane=='unclassified')):
            raise QueueUnavailable()
        if current is None or tuple(current) != (generation, 1) or lane == 'unclassified':
            self.reason = 'rebuilding'
            return False
        if lane == 'invalid':
            return True
        config = _config(db, channel)
        if not config.get('enabled') or not config.get('allow_inbound'):
            return True
        route = db.execute('SELECT r.chat_id,r.project_id,c.project_id,c.archived FROM channel_routes r LEFT JOIN chats c ON c.id=r.chat_id WHERE r.channel_id=? AND r.recipient=?', (channel, recipient)).fetchone()
        project = config.get('project_id')
        if route and (route[1] != project or route[2] != project or route[3] != 0):
            return True
        key = ('chat', route[0], project) if route else ('route', channel, recipient)
        if key not in heads:
            if route:
                # Bound raw routes before filtering or JSON inspection. Sorting
                # or filtering first can scan every alias despite LIMIT 65.
                raw = db.execute('SELECT channel_id,recipient,project_id FROM channel_routes INDEXED BY channel_queue_chat WHERE chat_id=? LIMIT ?',
                                 (route[0], ALIAS_LIMIT+1)).fetchall()
                self.last_work['alias_metadata'] += len(raw)
                aliases = []
                if len(raw) > ALIAS_LIMIT:
                    heads[key] = None
                    self.reason = 'alias_limit'
                else:
                    for alias_channel, alias_recipient, alias_project in raw:
                        if alias_project != project:
                            continue
                        metadata = db.execute("""SELECT g.generation,g.ready FROM entities e
                            LEFT JOIN channel_queue_generations g ON g.scope=1 AND g.channel_id=e.id
                            WHERE e.kind='channels' AND e.id=? AND CASE WHEN json_valid(e.data)
                            THEN json_extract(e.data,'$.project_id') IS ? ELSE 0 END""", (alias_channel, project)).fetchone()
                        if metadata:
                            aliases.append((alias_channel, alias_recipient, *metadata))
                if key not in heads and any(row[2] is None or row[3] != 1 for row in aliases):
                    heads[key] = None
                    self.reason = 'rebuilding'
            else:
                aliases = [(channel, recipient, generation, 1)]
            if key not in heads:
                oldest, control = [], []
                for alias_channel, alias_recipient, alias_generation, _ in aliases:
                    base = 'SELECT created_at,queue_seq FROM channel_inbound_queue WHERE channel_id=? AND recipient=? AND generation=?'
                    params = (alias_channel, alias_recipient, alias_generation)
                    first = db.execute(base+' ORDER BY created_at,queue_seq LIMIT 1', params).fetchone()
                    command = db.execute(base+" AND lane='control' ORDER BY created_at,queue_seq LIMIT 1", params).fetchone()
                    if first: oldest.append(tuple(first))
                    if command: control.append(tuple(command))
                heads[key] = (min(oldest) if oldest else None, min(control) if control else None)
        head = heads[key]
        return head is not None and (stamp, seq) == head[1 if lane == 'control' else 0]

    def record(self, db, item, budget=None, *, semantic=True, classification=False):
        _validate_formats(db)
        if budget:
            budget.payload('verification' if classification else 'source',classification=classification)
        row = _source_read(db,"SELECT rowid AS queue_rowid,channel_id,external_id,status,"+_SOURCE_DATA+",run_id,error,created_at,updated_at FROM channel_inbound WHERE channel_id=? AND external_id=? AND status='pending'", (item[0], item[1]))
        self.last_work['dispatch_payloads'] = self.last_work.get('dispatch_payloads',0) + int(row is not None)
        if row is None:
            return None
        if type(row['data']) is not str or len(row['data'].encode('utf-8'))>MAX_BODY:
            raise QueueUnavailable()
        if row['queue_rowid'] != item[2] or row['created_at'] != item[3] or item[5]!='unclassified' and sha256(row['data'].encode('utf-8')).hexdigest() != item[7]:
            raise QueueUnavailable()
        if semantic and classify(row['data'],_config(db,item[0])) != (item[5],item[6]):
            raise QueueUnavailable()
        return row

    def validate_selection(self, db, item, budget=None):
        validate_schema(db)
        current = db.execute('SELECT channel_id,external_id,queue_seq,created_at,generation,lane,recipient,data_hash FROM channel_inbound_queue WHERE channel_id=? AND external_id=?', item[:2]).fetchone()
        if current is None or tuple(current)!=tuple(item) or not self.eligible(db,item,{}):
            raise QueueDeferred()
        if self.record(db,item,budget) is None:
            raise QueueDeferred()

    def advance(self, db, item):
        _validate_formats(db)
        db.execute("UPDATE channel_queue_generations SET scan_stamp=?,scan_seq=? WHERE scope=0 AND channel_id=''", (item[3], item[2]))
