"""The Core fixture waits on committed events without losing an approval wakeup."""
from types import SimpleNamespace
import json
import threading

import pytest

from forge_store import ForgeStore
import test_forge_core as core


def _boundary_fixture(tmp_path,monkeypatch,boundary):
    store=ForgeStore(tmp_path/'store')
    run=store.create_run({'chat_id':'fixture-chat'})
    other=store.create_run({'chat_id':'other-chat'})
    ready=threading.Event(); waits=[]; polls=[]
    def commit():
        return store.event(run['id'],'approval',id='fixture-approval')
    class Signal:
        def set(self):
            assert any(e['type']=='approval' for e in store.events(run['id']))
            ready.set()
        def wait(self,timeout):
            waits.append(timeout)
            if boundary=='during_wait': commit()
            # No sleeping: assert the real latch state at this exact boundary.
            assert ready.is_set()==(boundary in {'after_first_read','during_wait'})
            return ready.wait(0)
    monkeypatch.setattr(core,'threading',SimpleNamespace(Event=Signal))
    monkeypatch.setattr(core,'time',SimpleNamespace(monotonic=lambda:90))
    def poll(identifier):
        assert identifier==run['id']
        events=store.events(identifier)
        polls.append(events)
        if len(polls)==1:
            if boundary=='after_first_read': commit()
            elif boundary=='other_run': store.event(other['id'],'approval',id='unrelated')
            elif boundary=='other_type': store.event(run['id'],'status',text='Synthetic status')
        return {'events':events,'status':'running','finished':False}
    svc=SimpleNamespace(store=store,jobs=SimpleNamespace(poll=poll))
    return svc,run,commit,waits,polls


@pytest.mark.parametrize('boundary',['before_subscription','after_first_read','during_wait'])
def test_approval_wait_reads_committed_events_at_wakeup_boundaries(tmp_path,monkeypatch,boundary):
    svc,run,commit,waits,polls=_boundary_fixture(tmp_path,monkeypatch,boundary)
    if boundary=='before_subscription': commit()
    pending,snapshot=core._wait_for_run_approval(svc,run,100)
    assert pending['id']=='fixture-approval' and pending['run_id']==run['id']
    assert pending in snapshot['events']
    assert waits==([] if boundary=='before_subscription' else [10])
    assert len(polls)==(1 if boundary=='before_subscription' else 2)
    assert svc.store._event_listeners==[]


@pytest.mark.parametrize('boundary',['other_run','other_type','expired'])
def test_approval_wait_filters_events_and_keeps_the_original_deadline(tmp_path,monkeypatch,boundary):
    svc,run,_,waits,polls=_boundary_fixture(tmp_path,monkeypatch,boundary)
    pending,snapshot=core._wait_for_run_approval(svc,run,80 if boundary=='expired' else 100)
    assert pending is None and all(e['type']!='approval' for e in snapshot['events'])
    assert waits==([0] if boundary=='expired' else [10])
    assert len(polls)==2 and svc.store._event_listeners==[]


def test_approval_wait_unsubscribes_after_a_snapshot_failure(tmp_path):
    store=ForgeStore(tmp_path/'store')
    def poll(_): raise ValueError('Synthetic snapshot failure')
    svc=SimpleNamespace(store=store,jobs=SimpleNamespace(poll=poll))
    with pytest.raises(ValueError,match='Synthetic snapshot failure'):
        core._wait_for_run_approval(svc,{'id':'fixture-run'},0)
    assert store._event_listeners==[]


@pytest.mark.parametrize('boundary',['missing_approval','snapshot_failure'])
def test_core_approval_fixture_shuts_down_on_failure_without_logging_payloads(tmp_path,monkeypatch,boundary):
    closed=[]
    sentinel='SYNTHETIC-PRIVATE-PAYLOAD'
    svc=SimpleNamespace(
        create_project=lambda _: {'id':'fixture-project'},
        jobs=SimpleNamespace(start=lambda _: {'id':'fixture-run'}),
        store=SimpleNamespace(run=lambda _: {'phase':sentinel,'settings':{'model':'fixture','private':sentinel}}),
        shutdown=lambda:closed.append(True),
    )
    def wait(*_):
        if boundary=='snapshot_failure': raise ValueError('Synthetic snapshot failure')
        return None,{'status':sentinel,'finished':False,'recovery':sentinel,
                     'events':[{'type':'status','text':sentinel}]}
    monkeypatch.setattr(core,'service',lambda *_:svc)
    monkeypatch.setattr(core,'_wait_for_run_approval',wait)
    expected=ValueError if boundary=='snapshot_failure' else AssertionError
    with pytest.raises(expected) as failure:
        core.test_always_ask_binds_exact_file_action_then_no_replay_on_resume(tmp_path)
    assert closed==[True]
    if boundary=='missing_approval':
        # JSON text survives pytest's assertion formatting without dict truncation.
        diagnostic=json.loads(str(failure.value).splitlines()[0])
        assert diagnostic['status']==diagnostic['phase']=='unknown'
        assert diagnostic['stop_category']=='approval_missing_recovery'
        assert diagnostic['event_types']==['status'] and diagnostic['hello_exists'] is False
        assert diagnostic['effective_settings']['model']=='fixture'
        assert sentinel not in str(diagnostic)
