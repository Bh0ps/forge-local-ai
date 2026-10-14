"""Actual Store on disposable data; no installed state or packaged rollback claims."""
import json
from pathlib import Path
import socket
import sqlite3

import pytest

import forge_channel_queue as queue
import forge_store
from forge_store import ForgeStore


def forbidden(*args,**kwargs):
    raise AssertionError('Native, external, credential and home access is forbidden.')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(Path,'home',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)


def original_pending(store,count=1):
    store.save_entity('channels',dict(id='synthetic-channel',kind='telegram',enabled=True,allow_inbound=True))
    with store._connection(transaction='write') as db:
        for number in range(count):
            payload=json.dumps({'message':{'chat':{'id':'42'},'from':{'id':'42'},'text':'Synthetic request '+str(number)}})
            db.execute('INSERT INTO channel_inbound VALUES(?,?,?,?,NULL,NULL,?,?)',('synthetic-channel',str(number),'pending',payload,str(number),'synthetic'))


def remove_private_group(store):
    with store._connection(transaction='write') as db:
        for kind in ('trigger','index','table'):
            for row in db.execute('SELECT name FROM sqlite_master WHERE type=?',(kind,)).fetchall():
                if row[0] in queue.SCHEMA:
                    db.execute('DROP '+kind+' "'+row[0]+'"')


def original_rows(store):
    with store._connection() as db:
        return [tuple(row) for row in db.execute('SELECT * FROM channel_inbound ORDER BY rowid')]


def test_verified_supplemental_install_is_atomic_and_reopen_does_not_repeat_backup(tmp_path):
    store=ForgeStore(tmp_path/'disposable')
    original_pending(store)
    remove_private_group(store)
    before=original_rows(store)
    old=set((store.home/'backups').iterdir())
    reopened=ForgeStore(store.home)
    added=set((store.home/'backups').iterdir())-old
    assert len(added)==1 and reopened.channel_queue_reason is None
    with sqlite3.connect(next(iter(added))/'forge.sqlite3') as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert backup.execute("SELECT 1 FROM sqlite_master WHERE name='channel_inbound_queue'").fetchone() is None
        assert backup.execute('SELECT * FROM channel_inbound').fetchall()==before
    ForgeStore(store.home)
    assert set((store.home/'backups').iterdir())==old|added
    assert original_rows(reopened)==before


def test_failure_mid_schema_creation_rolls_back_private_objects_and_keeps_chats_available(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'disposable')
    original_pending(store)
    remove_private_group(store)
    before=original_rows(store)
    def interrupted(db):
        for statement in list(queue.SCHEMA.values())[:2]:db.execute(statement)
        raise ValueError('Synthetic private schema interruption')
    monkeypatch.setattr(queue,'install',interrupted)
    reopened=ForgeStore(store.home)
    assert reopened.channel_queue_reason=='metadata_unavailable'
    with reopened._connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('channel_inbound_queue','channel_queue_generations')").fetchone() is None
    assert reopened.create_chat(title='Synthetic local chat')['title']=='Synthetic local chat'
    assert original_rows(reopened)==before


def test_failed_backup_does_not_install_schema_or_disable_ordinary_chats(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'disposable')
    remove_private_group(store)
    connect=forge_store.sqlite3.connect
    def unavailable(path,*args,**kwargs):
        if 'pre-channel-queue-' in str(path):raise sqlite3.OperationalError('Synthetic private backup failure')
        return connect(path,*args,**kwargs)
    monkeypatch.setattr(forge_store.sqlite3,'connect',unavailable)
    reopened=ForgeStore(store.home)
    assert reopened.channel_queue_reason=='metadata_unavailable'
    assert reopened.create_chat(title='Synthetic local chat')['title']=='Synthetic local chat'
    with reopened._connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='channel_inbound_queue'").fetchone() is None


@pytest.mark.parametrize('corruption',['unknown_trigger','partial_schema','bad_format','bad_scalar','missing_global'])
def test_unsupported_private_metadata_is_retained_and_only_channels_are_unavailable(tmp_path,corruption):
    store=ForgeStore(tmp_path/'disposable')
    original_pending(store)
    before=original_rows(store)
    with store._connection(transaction='write') as db:
        if corruption=='unknown_trigger':db.execute('CREATE TRIGGER synthetic_unknown AFTER INSERT ON channel_inbound_queue BEGIN SELECT 1; END')
        elif corruption=='partial_schema':db.execute('DROP INDEX channel_queue_route')
        elif corruption=='missing_global':db.execute("DELETE FROM channel_queue_generations WHERE scope=0")
        else:
            db.execute('PRAGMA ignore_check_constraints=ON')
            if corruption=='bad_format':db.execute('UPDATE channel_inbound_queue SET format_version=2')
            else:db.execute("UPDATE channel_queue_generations SET scan_stamp='bad',scan_seq=-1 WHERE scope=0")
    with store._connection() as db:
        rows=[tuple(row) for row in db.execute('SELECT * FROM channel_queue_generations')]
    reopened=ForgeStore(store.home)
    assert reopened.channel_queue_reason=='metadata_unavailable'
    assert reopened.create_chat(title='Synthetic local chat')['title']=='Synthetic local chat'
    with reopened._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM channel_queue_generations')]==rows
    assert original_rows(reopened)==before


def test_restart_defers_body_work_and_resumes_bounded_reconciliation(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'disposable')
    original_pending(store,450)
    before=original_rows(store)
    classify=queue.classify
    monkeypatch.setattr(queue,'classify',forbidden)
    reopened=ForgeStore(store.home)
    assert reopened.channel_queue_reason is None
    monkeypatch.setattr(queue,'classify',classify)
    helper=queue.InboundQueue(reopened)
    for tick in range(12):
        budget=queue.InboundWorkBudget()
        complete=helper.maintenance(budget=budget)
        assert budget.payloads<=200 and budget.classifications<=150
        if complete:break
    assert complete and tick>0 and original_rows(reopened)==before


def test_final_selection_validator_runs_inside_admission_before_any_new_request_and_prior_key_wins(tmp_path):
    store=ForgeStore(tmp_path/'disposable')
    original_pending(store)
    observed=[]
    def defer(db):
        observed.append(db.in_transaction)
        raise queue.QueueDeferred()
    data=dict(request='Synthetic assignment',settings=store.get_settings(),project_id=None)
    key='channel:synthetic-channel:0'
    with pytest.raises(queue.QueueDeferred):
        store.accept_request(data,source_key=key,channel=queue.ChannelAdmission('synthetic-channel','0',defer))
    with store._connection() as db:
        assert all(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]==0 for name in ('runs','messages','request_keys','chats'))
    assert observed==[True] and original_rows(store)[0][2]=='pending'
    created,fresh=store.accept_request(data,source_key=key,channel=('synthetic-channel','0'))
    repeated,fresh_again=store.accept_request(data,source_key=key,channel=queue.ChannelAdmission('synthetic-channel','0',defer))
    assert fresh and not fresh_again and repeated['id']==created['id'] and observed==[True]
    assert 'validate' not in json.dumps(repeated)
