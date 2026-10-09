"""Channel fixtures never contact Telegram or deliver external webhooks."""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from forge_channels import (ChannelManager, ChannelTransport, DeliveryFailure,
                            MAX_BODY, permission_ceiling, redact, webhook_signature)
from forge_service import ForgeService
from forge_store import encode
from test_forge_workspaces import ScriptedEngine


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, value, reference=None):
        reference = reference or 'fixture_' + str(len(self.values))
        self.values[reference] = value
        return reference

    def get(self, reference):
        return self.values.get(reference)


class BotFixture:
    """HTTP responses follow the official Bot API result envelopes."""
    def __init__(self):
        self.calls = []
        self.updates = []
        self.send_error = None

    def __call__(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        body = json.loads(request.content)
        self.calls.append((method, body, dict(request.headers)))
        if method == 'getMe':
            result = {'id': 123456, 'is_bot': True, 'first_name': 'Forge fixture', 'username': 'forge_fixture_bot'}
        elif method == 'getUpdates':
            result = self.updates
        elif method == 'sendMessage':
            if self.send_error:
                return self.send_error(request)
            result = {'message_id': len(self.calls), 'chat': {'id': int(body['chat_id']), 'type': 'private'}, 'text': body['text']}
        else:
            return httpx.Response(200, json={'ok': True, 'result': True})
        return httpx.Response(200, json={'ok': True, 'result': result})


@pytest.fixture
def env(tmp_path):
    service = ForgeService(core=ScriptedEngine(), data_dir=tmp_path/'forge')
    service.store.update_settings({'model': 'fixture', 'permission_profile': 'full_access'})
    service.jobs._launch = Mock()
    bot = BotFixture()
    client = httpx.Client(transport=httpx.MockTransport(bot), trust_env=False)
    vault = MemoryVault()
    now = [1000000.0]
    manager = ChannelManager(service, vault=vault, transport=ChannelTransport(client), clock=lambda: now[0])
    yield SimpleNamespace(service=service, manager=manager, bot=bot, vault=vault, now=now, client=client)
    manager.shutdown()
    client.close()
    service.shutdown()


def configured(env, **changes):
    values = {'name': 'Local fixture', 'kind': 'telegram', 'token': '123456:fixture_bot_token_abcdefghijklmnop',
              'enabled': True, 'inbound_tasks': True, 'allowlist': [{'sender_id': '42', 'chat_id': '42'}],
              'owner_id': '42', 'permission_profile': 'full_access', **changes}
    config = env.manager.save(values)
    env.manager.test(config['id'])
    return config


def update(identifier=1, sender=42, chat=42, text='Inspect the local project.'):
    return {'update_id': identifier, 'message': {'message_id': identifier, 'from': {'id': sender, 'is_bot': False},
            'chat': {'id': chat, 'type': 'private'}, 'date': 1000000, 'text': text}}


def inbound(env, channel_id, external_id):
    with env.service.store._connection() as db:
        return dict(db.execute('SELECT * FROM channel_inbound WHERE channel_id=? AND external_id=?', (channel_id, str(external_id))).fetchone())


def test_opt_in_configuration_contains_only_credential_reference_and_getme_is_explicit(env):
    config = env.manager.save({'kind': 'telegram', 'token': '123456:fixture_bot_token_abcdefghijklmnop'})
    assert not env.bot.calls and config['enabled'] is False and config['inbound_tasks'] is False
    saved = env.service.store.entity('channels', config['id'])
    assert 'token' not in saved and saved['credential_ref'] in env.vault.values
    connected = env.manager.test(config['id'])
    assert connected['bot']['username'] == 'forge_fixture_bot'
    assert [c[0] for c in env.bot.calls] == ['getMe','setMyCommands']


def test_pairing_requires_local_approval_and_binds_sender_and_chat(env):
    config = configured(env, allowlist=[], owner_id=None)
    code = env.manager.pair_begin(config['id'])
    saved = env.service.store.entity('channels', config['id'])
    assert code['code'] not in encode(saved) and saved['pair_hash'] == sha256(code['code'].encode()).hexdigest()
    env.manager.receive_telegram(config['id'], update(1, text=code['instruction']))
    env.manager.tick(poll=False)
    assert not env.service.store.runs()
    pending = env.service.store.entities('channel_pairings')[0]
    assert pending['status'] == 'pending'
    paired = env.manager.pair_approve(config['id'], pending['id'])
    assert paired['allowlist'] == [{'sender_id': '42', 'chat_id': '42'}]
    assert paired['owner_id'] == '42'
    env.manager.receive_telegram(config['id'], update(2, sender=42, chat=99))
    env.manager.receive_telegram(config['id'], update(3))
    env.manager.tick(poll=False)
    assert inbound(env, config['id'], 2)['status'] == 'rejected'
    assert inbound(env, config['id'], 3)['status'] == 'dispatched'
    assert len(env.service.store.runs()) == 1


def test_pairing_expiry_and_bad_codes_do_not_add_allowlist(env):
    config = configured(env, allowlist=[])
    code = env.manager.pair_begin(config['id'])
    env.now[0] += 601
    env.manager.receive_telegram(config['id'], update(text=code['instruction']))
    env.manager.tick(poll=False)
    assert inbound(env, config['id'], 1)['status'] == 'rejected'
    assert env.service.store.entities('channel_pairings') == []


def test_unauthorized_sender_cannot_start_or_control_run(env):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update(1, sender=99))
    env.manager.receive_telegram(config['id'], update(2, sender=99, text='/pause'))
    env.manager.tick(poll=False)
    assert env.service.store.runs() == []
    assert [inbound(env, config['id'], i)['status'] for i in (1, 2)] == ['rejected', 'rejected']
    assert not [c for c in env.bot.calls if c[0] == 'sendMessage']


