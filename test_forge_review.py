"""Independent continuity/concurrency regression scenarios for the coordinator."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from forge_inference import CompatibleProvider, InferenceQueue
from forge_service import ForgeService


@pytest.fixture
def coordinator(tmp_path):
    service = ForgeService(data_dir=tmp_path / 'forge')
    service.store.update_settings({'model': 'test-model', 'permission_profile': 'full_access'})
    service.jobs._launch = lambda run: None
    return service


def queued(service, text='Current request', **kwargs):
    result = service.jobs.start({'text': text, **kwargs})
    return service.store.run(result['id'])


def test_current_request_preserves_existing_chat_chronology(coordinator):
    chat = coordinator.store.create_chat(None, model='test-model')
    coordinator.store.add_message(chat['id'], 'user', 'Old question')
    coordinator.store.add_message(chat['id'], 'assistant', 'Old answer')
    run = queued(coordinator, chat_id=chat['id'])
    context = coordinator.jobs._context(run, [])
    content = [m['content'] for m in context]
    assert content.index('Old question') < content.index('Old answer') < content.index('Current request')
    assert content.count('Current request') == 1


def test_shared_budget_includes_grandchildren(coordinator):
    coordinator.store.update_settings({'goal_limits': {'minutes': 60, 'tokens': 1000, 'rounds': 20, 'tools': 2}})
    root = queued(coordinator, 'Root')
    child = queued(coordinator, 'Child', parent_id=root['id'])
    grandchild = queued(coordinator, 'Grandchild', parent_id=child['id'])
    coordinator.store.update_run(grandchild['id'], tools=2)
    assert 'tools' in coordinator.jobs._limits(coordinator.store.run(root['id']), time.monotonic())


def test_resume_extends_shared_allowance_after_child_usage(coordinator):
    coordinator.store.update_settings({'goal_limits': {'minutes': 60, 'tokens': 1000, 'rounds': 20, 'tools': 10}})
    root = queued(coordinator, 'Root')
    child = queued(coordinator, 'Child', parent_id=root['id'])
    coordinator.store.update_run(root['id'], tools=3, status='paused')
    coordinator.store.update_run(child['id'], tools=7, status='completed')
    coordinator.jobs.resume(root['id'])
    assert coordinator.jobs._limits(coordinator.store.run(root['id']), time.monotonic()) is None


def test_ambiguous_side_effect_pauses_and_cannot_replay(coordinator, tmp_path):
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = coordinator.create_project({'path': str(project_dir), 'name': 'Disposable project'})
    class Provider:
        def capabilities(self, model):
            return {'capabilities': ['tools'], 'model_info': {'test.context_length': 262144}}
    calls = []
    class Pool:
        def provider(self, identifier): return Provider()
        def generate(self, data, cancel, run, purpose, background):
            if not calls:
                yield {'done': True, 'message': {'content': '', 'tool_calls': [{'function': {'name': 'write_file',
                    'arguments': {'path': 'effect.txt', 'content': 'once'}}}]}, 'eval_count': 1}
            else:
                yield {'done': True, 'message': {'content': 'Finished'}, 'eval_count': 1}
    coordinator.providers = Pool()
    run = queued(coordinator, project_id=project['id'])
    def uncertain_effect(*args, **kwargs):
        calls.append('effect')
        (project_dir / 'effect.txt').write_text('once', encoding='utf-8')
        raise TimeoutError('Connection ended after the write; outcome requires inspection')
    coordinator.jobs.registry.execute = uncertain_effect
    coordinator.jobs._run(run['id'], {'cancel': threading.Event(), 'pause': False, 'approval': None})
    assert calls == ['effect']
    assert coordinator.store.run(run['id'])['status'] == 'paused'
    assert coordinator.store.unknown_actions(run['id'])
    with pytest.raises(ValueError, match='unknown|Unknown'):
        coordinator.jobs.resume(run['id'])


def test_recovery_blocks_unresolved_side_effect(coordinator):
    run = queued(coordinator)
    identifier = run['id'] + ':1:0'
    coordinator.store.invocation(identifier, run['id'], 'write_file', {'path': 'effect.txt', 'content': 'once'})
    coordinator.store.invocation_state(identifier, 'running')
    coordinator.store.recover()
    assert coordinator.store.run(run['id'])['status'] == 'interrupted'
    with pytest.raises(ValueError, match='unknown'):
        coordinator.jobs.resume(run['id'])


def test_compatible_tool_result_uses_assistant_call_id(monkeypatch):
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    provider = CompatibleProvider({'url': 'http://127.0.0.1:1234/v1'})
    messages = [{'role': 'user', 'content': 'Read file'}, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_123', 'function': {'name': 'read_file', 'arguments': {'path': 'test.txt'}}}]},
        {'role': 'tool', 'tool_name': 'read_file', 'tool_call_id': 'call_123', 'content': 'contents'}]
    list(provider.generate({'model': 'test-model', 'messages': messages, 'context': 32768, 'tokens': 1024}, threading.Event()))
    result = next(m for m in captured['messages'] if m['role'] == 'tool')
    assert result['tool_call_id'] == 'call_123'
    assert 'tool_name' not in result


def test_single_gpu_prioritizes_foreground_and_cancels_queued_request():
    queue = InferenceQueue()
    active, release = threading.Event(), threading.Event()
    order = []
    def first():
        with queue.lease(threading.Event()):
            active.set()
            release.wait(3)
    def worker(name, background):
        with queue.lease(threading.Event(), background):
            order.append(name)
    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert active.wait(1)
    background = threading.Thread(target=worker, args=('background', True))
    foreground = threading.Thread(target=worker, args=('foreground', False))
    background.start()
    foreground.start()
    deadline = time.monotonic() + 2
    while len(queue.pending) < 2 and time.monotonic() < deadline:
        time.sleep(.005)
    release.set()
    for thread in (first_thread, background, foreground): thread.join(3)
    assert order == ['foreground', 'background']
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match='cancelled'):
        with queue.lease(cancelled): pass
    assert not queue.active and not queue.pending
