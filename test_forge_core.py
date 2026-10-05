"""Continuity and accounting tests exercise the durable coordinator contract."""
from datetime import datetime,timezone
import json
from pathlib import Path
import threading
import time

import pytest
from forge_service import ForgeService
from forge_store import ForgeStore

class Engine:
    base='http://127.0.0.1:11434/api'
    def __init__(self,rounds=None,summary_fail=False): self.rounds=list(rounds or []); self.requests=[]; self.summary_fail=summary_fail
    def dispatch(self,action,data):
        if action=='show': return {'capabilities':['tools','vision','thinking'],'model_info':{'local.context_length':262144}}
        if action=='models': return {'models':[{'name':'fixture'}]}
        raise ValueError(action)
    def stream_agent(self,data,cancel):
        self.requests.append(data)
        if any('continuity summary' in m['content'] for m in data['messages'] if m['role']=='system'):
            if self.summary_fail: raise ValueError('Summary failed')
            yield {'message':{'content':'Saved progress 漢字 🌍.'},'done':True,'eval_count':8,'prompt_eval_count':200,'eval_duration':1000000000}; return
        response=self.rounds.pop(0) if self.rounds else {'content':'Finished.'}
        if callable(response): yield from response(data,cancel); return
        yield {'message':response,'done':True,'done_reason':'stop','eval_count':12,'prompt_eval_count':250,'prompt_eval_cached_count':100,'eval_duration':2000000000}

def call(name,args): return {'function':{'name':name,'arguments':args}}

def service(tmp_path,engine=None):
    svc=ForgeService(core=engine or Engine(),data_dir=tmp_path/'forge')
    svc.store.update_settings({'model':'fixture','context':8192})
    return svc

def finished(svc,run):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        result=svc.dispatch('poll',{'id':run['id']})
        if result['finished']: return result
        time.sleep(.02)
    raise AssertionError('Run did not finish')

def test_journal_replays_without_consuming_and_usage_deduplicates(tmp_path):
    svc=service(tmp_path); run=svc.jobs.start({'text':'Hello 🌍'})
    result=finished(svc,run); assert result['status']=='completed'
    replay=svc.jobs.poll(run['id']); assert replay['events']==result['events']
    assert svc.jobs.poll(run['id'],replay['next_cursor'])['events']==[]
    usage=svc.dispatch('usage',{}); assert usage['totals']['requests']==1
    assert usage['totals']['output_tokens']==12 and usage['average_tps']==6
    assert usage['totals']['cached_input_tokens']==100
    svc.shutdown(); restored=service(tmp_path)
    assert restored.store.usage()['totals']['requests']==1
    assert restored.jobs.poll(run['id'])['events']==replay['events']
    restored.shutdown()

def test_always_ask_binds_exact_file_action_then_no_replay_on_resume(tmp_path):
    project=tmp_path/'project'; project.mkdir()
    engine=Engine([{'tool_calls':[call('write_file',{'path':'hello.txt','content':'hello'})]}, {'content':'Done.'}])
    svc=service(tmp_path,engine); registered=svc.create_project({'name':'Project','path':str(project)})
    run=svc.jobs.start({'text':'Write hello','project_id':registered['id']})
    deadline=time.monotonic()+10; pending=None
    while time.monotonic()<deadline:
        pending=next((e for e in svc.jobs.poll(run['id'])['events'] if e['type']=='approval'),None)
        if pending: break
        time.sleep(.02)
    assert pending and not (project/'hello.txt').exists()
    assert pending['arguments']=={'path':'hello.txt','content':'hello'} and pending['target']==str(project)
    with pytest.raises(ValueError): svc.jobs.approve(run['id'],'wrong',True)
    svc.jobs.approve(run['id'],pending['id'],True)
    assert finished(svc,run)['status']=='completed'
    assert (project/'hello.txt').read_text()=='hello'
    with svc.store._connection() as db: assert db.execute("SELECT COUNT(*) FROM invocations WHERE status='completed'").fetchone()[0]==1
    svc.shutdown()

def test_deny_access_and_plan_never_expose_mutation_tools(tmp_path):
    engine=Engine(); svc=service(tmp_path,engine)
    svc.store.update_settings({'permission_profile':'deny_access'})
    run=svc.jobs.start({'text':'Hello'}); finished(svc,run)
    assert engine.requests[0]['tools']==[]
    svc.store.update_settings({'permission_profile':'full_access'})
    folder=tmp_path/'project'; folder.mkdir(); project=svc.create_project({'name':'p','path':str(folder)})
    run=svc.jobs.start({'text':'Plan edits','mode':'plan','project_id':project['id']}); finished(svc,run)
    names={s['function']['name'] for s in engine.requests[-1]['tools']}
    assert 'read_file' in names and not {'write_file','run_command'}&names
    svc.shutdown()

