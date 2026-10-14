"""Project appearance reads stay bounded and coherent without caching."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
from forge_store import ForgeStore
from forge_workspace_ui import WorkspaceUI

def projects(store,count):
    with store._connection(transaction='write') as db:
        for i in range(count):
            identity=f'{i:032x}'
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)',(identity,'same' if i%2 else 'SAME','Synthetic private folder','2026-10-14','2026-10-14'))
            if i%3==0:db.execute('INSERT INTO entities VALUES(?,?,?)',('project_appearance',identity,json.dumps({'appearance':{'icon':'code','accent':'copper'}})))
            elif i%3==1:db.execute('INSERT INTO entities VALUES(?,?,?)',('project_appearance',identity,'{"appearance":{"icon":"unknown"}}'))
        db.execute('INSERT INTO entities VALUES(?,?,?)',('project_appearance','orphan','{"private":"Synthetic orphan"}'))

@pytest.mark.parametrize('count',[0,1,20,200])
def test_project_catalog_reads_one_snapshot_and_preserves_payload_order_defaults(tmp_path,count,monkeypatch):
    store=ForgeStore(tmp_path);projects(store,count)
    expected=[WorkspaceUI(store).project(project) for project in store.list_projects()]
    original=store._connection;opened=[]
    @contextmanager
    def connection(**kwargs):
        opened.append(kwargs.get('transaction'))
        with original(**kwargs) as db:yield db
    monkeypatch.setattr(store,'_connection',connection)
    actual=WorkspaceUI(store).projects()
    assert actual==expected and opened==['read']
    assert all(set(row)=={'id','name','path','created_at','updated_at','appearance'} for row in actual)
    assert [row['id'] for row in actual]==[f'{i:032x}' for i in range(count)]

def test_concurrent_project_appearance_change_cannot_mix_the_current_snapshot(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path);projects(store,1);identity='0'*32
    original=store._connection;changed=False
    class DB:
        def __init__(self,db):self.db=db
        def execute(self,sql,args=()):
            nonlocal changed
            if sql.startswith('SELECT e.id,e.data') and not changed:
                changed=True
                with original(transaction='write') as writer:
                    writer.execute('UPDATE projects SET name=? WHERE id=?',('New current project',identity))
                    writer.execute("UPDATE entities SET data=? WHERE kind='project_appearance' AND id=?",(json.dumps({'appearance':{'icon':'globe','accent':'sage'}}),identity))
            return self.db.execute(sql,args)
    @contextmanager
    def connection(**kwargs):
        with original(**kwargs) as db:yield DB(db)
    monkeypatch.setattr(store,'_connection',connection)
    old=WorkspaceUI(store).projects()[0]
    assert old['name']=='SAME' and old['appearance']=={'icon':'code','accent':'copper'}
    new=WorkspaceUI(store).projects()[0]
    assert new['name']=='New current project' and new['appearance']=={'icon':'globe','accent':'sage'}

@pytest.mark.parametrize('saved',['not-json','null','[]','{}','{"appearance":null}',
    '{"appearance":{"icon":"folder","accent":"invalid"}}','{"appearance":{"icon":"folder","private":"hidden"}}'])
def test_malformed_cosmetic_record_uses_existing_default_without_exposing_fields(tmp_path,saved):
    store=ForgeStore(tmp_path);projects(store,1)
    with store._connection(transaction='write') as db:
        db.execute("UPDATE entities SET data=? WHERE kind='project_appearance'",(saved,))
    result=WorkspaceUI(store).projects()
    assert result[0]['appearance']=={'icon':'folder'} and 'hidden' not in json.dumps(result)
