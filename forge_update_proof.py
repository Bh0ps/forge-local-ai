"""Bounded publisher proofs, independent of GitHub metadata and Windows PKI."""
from __future__ import annotations

import base64
from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPOSITORY = 'Bh0ps/forge-local-ai'
PLATFORM = 'windows-x64'
BUILD_NAME = 'Forge-build-proof.json'
RELEASE_NAME = 'Forge-update-proof.json'
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_FILES = 10000
MAX_EXPANDED_BYTES = 4 * 1024**3
MAX_ARTIFACT_BYTES = 1024**3
MAX_RELEASE_LIFETIME = 90 * 24 * 60 * 60
HOST_OUTPUTS = frozenset(('unins000.exe', 'unins000.dat'))
DOMAINS = {'forge-build': b'Forge build proof v1\x00', 'forge-release': b'Forge release proof v1\x00'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate JSON field.')
        result[key] = value
    return result


def strict_json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicates,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON number.')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid proof JSON.') from exc


def key_id(public):
    if not isinstance(public, bytes) or len(public) != 32: raise ValueError('Invalid Ed25519 public key.')
    return sha256(public).hexdigest()


def bounded_b64(value, maximum):
    if not isinstance(value, str) or len(value) > (maximum + 2) // 3 * 4:
        raise ValueError('Invalid encoded proof field.')
    try: result = base64.b64decode(value, validate=True)
    except ValueError as exc: raise ValueError('Invalid base64 proof field.') from exc
    if len(result) > maximum or base64.b64encode(result).decode('ascii') != value:
        raise ValueError('Noncanonical encoded proof field.')
    return result


def normalize_keys(values):
    """Copy compiled public pins; settings and mutable callers cannot add trust."""
    if not isinstance(values, Mapping):
        raise ValueError('Invalid embedded update signing policy.')
    if len(values) > 4: raise ValueError('Too many embedded update publishers.')
    result = {}
    for identifier, value in values.items():
        public = value if isinstance(value, bytes) else bounded_b64(value, 32)
        if key_id(public) != identifier: raise ValueError('Embedded update key identity differs.')
        result[identifier] = public
    return MappingProxyType(result)


def envelope(payload, signatures):
    """Assemble public signatures produced by a separate private release tool."""
    return canonical({'schema': 1, 'payload': base64.b64encode(canonical(payload)).decode('ascii'), 'signatures': signatures})


def authenticate(envelope, kind, pinned_keys):
    """Authenticate exact bounded payload bytes before semantic JSON parsing."""
    if kind not in DOMAINS or not isinstance(envelope, bytes) or not 0 < len(envelope) <= MAX_RECORD_BYTES:
        raise ValueError('Invalid proof size or kind.')
    outer = strict_json(envelope)
    if not isinstance(outer, dict) or set(outer) != {'schema', 'payload', 'signatures'} or outer['schema'] != 1 or type(outer['schema']) is not int:
        raise ValueError('Invalid proof envelope.')
    signatures = outer['signatures']
    if not isinstance(signatures, list) or not 1 <= len(signatures) <= 4:
        raise ValueError('Invalid proof signatures.')
    raw = bounded_b64(outer['payload'], MAX_RECORD_BYTES * 3 // 4)
    seen = set(); valid = False
    for signature in signatures:
        if not isinstance(signature, dict) or set(signature) != {'key_id', 'signature'}:
            raise ValueError('Invalid proof signature entry.')
        identifier = signature['key_id']
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier) or identifier in seen:
            raise ValueError('Duplicate or invalid signing identity.')
        seen.add(identifier)
        sig = bounded_b64(signature['signature'], 64)
        if len(sig) != 64: raise ValueError('Invalid Ed25519 signature size.')
        public = pinned_keys.get(identifier)
        if public is None: continue
        if key_id(public) != identifier: raise ValueError('Pinned key identity differs from its bytes.')
        try: Ed25519PublicKey.from_public_bytes(public).verify(sig, DOMAINS[kind] + raw)
        except InvalidSignature: continue
        valid = True
    if not valid: raise ValueError('No signature from an embedded trusted publisher.')
    payload = strict_json(raw)
    if not isinstance(payload, dict) or payload.get('kind') != kind or canonical(payload) != raw:
        raise ValueError('Proof payload is not canonical or has the wrong domain.')
    return payload


