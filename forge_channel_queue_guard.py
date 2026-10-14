"""Read-only rollback certificate for the exact, fully reconciled channel cache.

This module grants no routing authority and never repairs a database. Unsupported
or incomplete caches remain protected alongside the authoritative journal.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
import sqlite3
import time

import forge_channel_queue as queue


FAILURE = 'Rollback state could not be safely inspected. Current state and snapshots were preserved.'
AUDIT_SECONDS = 10.0


class GuardFailure(ValueError):
    def __init__(self):
        super().__init__(FAILURE)


class GuardBudget:
    """The default bounds the queue audit; caller limits cover later hashing too."""
    def __init__(self, cancel=None, deadline=None, *, clock=None):
        self.clock = clock or time.monotonic
        if cancel is not None and not callable(cancel):
            raise GuardFailure()
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise GuardFailure()
        self.cancel, self.deadline = cancel, deadline
        self.audit_deadline = min(self.clock() + AUDIT_SECONDS, deadline) if deadline is not None else self.clock() + AUDIT_SECONDS
        self.auditing, self.stopped = False, False

    def check(self):
        try:
            end = self.audit_deadline if self.auditing else self.deadline
            if self.stopped or self.cancel is not None and self.cancel() or end is not None and self.clock() >= end:
                raise GuardFailure()
        except Exception:
            self.stopped = True
            raise GuardFailure() from None

    def progress(self):
        try:
            self.check()
            return 0
        except GuardFailure:
            return 1


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def _tokens(sql):
    # Whitespace/case outside string literals has no identity. Punctuation,
    # defaults, constraints and quoted literals must still match exactly.
    return tuple(token if token.startswith("'") else token.lower() for token in
                 re.findall(r"'(?:''|[^'])*'|[^\W]+|[^\s;]", sql))


PARENTS = {
    'channel_inbound': 'CREATE TABLE channel_inbound(channel_id TEXT NOT NULL,external_id TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,run_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(channel_id,external_id))',
    'channel_routes': 'CREATE TABLE channel_routes(channel_id TEXT NOT NULL,recipient TEXT NOT NULL,chat_id TEXT NOT NULL,project_id TEXT,PRIMARY KEY(channel_id,recipient))',
    'entities': 'CREATE TABLE entities(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id))',
    'projects': 'CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
    'chats': "CREATE TABLE chats(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id),title TEXT NOT NULL,model TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',compacted_through INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)))",
}
# (name, type, not-null, default, primary-key ordinal, hidden)
PARENT_COLUMNS = {
    'channel_inbound': [('channel_id','TEXT',1,None,1,0),('external_id','TEXT',1,None,2,0),('status','TEXT',1,None,0,0),('data','TEXT',1,None,0,0),('run_id','TEXT',0,None,0,0),('error','TEXT',0,None,0,0),('created_at','TEXT',1,None,0,0),('updated_at','TEXT',1,None,0,0)],
    'channel_routes': [('channel_id','TEXT',1,None,1,0),('recipient','TEXT',1,None,2,0),('chat_id','TEXT',1,None,0,0),('project_id','TEXT',0,None,0,0)],
    'entities': [('kind','TEXT',1,None,1,0),('id','TEXT',1,None,2,0),('data','TEXT',1,None,0,0)],
    'projects': [('id','TEXT',0,None,1,0),('name','TEXT',1,None,0,0),('path','TEXT',1,None,0,0),('created_at','TEXT',1,None,0,0),('updated_at','TEXT',1,None,0,0)],
    'chats': [('id','TEXT',0,None,1,0),('project_id','TEXT',0,None,0,0),('title','TEXT',1,None,0,0),('model','TEXT',1,"''",0,0),('summary','TEXT',1,"''",0,0),('compacted_through','INTEGER',1,'0',0,0),('created_at','TEXT',1,None,0,0),('updated_at','TEXT',1,None,0,0),('archived','INTEGER',1,'0',0,0)],
}
SCHEMA_SHA256 = sha256(json.dumps(sorted((name, _tokens(sql)) for name, sql in {**PARENTS, **queue.SCHEMA}.items()), separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class QueueAudit:
    state: str
    sql_sha256: str
    tables: frozenset


def _reserved(name):
    name = name.casefold()
    return name.startswith(('channel_queue_', 'channel_inbound_queue', 'channel_generation_'))


def _objects(db, budget):
    all_rows, group = {}, []
    for row in db.execute('SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY name,type,tbl_name'):
        budget.check()
        name, kind, table, sql = row
        all_rows[name] = tuple(row)
        references = isinstance(sql, str) and any(private in sql.casefold() for private in queue.PRIVATE_TABLES)
        if (_reserved(name) or name in queue.SCHEMA or table.casefold() in queue.PRIVATE_TABLES or
                kind == 'trigger' and table.casefold() in ('channel_inbound', 'entities') or references):
            group.append(tuple(row))
    return all_rows, group


def _parents_valid(db, objects):
    for name, sql in PARENTS.items():
        row = objects.get(name)
        if row is None or row[1] != 'table' or not isinstance(row[3], str) or _tokens(row[3]) != _tokens(sql):
            return False
        info = [tuple(row)[1:] for row in db.execute('PRAGMA table_xinfo(' + quoted(name) + ')')]
        if info != PARENT_COLUMNS[name]:
            return False
        foreign = [tuple(row) for row in db.execute('PRAGMA foreign_key_list(' + quoted(name) + ')')]
        expected = [(0,0,'projects','project_id','id','NO ACTION','NO ACTION','NONE')] if name == 'chats' else []
        if foreign != expected:
            return False
    return True


def _autoindexes_valid(db, group):
    expected = {'channel_inbound_queue': {('channel_id','external_id'), ('queue_seq',)},
                'channel_queue_generations': {('scope','channel_id')}}
    found = {name: set() for name in expected}
    for name, kind, table, sql in group:
        if name.startswith('sqlite_autoindex_'):
            if kind != 'index' or table not in expected or sql is not None:
                return False
            cols = tuple(row[2] for row in db.execute('PRAGMA index_info(' + quoted(name) + ')'))
            if cols not in expected[table] or cols in found[table]:
                return False
            found[table].add(cols)
    return found == expected


def _ready(db, objects, group, budget):
    known = {name for name, kind, table, sql in group if not name.startswith('sqlite_autoindex_')}
    if known != set(queue.SCHEMA) or not _parents_valid(db, objects) or not _autoindexes_valid(db, group):
        return False
    queue.validate_schema(db)
    for table, predicate in (('channel_queue_generations', queue._GEN_VALID), ('channel_inbound_queue', queue._QUEUE_VALID)):
        budget.check()
        if db.execute('SELECT 1 FROM ' + table + ' NOT INDEXED WHERE NOT COALESCE((' + predicate + '),0) LIMIT 1').fetchone():
            return False
    if db.execute("SELECT COUNT(*) FROM channel_queue_generations WHERE scope=0").fetchone()[0] != 1:
        return False
    if not queue.ready(db) or db.execute('SELECT 1 FROM channel_queue_generations WHERE ready!=1 LIMIT 1').fetchone():
        return False
    if db.execute("SELECT 1 FROM channel_inbound s LEFT JOIN channel_inbound_queue h ON h.channel_id=s.channel_id AND h.external_id=s.external_id WHERE s.status='pending' AND h.channel_id IS NULL LIMIT 1").fetchone():
        return False
    bounded = lambda name: f"CASE WHEN typeof({name}.data)='text' AND length(CAST({name}.data AS BLOB))<={queue.MAX_BODY} THEN {name}.data END"
    cursor = db.execute('SELECT h.queue_seq,h.created_at,h.generation,h.lane,h.recipient,h.data_hash,'
        's.rowid,s.created_at,' + bounded('s') + ',s.status,g.generation,g.ready,' + bounded('e') + ' '
        'FROM channel_inbound_queue h LEFT JOIN channel_inbound s ON s.channel_id=h.channel_id AND s.external_id=h.external_id '
        "LEFT JOIN channel_queue_generations g ON g.scope=1 AND g.channel_id=h.channel_id LEFT JOIN entities e ON e.kind='channels' AND e.id=h.channel_id ORDER BY h.queue_seq")
    while True:
        budget.check()
        rows = cursor.fetchmany(64)
        if not rows:
            break
        for seq, stamp, generation, lane, recipient, signature, source_seq, source_stamp, data, status, current, ready, config in rows:
            budget.check()
            if source_seq != seq or source_stamp != stamp or status != 'pending' or generation != current or ready != 1 or lane == 'unclassified':
                return False
            if type(data) is not str or type(config) is not str:
                return False
            config = json.loads(config)
            if not isinstance(config, dict) or config.get('kind') not in ('telegram', 'webhook'):
                return False
            if sha256(data.encode('utf-8')).hexdigest() != signature or queue.classify(data, config) != (lane, recipient):
                return False
    return db.execute('PRAGMA foreign_key_check(channel_inbound_queue)').fetchone() is None


def inspect_queue(db, budget):
    """O(backlog), read-only; no claim that this offline audit takes 200 steps."""
    budget.auditing = True
    try:
        budget.check()
        objects, group = _objects(db, budget)
        budget.check()
        if not group:
            return QueueAudit('absent', sha256(b'[]').hexdigest(), frozenset())
        protected = sorted(group + [objects[name] for name in PARENTS if name in objects])
        signature = sha256(json.dumps(protected, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        tables = frozenset(name for name, kind, table, sql in group if kind == 'table')
        try:
            supported = _ready(db, objects, group, budget)
        except (queue.QueueUnavailable, sqlite3.Error, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            budget.check()
            supported = False
        budget.check()
        return QueueAudit('ready' if supported else 'protected', signature, tables)
    except GuardFailure:
        raise
    except (sqlite3.Error, UnicodeError, ValueError, TypeError):
        raise GuardFailure() from None
    finally:
        budget.auditing = False


def projection(baseline, audit):
    """Preserve historical protections; a baseline can never opt itself out."""
    if baseline is not None and (type(baseline) is not dict or type(baseline.get('version')) is not int or baseline['version'] not in (1, 2) or type(baseline.get('tables')) is not dict):
        raise GuardFailure()
    wanted = baseline['tables'] if baseline is not None else {}
    for name, entry in wanted.items():
        if type(name) is not str or not name or type(entry) is not dict:
            raise GuardFailure()
        if 'columns' in entry and (type(entry['columns']) is not list or not entry['columns'] or any(type(c) is not str or not c for c in entry['columns'])):
            raise GuardFailure()
        if 'columns' not in entry and entry not in ({'missing': True}, {'missing_columns': True}):
            raise GuardFailure()
    explicit = bool(queue.PRIVATE_TABLES & set(wanted))
    protection = baseline.get('channel_queue_protection') if baseline else None
    if protection is not None and (type(protection) is not dict or set(protection) != {'state','sql_sha256'} or protection['state'] not in ('protected','missing','legacy_uncertified') or type(protection['sql_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', protection['sql_sha256'])):
        raise GuardFailure()
    legacy = baseline is not None and baseline['version'] == 1
    if legacy:
        if set(baseline) - {'version', 'tables', 'channel_queue_protection'}:
            raise GuardFailure()
        result = {'version': 1}
        required = False
    else:
        if baseline is not None:
            if set(baseline) - {'version','tables','capabilities','channel_queue_protection'} or type(baseline.get('capabilities')) is not dict or set(baseline['capabilities']) != {'channel_queue'}:
                raise GuardFailure()
            cap = baseline['capabilities']['channel_queue']
            if type(cap) is not dict or set(cap) != {'version','schema_sha256','required'} or type(cap['version']) is not int or cap['version'] != 1 or cap['schema_sha256'] != SCHEMA_SHA256 or type(cap['required']) is not bool:
                raise GuardFailure()
            if explicit and protection is None:
                raise GuardFailure()
            required = cap['required']
        else:
            required = audit.state != 'absent'
        result = {'version': 2, 'capabilities': {'channel_queue': {'version': 1, 'schema_sha256': SCHEMA_SHA256, 'required': required}}}
    protect = explicit or protection is not None or audit.state == 'protected' or required and audit.state == 'absent'
    if protect:
        state = 'legacy_uncertified' if legacy and explicit else ('missing' if required and audit.state == 'absent' else 'protected')
        result['channel_queue_protection'] = {'state': state, 'sql_sha256': audit.sql_sha256}
    excluded = queue.PRIVATE_TABLES if audit.state == 'ready' and not protect else frozenset()
    return result, excluded, audit.tables
