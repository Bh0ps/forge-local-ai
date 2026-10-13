"""The publisher-proof seed never restarts a window with unsaved private work."""
import ast
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


@pytest.fixture
def bridge():
    # Execute the actual host class without WebView2, logging setup, OS vaults,
    # installed application state or native startup.
    source = Path(__file__).resolve().parents[1] / 'desktop.py'
    definition = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                      if isinstance(node, ast.ClassDef) and node.name == 'Bridge')
    namespace = {'threading': threading, 'log': logging.getLogger('native-update-fixture')}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
    instance = namespace['Bridge'](pairing=object())
    instance._ensure_service = lambda: pytest.fail('A refused update must not initialize or dispatch into the coordinator')
    instance.quit = lambda: pytest.fail('A refused update cannot close the workspace')
    return instance


@pytest.mark.parametrize('action', ['update_apply', 'update_rollback'])
@pytest.mark.parametrize('frozen', [False, True])
def test_seed_refuses_installer_handoff_before_state_or_helper_side_effects(bridge, monkeypatch, action, frozen):
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('No installer helper can be launched'))
    monkeypatch.setattr(threading, 'Timer', lambda *args, **kwargs: pytest.fail('No delayed quit can be scheduled'))
    result = bridge.call(action, {})
    assert 'unavailable in this version' in result['error']
    assert 'Save your work' in result['error']
    assert not bridge._closing


@pytest.mark.parametrize('data', [{'native_ready_token': 'synthetic-stale-token'},
    {'prepared': True, 'operation_id': 'synthetic-operation', 'command': ['synthetic-installer']},
    {'ready': True, 'workspace_saved': True, 'private_text': 'synthetic-private-draft'}])
def test_untrusted_acknowledgment_fields_cannot_bypass_the_seed_gate(bridge, data):
    result = bridge.call('update_apply', data)
    assert 'unavailable in this version' in result['error']
    assert 'synthetic' not in result['error'] and not bridge._closing


@pytest.mark.parametrize('arguments', [[], ['--forge-home', 'synthetic-private-path', '--forge-parent-pid', '1']])
def test_seed_helper_entry_refuses_before_argument_or_home_loading(monkeypatch, arguments):
    import forge_native_handoff
    monkeypatch.setattr(forge_native_handoff, 'HANDOFF_ENABLED', False)
    source = Path(__file__).resolve().parents[1] / 'desktop.py'
    definition = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                      if isinstance(node, ast.FunctionDef) and node.name == 'main')
    namespace = {'sys': sys, 'os': os}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
    monkeypatch.setattr(sys, 'argv', ['Forge.exe', '--forge-update', 'synthetic-operation', *arguments])
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('The seed cannot start a helper'))
    # The actual entry function must stop before importing WebView2, parsing a
    # private home argument, or initializing any native/coordinator resources.
    with pytest.raises(SystemExit, match='unavailable in this version') as failure:
        namespace['main']()
    assert 'synthetic' not in str(failure.value)


@pytest.mark.parametrize('arguments', [[], ['--forge-home', 'synthetic-private-path', '--forge-parent-pid', '1']])
def test_enabled_helper_still_refuses_bare_arguments_before_home_or_native_startup(monkeypatch, arguments):
    import forge_native_handoff
    source = Path(__file__).resolve().parents[1] / 'desktop.py'
    definition = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                      if isinstance(node, ast.FunctionDef) and node.name == 'main')
    namespace = {'sys': sys, 'os': os}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
    monkeypatch.setattr(forge_native_handoff, 'HANDOFF_ENABLED', True)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'argv', ['Forge.exe', '--forge-update', 'synthetic-operation', *arguments])
    monkeypatch.setattr(forge_native_handoff, 'inherited_pipe', lambda *args: pytest.fail('Bare arguments must not reach a pipe'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Bare arguments cannot launch a helper'))
    with pytest.raises(SystemExit, match='Authenticated native handoff is required') as failure:
        namespace['main']()
    assert 'synthetic' not in str(failure.value)
