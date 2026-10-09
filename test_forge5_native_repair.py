"""Only known local pre-execution tool JSON errors get one smaller-call repair."""
import threading
from types import SimpleNamespace

import httpx
import pytest

from forge_runs import native_tool_json_issue
from test_forge_core import Engine, call, finished, service
from tool_routing import catalog_search

NATIVE_ERROR='llama-server returned invalid tool call arguments for "write_file": unexpected end of JSON input'


def project(svc,tmp_path):
    svc.store.update_settings({'permission_profile':'full_access'})
    root=tmp_path/'project'; root.mkdir()
    return svc.create_project({'name':'Parser fixture','path':str(root)}),root


def broken(data,cancel):
    # Even a complete earlier call in this unfinished response must not execute.
    yield {'message':{'content':'Preparing a focused edit.',
        'tool_calls':[call('write_file',{'path':'discarded.txt','content':'Never execute this unfinished batch.'})]}}
    raise ValueError(NATIVE_ERROR)


def test_native_json_failure_repairs_with_one_small_complete_write(tmp_path):
    engine=Engine([broken,{'tool_calls':[call('write_file',{'path':'small.txt','content':'Small valid file.','expected_sha256':'missing'})]},
                   {'content':'Verified the small file.'}])
    svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path)
        accepted=svc.jobs.start({'text':'Write a small file and report the result.','project_id':selected['id'],
            'permission_profile':'full_access','tokens':2048})
        assert finished(svc,accepted)['status']=='completed'
        assert (root/'small.txt').read_text()=='Small valid file.' and not (root/'discarded.txt').exists()
        assert len(engine.requests)==3 and svc.store.run(accepted['id'])['tool_repairs']==1
        feedback='\n'.join(m['content'] for m in engine.requests[1]['messages'])
        assert 'one smaller complete call' in feedback and 'No actions from this generation ran' in feedback
        assert '2048 tokens for this whole turn' in feedback and 'hash-guarded verified edits' in feedback
        partial=next(m for m in svc.store.run_chat(accepted['chat_id'],0)['messages'] if m.get('status')=='partial')
        assert partial['content']=='Preparing a focused edit.' and not partial.get('tool_calls')
        validation=[e for e in svc.store.events(accepted['id']) if e['type']=='tool_validation']
        assert len(validation)==1 and validation[0]['executed'] is False
        with svc.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(accepted['id'],)).fetchone()[0]==1
    finally: svc.shutdown()


def test_second_native_json_failure_stops_without_actions(tmp_path):
    engine=Engine([broken,broken,{'content':'Must not be requested.'}]); svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path)
        accepted=svc.jobs.start({'text':'Write the small file.','project_id':selected['id'],'permission_profile':'full_access'})
        result=finished(svc,accepted)
        assert result['status']=='paused' and 'after one repair attempt' in result['recovery']
        assert len(engine.requests)==2 and svc.store.run(accepted['id'])['tool_repair_streak']==2
        assert not (root/'discarded.txt').exists()
        with svc.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(accepted['id'],)).fetchone()[0]==0
    finally: svc.shutdown()


@pytest.mark.parametrize('failure',[
    ValueError('Local provider HTTP 503. Check engine status and resume.'),
    ValueError('Free route quota exhausted.'),
    httpx.HTTPStatusError(NATIVE_ERROR,request=httpx.Request('POST','http://fixture/chat'),response=httpx.Response(503)),
    ValueError('llama-server returned invalid tool call arguments for "unoffered_tool": unexpected end of JSON input')])
def test_general_provider_failures_are_not_retried_or_executed(tmp_path,failure):
    def fail(data,cancel):
        yield {'message':{'content':'Visible partial progress.',
            'tool_calls':[call('write_file',{'path':'bad.txt','content':'Do not execute.'})]}}
        raise failure
    engine=Engine([fail,{'content':'Must not be requested.'}]); svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path)
        accepted=svc.jobs.start({'text':'Write a small file.','project_id':selected['id'],'permission_profile':'full_access'})
        assert finished(svc,accepted)['status']=='paused'
        assert len(engine.requests)==1 and not (root/'bad.txt').exists()
        assert not svc.store.run(accepted['id']).get('tool_repairs')
        assert not any(e['type']=='tool_validation' for e in svc.store.events(accepted['id']))
        assert any(m['content']=='Visible partial progress.' and m.get('status')=='partial'
            for m in svc.store.run_chat(accepted['chat_id'],0)['messages'])
    finally: svc.shutdown()


def test_native_error_after_a_successful_done_packet_is_not_repaired(tmp_path):
    def completed_then_failed(data,cancel):
        yield {'message':{'tool_calls':[call('write_file',{'path':'bad.txt','content':'Do not execute.'})]},'done':True}
        raise ValueError(NATIVE_ERROR)
    engine=Engine([completed_then_failed]); svc=service(tmp_path,engine)
    try:
        selected,root=project(svc,tmp_path)
        accepted=svc.jobs.start({'text':'Write a file.','project_id':selected['id'],'permission_profile':'full_access'})
        assert finished(svc,accepted)['status']=='paused'
        assert len(engine.requests)==1 and not (root/'bad.txt').exists()
        assert not svc.store.run(accepted['id']).get('tool_repairs')
    finally: svc.shutdown()


def test_non_ollama_errors_and_unrecognized_envelopes_do_not_classify(tmp_path):
    svc=service(tmp_path)
    try:
        provider=svc.providers.provider('ollama'); names={'write_file'}
        assert native_tool_json_issue(SimpleNamespace(),ValueError(NATIVE_ERROR),names) is None
        assert native_tool_json_issue(provider,ValueError('HTTP 500: '+NATIVE_ERROR),names) is None
        assert native_tool_json_issue(provider,ValueError(NATIVE_ERROR),set()) is None
        assert native_tool_json_issue(provider,ValueError(NATIVE_ERROR.replace('unexpected end of JSON input',"invalid character 'x' looking for beginning of value")),names)
    finally: svc.shutdown()


@pytest.mark.parametrize('query,expected',[
    ('write_file','write_file'),('apply_patch','apply_patch'),
    ('replace file text','edit_file'),('hash checked patch','apply_patch')])
def test_real_project_catalog_matches_exact_names_and_edit_intent(tmp_path,query,expected):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        selected,_=project(svc,tmp_path)
        accepted=svc.jobs.start({'text':'Build the project.','project_id':selected['id']})
        schemas=svc.jobs.registry.schemas(svc.store.run(accepted['id']),['tools'],all_tools=True)
        results=catalog_search(schemas,query,limit=8)
        assert expected in {r['name'] for r in results[:3]},results
        if query==expected: assert results[0]['name']==expected
        plan=svc.jobs.registry.schemas({**svc.store.run(accepted['id']),'mode':'plan'},['tools'],all_tools=True)
        assert expected not in {r['name'] for r in catalog_search(plan,query)}
    finally: svc.shutdown()
