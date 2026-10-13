"""Seal release bytes with a publisher key held only in the OS credential vault."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from forge_credentials import CredentialVault
from forge_update_keys import TRUSTED_UPDATE_KEYS
from forge_update_proof import (BUILD_NAME, DOMAINS, MAX_ARTIFACT_BYTES, MAX_EXPANDED_BYTES,
    PLATFORM, REPOSITORY, authenticate,
    bind_release_build, canonical, envelope, file_hash, key_id, normalize_keys,
    numeric_version, read_record, relative_path, strict_json, tree_files, validated_build_manifest,
    verify_artifact, verify_build, verify_release)

RELEASE_NAME = 'Forge-update-proof.json'
REQUIRED_FILES = frozenset(('Forge.exe', 'ForgeBrowserHost.exe', 'LICENSE',
                            'THIRD_PARTY_NOTICES.md', 'third-party-licenses/manifest.json'))


def location(value, *, directory=None, fresh=False):
    """Inspect the supplied spelling before resolution can hide a junction."""
    path = Path(value).absolute()
    for part in path.parts[1:]:
        relative_path(part)
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            item = part.lstat()
            if part.is_symlink() or getattr(item, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Publisher inputs and outputs cannot contain links or junctions.')
    exists = path.exists()
    if fresh:
        if exists: raise ValueError('Publisher proofs require fresh output files.')
        if not path.parent.is_dir(): raise ValueError('Publisher output folder is unavailable.')
    elif not exists:
        raise ValueError('A required publisher input is unavailable.')
    if exists:
        item = path.lstat()
        if directory is True and not stat.S_ISDIR(item.st_mode):
            raise ValueError('Publisher input folder is unavailable.')
        if directory is False and (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1):
            raise ValueError('Publisher inputs must be ordinary files without aliases.')
    return path.resolve()


def checked_tree(root):
    root = location(root, directory=True)
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            location(Path(parent) / name, directory=True)
        for name in files:
            location(Path(parent) / name, directory=False)
    values = tree_files(root)
    if not REQUIRED_FILES <= {entry['path'] for entry in values}:
        raise ValueError('The application is missing native hosts or dependency notices.')
    return values


def release_identity(number, source_commit, now):
    numeric_version(number)
    if not isinstance(source_commit, str) or not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('Specify the exact committed release source.')
    if type(now) is not int or now <= 0:
        raise ValueError('Use a positive integral release time.')


def load_signer(record_path, pins, vault_factory=CredentialVault):
    policy = normalize_keys(pins)
    try:
        path = location(record_path, directory=False)
        if path.stat().st_size > 16384:
            raise ValueError('Publisher record exceeds its size bound.')
        record = strict_json(read_record(path))
    except (OSError, ValueError):
        raise ValueError('The private publisher record is unavailable or invalid.') from None
    if path.is_relative_to(ROOT.resolve()):
        raise ValueError('Keep the private publisher record outside the source repository.')
    fields = {'schema', 'namespace', 'credential_ref', 'public_key_id', 'public_key'}
    if (not isinstance(record, dict) or set(record) != fields or type(record['schema']) is not int
            or record['schema'] != 1 or record['namespace'] != 'ForgeRelease'
            or not isinstance(record['credential_ref'], str)
            or not isinstance(record['public_key_id'], str) or not isinstance(record['public_key'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', record['credential_ref'])):
        raise ValueError('Invalid publisher record.')
    if policy.get(record['public_key_id']) is None or base64.b64encode(
            policy[record['public_key_id']]).decode('ascii') != record['public_key']:
        raise ValueError('The publisher record differs from the committed public policy.')
    try:
        stored = vault_factory(namespace='ForgeRelease').get(record['credential_ref'])
        raw = base64.b64decode(stored, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode('ascii') != stored:
            raise ValueError('Invalid signing credential.')
        private = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        raise ValueError('The signing credential could not be read from the operating system vault.') from None
    public = private.public_key().public_bytes_raw()
    identifier = key_id(public)
    if (record['public_key_id'] != identifier or record['public_key'] != base64.b64encode(public).decode('ascii')
            or policy.get(identifier) != public):
        raise ValueError('The signing key differs from the committed public publisher policy.')
    return private


def signed_record(payload, private, pins):
    raw = canonical(payload)
    signature = private.sign(DOMAINS[payload['kind']] + raw)
    result = envelope(payload, [{'key_id': key_id(private.public_key().public_bytes_raw()),
                                'signature': base64.b64encode(signature).decode('ascii')}])
    authenticate(result, payload['kind'], normalize_keys(pins))
    return result


def stage_bytes(path, raw):
    temporary = location(Path(path).with_name(Path(path).name + '.pending'), fresh=True)
    identity = None
    try:
        with temporary.open('xb') as handle:
            item = os.fstat(handle.fileno())
            identity = (item.st_dev, item.st_ino)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if identity is not None: cleanup_stage((temporary, identity))
        raise
    return temporary, identity


def cleanup_stage(staged):
    path, identity = staged
    try:
        location(path.parent, directory=True)
        item = path.lstat()
        if (stat.S_ISREG(item.st_mode) and not getattr(item, 'st_file_attributes', 0) & 0x400
                and (item.st_dev, item.st_ino) == identity):
            path.unlink()
    except (OSError, ValueError):
        pass


def publish_new(staged, path):
    # Linking a staged file is an atomic create-if-absent on NTFS. os.replace
    # would silently overwrite a proof another invocation just published.
    location(path, fresh=True)
    temporary, _ = staged
    location(temporary, directory=False)
    os.link(temporary, path)
    cleanup_stage(staged)


def write_new(path, raw):
    path = location(path, fresh=True)
    staged = stage_bytes(path, raw)
    try:
        publish_new(staged, path)
    finally:
        cleanup_stage(staged)


def seal_app(app, number, source_commit, private, pins, *, now=None):
    created = int(time.time()) if now is None else now
    release_identity(number, source_commit, created)
    policy = normalize_keys(pins)
    app = location(app, directory=True)
    output = location(app / BUILD_NAME, fresh=True)
    location(output.with_name(output.name + '.pending'), fresh=True)
    files = checked_tree(app)
    payload = {'schema': 1, 'kind': 'forge-build', 'repository': REPOSITORY, 'platform': PLATFORM,
               'version': number, 'source_commit': source_commit,
               'created_at': created, 'files': files}
    raw = signed_record(payload, private, policy)
    validated_build_manifest(raw, policy, expected_version=number, expected_source=source_commit)
    if checked_tree(app) != files:
        raise ValueError('Application bytes changed while preparing their publisher proof.')
    write_new(output, raw)
    try:
        verify_build(raw, policy, root=app, expected_version=number, expected_source=source_commit)
    except BaseException:
        # Remove only the proof this invocation published, never a replacement.
        if read_record(output) == raw: output.unlink()
        raise
    return {'version': number, 'sealed_files': len(payload['files']), 'build_proof_sha256': hashlib.sha256(raw).hexdigest()}


def verify_portable(archive, files):
    """Read and hash the archive in place; signing never extracts or executes it."""
    wanted = {entry['path']: entry for entry in files}
    with zipfile.ZipFile(archive) as source:
        rows = source.infolist()
        if len(rows) != len(wanted):
            raise ValueError('Portable archive has a different application file set.')
        seen, total = set(), 0
        for row in rows:
            name = relative_path(row.filename)
            if (row.is_dir() or name.casefold() in seen or name not in wanted
                    or stat.S_IFMT(row.external_attr >> 16) not in (0, stat.S_IFREG)
                    or row.external_attr & 0x400 or row.flag_bits & 1
                    or row.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError('Portable archive contains an unsafe or unexpected entry.')
            seen.add(name.casefold())
            total += row.file_size
            if row.file_size != wanted[name]['size'] or total > MAX_EXPANDED_BYTES:
                raise ValueError('Portable archive differs from its bounded file inventory.')
        for row in rows:
            digest, count = hashlib.sha256(), 0
            with source.open(row) as data:
                for chunk in iter(lambda: data.read(1024 * 1024), b''):
                    count += len(chunk)
                    if count > wanted[row.filename]['size']:
                        raise ValueError('Portable archive exceeds its declared file size.')
                    digest.update(chunk)
            if count != wanted[row.filename]['size'] or digest.hexdigest() != wanted[row.filename]['sha256']:
                raise ValueError('Portable bytes differ from the sealed application.')


def verify_manifest(raw, number, files, artifacts):
    manifest = strict_json(raw)
    fields = {'schema', 'version', 'repository', 'signing', 'publisher_policy_embedded', 'files', 'artifacts'}
    if (not isinstance(manifest, dict) or set(manifest) != fields or type(manifest.get('schema')) is not int
            or manifest['schema'] != 1 or manifest['version'] != number or manifest['repository'] != REPOSITORY
            or manifest['signing'] not in ('unsigned-development-preview', 'verified-authenticode')
            or type(manifest['publisher_policy_embedded']) is not bool
            or manifest['publisher_policy_embedded'] != (manifest['signing'] == 'verified-authenticode')
            or canonical(manifest['files']) != canonical(files)
            or canonical(manifest['artifacts']) != canonical(artifacts)):
        raise ValueError('The release manifest differs from the sealed application and packages.')
    return manifest


def publish_release(release, raw, manifest_raw, sums_raw, originals, validate):
    outputs = [(release / RELEASE_NAME, raw), (release / 'Forge-release.json', manifest_raw),
               (release / 'SHA256SUMS.txt', sums_raw)]
    staged, replaced = {}, []
    try:
        for path, data in outputs:
            staged[path] = stage_bytes(path, data)
        # All temporary outputs are complete before modifying any public file.
        for path, data in outputs:
            if read_record(staged[path][0]) != data:
                raise ValueError('Staged publisher output was modified.')
        validate()
        for path, _ in outputs[1:]:
            location(path, directory=False)
            if read_record(path) != originals[path]:
                raise ValueError('Release metadata changed while preparing its publisher proof.')
        for path, _ in outputs[1:]:
            os.replace(staged[path][0], path)
            replaced.append(path)
        # This new, non-overwriting proof is the final authenticated commit
        # marker. A crash before it appears leaves an untrusted release.
        publish_new(staged[outputs[0][0]], outputs[0][0])
    except BaseException:
        proof_path = outputs[0][0]
        if proof_path in staged:
            cleanup_stage((proof_path, staged[proof_path][1]))
        for path in reversed(replaced):
            emitted = next(data for target, data in outputs if target == path)
            if read_record(path) != emitted:
                raise ValueError('Release metadata changed during publisher output recovery.') from None
            restoration = stage_bytes(path, originals[path])
            try:
                os.replace(restoration[0], path)
            finally:
                cleanup_stage(restoration)
        raise
    finally:
        for value in staged.values(): cleanup_stage(value)


def seal_release(release, app, number, source_commit, private, pins, *, now=None, days=30, revision=1, channel='preview'):
    created = int(time.time()) if now is None else now
    release_identity(number, source_commit, created)
    if type(days) is not int or not 1 <= days <= 90 or type(revision) is not int or revision < 1:
        raise ValueError('Use a validity window of 1–90 days and a positive proof revision.')
    if channel not in ('stable', 'preview'):
        raise ValueError('Use a supported public release channel.')
    policy = normalize_keys(pins)
    release, app = location(release, directory=True), location(app, directory=True)
    if release.is_relative_to(app) or app.is_relative_to(release):
        raise ValueError('Keep the application and release output folders separate.')
    proof_path = location(release / RELEASE_NAME, fresh=True)
    manifest_path = location(release / 'Forge-release.json', directory=False)
    sums_path = location(release / 'SHA256SUMS.txt', directory=False)
    for path in (proof_path, manifest_path, sums_path):
        location(path.with_name(path.name + '.pending'), fresh=True)
    raw_build = read_record(app / BUILD_NAME)
    build = verify_build(raw_build, policy, root=app,
                         expected_version=number, expected_source=source_commit)
    files = checked_tree(app)
    artifacts = []
    for name in (f'Forge-{number}-Setup.exe', f'Forge-{number}-Portable.zip'):
        path = location(release / name, directory=False)
        if not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError('Release artifacts exceed their supported size bound.')
        artifacts.append({'name': name, 'size': path.stat().st_size, 'sha256': file_hash(path)})
    originals = {manifest_path: read_record(manifest_path), sums_path: read_record(sums_path)}
    manifest = verify_manifest(originals[manifest_path], number, files, artifacts)
    expected_sums = [entry['sha256'] + '  ' + entry['name'] for entry in artifacts] + [
        hashlib.sha256(originals[manifest_path]).hexdigest() + '  Forge-release.json']
    if originals[sums_path].decode('ascii').splitlines() != expected_sums:
        raise ValueError('Release checksums differ from the packaged artifacts and manifest.')
    verify_portable(release / artifacts[1]['name'], files)
    payload = {'schema': 1, 'kind': 'forge-release', 'repository': REPOSITORY, 'platform': PLATFORM,
               'version': number, 'source_commit': source_commit, 'created_at': created,
               'expires_at': created + days * 86400, 'revision': revision, 'channel': channel,
               'tag': 'v' + number, 'build_proof_sha256': hashlib.sha256(raw_build).hexdigest(), 'artifacts': artifacts}
    raw = signed_record(payload, private, policy)
    verified = verify_release(raw, policy, expected_version=number, expected_source=source_commit,
                              expected_channel=channel, expected_tag='v' + number, now=created, current_version='0.0.0')
    bind_release_build(verified, raw_build, build)
    def validate():
        if checked_tree(app) != files:
            raise ValueError('Application bytes changed while preparing the release proof.')
        for artifact in artifacts:
            path = location(release / artifact['name'], directory=False)
            verify_artifact(path, verified, artifact['name'])
    manifest.update(signing='ed25519-publisher-proof', publisher_policy_embedded=True,
                    source_commit=source_commit, update_proof={'name': RELEASE_NAME, 'size': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()})
    manifest_raw = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    sums = [(entry['name'], entry['sha256']) for entry in artifacts] + [
        ('Forge-release.json', hashlib.sha256(manifest_raw).hexdigest()), (RELEASE_NAME, hashlib.sha256(raw).hexdigest())]
    sums_raw = ''.join(digest + '  ' + name + '\n' for name, digest in sums).encode('ascii')
    publish_release(release, raw, manifest_raw, sums_raw, originals, validate)
    return {'version': number, 'publisher_authenticated': True, 'release_assets': 5, 'proof_revision': revision}


def source_identity(number, commit):
    numeric_version(number)
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise ValueError('Specify the exact committed release source.')
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, check=True,
                           capture_output=True, text=True, encoding='utf-8').stdout.strip()
    if head != commit or dirty or json.loads((ROOT / 'frontend/package.json').read_text())['version'] != number:
        raise ValueError('Seal only a clean committed source with the exact package version.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('app', 'release'))
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--publisher-record', type=Path, required=True)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--revision', type=int, default=1)
    parser.add_argument('--channel', choices=('stable', 'preview'), default='preview')
    args = parser.parse_args()
    try:
        source_identity(args.version, args.source_commit)
        private = load_signer(args.publisher_record, TRUSTED_UPDATE_KEYS)
        work = location(args.work, directory=True)
        app = work / 'dist/Forge'
        result = (seal_app(app, args.version, args.source_commit, private, TRUSTED_UPDATE_KEYS) if args.stage == 'app'
                  else seal_release(work / 'release', app, args.version, args.source_commit,
                                    private, TRUSTED_UPDATE_KEYS, days=args.days, revision=args.revision, channel=args.channel))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'publisher_authenticated': False, 'failure_class': type(error).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
