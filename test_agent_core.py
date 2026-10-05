"""Ollama protocol, bounded compaction and cancellation regression checks."""
import copy
import json
import threading

import httpx
import pytest

from core import AGENT_SYSTEM_PROMPT, COMPACTION_SYSTEM_PROMPT, Core
from test_core_responsiveness import AsyncBody, install_async_transport


TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_file', 'description': 'Read a project file.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
    },
}
CALL = {'function': {'index': 0, 'name': 'read_file', 'arguments': {'path': 'src/app.py'}}}
MESSAGES = [
    {'role': 'system', 'content': 'Project: C:/Projects/Example'},
    {'role': 'user', 'content': 'Check the app.'},
    {'role': 'assistant', 'content': '', 'thinking': 'Inspect the app first.', 'tool_calls': [CALL]},
    {'role': 'tool', 'tool_name': 'read_file', 'content': 'print("hello")'},
]
AGENT = {'model': 'qwen3.5:9b', 'messages': MESSAGES, 'tools': [TOOL]}


def test_agent_retains_complete_tool_history_and_detaches_mutable_values():
    original = copy.deepcopy(AGENT)
    payload = Core._agent_payload(AGENT)
    assert payload['messages'] == [{'role': 'system', 'content': AGENT_SYSTEM_PROMPT}] + MESSAGES
    assert payload['tools'] == [TOOL]
    assert payload['options'] == {'temperature': 0.3, 'num_predict': 2048, 'num_ctx': 8192}
    assert payload['think'] is False
    assert payload['truncate'] is False and payload['shift'] is False
    payload['messages'][3]['tool_calls'][0]['function']['arguments']['path'] = 'changed'
    payload['tools'][0]['function']['parameters']['required'].append('other')
    assert AGENT == original


def test_agent_does_not_silently_remove_older_messages_or_tool_sequences():
    messages = [{'role': 'user', 'content': str(index) * 100} for index in range(30)] + MESSAGES[1:]
    payload = Core._agent_payload({**AGENT, 'messages': messages})
    assert payload['messages'][1:] == messages
    with pytest.raises(ValueError, match='Compact'):
        Core._agent_payload({**AGENT, 'messages': [{'role': 'user', 'content': 'x' * 60001}]})


@pytest.mark.parametrize('change', [
    {'model': ''}, {'context': True}, {'context': 1000000}, {'tokens': True},
    {'thinking': 'yes'}, {'temperature': float('nan')}, {'messages': []},
    {'tools': [TOOL, TOOL]}, {'tools': [{'type': 'function', 'function': {'name': 'bad'}}]},
    {'messages': [{'role': 'tool', 'content': 'missing name'}]},
    {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {'name': 'read_file', 'arguments': 'invalid'}}]}]},
    {'messages': [{'role': 'user', 'content': 'x', 'tool_calls': [CALL]}]},
    {'messages': [{'role': 'user', 'content': 'x', 'thinking': 'invalid'}]},
])
def test_agent_rejects_invalid_protocol_fields(change):
    with pytest.raises(ValueError):
        Core._agent_payload({**AGENT, **change})


def test_stream_agent_returns_unexecuted_tool_chunks_and_uses_one_lock(monkeypatch):
    packets = [
        {'message': {'content': '', 'thinking': 'Checking.'}, 'done': False},
        {'message': {'tool_calls': [{'function': {'index': 0, 'name': 'read_file', 'arguments': '{"path":'}}]}, 'done': False},
        {'message': {'tool_calls': [{'function': {'index': 0, 'arguments': '"src/app.py"}'}}]}, 'done': False},
        {'message': {}, 'done': True},
    ]

    def handler(request):
        payload = json.loads(request.content)
        assert payload['tools'] == [TOOL]
        assert payload['messages'][1:] == MESSAGES
        assert payload['truncate'] is False and payload['shift'] is False
        return httpx.Response(200, content=('\n'.join(json.dumps(packet) for packet in packets) + '\n').encode())

    install_async_transport(monkeypatch, handler)
    core = Core()
    assert list(core.stream_agent(AGENT)) == packets
    assert not core._stream_lock.locked()


@pytest.mark.parametrize('calls', [
    {}, [None], [{'function': {'name': 'read_file', 'arguments': []}}],
    [{'function': {'name': 'read_file', 'index': -1, 'arguments': {}}}],
    [CALL] * 33,
])
def test_malformed_upstream_tool_calls_are_rejected(monkeypatch, calls):
    body = json.dumps({'message': {'tool_calls': calls}, 'done': True}).encode() + b'\n'
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match='[Tt]ool'):
        list(Core().stream_agent(AGENT))


def test_compaction_preserves_originals_and_retains_task_evidence(monkeypatch):
    original = copy.deepcopy(MESSAGES)
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, content=b'{"message":{"content":"Checked src/app.py. "},"done":false}\n'
                                            b'{"message":{"content":"Next: run tests."},"done":true}\n')

    install_async_transport(monkeypatch, handler)
    result = Core().compact_messages('qwen3.5:9b', 'Original task: check app.', MESSAGES)
    assert result == 'Checked src/app.py. Next: run tests.'
    assert MESSAGES == original
    payload = captured[0]
    assert 'tools' not in payload and payload['think'] is False
    assert payload['truncate'] is False and payload['shift'] is False
    assert payload['options']['num_ctx'] == 16384
    assert payload['messages'][0]['content'] == COMPACTION_SYSTEM_PROMPT
    transcript = json.loads(payload['messages'][1]['content'])
    assert transcript['prior_summary'] == 'Original task: check app.'
    assert transcript['transcript'][-1] == MESSAGES[-1]
    assert transcript['transcript'][2]['tool_calls'] == [CALL]
    assert 'thinking' not in transcript['transcript'][2]


