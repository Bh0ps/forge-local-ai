"""Independent final regressions for revocation and interrupted model streams."""
import copy
import json
from types import SimpleNamespace
import threading

import httpx
import pytest

from forge_inference import CompatibleProvider
from forge_runs import ToolRegistry
from forge_service import ForgeService
from forge_store import ForgeStore


@pytest.mark.parametrize("update", [
    {"permission_profile": "deny_access"},
    {"permission_profile": "always_ask"},
    {"permission_overrides": {"tool:write_file": "deny_access"}},
])
def test_active_run_observes_permission_revocation(tmp_path, update):
    store = ForgeStore(tmp_path / "forge")
    store.update_settings({"permission_profile": "full_access"})
    registry = ToolRegistry(SimpleNamespace(store=store))
    run = {"settings": copy.deepcopy(store.get_settings()), "project_id": "project"}
    schema = {"function": {"name": "write_file"}}
    assert registry.permission(run, schema, {"path": "source.py"}) == "allow"
    store.update_settings(update)
    expected = "ask" if update.get("permission_profile") == "always_ask" else "deny"
    assert registry.permission(run, schema, {"path": "source.py"}) == expected


def compatible_response(monkeypatch, packets):
    raw = "".join("data: " + (json.dumps(packet) if isinstance(packet, dict) else packet) + "\n\n"
                  for packet in packets).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw,
                                                                  headers={"content-type": "text/event-stream"}))
    original = httpx.AsyncClient
    monkeypatch.setattr("forge_inference.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    provider = CompatibleProvider({"url": "http://127.0.0.1:8080/v1"})
    data = {"model": "fixture", "context": 32768, "tokens": 1024,
            "messages": [{"role": "user", "content": "Complete this task."}]}
    return provider.generate(data, threading.Event())


def test_incomplete_compatible_stream_never_signals_completion(monkeypatch):
    chunks = compatible_response(monkeypatch, [
        {"choices": [{"delta": {"content": "Partial reply"}, "finish_reason": None}]}
    ])
    observed = []
    try:
        observed.extend(chunks)
    except (ValueError, httpx.HTTPError):
        pass
    assert not any(chunk.get("done") for chunk in observed)


def test_compatible_finished_stream_completes_and_preserves_usage(monkeypatch):
    chunks = list(compatible_response(monkeypatch, [
        {"choices": [{"delta": {"content": "Done"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}, "[DONE]",
    ]))
    assert chunks[-1]["done"] is True
    assert chunks[-1]["usage"]["completion_tokens"] == 3


def test_provider_rejects_credentials_embedded_in_query(tmp_path):
    service = ForgeService.__new__(ForgeService)
    service.store = ForgeStore(tmp_path / "forge")
    service.vault = None
    with pytest.raises(ValueError, match="credential|secret|query",):
        service.provider_save({"name": "Fixture", "url": "https://fixture.example/v1?api_key=private-test-value"})
    assert "private-test-value" not in service.store.db_path.read_bytes().decode("utf-8", errors="ignore")
