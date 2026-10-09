"""Paired model choices retain real profile and coordinator boundaries."""
from pathlib import Path

import pytest

from test_forge_channel_service import routed_request
from test_forge_channels import env, configured, inbound
from test_forge_workspaces import git_project


def profile(env, **changes):
    return env.service.agent_save({'id': 'researcher', 'model': 'profile-default',
        'provider_id': 'ollama', 'context': 8192, 'instructions': 'Inspect the assigned project.',
        'tools': ['read_file'], 'skills': [], **changes})


@pytest.mark.parametrize('command', [None, 'builder', 'plan', 'goal'])
def test_per_chat_model_and_reasoning_override_profile_defaults_only(env, tmp_path, command):
    folder=tmp_path/'project'; folder.mkdir()
    project=env.service.store.create_project('Channel settings fixture', str(folder))
    agent=profile(env)
    config=configured(env, project_id=project['id'], agent_profile_id=agent['id'],
        permission_profile='always_ask')
    before=env.service.store.get_settings()
    data=routed_request(env, config)
    data.update(agent_profile_id=agent['id'], provider_id='channel-default', channel_command=command,
        channel_settings={'provider_id': 'ollama', 'model': 'chat-selected', 'thinking': False})
    result=env.service.channel_start(data, config['id'], '1')
    run=env.service.store.run(result['id'])
    assert run['settings']['model']=='chat-selected' and run['settings']['provider_id']=='ollama'
    assert run['settings']['thinking'] is False and run['settings']['adaptive_model_locked'] is True
    assert run['settings']['adaptive_enabled'] is False
    assert run['settings']['context']==8192 and run['readonly'] is True
    assert run['agent_id']==agent['id'] and run['agent_tools']==['read_file'] and run['skills']==[]
    if command!='builder': assert run['instructions']=='Inspect the assigned project.'
    assert run['project_id']==project['id'] and not run.get('workspace_project_id')
    assert run['permission_ceiling']=='always_ask' and run['settings']['permission_profile']=='always_ask'
    assert env.service.store.get_settings()==before
    assert env.service.store.entity('agents', agent['id'])==agent


def test_default_channel_choice_keeps_profile_model_and_adaptive_preferences(env):
    agent=profile(env)
    config=configured(env, agent_profile_id=agent['id'])
    data=routed_request(env, config); data['agent_profile_id']=agent['id']
    run=env.service.store.run(env.service.channel_start(data, config['id'], '1')['id'])
    assert run['settings']['model']=='profile-default' and run['settings']['provider_id']=='ollama'
    assert run['settings']['thinking'] is env.service.store.get_settings()['thinking']


@pytest.mark.parametrize('overrides, message', [
    ({'provider_id': 'changed', 'model': 'chat-selected'}, 'configured provider changed'),
    ({'provider_id': 'ollama', 'model': ''}, 'valid model'),
    ({'provider_id': 'ollama', 'model': ' leading'}, 'valid model'),
    ({'provider_id': 'ollama', 'model': 'bad\nname'}, 'valid model'),
    ({'provider_id': 'ollama', 'thinking': 'off'}, 'reasoning must be'),
    ({'provider_id': 'ollama', 'permission_profile': 'full_access'}, 'Invalid channel'),
])
def test_invalid_or_stale_channel_settings_fail_before_goal_or_request_commit(env, overrides, message):
    agent=profile(env)
    config=configured(env, agent_profile_id=agent['id'])
    data=routed_request(env, config)
    data.update(agent_profile_id=agent['id'], channel_command='goal', channel_settings=overrides)
    with pytest.raises(ValueError, match=message):
        env.service.channel_start(data, config['id'], '1')
    assert not env.service.store.runs() and not env.service.store.entities('goals')
    assert not env.service.store.get_chat(data['chat_id'])['messages']
    assert inbound(env, config['id'], '1')['status']=='pending'
    env.service.jobs._launch.assert_not_called()


def test_selected_model_stays_selected_with_approved_unlocked_adaptive_profile(env, monkeypatch):
    calibration=env.service.store.save_entity('calibrations', {'id': 'fast-calibration', 'validated': True,
        'immutable_identity': True, 'provider_id': 'ollama', 'model': 'adaptive-model', 'context': 32768,
        'median_total_seconds': 1, 'settings': {'model': 'adaptive-model'}})
    env.service.store.update_settings({'adaptive_enabled': True, 'adaptive_model_locked': False,
        'adaptive_profile_ids': [calibration['id']]})
    monkeypatch.setattr(env.service.providers, 'models', lambda provider: [{'name': 'adaptive-model'}, {'name': 'chat-selected'}])
    config=configured(env)
    data=routed_request(env, config)
    data['channel_settings']={'provider_id': 'ollama', 'model': 'chat-selected'}
    run=env.service.store.run(env.service.channel_start(data, config['id'], '1')['id'])
    assert run['settings']['model']=='chat-selected' and run['settings']['adaptive_model_locked'] is True
    assert run['settings']['adaptive_enabled'] is True
    assert env.service.store.get_settings()['adaptive_model_locked'] is False


def test_explicit_reasoning_equal_to_desktop_default_survives_adaptive_calibration(env):
    calibration=env.service.store.save_entity('calibrations', {'id': 'fast-calibration', 'validated': True,
        'immutable_identity': True, 'provider_id': 'ollama', 'model': 'fixture', 'context': 32768,
        'median_total_seconds': 1, 'settings': {'thinking': False}})
    env.service.store.update_settings({'thinking': True, 'adaptive_enabled': True,
        'adaptive_profile_ids': [calibration['id']]})
    config=configured(env)
    data=routed_request(env, config); data['channel_settings']={'provider_id': 'ollama', 'thinking': True}
    run=env.service.store.run(env.service.channel_start(data, config['id'], '1')['id'])
    assert run['settings']['thinking'] is True and run['settings']['adaptive_enabled'] is False
    assert env.service.store.get_settings()['adaptive_enabled'] is True


def test_model_override_preserves_writing_profile_managed_workspace_and_dedup(env, tmp_path):
    project=git_project(env.service, tmp_path/'writer')
    agent=profile(env, id='coder', role='coder', instructions='Implement in the managed worktree.')
    config=configured(env, project_id=project['id'], agent_profile_id=agent['id'])
    data=routed_request(env, config)
    data.update(agent_profile_id=agent['id'], channel_settings={'provider_id': 'ollama', 'model': 'chat-selected'})
    result=env.service.channel_start(data, config['id'], '1')
    run=env.service.store.run(result['id'])
    assert run['settings']['model']=='chat-selected' and not run['readonly']
    assert run['workspace_project_id']!=project['id'] and run['worktree_id']
    workspace=Path(env.service.store.get_project(run['workspace_project_id'])['path'])
    assert (workspace/'shared.txt').read_text(encoding='utf-8')=='base\n'
    assert run['instructions']=='Implement in the managed worktree.'
    duplicate=env.service.channel_start({**data, 'channel_settings': {'provider_id': 'stale'}}, config['id'], '1')
    assert duplicate['id']==result['id'] and env.service.jobs._launch.call_count==1
    assert len(env.service.store.entities('channel_workspaces'))==1
    assert len(env.service.store.entities('worktrees'))==1
