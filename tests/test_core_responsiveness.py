"""Regression tests for startup work, bounded context and cancellable HTTP reads."""
import asyncio
import copy
import json
import threading
import time

import httpx
import pytest

from core import Core, MAX_HISTORY_CHARACTERS, MAX_HISTORY_MESSAGES


CHAT = {'model': 'qwen3.5:9b', 'messages': [{'role': 'user', 'content': 'Hello'}]}


class AsyncBody(httpx.AsyncByteStream):
    def __init__(self, chunks, block=False):
        self.chunks, self.block = chunks, block
        self.closed = threading.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.block:
            await asyncio.sleep(60)

    async def aclose(self):
        self.closed.set()


def install_async_transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient',
                        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_startup_does_not_open_network_or_load_model(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail('Constructing Core must not open a network client')
    monkeypatch.setattr(httpx, 'Client', unexpected)
    monkeypatch.setattr(httpx, 'AsyncClient', unexpected)
    Core()


def test_model_list_has_short_timeout_and_actionable_error(monkeypatch):
    client = httpx.Client
    settings = []

    def handler(request):
        assert request.url.path == '/api/tags'
        raise httpx.ReadTimeout('stalled', request=request)

    def create_client(**kwargs):
        settings.append(kwargs)
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'Client', create_client)
    with pytest.raises(ValueError, match='too long'):
        Core().dispatch('models', {})
    assert settings[0]['timeout'].connect <= 2
    assert settings[0]['timeout'].read <= 3
    assert settings[0]['trust_env'] is False


def test_short_conversations_use_smaller_context():
    assert Core._chat_payload(CHAT)['options']['num_ctx'] == 4096
    assert Core._chat_payload({**CHAT, 'tokens': 4096})['options']['num_ctx'] == 8192


def test_thinking_is_explicit_and_does_not_change_other_models_default():
    assert Core._chat_payload(CHAT)['think'] is False
    assert Core._chat_payload({**CHAT, 'thinking': False})['think'] is False
    assert Core._chat_payload({**CHAT, 'thinking': True})['think'] is True
    other_model = {**CHAT, 'model': 'other-model'}
    assert 'think' not in Core._chat_payload(other_model)
    assert Core._chat_payload({**other_model, 'thinking': True})['think'] is True
    for invalid in ('yes', 1, None):
        with pytest.raises(ValueError, match='thinking setting'):
            Core._chat_payload({**CHAT, 'thinking': invalid})


def test_history_and_images_are_bounded_without_mutating_saved_messages():
    messages = [
        {'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i) + 'x' * 1100,
         'images': [str(i)] if i % 2 == 0 else []}
        for i in range(100)
    ]
    original = copy.deepcopy(messages)
    payload = Core._chat_payload({'model': 'test', 'messages': messages})
    history = payload['messages'][1:]
    assert len(history) <= MAX_HISTORY_MESSAGES
    assert sum(len(message['content']) for message in history) <= MAX_HISTORY_CHARACTERS
    assert [message['images'] for message in history if message.get('images')] == [['98']]
    assert history[-1]['content'] == messages[-1]['content']
    assert messages == original


def test_oversized_current_message_is_rejected_instead_of_silently_cutting_code():
    with pytest.raises(ValueError, match='24,000'):
        Core._chat_payload({'model': 'test', 'messages': [{'role': 'user', 'content': 'x' * 24001}]})


def test_research_with_selected_model_does_not_trigger_inference(monkeypatch):
    import ddgs
    seen = []

    class Search:
        def __init__(self, timeout):
            assert timeout <= 8

        def text(self, query, **kwargs):
            seen.append(query)
            return [{'title': 'Docs', 'href': 'https://example.com/docs', 'body': 'Documentation'}]

    def unexpected(*args):
        pytest.fail('Ordinary research must not load a model')

    core = Core()
    monkeypatch.setattr(core, 'request', unexpected)
    monkeypatch.setattr(ddgs, 'DDGS', Search)
    result = core.dispatch('research', {'query': '  official docs  ', 'model': 'qwen3.5:9b'})
    assert seen == ['official docs']
    assert result['queries'] == seen
    assert result['sources'][0]['url'] == 'https://example.com/docs'


