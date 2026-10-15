from types import SimpleNamespace

import pytest

import test_forge_core as core


def setup(monkeypatch, replies, delay=0, hook=None):
    listeners=[];calls=[];waits=[];clock=SimpleNamespace(now=0)
    class Signal:
        ready=False
        def set(self): self.ready=True
        def is_set(self): return self.ready
        def wait(self, seconds):
            waits.append(seconds)
            if hook: hook(listeners)
    signal=Signal()
    monkeypatch.setattr(core,'threading',SimpleNamespace(Event=lambda:signal))
    monkeypatch.setattr(core,'time',SimpleNamespace(monotonic=lambda:clock.now))
    replies=iter(replies)
    def poll(action,data):
        assert listeners and action=='poll'
        calls.append(data)
        clock.now+=delay
        value=next(replies)
        if isinstance(value,Exception): raise value
        if callable(value): return value(listeners)
        return value
    store=SimpleNamespace(subscribe_events=listeners.append,unsubscribe_events=listeners.remove)
    return SimpleNamespace(store=store,dispatch=poll),listeners,calls,waits,signal


DONE={'finished':True,'status':'completed','events':[],'has_more':False,'next_cursor':0}
PENDING={'finished':False,'status':'running','events':[],'has_more':False,'next_cursor':0}


def test_historical_completion_needs_no_wait(monkeypatch):
    svc,listeners,calls,waits,_=setup(monkeypatch,[DONE])
    assert core.finished(svc,{'id':'main'})==DONE
    assert len(calls)==1 and waits==[] and listeners==[]


def test_commit_between_read_and_wait_stays_latched(monkeypatch):
    def first(listeners):
        listeners[0]({'run_id':'main','type':'done'})
        return PENDING
    svc,listeners,calls,waits,signal=setup(monkeypatch,[first,DONE],delay=3)
    assert core.finished(svc,{'id':'main'})==DONE
    assert signal.ready and waits==[12] and len(calls)==2 and not listeners


def test_commit_during_wait_requires_durable_completion(monkeypatch):
    svc,listeners,calls,waits,signal=setup(monkeypatch,[PENDING,DONE],
        hook=lambda listeners:listeners[0]({'run_id':'main','type':'done'}))
    assert core.finished(svc,{'id':'main'})==DONE
    assert signal.ready and waits==[15] and len(calls)==2 and not listeners


@pytest.mark.parametrize('run,kind',[('child','done'),('main','status'),('other','done')])
def test_unrelated_events_do_not_establish_completion(monkeypatch,run,kind):
    svc,listeners,_,_,signal=setup(monkeypatch,[PENDING,PENDING],
        hook=lambda listeners:listeners[0]({'run_id':run,'type':kind}))
    with pytest.raises(AssertionError,match='Run did not finish'):
        core.finished(svc,{'id':'main'})
    assert not signal.ready and not listeners


def test_notification_without_durable_finished_flag_cannot_pass(monkeypatch):
    svc,listeners,_,_,signal=setup(monkeypatch,[PENDING,PENDING],
        hook=lambda listeners:listeners[0]({'run_id':'main','type':'done'}))
    with pytest.raises(AssertionError,match='Run did not finish'):
        core.finished(svc,{'id':'main'})
    assert signal.ready and not listeners


@pytest.mark.parametrize('flag',[1,'true',{}])
def test_truthy_nonboolean_finished_flag_cannot_pass(monkeypatch,flag):
    pending={**PENDING,'finished':flag}
    svc,listeners,_,_,_=setup(monkeypatch,[pending,pending])
    with pytest.raises(AssertionError,match='Run did not finish'):
        core.finished(svc,{'id':'main'})
    assert not listeners


def test_wait_budget_includes_initial_read_and_is_not_reset(monkeypatch):
    svc,listeners,_,waits,_=setup(monkeypatch,[PENDING,DONE],delay=16)
    assert core.finished(svc,{'id':'main'})==DONE
    assert waits==[0] and not listeners


def test_historical_terminal_backlog_advances_cursor_and_keeps_all_events(monkeypatch):
    page={'finished':False,'status':'completed','events':[{'seq':1}], 'has_more':True,'next_cursor':1}
    last={**DONE,'events':[{'seq':2}],'next_cursor':2}
    svc,listeners,calls,waits,_=setup(monkeypatch,[page,last])
    result=core.finished(svc,{'id':'main'})
    assert result['events']==[{'seq':1},{'seq':2}] and result['next_cursor']==2
    assert [c['after'] for c in calls]==[0,1] and not waits and not listeners


def test_nonadvancing_event_page_cannot_loop_or_report_success(monkeypatch):
    page={**PENDING,'has_more':True}
    svc,listeners,calls,_,_=setup(monkeypatch,[page])
    with pytest.raises(AssertionError,match='cursor did not advance'):
        core.finished(svc,{'id':'main'})
    assert len(calls)==1 and not listeners


def test_transport_failure_always_unsubscribes(monkeypatch):
    svc,listeners,_,_,_=setup(monkeypatch,[ValueError('Synthetic failure')])
    with pytest.raises(ValueError,match='Synthetic'):
        core.finished(svc,{'id':'main'})
    assert not listeners
