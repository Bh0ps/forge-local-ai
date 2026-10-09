"""HF revision selection, durable download and real blob/create protocol regressions."""
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from gguf import GGUFWriter

from forge_models import ModelManager
from forge_store import ForgeStore

REVISION = 'a' * 40


def gguf_bytes(tmp_path, architecture='llama', projector=False):
    path = tmp_path / ('projector.gguf' if projector else 'model.gguf')
    writer = GGUFWriter(path, architecture)
    if projector:
        writer.add_bool('clip.has_vision_encoder', True)
        writer.add_uint32('clip.vision.projection_dim', 4)
    else:
        writer.add_uint32(architecture + '.embedding_length', 4)
    writer.add_tensor('fixture.weight', np.zeros((4, 4), dtype=np.float32))
    writer.write_header_to_file(); writer.write_kv_data_to_file(); writer.write_tensors_to_file(); writer.close()
    return path.read_bytes()


class Vault:
    def __init__(self): self.values = {}
    def put(self, token, reference=None):
        reference = reference or 'opaque-reference'
        self.values[reference] = token
        return reference
    def get(self, reference): return self.values.get(reference)
    def delete(self, reference): self.values.pop(reference, None)


class HubAPI:
    def __init__(self, files, **kwargs): self.files = files; self.kwargs = kwargs
    def model_info(self, repo, **kwargs):
        return SimpleNamespace(sha=REVISION, siblings=[SimpleNamespace(rfilename=name, size=len(content), lfs={'sha256': hashlib.sha256(content).hexdigest()})
            for name, content in self.files.items()], tags=['license:mit'], card_data={'license': 'mit'}, gated=False,
            pipeline_tag='image-text-to-text' if any('mmproj' in name for name in self.files) else 'text-generation')
    def list_models(self, **kwargs):
        assert kwargs['filter'] == 'gguf'
        return [SimpleNamespace(id='fixtures/model-GGUF', tags=['gguf', 'license:mit'], downloads=20, likes=1,
                                gated=False, card_data={'license': 'mit'}, pipeline_tag='text-generation')]


class Ollama:
    def __init__(self): self.core = SimpleNamespace(base='http://127.0.0.1:11434/api'); self.installed = []
    def models(self): return [{'name': name} for name in self.installed]


def wait(manager, identifier, statuses=('completed', 'failed', 'cancelled', 'interrupted', 'import_unknown')):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = manager.dispatch('hf_jobs')['jobs']
        item = next(j for j in job if j['id'] == identifier)
        if item['status'] in statuses: return item
        time.sleep(.01)
    raise AssertionError('Disposable model job did not finish')


@pytest.fixture
def manager(tmp_path):
    files = {'fixture-Q4_K_M.gguf': gguf_bytes(tmp_path), 'LICENSE': b'Fixture license notice\n'}
    api_kwargs = []
    def factory(**kwargs): api_kwargs.append(kwargs); return HubAPI(files, **kwargs)
    def download(**kwargs):
        path = Path(kwargs['local_dir']) / kwargs['filename']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[kwargs['filename']])
        return str(path)
    provider = Ollama()
    mgr = ModelManager(ForgeStore(tmp_path / 'forge'), SimpleNamespace(provider=lambda id: provider), Vault(), api_factory=factory, download_fn=download)
    mgr.fixture_files = files; mgr.fixture_provider = provider; mgr.fixture_api_kwargs = api_kwargs
    yield mgr
    mgr.shutdown()


def download_model(manager, filenames=None):
    return manager.dispatch('hf_download', {'repo_id': 'fixtures/model-GGUF', 'revision': 'main',
        'filenames': filenames or ['fixture-Q4_K_M.gguf']})['job']['id']


def test_search_pins_revision_and_displays_file_license_size_quantization(manager):
    assert manager.dispatch('hf_search', {'query': 'fixture'})['models'][0]['repo_id'] == 'fixtures/model-GGUF'
    inspected = manager.dispatch('hf_files', {'repo_id': 'fixtures/model-GGUF'})
    assert inspected['revision'] == REVISION and inspected['license'] == 'mit'
    assert inspected['files'][0]['quantization'] == 'Q4_K_M'
    assert inspected['files'][0]['size'] == len(manager.fixture_files['fixture-Q4_K_M.gguf'])
    assert manager.fixture_api_kwargs[0]['token'] is False


