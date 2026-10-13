"""Publisher-verified updates use ephemeral keys, inert files and synthetic HTTP."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import subprocess
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from forge_store import ForgeStore
import forge_update_proof as proof
from forge_updates import UpdateManager, helper_main, load_operation, retire_obsolete_files


OLD_SOURCE, NEW_SOURCE = '1' * 40, '2' * 40
PACKAGE = b'Synthetic unsigned installer bytes, never executed'


def sign(payload, private):
    public = private.public_key().public_bytes_raw()
    return proof.envelope(payload, [{'key_id': proof.key_id(public), 'signature': base64.b64encode(
        private.sign(proof.DOMAINS[payload['kind']] + proof.canonical(payload))).decode('ascii')}])


def seal(root, private, number, source, now):
    root.mkdir(parents=True)
    (root / '_internal').mkdir()
    (root / 'Forge.exe').write_bytes(b'Synthetic host ' + number.encode())
    (root / 'ForgeBrowserHost.exe').write_bytes(b'Synthetic browser ' + number.encode())
    (root / '_internal/runtime.dll').write_bytes(b'Synthetic runtime ' + number.encode())
    payload = dict(schema=1, kind='forge-build', repository=proof.REPOSITORY, platform=proof.PLATFORM,
        version=number, source_commit=source, created_at=now, files=proof.tree_files(root))
    raw = sign(payload, private)
    (root / proof.BUILD_NAME).write_bytes(raw)
    return raw


@pytest.fixture
def fixture(tmp_path):
    now = int(time.time())
    private = Ed25519PrivateKey.generate(); public = private.public_key().public_bytes_raw()
    pins = {proof.key_id(public): public}
    app, new_app = tmp_path / 'Programs/Forge5', tmp_path / 'new-app'
    seal(app, private, '5.1.2', OLD_SOURCE, now)
    build_raw = seal(new_app, private, '5.1.3', NEW_SOURCE, now)
    payload = dict(schema=1, kind='forge-release', repository=proof.REPOSITORY, platform=proof.PLATFORM,
        version='5.1.3', source_commit=NEW_SOURCE, created_at=now, expires_at=now + 3600,
        revision=1, channel='preview', tag='v5.1.3', build_proof_sha256=sha256(build_raw).hexdigest(),
        artifacts=[dict(name='Forge-5.1.3-Setup.exe', size=len(PACKAGE), sha256=sha256(PACKAGE).hexdigest()),
                   dict(name='Forge-5.1.3-Portable.zip', size=10, sha256=sha256(b'Synthetic!').hexdigest())])
    raw = sign(payload, private)
    base = 'https://github.com/' + proof.REPOSITORY + '/releases'
    metadata = {'tag_name': 'v5.1.3', 'target_commitish': NEW_SOURCE, 'html_url': base + '/tag/v5.1.3', 'draft': False, 'prerelease': True,
        'assets': [{'name': 'Forge-5.1.3-Setup.exe', 'size': len(PACKAGE), 'digest': 'sha256:' + sha256(PACKAGE).hexdigest(),
                    'browser_download_url': base + '/download/v5.1.3/Forge-5.1.3-Setup.exe'},
                   {'name': proof.RELEASE_NAME, 'size': len(raw), 'digest': 'sha256:' + sha256(raw).hexdigest(),
                    'browser_download_url': base + '/download/v5.1.3/' + proof.RELEASE_NAME}]}
    store = ForgeStore(tmp_path / 'data')
    service = SimpleNamespace(store=store, jobs=SimpleNamespace(lock=threading.RLock()))
    value = SimpleNamespace(private=private, pins=pins, app=app, new_app=new_app, raw=raw, payload=payload,
        metadata=metadata, now=now, requests=[])
    def response(request):
        value.requests.append(str(request.url))
        if request.url.path.endswith('/releases'): return httpx.Response(200, json=[value.metadata])
        if request.url.path.endswith('/' + proof.RELEASE_NAME): return httpx.Response(200, content=value.raw)
        return httpx.Response(200, content=PACKAGE)
    value.factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    value.manager = UpdateManager(service, '5.1.2', app, (), value.factory,
        lambda path: pytest.fail('Publisher proofs do not substitute fake Windows signatures'), proof_policy=pins, _allow_installation=True)
    yield value
    value.manager.shutdown()


def replace_record(value, payload=None, private=None):
    value.payload = payload or value.payload
    value.raw = sign(value.payload, private or value.private)
    value.metadata['assets'][1].update(size=len(value.raw), digest='sha256:' + sha256(value.raw).hexdigest())


def ready(value):
    result = value.manager.check('preview')
    assert result['release']['trust_mode'] == 'ed25519'
    value.manager.download(); value.manager.thread.join(5)
    assert value.manager.status()['state'] == 'ready'


def prepared(value, monkeypatch):
    ready(value)
    result = value.manager.apply()
    monkeypatch.setattr('forge_updates.psutil.pid_exists', lambda pid: False)
    return load_operation(value.manager.home, result['operation_id'])


def installer(value):
    def run(arguments):
        for source in value.new_app.rglob('*'):
            if source.is_file(): (value.app / source.relative_to(value.new_app)).write_bytes(source.read_bytes())
        (value.app / 'unins000.exe').write_bytes(b'Synthetic generated uninstaller, never executed')
        (value.app / 'unins000.dat').write_bytes(b'Synthetic generated uninstall log')
        return SimpleNamespace(returncode=0)
    return run


def hashed_assets(value):
    for root, name, data in ((value.app, 'old-hash.js', b'Old authenticated frontend'),
                              (value.new_app, 'new-hash.js', b'New authenticated frontend')):
        folder = root / '_internal/frontend/assets'; folder.mkdir(parents=True)
        (folder / name).write_bytes(data)
        number, source = ('5.1.2', OLD_SOURCE) if root == value.app else ('5.1.3', NEW_SOURCE)
        payload = dict(schema=1, kind='forge-build', repository=proof.REPOSITORY, platform=proof.PLATFORM,
            version=number, source_commit=source, created_at=value.now,
            files=[entry for entry in proof.tree_files(root) if entry['path'] != proof.BUILD_NAME])
        (root / proof.BUILD_NAME).write_bytes(sign(payload, value.private))
    updated = deepcopy(value.payload)
    updated['build_proof_sha256'] = sha256((value.new_app / proof.BUILD_NAME).read_bytes()).hexdigest()
    replace_record(value, updated)
    old = proof.validated_build_manifest(proof.read_record(value.app / proof.BUILD_NAME), value.pins, expected_version='5.1.2')
    new = proof.validated_build_manifest(proof.read_record(value.new_app / proof.BUILD_NAME), value.pins, expected_version='5.1.3')
    return old, new


def test_proof_update_download_prepare_install_and_rollback_preserve_trust(fixture, monkeypatch):
    value = fixture; data = prepared(value, monkeypatch)
    assert data['trust_mode'] == 'ed25519' and data['previous_source_commit'] == OLD_SOURCE
    assert 'release_proof' not in value.manager.status()
    outcome = helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=installer(value))
    assert outcome['installed'] and outcome['version'] == '5.1.3'
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins, _allow_installation=True)
    # New authenticated observations are updater bookkeeping, but cannot be
    # forgotten when an explicit rollback restores the previous SQLite backup.
    newest = dict(id='preview', version='5.1.4', revision=1, proof_sha256='3' * 64)
    newest = restarted.store.save_entity('update_trust', newest)
    accepted = restarted.rollback(); data = load_operation(restarted.home, accepted['operation_id'])
    restored = helper_main(restarted.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=lambda args: pytest.fail('Rollback never runs an installer'))
    assert restored['rolled_back'] and restored['version'] == '5.1.2'
    assert restarted.store.entity('update_trust', 'preview') == newest
    proof.verify_build(proof.read_record(value.app / proof.BUILD_NAME), value.pins,
        root=value.app, expected_version='5.1.2', expected_source=OLD_SOURCE, installed=True)


@pytest.mark.parametrize('change', ['wrong-key', 'tamper', 'expiry', 'source', 'platform', 'repo', 'artifact', 'duplicate-asset'])
def test_failed_proof_is_visible_manual_release_and_never_downloads(fixture, change):
    value = fixture
    if change == 'wrong-key': replace_record(value, private=Ed25519PrivateKey.generate())
    elif change == 'tamper':
        outer = json.loads(value.raw); outer['payload'] = base64.b64encode(b'{"kind":"forge-release"}').decode()
        value.raw = proof.canonical(outer)
        value.metadata['assets'][1].update(size=len(value.raw), digest='sha256:' + sha256(value.raw).hexdigest())
    elif change == 'duplicate-asset': value.metadata['assets'].append(dict(value.metadata['assets'][1]))
    else:
        changed = deepcopy(value.payload)
        if change == 'expiry': changed['expires_at'] = value.now
        if change == 'source': changed['source_commit'] = '3' * 40
        if change == 'platform': changed['platform'] = 'linux-x64'
        if change == 'repo': changed['repository'] = 'other/project'
        if change == 'artifact': changed['artifacts'][0]['sha256'] = '0' * 64
        replace_record(value, changed)
    result = value.manager.check('preview')
    assert result['state'] == 'available' and result['release']['trust_mode'] == 'manual'
    assert result['release']['verification_error'] and not result['automatic_install_available']
    with pytest.raises(ValueError, match='publisher'): value.manager.download()
    assert not any(url.endswith('Setup.exe') for url in value.requests)
    assert value.manager.thread is None


def test_missing_proof_does_not_enable_automatic_execution_from_hashes(fixture):
    value = fixture; value.metadata['assets'].pop()
    result = value.manager.check('preview')
    assert result['release']['trust_mode'] == 'manual' and not result['automatic_install_available']
    with pytest.raises(ValueError, match='publisher'): value.manager.download()
    value.manager.state.update(verified=True, downloaded=str(value.app / 'Forge.exe'))
    with pytest.raises(ValueError, match='publisher'): value.manager.apply()


def test_proof_cache_tampering_and_expiry_block_download_and_apply(fixture, monkeypatch):
    value = fixture; ready(value)
    descriptor = value.manager.state['release_proof']
    path = value.manager.root / 'proofs' / (descriptor['sha256'] + '.json')
    path.write_bytes(b'Modified publisher proof')
    with pytest.raises(ValueError, match='modified'): value.manager.apply()
    path.write_bytes(value.raw)
    monkeypatch.setattr('forge_updates.time.time', lambda: value.payload['expires_at'])
    with pytest.raises(ValueError, match='expired'): value.manager.apply()
    with pytest.raises(ValueError, match='expired'): value.manager.download()
    assert not value.manager.applying


def test_restart_revalidates_cache_and_never_takes_trust_from_settings(fixture):
    value = fixture; ready(value)
    restarted = UpdateManager(value.manager.service, '5.1.2', value.app, (), proof_policy=value.pins, _allow_installation=True)
    assert restarted.status()['state'] == 'ready'
    restarted.store.save_entity('providers', {'id': 'untrusted', 'public_key': 'supplied in settings'})
    revoked = UpdateManager(value.manager.service, '5.1.2', value.app, (), proof_policy={}, _allow_installation=True)
    assert revoked.status()['state'] == 'error' and not revoked.status()['verified']
    with pytest.raises(ValueError, match='publisher'): revoked.apply()
    assert not revoked.applying


def test_restart_disarms_an_expired_cached_proof(fixture, monkeypatch):
    value = fixture; ready(value)
    monkeypatch.setattr('forge_updates.time.time', lambda: value.payload['expires_at'])
    restarted = UpdateManager(value.manager.service, '5.1.2', value.app, (), proof_policy=value.pins, _allow_installation=True)
    assert restarted.status()['state'] == 'error' and not restarted.status()['verified']
    assert 'Check for updates again' in restarted.status()['error']


def test_completed_upgrade_clears_same_version_offer_but_retains_rollback(fixture, monkeypatch):
    value = fixture; data = prepared(value, monkeypatch)
    helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=installer(value))
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins, _allow_installation=True)
    status = restarted.status()
    assert status['state'] == 'installed' and status['release'] is None
    assert status['operation_id'] == data['id']


@pytest.mark.parametrize('change', ['revision', 'conflict', 'feed'])
def test_observed_publisher_records_cannot_be_replayed(fixture, change):
    value = fixture; value.manager.check('preview')
    if change == 'revision':
        advanced = deepcopy(value.payload); advanced['revision'] = 2
        replace_record(value, advanced); value.manager.check('preview')
        stale = deepcopy(advanced); stale['revision'] = 1; replace_record(value, stale)
    elif change == 'conflict':
        altered = deepcopy(value.payload); altered['expires_at'] += 1; replace_record(value, altered)
    else:
        value.metadata = dict(value.metadata, tag_name='v5.1.2', html_url=value.metadata['html_url'].replace('5.1.3', '5.1.2'))
    if change == 'feed':
        with pytest.raises(ValueError, match='older than'): value.manager.check('preview')
    else:
        assert value.manager.check('preview')['release']['trust_mode'] == 'manual'
    assert not value.manager.status()['verified']


@pytest.mark.parametrize('change', ['current-host', 'current-extra', 'backup-host', 'cache', 'expired', 'live-upgrade', 'live-extra'])
def test_old_current_and_prepared_bytes_are_rechecked_before_helper_execution(fixture, monkeypatch, change):
    value = fixture
    if change.startswith('current'):
        ready(value)
        (value.app / ('Forge.exe' if change == 'current-host' else 'unknown.exe')).write_bytes(b'Tampered')
        with pytest.raises(ValueError): value.manager.apply()
        assert not value.manager.applying
        return
    data = prepared(value, monkeypatch)
    if change == 'backup-host': (Path(data['backup']) / 'installation/Forge.exe').write_bytes(b'Tampered')
    if change == 'cache': (value.manager.root / 'proofs' / (data['release_proof']['sha256'] + '.json')).write_bytes(b'Tampered')
    if change == 'expired': monkeypatch.setattr('forge_updates.time.time', lambda: value.payload['expires_at'])
    if change == 'live-extra': (value.app / 'unknown-after-prepare.dll').write_bytes(b'Unknown')
    if change == 'live-upgrade':
        newer = value.app.parent / 'newer-build'; seal(newer, value.private, '5.1.4', '4' * 40, value.now)
        for source in newer.rglob('*'):
            if source.is_file(): (value.app / source.relative_to(newer)).write_bytes(source.read_bytes())
    with pytest.raises(ValueError):
        helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
            proof_policy=value.pins, runner=lambda args: pytest.fail('Must not execute an unverified installer'))


def test_inconsistent_new_build_is_restored_before_restart(fixture, monkeypatch):
    value = fixture; data = prepared(value, monkeypatch)
    def run(arguments):
        installer(value)(arguments)
        (value.app / '_internal/runtime.dll').write_bytes(b'Wrong runtime')
        return SimpleNamespace(returncode=0)
    result = helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=run)
    assert result['rolled_back'] and result['version'] == '5.1.2'
    proof.verify_build(proof.read_record(value.app / proof.BUILD_NAME), value.pins,
        root=value.app, expected_version='5.1.2', installed=True)


def test_user_work_during_failed_proof_install_still_blocks_state_rewind(fixture, monkeypatch):
    value = fixture; data = prepared(value, monkeypatch)
    def run(arguments):
        installer(value)(arguments)
        value.manager.store.create_chat(title='Synthetic newer work')
        (value.app / 'unknown.dll').write_bytes(b'Unexpected install output')
        return SimpleNamespace(returncode=0)
    with pytest.raises(ValueError, match='newer user work'):
        helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
            proof_policy=value.pins, runner=run)
    assert load_operation(value.manager.home, data['id'])['state'] == 'rollback_blocked'
    assert len(value.manager.store.list_chats()) == 1


def test_corrupt_trust_record_cannot_silently_reset_replay_protection(fixture):
    value = fixture; value.manager.check('preview')
    with value.manager.store._connection(transaction='write') as db:
        db.execute("UPDATE entities SET data='malformed-json' WHERE kind='update_trust' AND id='preview'")
    with pytest.raises(ValueError, match='trust record'): value.manager.check('preview')
    assert not value.manager.status()['verified']


def test_cancel_during_publisher_proof_read_stops_cache_admission(fixture):
    import asyncio
    value = fixture; started = threading.Event()
    async def response(request):
        if request.url.path.endswith('/releases'): return httpx.Response(200, json=[value.metadata])
        started.set(); await asyncio.sleep(20)
        return httpx.Response(200, content=value.raw)
    value.manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    value.manager.check_background('preview')
    assert started.wait(2)
    value.manager.shutdown()
    assert not value.manager.thread.is_alive() and value.manager.status()['state'] == 'cancelled'
    assert value.manager._trusted_release('preview') is None
    assert not (value.manager.root / 'proofs').exists()


def test_signed_revision_renewal_preserves_verified_artifact_but_rebinds_proof(fixture):
    value = fixture; ready(value)
    package = value.manager.status()['downloaded']
    previous = value.manager.state['release_proof']['sha256']
    renewed = deepcopy(value.payload); renewed['revision'] += 1; renewed['expires_at'] += 3600
    replace_record(value, renewed)
    result = value.manager.check('preview')
    assert result['state'] == 'ready' and result['verified'] and result['downloaded'] == package
    assert value.manager.state['release_proof']['sha256'] != previous
    value.manager._package()


def test_helper_retires_only_unchanged_old_authenticated_asset(fixture, monkeypatch):
    value = fixture; hashed_assets(value); data = prepared(value, monkeypatch)
    backup_asset = Path(data['backup']) / 'installation/_internal/frontend/assets/old-hash.js'
    before = backup_asset.read_bytes()
    outcome = helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=installer(value))
    assert outcome['installed']
    assert not (value.app / '_internal/frontend/assets/old-hash.js').exists()
    assert (value.app / '_internal/frontend/assets/new-hash.js').read_bytes() == b'New authenticated frontend'
    assert backup_asset.read_bytes() == before
    assert (value.app / 'unins000.exe').is_file() and (value.app / 'unins000.dat').is_file()


def test_rollback_restores_clean_old_inventory_and_retains_entire_new_folder(fixture, monkeypatch):
    value = fixture; hashed_assets(value); data = prepared(value, monkeypatch)
    helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=installer(value))
    restarted = UpdateManager(value.manager.service, '5.1.3', value.app, (), proof_policy=value.pins, _allow_installation=True)
    accepted = restarted.rollback(); data = load_operation(restarted.home, accepted['operation_id'])
    outcome = helper_main(restarted.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=lambda args: pytest.fail('Rollback must not execute an installer'))
    assert outcome['rolled_back']
    assert (value.app / '_internal/frontend/assets/old-hash.js').read_bytes() == b'Old authenticated frontend'
    assert not (value.app / '_internal/frontend/assets/new-hash.js').exists()
    failed = next(Path(data['backup']).glob('failed-installation-*'))
    assert (failed / '_internal/frontend/assets/new-hash.js').read_bytes() == b'New authenticated frontend'
    assert (failed / 'unins000.exe').is_file()
    proof.verify_build(proof.read_record(value.app / proof.BUILD_NAME), value.pins,
        root=value.app, expected_version='5.1.2', expected_source=OLD_SOURCE, installed=True)


@pytest.mark.parametrize('mutation', ['modified', 'unknown', 'missing-new'])
def test_retirement_preflight_aborts_without_any_deletion(fixture, mutation):
    value = fixture; old, new = hashed_assets(value); installer(value)([])
    obsolete = value.app / '_internal/frontend/assets/old-hash.js'
    if mutation == 'modified': obsolete.write_bytes(b'Modified obsolete asset')
    if mutation == 'unknown': (value.app / '_internal/frontend/assets/orphan.js').write_bytes(b'Unlisted obsolete bytes')
    if mutation == 'missing-new': (value.app / '_internal/frontend/assets/new-hash.js').unlink()
    before = obsolete.read_bytes()
    with pytest.raises(ValueError): retire_obsolete_files(value.app, old, new)
    assert obsolete.read_bytes() == before


@pytest.mark.parametrize('mutation', ['wrong-key', 'mixed-proof'])
def test_invalid_or_mixed_new_proof_cannot_retire_files(fixture, monkeypatch, mutation):
    value = fixture; hashed_assets(value); data = prepared(value, monkeypatch)
    old_path = value.app / '_internal/frontend/assets/old-hash.js'
    deletions = []; original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == old_path: deletions.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    def run(arguments):
        installer(value)(arguments)
        raw = proof.read_record(value.new_app / proof.BUILD_NAME)
        payload = proof.validated_build_manifest(raw, value.pins, expected_version='5.1.3')
        if mutation == 'mixed-proof': payload['created_at'] += 1
        (value.app / proof.BUILD_NAME).write_bytes(sign(payload,
            Ed25519PrivateKey.generate() if mutation == 'wrong-key' else value.private))
        return SimpleNamespace(returncode=0)
    outcome = helper_main(value.manager.home, data['id'], data['parent_pid'], install_dir=value.app,
        proof_policy=value.pins, runner=run)
    assert outcome['rolled_back'] and deletions == []
    assert old_path.read_bytes() == b'Old authenticated frontend'


def test_missing_obsolete_asset_is_harmless_and_parent_directories_are_retained(fixture):
    value = fixture; old, new = hashed_assets(value); installer(value)([])
    folder = value.app / '_internal/frontend/assets'
    (folder / 'old-hash.js').unlink()
    retire_obsolete_files(value.app, old, new)
    assert folder.is_dir() and (folder / 'new-hash.js').is_file()


def test_retirement_rejects_a_real_windows_junction_without_unlinking(fixture, tmp_path):
    value = fixture; old, new = hashed_assets(value); installer(value)([])
    outside = tmp_path / 'outside-fixture'; outside.mkdir()
    (outside / 'sentinel.js').write_bytes(b'Outside synthetic data')
    junction = value.app / '_internal/frontend/assets/outside-link'
    if os.name == 'nt':
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            'New-Item -ItemType Junction -Path $env:FORGE_FIXTURE_LINK -Target $env:FORGE_FIXTURE_TARGET | Out-Null'],
            env={**os.environ, 'FORGE_FIXTURE_LINK': str(junction), 'FORGE_FIXTURE_TARGET': str(outside)},
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0 and junction.is_junction()
    else:
        junction.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='links|junctions'): retire_obsolete_files(value.app, old, new)
        assert (value.app / '_internal/frontend/assets/old-hash.js').read_bytes() == b'Old authenticated frontend'
        assert (outside / 'sentinel.js').read_bytes() == b'Outside synthetic data'
    finally:
        if os.name == 'nt': junction.rmdir()
        else: junction.unlink()
