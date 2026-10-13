"""Exact compact UTF-8 budgeting uses linear, uncached wire serialization."""
from copy import deepcopy
import json
import random

import pytest

import tool_routing
from tool_routing import select_schemas,wire_schemas


def schema(name,description='Synthetic tool.',**metadata):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{
        'type':'object','properties':{'path':{'type':'string','description':'Unicode path 日本語 🌍'}}}},**metadata}


def size(values):return len(json.dumps(wire_schemas(values),ensure_ascii=False,separators=(',',':')).encode('utf-8'))


def prefix_reference(ranked,budget,limit=64):
    selected=[]
    for candidate in ranked:
        if len(selected)>=limit:break
        if size(selected+[candidate])<=budget:selected.append(candidate)
    return selected


def test_exact_unicode_brackets_commas_and_projection_at_budget_boundaries():
    values=[schema('tools_search','Find synthetic tools. 雪 ❄️'),schema('read_file','Read a synthetic path. 漢字'),
        schema('custom','A smaller fallback.')]
    values[0]['verification_contract']={'private':'never transmitted'*300}
    values[0]['capability']='coordinator-only';values[0]['private_epoch']=object()
    ranked=select_schemas(values,'read',10**9)
    boundaries={0,1,2,*[size(ranked[:index])+delta for index in range(1,len(ranked)+1) for delta in (-1,0,1)]}
    for budget in boundaries:
        actual=select_schemas(values,'read',budget)
        assert actual==prefix_reference(ranked,budget)
        assert all(any(item is original for original in values) for item in actual)
        assert all(set(item)=={'type','function'} for item in wire_schemas(actual))
        assert 'never transmitted' not in json.dumps(wire_schemas(actual))
    first=size([ranked[0]])
    assert select_schemas(values,'read',first)==[ranked[0]]
    assert select_schemas(values,'read',first-1)!=[ranked[0]]


def test_unfitting_first_candidate_does_not_charge_a_comma_or_count_as_selected():
    big=schema('tools_search','Synthetic long text. '*100)
    small=schema('read_file','Small.');budget=size([small])
    assert select_schemas([small,big],'read',budget)==[small]
    assert size([small])==budget


@pytest.mark.parametrize('phase',[None,'inspect','implement','repair','verify'])
def test_loaded_coordination_and_phase_order_are_preserved(phase):
    names=['quality_check','preview_status','edit_file','read_files','custom_loaded','read_file',
        'request_user_input','tools_load','goal_read','tools_search','goal_update','artifact_read']
    values=[schema(name,verification_contract={'id':'fixture'} if name=='quality_check' else None) for name in names]
    selected=select_schemas(values,'inspect project',10**9,['custom_loaded'],phase=phase)
    assert [item['function']['name'] for item in selected[:8]]==[
        'tools_search','tools_load','request_user_input','goal_read','goal_update','artifact_read','custom_loaded','read_file']
    rest=[item['function']['name'] for item in selected[8:]]
    if phase=='inspect':assert rest[0]=='read_files'
    if phase in ('implement','repair'):assert rest[0]=='edit_file'
    if phase=='verify':assert rest[:2]==['quality_check','preview_status']
    for limit in (0,1,7,64):
        assert select_schemas(values,'inspect project',size(selected[:7]),['custom_loaded'],limit=limit,phase=phase)==prefix_reference(selected,size(selected[:7]),limit)


def test_150_unicode_loaded_phase_budget_comparisons_and_each_call_sees_changes():
    rng=random.Random(716)
    names=['tools_search','tools_load','request_user_input','goal_read','goal_update','artifact_read','read_file',
        'edit_file','write_file','command_start','preview_status']+[f'fixture_{i}' for i in range(53)]
    original=[schema(name,'Read write verify synthetic files. 日本語 🌍 '+('x'*(i%7)*30),
        verification_contract={'id':'check'} if i%9==0 else None) for i,name in enumerate(names)]
    for index in range(150):
        values=deepcopy(original);rng.shuffle(values)
        values[0]['function']['description']+=' 雪'*(index%5)
        loaded=[item['function']['name'] for item in values[:index%4]]
        phase=[None,'inspect','implement','repair','verify'][index%5]
        query=['read files','fix code and tests','preview website'][index%3]
        budget=rng.randint(0,22000);limit=[0,1,8,17,64][index%5]
        ranked=select_schemas(values,query,10**9,loaded,limit=len(values),phase=phase)
        assert select_schemas(values,query,budget,loaded,limit=limit,phase=phase)==prefix_reference(ranked,budget,limit)
    one=schema('read_file','Original.');budget=size([one])
    assert select_schemas([one],'read',budget)==[one]
    one['function']['description']+=' Changed after selection. '*100
    assert select_schemas([one],'read',budget)==[]
    one['function']['description']='New.'
    assert select_schemas([one],'read',budget)==[one]


def test_each_projected_wire_item_is_encoded_once_with_less_serialized_byte_work(monkeypatch):
    values=[schema(f'fixture_{i}','Synthetic read schema 日本語 🌍 '+('x'*350),
        private_contract={'excluded':'secret-like synthetic marker'*100}) for i in range(64)]
    budget=8000;ranked=select_schemas(values,'read',10**9)
    expected=prefix_reference(ranked,budget)
    dumps=json.dumps;calls=[]
    def measured(value,*args,**kwargs):
        serialized=dumps(value,*args,**kwargs);calls.append(len(serialized.encode('utf-8')));return serialized
    monkeypatch.setattr(tool_routing.json,'dumps',measured)
    actual=select_schemas(values,'read',budget)
    assert actual==expected and len(calls)==len(values)
    actual_bytes=sum(calls);calls.clear()
    assert prefix_reference(ranked,budget)==expected
    legacy_bytes=sum(calls)
    assert len(calls)==len(values) and actual_bytes*5<legacy_bytes
    assert actual_bytes==sum(len(dumps(wire_schemas([item])[0],ensure_ascii=False,separators=(',',':')).encode('utf-8')) for item in ranked)


def test_invalid_unicode_keeps_encoder_failure_and_zero_limit_does_no_encoding():
    malformed=schema('fixture','\ud800')
    with pytest.raises(UnicodeEncodeError):select_schemas([malformed],'read',100000)
    assert select_schemas([malformed],'read',100000,limit=0)==[]
