"""Performance checks use disposable state and never load a user's models."""
from copy import deepcopy
from types import SimpleNamespace
import asyncio
import threading
import time

import httpx
import pytest

import forge_performance as performance
from forge_inference import ProviderPool
from forge_store import ForgeStore


GIB = 1024 ** 3
VALID_CODE = 'def sum_even(values):\n    return sum(x for x in values if x % 2 == 0)'


def telemetry():
    return {'cpu': {'physical_cores': 8, 'logical_cores': 16},
            'ram': {'total_bytes': 32 * GIB, 'used_bytes': 12 * GIB},
            'gpus': [{'total_bytes': 8 * GIB, 'used_bytes': 2 * GIB}]}


class CoreFixture:
    base = 'http://127.0.0.1:11434/api'

    def __init__(self, packets=None):
        self.packets = packets or []
        self.streams = 0
        self.after_packet = None
        self.model_values = [dict(name='fixture', size=2 * GIB)]
        self.limit = 32768

    def dispatch(self, action, data):
        if action == 'models': return {'models': self.model_values}
        if action == 'show':
            return {'capabilities': ['tools', 'vision'], 'model_info': {'fixture.context_length': self.limit}}
        raise AssertionError(action)

    def request(self, method, path):
        assert method == 'GET' and path == '/ps'
        return {'models': [dict(name='fixture', size=2 * GIB, size_vram=GIB, context_length=32768,
                                expires_at='2026-01-01T00:00:00Z')]}

    def _stream_payload(self, payload, cancel):
        self.streams += 1
        for packet in self.packets:
            yield deepcopy(packet)
            if self.after_packet: self.after_packet(cancel)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    store = ForgeStore(tmp_path)
    store.update_settings({'model': 'fixture', 'context': 32768})
    core = CoreFixture()
    pool = ProviderPool(core, store)
    service = SimpleNamespace(store=store, providers=pool, jobs=SimpleNamespace(lock=threading.RLock(), jobs={}))
    monkeypatch.setattr(performance, 'hardware', telemetry)
    return performance.PerformanceManager(service)


def packets(content=VALID_CODE, **final):
    return [dict(message={'content': content}),
            dict(done=True, message={}, prompt_eval_count=40, eval_count=20, eval_duration=2_000_000_000, **final)]


def test_hardware_gpu_mib_and_ram_bytes_stay_consistent(monkeypatch):
    monkeypatch.setattr(performance, 'memory_telemetry', lambda: {'gpus': [
        dict(name='Disposable GPU', total_mb=8192, used_mb=2048, utilization=12)]})
    monkeypatch.setattr(performance.psutil, 'virtual_memory', lambda: SimpleNamespace(
        total=32 * GIB, available=20 * GIB, percent=37.5))
    monkeypatch.setattr(performance.psutil, 'cpu_count', lambda logical=True: 16 if logical else 8)
    monkeypatch.setattr(performance.psutil, 'cpu_percent', lambda: 5)
    actual = performance.hardware()
    assert actual['ram'] == dict(total_bytes=32 * GIB, available_bytes=20 * GIB, used_bytes=12 * GIB, percent_used=37.5)
    assert actual['gpus'][0] == dict(name='Disposable GPU', total_bytes=8 * GIB, used_bytes=2 * GIB,
                                    free_bytes=6 * GIB, utilization_percent=12)
    assert actual['cpu']['physical_cores'] == 8 and actual['cpu']['logical_cores'] == 16


def test_recommendations_preserve_vision_tools_and_require_acceptance(manager):
    manager.service.providers.core.model_values = [
        dict(name='vision-small', size=2 * GIB), dict(name='vision-large', size=4 * GIB),
        dict(name='too-large', size=7 * GIB)]
    manager.service.providers.core.limit = 16384
    before = manager.store.get_settings()
    values = manager.recommendations(telemetry(), before)
    assert len(values) == 3 and all(v['requires_acceptance'] for v in values)
    assert values[0]['settings']['model'] == 'vision-small'
    assert values[2]['settings']['model'] == 'vision-large'
    assert all(v['settings']['context'] <= 16384 for v in values)
    assert all(v['settings']['num_thread'] == 8 for v in values)
    assert manager.store.get_settings() == before


def test_unusable_below_minimum_model_context_is_not_recommended(manager):
    manager.service.providers.core.limit = 1024
    assert manager.recommendations(telemetry(), manager.store.get_settings()) == []


def test_profile_apply_refuses_active_writer_and_changes_only_after_acceptance(manager):
    before = manager.store.get_settings()
    manager.service.jobs.jobs['active'] = {'cancel': threading.Event()}
    with pytest.raises(ValueError, match='Pause'):
        manager.apply('speed')
    assert manager.store.get_settings() == before
    manager.service.jobs.jobs.clear()
    actual = manager.apply('speed')['settings']
    assert actual['performance'] == 'speed' and actual['context'] == 16384


