"""Owned runtime and migration boundaries, with disposable GGUFs and no network."""
import json
import os
from pathlib import Path
import queue
import struct
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from gguf import GGUFWriter

from forge_native import NativeRuntime, NativeFailure, NativeProvider
from forge_native_models import NativeCatalog, file_hash


def gguf(path):
    writer = GGUFWriter(path, 'llama')
    writer.add_name('Synthetic local model')
    writer.add_context_length(8192)
    writer.add_chat_template('{% for message in messages %}{{ message.content }}{% endfor %}{% if tools %}tools{% endif %}')
    writer.add_tensor('token_embd.weight', np.ones((2, 2), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    return path


def test_owned_import_hash_and_restart(tmp_path):
    original = gguf(tmp_path / 'source.gguf')
    catalog = NativeCatalog(tmp_path / 'home')
    owned = tmp_path / 'owned'
    catalog.configure(str(owned))
    model = catalog.add([original], identifier='test')
    assert original.is_file()
    target = owned / model['files'][0]['path']
    assert target.is_file() and file_hash(target) == file_hash(original)
    assert NativeCatalog(tmp_path / 'home').model('test')['context'] == 8192
    target.touch()
    with pytest.raises(ValueError, match='changed after verification'):
        catalog.model('test')


def test_hash_mismatch_never_registers(tmp_path):
    catalog = NativeCatalog(tmp_path / 'home')
    with pytest.raises(ValueError, match='pinned download'):
        catalog.add([gguf(tmp_path / 'model.gguf')], expected=[{'sha256': '0'*64, 'size': 1}])
    assert catalog.public()['models'] == []


def test_missing_shards_never_register(tmp_path):
    catalog = NativeCatalog(tmp_path / 'home')
    source = gguf(tmp_path / 'model.gguf')
    # The actual split.count path is exercised independently of filename guesses.
    original = catalog.inspect
    catalog.inspect = lambda path: {**original(path), 'split_count': 2}
    with pytest.raises(ValueError, match='every matching'):
        catalog.add([source])
    assert catalog.public()['models'] == []


def test_ollama_inventory_ignores_large_non_json_and_links(tmp_path):
    catalog = NativeCatalog(tmp_path / 'home')
    manifests = tmp_path / 'ollama' / 'manifests'
    manifests.mkdir(parents=True)
    with (manifests / 'misplaced.gguf').open('wb') as stream:
        stream.truncate(3 * 1024**2)
    (manifests / 'broken').write_text('{')
    assert catalog.ollama_inventory(manifests.parent) == []


def test_ollama_import_retains_defaults_and_originals(tmp_path):
    catalog = NativeCatalog(tmp_path / 'home')
    root = tmp_path / 'ollama'
    blobs = root / 'blobs'
    manifests = root / 'manifests' / 'registry.ollama.ai' / 'library' / 'synthetic'
    blobs.mkdir(parents=True)
    manifests.mkdir(parents=True)
    source = gguf(tmp_path / 'original.gguf')
    digest = file_hash(source)
    source.rename(blobs / ('sha256-' + digest))
    (manifests / 'latest').write_text(json.dumps({'layers': [{'mediaType': 'application/vnd.ollama.image.model',
        'digest': 'sha256:' + digest, 'size': (blobs / ('sha256-' + digest)).stat().st_size}]}))
    items = catalog.ollama_inventory(root)
    model = catalog.import_ollama(root, items[0]['id'])
    assert model['origin']['kind'] == 'ollama'
    assert (manifests / 'latest').exists()
    assert (blobs / ('sha256-' + digest)).exists()
    assert catalog.model(model['id'])['validation'] == 'not_run'


def test_runtime_integrity_fails_before_process(tmp_path):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'manifest.json').write_text(json.dumps({'files': {'llama-cpp/llama-server.exe': {'size':1,'sha256':'0'*64}}}))
    runtime = NativeRuntime(tmp_path / 'home', bundle=bundle)
    with pytest.raises(NativeFailure, match='missing'):
        runtime._executable('llama-cpp')
    assert runtime.worker is None


def test_worker_refuses_cross_directory_binary(tmp_path):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    outside = tmp_path / 'outside.exe'
    outside.write_bytes(b'x')
    (bundle / 'manifest.json').write_text(json.dumps({'files': {'llama-cpp/../../outside.exe': {'size':1,'sha256':file_hash(outside)}}}))
    with pytest.raises(NativeFailure, match='outside'):
        NativeRuntime(tmp_path / 'home', bundle=bundle)._executable('llama-cpp')


def test_external_work_blocks_native_loading(tmp_path):
    runtime = NativeRuntime(tmp_path / 'home', busy=lambda: True)
    with pytest.raises(NativeFailure) as exc:
        runtime._start({'id':'test','engine':'llama-cpp'},8192,threading.Event())
    assert exc.value.category == 'resource_busy' and runtime.worker is None


def test_cancelled_pipe_wait_is_prompt(tmp_path):
    runtime = NativeRuntime(tmp_path / 'home')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(NativeFailure) as exc:
        runtime._line({'lines':queue.Queue()},cancel,10**20)
    assert exc.value.category == 'cancelled'


def test_metadata_never_loads_worker(tmp_path):
    runtime = NativeRuntime(tmp_path / 'home')
    runtime.catalog.add([gguf(tmp_path / 'model.gguf')],identifier='test')
    provider = NativeProvider(runtime)
    assert provider.models()[0]['name'] == 'test'
    assert provider.capabilities('test')['model_info']['local.context_length'] == 8192
    assert runtime.worker is None


def test_bounded_gguf_rejects_malicious_counts(tmp_path):
    from forge_vendor.strata.gguf_reader import GGUFFile
    path = tmp_path / 'oversized.gguf'
    path.write_bytes(struct.pack('<IIQQ',0x46554747,3,100001,0))
    with pytest.raises(ValueError, match='bounds'):
        GGUFFile(path)


def test_strata_stream_normalizes_tool_and_text_packets(tmp_path):
    from forge_vendor.strata.frontend import OutputParser
    parser = OutputParser(thinking=False,tools=[{'name':'project_read','parameters':{'type':'object','properties':{'path':{'type':'string'}}}}])
    fragments=['Ready.\n<tool_', 'call><function=project_read><parameter=path>README.md</parameter></function></tool_call>']
    events=[]
    for fragment in fragments: events.extend(parser.feed(fragment))
    events.extend(parser.finish())
    calls=[e.call for e in events if e.kind=='tool_call']
    assert len(calls)==1 and calls[0].arguments=={'path':'README.md'}
    assert calls[0].name=='project_read'


def test_shutdown_during_verification_cannot_spawn_worker(tmp_path,monkeypatch):
    runtime=NativeRuntime(tmp_path/'home')
    record=runtime.catalog.add([gguf(tmp_path/'weights.gguf')],identifier='test')
    monkeypatch.setattr(runtime,'_executable',lambda engine:tmp_path/'not-executed.exe')
    def hash_then_close(path):
        runtime.close()
        return record['files'][0]['sha256']
    monkeypatch.setattr('forge_native.file_hash',hash_then_close)
    spawn=[]
    monkeypatch.setattr(runtime,'_spawn',lambda *a,**k:spawn.append(a))
    with pytest.raises(NativeFailure) as exc:
        runtime._start(record,8192,threading.Event())
    assert exc.value.category=='cancelled' and spawn==[] and runtime.worker is None


def test_stale_idle_timer_cannot_stop_new_request(tmp_path):
    runtime=NativeRuntime(tmp_path/'home')
    closed=[]
    runtime.worker={'process':SimpleNamespace(poll=lambda:0,stdin=None),'job':SimpleNamespace(close=lambda:closed.append(True))}
    runtime.state='ready'
    runtime._retain('10m')
    expire=runtime.idle_timer.function
    runtime.generation+=1
    expire()
    assert runtime.worker is not None and closed==[]
    runtime.close()
    assert closed==[True]


def test_release_after_request_disposes_only_owned_worker(tmp_path):
    runtime=NativeRuntime(tmp_path/'home')
    closed=[]
    runtime.worker={'process':SimpleNamespace(poll=lambda:0,stdin=None),'job':SimpleNamespace(close=lambda:closed.append(True))}
    runtime._retain(0)
    assert runtime.worker is None and runtime.state=='stopped' and closed==[True]
