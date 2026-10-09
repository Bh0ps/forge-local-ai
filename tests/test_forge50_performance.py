"""Phase accounting, scheduling fairness, calibration binding and migrations."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from forge_inference import InferenceQueue, ProviderPool
from forge_performance import adaptive_settings
from forge_store import ForgeStore


class Core:
    base='http://127.0.0.1:11434/api'
    def dispatch(self,action,data):
        if action=='models': return {'models':[{'name':'fixture','digest':'weights-a'}]}
        return {'capabilities':['tools'],'model_info':{'local.context_length':32768}}
    def stream_agent(self,data,cancel):
        yield {'message':{'tool_calls':[{'function':{'name':'read_file','arguments':{'path':'x'}}}]}}
        yield {'done':True,'message':{},'eval_count':4,'prompt_eval_count':12,'eval_duration':100000000,
               'prompt_eval_duration':200000000,'load_duration':50000000}


def test_tool_only_first_output_includes_queue_and_phase_provenance(tmp_path):
    store=ForgeStore(tmp_path); pool=ProviderPool(Core(),store); cancel=threading.Event()
    data={'provider_id':'ollama','model':'fixture','messages':[{'role':'user','content':'read'}],'tools':[]}
    done=threading.Event()
    with pool.queue.lease(threading.Event()):
        thread=threading.Thread(target=lambda:(list(pool.generate(data,cancel,{})),done.set()))
        thread.start(); time.sleep(.06)
    assert done.wait(2); thread.join()
    row=store.usage_requests()['requests'][0]; phases=row['phase_timings']
    assert phases['queue_seconds']>=.04
    assert phases['first_output_seconds']>=phases['queue_seconds']
    assert phases['first_visible_seconds'] is None
    assert phases['load_seconds']==.05 and phases['prefill_seconds']==.2
    assert phases['provenance']['prefill']=='reported'
    assert row['total_seconds']>=phases['queue_seconds']


def test_foreground_burst_cannot_starve_waiting_background():
    queue=InferenceQueue(); order=[]; threads=[]
    with queue.lease(threading.Event()):
        for name,background in [('background',True)]+[(str(n),False) for n in range(7)]:
            def work(label=name,bg=background):
                with queue.lease(threading.Event(),bg): order.append(label)
            thread=threading.Thread(target=work); threads.append(thread); thread.start()
            time.sleep(.005)
    for thread in threads: thread.join(2)
    assert len(order)==8 and order.index('background')<=4


def test_cache_detached_and_explicit_invalidation(tmp_path):
    pool=ProviderPool(Core(),ForgeStore(tmp_path))
    first=pool.models(); first[0]['name']='mutated'
    assert pool.models()[0]['name']=='fixture'
    info=pool.capabilities('ollama','fixture'); info['capabilities'].clear()
    assert pool.capabilities('ollama','fixture')['capabilities']==['tools']
    pool.invalidate(); assert not pool.model_cache and not pool.capability_cache


def test_adaptive_only_approved_profile_and_unlocked_settings(tmp_path):
    store=ForgeStore(tmp_path)
    profile=store.save_entity('calibrations',{'id':'cal-one','validated':True,'provider_id':'ollama',
        'model':'fixture','model_digest':'weights-a','median_total_seconds':1,
        'settings':{'model':'fixture','context':16384,'thinking':False}})
    current={**store.get_settings(),'model':'fixture','context':32768,'adaptive_enabled':True,'adaptive_profile_ids':[profile['id']]}
    locked=adaptive_settings(store,{},current,ProviderPool(Core(),store))
    assert locked['context']==32768
    unlocked=adaptive_settings(store,{}, {**current,'adaptive_context_locked':False},ProviderPool(Core(),store))
    assert unlocked['context']==16384
    assert adaptive_settings(store,{}, {**current,'adaptive_profile_ids':[]})['context']==32768
    stale=SimpleNamespace(models=lambda identifier:[{'name':'fixture','digest':'weights-b'}])
    assert adaptive_settings(store,{}, {**current,'adaptive_context_locked':False},stale)['context']==32768


def test_usage_pagination_and_local_calendar_filter(tmp_path):
    store=ForgeStore(tmp_path)
    for identifier,stamp in [('a','2026-10-06T23:30:00+00:00'),('b','2026-10-07T23:30:00+00:00')]:
        store.record_usage({'id':identifier,'model':'fixture','provider':'ollama','purpose':'main',
            'input_tokens':2,'output_tokens':3,'created_at':stamp,'phase_timings':{'queue_seconds':.1}})
    from datetime import datetime,timezone
    usage=store.usage('day','Europe/Dublin',now=datetime(2026,10,7,12,tzinfo=timezone.utc))
    assert usage['totals']['requests']==1
    page=store.usage_requests(limit=1)
    assert page['total']==2 and len(page['requests'])==1
    assert isinstance(page['requests'][0]['phase_timings'],dict)
    with store._connection() as db: assert db.execute('SELECT MAX(version) FROM forge_migrations').fetchone()[0]==6
