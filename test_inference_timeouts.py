"""Cold start, meaningful progress and Stop regressions shared by both engines."""
import asyncio
import json
import threading
import time

import httpx
import pytest

import core
from core import Core
from forge_inference import CompatibleProvider, ProviderPool
from inference_stream import (InferenceTimeoutError, InferenceTimeouts,
                              FIRST_RESPONSE_TIMEOUT_SECONDS, GENERATION_IDLE_TIMEOUT_SECONDS,
                              TOTAL_REQUEST_TIMEOUT_SECONDS)

REQUEST = {'model': 'fixture-large-model', 'context': 131072, 'tokens': 1024,
           'thinking': True, 'messages': [{'role': 'user', 'content': 'Continue the saved task.'}]}


class TimedBody(httpx.AsyncByteStream):
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall = chunks, stall
        self.closed = threading.Event()

    async def __aiter__(self):
        for delay, chunk in self.chunks:
            await asyncio.sleep(delay)
            yield chunk
        if self.stall:
            await asyncio.sleep(60)

    async def aclose(self):
        self.closed.set()


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def limits(monkeypatch, first=.18, idle=.10, total=.60):
    monkeypatch.setattr(core, 'FIRST_RESPONSE_TIMEOUT_SECONDS', first)
    monkeypatch.setattr(core, 'GENERATION_IDLE_TIMEOUT_SECONDS', idle)
    monkeypatch.setattr(core, 'STREAM_DEADLINE_SECONDS', total)


def packet(engine, content=None, thinking=None, tools=None, done=False):
    message = {}
    if content is not None: message['content'] = content
    if thinking is not None: message['thinking' if engine == 'ollama' else 'reasoning_content'] = thinking
    if tools is not None: message['tool_calls'] = tools
    if engine == 'ollama':
        return (json.dumps({'message': message, 'done': done}, ensure_ascii=False) + '\n').encode()
    result = {'choices': [{'delta': message, 'finish_reason': 'stop' if done else None}]}
    return ('data: ' + json.dumps(result, ensure_ascii=False) + '\n\n').encode()


def stream(engine, cancel=None):
    if engine == 'ollama': return Core().stream_agent(REQUEST, cancel)
    return CompatibleProvider({'url': 'http://127.0.0.1:8080/v1'}).generate(REQUEST, cancel)


def test_production_policy_gives_a_full_hour_before_first_output_and_keeps_probes_fast():
    assert FIRST_RESPONSE_TIMEOUT_SECONDS == 3600
    assert GENERATION_IDLE_TIMEOUT_SECONDS == 180
    assert TOTAL_REQUEST_TIMEOUT_SECONDS == 5400
    policy = Core._inference_timeouts()
    assert policy.first_response == 3600 and policy.total == 5400
    assert policy.http_timeout().read is None
    assert Core._timeout('/chat').read == 3600
    assert Core._timeout('/tags').read == 3
    assert Core._timeout('/show').read == 8
    assert Core._timeout('/chat').connect == 2


