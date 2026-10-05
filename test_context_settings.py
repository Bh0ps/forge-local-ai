"""The shared desktop/Docker API persists and validates context preferences."""
import pytest

from service import Service
from storage import Store


class NoNetworkCore:
    def dispatch(self, action, data):
        pytest.fail('Context preferences must not contact the model service.')


@pytest.fixture
def service(tmp_path):
    return Service(core=NoNetworkCore(), store=Store(tmp_path / 'state'))


def test_settings_api_default_and_restart_persistence(service):
    assert service.dispatch('settings') == {'context': 32_768}
    assert service.dispatch('settings', None) == {'context': 32_768}
    assert service.dispatch('settings', {}) == {'context': 32_768}
    assert service.dispatch('settings', {'context': 96_256}) == {'context': 96_256}
    restarted = Service(core=NoNetworkCore(), store=Store(service.store.data_dir))
    assert restarted.dispatch('settings') == {'context': 96_256}


@pytest.mark.parametrize('data', [
    {'context': 2_047}, {'context': 262_145}, {'context': 3_072}, {'context': True},
    {'context': 65_536.0}, {'context': '65536'}, {'context': None},
    {'context': 65_536, 'temperature': 0.2}, {'unknown': 1},
    [], '', False, 0, 'context', 65_536,
], ids=['below-min', 'above-max', 'wrong-step', 'bool-value', 'float-value', 'string-value',
        'null-value', 'mixed-key', 'unknown-key', 'empty-list', 'empty-string',
        'false-request', 'zero-request', 'string-request', 'number-request'])
def test_settings_api_invalid_updates_preserve_saved_value(service, data):
    service.dispatch('settings', {'context': 16_384})
    with pytest.raises(ValueError):
        service.dispatch('settings', data)
    assert service.dispatch('settings') == {'context': 16_384}


@pytest.mark.parametrize('context', [2_048, 262_144])
def test_settings_api_accepts_both_slider_extremes(service, context):
    assert service.dispatch('settings', {'context': context}) == {'context': context}
