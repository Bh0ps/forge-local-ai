import threading
from types import SimpleNamespace

import pytest

from forge_store import ForgeStore
from forge_submissions import SubmissionManager


def manager(tmp_path):
    store = ForgeStore(tmp_path)
    return SubmissionManager(SimpleNamespace(store=store, admission_lock=threading.RLock()))


def admit_run(store, data):
    run, _ = store.accept_request({'id': 'run1', 'request': data['text'], 'settings': store.get_settings(),
                                  'project_id': None, 'mode': 'chat'}, source_key=data['source_key'])
    return {'id': run['id'], 'chat_id': run['chat_id'], 'status': run['status']}


def test_submission_replay_and_changed_payload(tmp_path):
    receipts = manager(tmp_path)
    data = {'client_submission_id': 'send1', 'text': 'Work'}
    first = receipts.admit('start_chat', data, lambda clean: admit_run(receipts.store, clean))
    again = receipts.admit('start_chat', data, lambda _: pytest.fail('duplicate effect'))
    assert again['id'] == first['id'] and again['submission_reused']
    with pytest.raises(ValueError, match='different content'):
        receipts.admit('start_chat', {**data, 'text': 'New draft'}, lambda _: pytest.fail('duplicate effect'))
    assert len(receipts.store.runs()) == 1


def test_admitted_unknown_response_recovers_after_restart(tmp_path):
    receipts = manager(tmp_path)
    data = {'client_submission_id': 'send1', 'text': 'Work'}
    key = receipts.identity('start_chat', data)
    record = receipts.store.save_entity('ui_submissions', {'id': key, 'state': 'admitting',
        'fingerprint': receipts.fingerprint(data), 'source_key': 'ui:' + key})
    admit_run(receipts.store, {**data, 'source_key': record['source_key']})
    restarted = manager(tmp_path)
    result = restarted.admit('start_chat', data, lambda _: pytest.fail('must inspect committed run'))
    assert result['id'] == 'run1' and result['submission_reused']


def test_unjournaled_outcome_never_repeats_side_effect(tmp_path):
    receipts = manager(tmp_path)
    data = {'client_submission_id': 'send1', 'text': '/new'}
    calls = []
    def interrupted(clean):
        calls.append(clean)
        raise RuntimeError('lost response')
    with pytest.raises(RuntimeError):
        receipts.admit('command', data, interrupted)
    with pytest.raises(ValueError, match='unknown outcome'):
        manager(tmp_path).admit('command', data, interrupted)
    assert len(calls) == 1
    status = receipts.get({'action': 'command', **data})
    assert status['state'] == 'unknown' and status['run_id'] is None
    assert status['admission_error'] == 'lost response' and 'error' not in status


def test_admitted_exception_returns_existing_run(tmp_path):
    receipts = manager(tmp_path)
    data = {'client_submission_id': 'send1', 'text': 'Work'}
    def interrupted(clean):
        admit_run(receipts.store, clean)
        raise RuntimeError('lost response')
    assert receipts.admit('start_chat', data, interrupted)['id'] == 'run1'


def test_identity_captures_owner_and_payload_digest_only(tmp_path):
    receipts = manager(tmp_path)
    data = {'client_submission_id': 'send1', 'text': 'private prompt', 'chat_id': 'a'}
    first = receipts.admit('start_chat', data, lambda _: {'id': 'fake'})
    second = receipts.admit('start_chat', {**data, 'chat_id': 'b'}, lambda _: {'id': 'other'})
    assert first['submission_id'] != second['submission_id']
    rows = receipts.store.entities('ui_submissions')
    assert all('private prompt' not in str(row) for row in rows)


def test_invalid_submission_identity_is_not_executed(tmp_path):
    receipts = manager(tmp_path)
    with pytest.raises(ValueError, match='identity'):
        receipts.admit('start_chat', {'client_submission_id': '../bad'}, lambda _: pytest.fail('invalid'))
    assert not receipts.store.entities('ui_submissions')
