"""Interrupted tool results remain recoverable without replaying any action."""
import json

import pytest

from recovery import recover_interrupted_tools
from storage import Store


def call(name, **arguments):
    return {'function': {'name': name, 'arguments': arguments}}


@pytest.fixture
def saved(tmp_path):
    store = Store(tmp_path / 'state')
    chat = store.create_chat()
    store.add_message(chat['id'], 'user', 'Help with the project.')
    return store, chat['id']


def test_chat_without_tools_is_unchanged(saved, monkeypatch):
    store, chat_id = saved
    store.add_message(chat_id, 'assistant', 'Hello.', status='partial')
    original = store.get_active_chat(chat_id)
    monkeypatch.setattr(store, 'add_message', lambda *args, **kwargs: pytest.fail('No recovery write expected'))
    assert recover_interrupted_tools(store, chat_id) == original


def test_completed_old_block_is_unchanged_and_only_missing_results_are_repaired(saved):
    store, chat_id = saved
    store.add_message(chat_id, 'assistant', '', tool_calls=[call('read_file', path='old.py')])
    store.add_message(chat_id, 'tool', '{"ok":true}', tool_name='read_file')
    store.add_message(chat_id, 'assistant', 'Old task completed.')
    store.add_message(chat_id, 'user', 'Now update both files.')
    interrupted = store.add_message(chat_id, 'assistant', '', tool_calls=[
        call('write_file', path='one.py', content='one'), call('write_file', path='two.py', content='two')])
    store.add_message(chat_id, 'tool', '{"ok":false,"error":"one failed"}', tool_name='write_file')
    original = store.get_chat(chat_id)['messages']
    repaired = recover_interrupted_tools(store, chat_id)
    assert repaired['messages'][:-1] == original
    added = repaired['messages'][-1]
    assert added['role'] == 'tool' and added['tool_name'] == 'write_file'
    assert added['status'] == 'interrupted'
    result = json.loads(added['content'])
    assert result['execution_outcome'] == 'unknown'
    assert 'may already have happened' in result['error']
    assert result['sidekick_recovery'] == {'assistant_message_id': interrupted['id'], 'call_index': 1}


def test_legacy_user_after_unresolved_call_is_not_confused_with_later_completed_call(saved):
    store, chat_id = saved
    old = store.add_message(chat_id, 'assistant', '', tool_calls=[call('run_command', argv=['tool', 'old'])])
    store.add_message(chat_id, 'user', 'Continue after the crash.')
    store.add_message(chat_id, 'assistant', '', tool_calls=[call('run_command', argv=['tool', 'new'])])
    store.add_message(chat_id, 'tool', '{"ok":true}', tool_name='run_command')
    store.add_message(chat_id, 'assistant', 'The later command completed.')
    original = store.get_chat(chat_id)['messages']
    repaired = recover_interrupted_tools(store, chat_id)
    assert repaired['messages'][:-1] == original
    result = json.loads(repaired['messages'][-1]['content'])
    assert result['sidekick_recovery']['assistant_message_id'] == old['id']


def test_recovery_is_idempotent_across_store_restarts(saved):
    store, chat_id = saved
    store.add_message(chat_id, 'assistant', '', tool_calls=[call('run_command', argv=['example'])])
    repaired = recover_interrupted_tools(store, chat_id)
    restored = Store(store.data_dir)
    assert recover_interrupted_tools(restored, chat_id) == repaired


def test_partial_recovery_write_failure_resumes_without_duplicate_records(saved, monkeypatch):
    store, chat_id = saved
    store.add_message(chat_id, 'assistant', '', tool_calls=[call('read_file', path='one'), call('read_file', path='two')])
    real = store.add_message
    writes = []

    def interrupted_write(*args, **kwargs):
        if writes:
            raise ValueError('Simulated storage interruption')
        writes.append(real(*args, **kwargs))
        return writes[-1]

    monkeypatch.setattr(store, 'add_message', interrupted_write)
    with pytest.raises(ValueError, match='storage interruption'):
        recover_interrupted_tools(store, chat_id)
    monkeypatch.setattr(store, 'add_message', real)
    repaired = recover_interrupted_tools(store, chat_id)
    records = [row for row in repaired['messages'] if row.get('status') == 'interrupted']
    assert len(records) == 2
    assert [json.loads(row['content'])['sidekick_recovery']['call_index'] for row in records] == [0, 1]
    assert recover_interrupted_tools(store, chat_id) == repaired


def test_compacted_tool_history_is_not_reintroduced(saved):
    store, chat_id = saved
    old = store.add_message(chat_id, 'assistant', '', tool_calls=[call('read_file', path='old')])
    store.set_summary(chat_id, 'Archived earlier interrupted attempt.', old['id'])
    store.add_message(chat_id, 'user', 'Fresh task.')
    original = store.get_active_chat(chat_id)
    assert recover_interrupted_tools(store, chat_id) == original


def test_out_of_order_named_results_complete_the_correct_calls(saved):
    store, chat_id = saved
    store.add_message(chat_id, 'assistant', '', tool_calls=[call('read_file', path='one'), call('list_tasks')])
    store.add_message(chat_id, 'tool', '{"tasks":[]}', tool_name='list_tasks')
    store.add_message(chat_id, 'tool', '{"ok":true}', tool_name='read_file')
    original = store.get_active_chat(chat_id)
    assert recover_interrupted_tools(store, chat_id) == original
