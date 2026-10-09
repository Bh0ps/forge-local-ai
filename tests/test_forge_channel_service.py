"""Connected channels cross the real service and HTTP security boundary locally."""
import json
import sqlite3
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from forge_channels import ChannelManager, quarantine_after_rollback
from forge_host import PairingAuthority
from forge_service import ForgeService
from forge_store import encode
from model_manager import create_app
from project_tools import ProjectTools
from test_forge_channels import (env, configured, inbound, signed, update,
                                webhook_config)
from test_forge_workspaces import ScriptedEngine


def routed_request(env, config, external_id='1'):
    env.manager.receive_telegram(config['id'], update(int(external_id)))
    chat_id = env.manager._route(env.service.store.entity('channels', config['id']), '42')
    return {'text': 'Local integration request.', 'chat_id': chat_id, 'project_id': config.get('project_id'),
            'model': 'fixture', 'permission_profile': config['permission_profile']}


def test_channel_acceptance_commits_user_message_run_and_inbound_before_launch(env):
    config = configured(env)
    data = routed_request(env, config)
    captured = []
    def launch(run):
        record = inbound(env, config['id'], 1)
        assert record['status'] == 'dispatched' and record['run_id'] == run['id']
        chat = env.service.store.get_chat(run['chat_id'])
        assert len(chat['messages']) == 1 and chat['messages'][0]['id'] == run['request_message_id']
        assert chat['messages'][0]['content'] == data['text']
        with env.service.store._connection() as db:
            assert db.execute('SELECT run_id FROM request_keys WHERE source_key=?', (run['source_key'],)).fetchone()[0] == run['id']
        captured.append(run)
    env.service.jobs._launch = launch
    first = env.service.channel_start(data, config['id'], '1')
    second = env.service.channel_start(data, config['id'], '1')
    assert first['id'] == second['id'] and len(captured) == 1
    assert len(env.service.store.runs()) == 1


def test_missing_original_inbound_rolls_back_request_and_run(env):
    config = configured(env)
    data = routed_request(env, config)
    with pytest.raises(ValueError, match='no longer pending'):
        env.service.channel_start(data, config['id'], 'missing_event')
    assert not env.service.store.runs()
    assert env.service.store.get_chat(data['chat_id'])['messages'] == []
    env.service.jobs._launch.assert_not_called()


def test_restart_request_identity_returns_original_interrupted_run_without_relaunch(env):
    config = configured(env)
    data = routed_request(env, config)
    first = env.service.channel_start(data, config['id'], '1')
    restarted = ForgeService(core=ScriptedEngine(), data_dir=env.service.store.home)
    restarted.jobs._launch = Mock()
    manager = ChannelManager(restarted, vault=env.vault, transport=env.manager.transport, clock=lambda: env.now[0])
    try:
        manager.recover()
        same = restarted.channel_start(data, config['id'], '1')
        assert same['id'] == first['id'] and same['status'] == 'interrupted'
        restarted.jobs._launch.assert_not_called()
        assert len(restarted.store.get_chat(data['chat_id'])['messages']) == 1
        assert manager.receive_telegram(config['id'], update())['duplicate']
    finally:
        restarted.shutdown()


def test_channel_permission_ceiling_blocks_tool_override_escalation(env, tmp_path):
    folder = tmp_path/'permission_project'; folder.mkdir()
    project = env.service.store.create_project('Permission fixture', str(folder))
    env.service.store.update_settings({'permission_profile': 'full_access', 'permission_overrides': {'tool:write_file': 'full_access'}})
    config = configured(env, project_id=project['id'], permission_profile='always_ask')
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    schema = next(s for s in ProjectTools.schemas() if s['function']['name'] == 'write_file')
    assert run['permission_ceiling'] == 'always_ask'
    assert env.service.jobs.registry.permission(run, schema, {'path': 'fixture.txt', 'content': 'sample'}) == 'ask'
    env.service.store.update_settings({'permission_profile': 'deny_access'})
    assert env.service.jobs.registry.permission(run, schema, {'path': 'fixture.txt', 'content': 'sample'}) == 'deny'


