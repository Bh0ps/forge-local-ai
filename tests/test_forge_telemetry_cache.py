from concurrent.futures import ThreadPoolExecutor
import threading

from forge_inference import MemoryTelemetrySampler


def test_gpu_probe_is_shared_and_detached():
    entered=threading.Event(); release=threading.Event(); calls=[]
    def sample():
        calls.append(1); entered.set(); assert release.wait(3)
        return {'gpus':[{'name':'Fixture GPU'}], 'ram_available':4096, 'probe_state':'ok'}
    sampler=MemoryTelemetrySampler(sample, lambda: 10)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures=[executor.submit(sampler.read) for _ in range(8)]
        assert entered.wait(2); release.set(); results=[future.result(3) for future in futures]
    assert len(calls)==1 and all(result['probe_state']=='ok' for result in results)
    results[0]['gpus'][0]['name']='modified'
    assert sampler.read()['gpus'][0]['name']=='Fixture GPU'


def test_expired_concurrent_refresh_is_explicitly_stale_and_nonblocking():
    now=[10]; entered=threading.Event(); release=threading.Event(); calls=[]
    def sample():
        calls.append(1)
        if len(calls)>1: entered.set(); assert release.wait(3)
        return {'gpus':[{'name':'Fixture GPU'}], 'probe_state':'ok'}
    sampler=MemoryTelemetrySampler(sample, lambda: now[0])
    sampler.read(); now[0]=16
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(sampler.read); assert entered.wait(2)
        stale=sampler.read()
        assert stale['stale'] is True and stale['probe_state']=='refreshing' and stale['sample_age_seconds']==6
        release.set(); assert future.result(3)['stale'] is False


def test_failed_probe_does_not_advertise_previous_gpu_as_current():
    now=[10]; calls=[]
    def sample():
        calls.append(1)
        if len(calls)>1: raise OSError('Synthetic probe unavailable')
        return {'gpus':[{'name':'Fixture GPU'}], 'probe_state':'ok'}
    sampler=MemoryTelemetrySampler(sample, lambda: now[0])
    assert sampler.read()['gpus']; now[0]=16
    failed=sampler.read()
    assert failed['gpus']==[] and failed['probe_state']=='failed' and not failed['stale']
    assert len(calls)==2 and sampler.read()['probe_state']=='failed'


def test_clock_rollback_and_unknown_gpu_remain_bounded_and_honest():
    now=[10]; calls=[]
    def sample():
        calls.append(1); return {'gpus':[], 'ram_available':4096, 'probe_state':'unavailable'}
    sampler=MemoryTelemetrySampler(sample, lambda: now[0])
    assert sampler.read()['probe_state']=='unavailable'; now[0]=9
    assert sampler.read()['sample_age_seconds']==0 and len(calls)==1
