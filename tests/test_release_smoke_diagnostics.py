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


class PreviewClock:
    def __init__(self):
        self.now = 0.
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now

    def pause(self, seconds):
        self.now += seconds


def preview_readiness_fixture():
    from embedded_browser import EmbeddedWebView2Page
    owner = SimpleNamespace(Visible=True, webview=SimpleNamespace(Left=0, Top=0,
        ClientSize=SimpleNamespace(Width=1000, Height=800)))
    snapshot = {'visible': True, 'hud': False, 'modal': False,
        'rect': {'x': 250, 'y': 150, 'width': 700, 'height': 600},
        'viewport': {'width': 1000, 'height': 800}}
    view = object.__new__(EmbeddedWebView2Page)
    view.visible, view.bound, view.disposed = True, (250, 150, 700, 600), False
    view.panel_id = 'forge-preview-panel'
    view.container = SimpleNamespace(Visible=True)
    view.changed = lambda *args: None
    view.ui = lambda operation: operation(owner)
    view._layout = lambda: snapshot
    browser = SimpleNamespace(view=view, status=lambda: {'visible': view.visible, 'loading': False})
    manager = SimpleNamespace(previews=SimpleNamespace(browser=browser, browser_preview='preview'))
    return manager, view, owner, snapshot, PreviewClock()


def test_standalone_readiness_waits_for_real_matching_bounds_without_native_effects():
    manager, view, _, snapshot, clock = preview_readiness_fixture()
    calls = []
    def layout():
        calls.append('layout')
        if len(calls) <= 2:
            # A mounted pane has resized but its serialized frontend bind is pending.
            snapshot['rect']['height'] = 560
        else:
            view.bound = (250, 150, 700, 560)
        return snapshot
    view._layout = layout
    def forbidden(*args, **kwargs):
        pytest.fail('A readiness observation must not dispatch a native effect.')
    for name in ('show', 'bind', 'navigate', 'set_viewport', 'hide', 'evaluate', 'screenshot'):
        setattr(view, name, forbidden)
    manager.execute = forbidden
    status_calls, native_calls = [], []
    status, native = manager.previews.browser.status, view.ui
    manager.previews.browser.status = lambda: (status_calls.append(True), status())[1]
    view.ui = lambda operation: (native_calls.append(True), native(operation))[1]
    diagnostics = {}
    assert desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock, pause=clock.pause,
                                       diagnostics=diagnostics) is True
    assert len(calls) == len(status_calls) == len(native_calls) == 4
    assert clock.now == pytest.approx(.15) and clock.reads == 16
    observed = diagnostics['smoke_standalone_readiness']
    assert set(observed) == set(desktop.SMOKE_STANDALONE_READINESS_FLAGS) | {'iterations'}
    assert all(type(observed[key]) is bool for key in desktop.SMOKE_STANDALONE_READINESS_FLAGS)
    assert type(observed['iterations']) is int and observed['iterations'] == 4
    assert all(observed[key] for key in desktop.SMOKE_STANDALONE_READINESS_FLAGS
               if key not in ('dom_hud', 'dom_modal', 'deadline_expired'))
    assert not any(observed[key] for key in ('dom_hud', 'dom_modal', 'deadline_expired'))


@pytest.mark.parametrize('condition', ['hidden', 'loading', 'modal', 'unbound', 'moving', 'invalid'])
def test_standalone_readiness_refuses_unstable_or_hidden_pane_at_original_deadline(condition):
    manager, view, owner, snapshot, clock = preview_readiness_fixture()
    reads = 0
    def layout():
        nonlocal reads
        reads += 1
        if condition == 'moving':
            snapshot['rect']['y'] = 150 + reads % 2
            view.bound = (250, snapshot['rect']['y'], 700, 600)
        return snapshot
    view._layout = layout
    if condition == 'hidden': owner.Visible = False
    if condition == 'loading': manager.previews.browser.status = lambda: {'visible': True, 'loading': True}
    if condition == 'modal': snapshot['modal'] = True
    if condition == 'unbound': view.bound = (250, 150, 700, 560)
    if condition == 'invalid': snapshot['rect']['width'] = float('nan')
    diagnostics = {}
    with pytest.raises(ValueError, match='The standalone preview pane did not open'):
        desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock, pause=clock.pause,
                                       diagnostics=diagnostics)
    assert clock.now == 10 and reads >= 2
    observed = diagnostics['smoke_standalone_readiness']
    assert observed['deadline_expired'] is True
    assert observed['owner_same'] is True
    assert observed['snapshot_observed'] is observed['native_observed'] is True
    relevant = {'hidden': 'owner_visible', 'loading': 'loading_complete', 'modal': 'layout_valid',
                'unbound': 'bounds_match', 'moving': 'same_sample', 'invalid': 'layout_valid'}
    assert observed[relevant[condition]] is False
    assert observed['dom_modal'] is (condition == 'modal')


