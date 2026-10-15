"""Remove PyInstaller's duplicate CUDA aliases from a fresh release stage.

The workers use the authenticated native-runtimes/shared directory on their
private PATH. Dictation uses CPU INT8 and does not load CUDA. Keep each pinned
shared library, and remove a root alias only after checking both copies.
"""
import json
from pathlib import Path

from forge_updates import file_hash, plain_path
from forge_update_proof import BUILD_NAME

CUDA_LIBRARIES = ('cublas64_13.dll', 'cublasLt64_13.dll', 'cudart64_13.dll')


def prune(app):
    app = plain_path(app)
    work = app.parent.parent
    if (app.name != 'Forge' or app.parent.name != 'dist'
            or not (work / 'source/version-info.py').is_file()
            or (app / BUILD_NAME).exists()):
        raise ValueError('Prune only a fresh, unsealed release build.')
    internal = plain_path(app / '_internal')
    manifest = json.loads((internal / 'native-runtimes/manifest.json').read_text(encoding='utf-8'))
    pending = []
    for name in CUDA_LIBRARIES:
        alias = plain_path(internal / name)
        if not alias.exists():
            continue
        shared = plain_path(internal / 'native-runtimes/shared' / name)
        expected = manifest['files'].get('shared/' + name)
        if not expected or not alias.is_file() or not shared.is_file():
            raise ValueError('A CUDA alias has no pinned shared library.')
        if any(path.stat().st_size != expected['size'] or file_hash(path) != expected['sha256']
               for path in (alias, shared)):
            raise ValueError('CUDA alias differs from the pinned shared library.')
        pending.append(alias)
    # Validate every candidate before altering this new build directory.
    removed_bytes = sum(path.stat().st_size for path in pending)
    for path in pending:
        path.unlink()
    return {'duplicate_files_removed': len(pending), 'duplicate_bytes_removed': removed_bytes}
