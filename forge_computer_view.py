"""Read-only projection of acknowledged desktop evidence, never native input."""
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import stat

from forge_images import provider_image_url
from PIL import Image

TOOLS = frozenset(('computer_windows', 'computer_inspect', 'computer_focus', 'computer_click',
                  'computer_type', 'computer_set_value', 'computer_key', 'computer_coordinate_click',
                  'computer_clipboard_read', 'computer_clipboard_write'))
RUN_STATES = frozenset(('queued', 'running', 'compacting', 'reviewing', 'awaiting_approval',
                       'awaiting_question', 'paused', 'interrupted', 'failed', 'completed', 'cancelled'))
MAX_ARTIFACT_BYTES = 16_000_000


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)


def _invocation_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', value) else None


def _record(path):
    # Only the exact coordinator artifact ID is resolved. No model path, window
    # title, clipboard value or tool argument participates in retrieval.
    if path.resolve() != path or path.parent.resolve() != path.parent:
        raise ValueError('Unavailable evidence.')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError('Unavailable evidence.')
    with path.open('rb') as source:
        raw = source.read(MAX_ARTIFACT_BYTES + 1)
    after = path.stat()
    if len(raw) > MAX_ARTIFACT_BYTES or path.resolve() != path or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Unavailable evidence.')
    return json.loads(raw)


def _age(value):
    try:
        recorded = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if recorded.tzinfo is None: return None
        return min(315_576_000, max(0, int((datetime.now(timezone.utc) - recorded).total_seconds())))
    except (AttributeError, ValueError, OverflowError):
        return None


def computer_view_state(store, data):
    if not isinstance(data, dict) or set(data) - {'chat_id', 'run_id'} or not _identifier(data.get('chat_id')) or data.get('run_id') is not None and not _identifier(data['run_id']):
        raise ValueError('Select a conversation to view desktop activity.')
    chat_id, selected = data['chat_id'], data.get('run_id')
    with store._connection(transaction='read') as db:
        if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
            raise ValueError('Conversation is unavailable.')
        # A helper can have a separate chat, but only descendants of this exact
        # conversation belong to its activity. A bounded depth handles cycles.
        rows = db.execute('WITH RECURSIVE scope(id,depth) AS ('
            'SELECT id,0 FROM runs WHERE chat_id=? UNION ALL '
            'SELECT r.id,s.depth+1 FROM runs r JOIN scope s ON r.parent_id=s.id WHERE s.depth<16) '
            'SELECT DISTINCT r.id,r.status,r.parent_id FROM runs r JOIN scope s ON r.id=s.id LIMIT 2049', (chat_id,)).fetchall()
        if len(rows) > 2048:
            raise ValueError('Desktop activity scope exceeds its bound.')
        ids = {row['id'] for row in rows}
        if selected and selected not in ids:
            raise ValueError('This request does not belong to the conversation.')
        if selected:
            rows = db.execute('WITH RECURSIVE scope(id,depth) AS ('
                'SELECT id,0 FROM runs WHERE id=? UNION ALL '
                'SELECT r.id,s.depth+1 FROM runs r JOIN scope s ON r.parent_id=s.id WHERE s.depth<16) '
                'SELECT DISTINCT r.id,r.status,r.parent_id FROM runs r JOIN scope s ON r.id=s.id LIMIT 2049', (selected,)).fetchall()
            if len(rows) > 2048: raise ValueError('Desktop activity scope exceeds its bound.')
            ids = {row['id'] for row in rows}
        if any(not _identifier(row['id']) for row in rows):
            raise ValueError('Desktop activity scope is unavailable.')
        states = {row['id']: row['status'] for row in rows}
        parents = {row['id']: row['parent_id'] for row in rows}
        records = []
        if ids:
            marks, tool_marks = ','.join('?' for _ in ids), ','.join('?' for _ in TOOLS)
            records = db.execute('SELECT id,run_id,name,updated_at FROM invocations '
                'WHERE run_id IN (' + marks + ') AND name IN (' + tool_marks + ") AND status='completed' ORDER BY updated_at DESC,rowid DESC LIMIT 2049",
                (*ids, *TOOLS)).fetchall()
        bounded = len(records) > 2048
        records = records[:2048]
        inspected = next((row for row in records if row['name'] == 'computer_inspect'), None)
        inspected_result = db.execute("SELECT result FROM invocations WHERE id=? AND run_id=? AND name='computer_inspect' AND status='completed'", (inspected['id'], inspected['run_id'])).fetchone()[0] if inspected else None
    latest = records[0] if records else None
    owner = selected or (latest['run_id'] if latest else None)
    if not selected:
        visited = set()
        while owner and parents.get(owner) in states and owner not in visited:
            visited.add(owner)
            owner = parents[owner]
    result = {'chat_id': chat_id, 'scope_run_id': selected, 'mode': 'shared_desktop',
              'run_id': owner, 'invocation_id': _invocation_id(latest['id']) if latest else None,
              'tool': latest['name'] if latest else None, 'status': 'idle',
              'action_count': len(records), 'action_count_truncated': bounded,
              'evidence_mode': 'none', 'snapshot': None, 'failure': None}
    state = states.get(result['run_id'])
    result['status'] = state if state in RUN_STATES else ('needs_attention' if state else 'idle')
    if not inspected: return result
    try:
        wrapped = json.loads(inspected_result or '{}')
        artifact = wrapped.get('artifact')
        if not isinstance(artifact, str) or not re.fullmatch(r'[0-9a-f]{32}', artifact):
            raise ValueError('Unavailable evidence.')
        saved = _record(Path(store.home) / 'artifacts' / (artifact + '.json'))
        if not isinstance(saved, dict) or saved.get('name') != 'computer_inspect' or not isinstance(saved.get('result'), dict) or saved['result'] != wrapped.get('result'):
            raise ValueError('Unavailable evidence.')
        evidence = saved['result']
        if evidence.get('error') or evidence.get('not_executed') or evidence.get('ok') is False:
            result['failure'] = 'inspection_refused'
            return result
        result['evidence_mode'] = 'accessibility_only'
        image = evidence.get('image')
        if image is None: return result
        url = provider_image_url(image)
        mime = url.split(';', 1)[0][5:]
        if mime not in ('image/jpeg', 'image/png'):
            raise ValueError('Unavailable evidence.')
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1], validate=True))) as picture:
            width, height = picture.size
        result['snapshot'] = {'run_id': inspected['run_id'], 'invocation_id': _invocation_id(inspected['id']),
                              'data_url': url, 'mime_type': mime, 'width': width, 'height': height,
                              'age_seconds': _age(inspected['updated_at'])}
        result['evidence_mode'] = 'image'
    except (ValueError, TypeError, AttributeError, OSError, RecursionError, OverflowError):
        result['failure'] = 'evidence_unavailable'
        result['evidence_mode'] = 'none'
        result['snapshot'] = None
    return result
