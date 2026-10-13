"""Temporary helper storage is retired only after durable parent consumption."""
import json

import pytest

from forge_drafts import DraftManager
from forge_helper_cleanup import cleanup_helpers
from forge_store import ForgeStore
from forge_workspace_ui import WorkspaceUI


def assignment(store, parent=None, **changes):
    data = dict(request='Synthetic bounded assignment', project_id=None,
        settings={'provider_id': 'openrouter', 'model': 'fixture', 'context': 4096},
        mode='chat', parent_id=parent and parent['id'], rounds=1, tools=1, output_tokens=8)
    run, _ = store.accept_request({**data, **changes})
    return run


def fixture(tmp_path):
    store = ForgeStore(tmp_path)
    parent = assignment(store)
    child = assignment(store, parent)
    store.save_assistant_turn(child['id'], 'Useful synthetic findings', provider_state={
        'provider_id': 'openrouter', 'model_id': 'fixture', 'reasoning_details': [
            {'type': 'reasoning.encrypted', 'data': 'private-opaque-fixture', 'signature': 'private-signature'}]})
    store.invocation('tool-fixture', child['id'], 'read_file', {'path': 'synthetic'})
    store.invocation_state('tool-fixture', 'completed', {'content': 'synthetic evidence'})
    store.event(child['id'], 'tool', name='read_file', state='completed')
    store.update_run(child['id'], status='completed', result_consumed=True, result_consumed_by=parent['id'])
    store.update_run(parent['id'], status='completed')
    return store, parent, child


def assert_retained(store, child):
    assert store.get_chat(child['chat_id'])['messages']
    assert not store.run(child['id']).get('helper_cleanup')