@pytest.mark.parametrize('invalid', [0, -1, True, float('nan'), float('inf'), 7201, '3600'])
def test_deadline_values_remain_bounded(invalid):
    with pytest.raises(ValueError, match='Invalid inference timeout'):
        InferenceTimeouts(first_response=invalid)


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
@pytest.mark.parametrize('stage', ['headers', 'tokens'])
def test_cold_loading_can_exceed_former_read_deadline_without_changing_model_context(monkeypatch, engine, stage):
    # Seconds represent the old 90 s read at 1/1000 scale. The transport has
    # no read timeout; the explicit first-response policy permits this delay.
    limits(monkeypatch, first=.30, idle=.12, total=.8)
    body = TimedBody([(.13 if stage == 'tokens' else 0, packet(engine, content='Ready')),
                      (0, packet(engine, done=True))])
    calls = []

    async def handler(request):
        calls.append(request)
        data = json.loads(request.content)
        assert data['model'] == REQUEST['model']
        if engine == 'ollama':
            assert data['options']['num_ctx'] == REQUEST['context'] and data['think'] is True
        assert request.extensions['timeout']['read'] is None
        if stage == 'headers': await asyncio.sleep(.13)
        return httpx.Response(200, stream=body)

    transport(monkeypatch, handler)
    result = list(stream(engine))
    assert result[0]['message']['content'] == 'Ready' and result[-1]['done'] is True
    assert len(calls) == 1 and body.closed.is_set()


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
def test_empty_heartbeats_do_not_hide_a_missing_first_response(monkeypatch, engine):
    limits(monkeypatch, first=.14, idle=.06, total=.5)
    body = TimedBody([(.025, packet(engine, content='')) for _ in range(20)], stall=True)
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    seen = []
    with pytest.raises(InferenceTimeoutError, match='first response') as error:
        seen.extend(stream(engine))
    assert error.value.stage == 'first_response'
    assert not any(item.get('done') for item in seen) and body.closed.is_set()
    assert 'resume the run' in str(error.value)


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
@pytest.mark.parametrize('progress', ['thinking', 'tool'])
def test_reasoning_and_tool_progress_start_generation_idle_timer(monkeypatch, engine, progress):
    limits(monkeypatch, first=.09, idle=.12, total=.6)
    kwargs = {'thinking': 'Checking the task.'} if progress == 'thinking' else {
        'tools': [{'index': 0, 'function': {'name': 'read_file', 'arguments': '{'}}]}
    body = TimedBody([(.04, packet(engine, **kwargs)), (.07, packet(engine, **kwargs)),
                      (.06, packet(engine, content='Finished', done=True))])
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    result = list(stream(engine))
    assert result[-1]['done'] is True and body.closed.is_set()


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
def test_empty_packets_cannot_extend_generation_stall_deadline(monkeypatch, engine):
    limits(monkeypatch, first=.3, idle=.10, total=.5)
    body = TimedBody([(0, packet(engine, content='Partial output'))] +
                     [(.02, packet(engine, content='')) for _ in range(20)], stall=True)
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    seen = []
    with pytest.raises(InferenceTimeoutError, match='continue generating') as error:
        seen.extend(stream(engine))
    assert error.value.stage == 'generation_idle'
    assert seen[0]['message']['content'] == 'Partial output'
    assert not any(item.get('done') for item in seen) and body.closed.is_set()


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
def test_total_bound_stops_even_continuous_model_progress(monkeypatch, engine):
    limits(monkeypatch, first=.3, idle=.1, total=.16)
    body = TimedBody([(.025, packet(engine, content='Still working.')) for _ in range(20)], stall=True)
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    seen = []
    with pytest.raises(InferenceTimeoutError, match='request limit') as error:
        seen.extend(stream(engine))
    assert error.value.stage == 'total' and seen and body.closed.is_set()
    assert not any(item.get('done') for item in seen)


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
@pytest.mark.parametrize('stage', ['headers', 'tokens'])
def test_stop_before_response_interrupts_socket_without_retry(monkeypatch, engine, stage):
    entered, exited = threading.Event(), threading.Event()
    body = TimedBody([], stall=True)
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        if stage == 'headers':
            try: await asyncio.sleep(60)
            finally: exited.set()
        return httpx.Response(200, stream=body)

    transport(monkeypatch, handler)
    cancel, result, errors = threading.Event(), [], []

    def consume():
        try: result.extend(stream(engine, cancel))
        except Exception as exc: errors.append(exc)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert entered.wait(2)
    started = time.monotonic()
    cancel.set()
    consumer.join(2)
    assert not consumer.is_alive() and time.monotonic() - started < 1
    assert len(calls) == 1 and result == [] and errors == []
    assert exited.is_set() if stage == 'headers' else body.closed.is_set()


