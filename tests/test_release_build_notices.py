"""The release validator consumes the real license collector's output contract."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import collect_licenses, release_build


def test_collected_notices_pass_application_release_validation(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'requirements-dev.txt').write_text('sample==1\n', encoding='utf-8')
    (source / 'requirements-integrations.txt').write_text('', encoding='utf-8')
    installed = tmp_path / 'installed'
    license_file = Path('sample.dist-info/licenses/LICENSE')
    (installed / license_file).parent.mkdir(parents=True)
    (installed / license_file).write_text('Copyright sample contributors. MIT license.', encoding='utf-8')
    distribution = SimpleNamespace(metadata={'Name': 'sample', 'License': 'MIT'},
        version='1', requires=[], files=[license_file], locate_file=lambda item: installed / item)
    monkeypatch.setattr(collect_licenses.importlib.metadata, 'distribution', lambda name: distribution)
    app = tmp_path / 'app'
    app.mkdir()
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        (app / name).write_text('Synthetic project notice', encoding='utf-8')
    assert collect_licenses.collect(app / 'third-party-licenses', source) == 1
    release_build.verify_app(app, '4.2.0', False)
    manifest = app / 'third-party-licenses/manifest.json'
    assert manifest.is_file()
    manifest.unlink()
    with pytest.raises(ValueError, match='third-party-licenses/manifest.json'):
        release_build.verify_app(app, '4.2.0', False)