@pytest.mark.parametrize('change', ['view', 'browser', 'preview'])
def test_standalone_readiness_never_adopts_replaced_owner(change):
    manager, view, _, snapshot, clock = preview_readiness_fixture()
    def layout():
        if change == 'view': manager.previews.browser.view = object()
        elif change == 'browser': manager.previews.browser = object()
        else: manager.previews.browser_preview = 'other-preview'
        return snapshot
    view._layout = layout
    diagnostics = {}
    with pytest.raises(ValueError, match='The standalone preview pane did not open'):
        desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock, pause=clock.pause,
                                       diagnostics=diagnostics)
    assert clock.now == 0
    observed = diagnostics['smoke_standalone_readiness']
    assert observed['owner_same'] is observed['native_observed'] is observed['deadline_expired'] is False
    assert observed['snapshot_observed'] is True and observed['iterations'] == 1


@pytest.mark.parametrize('phase', ['layout', 'native'])
def test_standalone_readiness_late_read_cannot_confirm_after_deadline(phase):
    manager, view, _, _, clock = preview_readiness_fixture()
    original = view._layout if phase == 'layout' else view.ui
    calls = 0
    def delayed(*args):
        nonlocal calls
        calls += 1
        if calls == 2: clock.now = 10.
        return original(*args)
    setattr(view, '_layout' if phase == 'layout' else 'ui', delayed)
    diagnostics = {}
    with pytest.raises(ValueError, match='The standalone preview pane did not open'):
        desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock, pause=clock.pause,
                                       diagnostics=diagnostics)
    assert calls == 2
    observed = diagnostics['smoke_standalone_readiness']
    assert observed['deadline_expired'] is True and observed['iterations'] == 2
    assert observed['native_observed'] is (phase == 'native')
    assert observed['same_sample'] is False


@pytest.mark.parametrize('mode', ['none', 'list', 'dict-subclass', 'foreign'],
                         ids=['default-none', 'list-input', 'dict-subclass-input', 'foreign-input'])
def test_standalone_readiness_ignores_non_plain_diagnostic_inputs_without_touching_them(mode):
    class Foreign:
        def __bool__(self):
            pytest.fail('Diagnostic input must not be inspected.')
        __str__ = __repr__ = __bool__
    class ForeignDict(dict):
        __bool__ = __str__ = __repr__ = Foreign.__bool__
        def __setitem__(self, *args):
            pytest.fail('Diagnostic input must not be written through a foreign method.')
    diagnostics = {'none': None, 'list': [], 'dict-subclass': ForeignDict(), 'foreign': Foreign()}[mode]
    manager, view, _, _, clock = preview_readiness_fixture()
    calls = []
    layout, native, status = view._layout, view.ui, manager.previews.browser.status
    view._layout = lambda: (calls.append('layout'), layout())[1]
    view.ui = lambda operation: (calls.append('native'), native(operation))[1]
    manager.previews.browser.status = lambda: (calls.append('status'), status())[1]
    assert desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock,
                                       pause=clock.pause, diagnostics=diagnostics) is True
    assert calls == ['status', 'layout', 'native'] * 2
    assert clock.now == .05 and clock.reads == 8
    if mode == 'dict-subclass':
        assert len(diagnostics) == 0


def test_standalone_readiness_report_contains_only_last_fixed_primitives_not_private_inputs():
    manager, view, owner, snapshot, clock = preview_readiness_fixture()
    marker = 'synthetic-private-path-api-key-opaque-content'
    snapshot.update(text=marker, selector=marker, error=marker, url=marker, coordinates=[1, 2])
    owner.title = marker
    manager.previews.browser.status = lambda: {'visible': True, 'loading': False, 'error': marker, 'api_key': marker}
    diagnostics = {'existing': marker}
    assert desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock,
                                       pause=clock.pause, diagnostics=diagnostics) is True
    saved = diagnostics['smoke_standalone_readiness']
    assert marker not in json.dumps(saved)
    assert set(saved) == set(desktop.SMOKE_STANDALONE_READINESS_FLAGS) | {'iterations'}
    assert all(type(value) in (bool, int) for value in saved.values())
    assert diagnostics['existing'] == marker
    # The reported object owns copied scalars, never the source DOM/native dict.
    snapshot['modal'] = True
    assert saved['dom_modal'] is False


