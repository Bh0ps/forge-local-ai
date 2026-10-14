"""Status polling skips inactive cues without caching scope authorization."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from browser_tools import BrowserTools
from native_browser import NativeBrowser


@pytest.fixture
def cue(tmp_path):
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute('CREATE TABLE runs(id TEXT,chat_id TEXT,parent_id TEXT)')
    database.executemany('INSERT INTO runs VALUES(?,?,?)',
        [('root', 'chat-a', None), ('helper', 'helper-chat', 'root')])
    reads = []
    @contextmanager
    def connection(**_):
        reads.append(True)
        yield database
    store = SimpleNamespace(_connection=connection)
    clock = [10.0]
    broker = NativeBrowser(tmp_path, view_factory=lambda *_: None,
        clock=lambda: clock[0], indicator_scope=lambda owner, scope:
            BrowserTools.native_indicator_scope(store, owner, scope))
    broker.view = SimpleNamespace(visible=True)
    broker._pane_allowed = True
    broker._indicator = dict(run='helper', time=10.0, pane=0, generation=0)
    scope = dict(chat_id='chat-a', run_id='root')
    yield broker, clock, reads, scope
    broker.pool.shutdown(wait=True)
    database.close()


@pytest.mark.parametrize('condition', [
    'expired', 'old', 'clock-rollback', 'hidden', 'missing-view', 'loading',
    'cancelled', 'revoked', 'stale-pane', 'stale-navigation', 'closed', 'no-indicator'])
def test_ineligible_cues_never_read_ancestry(cue, condition):
    broker, clock, reads, scope = cue
    if condition == 'expired': clock[0] = 13.0
    elif condition == 'old': clock[0] = 100.0
    elif condition == 'clock-rollback': clock[0] = 9.0
    elif condition == 'hidden': broker.view.visible = False
    elif condition == 'missing-view': broker.view = None
    elif condition == 'loading': broker.state['loading'] = True
    elif condition == 'cancelled': broker.cancelled.set()
    elif condition == 'revoked': broker._pane_allowed = False
    elif condition == 'stale-pane': broker._pane_generation += 1
    elif condition == 'stale-navigation': broker.generation += 1
    elif condition == 'closed': broker.closed = True
    elif condition == 'no-indicator': broker._indicator = None
    for _ in range(10):
        assert broker.status(scope)['target_indicator'] == 'none'
    assert reads == []


def test_fresh_cue_revalidates_each_scope_then_stops_reading_at_expiry(cue):
    broker, clock, reads, scope = cue
    assert broker.status(scope)['target_indicator'] == 'active'
    assert len(reads) == 1
    assert broker.status(dict(chat_id='other', run_id='root'))['target_indicator'] == 'none'
    assert len(reads) == 2
    clock[0] = 12.999
    assert broker.status(scope)['target_indicator'] == 'active'
    assert len(reads) == 3
    clock[0] = 13.0
    for _ in range(10):
        assert broker.status(scope)['target_indicator'] == 'none'
    assert len(reads) == 3


@pytest.mark.parametrize('change', ['expiry', 'hide', 'stop', 'navigation', 'replaced'])
def test_scope_read_cannot_publish_a_cue_revoked_during_the_read(cue, change):
    broker, clock, reads, scope = cue
    original = broker.indicator_scope
    def delayed(owner, requested):
        authorized = original(owner, requested)
        if change == 'expiry': clock[0] = 13.0
        elif change == 'hide': broker._pane_allowed = False
        elif change == 'stop': broker.cancelled.set()
        elif change == 'navigation': broker.generation += 1
        elif change == 'replaced': broker._indicator = dict(broker._indicator)
        return authorized
    broker.indicator_scope = delayed
    assert broker.status(scope)['target_indicator'] == 'none'
    assert len(reads) == 1


def test_scope_failure_hides_cue_and_can_retry_without_cached_authorization(cue):
    broker, clock, reads, scope = cue
    original = broker.indicator_scope
    broker.indicator_scope = lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic read'))
    assert broker.status(scope)['target_indicator'] == 'none'
    assert reads == []
    broker.indicator_scope = original
    assert broker.status(scope)['target_indicator'] == 'active'
    assert len(reads) == 1