@pytest.mark.parametrize(('response', 'error'), [
    ({'message': {'content': ''}, 'done': True}, 'no complete summary'),
    ({'message': {'content': 'partial'}, 'done': True, 'done_reason': 'length'}, 'response space'),
    ({'message': {'content': 'x' * 6001}, 'done': True}, '6,000'),
])
def test_invalid_compaction_output_never_commits_partial_memory(monkeypatch, response, error):
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, content=json.dumps(response).encode() + b'\n'))
    with pytest.raises(ValueError, match=error):
        Core().compact_messages('qwen3:8b', '', MESSAGES)


def test_compaction_rejects_oversized_input_before_network(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail('Oversized compaction must not contact Ollama')

    monkeypatch.setattr(httpx, 'AsyncClient', unexpected)
    with pytest.raises(ValueError, match='Compaction input is too large'):
        Core().compact_messages('test', '', [{'role': 'user', 'content': 'x' * 40000}])


def test_compaction_cancel_interrupts_stalled_request_and_preserves_input(monkeypatch):
    entered = threading.Event()
    body = AsyncBody([], block=True)

    async def handler(request):
        entered.set()
        return httpx.Response(200, stream=body)

    install_async_transport(monkeypatch, handler)
    core, cancel, errors = Core(), threading.Event(), []

    def compact():
        try:
            core.compact_messages('qwen3.5:9b', '', MESSAGES, cancel)
        except ValueError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=compact)
    worker.start()
    assert entered.wait(2)
    cancel.set()
    worker.join(2)
    assert not worker.is_alive()
    assert errors == ['Compaction cancelled.']
    assert body.closed.is_set()
    assert not core._stream_lock.locked()


def test_tool_arguments_count_towards_stream_size_limit(monkeypatch):
    import core as core_module
    monkeypatch.setattr(core_module, 'MAX_STREAM_CHARACTERS', 20)
    packet = {'message': {'tool_calls': [CALL]}, 'done': True}
    install_async_transport(monkeypatch, lambda request: httpx.Response(200, content=json.dumps(packet).encode() + b'\n'))
    with pytest.raises(ValueError, match='too large'):
        list(Core().stream_agent(AGENT))


@pytest.mark.parametrize('context', [2048, 4096, 6144, 12288, 32768, 65536, 131072, 260096, 262144])
def test_context_slider_endpoints_and_intermediate_steps_reach_ollama(context):
    payload = Core._agent_payload({**AGENT, 'context': context, 'tokens': 8192})
    assert payload['options']['num_ctx'] == context
    assert payload['options']['num_predict'] == min(8192, context // 4)
    assert payload['truncate'] is False and payload['shift'] is False


@pytest.mark.parametrize('context', [0, 1024, 2049, 5000, 264192, True, False, 8192.0, '32768', None])
def test_context_slider_rejects_invalid_values(context):
    with pytest.raises(ValueError, match='Context must'):
        Core._agent_payload({**AGENT, 'context': context})


def test_larger_context_retains_history_beyond_former_sixty_thousand_character_limit():
    messages = [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': str(index) + 'x' * 5000}
                for index in range(20)]
    original = copy.deepcopy(messages)
    payload = Core._agent_payload({**AGENT, 'messages': messages, 'context': 131072})
    assert payload['messages'][1:] == original
    assert messages == original
    with pytest.raises(ValueError, match='60,000'):
        Core._agent_payload({**AGENT, 'messages': messages, 'context': 8192})


def test_tiny_context_rejects_tools_that_leave_no_room_for_the_user():
    tools = [copy.deepcopy(TOOL) for _ in range(6)]
    for index, tool in enumerate(tools):
        tool['function']['name'] += str(index)
        tool['function']['description'] = 'Detailed tool documentation. ' * 60
    with pytest.raises(ValueError, match='Increase Context'):
        Core._agent_payload({**AGENT, 'tools': tools, 'context': 2048})


def test_compaction_inference_stays_bounded_even_when_given_a_large_window(monkeypatch):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, content=b'{"message":{"content":"Summary complete."},"done":true}\n')

    install_async_transport(monkeypatch, handler)
    assert Core().compact_messages('test-model', '', MESSAGES, context=262144) == 'Summary complete.'
    assert captured[0]['options']['num_ctx'] == 16384


def test_compaction_can_respect_a_smaller_advertised_model_window(monkeypatch):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, content=b'{"message":{"content":"Summary complete."},"done":true}\n')

    install_async_transport(monkeypatch, handler)
    Core().compact_messages('test-model', '', MESSAGES, context=4096)
    assert captured[0]['options']['num_ctx'] == 4096
    assert captured[0]['options']['num_predict'] == 1024
