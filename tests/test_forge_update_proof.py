"""Ephemeral keys and disposable files only. No network or executable launch."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

import forge_update_proof as proof

SOURCE = '1' * 40
NOW = 1_800_000_000


def sign(payload, private_keys):
    raw = proof.canonical(payload)
    return proof.envelope(payload, [{'key_id': proof.key_id(private.public_key().public_bytes_raw()),
        'signature': base64.b64encode(private.sign(proof.DOMAINS[payload['kind']] + raw)).decode('ascii')}
        for private in private_keys])


@pytest.fixture
def publisher():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return private, {proof.key_id(public): public}


@pytest.fixture
def build(tmp_path, publisher):
    root = tmp_path / 'app'; root.mkdir()
    (root / 'Forge.exe').write_bytes(b'Unexecuted synthetic Forge host')
    (root / 'ForgeBrowserHost.exe').write_bytes(b'Unexecuted synthetic browser host')
    internal = root / '_internal'; internal.mkdir()
    (internal / 'runtime.dll').write_bytes(b'Synthetic runtime bytes')
    payload = dict(schema=1, kind='forge-build', repository=proof.REPOSITORY, platform=proof.PLATFORM,
                   version='5.1.2', source_commit=SOURCE, created_at=NOW, files=proof.tree_files(root))
    envelope = sign(payload, [publisher[0]])
    (root / proof.BUILD_NAME).write_bytes(envelope)
    return root, payload, envelope


@pytest.fixture
def release(tmp_path, publisher, build):
    artifacts = []
    for name in ('Forge-5.1.2-Setup.exe', 'Forge-5.1.2-Portable.zip'):
        path = tmp_path / name; path.write_bytes(b'Synthetic artifact: ' + name.encode())
        artifacts.append(dict(name=name, size=path.stat().st_size, sha256=proof.file_hash(path)))
    payload = dict(schema=1, kind='forge-release', repository=proof.REPOSITORY, platform=proof.PLATFORM,
        version='5.1.2', source_commit=SOURCE, created_at=NOW, expires_at=NOW + 3600,
        revision=1, channel='preview', tag='v5.1.2', build_proof_sha256=sha256(build[2]).hexdigest(), artifacts=artifacts)
    envelope = sign(payload, [publisher[0]])
    return payload, envelope


def verify_build(build, publisher, **kwargs):
    return proof.verify_build(build[2], publisher[1], root=build[0], expected_version='5.1.2',
                              expected_source=SOURCE, **kwargs)


def verify_release(envelope, publisher, **kwargs):
    values = dict(expected_version='5.1.2', expected_source=SOURCE, expected_channel='preview',
                  expected_tag='v5.1.2', now=NOW + 1, current_version='5.1.1')
    values.update(kwargs)
    return proof.verify_release(envelope, publisher[1], **values)


def test_build_and_detached_release_authenticate_without_hash_cycle(build, release, publisher, tmp_path):
    verified_build = verify_build(build, publisher)
    verified_release = verify_release(release[1], publisher)
    proof.bind_release_build(verified_release, build[2], verified_build)
    for item in verified_release['artifacts']:
        proof.verify_artifact(tmp_path / item['name'], verified_release, item['name'])
    assert not any(item['path'] == proof.BUILD_NAME for item in verified_build['files'])


@pytest.mark.parametrize('kind', ['forge-build', 'forge-release'])
def test_wrong_key_and_cross_domain_signature_cannot_authenticate(build, release, kind):
    envelope = build[2] if kind == 'forge-build' else release[1]
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    with pytest.raises(ValueError, match='trusted'):
        proof.authenticate(envelope, kind, {proof.key_id(other): other})


def test_known_key_cannot_substitute_the_other_proof_domain(build, publisher):
    with pytest.raises(ValueError, match='trusted'):
        proof.authenticate(build[2], 'forge-release', publisher[1])


@pytest.mark.parametrize('tamper', ['payload', 'signature', 'key_id', 'extra', 'boolschema', 'duplicate'])
def test_untrusted_envelope_tampering_fails(build, publisher, tamper):
    value = json.loads(build[2])
    if tamper == 'payload': value['payload'] = base64.b64encode(b'{"kind":"forge-build"}').decode()
    if tamper == 'signature': value['signatures'][0]['signature'] = base64.b64encode(b'X' * 64).decode()
    if tamper == 'key_id': value['signatures'][0]['key_id'] = '0' * 64
    if tamper == 'extra': value['public_key'] = 'not a trust anchor'
    if tamper == 'boolschema': value['schema'] = True
    if tamper == 'duplicate': value['signatures'] *= 2
    with pytest.raises(ValueError): proof.authenticate(proof.canonical(value), 'forge-build', publisher[1])


@pytest.mark.parametrize('field,value', [('repository', 'other/project'), ('platform', 'linux-x64'),
    ('version', '5.1.3'), ('source_commit', '2' * 40), ('schema', True), ('created_at', True)])
def test_even_valid_signatures_must_match_the_expected_build_identity(build, publisher, field, value):
    payload = deepcopy(build[1]); payload[field] = value
    envelope = sign(payload, [publisher[0]])
    (build[0] / proof.BUILD_NAME).write_bytes(envelope)
    with pytest.raises(ValueError): verify_build((build[0], payload, envelope), publisher)


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'unknown', 'missingproof', 'proofbytes'])
def test_modified_or_unknown_installed_files_abort(build, publisher, mutation):
    if mutation == 'changed': (build[0] / 'Forge.exe').write_bytes(b'Tampered host')
    if mutation == 'missing': (build[0] / 'ForgeBrowserHost.exe').unlink()
    if mutation == 'unknown': (build[0] / 'unknown-helper.exe').write_bytes(b'Never run')
    if mutation == 'missingproof': (build[0] / proof.BUILD_NAME).unlink()
    if mutation == 'proofbytes': (build[0] / proof.BUILD_NAME).write_bytes(b'Altered sidecar')
    with pytest.raises(ValueError): verify_build(build, publisher, installed=True)


def test_only_exact_root_inno_generated_files_are_classified(build, publisher):
    (build[0] / 'unins000.exe').write_bytes(b'Synthetic installation artifact')
    (build[0] / 'unins000.dat').write_bytes(b'Synthetic installation artifact')
    verify_build(build, publisher, installed=True)
    with pytest.raises(ValueError): verify_build(build, publisher, installed=False)
    (build[0] / '_internal/unins000.exe').write_bytes(b'Unknown distributable executable')
    with pytest.raises(ValueError): verify_build(build, publisher, installed=True)


@pytest.mark.parametrize('path', ['../outside', 'C:/outside', '/root', 'a\\b', 'a//b', './file', 'a/../b',
    'a./b', 'a /b', 'nul.txt', 'aux', 'COM1.dll', 'conin$', 'file:stream', 'a\x00b',
    'COM¹', 'COM².txt', 'COM³.dll', 'LPT¹', 'LPT².txt', 'LPT³.dll'])
def test_windows_path_aliases_and_escape_are_rejected(path):
    with pytest.raises(ValueError): proof.relative_path(path)


def test_duplicate_case_paths_in_signed_manifest_fail(build, publisher):
    payload = deepcopy(build[1]); alias = dict(payload['files'][0]); alias['path'] = alias['path'].swapcase()
    payload['files'].append(alias)
    envelope = sign(payload, [publisher[0]])
    (build[0] / proof.BUILD_NAME).write_bytes(envelope)
    with pytest.raises(ValueError, match='Duplicate'): verify_build((build[0], payload, envelope), publisher)


@pytest.mark.parametrize('field,value', [('repository', 'other/project'), ('platform', 'linux-x64'),
    ('version', '5.1.3'), ('source_commit', '2' * 40), ('channel', 'stable'), ('tag', 'v5.1.3'),
    ('revision', True), ('build_proof_sha256', 'bad'), ('unexpected', 'field')])
def test_signed_release_identity_cannot_be_repurposed(release, publisher, field, value):
    payload = deepcopy(release[0]); payload[field] = value
    with pytest.raises(ValueError): verify_release(sign(payload, [publisher[0]]), publisher)


@pytest.mark.parametrize('field,value', [('expires_at', NOW), ('expires_at', NOW - 1),
    ('expires_at', NOW + proof.MAX_RELEASE_LIFETIME + 1), ('expires_at', True), ('created_at', NOW + 2)])
def test_expired_future_and_unbounded_validity_records_are_rejected(release, publisher, field, value):
    payload = deepcopy(release[0]); payload[field] = value
    with pytest.raises(ValueError, match='validity|expired'):
        verify_release(sign(payload, [publisher[0]]), publisher)


@pytest.mark.parametrize('current', ['5.1.2', '5.1.3'])
def test_current_or_older_installer_cannot_be_admitted(release, publisher, current):
    with pytest.raises(ValueError, match='admitted'): verify_release(release[1], publisher, current_version=current)


def test_trusted_version_revision_and_conflict_pin_stop_metadata_replay(release, publisher):
    prior = dict(version='5.1.3', revision=1, proof_sha256=sha256(release[1]).hexdigest())
    with pytest.raises(ValueError, match='rolled back'): verify_release(release[1], publisher, prior=prior)
    prior.update(version='5.1.2', revision=2)
    with pytest.raises(ValueError, match='rolled back'): verify_release(release[1], publisher, prior=prior)
    prior.update(revision=1)
    assert verify_release(release[1], publisher, prior=prior)['revision'] == 1
    changed = deepcopy(release[0]); changed['expires_at'] += 1
    with pytest.raises(ValueError, match='conflicting'):
        verify_release(sign(changed, [publisher[0]]), publisher, prior=prior)


def test_release_revisions_can_renew_validity_without_replacing_a_package(release, publisher):
    prior = dict(version='5.1.2', revision=1, proof_sha256=sha256(release[1]).hexdigest())
    renewed = deepcopy(release[0]); renewed['revision'] = 2; renewed['expires_at'] += 3600
    value = verify_release(sign(renewed, [publisher[0]]), publisher, prior=prior)
    assert value['artifacts'] == release[0]['artifacts']


@pytest.mark.parametrize('mutation', ['wrong-name', 'duplicate', 'extra', 'sizebool', 'hash', 'empty', 'overlarge'])
def test_release_artifact_allowlist_integrity_fields_are_strict(release, publisher, mutation):
    value = deepcopy(release[0])
    if mutation == 'wrong-name': value['artifacts'][0]['name'] = 'Unknown-helper.exe'
    if mutation == 'duplicate': value['artifacts'][1] = dict(value['artifacts'][0])
    if mutation == 'extra': value['artifacts'].append(dict(value['artifacts'][0], name='README.txt'))
    if mutation == 'sizebool': value['artifacts'][0]['size'] = True
    if mutation == 'hash': value['artifacts'][0]['sha256'] = 'bad'
    if mutation == 'empty': value['artifacts'][0]['size'] = 0
    if mutation == 'overlarge': value['artifacts'][0]['size'] = proof.MAX_ARTIFACT_BYTES + 1
    with pytest.raises(ValueError): verify_release(sign(value, [publisher[0]]), publisher)


def test_downloaded_bytes_and_build_proof_cannot_be_mixed_between_releases(release, build, publisher, tmp_path):
    trusted = verify_release(release[1], publisher)
    name = trusted['artifacts'][0]['name']; (tmp_path / name).write_bytes(b'Wrong artifact')
    with pytest.raises(ValueError, match='artifact'): proof.verify_artifact(tmp_path / name, trusted, name)
    with pytest.raises(ValueError, match='same build'): proof.bind_release_build(trusted, build[2] + b'\n', build[1])
    altered = dict(build[1], source_commit='2' * 40)
    with pytest.raises(ValueError, match='same build'): proof.bind_release_build(trusted, build[2], altered)


def test_duplicate_json_and_noncanonical_authenticated_payloads_fail(publisher):
    private, keys = publisher
    for raw in (b'{"kind":"forge-build","kind":"forge-build"}', b'{ "kind": "forge-build" }', b'{"kind":"forge-build","x":NaN}'):
        signature = private.sign(proof.DOMAINS['forge-build'] + raw)
        envelope = proof.canonical({'schema': 1, 'payload': base64.b64encode(raw).decode(),
            'signatures': [{'key_id': next(iter(keys)), 'signature': base64.b64encode(signature).decode()}]})
        with pytest.raises(ValueError): proof.authenticate(envelope, 'forge-build', keys)


def test_live_feed_cannot_mutate_pins_and_rotation_requires_a_new_authenticated_application(build, publisher):
    new_private = Ed25519PrivateKey.generate(); new_public = new_private.public_key().public_bytes_raw()
    new_pins = {proof.key_id(new_public): new_public}
    new_only = sign(build[1], [new_private])
    with pytest.raises(ValueError, match='trusted'): proof.authenticate(new_only, 'forge-build', publisher[1])
    dual = sign(build[1], [publisher[0], new_private])
    assert proof.authenticate(dual, 'forge-build', publisher[1]) == build[1]
    assert proof.authenticate(dual, 'forge-build', new_pins) == build[1]
    with pytest.raises(ValueError, match='trusted'): proof.authenticate(build[2], 'forge-build', new_pins)


def test_unknown_payload_fields_never_bootstrap_a_public_key(build, publisher):
    payload = dict(build[1], new_public_key='Untrusted proposed key')
    envelope = sign(payload, [publisher[0]])
    (build[0] / proof.BUILD_NAME).write_bytes(envelope)
    with pytest.raises(ValueError, match='Unknown'): verify_build((build[0], payload, envelope), publisher)


def test_size_bounds_stop_endless_proof_data_before_parsing(build, publisher):
    with pytest.raises(ValueError, match='size'): proof.authenticate(b'X' * (proof.MAX_RECORD_BYTES + 1), 'forge-build', publisher[1])


def test_tree_link_cannot_escape_the_disposable_app(build, publisher, tmp_path):
    target = tmp_path / 'external-fixture'; target.write_bytes(b'Synthetic external bytes')
    if os.name == 'nt':
        outside = tmp_path / 'external-directory'; outside.mkdir()
        (outside / 'sentinel').write_bytes(b'Outside synthetic fixture')
        link = build[0] / 'linked'
        # NTFS directory junctions need no symbolic-link privilege. All targets
        # are explicit disposable fixture directories; no personal path is used.
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            'New-Item -ItemType Junction -Path $env:FORGE_FIXTURE_LINK -Target $env:FORGE_FIXTURE_TARGET | Out-Null'],
            env={**os.environ, 'FORGE_FIXTURE_LINK': str(link), 'FORGE_FIXTURE_TARGET': str(outside)},
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0 and link.is_junction()
        try:
            with pytest.raises(ValueError, match='links'): verify_build(build, publisher)
            from forge_updates import UpdateManager
            service = SimpleNamespace(store=SimpleNamespace(home=tmp_path / 'disposable-data'))
            with pytest.raises(ValueError, match='links|junctions'):
                UpdateManager(service, install_dir=link, proof_policy=publisher[1])
            assert (outside / 'sentinel').read_bytes() == b'Outside synthetic fixture'
        finally:
            link.rmdir()  # Remove only the junction, never its target contents.
    else:
        (build[0] / 'linked').symlink_to(target)
        with pytest.raises(ValueError, match='links'): verify_build(build, publisher)


def test_compiled_pins_are_copied_and_cannot_be_changed_by_callers(publisher):
    keys = dict(publisher[1]); pins = proof.normalize_keys(keys); keys.clear()
    assert len(pins) == 1
    with pytest.raises(TypeError): pins['0' * 64] = b'X' * 32


def test_oversized_cached_proof_is_rejected_before_parsing(tmp_path):
    path = tmp_path / 'proof.json'; path.write_bytes(b'X' * (proof.MAX_RECORD_BYTES + 1))
    with pytest.raises(ValueError, match='size'): proof.read_record(path)
