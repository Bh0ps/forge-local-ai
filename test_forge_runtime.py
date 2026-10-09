"""Managed engines are tested with fake processes, never external services."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from forge_runtime import RuntimeManager
from forge_inference import ENGINE_PROFILES


def register(manager, tmp_path, monkeypatch):
    executable = tmp_path / 'fake-ollama.exe'
    executable.write_bytes(b'Disposable fake runtime')
    version = next(p['version'] for p in ENGINE_PROFILES if p['engine'] == 'ollama')
    monkeypatch.setattr(manager, '_version', lambda path: 'ollama version is ' + version)
    return manager.install({'engine': 'ollama', 'executable': str(executable), 'id': 'test'})


def test_explicit_registration_preserves_external_settings_and_pins_executable(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    config = register(manager, tmp_path, monkeypatch)
    assert manager.config_path.exists()
    assert config['executable_sha256'] == hashlib.sha256(b'Disposable fake runtime').hexdigest()
    assert config['validation'] is None
    with pytest.raises(ValueError, match='validation'):
        manager.start({'id': config['id'], 'profile': 'q8_0', 'model': 'test'})
    Path(config['executable']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        manager.start({'id': config['id']})


def test_runtime_install_rejects_version_mismatch(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    executable = tmp_path / 'fake.exe'
    executable.write_bytes(b'fake')
    monkeypatch.setattr(manager, '_version', lambda path: 'ollama 0.0.0')
    with pytest.raises(ValueError, match='does not match'):
        manager.install({'engine': 'ollama', 'executable': str(executable)})
    with pytest.raises(ValueError, match='official'):
        manager.install({'engine': 'ollama', 'url': 'https://untrusted.invalid/runtime.zip', 'sha256': 'a'*64})


def test_verified_archive_still_rejects_traversal(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as package:
        package.writestr('../outside.exe', b'unsafe')
    payload = buffer.getvalue()
    version = next(p['version'] for p in ENGINE_PROFILES if p['engine'] == 'ollama')
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload)), **kwargs))
    with pytest.raises(ValueError, match='unsafe paths'):
        manager.install({'id': 'unsafe', 'engine': 'ollama',
                         'url': 'https://github.com/ollama/ollama/releases/download/v' + version + '/runtime.zip',
                         'sha256': hashlib.sha256(payload).hexdigest()})
    assert not list(tmp_path.rglob('outside.exe'))
    assert 'unsafe' not in manager.configs


def test_owned_engine_launcher_only_updates_child_environment(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    config = register(manager, tmp_path, monkeypatch)
    captured = {}
    class Process:
        pid = 42
        stdout = io.StringIO('Started\n')
        def poll(self): return None
        def terminate(self): captured['terminated'] = True
        def wait(self, timeout): return 0
    def popen(arguments, **kwargs):
        captured.update(arguments=arguments, **kwargs)
        return Process()
    monkeypatch.setattr('forge_runtime.subprocess.Popen', popen)
    monkeypatch.setattr(manager, '_available_port', lambda value: value)
    launched = manager.start({'id': config['id'], 'port': 11435})
    assert captured['arguments'] == [config['executable'], 'serve']
    assert captured['env']['OLLAMA_HOST'] == '127.0.0.1:11435'
    assert captured['env']['OLLAMA_KV_CACHE_TYPE'] == 'f16'
    assert launched['profile'] == 'f16'
    manager.stop(config['id'])
    assert captured['terminated']


def test_promotion_requires_exact_context_projector_and_model(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    config = register(manager, tmp_path, monkeypatch)
    config['validation'] = {'promoted': True, 'model': 'tested-model', 'context': 32768,
                            'projector': None, 'executable_sha256': config['executable_sha256']}
    for values in ({'model': 'other'}, {'model': 'tested-model', 'context': 65536},
                   {'model': 'tested-model', 'mmproj_path': 'other.gguf'}):
        with pytest.raises(ValueError, match='exact'):
            manager.start({'id': config['id'], 'profile': 'q8_0', **values})


def test_coding_probe_validates_results_without_arbitrary_execution():
    assert RuntimeManager._coding_valid('def sum_even(values):\n    return sum(x for x in values if x % 2 == 0)')
    assert not RuntimeManager._coding_valid('def sum_even(values):\n    return sum(x for x in values if x % 2 == 1)')
    assert not RuntimeManager._coding_valid('import os\nos.remove("example")')
    assert not RuntimeManager._coding_valid('def sum_even(values):\n    return sum(__import__("os").system("echo bad") for x in values)')


def test_streamed_tool_probe_checks_arguments_and_real_timings(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path / 'forge')
    packets = [{'message': {'tool_calls': [{'function': {'name': 'forge_validation_echo', 'arguments': {'marker': 'FORGE_TOOL_OK'}}}]}},
               {'done': True, 'eval_count': 10, 'eval_duration': 500000000, 'message': {}}]
    real = httpx.AsyncClient
    response = '\n'.join(json.dumps(packet) for packet in packets)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=response)), **kwargs))
    result = manager._probe('http://127.0.0.1:11435', 'ollama', 'test-model', 'tools')
    assert result['passed'] and result['tps'] == 20

@pytest.mark.parametrize('engine,ending', [('ollama','eof'),('ollama','length'),('compatible','eof'),('compatible','length')])
def test_runtime_probe_does_not_promote_truncated_tool_stream(tmp_path,monkeypatch,engine,ending):
    manager=RuntimeManager(tmp_path/'forge')
    tool={'function':{'name':'forge_validation_echo','arguments':{'marker':'FORGE_TOOL_OK'}}}
    if engine=='ollama':
        packets=[{'message':{'tool_calls':[tool]}}]
        if ending=='length':packets.append({'done':True,'done_reason':'length'})
        response='\n'.join(json.dumps(packet) for packet in packets)
    else:
        tool={'index':0,'id':'fixture','type':'function','function':{**tool['function'],'arguments':json.dumps(tool['function']['arguments'])}}
        packets=[{'choices':[{'delta':{'tool_calls':[tool]},'finish_reason':None}]}]
        if ending=='length':packets.append({'choices':[{'delta':{},'finish_reason':'length'}]})
        response=''.join('data: '+json.dumps(packet)+'\n\n' for packet in packets)
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=response)),**kwargs))
    assert manager._probe('http://127.0.0.1:8082',engine,'fixture','tools')['passed'] is False

def test_managed_model_manifest_change_invalidates_binding(tmp_path,monkeypatch):
    manager=RuntimeManager(tmp_path/'forge');config=register(manager,tmp_path,monkeypatch)
    manifest=Path(config['model_directory'])/'manifests'/'registry.ollama.ai'/'library'/'fixture'/'latest'
    manifest.parent.mkdir(parents=True);manifest.write_text('{"model":"first"}')
    data={'id':config['id'],'model':'fixture','context':32768}
    first=manager._model_binding(data)
    assert first['manifest_sha256'] and first['advanced']=={}
    manifest.write_text('{"model":"replacement"}')
    assert manager._model_binding(data)!=first