@pytest.mark.parametrize('content', [
    'Here is a Python function.',
    'def sum_even(values):\n    return sum(x for x in values if x % 2 != 0)',
    "def sum_even(values):\n    return __import__('os').system('untrusted')",
])
def test_invalid_or_unsafe_coding_probe_cannot_pass(manager, content):
    manager.service.providers.core.packets = packets(content)
    actual = manager._probe(manager.store.get_settings(), 'first_request', threading.Event())
    assert actual['status'] == 'failed'


def test_probe_reported_usage_persists_once_and_speed_uses_decode_duration(manager):
    manager.service.providers.core.packets = packets()
    actual = manager._probe(manager.store.get_settings(), 'first_request', threading.Event())
    assert actual['status'] == 'passed' and actual['tps'] == 10 and not actual['estimated']
    totals = manager.store.usage()['totals']
    assert totals['requests'] == 1 and totals['input_tokens'] == 40 and totals['output_tokens'] == 20
    assert totals['estimated_requests'] == 0
    assert manager.store.usage()['average_tps'] == 10


@pytest.mark.parametrize('reason', ['length', 'max_tokens'])
def test_truncated_tool_probe_cannot_pass(manager, reason):
    call = {'function': {'name': 'forge_validation_echo', 'arguments': {'marker': 'FORGE_TOOL_OK'}}}
    manager.service.providers.core.packets = [dict(message={'tool_calls': [call]}),
        dict(done=True, done_reason=reason, message={}, eval_count=4, prompt_eval_count=40)]
    assert manager._probe(manager.store.get_settings(), 'tools', threading.Event())['status'] == 'failed'


def test_partial_tool_stream_cannot_pass(manager):
    call = {'function': {'name': 'forge_validation_echo', 'arguments': {'marker': 'FORGE_TOOL_OK'}}}
    manager.service.providers.core.packets = [dict(message={'tool_calls': [call]})]
    assert manager._probe(manager.store.get_settings(), 'tools', threading.Event())['status'] == 'failed'
    assert manager.store.usage()['totals']['estimated_requests'] == 1


def test_missing_counters_and_zero_decode_do_not_invent_speed(manager):
    manager.service.providers.core.packets = [dict(message={'content': VALID_CODE}), dict(done=True, message={})]
    actual = manager._probe(manager.store.get_settings(), 'warm_request', threading.Event())
    assert actual['status'] == 'passed' and actual['tps'] is None and actual['estimated']
    totals = manager.store.usage()['totals']
    assert totals['estimated_requests'] == 1 and totals['output_tokens'] > 0
    manager.service.providers.core.packets = [dict(message={'content': VALID_CODE}),
        dict(done=True, message={}, prompt_eval_count=40, eval_count=20, eval_duration=0)]
    assert manager._probe(manager.store.get_settings(), 'warm_request', threading.Event())['tps'] is None


def test_cancelled_probe_records_one_cancelled_request_and_releases_gpu(manager):
    manager.service.providers.core.packets = packets()
    manager.service.providers.core.after_packet = lambda cancel: cancel.set()
    with pytest.raises(ValueError, match='cancelled'):
        manager._probe(manager.store.get_settings(), 'first_request', threading.Event())
    assert not manager.service.providers.queue.active
    with manager.store._connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM usage')]
    assert len(rows) == 1 and rows[0]['cancelled'] == 1 and rows[0]['purpose'] == 'benchmark'


def test_residency_uses_engine_report_and_keeps_external_engines_capability_gated(manager):
    actual = manager.engine('ollama')
    assert actual['residency_supported'] and actual['resident_models'][0]['size_bytes'] == 2 * GIB
    assert actual['resident_models'][0]['size_vram_bytes'] == GIB
    external = SimpleNamespace(models=lambda: [], capabilities=lambda model: {'model_info': {'fixture.context_length': 32768}})
    manager.service.providers.provider = lambda identifier: external
    assert manager.engine('external')['residency_supported'] is False
    with pytest.raises(ValueError, match='residency'):
        manager.start({}, 'warm')


def test_restart_marks_unfinished_benchmark_interrupted(manager):
    manager.store.save_entity('benchmarks', {'id': 'unfinished', 'status': 'running'})
    performance.PerformanceManager(manager.service)
    assert manager.store.entity('benchmarks', 'unfinished')['status'] == 'interrupted'


def test_benchmark_rejects_context_over_model_limit_before_queueing(manager):
    manager.service.providers.core.limit = 8192
    with pytest.raises(ValueError, match='advertises'):
        manager.start({'context': 32768})
    assert manager.jobs == {} and manager.store.entities('benchmarks') == []
    assert manager.service.providers.core.streams == 0


