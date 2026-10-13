"""The publisher seed verifies downloads while installation stays fail-closed."""
import pytest
from pathlib import Path
from forge_service import ForgeService
from forge_updates import UpdateManager

from test_forge_publisher_updates import fixture, ready
from test_forge_updates import PUBLISHER, setup as ca_fixture, signature
from test_forge_core import Engine


def test_changed_trust_mode_cannot_relabel_a_previously_verified_installer(fixture):
    value = fixture
    value.manager.publishers = ('CN=Synthetic CA publisher',)
    ready(value)
    assert value.manager.status()['signature']['mode'] == 'ed25519'
    value.metadata['assets'].pop()
    result = value.manager.check('preview')
    assert result['release']['trust_mode'] == 'authenticode'
    assert result['state'] == 'available' and not result['verified']
    assert result['downloaded'] is None and result['signature'] is None


def test_certificate_to_publisher_mode_also_requires_fresh_download_evidence(fixture):
    value = fixture
    value.manager.publishers = ('CN=Synthetic CA publisher',)
    value.manager.verifier = lambda path: dict(status='Valid', publisher=value.manager.publishers[0],
                                               timestamped=True, version='5.1.3')
    asset = value.metadata['assets'].pop()
    assert value.manager.check('preview')['release']['trust_mode'] == 'authenticode'
    value.manager.download(); value.manager.thread.join(5)
    assert value.manager.status()['verified']
    value.metadata['assets'].append(asset)
    result = value.manager.check('preview')
    assert result['release']['trust_mode'] == 'ed25519'
    assert result['state'] == 'available' and not result['verified'] and result['signature'] is None


@pytest.mark.parametrize('signature', [None, [], {'mode': 'ed25519', 'version': '5.1.4'}, {'status': 'Valid', 'version': '5.1.3'}])
def test_same_artifact_needs_compatible_signature_metadata_before_preserving_ready(fixture, signature):
    value = fixture; ready(value)
    value.manager._save(signature=signature)
    result = value.manager.check('preview')
    assert result['state'] == 'available' and not result['verified'] and result['signature'] is None


def test_default_seed_can_verify_a_publisher_download_but_never_prepare_install(fixture):
    value = fixture
    manager = UpdateManager(value.manager.service, install_dir=value.app, trust_policy=(),
                            client_factory=value.factory, proof_policy=value.pins)
    assert manager.current_version == '5.1.2'
    assert manager.check('preview')['verified_download_available']
    manager.download(); manager.thread.join(5)
    status = manager.status()
    assert status['state'] == 'ready' and status['verified']
    assert status['verified_download_available'] and not status['automatic_install_available']
    assert status['install_block_reason'] and not manager.applying
    before = manager.store.entity('updates', 'current')
    for action in ('update_apply', 'update_rollback'):
        with pytest.raises(ValueError, match='unavailable in this version'):
            manager.dispatch(action, {'_allow_installation': True, 'native_ready': True})
    assert manager.store.entity('updates', 'current') == before
    assert not list((manager.home / 'backups').glob('update-*'))
    assert not (manager.root / 'operations').exists() and not manager.applying
    manager.shutdown()


def test_default_seed_preserves_windows_certificate_download_compatibility(ca_fixture):
    original, app = ca_fixture
    manager = UpdateManager(original.service, '4.1.1', app, (PUBLISHER,), original.client_factory,
                            signature, proof_policy={})
    assert manager.check()['verified_download_available']
    manager.download(); manager.thread.join(5)
    assert manager.status()['verified'] and manager.status()['signature']['status'] == 'Valid'
    assert not manager.status()['automatic_install_available']
    for method in (manager.apply, manager.rollback):
        with pytest.raises(ValueError, match='unavailable in this version'): method()
    assert not (manager.root / 'operations').exists() and not manager.applying
    manager.shutdown()


