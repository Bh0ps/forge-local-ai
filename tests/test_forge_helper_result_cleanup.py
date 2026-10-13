"""Consumed helper reads remain useful after temporary conversation removal."""
import json
import threading

import pytest

from forge_helper_cleanup import cleanup_helpers
from forge_service import ForgeService
from test_forge_core import Engine
from test_forge_helper_cleanup import assignment, fixture


@pytest.mark.parametrize('status,summary,successful', [('completed', 'Useful synthetic findings', True),
    ('completed', '   ', False), ('failed', 'Useful synthetic findings', False)])
def test_retired_helper_result_reads_bounded_summary_without_chat_or_consumption_mutation(
        tmp_path, monkeypatch, status, summary, successful):
    store, parent, child = fixture(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute("UPDATE messages SET content=? WHERE chat_id=? AND role='assistant'", (summary, child['chat_id']))
    store.update_run(child['id'], status=status)
    assert cleanup_helpers(store) == [child['id']]
    svc = ForgeService(core=Engine(), store=store)
    before = store.run(child['id'])
    monkeypatch.setattr(store, 'get_chat', lambda *args, **kwargs: pytest.fail('Retired chat was read'))
    monkeypatch.setattr(store, 'subscribe_events', lambda *args: pytest.fail('Retired result began waiting'))
    monkeypatch.setattr(store, 'update_run', lambda *args, **kwargs: pytest.fail('Read mutated consumption'))
    try:
        result = svc.agent_result(parent, {'run_id': child['id'], 'wait_seconds': 0}, threading.Event())
        assert result['response'] == summary.strip()
        assert result['finished'] and result['conversation_removed']
        assert result['successful'] is successful and result['needs_attention'] is not successful
        assert result['messages'] == [] and not result['retry_available'] and result['retries_remaining'] == 0
        assert result['result_consumed'] and not result['waiting']
        assert set(result['run']['settings']) == {'provider_id', 'model', 'context'}
        assert not {'checkpoint', 'instructions', 'cloud_scope', 'helper_cleanup', 'request', 'failure'} & result['run'].keys()
        assert 'private-opaque-fixture' not in json.dumps(result) and 'private-signature' not in json.dumps(result)
        assert store.run(child['id']) == before
    finally:
        svc.shutdown()


def test_retired_helper_summary_remains_bounded_and_marks_truncation(tmp_path):
    store, parent, child = fixture(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute("UPDATE messages SET content=? WHERE chat_id=? AND role='assistant'", ('x' * 9000, child['chat_id']))
    assert cleanup_helpers(store) == [child['id']]
    svc = ForgeService(core=Engine(), store=store)
    try:
        result = svc.agent_result(parent, {'run_id': child['id']}, threading.Event())
        assert result['summary_truncated'] and len(result['response']) == 8000
        assert result['successful'] and result['finished']
    finally:
        svc.shutdown()


def test_retired_helper_keeps_direct_parent_ownership_as_first_boundary(tmp_path):
    store, parent, child = fixture(tmp_path)
    foreign = assignment(store)
    assert cleanup_helpers(store) == [child['id']]
    svc = ForgeService(core=Engine(), store=store)
    try:
        with pytest.raises(ValueError, match='not a child'):
            svc.agent_result(foreign, {'run_id': child['id'], 'wait_seconds': 'invalid'}, threading.Event())
    finally:
        svc.shutdown()


def test_cleanup_between_final_run_read_and_message_query_keeps_retained_result(tmp_path, monkeypatch):
    store, parent, child = fixture(tmp_path)
    store.update_run(parent['id'], status='paused')
    svc = ForgeService(core=Engine(), store=store)
    store.update_run(parent['id'], status='completed')
    original = store.run; reads = [0]
    def retire_after_snapshot(identifier):
        run = original(identifier)
        if identifier == child['id']:
            reads[0] += 1
            if reads[0] == 3:
                assert cleanup_helpers(store) == [child['id']]
        return run
    monkeypatch.setattr(store, 'run', retire_after_snapshot)
    try:
        result = svc.agent_result(parent, {'run_id': child['id'], 'wait_seconds': 0}, threading.Event())
        assert result['successful'] and result['conversation_removed']
        assert result['response'] == 'Useful synthetic findings' and result['messages'] == []
        assert reads[0] == 4
    finally:
        svc.shutdown()
