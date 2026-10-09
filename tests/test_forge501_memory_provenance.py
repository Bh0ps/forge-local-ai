"""Only private memory in the finalized local model context taints narrative."""
from copy import deepcopy
import json
import threading

import pytest

from forge_memory import MEMORY_NOTICE
from prompt_compiler import private_memory_context, private_memory_provenance
from test_forge_core import Engine, finished, service
from test_forge_memory import saved


PRIVATE='PRIVATE-MEMORY-CONTENT-739 belongs in local context only.'
QUERY='memoryprovenancemarker'


def block(matches):return MEMORY_NOTICE+'\n'+json.dumps(matches,ensure_ascii=False,separators=(',',':'))


@pytest.mark.parametrize('value',[block([]),MEMORY_NOTICE+'\nnot-json',block('untrusted-string'),block([1])])
def test_empty_or_invalid_recall_has_no_supplied_provenance(value):
    assert private_memory_provenance([private_memory_context(value)]) is None


def test_only_exact_trusted_block_in_finalized_local_messages_counts():
    content=block([{'id':'owned-memory','snippet':PRIVATE}]);trusted=private_memory_context(content)
    value=private_memory_provenance([trusted])
    assert value['count']==1 and len(value['digest'])==64 and PRIVATE not in json.dumps(value)
    assert json.loads(json.dumps(trusted))=={'role':'system','content':content}
    assert private_memory_provenance([{'role':'system','content':content}]) is None
    assert private_memory_provenance([{'role':'user','content':content}]) is None
    assert private_memory_provenance([]) is None  # compiled recall was never supplied
    assert private_memory_provenance([trusted],cloud_scope=True) is None
    changed=deepcopy(trusted);changed['content']=block([{'id':'forged','snippet':'Different'}])
    assert private_memory_provenance([changed]) is None


def local_memory(svc):
    return saved(svc.get_memory(),title=QUERY,content=PRIVATE)


def test_actual_approved_recall_records_only_count_digest_at_provider_turn(tmp_path):
    engine=Engine();svc=service(tmp_path,engine)
    try:
        item=local_memory(svc);settings=svc.store.get_settings();assert settings['memory_enabled']
        accepted=svc.jobs.start({'text':QUERY});assert finished(svc,accepted)['status']=='completed'
        current=svc.store.run(accepted['id']);metadata=current['private_memory_provenance']
        assert current['private_memory_supplied'] is True and metadata['count']==1
        assert current['context_snapshot']['memory_provenance']==metadata
        assert PRIVATE in '\n'.join(m['content'] for m in engine.requests[0]['messages'])
        assert PRIVATE not in json.dumps(current)
        events=[e for e in svc.store.events(current['id']) if e['type']=='context']
        assert events[0]['memory_provenance']==metadata and PRIVATE not in json.dumps(events)
        assert svc.store.get_settings()==settings
    finally:svc.shutdown()


@pytest.mark.parametrize('remove',[False,True])
def test_empty_or_removed_final_recall_does_not_mark_supply(tmp_path,monkeypatch,remove):
    engine=Engine();svc=service(tmp_path,engine)
    try:
        if remove:local_memory(svc)
        compact=svc.jobs._compact
        def finalize(run,schemas,job,**kwargs):
            current=compact(run,schemas,job,**kwargs)
            if remove:job['context_messages']=[m for m in job['context_messages'] if not m['content'].startswith(MEMORY_NOTICE)]
            return current
        monkeypatch.setattr(svc.jobs,'_compact',finalize)
        accepted=svc.jobs.start({'text':QUERY});finished(svc,accepted)
        current=svc.store.run(accepted['id'])
        assert not current.get('private_memory_supplied') and current['context_snapshot']['memory_provenance'] is None
        assert PRIVATE not in '\n'.join(m['content'] for m in engine.requests[0]['messages'])
    finally:svc.shutdown()


def test_private_memory_flag_stays_sticky_after_compaction_forget_restart_and_resume(tmp_path):
    svc=service(tmp_path)
    def stop(data,cancel):
        svc.jobs.cancel(svc.store.runs()[0]['id'],pause=True)
        yield {'message':{'content':'Controlled stop after context admission.'},'done':True}
    svc.core.rounds=[stop]
    try:
        item=local_memory(svc)
        goal=svc.goal_create({'text':QUERY})
        accepted=svc.jobs.start({'text':QUERY,'goal_id':goal['id'],'mode':'goal'})
        assert finished(svc,accepted)['status']=='paused'
        initial=svc.store.run(accepted['id']);metadata=initial['private_memory_provenance']
        assert initial['private_memory_supplied'] is True
        svc.get_memory().dispatch('memory_forget',{'ids':[item['id']]},human=True)
        svc.jobs._compact(initial,[],{'cancel':threading.Event()},force=True)
        assert svc.store.run(initial['id'])['private_memory_supplied'] is True
    finally:svc.shutdown()
    replacement=service(tmp_path)
    def stop_again(data,cancel):
        replacement.jobs.cancel(initial['id'],pause=True)
        yield {'message':{'content':'Controlled second stop.'},'done':True}
    replacement.core.rounds=[stop_again]
    try:
        assert replacement.store.run(initial['id'])['private_memory_supplied'] is True
        replacement.jobs.resume(initial['id']);finished(replacement,initial)
        resumed=replacement.store.run(initial['id'])
        assert resumed['private_memory_supplied'] is True and resumed['private_memory_provenance']==metadata
        assert resumed['context_snapshot']['memory_provenance'] is None
        assert PRIVATE not in '\n'.join(m['content'] for m in replacement.core.requests[0]['messages'])
        assert replacement.store.get_settings()['memory_enabled'] is True
    finally:replacement.shutdown()


def test_local_child_records_its_own_private_memory_contribution(tmp_path):
    svc=service(tmp_path)
    try:
        local_memory(svc)
        parent=svc.jobs.start({'text':'neutralparentrequest'});finished(svc,parent)
        assert not svc.store.run(parent['id']).get('private_memory_supplied')
        child=svc.jobs.start({'text':QUERY,'parent_id':parent['id'],'readonly':True});finished(svc,child)
        assert svc.store.run(child['id'])['private_memory_supplied'] is True
    finally:svc.shutdown()


def test_scoped_cloud_context_never_calls_local_memory(tmp_path,monkeypatch):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    try:
        accepted=svc.jobs.start({'text':QUERY});run=svc.store.run(accepted['id'])
        monkeypatch.setattr(svc,'get_memory',lambda:pytest.fail('Scoped cloud context retrieved private memory.'))
        scoped={**run,'cloud_scope':{'version':1,'files':[],'artifacts':[]}}
        messages=svc.jobs._context(scoped,[])
        assert not any(m['content'].startswith(MEMORY_NOTICE) for m in messages)
        assert private_memory_provenance(messages,cloud_scope=True) is None
    finally:svc.shutdown()
