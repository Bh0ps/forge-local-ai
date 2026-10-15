"""Forge-owned GGUF catalog. Imports preserve their sources until validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from uuid import uuid4


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path, maximum=2 * 1024**2):
    with Path(path).open('rb') as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError('Model metadata exceeds the supported size.')
    return json.loads(content)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt==5: raise
                time.sleep(.01*2**attempt)
    finally:
        temporary.unlink(missing_ok=True)


class NativeCatalog:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / 'state' / 'native-models.json'
        self.lock = threading.RLock()
        self.data = read_json(self.path) if self.path.exists() else {
            'schema': 1, 'root': str(self.home / 'models' / 'native'), 'models': []}
        if (self.data.get('schema') != 1 or not isinstance(self.data.get('models'), list)
                or len(self.data['models']) > 512):
            raise ValueError('The native model catalog needs recovery from its backup.')

    @property
    def root(self):
        return Path(self.data['root']).resolve()

    def configure(self, root):
        path = Path(root).expanduser()
        if not path.is_absolute():
            raise ValueError('Choose an absolute model directory.')
        with self.lock:
            if self.data['models'] and path.resolve() != self.root:
                raise ValueError('The model directory contains registered models. Migrate those models before changing it.')
            path.mkdir(parents=True, exist_ok=True)
            self.data['root'] = str(path.resolve())
            atomic_json(self.path, self.data)
        return self.public()

    def public(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def model(self, identifier):
        with self.lock:
            found = next((m for m in self.data['models'] if m['id'] == identifier), None)
            if found is None:
                raise ValueError('Choose an installed Forge model.')
            result = json.loads(json.dumps(found))
        for entry in result['files'] + result.get('auxiliary', []):
            path = self.root / entry['path']
            if not path.resolve().is_relative_to(self.root) or not path.is_file():
                raise ValueError('A Forge model file is missing or outside its model directory.')
            stat = path.stat()
            if stat.st_size != entry['size'] or stat.st_mtime_ns != entry['mtime_ns']:
                raise ValueError('A Forge model changed after verification. Import it again before use.')
        return result

    def binding(self):
        with self.lock:
            fields=('id','engine','files','auxiliary','projector','architecture','context','capabilities','reasoning_control')
            models=[{**{key:model.get(key) for key in fields},'defaults':model.get('origin',{}).get('defaults',{})}
                    for model in self.data['models']]
            return hashlib.sha256(json.dumps({'root':self.data['root'],'models':models},sort_keys=True).encode()).hexdigest()

    @staticmethod
    def inspect(path):
        from forge_vendor.strata.gguf_reader import GGUFFile
        reader = GGUFFile(Path(path))
        md = reader.metadata
        architecture = md.get('general.architecture')
        if not architecture or not reader.tensors:
            raise ValueError('The GGUF is missing its architecture or weights.')
        template = md.get('tokenizer.chat_template', '')
        context = md.get(architecture + '.context_length', 8192)
        return {'architecture': architecture, 'name': md.get('general.name', Path(path).stem),
                'context': context if type(context) is int and context >= 2048 else 8192,
                'template': template if isinstance(template, str) else '',
                'split_count': md.get('split.count', 1),
                'capabilities': ['completion'] + (['tools'] if isinstance(template, str) and 'tools' in template else [])
                    + (['thinking'] if isinstance(template, str) and ('enable_thinking' in template or '<think>' in template) else []),
                'reasoning_control': isinstance(template, str) and 'enable_thinking' in template}

    def add(self, sources, *, name=None, engine='llama-cpp', expected=None, origin=None, identifier=None, publish=True):
        if engine not in ('llama-cpp', 'strata') or not 1 <= len(sources) <= 128:
            raise ValueError('Unsupported native model import.')
        sources = [Path(path).resolve(strict=True) for path in sources]
        metadata = self.inspect(sources[0])
        if metadata['split_count'] != len(sources):
            raise ValueError('Import every matching GGUF shard together.')
        identifier = identifier or 'model-' + uuid4().hex
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', identifier):
            raise ValueError('Invalid native model identity.')
        root = self.root
        directory = root / identifier
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError('The model import directory is outside Forge storage.')
        files = []
        for index, source in enumerate(sources):
            if not source.is_file():
                raise ValueError('Choose a GGUF file.')
            before = source.stat()
            digest = file_hash(source)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('The model changed during import.')
            if expected and (digest != expected[index]['sha256'] or after.st_size != expected[index]['size']):
                raise ValueError('The model does not match its pinned download.')
            filename = source.name if source.suffix.lower() == '.gguf' else 'model.gguf'
            target = directory / filename
            if target.is_symlink() or not target.resolve().is_relative_to(root):
                raise ValueError('The model import target is outside Forge storage.')
            if target.exists():
                if file_hash(target) != digest:
                    raise ValueError('A different model already occupies this location.')
            else:
                try:
                    os.link(source, target)
                except OSError:
                    part = target.with_suffix('.gguf.partial')
                    with source.open('rb') as inp, part.open('xb') as out:
                        shutil.copyfileobj(inp, out, 1024**2)
                    if file_hash(part) != digest:
                        raise ValueError('Model copy did not pass verification.')
                    part.replace(target)
            stat = target.stat()
            files.append({'path': target.relative_to(root).as_posix(), 'sha256': digest,
                          'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
        # Keep original defaults and provenance private, beside the owned catalog.
        record = {'id': identifier, 'name': str(name or metadata['name'])[:300], 'engine': engine,
                  'files': files, 'architecture': metadata['architecture'], 'context': metadata['context'],
                  'capabilities': metadata['capabilities'], 'reasoning_control':metadata['reasoning_control'],
                  'validation': 'not_run', 'origin': origin or {}}
        if publish:
            self.publish(record)
        return record

    def publish(self, record):
        with self.lock:
            self.data['models'] = [m for m in self.data['models'] if m['id'] != record['id']] + [record]
            atomic_json(self.path, self.data)

    def ollama_inventory(self, root):
        root = Path(root).resolve(strict=True)
        manifests = root / 'manifests'
        result = []
        for path in sorted(manifests.rglob('*')):
            if not path.is_file():
                continue
            if path.suffix.lower()=='.gguf' and path.resolve().is_relative_to(manifests.resolve()):
                with path.open('rb') as stream: magic=stream.read(4)
                if magic==b'GGUF':
                    relative=path.relative_to(manifests).as_posix()
                    result.append({'id':hashlib.sha256(relative.encode()).hexdigest()[:24],
                        'name':path.stem,'size':path.stat().st_size,'manifest':relative,'has_adapter':False,'kind':'gguf'})
                continue
            if path.stat().st_size > 2 * 1024**2:
                continue  # A GGUF accidentally saved among manifests is not JSON.
            try:
                if not path.resolve().is_relative_to(manifests.resolve()):
                    continue
                manifest = read_json(path)
                layers = manifest.get('layers', [])
                weights = [layer for layer in layers if layer.get('mediaType') == 'application/vnd.ollama.image.model']
                if len(weights) != 1:
                    continue
                for layer in layers:
                    if not re.fullmatch(r'sha256:[0-9a-f]{64}', layer.get('digest', '')):
                        raise ValueError('Invalid Ollama content identity.')
                relative = path.relative_to(manifests).as_posix()
                result.append({'id': hashlib.sha256(relative.encode()).hexdigest()[:24], 'name': relative,
                    'size': weights[0]['size'], 'manifest': relative,
                    'has_adapter': any('adapter' in layer.get('mediaType', '') for layer in layers)})
            except (ValueError, OSError, TypeError, KeyError):
                continue
        return result

    def import_ollama(self, root, identifier):
        root = Path(root).resolve(strict=True)
        found = next((m for m in self.ollama_inventory(root) if m['id'] == identifier), None)
        if not found or found['has_adapter']:
            raise ValueError('Select a supported Ollama model without an external adapter.')
        if found.get('kind')=='gguf':
            return self.add([root/'manifests'/found['manifest']],name=found['name'],identifier='ollama-'+identifier,
                origin={'kind':'local_gguf','review':'GGUF discovered in the Ollama directory; original retained.'})
        manifest = read_json(root / 'manifests' / found['manifest'])
        sources, expected, origin = [], [], {'kind': 'ollama', 'manifest': found['manifest'], 'defaults': {}}
        projectors = []
        for layer in manifest['layers']:
            blob = root / 'blobs' / layer['digest'].replace(':', '-')
            if not blob.resolve().is_relative_to(root) or not blob.is_file():
                raise ValueError('An Ollama model layer is missing.')
            kind = layer['mediaType'].rsplit('.', 1)[-1]
            if kind == 'model':
                sources.append(blob)
                expected.append({'sha256': layer['digest'][7:], 'size': layer['size']})
            elif kind in ('template', 'system', 'params', 'license'):
                if blob.stat().st_size > 1024**2 or file_hash(blob) != layer['digest'][7:]:
                    raise ValueError('An Ollama default failed integrity validation.')
                text = blob.read_text(encoding='utf-8')
                origin['defaults'][kind] = json.loads(text) if kind == 'params' else text
            elif kind == 'projector':
                projectors.append({'source': str(blob), 'sha256': layer['digest'][7:], 'size': layer['size']})
        origin['projectors'] = projectors
        origin['review'] = 'Original defaults retained. Confirm template and vision behavior before removing the Ollama copy.'
        record = self.add(sources, name=found['name'], identifier='ollama-' + identifier,
                          expected=expected, origin=origin, publish=False)
        auxiliary = []
        for projector in projectors:
            source = Path(projector['source'])
            if source.stat().st_size != projector['size'] or file_hash(source) != projector['sha256']:
                raise ValueError('The vision projector failed verification. Ollama originals were retained.')
            target = self.root / record['id'] / ('projector-' + projector['sha256'][:16] + '.gguf')
            if target.is_symlink() or not target.resolve().is_relative_to(self.root):
                raise ValueError('The vision projector target is outside Forge storage.')
            if not target.exists():
                try: os.link(source, target)
                except OSError: shutil.copyfile(source, target)
            if file_hash(target) != projector['sha256']:
                raise ValueError('The copied vision projector failed verification.')
            stat = target.stat()
            auxiliary.append({'path': target.relative_to(self.root).as_posix(), 'size': stat.st_size,
                              'sha256': projector['sha256'], 'mtime_ns': stat.st_mtime_ns})
        if len(auxiliary) == 1:
            record['projector'] = auxiliary[0]['path']
            record['auxiliary'] = auxiliary
            record['capabilities'] = list(dict.fromkeys(record['capabilities'] + ['vision']))
        self.publish(record)
        return record
