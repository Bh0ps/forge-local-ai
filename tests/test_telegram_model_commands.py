"""Telegram model commands use synthetic catalogs and never contact a live bot/model."""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from test_forge_channels import configured, env, inbound, update


@pytest.fixture
def catalog(env, monkeypatch):
    control={'capabilities': ['tools', 'thinking'], 'thinking': {'values': [False, True], 'default': True},
             'model_info': {'fixture.context_length': 262144}}
    names=['zeta-model', 'Alpha-model', 'fixture', 'named-model', 'fixed-off-model', 'unsupported-model', 'small-model']
    metadata={name: deepcopy(control) for name in names}
    metadata['named-model']['thinking']={'values': ['low', 'medium', 'high'], 'default': 'medium'}
    metadata['fixed-off-model']['thinking']={'values': [False], 'default': False}
    metadata['unsupported-model']={'capabilities': ['tools'], 'model_info': {'fixture.context_length': 262144}}
    metadata['small-model']['model_info']={'fixture.context_length': 4096}
    def dispatch(action, data):
        if action=='models': return {'models': [{'name': name} for name in names]}
        if action=='show': return deepcopy(metadata[data['model']])
        raise AssertionError('Unexpected synthetic engine operation: '+action)
    monkeypatch.setattr(env.service.core, 'dispatch', dispatch)
    env.service.providers.invalidate()
    return SimpleNamespace(names=names, metadata=metadata, control=control)


def send(env, config, number, text, sender=42):
    env.manager.receive_telegram(config['id'], update(number, sender=sender, text=text))
    env.manager.tick(poll=False)
    return inbound(env, config['id'], number)


def choice(env, config):
    return env.manager._checkpoint(config['id'], 'chat_settings:42', {})


def run_for(env, row):
    assert row['status']=='dispatched' and row['run_id'], row
    return env.service.store.run(row['run_id'])


def replies(env):
    return [body['text'] for method, body, _ in env.bot.calls if method=='sendMessage']


def test_bot_suffix_and_number_use_last_displayed_catalog_over_profile_defaults(env, catalog):
    env.service.agent_save({'id': 'researcher', 'model': 'zeta-model', 'provider_id': 'ollama', 'context': 8192})
    config=configured(env, model='Alpha-model', agent_profile_id='researcher')
    assert send(env, config, 1, '/model@forge_fixture_bot list')['status']=='dispatched'
    assert 'Model: zeta-model (configured default)' in replies(env)[-1]
    snapshot=env.manager._checkpoint(config['id'], 'model_catalog:42')
    assert snapshot['models'][:2]==['Alpha-model', 'fixed-off-model']
    # A new earlier name changes today's numeric order; selection must still
    # refer to the exact catalog the owner saw, rather than the fresh ordering.
    catalog.names.reverse(); catalog.names.append('Aardvark-model')
    catalog.metadata['Aardvark-model']=deepcopy(catalog.control)
    assert send(env, config, 2, '/model@forge_fixture_bot 1')['status']=='dispatched'
    assert choice(env, config)['model']=='Alpha-model'
    run=run_for(env, send(env, config, 3, 'Inspect with the chosen model.'))
    assert run['settings']['model']=='Alpha-model' and run['settings']['provider_id']=='ollama'
    assert run['settings']['context']==8192 and run['readonly'] is True
    assert env.service.store.get_settings()['model']=='fixture'
    assert env.service.store.entity('agents', 'researcher')['model']=='zeta-model'
    assert not env.service.core.requests


@pytest.mark.parametrize('enabled', [True, False])
def test_reasoning_command_reaches_real_ollama_payload_from_run_settings(env, catalog, monkeypatch, enabled):
    config=configured(env)
    assert send(env, config, 1, '/reasoning@forge_fixture_bot '+('on' if enabled else 'off'))['status']=='dispatched'
    run=run_for(env, send(env, config, 2, 'Use the explicit reasoning setting.'))
    payloads=[]
    def stream(payload, cancel):
        payloads.append(deepcopy(payload))
        yield {'done': True, 'message': {'content': 'Synthetic result.'}}
    monkeypatch.setattr(env.service.core, '_stream_payload', stream, raising=False)
    # Exercise the shipping provider and Core compiler against the capabilities
    # cached by the command, replacing only the HTTP inference boundary.
    provider=env.service.providers.provider('ollama')
    packets=list(provider.generate({**run['settings'], 'messages': [{'role': 'user', 'content': run['request']}],
                                   'tools': []}, threading.Event()))
    assert packets[-1]['done'] and payloads[0]['think'] is enabled
    assert payloads[0]['model']=='fixture' and run['settings']['thinking'] is enabled
    assert run['settings']['adaptive_enabled'] is False
    assert 'model_metadata' not in payloads[0] and not env.service.core.requests


