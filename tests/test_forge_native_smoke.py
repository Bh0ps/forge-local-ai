from pathlib import Path
from types import SimpleNamespace
import pytest
from forge_native_smoke import frozen_native_smoke


def test_native_smoke_refuses_unverified_weights_before_catalog_mutation(tmp_path,monkeypatch):
    source=tmp_path/'not-a-model.gguf'
    source.write_bytes(b'synthetic unrelated file')
    monkeypatch.setenv('FORGE_NATIVE_SMOKE_MODEL',str(source))
    service=SimpleNamespace(store=SimpleNamespace(home=tmp_path/'state'))
    with pytest.raises(ValueError,match='pinned digest'):
        frozen_native_smoke(service)
    assert not service.store.home.exists()
