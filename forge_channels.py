"""Opt-in Telegram gateway, authenticated webhooks and durable notification outbox.

The store owns migrations. Importing or constructing this module never creates
tables, connects an account, or sends a message. All external I/O runs on the
channel worker; run-event subscribers only wake that worker.
"""
from __future__ import annotations

from hashlib import sha256
from functools import wraps
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from forge_credentials import CredentialVault
from forge_store import TERMINAL, encode
from storage import _now


# Applied exactly once by ForgeStore's ordered schema-5 migration.
CHANNEL_SCHEMA = (
    'CREATE TABLE channel_inbound(channel_id TEXT NOT NULL,external_id TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,run_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(channel_id,external_id))',
    'CREATE INDEX channel_inbound_pending ON channel_inbound(status,created_at)',
    'CREATE TABLE channel_routes(channel_id TEXT NOT NULL,recipient TEXT NOT NULL,chat_id TEXT NOT NULL,project_id TEXT,PRIMARY KEY(channel_id,recipient))',
    'CREATE TABLE channel_checkpoints(channel_id TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(channel_id,key))',
    'CREATE TABLE channel_outbox(id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,recipient TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,remote_id TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
    'CREATE INDEX channel_outbox_pending ON channel_outbox(status,next_attempt)',
    'CREATE TABLE channel_callbacks(token TEXT PRIMARY KEY,channel_id TEXT NOT NULL,recipient TEXT NOT NULL,sender_id TEXT NOT NULL,run_id TEXT NOT NULL,approval_id TEXT NOT NULL,action_hash TEXT NOT NULL,allowed INTEGER NOT NULL,expires_at REAL NOT NULL,used_at TEXT)',
    'CREATE TABLE channel_notifications(id TEXT PRIMARY KEY,run_id TEXT,kind TEXT NOT NULL,data TEXT NOT NULL,unread INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL)',
    'CREATE TABLE channel_replays(channel_id TEXT NOT NULL,nonce TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(channel_id,nonce))',
    'CREATE TABLE channel_event_journal(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,seq INTEGER NOT NULL,UNIQUE(run_id,seq))',
    'INSERT INTO channel_event_journal(run_id,seq) SELECT run_id,seq FROM run_events ORDER BY rowid',
)

MAX_BODY = 65536
MAX_TEXT = 16000
TELEGRAM_COMMANDS = [{'command': name, 'description': description} for name, description in (
    ('builder', 'Explore ideas and shape a project together'), ('build', 'Accept your Builder brief and build in the connected project'),
    ('plan', 'Research and prepare a plan'), ('goal', 'Start a tracked task in this chat'),
    ('answer', 'Answer a pending question; separate answers with |'), ('status', 'Show current progress'),
    ('pause', 'Pause work'), ('resume', 'Resume safe paused work'), ('cancel', 'Cancel work'), ('help', 'Show commands'))]


def telegram_chunks(text, limit=3500):
    """Plain-text messages, bounded in UTF-16 units (including emoji)."""
    chunks=[]; start=0; units=0
    for index, character in enumerate(text):
        size=2 if ord(character)>0xffff else 1
        if units+size>limit:
            chunks.append(text[start:index]); start=index; units=0
        units+=size
    if start<len(text): chunks.append(text[start:])
    return chunks or ['Forge finished without a text reply. Use /status for details.']


PERMISSIONS = {'deny_access': 0, 'always_ask': 1, 'full_access': 2}


def _channel_admitted(method):
    @wraps(method)
    def admitted(self, *args, **kwargs):
        with self.lock:
            self._require_admission()
            return method(self, *args, **kwargs)
    return admitted


def quarantine_after_rollback(restored_db, latest_db):
    """Retain external identities while requiring fresh local channel setup.

    Connections belong to the update helper after the coordinator has exited.
    A previous app without channel tables keeps its original schema unchanged;
    the helper retains its separate latest database for inspection.
    """
    tables = {
        'channel_inbound': ('channel_id', 'external_id', 'status', 'data', 'run_id', 'error', 'created_at', 'updated_at'),
        'channel_checkpoints': ('channel_id', 'key', 'value'),
        'channel_replays': ('channel_id', 'nonce', 'created_at'),
        'channel_outbox': ('id', 'channel_id', 'recipient', 'kind', 'payload', 'status', 'attempts', 'next_attempt', 'remote_id', 'last_error', 'created_at', 'updated_at'),
    }
    def names(db):
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    restored_names = names(restored_db)
    if not set(tables) <= restored_names or 'entities' not in restored_names:
        return {'channels': 0, 'merged': 0, 'previous_schema_without_channels': True}
    latest_names = names(latest_db)
    merged = 0; channels = 0
    reason = 'Rollback paused connected workflows. Enter a fresh credential and pair again before resuming.'
    with restored_db:
        run_ids = {row[0] for row in restored_db.execute('SELECT id FROM runs')}
        for table, allowed in tables.items():
            if table not in latest_names:
                continue
            # Identifiers come exclusively from the allowlist above. Unknown
            # newer schema columns cannot become SQL or bypass older defaults.
            old_columns = {row[1] for row in restored_db.execute('PRAGMA table_info(' + table + ')')}
            new_columns = {row[1] for row in latest_db.execute('PRAGMA table_info(' + table + ')')}
            columns = [column for column in allowed if column in old_columns and column in new_columns]
            selected = ','.join(columns)
            for row in latest_db.execute('SELECT ' + selected + ' FROM ' + table):
                record = dict(zip(columns, row))
                if table == 'channel_checkpoints' and record.get('channel_id') == '_global':
                    continue
                if table == 'channel_checkpoints' and record.get('key') == 'telegram_offset':
                    previous = restored_db.execute('SELECT value FROM channel_checkpoints WHERE channel_id=? AND key=?', (record['channel_id'], record['key'])).fetchone()
                    try:
                        offset = max(json.loads(previous[0]) if previous else 0, json.loads(record['value']))
                        if type(offset) is not int or offset < 0:
                            continue
                        record['value'] = encode(offset)
                    except (ValueError, TypeError):
                        continue
                if table == 'channel_inbound' and record.get('run_id') not in run_ids:
                    record['run_id'] = None
                restored_db.execute('INSERT OR REPLACE INTO ' + table + '(' + selected + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(record[column] for column in columns))
                merged += 1
        restored_db.execute("UPDATE channel_inbound SET status='outcome_unknown',error=?,updated_at=? WHERE status IN ('pending','dispatching')", (reason, _now()))
        restored_db.execute("UPDATE channel_outbox SET status='outcome_unknown',last_error=?,updated_at=? WHERE status='sending'", (reason, _now()))
        restored_db.execute("UPDATE channel_outbox SET status='cancelled',last_error=?,updated_at=? WHERE status IN ('pending','retry','failed')", (reason, _now()))
        restored_db.execute("UPDATE channel_outbox SET payload='{}'")
        restored_db.execute('DELETE FROM channel_callbacks')
        restored_db.execute('DELETE FROM channel_routes')
        restored_db.execute("DELETE FROM entities WHERE kind='channel_pairings'")
        for row in restored_db.execute("SELECT id,data FROM entities WHERE kind='channels'").fetchall():
            config = json.loads(row[1])
            config.update(enabled=False, connected=False, allowlist=[], owner_id=None, requires_review=True, last_error=reason)
            for field in ('credential_ref', 'pair_hash', 'pair_expires'):
                config.pop(field, None)
            restored_db.execute("UPDATE entities SET data=? WHERE kind='channels' AND id=?", (encode(config), row[0]))
            channels += 1
    return {'channels': channels, 'merged': merged, 'previous_schema_without_channels': False}