def test_download_keeps_exact_revision_checksum_notices_and_no_phantom_model(manager):
    job = wait(manager, download_model(manager))
    assert job['status'] == 'completed' and job['revision'] == REVISION
    path = Path(job['files'][0]['local_path'])
    assert path.parent.name == REVISION and path.read_bytes() == manager.fixture_files['fixture-Q4_K_M.gguf']
    assert (path.parent / 'LICENSE').read_bytes() == manager.fixture_files['LICENSE']
    assert job['files'][0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manager.fixture_provider.models() == []


def test_hf_token_is_only_in_credential_vault(manager):
    token = 'hf_' + 'SyntheticCredential' * 3
    manager.dispatch('hf_auth', {'token': token})
    assert manager.status()['authenticated']
    assert token not in manager.store.db_path.read_bytes().decode(errors='ignore')
    assert token not in manager.journal.read_text()
    assert manager._api().kwargs['token'] == token
    manager.dispatch('hf_auth', {'token': ''})
    assert not manager.status()['authenticated']


@pytest.mark.parametrize('filename', ['../secret.gguf', '/secret.gguf', 'CON.gguf', 'file:stream.gguf', 'nested\\file.gguf'])
def test_unsafe_download_paths_are_rejected_before_any_download(manager, filename):
    with pytest.raises(ValueError):
        download_model(manager, [filename])
    assert not manager.jobs


def test_download_checks_sha_before_becoming_complete(manager):
    manager.download_fn = lambda **kwargs: corrupt_download(kwargs)
    def corrupt_download(kwargs):
        path = Path(kwargs['local_dir']) / kwargs['filename']
        path.parent.mkdir(parents=True, exist_ok=True)
        data = manager.fixture_files[kwargs['filename']]
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        return path
    job = wait(manager, download_model(manager))
    assert job['status'] == 'failed' and 'checksum' in job['error']
    assert manager.fixture_provider.models() == []


def test_cancel_and_retry_resume_same_pinned_job(manager):
    started = threading.Event(); release = threading.Event()
    original = manager.download_fn
    def delayed(**kwargs):
        started.set(); release.wait(3)
        return original(**kwargs)
    manager.download_fn = delayed
    identifier = download_model(manager)
    assert started.wait(3)
    manager.dispatch('hf_cancel', {'id': identifier}); release.set()
    assert wait(manager, identifier)['status'] == 'cancelled'
    manager.download_fn = original
    manager.dispatch('hf_retry', {'id': identifier})
    assert wait(manager, identifier)['status'] == 'completed'


def test_restart_marks_download_interrupted_and_import_outcome_unknown(manager):
    manager.jobs.update({'download': {'id': 'download', 'kind': 'download', 'status': 'downloading'},
                         'import': {'id': 'import', 'kind': 'import', 'status': 'importing', 'create_started': True}})
    manager._save()
    restored = ModelManager(manager.store, manager.providers, manager.vault)
    assert restored.jobs['download']['status'] == 'interrupted'
    assert restored.jobs['import']['status'] == 'import_unknown'
    with pytest.raises(ValueError, match='Inspect'): restored.retry('import')
    restored.shutdown()


def import_transport(manager, fail=False, stalled=False):
    requests = []
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"status":"parsing GGUF"}\n'
            await asyncio.sleep(10)
        async def aclose(self): pass
    async def handler(request):
        content = await request.aread()
        requests.append((request.method, request.url.path, content))
        if request.method == 'HEAD': return httpx.Response(404)
        if '/blobs/' in request.url.path: return httpx.Response(201)
        if request.url.path == '/api/create':
            if stalled: return httpx.Response(200, stream=Stalled())
            if fail: return httpx.Response(200, content=b'{"status":"parsing GGUF"}\n')
            manager.fixture_provider.installed.append(json.loads(content)['model'])
            return httpx.Response(200, content=b'{"status":"success"}\n')
        if request.url.path == '/api/show': return httpx.Response(200, json={'capabilities': ['completion', 'tools']})
        if request.url.path == '/api/version': return httpx.Response(200, json={'version': '0.35.1'})
        raise AssertionError(request.url)
    transport = httpx.MockTransport(handler)
    manager.client_factory = lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)
    return requests


