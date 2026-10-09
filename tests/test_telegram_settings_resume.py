"""Telegram Resume keeps the original admission and uses chat-scoped choices."""
from test_forge_channels import configured, env
from test_telegram_settings_review import accepted_run, installed_models, send


def pause(env, run):
    env.service.store.update_run(run['id'], status='paused', context_snapshot={'fixture': 'saved'},
                                 settings={**run['settings'], 'context': 4096})


def test_resume_applies_future_chat_choice_without_losing_context_or_checkpoint(env, installed_models):
    config = configured(env)
    run = accepted_run(env, send(env, config, 1, 'Start this task.'))
    pause(env, run)
    assert send(env, config, 2, '/model studio-model')['status'] == 'dispatched'
    assert send(env, config, 3, '/reasoning off')['status'] == 'dispatched'
    assert env.service.store.run(run['id'])['settings']['model'] == 'fixture'
    assert send(env, config, 4, '/resume@forge_fixture_bot')['status'] == 'dispatched'
    resumed = env.service.store.run(run['id'])
    assert resumed['settings']['provider_id'] == 'ollama'
    assert resumed['settings']['model'] == 'studio-model'
    assert resumed['settings']['thinking'] is False and resumed['settings']['adaptive_enabled'] is False
    assert resumed['settings']['context'] == 4096 and resumed['context_snapshot'] == {'fixture': 'saved'}
    assert resumed['request'] == run['request'] and resumed['resume_count'] == 1
    assert env.service.jobs._launch.call_count == 2 and not env.service.core.requests


def test_reset_commands_restore_configured_defaults_on_resume_independently(env, installed_models):
    config = configured(env)
    assert send(env, config, 1, '/model studio-model')['status'] == 'dispatched'
    assert send(env, config, 2, '/reasoning off')['status'] == 'dispatched'
    run = accepted_run(env, send(env, config, 3, 'Keep this conversation.'))
    pause(env, run)
    assert send(env, config, 4, '/model default')['status'] == 'dispatched'
    choice = env.manager._chat_settings(config, '42', run['chat_id'])
    assert 'model' not in choice and choice['thinking'] is False
    assert send(env, config, 5, '/reasoning default')['status'] == 'dispatched'
    assert send(env, config, 6, '/resume')['status'] == 'dispatched'
    resumed = env.service.store.run(run['id'])
    assert resumed['settings']['model'] == 'fixture' and resumed['settings']['thinking'] is True
    assert resumed['settings']['context'] == 4096
    assert env.service.store.get_settings()['model'] == 'fixture'
    assert env.service.store.get_settings()['thinking'] is True and not env.service.core.requests


def test_resume_rejects_provider_change_even_without_a_previous_chat_override(env, installed_models):
    config = configured(env)
    run = accepted_run(env, send(env, config, 1, 'Retain this admitted run.'))
    pause(env, run)
    env.manager.save({'id': config['id'], 'provider_id': 'another-provider'})
    rejected = send(env, config, 2, '/resume')
    assert rejected['status'] == 'rejected' and 'different provider' in rejected['error']
    saved = env.service.store.run(run['id'])
    assert saved['status'] == 'paused' and saved['settings']['provider_id'] == 'ollama'
    assert env.service.jobs._launch.call_count == 1 and not env.service.core.requests


def test_pinned_channel_provider_survives_global_provider_change_on_resume(env, installed_models):
    config = configured(env, provider_id='ollama')
    run = accepted_run(env, send(env, config, 1, 'Remain on this channel’s provider.'))
    pause(env, run)
    env.service.store.update_settings({'provider_id': 'openrouter'})
    assert send(env, config, 2, '/resume')['status'] == 'dispatched'
    resumed = env.service.store.run(run['id'])
    assert resumed['settings']['provider_id'] == 'ollama' and resumed['status'] == 'queued'
    assert env.service.store.get_settings()['provider_id'] == 'openrouter'
    assert env.service.jobs._launch.call_count == 2 and not env.service.core.requests


def test_resume_keeps_outcome_unknown_reconciliation_guard(env, installed_models):
    config = configured(env)
    assert send(env, config, 1, '/model studio-model')['status'] == 'dispatched'
    run = accepted_run(env, send(env, config, 2, 'Do not repeat an uncertain action.'))
    pause(env, run)
    invocation = run['id'] + ':1:0'
    env.service.store.invocation(invocation, run['id'], 'write_file', {'path': 'fixture.txt', 'text': 'saved'})
    env.service.store.invocation_state(invocation, 'outcome_unknown', {'error': 'Fixture uncertain outcome.'})
    rejected = send(env, config, 3, '/resume')
    assert rejected['status'] == 'rejected' and 'outcome-unknown' in rejected['error']
    assert env.service.store.run(run['id'])['status'] == 'paused'
    assert env.service.jobs._launch.call_count == 1 and not env.service.core.requests
