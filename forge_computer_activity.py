"""Bounded coordinator metadata for computer steps whose helper journal retired."""
from datetime import datetime
import json
import re


TOOLS = frozenset(('computer_windows', 'computer_inspect', 'computer_focus', 'computer_click',
                  'computer_type', 'computer_set_value', 'computer_key', 'computer_coordinate_click',
                  'computer_clipboard_read', 'computer_clipboard_write'))
RUN_STATES = frozenset(('queued', 'running', 'compacting', 'reviewing', 'awaiting_approval',
                       'awaiting_question', 'paused', 'interrupted', 'failed', 'completed', 'cancelled'))
RETIRED_STATES = frozenset(('completed', 'cancelled', 'failed', 'paused', 'interrupted'))
MAX_STEPS = 2048
MAX_SUMMARY_CHARS = 2048
SUMMARY_FIELDS = frozenset(('version', 'run_id', 'parent_run_id', 'chat_id', 'parent_chat_id',
                            'status', 'action_count', 'action_count_truncated', 'latest'))
LATEST_FIELDS = frozenset(('invocation_id', 'tool', 'completed_at'))


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is not None


def invocation_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', value) else None


def completion_time(value):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def retired_summary(value, row):
    """Accept only exact metadata bound to the surviving SQL run relationships."""
    if isinstance(value, str):
        if len(value) > MAX_SUMMARY_CHARS: return None
        try: value = json.loads(value)
        except (ValueError, RecursionError): return None
    if not isinstance(value, dict) or set(value) != SUMMARY_FIELDS:
        return None
    if type(value['version']) is not int or value['version'] != 1:
        return None
    bindings = {'run_id': 'id', 'parent_run_id': 'parent_id',
                'chat_id': 'chat_id', 'parent_chat_id': 'parent_chat_id'}
    if any(not identifier(value[key]) or value[key] != row[field] for key, field in bindings.items()):
        return None
    if value['run_id'] == value['parent_run_id'] or value['chat_id'] == value['parent_chat_id']:
        return None
    if not isinstance(value['status'], str) or value['status'] not in RETIRED_STATES or value['status'] != row['status']:
        return None
    count, truncated, latest = value['action_count'], value['action_count_truncated'], value['latest']
    if type(count) is not int or not 0 <= count <= MAX_STEPS or type(truncated) is not bool:
        return None
    if truncated and count != MAX_STEPS:
        return None
    if count == 0:
        if latest is not None: return None
    elif (not isinstance(latest, dict) or set(latest) != LATEST_FIELDS or
          invocation_id(latest['invocation_id']) is None or not isinstance(latest['tool'], str) or latest['tool'] not in TOOLS or
          completion_time(latest['completed_at']) is None):
        return None
    # This object supplies counts/labels only. It never carries an artifact or
    # snapshot reference and never authorizes a tool replay.
    return value


def capture_summary(db, child, parent):
    """Call in the retirement write transaction, before deleting invocations."""
    tool_marks = ','.join('?' for _ in TOOLS)
    records = db.execute('SELECT id,name,updated_at FROM invocations WHERE run_id=? '
        'AND name IN (' + tool_marks + ") AND status='completed' "
        'ORDER BY julianday(updated_at) DESC,updated_at DESC,rowid DESC LIMIT ?',
        (child['id'], *TOOLS, MAX_STEPS + 1)).fetchall()
    if any(completion_time(record['updated_at']) is None for record in records):
        return None
    latest = max(records, key=lambda record: completion_time(record['updated_at'])) if records else None
    summary = {'version': 1, 'run_id': child['id'], 'parent_run_id': parent['id'],
               'chat_id': child['chat_id'], 'parent_chat_id': parent['chat_id'],
               'status': child['status'], 'action_count': min(len(records), MAX_STEPS),
               'action_count_truncated': len(records) > MAX_STEPS,
               'latest': {'invocation_id': latest['id'], 'tool': latest['name'],
                          'completed_at': latest['updated_at']} if latest else None}
    row = db.execute('SELECT r.id,r.parent_id,r.chat_id,r.status,p.chat_id AS parent_chat_id '
        'FROM runs r JOIN runs p ON p.id=r.parent_id WHERE r.id=?', (child['id'],)).fetchone()
    return retired_summary(summary, row) if row else None