def test_compaction_uses_same_loading_window_and_retains_original_input(monkeypatch):
    limits(monkeypatch, first=.3, idle=.12, total=.8)
    body = TimedBody([(.13, packet('ollama', content='Evidence saved. Next: run tests.', done=True))])
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        assert request.extensions['timeout']['read'] is None
        return httpx.Response(200, stream=body)

    transport(monkeypatch, handler)
    messages = [{'role': 'user', 'content': 'Check the project 漢字 🌍.'}]
    assert Core().compact_messages('fixture-large-model', '', messages) == 'Evidence saved. Next: run tests.'
    assert messages == [{'role': 'user', 'content': 'Check the project 漢字 🌍.'}]
    assert len(captured) == 1 and body.closed.is_set()


def test_compatible_stop_releases_gpu_lease_and_records_cancelled_usage_once(monkeypatch):
    entered = threading.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await asyncio.sleep(60)
        raw = packet('compatible', content='Done', done=True) + b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=raw)

    transport(monkeypatch, handler)

    class Store:
        def __init__(self): self.usage, self.events = [], []
        def record_usage(self, value): self.usage.append(value)
        def event(self, *args, **kwargs): self.events.append((args, kwargs))

    store = Store()
    pool = ProviderPool(Core(), store)
    pool.ollama = CompatibleProvider({'url': 'http://127.0.0.1:8080/v1'})
    cancel = threading.Event()
    consumer = threading.Thread(target=lambda: list(pool.generate(REQUEST, cancel, {'id': 'cancelled'})))
    consumer.start()
    assert entered.wait(2) and pool.queue.active
    cancel.set()
    consumer.join(2)
    assert not consumer.is_alive() and not pool.queue.active
    result = list(pool.generate(REQUEST, threading.Event(), {'id': 'next'}))
    assert result[-1]['done'] is True
    assert len(store.usage) == 2 and len({item['id'] for item in store.usage}) == 2
    assert store.usage[0]['cancelled'] is True and store.usage[1]['cancelled'] is False
    assert store.usage[1]['input_tokens'] == 7 and store.usage[1]['output_tokens'] == 2
    assert store.usage[1]['estimated'] is False and len(calls) == 2


@pytest.mark.parametrize('engine', ['ollama', 'compatible'])
def test_oversized_unterminated_line_is_rejected_and_closed(monkeypatch, engine):
    body = TimedBody([(0, b'x' * 32768), (0, b'x' * 32769)], stall=True)
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    with pytest.raises(ValueError, match='oversized response packet'):
        list(stream(engine))
    assert body.closed.is_set()


def test_compatible_final_counts_and_decode_timing_remain_exact(monkeypatch):
    content = packet('compatible', content='Done', done=True) + (
        'data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 3,
         'prompt_tokens_details': {'cached_tokens': 5}}, 'timings': {'predicted_ms': 150, 'predicted_n': 3}})
        + '\n\ndata: [DONE]\n\n').encode()
    transport(monkeypatch, lambda request: httpx.Response(200, content=content))
    result = list(stream('compatible'))
    assert result[-1]['usage']['completion_tokens'] == 3
    assert result[-1]['usage']['prompt_tokens_details']['cached_tokens'] == 5
    assert result[-1]['eval_duration'] == 150_000_000


@pytest.mark.parametrize('ending', [b'\r', b'\r\n'])
def test_sse_line_endings_and_split_unicode_are_preserved(monkeypatch, ending):
    raw=packet('compatible', content='漢字 🌍', done=True).replace(b'\n', ending)
    unicode_start=raw.index('漢'.encode('utf-8'))
    body=TimedBody([(0,raw[:unicode_start+1]), (0,raw[unicode_start+1:-1]), (0,raw[-1:])])
    transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    result=list(stream('compatible'))
    assert result[0]['message']['content']=='漢字 🌍' and result[-1]['done'] is True
    assert body.closed.is_set()


@pytest.mark.parametrize('message', [[], {'content': {}}, {'tool_calls': [{'function': {'name': 'bad/name'}}]}])
def test_compatible_packet_validation_remains_bounded(monkeypatch, message):
    raw = ('data: ' + json.dumps({'choices': [{'delta': message}]}) + '\n\n').encode()
    transport(monkeypatch, lambda request: httpx.Response(200, content=raw))
    with pytest.raises(ValueError, match='invalid|Invalid'):
        list(stream('compatible'))
