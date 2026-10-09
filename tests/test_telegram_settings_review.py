"""Independent Telegram settings boundaries; synthetic transport and models only."""
import httpx
import pytest

from forge_channels import ChannelManager, DeliveryFailure
from test_forge_channels import configured, env, inbound, update


@pytest.fixture
def installed_models(env, monkeypatch):
    engine = env.service.core
    def dispatch(action, data):
        if action == 'models':
            return {'models': [{'name': name} for name in ('fixture', 'studio-model', 'draft-model')]}
        if action == 'show':
            if data['model'] not in ('fixture', 'studio-model', 'draft-model'):
                raise ValueError('Fixture model unavailable.')
            return {'capabilities': ['tools', 'thinking'],
                    'thinking': {'values': [False, True], 'default': True},
                    'model_info': {'fixture.context_length': 262144}}
        raise AssertionError('Unexpected synthetic model operation: ' + action)
    monkeypatch.setattr(engine, 'dispatch', dispatch)
    env.service.providers.invalidate()
    return dispatch


def send(env, config, number, text, recipient=42):
    env.manager.receive_telegram(config['id'], update(number, sender=42, chat=recipient, text=text))
    env.manager.tick(poll=False)
    return inbound(env, config['id'], number)


def accepted_run(env, row):
    assert row['status'] == 'dispatched' and row['run_id'], row
    return env.service.store.run(row['run_id'])


def test_two_approved_recipient_chats_keep_independent_model_and_reasoning(env, installed_models):
    config = configured(env, allowlist=[{'sender_id': '42', 'chat_id': '42'},
                                        {'sender_id': '42', 'chat_id': '99'}])
    global_before = env.service.store.get_settings()
    assert send(env, config, 1, '/model studio-model')['status'] == 'dispatched'
    assert send(env, config, 2, '/reasoning off')['status'] == 'dispatched'
    first = accepted_run(env, send(env, config, 3, 'Use this recipient’s selection.'))
    second = accepted_run(env, send(env, config, 4, 'Use the other recipient’s default.', recipient=99))
    assert first['chat_id'] != second['chat_id']
    assert first['settings']['model'] == 'studio-model' and first['settings']['thinking'] is False
    assert second['settings']['model'] == 'fixture' and second['settings']['thinking'] is True
    assert env.service.store.get_settings() == global_before
    assert env.service.store.entity('channels', config['id']).get('model') == config.get('model')
    assert env.service.store.entity('channels', config['id']).get('provider_id') == config.get('provider_id')
    env.service.jobs._launch.assert_called()
    assert not env.service.core.requests


def test_deleted_route_does_not_transfer_overrides_or_numbered_catalog_to_recreated_chat(env, installed_models):
    config = configured(env)
    assert send(env, config, 1, '/model studio-model')['status'] == 'dispatched'
    assert send(env, config, 2, '/reasoning off')['status'] == 'dispatched'
    assert send(env, config, 3, '/model list')['status'] == 'dispatched'
    original_chat = env.manager._route(config, '42')
    env.service.dispatch('chat_delete', {'id': original_chat})
    recreated = accepted_run(env, send(env, config, 4, 'A new conversation should use its defaults.'))
    assert recreated['chat_id'] != original_chat
    assert recreated['settings']['model'] == 'fixture' and recreated['settings']['thinking'] is True
    env.service.store.update_run(recreated['id'], status='completed')
    stale_choice = send(env, config, 5, '/model 2')
    assert stale_choice['status'] == 'rejected'
    assert stale_choice['error']
    # The original Telegram identities stay deduplicated after deletion.
    assert env.manager.receive_telegram(config['id'], update(1, text='/model studio-model'))['duplicate']
    unchanged = accepted_run(env, send(env, config, 6, 'The rejected choice left defaults intact.'))
    assert unchanged['settings']['model'] == 'fixture' and unchanged['settings']['thinking'] is True
    assert not env.service.core.requests


@pytest.mark.parametrize('command', ['/model list', '/model draft-model'])
def test_failed_catalog_returns_an_error_and_preserves_the_previous_choice(env, installed_models, monkeypatch, command):
    config = configured(env)
    assert send(env, config, 1, '/model studio-model')['status'] == 'dispatched'
    assert send(env, config, 2, '/reasoning off')['status'] == 'dispatched'
    global_before = env.service.store.get_settings()
    def unavailable(action, data):
        if action == 'models':
            raise httpx.ConnectError('token=fixture_secret_do_not_echo')
        return installed_models(action, data)
    monkeypatch.setattr(env.service.core, 'dispatch', unavailable)
    env.service.providers.invalidate()
    failed = send(env, config, 3, command)
    assert failed['status'] == 'rejected' and failed['error']
    replies = [body['text'] for method, body, _ in env.bot.calls if method == 'sendMessage']
    assert replies and 'fixture_secret_do_not_echo' not in '\n'.join(replies)
    monkeypatch.setattr(env.service.core, 'dispatch', installed_models)
    env.service.providers.invalidate()
    run = accepted_run(env, send(env, config, 4, 'The last valid selection still works.'))
    assert run['settings']['model'] == 'studio-model' and run['settings']['thinking'] is False
    assert env.service.store.get_settings() == global_before
    assert not env.service.core.requests


