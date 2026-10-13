"""Poll progress stays a single short, coherent read of disposable local state."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import threading

import pytest

from forge_runs import RunManager
from forge_store import ForgeStore,TERMINAL


@pytest.fixture
def journal(tmp_path):
    store=ForgeStore(tmp_path/'workspace')
    manager=RunManager(SimpleNamespace(store=store))
    goal=store.save_goal({'title':'Synthetic goal','tasks':[
        {'id':'accepted-task','text':'Inspect synthetic evidence.','status':'completed','evidence':['fixture evidence']},
        {'id':'next-task','text':'Verify synthetic evidence.','status':'pending'}],
        'next_action':'Verify the accepted tasks.'})
    chat=store.create_chat()
    run=store.create_run({'chat_id':chat['id'],'goal_id':goal['id'],'request':'Synthetic request',
        'settings':store.get_settings(),'workflow':{'phase':'verify'},
        'verification_feedback':{'version':0,'receipts':{'check':{'schema_version':2,'version':0,
            'scope_revision':goal['scope_revision'],'complete':True,'available':True,'passed':True,
            'task_ids':['accepted-task'],'producer_type':'registered_check'}}}})
    return store,manager,goal,run


def legacy_poll(manager,identifier,after=0):
    # Reference public payload before batching; default snapshot tuple remains compatible.
    run,events,latest=manager.store.poll_snapshot(identifier,after)
    cursor=events[-1]['seq'] if events else after;more=cursor<latest
    return dict(id=identifier,chat_id=run['chat_id'],events=events,next_cursor=cursor,has_more=more,
        finished=run['status'] in TERMINAL and not more,status=run['status'],recovery=run.get('recovery'),
        progress_summary=manager.workflow.progress(run),workflow=run.get('workflow'))


def count_reads(store,monkeypatch):
    original=store._connection;reads=[]
    @contextmanager
    def connection(*,transaction=None):
        with original(transaction=transaction) as db:
            reads.append((transaction,db))
            yield db
    monkeypatch.setattr(store,'_connection',connection)
    return reads


def assert_closed(reads):
    for _,db in reads:
        with pytest.raises(sqlite3.ProgrammingError):db.execute('SELECT 1')


@pytest.mark.parametrize('status',['queued','running','waiting_question','waiting_approval','compacting',
    'paused','interrupted','failed','completed','cancelled'])
@pytest.mark.parametrize('with_goal',[True,False])
def test_public_progress_matches_with_one_read_and_no_secondary_lookup(journal,monkeypatch,status,with_goal):
    store,manager,goal,run=journal
    store.update_run(run['id'],status=status,phase=status,recovery='Synthetic recovery.',goal_id=goal['id'] if with_goal else None)
    store.invocation('interrupted-effect',run['id'],'write_file',{'path':'synthetic.txt'})
    store.invocation_state('interrupted-effect','outcome_unknown')
    store.event(run['id'],'status',text='Synthetic status.')
    expected=legacy_poll(manager,run['id'])
    reads=count_reads(store,monkeypatch)
    monkeypatch.setattr(store,'goal',lambda *args:pytest.fail('Poll must not reconcile an external checklist'))
    monkeypatch.setattr(store,'unknown_actions',lambda *args:pytest.fail('Poll must not open a second read'))
    actual=manager.poll(run['id'])
    assert actual==expected and len(reads)==1 and reads[0][0]=='read'
    assert set(actual)=={'id','chat_id','events','next_cursor','has_more','finished','status','recovery','progress_summary','workflow'}
    assert_closed(reads)


def test_empty_stored_goal_and_missing_markdown_do_not_reopen_progress_read(journal,monkeypatch):
    store,manager,goal,run=journal
    with store._connection(transaction='write') as db:
        db.execute("UPDATE entities SET data='{}' WHERE kind='goals' AND id=?",(goal['id'],))
    expected=legacy_poll(manager,run['id']);reads=count_reads(store,monkeypatch)
    assert manager.poll(run['id'])==expected and len(reads)==1
    assert_closed(reads)


def test_poll_skips_external_file_io_but_goal_detail_still_detects_edits(journal,monkeypatch):
    store,manager,goal,run=journal;path=Path(goal['path'])
    path.write_text('An external synthetic checklist edit.\n',encoding='utf-8')
    original=Path.read_text;reads=[]
    def read(self,*args,**kwargs):
        if self==path:reads.append(self)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    result=manager.poll(run['id'])
    assert result['progress_summary']['total']==2 and reads==[]
    detail=store.goal(goal['id'])
    assert detail['external_edits'] and detail['markdown']=='An external synthetic checklist edit.\n' and reads==[path]


def add_events(store,run,count):
    with store._connection(transaction='write') as db:
        db.executemany('INSERT INTO run_events VALUES(?,?,?,?,?)',[(run['id'],i,'status',json.dumps({'text':f'Synthetic event {i}'}),'fixture')
            for i in range(1,count+1)])


def test_terminal_events_drain_over_500_without_consumption_and_survive_reopen(journal,monkeypatch):
    store,manager,_,run=journal;add_events(store,run,600);store.update_run(run['id'],status='completed')
    reads=count_reads(store,monkeypatch)
    first=manager.poll(run['id']);second=manager.poll(run['id'],first['next_cursor'])
    assert len(first['events'])==500 and first['next_cursor']==500 and first['has_more'] and not first['finished']
    assert len(second['events'])==100 and second['next_cursor']==600 and not second['has_more'] and second['finished']
    assert manager.poll(run['id'])==first and manager.poll(run['id'],600)['events']==[]
    future=manager.poll(run['id'],1000)
    assert future['next_cursor']==1000 and future['events']==[] and future['finished']
    assert len(reads)==5;assert_closed(reads)
    restored=RunManager(SimpleNamespace(store=ForgeStore(store.home)))
    assert restored.poll(run['id'])==first and restored.poll(run['id'],500)==second


def test_cancel_and_restart_keep_completed_actions_and_unknown_recovery(journal):
    store,manager,_,run=journal
    store.invocation('done-effect',run['id'],'write_file',{'path':'fixture.txt'})
    store.invocation_state('done-effect','completed',{'synthetic':'saved evidence'})
    store.invocation('pending-effect',run['id'],'write_file',{'path':'another.txt'})
    store.invocation_state('pending-effect','running')
    manager.cancel(run['id'],pause=True)
    assert manager.poll(run['id'])['progress_summary']['actions']==['resume']
    restored=RunManager(SimpleNamespace(store=ForgeStore(store.home)))
    replay=restored.poll(run['id'])
    assert replay['status']=='paused' and replay['progress_summary']['actions']==['inspect_interruption']
    with store._connection() as db:
        assert db.execute("SELECT status,result FROM invocations WHERE id='done-effect'").fetchone()['status']=='completed'
        assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==2
    restored.cancel(run['id'])
    assert restored.poll(run['id'])['status']=='cancelled'


@pytest.mark.parametrize('problem',['missing_run','invalid_run_json','missing_goal','invalid_goal_json','invalid_goal_shape',
    'invalid_event_json','missing_invocations'])
def test_missing_and_corrupt_records_close_the_snapshot_on_errors(journal,monkeypatch,problem):
    store,manager,goal,run=journal
    with store._connection(transaction='write') as db:
        if problem=='missing_run':db.execute('DELETE FROM runs WHERE id=?',(run['id'],))
        elif problem=='invalid_run_json':db.execute("UPDATE runs SET data='{' WHERE id=?",(run['id'],))
        elif problem=='missing_goal':db.execute("DELETE FROM entities WHERE kind='goals' AND id=?",(goal['id'],))
        elif problem in ('invalid_goal_json','invalid_goal_shape'):
            db.execute("UPDATE entities SET data=? WHERE kind='goals' AND id=?",('{' if problem=='invalid_goal_json' else '[]',goal['id']))
        elif problem=='invalid_event_json':db.execute('INSERT INTO run_events VALUES(?,?,?,?,?)',(run['id'],1,'status','{','fixture'))
        else:
            current=store.run(run['id']);current['status']='paused'
            db.execute('UPDATE runs SET status=?,data=? WHERE id=?',('paused',json.dumps(current),run['id']))
            db.execute('DROP TABLE invocations')
    reads=count_reads(store,monkeypatch)
    with pytest.raises((ValueError,AttributeError)):manager.poll(run['id'])
    assert len(reads)==1;assert_closed(reads)
    with store._connection(transaction='write') as db:db.execute("INSERT OR REPLACE INTO forge_settings VALUES('fixture_cleanup','true')")


@pytest.mark.parametrize('cursor',[-1,True,1.5,'1',None])
def test_invalid_cursor_opens_no_read(journal,monkeypatch,cursor):
    store,manager,_,run=journal;reads=count_reads(store,monkeypatch)
    with pytest.raises(ValueError,match='Invalid event cursor'):manager.poll(run['id'],cursor)
    assert reads==[]


def test_paused_run_missing_saved_json_id_keeps_the_existing_error_and_closes_read(journal,monkeypatch):
    store,manager,_,run=journal
    broken={**run,'status':'paused'};broken.pop('id')
    with store._connection(transaction='write') as db:
        db.execute('UPDATE runs SET status=?,data=? WHERE id=?',('paused',json.dumps(broken),run['id']))
    with pytest.raises(KeyError,match='id'):legacy_poll(manager,run['id'])
    reads=count_reads(store,monkeypatch)
    with pytest.raises(KeyError,match='id'):manager.poll(run['id'])
    assert len(reads)==1;assert_closed(reads)


def test_unknown_actions_use_the_saved_run_identity_as_before_even_for_malformed_state(journal,monkeypatch):
    store,manager,_,run=journal
    other=store.create_run({'chat_id':store.create_chat()['id'],'settings':store.get_settings()})
    store.invocation('original-unknown',run['id'],'write_file',{'path':'fixture.txt'})
    store.invocation_state('original-unknown','outcome_unknown')
    broken={**run,'id':other['id'],'status':'paused'}
    with store._connection(transaction='write') as db:
        db.execute('UPDATE runs SET status=?,data=? WHERE id=?',('paused',json.dumps(broken),run['id']))
    expected=legacy_poll(manager,run['id']);assert expected['progress_summary']['actions']==['resume']
    reads=count_reads(store,monkeypatch)
    assert manager.poll(run['id'])==expected and len(reads)==1;assert_closed(reads)


def test_concurrent_atomic_update_cannot_mix_run_goal_or_unknown_action_snapshots(journal,monkeypatch):
    store,manager,goal,run=journal;entered,release=threading.Event(),threading.Event()
    original=store._connection;connections=[]
    class PausedRead:
        def __init__(self,db):self.db=db
        def execute(self,sql,parameters=()):
            cursor=self.db.execute(sql,parameters)
            if sql=='SELECT data FROM runs WHERE id=?':
                entered.set();assert release.wait(5)
            return cursor
    @contextmanager
    def connection(*,transaction=None):
        with original(transaction=transaction) as db:
            connections.append(db);yield PausedRead(db)
    monkeypatch.setattr(store,'_connection',connection)
    before=deepcopy(run)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(manager.poll,run['id'])
        try:
            assert entered.wait(5)
            with original(transaction='write') as db:
                current={**before,'status':'paused','workflow':{'phase':'report','next_action':'New scoped action.'}}
                newer={**goal,'tasks':[{**task,'status':'completed'} for task in goal['tasks']]}
                db.execute('UPDATE runs SET status=?,data=? WHERE id=?',('paused',json.dumps(current),run['id']))
                db.execute("UPDATE entities SET data=? WHERE kind='goals' AND id=?",(json.dumps(newer),goal['id']))
                db.execute('INSERT INTO invocations(id,run_id,name,arguments,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    ('new-unknown',run['id'],'write_file','{}','outcome_unknown',None,'fixture','fixture'))
                db.execute('INSERT INTO run_events VALUES(?,?,?,?,?)',(run['id'],1,'status','{}','fixture'))
        finally:release.set()
        old=future.result(timeout=5)
    assert len(connections)==1 and old['status']=='queued' and old['events']==[]
    assert old['progress_summary']['completed']==1 and old['progress_summary']['phase']=='verify'
    assert old['progress_summary']['actions']==[]
    fresh=manager.poll(run['id'])
    assert fresh['status']=='paused' and len(fresh['events'])==1 and fresh['progress_summary']['completed']==2
    assert fresh['progress_summary']['phase']=='report' and fresh['progress_summary']['actions']==['inspect_interruption']
    assert len(connections)==2;assert_closed([('read',db) for db in connections])