@pytest.mark.parametrize('action', ['update_apply', 'update_rollback'])
def test_real_service_dispatch_cannot_enable_installation_from_browser_or_settings(tmp_path, monkeypatch, action):
    import forge_credentials
    class FakeVault:
        def __init__(self, *args, **kwargs): pass
        def get(self, reference): pytest.fail('No user credentials are needed for seed gating')
    monkeypatch.setattr(forge_credentials, 'CredentialVault', FakeVault)
    service = ForgeService(core=Engine(), data_dir=tmp_path / 'synthetic-data')
    service.store.save_entity('providers', {'id': 'untrusted', '_allow_installation': True})
    status = service.dispatch('update_status')
    assert status['current_version'] == '5.1.2' and not status['automatic_install_available']
    with pytest.raises(ValueError, match='unavailable in this version'):
        service.dispatch(action, {'_allow_installation': True, 'native_ready': True, 'source': 'native'})
    assert not service.update_manager.applying
    assert not (service.update_manager.root / 'operations').exists()
    assert not list((service.store.home / 'backups').glob('update-*'))
    service.update_manager.shutdown()


@pytest.mark.parametrize('flag', [1, 'true', None])
def test_internal_fixture_capability_requires_a_real_boolean(fixture, flag):
    with pytest.raises(ValueError, match='internal installation capability'):
        UpdateManager(fixture.manager.service, _allow_installation=flag)


@pytest.mark.parametrize('failure', ['tamper', 'expiry', 'revoked-pin'])
def test_invalid_cached_proof_restart_disarms_public_evidence_but_retains_private_files(fixture, monkeypatch, failure):
    value = fixture; ready(value)
    descriptor = value.manager.state['release_proof']
    proof_path = value.manager.root / 'proofs' / (descriptor['sha256'] + '.json')
    package_path = Path(value.manager.state['downloaded'])
    package_before = package_path.read_bytes()
    trust_before = value.manager.store.entity('update_trust', 'preview')
    if failure == 'tamper': proof_path.write_bytes(b'Synthetic proof tampering')
    if failure == 'expiry': monkeypatch.setattr('forge_updates.time.time', lambda: value.payload['expires_at'])
    pins = {} if failure == 'revoked-pin' else value.pins
    manager = UpdateManager(value.manager.service, install_dir=value.app, trust_policy=(), proof_policy=pins)
    status = manager.status()
    assert status['state'] == 'error' and status['release']['trust_mode'] == 'manual'
    assert status['release']['verification_error'] == 'The cached publisher proof is no longer valid. Check for updates again.'
    assert not status['verified'] and not status['verified_download_available']
    assert not status['automatic_install_available'] and status['downloaded'] is None and status['signature'] is None
    assert manager.state['release_proof'] is None and status['received_bytes'] == 0
    assert package_path.read_bytes() == package_before and proof_path.exists()
    assert manager.store.entity('update_trust', 'preview') == trust_before
    with pytest.raises(ValueError, match='publisher'): manager.download()


def test_fresh_authenticated_check_recovers_from_invalid_cached_proof(fixture):
    value = fixture; ready(value)
    digest = value.manager.state['release_proof']['sha256']
    (value.manager.root / 'proofs' / (digest + '.json')).write_bytes(b'Synthetic proof tampering')
    manager = UpdateManager(value.manager.service, install_dir=value.app, trust_policy=(),
                            client_factory=value.factory, proof_policy=value.pins)
    assert manager.status()['release']['trust_mode'] == 'manual'
    status = manager.check('preview')
    assert status['state'] == 'available' and status['release']['trust_mode'] == 'ed25519'
    assert 'verification_error' not in status['release'] and status['verified_download_available']
    manager.download(); manager.thread.join(5)
    assert manager.status()['state'] == 'ready' and manager.status()['verified']
    manager.shutdown()


def test_unknown_operation_evidence_is_not_cleared_by_seed_cache_invalidation(fixture):
    value = fixture; ready(value)
    value.manager._save(operation_id='missing-operation-evidence')
    downloaded = value.manager.state['downloaded']
    descriptor = dict(value.manager.state['release_proof'])
    manager = UpdateManager(value.manager.service, install_dir=value.app, trust_policy=(), proof_policy=value.pins)
    status = manager.status()
    assert status['inspection_required'] and status['operation_id'] == 'missing-operation-evidence'
    assert status['downloaded'] == downloaded and manager.state['release_proof'] == descriptor
    assert not status['verified_download_available'] and not status['automatic_install_available']