def test_legacy_ready_menu_refresh_retries_with_backoff_and_stops_after_success(env, monkeypatch):
    config = configured(env)
    # An older installation knows only commands_ready, so an upgraded worker
    # must not mistake the old menu for the new command catalog.
    connected = env.service.store.entity('channels', config['id'])
    legacy = {key: value for key, value in connected.items()
              if not key.startswith('commands_') or key in ('commands_ready', 'commands_error')}
    legacy.update(commands_ready=True, commands_error=None)
    env.service.store.save_entity('channels', legacy)
    transport = env.manager.transport.telegram
    attempts, offline = [], [True]
    def telegram(token, method, data, cancel=None):
        if method == 'setMyCommands':
            attempts.append(data)
            if offline[0]:
                raise DeliveryFailure('Menu update unavailable.')
        return transport(token, method, data, cancel)
    monkeypatch.setattr(env.manager.transport, 'telegram', telegram)
    env.manager.tick()
    assert len(attempts) == 1
    saved = env.service.store.entity('channels', config['id'])
    assert saved['connected'] is True and saved['commands_ready'] is False and saved['commands_error']
    for _ in range(10):
        env.manager.tick()
    assert len(attempts) == 1
    env.now[0] += 601
    env.manager.tick()
    assert len(attempts) == 2
    offline[0] = False
    env.now[0] += 601
    env.manager.tick()
    assert len(attempts) == 3
    saved = env.service.store.entity('channels', config['id'])
    assert saved['commands_ready'] is True and not saved.get('commands_error')
    for _ in range(3):
        env.manager.tick()
    assert len(attempts) == 3
    assert {'model', 'reasoning'} <= {item['command'] for item in attempts[-1]['commands']}
    assert not any(method == 'sendMessage' for method, _, _ in env.bot.calls)
    assert not env.service.store.runs() and not env.service.core.requests


@pytest.mark.parametrize('field', ['enabled', 'connected'])
def test_menu_refresh_does_not_connect_disabled_or_unconnected_bots(env, field):
    config = configured(env)
    saved = env.service.store.entity('channels', config['id'])
    saved.update(commands_hash=None, commands_ready=False)
    saved[field] = False
    env.service.store.save_entity('channels', saved)
    before = list(env.bot.calls)
    env.manager.tick()
    assert env.bot.calls == before
    assert env.service.store.entity('channels', config['id'])[field] is False
    assert not env.service.store.runs() and not env.service.core.requests


def test_six_automatic_menu_attempts_survive_restart_and_explicit_test_resets_allowance(env, monkeypatch):
    config = configured(env)
    saved = env.service.store.entity('channels', config['id'])
    saved.update(commands_hash=None, commands_ready=False)
    env.service.store.save_entity('channels', saved)
    original = env.manager.transport.telegram
    attempts = []
    getme_count = sum(method == 'getMe' for method, _, _ in env.bot.calls)
    def telegram(token, method, data, cancel=None):
        if method == 'setMyCommands':
            attempts.append(data)
            raise DeliveryFailure('Synthetic menu outage.')
        return original(token, method, data, cancel)
    monkeypatch.setattr(env.manager.transport, 'telegram', telegram)
    for index in range(6):
        env.manager.tick()
        assert len(attempts) == index + 1
        env.now[0] += 601
    restarted = ChannelManager(env.service, vault=env.vault, transport=env.manager.transport,
                               clock=lambda: env.now[0])
    try:
        restarted.tick()
        assert len(attempts) == 6
        assert sum(method == 'getMe' for method, _, _ in env.bot.calls) == getme_count
        restarted.test(config['id'])
        assert len(attempts) == 7
        assert sum(method == 'getMe' for method, _, _ in env.bot.calls) == getme_count + 1
        env.now[0] += 601
        restarted.tick()
        assert len(attempts) == 8
        assert restarted._checkpoint(config['id'], 'telegram_command_menu')['attempts'] == 1
    finally:
        restarted.shutdown()
    assert not any(method == 'sendMessage' for method, _, _ in env.bot.calls)
    assert not env.service.store.runs() and not env.service.core.requests
