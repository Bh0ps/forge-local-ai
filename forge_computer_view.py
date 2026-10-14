"""Read-only projection of acknowledged desktop evidence, never native input."""
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import stat

from forge_images import provider_image_url
from forge_computer_activity import (MAX_STEPS, RUN_STATES, TOOLS, completion_time,
                                     identifier as _identifier, invocation_id as _invocation_id,
                                     retired_summary)
from PIL import Image

MAX_ARTIFACT_BYTES = 16_000_000


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
        fields = ('SELECT DISTINCT r.id,r.status,r.parent_id,r.chat_id,p.chat_id AS parent_chat_id,'
            "CASE WHEN json_valid(r.data) THEN CASE WHEN json_type(r.data,'$.helper_cleanup.version')='integer' "
            "AND json_extract(r.data,'$.helper_cleanup.version')=1 "
            "AND json_extract(r.data,'$.phase')='retired' "
            "AND json_type(r.data,'$.helper_cleanup.computer_activity')='object' "
            "THEN json_extract(r.data,'$.helper_cleanup.computer_activity') END END AS computer_activity "
            'FROM runs r JOIN scope s ON r.id=s.id LEFT JOIN runs p ON p.id=r.parent_id LIMIT 2049')
        rows = db.execute('WITH RECURSIVE scope(id,depth) AS ('
            'SELECT id,0 FROM runs WHERE chat_id=? UNION ALL '
            'SELECT r.id,s.depth+1 FROM runs r JOIN scope s ON r.parent_id=s.id WHERE s.depth<16) '
            + fields, (chat_id,)).fetchall()
        if len(rows) > 2048:
            raise ValueError('Desktop activity scope exceeds its bound.')
        ids = {row['id'] for row in rows}
        if selected and selected not in ids:
            raise ValueError('This request does not belong to the conversation.')
        if selected:
            rows = db.execute('WITH RECURSIVE scope(id,depth) AS ('
                'SELECT id,0 FROM runs WHERE id=? UNION ALL '
                'SELECT r.id,s.depth+1 FROM runs r JOIN scope s ON r.parent_id=s.id WHERE s.depth<16) '
                + fields, (selected,)).fetchall()
            if len(rows) > 2048: raise ValueError('Desktop activity scope exceeds its bound.')
            ids = {row['id'] for row in rows}
        if any(not _identifier(row['id']) for row in rows):
            raise ValueError('Desktop activity scope is unavailable.')
        states = {row['id']: row['status'] for row in rows}
        parents = {row['id']: row['parent_id'] for row in rows}
        records = []
        summaries = []
        if ids:
            marks, tool_marks = ','.join('?' for _ in ids), ','.join('?' for _ in TOOLS)
            # A retained summary substitutes only for a removed journal. Even
            # non-computer/uncompleted rows must prevent double counting.
            current = {row['run_id'] for row in db.execute(
                'SELECT DISTINCT run_id FROM invocations WHERE run_id IN (' + marks + ')', tuple(ids))}
            summaries = [summary for row in rows if row['id'] not in current
                and (summary := retired_summary(row['computer_activity'], row)) is not None]
            records = db.execute('SELECT id,run_id,name,updated_at FROM invocations '
                'WHERE run_id IN (' + marks + ') AND name IN (' + tool_marks + ") AND status='completed' "
                'ORDER BY julianday(updated_at) DESC,updated_at DESC,rowid DESC LIMIT 2049',
                (*ids, *TOOLS)).fetchall()
        retired_count = sum(summary['action_count'] for summary in summaries)
        retired_bounded = retired_count > MAX_STEPS or any(summary['action_count_truncated'] for summary in summaries)
        bounded = len(records) + retired_count > MAX_STEPS or retired_bounded
        records = records[:MAX_STEPS]
        inspected = next((row for row in records if row['name'] == 'computer_inspect'), None)
        inspected_result = db.execute("SELECT result FROM invocations WHERE id=? AND run_id=? AND name='computer_inspect' AND status='completed'", (inspected['id'], inspected['run_id'])).fetchone()[0] if inspected else None
    earlier = [{'id': summary['latest']['invocation_id'], 'run_id': summary['run_id'],
                'name': summary['latest']['tool'], 'updated_at': summary['latest']['completed_at']}
               for summary in summaries if summary['latest']]
    candidates = [*records, *earlier]
    latest = max(candidates, key=lambda row: completion_time(row['updated_at']) or
        datetime.min.replace(tzinfo=timezone.utc)) if candidates else None
    owner = selected or (latest['run_id'] if latest else None)
    if not selected:
        visited = set()
        while owner and parents.get(owner) in states and owner not in visited:
            visited.add(owner)
            owner = parents[owner]
    result = {'chat_id': chat_id, 'scope_run_id': selected, 'mode': 'shared_desktop',
              'run_id': owner, 'invocation_id': _invocation_id(latest['id']) if latest else None,
              'tool': latest['name'] if latest else None, 'status': 'idle',
              'action_count': min(len(records) + retired_count, MAX_STEPS), 'action_count_truncated': bounded,
              'retired_action_count': min(retired_count, MAX_STEPS), 'retired_action_count_truncated': retired_bounded,
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
