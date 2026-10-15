from types import SimpleNamespace

import pytest

import desktop


def fixture(monkeypatch, loaded=0, bridge=0, textarea=True):
    clock = SimpleNamespace(now=0)
    def sleep(seconds): clock.now += seconds
    monkeypatch.setattr(desktop, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    waits, calls = [], []
    def event(delay):
        def wait(timeout):
            waits.append(timeout)
            clock.now += min(delay, timeout)
            return delay <= timeout
        return SimpleNamespace(wait=wait)
    def evaluate(script):
        calls.append(script)
        assert len(waits) == 2
        if script == 'document.readyState': return 'complete'
        if script == 'document.title': return 'Forge'
        return {'composer': True, 'textarea': textarea, 'navigation': 5}
    window = SimpleNamespace(events=SimpleNamespace(loaded=event(loaded), _pywebviewready=event(bridge)), evaluate_js=evaluate)
    return window, clock, waits, calls


def test_probes_only_after_page_and_bridge_ready(monkeypatch):
    window, clock, waits, calls = fixture(monkeypatch, loaded=18, bridge=4)
    result = desktop._smoke_workspace_state(window)
    assert result['ui']['textarea'] is True and len(calls) == 3
    assert waits == [25, 7] and clock.now == 22


def test_bridge_wait_uses_remaining_budget_without_script_retry(monkeypatch):
    window, clock, waits, calls = fixture(monkeypatch, loaded=18, bridge=8)
    with pytest.raises(ValueError, match='bridge initialization'):
        desktop._smoke_workspace_state(window)
    assert waits == [25, 7] and clock.now == 25 and calls == []


def test_failed_page_never_probes_bridge_or_scripts(monkeypatch):
    window, clock, waits, calls = fixture(monkeypatch, loaded=26)
    with pytest.raises(ValueError, match='page load'):
        desktop._smoke_workspace_state(window)
    assert waits == [25] and clock.now == 25 and calls == []


def test_composer_without_input_cannot_pass(monkeypatch):
    window, clock, waits, calls = fixture(monkeypatch, loaded=24, textarea=False)
    with pytest.raises(ValueError, match='composer'):
        desktop._smoke_workspace_state(window)
    assert clock.now == 25 and calls and waits == [25, 1]