def test_task_start_must_be_enabled_locally(env):
    config = configured(env, inbound_tasks=False)
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    assert inbound(env, config['id'], 1)['status'] == 'rejected'
    assert env.service.store.runs() == []


def test_inbound_dedup_survives_restart_and_routes_to_stable_chat_with_ceiling(env):
    config = configured(env)
    env.service.store.update_settings({'permission_profile': 'always_ask'})
    assert env.manager.receive_telegram(config['id'], update())['accepted']
    env.manager.tick(poll=False)
    first = env.service.store.runs()[0]
    assert first['settings']['permission_profile'] == 'always_ask'
    assert first['source_key'] == 'channel:' + config['id'] + ':1'
    assert env.manager._checkpoint(config['id'], 'telegram_offset') == 2
    restarted = ChannelManager(env.service, vault=env.vault, transport=env.manager.transport, clock=lambda: env.now[0])
    assert restarted.receive_telegram(config['id'], update())['duplicate']
    restarted.tick(poll=False)
    assert len(env.service.store.runs()) == 1
    env.service.store.update_run(first['id'], status='completed')
    restarted.receive_telegram(config['id'], update(2, text='Continue in the same chat.'))
    restarted.tick(poll=False)
    runs = env.service.store.runs()
    assert len(runs) == 2 and all(r['chat_id'] == first['chat_id'] for r in runs)
    assert len([m for m in env.service.store.get_chat(first['chat_id'])['messages'] if m['role'] == 'user']) == 2


def test_queued_incoming_message_waits_for_current_writer(env):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update(1))
    env.manager.receive_telegram(config['id'], update(2, text='Next request.'))
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 1
    assert inbound(env, config['id'], 2)['status'] == 'pending'
    env.service.store.update_run(env.service.store.runs()[0]['id'], status='completed')
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 2


def test_dispatch_crash_after_commit_reconciles_source_key_without_duplicate(env, monkeypatch):
    config = configured(env)
    original = getattr(env.service, 'channel_start', None)
    if original is None:
        original = lambda data, *_: env.service.jobs.start(data)
    def crash_after(data, channel_id, external_id):
        original(data, channel_id, external_id)
        raise RuntimeError('Simulated process crash after accepted request.')
    monkeypatch.setattr(env.service, 'channel_start', crash_after, raising=False)
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 1
    assert inbound(env, config['id'], 1)['status'] == 'outcome_unknown'
    env.manager.recover()
    assert inbound(env, config['id'], 1)['status'] == 'dispatched'
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 1 and env.service.jobs._launch.call_count == 1


