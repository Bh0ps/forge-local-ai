"""Publisher package checks bind executable bytes to a pinned source identity."""
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from forge_update_proof import BUILD_NAME, RELEASE_NAME, key_id, tree_files
from scripts import release_build, release_proof


@pytest.fixture
def publisher_package(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    pins = {key_id(public): base64.b64encode(public).decode('ascii')}
    monkeypatch.setattr(release_build, 'TRUSTED_UPDATE_KEYS', pins)
    work = tmp_path / 'work'
    app = work / 'dist/Forge'
    app.mkdir(parents=True)
    for name in ('Forge.exe', 'ForgeBrowserHost.exe', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'third-party-licenses/manifest.json'):
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'Synthetic inert release fixture')
    source = 'a' * 40
    number = '5.1.2'
    release_proof.seal_app(app, number, source, private, pins)
    output = work / 'release'
    output.mkdir()
    (output / f'Forge-{number}-Setup.exe').write_bytes(b'Synthetic inert installer')
    args = SimpleNamespace(work=work, version=number, require_signed=False, require_publisher_proof=True)
    release_build.package(args)
    release_proof.seal_release(output, app, number, source, private, pins)
    return SimpleNamespace(work=output, app=app, version=number, source_commit=source,
                           channel='preview', require_publisher_proof=True, private=private, pins=pins)


def rebind_zip(package, rewrite):
    archive = package.work / f'Forge-{package.version}-Portable.zip'
    with zipfile.ZipFile(archive) as source:
        records = [(row, source.read(row)) for row in source.infolist()]
    with zipfile.ZipFile(archive, 'w') as target:
        rewrite(target, records)
    # A legitimate publisher can still make a packaging mistake; a valid release
    # signature must never make a malformed portable inventory acceptable.
    from forge_update_proof import strict_json
    proof = strict_json((package.work / RELEASE_NAME).read_bytes())
    payload = strict_json(base64.b64decode(proof['payload']))
    for item in payload['artifacts']:
        if item['name'] == archive.name:
            item.update(size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (package.work / RELEASE_NAME).write_bytes(release_proof.signed_record(payload, package.private, package.pins))
    names = [item['name'] for item in payload['artifacts']] + ['Forge-release.json', RELEASE_NAME]
    (package.work / 'SHA256SUMS.txt').write_text(''.join(hashlib.sha256((package.work / name).read_bytes()).hexdigest()
        + '  ' + name + '\n' for name in names), encoding='ascii', newline='\n')


def test_publisher_download_verifies_exact_tree_and_never_executes(publisher_package, monkeypatch):
    monkeypatch.setattr(release_build, 'require_trust', lambda *args: pytest.fail('Publisher path must not use a CA fallback'))
    monkeypatch.setattr(release_build, 'run', lambda *args: pytest.fail('Verification must not run executables'))
    release_build.verify_downloaded(publisher_package)
    assert tree_files(publisher_package.work / 'verified-portable') == tree_files(publisher_package.app)


def test_publisher_proof_is_required_before_extracting(publisher_package):
    (publisher_package.work / RELEASE_NAME).unlink()
    with pytest.raises(ValueError, match='unavailable'):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


def test_invalid_proof_never_falls_back_to_windows_trust(publisher_package, monkeypatch):
    (publisher_package.work / RELEASE_NAME).write_bytes(b'{}')
    monkeypatch.setattr(release_build, 'require_trust', lambda *args: pytest.fail('Invalid proof cannot become CA trusted'))
    with pytest.raises(ValueError):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


def test_explicit_windows_certificate_requirement_is_not_weakened_by_a_proof(publisher_package, monkeypatch):
    publisher_package.require_signed = True
    def reject(*args):
        raise ValueError('Synthetic missing Windows signature')
    monkeypatch.setattr(release_build, 'require_trust', reject)
    with pytest.raises(ValueError, match='Windows signature'):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


@pytest.mark.parametrize('mutation', ['source', 'setup', 'zip'])
def test_mismatched_release_bytes_are_rejected_before_extraction(publisher_package, mutation):
    if mutation == 'source':
        publisher_package.source_commit = 'b' * 40
    else:
        name = f'Forge-{publisher_package.version}-' + ('Setup.exe' if mutation == 'setup' else 'Portable.zip')
        with (publisher_package.work / name).open('ab') as output:
            output.write(b'Unexpected changed bytes')
    with pytest.raises(ValueError):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


@pytest.mark.parametrize('entry', ['../escape.exe', 'CON.txt', 'extra.js', 'forge.exe', 'directory/'])
def test_valid_release_signature_does_not_allow_unknown_zip_entries(publisher_package, entry):
    def rewrite(target, records):
        for row, data in records:
            target.writestr(row, data)
        target.writestr(entry, b'Unexpected synthetic entry')
    rebind_zip(publisher_package, rewrite)
    with pytest.raises(ValueError):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


def test_valid_release_signature_still_checks_each_file_hash(publisher_package):
    def rewrite(target, records):
        for row, data in records:
            target.writestr(row, b'X' * len(data) if row.filename == 'Forge.exe' else data)
    rebind_zip(publisher_package, rewrite)
    with pytest.raises(ValueError, match='bytes differ'):
        release_build.verify_downloaded(publisher_package)


def test_application_seal_rejects_leftover_or_changed_files(publisher_package):
    (publisher_package.app / 'old-chunk.js').write_text('synthetic stale output')
    with pytest.raises(ValueError, match='differ'):
        release_build.verify_app(publisher_package.app, publisher_package.version, False)


def test_checksum_disagreement_is_rejected_before_extraction(publisher_package):
    (publisher_package.work / 'SHA256SUMS.txt').write_text('0' * 64 + '  Forge-release.json\n')
    with pytest.raises(ValueError, match='checksum'):
        release_build.verify_downloaded(publisher_package)
    assert not (publisher_package.work / 'verified-portable').exists()


def test_fresh_package_cannot_require_a_missing_build_proof(publisher_package):
    (publisher_package.app / BUILD_NAME).unlink()
    with pytest.raises(ValueError, match='unavailable'):
        release_build.verify_app(publisher_package.app, publisher_package.version, False, require_proof=True)


def test_unsigned_legacy_validation_is_still_available(tmp_path):
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'third-party-licenses/manifest.json'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic notice')
    release_build.verify_app(tmp_path, '5.1.2', False)
