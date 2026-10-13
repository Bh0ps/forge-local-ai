"""Publisher CLI tests use inert packages, ephemeral keys and an in-memory vault."""
import base64
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
import pytest

import forge_update_proof as proof
from scripts import release_proof as tool


NUMBER, SOURCE, NOW = '5.1.2', 'a7' * 20, 1800000000


@pytest.fixture
def fixture(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    pins = {proof.key_id(public): public}
    app = tmp_path / 'work/dist/Forge'
    app.mkdir(parents=True)
    files = {'Forge.exe': b'Inert host fixture', 'ForgeBrowserHost.exe': b'Inert browser fixture',
             'LICENSE': b'Synthetic license notice', 'THIRD_PARTY_NOTICES.md': b'Synthetic third party notice',
             'third-party-licenses/manifest.json': b'{"synthetic":true}',
             '_internal/frontend/assets/current.js': b'Synthetic frontend'}
    for name, data in files.items():
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    release = tmp_path / 'work/release'
    release.mkdir()
    record = tmp_path / 'private-public-publisher-record.json'
    record.write_bytes(proof.canonical(dict(schema=1, namespace='ForgeRelease', credential_ref='fixture-ref',
        public_key_id=proof.key_id(public), public_key=base64.b64encode(public).decode('ascii'))))
    credential = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode('ascii')
    calls = []
    class FakeVault:
        def __init__(self, namespace):
            calls.append(('namespace', namespace))
        def get(self, reference):
            calls.append(('get', reference))
            return credential
    return SimpleNamespace(private=private, pins=pins, app=app, release=release, record=record,
                           vault=FakeVault, calls=calls, work=tmp_path / 'work')


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def seal_app(value):
    return tool.seal_app(value.app, NUMBER, SOURCE, value.private, value.pins, now=NOW)


def write_package(value, *, rows=None, manifest_change=None):
    if not (value.app / proof.BUILD_NAME).exists(): seal_app(value)
    setup = value.release / f'Forge-{NUMBER}-Setup.exe'
    setup.write_bytes(b'Inert synthetic installer, never executed')
    archive = value.release / f'Forge-{NUMBER}-Portable.zip'
    inventory = proof.tree_files(value.app)
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in rows if rows is not None else inventory:
            if isinstance(entry, tuple):
                name, data, mode = entry
            else:
                name, mode = entry['path'], 0o100644
                data = (value.app / name).read_bytes()
            row = zipfile.ZipInfo(name)
            row.compress_type = zipfile.ZIP_DEFLATED
            row.external_attr = mode << 16
            target.writestr(row, data)
    artifacts = [dict(name=path.name, size=path.stat().st_size, sha256=proof.file_hash(path))
                 for path in (setup, archive)]
    manifest = dict(schema=1, version=NUMBER, repository=proof.REPOSITORY,
        signing='unsigned-development-preview', publisher_policy_embedded=False,
        files=inventory, artifacts=artifacts)
    if manifest_change: manifest_change(manifest)
    raw_manifest = (json.dumps(manifest, indent=2) + '\n').encode()
    (value.release / 'Forge-release.json').write_bytes(raw_manifest)
    sums = [entry['sha256'] + '  ' + entry['name'] for entry in artifacts]
    sums.append(hashlib.sha256(raw_manifest).hexdigest() + '  Forge-release.json')
    # The ordinary Windows packaging stage may have emitted CRLF text.
    (value.release / 'SHA256SUMS.txt').write_bytes(('\r\n'.join(sums) + '\r\n').encode())
    return manifest


def seal_release(value, **kwargs):
    return tool.seal_release(value.release, value.app, NUMBER, SOURCE, value.private, value.pins, now=NOW, **kwargs)


def junction(link, target):
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_load_signer_reads_only_expected_namespace_and_matches_public_policy(fixture):
    value = fixture
    signer = tool.load_signer(value.record, value.pins, value.vault)
    assert signer.public_key().public_bytes_raw() == value.private.public_key().public_bytes_raw()
    assert value.calls == [('namespace', 'ForgeRelease'), ('get', 'fixture-ref')]
    assert not any('PRIVATE' in item for item in value.record.read_text().splitlines())


@pytest.mark.parametrize('change', ['schema-bool', 'namespace', 'reference', 'public', 'identifier', 'extra', 'nonobject', 'oversize', 'duplicate'])
def test_invalid_publisher_record_never_reads_vault(fixture, change):
    value = fixture
    record = json.loads(value.record.read_bytes())
    if change == 'schema-bool': record['schema'] = True
    elif change == 'namespace': record['namespace'] = 'Forge'
    elif change == 'reference': record['credential_ref'] = '../private-key'
    elif change == 'public': record['public_key'] = base64.b64encode(b'0' * 32).decode()
    elif change == 'identifier': record['public_key_id'] = []
    elif change == 'extra': record['private_key'] = 'must-never-be-read'
    elif change == 'nonobject': record = []
    if change == 'oversize': raw = b'x' * 16385
    elif change == 'duplicate': raw = b'{"schema":1,"schema":1}'
    else: raw = proof.canonical(record)
    value.record.write_bytes(raw)
    with pytest.raises(ValueError): tool.load_signer(value.record, value.pins, value.vault)
    assert not value.calls


def test_publisher_record_inside_source_is_refused_before_vault(fixture, monkeypatch):
    monkeypatch.setattr(tool, 'ROOT', fixture.record.parent)
    with pytest.raises(ValueError, match='outside'): tool.load_signer(fixture.record, fixture.pins, fixture.vault)
    assert not fixture.calls


@pytest.mark.parametrize('stored', [None, '', 'invalid', base64.b64encode(b'0' * 31).decode(), base64.b64encode(b'0' * 32).decode()])
def test_bad_os_credential_has_fixed_safe_error(fixture, stored):
    class FakeVault:
        def __init__(self, namespace): assert namespace == 'ForgeRelease'
        def get(self, reference): return stored
    with pytest.raises(ValueError) as caught:
        tool.load_signer(fixture.record, fixture.pins, FakeVault)
    assert 'fixture-ref' not in str(caught.value) and str(fixture.record) not in str(caught.value)


def test_vault_exception_details_are_suppressed(fixture):
    class FakeVault:
        def __init__(self, namespace): pass
        def get(self, reference): raise RuntimeError('synthetic-secret-and-reference')
    with pytest.raises(ValueError, match='operating system vault') as caught:
        tool.load_signer(fixture.record, fixture.pins, FakeVault)
    assert caught.value.__suppress_context__ and 'synthetic-secret' not in str(caught.value)


def test_seal_app_and_release_produce_exact_verifiable_public_contract(fixture):
    value = fixture
    receipt = seal_app(value)
    assert receipt['sealed_files'] == 6
    raw_build = proof.read_record(value.app / proof.BUILD_NAME)
    build = proof.verify_build(raw_build, value.pins, root=value.app, expected_version=NUMBER, expected_source=SOURCE)
    write_package(value)
    result = seal_release(value, revision=3, days=90)
    assert result == dict(version=NUMBER, publisher_authenticated=True, release_assets=5, proof_revision=3)
    raw = proof.read_record(value.release / proof.RELEASE_NAME)
    release = proof.verify_release(raw, value.pins, expected_version=NUMBER, expected_source=SOURCE,
        expected_channel='preview', expected_tag='v' + NUMBER, now=NOW, current_version='0.0.0')
    proof.bind_release_build(release, raw_build, build)
    metadata = json.loads((value.release / 'Forge-release.json').read_bytes())
    assert metadata['artifacts'] == release['artifacts']
    assert metadata['files'] == proof.tree_files(value.app)
    assert metadata['signing'] == 'ed25519-publisher-proof' and metadata['publisher_policy_embedded'] is True
    assert metadata['source_commit'] == SOURCE
    assert metadata['update_proof'] == dict(name=proof.RELEASE_NAME, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    for line in (value.release / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split('  ')
        assert proof.file_hash(value.release / name) == digest
    assert not list(value.work.rglob('*.pending'))


@pytest.mark.parametrize('field,bad', [('schema', True), ('version', '5.1.3'), ('repository', 'wrong/repo'),
    ('signing', 'ed25519-publisher-proof'), ('publisher_policy_embedded', True), ('files', []), ('artifacts', [])])
def test_invalid_package_manifest_is_rejected_without_any_output_mutation(fixture, field, bad):
    write_package(fixture, manifest_change=lambda manifest: manifest.update({field: bad}))
    before = snapshot(fixture.work)
    with pytest.raises(ValueError, match='release manifest'): seal_release(fixture)
    assert snapshot(fixture.work) == before


def test_manifest_boolean_file_sizes_cannot_alias_integer_inventory(fixture):
    def change(manifest):
        manifest['files'] = deepcopy(manifest['files'])
        manifest['files'][0]['size'] = float(manifest['files'][0]['size'])
    write_package(fixture, manifest_change=change)
    before = snapshot(fixture.work)
    with pytest.raises(ValueError, match='release manifest'): seal_release(fixture)
    assert snapshot(fixture.work) == before


@pytest.mark.parametrize('change', ['changed-app', 'changed-installer', 'wrong-sums', 'wrong-key', 'existing-proof', 'pending-proof', 'pending-manifest', 'pending-sums'])
def test_release_preflight_failures_leave_all_existing_bytes_intact(fixture, change):
    value = fixture; write_package(value)
    if change == 'changed-app': (value.app / 'Forge.exe').write_bytes(b'Changed app')
    elif change == 'changed-installer': (value.release / f'Forge-{NUMBER}-Setup.exe').write_bytes(b'Changed setup')
    elif change == 'wrong-sums': (value.release / 'SHA256SUMS.txt').write_bytes(b'Wrong sums')
    elif change == 'existing-proof': (value.release / proof.RELEASE_NAME).write_bytes(b'Existing output')
    elif change.startswith('pending-'):
        name = {'pending-proof': proof.RELEASE_NAME, 'pending-manifest': 'Forge-release.json', 'pending-sums': 'SHA256SUMS.txt'}[change]
        (value.release / (name + '.pending')).write_bytes(b'Unowned pending output')
    before = snapshot(value.work)
    private = Ed25519PrivateKey.generate() if change == 'wrong-key' else value.private
    with pytest.raises(ValueError):
        tool.seal_release(value.release, value.app, NUMBER, SOURCE, private, value.pins, now=NOW)
    assert snapshot(value.work) == before


@pytest.mark.parametrize('change', ['missing', 'extra', 'wrong-bytes', 'duplicate', 'escape', 'case-alias', 'link', 'directory', 'reserved'])
def test_wrong_or_unsafe_portable_is_never_sealed(fixture, change):
    value = fixture; seal_app(value)
    rows = [(entry['path'], (value.app / entry['path']).read_bytes(), 0o100644) for entry in proof.tree_files(value.app)]
    if change == 'missing': rows.pop()
    elif change == 'extra': rows.append(('unknown.js', b'Unknown file', 0o100644))
    elif change == 'wrong-bytes': rows[0] = (rows[0][0], b'x' * len(rows[0][1]), rows[0][2])
    elif change == 'duplicate': rows[-1] = rows[0]
    elif change == 'escape': rows[0] = ('../outside.exe', rows[0][1], rows[0][2])
    elif change == 'case-alias': rows[0] = (rows[0][0].upper(), rows[0][1], rows[0][2])
    elif change == 'link': rows[0] = (rows[0][0], rows[0][1], 0o120777)
    elif change == 'directory': rows[0] = (rows[0][0] + '/', rows[0][1], 0o040755)
    elif change == 'reserved': rows[0] = ('COM¹.log', rows[0][1], rows[0][2])
    with pytest.warns(UserWarning) if change == 'duplicate' else nullcontext():
        write_package(value, rows=rows)
    before = snapshot(value.work)
    with pytest.raises(ValueError): seal_release(value)
    assert snapshot(value.work) == before


@pytest.mark.parametrize('kwargs', [dict(days=True), dict(days=0), dict(days=91), dict(revision=True), dict(revision=0), dict(channel='nightly')])
def test_invalid_release_controls_do_not_mutate(fixture, kwargs):
    write_package(fixture)
    before = snapshot(fixture.work)
    with pytest.raises(ValueError): seal_release(fixture, **kwargs)
    assert snapshot(fixture.work) == before


@pytest.mark.parametrize('number,source,now', [('v5.1.2', SOURCE, NOW), (NUMBER, 'main', NOW), (NUMBER, SOURCE.upper(), NOW),
    (NUMBER, SOURCE, True), (NUMBER, SOURCE, 0), (NUMBER, SOURCE, float(NOW))])
def test_invalid_build_identity_does_not_create_proof(fixture, number, source, now):
    before = snapshot(fixture.work)
    with pytest.raises(ValueError): tool.seal_app(fixture.app, number, source, fixture.private, fixture.pins, now=now)
    assert snapshot(fixture.work) == before


@pytest.mark.parametrize('change', ['missing-notices', 'wrong-key', 'existing-proof', 'pending-proof', 'hard-link'])
def test_app_preflight_never_leaves_invalid_or_overwritten_proof(fixture, change):
    value = fixture
    if change == 'missing-notices': (value.app / 'LICENSE').unlink()
    elif change == 'existing-proof': (value.app / proof.BUILD_NAME).write_bytes(b'Existing proof')
    elif change == 'pending-proof': (value.app / (proof.BUILD_NAME + '.pending')).write_bytes(b'Unowned pending')
    elif change == 'hard-link': os.link(value.app / 'Forge.exe', value.app / 'alias.exe')
    before = snapshot(value.work)
    private = Ed25519PrivateKey.generate() if change == 'wrong-key' else value.private
    with pytest.raises(ValueError): tool.seal_app(value.app, NUMBER, SOURCE, private, value.pins, now=NOW)
    assert snapshot(value.work) == before


def test_actual_native_junction_is_checked_before_resolving_input(fixture):
    value = fixture; write_package(value)
    link = value.work / 'release-junction'
    junction(link, value.release)
    before = snapshot(value.release)
    try:
        with pytest.raises(ValueError, match='junction'):
            tool.seal_release(link, value.app, NUMBER, SOURCE, value.private, value.pins, now=NOW)
        assert snapshot(value.release) == before
    finally:
        if os.name == 'nt': link.rmdir()
        else: link.unlink()


@pytest.mark.parametrize('name', ['Forge-release.json', 'SHA256SUMS.txt', f'Forge-{NUMBER}-Setup.exe', f'Forge-{NUMBER}-Portable.zip'])
def test_aliased_release_files_refuse_before_publishing(fixture, name):
    write_package(fixture)
    target = fixture.release / name
    os.link(target, fixture.work / 'outside-file-alias')
    before = snapshot(fixture.work)
    with pytest.raises(ValueError, match='aliases'): seal_release(fixture)
    assert snapshot(fixture.work) == before


def test_metadata_replacement_failure_restores_originals_and_removes_owned_staging(fixture, monkeypatch):
    write_package(fixture); before = snapshot(fixture.work)
    original = tool.os.replace
    failed = []
    def replace(source, target):
        if Path(target).name == 'SHA256SUMS.txt' and not failed:
            failed.append(True)
            raise OSError('Synthetic metadata replace failure')
        return original(source, target)
    monkeypatch.setattr(tool.os, 'replace', replace)
    with pytest.raises(OSError): seal_release(fixture)
    assert failed and snapshot(fixture.work) == before


def test_proof_promotion_failure_restores_both_original_metadata_files(fixture, monkeypatch):
    write_package(fixture); before = snapshot(fixture.work)
    monkeypatch.setattr(tool, 'publish_new', lambda *args: (_ for _ in ()).throw(OSError('Synthetic final failure')))
    with pytest.raises(OSError): seal_release(fixture)
    assert snapshot(fixture.work) == before


def test_failure_while_staging_does_not_modify_or_leave_owned_files(fixture, monkeypatch):
    write_package(fixture); before = snapshot(fixture.work)
    original = tool.stage_bytes
    def stage(path, raw):
        if Path(path).name == 'Forge-release.json': raise OSError('Synthetic staging failure')
        return original(path, raw)
    monkeypatch.setattr(tool, 'stage_bytes', stage)
    with pytest.raises(OSError): seal_release(fixture)
    assert snapshot(fixture.work) == before


@pytest.mark.parametrize('changed', ['app', 'artifact', 'manifest'])
def test_inputs_modified_after_staging_abort_before_public_mutation(fixture, monkeypatch, changed):
    value = fixture; write_package(value)
    original_manifest = (value.release / 'Forge-release.json').read_bytes()
    original_sums = (value.release / 'SHA256SUMS.txt').read_bytes()
    original = tool.stage_bytes
    def stage(path, raw):
        result = original(path, raw)
        if Path(path).name == 'SHA256SUMS.txt':
            target = {'app': value.app / 'Forge.exe', 'artifact': value.release / f'Forge-{NUMBER}-Setup.exe',
                      'manifest': value.release / 'Forge-release.json'}[changed]
            target.write_bytes(b'Synthetic concurrent edit')
        return result
    monkeypatch.setattr(tool, 'stage_bytes', stage)
    with pytest.raises(ValueError): seal_release(value)
    assert not (value.release / proof.RELEASE_NAME).exists() and not list(value.work.rglob('*.pending'))
    assert (value.release / 'SHA256SUMS.txt').read_bytes() == original_sums
    if changed != 'manifest': assert (value.release / 'Forge-release.json').read_bytes() == original_manifest
    else: assert (value.release / 'Forge-release.json').read_bytes() == b'Synthetic concurrent edit'


def test_atomic_new_output_does_not_overwrite_a_collision(fixture, monkeypatch):
    output = fixture.release / proof.RELEASE_NAME
    original = tool.os.link
    def collide(source, target):
        Path(target).write_bytes(b'Unowned concurrently created output')
        return original(source, target)
    monkeypatch.setattr(tool.os, 'link', collide)
    with pytest.raises(FileExistsError): tool.write_new(output, b'Our proposed output')
    assert output.read_bytes() == b'Unowned concurrently created output'
    assert not list(fixture.work.rglob('*.pending'))


def test_exclusive_staging_collision_never_deletes_someone_elses_output(fixture, monkeypatch):
    output = fixture.release / proof.RELEASE_NAME
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path.name == proof.RELEASE_NAME + '.pending' and args == ('xb',):
            with original(path, 'wb') as handle: handle.write(b'Unowned concurrent stage')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_file)
    with pytest.raises(FileExistsError): tool.write_new(output, b'Our proposed output')
    assert not output.exists()
    assert output.with_name(output.name + '.pending').read_bytes() == b'Unowned concurrent stage'


def test_final_proof_collision_retains_external_bytes_and_restores_metadata(fixture, monkeypatch):
    write_package(fixture)
    original_manifest = (fixture.release / 'Forge-release.json').read_bytes()
    original_sums = (fixture.release / 'SHA256SUMS.txt').read_bytes()
    original = tool.os.link
    def collide(source, target):
        Path(target).write_bytes(b'Unowned concurrently created proof')
        return original(source, target)
    monkeypatch.setattr(tool.os, 'link', collide)
    with pytest.raises(FileExistsError): seal_release(fixture)
    assert (fixture.release / proof.RELEASE_NAME).read_bytes() == b'Unowned concurrently created proof'
    assert (fixture.release / 'Forge-release.json').read_bytes() == original_manifest
    assert (fixture.release / 'SHA256SUMS.txt').read_bytes() == original_sums
    assert not list(fixture.work.rglob('*.pending'))


def test_oversized_artifact_is_refused_before_manifest_or_proof_mutation(fixture, monkeypatch):
    write_package(fixture); before = snapshot(fixture.work)
    monkeypatch.setattr(tool, 'MAX_ARTIFACT_BYTES', 1)
    with pytest.raises(ValueError, match='size bound'): seal_release(fixture)
    assert snapshot(fixture.work) == before


def test_post_build_verification_failure_removes_only_own_proof(fixture, monkeypatch):
    before = snapshot(fixture.work)
    monkeypatch.setattr(tool, 'verify_build', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('Changed build')))
    with pytest.raises(ValueError): seal_app(fixture)
    assert snapshot(fixture.work) == before


def test_cli_failure_output_contains_only_safe_classification(fixture, monkeypatch, capsys):
    monkeypatch.setattr(tool, 'source_identity', lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic-secret-and-ref')))
    monkeypatch.setattr(tool.sys, 'argv', ['release_proof', 'release', '--work', str(fixture.work), '--version', NUMBER,
        '--source-commit', SOURCE, '--publisher-record', str(fixture.record)])
    assert tool.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == dict(publisher_authenticated=False, failure_class='RuntimeError')
    assert not output.err and 'synthetic-secret' not in output.out and str(fixture.record) not in output.out


def test_cli_app_stage_has_no_real_vault_and_emits_only_public_receipt(fixture, monkeypatch, capsys):
    original = tool.load_signer
    monkeypatch.setattr(tool, 'TRUSTED_UPDATE_KEYS', fixture.pins)
    monkeypatch.setattr(tool, 'source_identity', lambda number, source: tool.release_identity(number, source, NOW))
    monkeypatch.setattr(tool, 'load_signer', lambda record, pins: original(record, pins, fixture.vault))
    monkeypatch.setattr(tool.sys, 'argv', ['release_proof', 'app', '--work', str(fixture.work), '--version', NUMBER,
        '--source-commit', SOURCE, '--publisher-record', str(fixture.record)])
    assert tool.main() == 0
    output = capsys.readouterr()
    assert set(json.loads(output.out)) == {'version', 'sealed_files', 'build_proof_sha256'}
    assert not output.err and 'fixture-ref' not in output.out and str(fixture.record) not in output.out


def test_cli_rejects_actual_work_junction_before_app_output(fixture, monkeypatch, capsys):
    link = fixture.work.parent / 'work-junction'
    junction(link, fixture.work)
    monkeypatch.setattr(tool, 'source_identity', lambda *args: None)
    monkeypatch.setattr(tool, 'load_signer', lambda *args: fixture.private)
    monkeypatch.setattr(tool.sys, 'argv', ['release_proof', 'app', '--work', str(link), '--version', NUMBER,
        '--source-commit', SOURCE, '--publisher-record', str(fixture.record)])
    before = snapshot(fixture.work)
    try:
        assert tool.main() == 1
        assert json.loads(capsys.readouterr().out) == dict(publisher_authenticated=False, failure_class='ValueError')
        assert snapshot(fixture.work) == before
    finally:
        if os.name == 'nt': link.rmdir()
        else: link.unlink()


@pytest.mark.parametrize('change', ['dirty', 'head', 'version'])
def test_source_identity_uses_exact_clean_committed_package(fixture, monkeypatch, change):
    root = fixture.work / 'synthetic-source'
    (root / 'frontend').mkdir(parents=True)
    (root / 'frontend/package.json').write_text(json.dumps({'version': '5.1.3' if change == 'version' else NUMBER}))
    monkeypatch.setattr(tool, 'ROOT', root)
    def run(arguments, **kwargs):
        assert kwargs['cwd'] == root and kwargs['capture_output'] is True
        return SimpleNamespace(stdout=('M changed.py' if change == 'dirty' else '') if arguments[1] == 'status'
            else ('8' * 40 if change == 'head' else SOURCE))
    monkeypatch.setattr(tool.subprocess, 'run', run)
    with pytest.raises(ValueError, match='clean committed'): tool.source_identity(NUMBER, SOURCE)


def test_source_identity_accepts_only_matching_clean_committed_package(fixture, monkeypatch):
    root = fixture.work / 'synthetic-source'
    (root / 'frontend').mkdir(parents=True)
    (root / 'frontend/package.json').write_text(json.dumps({'version': NUMBER}))
    monkeypatch.setattr(tool, 'ROOT', root)
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(stdout=SOURCE if arguments[1] == 'rev-parse' else '')
    monkeypatch.setattr(tool.subprocess, 'run', run)
    tool.source_identity(NUMBER, SOURCE)
    assert calls == [['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain']]
