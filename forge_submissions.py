"""Journal UI admission before starting work; uncertain requests never replay effects."""
from hashlib import sha256
import json
import re

from storage import _now


class SubmissionManager:
    ACTIONS = frozenset(('start_chat', 'command', 'plan_build', 'builder_start', 'builder_start_goal'))

    def __init__(self, service):
        self.service = service
        self.store = service.store

    @staticmethod
    def identity(action, data):
        client = data.get('client_submission_id')
        if not isinstance(client, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client):
            raise ValueError('Use a valid submission identity.')
        scope = {key: data.get(key) for key in ('project_id', 'chat_id', 'builder_id', 'id', 'run_id')}
        identity = json.dumps([action, scope, client], sort_keys=True, separators=(',', ':'))
        return sha256(identity.encode()).hexdigest()

    @staticmethod
    def fingerprint(data):
        # Persist only a digest, never the prompt, attachment bytes or secrets.
        payload = {key: value for key, value in data.items() if key not in ('client_submission_id', 'source_key')}
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError('Submission arguments must be finite JSON data.') from None
        return sha256(encoded.encode()).hexdigest()

    def _recover(self, record):
        if record['state'] == 'accepted':
            return {**record['response'], 'submission_reused': True, 'submission_id': record['id']}
        with self.store._connection() as db:
            row = db.execute('SELECT run_id FROM request_keys WHERE source_key=?', (record['source_key'],)).fetchone()
        if row:
            run = self.store.run(row['run_id'])
            response = {key: run.get(key) for key in ('id', 'chat_id', 'status', 'mode', 'request',
                        'request_message_id', 'goal_id', 'project_id', 'parent_id')}
            self.store.save_entity('ui_submissions', {**record, 'state': 'accepted', 'response': response})
            return {**response, 'submission_reused': True, 'submission_id': record['id']}
        if record['state'] == 'rejected':
            raise ValueError(record.get('error', 'The submission was rejected.'))
        raise ValueError('This submission has an unknown outcome. Inspect its saved status before submitting again.')

    def admit(self, action, data, callback):
        if action not in self.ACTIONS:
            raise ValueError('This operation does not support chat submission receipts.')
        identifier = self.identity(action, data)
        fingerprint = self.fingerprint(data)
        with self.service.admission_lock:
            try:
                previous = self.store.entity('ui_submissions', identifier)
            except ValueError:
                previous = None
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('This submission identity already belongs to different content. Preserve your draft and submit it as a new request.')
                return self._recover(previous)
            record = self.store.save_entity('ui_submissions', {
                'id': identifier, 'version': 1, 'action': action, 'fingerprint': fingerprint,
                'source_key': 'ui:' + identifier, 'state': 'admitting', 'created_at': _now(),
                'scope': {key: data.get(key) for key in ('project_id', 'chat_id', 'builder_id', 'id', 'run_id')},
            })
            clean = {key: value for key, value in data.items() if key != 'client_submission_id'}
            clean['source_key'] = record['source_key']
            try:
                result = callback(clean)
            except Exception as exc:
                # A run admitted before an exception is authoritative. Recover
                # it now or after restart rather than launching a second run.
                with self.store._connection() as db:
                    admitted = db.execute('SELECT run_id FROM request_keys WHERE source_key=?', (record['source_key'],)).fetchone()
                if admitted:
                    return self._recover(record)
                state = 'rejected' if getattr(exc, 'not_executed', False) else 'unknown'
                self.store.save_entity('ui_submissions', {**record, 'state': state, 'error': str(exc)[:2000]})
                raise
            if not isinstance(result, dict):
                self.store.save_entity('ui_submissions', {**record, 'state': 'unknown', 'error': 'Admission returned an unexpected response.'})
                raise ValueError('Inspect the submission outcome before retrying.')
            self.store.save_entity('ui_submissions', {**record, 'state': 'accepted', 'response': result})
            return {**result, 'submission_id': identifier}

    def get(self, data):
        identifier = self.identity(data['action'], data)
        record = self.store.entity('ui_submissions', identifier)
        with self.store._connection() as db:
            row = db.execute('SELECT run_id FROM request_keys WHERE source_key=?', (record['source_key'],)).fetchone()
        return {key: record.get(key) for key in ('id', 'version', 'action', 'scope', 'state', 'created_at', 'updated_at')} | {
            'admission_error': record.get('error'),
            'run_id': row['run_id'] if row else (record.get('response') or {}).get('id')}
