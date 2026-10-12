"""Crash, delivery fairness and actual isolated check-output regressions."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest

from forge_sources import command_outputs, source_manifest
from forge_workflow import verification_sources
from test_forge_channels import env, configured, update
from test_forge_core import service


def test_telegram_pause_cancel_preserve_completed_run(env):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update(1))
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    env.service.store.update_run(run['id'], status='completed')
    for number, command in enumerate(('/pause', '/cancel', '/resume'), 2):
        env.manager.receive_telegram(config['id'], update(number, text=command))
        env.manager.tick(poll=False)
    assert env.service.store.run(run['id'])['status'] == 'completed'
    assert env.service.jobs._launch.call_count == 1


def test_blocked_chunks_do_not_starve_other_replies_and_retry_preserves_order(env):
    config = configured(env)
    first = env.manager._enqueue(config['id'], '42', 'done', {'chat_id': '42', 'text': 'First'}, 'first')
    env.manager._outcome(first, 'outcome_unknown')
    previous = first
    for number in range(35):
        previous = env.manager._enqueue(config['id'], '42', 'done',
            {'chat_id': '42', 'text': str(number), '_forge_previous': previous}, 'part-' + str(number))
    env.manager._enqueue(config['id'], '42', 'command', {'chat_id': '42', 'text': 'Independent approval'}, 'approval')
    env.manager.tick(poll=False)
    assert [body['text'] for method, body, _ in env.bot.calls if method == 'sendMessage'] == ['Independent approval']
    env.manager.retry(first, confirmed=True)
    env.manager.tick(poll=False)
    env.manager.tick(poll=False)
    assert [body['text'] for method, body, _ in env.bot.calls if method == 'sendMessage'] == ['Independent approval', 'First', *map(str, range(35))]


def test_disconnected_channel_does_not_starve_connected_channel(env):
    offline = configured(env)
    online = configured(env, name='Online')
    saved = env.service.store.entity('channels', offline['id'])
    env.service.store.save_entity('channels', {**saved, 'connected': False})
    for number in range(35):
        env.manager._enqueue(offline['id'], '42', 'done', {'chat_id': '42', 'text': 'Waiting'}, 'offline-' + str(number))
    env.manager._enqueue(online['id'], '42', 'command', {'chat_id': '42', 'text': 'Delivered'}, 'online')
    env.manager.tick(poll=False)
    assert [body['text'] for method, body, _ in env.bot.calls if method == 'sendMessage'] == ['Delivered']


@pytest.mark.parametrize('recurrence', ['daily', 'once'])
def test_scheduler_legacy_orphan_advances_without_replay(tmp_path, recurrence):
    svc = service(tmp_path)
    svc.jobs._launch = Mock()
    now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    due = now - timedelta(minutes=1)
    try:
        schedule = svc.schedule_save({'prompt': 'Legacy interrupted work', 'recurrence': recurrence,
            'timezone': 'UTC', 'time': '09:59', 'next_run': due.isoformat()})
        with svc.store._connection(transaction='write') as db:
            db.execute('INSERT INTO occurrences VALUES(?,?,NULL)', (schedule['id'], due.isoformat()))
        svc.tick_schedules(now)
        updated = svc.store.entity('schedules', schedule['id'])
        assert not svc.jobs._launch.called and not svc.store.runs()
        assert 'not replayed' in updated['last_error']
        assert not updated['enabled'] if recurrence == 'once' else datetime.fromisoformat(updated['next_run']) > now
    finally:
        svc.shutdown()


@pytest.mark.parametrize('agent', [False, True])
def test_scheduler_restart_after_admission_links_original_without_relaunch(tmp_path, monkeypatch, agent):
    svc = service(tmp_path)
    svc.jobs._launch = Mock()
    now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    # Equivalent non-UTC source slots must use the same canonical key.
    due = '2026-10-12T10:59:00+01:00'
    profile = svc.agent_save({'name': 'Scheduled fixture', 'role': 'researcher', 'model': 'fixture'}) if agent else None
    schedule = svc.schedule_save({'prompt': 'One admitted occurrence', 'recurrence': 'daily', 'timezone': 'UTC',
        'time': '09:59', 'next_run': due, **({'agent_id': profile['id']} if profile else {})})
    original = svc.schedule_run
    def crashed(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt('Crash between admission and schedule advancement')
    monkeypatch.setattr(svc, 'schedule_run', crashed)
    try:
        with pytest.raises(KeyboardInterrupt): svc.tick_schedules(now)
        run = svc.store.runs()[0]
        assert run['source_key'] == 'schedule:' + schedule['id'] + ':2026-10-12T09:59:00+00:00'
        assert run['schedule_id'] == schedule['id']
    finally:
        svc.shutdown()
    # Restore the durable pre-crash status; graceful fixture cleanup pauses it.
    svc.store.update_run(run['id'], status='queued')
    restored = service(tmp_path)
    restored.jobs._launch = Mock()
    try:
        restored.tick_schedules(now)
        assert not restored.jobs._launch.called and len(restored.store.runs()) == 1
        assert restored.store.run(run['id'])['status'] == 'interrupted'
        updated = restored.store.entity('schedules', schedule['id'])
        assert updated['last_run_id'] == run['id'] and datetime.fromisoformat(updated['next_run']) > now
        with restored.store._connection() as db:
            row = db.execute('SELECT run_id FROM occurrences WHERE schedule_id=?', (schedule['id'],)).fetchone()
        assert row['run_id'] == run['id']
    finally:
        restored.shutdown()


def build_case(tmp_path, source='def test_pass():\n    assert True\n'):
    svc = service(tmp_path)
    svc.jobs._launch = Mock()
    root = tmp_path / 'project'; root.mkdir()
    (root / 'test_sample.py').write_text(source, encoding='utf-8')
    project = svc.store.create_project('Check outputs', str(root))
    chat = svc.store.create_chat(project['id'], 'Check outputs', 'fixture')
    manager = svc.get_builder()
    builder = manager.save({'project_id': project['id'], 'chat_id': chat['id'], 'directory': '.',
        'objective': 'Verify this app', 'requirements': ['Primary flow works']})
    accepted = svc.jobs.start({'text': 'Run checks', 'chat_id': chat['id']})
    return svc, manager, builder, root, svc.store.run(accepted['id'])


def managed_check(svc, manager, builder, run, argv):
    scope = manager.command_verification_scope(run, {'argv': argv, 'cwd': '.'})
    project = svc.store.get_project(builder['project_id'])
    session = svc.command_sessions.start(project['path'], argv, run_id=run['id'], verification_scope=scope)
    completed = svc.command_sessions.wait(session['id'], timeout=20)
    assert completed['status'] == 'completed' and completed['exit_code'] == 0
    return {'id': builder['id'], 'expected_revision': builder['revision'], 'gate': 'functional',
        'status': 'passed', 'source': 'session', 'reference': session['id']}


def test_real_junit_check_passes_and_source_additions_and_backdated_edits_invalidate(tmp_path):
    svc, manager, builder, root, run = build_case(tmp_path)
    try:
        data = managed_check(svc, manager, builder, run, [sys.executable, '-m', 'pytest', '-q', '--junitxml=report.xml'])
        assert (root / 'report.xml').exists()
        checked = manager.check(data)
        assert checked['gates']['functional']['status'] == 'passed'
        assert manager.get(builder['id'])['gates']['functional']['status'] == 'passed'
        snapshot = verification_sources(svc.store, run, {'builder_id': builder['id'], 'scope_path': '.'})
        assert snapshot['fingerprint'] == manager.fingerprint(checked)['sha256']
        original = (root / 'test_sample.py').stat()
        (root / 'test_sample.py').write_text('def test_pass():\n    assert False\n', encoding='utf-8')
        os.utime(root / 'test_sample.py', ns=(original.st_atime_ns, original.st_mtime_ns))
        with pytest.raises(ValueError, match='source files changed'): manager.check(data)
        assert manager.get(builder['id'])['gates']['functional']['status'] == 'pending'
        (root / 'test_sample.py').write_text('def test_pass():\n    assert True\n', encoding='utf-8')
        assert manager.check(data)['gates']['functional']['status'] == 'passed'
        (root / 'new-source.json').write_text('{"route":"new"}', encoding='utf-8')
        with pytest.raises(ValueError, match='source files changed'): manager.check(data)
        (root / 'new-source.json').unlink()
        (root / 'test_sample.py').unlink()
        with pytest.raises(ValueError, match='source files changed'): manager.check(data)
    finally:
        svc.shutdown()


def test_real_check_mutating_source_during_execution_cannot_pass(tmp_path):
    svc, manager, builder, root, run = build_case(tmp_path,
        'from pathlib import Path\ndef test_pass():\n    Path("new-source.py").write_text("value = 1")\n')
    try:
        data = managed_check(svc, manager, builder, run, [sys.executable, '-m', 'pytest', '-q', '--junitxml=report.xml'])
        with pytest.raises(ValueError, match='source files changed'): manager.check(data)
    finally:
        svc.shutdown()


@pytest.mark.parametrize('tracked', [False, True])
def test_report_destination_cannot_hide_existing_source(tmp_path, tracked):
    svc, manager, builder, root, run = build_case(tmp_path)
    try:
        content = '<testsuites><testsuite><testcase name="Source fixture"/></testsuite></testsuites>' if tracked else '<config>Real source</config>'
        (root / 'report.xml').write_text(content, encoding='utf-8')
        if tracked:
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True, capture_output=True)
            subprocess.run(['git', 'add', 'report.xml'], cwd=root, check=True, capture_output=True)
        data = managed_check(svc, manager, builder, run, [sys.executable, '-m', 'pytest', '-q', '--junitxml=report.xml'])
        with pytest.raises(ValueError, match='source files changed'): manager.check(data)
    finally:
        svc.shutdown()


def test_only_validated_coverage_and_compiler_outputs_are_excluded(tmp_path):
    root = tmp_path
    (root / 'main.py').write_text('value = 1', encoding='utf-8')
    argv = ['python', '-m', 'pytest', '--cov=main', '--cov-report=xml:coverage.xml', '--cov-report=json']
    outputs = command_outputs(root, root, argv) + command_outputs(root, root, ['npm', 'run', 'build'])
    before = source_manifest(root, outputs=outputs)
    (root / 'coverage.xml').write_text('<coverage version="7"><packages/></coverage>', encoding='utf-8')
    (root / 'coverage.json').write_text(json.dumps({'meta': {'version': '7'}, 'files': {}}), encoding='utf-8')
    (root / 'project.tsbuildinfo').write_text(json.dumps({'version': '5.9', 'fileNames': ['main.ts']}), encoding='utf-8')
    with sqlite3.connect(root / '.coverage') as db:
        for name in ('coverage_schema', 'meta', 'file', 'context', 'line_bits'): db.execute('CREATE TABLE '+name+'(value TEXT)')
    after = source_manifest(root, outputs=outputs)
    assert before['fingerprint'] == after['fingerprint']
    (root / 'new-source.xml').write_text('<config/>', encoding='utf-8')
    assert source_manifest(root, outputs=outputs)['fingerprint'] != after['fingerprint']
    (root / 'coverage.json').write_text('{"real":"source"}', encoding='utf-8')
    assert 'coverage.json' in {item['path'] for item in source_manifest(root, outputs=outputs)['files']}


def test_compiler_cache_collision_and_html_source_remain_protected(tmp_path):
    cache = tmp_path / 'project.tsbuildinfo'
    cache.write_text('Real source, not a generated cache', encoding='utf-8')
    outputs = command_outputs(tmp_path, tmp_path, ['npm', 'run', 'build'])
    before = source_manifest(tmp_path, outputs=outputs)
    cache.write_text(json.dumps({'version': '5.9', 'fileNames': ['main.ts']}), encoding='utf-8')
    after = source_manifest(tmp_path, outputs=outputs)
    assert before['fingerprint'] != after['fingerprint']
    assert 'project.tsbuildinfo' in {item['path'] for item in after['files']}
    html = tmp_path / 'htmlcov'; html.mkdir()
    (html / 'index.html').write_text('<html>Existing app source</html>', encoding='utf-8')
    (html / 'status.json').write_text(json.dumps({'format': 5, 'version': '7', 'files': {}}), encoding='utf-8')
    html_outputs = command_outputs(tmp_path, tmp_path, ['python', '-m', 'pytest', '--cov=app', '--cov-report=html'])
    assert next(item for item in html_outputs if item['kind'] == 'coverage_html')['eligible'] is False
    assert 'htmlcov/index.html' in {item['path'] for item in source_manifest(tmp_path, outputs=html_outputs)['files']}


def test_generated_html_coverage_files_do_not_hide_other_new_source(tmp_path):
    outputs = command_outputs(tmp_path, tmp_path, ['python', '-m', 'pytest', '--cov=app', '--cov-report=html'])
    before = source_manifest(tmp_path, outputs=outputs)
    report = tmp_path / 'htmlcov'; report.mkdir()
    (report / 'index.html').write_text('<html>Generated by coverage.py</html>', encoding='utf-8')
    (report / 'status.json').write_text(json.dumps({'format': 5, 'version': '7', 'files': {}}), encoding='utf-8')
    assert source_manifest(tmp_path, outputs=outputs)['fingerprint'] == before['fingerprint']
    (report / 'new-source.js').write_text('export const value = 1;', encoding='utf-8')
    after = source_manifest(tmp_path, outputs=outputs)
    assert 'htmlcov/new-source.js' in {item['path'] for item in after['files']}


def test_schedule_advancement_preserves_concurrent_settings_edit(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.jobs._launch = Mock()
    now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
    try:
        schedule = svc.schedule_save({'prompt': 'Original work', 'recurrence': 'daily', 'timezone': 'UTC',
            'time': '09:59', 'next_run': '2026-10-12T09:59:00Z'})
        original = svc.schedule_run
        def user_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            svc.schedule_save({'id': schedule['id'], 'prompt': 'New future work', 'enabled': False})
            return result
        monkeypatch.setattr(svc, 'schedule_run', user_edit)
        svc.tick_schedules(now)
        saved = svc.store.entity('schedules', schedule['id'])
        assert saved['prompt'] == 'New future work' and not saved['enabled']
        assert datetime.fromisoformat(saved['next_run']) > now and saved['last_run_id']
    finally:
        svc.shutdown()


@pytest.mark.parametrize('flag', ['--help', '--collect-only'])
@pytest.mark.parametrize('configured_command', [False, True])
def test_real_pytest_help_and_collection_cannot_pass_a_gate(tmp_path, flag, configured_command):
    svc, manager, builder, root, run = build_case(tmp_path)
    try:
        argv = [sys.executable, '-m', 'pytest', flag]
        if configured_command:
            builder = manager.save({**builder, 'expected_revision': builder['revision'], 'check_commands': {'functional': argv}})
        data = managed_check(svc, manager, builder, run, argv)
        with pytest.raises(ValueError, match='cannot verify a gate'): manager.check(data)
        assert manager.store.entity('builders', builder['id'])['gates']['functional']['status'] == 'pending'
    finally:
        svc.shutdown()


def test_real_pytest_verbose_still_passes_a_gate(tmp_path):
    svc, manager, builder, root, run = build_case(tmp_path)
    try:
        data = managed_check(svc, manager, builder, run, [sys.executable, '-m', 'pytest', '-v'])
        assert manager.check(data)['gates']['functional']['status'] == 'passed'
    finally:
        svc.shutdown()
