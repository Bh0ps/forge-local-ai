"""Chat/project lifecycle preserves folders, usage and original action journals."""
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from forge_chats import dispatch_chat
from forge_service import ForgeService
from forge_store import ForgeStore
from test_forge_workspaces import ScriptedEngine


@pytest.fixture
def service(tmp_path):
    svc=ForgeService(core=ScriptedEngine(),data_dir=tmp_path/'forge')
    svc.store.update_settings({'model':'fixture','permission_profile':'full_access'})
    svc.jobs._launch=lambda run:None
    yield svc
    svc.shutdown()


def project(service,tmp_path,name):
    folder=tmp_path/name;folder.mkdir();(folder/'preserved.txt').write_text('original project contents')
    return service.create_project({'name':name,'path':str(folder)})


def run(service,project_id=None,chat_id=None,status='paused',parent_id=None):
    result=service.jobs.start({'text':'Disposable workflow','project_id':project_id,'chat_id':chat_id,'parent_id':parent_id})
    service.store.update_run(result['id'],status=status)
    return result


def test_schema4_backup_is_consistent_and_keeps_chat_project_identity(tmp_path):
    home=tmp_path/'forge';store=ForgeStore(home)
    folder=tmp_path/'existing';folder.mkdir()
    proj=store.create_project('Existing',str(folder));chat=store.create_chat(proj['id'],'Saved history','fixture')
    store.add_message(chat['id'],'user','Synthetic old history')
    # Reproduce the prior schema without any new archive column.
    with sqlite3.connect(store.db_path) as db:
        db.execute('DROP INDEX chats_archived_updated')
        db.execute('ALTER TABLE chats DROP COLUMN archived')
        db.execute('DELETE FROM forge_migrations WHERE version>=4')
        for table in ('memory_vectors','memory_fts','memory_items','memory_skills','channel_inbound','channel_routes','channel_checkpoints','channel_outbox','channel_callbacks','channel_notifications','channel_replays','channel_event_journal','request_keys'):
            db.execute('DROP TABLE '+table)
        db.execute("DELETE FROM forge_settings WHERE key='setup_origin'")
    upgraded=ForgeStore(home)
    assert upgraded.get_chat(chat['id'])['archived'] is False
    assert upgraded.get_project(proj['id'])['path']==str(folder)
    backups=list((home/'backups').glob('pre-schema-6-*/forge.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as snapshot:
        assert snapshot.execute('SELECT MAX(version) FROM forge_migrations').fetchone()[0]==3
        assert snapshot.execute('SELECT project_id FROM chats WHERE id=?',(chat['id'],)).fetchone()[0]==proj['id']
        assert snapshot.execute('SELECT content FROM messages').fetchone()[0]=='Synthetic old history'
        assert 'archived' not in [row[1] for row in snapshot.execute('PRAGMA table_info(chats)')]
    ForgeStore(home)
    assert len(list((home/'backups').glob('pre-schema-6-*/forge.sqlite3')))==1


def test_archive_restore_lists_boolean_state_and_prevents_running_or_resuming(service):
    saved=run(service)
    chat_id=saved['chat_id']
    archived=dispatch_chat(service,'chat_archive',{'id':chat_id})['chat']
    assert archived['archived'] is True
    assert service.store.list_chats()==[]
    assert service.store.list_chats(archived=True)[0]['archived'] is True
    assert len(service.store.list_chats(archived=None))==1
    with pytest.raises(ValueError,match='Restore'):service.jobs.resume(saved['id'])
    with pytest.raises(ValueError,match='Restore'):service.jobs.start({'text':'Another turn','chat_id':chat_id})
    restored=dispatch_chat(service,'chat_archive',{'id':chat_id,'archived':False})['chat']
    assert restored['archived'] is False
    assert service.jobs.resume(saved['id'])['status']=='queued'


def test_archive_blocks_active_work_and_accepts_only_actual_boolean(service):
    saved=run(service,status='running')
    with pytest.raises(ValueError,match='active'):dispatch_chat(service,'chat_archive',{'id':saved['chat_id']})
    assert not service.store.get_chat(saved['chat_id'])['archived']
    with pytest.raises(ValueError,match='true or false'):dispatch_chat(service,'chat_archive',{'id':saved['chat_id'],'archived':'false'})


def test_move_stops_paused_runs_preserves_journal_original_project_and_future_scope(service,tmp_path):
    original=project(service,tmp_path,'original');target=project(service,tmp_path,'target')
    saved=run(service,original['id'])
    service.store.event(saved['id'],'checkpoint',text='Original project action')
    dispatch_chat(service,'chat_move',{'id':saved['chat_id'],'project_id':target['id']})
    assert service.store.get_chat(saved['chat_id'])['project_id']==target['id']
    history=service.store.run(saved['id'])
    assert history['project_id']==original['id'] and history['status']=='cancelled'
    assert service.store.events(saved['id'])[0]['text']=='Original project action'
    future=service.jobs.start({'text':'Work in the new project','chat_id':saved['chat_id']})
    assert service.store.run(future['id'])['project_id']==target['id']
    assert (Path(original['path'])/'preserved.txt').read_text()=='original project contents'


@pytest.mark.parametrize('action',['chat_move','chat_delete'])
def test_unknown_side_effect_blocks_move_and_delete_and_keeps_inspection_journal(service,tmp_path,action):
    original=project(service,tmp_path,'original');target=project(service,tmp_path,'target')
    saved=run(service,original['id'])
    service.store.invocation('unknown',saved['id'],'write_file',{'path':'preserved.txt'})
    service.store.invocation_state('unknown','outcome_unknown',{'error':'Stopped after writing'})
    with pytest.raises(ValueError,match='unknown'):
        dispatch_chat(service,action,{'id':saved['chat_id'],'project_id':target['id']})
    assert service.store.get_chat(saved['chat_id'])['project_id']==original['id']
    assert service.store.unknown_actions(saved['id'])[0]['id']=='unknown'


def test_still_live_thread_refuses_mutation_after_join_timeout(service):
    saved=run(service,status='running')
    class Thread:
        def __init__(self):self.joined=False
        def is_alive(self):return True
        def join(self,timeout):self.joined=True
    thread=Thread()
    service.jobs.jobs[saved['id']]={'thread':thread,'cancel':threading.Event(),'pause':False,'approval':None}
    try:
        with pytest.raises(ValueError,match='stopping'):dispatch_chat(service,'chat_delete',{'id':saved['chat_id']})
        assert thread.joined and service.store.get_chat(saved['chat_id'])
    finally:service.jobs.jobs.pop(saved['id'])


def test_delete_removes_protocol_rows_but_keeps_usage_goals_plans_children(service,tmp_path):
    proj=project(service,tmp_path,'project');saved=run(service,proj['id'],status='completed')
    service.store.invocation('completed',saved['id'],'read_file',{'path':'preserved.txt'})
    service.store.invocation_state('completed','completed',{'content':'original'})
    service.store.tool_message('completed',0,'read_file','{"content":"original"}')
    service.store.event(saved['id'],'tool',state='done')
    goal=service.store.save_goal({'title':'Keep goal artifact','chat_id':saved['chat_id'],'project_id':proj['id'],
        'tasks':[{'text':'Ordered task','status':'pending'}]})
    goal_bytes=Path(goal['path']).read_bytes()
    service.store.save_entity('plans',{'id':saved['id'],'chat_id':saved['chat_id'],'run_id':saved['id'],'markdown':'Ordered plan'})
    service.store.record_usage({'id':'usage','run_id':saved['id'],'project_id':proj['id'],'provider':'ollama','model':'fixture',
        'purpose':'main','input_tokens':10,'output_tokens':20})
    child=run(service,proj['id'],parent_id=saved['id'])
    with service.store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',('approval',saved['id'],'completed','hash','approved','{}'))
        db.execute('INSERT INTO occurrences VALUES(?,?,?)',('schedule','slot',saved['id']))
    dispatch_chat(service,'chat_delete',{'id':saved['chat_id']})
    with pytest.raises(ValueError,match='Chat not found'):service.store.get_chat(saved['chat_id'])
    with pytest.raises(ValueError,match='Run not found'):service.store.run(saved['id'])
    with service.store._connection() as db:
        for table in ('messages','runs','run_events','invocations','approvals'):
            column='chat_id' if table in ('messages','runs') else 'run_id'
            identifier=saved['chat_id'] if column=='chat_id' else saved['id']
            assert db.execute('SELECT COUNT(*) FROM '+table+' WHERE '+column+'=?',(identifier,)).fetchone()[0]==0
        assert tuple(db.execute('SELECT run_id,output_tokens FROM usage').fetchone())==(None,20)
        assert db.execute('SELECT run_id FROM occurrences').fetchone()[0] is None
    assert service.store.usage()['totals']['output_tokens']==20
    assert Path(goal['path']).read_bytes()==goal_bytes
    assert service.store.goal(goal['id'])['chat_id'] is None
    assert service.store.entity('plans',saved['id'])['run_id'] is None
    assert service.store.run(child['id'])['parent_id'] is None
    assert service.store.run(child['id'])['status']=='cancelled'


def test_project_delete_preserves_folder_unscopes_archived_chats_and_blocks_saved_work(service,tmp_path):
    proj=project(service,tmp_path,'registered');saved=run(service,proj['id'])
    dispatch_chat(service,'chat_archive',{'id':saved['chat_id']})
    task=service.store.create_task(proj['id'],'Retain task history')
    goal=service.store.save_goal({'title':'Retain goal','chat_id':saved['chat_id'],'project_id':proj['id'],
        'tasks':[{'text':'Inspect original folder','status':'pending'}]})
    service.store.save_entity('spaces',{'id':'space','project_ids':[proj['id']]})
    service.store.save_entity('schedules',{'id':'schedule','project_id':proj['id'],'enabled':True})
    result=dispatch_chat(service,'project_delete',{'id':proj['id']})
    assert result['folder_preserved'] and Path(proj['path']).is_dir()
    assert (Path(proj['path'])/'preserved.txt').read_text()=='original project contents'
    with pytest.raises(ValueError,match='Project not found'):service.store.get_project(proj['id'])
    chat=service.store.get_chat(saved['chat_id']);assert chat['project_id'] is None and chat['archived'] is True
    assert service.store.run(saved['id'])['project_id']==proj['id']
    assert service.store.goal(goal['id'])['project_missing']
    assert Path(goal['path']).is_file()
    assert not service.store.entity('schedules','schedule')['enabled']
    assert service.store.entity('spaces','space')['project_ids']==[]
    archived=service.store.entity('project_archives',proj['id'])
    assert archived['tasks'][0]['id']==task['id'] and archived['project']['path']==proj['path']


def test_project_delete_refuses_active_worktrees_without_changing_checkout(service,tmp_path):
    proj=project(service,tmp_path,'registered')
    service.store.save_entity('worktrees',{'id':'tree','parent_project_id':proj['id'],'project_id':'child','status':'active'})
    with pytest.raises(ValueError,match='worktree'):dispatch_chat(service,'project_delete',{'id':proj['id']})
    assert service.store.get_project(proj['id']) and Path(proj['path']).is_dir()
