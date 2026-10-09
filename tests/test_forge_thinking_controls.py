"""Cached thinking controls reach the wire for aliases without fresh metadata IO."""
import copy
import threading

import pytest

from core import Core, supports_thinking
from forge_inference import OllamaProvider, ProviderPool
from forge_store import ForgeStore

ALIAS='hf.co/peculiar-ragdoll/Cyber-Tiel-Coder-35B-A3B-GGUF-MTP:UD-IQ3_XXS'
DATA={'model':ALIAS,'context':8192,'tokens':2048,'temperature':0,
      'messages':[{'role':'user','content':'Write a small function.'}],'tools':[]}
ACTUAL_METADATA={'thinking':None,'capabilities':['tools','thinking','completion','vision'],
                'details':{'family':'qwen35moe'},'model_info':{'general.architecture':'qwen35moe'}}


class CapturedCore:
    base='http://127.0.0.1:11434/api'
    def __init__(self,metadata): self.metadata=metadata; self.shows=0; self.payloads=[]
    def dispatch(self,action,data):
        assert action=='show'
        self.shows+=1
        return self.metadata
    def _stream_payload(self,payload,cancel):
        self.payloads.append(copy.deepcopy(payload))
        yield {'message':{'content':'Result.'},'done':True,'prompt_eval_count':10,'eval_count':2}


@pytest.mark.parametrize('metadata',[
    ACTUAL_METADATA,
    {'capabilities':['thinking']},
    {'model_info':{'general.architecture':'qwen3moe'}},
    {'details':{'family':'qwen3.5'}},
    {'thinking':{'values':[True,False],'default':True}}])
@pytest.mark.parametrize('requested',[False,True])
@pytest.mark.parametrize('compiler',[Core._agent_payload,Core._chat_payload])
def test_alias_controls_use_supported_cached_metadata(metadata,requested,compiler):
    data={**DATA,'thinking':requested,'model_metadata':copy.deepcopy(metadata)}
    before=copy.deepcopy(data); payload=compiler(data)
    assert payload['think'] is requested
    assert supports_thinking(ALIAS,metadata)
    assert data==before


@pytest.mark.parametrize('compiler',[Core._agent_payload,Core._chat_payload])
def test_explicit_supported_values_override_capability_and_family_fallback(compiler):
    metadata={**ACTUAL_METADATA,'thinking':{'values':[False],'default':False}}
    assert not supports_thinking(ALIAS,metadata)
    assert compiler({**DATA,'thinking':False,'model_metadata':metadata})['think'] is False
    with pytest.raises(ValueError,match='does not permit enabling'):
        compiler({**DATA,'thinking':True,'model_metadata':metadata})


@pytest.mark.parametrize('metadata',[
    {'capabilities':['tools','completion'],'details':{'family':'llama'}},
    {'capabilities':['completion'],'model_info':{'general.architecture':'qwen2'}},
    {'thinking':None,'capabilities':[]}])
def test_unsupported_false_does_not_add_a_new_think_parameter(metadata):
    for compiler in (Core._agent_payload,Core._chat_payload):
        assert 'think' not in compiler({**DATA,'thinking':False,'model_metadata':metadata})
    assert not supports_thinking(ALIAS,metadata)


def test_declared_named_levels_use_exact_default_and_reject_disallowed_false():
    metadata={'thinking':{'values':['low','medium','high'],'default':'medium'}}
    assert Core._agent_payload({**DATA,'thinking':True,'model_metadata':metadata})['think']=='medium'
    with pytest.raises(ValueError,match='does not permit disabling'):
        Core._agent_payload({**DATA,'thinking':False,'model_metadata':metadata})


def test_provider_reuses_existing_show_facts_without_generation_probes():
    core=CapturedCore(copy.deepcopy(ACTUAL_METADATA)); provider=OllamaProvider(core)
    metadata=provider.capabilities(ALIAS)
    metadata['capabilities'].clear(); metadata['model_info'].clear(); metadata['details'].clear()
    for requested in (False,True,False):
        list(provider.generate({**DATA,'thinking':requested},threading.Event()))
    assert core.shows==1
    assert [p['think'] for p in core.payloads]==[False,True,False]
    assert all('model_metadata' not in p for p in core.payloads)
    assert all(p['options']=={'temperature':0,'num_predict':2048,'num_ctx':8192} for p in core.payloads)
    assert all(p['truncate'] is False and p['shift'] is False for p in core.payloads)


def test_pool_cache_invalidation_discards_stale_provider_thinking_controls(tmp_path):
    core=CapturedCore(copy.deepcopy(ACTUAL_METADATA)); pool=ProviderPool(core,ForgeStore(tmp_path))
    assert pool.capabilities('ollama',ALIAS)['capabilities']
    list(pool.generate({**DATA,'thinking':False},threading.Event(),{}))
    assert core.payloads[-1]['think'] is False and core.shows==1
    core.metadata={'capabilities':['completion']}; pool.invalidate('ollama')
    pool.capabilities('ollama',ALIAS)
    list(pool.generate({**DATA,'thinking':False},threading.Event(),{}))
    assert 'think' not in core.payloads[-1] and core.shows==2


def test_cancellation_before_a_lease_performs_no_inference_or_new_probe(tmp_path):
    core=CapturedCore(ACTUAL_METADATA); pool=ProviderPool(core,ForgeStore(tmp_path))
    pool.capabilities('ollama',ALIAS); cancelled=threading.Event(); cancelled.set()
    with pytest.raises(ValueError,match='cancelled while queued'):
        list(pool.generate({**DATA,'thinking':False},cancelled,{}))
    assert not core.payloads and core.shows==1
    assert not pool.queue.active and not pool.queue.pending


@pytest.mark.parametrize('requested',[False,True])
def test_runtime_honors_declared_alias_controls_without_legacy_thinking_capability(tmp_path,requested):
    from forge_service import ForgeService
    from test_forge_core import finished
    core=CapturedCore({'thinking':{'values':[False,True],'default':True},
        'capabilities':['tools','completion'],'model_info':{'local.context_length':8192}})
    svc=ForgeService(core=core,data_dir=tmp_path/'forge')
    try:
        svc.store.update_settings({'model':ALIAS,'context':8192,'tokens':2048,'thinking':requested,
                                  'memory_enabled':False,'memory_suggestions':False})
        accepted=svc.jobs.start({'text':'Explain this small task.'})
        assert finished(svc,accepted)['status']=='completed'
        assert len(core.payloads)==1 and core.payloads[0]['think'] is requested
        assert core.shows==1
    finally: svc.shutdown()
