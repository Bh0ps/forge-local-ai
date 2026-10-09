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


def test_provider_is_reloaded_after_queue_configuration_changes(tmp_path):
    svc=setup(tmp_path);calls=[];versions=[];cancel=threading.Event();errors=[]
    run=svc.store.run(svc.jobs.start({'text':'Queued fixture.'})['id'])
    def provider(identifier):
        reference=svc.store.entity('providers',identifier)['credential_ref'];versions.append(reference)
        return SimpleNamespace(remote_inference=True,generate=lambda *args:calls.append(reference) or iter([]),estimate_tokens=lambda *args:0)
    svc.providers.provider=provider
    def consume():
        try:list(svc.providers.generate({'provider_id':'openrouter','model':'openrouter/free','messages':[]},cancel,run))
        except Exception as exc:errors.append(str(exc))
    try:
        with svc.providers.remote_queue.lease(threading.Event()):
            worker=threading.Thread(target=consume);worker.start()
            deadline=time.monotonic()+2
            while not svc.providers.remote_queue.pending and time.monotonic()<deadline:time.sleep(.01)
            assert svc.providers.remote_queue.pending
            cfg=svc.store.entity('providers','openrouter')
            svc.store.save_entity('providers',{**cfg,'credential_ref':'new-synthetic-vault-reference'})
        worker.join(3)
        assert not worker.is_alive() and not errors and versions==['synthetic-vault-reference','new-synthetic-vault-reference']
        assert calls==['new-synthetic-vault-reference']
    finally:svc.shutdown()