def test_unicode_large_batch_compacts_and_retains_exact_request(tmp_path):
    folder=tmp_path/'project'; folder.mkdir()
    for n in range(4): (folder/f'{n}.txt').write_text('漢字🌍'*7000,encoding='utf-8')
    engine=Engine([{'tool_calls':[call('read_file',{'path':f'{n}.txt'}) for n in range(4)]},{'content':'Verified.'}],summary_fail=True)
    svc=service(tmp_path,engine); svc.store.update_settings({'context':4096})
    project=svc.create_project({'name':'p','path':str(folder)}); request='Inspect all four files. Keep this exact request 🌍.'
    run=svc.jobs.start({'text':request,'project_id':project['id']})
    result=finished(svc,run); assert result['status']=='completed',result
    assert any(e['type']=='compacted' for e in result['events'])
    assert any(m['content']==request for m in engine.requests[-1]['messages'])
    usage=svc.store.usage(); assert usage['totals']['requests']==5 # two main + three failed summary requests
    assert len(list((svc.store.home/'artifacts').glob('*.json')))==4
    for file in (svc.store.home/'artifacts').glob('*.json'): assert len(file.read_text(encoding='utf-8'))>20000
    svc.shutdown()

def test_unknown_outcome_requires_inspection_and_never_reexecutes(tmp_path):
    svc=service(tmp_path); chat=svc.store.create_chat()
    run=svc.store.create_run(dict(chat_id=chat['id'],settings=svc.store.get_settings(),request='Request',rounds=1,tools=0,output_tokens=0,
        pending_calls=[call('write_file',{'path':'x','content':'x'})],next_tool=0))
    invocation=run['id']+':1:0'; svc.store.invocation(invocation,run['id'],'write_file',{'path':'x','content':'x'})
    svc.store.invocation_state(invocation,'running'); svc.store.recover()
    with pytest.raises(ValueError,match='unknown'): svc.jobs.resume(run['id'])
    svc.resolve_action({'invocation_id':invocation,'outcome':'completed','evidence':'Inspected target and verified contents.'})
    resumed=svc.jobs.resume(run['id']); finished(svc,resumed)
    with svc.store._connection() as db: assert db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]==1
    svc.shutdown()

def test_todo_external_edits_are_not_overwritten_and_import_reconciles(tmp_path):
    svc=service(tmp_path); goal=svc.goal_create({'text':'First step\nSecond step'})
    assert len(goal['tasks'])==2 and svc.store.runs()==[]
    Path(goal['path']).write_text('# Edited\n1. [x] First step\n2. [ ] Changed next step\n',encoding='utf-8')
    with pytest.raises(ValueError,match='externally'): svc.store.save_goal({**goal,'checkpoint':'New'})
    goal=svc.goal_reconcile({'id':goal['id'],'mode':'import'})
    assert goal['tasks'][0]['status']=='completed' and goal['tasks'][1]['text']=='Changed next step'
    assert not goal['external_edits']; svc.shutdown()

def test_calendar_usage_counts_local_midnight_cached_and_zero_duration(tmp_path):
    store=ForgeStore(tmp_path/'forge')
    common=dict(provider='ollama',model='fixture',purpose='main',input_tokens=10,output_tokens=5,cached_input_tokens=2,estimated=False,decode_seconds=0)
    record={**common,'id':'a','created_at':'2026-09-30T23:30:00+00:00'}
    store.record_usage(record); store.record_usage(record)
    store.record_usage({**common,'id':'b','created_at':'2026-10-01T01:30:00+00:00'})
    usage=store.usage('month','Europe/Dublin',now=datetime(2026,10,1,4,tzinfo=timezone.utc))
    assert usage['totals']['requests']==2 and usage['totals']['output_tokens']==10
    assert usage['daily'][0]['date']=='2026-10-01' and usage['average_tps'] is None
    assert store.usage('month','UTC',now=datetime(2026,10,1,4,tzinfo=timezone.utc))['totals']['requests']==1

def test_one_writer_and_single_gpu_lease(tmp_path):
    entered=threading.Event(); release=threading.Event(); active=0; maximum=0; lock=threading.Lock()
    def blocked(data,cancel):
        nonlocal active,maximum
        with lock: active+=1; maximum=max(maximum,active)
        entered.set(); release.wait(5)
        with lock: active-=1
        yield {'message':{'content':'done'},'done':True}
    svc=service(tmp_path,Engine([blocked,blocked])); first=svc.jobs.start({'text':'First'})
    assert entered.wait(3)
    with pytest.raises(ValueError,match='already'): svc.jobs.start({'text':'Duplicate','chat_id':first['chat_id']})
    second=svc.jobs.start({'text':'Second'}); time.sleep(.15); assert maximum==1
    release.set(); finished(svc,first); finished(svc,second); assert maximum==1; svc.shutdown()