def test_import_streams_verified_blob_and_uses_api_create_then_refreshes_models(manager):
    source = wait(manager, download_model(manager))
    requests = import_transport(manager)
    job = manager.dispatch('hf_import', {'job_id': source['id'], 'model_name': 'forge-fixture:q4_k_m'})['job']
    result = wait(manager, job['id'])
    assert result['status'] == 'completed' and 'tools' in result['capabilities']
    uploaded = next(content for method, url, content in requests if method == 'POST' and '/blobs/' in url)
    assert uploaded == manager.fixture_files['fixture-Q4_K_M.gguf']
    created = json.loads(next(content for _, url, content in requests if url == '/api/create'))
    assert created['files'] == {'fixture-Q4_K_M.gguf': 'sha256:' + source['files'][0]['sha256']}
    assert result['models'] == [{'name': 'forge-fixture:q4_k_m'}]
    assert manager.fixture_provider.installed == ['forge-fixture:q4_k_m']


def test_import_rejects_modified_download_before_upload(manager):
    source = wait(manager, download_model(manager))
    Path(source['files'][0]['local_path']).write_bytes(b'GGUF changed')
    requests = import_transport(manager)
    result = wait(manager, manager.dispatch('hf_import', {'job_id': source['id'], 'model_name': 'fixture'})['job']['id'])
    assert result['status'] == 'failed' and 'changed' in result['error']
    assert not requests and not manager.fixture_provider.installed


def test_incomplete_import_requires_reconciliation_before_retry(manager):
    source = wait(manager, download_model(manager))
    import_transport(manager, fail=True)
    result = wait(manager, manager.dispatch('hf_import', {'job_id': source['id'], 'model_name': 'fixture'})['job']['id'])
    assert result['status'] == 'import_unknown'
    with pytest.raises(ValueError, match='Inspect'): manager.retry(result['id'])
    assert manager.reconcile(result['id'])['job']['status'] == 'interrupted'


def test_cancel_aborts_stalled_import_and_preserves_unknown_action(manager):
    source = wait(manager, download_model(manager))
    import_transport(manager, stalled=True)
    job = manager.dispatch('hf_import', {'job_id': source['id'], 'model_name': 'fixture'})['job']
    deadline = time.monotonic() + 5
    while not manager.jobs[job['id']].get('create_started') and time.monotonic() < deadline: time.sleep(.01)
    assert manager.jobs[job['id']]['create_started']
    start = time.monotonic(); manager.cancel(job['id'])
    assert wait(manager, job['id'])['status'] == 'import_unknown'
    assert time.monotonic() - start < 2


def test_vision_repository_requires_projector_or_explicit_text_only(manager):
    manager.fixture_files['mmproj-fixture-f16.gguf'] = gguf_bytes(manager.store.home, 'clip', projector=True)
    manager.inspections.clear()
    source = wait(manager, download_model(manager, ['fixture-Q4_K_M.gguf', 'mmproj-fixture-f16.gguf']))
    with pytest.raises(ValueError, match='projector'):
        manager.import_model({'job_id': source['id'], 'model_name': 'fixture', 'filename': 'fixture-Q4_K_M.gguf'})


def test_split_model_cannot_import_missing_shards(manager):
    manager.fixture_files['fixture-Q4_K_M-00001-of-00002.gguf'] = manager.fixture_files['fixture-Q4_K_M.gguf']
    manager.inspections.clear()
    source = wait(manager, download_model(manager, ['fixture-Q4_K_M-00001-of-00002.gguf']))
    with pytest.raises(ValueError, match='all split'):
        manager.import_model({'job_id': source['id'], 'model_name': 'fixture'})


def test_model_name_never_overwrites_existing_installation(manager):
    source = wait(manager, download_model(manager))
    manager.fixture_provider.installed = ['fixture:latest']
    with pytest.raises(ValueError, match='already exists'):
        manager.import_model({'job_id': source['id'], 'model_name': 'fixture'})


def test_parallel_same_name_imports_cannot_race_creation(manager):
    source = wait(manager, download_model(manager))
    requests = import_transport(manager, stalled=True)
    job = manager.import_model({'job_id': source['id'], 'model_name': 'fixture'})['job']
    with pytest.raises(ValueError, match='active|unknown'):
        manager.import_model({'job_id': source['id'], 'model_name': 'fixture:latest'})
    manager.cancel(job['id'])
    wait(manager, job['id'])
    assert len([r for r in requests if r[1] == '/api/create']) <= 1
