"""Tests must never create or change the user's real Sidekick database."""
import pytest

@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv('SIDEKICK_DATA_DIR', str(tmp_path/'app-data'))
