"""Pinned optional Strata control vectors; catalog metadata is never CLI input."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import struct

from forge_native_models import read_json


VARIANT_ID = 'strata-coder-uncensored-iq1-m'
_SOURCE_REPO = 'alesha-pro/Qwen3.8-Flash-Next-abliterated-GSQ-RCO-Coder-Strata-GGUF'
_SOURCE_REVISION = 'b8186202470558c2a7901327b135dd3482d2186b'
_FILENAME = 'Huihui-Qwen3.8-Flash-Next-refusal-direction-r.gguf'
_PROJECTION_KEYS = {'repo', 'revision', 'license', 'file', 'layers', 'scale', 'mode', 'direction'}
_RECIPE = {'layers': [4, 44], 'scale': 1.0, 'mode': 'project', 'direction': 'per-layer'}
_MAXIMUM_BYTES = 512 * 1024
_LAYERS, _WIDTH = 47, 2560


def _projection(model):
    if type(model) is not dict:
        raise ValueError('Expected a native model record.')
    projection = model.get('projection')
    if projection is None and model.get('id') != VARIANT_ID:
        return None
    lock = read_json(Path(__file__).parent / 'native-models.lock.json')
    if type(lock) is not dict or lock.get('schema') != 1 or type(lock.get('models')) is not list:
        raise ValueError('The native model lock metadata is invalid.')
    selected = [entry for entry in lock.get('models', []) if entry.get('id') == model.get('id')]
    if len(selected) != 1 or not selected[0].get('projection'):
        raise ValueError('This model does not have a pinned projection.')
    selected = selected[0]
    expected = selected['projection']
    if (model.get('id') != VARIANT_ID or model.get('engine') != 'strata' or selected.get('engine') != 'strata'
            or type(expected) is not dict or set(expected) != _PROJECTION_KEYS
            or expected.get('repo') != _SOURCE_REPO or expected.get('revision') != _SOURCE_REVISION
            or type(expected.get('license')) is not str or not 1 <= len(expected['license']) <= 100
            or any(expected.get(key) != value for key, value in _RECIPE.items())
            or type(expected.get('scale')) is not float or type(expected.get('layers')) is not list
            or any(type(layer) is not int for layer in expected['layers'])):
        raise ValueError('The model projection does not match the supported pinned recipe.')
    source = expected.get('file')
    if (type(source) is not dict or set(source) != {'path', 'size', 'sha256'}
            or source.get('path') != _FILENAME or type(source.get('size')) is not int
            or not 0 < source['size'] <= _MAXIMUM_BYTES
            or type(source.get('sha256')) is not str or not re.fullmatch('[0-9a-f]{64}', source['sha256'])):
        raise ValueError('The pinned projection file metadata is invalid.')
    if type(projection) is not dict or set(projection) != _PROJECTION_KEYS:
        raise ValueError('Complete the pinned model setup, including its projection.')
    if any(projection.get(key) != expected[key] or type(projection.get(key)) is not type(expected[key])
           for key in _PROJECTION_KEYS - {'file'}) or any(type(layer) is not int for layer in projection['layers']):
        raise ValueError('The model projection metadata differs from its pinned recipe.')
    entry = projection.get('file')
    if (type(entry) is not dict or set(entry) != {'path', 'size', 'sha256', 'mtime_ns'}
            or entry.get('path') != VARIANT_ID + '/' + _FILENAME
            or any(entry.get(key) != source[key] or type(entry.get(key)) is not type(source[key])
                   for key in ('size', 'sha256'))
            or type(entry.get('mtime_ns')) is not int or entry['mtime_ns'] < 0):
        raise ValueError('The model projection file differs from its pinned identity.')
    files, wanted = model.get('files'), selected.get('files')
    if (type(files) is not list or type(wanted) is not list or len(files) != 2 or len(wanted) != 2
            or any(type(actual) is not dict or type(pinned) is not dict
                   or any(actual.get(key) != pinned.get(key) or type(actual.get(key)) is not type(pinned.get(key))
                          for key in ('size', 'sha256')) for actual, pinned in zip(files, wanted))):
        raise ValueError('The model projection requires its exact pinned Strata checkpoint.')
    return projection


def projection_binding(model):
    """Bind the optional recipe, source hash and catalog stat to worker reuse."""
    projection = _projection(model)
    if projection is None:
        return None
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _plain(path):
    """Reject reparse points in every component, including the model root."""
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('The projection must stay inside plain Forge model storage.')


def _path(root, relative):
    if (type(relative) is not str or not relative or '\\' in relative or ':' in relative or '\0' in relative
            or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute()
            or any(part in ('', '.', '..') for part in relative.split('/'))):
        raise ValueError('The projection needs a relative path inside Forge model storage.')
    root = Path(root)
    if not root.is_absolute():
        raise ValueError('The Forge model root must be absolute.')
    _plain(root)
    path = root / relative
    _plain(path)
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError('The projection is outside Forge model storage.')
    return path


class _Reader:
    def __init__(self, data):
        self.data, self.position = data, 0

    def unpack(self, shape):
        size = struct.calcsize(shape)
        if self.position + size > len(self.data):
            raise ValueError('The projection GGUF header is truncated.')
        result = struct.unpack_from(shape, self.data, self.position)
        self.position += size
        return result

    def string(self):
        length, = self.unpack('<Q')
        if length > 256 or self.position + length > len(self.data):
            raise ValueError('The projection GGUF string exceeds its bounds.')
        value = self.data[self.position:self.position + length].decode('utf-8')
        self.position += length
        return value


def _validate_gguf(data):
    reader = _Reader(data)
    if reader.unpack('<4sIQQ') != (b'GGUF', 3, _LAYERS, 3):
        raise ValueError('The projection needs the pinned control-vector GGUF layout.')
    metadata = {}
    for _ in range(3):
        key = reader.string()
        kind, = reader.unpack('<I')
        if key in metadata or kind not in (4, 8, 10):
            raise ValueError('The projection GGUF metadata is invalid.')
        metadata[key] = reader.string() if kind == 8 else reader.unpack('<I' if kind == 4 else '<Q')[0]
    if metadata != {'general.architecture': 'controlvector', 'controlvector.model_hint': 'qwen4exp',
                    'controlvector.layer_count': _LAYERS}:
        raise ValueError('The projection is incompatible with this Strata checkpoint.')
    for layer in range(1, _LAYERS + 1):
        if (reader.string(), reader.unpack('<IQIQ')) != (
                'direction.' + str(layer), (1, _WIDTH, 0, (layer - 1) * _WIDTH * 4)):
            raise ValueError('The projection tensor layout is incompatible.')
    start = (reader.position + 31) // 32 * 32
    if start + _LAYERS * _WIDTH * 4 != len(data):
        raise ValueError('The projection tensor payload is truncated or oversized.')
    if any(not math.isfinite(value[0]) for value in struct.iter_unpack('<f', data[start:])):
        raise ValueError('The projection tensor contains a non-finite value.')


def projection_arguments(root, model, *, check=None):
    """Return only the reviewed fixed CLI recipe after full file verification."""
    projection = _projection(model)
    if projection is None:
        return []
    if check: check()
    entry = projection['file']
    try:
        path = _path(root, entry['path'])
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size != entry['size']
                or before.st_mtime_ns != entry['mtime_ns']):
            raise ValueError('The projection changed after verification. Complete its setup again.')
        with path.open('rb') as stream:
            stat_tuple = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            current = os.fstat(stream.fileno())
            if stat_tuple != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise ValueError('The projection changed during verification.')
            data = stream.read(entry['size'] + 1)
            after = os.fstat(stream.fileno())
        if check: check()
        _plain(path)
        final = path.lstat()
        if (any((item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) != stat_tuple for item in (after, final))
                or len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']):
            raise ValueError('The projection failed its pinned integrity check.')
        _validate_gguf(data)
        if check: check()
        # Parsing and cancellation checks cannot authorize a later replacement.
        resolved = _path(root, entry['path']).resolve(strict=True)
        final = resolved.lstat()
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != stat_tuple:
            raise ValueError('The projection changed during verification.')
    except OSError:
        raise ValueError('The pinned projection is missing or unavailable in Forge model storage.') from None
    return ['--control-vector-scaled', str(resolved) + ':1.0',
            '--control-vector-layer-range', '4', '44', '--cvec-mode', 'project', '--cvec-dir', 'per-layer']
