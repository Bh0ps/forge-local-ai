"""Stream accounting keeps evidence while avoiding a second transcript buffer."""
import json
import threading
from types import SimpleNamespace

import pytest

from forge_inference import ProviderPool
from forge_speed import TokenSpeedEstimator


class UsageStore:
    def __init__(self): self.usage=[]
    def entity(self,*args): return {}
    def record_usage(self,value): self.usage.append(value)
    def event(self,*args,**kwargs): pass


def stream_fixture(packets):
    store=UsageStore()
    pool=ProviderPool(SimpleNamespace(base='http://fixture.invalid/api'),store)
    def generate(data,cancel):
        for packet in packets:
            if isinstance(packet,Exception): raise packet
            yield packet
    pool.provider=lambda identifier:SimpleNamespace(generate=generate,estimate_tokens=lambda *args:5)
    cancel=threading.Event()
    stream=pool.generate({'provider_id':'fixture','model':'fixture','messages':[]},cancel,{})
    return stream,store,cancel


def test_visible_thinking_and_full_tool_envelope_keep_exact_fallback_count():
    tools=[{'id':'call','type':'function','function':{'name':'inspect','arguments':'{"text":"字"}'}}]
    stream,store,_=stream_fixture([
        {'message':{'content':'字','thinking':'A😀'}},
        {'message':{'tool_calls':tools}}, {'done':True,'message':{}}])
    list(stream)
    expected_bytes=8+len(json.dumps(tools,ensure_ascii=False).encode('utf-8'))
    assert store.usage[0]['output_tokens']==(expected_bytes+2)//3
    assert store.usage[0]['estimated'] is True


@pytest.mark.parametrize('reported',[0,7])
def test_reported_eval_count_precedes_later_usage_and_fallback(reported):
    stream,store,_=stream_fixture([
        {'message':{'content':'字'*100}},
        {'done':True,'message':{},'eval_count':reported,'prompt_eval_count':4,
         'usage':{'completion_tokens':99,'prompt_tokens':9},'eval_duration':1_000_000_000}])
    list(stream)
    assert store.usage[0]['output_tokens']==reported
    assert store.usage[0]['input_tokens']==4
    assert store.usage[0]['estimated'] is False


def test_partial_stream_failure_records_only_emitted_visible_and_thinking_bytes():
    stream,store,_=stream_fixture([
        {'message':{'content':'😀','thinking':'字'}},ValueError('Synthetic interrupted stream')])
    assert next(stream)['message']['content']=='😀'
    with pytest.raises(ValueError,match='Synthetic interrupted'): next(stream)
    assert store.usage[0]['output_tokens']==3


def test_cancelled_generator_close_retains_partial_usage_without_a_transcript():
    stream,store,cancel=stream_fixture([{'message':{'content':'字'*65536}}, {'done':True,'message':{}}])
    next(stream)
    owned=stream.gi_frame.f_locals
    assert 'text' not in owned and owned['output_bytes']==196608
    cancel.set();stream.close()
    assert store.usage[0]['output_tokens']==65536 and store.usage[0]['cancelled'] is True


def test_tool_accounting_cap_keeps_its_existing_bound():
    tools=[{'function':{'name':'inspect','arguments':'x'*2_000_001}}]
    stream,store,_=stream_fixture([{'message':{'tool_calls':tools}}, {'done':True,'message':{}}])
    list(stream)
    assert store.usage[0]['output_tokens']==666667


def test_malformed_unicode_does_not_create_a_new_fallback_usage_receipt():
    stream,store,_=stream_fixture([{'message':{'content':'Valid'}},{'message':{'content':'\ud800'}}])
    next(stream)
    with pytest.raises(UnicodeEncodeError): next(stream)
    assert store.usage==[]


def test_byte_observation_matches_unicode_text_speed_and_rejects_invalid_counts():
    text_meter=TokenSpeedEstimator();byte_meter=TokenSpeedEstimator()
    for tick in range(61):
        text_meter.observe('字A',now=tick/20)
        byte_meter.observe_bytes(4,now=tick/20)
    assert byte_meter.total==text_meter.total
    assert byte_meter.estimate(now=3)==text_meter.estimate(now=3)
    for value in (-1,True,1.5):
        with pytest.raises(ValueError): byte_meter.observe_bytes(value,now=4)