def test_cleanup_is_atomic_idempotent_and_preserves_parent_activity_and_dedup(tmp_path):
    store, parent, child = fixture(tmp_path)
    store.update_run(child['id'], settings={**child['settings'], 'auto_delegate': False,
        'memory_enabled': False, 'memory_suggestions': False, 'instructions': 'private-policy-fixture'})
    store.add_message(parent['chat_id'], 'assistant', 'Parent result with consumed evidence')
    with store._connection(transaction='write') as db:
        db.execute('INSERT INTO request_keys VALUES(?,?)', ('delegate:fixture', child['id']))
    before = store.get_chat(parent['chat_id'])
    assert cleanup_helpers(store) == [child['id']]
    assert cleanup_helpers(store) == []
    with pytest.raises(ValueError, match='Chat not found'):
        store.get_chat(child['chat_id'])
    with store._connection() as db:
        for table in ('provider_turn_state', 'invocations', 'run_events', 'channel_event_journal', 'approvals'):
            assert db.execute('SELECT COUNT(*) FROM '+table+' WHERE run_id=?', (child['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT run_id FROM request_keys WHERE source_key=?', ('delegate:fixture',)).fetchone()[0] == child['id']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert store.get_chat(parent['chat_id']) == before
    activity = WorkspaceUI(ForgeStore(tmp_path)).chat(parent['chat_id'])['activity_groups'][0]
    assert activity['helper_count'] == 1
    summary = activity['children'][0]
    assert summary['tool_count'] == 1 and summary['summary'] == 'Useful synthetic findings'
    assert summary['conversation_removed'] is True
    assert 'private-opaque-fixture' not in json.dumps(activity)
    assert 'private-signature' not in json.dumps(activity)
    compact = store.run(child['id'])
    assert compact['request'] == '' and 'instructions' not in compact and 'cloud_scope' not in compact
    assert compact['settings'] == {**child['settings'], 'auto_delegate': False,
        'memory_enabled': False, 'memory_suggestions': False}


@pytest.mark.parametrize('status', ['queued', 'running', 'paused', 'failed', 'interrupted', 'waiting_question'])
def test_recoverable_parent_keeps_full_child_state(tmp_path, status):
    store, parent, child = fixture(tmp_path)
    store.update_run(parent['id'], status=status)
    assert cleanup_helpers(store) == []
    assert_retained(store, child)


@pytest.mark.parametrize('owner', ['parent', 'child'])
@pytest.mark.parametrize('status', ['prepared', 'running', 'outcome_unknown'])
def test_unknown_and_unfinished_actions_never_purge(tmp_path, owner, status):
    store, parent, child = fixture(tmp_path)
    identifier = (parent if owner == 'parent' else child)['id']
    store.invocation('unfinished', identifier, 'command_run', {})
    store.invocation_state('unfinished', status)
    assert cleanup_helpers(store) == []
    assert_retained(store, child)


@pytest.mark.parametrize('kind,status', [('questions', 'pending'), ('steers', 'queued')])
@pytest.mark.parametrize('owner', ['parent', 'child'])
def test_unresolved_input_keeps_conversation(tmp_path, kind, status, owner):
    store, parent, child = fixture(tmp_path)
    target = parent if owner == 'parent' else child
    store.save_entity(kind, {'run_id': target['id'], 'chat_id': target['chat_id'], 'status': status})
    assert cleanup_helpers(store) == []
    assert_retained(store, child)


@pytest.mark.parametrize('owner', ['parent', 'child'])
def test_live_worker_blocks_cleanup_even_after_terminal_commit(tmp_path, owner):
    store, parent, child = fixture(tmp_path)
    assert cleanup_helpers(store, [(parent if owner == 'parent' else child)['id']]) == []
    assert_retained(store, child)


def test_unconsumed_child_and_shared_manual_chat_are_preserved(tmp_path):
    store, parent, child = fixture(tmp_path)
    store.update_run(child['id'], result_consumed=False)
    assert cleanup_helpers(store) == []
    store.update_run(child['id'], result_consumed=True)
    store.add_message(child['chat_id'], 'user', 'Manually retained follow-up')
    assert cleanup_helpers(store) == []
    assert_retained(store, child)
    assert child['chat_id'] in {chat['id'] for chat in store.list_chats()}


def test_helper_reusing_a_manual_empty_chat_never_hides_or_deletes_it(tmp_path):
    store=ForgeStore(tmp_path)
    parent=assignment(store)
    manual=store.create_chat(title='User-owned chat')
    child,_=store.accept_request(dict(request='Synthetic helper', project_id=None,
        settings={'provider_id':'openrouter','model':'fixture'},parent_id=parent['id']),manual['id'])
    assert child['helper_chat_owned'] is False
    store.update_run(parent['id'],status='completed')
    store.update_run(child['id'],status='completed',result_consumed=True)
    assert manual['id'] in {chat['id'] for chat in store.list_chats()}
    assert cleanup_helpers(store)==[]


def test_helper_skills_association_is_removed_but_parent_skills_are_preserved(tmp_path):
    store,parent,child=fixture(tmp_path)
    store.save_entity('chat_skills',{'id':child['chat_id'],'skills':['synthetic-child-skill']})
    store.save_entity('chat_skills',{'id':parent['chat_id'],'skills':['synthetic-parent-skill']})
    assert cleanup_helpers(store)==[child['id']]
    assert store.entities('chat_skills')==[store.entity('chat_skills',parent['chat_id'])]


def test_pending_approval_and_owned_unsent_draft_block_cleanup(tmp_path):
    store, parent, child = fixture(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)', ('approval', child['id'], 'tool-fixture', 'hash', 'pending', '{}'))
    assert cleanup_helpers(store) == []
    with store._connection(transaction='write') as db:
        db.execute("UPDATE approvals SET status='denied'")
    scope = {'scope_kind': 'chat', 'chat_id': child['chat_id']}
    DraftManager(store).save({**scope, 'expected_revision': 0, 'content': {'command_mode': 'goal'}})
    assert cleanup_helpers(store) == []
    assert_retained(store, child)


def test_sidebar_hides_only_helper_owned_chats_including_active_helpers(tmp_path):
    store = ForgeStore(tmp_path)
    parent = assignment(store)
    child = assignment(store, parent)
    standalone = assignment(store)
    assert {chat['id'] for chat in store.list_chats()} == {parent['chat_id'], standalone['chat_id']}
    assert store.get_chat(child['chat_id'])['messages']
    store.update_run(child['id'], status='completed')
    store.accept_request(dict(request='Manual reuse', project_id=None, settings={'model': 'fixture'}), child['chat_id'])
    assert child['chat_id'] in {chat['id'] for chat in store.list_chats()}


def test_long_results_are_bounded_and_retry_identity_is_retained(tmp_path):
    store, parent, child = fixture(tmp_path)
    store.save_assistant_turn(child['id'], 'x' * 9000)
    store.update_run(child['id'], retry_of='earlier-fixture', assignment_id='original-fixture', attempt=4, retry_count=3)
    assert cleanup_helpers(store) == [child['id']]
    compact = store.run(child['id'])
    assert compact['retry_of'] == 'earlier-fixture' and compact['retry_count'] == 3
    assert len(compact['helper_cleanup']['summary']) == 8000 and compact['helper_cleanup']['summary_truncated']


def test_partial_cleanup_rolls_back_when_any_delete_fails(tmp_path):
    store, parent, child = fixture(tmp_path)
    with store._connection(transaction='write') as db:
        db.execute("CREATE TRIGGER reject_retire BEFORE DELETE ON chats BEGIN SELECT RAISE(ABORT,'synthetic deletion failed'); END")
    with pytest.raises(ValueError, match='synthetic deletion failed'):
        cleanup_helpers(store)
    assert_retained(store, child)
    assert store.events(child['id'])
    assert store.provider_turn_states(child['id'], [message['id'] for message in store.get_chat(child['chat_id'])['messages']])
