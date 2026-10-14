"""Disposable stage isolation and fixed shutdown evidence, without native I/O."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import desktop


@pytest.mark.parametrize('arguments', [[], ['Forge'], ['--smoke-test'],
    ['--smoke-test', '--native-browser-smoke']])
def test_default_smoke_stage_remains_full(arguments):
    assert desktop._smoke_native_through(arguments, {}) == 'full'


@pytest.mark.parametrize('stage', ['research', 'builder'])
def test_explicit_selector_requires_both_smoke_flags_and_explicit_disposable_home(stage):
    arguments = ['--smoke-test', '--native-browser-smoke', '--smoke-native-through=' + stage]
    assert desktop._smoke_native_through(arguments, {'FORGE_DATA_DIR': 'synthetic-disposable'}) == stage
    for removed in ('--smoke-test', '--native-browser-smoke'):
        with pytest.raises(ValueError, match='requires a disposable browser smoke'):
            desktop._smoke_native_through([value for value in arguments if value != removed],
                                         {'FORGE_DATA_DIR': 'synthetic-disposable'})
    for environment in ({}, {'FORGE_DATA_DIR': ''}):
        with pytest.raises(ValueError, match='requires a disposable browser smoke'):
            desktop._smoke_native_through(arguments, environment)


@pytest.mark.parametrize('selectors', [
    ['--smoke-native-through'], ['--smoke-native-through='],
    ['--smoke-native-through=full'], ['--smoke-native-through=private-synthetic-content'],
    ['--smoke-native-through=research', '--smoke-native-through=builder'],
    ['--smoke-native-through=research', '--smoke-native-through=research'],
])
def test_unknown_or_duplicate_selectors_fail_with_fixed_private_error(selectors):
    with pytest.raises(ValueError) as failure:
        desktop._smoke_native_through(['--smoke-test', '--native-browser-smoke', *selectors],
                                     {'FORGE_DATA_DIR': 'private-synthetic-home'})
    assert str(failure.value) == 'Native stage selection requires a disposable browser smoke.'


def source_functions():
    tree = ast.parse(Path(desktop.__file__).read_text(encoding='utf-8'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    smoke = next(node for node in ast.walk(main) if isinstance(node, ast.FunctionDef) and node.name == 'smoke')
    return main, smoke


def test_selector_validation_precedes_webview_loading_and_coordinator_admission():
    main, _ = source_functions()
    validation = next(index for index, node in enumerate(main.body)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == '_smoke_native_through')
    webview = next(index for index, node in enumerate(main.body)
        if isinstance(node, ast.Import) and any(alias.name == 'webview' for alias in node.names))
    service = next(index for index, node in enumerate(main.body)
        if isinstance(node, ast.Try) and any(isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute) and child.func.attr == '_ensure_service'
            for child in ast.walk(node)))
    assert validation < webview < service


@pytest.mark.parametrize('selected,expected', [
    ('research', ['research']), ('builder', ['research', 'builder']),
    ('full', ['research', 'builder', 'standalone']),
])
def test_actual_early_returns_skip_only_later_stages_and_keep_report_checkpoint_and_quit(selected, expected):
    _, smoke = source_functions()
    outer = next(node for node in smoke.body if isinstance(node, ast.Try))
    native = next(node for node in outer.body if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Constant)
        and node.test.left.value == '--native-browser-smoke')
    selectors = {node.test.comparators[0].value: node for node in native.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == 'native_through'}
    assert set(selectors) == {'research', 'builder'}
    # Execute the actual source branches and its actual outer finally. Native
    # stages are inert probes; this verifies return boundaries, not native work.
    statements = []
    for stage in ('research', 'builder', 'standalone'):
        statements.extend(ast.parse('stage_probe(' + repr(stage) + ', result)').body)
        if stage in selectors:
            statements.append(selectors[stage])
    function = ast.FunctionDef(name='driver', args=ast.arguments(posonlyargs=[],
        args=[ast.arg(arg='native_through'), ast.arg(arg='result')], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[ast.Try(body=statements, handlers=[],
        orelse=[], finalbody=outer.finalbody)], decorator_list=[])
    events, reports = [], []
    def stage_probe(stage, result):
        events.append(stage)
        result[{'research': 'native_browser', 'builder': 'native_preview',
                'standalone': 'native_standalone_preview'}[stage]] = {'synthetic': True}
    namespace = {'stage_probe': stage_probe, 'json': json,
        'os': SimpleNamespace(getenv=lambda name: 'synthetic-disposable-report'),
        'Path': lambda path: SimpleNamespace(write_text=lambda value, **kwargs: reports.append(json.loads(value))),
        '_smoke_checkpoint': lambda phase: events.append(phase),
        'api': SimpleNamespace(_tray_action=lambda action: events.append(action))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 '<actual-disposable-smoke-branches>', 'exec'), namespace)
    result = {}
    namespace['driver'](selected, result)
    assert events == [*expected, 'smoke-complete', 'quit']
    assert reports == [result]
    if selected == 'full':
        assert 'smoke_native_through' not in result
        assert all(key in result for key in ('native_browser', 'native_preview', 'native_standalone_preview'))
    else:
        assert result['smoke_native_through'] == selected
        assert 'native_standalone_preview' not in result
        if selected == 'research':
            assert 'native_preview' not in result


def test_fixed_cleanup_markers_require_active_smoke_and_reject_dynamic_values(monkeypatch):
    emitted = []
    monkeypatch.setattr(desktop.log, 'info', lambda *values: emitted.append(values))
    monkeypatch.setattr(desktop, '_ACTIVE_SMOKE_CHECKPOINT', None)
    desktop._smoke_cleanup_marker('window', 'started')
    assert emitted == []
    monkeypatch.setattr(desktop, '_ACTIVE_SMOKE_CHECKPOINT', object())
    desktop._smoke_cleanup_marker('private-synthetic-window', 'started')
    desktop._smoke_cleanup_marker('window', 'private-synthetic-error')
    desktop._smoke_cleanup_marker('window', 'started')
    desktop._smoke_cleanup_marker('window', 'completed')
    assert emitted == [('Native smoke cleanup %s %s', 'window', 'started'),
                       ('Native smoke cleanup %s %s', 'window', 'completed')]


@pytest.mark.parametrize('logging_failure', [False, True])
def test_actual_owned_cleanup_continues_after_failure_and_logs_only_returned_steps(monkeypatch, logging_failure):
    emitted, cleaned = [], []
    monkeypatch.setattr(desktop, '_ACTIVE_SMOKE_CHECKPOINT', object())
    def info(*values):
        if logging_failure:
            raise RuntimeError('synthetic logger failure')
        emitted.append(values[1:])
    monkeypatch.setattr(desktop.log, 'info', info)
    api = desktop.Bridge(SimpleNamespace())
    api._tray = SimpleNamespace(stop=lambda: cleaned.append('tray'))
    def lifecycle():
        cleaned.append('lifecycle')
        raise RuntimeError('synthetic private failure')
    api._lifecycle = SimpleNamespace(quit=lifecycle)
    api._window = SimpleNamespace(destroy=lambda: cleaned.append('window'))
    assert api.quit()['ok'] is False
    assert cleaned == ['tray', 'lifecycle', 'window'] and api._quit_done.is_set()
    assert emitted == ([] if logging_failure else [('tray', 'started'), ('tray', 'completed'),
        ('lifecycle', 'started'), ('window', 'started'), ('window', 'completed')])


@pytest.mark.parametrize('success', [False, True])
def test_actual_main_finalization_marks_success_only_after_cleanup_and_keeps_resource_finally(monkeypatch, success):
    main, _ = source_functions()
    outer = next(node for node in main.body if isinstance(node, ast.Try))
    function = ast.FunctionDef(name='finish', args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=outer.finalbody, decorator_list=[])
    events = []
    namespace = {'_smoke_cleanup_marker': lambda step, phase: events.append((step, phase)),
        'api': SimpleNamespace(_finish_native_host=lambda: success),
        'sock': SimpleNamespace(close=lambda: events.append('socket-close')),
        'singleton': SimpleNamespace(close=lambda: events.append('singleton-close'))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 '<actual-main-finalization>', 'exec'), namespace)
    if success:
        namespace['finish']()
    else:
        with pytest.raises(RuntimeError, match='could not complete owned cleanup'):
            namespace['finish']()
    assert events == [('main-wait', 'started'), *([('main-wait', 'completed')] if success else []),
                      'socket-close', 'singleton-close']