def permission_ceiling(requested, current):
    if requested not in PERMISSIONS or current not in PERMISSIONS:
        raise ValueError('Unknown channel permission profile.')
    return min((requested, current), key=PERMISSIONS.__getitem__)


def redact(value, known_secrets=()):
    """Remove credential-looking material before it reaches logs or delivery."""
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'(?:token|password|secret|authorization|api[_-]?key)', k, re.I)
                    else redact(v, known_secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, known_secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in known_secrets:
        if secret:
            value = value.replace(str(secret), '[redacted]')
    value = re.sub(r'(?i)\bBearer\s+[^\s"\']+', 'Bearer [redacted]', value)
    value = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{25,}\b', '[redacted]', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[redacted]', value)
    return re.sub(r'(?i)\b(password|secret|api[_-]?key|token)\s*[:=]\s*["\']?[^\s,"\']+', r'\1=[redacted]', value)


def webhook_signature(secret, timestamp, nonce, body):
    canonical = str(timestamp).encode() + b'.' + str(nonce).encode() + b'.' + body
    return 'sha256=' + hmac.new(str(secret).encode(), canonical, sha256).hexdigest()


class DeliveryFailure(RuntimeError):
    def __init__(self, message='Channel delivery failed.', *, unknown=False, retry_after=0):
        super().__init__(message)
        self.unknown = unknown
        self.retry_after = retry_after


class ChannelTransport:
    """Official Bot API shape. Endpoint overrides are constructor-only fixtures."""
    def __init__(self, client=None, telegram_url='https://api.telegram.org'):
        self.client = client or httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
        self.telegram_url = telegram_url.rstrip('/')
        self.owns_client = client is None

    def telegram(self, token, method, data, cancel=None):
        if cancel and cancel.is_set():
            raise DeliveryFailure('Channel stopped.')
        try:
            response = self.client.post(self.telegram_url + '/bot' + token + '/' + method,
                                        json=data, timeout=8)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise DeliveryFailure('Telegram is offline.') from None
        except httpx.HTTPError:
            raise DeliveryFailure('Telegram delivery outcome is unknown.', unknown=method not in ('getMe', 'getUpdates')) from None
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise DeliveryFailure('Telegram returned an invalid response.', unknown=method not in ('getMe', 'getUpdates')) from None
        if not isinstance(result, dict) or result.get('ok') is not True:
            retry_after = (result.get('parameters') or {}).get('retry_after', 0) if isinstance(result, dict) else 0
            raise DeliveryFailure('Telegram rejected the request.', retry_after=min(3600, max(0, float(retry_after))))
        return result.get('result')

    def webhook(self, url, body, headers, cancel=None):
        if cancel and cancel.is_set():
            raise DeliveryFailure('Channel stopped.')
        try:
            response = self.client.post(url, content=body, headers=headers, timeout=8)
        except httpx.HTTPError:
            raise DeliveryFailure('Webhook delivery failed.', unknown=True) from None
        if not 200 <= response.status_code < 300:
            raise DeliveryFailure('Webhook rejected delivery.')
        return {'status': response.status_code}

    def close(self):
        if self.owns_client:
            self.client.close()


