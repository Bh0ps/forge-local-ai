"""Actual command sessions bind exit evidence to commands rather than process IDs."""
import sys
import threading
import time

from project_tools import ProjectTools
from prompt_compiler import verification_packet,verification_outcome
from forge_runs import WORKFLOW
from test_forge_core import call
from test_forge501_workflow import fixture


def execute(svc,run,round_number,name,args):
    run=svc.store.update_run(run['id'],rounds=round_number)
    names={schema['function']['name']:schema for schema in ProjectTools.schemas()+WORKFLOW}
    wrapped=svc.jobs._execute_call(run,{'cancel':threading.Event(),'approval':None},call(name,args),0,names,{'capabilities':[]},time.monotonic())
    svc.jobs._tool_result(run,call(name,args),0,wrapped)
    svc.jobs._check_verification(run,[(name,args,wrapped)],names)
    return svc.store.run(run['id']),wrapped['result']


def test_repaired_same_command_new_session_clears_failure_but_not_task_coverage(tmp_path):
    svc,run,goal,task,_=fixture(tmp_path)
    root=tmp_path/'project'
    (root/'check.py').write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('ready.marker').is_file() else 1)\n")
    argv=[sys.executable,'check.py']
    try:
        run,first=execute(svc,run,1,'command_start',{'argv':argv,'cwd':'.','max_seconds':10})
        run,failed=execute(svc,run,2,'command_wait',{'id':first['id'],'timeout':5})
        assert failed['status']=='completed' and failed['exit_code']==1
        failure=verification_packet(run)['failures'][0]
        assert failure['invocation_id']==run['id']+':2:0'
        contract=next(iter(run['verification_feedback']['receipts'].values()))
        assert contract['contract_id']=='command-exit-v1' and contract['task_ids']==[] and contract['requirement_ids']==[]
        run,unrelated=execute(svc,run,3,'command_start',{'argv':[sys.executable,'-c','pass'],'max_seconds':10})
        run,_=execute(svc,run,4,'command_wait',{'id':unrelated['id'],'timeout':5})
        assert verification_packet(run)
        run,_=execute(svc,run,5,'write_file',{'path':'ready.marker','content':'Verified repair input'})
        run,second=execute(svc,run,6,'command_start',{'argv':argv,'cwd':'./','max_seconds':20})
        assert first['id']!=second['id']
        complete=svc.command_sessions.wait(second['id'],timeout=5)
        assert complete['status']=='completed' and complete['exit_code']==0
        run,_=execute(svc,run,7,'command_read',{'id':second['id'],'start':0,'limit':1000})
        assert verification_packet(run) is None
        current=svc.store.goal(goal['id'])
        svc.store.update_goal_task(goal['id'],task['id'],current['revision'],'completed',['The command exited zero.'])
        assert 'narrative evidence' in svc.jobs.workflow.completion_issues(run)[0]
        assert svc.jobs.workflow.progress(run)['verified']==0
        assert not svc.command_sessions.jobs
    finally:svc.shutdown()


def test_sync_and_managed_command_share_actual_canonical_exit_contract(tmp_path):
    svc,run,goal,task,_=fixture(tmp_path);root=tmp_path/'project'
    (root/'check.py').write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('ready.marker').is_file() else 1)\n")
    argv=[sys.executable,'check.py']
    try:
        run,failed=execute(svc,run,1,'run_command',{'argv':argv,'cwd':'.','timeout':5})
        assert failed['exit_code']==1 and verification_packet(run)
        run,_=execute(svc,run,2,'write_file',{'path':'ready.marker','content':'Repair'})
        run,started=execute(svc,run,3,'command_start',{'argv':argv,'cwd':'./','max_seconds':10})
        run,passed=execute(svc,run,4,'command_wait',{'id':started['id'],'timeout':5})
        assert passed['exit_code']==0 and verification_packet(run) is None
        receipt=next(value for value in run['verification_feedback']['receipts'].values() if value['contract_id']=='command-exit-v1')
        assert receipt['scope']['argv']==argv and receipt['scope']['cwd']
        assert receipt['task_ids']==[]
    finally:svc.shutdown()


def test_legacy_owned_session_failure_migrates_only_to_identical_command(tmp_path):
    from test_forge5_checkpoints import journal
    svc,run,goal,task,_=fixture(tmp_path);root=tmp_path/'project'
    (root/'check.py').write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('ready.marker').is_file() else 1)\n")
    argv=[sys.executable,'check.py']
    try:
        first=svc.command_sessions.start(root,argv,run_id=run['id'],max_seconds=10)
        failed=svc.command_sessions.wait(first['id'],timeout=5)
        assert failed['exit_code']==1
        run=svc.store.update_run(run['id'],rounds=1)
        invocation,artifact=journal(svc,run,1,0,'command_wait',{'id':first['id'],'timeout':5},failed)
        old=verification_outcome('command_wait',{'id':first['id'],'timeout':5},failed)
        with svc.store._connection() as db:message_id=db.execute('SELECT message_id FROM invocations WHERE id=?',(invocation,)).fetchone()[0]
        svc.store.update_run(run['id'],verification_feedback={'pending':{old['key']:{**old,'tool':'command_wait','version':0,'repeated':1,
            'invocation_id':invocation,'artifact_id':artifact,'message_id':message_id}}})
        run=svc.store.run(run['id'])
        run,_=execute(svc,run,2,'write_file',{'path':'ready.marker','content':'Repair'})
        run,second=execute(svc,run,3,'command_start',{'argv':argv,'max_seconds':10})
        run,_=execute(svc,run,4,'command_wait',{'id':second['id'],'timeout':5})
        assert verification_packet(run) is None
        assert any(event.get('migration')=='registered_contract' and event.get('cleared_invocation_id')==invocation for event in svc.store.events(run['id']))
    finally:svc.shutdown()


def test_model_claimed_command_coverage_is_not_accepted():
    scope={'argv':['check'],'cwd':'C:/owned','purpose':'command'}
    receipt=verification_outcome('command_wait',{}, {'id':'process','status':'completed','exit_code':0,'task_ids':['forged'],'requirement_ids':['forged']},
        {'id':'command-exit-v1','result_adapter':'command_exit','scope':scope,'task_ids':['forged'],'requirement_ids':['forged']})
    assert receipt['task_ids']==[] and receipt['requirement_ids']==[]
