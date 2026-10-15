"""Synthetic bounded GGUF vectors, with no production models or workers."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

import forge_native_variants as variants


def vector(*, width=2560, tensor_type=0, layer_count=47, bad_offset=False, nonfinite=False):
    def string(value):
        data = value.encode('utf-8')
        return struct.pack('<Q', len(data)) + data
    data = bytearray(struct.pack('<4sIQQ', b'GGUF', 3, 47, 3))
    for key, value in (('general.architecture', 'controlvector'), ('controlvector.model_hint', 'qwen4exp'),
                       ('controlvector.layer_count', layer_count)):
        data += string(key) + struct.pack('<I', 4 if type(value) is int else 8)
        data += struct.pack('<I', value) if type(value) is int else string(value)
    for layer in range(1, 48):
        data += string('direction.' + str(layer))
        offset = (layer - 1) * 2560 * 4 + int(bad_offset and layer == 1)
        data += struct.pack('<IQIQ', 1, width, tensor_type, offset)
    data += bytes((-len(data)) % 32)
    data += bytes(47 * 2560 * 4)
    if nonfinite: data[-4:] = struct.pack('<f', float('nan'))
    return bytes(data)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'models'; root.mkdir()
    target = root / variants.VARIANT_ID / variants._FILENAME
    target.parent.mkdir(); target.write_bytes(vector())
    projection = {'repo': variants._SOURCE_REPO, 'revision': variants._SOURCE_REVISION,
                  'license': 'apache-2.0', 'file': {'path': variants._FILENAME, 'size': target.stat().st_size,
                  'sha256': hashlib.sha256(target.read_bytes()).hexdigest()},
                  'layers': [4, 44], 'scale': 1.0, 'mode': 'project', 'direction': 'per-layer'}
    shards = [{'path': 'IQ1_M/first.gguf', 'size': 29608446496, 'sha256': 'a' * 64},
              {'path': 'IQ1_M/second.gguf', 'size': 28800138432, 'sha256': 'b' * 64}]
    selected = {'id': variants.VARIANT_ID, 'engine': 'strata', 'files': shards, 'projection': projection}
    lock = {'schema': 1, 'models': [selected, {'id': 'strata-coder-iq1-m', 'engine': 'strata', 'files': shards}]}
    monkeypatch.setattr(variants, 'read_json', lambda path: deepcopy(lock))
    record = {**deepcopy(selected), 'projection': {**deepcopy(projection), 'file': {
        **projection['file'], 'path': target.relative_to(root).as_posix(), 'mtime_ns': target.stat().st_mtime_ns}}}
    return root, target, record, lock


def repin(installed, data):
    _, target, record, lock = installed
    target.write_bytes(data)
    for entry in (record['projection']['file'], lock['models'][0]['projection']['file']):
        entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    record['projection']['file']['mtime_ns'] = target.stat().st_mtime_ns


def test_verified_variant_constructs_only_fixed_cli_arguments(installed):
    root, target, record, _ = installed
    assert target.stat().st_size == 483520
    checks = []
    assert variants.projection_arguments(root, record, check=lambda: checks.append(True)) == [
        '--control-vector-scaled', str(target.resolve()) + ':1.0', '--control-vector-layer-range', '4', '44',
        '--cvec-mode', 'project', '--cvec-dir', 'per-layer']
    assert len(checks) == 3


def test_base_models_have_no_projection_and_do_not_need_files(installed):
    root, _, _, _ = installed
    for record in ({'id': 'strata-coder-iq1-m', 'engine': 'strata'}, {'id': 'imported-local', 'engine': 'llama-cpp'}):
        assert variants.projection_arguments(root / 'missing', record) == []
        assert variants.projection_binding(record) is None


def test_projection_binding_is_canonical_and_includes_catalog_stat(installed):
    _, _, record, _ = installed
    initial = variants.projection_binding(record)
    shuffled = {**record, 'projection': dict(reversed(list(record['projection'].items())))}
    assert variants.projection_binding(shuffled) == initial and len(initial) == 64
    record['projection']['file']['mtime_ns'] += 1
    assert variants.projection_binding(record) != initial


@pytest.mark.parametrize('failure', ['base-injection', 'unknown-injection', 'wrong-engine', 'missing-projection',
    'missing-shard', 'changed-shard', 'reordered-shards', 'extra-flags', 'wrong-repo', 'wrong-revision',
    'wrong-license', 'wrong-mode', 'wrong-range', 'fractional-layer', 'boolean-scale', 'extra-file-selector'])
def test_catalog_cannot_inject_unpinned_projection_or_checkpoint(installed, failure):
    root, _, record, _ = installed
    if failure == 'base-injection': record['id'] = 'strata-coder-iq1-m'
    elif failure == 'unknown-injection': record['id'] = 'unregistered'
    elif failure == 'wrong-engine': record['engine'] = 'llama-cpp'
    elif failure == 'missing-projection': del record['projection']
    elif failure == 'missing-shard': record['files'].pop()
    elif failure == 'changed-shard': record['files'][0]['sha256'] = 'f' * 64
    elif failure == 'reordered-shards': record['files'].reverse()
    elif failure == 'extra-flags': record['projection']['flags'] = ['--unreviewed']
    elif failure == 'wrong-repo': record['projection']['repo'] = 'untrusted/model'
    elif failure == 'wrong-revision': record['projection']['revision'] = 'f' * 40
    elif failure == 'wrong-license': record['projection']['license'] = 'unverified'
    elif failure == 'wrong-mode': record['projection']['mode'] = 'add'
    elif failure == 'wrong-range': record['projection']['layers'] = [0, 99]
    elif failure == 'fractional-layer': record['projection']['layers'] = [4, 44.0]
    elif failure == 'boolean-scale': record['projection']['scale'] = True
    elif failure == 'extra-file-selector': record['projection']['file']['selector'] = 'other.gguf'
    for operation in (lambda: variants.projection_arguments(root, record), lambda: variants.projection_binding(record)):
        with pytest.raises(ValueError): operation()


@pytest.mark.parametrize('relative', ['../outside.gguf', '/tmp/outside.gguf', 'C:/outside.gguf',
    r'\\server\share\outside.gguf', 'strata-coder-uncensored-iq1-m/../outside.gguf',
    'strata-coder-uncensored-iq1-m//other.gguf', 'strata-coder-uncensored-iq1-m/./other.gguf',
    'strata-coder-uncensored-iq1-m/other.gguf:stream'])
def test_projection_paths_cannot_escape_or_select_another_file(installed, relative):
    root, _, record, _ = installed
    record['projection']['file']['path'] = relative
    with pytest.raises(ValueError): variants.projection_arguments(root, record)


def test_projection_content_tampering_is_detected_even_with_original_stat(installed):
    root, target, record, _ = installed
    before = target.stat()
    changed = bytearray(target.read_bytes()); changed[-1] ^= 1
    target.write_bytes(changed)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match='integrity'): variants.projection_arguments(root, record)


@pytest.mark.parametrize('change', ['missing', 'mtime', 'size'])
def test_projection_requires_existing_unchanged_catalog_file(installed, change):
    root, target, record, _ = installed
    if change == 'missing': target.unlink()
    elif change == 'mtime': record['projection']['file']['mtime_ns'] += 1
    else: target.write_bytes(target.read_bytes() + b'x')
    with pytest.raises(ValueError): variants.projection_arguments(root, record)


@pytest.mark.parametrize('location', ['root', 'directory', 'file'])
def test_reparse_components_are_rejected_before_opening(installed, monkeypatch, location):
    root, target, record, _ = installed
    selected = {'root': root, 'directory': target.parent, 'file': target}[location]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        return actual
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='plain Forge model storage'):
        variants.projection_arguments(root, record)


def test_symlinked_projection_directory_is_not_followed(installed, tmp_path):
    root, target, record, _ = installed
    outside = tmp_path / 'outside'; target.parent.rename(outside)
    try: target.parent.symlink_to(outside, target_is_directory=True)
    except OSError: pytest.skip('This Windows session cannot create directory symlinks.')
    with pytest.raises(ValueError, match='plain Forge model storage'):
        variants.projection_arguments(root, record)


@pytest.mark.parametrize('data', [b'not a GGUF', vector(width=128), vector(tensor_type=1),
    vector(layer_count=48), vector(bad_offset=True), vector(nonfinite=True), vector()[:-4]],
    ids=['bad-magic', 'wrong-width', 'wrong-type', 'wrong-layer-count', 'wrong-offset', 'nonfinite', 'truncated'])
def test_projection_header_and_tensor_geometry_are_checked_independently_of_hash(installed, data):
    root, _, record, _ = installed
    repin(installed, data)
    with pytest.raises(ValueError): variants.projection_arguments(root, record)


def test_cancellation_is_propagated_without_reading_projection(installed, monkeypatch):
    root, _, record, _ = installed
    def stopped(): raise RuntimeError('Synthetic cancelled model opening')
    monkeypatch.setattr(variants, '_path', lambda *_: pytest.fail('Cancelled work opened a projection.'))
    with pytest.raises(RuntimeError, match='cancelled'):
        variants.projection_arguments(root, record, check=stopped)


def test_file_change_during_verification_cannot_reach_worker_arguments(installed):
    root, target, record, _ = installed
    checks = []
    def check():
        checks.append(True)
        if len(checks) == 2: target.write_bytes(b'changed during verification')
    with pytest.raises(ValueError, match='integrity'):
        variants.projection_arguments(root, record, check=check)


def test_replacement_after_payload_validation_cannot_reach_worker_arguments(installed):
    root, target, record, _ = installed
    checks = []
    def check():
        checks.append(True)
        if len(checks) == 3: target.write_bytes(b'late replacement')
    with pytest.raises(ValueError, match='changed during verification'):
        variants.projection_arguments(root, record, check=check)


def test_unreviewed_lock_recipe_is_rejected_even_when_catalog_matches(installed):
    root, _, record, lock = installed
    for projection in (record['projection'], lock['models'][0]['projection']):
        projection['mode'] = 'unreviewed'
    with pytest.raises(ValueError, match='supported pinned recipe'):
        variants.projection_arguments(root, record)
