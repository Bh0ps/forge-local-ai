"""No tokens, origins or editor content leave native update readiness."""
import threading

import pytest

from forge_host import PairingAuthority


def test_every_unexpired_paired_session_blocks_without_revealing_identity():
    now = [1.0]
    pairing = PairingAuthority(clock=lambda: now[0])
    assert pairing.no_active_editors() is True
    first = pairing.pair_browser(pairing.issue()['code'], 'synthetic-client', 'http://127.0.0.1:12345')
    assert pairing.no_active_editors() is False
    pairing.revoke(first['session'])
    assert pairing.no_active_editors() is True
    second = pairing.pair(pairing.issue()['code'], 'synthetic-legacy-client')
    assert pairing.no_active_editors() is False
    now[0] += 86400
    assert pairing.no_active_editors() is True
    assert not pairing.valid(second)


@pytest.mark.parametrize('record', [None, {'expires_at': True}, {'expires_at': 'synthetic-secret'}, {'expires_at': float('nan')}, {'expires_at': float('inf')}])
def test_unknown_editor_records_fail_closed(record):
    pairing = PairingAuthority()
    pairing._sessions['synthetic'] = record
    assert pairing.no_active_editors() is False


def test_close_synchronously_clears_session_guard():
    pairing = PairingAuthority()
    pairing.pair_browser(pairing.issue()['code'], 'synthetic-client', 'http://127.0.0.1:12345')
    assert not pairing.no_active_editors()
    pairing.close()
    assert pairing.no_active_editors()


@pytest.mark.parametrize('allowed', [False, None, 'true', 1])
def test_unknown_or_closed_update_admission_does_not_consume_pair_code(allowed):
    pairing = PairingAuthority()
    state = [allowed]
    pairing.bind_update_admission(threading.RLock(), lambda: state[0])
    code = pairing.issue()['code']
    with pytest.raises(ValueError, match='Pairing is paused'):
        pairing.pair_browser(code, 'synthetic-client', 'http://127.0.0.1:12345')
    assert pairing.no_active_editors()
    state[0] = True
    assert pairing.pair_browser(code, 'synthetic-client', 'http://127.0.0.1:12345')


def test_pairing_and_update_gate_share_atomic_admission_boundary():
    pairing = PairingAuthority()
    admission = threading.RLock()
    applying = [False]
    pairing.bind_update_admission(admission, lambda: not applying[0])
    code = pairing.issue()['code']
    started = threading.Event()
    outcomes = []
    def pair():
        started.set()
        try:
            pairing.pair_browser(code, 'synthetic-client', 'http://127.0.0.1:12345')
            outcomes.append('paired')
        except ValueError:
            outcomes.append('blocked')
    with admission:
        thread = threading.Thread(target=pair)
        thread.start()
        assert started.wait(1)
        applying[0] = True
    thread.join(timeout=2)
    assert outcomes == ['blocked'] and pairing.no_active_editors()
    applying[0] = False
    assert pairing.pair_browser(code, 'synthetic-client', 'http://127.0.0.1:12345')
