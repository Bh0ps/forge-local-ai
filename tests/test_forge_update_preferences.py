import pytest
from forge_store import ForgeStore


def test_update_preferences_survive_restart_and_preserve_other_settings(tmp_path):
    store = ForgeStore(tmp_path)
    assert store.get_settings()['update_checks'] is True
    assert store.get_settings()['update_channel'] == 'preview'
    store.update_settings({'model': 'fixture', 'context': 65536, 'temperature': 0.2})
    store.update_settings({'update_checks': False, 'update_channel': 'stable'})
    restored = ForgeStore(tmp_path).get_settings()
    assert restored['update_checks'] is False and restored['update_channel'] == 'stable'
    assert restored['model'] == 'fixture' and restored['context'] == 65536 and restored['temperature'] == 0.2


@pytest.mark.parametrize('preferences', [{'update_checks': 'false'}, {'update_checks': 1},
    {'update_channel': 'nightly'}, {'update_channel': None}])
def test_invalid_update_preferences_do_not_partially_save(tmp_path, preferences):
    store = ForgeStore(tmp_path)
    before = store.get_settings()
    with pytest.raises(ValueError):
        store.update_settings({**preferences, 'model': 'should-not-save'})
    assert store.get_settings() == before
