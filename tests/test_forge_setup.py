"""Setup tests use isolated profiles, synthetic engines and disposable local pages."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import zipfile

import httpx
import pytest

import forge_setup as setup
from forge_inference import ENGINE_PROFILES, ProviderPool
from forge_runtime import RuntimeManager
from forge_store import ForgeStore

GIB=1024**3
CODE='def sum_even(values):\n    return sum(x for x in values if x % 2 == 0)'


def hardware():
    return {'cpu':{'name':'Fixture CPU','physical_cores':8,'logical_cores':16},
            'ram':{'total_bytes':32*GIB,'available_bytes':20*GIB},
            'gpus':[{'name':'Fixture GPU','total_bytes':16*GIB,'free_bytes':GIB}]}


class Engine:
    base='http://127.0.0.1:11434/api'
    def __init__(self):self.requests=[];self.calls=[];self.limit=32768;self.capability_values=['tools','vision'];self.fail_case=None
    def dispatch(self,action,data):
        self.calls.append(action)
        if action=='models':return {'models':[{'name':'fixture','size':3*GIB},{'name':'oversized','size':18*GIB}]}
        if action=='show':return {'capabilities':self.capability_values,'model_info':{'fixture.context_length':self.limit},'digest':'fixture-digest'}
        raise AssertionError(action)
    def _stream_payload(self,payload,cancel):
        self.requests.append(deepcopy(payload))
        text=payload['messages'][-1]['content']
        if self.fail_case and self.fail_case in text:raise ValueError('Synthetic provider failure')
        if 'FORGE_CONTEXT_OK_482' in text:message={'content':'FORGE_CONTEXT_OK_482'}
        elif payload.get('tools'):message={'tool_calls':[{'function':{'name':'forge_validation_echo','arguments':{'marker':'FORGE_TOOL_OK'}}}]}
        elif any(message.get('images') for message in payload['messages']):message={'content':'FORGE VISION 482'}
        else:message={'content':CODE}
        yield {'message':message}
        yield {'done':True,'message':{},'prompt_eval_count':1200,'eval_count':20,'eval_duration':1_000_000_000}


class Speech:
    model_name='base'
    def __init__(self):self.value={'state':'idle','model':'base','model_installed':True,'setup_required':False,'device':'cpu','compute_type':'int8'};self.recordings=0
    def status(self):return dict(self.value)
    def start(self):self.recordings+=1;raise AssertionError('Ambient recording is forbidden')


@pytest.fixture
def manager(tmp_path,monkeypatch):
    store=ForgeStore(tmp_path/'forge')
    store.update_settings({'model':'fixture','context':8192,'setup_origin':'new'})
    engine=Engine()
    native=SimpleNamespace(status=lambda:{'available':True,'running':True,'url':'https://existing.example/'})
    service=SimpleNamespace(store=store,providers=ProviderPool(engine,store),runtime=None,
        host_dictation=Speech(),integrations=SimpleNamespace(browser=SimpleNamespace(native=native)),
        performance_manager=None,manager_lock=threading.RLock())
    monkeypatch.setattr(setup,'hardware',hardware)
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(query_devices=lambda:[{'max_input_channels':1}]))
    return setup.SetupManager(service)


def finished(manager,job):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        value=manager.store.entity('setup_jobs',job['id'])
        if value['status'] not in ('queued','running','cancelling'):
            return value
        time.sleep(.01)
    raise AssertionError('Setup job did not finish')


def test_construction_and_status_never_request_engine_or_change_preferences(manager):
    before=manager.store.get_settings()
    manager.status()
    assert manager.service.providers.core.calls==[]
    assert manager.service.providers.core.requests==[]
    assert manager.store.get_settings()==before
    assert manager.service.host_dictation.recordings==0


def test_first_run_origin_and_unfinished_resume_survive_restart(manager):
    assert manager.status()['first_run'] is True
    manager._save(scan={'hardware':hardware()})
    restarted=setup.SetupManager(manager.service)
    assert restarted.status()['first_run'] and restarted.status()['resume_available']
    restarted.dispatch('setup_skip',{})
    assert not restarted.status()['first_run']
    assert not restarted.status()['completed']
    manager.store.update_settings({'setup_origin':'upgraded'})
    with manager.store._connection(transaction='write') as db:
        db.execute("DELETE FROM entities WHERE kind='setup'")
    assert not manager.status()['first_run'] and manager.status()['origin']=='upgraded'


def test_scan_recommends_installed_vision_tools_as_advertising_not_measurement(manager):
    before=manager.store.get_settings()
    result=manager.scan(threading.Event())
    recommendation=result['recommendations'][0]
    assert recommendation['model']=='fixture' and recommendation['requires_acceptance']
    assert recommendation['context_qualified'] is False
    assert recommendation['memory_available_now'] is False
    assert recommendation['capability_evidence']=='engine-advertised'
    assert all(not model['measured'] for model in result['models'])
    assert result['selected']['context']==8192 and manager.store.get_settings()==before
    assert not manager.service.providers.core.requests


def test_setup_jobs_capture_request_progress_results_and_preferences(manager):
    before=manager.store.get_settings()
    job=manager.dispatch('setup_verify',{'context':4096})
    value=finished(manager,job)
    assert value['status']=='completed'
    assert value['request']=={'context':4096,'model':'fixture','provider_id':'ollama'}
    assert len(value['results'])==5 and value['phase']=='context'
    assert all(result['status']=='passed' for result in value['result']['results'])
    assert value['result']['qualification']['filled_context'] is False
    assert value['result']['qualification']['input_tokens_measured']==1200
    assert value['result']['qualification']['context']==4096
    assert manager.store.get_settings()==before
    assert manager.store.usage()['totals']['requests']==5
    assert any(any(message.get('images') for message in request['messages']) for request in manager.service.providers.core.requests)
    assert all(request['options']['num_ctx']==4096 for request in manager.service.providers.core.requests)
    assert manager.service.host_dictation.recordings==0


def test_verify_reports_missing_capabilities_and_failures_without_claiming_qualification(manager):
    manager.service.providers.core.capability_values=[]
    result=manager.verify({},threading.Event())
    assert {r['case'] for r in result['results'] if r['status']=='unsupported'}=={'tools','vision'}
    assert result['qualification']['status']=='needs-attention'
    assert result['browser']['native'] and result['browser']['verified'] is False
    assert not result['dictation']['verified']


def test_verify_keeps_failed_checks_visible_and_context_limit_is_not_silently_lowered(manager):
    before=manager.store.get_settings()
    manager.service.providers.core.limit=2048
    with pytest.raises(ValueError,match='advertises'):
        manager.verify({},threading.Event())
    assert manager.store.get_settings()==before and manager.service.providers.core.requests==[]
    manager.service.providers.core.limit=32768
    manager.service.providers.core.fail_case='sum_even'
    result=manager.verify({},threading.Event())
    assert any(check['status']=='failed' for check in result['results'])
    assert result['qualification']['status']=='needs-attention'


def test_verify_reuses_existing_performance_manager_and_refuses_live_check(manager):
    performance=manager._performance()
    assert manager._performance() is performance
    performance.jobs['active']={'cancel':threading.Event()}
    with pytest.raises(ValueError,match='performance check'):
        manager.verify({},threading.Event())


def test_cancel_is_durable_and_preserves_progress_then_explicit_retry(manager,monkeypatch):
    entered=threading.Event()
    def slow(cancel):
        entered.set()
        cancel.wait(3)
        manager._check_cancel(cancel)
    monkeypatch.setattr(manager,'scan',slow)
    job=manager.start('scan',{})
    assert entered.wait(2)
    manager._job(job['id'],phase='Scanning engine',completed_bytes=7,total_bytes=10)
    manager.dispatch('setup_cancel',{'id':job['id']})
    value=finished(manager,job)
    assert value['status']=='cancelled' and value['completed_bytes']==7
    monkeypatch.setattr(manager,'scan',lambda cancel:{'hardware':hardware()})
    retry=manager.dispatch('setup_retry',{'id':job['id']})
    assert retry['id']!=job['id'] and finished(manager,retry)['status']=='completed'


def test_restart_marks_live_jobs_interrupted_and_retry_retains_selection(manager,monkeypatch):
    old=manager.store.save_entity('setup_jobs',{'id':'stopped','operation':'verify','status':'running',
        'request':{'model':'fixture','provider_id':'ollama','context':4096},'created_at':'2020-01-01',
        'results':[{'case':'first_request','status':'passed'}]})
    restored=setup.SetupManager(manager.service)
    assert restored.store.entity('setup_jobs',old['id'])['status']=='interrupted'
    manager.store.update_settings({'context':8192})
    retry=restored.dispatch('setup_retry',{'id':old['id']})
    result=finished(restored,retry)
    assert result['result']['context']==4096


def test_only_one_setup_operation_and_no_conflicting_active_run(manager,monkeypatch):
    gate=threading.Event()
    monkeypatch.setattr(manager,'scan',lambda cancel:(gate.wait(2) or {}))
    job=manager.start('scan',{})
    with pytest.raises(ValueError,match='current setup'):
        manager.start('verify',{})
    manager.dispatch('setup_cancel',{'id':job['id']});gate.set();finished(manager,job)
    chat=manager.store.create_chat()
    manager.store.create_run({'chat_id':chat['id']})
    with pytest.raises(ValueError,match='Pause'):
        manager.start('verify',{})


def release(asset=None,**extra):
    version=next(p['version'] for p in ENGINE_PROFILES if p['engine']=='ollama')
    asset=asset or {'name':'ollama-windows-amd64.zip','digest':'sha256:'+'a'*64,
        'browser_download_url':'https://github.com/ollama/ollama/releases/download/v'+version+'/ollama-windows-amd64.zip'}
    return {'tag_name':'v'+version,'draft':False,'prerelease':False,'assets':[asset],**extra}


@pytest.mark.parametrize('bad',[
    {'draft':True},{'prerelease':True},{'tag_name':'v0.0.0'},
    {'assets':[{'name':'ollama-windows-amd64.zip','digest':'sha256:invalid'}]},
    {'assets':[{'name':'ollama-windows-amd64.zip','digest':'sha256:'+'a'*64,'browser_download_url':'https://evil.example/runtime.zip'}]},
])
def test_guided_runtime_asset_refuses_unpinned_unverified_or_unofficial_metadata(bad):
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=release(**bad)))) as client:
        with pytest.raises(ValueError):setup.SetupManager._runtime_asset(client,'ollama')


def test_guided_engine_install_hands_off_provider_without_changing_selection(manager):
    before=manager.store.get_settings()
    manager.runtime_client_factory=lambda **kwargs:httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=release())),**kwargs)
    captured={}
    class Runtime:
        def install(self,config,cancel=None):captured.update(config);return {'id':'owned-runtime'}
    manager.service.runtime=Runtime()
    def dispatch(action,data):
        assert action=='runtime_start' and data['id']=='owned-runtime'
        return {'provider_id':'runtime-owned-runtime','url':'http://127.0.0.1:11435'}
    manager.service.dispatch=dispatch
    result=manager.install_engine({},threading.Event())
    assert captured['sha256']=='a'*64 and result['requires_selection']
    assert result['provider_id']=='runtime-owned-runtime'
    assert manager.store.get_settings()==before


def test_download_model_preserves_progress_and_requires_real_success(manager):
    before=manager.store.get_settings()
    lines='\n'.join(json.dumps(packet) for packet in (
        {'status':'pulling manifest'},{'status':'pulling layer','completed':50,'total':100},{'status':'success'}))
    manager.client_factory=lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text=lines)),**kwargs)
    job=manager.dispatch('setup_download_model',{'model':'qwen3.5:0.8b'})
    result=finished(manager,job)
    assert result['status']=='completed' and result['result']['requires_selection']
    assert manager.store.get_settings()==before
    manager.client_factory=lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='{"status":"partial","completed":3,"total":100}\n')),**kwargs)
    result=finished(manager,manager.start('download_model',{'model':'qwen3.5:0.8b'}))
    assert result['status']=='failed' and result['completed_bytes']==3


def test_download_cancellation_closes_waiting_stream(manager):
    closed=threading.Event()
    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            import asyncio
            yield b'{"status":"pulling layer","completed":1,"total":100}\n'
            await asyncio.sleep(30)
        async def aclose(self):closed.set()
    manager.client_factory=lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Waiting())),**kwargs)
    job=manager.start('download_model',{'model':'qwen3.5:0.8b'})
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if manager.store.entity('setup_jobs',job['id']).get('completed_bytes')==1:break
        time.sleep(.01)
    manager.dispatch('setup_cancel',{'id':job['id']})
    assert finished(manager,job)['status']=='cancelled' and closed.wait(2)


def test_browser_verification_is_explicit_disposable_local_and_keeps_existing_page(manager):
    events=[]
    class Clone:
        def __init__(self,directory,native):self.directory=directory;self.url=None;events.append(('create',directory))
        def execute(self,name,args,context):
            if name=='browser_navigate':
                self.url=args['url']
                text=httpx.get(self.url,trust_env=False).text
                assert 'FORGE SETUP BROWSER 482' in text
                events.append(('navigate',self.url));return {'ok':True}
            return {'ok':True,'url':self.url,'text':'FORGE SETUP BROWSER 482'}
        def status(self):return {'loading':False}
        def shutdown(self):events.append(('close',self.directory))
    manager.browser_factory=Clone
    assert not events
    result=finished(manager,manager.dispatch('setup_browser_verify',{}))
    assert result['result']['verified'] and result['result']['external_network'] is False
    assert manager.service.integrations.browser.native.status()['url']=='https://existing.example/'
    assert events[1][1].startswith('http://127.0.0.1:') and events[-1][0]=='close'
    assert not Path(events[0][1]).exists()


def test_microphone_confirmation_requires_real_done_transcript_and_does_not_store_text(manager):
    with pytest.raises(ValueError,match='transcript'):
        manager.dispatch('setup_dictation_confirm',{'accepted':True,'id':'fake'})
    speech=manager.service.host_dictation
    speech.value.update(state='done',id='interactive-microphone',text='Private test phrase 482')
    with pytest.raises(ValueError,match='Confirm'):
        manager.dispatch('setup_dictation_confirm',{'accepted':False,'id':'interactive-microphone'})
    confirmed=manager.dispatch('setup_dictation_confirm',{'accepted':True,'id':'interactive-microphone'})
    assert confirmed['dictation']['verified']
    assert 'Private test phrase' not in json.dumps(manager.status())
    result=manager.verify({},threading.Event())
    assert result['dictation']['verified'] and 'Private test phrase' not in json.dumps(result)
    assert speech.recordings==0


def test_runtime_archive_cancel_and_traversal_leave_no_registered_install(manager,monkeypatch):
    runtime=RuntimeManager(manager.store.home)
    version=next(p['version'] for p in ENGINE_PROFILES if p['engine']=='ollama')
    config={'id':'isolated-runtime','engine':'ollama','version':version,
            'url':'https://github.com/ollama/ollama/releases/download/v'+version+'/ollama-windows-amd64.zip'}
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as package:package.writestr('../escaped.exe',b'unsafe')
    payload=archive.getvalue()
    real=httpx.Client
    monkeypatch.setattr('forge_runtime.httpx.Client',lambda **kwargs:real(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=payload)),**kwargs))
    with pytest.raises(ValueError,match='unsafe paths|escapes'):
        runtime.install({**config,'sha256':hashlib.sha256(payload).hexdigest()})
    assert not list(manager.store.home.rglob('escaped.exe')) and not runtime.configs
    cancel=threading.Event();cancel.set()
    with pytest.raises(ValueError,match='cancelled'):
        runtime.install({**config,'sha256':hashlib.sha256(payload).hexdigest()},cancel=cancel)
    assert not runtime.configs and not (runtime.directory/config['id']).exists()
