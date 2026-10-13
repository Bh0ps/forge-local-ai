"""Retire consumed helper conversations without revoking recovery evidence.

Run identities and bounded summaries remain attached to their parent. Full
conversation and provider continuation state are private, temporary working
data. This coordinator operation never removes user files or worktrees.
"""
import json

from forge_store import encode
from storage import _now


CLOSED_PARENTS = {'completed', 'cancelled'}
RUN_FIELDS = ('id', 'chat_id', 'parent_id', 'goal_id', 'project_id', 'agent_id',
    'mode', 'status', 'readonly', 'created_at', 'updated_at', 'request_message_id',
    'assignment_id', 'retry_of', 'attempt', 'retry_count', 'assignment_required',
    'assignment_state', 'assignment_result_id', 'result_consumed', 'result_consumed_by',
    'rounds', 'tools', 'output_tokens', 'elapsed_seconds', 'returned_model_last',
    'response_provider', 'last_finish_reason', 'failure', 'worktree_id', 'workspace_project_id',
    'source_key', 'helper_chat_owned')


def cleanup_helpers(store, live_run_ids=()):
    """One transaction proves ownership, snapshots the result, and removes data.

    Caller holds RunManager.lock while supplying its live worker identities.
    Paused/interrupted parents, unknown actions and unanswered inputs keep the
    complete journal. SQLite reuses freed pages; there is no blocking VACUUM.
    """
    live = set(live_run_ids)
    cleaned = []
    with store._connection(transaction='write') as db:
        runs = {row['id']: json.loads(row['data']) for row in db.execute('SELECT id,data FROM runs')}
        entities = [(row['kind'], row['id'], json.loads(row['data']))
                    for row in db.execute('SELECT kind,id,data FROM entities')]
        parents = [run for run in runs.values() if not run.get('parent_id') and
                   run['status'] in CLOSED_PARENTS and run['id'] not in live]
        for parent in parents:
            if db.execute("SELECT 1 FROM invocations WHERE run_id=? AND status IN ('prepared','running','outcome_unknown') LIMIT 1", (parent['id'],)).fetchone():
                continue
            if db.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='pending' LIMIT 1", (parent['id'],)).fetchone():
                continue
            if any(item.get('run_id') == parent['id'] and
                   (kind == 'questions' and item.get('status') == 'pending' or
                    kind == 'steers' and item.get('status') == 'queued')
                   for kind, _, item in entities):
                continue
            children = [run for run in runs.values() if run.get('parent_id') == parent['id']]
            for child in children:
                if child.get('helper_cleanup') or child.get('helper_chat_owned') is False or child['id'] in live or not child.get('result_consumed'):
                    continue
                if child.get('result_consumed_by', parent['id']) != parent['id']:
                    continue
                # Older failed retry attempts are safe only after the logical
                # assignment was consumed. An unresolved required helper is not.
                if child['status'] not in ('completed', 'cancelled', 'failed', 'paused', 'interrupted'):
                    continue
                if any(run.get('parent_id') == child['id'] and not run.get('helper_cleanup') for run in runs.values()):
                    continue
                chat_id = child['chat_id']
                owners = [run for run in runs.values() if run['chat_id'] == chat_id]
                if len(owners) != 1 or owners[0]['id'] != child['id'] or chat_id == parent['chat_id']:
                    continue
                if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                    continue
                messages = db.execute('SELECT id,role,content,metadata FROM messages WHERE chat_id=? ORDER BY id', (chat_id,)).fetchall()
                # Legacy helper chats predate the admission ownership marker.
                # The original request must be their first message, and every
                # later user input must be an owned steering/question response.
                if not messages or messages[0]['id'] != child.get('request_message_id'):
                    continue
                if any(row['role'] == 'user' and row['id'] != child['request_message_id'] and
                       json.loads(row['metadata']).get('interaction_run_id') != child['id'] for row in messages):
                    continue
                linked = [(kind, identity, item) for kind, identity, item in entities if
                          item.get('chat_id') == chat_id or chat_id in (item.get('chat_ids') or []) or
                          (item.get('scope') or {}).get('chat_id') == chat_id or
                          kind == 'chat_skills' and identity == chat_id]
                if any(kind not in ('questions', 'steers', 'drafts', 'chat_skills') for kind, _, _ in linked):
                    continue
                if any((kind == 'questions' and item.get('status') == 'pending') or
                       (kind == 'steers' and item.get('status') == 'queued') or
                       (kind == 'drafts' and any((item.get('content') or {}).get(key) for key in
                        ('text', 'document_ids', 'skills', 'space_ids', 'command_mode')))
                       for kind, _, item in linked):
                    continue
                if db.execute("SELECT 1 FROM invocations WHERE run_id=? AND status IN ('prepared','running','outcome_unknown') LIMIT 1", (child['id'],)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='pending' LIMIT 1", (child['id'],)).fetchone():
                    continue
                if any(item.get('run_id') == child['id'] and
                       (kind == 'questions' and item.get('status') == 'pending' or
                        kind == 'steers' and item.get('status') == 'queued')
                       for kind, _, item in entities):
                    continue
                if db.execute('SELECT 1 FROM channel_routes WHERE chat_id=? LIMIT 1', (chat_id,)).fetchone():
                    continue
                final = next((row for row in reversed(messages) if row['role'] == 'assistant' and
                    not json.loads(row['metadata']).get('tool_calls') and
                    json.loads(row['metadata']).get('status', 'complete') == 'complete'), None)
                report = final['content'].strip() if final else ''
                tool_count = db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name NOT IN ('delegate_agent','agent_result','tools_search','tools_load')", (child['id'],)).fetchone()[0]
                compact = {key: child[key] for key in RUN_FIELDS if key in child}
                settings = child.get('settings', {})
                compact_settings = {key: settings.get(key) for key in ('provider_id', 'model', 'context')}
                # Reviewer isolation is useful audit evidence, not prompt state.
                compact_settings.update({key: settings[key] for key in
                    ('auto_delegate', 'memory_enabled', 'memory_suggestions')
                    if type(settings.get(key)) is bool})
                compact.update(request='', settings=compact_settings,
                    helper_cleanup={'version': 1, 'cleaned_at': _now(), 'tool_count': tool_count,
                        'summary': report[:8000], 'summary_truncated': len(report) > 8000,
                        'messages_removed': len(messages)}, phase='retired', pending_calls=[])
                # These rows have no surviving message references. Deduplication
                # keys, usage totals, worktrees and parent evidence stay intact.
                db.execute('DELETE FROM provider_turn_state WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM invocations WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM approvals WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM channel_callbacks WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM channel_notifications WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM channel_event_journal WHERE run_id=?', (child['id'],))
                db.execute('DELETE FROM run_events WHERE run_id=?', (child['id'],))
                for kind, identity, _ in linked:
                    db.execute('DELETE FROM entities WHERE kind=? AND id=?', (kind, identity))
                db.execute('DELETE FROM messages WHERE chat_id=?', (chat_id,))
                db.execute('DELETE FROM chats WHERE id=?', (chat_id,))
                db.execute('UPDATE runs SET data=? WHERE id=?', (encode(compact), child['id']))
                cleaned.append(child['id'])
    return cleaned
