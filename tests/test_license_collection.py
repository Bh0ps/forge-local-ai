"""Release notices preserve copyrights without copying installed Python bytecode."""
from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
import json
import shutil

import pytest

from scripts import collect_licenses


def fake_distribution(name, version, installed, files=(), metadata=None):
    return SimpleNamespace(metadata=metadata or {'Name': name, 'License': 'MIT'}, version=version,
        requires=[], files=[Path(item) for item in files], locate_file=lambda item: installed / item)


def fallback_source(tmp_path, requirement):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'requirements-dev.txt').write_text(requirement + '\n', encoding='utf-8')
    (root / 'requirements-integrations.txt').write_text('', encoding='utf-8')
    shutil.copytree(Path(__file__).resolve().parents[1] / 'assets/third-party-licenses', root / 'assets/third-party-licenses')
    return root


@pytest.mark.parametrize(('name', 'version', 'count'), [
    ('ctranslate2', '4.8.2', 1), ('flatbuffers', '25.12.19', 1),
    ('openpyxl', '3.1.5', 2), ('primp', '2.0.1', 6),
    ('proxy_tools', '0.1.0', 2), ('tokenizers', '0.23.2', 1),
])
def test_exact_version_full_upstream_notices_are_collected_offline(tmp_path, monkeypatch, name, version, count):
    root = fallback_source(tmp_path, f'{name}=={version}')
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution(name, version, tmp_path))
    output = tmp_path / 'notices'
    assert collect_licenses.collect(output, root) == 1
    record = json.loads((output / 'manifest.json').read_text())[0]
    assert record['notice_source'] == 'pinned-upstream-fallback'
    assert len(record['files']) == count
    assert 'LICENSE-METADATA.txt' not in record['files']
    for file in record['file_records']:
        data = (output / name / file['file']).read_bytes()
        assert sha256(data).hexdigest() == file['sha256']
        assert file['provenance']['url'].startswith('https://')
    if name == 'proxy_tools':
        assert record['metadata_license'] == 'MIT'
        assert 'two redistribution conditions' in record['license']
        license_text = (output / name / 'LICENSE.txt').read_text()
        assert sum(line.lstrip().startswith('* Redistributions') for line in license_text.splitlines()) == 2
        assert 'Neither the name' not in license_text
        assert 'Armin Ronacher' in (output / name / 'LICENSE.txt').read_text()
        assert 'byte for byte' in (output / name / 'ATTRIBUTION.txt').read_text()
    if name == 'primp':
        assert record['license'] == 'MIT; bundled rustls is Apache-2.0 OR MIT OR ISC'
        assert 'at your option' in (output / name / 'crates__primp-rustls__rustls__LICENSE').read_text()


def test_installed_british_license_precedes_fallback_and_removes_old_placeholder(tmp_path, monkeypatch):
    root = fallback_source(tmp_path, 'openpyxl==9.0')
    installed = tmp_path / 'installed'
    member = Path('openpyxl-9.0.dist-info/LICENCE.rst')
    (installed / member).parent.mkdir(parents=True)
    (installed / member).write_text('Copyright upstream. Complete installed license.', encoding='utf-8')
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution('openpyxl', '9.0', installed, [member]))
    output = tmp_path / 'notices'
    (output / 'openpyxl').mkdir(parents=True)
    (output / 'openpyxl/LICENSE-METADATA.txt').write_text('MIT', encoding='utf-8')
    assert collect_licenses.collect(output, root) == 1
    record = json.loads((output / 'manifest.json').read_text())[0]
    assert record['notice_source'] == 'installed-distribution'
    assert record['files'] == ['openpyxl-9.0.dist-info__LICENCE.rst']
    assert not (output / 'openpyxl/LICENSE-METADATA.txt').exists()