def test_configured_research_profile_is_readonly_and_cannot_write(env, tmp_path):
    folder = tmp_path/'research_project'; folder.mkdir()
    project = env.service.store.create_project('Research fixture', str(folder))
    config = configured(env, project_id=project['id'], agent_profile_id='researcher')
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    assert run['agent_id'] == 'researcher' and run['readonly']
    assert not run.get('workspace_project_id')
    schema = next(s for s in ProjectTools.schemas() if s['function']['name'] == 'write_file')
    assert env.service.jobs.registry.permission(run, schema, {'path': 'fixture.txt', 'content': 'sample'}) == 'deny'


def test_configured_writing_profile_uses_reused_managed_git_workspace(env, tmp_path):
    folder = tmp_path/'writer_project'; folder.mkdir()
    (folder/'original.txt').write_text('Original project.', encoding='utf-8')
    env.service._git(folder, ['init', '-b', 'main'])
    env.service._git(folder, ['add', 'original.txt'])
    env.service._git(folder, ['-c', 'user.name=Forge Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Initial fixture'])
    project = env.service.store.create_project('Writer fixture', str(folder))
    config = configured(env, project_id=project['id'], agent_profile_id='coder')
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    assert run['project_id'] == project['id'] and not run['readonly']
    assert run['workspace_project_id'] != project['id'] and run['worktree_id']
    workspace = Path(env.service.store.get_project(run['workspace_project_id'])['path'])
    env.service.jobs.registry.execute(run, 'write_file', {'path': 'fixture_change.txt', 'content': 'Written in worktree.'}, threading.Event())
    assert (workspace/'fixture_change.txt').read_text(encoding='utf-8') == 'Written in worktree.'
    assert not (folder/'fixture_change.txt').exists()
    assert env.service.store.get_chat(run['chat_id'])['project_id'] == project['id']
    env.service.store.update_run(run['id'], status='completed')
    env.manager.receive_telegram(config['id'], update(2, text='Continue writing in this workspace.'))
    env.manager.tick(poll=False)
    next_run = env.service.store.runs()[0]
    assert next_run['workspace_project_id'] == run['workspace_project_id'] and next_run['chat_id'] == run['chat_id']
    assert len(env.service.store.entities('channel_workspaces')) == 1
    assert len(env.service.store.entities('worktrees')) == 1


def local_client(env):
    env.service.channel_manager = env.manager
    return TestClient(create_app(service=env.service, auth=PairingAuthority()), base_url='http://127.0.0.1')


def test_http_signed_ingest_is_scoped_exception_and_generic_routes_require_pairing(env):
    config = webhook_config(env)
    body, headers = signed(env, config, {'id': 'http_event_1', 'text': 'Inspect this local fixture.'})
    client = local_client(env)
    response = client.post('/api/v1/channels/' + config['id'] + '/ingest', content=body, headers=headers)
    assert response.status_code == 200 and response.json()['accepted']
    # Valid HMACs cannot authenticate a general action or change config.
    for path in ('/api/v1/channel_save', '/api/channel_save', '/api/v1/run', '/api/v1/bootstrap'):
        assert client.post(path, content=body, headers={**headers, 'Content-Type': 'application/json'}).status_code == 401
    assert client.post('/api/channels/' + config['id'] + '/ingest', content=body, headers=headers).status_code in (404, 405)
    assert client.post('/api/v1/channels/' + config['id'] + '/ingest', content=body, headers=headers).status_code == 403
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 1


def test_http_webhook_rejects_bad_mac_old_timestamp_huge_body_and_cross_project(env, tmp_path):
    folder = tmp_path/'http_project'; folder.mkdir()
    project = env.service.store.create_project('HTTP fixture', str(folder))
    config = webhook_config(env, project_id=project['id'])
    client = local_client(env)
    path = '/api/v1/channels/' + config['id'] + '/ingest'
    body, headers = signed(env, config, {'text': 'Local fixture request.'})
    assert client.post(path, content=body + b' ', headers=headers).status_code == 403
    old_body, old_headers = signed(env, config, {'text': 'Old fixture.'}, timestamp=env.now[0] - 301)
    assert client.post(path, content=old_body, headers=old_headers).status_code == 403
    assert client.post(path, content=b'x' * 65537, headers=headers).status_code == 413
    escape_body, escape_headers = signed(env, config, {'text': 'Wrong project.', 'project_id': 'other'})
    assert client.post(path, content=escape_body, headers=escape_headers).status_code == 403
    extra_body, extra_headers = signed(env, config, {'text': 'Expanded privilege.', 'permission_profile': 'full_access'})
    assert client.post(path, content=extra_body, headers=extra_headers).status_code == 403
    assert not env.service.store.runs()
    with env.service.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM channel_inbound').fetchone()[0] == 0
    rejected = client.post(path, content=body, headers={**headers, 'Origin': 'https://untrusted.invalid'})
    assert rejected.status_code == 403


