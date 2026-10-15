import hashlib
import json
from pathlib import Path

import pytest

from forge_update_proof import BUILD_NAME
from scripts.prune_native_runtime_aliases import prune


def bundle(tmp_path):
    app = tmp_path / 'work/dist/Forge'
    shared = app / '_internal/native-runtimes/shared'
    shared.mkdir(parents=True)
    stage = tmp_path / 'work/source'
    stage.mkdir()
    (stage / 'version-info.py').write_text('synthetic version')
    data = b'synthetic pinned CUDA bytes'
    (shared / 'cublasLt64_13.dll').write_bytes(data)
    alias = app / '_internal/cublasLt64_13.dll'
    alias.write_bytes(data)
    manifest = {'files': {'shared/cublasLt64_13.dll': {
        'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
    (shared.parent / 'manifest.json').write_text(json.dumps(manifest))
    return app, shared, alias, data


def test_duplicate_alias_removed_and_pinned_worker_library_preserved(tmp_path):
    app, shared, alias, data = bundle(tmp_path)
    unrelated = app / '_internal/other.dll'
    unrelated.write_bytes(data)
    assert prune(app) == {'duplicate_files_removed': 1, 'duplicate_bytes_removed': len(data)}
    assert not alias.exists()
    assert (shared / alias.name).read_bytes() == data and unrelated.read_bytes() == data
    assert prune(app)['duplicate_files_removed'] == 0


@pytest.mark.parametrize('changed', ['alias', 'shared', 'missing_shared', 'unlisted'])
def test_no_removal_if_either_copy_is_not_pinned(tmp_path, changed):
    app, shared, alias, data = bundle(tmp_path)
    if changed == 'alias': alias.write_bytes(b'wrong')
    if changed == 'shared': (shared / alias.name).write_bytes(b'wrong')
    if changed == 'missing_shared': (shared / alias.name).unlink()
    if changed == 'unlisted': (shared.parent / 'manifest.json').write_text('{"files":{}}')
    with pytest.raises(ValueError): prune(app)
    assert alias.exists()


def test_pruning_refuses_installed_or_sealed_trees(tmp_path):
    app, _, alias, _ = bundle(tmp_path)
    (app / BUILD_NAME).write_text('synthetic proof')
    with pytest.raises(ValueError, match='fresh, unsealed'): prune(app)
    assert alias.exists()
    with pytest.raises(ValueError, match='fresh, unsealed'): prune(tmp_path)