def run_residency(manager, monkeypatch, final, cancel_after=False):
    cancel = threading.Event()
    def request(url, payload, event):
        assert event is cancel and payload['model'] == 'fixture'
        if cancel_after: cancel.set()
        return final
    monkeypatch.setattr(manager, '_resident_request', request)
    job = manager.store.save_entity('benchmarks', {'id': 'residency-check', 'status': 'queued', 'operation': 'warm'})
    manager.jobs[job['id']] = {'cancel': cancel}
    manager._worker(job, manager.store.get_settings(), {}, cancel)
    return manager.store.entity('benchmarks', job['id'])


def test_residency_missing_counts_are_marked_estimated(manager, monkeypatch):
    run_residency(manager, monkeypatch, {'done': True})
    totals = manager.store.usage()['totals']
    assert totals['requests'] == 1 and totals['estimated_requests'] == 1


def test_residency_cancel_during_request_does_not_claim_success(manager, monkeypatch):
    job = run_residency(manager, monkeypatch, {'done': True}, cancel_after=True)
    assert job['status'] == 'cancelled'
    with manager.store._connection() as db:
        usage = dict(db.execute('SELECT * FROM usage').fetchone())
    assert usage['cancelled'] == 1


def test_residency_cancel_interrupts_stalling_http_and_releases_gpu(manager, monkeypatch):
    started, interrupted = threading.Event(), threading.Event()
    async def stall(request):
        started.set()
        try:
            await asyncio.sleep(60)
            return httpx.Response(200, json={'done': True})
        finally:
            interrupted.set()
    real = httpx.AsyncClient
    monkeypatch.setattr(performance.httpx, 'AsyncClient', lambda **kwargs: real(
        transport=httpx.MockTransport(stall), **kwargs))
    cancel = threading.Event()
    job = manager.store.save_entity('benchmarks', {'id': 'stall', 'status': 'queued', 'operation': 'warm'})
    manager.jobs[job['id']] = {'cancel': cancel}
    worker = threading.Thread(target=manager._worker, args=(job, manager.store.get_settings(), {}, cancel))
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    cancel.set()
    worker.join(2)
    assert not worker.is_alive() and time.monotonic() - began < 1
    assert interrupted.is_set() and not manager.service.providers.queue.active
    assert manager.store.entity('benchmarks', 'stall')['status'] == 'cancelled'
    with manager.store._connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM usage')]
    assert len(rows) == 1 and rows[0]['cancelled'] == 1 and rows[0]['purpose'] == 'warm'


def test_goal_executes_twelve_file_calls_in_one_round_and_journals_completion(tmp_path):
    from forge_service import ForgeService
    service = ForgeService(data_dir=tmp_path / 'state')
    try:
        service.store.update_settings({'model': 'fixture', 'permission_profile': 'full_access'})
        service.jobs._launch = lambda run: None
        folder = tmp_path / 'disposable-project'
        folder.mkdir()
        project = service.create_project({'name': 'Tool batch fixture', 'path': str(folder)})
        goal = service.goal_create({'text': 'Create twelve fixture files', 'project_id': project['id'],
            'tasks': [{'text': 'Create all fixture files and verify contents', 'status': 'pending', 'evidence': []}]})
        completed = [{**task, 'status': 'completed', 'evidence': ['Twelve unique fixture files created.']}
                     for task in goal['tasks']]
        calls = [{'function': {'name': 'write_file', 'arguments': {
            'path': 'fixture-' + str(index) + '.txt', 'content': 'value-' + str(index)}}} for index in range(12)]
        calls.append({'function': {'name': 'goal_update', 'arguments': {
            'tasks': completed, 'checkpoint': 'All twelve files written.', 'next_action': 'Review fixture contents.'}}})
        class BatchCore(CoreFixture):
            def _stream_payload(self, payload, cancel):
                self.streams += 1
                if self.streams == 1:
                    yield {'done': True, 'message': {'tool_calls': calls}, 'eval_count': 100}
                else:
                    yield {'done': True, 'message': {'content': 'Fixture files complete.'}, 'eval_count': 4}
        core = BatchCore()
        service.providers = ProviderPool(core, service.store)
        run = service.goal_resume({'id': goal['id']})
        service.jobs._run(run['id'], {'cancel': threading.Event(), 'pause': False, 'approval': None})
        saved = service.store.run(run['id'])
        assert saved['status'] == 'completed' and saved['tools'] == 13 and saved['rounds'] == 2
        assert [(folder / ('fixture-' + str(i) + '.txt')).read_text() for i in range(12)] == ['value-' + str(i) for i in range(12)]
        with service.store._connection() as db:
            statuses = [row[0] for row in db.execute('SELECT status FROM invocations WHERE run_id=?', (run['id'],))]
        assert statuses == ['completed'] * 13 and not service.store.unknown_actions(run['id'])
        final_goal = service.store.goal(goal['id'])
        assert final_goal['status'] == 'completed' and '[x]' in final_goal['markdown']
        assert final_goal['tasks'][0]['evidence'] == ['Twelve unique fixture files created.']
        assert service.store.usage()['totals']['requests'] == 2
    finally:
        service.shutdown()