def test_crash_before_dispatch_keeps_pending_request_for_restart(env):
    config = configured(env)
    env.manager.receive_telegram(config['id'], update())
    restarted = ChannelManager(env.service, vault=env.vault, transport=env.manager.transport, clock=lambda: env.now[0])
    restarted.tick(poll=False)
    assert len(env.service.store.runs()) == 1


def approval_fixture(env, config):
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    action = {'tool': 'file_write', 'arguments': {'path': 'fixture.txt', 'text': 'example'}, 'target': 'Local fixture project'}
    approval_id = 'fixture_approval'
    with env.service.store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)', (approval_id, run['id'], 'fixture_invocation', sha256(encode(action).encode()).hexdigest(), 'pending', encode(action)))
    env.service.store.update_run(run['id'], status='awaiting_approval')
    env.service.store.event(run['id'], 'approval', id=approval_id, approval_id=approval_id, **action)
    env.manager.tick(poll=False)
    message = [c[1] for c in env.bot.calls if c[0] == 'sendMessage'][-1]
    token = message['reply_markup']['inline_keyboard'][0][0]['callback_data']
    return run, approval_id, token


def callback(identifier, token, sender=42, chat=42):
    return {'update_id': identifier, 'callback_query': {'id': 'fixture_callback_' + str(identifier), 'from': {'id': sender},
            'message': {'message_id': 999, 'chat': {'id': chat, 'type': 'private'}}, 'data': token}}


def test_approval_bound_to_recipient_owner_action_hash_and_single_use(env):
    config = configured(env)
    run, approval_id, token = approval_fixture(env, config)
    env.service.jobs.approve = Mock(return_value={'ok': True})
    env.manager.receive_telegram(config['id'], callback(2, token, sender=99))
    env.manager.receive_telegram(config['id'], callback(3, token, chat=99))
    env.manager.tick(poll=False)
    env.service.jobs.approve.assert_not_called()
    env.manager.receive_telegram(config['id'], callback(4, token))
    env.manager.receive_telegram(config['id'], callback(5, token))
    env.manager.tick(poll=False)
    env.service.jobs.approve.assert_called_once_with(run['id'], approval_id, True)
    assert inbound(env, config['id'], 5)['status'] == 'rejected'


def test_approval_rejects_changed_action_and_current_deny(env):
    config = configured(env)
    _, approval_id, token = approval_fixture(env, config)
    env.service.jobs.approve = Mock()
    with env.service.store._connection(transaction='write') as db:
        db.execute('UPDATE approvals SET data=? WHERE id=?', (encode({'tool': 'different'}), approval_id))
    env.manager.receive_telegram(config['id'], callback(2, token))
    env.manager.tick(poll=False)
    env.service.jobs.approve.assert_not_called()
    assert inbound(env, config['id'], 2)['status'] == 'rejected'


def test_owner_controls_only_own_stable_chat_and_permissions_cannot_escalate(env):
    config = configured(env, allowlist=[{'sender_id': '42', 'chat_id': '42'}, {'sender_id': '77', 'chat_id': '42'}])
    env.manager.receive_telegram(config['id'], update(1))
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    env.service.jobs.cancel = Mock(return_value={'ok': True})
    env.manager.receive_telegram(config['id'], update(2, sender=77, text='/pause'))
    env.manager.receive_telegram(config['id'], update(3, text='/pause arbitrary_run_id'))
    env.manager.tick(poll=False)
    env.service.jobs.cancel.assert_called_once_with(run['id'], pause=True)
    env.service.store.update_run(run['id'], status='paused')
    env.service.store.update_settings({'permission_profile': 'deny_access'})
    env.service.jobs.resume = Mock(return_value={'ok': True})
    env.manager.receive_telegram(config['id'], update(4, text='/resume'))
    env.manager.tick(poll=False)
    env.service.jobs.resume.assert_called_once_with(run['id'], {'permission_profile': 'deny_access'})