def test_monotonic_channel_event_journal_survives_highest_run_event_deletion(env):
    first = env.service.jobs.start({'text': 'First fixture run.'})
    env.service.store.update_run(first['id'], status='completed')
    env.service.store.event(first['id'], 'done', status='completed')
    env.manager.tick(poll=False)
    first_cursor = env.manager._checkpoint('_global', 'event_rowid')
    with env.service.store._connection(transaction='write') as db:
        db.execute('DELETE FROM run_events WHERE run_id=?', (first['id'],))
    second = env.service.jobs.start({'text': 'Second fixture run.'})
    env.service.store.update_run(second['id'], status='completed')
    env.service.store.event(second['id'], 'done', status='completed')
    env.manager.tick(poll=False)
    assert env.manager._checkpoint('_global', 'event_rowid') > first_cursor
    assert {n['run_id'] for n in env.manager.notifications()} == {first['id'], second['id']}


def test_terminal_finish_rolls_back_state_event_and_notification_pointer_together(env):
    run = env.service.jobs.start({'text': 'Atomic finish fixture.'})
    observed = []
    env.service.store.subscribe_events(observed.append)
    with env.service.store._connection(transaction='write') as db:
        db.execute("CREATE TRIGGER fixture_reject_finish BEFORE INSERT ON channel_event_journal BEGIN SELECT RAISE(ABORT,'Fixture journal write failure'); END")
    with pytest.raises(ValueError, match='Fixture journal write failure'):
        env.service.store.finish_run(run['id'], {'status': 'completed'}, {'status': 'completed'})
    assert env.service.store.run(run['id'])['status'] == 'queued'
    assert env.service.store.events(run['id']) == [] and observed == []
    with env.service.store._connection(transaction='write') as db:
        db.execute('DROP TRIGGER fixture_reject_finish')
    def inspect_committed(event):
        assert env.service.store.run(run['id'])['status'] == 'completed'
        assert len(env.service.store.events(run['id'])) == 1
        with env.service.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM channel_event_journal WHERE run_id=?', (run['id'],)).fetchone()[0] == 1
    env.service.store.subscribe_events(inspect_committed)
    env.service.store.finish_run(run['id'], {'status': 'completed'}, {'status': 'completed'})
    assert len(observed) == 1
    env.manager.tick(poll=False)
    assert len(env.manager.notifications()) == 1


def test_deleting_channel_chat_preserves_inbound_dedup_and_allows_new_request(env):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    first = env.service.store.runs()[0]
    env.service.store.finish_run(first['id'], {'status': 'completed'}, {'status': 'completed'})
    env.manager.tick(poll=False)
    assert env.service.dispatch('chat_delete', {'id': first['chat_id']})['ok']
    assert env.manager.receive_telegram(config['id'], update())['duplicate']
    assert inbound(env, config['id'], 1)['status'] == 'dispatched'
    env.manager.receive_telegram(config['id'], update(2, text='A new explicitly sent request.'))
    env.manager.tick(poll=False)
    runs = env.service.store.runs()
    assert len(runs) == 1 and runs[0]['chat_id'] != first['chat_id']
    assert env.service.jobs._launch.call_count == 2


def test_lowered_live_channel_ceiling_applies_to_previously_accepted_run(env, tmp_path):
    folder = tmp_path/'live_ceiling'; folder.mkdir()
    project = env.service.store.create_project('Live channel ceiling', str(folder))
    config = configured(env, project_id=project['id'], permission_profile='full_access')
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    schema = next(s for s in ProjectTools.schemas() if s['function']['name'] == 'write_file')
    args = {'path': 'fixture.txt', 'content': 'Synthetic content.'}
    assert env.service.jobs.registry.permission(run, schema, args) == 'allow'
    env.manager.save({'id': config['id'], 'permission_profile': 'deny_access'})
    assert env.service.jobs.registry.permission(run, schema, args) == 'deny'


