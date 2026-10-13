"""Smoke evidence remains bounded and private without launching Forge."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import desktop


def checkpoint(tmp_path):
    folder = tmp_path / 'smoke'
    folder.mkdir()
    return desktop._SmokeCheckpoint(str(folder / 'state'), str(folder / 'native-smoke.json')), folder


def test_checkpoint_keeps_raw_report_and_only_publishes_fixed_labels(tmp_path):
    writer, folder = checkpoint(tmp_path)
    raw = folder / 'native-smoke.json'
    raw.write_text('Synthetic private fixture: transcript, path, nonce and credentials.', encoding='utf-8')
    before = raw.read_bytes()
    assert writer.write('service-start')
    assert writer.write('startup-failed', error_type='SyntheticPrivateError', error_category='startup-failure')
    saved = (folder / 'native-smoke-checkpoint.json').read_bytes()
    assert len(saved) <= 2048
    assert json.loads(saved) == {'schema': 1, 'phase': 'startup-failed', 'error_type': 'unclassified', 'error_category': 'startup-failure'}
    assert raw.read_bytes() == before
    assert not writer.write('private-path-and-nonce')
    assert not writer.write('service-start', error_category='private-credential')
    assert (folder / 'native-smoke-checkpoint.json').read_bytes() == saved


def test_checkpoint_replacement_failure_preserves_last_phase_without_escaping(tmp_path, monkeypatch):
    writer, folder = checkpoint(tmp_path)
    assert writer.write('window-created')
    saved = (folder / 'native-smoke-checkpoint.json').read_bytes()
    def unavailable(*args):
        raise PermissionError('Synthetic private exception text must never be published.')
    monkeypatch.setattr(desktop.os, 'replace', unavailable)
    assert not writer.write('workspace-ready')
    assert (folder / 'native-smoke-checkpoint.json').read_bytes() == saved
    assert not list(folder.glob('.smoke-checkpoint-*'))


def test_checkpoint_requires_disposable_sibling_state_and_refuses_hardlinks(tmp_path):
    writer, folder = checkpoint(tmp_path)
    assert not desktop._SmokeCheckpoint(str(tmp_path / 'personal'), str(folder / 'native-smoke.json')).write('service-start')
    assert not desktop._SmokeCheckpoint(None, str(folder / 'native-smoke.json')).write('service-start')
    assert not desktop._SmokeCheckpoint(str(folder / 'state'), None).write('service-start')
    external = tmp_path / 'external.json'
    external.write_bytes(b'Synthetic external sentinel')
    os.link(external, folder / 'native-smoke-checkpoint.json')
    assert not writer.write('service-start')
    assert external.read_bytes() == b'Synthetic external sentinel'


@pytest.mark.parametrize('flag,entry', [('--forge-update', 'helper_entry'), ('--forge-update-protocol-smoke', 'protocol_smoke_entry')])
@pytest.mark.parametrize('mixed_smoke', [False, True])
def test_native_helpers_return_before_checkpoint_or_report_path_handling(monkeypatch, flag, entry, mixed_smoke):
    import forge_native_handoff
    monkeypatch.setattr(sys, 'argv', ['Forge.exe', flag, *(['--smoke-test'] if mixed_smoke else [])])
    monkeypatch.setattr(forge_native_handoff, entry, lambda: 'synthetic-helper-result')
    def forbidden(*args, **kwargs):
        pytest.fail('Helper mode must not construct a checkpoint or inspect a smoke path.')
    monkeypatch.setattr(desktop, '_SmokeCheckpoint', forbidden)
    monkeypatch.setattr(desktop.os, 'getenv', forbidden)
    assert desktop.main() == 'synthetic-helper-result'


def test_ordinary_and_helper_modes_cannot_write_a_stale_smoke_checkpoint(monkeypatch):
    class Writer:
        def write(self, *args, **kwargs):
            pytest.fail('Only explicit smoke mode may write a checkpoint.')
    monkeypatch.setattr(desktop, '_ACTIVE_SMOKE_CHECKPOINT', Writer())
    for flags in ([], ['--forge-update'], ['--smoke-test', '--forge-update'], ['--smoke-test', '--forge-update-protocol-smoke']):
        monkeypatch.setattr(sys, 'argv', ['Forge.exe', *flags])
        desktop._smoke_checkpoint('service-start')


@pytest.mark.parametrize('failure,expected', [('guard', 'not-executed'),
    ('timeout', 'outcome-unknown'), ('runtime', 'outcome-unknown')])
def test_research_smoke_retains_native_refusal_and_unknown_outcome_evidence(tmp_path, failure, expected):
    from browser_tools import BrowserTools
    from forge_integrations import IntegrationHub
    from forge_runs import ToolRegistry
    from native_browser import BrowserGuard, NativeBrowser
    errors = {'guard': BrowserGuard('Browser panel bounds must be finite numbers.'),
        'timeout': TimeoutError('Synthetic browser dispatch timeout'),
        'runtime': RuntimeError('Synthetic browser initialization failure')}
    class Page:
        def ready(self): pass
        def show(self): raise errors[failure]
    browser = BrowserTools(tmp_path)
    native = NativeBrowser(tmp_path, view_factory=lambda changed: Page(), session_check=lambda: True)
    browser.native = native
    hub = object.__new__(IntegrationHub)
    hub.browser = browser
    service = SimpleNamespace(store=object(), integrations=hub)
    service.jobs = SimpleNamespace(registry=ToolRegistry(service))
    result = {}
    # Execute the shipped smoke helper against the real registry/integration/
    # native worker path. No WebView2 control, host startup or network is used.
    source = Path(desktop.__file__).read_text(encoding='utf-8')
    definition = next(node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == 'tool')
    namespace = {'service': service, 'browser': browser, 'result': result,
        'run': {'id': 'synthetic-smoke', 'project_id': None}, 'cancel': threading.Event()}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), desktop.__file__, 'exec'), namespace)
    try:
        with pytest.raises(ValueError):
            namespace['tool']('browser_navigate', {'url': 'http://127.0.0.1:1/'})
        assert result == {'tool_failure': expected, 'research_view_created': True}
    finally:
        native.view = None
        browser.shutdown()


def test_powershell_reads_bounded_reports_and_redacts_before_persistence(tmp_path):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if os.name != 'nt' or not shell:
        pytest.skip('Windows PowerShell is required for the native diagnostic reader.')
    script = Path(__file__).resolve().parents[1] / 'scripts/release_smoke.ps1'
    ordinary = tmp_path / 'ordinary.json'
    ordinary.write_bytes(b'{"phase":"service-start"}')
    os.link(ordinary, tmp_path / 'hardlink.json')
    probe = tmp_path / 'probe.ps1'
    probe.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:FORGE_SMOKE_TEST_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Smoke script syntax is invalid.' }
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Read-ForgeSmokeReport','Write-ForgeSmokeDiagnostics')},$true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$root=$env:FORGE_SMOKE_TEST_ROOT
$raw=Join-Path $root 'raw.json'
$script:ForgeSmokeDiagnosticPath=Join-Path $root 'safe.json'
$secret='synthetic-private-content-and-nonce'
[IO.File]::WriteAllText($raw, ('{"smoke_phase":"research-start","ready":"complete","title":"Forge","ui":{"composer":true},"error":"'+$secret+'","api_key":"'+$secret+'"}'))
$partial=Read-ForgeSmokeReport $raw
if ($partial.state -ne 'ready') { throw 'Ordinary partial report was not read.' }
$checkpoint=[pscustomobject]@{phase='service-start';error_type='SyntheticPrivateError';error_category='startup-failure';private=$secret}
Write-ForgeSmokeDiagnostics $partial.data 'process-timeout' $null $partial.state $checkpoint 'ready'
$saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
if ($saved.Contains($secret)) { throw 'Private fixture content escaped the sanitized summary.' }
$safe=$saved|ConvertFrom-Json
if ($safe.gate -ne 'process-timeout' -or $safe.phase -ne 'research-start' -or $safe.checks.composer -ne 'pass' -or $safe.checkpoint_phase -ne 'service-start' -or $safe.checkpoint_error_type -ne 'unclassified') { throw 'Partial report/checkpoint evidence was lost.' }
Write-ForgeSmokeDiagnostics $null $secret ([pscustomobject]@{private=$secret})
$saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
$safe=$saved|ConvertFrom-Json
if ($saved.Contains($secret) -or $safe.gate -ne 'unclassified' -or $null -ne $safe.process_exit) { throw 'Unknown gate or exit object escaped the closed summary.' }
foreach ($case in @(
    @('Integration timed out. Inspect its outcome before retrying a side effect.','integration-timeout','outcome-unknown'),
    @('Native input is paused while Forge confirms a restart.','native-admission','refused'),
    @('Browser panel bounds must be finite numbers.','panel-bounds','not-executed'),
    @('Integration failed (AttributeError). Check the endpoint, authentication and server availability.','integration-attribute','outcome-unknown'))) {
    $data=[pscustomobject]@{smoke_phase='research-navigate';error_type='ValueError';error=$case[0];tool_failure=$case[2];research_view_created=$true;private=$secret}
    Write-ForgeSmokeDiagnostics $data 'process-timeout' $null 'ready'
    $saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
    $safe=$saved|ConvertFrom-Json
    if ($saved.Contains($secret) -or $safe.error_category -ne $case[1] -or $safe.tool_failure -ne $case[2] -or $safe.research_view_created -ne $true -or $safe.gate -ne 'process-timeout') { throw 'Fixed browser/integration refusal evidence was not preserved safely.' }
}
$data=[pscustomobject]@{error=$secret;tool_failure=$secret;research_view_created=[pscustomobject]@{private=$secret}}
Write-ForgeSmokeDiagnostics $data 'process-timeout' $null
$saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
$safe=$saved|ConvertFrom-Json
if ($saved.Contains($secret) -or $safe.tool_failure -ne 'unrecorded' -or $null -ne $safe.research_view_created -or $safe.error_category -ne 'unclassified') { throw 'Unknown tool status escaped the closed summary.' }
[IO.File]::WriteAllText($raw, '{invalid')
if ((Read-ForgeSmokeReport $raw).state -ne 'invalid') { throw 'Malformed report was accepted.' }
[IO.File]::WriteAllText($raw, ('x' * 1048577))
if ((Read-ForgeSmokeReport $raw).state -ne 'too-large') { throw 'Oversized report was accepted.' }
if ((Read-ForgeSmokeReport (Join-Path $root 'missing.json')).state -ne 'missing') { throw 'Missing report classification changed.' }
if ((Read-ForgeSmokeReport $root).state -ne 'unsafe') { throw 'Directory report was accepted.' }
if ((Read-ForgeSmokeReport (Join-Path $root 'hardlink.json')).state -ne 'unsafe') { throw 'Hard-linked report was accepted.' }
[IO.File]::WriteAllBytes($raw, [byte[]]@(123,255,125))
if ((Read-ForgeSmokeReport $raw).state -ne 'invalid') { throw 'Malformed UTF-8 was accepted.' }
$script:ForgeSmokeDiagnosticPath=$root
Write-ForgeSmokeDiagnostics $null 'process-timeout' $null 'missing'
Write-Output 'sanitized-reader-contract-passed'
""", encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(probe)], capture_output=True, text=True,
        timeout=30, env={**os.environ, 'FORGE_SMOKE_TEST_SCRIPT': str(script), 'FORGE_SMOKE_TEST_ROOT': str(tmp_path)},
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'synthetic-private-content-and-nonce' not in result.stdout + result.stderr
    assert 'sanitized-reader-contract-passed' in result.stdout
