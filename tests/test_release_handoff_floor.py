"""Disposable trust-floor smoke covers both feature states without update authority."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import desktop
from forge_native_handoff import NativeAuthority
from forge_service import ForgeService
from forge_updates import UpdateManager
from test_forge_core import Engine


@pytest.fixture
def floor(tmp_path, monkeypatch):
    import forge_credentials
    import forge_native_handoff
    import httpx

    def forbidden(*args, **kwargs):
        pytest.fail('No network, credentials, preparation, helper or restart in a trust-floor fixture')

    class FakeVault:
        def __init__(self, *args, **kwargs): pass
        get = put = delete = forbidden

    monkeypatch.setattr(forge_credentials, 'CredentialVault', FakeVault)
    home = tmp_path / 'disposable' / 'state'
    monkeypatch.setenv('FORGE_DATA_DIR', str(home))
    service = ForgeService(core=Engine(), data_dir=home)
    service.store.update_settings({'update_checks': False})
    unsigned = tmp_path / 'unsigned-app'
    unsigned.mkdir()
    manager = UpdateManager(service, install_dir=unsigned, trust_policy=(), proof_policy={})
    service.update_manager = manager
    service.update_editor_guard = lambda: True
    service.update_native_input_guard = lambda: True
    api = desktop.Bridge(service)
    api._ensure_service = lambda: service
    api._window = SimpleNamespace(uid='synthetic-main', _js_api=api,
        evaluate_js=lambda script: {'installed': True, 'invalid_attempt_rejected': True, 'phase': 'idle', 'participants': 2})
    api._server_url = 'http://127.0.0.1:12345'
    api._trusted_loaded = True
    form = SimpleNamespace(InvokeRequired=False, webview=SimpleNamespace(Source=api._server_url))
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms',
        SimpleNamespace(BrowserView=SimpleNamespace(instances={api._window.uid: form})))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda callback: callback))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(httpx, 'Client', forbidden)
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(manager, '_prepare_apply', forbidden)
    monkeypatch.setattr(manager, '_prepare_rollback', forbidden)
    monkeypatch.setattr(forge_native_handoff.HelperTransport, '__init__', forbidden)
    monkeypatch.setattr(api, '_handoff_depart', forbidden)
    monkeypatch.setattr(api, 'quit', forbidden)
    try:
        yield api, service, manager
    finally:
        service.shutdown()


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_bridge_service_controller_rejects_unstaged_unsigned_update_without_side_effects(floor, monkeypatch, enabled):
    api, _, manager = floor
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', enabled)
    before = dict(manager.state)
    result = desktop._smoke_handoff_floor(api)
    assert result == {'feature_available': enabled, 'trusted_main': True, 'fence_installed': True,
        'invalid_attempt_rejected': True, 'phase': 'idle', 'participant_count': 2, 'typed_block': True,
        'trust_floor_blocked': True, 'reason': 'helper_failed' if enabled else 'handoff_unavailable',
        'no_attempt': True, 'no_preparation': True, 'no_helper': True, 'no_restart': True}
    assert manager.state == before and not manager.applying
    assert api._update_authority.status() == {'phase': 'idle', 'locked': False}
    assert not (manager.root / 'operations').exists()
    assert not list((manager.home / 'backups').glob('update-*'))
    if enabled:
        assert api._update_controller.manager is manager
        assert api._update_controller.attempt is None
        assert manager.native_handoff.active is None
    else:
        assert api._update_controller is None and manager.native_handoff is None


@pytest.mark.parametrize('action', ['begin', 'ack', 'commit', 'abort', 'status'])
def test_explicit_closed_floor_refuses_before_coordinator_or_helper_loading(monkeypatch, action):
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', False)
    api = desktop.Bridge(pairing=object())
    api._ensure_service = lambda: pytest.fail('Closed handoff must not load coordinator state')
    api.quit = lambda: pytest.fail('Closed handoff must not close editing')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('No closed helper'))
    response = api.call('update_handoff_' + action, {'ready': True, 'private': 'synthetic-private-nonce'})
    assert response == {'blocked': True, 'phase': 'failed', 'reason': 'handoff_unavailable',
        'aborted': True, 'not_executed': True}
    assert api._update_authority.status() == {'phase': 'idle', 'locked': False}
    assert 'synthetic' not in json.dumps(response) and not api._closing


@pytest.mark.parametrize('state', ['verified', 'unknown-verified', 'downloaded', 'signature', 'operation',
    'applying', 'shutdown', 'inspection', 'checking', 'downloading', 'error', 'unknown-state', 'worker', 'operations', 'backup', 'authority', 'wrong-home'])
def test_smoke_pristine_precondition_refuses_existing_or_unknown_work_before_begin(floor, monkeypatch, state):
    api, _, manager = floor
    api.call = lambda *args, **kwargs: pytest.fail('Non-pristine smoke cannot request an attempt')
    if state == 'verified': manager.state['verified'] = True
    elif state == 'unknown-verified': manager.state['verified'] = 0
    elif state in ('downloaded', 'signature'): manager.state[state] = 'synthetic-private-content'
    elif state == 'operation': manager.state['operation_id'] = 'a' * 32
    elif state == 'applying': manager.applying = True
    elif state == 'shutdown': manager.shutting_down = True
    elif state == 'inspection': manager.inspection_required = True
    elif state in ('checking', 'downloading', 'error'): manager.state['state'] = state
    elif state == 'unknown-state': manager.state['state'] = None
    elif state == 'worker': manager.thread = SimpleNamespace(is_alive=lambda: True)
    elif state == 'operations':
        path = manager.root / 'operations'; path.mkdir(); (path / 'synthetic.json').write_text('{}')
    elif state == 'backup': (manager.home / 'backups' / ('update-' + 'a' * 32)).mkdir()
    elif state == 'authority':
        binding = api._native_binding(); api._update_authority.update_binding(binding)
        api._update_authority.begin(binding, object())
    elif state == 'wrong-home': monkeypatch.setenv('FORGE_DATA_DIR', str(manager.home.parent / 'another-state'))
    with pytest.raises(ValueError, match=r'^Handoff smoke requires pristine un-staged disposable state\.$'):
        desktop._smoke_handoff_floor(api)
    manager.applying = manager.shutting_down = manager.inspection_required = False
    manager.thread = None


@pytest.mark.parametrize('field', ['attempt', 'reservation', 'prepared', 'transport', 'commit', 'stopped_receipt', 'abort_worker'])
def test_smoke_pristine_refuses_owned_controller_work(floor, field):
    api, _, _ = floor
    controller = api._ensure_update_controller()
    setattr(controller, field, object())
    api.call = lambda *args, **kwargs: pytest.fail('Existing controller work cannot be used by the smoke')
    with pytest.raises(ValueError, match='pristine un-staged'):
        desktop._smoke_handoff_floor(api)
    setattr(controller, field, None)


@pytest.mark.parametrize('delta', [{'blocked': False}, {'aborted': False}, {'not_executed': False},
    {'not_executed': 1}, {'blocked': 'true'}, {'phase': 'checking'}, {'reason': 'active_work'},
    {'reason': 'handoff_unavailable'}, {'error': 'synthetic-private-error'}, {'attempt_id': 'a' * 32},
    {'challenge': 'b' * 64}, {'confirmation': 'c' * 64}])
def test_enabled_smoke_requires_exact_trust_floor_refusal(floor, monkeypatch, delta):
    api, _, _ = floor
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', True)
    response = {'blocked': True, 'aborted': True, 'not_executed': True, 'phase': 'failed', 'reason': 'helper_failed', **delta}
    api.call = lambda action, data: response
    with pytest.raises(ValueError, match=r'^Native handoff trust-floor smoke failed\.$') as error:
        desktop._smoke_handoff_floor(api)
    assert 'synthetic' not in str(error.value)


@pytest.mark.parametrize('delta', [{'installed': False}, {'invalid_attempt_rejected': False},
    {'invalid_attempt_rejected': 1}, {'phase': 'prepared'}, {'phase': 'synthetic-private-content'},
    {'participants': True}, {'participants': -1}, {'participants': 513}, {'participants': 'synthetic-secret'}])
def test_smoke_validates_renderer_fence_before_requesting_begin(floor, delta):
    api, _, _ = floor
    api._window.evaluate_js = lambda script: {'installed': True, 'invalid_attempt_rejected': True, 'phase': 'idle', 'participants': 2, **delta}
    api.call = lambda action, data: pytest.fail('Invalid renderer fence must not request begin')
    with pytest.raises(ValueError, match='trust-floor smoke failed'):
        desktop._smoke_handoff_floor(api)


def test_smoke_rechecks_owned_state_after_refusal_instead_of_trusting_json(floor, monkeypatch):
    api, _, manager = floor
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', True)
    def response(action, data):
        manager.applying = True
        return {'blocked': True, 'aborted': True, 'not_executed': True, 'phase': 'failed', 'reason': 'helper_failed'}
    api.call = response
    with pytest.raises(ValueError, match='trust-floor smoke failed'):
        desktop._smoke_handoff_floor(api)
    manager.applying = False


@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_smoke_requires_actual_trusted_main_before_and_after_refusal(floor, monkeypatch, boundary):
    api, _, _ = floor
    monkeypatch.setattr('forge_native_handoff.HANDOFF_ENABLED', True)
    actual = api.call
    def invalidate(action, data):
        result = actual(action, data); api._trusted_loaded = False; return result
    if boundary == 'after': api.call = invalidate
    else:
        api._trusted_loaded = False
        api.call = lambda *args: pytest.fail('Untrusted main cannot request begin')
    with pytest.raises(ValueError, match='trust-floor smoke failed'):
        desktop._smoke_handoff_floor(api)
    assert api._update_controller is None or api._update_controller.attempt is None


def test_smoke_setup_initializes_status_and_disables_checks_before_any_background_work(floor):
    api, service, previous = floor
    before = service.store.update_settings({'update_checks': True, 'permission_profile': 'deny_access', 'context': 16384})
    previous.shutdown(); service.update_manager = None
    desktop._smoke_handoff_setup(api)
    assert type(service.update_manager) is UpdateManager and service.update_manager is not previous
    assert service.store.get_settings() == {**before, 'update_checks': False}
    assert service.background is None and service.update_manager.thread is None
    assert desktop._smoke_handoff_pristine(api)['no_preparation'] is True


@pytest.mark.parametrize('existing', ['chat', 'project', 'updates', 'background'])
def test_smoke_setup_refuses_existing_workspace_or_update_history_without_changing_preferences(floor, existing):
    api, service, _ = floor
    service.store.update_settings({'update_checks': True})
    if existing == 'chat': service.store.create_chat(None, 'Synthetic saved chat', 'fixture')
    elif existing == 'project': service.store.create_project('Synthetic project', str(service.store.home))
    elif existing == 'updates': service.store.save_entity('updates', {'id': 'current', 'state': 'idle'})
    elif existing == 'background': service.background = SimpleNamespace(is_alive=lambda: False, join=lambda **kwargs: None)
    with pytest.raises(ValueError, match='pristine un-staged'):
        desktop._smoke_handoff_setup(api)
    assert service.store.get_settings()['update_checks'] is True


def test_powershell_floor_contract_is_strict_and_sanitizes_unknown_metadata(tmp_path):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if os.name != 'nt' or not shell:
        pytest.skip('Windows PowerShell is required for the release wrapper contract.')
    script = Path(desktop.__file__).parent / 'scripts' / 'release_smoke.ps1'
    probe = tmp_path / 'floor-contract.ps1'
    probe.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:FORGE_SMOKE_TEST_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Smoke script syntax is invalid.' }
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Test-ForgeHandoffFloor','Write-ForgeSmokeDiagnostics')},$true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
foreach ($enabled in @($false,$true)) {
    $floor=[pscustomobject]@{feature_available=$enabled;phase='idle';participant_count=2;reason=$(if($enabled){'helper_failed'}else{'handoff_unavailable'});trusted_main=$true;fence_installed=$true;invalid_attempt_rejected=$true;typed_block=$true;trust_floor_blocked=$true;no_attempt=$true;no_preparation=$true;no_helper=$true;no_restart=$true}
    if (-not (Test-ForgeHandoffFloor $floor)) { throw 'Valid floor was rejected.' }
    foreach ($name in @('trusted_main','fence_installed','invalid_attempt_rejected','typed_block','trust_floor_blocked','no_attempt','no_preparation','no_helper','no_restart')) {
        foreach ($invalid in @($false,1,'true',$null)) {
            $floor.$name=$invalid
            if (Test-ForgeHandoffFloor $floor) { throw 'Non-boolean floor evidence was accepted.' }
        }
        $floor.$name=$true
    }
    $floor.reason='active_work'
    if (Test-ForgeHandoffFloor $floor) { throw 'Unrelated refusal was accepted as the trust floor.' }
    $floor.reason=$(if($enabled){'helper_failed'}else{'handoff_unavailable'})
    foreach($value in @(-1,513,'2',$true,2.5,$null)) {
        $floor.participant_count=$value
        if(Test-ForgeHandoffFloor $floor) { throw 'Invalid participant count was accepted.' }
    }
    $floor.participant_count=2
    $floor.phase=@('idle','synthetic-private-content')
    if(Test-ForgeHandoffFloor $floor) { throw 'A mixed phase array was accepted.' }
    $floor.phase='idle'
    $floor.feature_available='true'
    if(Test-ForgeHandoffFloor $floor) { throw 'A string feature flag was accepted.' }
}
$secret='synthetic-private-path-key-and-nonce'
$script:ForgeSmokeDiagnosticPath=Join-Path $env:FORGE_SMOKE_TEST_ROOT 'safe-floor.json'
$floor=[pscustomobject]@{feature_available=$secret;phase=$secret;participant_count=$secret;reason=$secret;no_helper=$secret;no_preparation=[pscustomobject]@{private=$secret};private=$secret}
Write-ForgeSmokeDiagnostics ([pscustomobject]@{smoke_phase='handoff-floor';handoff_seed=$floor;error='Native handoff trust-floor smoke failed.'}) 'handoff-floor' 0 'ready'
$saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
$safe=$saved|ConvertFrom-Json
if ($saved.Contains($secret) -or $safe.handoff_reason -ne 'unrecorded' -or $safe.handoff_phase -ne 'unrecorded' -or $null -ne $safe.handoff_feature_available -or $null -ne $safe.handoff_participant_count -or $safe.checks.handoff_floor -ne 'fail' -or $safe.gate -ne 'handoff-floor' -or $safe.error_category -ne 'handoff-trust-floor') { throw 'Floor summary violated its closed schema.' }
Write-Output 'strict-floor-contract-passed'
""", encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(probe)], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'FORGE_SMOKE_TEST_SCRIPT': str(script), 'FORGE_SMOKE_TEST_ROOT': str(tmp_path)},
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'synthetic-private-path-key-and-nonce' not in result.stdout + result.stderr
    assert 'strict-floor-contract-passed' in result.stdout