def test_standalone_readiness_iteration_diagnostic_saturates_without_shortening_deadline():
    manager, view, _, _, clock = preview_readiness_fixture()
    view.bound = None
    pauses = []
    def fast_pause(seconds):
        pauses.append(seconds)
        clock.now = min(1., clock.now + .001)
    diagnostics = {}
    with pytest.raises(ValueError, match='The standalone preview pane did not open'):
        desktop._smoke_preview_ready(manager, 'preview', deadline=1, clock=clock,
                                       pause=fast_pause, diagnostics=diagnostics)
    assert clock.now == 1 and len(pauses) > 512
    saved = diagnostics['smoke_standalone_readiness']
    assert saved['iterations'] == 512 and type(saved['iterations']) is int
    assert saved['deadline_expired'] is True and saved['native_bound_present'] is False


@pytest.mark.parametrize('outcome', ['success', 'refusal', 'native-error'],
                         ids=['success-preserved', 'fixed-refusal-preserved', 'native-error-preserved'])
def test_standalone_diagnostic_publication_failure_preserves_original_outcome(monkeypatch, outcome):
    from native_browser import BrowserGuard
    manager, view, _, _, clock = preview_readiness_fixture()
    original = BrowserGuard('Synthetic native failure')
    if outcome == 'refusal':
        view.bound = None
    elif outcome == 'native-error':
        def fail(operation):
            raise original
        view.ui = fail
    publications = []
    def publication(*args):
        publications.append(True)
        raise MemoryError('Synthetic diagnostic failure')
    monkeypatch.setattr(desktop, '_smoke_preview_readiness_report', publication)
    if outcome == 'success':
        assert desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock,
                                           pause=clock.pause, diagnostics={}) is True
        assert clock.reads == 8 and clock.now == .05
    else:
        expected = ValueError if outcome == 'refusal' else BrowserGuard
        with pytest.raises(expected) as raised:
            desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock,
                                           pause=clock.pause, diagnostics={})
        if outcome == 'refusal':
            assert str(raised.value) == 'The standalone preview pane did not open.' and clock.now == 10
        else:
            assert raised.value is original and clock.now == 0
    assert publications == [True]


@pytest.mark.parametrize('state', ['absent', 'expired'], ids=['no-owned-view', 'deadline-already-expired'])
def test_standalone_readiness_unobserved_fields_cannot_claim_a_sample(state):
    manager, view, _, _, clock = preview_readiness_fixture()
    if state == 'absent':
        manager.previews.browser.view = None
    diagnostics = {}
    with pytest.raises(ValueError, match='The standalone preview pane did not open'):
        desktop._smoke_preview_ready(manager, 'preview', deadline=0 if state == 'expired' else 10,
                                       clock=clock, pause=clock.pause, diagnostics=diagnostics)
    saved = diagnostics['smoke_standalone_readiness']
    assert saved['view_present'] is (state == 'expired')
    assert saved['iterations'] == (0 if state == 'expired' else 1)
    assert saved['deadline_expired'] is (state == 'expired')
    assert not any(saved[name] for name in ('owner_same', 'snapshot_observed', 'native_observed', 'same_sample'))


def test_standalone_ready_does_not_relax_final_guard_or_retry_viewport(tmp_path):
    from native_browser import BrowserGuard, NativeBrowser
    manager, view, _, snapshot, clock = preview_readiness_fixture()
    assert desktop._smoke_preview_ready(manager, 'preview', deadline=10, clock=clock, pause=clock.pause) is True
    # The real Embedded preflight must reject a later move before importing WinForms
    # or setting a viewport; readiness is an observation, not an authorization.
    snapshot['rect']['height'] = 560
    native = NativeBrowser(tmp_path, view_factory=lambda changed: view, session_check=lambda: True,
                           panel_id='forge-preview-panel')
    native.view = view
    calls = []
    def execute(run, name, args, cancel):
        calls.append(name)
        return native.viewport(args)
    manager.execute = execute
    result = {}
    try:
        with pytest.raises(BrowserGuard, match='Browser panel changed'):
            desktop._smoke_preview_tool(manager, {'id': 'smoke'}, threading.Event(), result,
                'preview_viewport', {'id': 'preview'})
        assert calls == ['preview_viewport'] and result == {'tool_failure': 'outcome-unknown'}
        assert view.visible is False and view.bound is None and native.pending_io() == 0
    finally:
        native.pool.shutdown(wait=True, cancel_futures=True)


