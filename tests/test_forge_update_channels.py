"""Preview detection and non-blocking checks keep artifact trust fail-closed."""
import asyncio
from hashlib import sha256
import threading
from types import SimpleNamespace

import httpx
import pytest

from forge_store import ForgeStore
from forge_updates import MAX_RELEASE_PAGES, REPOSITORY, UpdateManager


PACKAGE = b'Synthetic update installer'
PUBLISHER = 'CN=Synthetic test publisher'


def release(number, *, preview=True, draft=False):
    tag = 'v' + number
    name = 'Forge-' + number + '-Setup.exe'
    return {'tag_name': tag, 'draft': draft, 'prerelease': preview,
            'html_url': 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag,
            'assets': [{'name': name, 'size': len(PACKAGE), 'digest': 'sha256:' + sha256(PACKAGE).hexdigest(),
                        'browser_download_url': 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + name}]}


@pytest.fixture
def updater(tmp_path):
    store = ForgeStore(tmp_path / 'data')
    install = tmp_path / 'Programs/Forge5'
    install.mkdir(parents=True)
    (install / 'Forge.exe').write_bytes(b'Synthetic installed host')
    service = SimpleNamespace(store=store, jobs=SimpleNamespace(lock=threading.RLock()))
    manager = UpdateManager(service, '5.1.0', install, (), verifier=lambda path: {
        'status': 'Valid', 'publisher': PUBLISHER, 'timestamped': True, 'version': '5.1.1'}, proof_policy={}, _allow_installation=True)
    return manager


def feed(manager, values, *, stable=None):
    requests = []
    def response(request):
        requests.append(str(request.url))
        if request.url.path.endswith('/latest'):
            return httpx.Response(200, json=stable or release('4.1.1', preview=False))
        if request.url.path.endswith('/releases'):
            page = int(request.url.params['page'])
            return httpx.Response(200, json=values[(page - 1) * 100:page * 100])
        return httpx.Response(200, content=PACKAGE)
    manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    return requests


def test_preview_channel_finds_next_preview_while_stable_latest_is_older(updater):
    requests = feed(updater, [release('5.1.0'), release('5.1.1'), release('5.3.0', draft=True),
                               release('4.1.1', preview=False)])
    assert updater.check()['state'] == 'current'
    result = updater.dispatch('update_check', {'channel': 'preview'})
    assert result['state'] == 'available' and result['channel'] == 'preview'
    assert result['release']['version'] == '5.1.1' and result['release']['prerelease']
    assert result['release']['html_url'].endswith('/releases/tag/v5.1.1')
    assert type(result['checked_at']) is int
    assert not result['automatic_install_available']
    assert len(requests) == 2 and requests[-1].endswith('/releases?per_page=100&page=1')
    with pytest.raises(ValueError, match='disabled'): updater.download()


def test_preview_channel_also_offers_a_newer_stable_release(updater):
    feed(updater, [release('5.1.1'), release('6.0.0', preview=False), release('5.1.2')])
    result = updater.check('preview')
    assert result['release']['version'] == '6.0.0' and not result['release']['prerelease']


def test_preview_scan_paginates_and_rejects_ambiguous_aliases(updater):
    values = [{'tag_name': 'unsupported-build-' + str(index), 'draft': False} for index in range(100)]
    requests = feed(updater, values + [release('5.1.1')])
    assert updater.check('preview')['release']['version'] == '5.1.1'
    assert len(requests) == 2 and requests[-1].endswith('page=2')
    duplicate = release('5.1.1'); duplicate['tag_name'] = '5.1.1'
    feed(updater, [release('5.1.1'), duplicate])
    with pytest.raises(ValueError, match='ambiguous'): updater.check('preview')


def test_truncated_feed_is_an_error_instead_of_claiming_up_to_date(updater):
    requests = feed(updater, [{'tag_name': 'unsupported', 'draft': False}] * (100 * MAX_RELEASE_PAGES))
    with pytest.raises(ValueError, match='safe check limit'): updater.check('preview')
    assert len(requests) == MAX_RELEASE_PAGES and updater.status()['state'] == 'error'


@pytest.mark.parametrize('change', ['repository', 'digest', 'url', 'duplicate', 'size', 'publication'])
def test_preview_candidates_retain_the_exact_origin_and_integrity_guards(updater, change):
    value = release('5.1.1')
    if change == 'repository': value['html_url'] = 'https://github.com/other/repository/releases/tag/v5.1.1'
    if change == 'digest': value['assets'][0]['digest'] = 'sha256:not-a-digest'
    if change == 'url': value['assets'][0]['browser_download_url'] = 'https://example.invalid/installer.exe'
    if change == 'duplicate': value['assets'] *= 2
    if change == 'size': value['assets'][0]['size'] = True
    if change == 'publication': value['prerelease'] = 'true'
    feed(updater, [value])
    with pytest.raises(ValueError): updater.check('preview')
    assert not updater.status()['verified'] and updater.thread is None


@pytest.mark.parametrize('channel', ['nightly', '', True, None, ['preview']])
def test_invalid_channel_never_contacts_github(updater, channel):
    requests = feed(updater, [])
    with pytest.raises(ValueError, match='channel'): updater.check(channel)
    assert requests == [] and updater.status()['state'] == 'idle'


