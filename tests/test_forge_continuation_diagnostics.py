"""Synthetic routed failures remain actionable without exposing provider state."""
import json
import threading

import httpx
import pytest

from forge_openrouter import OpenRouterFailure
from forge_service import ForgeService
from forge_store import ForgeStore
from test_forge_channels import env
from test_forge_openrouter import connected, pool_provider
from test_forge_openrouter_continuation import DETAILS, helper, launch, tool_packets
from test_forge_workspaces import wait_finished
from test_inference_timeouts import TimedBody, transport


def test_context_pause_preserves_profile_and_projects_durable_reason_without_executing_tool(env, monkeypatch):
    _, _, fixture, client = connected(env)
    fixture.models[1]['context_length'] = 65536
    pool_provider(env, client)
    parent, profile = helper(env, ['web_search'])
    env.service.agent_save({'id': profile['id'], 'context': 131072})
    before = env.service.store.get_settings()
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=TimedBody([(0, packet) for packet in
            tool_packets('web_search', {'query': 'synthetic'}, details=DETAILS)]))

    transport(monkeypatch, response)
    launch(env)
    try:
        child = env.service.agent_start({'agent_id': profile['id'], 'parent_id': parent['id'], 'text': 'Bounded synthetic assignment.'})
        assert wait_finished(env.service, child)['status'] == 'paused'
        saved = env.service.store.run(child['id'])
        assert saved['tools'] == 0 and len(calls) == 1 and not saved.get('continuation_model')
        assert saved['failure'] == {'provider_id': 'openrouter', 'category': 'continuation_unavailable',
            'continuation_reason': 'context_limit', 'requested_context': 131072, 'model_context_limit': 65536}
        assert env.service.store.entity('agents', profile['id'])['context'] == 131072
        assert env.service.store.get_settings() == before
        fresh = ForgeService(core=env.service.core, store=ForgeStore(env.service.store.home))
        try:
            chat = fresh.dispatch('get_chat', {'id': child['chat_id']})
            group = next(item for root in chat['activity_groups'] for item in [root, *root['children']]
                         if item['run_id'] == child['id'])
            assert group['failure'] == saved['failure']
            diagnostic = fresh.dispatch('run_diagnostics_export', {'run_id': child['id']})
            assert diagnostic['continuation_reason'] == 'context_limit'
            assert diagnostic['requested_context'] == 131072 and diagnostic['model_context_limit'] == 65536
            details = fresh.dispatch('run_activity', {'run_id': parent['id'], 'limit': 1})
            assert details['children'][0]['failure'] == saved['failure']
            parent_result = env.service.agent_result(env.service.store.run(parent['id']), {'run_id': child['id'], 'wait_seconds': 0}, threading.Event())
            public = json.dumps([chat, diagnostic, details, parent_result])
            assert 'synthetic-opaque-state' not in public and 'fixture-signature' not in public
        finally:
            fresh.shutdown()
    finally:
        client.close()


@pytest.mark.parametrize('change, expected', [
    ('paid', 'model_unavailable'), ('capabilities', 'capabilities'), ('small_context', 'context_limit')])
def test_current_catalog_pause_categories_remain_specific(env, change, expected):
    _, provider, fixture, client = connected(env)
    if change == 'paid':
        fixture.models[1]['pricing']['prompt'] = '0.01'
    elif change == 'capabilities':
        fixture.models[1]['supported_parameters'] = []
    else:
        fixture.models[1]['context_length'] = 8192
    try:
        with pytest.raises(OpenRouterFailure) as stopped:
            provider.validate_continuation('fixture/coder:free', 32768, ['tools'])
        assert stopped.value.diagnostics()['continuation_reason'] == expected
        assert stopped.value.category == 'continuation_unavailable'
    finally:
        client.close()


@pytest.mark.parametrize('reason, requested, limit', [
    (['context_limit'], 131072, 65536), ({'private': 'secret'}, 131072, 65536),
    ('context_limit', True, 65536), ('context_limit', 131072, 'private-secret'),
    ('context_limit', 131072, False), ('context_limit', 131072, 262144),
    ('context_limit', 2 ** 63, 65536)])
def test_history_and_diagnostics_ignore_malformed_private_failure_extras(env, reason, requested, limit):
    run = env.service.jobs.start({'text': 'Synthetic task.'})
    env.service.store.update_run(run['id'], status='paused', failure={'category': 'continuation_unavailable',
        'provider_id': 'openrouter', 'continuation_reason': reason, 'requested_context': requested,
        'model_context_limit': limit, 'reasoning_details': [{'data': 'opaque-private'}], 'raw_error': 'raw-private'})
    chat = env.service.dispatch('get_chat', {'id': run['chat_id']})
    diagnostic = env.service.dispatch('run_diagnostics_export', {'run_id': run['id']})
    assert 'requested_context' not in diagnostic and 'model_context_limit' not in diagnostic
    public = json.dumps([chat, diagnostic])
    assert 'opaque-private' not in public and 'raw-private' not in public and 'private-secret' not in public
