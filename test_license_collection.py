"""Release notices preserve copyrights without copying installed Python bytecode."""
from pathlib import Path
from types import SimpleNamespace

from scripts import collect_licenses


def test_notice_collection_excludes_code_and_private_bytecode(tmp_path, monkeypatch):
    package = tmp_path / 'installed'
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'requirements-dev.txt').write_text('sample==1\n', encoding='utf-8')
    (root / 'requirements-integrations.txt').write_text('', encoding='utf-8')
    texts = {
        'sample.dist-info/licenses/LICENSE.txt': 'Permission notice and copyright',
        'sample.dist-info/licenses/AUTHORS.rst': 'Upstream author <author@example.org>',
        'sample/licenses/__init__.py': '# installed implementation',
        'sample/licenses/__pycache__/__init__.cpython-312.pyc': 'private installation filename',
        'sample.dist-info/licenses/__pycache__/AUTHORS.cpython-312.pyc': 'private installation filename',
    }
    for name, text in texts.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    distribution = SimpleNamespace(metadata={'Name': 'sample', 'License': 'MIT'}, version='1', requires=[],
        files=[Path(name) for name in texts], locate_file=lambda item: package / item)
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution', lambda name: distribution)
    output = tmp_path / 'notices'
    assert collect_licenses.collect(output, root) == 1
    copied = list((output / 'sample').iterdir())
    assert len(copied) == 2
    assert all(path.suffix in ('.txt', '.rst') for path in copied)
    contents = '\n'.join(path.read_text(encoding='utf-8') for path in copied)
    assert 'Upstream author <author@example.org>' in contents
    assert 'Permission notice' in contents
    assert 'private installation filename' not in contents


def test_notice_cache_pruning_preserves_runtime_bytecode_and_license_text(tmp_path):
    from scripts.prune_license_cache import prune
    notice=tmp_path/'app'/'_internal'/'dependency.dist-info'/'licenses'
    cache=notice/'__pycache__';cache.mkdir(parents=True)
    (cache/'AUTHORS.cpython-312.pyc').write_bytes(b'generated build cache')
    (notice/'LICENSE').write_text('Dependency license',encoding='utf-8')
    runtime=tmp_path/'app'/'_internal'/'runtime'/'__pycache__';runtime.mkdir(parents=True)
    (runtime/'module.pyc').write_bytes(b'needed runtime')
    assert prune(tmp_path/'app')==1
    assert not cache.exists() and (notice/'LICENSE').read_text()=='Dependency license'
    assert (runtime/'module.pyc').read_bytes()==b'needed runtime'
