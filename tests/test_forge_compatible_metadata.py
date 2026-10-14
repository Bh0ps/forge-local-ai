from copy import deepcopy

import pytest

from forge_inference import CompatibleProvider


def metadata(instances, options=('off', 'on')):
    return {'state':'available', 'source':'advertised', 'models':[{
        'model':'exact/model', 'max_context_length':131072,
        'loaded_instances':instances, 'loaded_context_lengths':[item['context_length'] for item in instances],
        'vision':True, 'trained_for_tool_use':True, 'reasoning_options':list(options)}]}


@pytest.mark.parametrize('model,instances,expected,source', [
    ('exact/model',[{'model':'exact/model','context_length':4096}],4096,'loaded_instance'),
    ('instance-two',[{'model':'instance-one','context_length':4096},{'model':'instance-two','context_length':8192}],8192,'loaded_instance'),
    ('exact/model',[{'model':'instance-one','context_length':4096},{'model':'instance-two','context_length':8192}],32768,'configured'),
    ('exact/model',[{'model':'different-instance','context_length':4096}],32768,'configured'),
    ('exact/model',[],32768,'configured'),
    ('exact/model',[{'model':'exact/model','context_length':None}],32768,'configured'),
    ('exact/model',[{'model':'exact/model','context_length':131072}],32768,'loaded_instance'),
])
def test_context_is_exact_and_capabilities_are_never_automatically_granted(monkeypatch, model, instances, expected, source):
    facts=metadata(instances)
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata',lambda config,**kwargs: deepcopy(facts))
    provider=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','preset_id':'lm-studio',
        'context_limit':32768,'capabilities':[]})
    result=provider.capabilities(model)
    assert result['model_info']['local.context_length']==expected
    assert result['context_limit_source']==source
    assert result['advertised_metadata']['max_context_length']==131072
    assert result['capabilities']==[]
    assert result['reasoning']['control_supported'] is False and 'thinking' not in result


def test_reasoning_is_mandatory_only_when_advertised_and_wire_is_unsupported(monkeypatch):
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata',lambda *args,**kwargs:metadata([],('low','medium','high')))
    result=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','preset_id':'lm-studio'}).capabilities('exact/model')
    assert result['reasoning']=={'control_supported':False,'advertised_options':['low','medium','high'],'mandatory':True}
    assert 'thinking' not in result


def test_unknown_metadata_does_not_invent_model_facts(monkeypatch):
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata',lambda *args,**kwargs:{'state':'unknown','source':'unknown','models':[]})
    result=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','preset_id':'lm-studio'}).capabilities('exact/model')
    assert result['metadata_state']=='unknown'
    assert 'advertised_metadata' not in result and 'reasoning' not in result


def test_generic_provider_never_probes_an_unadvertised_native_endpoint(monkeypatch):
    def forbidden(*args,**kwargs): raise AssertionError('Generic compatibility must not guess native endpoints')
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata',forbidden)
    result=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','capabilities':['tools']}).capabilities('exact/model')
    assert result['capabilities']==['tools'] and result['context_limit_source']=='unknown'
    assert result['model_info']=={}


def test_loaded_instance_can_supply_exact_capacity_without_a_configured_limit(monkeypatch):
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata',lambda *args,**kwargs:
        metadata([{'model':'exact/model','context_length':4096}]))
    result=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','preset_id':'lm-studio'}).capabilities('exact/model')
    assert result['context_limit_source']=='loaded_instance' and result['model_info']['local.context_length']==4096


def test_positive_explicit_generic_limit_remains_configured():
    result=CompatibleProvider({'kind':'openai','url':'http://fixture.invalid/v1','context_limit':16384}).capabilities('exact/model')
    assert result['context_limit_source']=='configured' and result['model_info']['local.context_length']==16384


def test_docker_package_metadata_does_not_supply_loaded_context_or_reasoning_options(monkeypatch):
    from forge_provider_metadata import docker_model_runner_facts
    facts = docker_model_runner_facts([{'id':'ai/exact:tag'}])
    monkeypatch.setattr('forge_provider_metadata.fetch_provider_metadata', lambda *args, **kwargs:deepcopy(facts))
    provider = CompatibleProvider({'kind':'openai-compatible', 'url':'http://127.0.0.1:12434/engines/llama.cpp/v1',
        'preset_id':'docker-model-runner','context_limit':32768,'capabilities':[]})
    result = provider.capabilities('ai/exact:tag')
    assert result['model_info']=={'local.context_length':32768} and result['context_limit_source']=='configured'
    assert result['advertised_metadata']==facts['models'][0] and result['capabilities']==[]
    assert result['reasoning']=={'control_supported':False} and 'thinking' not in result
