import threading
import time
from types import SimpleNamespace

import pytest

from test_forge50_collaboration import setup


@pytest.mark.parametrize('revocation', ['consent', 'permissions', 'profile'])
def test_queued_cloud_request_rechecks_before_any_transport(tmp_path, revocation):
    svc=setup(tmp_path)
    svc.store.save_entity('agents',{'id':'cloud-fixture','enabled':True})
    run=svc.store.run(svc.jobs.start({'text':'Queued fixture.','agent_id':'cloud-fixture'})['id'])
    calls=[]; errors=[]; cancel=threading.Event()
    provider=SimpleNamespace(remote_inference=True,generate=lambda *args:calls.append(args) or iter([]),estimate_tokens=lambda *args:0)
    svc.providers.provider=lambda identifier:provider
    def consume():
        try:list(svc.providers.generate({'provider_id':'openrouter','model':'openrouter/free','messages':[]},cancel,run))
        except Exception as exc:errors.append(str(exc))
    try:
        with svc.providers.remote_queue.lease(threading.Event()):
            worker=threading.Thread(target=consume);worker.start()
            deadline=time.monotonic()+2
            while not svc.providers.remote_queue.pending and time.monotonic()<deadline:time.sleep(.01)
            assert svc.providers.remote_queue.pending, 'The request never entered the real queue.'
            if revocation=='consent':
                cfg=svc.store.entity('providers','openrouter')
                svc.store.save_entity('providers',{**cfg,'remote_consent':False})
            elif revocation=='permissions':svc.store.update_settings({'permission_profile':'deny_access'})
            else:svc.store.save_entity('agents',{'id':'cloud-fixture','enabled':False})
        worker.join(3)
        assert not worker.is_alive() and not calls
        assert errors and 'No request was sent' in errors[0]
        assert not svc.providers.remote_queue.active
        with svc.store._connection() as db:
            usage=dict(db.execute('SELECT * FROM usage WHERE run_id=?',(run['id'],)).fetchone())
        assert usage['input_tokens']==usage['output_tokens']==0
    finally:svc.shutdown()


def test_queued_provider_cannot_adopt_replacement_credentials(tmp_path,monkeypatch):
    from forge_engine_auth import EngineAccessFailure
    from test_forge_openrouter import request
    from test_forge_ollama_auth import install_http
    svc=setup(tmp_path);cancel=threading.Event();errors=[]
    run=svc.store.run(svc.jobs.start({'text':'Queued fixture.'})['id'])
    requests,_=install_http(monkeypatch)
    def consume():
        try:list(svc.providers.generate(request(provider_id='openrouter'),cancel,run))
        except Exception as exc:errors.append(exc)
    try:
        with svc.providers.remote_queue.lease(threading.Event()):
            worker=threading.Thread(target=consume);worker.start()
            deadline=time.monotonic()+2
            while not svc.providers.remote_queue.pending and time.monotonic()<deadline:time.sleep(.01)
            assert svc.providers.remote_queue.pending
            cfg=svc.store.entity('providers','openrouter')
            svc.store.save_entity('providers',{**cfg,'credential_ref':'new-synthetic-vault-reference'})
        worker.join(3)
        assert not worker.is_alive() and len(errors)==1 and isinstance(errors[0],EngineAccessFailure)
        assert errors[0].category=='configuration' and errors[0].not_executed and requests==[]
    finally:svc.shutdown()