def test_channel_child_inherits_live_ceiling_and_deleted_connection_fails_closed(env):
    config = configured(env, permission_profile='full_access')
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    parent = env.service.store.runs()[0]
    child = env.service.agent_start({'agent_id': 'researcher', 'parent_id': parent['id'], 'text': 'Read-only child fixture.'})
    run = env.service.store.run(child['id'])
    assert run['channel_id'] == config['id'] and run['permission_ceiling'] == parent['permission_ceiling']
    schema = {'function': {'name': 'web_search'}}
    assert env.service.jobs.registry.permission(run, schema, {'query': 'Fixture.'}) == 'allow'
    env.manager.save({'id': config['id'], 'permission_profile': 'deny_access'})
    assert env.service.jobs.registry.permission(run, schema, {'query': 'Fixture.'}) == 'deny'
    env.service.store.delete_entity('channels', config['id'])
    assert env.service.jobs.registry.permission(run, schema, {'query': 'Fixture.'}) == 'deny'


def test_project_removal_disables_bound_channel_without_losing_dedup_or_original_files(env, tmp_path):
    folder = tmp_path/'removed_project'; folder.mkdir(); (folder/'preserve.txt').write_text('Preserved original contents.')
    project = env.service.store.create_project('Removed channel project', str(folder))
    config = configured(env, project_id=project['id'])
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    assert env.service.dispatch('project_delete', {'id': project['id']})['folder_preserved']
    current = env.service.store.entity('channels', config['id'])
    assert not current['enabled'] and current['project_missing'] and current['detached_project_id'] == project['id']
    assert env.service.store.run(run['id'])['project_id'] == project['id']
    assert (folder/'preserve.txt').read_text() == 'Preserved original contents.'
    assert inbound(env, config['id'], 1)['status'] == 'dispatched'
    with pytest.raises(ValueError, match='disabled'):
        env.manager.receive_telegram(config['id'], update())


def test_coder_child_preserves_parent_goal_project_and_writes_only_git_workspace(env, tmp_path):
    folder = tmp_path/'goal_coder'; folder.mkdir(); (folder/'original.txt').write_text('Original project.')
    env.service._git(folder, ['init', '-b', 'main']); env.service._git(folder, ['add', 'original.txt'])
    env.service._git(folder, ['commit', '-m', 'Synthetic goal base'])
    project = env.service.store.create_project('Goal coder fixture', str(folder))
    goal = env.service.goal_create({'project_id': project['id'], 'text': 'Implement one synthetic change.'})
    parent = env.service.jobs.start({'project_id': project['id'], 'goal_id': goal['id'], 'text': 'Delegate the coding step.'})
    child = env.service.agent_start({'agent_id': 'coder', 'parent_id': parent['id'], 'text': 'Write the fixture change.'})
    run = env.service.store.run(child['id'])
    assert run['project_id'] == project['id'] and run['goal_id'] == goal['id']
    assert run['workspace_project_id'] != project['id'] and run['worktree_id']
    workspace = Path(env.service.store.get_project(run['workspace_project_id'])['path'])
    env.service.jobs.registry.execute(run, 'write_file', {'path': 'child.txt', 'content': 'Isolated change.'}, threading.Event())
    assert (workspace/'child.txt').read_text() == 'Isolated change.' and not (folder/'child.txt').exists()


def test_non_git_writer_delegation_fails_before_creating_child_or_side_effect(env, tmp_path):
    folder = tmp_path/'non_git_parent'; folder.mkdir(); (folder/'original.txt').write_text('Preserved parent.')
    project = env.service.store.create_project('Non-Git parent', str(folder))
    parent = env.service.jobs.start({'project_id': project['id'], 'text': 'Keep the parent project safe.'})
    with pytest.raises(ValueError, match='requires a Git worktree'):
        env.service.agent_start({'agent_id': 'coder', 'parent_id': parent['id'], 'text': 'Attempt writing delegation.'})
    assert len(env.service.store.runs()) == 1 and env.service.jobs._launch.call_count == 1
    assert (folder/'original.txt').read_text() == 'Preserved parent.'