def test_smoke_raised_browser_guard_does_not_claim_generic_action_was_unexecuted():
    from native_browser import BrowserGuard
    effects = []
    def execute(*args):
        effects.append('synthetic-action-dispatched')
        raise BrowserGuard('Browser panel changed. Inspect again after reopening it.')
    result = {}
    with pytest.raises(BrowserGuard):
        desktop._smoke_preview_tool(SimpleNamespace(execute=execute), {'id': 'smoke'},
            threading.Event(), result, 'preview_click', {})
    assert effects == ['synthetic-action-dispatched'] and result == {'tool_failure': 'outcome-unknown'}


def test_standalone_viewport_phase_precedes_one_checked_call_and_both_new_phases_are_sanitized():
    tree = ast.parse(Path(desktop.__file__).read_bytes())
    starts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == '_smoke_preview_ready']
    assert len(starts) == 1
    assert any(keyword.arg == 'diagnostics' and isinstance(keyword.value, ast.Name)
               and keyword.value.id == 'result' for keyword in starts[0].keywords)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == '_smoke_preview_tool'
        and any(isinstance(arg, ast.Constant) and arg.value == 'preview_viewport' for arg in node.args)]
    assert len(calls) == 1 and starts[0].lineno < calls[0].lineno
    script = (Path(desktop.__file__).parent / 'scripts/release_smoke.ps1').read_text()
    for phase in ('standalone-layout', 'standalone-viewport'):
        assert phase in desktop.SMOKE_PHASES and "'" + phase + "'" in script


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
        supports_pane_guard = True
        def ready(self): pass
        def show(self, check=None):
            if check: check()
        def navigate(self, url, check=None):
            if failure == 'guard': raise errors[failure]
            if check: check()
            raise errors[failure]
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
        native.dispatch('show')  # Only the current UI owns showing the research pane.
        with pytest.raises(ValueError):
            namespace['tool']('browser_navigate', {'url': 'http://127.0.0.1:1/'})
        assert result == {'tool_failure': expected, 'research_view_created': True}
    finally:
        native.view = None
        browser.shutdown()


@pytest.mark.parametrize('outcome,category', [
    ({'ok': False, 'not_executed': True}, 'not-executed'),
    ({'ok': False, 'outcome_unknown': True}, 'outcome-unknown'),
    ({'ok': False, 'not_executed': True, 'performed': 'focused_browser'}, 'outcome-unknown'),
    ({'ok': False, 'error': 'Synthetic private content'}, 'refused'),
    ({'ok': True, 'outcome_unknown': True}, 'outcome-unknown'),
    ({'ok': True, 'error': 'Synthetic private content'}, 'outcome-unknown'),
    ({'ok': 1}, 'outcome-unknown'),
    ({'snapshot_id': 'synthetic'}, 'outcome-unknown'),
    (None, 'outcome-unknown'),
])
def test_standalone_smoke_checks_one_outcome_without_replaying(outcome, category):
    calls = []
    run, cancel, args, result = {'id': 'synthetic'}, threading.Event(), {'id': 'fixture'}, {}
    manager = SimpleNamespace(execute=lambda *values: calls.append(values) or outcome)
    with pytest.raises(ValueError, match='Standalone preview tool did not complete successfully') as error:
        desktop._smoke_preview_tool(manager, run, cancel, result, 'preview_click', args)
    assert calls == [(run, 'preview_click', args, cancel)]
    assert result == {'tool_failure': category}
    assert 'Synthetic private content' not in str(error.value)


@pytest.mark.parametrize('failure', [TimeoutError, RuntimeError])
def test_standalone_smoke_preserves_unknown_dispatch_without_retry(failure):
    calls = []
    def execute(*values):
        calls.append(values)
        raise failure('Synthetic private failure')
    result = {}
    with pytest.raises(failure):
        desktop._smoke_preview_tool(SimpleNamespace(execute=execute), {}, threading.Event(), result, 'preview_type', {})
    assert len(calls) == 1 and result == {'tool_failure': 'outcome-unknown'}


