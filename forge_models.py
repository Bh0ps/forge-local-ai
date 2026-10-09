"""Hugging Face discovery and explicit, resumable local GGUF installation.

Hub credentials live in the OS vault. Downloads never execute repository code;
Ollama receives verified GGUF blobs through its API rather than filesystem hacks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from uuid import uuid4

import httpx

from forge_store import atomic_text, encode


class DownloadCancelled(Exception):
    pass


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _repo(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value):
        raise ValueError('Enter a Hugging Face model repository as owner/model-name.')
    if '..' in value or '--' in value:
        raise ValueError('Invalid Hugging Face repository identifier.')
    return value


def _filename(value):
    if not isinstance(value, str) or not value or len(value) > 400 or '\\' in value or '\x00' in value:
        raise ValueError('Invalid repository file path.')
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('Repository file paths must remain inside the selected model folder.')
    for part in path.parts:
        if ':' in part or part.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part):
            raise ValueError('Unsafe repository file path.')
    return path.as_posix()


def _projector(path):
    return bool(re.search(r'(?:mmproj|projector|vision[-_]?encoder)', PurePosixPath(path).name, re.I))


def _quant(path):
    match = re.search(r'(?i)(?:^|[-_.])(IQ\d(?:_[A-Z0-9]+)+|Q\d(?:_[A-Z0-9]+)*|BF16|F32|F16|FP16)(?=[-_.]|$)', PurePosixPath(path).name)
    return match[1].upper() if match else None


def _license(card, tags=()):
    if hasattr(card, 'to_dict'):
        card = card.to_dict()
    value = card.get('license') if isinstance(card, dict) else None
    return value or next((tag.split(':', 1)[1] for tag in tags if tag.startswith('license:')), 'See repository license')


def _error(exc):
    name = type(exc).__name__
    if name in ('GatedRepoError', 'RepositoryNotFoundError'):
        return 'Repository unavailable. For a gated/private model, accept its license on Hugging Face and add a read token.'
    value = str(exc)[:1000]
    return re.sub(r'hf_[A-Za-z0-9]{10,}', '[redacted token]', value)


class ModelManager:
    def __init__(self, store, providers, vault=None, *, api_factory=None, download_fn=None, client_factory=None):
        self.store = store
        self.providers = providers
        self.vault = vault
        self.root = store.home / 'models' / 'huggingface'
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = store.home / 'state' / 'model-jobs.json'
        self.lock = threading.RLock()
        self.active = {}
        self.import_reservations = set()
        self.jobs = {}
        self.closed = False
        self.api_factory = api_factory
        self.download_fn = download_fn
        self.client_factory = client_factory or httpx.AsyncClient
        self.inspections = {}
        if self.journal.is_file():
            try:
                self.jobs = json.loads(self.journal.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                raise ValueError('Model job journal is damaged. Restore its backup.') from None
        for job in self.jobs.values():
            if job['status'] in ('queued', 'downloading', 'verifying', 'importing', 'cancelling'):
                job.update(status='import_unknown' if job.get('create_started') else 'interrupted',
                           message='Forge restarted. Review this job before retrying.', updated_at=_stamp())
        self._save()

    def _save(self):
        with self.lock:
            atomic_text(self.journal, encode(self.jobs))

    def _credential(self):
        try:
            source = self.store.entity('model_sources', 'huggingface')
        except ValueError:
            return False
        return (self.vault.get(source.get('credential_ref')) if self.vault else None) or False

    def _api(self):
        factory = self.api_factory
        if factory is None:
            try:
                from huggingface_hub import HfApi
            except ImportError:
                raise ValueError('Install the Hugging Face Hub dependency to discover and download models.') from None
            factory = HfApi
        # False explicitly avoids reading unrelated global Hub login/environment tokens.
        return factory(endpoint='https://huggingface.co', token=self._credential(), library_name='forge', library_version='4.1')

    def status(self):
        return {'ok': True, 'dependency_available': bool(self.api_factory or importlib.util.find_spec('huggingface_hub')),
                'metadata_available': bool(importlib.util.find_spec('gguf')), 'authenticated': bool(self._credential()),
                'download_root': str(self.root), 'active_jobs': len(self.active),
                'message': 'Public GGUF models need no account. Gated models require accepted terms and a read token.'}

    def dispatch(self, action, data=None):
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError('Expected a model action object.')
        if action == 'hf_status': return self.status()
        if action == 'hf_search': return self.search(data)
        if action == 'hf_files': return self.files(data)
        if action == 'hf_download': return self.download(data)
        if action == 'hf_jobs':
            with self.lock: return {'ok': True, 'jobs': sorted(json.loads(encode(list(self.jobs.values()))), key=lambda j: j['created_at'], reverse=True)}
        if action == 'hf_cancel': return self.cancel(data['id'])
        if action == 'hf_retry': return self.retry(data['id'])
        if action == 'hf_import': return self.import_model(data)
        if action == 'hf_reconcile': return self.reconcile(data['id'])
        if action == 'hf_auth':
            token = data.get('token', '').strip()
            if token and (not token.startswith('hf_') or len(token) > 512 or any(c.isspace() for c in token)):
                raise ValueError('Enter a Hugging Face read token.')
            if not self.vault: raise ValueError('OS credential storage is unavailable.')
            try: previous = self.store.entity('model_sources', 'huggingface')
            except ValueError: previous = {}
            reference = previous.get('credential_ref')
            if token:
                reference = self.vault.put(token, reference)
            elif reference:
                self.vault.delete(reference); reference = None
            self.store.save_entity('model_sources', {'id': 'huggingface', 'credential_ref': reference})
            return self.status()
        raise ValueError('Unknown Hugging Face action.')

    def search(self, data):
        query = str(data.get('query', '')).strip()
        limit = data.get('limit', 20)
        if len(query) > 200 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Search is limited to 200 characters and 50 repositories.')
        try:
            models = self._api().list_models(search=query or None, filter='gguf', sort='downloads', limit=limit,
                expand=['tags', 'downloads', 'likes', 'gated', 'cardData', 'pipeline_tag'])
            result = []
            for model in models:
                tags = list(getattr(model, 'tags', None) or [])
                result.append({'repo_id': model.id, 'downloads': getattr(model, 'downloads', 0), 'likes': getattr(model, 'likes', 0),
                               'tags': tags[:30], 'gated': bool(getattr(model, 'gated', False)),
                               'license': _license(getattr(model, 'card_data', None), tags),
                               'vision': getattr(model, 'pipeline_tag', None) == 'image-text-to-text',
                               'url': 'https://huggingface.co/' + model.id})
            return {'ok': True, 'models': result}
        except Exception as exc:
            raise ValueError(_error(exc)) from None

    def files(self, data):
        repo = _repo(data.get('repo_id'))
        revision = str(data.get('revision') or 'main')
        if len(revision) > 200 or any(c in revision for c in ('\x00', '\n', '\r')):
            raise ValueError('Invalid model revision.')
        cache = self.inspections.get((repo, revision))
        if cache and time.monotonic() - cache[0] < 120:
            return json.loads(encode(cache[1]))
        try:
            info = self._api().model_info(repo, revision=revision, files_metadata=True, timeout=20)
        except Exception as exc:
            raise ValueError(_error(exc)) from None
        if not re.fullmatch('[a-fA-F0-9]{40,64}', str(info.sha)):
            raise ValueError('The Hub did not return an immutable revision for this model.')
        items = []
        for file in (info.siblings or [])[:10000]:
            try: path = _filename(file.rfilename)
            except ValueError: continue
            suffix = PurePosixPath(path).suffix.lower()
            if suffix != '.gguf' and not self._notice(path): continue
            lfs = getattr(file, 'lfs', None)
            digest = lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)
            items.append({'path': path, 'size': getattr(file, 'size', None), 'sha256': digest,
                          'quantization': _quant(path), 'is_projector': _projector(path), 'downloadable': suffix == '.gguf',
                          'format': 'GGUF' if suffix == '.gguf' else 'notice'})
        tags = list(getattr(info, 'tags', None) or [])
        projectors = [f for f in items if f['is_projector'] and f['downloadable']]
        result = {'ok': True, 'repo_id': repo, 'revision': info.sha, 'license': _license(getattr(info, 'card_data', None), tags),
                  'gated': bool(getattr(info, 'gated', False)), 'files': items, 'projectors': projectors,
                  'vision': getattr(info, 'pipeline_tag', None) == 'image-text-to-text' or bool(projectors),
                  'warnings': ['GGUF support depends on engine architecture and template support. Capabilities are verified after import.']}
        if projectors: result['warnings'].append('For vision, select a matching projector. Downloaded files also work with an explicitly configured llama.cpp runtime.')
        if result['gated']: result['warnings'].append('Accept repository terms on Hugging Face before downloading with a read token.')
        if not any(f['downloadable'] and not f['is_projector'] for f in items): result['warnings'].append('This repository has no supported GGUF model weights. Safetensors conversion is not performed by Forge.')
        self.inspections[(repo, revision)] = (time.monotonic(), result)
        self.inspections[(repo, info.sha)] = (time.monotonic(), result)
        return json.loads(encode(result))

    @staticmethod
    def _notice(path):
        name = PurePosixPath(path).name.lower()
        return name == 'readme.md' or name.startswith(('license', 'copying', 'notice'))

    def download(self, data):
        inspection = self.files(data)
        filenames = data.get('filenames')
        if not isinstance(filenames, list) or not 1 <= len(filenames) <= 128:
            raise ValueError('Select between 1 and 128 model files.')
        selected = list(dict.fromkeys(_filename(v) for v in filenames))
        available = {f['path']: f for f in inspection['files']}
        if any(f not in available or not available[f]['downloadable'] for f in selected):
            raise ValueError('Select GGUF files from the inspected repository revision.')
        total = sum(available[f]['size'] or 0 for f in selected)
        if total > shutil.disk_usage(self.root).free:
            raise ValueError('Insufficient free disk space for the selected model files.')
        if any(available[f]['size'] is None for f in selected):
            raise ValueError('The Hub did not report a file size. Inspect this repository again before downloading.')
        notices = [f['path'] for f in inspection['files'] if self._notice(f['path']) and f['size'] is not None and f['size'] <= 2_000_000]
        job = dict(id=uuid4().hex, kind='download', status='queued', repo_id=inspection['repo_id'], revision=inspection['revision'],
                   filenames=selected, notices=notices, metadata={f: available[f] for f in selected + notices},
                   license=inspection['license'], vision=inspection['vision'], total_bytes=total, completed_bytes=0,
                   files=[], created_at=_stamp(), updated_at=_stamp(), message='Waiting to download.')
        self._register(job, self._download_worker)
        return {'ok': True, 'job': json.loads(encode(job))}

    def _register(self, job, worker):
        with self.lock:
            if self.closed: raise ValueError('Model manager is shutting down.')
            self.jobs[job['id']] = job
            event = threading.Event()
            thread = threading.Thread(target=self._worker, args=(job['id'], event, worker), name='forge-model-' + job['id'][:8], daemon=True)
            self.active[job['id']] = {'cancel': event, 'thread': thread}
            self._save()
            thread.start()

    def _set(self, identifier, **changes):
        with self.lock:
            self.jobs[identifier].update(changes, updated_at=_stamp())
            self._save()

    @staticmethod
    def _check(cancel):
        if cancel.is_set(): raise DownloadCancelled('Model operation cancelled.')

    def _worker(self, identifier, cancel, worker):
        try:
            worker(identifier, cancel)
        except (DownloadCancelled, asyncio.CancelledError):
            job = self.jobs[identifier]
            self._set(identifier, status='import_unknown' if job.get('create_started') else 'interrupted' if self.closed else 'cancelled',
                      message='Inspect the target Ollama model before retrying.' if job.get('create_started') else 'Cancelled. Partial downloads are retained for retry.')
        except Exception as exc:
            job = self.jobs[identifier]
            self._set(identifier, status='import_unknown' if job.get('create_started') else 'failed', error=_error(exc),
                      message='Review the target model; downloaded GGUF files remain available.' if job.get('create_started') else 'Download/import failed. Retry is available.')
        finally:
            with self.lock:
                current = self.active.get(identifier)
                if current and current['cancel'] is cancel:
                    self.active.pop(identifier, None)

    def _download_worker(self, identifier, cancel):
        job = self.jobs[identifier]
        folder = self.root / job['repo_id'].replace('/', '--') / job['revision']
        folder.mkdir(parents=True, exist_ok=True)
        self._set(identifier, status='downloading', message='Downloading selected files.', directory=str(folder))
        download = self.download_fn
        if download is None:
            from huggingface_hub import hf_hub_download
            download = hf_hub_download
        completed = 0
        records = []
        for filename in job['filenames'] + job.get('notices', []):
            self._check(cancel)
            meta = job['metadata'][filename]
            notice = filename in job.get('notices', [])
            previous = completed
            manager = self
            from tqdm.auto import tqdm
            class Progress(tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs.pop('name', None)
                    kwargs.update(file=io.StringIO(), disable=False)
                    self.last_saved = 0
                    super().__init__(*args, **kwargs)
                def update(self, n=1):
                    manager._check(cancel)
                    result = super().update(n)
                    if not notice and time.monotonic() - self.last_saved > .5:
                        self.last_saved = time.monotonic()
                        manager._set(identifier, completed_bytes=min(job['total_bytes'], previous + min(int(self.n), meta['size'])), current_file=filename)
                    return result
            self._set(identifier, current_file=filename)
            path = Path(download(repo_id=job['repo_id'], filename=filename, revision=job['revision'], local_dir=folder,
                token=self._credential(), library_name='forge', library_version='4.1', tqdm_class=Progress)).resolve()
            self._check(cancel)
            if not path.is_relative_to(folder.resolve()) or path.is_symlink() or not path.is_file():
                raise ValueError('Downloaded file resolved outside its model folder.')
            if meta.get('size') is not None and path.stat().st_size != meta['size']:
                raise ValueError('Downloaded model file size differs from the inspected revision.')
            if notice:
                continue
            self._set(identifier, status='verifying', message='Verifying downloaded GGUF checksums.')
            digest = self._hash(path, cancel)
            if meta.get('sha256') and digest != meta['sha256']:
                raise ValueError('Downloaded file checksum differs from the inspected revision.')
            with path.open('rb') as handle:
                if handle.read(4) != b'GGUF': raise ValueError('Selected file is not a GGUF model. Nothing was imported.')
            records.append({**meta, 'local_path': str(path), 'sha256': digest})
            completed += meta['size']
            self._set(identifier, status='downloading', files=records, completed_bytes=completed)
        self._check(cancel)
        atomic_text(folder / 'forge-model.json', encode({'repo_id': job['repo_id'], 'revision': job['revision'], 'license': job['license'], 'files': records}))
        self._set(identifier, status='completed', files=records, completed_bytes=job['total_bytes'], current_file=None,
                  message='Download complete. Choose Import to Ollama, or select these files in a llama.cpp runtime.')

    @staticmethod
    def _hash(path, cancel):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                ModelManager._check(cancel)
                digest.update(chunk)
        return digest.hexdigest()

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.jobs: raise ValueError('Model job not found.')
            active = self.active.get(identifier)
            if active:
                active['cancel'].set(); self._set(identifier, status='cancelling', message='Stopping this operation…')
            return {'ok': True, 'job': json.loads(encode(self.jobs[identifier]))}

    def retry(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            if not job: raise ValueError('Model job not found.')
            if job['status'] not in ('failed', 'cancelled', 'interrupted'):
                raise ValueError('Inspect an unknown import outcome before retrying. Active or completed jobs are not repeated.')
            job = json.loads(encode(job))
            job.update(status='queued', error=None, message='Retrying from retained download state.', create_started=False)
            self._register(job, self._download_worker if job['kind'] == 'download' else self._import_worker)
            return {'ok': True, 'job': job}

    def _local_file(self, record):
        lexical = Path(os.path.abspath(record['local_path']))
        path = lexical.resolve(strict=True)
        if not lexical.is_relative_to(self.root.resolve()) or not path.is_relative_to(self.root.resolve()) or not path.is_file() or path.suffix.lower() != '.gguf':
            raise ValueError('Import requires a GGUF file from a completed Forge download.')
        current = self.root
        for part in lexical.relative_to(self.root.resolve()).parts:
            current /= part
            info = current.lstat()
            if current.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Model imports cannot use links or junctions.')
        return path

    @staticmethod
    def _metadata(path):
        try:
            from gguf import GGUFReader
        except ImportError:
            raise ValueError('Install the GGUF metadata dependency before importing.') from None
        try:
            reader = GGUFReader(path)
            keys = ('general.architecture', 'general.name', 'general.type', 'split.count', 'clip.projector_type',
                    'clip.has_vision_encoder', 'clip.vision.projection_dim')
            result = {key: reader.fields[key].contents() for key in keys if key in reader.fields}
            template = reader.fields.get('tokenizer.chat_template')
            if template is not None:
                template = template.contents()
                if not isinstance(template, str) or not template.strip() or len(template.encode('utf-8')) > 262144:
                    raise ValueError('GGUF chat template must be nonempty bounded text.')
                result['chat_template_sha256'] = hashlib.sha256(template.encode('utf-8')).hexdigest()
                result['chat_template_present'] = True
            else:
                result['chat_template_present'] = False
            if 'tokenizer.ggml.model' in reader.fields:
                result['tokenizer_model'] = reader.fields['tokenizer.ggml.model'].contents()
            architecture = result.get('general.architecture', '')
            for key in (architecture + '.embedding_length',):
                if key in reader.fields: result[key] = reader.fields[key].contents()
            if not architecture or not reader.tensors: raise ValueError('Model GGUF is missing architecture or tensors.')
            return result
        except (ValueError, OSError, KeyError, IndexError, OverflowError) as exc:
            raise ValueError('GGUF metadata could not be validated: ' + str(exc)[:300]) from None

    def import_model(self, data):
        source = self.jobs.get(data.get('job_id'))
        if not source or source['kind'] != 'download' or source['status'] != 'completed':
            raise ValueError('Complete a Forge GGUF download before importing.')
        model_name = str(data.get('model_name', '')).strip()
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?(?::[a-zA-Z0-9][a-zA-Z0-9_.-]*)?', model_name) or len(model_name) > 160:
            raise ValueError('Choose a valid Ollama model name, for example forge-model:q4_k_m.')
        records = {f['path']: f for f in source['files']}
        filename = data.get('filename')
        models = [f for f in source['files'] if not f['is_projector']]
        if not filename and len(models) == 1: filename = models[0]['path']
        if filename not in records or records[filename]['is_projector']:
            raise ValueError('Choose one downloaded model GGUF, not a projector.')
        selected = [records[filename]]
        split = re.match(r'^(.*)-(\d{5})-of-(\d{5})\.gguf$', filename, re.I)
        if split:
            expected = [split[1] + '-' + str(n).zfill(5) + '-of-' + split[3] + '.gguf' for n in range(1, int(split[3]) + 1)]
            if len(expected) > 128 or any(f not in records for f in expected): raise ValueError('Download all split GGUF shards before importing.')
            selected = [records[f] for f in expected]
        projector = data.get('projector')
        if projector:
            if projector not in records or not records[projector]['is_projector']:
                raise ValueError('Choose a downloaded vision projector from this repository revision.')
            selected.append(records[projector])
        elif source.get('vision') and not data.get('text_only'):
            raise ValueError('This repository advertises vision. Select its matching projector or explicitly choose a text-only import. GGUF files remain available for llama.cpp.')
        provider_id = data.get('provider_id') or 'ollama'
        provider = self.providers.provider(provider_id)
        if not hasattr(provider, 'core'):
            raise ValueError('GGUF import targets Ollama. Select downloaded files in a managed llama.cpp runtime for another engine.')
        reservation = (provider_id, model_name if ':' in model_name else model_name + ':latest')
        with self.lock:
            if reservation in self.import_reservations or any(j.get('kind') == 'import' and
                j.get('provider_id') == provider_id and self._same_model(j.get('model_name', ''), model_name) and
                (j['id'] in self.active or j['status'] == 'import_unknown') for j in self.jobs.values()):
                raise ValueError('An import with this model name is active or has an unknown outcome. Inspect it before creating another.')
            self.import_reservations.add(reservation)
        try:
            existing = provider.models()
            if any(self._same_model(m.get('name', m.get('id', '')), model_name) for m in existing):
                raise ValueError('That model name already exists. Choose a new name; Forge does not overwrite models during import.')
            job = dict(id=uuid4().hex, kind='import', status='queued', source_job=source['id'], repo_id=source['repo_id'],
                       revision=source['revision'], files=selected, model_name=model_name, provider_id=provider_id,
                       license=source['license'], vision_required=bool(projector), total_bytes=sum(f['size'] for f in selected),
                       completed_bytes=0, create_started=False, created_at=_stamp(), updated_at=_stamp(), message='Preparing Ollama import.')
            self._register(job, self._import_worker)
            return {'ok': True, 'job': json.loads(encode(job))}
        finally:
            with self.lock: self.import_reservations.discard(reservation)

    @staticmethod
    def _same_model(left, right):
        return (left if ':' in left else left + ':latest') == (right if ':' in right else right + ':latest')

    def _import_worker(self, identifier, cancel):
        job = self.jobs[identifier]
        provider = self.providers.provider(job['provider_id'])
        base = provider.core.base.rstrip('/')
        main_info = None
        validated = []
        for record in job['files']:
            self._check(cancel)
            path = self._local_file(record)
            self._set(identifier, status='verifying', current_file=record['path'], message='Checking GGUF architecture and checksums.')
            if self._hash(path, cancel) != record['sha256']:
                raise ValueError('A downloaded GGUF changed after verification. Download it again before importing.')
            metadata = self._metadata(path)
            if record['is_projector']:
                if metadata.get('general.architecture') not in ('clip', 'mmproj', 'projector') and metadata.get('general.type') not in ('projector', 'mmproj'):
                    raise ValueError('Selected projector does not contain recognized vision-projector metadata. Use llama.cpp explicitly after verifying the matching files.')
                dimension = metadata.get('clip.vision.projection_dim')
                embedding = main_info.get(main_info['general.architecture'] + '.embedding_length') if main_info else None
                if dimension and embedding and dimension != embedding:
                    raise ValueError('Projector output dimension does not match model embeddings. Select a matching projector.')
            else:
                main_info = metadata
                if metadata['general.architecture'] == 'clip': raise ValueError('A standalone vision encoder cannot be imported as a language model.')
            validated.append((record, path))
        self._check(cancel)
        self._set(identifier, status='importing', message='Uploading verified files to the selected Ollama engine.', metadata=main_info)
        async def request():
            token = None
            if job['provider_id'] != 'ollama':
                config = self.store.entity('providers', job['provider_id'])
                if self.vault and config.get('credential_ref'): token = self.vault.get(config['credential_ref'])
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            async with self.client_factory(timeout=httpx.Timeout(120, connect=10), trust_env=False, headers=headers) as client:
                if job['vision_required']:
                    version = await client.get(base + '/version'); version.raise_for_status()
                    match = re.match(r'(\d+)\.(\d+)\.(\d+)', str(version.json().get('version', '')))
                    if not match or tuple(map(int, match.groups())) < (0, 35, 1):
                        raise ValueError('This projector import requires the tested Ollama 0.35.1 or newer. Keep these files for llama.cpp, or update the engine explicitly.')
                blobs = {}
                completed = 0
                for record, path in validated:
                    self._check(cancel)
                    digest = 'sha256:' + record['sha256']
                    response = await client.head(base + '/blobs/' + digest)
                    if response.status_code == 404:
                        previous = completed
                        async def chunks():
                            written = 0; last = 0
                            with path.open('rb') as handle:
                                while chunk := handle.read(1024 * 1024):
                                    self._check(cancel); written += len(chunk)
                                    if time.monotonic() - last > .5:
                                        last = time.monotonic()
                                        self._set(identifier, completed_bytes=previous + written, current_file=record['path'])
                                    yield chunk
                        response = await client.post(base + '/blobs/' + digest, content=chunks(),
                            headers={'Content-Length': str(record['size']), 'Content-Type': 'application/octet-stream'})
                        response.raise_for_status()
                    else: response.raise_for_status()
                    completed += record['size']
                    self._set(identifier, completed_bytes=completed)
                    blobs[PurePosixPath(record['path']).name] = digest
                self._check(cancel)
                # Unknown outcome is persisted before the request can write a manifest.
                self._set(identifier, create_started=True, message='Creating the Ollama model from verified blobs.')
                success = False
                async with client.stream('POST', base + '/create', json={'model': job['model_name'], 'files': blobs,
                    'license': str(job['license']), 'stream': True}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        self._check(cancel)
                        if not line.strip(): continue
                        if len(line) > 100000: raise ValueError('Ollama returned oversized import progress.')
                        packet = json.loads(line)
                        if packet.get('error'): raise ValueError(str(packet['error']))
                        self._set(identifier, message=str(packet.get('status', 'Creating model.'))[:500])
                        if packet.get('status') == 'success': success = True
                if not success: raise ValueError('Ollama import stream ended before success. Inspect the model list before retrying.')
                info = await client.post(base + '/show', json={'model': job['model_name']}); info.raise_for_status()
                capabilities = info.json().get('capabilities', [])
                warnings = ['Imported capabilities are reported by the engine; image/tool generation still needs runtime validation.']
                if job['vision_required'] and 'vision' not in capabilities:
                    warnings.append('Ollama imported the files but did not advertise vision. Use the retained GGUF/projector with llama.cpp after validation.')
                self._set(identifier, status='completed', create_started=False, current_file=None, capabilities=capabilities,
                          warnings=warnings, message='Model imported. It is available in the model selector.', models=await asyncio.to_thread(provider.models))
        async def run():
            task = asyncio.create_task(request())
            async def watch():
                while not cancel.is_set(): await asyncio.sleep(.1)
                task.cancel()
            watcher = asyncio.create_task(watch())
            try: await task
            finally:
                watcher.cancel(); await asyncio.gather(watcher, return_exceptions=True)
        asyncio.run(run())

    def reconcile(self, identifier):
        job = self.jobs.get(identifier)
        if not job or job['kind'] != 'import' or job['status'] != 'import_unknown':
            raise ValueError('Choose an import with an unknown outcome.')
        provider = self.providers.provider(job['provider_id'])
        models = provider.models()
        found = any(self._same_model(m.get('name', m.get('id', '')), job['model_name']) for m in models)
        self._set(identifier, status='completed' if found else 'interrupted', create_started=False, models=models,
                  message='The target model exists. Review its capabilities before use.' if found else 'The target model is absent. Retry is available.')
        return {'ok': True, 'job': json.loads(encode(job)), 'models': models}

    def shutdown(self):
        with self.lock:
            self.closed = True
            active = list(self.active.values())
            for job in active: job['cancel'].set()
        for job in active: job['thread'].join(timeout=2)