def test_declared_custom_license_filename_is_discovered(tmp_path, monkeypatch):
    from email.message import Message
    root = fallback_source(tmp_path, 'sample==1')
    installed = tmp_path / 'installed'
    member = Path('sample.dist-info/terms.txt')
    (installed / member).parent.mkdir(parents=True)
    (installed / member).write_text('Copyright upstream. Full terms.', encoding='utf-8')
    metadata = Message()
    metadata['Name'] = 'sample'
    metadata['License-File'] = 'terms.txt'
    metadata['License'] = 'MIT'
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution('sample', '1', installed, [member], metadata))
    assert collect_licenses.collect(tmp_path / 'notices', root) == 1


@pytest.mark.parametrize('version', ['4.8.3', '4.8.2'])
def test_unknown_version_or_missing_manifest_fails_instead_of_placeholder(tmp_path, monkeypatch, version):
    root = fallback_source(tmp_path, f'ctranslate2=={version}')
    if version == '4.8.2':
        (root / 'assets/third-party-licenses/manifest.json').unlink()
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution('ctranslate2', version, tmp_path))
    with pytest.raises(ValueError, match='Full upstream notices required'):
        collect_licenses.collect(tmp_path / 'notices', root)
    assert not list((tmp_path / 'notices').rglob('LICENSE-METADATA.txt'))
    assert not (tmp_path / 'notices/manifest.json').exists()


@pytest.mark.parametrize('fault', ['hash', 'missing', 'traversal', 'absolute', 'drive', 'duplicate', 'provenance'])
def test_pinned_notice_corruption_and_unsafe_paths_fail(tmp_path, fault):
    root = fallback_source(tmp_path, 'ctranslate2==4.8.2')
    assets = root / 'assets/third-party-licenses'
    manifest = json.loads((assets / 'manifest.json').read_text())
    entry = next(x for x in manifest['distributions'] if x['name'] == 'ctranslate2')
    if fault == 'hash':
        (assets / entry['files'][0]['path']).write_text('MIT', encoding='utf-8')
    elif fault == 'missing':
        (assets / entry['files'][0]['path']).unlink()
    elif fault == 'traversal':
        entry['files'][0]['path'] = '../LICENSE'
    elif fault == 'absolute':
        entry['files'][0]['path'] = '/LICENSE'
    elif fault == 'drive':
        entry['files'][0]['path'] = 'C:\\LICENSE'
    elif fault == 'duplicate':
        entry['files'].append(entry['files'][0].copy())
    elif fault == 'provenance':
        entry['files'][0]['provenance'] = {}
    (assets / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        collect_licenses.pinned_notices(root, 'ctranslate2', '4.8.2')


def test_installed_binary_disguised_as_license_cannot_enter_package(tmp_path, monkeypatch):
    root = fallback_source(tmp_path, 'sample==1')
    member = Path('sample.dist-info/LICENSE')
    (tmp_path / member).parent.mkdir()
    (tmp_path / member).write_bytes(b'MZ\0executable')
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution('sample', '1', tmp_path, [member]))
    with pytest.raises(ValueError, match='Binary dependency notice'):
        collect_licenses.collect(tmp_path / 'notices', root)


def test_colliding_vendor_notice_names_preserve_all_original_bytes(tmp_path, monkeypatch):
    root = fallback_source(tmp_path, 'sample==1')
    members = ['sample/vendor-a/lib/licenses/LICENSE', 'sample/vendor-b/lib/licenses/LICENSE']
    for index, member in enumerate(members):
        path = tmp_path / member
        path.parent.mkdir(parents=True)
        path.write_text(f'Copyright vendor {index}. Full license text.', encoding='utf-8')
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution',
                        lambda _: fake_distribution('sample', '1', tmp_path, members))
    output = tmp_path / 'notices'
    assert collect_licenses.collect(output, root) == 1
    record = json.loads((output / 'manifest.json').read_text())[0]
    assert len(record['files']) == len(set(record['files'])) == 2
    texts = [(output / 'sample' / name).read_text() for name in record['files']]
    assert any('vendor 0' in text for text in texts) and any('vendor 1' in text for text in texts)


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
