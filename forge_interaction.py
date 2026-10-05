"""Durable human input, separate from action permissions and inference."""
from hashlib import sha256
import json
import re
from uuid import uuid4

from forge_store import TERMINAL, encode
from storage import _now


def text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError(f'{label} must contain 1–{maximum:,} characters.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(f'{label} contains invalid Unicode text.') from None
    return value.strip()


class RunInteraction:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    @staticmethod
    def _event(db, run_id, kind, payload):
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('INSERT INTO run_events VALUES(?,?,?,?,?)', (run_id, seq, kind, encode(payload), _now()))
        db.execute('INSERT INTO channel_event_journal(run_id,seq) VALUES(?,?)', (run_id, seq))

    @staticmethod
    def _run(db, identifier):
        row = db.execute('SELECT data FROM runs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Run not found.')
        run = json.loads(row[0])
        chat = db.execute('SELECT archived FROM chats WHERE id=?', (run['chat_id'],)).fetchone()
        if not chat or chat['archived']:
            raise ValueError('This chat is unavailable or archived.')
        return run

    def queue_steer(self, identifier, value, client_id=None):
        value = text(value, 'Steering message', 20000)
        client_id = client_id or uuid4().hex
        if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', client_id):
            raise ValueError('Invalid steering request ID.')
        key = sha256((identifier + ':' + client_id).encode()).hexdigest()
        with self.store._connection(transaction='write') as db:
            run = self._run(db, identifier)
            old = db.execute("SELECT data FROM entities WHERE kind='steers' AND id=?", (key,)).fetchone()
            if old:
                item = json.loads(old[0])
                if item['text'] != value:
                    raise ValueError('This steering request ID was already used for another message.')
                return {'ok': True, 'id': key, 'run_id': identifier, 'status': item['status']}
            if run['status'] in TERMINAL:
                raise ValueError('This run has stopped. Send a new message or Resume first.')
            pending = [json.loads(row[0]) for row in db.execute("SELECT data FROM entities WHERE kind='steers'")]
            if sum(item.get('run_id') == identifier and item.get('status') == 'queued' for item in pending) >= 8:
                raise ValueError('Eight steering messages are already queued. Wait for the next model round.')
            item = dict(id=key, run_id=identifier, chat_id=run['chat_id'], text=value,
                        status='queued', created_at=_now())
            db.execute('INSERT INTO entities VALUES(?,?,?)', ('steers', key, encode(item)))
            self._event(db, identifier, 'steer', {'id': key, 'status': 'queued'})
        return {'ok': True, 'id': key, 'run_id': identifier, 'status': 'queued'}

    def has_steers(self, identifier):
        return any(item.get('run_id') == identifier and item.get('status') == 'queued'
                   for item in self.store.entities('steers'))

    def retained_input(self, identifier, boundary):
        """Keep the latest exact steer and choices after their chat rows compact."""
        if not boundary:
            return []
        identifiers = []
        for kind, status in (('steers', 'applied'), ('questions', 'answered')):
            eligible = [item for item in self.store.entities(kind) if item.get('run_id') == identifier
                        and item.get('status') == status and item.get('message_id', boundary + 1) <= boundary]
            if eligible:
                identifiers.append(max(eligible, key=lambda item: item['message_id'])['message_id'])
        with self.store._connection() as db:
            return [{'role': 'user', 'content': row['content']} for message_id in sorted(identifiers)
                    for row in db.execute('SELECT content FROM messages WHERE id=?', (message_id,))]

    def apply_steers(self, identifier):
        applied = []
        with self.store._connection(transaction='write') as db:
            run = self._run(db, identifier)
            rows = db.execute("SELECT id,data FROM entities WHERE kind='steers' ORDER BY rowid").fetchall()
            for row in rows:
                item = json.loads(row['data'])
                if item.get('run_id') != identifier or item.get('status') != 'queued':
                    continue
                stamp = _now()
                cursor = db.execute('INSERT INTO messages(chat_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)',
                    (run['chat_id'], 'user', item['text'], encode({'steer_id': item['id'], 'interaction_run_id': identifier, 'status': 'complete'}), stamp))
                item.update(status='applied', message_id=cursor.lastrowid, updated_at=stamp)
                db.execute("UPDATE entities SET data=? WHERE kind='steers' AND id=?", (encode(item), item['id']))
                db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, run['chat_id']))
                self._event(db, identifier, 'steer', {'id': item['id'], 'status': 'applied', 'message_id': cursor.lastrowid})
                applied.append(item)
            if applied:
                self._dismiss(db, identifier, 'dismissed')
        return applied

    @staticmethod
    def _items(arguments):
        questions = arguments.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError('Ask between one and three questions.')
        items = []
        identifiers = set()
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                raise ValueError('Each question must be an object.')
            identifier = question.get('id') or f'question_{index + 1}'
            if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', identifier) or identifier in identifiers:
                raise ValueError('Question IDs must be unique short identifiers.')
            identifiers.add(identifier)
            options = question.get('options')
            if not isinstance(options, list) or not 2 <= len(options) <= 4:
                raise ValueError('Each question needs two to four options; free text is always available.')
            normalized = []
            labels = set()
            for option in options:
                if not isinstance(option, dict):
                    raise ValueError('Each option must be an object.')
                label = text(option.get('label'), 'Option label', 120)
                if label in labels:
                    raise ValueError('Option labels must be unique.')
                labels.add(label)
                description = option.get('description', '')
                if not isinstance(description, str) or len(description) > 600:
                    raise ValueError('Option descriptions must be short text.')
                normalized.append({'label': label, 'description': description,
                                   'recommended': option.get('recommended') is True})
            marked = [i for i, option in enumerate(normalized) if option['recommended']]
            recommended = marked[0] if marked else 0
            for i, option in enumerate(normalized):
                option['recommended'] = i == recommended
            header = question.get('header') or question.get('title') or 'Preference'
            items.append({'id': identifier, 'header': text(header, 'Question heading', 80),
                          'question': text(question.get('question'), 'Question', 2000),
                          'options': normalized, 'allow_free_text': True})
        return items

    def ask(self, run, arguments, invocation_id):
        if not invocation_id:
            raise ValueError('A durable tool invocation is required for questions.')
        items = self._items(arguments)
        identifier = sha256(('question:' + invocation_id).encode()).hexdigest()
        with self.store._connection(transaction='write') as db:
            current = self._run(db, run['id'])
            existing = db.execute("SELECT data FROM entities WHERE kind='questions' AND id=?", (identifier,)).fetchone()
            if existing:
                question = json.loads(existing[0])
                if question['items'] != items:
                    raise ValueError('This invocation already asked different questions.')
            else:
                if current['status'] in TERMINAL:
                    raise ValueError('This run is no longer asking questions.')
                question = dict(id=identifier, run_id=run['id'], chat_id=run['chat_id'],
                                status='pending', ready=False, items=items, created_at=_now())
                db.execute('INSERT INTO entities VALUES(?,?,?)', ('questions', identifier, encode(question)))
            result = {'question_id': identifier, 'status': question['status'],
                      'message': 'Await the user answer before continuing. Answers do not authorize tool actions.'}
            if question.get('answers'):
                result['answers'] = question['answers']
            # The question and its harmless tool outcome commit together. A crash
            # cannot turn asking a question into an unknown side effect.
            db.execute("UPDATE invocations SET status='completed',result=?,updated_at=? WHERE id=? AND run_id=?",
                       (encode(result), _now(), invocation_id, run['id']))
        return result

    def publish_pending(self, identifier):
        """Expose choices only after all tool responses are paired/recovered."""
        with self.store._connection(transaction='write') as db:
            for row in db.execute("SELECT id,data FROM entities WHERE kind='questions'").fetchall():
                question = json.loads(row['data'])
                if question.get('run_id') == identifier and question.get('status') == 'pending' and not question.get('ready'):
                    question.update(ready=True, updated_at=_now())
                    db.execute("UPDATE entities SET data=? WHERE kind='questions' AND id=?", (encode(question), row['id']))
                    self._event(db, identifier, 'question', {'question_id': row['id'], 'status': 'pending'})

    def pending(self, chat_id=None, run_id=None, *, include_unready=False):
        with self.store._connection() as db:
            rows = db.execute("SELECT e.data FROM entities e WHERE e.kind='questions' ORDER BY e.rowid").fetchall()
            result = []
            for row in rows:
                item = json.loads(row[0])
                if item.get('status') != 'pending' or chat_id and item.get('chat_id') != chat_id or run_id and item.get('run_id') != run_id:
                    continue
                if not include_unready and not item.get('ready'):
                    continue
                linked = db.execute('SELECT r.status,c.archived FROM runs r JOIN chats c ON c.id=r.chat_id WHERE r.id=?', (item['run_id'],)).fetchone()
                if linked and not linked['archived'] and linked['status'] not in ('completed', 'cancelled'):
                    result.append(item)
            return result

    def answer(self, identifier, answers):
        with self.store._connection(transaction='write') as db:
            row = db.execute("SELECT data FROM entities WHERE kind='questions' AND id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError('Question not found.')
            question = json.loads(row[0])
            run = self._run(db, question['run_id'])
            if not isinstance(answers, dict) or set(answers) != {item['id'] for item in question['items']}:
                raise ValueError('Answer each question once.')
            normalized = {}
            for item in question['items']:
                value = answers[item['id']]
                if not isinstance(value, dict) or set(value) - {'option', 'text'}:
                    raise ValueError('Invalid answer.')
                selected = value.get('option')
                free = value.get('text')
                if selected and free:
                    raise ValueError('Choose an option or enter a free-text answer.')
                if selected is not None:
                    if selected not in [option['label'] for option in item['options']]:
                        raise ValueError('Choose one of the offered options.')
                    normalized[item['id']] = {'option': selected}
                else:
                    normalized[item['id']] = {'text': text(free, 'Answer', 6000)}
            if question['status'] == 'answered':
                if question['answers'] != normalized:
                    raise ValueError('This question was already answered differently.')
                return {'ok': True, 'id': identifier, 'status': 'answered'}
            if run['status'] in ('completed', 'cancelled'):
                raise ValueError('This question is no longer active.')
            if question['status'] != 'pending':
                raise ValueError('This question is no longer pending.')
            if not question.get('ready'):
                raise ValueError('Wait for the current tool round to finish, or Resume to recover it, before answering.')
            content = 'Answers to your questions:\n' + '\n'.join(
                item['question'] + '\n' + (normalized[item['id']].get('option') or normalized[item['id']]['text'])
                for item in question['items'])
            stamp = _now()
            cursor = db.execute('INSERT INTO messages(chat_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)',
                (run['chat_id'], 'user', content, encode({'question_id': identifier, 'interaction_run_id': run['id'], 'status': 'complete'}), stamp))
            question.update(status='answered', answers=normalized, message_id=cursor.lastrowid, updated_at=stamp)
            db.execute("UPDATE entities SET data=? WHERE kind='questions' AND id=?", (encode(question), identifier))
            db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, run['chat_id']))
            self._event(db, run['id'], 'question_answered', {'question_id': identifier, 'message_id': cursor.lastrowid})
        return {'ok': True, 'id': identifier, 'status': 'answered'}

    def _dismiss(self, db, identifier, status):
        for row in db.execute("SELECT id,data FROM entities WHERE kind='questions'").fetchall():
            question = json.loads(row['data'])
            if question.get('run_id') == identifier and question.get('status') == 'pending':
                question.update(status=status, updated_at=_now())
                db.execute("UPDATE entities SET data=? WHERE kind='questions' AND id=?", (encode(question), row['id']))
                self._event(db, identifier, 'question_answered', {'question_id': row['id'], 'status': status})

    def cancel_questions(self, identifier):
        with self.store._connection(transaction='write') as db:
            self._dismiss(db, identifier, 'cancelled')