def test_stream_returns_incremental_packets_and_closes_connection(monkeypatch):
    packets = [
        {'message': {'content': 'Hello ', 'thinking': ''}, 'done': False},
        {'message': {'content': 'there'}, 'done': False},
        {'message': {'content': ''}, 'done': True, 'eval_count': 2},
    ]
    content = ('\n'.join(json.dumps(packet) for packet in packets) + '\n').encode()
    body = AsyncBody([content[:12], content[12:55], content[55:]])

    async def handler(request):
        payload = json.loads(request.content)
        assert payload['stream'] is True
        assert payload['think'] is False
        assert payload['options']['num_ctx'] == 4096
        return httpx.Response(200, stream=body)

    install_async_transport(monkeypatch, handler)
    assert list(Core().stream_chat(CHAT, threading.Event())) == packets
    assert body.closed.is_set()


@pytest.mark.parametrize(('body', 'message'), [
    (b'{"error":"model is missing"}\n', 'model is missing'),
    (b'not-json\n', 'invalid response stream'),
    (b'{"message":{"content":"unfinished"},"done":false}\n', 'before completing'),
    (b'{"message":{"content":{}}}\n', 'invalid message'),
])
def test_stream_failures_are_visible(monkeypatch, body, message):
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match=message):
        list(Core().stream_chat(CHAT))


def test_stream_reports_http_error(monkeypatch):
    install_async_transport(monkeypatch, lambda request: httpx.Response(404, json={'error': 'model missing'}))
    with pytest.raises(ValueError, match='model missing'):
        list(Core().stream_chat(CHAT))


def test_cancel_before_first_token_interrupts_stalled_network_and_releases_lock(monkeypatch):
    entered = threading.Event()
    body = AsyncBody([], block=True)

    async def handler(request):
        entered.set()
        return httpx.Response(200, stream=body)

    install_async_transport(monkeypatch, handler)
    core, cancel, result = Core(), threading.Event(), []
    consumer = threading.Thread(target=lambda: result.extend(core.stream_chat(CHAT, cancel)))
    consumer.start()
    assert entered.wait(2)
    started = time.monotonic()
    cancel.set()
    consumer.join(2)
    assert not consumer.is_alive()
    assert time.monotonic() - started < 1
    assert result == []
    assert body.closed.is_set()
    assert not core._stream_lock.locked()


def test_cancel_interrupts_waiting_for_response_headers(monkeypatch):
    entered, exited = threading.Event(), threading.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            exited.set()

    install_async_transport(monkeypatch, handler)
    core, cancel = Core(), threading.Event()
    consumer = threading.Thread(target=lambda: list(core.stream_chat(CHAT, cancel)))
    consumer.start()
    assert entered.wait(2)
    cancel.set()
    consumer.join(2)
    assert not consumer.is_alive()
    assert exited.is_set()
    assert not core._stream_lock.locked()


def test_closing_generator_cancels_stream_and_prevents_parallel_generations(monkeypatch):
    body = AsyncBody([b'{"message":{"content":"Hi"},"done":false}\n'], block=True)
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    core = Core()
    stream = core.stream_chat(CHAT)
    assert next(stream)['message']['content'] == 'Hi'
    try:
        with pytest.raises(ValueError, match='already running'):
            list(core.stream_chat(CHAT))
    finally:
        stream.close()
    assert body.closed.is_set()
    assert not core._stream_lock.locked()


def test_pre_cancelled_stream_does_not_make_request(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail('A pre-cancelled request must not contact Ollama')
    monkeypatch.setattr(httpx, 'AsyncClient', unexpected)
    cancel = threading.Event()
    cancel.set()
    assert list(Core().stream_chat(CHAT, cancel)) == []


def test_total_deadline_closes_stalled_request(monkeypatch):
    import core as core_module
    monkeypatch.setattr(core_module, 'STREAM_DEADLINE_SECONDS', 0.05)
    body = AsyncBody([], block=True)
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, stream=body))
    core = Core()
    with pytest.raises(ValueError, match='too long'):
        list(core.stream_chat(CHAT))
    assert body.closed.is_set()
    assert not core._stream_lock.locked()
