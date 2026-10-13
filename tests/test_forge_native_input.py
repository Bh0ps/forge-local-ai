"""Synthetic speech and native-input retention; no microphone or model download."""
import base64
from types import SimpleNamespace
import threading
import time

import pytest

from dictation import Dictation
from forge_native_input import NativeInputGuard


SCOPE = {'kind': 'chat', 'project_id': 'synthetic-project', 'chat_id': 'synthetic-chat'}


def complete(dictation):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = dictation.status()
        if result['state'] in ('done', 'error', 'cancelled'):
            return result
        time.sleep(.005)
    pytest.fail('Synthetic dictation did not complete')


@pytest.fixture
def speech(tmp_path):
    text = [' synthetic speech']
    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text=text[0])]), SimpleNamespace(language='en')
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model())
    return dictation, NativeInputGuard(dictation), text


def deliver(speech, scope=SCOPE):
    dictation, guard, _ = speech
    with guard.operation(scope):
        dictation.upload(base64.b64encode(b'synthetic audio').decode())
    return guard.decorate(complete(dictation))


def test_nonempty_result_requires_original_saved_scope_and_does_not_store_text(speech):
    dictation, guard, _ = speech
    result = deliver(speech)
    assert guard.idle() is False
    token = result['delivery_id']
    assert len(token) == 64 and result['text'] == 'synthetic speech'
    assert 'synthetic speech' not in repr(guard.jobs) + repr(guard.deliveries)
    assert guard.confirm({'scope': {**SCOPE, 'chat_id': 'other'}}, (token,)) is False
    assert guard.idle() is False
    assert guard.confirm({'scope': SCOPE}, (token,)) is True
    assert guard.idle() is True
    assert guard.confirm({'scope': SCOPE}, (token,)) is True
    assert guard.decorate(dictation.status())['delivery_id'] == token


def test_later_job_cannot_overwrite_pending_prior_delivery(speech):
    _, guard, _ = speech
    first = deliver(speech)
    second = deliver(speech, {**SCOPE, 'chat_id': 'second'})
    assert first['id'] != second['id']
    assert guard.confirm({'scope': SCOPE}, (first['delivery_id'],))
    assert guard.idle() is False
    assert guard.confirm({'scope': {**SCOPE, 'chat_id': 'second'}}, (second['delivery_id'],))
    assert guard.idle() is True


def test_subset_confirmation_is_atomic_and_never_expires_pending_work(speech):
    _, guard, _ = speech
    tokens = tuple(deliver(speech)['delivery_id'] for _ in range(17))
    assert guard.confirm({'scope': SCOPE}, tokens) is False
    assert guard.confirm({'scope': SCOPE}, (tokens[0], 'f' * 64)) is False
    assert not any(record['consumed'] for record in guard.deliveries.values())
    assert guard.confirm({'scope': SCOPE}, tokens[:16])
    assert guard.idle() is False
    assert guard.confirm({'scope': SCOPE}, tokens[16:])
    assert guard.idle() is True


def test_empty_done_has_no_delivery_and_no_false_pending_work(speech):
    _, guard, text = speech
    text[0] = ' '
    result = deliver(speech)
    assert result['text'] == '' and 'delivery_id' not in result
    assert guard.idle() is True


def test_unknown_scope_completion_fails_closed(speech):
    dictation, guard, _ = speech
    dictation.upload(base64.b64encode(b'synthetic audio').decode())
    complete(dictation)
    assert guard.idle() is False and guard.unknown


def test_explicit_cancel_only_discards_current_exact_job(speech):
    dictation, guard, _ = speech
    old, current = deliver(speech), deliver(speech)
    response = dictation.dispatch('cancel', {'id': old['id']})
    assert response['not_current'] is True and response['id'] == current['id']
    assert guard.idle() is False
    assert dictation.dispatch('cancel', {'id': current['id']})['state'] == 'cancelled'
    assert guard.idle() is False
    assert guard.confirm({'scope': SCOPE}, (old['delivery_id'],))
    assert guard.idle() is True


@pytest.mark.parametrize('identifier', [True, '', '../private', 'A' * 32, '1' * 31])
def test_malformed_exact_cancel_is_not_an_unconditional_cancel(speech, identifier):
    dictation, _, _ = speech
    result = deliver(speech)
    with pytest.raises(ValueError, match='exact dictation'):
        dictation.cancel(identifier)
    assert dictation.status()['id'] == result['id'] and dictation.status()['state'] == 'done'


def test_recording_identity_survives_stop_and_cancel_does_not_touch_new_stream(tmp_path):
    streams = []
    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs['callback']; self.stopped = False; streams.append(self)
        def start(self): self.callback(b'\0\0' * 160, 160, None, None)
        def stop(self): self.stopped = True
        def close(self): pass
    class Model:
        def transcribe(self, audio, **kwargs): return iter([]), SimpleNamespace(language='en')
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model(), stream_factory=Stream)
    first = dictation.start()
    assert dictation.stop()['id'] == first['id']
    complete(dictation)
    second = dictation.start()
    assert second['id'] != first['id']
    assert dictation.cancel(first['id'])['not_current']
    assert streams[1].stopped is False and dictation.status()['state'] == 'recording'
    dictation.cancel(second['id'])
    assert streams[1].stopped is True


def test_pending_native_operation_blocks_readiness_before_producer_starts(speech):
    _, guard, _ = speech
    with guard.operation(SCOPE):
        assert guard.idle() is False
    assert guard.idle() is True