def staged(updater):
    updater.publishers = (PUBLISHER,)
    feed(updater, [release('5.1.1')])
    updater.check('preview'); updater.download(); updater.thread.join(5)
    assert updater.status()['state'] == 'ready'


def test_periodic_check_preserves_same_verified_package_but_clears_changed_release(updater):
    staged(updater)
    path = updater.status()['downloaded']
    assert updater.check('preview')['state'] == 'ready'
    assert updater.status()['verified'] and updater.status()['downloaded'] == path
    feed(updater, [release('5.1.2')])
    result = updater.check('preview')
    assert result['state'] == 'available' and not result['verified']
    assert result['downloaded'] is None and result['signature'] is None
    assert result['received_bytes'] == 0


def test_channel_switch_to_current_clears_old_installer_state(updater):
    staged(updater)
    result = updater.check('stable')
    assert result['state'] == 'current' and result['release'] is None
    assert not result['verified'] and result['signature'] is None and result['downloaded'] is None
    with pytest.raises(ValueError): updater.apply()


def test_failed_feed_check_disarms_a_previously_verified_installer(updater):
    staged(updater)
    updater.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'unexpected': 'object'})), trust_env=False)
    with pytest.raises(ValueError, match='feed'): updater.check('preview')
    assert updater.status()['state'] == 'error' and not updater.status()['verified']
    with pytest.raises(ValueError, match='verify'): updater.apply()


def test_restart_does_not_rearm_interrupted_check_or_offer_downgrade(updater):
    staged(updater)
    updater._save(state='checking')
    restarted = UpdateManager(updater.service, '5.1.0', updater.install_dir, (PUBLISHER,))
    assert restarted.status()['state'] == 'interrupted' and not restarted.status()['verified']
    updater._save(state='ready', verified=True)
    newer = UpdateManager(updater.service, '5.1.1', updater.install_dir, (PUBLISHER,))
    assert newer.status()['state'] == 'current' and newer.status()['release'] is None
    assert newer.status()['downloaded'] is None and not newer.status()['verified']


def test_background_check_returns_immediately_and_blocks_competing_install(updater):
    staged(updater)
    started, release_read = threading.Event(), threading.Event()
    async def response(request):
        started.set()
        while not release_read.is_set(): await asyncio.sleep(.01)
        return httpx.Response(200, json=[release('5.1.1')])
    updater.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    try:
        result = updater.dispatch('update_check', {'channel': 'preview', 'background': True})
        assert result['state'] == 'checking' and started.wait(2)
        with pytest.raises(ValueError, match='active'): updater.apply()
        with pytest.raises(ValueError, match='active'): updater.rollback()
        with pytest.raises(ValueError, match='active'): updater.check_background('preview')
        with pytest.raises(ValueError, match='active'): updater.download()
        assert not updater.applying
    finally:
        release_read.set(); updater.thread.join(5)
    assert updater.status()['state'] == 'ready' and updater.status()['verified']


def test_background_shutdown_cancels_stalled_metadata_without_executing(updater):
    started = threading.Event()
    async def response(request):
        started.set(); await asyncio.sleep(20)
        return httpx.Response(200, json=[release('5.1.1')])
    updater.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    updater.check_background('preview')
    assert started.wait(2)
    updater.shutdown()
    assert not updater.thread.is_alive()
    assert updater.status()['state'] == 'cancelled' and not updater.status()['verified']
    assert updater.status()['error'] == 'Update check cancelled.'


def test_cancellation_after_metadata_read_cannot_rearm_ready_package(updater, monkeypatch):
    import forge_updates
    staged(updater)
    validating, finish = threading.Event(), threading.Event()
    original = forge_updates.release_metadata
    def validate(value):
        validating.set(); assert finish.wait(5)
        return original(value)
    monkeypatch.setattr(forge_updates, 'release_metadata', validate)
    updater.check_background('preview')
    try:
        assert validating.wait(2)
        updater.cancel()
    finally:
        finish.set(); updater.thread.join(5)
    assert updater.status()['state'] == 'cancelled' and not updater.status()['verified']


def test_cancellation_during_signature_check_cannot_publish_ready_installer(updater):
    updater.publishers = (PUBLISHER,)
    feed(updater, [release('5.1.1')]); updater.check('preview')
    verifying, finish = threading.Event(), threading.Event()
    original = updater.verifier
    def verify(path):
        verifying.set(); assert finish.wait(5)
        return original(path)
    updater.verifier = verify
    updater.download()
    try:
        assert verifying.wait(2)
        updater.cancel()
    finally:
        finish.set(); updater.thread.join(5)
    assert updater.status()['state'] == 'cancelled' and not updater.status()['verified']
    assert not list(updater.root.rglob('installer.exe'))


def test_background_argument_is_a_boolean(updater):
    requests = feed(updater, [])
    with pytest.raises(ValueError, match='Background'):
        updater.dispatch('update_check', {'background': 'true'})
    assert requests == []


def test_status_dispatch_exposes_operation_failures_without_a_transport_error(updater):
    updater._save(state='error', error='Synthetic network failure')
    result = updater.dispatch('update_status')
    assert result['state'] == 'error' and result['operation_error'] == 'Synthetic network failure'
    assert 'error' not in result
    assert updater.status()['error'] == 'Synthetic network failure'
