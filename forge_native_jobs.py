"""Explicit model operations with durable progress and no external setup executables."""
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from forge_native_models import atomic_json, read_json, file_hash


def download_space_required(selected, root):
    """Credit only this pinned download's files, never unrelated caches."""
    present = 0
    files=selected['files']+selected.get('support',{}).get('files',[])
    if selected.get('projection'): files = files + [selected['projection']['file']]
    for item in files:
        target = root / item['path']
        partial = target.with_suffix(target.suffix + '.forge-partial')
        if target.is_file() and not target.is_symlink() and target.stat().st_size == item['size']:
            present += item['size']
        elif partial.is_file() and not partial.is_symlink():
            present += min(partial.stat().st_size, item['size'])
    preparation = selected.get('preparation_bytes', 0)
    prepared = root / 'pack' / 'experts.bin'
    if preparation and prepared.is_file() and not prepared.is_symlink() and prepared.stat().st_size == preparation:
        present += preparation
    return max(0, sum(f['size'] for f in files) + preparation + 4 * 1024**3 - present)


def reuse_installed_file(catalog, item, directory, cancel):
    """Reuse exact immutable bytes without removing another installed model."""
    from forge_updates import plain_path
    target = plain_path(Path(directory) / item['path'])
    if not target.is_relative_to(catalog.root):
        raise ValueError('The model reuse target is outside Forge storage.')
    if target.exists(): return False
    def check():
        if cancel.is_set(): raise ValueError('Model setup paused.')
    check()
    for model in catalog.public()['models']:
        entries = model['files'] + model.get('support', {}).get('files', [])
        if model.get('projection'): entries = entries + [model['projection']['file']]
        for row in entries:
            if row.get('sha256') != item['sha256'] or row.get('size') != item['size']: continue
            source = plain_path(catalog.root / row['path'])
            if not source.is_relative_to(catalog.root) or not source.is_file(): continue
            before = source.stat()
            if before.st_size != item['size'] or file_hash(source, check=check) != item['sha256']: continue
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('A local model changed during reuse verification.')
            check()
            target.parent.mkdir(parents=True, exist_ok=True)
            try: os.link(source, target)
            except FileExistsError: return False
            except OSError: continue  # The normal verified download remains available.
            return True
    return False


