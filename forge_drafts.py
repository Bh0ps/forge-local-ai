"""Local, scope-bound text drafts with optimistic concurrency.

Drafts are user work, not accepted tasks or evidence. Saving never starts a run,
sends context to a provider, or persists an unsent image.
"""
from hashlib import sha256
import json

from storage import _now


CONFLICT = 'Draft changed in another window. Reload or keep your local edits.'
KINDS = {'chat', 'new_chat', 'builder'}


class DraftManager:
    def __init__(self, store, skill_catalog=None):
        self.store = store
        self.skill_catalog = skill_catalog

    @staticmethod
    def _identifier(value, required=False):
        if value is None and not required:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or not all(c.isalnum() or c in '-_' for c in value):
            raise ValueError('Use a valid saved draft scope identifier.')
        return value

    def scope(self, data):
        supplied = data.get('scope')
        source = supplied if isinstance(supplied, dict) else data
        kind = source.get('kind', source.get('scope_kind'))
        if kind not in KINDS:
            raise ValueError('Choose chat, new_chat or builder draft scope.')
        project = self._identifier(source.get('project_id'))
        scope = {'kind': kind, 'project_id': project}
        if kind == 'chat':
            scope['chat_id'] = self._identifier(source.get('chat_id'), True)
        elif kind == 'builder':
            scope['builder_id'] = self._identifier(source.get('builder_id'), True)
        if any(source.get(key) for key in ('chat_id', 'builder_id') if key not in scope):
            raise ValueError('The draft scope contains an unrelated owner.')
        return scope

    @staticmethod
    def key(scope):
        return sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _available(self, scope):
        try:
            if scope['project_id']:
                self.store.get_project(scope['project_id'])
            if scope['kind'] == 'chat':
                with self.store._connection() as db:
                    row = db.execute('SELECT project_id FROM chats WHERE id=?', (scope['chat_id'],)).fetchone()
                if not row:
                    return False
                if row['project_id'] != scope['project_id']:
                    raise ValueError('The chat moved to another project. Preserve your text and reload its scope.')
            elif scope['kind'] == 'builder':
                builder = self.store.entity('builders', scope['builder_id'])
                if builder.get('project_id') != scope['project_id']:
                    raise ValueError('The Builder belongs to another project.')
            return True
        except ValueError as exc:
            if 'another project' in str(exc) or 'moved' in str(exc):
                raise
            return False

    @staticmethod
    def _references(values, maximum):
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in values):
            raise ValueError('Draft references must be a bounded list of saved IDs.')
        return list(dict.fromkeys(values))

    def content(self, data, scope):
        if not isinstance(data, dict):
            raise ValueError('Draft content must be an object.')
        allowed = {'text', 'document_ids', 'skills', 'space_ids', 'brief', 'base_revision'}
        if set(data) - allowed:
            raise ValueError('Drafts store text and references only; unsent images remain in session memory.')
        text = data.get('text', '')
        if not isinstance(text, str) or len(text) > 72000:
            raise ValueError('Draft text must contain at most 72,000 characters.')
        result = {'text': text, 'document_ids': self._references(data.get('document_ids'), 20),
                  'skills': self._references(data.get('skills'), 32), 'space_ids': self._references(data.get('space_ids'), 5)}
        if 'brief' in data:
            if scope['kind'] != 'builder' or not isinstance(data['brief'], dict):
                raise ValueError('Brief drafts belong to a Builder.')
            # A draft is never a back door for setting runtime gates or preview state.
            brief_keys = {'id', 'project_id', 'chat_id', 'goal_id', 'title', 'objective', 'audience', 'constraints',
                          'requirements', 'directory', 'template', 'deliverable', 'outputs', 'design', 'revision',
                          'pages', 'flows', 'visual_direction', 'acceptance_criteria'}
            if set(data['brief']) - brief_keys:
                raise ValueError('Save editable brief fields only, not runtime/check evidence.')
            result['brief'] = data['brief']
        if 'base_revision' in data:
            if type(data['base_revision']) is not int or data['base_revision'] < 0:
                raise ValueError('Use a nonnegative saved brief revision.')
            result['base_revision'] = data['base_revision']
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError('Draft content must be valid finite JSON.') from None
        if len(encoded.encode()) > 256000:
            raise ValueError('The draft is too large; retain longer material as a document reference.')
        return json.loads(encoded)

    def _stale(self, scope, content):
        stale = []
        for field, kind in (('document_ids', 'documents'), ('space_ids', 'spaces')):
            for identifier in content.get(field, []):
                try:
                    item = self.store.entity(kind, identifier)
                    if item.get('project_id') and item['project_id'] != scope['project_id']:
                        raise ValueError('Reference is outside this project.')
                    if kind == 'spaces' and item.get('project_ids') and scope['project_id'] not in item['project_ids']:
                        raise ValueError('Reference is outside this project.')
                except ValueError:
                    stale.append({'kind': kind, 'id': identifier, 'reason': 'Reference is missing or outside this scope.'})
        if content.get('skills') and self.skill_catalog:
            try:
                known = {s.get('id') for s in self.skill_catalog(scope['project_id']) if s.get('enabled', True)}
            except (ValueError, OSError):
                known = set()
            stale.extend({'kind': 'skills', 'id': identity, 'reason': 'Skill is unavailable or disabled.'}
                         for identity in content['skills'] if identity not in known)
        return stale

    def _response(self, record, scope, available):
        content = record.get('content') or {'text': '', 'document_ids': [], 'skills': [], 'space_ids': []}
        return {'id': self.key(scope), 'version': 1, 'scope': scope, 'revision': record.get('revision', 0),
                'content': content, 'updated_at': record.get('updated_at'), 'scope_available': available,
                'stale_refs': self._stale(scope, content)}

    def get(self, data):
        scope = self.scope(data); available = self._available(scope)
        try:
            record = self.store.entity('drafts', self.key(scope))
        except ValueError:
            record = {}
        return self._response(record, scope, available)

    def save(self, data, *, clear=False):
        scope = self.scope(data); available = self._available(scope)
        if not available and not clear:
            raise ValueError('This draft scope is unavailable. Keep your local text until its chat or project is restored.')
        expected = data.get('expected_revision')
        if type(expected) is not int or expected < 0:
            raise ValueError('Supply the draft revision read by this window.')
        content = self.content({} if clear else data.get('content'), scope)
        identity = self.key(scope)
        with self.store._connection(transaction='write') as db:
            row = db.execute("SELECT data FROM entities WHERE kind='drafts' AND id=?", (identity,)).fetchone()
            previous = json.loads(row['data']) if row else {}
            if previous.get('revision', 0) != expected:
                raise ValueError(CONFLICT)
            if previous and previous.get('content') == content and not clear:
                record = previous
            else:
                record = {'id': identity, 'version': 1, 'scope': scope, 'revision': expected + 1,
                          'content': content, 'updated_at': _now()}
                db.execute("INSERT INTO entities(kind,id,data) VALUES('drafts',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                           (identity, json.dumps(record, ensure_ascii=False, allow_nan=False)))
        return self._response(record, scope, available)

    def dispatch(self, action, data):
        if action == 'draft_get':
            return self.get(data)
        if action == 'draft_save':
            return self.save(data)
        if action == 'draft_clear':
            return self.save(data, clear=True)
        raise ValueError('Unknown draft operation.')