def numeric_version(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Invalid release version.')
    return tuple(map(int, value.split('.')))


def common(payload, *, expected_version, expected_source=None):
    if type(payload.get('schema')) is not int or payload['schema'] != 1 or payload.get('repository') != REPOSITORY or payload.get('platform') != PLATFORM:
        raise ValueError('Proof repository, platform or schema differs.')
    if numeric_version(payload.get('version')) != numeric_version(expected_version):
        raise ValueError('Proof version differs.')
    source = payload.get('source_commit')
    if not isinstance(source, str) or not re.fullmatch('[0-9a-f]{40}', source) or expected_source is not None and source != expected_source:
        raise ValueError('Proof source identity differs.')
    if type(payload.get('created_at')) is not int or payload['created_at'] <= 0:
        raise ValueError('Invalid proof creation time.')


def relative_path(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or value.startswith('/'):
        raise ValueError('Invalid proof path.')
    parts = value.split('/')
    if len(parts) > 32 or any(part in ('', '.', '..') or part.endswith((' ', '.')) or
            part.casefold() in ('plugins', '.forge', '.sidekick') or
            any(ord(char) < 32 or char in '\\:*?"<>|' for char in part) or
            re.fullmatch(r'(?i)(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?', part)
            for part in parts):
        raise ValueError('Proof path has an escape or Windows alias.')
    return value


def entries(values, *, field):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_FILES: raise ValueError('Invalid proof entries.')
    result = {}; total = 0
    for value in values:
        if not isinstance(value, dict) or set(value) != {field, 'size', 'sha256'}:
            raise ValueError('Invalid proof entry shape.')
        path = relative_path(value[field])
        if path.casefold() in result: raise ValueError('Duplicate proof paths.')
        if type(value['size']) is not int or not 0 <= value['size'] <= MAX_EXPANDED_BYTES or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
            raise ValueError('Invalid proof entry integrity.')
        total += value['size']
        if total > MAX_EXPANDED_BYTES: raise ValueError('Proof expands beyond its size bound.')
        result[path.casefold()] = value
    return result


def file_hash(path):
    digest = sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()


def plain_root(root):
    root = Path(root).absolute()
    for ancestor in (root, *root.parents):
        if ancestor.exists() or ancestor.is_symlink():
            item = ancestor.lstat()
            if ancestor.is_symlink() or getattr(item, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Proof trees cannot contain links or junctions.')
    if not root.is_dir(): raise ValueError('Proof installation folder is unavailable.')
    return root.resolve()


def tree_files(root, *, wanted=None, installed=False):
    root = plain_root(root)
    values = []; names = set(); total = 0
    for parent, directories, files in os.walk(root, topdown=True, followlinks=False):
        for name in sorted(directories + files):
            path = Path(parent) / name
            item = path.lstat()
            if path.is_symlink() or getattr(item, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Proof trees cannot contain links or junctions.')
            relative = relative_path(path.relative_to(root).as_posix())
            if relative.casefold() in names: raise ValueError('Tree contains duplicate Windows identities.')
            names.add(relative.casefold())
            if path.is_file():
                if wanted is not None:
                    if relative == BUILD_NAME or installed and relative in HOST_OUTPUTS: continue
                    expected = wanted.get(relative.casefold())
                    if expected is None or relative != expected['path'] or item.st_size != expected['size']:
                        raise ValueError('Build files differ from the trusted proof.')
                total += item.st_size
                if len(values) >= MAX_FILES or total > MAX_EXPANDED_BYTES: raise ValueError('Proof tree exceeds its bound.')
                values.append({'path': relative, 'size': item.st_size, 'sha256': file_hash(path)})
    return sorted(values, key=lambda value: value['path'])


def validated_build_manifest(envelope, pinned_keys, *, expected_version, expected_source=None):
    payload = authenticate(envelope, 'forge-build', pinned_keys)
    if set(payload) != {'schema', 'kind', 'repository', 'platform', 'version', 'source_commit', 'created_at', 'files'}:
        raise ValueError('Unknown build proof fields.')
    common(payload, expected_version=expected_version, expected_source=expected_source)
    wanted = entries(payload['files'], field='path')
    if BUILD_NAME.casefold() in wanted or any(name in wanted for name in HOST_OUTPUTS):
        raise ValueError('Build proof cannot include itself or installation-generated files.')
    if not {'forge.exe', 'forgebrowserhost.exe'} <= set(wanted): raise ValueError('Build proof is missing native hosts.')
    return payload


def verify_build(envelope, pinned_keys, *, root, expected_version, expected_source=None, installed=False):
    payload = validated_build_manifest(envelope, pinned_keys, expected_version=expected_version, expected_source=expected_source)
    wanted = entries(payload['files'], field='path')
    root = plain_root(root)
    proof_path = root / BUILD_NAME
    if read_record(proof_path) != envelope: raise ValueError('Installed proof differs from verified proof.')
    actual = tree_files(root, wanted=wanted, installed=installed)
    if {value['path']: value for value in actual} != {value['path']: value for value in wanted.values()}:
        raise ValueError('Build files differ from the trusted proof.')
    return payload


def verify_release(envelope, pinned_keys, *, expected_version, expected_source=None, expected_channel,
                   expected_tag, now, current_version, prior=None):
    payload = authenticate(envelope, 'forge-release', pinned_keys)
    if set(payload) != {'schema', 'kind', 'repository', 'platform', 'version', 'source_commit', 'created_at',
                       'expires_at', 'revision', 'channel', 'tag', 'build_proof_sha256', 'artifacts'}:
        raise ValueError('Unknown release proof fields.')
    common(payload, expected_version=expected_version, expected_source=expected_source)
    if payload['tag'] != expected_tag or payload['tag'] not in (expected_version, 'v' + expected_version) or payload['channel'] != expected_channel or expected_channel not in ('stable', 'preview'):
        raise ValueError('Release channel or tag differs.')
    created, expires = payload['created_at'], payload['expires_at']
    if type(now) is not int or type(expires) is not int or not created <= now < expires or not 0 < expires - created <= MAX_RELEASE_LIFETIME:
        raise ValueError('Release proof is expired or outside its validity window.')
    if type(payload['revision']) is not int or payload['revision'] < 1:
        raise ValueError('Invalid proof revision.')
    if numeric_version(payload['version']) <= numeric_version(current_version):
        raise ValueError('An older or current package cannot be admitted.')
    if prior:
        if not isinstance(prior, dict) or type(prior.get('revision')) is not int or prior['revision'] < 1 or not isinstance(prior.get('proof_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', prior['proof_sha256']):
            raise ValueError('Stored publisher trust record is invalid.')
        newest = numeric_version(prior.get('version')); offered = numeric_version(payload['version'])
        if offered < newest or offered == newest and payload['revision'] < prior['revision']:
            raise ValueError('Previously trusted release metadata cannot be rolled back.')
        if offered == newest and payload['revision'] == prior['revision'] and sha256(envelope).hexdigest() != prior['proof_sha256']:
            raise ValueError('Same proof revision has conflicting identities.')
    digest = payload['build_proof_sha256']
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest): raise ValueError('Invalid build proof identity.')
    artifacts = entries(payload['artifacts'], field='name')
    names = {'Forge-' + expected_version + '-Setup.exe', 'Forge-' + expected_version + '-Portable.zip'}
    if {value['name'] for value in artifacts.values()} != names:
        raise ValueError('Release proof must bind exactly the installer and portable archive.')
    if any(not 0 < value['size'] <= MAX_ARTIFACT_BYTES for value in artifacts.values()):
        raise ValueError('Release artifacts exceed their supported size bound.')
    return payload


def read_record(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            item = part.lstat()
            if part.is_symlink() or getattr(item, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Proof files cannot contain links or junctions.')
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_RECORD_BYTES + 1)
    except OSError as exc:
        raise ValueError('The required publisher proof is unavailable.') from exc
    if not 0 < len(raw) <= MAX_RECORD_BYTES: raise ValueError('Publisher proof exceeds its size bound.')
    return raw


def verify_artifact(path, proof, name):
    values = {value['name']: value for value in proof['artifacts']}
    wanted = values[name]
    if Path(path).stat().st_size != wanted['size'] or file_hash(Path(path)) != wanted['sha256']:
        raise ValueError('Downloaded artifact differs from the trusted release proof.')


def bind_release_build(release, envelope, build):
    if sha256(envelope).hexdigest() != release['build_proof_sha256'] or any(
            release[key] != build[key] for key in ('repository', 'platform', 'version', 'source_commit')):
        raise ValueError('Release and build proofs do not describe the same build.')