def test_outbound_unknown_outcome_is_not_automatically_resent(env):
    config = configured(env)
    def timeout(request):
        raise httpx.ReadTimeout('Synthetic response was lost.', request=request)
    env.bot.send_error = timeout
    delivery_id = env.manager._enqueue(config['id'], '42', 'done', {'chat_id': '42', 'text': 'Completed.'}, 'fixture_done')
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'outcome_unknown'
    sent = len([c for c in env.bot.calls if c[0] == 'sendMessage'])
    env.now[0] += 10000
    env.manager.tick(poll=False)
    assert len([c for c in env.bot.calls if c[0] == 'sendMessage']) == sent
    with pytest.raises(ValueError, match='duplicate'):
        env.manager.retry(delivery_id)
    env.bot.send_error = None
    env.manager.retry(delivery_id, confirmed=True)
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'delivered'


def test_outbox_restart_marks_inflight_telegram_unknown(env):
    config = configured(env)
    identity = env.manager._enqueue(config['id'], '42', 'done', {'chat_id': '42', 'text': 'Completed.'}, 'fixture_restart')
    with env.service.store._connection(transaction='write') as db:
        db.execute("UPDATE channel_outbox SET status='sending' WHERE id=?", (identity,))
    env.manager.recover()
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'outcome_unknown'
    assert not [c for c in env.bot.calls if c[0] == 'sendMessage']


def test_stale_outbox_snapshot_cannot_send_a_cancelled_delivery(env):
    config = configured(env)
    identity = env.manager._enqueue(config['id'], '42', 'done', {'chat_id': '42', 'text': 'Private queued result.'}, 'fixture_stale_snapshot')
    with env.service.store._connection() as db:
        snapshot = dict(db.execute('SELECT * FROM channel_outbox WHERE id=?', (identity,)).fetchone())
    with env.service.store._connection(transaction='write') as db:
        db.execute("UPDATE channel_outbox SET status='cancelled',payload='{}' WHERE id=?", (identity,))
    env.manager._deliver(snapshot)
    assert not [call for call in env.bot.calls if call[0] == 'sendMessage']
    assert env.manager.outbox()[0]['status'] == 'cancelled'


def test_telegram_retry_backoff_and_revocation_are_durable(env):
    config = configured(env)
    env.bot.send_error = lambda request: httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 30}})
    env.manager._enqueue(config['id'], '42', 'done', {'chat_id': '42', 'text': 'Completed.'}, 'fixture_rate_limit')
    env.manager.tick(poll=False)
    first = env.manager.outbox()[0]
    assert first['status'] == 'retry' and first['next_attempt'] == env.now[0] + 30
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['attempts'] == 1
    env.manager.pair_revoke(config['id'], '42', '42')
    env.now[0] += 31
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'cancelled'


def webhook_config(env, **changes):
    return env.manager.save({'kind': 'webhook', 'name': 'Webhook fixture', 'secret': 'fixture_hmac_secret_abcdef',
                            'enabled': True, 'allow_inbound': True, 'inbound_tasks': True,
                            'url': 'https://fixture.example/completed', **changes})


def signed(env, config, data, nonce='fixture_nonce_00000001', timestamp=None):
    body = encode(data).encode()
    stamp = str(int(env.now[0])) if timestamp is None else str(timestamp)
    secret = env.vault.get(config['credential_ref'])
    return body, {'X-Forge-Timestamp': stamp, 'X-Forge-Nonce': nonce, 'X-Forge-Signature': webhook_signature(secret, stamp, nonce, body)}


