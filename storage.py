"""Durable local projects, conversations and task lists.

Every operation opens its own SQLite connection. WAL allows a conversation to
be read while its background agent saves messages, without sharing connections
between webview, HTTP and worker threads. Compaction stores a separate summary;
the original conversation is never removed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


TASK_STATUSES = ('pending', 'in_progress', 'completed', 'cancelled')
MESSAGE_STATUSES = ('complete', 'completed', 'partial', 'cancelled', 'error', 'interrupted', 'tool')
MESSAGE_ROLES = ('user', 'assistant', 'tool', 'system')
MESSAGE_FIELDS = frozenset(('thinking', 'sources', 'tool_calls', 'tool_name', 'images', 'status'))
MAX_MESSAGE_CHARACTERS = 1_000_000
MAX_METADATA_BYTES = 24_000_000
DEFAULT_CONTEXT = 32_768


def _context_setting(value):
    if type(value) is not int or not 2_048 <= value <= 262_144 or value % 2_048:
        raise ValueError('Context must be an integer from 2,048 to 262,144 in steps of 2,048.')
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _text(value, label, limit, *, empty=False, strip=True):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text.')
    value = value.strip() if strip else value
    if (not empty and not value) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{label} must contain {"0" if empty else "1"}-{limit:,} characters.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(f'{label} contains invalid Unicode text.') from None
    return value


def _id(value, label):
    value = _text(value, label, 64, strip=False)
    if value != value.strip():
        raise ValueError(f'Invalid {label.lower()}.')
    return value


def _limit(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{label} must be an integer from 1 to {maximum:,}.')
    return value


def _default_directory():
    configured = os.getenv('SIDEKICK_DATA_DIR')
    if configured:
        return Path(configured).expanduser()
    local = os.getenv('LOCALAPPDATA')
    return Path(local) / 'Sidekick' if local else Path.home() / '.local' / 'share' / 'sidekick'


class Store:
    def __init__(self, data_dir=None):
        try:
            self.data_dir = (Path(data_dir).expanduser() if data_dir is not None
                             else _default_directory()).resolve()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = self.data_dir / 'sidekick.sqlite3'
            with self._connection() as connection:
                # journal_mode must be set before opening a transaction.
                connection.execute('PRAGMA journal_mode=WAL')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version > 1:
                    raise ValueError('This saved data requires a newer version of Sidekick.')
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chats (
                        id TEXT PRIMARY KEY,
                        project_id TEXT REFERENCES projects(id),
                        title TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        compacted_through INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL REFERENCES chats(id),
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS messages_chat_order ON messages(chat_id, id);
                    CREATE INDEX IF NOT EXISTS chats_project_updated ON chats(project_id, updated_at);
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id),
                        title TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS tasks_project_created ON tasks(project_id, created_at);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value INTEGER NOT NULL
                    );
                    PRAGMA user_version=1;
                ''')
        except (OSError, TypeError, RuntimeError) as exc:
            raise ValueError(f'Cannot open Sidekick saved data: {exc}') from None

    @contextmanager
    def _connection(self, *, transaction=None):
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            if transaction:
                connection.execute('BEGIN IMMEDIATE' if transaction == 'write' else 'BEGIN')
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise ValueError(f'Could not access saved Sidekick data: {exc}') from None
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _project(connection, project_id):
        project_id = _id(project_id, 'Project ID')
        row = connection.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
        if row is None:
            raise ValueError('Project not found.')
        return dict(row)

    @staticmethod
    def _chat(connection, chat_id):
        chat_id = _id(chat_id, 'Chat ID')
        row = connection.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone()
        if row is None:
            raise ValueError('Chat not found.')
        return dict(row)

    @staticmethod
    def _message(row):
        item = dict(row)
        try:
            metadata = json.loads(item.pop('metadata'))
            if not isinstance(metadata, dict) or set(metadata) - MESSAGE_FIELDS:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('A saved message contains invalid metadata.') from None
        item.update(metadata)
        return item

    def list_projects(self):
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM projects ORDER BY name COLLATE NOCASE, id')]

    @staticmethod
    def _settings(connection):
        row = connection.execute('SELECT value FROM settings WHERE key = ?', ('context',)).fetchone()
        return {'context': _context_setting(row['value']) if row is not None else DEFAULT_CONTEXT}

    def get_settings(self):
        """Read preferences; reading an unset default performs no database write."""
        with self._connection() as connection:
            return self._settings(connection)

    def update_settings(self, settings):
        """Validate the whole preference update before making any durable change."""
        if not isinstance(settings, dict):
            raise ValueError('Settings must be an object.')
        if set(settings) - {'context'}:
            raise ValueError('Unknown setting. Only context can be changed.')
        if not settings:
            return self.get_settings()
        context = _context_setting(settings['context'])
        with self._connection(transaction='write') as connection:
            connection.execute('''
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            ''', ('context', context))
            return self._settings(connection)

    def create_project(self, name, path):
        name = _text(name, 'Project name', 160)
        if isinstance(path, Path):
            path = str(path)
        path = _text(path, 'Project folder', 4096)
        try:
            folder = Path(path).expanduser().resolve(strict=True)
            if not folder.is_dir():
                raise ValueError('Select an existing project folder.')
        except (OSError, RuntimeError):
            raise ValueError('Select an existing project folder.') from None
        project_id, stamp = uuid4().hex, _now()
        with self._connection(transaction='write') as connection:
            connection.execute(
                'INSERT INTO projects (id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                (project_id, name, str(folder), stamp, stamp))
            return self._project(connection, project_id)

    def get_project(self, project_id):
        with self._connection() as connection:
            return self._project(connection, project_id)

    def list_chats(self, project_id=None):
        """List conversation headers; None includes standalone and project chats."""
        with self._connection(transaction='read') as connection:
            if project_id is not None:
                self._project(connection, project_id)
                rows = connection.execute(
                    'SELECT * FROM chats WHERE project_id = ? ORDER BY updated_at DESC, id', (project_id,))
            else:
                rows = connection.execute('SELECT * FROM chats ORDER BY updated_at DESC, id')
            return [dict(row) for row in rows]

    def create_chat(self, project_id=None, title='New chat', model=''):
        title = _text(title, 'Chat title', 200)
        model = _text(model, 'Model', 300, empty=True)
        chat_id, stamp = uuid4().hex, _now()
        with self._connection(transaction='write') as connection:
            if project_id is not None:
                self._project(connection, project_id)
            connection.execute(
                'INSERT INTO chats (id, project_id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (chat_id, project_id, title, model, stamp, stamp))
            return self._chat(connection, chat_id)

    def get_chat(self, chat_id, *, limit=None):
        """Read a conversation, optionally loading only its latest N messages.

        ``total_messages`` counts the full saved history. A limited response is
        still chronological, and older message attachment JSON is never loaded.
        """
        if limit is not None:
            limit = _limit(limit, 'Message limit', 10_000)
        with self._connection(transaction='read') as connection:
            chat = self._chat(connection, chat_id)
            chat['total_messages'] = connection.execute(
                'SELECT COUNT(*) FROM messages WHERE chat_id = ?', (chat_id,)).fetchone()[0]
            if limit is None:
                rows = connection.execute('SELECT * FROM messages WHERE chat_id = ? ORDER BY id', (chat_id,))
            else:
                rows = connection.execute('''
                    SELECT * FROM (
                        SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?
                    ) ORDER BY id
                ''', (chat_id, limit))
            chat['messages'] = [self._message(row) for row in rows]
            return chat

    def get_active_chat(self, chat_id):
        """Read only messages newer than the durable compaction boundary."""
        with self._connection(transaction='read') as connection:
            chat = self._chat(connection, chat_id)
            chat['total_messages'] = connection.execute(
                'SELECT COUNT(*) FROM messages WHERE chat_id = ?', (chat_id,)).fetchone()[0]
            chat['messages'] = [self._message(row) for row in connection.execute(
                'SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id',
                (chat_id, chat['compacted_through']))]
            return chat

    def search_memory(self, project_id, query, limit=8, chat_limit=100):
        """Search bounded saved-chat text snippets without loading attachments.

        None scopes the search to standalone chats. Chats are searched in order
        of recent activity, and matching user/assistant messages appear in
        chronological order within each chat. Search also includes compacted
        history because original messages remain stored.
        """
        query = _text(query, 'Memory search query', 1000).lower()
        limit = _limit(limit, 'Memory result limit', 100)
        chat_limit = _limit(chat_limit, 'Memory chat limit', 1000)
        with self._connection(transaction='read') as connection:
            if project_id is not None:
                self._project(connection, project_id)
            # SQLite's built-in lower() handles ASCII only. This small function
            # preserves the previous Unicode-aware search while SQLite returns
            # only snippets, never the full content or metadata to the caller.
            connection.create_function('sidekick_find', 2,
                                       lambda content, needle: content.lower().find(needle) + 1,
                                       deterministic=True)
            chats = connection.execute('''
                SELECT id, title FROM chats WHERE project_id IS ?
                ORDER BY updated_at DESC, id LIMIT ?
            ''', (project_id, chat_limit)).fetchall()
            matches = []
            for chat in chats:
                rows = connection.execute('''
                    SELECT chat_id, ? AS title,
                           substr(content, max(1, sidekick_find(content, ?) - 120), 920) AS snippet
                    FROM messages
                    WHERE chat_id = ? AND role IN ('user', 'assistant')
                          AND sidekick_find(content, ?) > 0
                    ORDER BY id LIMIT ?
                ''', (chat['title'], query, chat['id'], query, limit - len(matches)))
                matches.extend(dict(row) for row in rows)
                if len(matches) >= limit:
                    break
            return matches

    @staticmethod
    def _metadata(kwargs):
        unknown = set(kwargs) - MESSAGE_FIELDS
        if unknown:
            raise ValueError(f'Unsupported message fields: {", ".join(sorted(unknown))}.')
        for key in ('thinking', 'tool_name'):
            if key in kwargs:
                _text(kwargs[key], key, MAX_MESSAGE_CHARACTERS if key == 'thinking' else 200,
                      empty=True, strip=False)
        for key in ('sources', 'tool_calls', 'images'):
            if key in kwargs and not isinstance(kwargs[key], list):
                raise ValueError(f'{key} must be a list.')
        if 'status' in kwargs and kwargs['status'] not in MESSAGE_STATUSES:
            raise ValueError('Invalid message status.')
        if len(kwargs.get('images', [])) > 4 or any(
                not isinstance(item, str) or len(item) > 8_000_000 for item in kwargs.get('images', [])):
            raise ValueError('Invalid saved image attachments.')
        if len(kwargs.get('sources', [])) > 100 or len(kwargs.get('tool_calls', [])) > 100:
            raise ValueError('Too many message sources or tool calls.')
        try:
            encoded = json.dumps(kwargs, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise ValueError('Message metadata must contain valid JSON values.') from None
        try:
            if len(encoded.encode('utf-8')) > MAX_METADATA_BYTES:
                raise ValueError('Saved message attachments are too large.')
        except UnicodeError:
            raise ValueError('Message metadata contains invalid Unicode text.') from None
        return encoded

    def add_message(self, chat_id, role, content, **kwargs):
        if role not in MESSAGE_ROLES:
            raise ValueError('Invalid message role.')
        content = _text(content, 'Message', MAX_MESSAGE_CHARACTERS, empty=True, strip=False)
        metadata = self._metadata(kwargs)
        with self._connection(transaction='write') as connection:
            self._chat(connection, chat_id)
            stamp = _now()
            cursor = connection.execute(
                'INSERT INTO messages (chat_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)',
                (chat_id, role, content, metadata, stamp))
            connection.execute('UPDATE chats SET updated_at = ? WHERE id = ?', (stamp, chat_id))
            row = connection.execute('SELECT * FROM messages WHERE id = ?', (cursor.lastrowid,)).fetchone()
            return self._message(row)

    def rename_chat(self, chat_id, title):
        title = _text(title, 'Chat title', 200)
        with self._connection(transaction='write') as connection:
            self._chat(connection, chat_id)
            connection.execute('UPDATE chats SET title = ?, updated_at = ? WHERE id = ?',
                               (title, _now(), chat_id))

    def update_chat_model(self, chat_id, model):
        model = _text(model, 'Model', 300, empty=True)
        with self._connection(transaction='write') as connection:
            self._chat(connection, chat_id)
            connection.execute('UPDATE chats SET model = ?, updated_at = ? WHERE id = ?',
                               (model, _now(), chat_id))

    def set_summary(self, chat_id, summary, compacted_through):
        summary = _text(summary, 'Conversation summary', 100_000, empty=True, strip=False)
        if type(compacted_through) is not int or not 0 <= compacted_through <= 2**63 - 1:
            raise ValueError('Invalid compaction message ID.')
        with self._connection(transaction='write') as connection:
            chat = self._chat(connection, chat_id)
            if compacted_through < chat['compacted_through']:
                raise ValueError('Compaction cannot move backwards.')
            if compacted_through and connection.execute(
                    'SELECT 1 FROM messages WHERE chat_id = ? AND id = ?',
                    (chat_id, compacted_through)).fetchone() is None:
                raise ValueError('Compaction message does not belong to this chat.')
            connection.execute(
                'UPDATE chats SET summary = ?, compacted_through = ?, updated_at = ? WHERE id = ?',
                (summary, compacted_through, _now(), chat_id))

    def list_tasks(self, project_id):
        with self._connection(transaction='read') as connection:
            self._project(connection, project_id)
            return [dict(row) for row in connection.execute(
                'SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at, id', (project_id,))]

    def create_task(self, project_id, title):
        title = _text(title, 'Task title', 500)
        task_id, stamp = uuid4().hex, _now()
        with self._connection(transaction='write') as connection:
            self._project(connection, project_id)
            connection.execute(
                'INSERT INTO tasks (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                (task_id, project_id, title, stamp, stamp))
            return dict(connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone())

    def update_task(self, project_id, task_id, status):
        task_id = _id(task_id, 'Task ID')
        if status not in TASK_STATUSES:
            raise ValueError(f'Task status must be one of: {", ".join(TASK_STATUSES)}.')
        with self._connection(transaction='write') as connection:
            self._project(connection, project_id)
            cursor = connection.execute(
                'UPDATE tasks SET status = ?, updated_at = ? WHERE project_id = ? AND id = ?',
                (status, _now(), project_id, task_id))
            if cursor.rowcount != 1:
                raise ValueError('Task not found in this project.')
            return dict(connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone())
