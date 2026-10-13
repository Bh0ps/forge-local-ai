import json
import threading

import httpx
import pytest

from core import Core, OllamaRequestFailure
from forge_inference import OllamaProvider
from test_forge_core import Engine, finished, service
from test_inference_timeouts import TimedBody, transport


MARKER='synthetic-private-path-and-credential-marker'


@pytest.mark.parametrize('status,category', [(400,'provider_error'),(401,'authentication'),(403,'access_denied'),(429,'quota'),(503,'provider_error')])
@pytest.mark.parametrize('body', ['json','text'])
def test_engine_error_body_never_enters_public_exception(status,category,body):
    response=httpx.Response(status,json={'error':MARKER}) if body=='json' else httpx.Response(status,text=MARKER)
    with pytest.raises(OllamaRequestFailure) as failure: Core._response_json(response)
    assert MARKER not in str(failure.value)
    assert failure.value.diagnostics()=={'provider_id':'ollama','category':category,'http_status':status}


def test_success_status_error_keeps_fixed_failure_without_fabricated_http_error():
    with pytest.raises(OllamaRequestFailure) as failure: Core._response_json(httpx.Response(200,json={'error':MARKER}))
    assert MARKER not in str(failure.value)
    assert failure.value.diagnostics()=={'provider_id':'ollama','category':'provider_error'}


def test_authentication_status_survives_an_unexpected_json_shape():
    with pytest.raises(OllamaRequestFailure) as failure: Core._response_json(httpx.Response(401,json=[MARKER]))
    assert failure.value.category=='authentication' and failure.value.http_status==401
    assert MARKER not in str(failure.value)


def test_streamed_engine_failure_preserves_partial_output_and_sanitizes_error(monkeypatch):
    lines=[json.dumps({'message':{'content':'Synthetic partial output'}})+'\n',json.dumps({'error':{'message':MARKER,'code':429}})+'\n']
    transport(monkeypatch,lambda request:httpx.Response(200,stream=TimedBody([(0,line.encode()) for line in lines])))
    stream=Core(base='http://fixture.invalid/api')._stream_payload({'model':'fixture','messages':[],'stream':True},threading.Event())
    assert next(stream)['message']['content']=='Synthetic partial output'
    with pytest.raises(OllamaRequestFailure) as failure: next(stream)
    assert MARKER not in str(failure.value)
    assert failure.value.category=='quota'


def test_saved_run_failure_identifies_the_configured_engine_profile(tmp_path):
    class RejectedEngine(Engine):
        def stream_agent(self,data,cancel):
            yield {'message':{'content':'Synthetic partial output'}}
            raise OllamaRequestFailure(429)
    engine=RejectedEngine(); svc=service(tmp_path,engine)
    try:
        config=svc.store.save_entity('providers',{'id':'fixture-engine','kind':'ollama',
            'name':'Fixture engine','url':'http://fixture.invalid/api','enabled':True})
        svc.providers.instances[config['id']]={'signature':json.dumps(config,sort_keys=True),
                                              'provider':OllamaProvider(engine)}
        run=svc.jobs.start({'text':'Synthetic request','provider_id':config['id']})
        assert finished(svc,run)['status']=='paused'
        failure=svc.store.run(run['id'])['failure']
        assert failure=={'provider_id':'fixture-engine','category':'quota','http_status':429}
        diagnostics=svc.dispatch('run_diagnostics_export',{'run_id':run['id']})
        assert diagnostics['provider_id']=='fixture-engine' and diagnostics['stop_category']=='quota'
        assert svc.store.get_chat(run['chat_id'])['messages'][-1]['content']=='Synthetic partial output'
    finally: svc.shutdown()
