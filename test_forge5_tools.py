"""Command lifecycle and transactional file operations in disposable folders."""
import json
import sys
import threading

import pytest

from command_sessions import CommandSessionManager
from project_tools import ProjectTools


def test_read_ranges_and_related_hash_guarded_patch(tmp_path):
    root=tmp_path/'project'; root.mkdir(); tools=ProjectTools(root,tmp_path/'backups')
    (root/'a.txt').write_text('alpha\nbeta\n'); (root/'b.txt').write_text('before\n')
    first=tools.read_file('a.txt'); second=tools.read_file('b.txt')
    read=tools.read_files([{'path':'a.txt','start_line':2,'end_line':2},{'path':'b.txt'}])
    assert read['files'][0]['content'].splitlines()==['beta'] and read['files'][1]['sha256']==second['sha256']
    patched=tools.execute('apply_patch',{'changes':[
        {'path':'a.txt','expected_sha256':first['sha256'],'edits':[{'old_text':'alpha','new_text':'updated'}]},
        {'path':'b.txt','expected_sha256':second['sha256'],'content':'after\n'},
        {'path':'new.txt','expected_sha256':'missing','content':'new'}]})
    assert patched['ok'] and len(patched['changes'])==3
    assert (root/'a.txt').read_text()=='updated\nbeta\n' and (root/'new.txt').read_text()=='new'
    assert all(change['backup_id'] for change in patched['changes'])


def test_patch_validates_all_files_before_writing(tmp_path):
    root=tmp_path/'project'; root.mkdir(); tools=ProjectTools(root,tmp_path/'backups')
    (root/'a.txt').write_text('original')
    digest=tools.read_file('a.txt')['sha256']
    result=tools.execute('apply_patch',{'changes':[
        {'path':'a.txt','expected_sha256':digest,'content':'changed'},
        {'path':'missing.txt','expected_sha256':'0'*64,'content':'must not create'}]})
    assert not result['ok'] and (root/'a.txt').read_text()=='original'
    assert not (root/'missing.txt').exists()


def test_patch_rolls_back_if_later_commit_fails(tmp_path,monkeypatch):
    root=tmp_path/'project'; root.mkdir(); tools=ProjectTools(root,tmp_path/'backups')
    for name in ('a','b'): (root/name).write_text('old')
    original=tools._commit
    def commit(target,old,new):
        if target.name=='b' and new==b'new': raise OSError('Simulated disk failure')
        return original(target,old,new)
    monkeypatch.setattr(tools,'_commit',commit)
    result=tools.execute('apply_patch',{'changes':[{'path':name,'expected_sha256':tools.read_file(name)['sha256'],'content':'new'} for name in ('a','b')]})
    assert result['rolled_back'] and not result['outcome_unknown']
    assert all((root/name).read_text()=='old' for name in ('a','b'))


def test_fast_search_glob_and_ignore_case(tmp_path):
    root=tmp_path/'project'; root.mkdir(); tools=ProjectTools(root,tmp_path/'backups')
    (root/'a.tsx').write_text('Needle value'); (root/'b.py').write_text('Needle excluded')
    result=tools.search_files('needle',glob='*.tsx',case_sensitive=False)
    assert [m['path'] for m in result['matches']]==['a.tsx']


def test_command_session_output_identity_and_exit_status(tmp_path):
    root=tmp_path/'project'; root.mkdir(); manager=CommandSessionManager(tmp_path/'forge')
    try:
        session=manager.start(root,[sys.executable,'-c','print("CHECKED",flush=True)'],run_id='run-a',owner_id='owner-a',source_key='action-a')
        assert session['pid'] and session['project_root']==str(root.resolve())
        done=manager.wait(session['id'],timeout=10)
        assert done['status']=='completed' and done['exit_code']==0 and done['owner_id']=='owner-a'
        output=manager.read(session['id'])
        assert 'CHECKED' in output['output'] and output['next']==output['next_start']
        reused=manager.start(root,session['argv'],source_key='action-a')
        assert reused['id']==session['id'] and reused['reused']
    finally: manager.shutdown()


def test_command_stop_and_cancellable_wait(tmp_path):
    root=tmp_path/'project'; root.mkdir(); manager=CommandSessionManager(tmp_path/'forge')
    try:
        session=manager.start(root,[sys.executable,'-c','import time; print("START",flush=True); time.sleep(30)'])
        cancel=threading.Event(); cancel.set()
        assert manager.wait(session['id'],timeout=30,cancel=cancel)['status']=='running'
        assert manager.stop(session['id'])['status']=='stopped'
        assert not manager.jobs
    finally: manager.shutdown()


def test_restart_never_adopts_or_kills_stale_pid(tmp_path):
    state=tmp_path/'forge/state/commands'; state.mkdir(parents=True)
    identity='a'*32
    (state/(identity+'.json')).write_text(json.dumps({'id':identity,'status':'running','pid':1,'argv':['stale'],
        'output_bytes':0,'exit_code':None,'truncated':False}),encoding='utf-8')
    manager=CommandSessionManager(tmp_path/'forge')
    try:
        assert manager.status(identity)['status']=='interrupted' and not manager.jobs
        assert 'not adopted' in manager.status(identity)['recovery']
    finally: manager.shutdown()


def test_command_missing_executable_is_known_not_executed(tmp_path):
    root=tmp_path/'project'; root.mkdir(); manager=CommandSessionManager(tmp_path/'forge')
    try:
        result=manager.start(root,['forge-test-executable-that-does-not-exist'])
        assert result['status']=='failed' and result['not_executed'] and not manager.jobs
        with pytest.raises(ValueError): manager.start(root,[sys.executable],cwd='..')
    finally: manager.shutdown()


def test_binary_artifact_export_and_restore_preserve_guards(tmp_path):
    root=tmp_path/'project'; root.mkdir(); tools=ProjectTools(root,tmp_path/'backups')
    first=tools.write_bytes('fixture.pdf',b'%PDF-1.4\x00\xff')
    second=tools.write_bytes('fixture.pdf',b'%PDF-1.4\x00\xf0',first['sha256'])
    assert 'Binary artifact' in second['diff']
    tools.restore_file('fixture.pdf',second['backup_id'])
    assert (root/'fixture.pdf').read_bytes()==b'%PDF-1.4\x00\xff'
    with pytest.raises(ValueError): tools.write_bytes('../escaped.pdf',b'bytes')
    with pytest.raises(ValueError): tools.write_bytes('fixture.pdf',b'bytes','missing')


def test_command_running_journal_failure_stops_owned_process(tmp_path,monkeypatch):
    import command_sessions
    root=tmp_path/'project'; root.mkdir(); manager=CommandSessionManager(tmp_path/'forge')
    original_save=manager._save; original_popen=command_sessions.subprocess.Popen
    processes=[]; saves=0
    def save(record):
        nonlocal saves
        saves+=1
        if saves==2: raise OSError('Simulated journal write failure')
        original_save(record)
    def popen(*args,**kwargs):
        process=original_popen(*args,**kwargs); processes.append(process); return process
    monkeypatch.setattr(manager,'_save',save)
    monkeypatch.setattr(command_sessions.subprocess,'Popen',popen)
    try:
        result=manager.start(root,[sys.executable,'-c','import time; time.sleep(30)'])
        assert result['outcome_unknown'] and result['persistence_error'] and result['status']=='interrupted'
        assert processes[0].poll() is not None and not manager.jobs
        assert manager.status(result['id'])['persistence_error']
    finally: manager.shutdown()