def test_standalone_smoke_retains_exact_successful_evidence():
    outcome = {'ok': True, 'snapshot_id': 'synthetic'}
    calls, result = [], {}
    manager = SimpleNamespace(execute=lambda *values: calls.append(values) or outcome)
    assert desktop._smoke_preview_tool(manager, {}, threading.Event(), result, 'preview_inspect', {}) is outcome
    assert len(calls) == 1 and not result


@pytest.mark.parametrize('exit_code', [0, 1, -1, -1073741819])
def test_native_smoke_requires_clean_process_exit_even_with_complete_report(tmp_path, exit_code):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if os.name != 'nt' or not shell:
        pytest.skip('Windows PowerShell is required for the native exit gate.')
    script = Path(__file__).resolve().parents[1] / 'scripts/release_smoke.ps1'
    floor = dict(feature_available=False, phase='idle', reason='handoff_unavailable', participant_count=0)
    for field in ('trusted_main', 'fence_installed', 'invalid_attempt_rejected', 'typed_block',
                  'trust_floor_blocked', 'no_attempt', 'no_preparation', 'no_helper', 'no_restart'):
        floor[field] = True
    report = dict(title='Forge', close_hides=True, handoff_seed=floor,
        library=dict(rendered=True, starter_skills=28, discover_cards=28),
        native_browser=dict(fixture_updated=True, bridge_exposed='undefined'),
        native_preview=dict(fixture_updated=True, separate_controller=True, separate_profile=True,
            bridge_exposed='undefined', snapshot_guarded=True, viewport_verified=True,
            diagnostics_captured=True, screenshot_bytes=1001, stopped=True),
        native_standalone_preview=dict(pane_visible=True, fixture_updated=True,
            without_builder=True, bridge_exposed='undefined', screenshot_bytes=1001))
    report['smoke_standalone_readiness'] = dict.fromkeys(desktop.SMOKE_STANDALONE_READINESS_FLAGS, True)
    report['smoke_standalone_readiness'].update(dom_hud=False, dom_modal=False, deadline_expired=False, iterations=2)
    (tmp_path / 'report.json').write_text(json.dumps(report), encoding='utf-8')
    probe = tmp_path / 'exit-gate.ps1'
    probe.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:FORGE_EXIT_TEST_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Smoke script syntax is invalid.' }
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$root=$env:FORGE_EXIT_TEST_ROOT
$report=Join-Path $root 'report.json'
$checkpoint=Join-Path $root 'absent-checkpoint.json'
$script:ForgeSmokeDiagnosticPath=Join-Path $root 'safe.json'
$process=[pscustomobject]@{ExitCode=[int]$env:FORGE_EXIT_TEST_CODE}
# Execute the real top-level post-wait gates. No executable is started.
$statements=@($ast.EndBlock.Statements)
$start=-1
for($i=0;$i -lt $statements.Count;$i++) {
 if($statements[$i].Extent.Text -eq '$partial = Read-ForgeSmokeReport $report') { $start=$i; break }
}
if($start -lt 0) { throw 'Post-wait evidence reader was not found.' }
try {
 for($i=$start;$i -lt $statements.Count;$i++) { Invoke-Expression $statements[$i].Extent.Text }
 [IO.File]::WriteAllText((Join-Path $root 'outcome.json'),' {"accepted":true}')
} catch {
 $safe=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)|ConvertFrom-Json
 if($safe.gate -ne 'process-exit' -or $safe.process_exit -ne $process.ExitCode -or $safe.report_state -ne 'ready') {
  throw 'Nonzero exit did not retain the fixed, bounded failure category.'
 }
 [IO.File]::WriteAllText((Join-Path $root 'outcome.json'),' {"accepted":false}')
}
""", encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(probe)], capture_output=True, text=True,
        timeout=30, env={**os.environ, 'FORGE_EXIT_TEST_SCRIPT': str(script),
            'FORGE_EXIT_TEST_ROOT': str(tmp_path), 'FORGE_EXIT_TEST_CODE': str(exit_code)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / 'outcome.json').read_text())['accepted'] is (exit_code == 0)


def test_powershell_standalone_readiness_projects_only_strict_fixed_primitives(tmp_path):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if os.name != 'nt' or not shell:
        pytest.skip('Windows PowerShell is required for the native diagnostic summary.')
    script = Path(__file__).resolve().parents[1] / 'scripts/release_smoke.ps1'
    probe = tmp_path / 'readiness-summary.ps1'
    probe.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:FORGE_READINESS_TEST_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Smoke script syntax is invalid.' }
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Test-ForgeHandoffFloor','Write-ForgeSmokeDiagnostics')},$true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$script:ForgeSmokeDiagnosticPath=Join-Path $env:FORGE_READINESS_TEST_ROOT 'safe.json'
$secret='synthetic-private-path-key-opaque-value'
$names=@('view_present','owner_same','snapshot_observed','native_observed',
    'owner_visible','view_visible','status_visible','loading_complete','dom_visible','dom_hud',
    'dom_modal','layout_valid','native_bound_present','bounds_match','same_sample','deadline_expired',
    'layout_left','layout_top','layout_right','layout_bottom','layout_scale')
function Project-Readiness($record) {
    $data=[pscustomobject]@{smoke_phase='standalone-layout';smoke_standalone_readiness=$record;private=$secret}
    Write-ForgeSmokeDiagnostics $data 'standalone-preview' $null 'ready'
    $saved=[IO.File]::ReadAllText($script:ForgeSmokeDiagnosticPath)
    if ($saved.Contains($secret)) { throw 'Private input escaped the sanitized summary.' }
    return ($saved|ConvertFrom-Json).smoke_standalone_readiness
}
foreach ($bad in @($null,$secret,$true,4,@(1,2))) {
    if ($null -ne (Project-Readiness $bad)) { throw 'Nonrecord diagnostic input was accepted.' }
}
$record=[pscustomobject]@{iterations=512;private=$secret;rect=@(1,2,3,4);url=$secret;error=$secret}
foreach ($name in $names) { $record|Add-Member -NotePropertyName $name -NotePropertyValue $true }
$record.dom_hud=$false
$safe=Project-Readiness $record
if (@($safe.PSObject.Properties).Count -ne ($names.Count+1) -or $safe.iterations -ne 512 -or $safe.dom_hud -isnot [bool] -or $safe.dom_hud) { throw 'Fixed scalar record was not preserved.' }
foreach ($name in $names) { if ($safe.$name -isnot [bool]) { throw 'A boolean observation changed type.' } }
foreach ($number in @(0,512,[long]5)) {
    $record.iterations=$number
    if ((Project-Readiness $record).iterations -ne $number) { throw 'Valid bounded iteration count was lost.' }
}
foreach ($number in @($true,$false,-1,513,1.5,'42',[pscustomobject]@{private=$secret},@(1,2),$null)) {
    $record.iterations=$number
    if ($null -ne (Project-Readiness $record).iterations) { throw 'Invalid diagnostic count was accepted.' }
}
$record.view_present=1
$record.owner_same='true'
$record.snapshot_observed=[pscustomobject]@{private=$secret}
$record.native_observed=$null
$safe=Project-Readiness $record
foreach ($name in @('view_present','owner_same','snapshot_observed','native_observed')) {
    if ($null -ne $safe.$name) { throw 'A nonboolean observation was coerced.' }
}
if ($safe.owner_visible -isnot [bool] -or $safe.owner_visible -ne $true) { throw 'One invalid value erased other fixed observations.' }
Write-Output 'fixed-readiness-projection-passed'
""", encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(probe)], capture_output=True, text=True,
        timeout=30, env={**os.environ, 'FORGE_READINESS_TEST_SCRIPT': str(script),
            'FORGE_READINESS_TEST_ROOT': str(tmp_path)}, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, 'PowerShell fixed readiness projection failed.'
    assert result.stdout.strip().endswith('fixed-readiness-projection-passed')
    assert 'synthetic-private-path-key-opaque-value' not in result.stdout


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
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Read-ForgeSmokeReport','Test-ForgeHandoffFloor','Write-ForgeSmokeDiagnostics')},$true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
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
    @('Workspace page load timed out.','workspace-page-timeout','not-executed'),
    @('Workspace bridge initialization timed out.','workspace-bridge-timeout','not-executed'),
    @('Main window failed to start','workspace-sdk-readiness','not-executed'),
    @('GUI is not initialized','workspace-sdk-gui','not-executed'),
    @('Create a web view window first, before invoking this function','workspace-sdk-window','not-executed'),
    @('Integration timed out. Inspect its outcome before retrying a side effect.','integration-timeout','outcome-unknown'),
    @('Native input is paused while Forge confirms a restart.','native-admission','refused'),
    @('Browser panel bounds must be finite numbers.','panel-bounds','not-executed'),
    @('Standalone preview tool did not complete successfully.','standalone-tool','not-executed'),
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
