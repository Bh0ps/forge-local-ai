import time

import pytest

from test_forge_engine_run_binding import fixture
from test_forge_native import gguf


def test_migrated_chat_composer_keeps_native_identity_and_original_records(fixture,tmp_path):
    svc,_=fixture
    svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    catalog=svc.providers.native().catalog
    catalog.add([gguf(tmp_path/'local.gguf')],identifier='migrated',
        origin={'kind':'ollama','manifest':'registry.ollama.ai/library/synthetic/latest'})
    svc.store.update_settings({'provider_id':'forge-native','model':'migrated'})
    chat=svc.store.create_chat(None,'Historical fixture','synthetic:latest')
    restored=svc.dispatch('get_chat',{'id':chat['id']})
    assert restored['model']=='synthetic:latest'
    assert restored['composer_model']=='migrated' and restored['composer_provider_id']=='forge-native'
    assert svc.store.get_chat(chat['id'])['model']=='synthetic:latest'
    assert catalog.composer_model('synthetic')=='migrated'
    assert catalog.composer_model('migrated')=='migrated'
    assert catalog.composer_model('unavailable')==''
    assert svc.providers.native().worker is None


def test_native_setup_and_metadata_are_local_and_durable(fixture,tmp_path):
    svc,_=fixture
    result=svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    assert result['root']==str((tmp_path/'owned').resolve())
    profile=next(p for p in svc.dispatch('providers',{})['providers'] if p['id']=='forge-native')
    assert profile['kind']=='forge-native' and profile['url']=='forge://native'
    assert not profile.get('credential_ref')
    runtime=svc.providers.native()
    runtime.catalog.add([gguf(tmp_path/'local.gguf')],identifier='synthetic')
    models=svc.dispatch('models',{'provider_id':'forge-native','refresh':True})['models']
    assert models[0]['name']=='synthetic'
    assert runtime.worker is None
    provider=svc.providers.provider('forge-native')
    assert svc.providers.run_binding_signature('forge-native',provider)
    with pytest.raises(ValueError,match='bundled Forge engine'):
        svc.dispatch('provider_save',{'id':'forge-native','kind':'openai-compatible','url':'https://example.invalid/v1'})


def test_async_import_updates_picker_without_touching_original(fixture,tmp_path):
    svc,_=fixture
    svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    source=gguf(tmp_path/'original.gguf')
    job=svc.dispatch('native_import',{'paths':[str(source)]})['job']
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        status=svc.dispatch('native_status',{})
        saved=next(j for j in status['jobs'] if j['id']==job['id'])
        if saved['state'] not in ('queued','running'):break
        time.sleep(.01)
    assert saved['state']=='completed'
    assert len(svc.dispatch('models',{'provider_id':'forge-native'})['models'])==1
    assert source.is_file() and svc.providers.native().worker is None


def test_import_errors_are_sanitized(fixture,tmp_path):
    svc,_=fixture
    svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    job=svc.dispatch('native_import',{'paths':[str(tmp_path/'synthetic-private-missing.gguf')]})['job']
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        saved=next(j for j in svc.dispatch('native_status',{})['jobs'] if j['id']==job['id'])
        if saved['state'] not in ('queued','running'):break
        time.sleep(.01)
    assert saved['state']=='interrupted'
    assert 'synthetic-private' not in saved['message']


def test_native_status_is_read_only_for_update_admission():
    from forge_service import READ_ACTIONS
    assert {'native_status','native_inventory'}<=READ_ACTIONS


def test_native_calibration_binds_weights_runtime_and_selected_context(fixture,tmp_path):
    import json
    from forge_performance import calibration_facts
    from test_forge_selected_tuning import hardware
    svc,_=fixture
    svc.dispatch('native_configure',{'root':str(tmp_path/'owned')})
    runtime=svc.providers.native()
    runtime.catalog.add([gguf(tmp_path/'local.gguf')],identifier='synthetic')
    bundle=tmp_path/'bundle';bundle.mkdir()
    manifest=bundle/'manifest.json'
    manifest.write_text(json.dumps({'schema':1,'engines':{'llama-cpp':'fixture-v1'},'files':{}}))
    runtime.bundle=bundle
    settings={**svc.store.get_settings(),'provider_id':'forge-native','model':'synthetic','thinking':False,'context':8192}
    facts=calibration_facts(svc.store,svc.providers,settings,telemetry=hardware())
    assert facts['engine_version'].startswith('llama-cpp/fixture-v1:')
    changed=calibration_facts(svc.store,svc.providers,{**settings,'context':4096},telemetry=hardware())
    assert facts['binding']!=changed['binding']
    assert runtime.worker is None