def test_webhook_mac_replay_timestamp_body_and_project_boundaries(env, tmp_path):
    folder = tmp_path/'project'; folder.mkdir()
    project = env.service.store.create_project('Fixture', str(folder))
    config = webhook_config(env, project_id=project['id'])
    body, headers = signed(env, config, {'id': 'event_1', 'text': 'Inspect project.'})
    with pytest.raises(ValueError, match='signature'):
        env.manager.ingest(config['id'], body + b' ', headers)
    with pytest.raises(ValueError, match='timestamp'):
        old_body, old_headers = signed(env, config, {'text': 'Old request.'}, timestamp=env.now[0] - 301)
        env.manager.ingest(config['id'], old_body, old_headers)
    with pytest.raises(ValueError, match='64 KiB'):
        env.manager.ingest(config['id'], b' ' * (MAX_BODY + 1), headers)
    other_body, other_headers = signed(env, config, {'text': 'Escape project.', 'project_id': 'other_project'})
    with pytest.raises(ValueError, match='different project'):
        env.manager.ingest(config['id'], other_body, other_headers)
    assert env.manager.ingest(config['id'], body, headers)['accepted']
    with pytest.raises(ValueError, match='already been used'):
        env.manager.ingest(config['id'], body, headers)
    replay_body, replay_headers = signed(env, config, {'id': 'event_1', 'text': 'Same ID.'}, nonce='fixture_nonce_00000002')
    assert env.manager.ingest(config['id'], replay_body, replay_headers)['duplicate']
    env.manager.tick(poll=False)
    assert len(env.service.store.runs()) == 1
    assert env.service.store.runs()[0]['project_id'] == project['id']


def test_webhook_completion_redaction_private_content_default_and_retry_id(env):
    config = webhook_config(env)
    requests = []
    def webhook_http(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 204)
    env.manager.transport = ChannelTransport(httpx.Client(transport=httpx.MockTransport(webhook_http), trust_env=False))
    run = env.service.jobs.start({'text': 'Local fixture request.'})
    env.service.store.add_message(run['chat_id'], 'assistant', 'Private result sk-example_secret_abcdefghijklmnop')
    env.service.store.update_run(run['id'], status='completed')
    env.service.store.event(run['id'], 'done', status='completed')
    env.manager.tick(poll=False)
    body = json.loads(requests[0].content)
    assert 'result' not in body and 'Private result' not in requests[0].content.decode()
    first = env.manager.outbox()[0]
    assert first['status'] == 'retry'
    env.now[0] += 3
    env.manager.tick(poll=False)
    assert env.manager.outbox()[0]['status'] == 'delivered'
    assert requests[0].headers['Idempotency-Key'] == requests[1].headers['Idempotency-Key']
    for request in requests:
        signature = webhook_signature(env.vault.get(config['credential_ref']), request.headers['X-Forge-Timestamp'], request.headers['X-Forge-Nonce'], request.content)
        assert request.headers['X-Forge-Signature'] == signature
    env.manager.transport.client.close()


def test_content_export_requires_configuration_and_redacts_credentials(env):
    config = configured(env, include_content=True)
    env.manager.receive_telegram(config['id'], update())
    env.manager.tick(poll=False)
    run = env.service.store.runs()[0]
    token = env.vault.get(config['credential_ref'])
    env.service.store.add_message(run['chat_id'], 'assistant', 'Result token=' + token + ' and Bearer fixture_credential_value')
    env.service.store.update_run(run['id'], status='completed')
    env.service.store.event(run['id'], 'done', status='completed')
    env.manager.tick(poll=False)
    text = [c[1]['text'] for c in env.bot.calls if c[0] == 'sendMessage'][-1]
    assert 'Result' in text and token not in text and 'fixture_credential_value' not in text
    assert '[redacted]' in text


def test_event_subscriber_is_nonblocking_and_notifications_read_state_persists(env):
    env.manager.transport.telegram = Mock(side_effect=AssertionError('Subscriber attempted network I/O.'))
    env.manager.on_run_event({'type': 'done', 'run_id': 'fixture'})
    env.manager.transport.telegram.assert_not_called()
    run = env.service.jobs.start({'text': 'Local fixture request.'})
    env.service.store.update_run(run['id'], status='completed')
    env.service.store.event(run['id'], 'done', status='completed')
    env.manager.tick(poll=False)
    notification = env.manager.notifications()[0]
    assert notification['unread'] and notification['run_id'] == run['id']
    env.manager.mark_read(notification['id'])
    env.manager.tick(poll=False)
    assert not env.manager.notifications()[0]['unread'] and len(env.manager.notifications()) == 1


