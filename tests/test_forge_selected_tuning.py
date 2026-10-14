"""Offline selected-model authority, measured comparisons and cancellation."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

import forge_performance as performance
from forge_inference import ProviderPool
from forge_store import ForgeStore
from forge_tuning import selected, thread_candidates, headroom_reason, compare

GIB=1024**3
CODE='def sum_even(values):\n    return sum(x for x in values if x % 2 == 0)'

def hardware():
    return {'cpu':{'name':'Fixture CPU','physical_cores':8,'logical_cores':16},
            'ram':{'total_bytes':32*GIB,'used_bytes':10*GIB,'available_bytes':22*GIB},
            'gpus':[{'name':'Fixture GPU','total_bytes':8*GIB,'used_bytes':2*GIB,'free_bytes':6*GIB}],
            'probe_state':'ok','stale':False}

class Core:
    base='http://127.0.0.1:11434/api'
    def __init__(self):
        self.catalog=[{'name':'fixture','digest':'a'*64,'size':2*GIB}]
        self.version='fixture-engine-1';self.payloads=[];self.model_reads=0
        self.info={'capabilities':['tools','vision','thinking'],'thinking':{'values':[False,True]},
                   'model_info':{'fixture.context_length':32768}}
    def dispatch(self,action,data):
        if action=='models':self.model_reads+=1;return {'models':deepcopy(self.catalog)}
        if action=='show':return deepcopy(self.info)
        raise AssertionError('Unexpected fixture action')
    def request(self,method,path):
        assert method=='GET'
        if path=='/version':return {'version':self.version}
        if path=='/ps':return {'models':[]}
        raise AssertionError('Unexpected fixture request')
    def _stream_payload(self,payload,cancel):
        self.payloads.append(deepcopy(payload))
        yield {'message':{'thinking':'Fixture reasoning','content':CODE}}
        yield {'done':True,'message':{},'eval_count':20,'prompt_eval_count':40,
               'eval_duration':2_000_000_000,'prompt_eval_duration':200_000_000}

@pytest.fixture
def manager(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path)
    store.update_settings({'model':'fixture','provider_id':'ollama','context':32768,'thinking':True,
                           'tokens':4096,'temperature':.4,'num_thread':4})
    core=Core();pool=ProviderPool(core,store)
    service=SimpleNamespace(store=store,providers=pool,jobs=SimpleNamespace(lock=threading.RLock(),jobs={}),
                            admission_lock=threading.RLock(),_native_io_pending=0,_performance_io_pending=0)
    monkeypatch.setattr(performance,'hardware',hardware)
    value=performance.PerformanceManager(service)
    yield value
    value.jobs.clear();value.shutdown()

def profile(manager,**extra):
    settings=manager.store.get_settings()
    facts=performance.calibration_facts(manager.store,manager.service.providers,settings)
    return manager.store.save_entity('calibrations',{'id':'measured',**facts['selected'],
        'validated':True,'immutable_identity':True,'binding':facts['binding'],'model_digest':facts['model_digest'],
        'median_total_seconds':1,'settings':{'num_thread':8,'thinking':False},**extra})

def sample(tps=10,**extra):
    return {'status':'passed','tps':tps,'estimated':False,'timing_provenance':'engine_reported',
        'decode_seconds':20/tps,'prefill_seconds':.2,'ttft_seconds':.1,'total_seconds':20/tps+.3,
        'input_tokens':40,'output_tokens':20,**extra}


def metadata_reads(manager,monkeypatch):
    core=manager.service.providers.core;reads={'models':0,'show':0,'version':0}
    dispatch=core.dispatch;request=core.request
    def counted_dispatch(action,data):
        if action in reads:reads[action]+=1
        return dispatch(action,data)
    def counted_request(method,path):
        if path=='/version':reads['version']+=1
        return request(method,path)
    monkeypatch.setattr(core,'dispatch',counted_dispatch)
    monkeypatch.setattr(core,'request',counted_request)
    return reads


def profiles(manager,count=10,**extra):
    first=profile(manager,**extra)
    for index in range(1,count):
        manager.store.save_entity('calibrations',{**first,'id':f'measured-{index}'})
    return [p['id'] for p in manager.store.entities('calibrations')]


@pytest.mark.parametrize('count',[1,10])
@pytest.mark.parametrize('stale',[False,True])
def test_performance_snapshot_reads_exact_model_once_even_for_rejected_profiles(manager,monkeypatch,count,stale):
    profiles(manager,count);manager.snapshot()
    if stale:manager.service.providers.core.version='fixture-engine-2'
    reads=metadata_reads(manager,monkeypatch)
    for request in range(2):
        result=manager.snapshot()
        assert reads=={key:request+1 for key in reads}
        assert len(result['calibrations'])==count
        assert all(p['calibrated'] is (not stale) for p in result['calibrations'])
        assert result['tuning']['eligible'] is True
        if stale:assert all(p['eligibility_reason']=='binding_changed' for p in result['calibrations'])


def test_failed_performance_metadata_is_read_once_and_retried_next_request(manager,monkeypatch):
    profiles(manager);manager.snapshot()
    core=manager.service.providers.core;original=core.dispatch
    fail=True
    def unavailable(action,data):
        if fail and action=='models':raise ValueError('private-fixture-metadata-marker')
        return original(action,data)
    monkeypatch.setattr(core,'dispatch',unavailable)
    reads=metadata_reads(manager,monkeypatch)
    result=manager.snapshot()
    assert reads=={'models':1,'show':0,'version':0}
    assert all(not p['calibrated'] and p['eligibility_reason']=='identity_unavailable' for p in result['calibrations'])
    assert result['tuning']['eligible'] is False
    assert 'private-fixture-metadata-marker' not in str(result)
    fail=False
    result=manager.snapshot()
    assert reads=={'models':2,'show':1,'version':1}
    assert all(p['calibrated'] for p in result['calibrations']) and result['tuning']['eligible']


def test_ineligible_profile_display_does_not_request_model_facts(manager,monkeypatch):
    profiles(manager,validated=False)
    reads=metadata_reads(manager,monkeypatch)
    result=manager.dispatch('performance_calibrations',{})
    assert reads=={'models':0,'show':0,'version':0}
    assert all(p['eligibility_reason']=='recalibration_required' for p in result['calibrations'])


@pytest.mark.parametrize('stale',[False,True])
def test_adaptive_admission_shares_facts_but_each_request_revalidates(manager,monkeypatch,stale):
    ids=profiles(manager)
    current={**manager.store.get_settings(),'adaptive_enabled':True,'adaptive_profile_ids':ids}
    if stale:manager.service.providers.core.version='fixture-engine-2'
    reads=metadata_reads(manager,monkeypatch)
    first=performance.adaptive_settings(manager.store,{},current,manager.service.providers)
    assert reads=={'models':1,'show':1,'version':1}
    assert first['num_thread']==(4 if stale else 8)
    assert all(first[k]==current[k] for k in ('provider_id','model','context','thinking','tokens','temperature'))
    manager.service.providers.core.version='fixture-engine-3'
    second=performance.adaptive_settings(manager.store,{},current,manager.service.providers)
    assert reads=={'models':2,'show':2,'version':2}
    assert second['num_thread']==4 and 'adaptive_applied_profile' not in second


@pytest.mark.parametrize('operation',['apply','approve','adaptive'])
def test_display_facts_never_authorize_a_later_profile_change(manager,monkeypatch,operation):
    ids=profiles(manager);manager.snapshot()
    manager.service.providers.core.catalog[0]['digest']='b'*64
    before=manager.store.get_settings();reads=metadata_reads(manager,monkeypatch)
    if operation=='apply':
        with pytest.raises(ValueError,match='recalibration'):manager.apply(ids[0])
    elif operation=='approve':
        with pytest.raises(ValueError,match='recalibration'):manager.dispatch('performance_profile_accept',{'id':ids[0]})
    else:
        current={**before,'adaptive_enabled':True,'adaptive_profile_ids':ids}
        assert performance.adaptive_settings(manager.store,{},current,manager.service.providers)['num_thread']==4
    assert reads=={'models':1,'show':1,'version':1} and manager.store.get_settings()==before


def test_provider_change_during_profile_review_cannot_apply_prior_candidate(manager,monkeypatch):
    ids=profiles(manager)
    current={**manager.store.get_settings(),'adaptive_enabled':True,'adaptive_profile_ids':ids}
    original=performance.fresh_profile;calls=0
    def changed(*args,**kwargs):
        nonlocal calls
        result=original(*args,**kwargs);calls+=1
        if calls==1:manager.service.providers.core.base='http://127.0.0.1:11435/api'
        return result
    monkeypatch.setattr(performance,'fresh_profile',changed)
    reads=metadata_reads(manager,monkeypatch)
    result=performance.adaptive_settings(manager.store,{},current,manager.service.providers)
    assert reads=={'models':1,'show':1,'version':1}
    assert result['num_thread']==4 and 'adaptive_applied_profile' not in result


@pytest.mark.parametrize('median',[None,False,True,'0.1',0,-1,'missing'],
                         ids=['null','false','true','string','zero','negative','missing'])
def test_bad_stored_probe_timing_cannot_be_approved_applied_or_break_profile_display(manager,median):
    bad=profile(manager,median_total_seconds=median)
    if median=='missing':
        bad.pop('median_total_seconds');manager.store.save_entity('calibrations',bad)
    before=manager.store.get_settings()
    with pytest.raises(ValueError,match='recalibration'):manager.dispatch('performance_profile_accept',{'id':bad['id']})
    with pytest.raises(ValueError,match='recalibration'):manager.apply(bad['id'])
    view=manager.snapshot()
    assert not view['calibrations'][0]['calibrated']
    assert view['calibrations'][0]['eligibility_reason']=='measurement_incomplete'
    current={**before,'adaptive_enabled':True,'adaptive_profile_ids':[bad['id']]}
    result=performance.adaptive_settings(manager.store,{},current,manager.service.providers)
    assert result['num_thread']==before['num_thread'] and 'adaptive_applied_profile' not in result
    assert manager.store.get_settings()==before
    good=profile(manager,id='measured-good',median_total_seconds=2,settings={'num_thread':6})
    result=performance.adaptive_settings(manager.store,{}, {**current,'adaptive_profile_ids':[bad['id'],good['id']]},manager.service.providers)
    assert result['num_thread']==6 and result['adaptive_applied_profile']==good['id']
    assert all(result[k]==before[k] for k in ('provider_id','model','context','thinking','tokens','temperature'))


@pytest.mark.parametrize('median',[float('nan'),float('inf'),float('-inf')],ids=['nan','infinite','negative-infinite'])
def test_nonfinite_in_memory_probe_timing_is_refused_before_model_reads(manager,monkeypatch,median):
    bad=profile(manager);bad['median_total_seconds']=median
    reads=metadata_reads(manager,monkeypatch)
    with pytest.raises(ValueError,match='measurement_incomplete'):
        performance.fresh_profile(manager.store,manager.service.providers,manager.store.get_settings(),bad)
    assert reads=={'models':0,'show':0,'version':0}


@pytest.mark.parametrize('median',[.000001,1,2.5])
def test_finite_positive_legacy_probe_timing_still_approves_and_applies(manager,median):
    good=profile(manager,median_total_seconds=median)
    accepted=manager.dispatch('performance_profile_accept',{'id':good['id']})
    assert good['id'] in accepted['settings']['adaptive_profile_ids']
    assert manager.apply(good['id'])['settings']['num_thread']==8

def run_tune(manager,monkeypatch,probe=None):
    settings=manager.store.get_settings();contract,facts=manager._tuning_contract(settings)
    assert contract['eligible']
    cancel=threading.Event()
    job=manager.store.save_entity('benchmarks',{'id':'tune-fixture','operation':'tune','status':'queued','results':[]})
    manager.jobs[job['id']]={'cancel':cancel,'operation':'tune','generation':manager.selection_generation}
    if probe is None:
        probe=lambda settings,case,event:{**sample({4:10,8:11,16:12}[settings['num_thread']]),'case':case,
                                         'selected_controls':selected(settings)}
    monkeypatch.setattr(manager,'_probe',probe)
    manager._worker(job,settings,{'tuning_contract':contract,'initial_facts':facts,'generation':manager.selection_generation},cancel)
    return manager.store.entity('benchmarks',job['id'])

def test_inherited_and_explicit_thinking_are_preserved_with_fresh_approved_profile(manager):
    p=profile(manager)
    current={**manager.store.get_settings(),'adaptive_enabled':True,'adaptive_profile_ids':[p['id']]}
    result=performance.adaptive_settings(manager.store,{'thinking':True},current,manager.service.providers)
    assert result['thinking'] is True and result['num_thread']==8
    assert all(result[key]==current[key] for key in ('model','context','tokens','temperature','provider_id'))
    assert performance.adaptive_settings(manager.store,{'num_thread':6},current,manager.service.providers)['num_thread']==6

@pytest.mark.parametrize('missing',['immutable_identity','binding','model_digest','thinking','tokens','temperature'])
def test_legacy_missing_authority_never_auto_applies_or_approves(manager,missing):
    p=profile(manager);p.pop(missing);manager.store.save_entity('calibrations',p)
    current={**manager.store.get_settings(),'adaptive_enabled':True,'adaptive_profile_ids':[p['id']]}
    assert performance.adaptive_settings(manager.store,{},current,manager.service.providers)['num_thread']==4
    before=manager.store.get_settings()
    with pytest.raises(ValueError,match='recalibration'):manager.dispatch('performance_profile_accept',{'id':p['id']})
    assert manager.store.get_settings()==before

@pytest.mark.parametrize('drift',['digest','engine','hardware','thinking','context','tokens','temperature','provider'])
def test_list_and_apply_revalidate_exact_facts_and_keep_settings_on_drift(manager,monkeypatch,drift):
    p=profile(manager)
    assert any(value['id']==p['id'] and value['calibrated'] for value in manager.recommendations(hardware(),manager.store.get_settings()))
    manager.dispatch('performance_profile_accept',{'id':p['id']})
    core=manager.service.providers.core
    if drift=='digest':core.catalog[0]['digest']='b'*64
    elif drift=='engine':core.version='fixture-engine-2'
    elif drift=='hardware':
        changed=hardware();changed['cpu']['physical_cores']=6
        monkeypatch.setattr(performance,'hardware',lambda:changed)
    elif drift=='provider':core.base='http://127.0.0.1:11435/api'
    else:manager.store.update_settings({drift:{'thinking':False,'context':16384,'tokens':2048,'temperature':.2}[drift]})
    before=manager.store.get_settings()
    assert not any(value['id']==p['id'] for value in manager.recommendations(performance.hardware(),before))
    assert manager.dispatch('performance_calibrations',{})['calibrations'][0]['calibrated'] is False
    with pytest.raises(ValueError,match='recalibration'):manager.apply(p['id'])
    assert manager.store.get_settings()==before
    assert performance.adaptive_settings(manager.store,{}, {**before,'adaptive_enabled':True},manager.service.providers)['num_thread']==4

def test_probe_uses_selected_thinking_tokens_temperature_and_exact_reported_decode(manager):
    settings=manager.store.get_settings()
    manager.service.providers.capabilities('ollama','fixture',refresh=True)
    result=manager._probe(settings,'warm_request',threading.Event())
    payload=manager.service.providers.core.payloads[-1]
    assert payload['think'] is True
    assert payload['options']=={'num_predict':4096,'num_ctx':32768,'temperature':.4,'num_thread':4}
    assert result['tps']==10 and result['prefill_seconds']==.2 and result['decode_seconds']==2
    assert result['timing_provenance']=='engine_reported'

@pytest.mark.parametrize('reported',[True,-1,0,float('nan')])
def test_invalid_reported_duration_cannot_claim_decode_speed(manager,monkeypatch,reported):
    packets=[{'message':{'content':CODE}},{'done':True,'message':{},'eval_count':20,
             'prompt_eval_count':40,'eval_duration':reported,'prompt_eval_duration':200_000_000}]
    monkeypatch.setattr(manager.service.providers.core,'_stream_payload',lambda payload,cancel:iter(packets))
    result=manager._probe(manager.store.get_settings(),'warm_request',threading.Event())
    assert result['tps'] is None and result['estimated'] and result['timing_provenance']=='unavailable'

def test_thread_candidates_are_hardware_bounded_and_unknown_gpu_is_advisory():
    telemetry=hardware();telemetry['cpu'].update(physical_cores=96,logical_cores=128)
    assert all(1<=value<=64 for value in thread_candidates(telemetry,4))
    telemetry['gpus']=[]
    assert headroom_reason(telemetry,{'size':2*GIB})=='gpu_telemetry_unknown'
    telemetry=hardware();telemetry['ram']['available_bytes']=100
    assert headroom_reason(telemetry,{'size':2*GIB})=='ram_headroom_low'

@pytest.mark.parametrize('failure',['missing_metrics','too_small_gain','noisy','ttft','prefill','quality','counts','control'])
def test_gain_rejects_incomplete_noisy_incomparable_or_regressed_evidence(failure):
    baseline=[sample() for _ in range(5)];candidate=[sample(11) for _ in range(5)];controls=[(sample(),sample(11))]
    if failure=='missing_metrics':candidate[0]['timing_provenance']='estimated'
    elif failure=='too_small_gain':candidate=[sample(10.4) for _ in range(5)]
    elif failure=='noisy':candidate[0]['tps']=30
    elif failure=='ttft':candidate=[sample(11,ttft_seconds=.2) for _ in range(5)]
    elif failure=='prefill':candidate=[sample(11,prefill_seconds=.4) for _ in range(5)]
    elif failure=='quality':candidate[0]['status']='failed'
    elif failure=='counts':candidate[0]['input_tokens']=41
    else:controls[0][1]['status']='failed'
    assert compare(baseline,candidate,controls)['accepted'] is False

def test_completed_tune_interleaves_warm_five_pairs_and_only_proposes(manager,monkeypatch):
    before=manager.store.get_settings()
    job=run_tune(manager,monkeypatch)
    assert job['status']=='completed' and job['completed_requests']==job['total_requests']==36
    assert job['tuning']['outcome']=='proposal' and job['tuning']['proposed_settings']=={'num_thread':16}
    assert manager.store.get_settings()==before
    calibration=manager.store.entity('calibrations',job['calibration_id'])
    assert calibration['settings']=={'num_thread':16} and calibration['thinking'] is True
    warm=[row for row in job['results'] if row['group'] in ('baseline','candidate')]
    assert [row['group'] for row in warm[:4]]==['baseline','candidate','candidate','baseline']
    assert all(row['selected_controls'][key]==before[key] for row in job['results'] for key in ('thinking','tokens','temperature','model','context','provider_id'))
    manager.dispatch('performance_profile_accept',{'id':calibration['id']})
    assert manager.store.get_settings()['num_thread']==4
    applied=manager.apply(calibration['id'])['settings']
    assert applied['num_thread']==16 and applied['thinking'] is True

@pytest.mark.parametrize('interrupt',['cancel','selection','provider','native','foreground','generation'])
def test_cancel_or_drift_retains_completed_evidence_without_profile(manager,monkeypatch,interrupt):
    calls=[]
    def probe(settings,case,cancel):
        calls.append(settings['num_thread'])
        if len(calls)==3:
            if interrupt=='cancel':cancel.set()
            elif interrupt=='selection':manager.store.update_settings({'thinking':False})
            elif interrupt=='provider':manager.service.providers.core.catalog[0]['digest']='b'*64
            elif interrupt=='native':manager.service.update_manager=SimpleNamespace(applying=True)
            elif interrupt=='foreground':manager.service.jobs.jobs['fixture']={'cancel':threading.Event()}
            else:manager.changed('selection_changed')
        return {**sample(11),'case':case,'selected_controls':selected(settings)}
    job=run_tune(manager,monkeypatch,probe)
    assert job['status'] in ('failed','cancelled') and job['results']
    assert manager.store.entities('calibrations')==[] and manager.store.get_settings()['num_thread']==4

def test_remote_ollama_and_unknown_gpu_cannot_use_desktop_hardware(manager,monkeypatch):
    manager.service.providers.core.base='http://192.0.2.1:11434/api'
    contract,_=manager._tuning_contract(manager.store.get_settings())
    assert not contract['eligible'] and contract['reason']=='server_hardware_unknown'
    manager.service.providers.core.base='http://127.0.0.1:11434/api'
    unknown=hardware();unknown['gpus']=[];monkeypatch.setattr(performance,'hardware',lambda:unknown)
    contract,_=manager._tuning_contract(manager.store.get_settings())
    assert not contract['eligible'] and contract['advisory_only']

def test_performance_read_counter_does_not_self_block_and_native_work_does(manager):
    manager.service._native_io_pending=manager.service._performance_io_pending=1
    assert manager._tuning_contract(manager.store.get_settings())[0]['eligible']
    manager.service._native_io_pending=2
    assert manager._tuning_contract(manager.store.get_settings())[0]['reason']=='foreground_work'

def test_slow_status_io_does_not_hold_manager_lock_or_publish_after_invalidation(manager):
    entered,release=threading.Event(),threading.Event()
    def loader():entered.set();assert release.wait(2);return {'stale':True}
    worker=threading.Thread(target=lambda:manager._cached('delayed',10,loader))
    worker.start();assert entered.wait(2)
    manager.invalidate();assert manager.cancel('unknown')=={'ok':True}
    release.set();worker.join(2)
    assert not worker.is_alive() and 'delayed' not in manager.status_cache

def test_selected_model_beyond_first_24_remains_visible(manager):
    manager.service.providers.core.catalog=[{'name':'unused-'+str(i),'digest':'c'*64,'size':GIB} for i in range(30)]+manager.service.providers.core.catalog
    assert any(model['name']=='fixture' for model in manager._models('ollama','fixture',refresh=True))

def test_budget_cancellation_is_bounded_and_preserves_completed_evidence(manager,monkeypatch):
    callback=[]
    class Timer:
        daemon=False
        def __init__(self,seconds,stop):assert seconds==300;callback.append(stop)
        def start(self):pass
        def cancel(self):pass
    monkeypatch.setattr(performance.threading,'Timer',Timer)
    calls=[]
    def probe(settings,case,event):
        calls.append(case)
        if len(calls)==3:callback[0]()
        return {**sample(11),'case':case}
    job=run_tune(manager,monkeypatch,probe)
    assert job['status']=='cancelled' and job['stop_reason']=='budget_exhausted'
    assert job['completed_requests']==3 and not manager.store.entities('calibrations')

def test_incomplete_tune_evidence_cannot_be_approved_or_auto_applied(manager):
    manager.store.save_entity('benchmarks',{'id':'unfinished','status':'running','completed_requests':35,'total_requests':36})
    p=profile(manager,selected_model_tuning=True,benchmark_id='unfinished',measurement={'accepted':True})
    before=manager.store.get_settings()
    with pytest.raises(ValueError,match='recalibration'):manager.dispatch('performance_profile_accept',{'id':p['id']})
    assert manager.store.get_settings()==before

def test_stale_applied_label_and_explicit_false_identity_have_no_authority(manager):
    p=profile(manager,immutable_identity=False)
    current={**manager.store.get_settings(),'adaptive_enabled':True,'adaptive_profile_ids':[p['id']],
             'adaptive_applied_profile':p['id']}
    result=performance.adaptive_settings(manager.store,{},current,manager.service.providers)
    assert 'adaptive_applied_profile' not in result and result['num_thread']==4

def test_quality_failed_warmup_keeps_settings_without_proposal(manager,monkeypatch):
    job=run_tune(manager,monkeypatch,lambda settings,case,cancel:{**sample(11,status='failed'),'case':case})
    assert job['status']=='failed' and job['stop_reason']=='quality_regression'
    assert not manager.store.entities('calibrations') and manager.store.get_settings()['num_thread']==4

def test_no_repeatable_gain_keeps_complete_evidence_without_a_proposal(manager,monkeypatch):
    job=run_tune(manager,monkeypatch,lambda settings,case,cancel:{**sample(10),'case':case})
    assert job['status']=='completed' and job['completed_requests']==job['total_requests']==36
    assert job['tuning']['outcome']=='no_improvement' and manager.store.entities('calibrations')==[]

@pytest.mark.parametrize('operation',['tune','apply','approve'])
def test_change_then_revert_during_preflight_revokes_original_intent(manager,monkeypatch,operation):
    entered,release=threading.Event(),threading.Event();errors=[];launched=[]
    p=profile(manager)
    if operation=='tune':
        original=manager._tuning_contract
        def delayed(*args,**kwargs):entered.set();assert release.wait(3);return original(*args,**kwargs)
        monkeypatch.setattr(manager,'_tuning_contract',delayed)
        monkeypatch.setattr(manager,'_worker',lambda *args:launched.append(True))
        action=lambda:manager.start({},'tune')
    else:
        original=performance.fresh_profile
        def delayed(*args,**kwargs):entered.set();assert release.wait(3);return original(*args,**kwargs)
        monkeypatch.setattr(performance,'fresh_profile',delayed)
        action=(lambda:manager.apply(p['id'])) if operation=='apply' else lambda:manager.dispatch('performance_profile_accept',{'id':p['id']})
    def request():
        try:action()
        except ValueError as error:errors.append(str(error))
    worker=threading.Thread(target=request);worker.start();assert entered.wait(2)
    with manager.service.admission_lock:
        manager.changed('selection_changed');manager.store.update_settings({'thinking':False})
        manager.changed('selection_changed');manager.store.update_settings({'thinking':True})
    release.set();worker.join(3)
    assert not worker.is_alive() and len(errors)==1 and 'changed during' in errors[0]
    assert not launched and manager.store.entities('benchmarks')==[]
    assert manager.store.get_settings()['num_thread']==4 and manager.store.get_settings()['adaptive_profile_ids']==[]

@pytest.mark.parametrize('operation',['tune','benchmark','calibrate'])
def test_provider_preflight_failure_never_exposes_raw_markers(manager,monkeypatch,operation):
    marker='private-fixture-home-and-key-marker'
    def fail(model):raise ValueError(marker)
    monkeypatch.setattr(manager.service.providers.ollama,'capabilities',fail)
    with pytest.raises(performance.PerformanceUnavailable) as caught:manager.start({},operation)
    assert marker not in str(caught.value) and caught.value.reason=='metadata_unavailable'
    assert not manager.jobs and manager.store.entities('benchmarks')==[]

@pytest.mark.parametrize('operation',['benchmark','calibrate','warm','release'])
def test_worker_failure_never_exposes_raw_markers_in_public_job_history(manager,monkeypatch,operation):
    marker='private-fixture-home-and-key-marker'
    def fail(*args,**kwargs):raise ValueError(marker)
    monkeypatch.setattr(manager,'_probe',fail);monkeypatch.setattr(manager,'_resident_request',fail)
    cancel=threading.Event();job=manager.store.save_entity('benchmarks',{'id':'safe-error','operation':operation,'status':'queued','results':[]})
    manager.jobs[job['id']]={'cancel':cancel,'operation':operation}
    manager._worker(job,manager.store.get_settings(),{},cancel)
    history=manager.dispatch('performance_benchmarks',{})
    assert marker not in str(history)
    assert history['benchmarks'][0]['status']=='failed' and history['benchmarks'][0]['stop_category']=='operation_failed'