class NativeJobs:
    def __init__(self, service):
        self.service = service
        self.runtime = service.providers.native()
        self.path = service.store.home / 'state' / 'native-jobs.json'
        self.lock = threading.RLock()
        self.jobs = read_json(self.path) if self.path.exists() else []
        self.closed = False
        self.cancel = threading.Event()
        for job in self.jobs:
            if job['state'] in ('queued', 'running'):
                job.update(state='interrupted', message='Forge restarted. Retry to continue the verified download or import.')

    def save(self):
        atomic_json(self.path, self.jobs[-32:])

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.jobs[-32:]))

    def active(self):
        return any(job['state'] in ('queued','running') for job in self.jobs)

    def profile(self):
        profile = {'id': 'forge-native', 'name': 'Forge', 'kind': 'forge-native', 'url': 'forge://native',
                   'managed': True, 'enabled': True, 'runtime_id': self.runtime.catalog.binding()}
        self.service.store.save_entity('providers', profile)
        self.service.providers.authority_changed('forge-native')

    def start(self, action, data):
        with self.lock:
            if self.active():
                raise ValueError('A Forge model operation is already in progress. Wait for it to finish.')
            job = {'id': uuid4().hex, 'action': action, 'state': 'queued',
                   'created_at': time.time(), 'message': 'Preparing model files.'}
            self.jobs.append(job)
            self.cancel.clear()
            self.save()
        def execute():
            try:
                with self.lock:
                    job.update(state='running')
                    self.save()
                if action == 'native_validate':
                    from forge_native_validation import validate_native
                    result = self.runtime.catalog.model(data['id'])
                    with self.service.providers.queue.lease(self.cancel):
                        evidence = validate_native(self.runtime,result['id'],self.cancel)
                    result.update(validation='passed', validation_evidence=evidence)
                    self.runtime.catalog.publish(result)
                elif action == 'native_import_ollama':
                    result = self.runtime.catalog.import_ollama(data['root'], data['id'])
                elif action == 'native_import':
                    result = self.runtime.catalog.add(data['paths'], name=data.get('name'))
                else:
                    lock = read_json(Path(__file__).parent / 'native-models.lock.json')
                    selected = next((m for m in lock['models'] if m['id'] == data.get('id')), None)
                    if selected is None:
                        raise ValueError('Choose a supported Forge model.')
                    from forge_native_download import pinned_download
                    import shutil
                    support=selected.get('support')
                    projection=selected.get('projection')
                    transfers=[(selected,item,True) for item in selected['files']]
                    if support:transfers += [(support,item,False) for item in support['files']]
                    if projection:transfers.append((projection,projection['file'],False))
                    download_bytes=sum(item['size'] for _,item,_ in transfers)
                    root = self.runtime.catalog.root / selected['id']
                    root.mkdir(parents=True, exist_ok=True)
                    with self.lock:
                        job.update(message='Checking reusable, verified model files.')
                        self.save()
                    for _, item, _ in transfers:
                        reuse_installed_file(self.runtime.catalog,item,root,self.cancel)
                    if shutil.disk_usage(root).free < download_space_required(selected, root):
                        raise ValueError('The Forge model directory needs more free disk space.')
                    sources = []
                    completed_bytes=0
                    for download_model,item,is_weight in transfers:
                        if self.closed:
                            raise ValueError('Model setup paused while Forge closed.')
                        with self.lock:
                            job.update(message='Downloading ' + item['path'], expected_bytes=download_bytes)
                            self.save()
                        def progress(done,total):
                            with self.lock:
                                job.update(received_bytes=completed_bytes+done, file_bytes=total)
                                self.save()
                        downloaded=pinned_download(download_model,item,root,cancel=self.cancel,progress=progress)
                        if is_weight:sources.append(downloaded)
                        completed_bytes+=item['size']
                    with self.lock:
                        job.update(message='Verifying model hashes. Original files are retained.')
                        self.save()
                    result = self.runtime.catalog.add(sources, name=selected['name'], engine=selected['engine'],publish=False,
                        identifier=selected['id'], expected=selected['files'],
                        origin={'kind': 'huggingface', 'repo': selected['repo'], 'revision': selected['revision'],
                                'license': selected['license']})
                    if support:result=self.runtime.catalog.attach_support(result,support)
                    if projection:result=self.runtime.catalog.attach_projection(result,projection)
                    self.runtime.catalog.publish(result)
                if self.closed:
                    raise ValueError('Model setup paused while Forge closed.')
                self.profile()
                with self.lock:
                    job.update(state='completed', model_id=result['id'], message='Synthetic text and tool continuation checks passed.' if action=='native_validate' else 'Ready in Forge. Original model files were retained.')
                    self.save()
            except Exception as exc:
                from forge_native import NativeFailure
                with self.lock:
                    message=str(exc) if isinstance(exc,NativeFailure) else 'Model setup did not finish. Check disk space, connectivity and model files, then retry. Original files were retained.'
                    job.update(state='interrupted', category=exc.category if isinstance(exc,NativeFailure) else 'model_setup', message=message)
                    self.save()
        threading.Thread(target=execute, name='Forge-model-setup', daemon=True).start()
        return {'job': dict(job)}


def native_action(service, action, data):
    with service.manager_lock:
        jobs = getattr(service, 'native_jobs', None)
        if jobs is None:
            jobs = service.native_jobs = NativeJobs(service)
    if action == 'native_status':
        return {**jobs.runtime.status(), 'jobs': jobs.status()}
    if action == 'native_inventory':
        return {'models': jobs.runtime.catalog.ollama_inventory(data['root'])}
    if action == 'native_cancel':
        jobs.cancel.set()
        return {'ok':True}
    active = any(run.get('status') not in ('completed','cancelled','failed','paused','interrupted')
                 and run.get('settings',{}).get('provider_id') == 'forge-native' for run in service.store.runs())
    if active:
        raise ValueError('Finish or pause the current Forge model task before changing its model files.')
    if action == 'native_configure':
        with jobs.lock:
            if jobs.active():
                raise ValueError('Wait for model setup to finish before changing its directory.')
            result = jobs.runtime.catalog.configure(data['root'])
            jobs.profile()
            return result
    if action == 'native_stop':
        jobs.runtime.stop()
        return {'ok': True}
    if action in ('native_import','native_import_ollama','native_download','native_validate'):
        return jobs.start(action, dict(data))
    raise ValueError('Unknown Forge model action.')