def test_native_notification_callback_runs_after_commit_with_status_metadata_only(env):
    run = env.service.jobs.start({'text': 'Private prompt content must not reach the native adapter.'})
    env.service.store.update_run(run['id'], status='completed')
    calls = []
    def desktop_notify(kind, summary):
        # A fresh connection can see both the notification and the advanced
        # cursor. The callback cannot observe uncommitted journal processing.
        assert len(env.manager.notifications()) == 1
        assert env.manager._checkpoint('_global', 'event_rowid', 0) > 0
        calls.append((kind, summary))
    env.service.desktop_notify = desktop_notify
    env.service.store.event(run['id'], 'done', status='completed', private_detail='Do not forward this.')
    assert not calls
    env.manager.tick(poll=False)
    env.manager.tick(poll=False)
    assert calls == [('done', {'run_id': run['id'], 'status': 'completed'})]
    env.service.desktop_notify = Mock(side_effect=RuntimeError('Native adapter unavailable.'))
    env.service.store.event(run['id'], 'outcome_unknown', arguments={'private': 'Never forwarded.'})
    env.manager.tick(poll=False)
    assert len(env.manager.notifications()) == 2


def test_offline_poll_backoff_preserves_pending_work_and_cancel_stops_delivery(env):
    config = configured(env)
    env.manager.transport.telegram = Mock(side_effect=DeliveryFailure('Offline fixture.'))
    env.manager.tick()
    assert env.manager._checkpoint(config['id'], 'poll_retry_at') > env.now[0]
    assert env.manager.notifications()[0]['kind'] == 'offline'
    count = env.manager.transport.telegram.call_count
    env.manager.tick()
    assert env.manager.transport.telegram.call_count == count
    env.manager.receive_telegram(config['id'], update())
    env.manager.stop()
    env.manager.tick(poll=False)
    assert inbound(env, config['id'], 1)['status'] == 'pending' and not env.service.store.runs()


@pytest.mark.parametrize('requested,current,expected', [('full_access','always_ask','always_ask'), ('full_access','deny_access','deny_access'), ('always_ask','full_access','always_ask')])
def test_permission_ceiling(requested, current, expected):
    assert permission_ceiling(requested, current) == expected


def test_secret_redaction_and_webhook_url_validation():
    result = redact({'authorization': 'private', 'text': 'Bearer ABCDEF password=secret sk-abcdefghijklmnop', 'token': 'private'})
    assert result['authorization'] == result['token'] == '[redacted]'
    assert 'ABCDEF' not in result['text'] and 'abcdefghijklmnop' not in result['text']
    ChannelManager._validate_url('http://127.0.0.1:1234/fixture')
    with pytest.raises(ValueError, match='HTTPS'):
        ChannelManager._validate_url('http://external.example/callback')
    with pytest.raises(ValueError, match='HTTPS'):
        ChannelManager._validate_url('https://user:password@external.example/callback')


def test_bot_api_over_local_http_fixture_commits_offset_before_dispatch(env):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            method = self.path.rsplit('/', 1)[-1]
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((method, payload))
            if method == 'getMe':
                result = {'id': 123456, 'is_bot': True, 'username': 'local_fixture_bot'}
            elif method == 'getUpdates':
                result = [update(100)] if payload['offset'] < 101 else []
            else:
                result = {'message_id': 500, 'chat': {'id': 42, 'type': 'private'}}
            body = encode({'ok': True, 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = httpx.Client(trust_env=False, timeout=2)
    manager = ChannelManager(env.service, vault=env.vault, transport=ChannelTransport(client, 'http://127.0.0.1:' + str(server.server_port)), clock=lambda: env.now[0])
    try:
        config = manager.save({'kind': 'telegram', 'token': '123456:fixture_bot_token_abcdefghijklmnop', 'enabled': True,
                              'inbound_tasks': True, 'allowlist': [{'sender_id': '42', 'chat_id': '42'}], 'owner_id': '42'})
        manager.test(config['id'])
        manager.tick()
        assert manager._checkpoint(config['id'], 'telegram_offset') == 101
        assert inbound(env, config['id'], 100)['status'] == 'dispatched'
        manager.tick()
        assert len(env.service.store.runs()) == 1
        assert received[-1][0] == 'getUpdates' and received[-1][1]['offset'] == 101
    finally:
        manager.shutdown()
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
