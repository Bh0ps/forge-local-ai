"""Editable presets and probes preserve saved configuration and admission."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import httpx
import pytest

from forge_provider_metadata import ProviderProbeRevoked
from forge_provider_presets import provider_presets
from forge_updates import UpdateManager
from test_forge_core import service
from test_forge_provider_probe import Vault, saved_engine, transport


def test_readonly_presets_are_fresh_bounded_suggestions_without_capability_grants(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(svc.providers, 'configurations', lambda: pytest.fail('Presets inspected providers'))
    transport(monkeypatch, lambda request: pytest.fail('Presets performed network I/O'))
    try:
        result = svc.dispatch('provider_presets')
        rows = result['presets']
        assert {row['id'] for row in rows} == {'ollama', 'lm-studio', 'llama-cpp',
            'jan-desktop', 'localai', 'vllm', 'sglang', 'koboldcpp', 'textgen'}
        for row in rows:
            assert set(row) == {'id', 'name', 'kind', 'url', 'setup_url', 'description',
                'hosting', 'metadata_strategy'}
            assert row['kind'] in ('ollama', 'openai-compatible')
            assert row['hosting'] in ('desktop', 'external', 'container')
            assert row['url'].startswith('http://127.0.0.1:')
            assert row['setup_url'].startswith('https://')
            assert all(value is None or len(value) < 512 for value in row.values())
        rows[0]['name'] = 'changed'; rows.clear()
        assert len(provider_presets()['presets']) == 9
        assert not svc.store.entities('providers')
    finally:
        svc.shutdown()


def test_saved_preset_survives_partial_edit_alongside_capabilities_and_resources(tmp_path):
    svc = service(tmp_path); svc.vault = Vault()
    previous = saved_engine(svc)
    previous = svc.store.save_entity('providers', {**previous, 'preset_id': 'lm-studio'})
    try:
        changed = svc.dispatch('provider_save', {'id': 'engine', 'name': 'Renamed'})
        for field in ('preset_id', 'capabilities', 'context_limit', 'resource_id',
                'independent_resource', 'credential_ref', 'enabled'):
            assert changed[field] == previous[field]
        assert not svc.vault.writes
        cleared = svc.dispatch('provider_save', {'id': 'engine', 'preset_id': None})
        assert cleared['preset_id'] is None and cleared['capabilities'] == previous['capabilities']
    finally:
        svc.shutdown()


@pytest.mark.parametrize('preset,kind', [('unknown', 'openai-compatible'),
    ('lm-studio', 'ollama'), ('ollama', 'openai-compatible'), ([], 'openai-compatible'),
    (True, 'openai-compatible')])
def test_invalid_preset_is_rejected_before_vault_or_requests(tmp_path, monkeypatch, preset, kind):
    svc = service(tmp_path); svc.vault = Vault()
    transport(monkeypatch, lambda request: pytest.fail('Invalid preset made request'))
    try:
        with pytest.raises(ValueError):
            svc.dispatch('provider_save', {'url': 'https://engine.example/v1',
                'kind': kind, 'preset_id': preset, 'api_key': 'synthetic-key'})
        with pytest.raises(ValueError):
            svc.dispatch('provider_test', {'draft': {'url': 'https://engine.example/v1',
                'kind': kind, 'preset_id': preset}, 'api_key': 'synthetic-key'})
        assert not svc.vault.writes and not svc.vault.reads and not svc.store.entities('providers')
    finally:
        svc.shutdown()


def test_legacy_protocol_edit_clears_incompatible_saved_preset_and_key(tmp_path):
    svc = service(tmp_path); svc.vault = Vault()
    svc.store.save_entity('providers', {**saved_engine(svc), 'preset_id': 'lm-studio'})
    try:
        changed = svc.dispatch('provider_save', {'id': 'engine', 'kind': 'ollama',
            'url': 'http://127.0.0.1:11434/api'})
        assert changed.get('preset_id') is None and changed.get('credential_ref') is None
        assert changed['capabilities'] == ['tools']
    finally:
        svc.shutdown()


def test_explicit_preset_is_editable_and_never_changes_protocol_or_endpoint(tmp_path):
    svc = service(tmp_path)
    try:
        saved = svc.dispatch('provider_save', {'name': 'My remote LM Studio',
            'kind': 'openai-compatible', 'preset_id': 'lm-studio', 'url': 'https://engine.example/custom/v1'})
        assert saved['url'] == 'https://engine.example/custom/v1'
        assert saved['preset_id'] == 'lm-studio'
        assert saved.get('capabilities') is None and saved.get('context_limit') is None
    finally:
        svc.shutdown()


def test_opt_in_metadata_does_not_save_advertised_capabilities(tmp_path, monkeypatch):
    svc = service(tmp_path); svc.vault = Vault()
    before = svc.store.save_entity('providers', {**saved_engine(svc), 'preset_id': 'lm-studio'})
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'fixture'}]})
        return httpx.Response(200, json={'models': [{'type': 'llm', 'key': 'fixture',
            'max_context_length': 65536, 'loaded_instances': [
                {'id': 'fixture', 'config': {'context_length': 4096}}],
            'capabilities': {'trained_for_tool_use': True, 'vision': True}}]})
    transport(monkeypatch, handle)
    try:
        result = svc.dispatch('provider_test', {'id': 'engine', 'metadata': True,
            'draft': {'kind': 'openai-compatible', 'url': before['url']}})
        assert result['ok'] and result['model_count'] == 1
        assert result['metadata']['state'] == 'available'
        assert len(requests) == 2 and all(request.headers['authorization'] ==
            'Bearer synthetic-engine-key' for request in requests)
        assert svc.vault.reads == ['saved-reference'] and not svc.vault.writes
        assert svc.store.entity('providers', 'engine') == before
    finally:
        svc.shutdown()


def test_optional_metadata_unavailable_keeps_successful_catalog_result(tmp_path, monkeypatch):
    svc = service(tmp_path)
    def handle(request):
        return (httpx.Response(200, json={'data': [{'id': 'fixture'}]})
            if request.url.path == '/v1/models' else httpx.Response(404, text='private-path synthetic-key'))
    transport(monkeypatch, handle)
    try:
        assert svc.dispatch('provider_test', {'metadata': True, 'draft': {
            'kind': 'openai-compatible', 'preset_id': 'lm-studio', 'url': 'https://engine.example/v1'}}) == {
            'ok': True, 'model_count': 1, 'metadata': {'state': 'unknown', 'source': 'unknown', 'models': []}}
    finally:
        svc.shutdown()


@pytest.mark.parametrize('change', [{'url': 'https://other.example/v1'},
    {'api_key': 'replacement-synthetic-key'}, {'preset_id': 'jan-desktop'}])
def test_pending_probe_allows_edit_but_rejects_stale_identity_and_blocks_update_snapshot(
        tmp_path, monkeypatch, change):
    svc = service(tmp_path); svc.vault = Vault()
    previous = svc.store.save_entity('providers', {**saved_engine(svc), 'preset_id': 'lm-studio'})
    entered, release = threading.Event(), threading.Event()
    requests = []
    def handle(request):
        requests.append(request); entered.set()
        assert release.wait(3), 'Offline probe fixture did not release HTTP'
        return httpx.Response(200, json={'data': [{'id': 'fixture'}]})
    transport(monkeypatch, handle)
    updater = object.__new__(UpdateManager)
    updater.service = svc; updater.inspection_required = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            probe = pool.submit(svc.dispatch, 'provider_test', {'id': 'engine', 'metadata': True,
                'draft': {'kind': 'openai-compatible', 'url': previous['url']}})
            assert entered.wait(2)
            with pytest.raises(ValueError, match='finish before updating'):
                updater._idle()
            edit = pool.submit(svc.dispatch, 'provider_save', {'id': 'engine', **change})
            changed = edit.result(timeout=2)
            assert all(changed.get(key) == value for key, value in change.items() if key != 'api_key')
            release.set()
            with pytest.raises(ProviderProbeRevoked, match='configuration changed'):
                probe.result(timeout=2)
        assert len(requests) == 1, 'A stale profile issued metadata HTTP'
        assert svc._native_io_pending == svc._performance_io_pending == 0
    finally:
        release.set(); svc.shutdown()


def test_cancel_during_catalog_prevents_metadata_and_releases_pending_lease(tmp_path, monkeypatch):
    svc = service(tmp_path); requests = []
    def handle(request):
        requests.append(request); svc.stop_event.set()
        return httpx.Response(200, json={'data': [{'id': 'fixture'}]})
    transport(monkeypatch, handle)
    try:
        with pytest.raises(ProviderProbeRevoked, match='cancelled'):
            svc.dispatch('provider_test', {'metadata': True, 'draft': {
                'kind': 'openai-compatible', 'preset_id': 'lm-studio', 'url': 'https://engine.example/v1'}})
        assert len(requests) == 1 and svc._native_io_pending == 0
    finally:
        svc.shutdown()


@pytest.mark.parametrize('payload', [{'data': [{}]}, {'data': ['fixture']},
    {'data': [{'id': 'bad\nname'}]}, {'data': [{'id': 'x' * 513}]},
    {'data': [{'id': 'fixture'}] * 10001}])
def test_invalid_catalog_is_sanitized_and_never_enriched(tmp_path, monkeypatch, payload):
    svc = service(tmp_path); requests = []
    transport(monkeypatch, lambda request: requests.append(request) or httpx.Response(200, json=payload))
    try:
        with pytest.raises(ValueError, match='invalid model catalog'):
            svc.dispatch('provider_test', {'metadata': True, 'draft': {
                'kind': 'openai-compatible', 'preset_id': 'lm-studio', 'url': 'https://engine.example/v1'}})
        assert len(requests) == 1 and svc._native_io_pending == 0
    finally:
        svc.shutdown()


@pytest.mark.parametrize('action', ['provider_test', 'performance', 'performance_tune'])
def test_exact_engine_io_never_starts_after_prepared_update(tmp_path, monkeypatch, action):
    svc = service(tmp_path); svc.update_manager = SimpleNamespace(applying=True, shutting_down=False)
    monkeypatch.setattr(svc, '_dispatch', lambda *args: pytest.fail('Prepared engine operation executed'))
    try:
        with pytest.raises(ValueError, match='Engine work is paused'):
            svc.dispatch(action)
        assert svc._native_io_pending == svc._performance_io_pending == 0
    finally:
        svc.update_manager = None; svc.shutdown()


def test_performance_wait_releases_gate_and_holds_both_counters_until_return(tmp_path):
    svc = service(tmp_path); entered, release = threading.Event(), threading.Event()
    class Performance:
        lock = threading.RLock()
        jobs = {}
        def dispatch(self, action, data):
            if action == 'performance_cancel':
                release.set(); return {'ok': True}
            entered.set(); assert release.wait(3)
            return {'ok': True}
        def shutdown(self): pass
    svc.performance_manager = Performance()
    updater = object.__new__(UpdateManager); updater.service = svc; updater.inspection_required = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(svc.dispatch, 'performance_tune')
            assert entered.wait(2)
            assert svc._native_io_pending == svc._performance_io_pending == 1
            with pytest.raises(ValueError, match='finish before updating'):
                updater._idle()
            assert pool.submit(svc.dispatch, 'performance_cancel', {'id': 'fixture'}).result(timeout=2)['ok']
            assert waiting.result(timeout=2)['ok']
        assert svc._native_io_pending == svc._performance_io_pending == 0
    finally:
        release.set(); svc.shutdown()


def test_unknown_performance_action_cannot_bypass_existing_update_gate(tmp_path):
    svc = service(tmp_path); svc.update_manager = SimpleNamespace(applying=True)
    try:
        with pytest.raises(ValueError, match='Changes are paused'):
            svc.dispatch('performance_unknown')
        assert svc._native_io_pending == svc._performance_io_pending == 0
    finally:
        svc.update_manager = None; svc.shutdown()


def test_cancel_after_shutdown_does_not_initialize_or_recover_performance_manager(tmp_path):
    svc = service(tmp_path); svc.stop_event.set()
    svc.update_manager = SimpleNamespace(applying=True, shutting_down=True)
    try:
        assert svc.dispatch('performance_cancel', {'id': 'missing'}) == {'ok': True}
        assert svc.performance_manager is None
        assert svc._native_io_pending == svc._performance_io_pending == 0
    finally:
        svc.update_manager = None; svc.shutdown()


def test_service_selection_change_then_revert_revokes_only_owned_tuning_generation(tmp_path):
    from forge_performance import PerformanceManager
    svc = service(tmp_path); manager = PerformanceManager(svc); svc.performance_manager = manager
    tuning, benchmark = threading.Event(), threading.Event()
    manager.jobs = {'tuning': {'operation': 'tune', 'cancel': tuning},
        'benchmark': {'operation': 'benchmark', 'cancel': benchmark}}
    original = svc.store.get_settings()['num_thread']
    try:
        svc.dispatch('settings', {'num_thread': original + 1})
        svc.dispatch('settings', {'num_thread': original})
        assert svc.store.get_settings()['num_thread'] == original
        assert manager.selection_generation == 2
        assert tuning.is_set() and not benchmark.is_set()
        assert manager.jobs['tuning']['stop_reason'] == 'selection_changed'
    finally:
        manager.jobs.clear(); svc.shutdown()


def test_provider_save_revokes_tune_and_invalidates_cached_identity(tmp_path):
    from forge_performance import PerformanceManager
    svc = service(tmp_path); svc.vault = Vault(); saved_engine(svc)
    manager = PerformanceManager(svc); svc.performance_manager = manager
    cancel = threading.Event()
    manager.jobs = {'tuning': {'operation': 'tune', 'cancel': cancel}}
    manager.cache['engine'] = 'old-model-catalog'
    manager.status_cache['engine:engine'] = 'old-engine-facts'
    try:
        svc.dispatch('provider_save', {'id': 'engine', 'preset_id': 'lm-studio'})
        assert cancel.is_set() and manager.jobs['tuning']['stop_reason'] == 'provider_changed'
        assert manager.selection_generation == 1
        assert 'engine' not in manager.cache and 'engine:engine' not in manager.status_cache
    finally:
        manager.jobs.clear(); svc.shutdown()


def test_foreground_command_revokes_tuning_without_mutating_selected_settings(tmp_path):
    from forge_performance import PerformanceManager
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    manager = PerformanceManager(svc); svc.performance_manager = manager
    cancel = threading.Event(); manager.jobs = {'tuning': {'operation': 'tune', 'cancel': cancel}}
    before = svc.store.get_settings()
    try:
        run = svc.dispatch('command', {'command_mode': 'do', 'text': 'Inspect parser'})
        assert svc.store.run(run['id'])['request'] == 'Inspect parser'
        assert cancel.is_set() and manager.jobs['tuning']['stop_reason'] == 'foreground_work'
        assert svc.store.get_settings() == before
    finally:
        manager.jobs.clear(); svc.shutdown()