def test_prepared_update_blocks_channel_writes_delivery_and_hmac_ack(env):
    telegram = configured(env)
    webhook = webhook_config(env)
    body, headers = signed(env, webhook, {'id': 'update_boundary_fixture', 'text': 'Do not acknowledge during a snapshot.'})
    delivery = env.manager._enqueue(telegram['id'], '42', 'done', {'chat_id': '42', 'text': 'Queued before update.'}, 'update_boundary_delivery')
    env.service.channel_manager = env.manager
    env.service.update_manager = SimpleNamespace(applying=True)
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'pending'
    assert not [call for call in env.bot.calls if call[0] == 'sendMessage']
    for mutate in (lambda: env.manager.receive_telegram(telegram['id'], update()),
                   lambda: env.manager.save({'id': telegram['id'], 'enabled': False}),
                   lambda: env.manager.test(telegram['id']),
                   lambda: env.manager.pair_begin(telegram['id']),
                   lambda: env.manager.retry(delivery, confirmed=True),
                   lambda: env.manager.ingest(webhook['id'], body, headers),
                   lambda: env.manager.dispatch('channel_delete', {'id': telegram['id']})):
        with pytest.raises(ValueError, match='preparing an update'):
            mutate()
    client = TestClient(create_app(service=env.service), base_url='http://127.0.0.1')
    result = client.post('/api/v1/channels/' + webhook['id'] + '/ingest', content=body, headers=headers)
    assert result.status_code == 403 and result.json()['error'] == 'Channel authentication or request rejected.'
    with env.service.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM channel_inbound').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM channel_replays').fetchone()[0] == 0


def test_rollback_keeps_latest_external_identities_and_requires_fresh_channel_pair(env, tmp_path):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    first = env.service.store.runs()[0]
    env.service.store.finish_run(first['id'], {'status': 'completed'}, {'status': 'completed'})
    env.manager._journal_events()
    restored_path = tmp_path/'restored_snapshot.sqlite3'
    with sqlite3.connect(env.service.store.db_path) as source, sqlite3.connect(restored_path) as destination:
        source.backup(destination)
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'delivered'
    env.manager.receive_telegram(config['id'], update(2, text='Dispatched after the old snapshot.'))
    env.manager.tick(poll=False)
    env.manager.receive_telegram(config['id'], update(3, text='Pending at rollback.'))
    sent_before = len([call for call in env.bot.calls if call[0] == 'sendMessage'])
    with sqlite3.connect(restored_path) as restored, sqlite3.connect(env.service.store.db_path) as latest:
        result = quarantine_after_rollback(restored, latest)
    assert result['channels'] == 1 and result['merged'] > 0
    with sqlite3.connect(restored_path) as restored, sqlite3.connect(env.service.store.db_path) as destination:
        restored.backup(destination)
    current = env.service.store.entity('channels', config['id'])
    assert not current['enabled'] and not current['connected'] and current['requires_review']
    assert current['allowlist'] == [] and 'credential_ref' not in current and not current['owner_id']
    assert env.manager._checkpoint(config['id'], 'telegram_offset') == 4
    assert inbound(env, config['id'], 2)['status'] == 'dispatched' and inbound(env, config['id'], 2)['run_id'] is None
    assert inbound(env, config['id'], 3)['status'] == 'outcome_unknown'
    assert env.manager.outbox()[0]['status'] == 'delivered'
    with env.service.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM channel_routes').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM channel_callbacks').fetchone()[0] == 0
    with pytest.raises(ValueError, match='credential'):
        env.manager.save({'id': config['id'], 'enabled': True})
    env.manager.tick(poll=False)
    env.manager.save({'id': config['id'], 'enabled': True, 'token': '123456:rotated_fixture_secret_abcdefghijklmnop'})
    assert env.manager.receive_telegram(config['id'], update(2))['duplicate']
    env.manager.tick(poll=False)
    assert env.service.jobs._launch.call_count == 2
    assert len([call for call in env.bot.calls if call[0] == 'sendMessage']) == sent_before


def test_rollback_does_not_recreate_channels_in_previous_schema():
    with sqlite3.connect(':memory:') as previous, sqlite3.connect(':memory:') as latest:
        previous.execute('CREATE TABLE entities(kind TEXT,id TEXT,data TEXT)')
        latest.execute('CREATE TABLE channel_inbound(channel_id TEXT)')
        assert quarantine_after_rollback(previous, latest)['previous_schema_without_channels']
        assert [row[0] for row in previous.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ['entities']