class ChannelManager:
    def __init__(self, service, vault=None, transport=None, clock=None):
        self.service = service
        self.store = service.store
        self.vault = vault or getattr(service, 'vault', None) or CredentialVault()
        self.transport = transport or ChannelTransport()
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.recovered = False

    def _require_admission(self):
        if getattr(getattr(self.service, 'update_manager', None), 'applying', False):
            raise ValueError('Forge is preparing an update. Connected workflows are paused until restart.')

    def _config(self, identifier):
        return self.store.entity('channels', identifier)

    @staticmethod
    def _public(config):
        return {k: v for k, v in config.items() if k not in ('pair_hash', 'pair_expires')}

    def configurations(self):
        return [self._public(c) for c in self.store.entities('channels')]

    def _secret(self, config, field='credential_ref'):
        value = self.vault.get(config.get(field))
        if not value:
            raise ValueError('Channel credential is missing. Configure it in Forge.')
        return value

    @staticmethod
    def _recipient(value):
        if isinstance(value, bool) or not re.fullmatch(r'-?\d{1,20}', str(value)):
            raise ValueError('Telegram chat and sender IDs must be numeric.')
        return str(value)

    @_channel_admitted
    def save(self, data):
        if not isinstance(data, dict):
            raise ValueError('Expected channel configuration.')
        identifier = data.get('id') or uuid4().hex
        existing = next((c for c in self.store.entities('channels') if c['id'] == identifier), {})
        config = dict(existing)
        kind = data.get('kind', existing.get('kind', 'telegram'))
        if kind not in ('telegram', 'webhook') or existing and kind != existing['kind']:
            raise ValueError('Unknown channel type or changed channel identity.')
        allowed = ('name', 'enabled', 'project_id', 'model', 'provider_id', 'permission_profile', 'agent_profile_id',
                   'include_content', 'allow_inbound', 'allow_outbound', 'inbound_tasks', 'url', 'owner_id')
        config.update({k: data[k] for k in allowed if k in data})
        config.update(id=identifier, kind=kind)
        config.setdefault('name', 'Telegram' if kind == 'telegram' else 'Webhook')
        if not isinstance(config['name'], str) or not 1 <= len(config['name']) <= 100:
            raise ValueError('Channel name must contain 1–100 characters.')
        for key, default in (('enabled', False), ('include_content', False), ('allow_inbound', kind == 'telegram'), ('allow_outbound', True), ('inbound_tasks', False)):
            config.setdefault(key, default)
            if type(config[key]) is not bool:
                raise ValueError('Channel switches must be true or false.')
        config.setdefault('permission_profile', 'always_ask')
        permission_ceiling(config['permission_profile'], self.store.get_settings()['permission_profile'])
        if config.get('project_id'):
            self.store.get_project(config['project_id'])
        if config.get('agent_profile_id'):
            profile = self.store.entity('agents', config['agent_profile_id'])
            if not profile.get('enabled', True):
                raise ValueError('Channel agent profile is disabled.')
        for key in ('model', 'provider_id'):
            if key in config and (not isinstance(config[key], str) or len(config[key]) > 300):
                raise ValueError('Invalid channel model or provider.')
        secret = data.get('token') if kind == 'telegram' else data.get('secret')
        if secret is not None:
            if not isinstance(secret, str) or not 16 <= len(secret) <= 1024:
                raise ValueError('Channel credential must contain 16–1,024 characters.')
            config['credential_ref'] = self.vault.put(secret)
            config['connected'] = False
            config.pop('requires_review', None)
            config.pop('last_error', None)
        if 'credential_ref' in data and secret is None:
            reference = data['credential_ref']
            if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', reference):
                raise ValueError('Invalid channel credential reference.')
            config['credential_ref'] = reference
        if config['enabled'] and not config.get('credential_ref'):
            raise ValueError('Configure a credential before enabling this channel.')
        if kind == 'telegram':
            config.setdefault('allowlist', [])
            if 'allowlist' in data:
                if not isinstance(data['allowlist'], list) or len(data['allowlist']) > 100:
                    raise ValueError('Invalid Telegram allowlist.')
                config['allowlist'] = [{'sender_id': self._recipient(a.get('sender_id')), 'chat_id': self._recipient(a.get('chat_id'))}
                                       for a in data['allowlist'] if isinstance(a, dict)]
                if len(config['allowlist']) != len(data['allowlist']):
                    raise ValueError('Invalid Telegram allowlist.')
            if config.get('owner_id') is not None:
                config['owner_id'] = self._recipient(config['owner_id'])
        else:
            self._validate_url(config.get('url'), required=config['allow_outbound'])
        saved = self.store.save_entity('channels', config)
        self.wake.set()
        return self._public(saved)

    @staticmethod
    def _validate_url(url, required=True):
        if not url and not required:
            return
        parsed = urlparse(url or '')
        try:
            loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or not (parsed.scheme == 'https' or parsed.scheme == 'http' and loopback):
            raise ValueError('Webhook URL must use HTTPS, or loopback HTTP for local fixtures.')

    @_channel_admitted
    def test(self, identifier):
        config = self._config(identifier)
        if config['kind'] == 'telegram':
            try:
                result = self.transport.telegram(self._secret(config), 'getMe', {}, self.stop_event)
            except DeliveryFailure as exc:
                raise ValueError(redact(str(exc))[:300]) from None
            if not isinstance(result, dict) or not result.get('is_bot') or type(result.get('id')) is not int:
                raise ValueError('Telegram did not identify a bot.')
            config.update(connected=True, bot={'id': result['id'], 'username': result.get('username', '')}, last_error=None)
        else:
            self._secret(config)
            self._validate_url(config.get('url'), required=config.get('allow_outbound', True))
            config.update(connected=True, last_error=None)
        if config['kind']=='telegram':
            try:
                self.transport.telegram(self._secret(config),'setMyCommands',{'commands':TELEGRAM_COMMANDS},self.stop_event)
                config.update(commands_ready=True,commands_error=None)
            except Exception:
                config.update(commands_ready=False,commands_error='Bot connected, but its command menu could not be updated. Connect / test again.')
        self.store.save_entity('channels', config)
        self.wake.set()
        return self._public(config)

    @_channel_admitted
    def pair_begin(self, identifier):
        config = self._config(identifier)
        if config['kind'] != 'telegram':
            raise ValueError('Pairing is supported for Telegram.')
        code = secrets.token_urlsafe(12)
        config.update(pair_hash=sha256(code.encode()).hexdigest(), pair_expires=self.clock() + 600)
        self.store.save_entity('channels', config)
        return {'code': code, 'expires_in': 600, 'instruction': '/pair ' + code}

    @_channel_admitted
    def pair_approve(self, identifier, pairing_id):
        config = self._config(identifier)
        pairing = self.store.entity('channel_pairings', pairing_id)
        if pairing['channel_id'] != identifier or pairing['status'] != 'pending' or pairing['expires_at'] < self.clock():
            raise ValueError('Pairing request is no longer pending.')
        entry = {k: pairing[k] for k in ('sender_id', 'chat_id')}
        if entry not in config['allowlist']:
            config['allowlist'].append(entry)
        # The first explicitly approved pairing becomes the owner. Changing it
        # later requires a local configuration action.
        if not config.get('owner_id'):
            config['owner_id'] = pairing['sender_id']
        config.pop('pair_hash', None)
        config.pop('pair_expires', None)
        self.store.save_entity('channels', config)
        self.store.save_entity('channel_pairings', {**pairing, 'status': 'approved'})
        return self._public(config)

    @_channel_admitted
    def pair_revoke(self, identifier, sender_id, chat_id):
        config = self._config(identifier)
        entry = {'sender_id': self._recipient(sender_id), 'chat_id': self._recipient(chat_id)}
        config['allowlist'] = [a for a in config.get('allowlist', []) if a != entry]
        self.store.save_entity('channels', config)
        return self._public(config)

    def dispatch(self, action, data=None):
        data = data or {}
        identifier = data.get('id') or data.get('channel_id')
        if action == 'channel_list':
            return {'channels': self.configurations(), 'pairings': self.store.entities('channel_pairings')}
        if action == 'channel_save':
            return {'channel': self.save(data)}
        if action in ('channel_test', 'channel_connect'):
            return {'channel': self.test(identifier)}
        if action == 'channel_pair_begin':
            return self.pair_begin(identifier)
        if action == 'channel_pair_approve':
            return {'channel': self.pair_approve(identifier, data['pairing_id'])}
        if action == 'channel_pair_revoke':
            return {'channel': self.pair_revoke(identifier, data['sender_id'], data['chat_id'])}
        if action == 'channel_delete':
            with self.lock:
                self._require_admission()
                self._config(identifier)
                self.store.delete_entity('channels', identifier)
                with self.store._connection(transaction='write') as db:
                    db.execute("UPDATE channel_outbox SET status='cancelled',updated_at=? WHERE channel_id=? AND status IN ('pending','retry')", (_now(), identifier))
                return {'ok': True}
        if action == 'channel_notifications':
            return {'notifications': self.notifications(), 'outbox': self.outbox()}
        if action == 'channel_notification_read':
            self.mark_read(data.get('notification_id') or identifier, data.get('read', True))
            return {'ok': True}
        if action == 'channel_outbox':
            return {'outbox': self.outbox()}
        if action == 'channel_outbox_retry':
            return self.retry(data.get('delivery_id') or identifier, data.get('confirm_duplicate_risk', False))
        raise ValueError('Unknown channel action.')

    def _checkpoint(self, identifier, key, default=None):
        with self.store._connection() as db:
            row = db.execute('SELECT value FROM channel_checkpoints WHERE channel_id=? AND key=?', (identifier, key)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set_checkpoint(db, identifier, key, value):
        db.execute('INSERT INTO channel_checkpoints VALUES(?,?,?) ON CONFLICT(channel_id,key) DO UPDATE SET value=excluded.value', (identifier, key, encode(value)))

    def _authorized(self, config, sender_id, chat_id, owner=False):
        entry = {'sender_id': str(sender_id), 'chat_id': str(chat_id)}
        return entry in config.get('allowlist', []) and (not owner or str(sender_id) == config.get('owner_id'))

    def _record_inbound(self, identifier, external_id, payload, db=None):
        stamp = _now()
        values = (identifier, str(external_id), 'pending', encode(payload), None, None, stamp, stamp)
        query = 'INSERT OR IGNORE INTO channel_inbound VALUES(?,?,?,?,?,?,?,?)'
        if db is not None:
            return bool(db.execute(query, values).rowcount)
        with self.store._connection(transaction='write') as connection:
            return bool(connection.execute(query, values).rowcount)

    @_channel_admitted
    def receive_telegram(self, identifier, update):
        config = self._config(identifier)
        if config['kind'] != 'telegram' or not config.get('enabled') or not config.get('allow_inbound'):
            raise ValueError('Telegram channel is disabled.')
        if not isinstance(update, dict) or type(update.get('update_id')) is not int or update['update_id'] < 0 or len(encode(update).encode()) > MAX_BODY:
            raise ValueError('Invalid Telegram update.')
        with self.store._connection(transaction='write') as db:
            fresh = self._record_inbound(identifier, update['update_id'], update, db)
            row = db.execute('SELECT value FROM channel_checkpoints WHERE channel_id=? AND key=?', (identifier, 'telegram_offset')).fetchone()
            offset = json.loads(row[0]) if row else 0
            self._set_checkpoint(db, identifier, 'telegram_offset', max(offset, update['update_id'] + 1))
        self.wake.set()
        return {'accepted': fresh, 'duplicate': not fresh}

    @_channel_admitted
    def ingest(self, identifier, body, headers):
        """Authenticate the original bytes before parsing. No bearer exemption
        is valid except this exact versioned inbound route in Forge's router.
        """
        config = self._config(identifier)
        if config['kind'] != 'webhook' or not config.get('enabled') or not config.get('allow_inbound'):
            raise ValueError('Inbound webhook is disabled.')
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise ValueError('Webhook body exceeds 64 KiB.')
        headers = {str(k).lower(): str(v) for k, v in headers.items()}
        timestamp = headers.get('x-forge-timestamp', '')
        nonce = headers.get('x-forge-nonce', '')
        if not re.fullmatch(r'\d{1,12}', timestamp) or abs(self.clock() - int(timestamp)) > 300 or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', nonce):
            raise ValueError('Webhook timestamp or nonce is invalid.')
        expected = webhook_signature(self._secret(config), timestamp, nonce, body)
        supplied = headers.get('x-forge-signature', '')
        if not re.fullmatch(r'sha256=[a-f0-9]{64}', supplied) or not hmac.compare_digest(supplied, expected):
            raise ValueError('Webhook signature is invalid.')
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError('Webhook must contain a JSON object.') from None
        if not isinstance(data, dict) or set(data) - {'id', 'text', 'project_id'}:
            raise ValueError('Webhook accepts id, text and its configured project_id only.')
        external_id = data.get('id', nonce)
        if not isinstance(external_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', external_id):
            raise ValueError('Invalid webhook event ID.')
        text = data.get('text')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            raise ValueError('Webhook text must contain 1–16,000 characters.')
        if data.get('project_id', config.get('project_id')) != config.get('project_id'):
            raise ValueError('Webhook cannot access a different project.')
        if config.get('project_id'):
            self.store.get_project(config['project_id'])
        with self.store._connection(transaction='write') as db:
            if db.execute('SELECT 1 FROM channel_replays WHERE channel_id=? AND nonce=?', (identifier, nonce)).fetchone():
                raise ValueError('Webhook nonce has already been used.')
            db.execute('INSERT INTO channel_replays VALUES(?,?,?)', (identifier, nonce, self.clock()))
            db.execute('DELETE FROM channel_replays WHERE created_at<?', (self.clock() - 86400,))
            fresh = self._record_inbound(identifier, external_id, {'text': text, 'recipient': 'webhook'}, db)
        self.wake.set()
        return {'accepted': fresh, 'duplicate': not fresh, 'event_id': external_id}

    def _route(self, config, recipient):
        project_id = config.get('project_id')
        if project_id:
            self.store.get_project(project_id)
        with self.store._connection(transaction='write') as db:
            row = db.execute('SELECT * FROM channel_routes WHERE channel_id=? AND recipient=?', (config['id'], recipient)).fetchone()
            if row:
                chat = self.store._chat(db, row['chat_id'])
                if row['project_id'] != project_id or chat['project_id'] != project_id or chat.get('archived'):
                    raise ValueError('Channel chat project changed or chat was archived. Reconnect it locally.')
                return row['chat_id']
            chat_id = uuid4().hex
            stamp = _now()
            db.execute('INSERT INTO chats(id,project_id,title,model,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (chat_id, project_id, config['name'] + ' · ' + recipient, config.get('model', ''), stamp, stamp))
            db.execute('INSERT INTO channel_routes VALUES(?,?,?,?)', (config['id'], recipient, chat_id, project_id))
            return chat_id

    def _inbound_status(self, identifier, external_id, status, run_id=None, error=None):
        with self.store._connection(transaction='write') as db:
            db.execute('UPDATE channel_inbound SET status=?,run_id=COALESCE(?,run_id),error=?,updated_at=? WHERE channel_id=? AND external_id=?',
                       (status, run_id, error, _now(), identifier, external_id))

    def _pair_request(self, config, sender, chat, text, external_id):
        code = text.removeprefix('/pair ').strip()
        digest = sha256(code.encode()).hexdigest()
        if not config.get('pair_hash') or config.get('pair_expires', 0) < self.clock() or not hmac.compare_digest(digest, config['pair_hash']):
            return False
        identifier = sha256((config['id'] + ':' + external_id).encode()).hexdigest()[:32]
        pairing = self.store.save_entity('channel_pairings', {'id': identifier, 'channel_id': config['id'],
                         'sender_id': sender, 'chat_id': chat, 'status': 'pending', 'expires_at': config['pair_expires']})
        self._notify('pair:' + identifier, None, 'pairing', {'channel_id': config['id'], 'pairing_id': pairing['id'], 'sender_id': sender, 'chat_id': chat})
        return True

    def _callback(self, config, callback):
        sender = self._recipient((callback.get('from') or {}).get('id'))
        chat = self._recipient(((callback.get('message') or {}).get('chat') or {}).get('id'))
        raw_token = callback.get('data')
        if not isinstance(raw_token, str) or not raw_token.startswith('forge:') or not self._authorized(config, sender, chat, owner=True):
            raise ValueError('Telegram approval sender is not authorized.')
        token = raw_token.removeprefix('forge:')
        with self.store._connection(transaction='write') as db:
            binding = db.execute('SELECT * FROM channel_callbacks WHERE token=?', (token,)).fetchone()
            if not binding or binding['channel_id'] != config['id'] or binding['recipient'] != chat or binding['sender_id'] != sender or binding['used_at'] or binding['expires_at'] < self.clock():
                raise ValueError('Telegram approval is expired, used or belongs to another recipient.')
            approval = db.execute('SELECT * FROM approvals WHERE id=? AND run_id=?', (binding['approval_id'], binding['run_id'])).fetchone()
            if not approval or approval['status'] != 'pending' or approval['action_hash'] != binding['action_hash'] or sha256(encode(json.loads(approval['data'])).encode()).hexdigest() != binding['action_hash']:
                raise ValueError('Telegram approval action no longer matches.')
            run = self.store.run(binding['run_id'])
            route = db.execute('SELECT chat_id FROM channel_routes WHERE channel_id=? AND recipient=?', (config['id'], chat)).fetchone()
            if not route or route['chat_id'] != run['chat_id']:
                raise ValueError('Telegram approval belongs to another chat.')
            if binding['allowed'] and permission_ceiling(config['permission_profile'], self.store.get_settings()['permission_profile']) == 'deny_access':
                raise ValueError('Current permissions deny this action.')
            db.execute('UPDATE channel_callbacks SET used_at=? WHERE token=?', (_now(), token))
        # A consumed callback cannot authorize a second tool action after a crash.
        self.service.jobs.approve(binding['run_id'], binding['approval_id'], bool(binding['allowed']))
        return binding['run_id']

    def _command(self, config, sender, recipient, text, chat_id):
        command = text.split()[0].split('@', 1)[0].lower()
        if command not in ('/status', '/pause', '/resume', '/cancel', '/help', '/builder', '/answer'):
            raise ValueError('Unknown command. Use /help to see the supported commands.')
        if not self._authorized(config, sender, recipient, owner=True):
            raise ValueError('Only this channel’s approved owner may control runs.')
        runs = [r for r in self.store.runs() if r['chat_id'] == chat_id]
        if command == '/help':
            return ('Send a message to chat with Forge. /builder [idea] explores ideas one step at a time; '
                    '/builder off ends brainstorming. /build accepts your current brief in the connected project. '
                    '/plan <request> prepares a plan; /goal <request> starts tracked work. '
                    '/answer <reply> answers a question (use | between multiple answers). '
                    '/status, /pause, /resume and /cancel control this chat only.')
        if command=='/builder':
            if text.split(maxsplit=1)[-1].lower()!='off': raise ValueError('Use /builder [idea] to start brainstorming.')
            from forge_builder_guide import disable
            disable(self.store,chat_id)
            return 'Builder conversation ended. Normal chat is ready.'
        if command=='/answer':
            questions=self.service.get_interaction().pending(chat_id=chat_id)
            if not questions: raise ValueError('No question is waiting in this chat.')
            argument=text.split(maxsplit=1)[1] if len(text.split(maxsplit=1))>1 else ''
            values=[value.strip() for value in argument.split('|')]
            question=questions[0]
            if len(values)!=len(question['items']) or any(not value for value in values):
                raise ValueError('Send one answer per question: /answer reply one | reply two. Your questions are still waiting.')
            answers={item['id']:{'text':value} for item,value in zip(question['items'],values)}
            self.service.get_interaction().answer(question['id'],answers)
            return 'Answers saved. Forge is continuing.'
        if not runs:
            return 'This channel has no run yet.'
        run = runs[0]
        # Remote commands never accept an arbitrary run ID or project target.
        if command == '/pause':
            self.service.jobs.cancel(run['id'], pause=True)
            return 'Pause requested for ' + run['id'][:8] + '.'
        if command == '/cancel':
            self.service.jobs.cancel(run['id'])
            return 'Cancellation requested for ' + run['id'][:8] + '.'
        if command == '/resume':
            settings = {'permission_profile': permission_ceiling(config['permission_profile'], self.store.get_settings()['permission_profile'])}
            self.service.jobs.resume(run['id'], settings)
            return 'Resume requested for ' + run['id'][:8] + '.'
        return 'Run ' + run['id'][:8] + ': ' + run['status'] + '. ' + str(run.get('checkpoint', ''))[:500]

    def _dispatch_inbound(self, record):
        identifier, external_id = record['channel_id'], record['external_id']
        config={}; sender=recipient=''
        try:
            config = self._config(identifier)
            if not config.get('enabled') or not config.get('allow_inbound'):
                self._inbound_status(identifier, external_id, 'cancelled')
                return
            payload = json.loads(record['data'])
            if config['kind'] == 'telegram':
                if 'callback_query' in payload:
                    self._inbound_status(identifier, external_id, 'dispatching')
                    run_id = self._callback(config, payload['callback_query'])
                    self._inbound_status(identifier, external_id, 'dispatched', run_id)
                    return
                message = payload.get('message') or {}
                sender = self._recipient((message.get('from') or {}).get('id'))
                recipient = self._recipient((message.get('chat') or {}).get('id'))
                text = message.get('text', '')
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
                    raise ValueError('Send a text request of 1–16,000 characters.')
                if text.startswith('/pair '):
                    if not self._pair_request(config, sender, recipient, text, external_id):
                        raise ValueError('Pairing code is invalid or expired.')
                    self._inbound_status(identifier, external_id, 'paired')
                    return
                if not self._authorized(config, sender, recipient):
                    raise ValueError('Telegram sender and chat are not approved.')
            else:
                sender = recipient = 'webhook'
                text = payload['text']
            chat_id = self._route(config, recipient)
            command=text.split()[0].split('@',1)[0].lower() if text.startswith('/') else ''
            argument=text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1))>1 else ''
            conversation_command=command in ('/builder','/build','/plan','/goal') and not (command=='/builder' and argument.lower()=='off')
            if config['kind'] == 'telegram' and text.startswith('/') and not conversation_command:
                self._inbound_status(identifier, external_id, 'dispatching')
                response = self._command(config, sender, recipient, text, chat_id)
                self._enqueue(identifier, recipient, 'command', {'chat_id': recipient, 'text': redact(response)}, 'reply:' + identifier + ':' + external_id)
                self._inbound_status(identifier, external_id, 'dispatched')
                return
            if not config.get('inbound_tasks'):
                raise ValueError('Starting tasks from this channel is disabled. Enable it locally in Forge.')
            if config['kind']=='telegram' and not command and self.service.get_interaction().pending(chat_id=chat_id):
                self._inbound_status(identifier,external_id,'dispatching')
                response=self._command(config,sender,recipient,'/answer '+text,chat_id)
                self._enqueue(identifier,recipient,'command',{'chat_id':recipient,'text':response},'reply:'+identifier+':'+external_id)
                self._inbound_status(identifier,external_id,'dispatched')
                return
            if any(r['chat_id'] == chat_id and r['status'] not in TERMINAL for r in self.store.runs()):
                # Durable queued messages wait for their chat's current writer.
                return
            if conversation_command and command in ('/plan','/goal') and not argument:
                raise ValueError('Use '+command+' followed by your request.')
            settings = self.store.get_settings()
            data = {'text': text, 'chat_id': chat_id, 'project_id': config.get('project_id'),
                    'model': config.get('model') or settings['model'],
                    'provider_id': config.get('provider_id') or settings['provider_id'],
                    'permission_profile': permission_ceiling(config['permission_profile'], settings['permission_profile']),
                    'source_key': 'channel:' + identifier + ':' + external_id,
                    'channel_id': identifier, 'channel_external_id': external_id,
                    'agent_profile_id': config.get('agent_profile_id')}
            if conversation_command:
                data.update(text=argument or 'Help me come up with a project idea. Suggest three ideas and help me choose.',channel_command=command[1:])
            if hasattr(self.service, 'channel_start'):
                run = self.service.channel_start(data, identifier, external_id)
            else:
                self._inbound_status(identifier, external_id, 'dispatching')
                run = self.service.jobs.start(data)
            self._inbound_status(identifier, external_id, 'dispatched', run['id'])
        except ValueError as exc:
            self._inbound_status(identifier, external_id, 'rejected', error=redact(str(exc))[:300])
            if config.get('kind')=='telegram' and config.get('allow_outbound',True) and self._authorized(config,sender,recipient):
                self._enqueue(identifier,recipient,'error',{'chat_id':recipient,'text':redact(str(exc))[:300]},'error:'+identifier+':'+external_id)
        except Exception:
            # Unknown dispatch outcomes stay stopped; recovery reconciles source
            # identity but never starts another model or replays a tool action.
            self._inbound_status(identifier, external_id, 'outcome_unknown', error='Channel dispatch outcome is unknown. Inspect the local run journal.')

    def _notify(self, identifier, run_id, kind, data, db=None):
        values = (identifier, run_id, kind, encode(redact(data)), 1, _now())
        if db is not None:
            db.execute('INSERT OR IGNORE INTO channel_notifications VALUES(?,?,?,?,?,?)', values)
        else:
            with self.store._connection(transaction='write') as connection:
                connection.execute('INSERT OR IGNORE INTO channel_notifications VALUES(?,?,?,?,?,?)', values)

    def notifications(self):
        with self.store._connection() as db:
            rows = db.execute('SELECT * FROM channel_notifications ORDER BY created_at DESC LIMIT 200').fetchall()
        return [dict(id=r['id'], run_id=r['run_id'], kind=r['kind'], data=json.loads(r['data']), unread=bool(r['unread']), created_at=r['created_at']) for r in rows]

    @_channel_admitted
    def mark_read(self, identifier, read=True):
        if type(read) is not bool:
            raise ValueError('Read must be true or false.')
        with self.store._connection(transaction='write') as db:
            db.execute('UPDATE channel_notifications SET unread=? WHERE id=?', (int(not read), identifier))

    def _enqueue(self, identifier, recipient, kind, payload, key, db=None):
        delivery_id = sha256(key.encode()).hexdigest()
        values = (delivery_id, identifier, recipient, kind, encode(redact(payload)), 'pending', 0, 0, None, None, _now(), _now())
        if db is not None:
            db.execute('INSERT OR IGNORE INTO channel_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', values)
        else:
            with self.store._connection(transaction='write') as connection:
                connection.execute('INSERT OR IGNORE INTO channel_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', values)
        return delivery_id

    def on_run_event(self, event):
        """Safe Store.event subscriber: no SQL, network or secret access."""
        self.wake.set()

    def _journal_events(self):
        cursor = self._checkpoint('_global', 'event_rowid', 0)
        with self.store._connection() as db:
            events = db.execute('SELECT j.id AS event_rowid,e.* FROM channel_event_journal j JOIN run_events e ON e.run_id=j.run_id AND e.seq=j.seq WHERE j.id>? ORDER BY j.id LIMIT 500', (cursor,)).fetchall()
        for event in events:
            if self.stop_event.is_set():
                break
            summary = None
            with self.store._connection(transaction='write') as db:
                if event['type'] in ('approval', 'done', 'outcome_unknown', 'question'):
                    summary = self._event_delivery(db, event)
                self._set_checkpoint(db, '_global', 'event_rowid', event['event_rowid'])
            # Optional native adapter runs only after durable notification and
            # cursor commit, on the channel worker, with status metadata only.
            callback = getattr(self.service, 'desktop_notify', None)
            if summary and callable(callback):
                try:
                    callback(event['type'], {'run_id': summary['run_id'], 'status': summary['status']})
                except Exception:
                    pass

    def _event_delivery(self, db, event):
        run_row = db.execute('SELECT data FROM runs WHERE id=?', (event['run_id'],)).fetchone()
        if not run_row:
            return
        run = json.loads(run_row[0])
        payload = json.loads(event['payload'])
        kind = event['type']
        key = 'run:' + run['id'] + ':' + str(event['seq'])
        summary = {'run_id': run['id'], 'chat_id': run['chat_id'], 'project_id': run.get('project_id'), 'status': payload.get('status', run['status'])}
        if kind == 'approval':
            summary.update(approval_id=payload.get('approval_id') or payload.get('id'), tool=payload.get('tool', 'Tool action'))
        self._notify(key, run['id'], kind, summary, db)
        routes = db.execute('SELECT * FROM channel_routes WHERE chat_id=?', (run['chat_id'],)).fetchall()
        configs = [json.loads(r[0]) for r in db.execute("SELECT data FROM entities WHERE kind='channels'")]
        for config in configs:
            if not config.get('enabled') or not config.get('allow_outbound', True):
                continue
            if config['kind'] == 'webhook':
                if kind != 'done' or payload.get('status') != 'completed' or config.get('project_id') != run.get('project_id'):
                    continue
                content = {**summary, 'event': 'run.completed'}
                if config.get('include_content'):
                    content['result'] = self._result_excerpt(db, run['chat_id'], run)
                self._enqueue(config['id'], 'webhook', 'completion', content, key + ':' + config['id'], db)
                continue
            for route in routes:
                if route['channel_id'] != config['id'] or not config.get('connected'):
                    continue
                recipient = route['recipient']
                text = 'Forge run ' + run['id'][:8] + ': ' + summary['status'] + '.'
                outbound = {'chat_id': recipient, 'text': text}
                if kind == 'approval':
                    approval = db.execute('SELECT * FROM approvals WHERE id=? AND run_id=?', (summary['approval_id'], run['id'])).fetchone()
                    if not approval or approval['status'] != 'pending':
                        continue
                    owner = config.get('owner_id')
                    if not owner or not self._authorized(config, owner, recipient, owner=True):
                        continue
                    buttons = []
                    for allowed in (True, False):
                        token = secrets.token_urlsafe(24)
                        db.execute('INSERT INTO channel_callbacks VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   (token, config['id'], recipient, owner, run['id'], approval['id'], approval['action_hash'], int(allowed), self.clock() + 1800, None))
                        buttons.append({'text': 'Approve' if allowed else 'Deny', 'callback_data': 'forge:' + token})
                    action = json.loads(approval['data'])
                    target = redact(str(action.get('target', 'Local action')))[:400]
                    outbound.update(text='Approval required for ' + summary['tool'] + '\nTarget: ' + target + '\nRun: ' + run['id'][:8], reply_markup={'inline_keyboard': [buttons]})
                    if config.get('include_content'):
                        outbound['text'] += '\nArguments: ' + redact(encode(action.get('arguments', {})))[:1200]
                elif kind == 'done' and config.get('include_content'):
                    answer=self._result_excerpt(db,run['chat_id'],run)
                    outbound['text']=(answer or 'Forge finished without a text reply. Use /status for details.') if summary['status']=='completed' else text+'\n'+str(run.get('recovery') or run.get('checkpoint') or '')+'\n'+answer+'\nUse /status, /resume or /cancel.'
                elif kind=='question':
                    row=db.execute("SELECT data FROM entities WHERE kind='questions' AND id=?",(payload.get('question_id'),)).fetchone()
                    question=json.loads(row[0]) if row else {}
                    if question.get('chat_id')!=run['chat_id'] or not question.get('ready') or question.get('status')!='pending': continue
                    if config.get('include_content'):
                        outbound['text']='\n\n'.join(item['question']+'\n'+'\n'.join('• '+option['label']+(' (recommended)' if option.get('recommended') else '') for option in item.get('options',[])) for item in question.get('items',[]))+'\n\nReply in plain text, or /answer reply one | reply two.'
                    else:
                        outbound['text']='Forge is waiting for your answers. Enable conversation replies in Channels or answer in Forge.'
                elif kind == 'outcome_unknown':
                    outbound['text'] = 'Forge run ' + run['id'][:8] + ' has an action with unknown outcome. Inspect it in Forge before resuming.'
                previous=None
                delivery_key=key+':'+config['id']+':'+recipient
                for index,chunk in enumerate(telegram_chunks(outbound['text'])):
                    part={**outbound,'text':chunk}
                    if previous: part['_forge_previous']=previous
                    previous=self._enqueue(config['id'],recipient,kind,part,delivery_key+(':part:'+str(index) if index else ''),db)
        return summary

    @staticmethod
    def _result_excerpt(db, chat_id, run=None):
        # Bound legacy messages to the actual request interval. New messages are
        # additionally tagged with their producing run, including partial replies.
        if run:
            lower=run.get('request_message_id')
            if not isinstance(lower,int): return ''
            upper=None
            for row in db.execute('SELECT data FROM runs WHERE chat_id=?',(chat_id,)):
                candidate=json.loads(row[0]).get('request_message_id')
                if isinstance(candidate,int) and candidate>lower and (upper is None or candidate<upper): upper=candidate
            rows=db.execute("SELECT content,metadata FROM messages WHERE chat_id=? AND role='assistant' AND id>? AND (? IS NULL OR id<?) ORDER BY id",(chat_id,lower,upper,upper)).fetchall()
        else:
            rows=db.execute("SELECT content,metadata FROM messages WHERE chat_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(chat_id,)).fetchall()
        parts=[]
        for row in rows:
            metadata=json.loads(row['metadata'])
            if run and metadata.get('interaction_run_id') not in (None,run['id']): continue
            if metadata.get('tool_calls'):
                parts=[]  # Only the final response after the last tool round.
                continue
            if row['content']: parts.append(row['content'])
        result=redact('\n\n'.join(parts))
        if len(result)>64000: result=result[:64000]+'\n[Reply shortened for Telegram. The full conversation is saved in Forge.]'
        return result

    def outbox(self):
        with self.store._connection() as db:
            rows = db.execute('SELECT id,channel_id,recipient,kind,status,attempts,next_attempt,remote_id,last_error,created_at,updated_at FROM channel_outbox ORDER BY created_at DESC LIMIT 200').fetchall()
        return [dict(row) for row in rows]

    @_channel_admitted
    def retry(self, identifier, confirmed=False):
        with self.store._connection(transaction='write') as db:
            row = db.execute('SELECT * FROM channel_outbox WHERE id=?', (identifier,)).fetchone()
            if not row or row['status'] not in ('outcome_unknown', 'failed', 'retry'):
                raise ValueError('This delivery cannot be retried.')
            if row['status'] == 'outcome_unknown' and confirmed is not True:
                raise ValueError('Confirm possible duplicate notification before retrying an unknown Telegram delivery.')
            db.execute("UPDATE channel_outbox SET status='pending',next_attempt=0,last_error=NULL,updated_at=? WHERE id=?", (_now(), identifier))
        self.wake.set()
        return {'ok': True}

    def _deliver(self, record):
        try:
            config = self._config(record['channel_id'])
        except ValueError:
            config = {}
        if not config.get('enabled') or not config.get('allow_outbound', True):
            self._outcome(record['id'], 'cancelled')
            return
        if config['kind'] == 'telegram' and not config.get('connected'):
            return
        # Recheck revoked recipients before every retry.
        if config['kind'] == 'telegram' and not any(a['chat_id'] == record['recipient'] for a in config.get('allowlist', [])):
            self._outcome(record['id'], 'cancelled')
            return
        payload=json.loads(record['payload'])
        previous=payload.get('_forge_previous')
        if previous:
            with self.store._connection() as db:
                predecessor=db.execute('SELECT status FROM channel_outbox WHERE id=?',(previous,)).fetchone()
            if not predecessor or predecessor['status']!='delivered': return
        attempts = record['attempts'] + 1
        with self.store._connection(transaction='write') as db:
            claimed = db.execute("UPDATE channel_outbox SET status='sending',attempts=?,updated_at=? WHERE id=? AND status IN ('pending','retry')", (attempts, _now(), record['id'])).rowcount
        if claimed != 1:
            return
        try:
            secret = self._secret(config)
            payload = redact(json.loads(record['payload']), (secret,))
            payload.pop('_forge_previous',None)
            if config['kind'] == 'telegram':
                result = self.transport.telegram(secret, 'sendMessage', payload, self.stop_event)
                if not isinstance(result, dict) or type(result.get('message_id')) is not int:
                    raise DeliveryFailure('Telegram delivery outcome is unknown.', unknown=True)
                self._outcome(record['id'], 'delivered', remote_id=str(result['message_id']))
            else:
                body = encode(payload).encode()
                timestamp = str(int(self.clock()))
                nonce = record['id'] + '_' + str(attempts)
                headers = {'Content-Type': 'application/json', 'X-Forge-Timestamp': timestamp, 'X-Forge-Nonce': nonce,
                           'X-Forge-Delivery-ID': record['id'], 'Idempotency-Key': record['id'],
                           'X-Forge-Signature': webhook_signature(secret, timestamp, nonce, body)}
                self.transport.webhook(config['url'], body, headers, self.stop_event)
                self._outcome(record['id'], 'delivered')
        except DeliveryFailure as exc:
            if config['kind'] == 'telegram' and exc.unknown:
                self._outcome(record['id'], 'outcome_unknown', error='Delivery may have reached Telegram. Inspect before retrying.')
            else:
                self._outcome(record['id'], 'failed' if attempts >= 8 else 'retry',
                              error=redact(str(exc))[:300], delay=max(exc.retry_after, min(3600, 2 ** min(attempts, 11))))
        except Exception:
            self._outcome(record['id'], 'failed', error='Channel delivery failed. Check the local credential and channel configuration.')

    def _outcome(self, identifier, status, error=None, delay=0, remote_id=None):
        with self.store._connection(transaction='write') as db:
            db.execute('UPDATE channel_outbox SET status=?,last_error=?,next_attempt=?,remote_id=COALESCE(?,remote_id),updated_at=? WHERE id=?',
                       (status, error, self.clock() + delay, remote_id, _now(), identifier))

    def recover(self):
        with self.store._connection(transaction='write') as db:
            # Telegram has no sendMessage idempotency key. A lost HTTP outcome
            # requires local review; a webhook retries with its original ID.
            rows = db.execute("SELECT o.id,e.data FROM channel_outbox o LEFT JOIN entities e ON e.kind='channels' AND e.id=o.channel_id WHERE o.status='sending'").fetchall()
            for row in rows:
                config = json.loads(row['data']) if row['data'] else {}
                status = 'retry' if config.get('kind') == 'webhook' else 'outcome_unknown'
                db.execute('UPDATE channel_outbox SET status=?,last_error=?,next_attempt=0,updated_at=? WHERE id=?',
                           (status, 'Previous delivery outcome is unknown.', _now(), row['id']))
            inbound = db.execute("SELECT * FROM channel_inbound WHERE status IN ('dispatching','outcome_unknown')").fetchall()
            runs = [json.loads(r[0]) for r in db.execute('SELECT data FROM runs')]
            for row in inbound:
                key = 'channel:' + row['channel_id'] + ':' + row['external_id']
                matching = next((r for r in runs if r.get('source_key') == key), None)
                db.execute('UPDATE channel_inbound SET status=?,run_id=?,error=?,updated_at=? WHERE channel_id=? AND external_id=?',
                           ('dispatched' if matching else 'outcome_unknown', matching['id'] if matching else row['run_id'],
                            None if matching else 'Dispatch was interrupted. Inspect the local journal before a new request.',
                            _now(), row['channel_id'], row['external_id']))
        self.recovered = True

    def _poll_telegram(self, config):
        if not config.get('connected') or not config.get('allow_inbound'):
            return
        retry_at = self._checkpoint(config['id'], 'poll_retry_at', 0)
        if self.clock() < retry_at:
            return
        try:
            updates = self.transport.telegram(self._secret(config), 'getUpdates',
                {'offset': self._checkpoint(config['id'], 'telegram_offset', 0), 'timeout': 4, 'limit': 50, 'allowed_updates': ['message', 'callback_query']}, self.stop_event)
            if not isinstance(updates, list):
                raise DeliveryFailure('Telegram returned invalid updates.')
            # Journal each update before advancing offset. Replayed updates dedup.
            for update in sorted(updates, key=lambda u: u.get('update_id', -1)):
                if self.stop_event.is_set():
                    break
                self.receive_telegram(config['id'], update)
            with self.store._connection(transaction='write') as db:
                self._set_checkpoint(db, config['id'], 'poll_failures', 0)
                self._set_checkpoint(db, config['id'], 'poll_retry_at', 0)
        except Exception:
            failures = self._checkpoint(config['id'], 'poll_failures', 0) + 1
            with self.store._connection(transaction='write') as db:
                self._set_checkpoint(db, config['id'], 'poll_failures', failures)
                self._set_checkpoint(db, config['id'], 'poll_retry_at', self.clock() + min(300, 2 ** min(failures, 8)))
            self._notify('offline:' + config['id'], None, 'offline', {'channel_id': config['id'], 'message': 'Telegram is offline. Pending work is saved.'})

    def tick(self, poll=True):
        if self.stop_event.is_set():
            return
        with self.lock:
            if getattr(getattr(self.service, 'update_manager', None), 'applying', False) or self.stop_event.is_set():
                return
            if not self.recovered:
                self.recover()
            self._journal_events()
            if poll:
                for config in self.store.entities('channels'):
                    if self.stop_event.is_set():
                        break
                    if config['kind'] == 'telegram' and config.get('enabled'):
                        self._poll_telegram(config)
            with self.store._connection() as db:
                inbound = db.execute("SELECT * FROM channel_inbound WHERE status='pending' ORDER BY created_at LIMIT 50").fetchall()
            for record in inbound:
                if self.stop_event.is_set():
                    break
                self._dispatch_inbound(record)
            self._journal_events()
            with self.store._connection() as db:
                outgoing = db.execute("SELECT * FROM channel_outbox WHERE status IN ('pending','retry') AND next_attempt<=? ORDER BY created_at,rowid LIMIT 30", (self.clock(),)).fetchall()
            for record in outgoing:
                if self.stop_event.is_set():
                    break
                self._deliver(record)

    def start(self):
        with self.lock:
            if getattr(getattr(self.service, 'update_manager', None), 'applying', False):
                return
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._worker, name='Forge-channels', daemon=True)
            self.thread.start()

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # Do not log transport exceptions: Telegram embeds tokens in URLs.
                pass
            self.wake.wait(1)
            self.wake.clear()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def shutdown(self):
        self.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        self.transport.close()