def test_named_native_default_rejects_off_and_named_intensity_without_losing_choice(env, catalog):
    config=configured(env)
    assert send(env, config, 1, '/model named-model')['status']=='dispatched'
    assert send(env, config, 2, '/reasoning on')['status']=='dispatched'
    before=choice(env, config)
    assert 'model default: medium' in replies(env)[-1]
    for number, command in ((3, '/reasoning off'), (4, '/reasoning high')):
        rejected=send(env, config, number, command)
        assert rejected['status']=='rejected' and rejected['error']
        assert choice(env, config)==before
    assert not env.service.store.runs() and not env.service.core.requests


def test_false_only_metadata_reports_actual_off_and_rejects_on(env, catalog):
    config=configured(env, model='fixed-off-model')
    assert env.service.store.get_settings()['thinking'] is True
    assert send(env, config, 1, '/reasoning')['status']=='dispatched'
    assert 'Reasoning: off (configured default)' in replies(env)[-1] and 'Available: off' in replies(env)[-1]
    before=choice(env, config)
    assert send(env, config, 2, '/reasoning on')['status']=='rejected'
    assert choice(env, config)==before
    assert send(env, config, 3, '/reasoning off')['status']=='dispatched'
    assert choice(env, config)['thinking'] is False and not env.service.core.requests


def test_approved_nonowner_cannot_change_chat_model_or_reasoning(env, catalog):
    config=configured(env, allowlist=[{'sender_id': '42', 'chat_id': '42'}, {'sender_id': '99', 'chat_id': '42'}])
    assert send(env, config, 1, '/model Alpha-model')['status']=='dispatched'
    before=choice(env, config)
    for number, text in ((2, '/model zeta-model'), (3, '/reasoning off')):
        rejected=send(env, config, number, text, sender=99)
        assert rejected['status']=='rejected' and 'approved owner' in rejected['error']
        assert choice(env, config)==before
    assert not env.service.store.runs() and not env.service.core.requests


def test_context_cap_and_unsupported_control_reject_without_changing_saved_settings(env, catalog):
    config=configured(env)
    assert send(env, config, 1, '/model Alpha-model')['status']=='dispatched'
    assert send(env, config, 2, '/reasoning off')['status']=='dispatched'
    before=choice(env, config); desktop=env.service.store.get_settings()
    small=send(env, config, 3, '/model small-model')
    assert small['status']=='rejected' and 'context' in small['error'].lower()
    assert choice(env, config)==before
    unsupported=send(env, config, 4, '/model unsupported-model')
    assert unsupported['status']=='rejected' and 'reasoning' in unsupported['error'].lower()
    assert choice(env, config)==before and env.service.store.get_settings()==desktop
    assert not env.service.store.runs() and not env.service.core.requests


def test_commands_during_active_work_change_only_future_run_settings(env, catalog):
    config=configured(env)
    first=run_for(env, send(env, config, 1, 'Keep these initial settings.'))
    before=deepcopy(first['settings']); desktop=env.service.store.get_settings()
    assert send(env, config, 2, '/model Alpha-model')['status']=='dispatched'
    assert send(env, config, 3, '/reasoning off')['status']=='dispatched'
    assert env.service.store.run(first['id'])['settings']==before and env.service.jobs._launch.call_count==1
    assert env.service.store.get_settings()==desktop
    env.service.store.update_run(first['id'], status='completed')
    future=run_for(env, send(env, config, 4, 'Now use my new selections.'))
    assert future['settings']['model']=='Alpha-model' and future['settings']['thinking'] is False
    assert env.service.jobs._launch.call_count==2 and not env.service.core.requests


def test_default_commands_clear_model_and_reasoning_independently(env, catalog):
    config=configured(env)
    assert send(env, config, 1, '/model Alpha-model')['status']=='dispatched'
    assert send(env, config, 2, '/reasoning off')['status']=='dispatched'
    assert send(env, config, 3, '/model default')['status']=='dispatched'
    assert 'model' not in choice(env, config) and choice(env, config)['thinking'] is False
    assert send(env, config, 4, '/model zeta-model')['status']=='dispatched'
    assert send(env, config, 5, '/reasoning default')['status']=='dispatched'
    assert choice(env, config)['model']=='zeta-model' and 'thinking' not in choice(env, config)
    assert not env.service.store.runs() and not env.service.core.requests


def test_changed_metadata_allows_replacement_of_now_unsupported_saved_reasoning(env, catalog):
    config=configured(env)
    assert send(env, config, 1, '/reasoning off')['status']=='dispatched'
    catalog.metadata['fixture']['thinking']={'values': [True], 'default': True}
    env.service.providers.invalidate()
    assert send(env, config, 2, '/reasoning')['status']=='dispatched'
    assert 'configured off is unsupported' in replies(env)[-1]
    assert send(env, config, 3, '/reasoning on')['status']=='dispatched'
    assert choice(env, config)['thinking'] is True and 'Reasoning: on' in replies(env)[-1]
    assert not env.service.core.requests
