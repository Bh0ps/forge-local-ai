"""Goal completion is independently checked, repaired and resumable."""
import json
import threading
import time

import pytest

from forge_goal_review import GoalReview, parse_verdict
from test_forge_core import Engine, service, call, finished


class Reviewer:
    remote_inference=True
    def __init__(self,handler): self.handler=handler; self.requests=[]
    def capabilities(self,model): return {'capabilities':['tools','structured_outputs'],'model_info':{'remote.context_length':131072}}
    def estimate_tokens(self,messages,tools): return 100
    def generate(self,data,cancel):
        self.requests.append(data)
        response=self.handler(data,cancel)
        if isinstance(response,Exception): raise response
        if isinstance(response,str): response={'content':response}
        yield {'message':response,'done':True,'done_reason':'stop','eval_count':40,'prompt_eval_count':400,'eval_duration':1000000000}


def packet(data):
    request=next(m['content'] for m in data['messages'] if m['role']=='user' and m['content'].startswith('Review this implementation evidence.'))
    return json.loads(request.split('\n',1)[1])


def verdict(data,decision='complete',feedback=None,ids=None):
    p=packet(data)
    evidence=ids or [p['candidate_answer']['id']]
    return json.dumps({'verdict':decision,'summary':'The fixture is verified.' if decision=='complete' else 'The output file is missing.',
                      'feedback':feedback or ([] if decision=='complete' else ['Create the requested file and read it back.']),
                      'verified_tasks':[{'task_id':t['id'],'evidence_ids':evidence} for t in p['tasks']] if decision=='complete' else []})


def environment(tmp_path,handler,engine=None):
    svc=service(tmp_path,engine or Engine()); remote=Reviewer(handler)
    svc.store.save_entity('providers',{'id':'openrouter','kind':'openrouter','url':'https://openrouter.ai/api/v1',
        'enabled':True,'remote_consent':True,'credential_ref':'synthetic-vault-ref'})
    original=svc.providers.provider
    svc.providers.provider=lambda identifier:remote if identifier=='openrouter' else original(identifier)
    svc.store.update_settings({'goal_review_enabled':True,'memory_suggestions':False})
    return svc,remote


def complete_tasks(svc,goal):
    def response(data,cancel):
        current=svc.store.goal(goal['id'])
        yield {'message':{'tool_calls':[call('goal_update',{'tasks':[{**t,'status':'completed','evidence':['Fixture evidence.']} for t in current['tasks']],
            'checkpoint':'Implemented.','next_action':'Verify.'})]},'done':True,'done_reason':'stop'}
    return response


def test_goal_is_independently_verified_and_usage_is_separate(tmp_path):
    svc,remote=environment(tmp_path,lambda data,cancel:verdict(data))
    try:
        goal=svc.goal_create({'text':'Write a brief greeting.','tasks':[{'text':'Compose the greeting.'}]})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello, world.'}]
        run=svc.jobs.start({'text':'Compose this greeting.','mode':'goal','goal_id':goal['id']})
        assert finished(svc,run)['status']=='completed'
        saved=svc.store.goal(goal['id'])
        assert saved['status']=='completed' and saved['review']['status']=='complete'
        child=svc.store.run(saved['review']['review_run_id'])
        assert child['parent_id']==run['id'] and child['mode']=='goal_review' and child['readonly']
        assert child['settings']['provider_id']=='openrouter' and not child['settings']['auto_delegate']
        assert not child['settings']['memory_enabled'] and not child['settings']['memory_suggestions']
        assert len(remote.requests)==1 and remote.requests[0]['response_format']['type']=='json_schema'
        identifiers=remote.requests[0]['response_format']['json_schema']['schema']['properties']['verified_tasks']['items']['properties']
        assert identifiers['task_id']['enum']==[t['id'] for t in saved['tasks']]
        assert identifiers['evidence_ids']['items']['enum']==[packet(remote.requests[0])['candidate_answer']['id']]
        assert not {'write_file','run_command','goal_update','delegate_agent','request_user_input'}&{s['function']['name'] for s in remote.requests[0]['tools']}
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM usage WHERE purpose='goal_review'").fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==3
        assert packet(remote.requests[0])['objective']=='Write a brief greeting.'
        assert packet(remote.requests[0])['initial']['tasks'][0]['status']=='pending'
    finally: svc.shutdown()


def test_review_fixes_return_to_local_agent_and_write_executes_once(tmp_path):
    holder={}; remote_rounds=[]
    def handler(data,cancel):
        remote_rounds.append(data)
        if len(remote_rounds)==1: return verdict(data,'needs_changes')
        if len(remote_rounds)==2: return {'tool_calls':[call('read_file',{'path':'result.txt'})]}
        result=json.loads(next(m['content'] for m in reversed(data['messages']) if m['role']=='tool'))
        assert 'verified output' in json.dumps(result)
        return verdict(data,ids=[result['artifact']])
    svc,remote=environment(tmp_path,handler)
    try:
        folder=tmp_path/'project'; folder.mkdir(); project=svc.create_project({'name':'Fixture','path':str(folder)})
        svc.store.update_settings({'permission_profile':'full_access'})
        goal=svc.goal_create({'text':'Create result.txt with verified output.','project_id':project['id'],'tasks':[{'text':'Write result.txt.'}]})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Created the file.'},
            {'tool_calls':[call('write_file',{'path':'result.txt','content':'verified output'})]},complete_tasks(svc,goal),{'content':'Created and verified the file.'}]
        run=svc.jobs.start({'text':'Implement the goal.','mode':'goal','goal_id':goal['id'],'project_id':project['id']})
        assert finished(svc,run)['status']=='completed'
        assert (folder/'result.txt').read_text()=='verified output'
        final_schema=remote.requests[-1]['response_format']['json_schema']['schema']['properties']['verified_tasks']['items']['properties']
        assert result_artifact(remote.requests[-1]) in final_schema['evidence_ids']['items']['enum']
        assert svc.store.run(run['id'])['review_revisions']==1
        assert len(svc.store.goal(goal['id'])['tasks'])==2
        assert any('Create the requested file' in m['content'] for request in svc.core.requests[2:] for m in request['messages'])
        with svc.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE name='write_file'").fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM usage WHERE purpose='goal_review'").fetchone()[0]==3
    finally: svc.shutdown()


def result_artifact(data):
    return json.loads(next(m['content'] for m in reversed(data['messages']) if m['role']=='tool'))['artifact']


@pytest.mark.parametrize('reply',['Done.','```json\n{}\n```',json.dumps({'verdict':'complete','summary':'Done','feedback':[],'verified_tasks':[]})])
def test_invalid_or_unsupported_completion_pauses_and_does_not_retry(tmp_path,reply):
    svc,remote=environment(tmp_path,lambda data,cancel:reply)
    try:
        goal=svc.goal_create({'text':'Compose a greeting.'})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        run=svc.jobs.start({'text':'Do the goal.','goal_id':goal['id']})
        assert finished(svc,run)['status']=='paused'
        assert svc.store.goal(goal['id'])['status']=='paused'
        assert svc.store.goal(goal['id'])['review']['status']=='error'
        assert len(remote.requests)==1 and svc.store.run(run['id'])['review_pending']
        remote.handler=lambda data,cancel:verdict(data)
        svc.jobs.resume(run['id'])
        assert finished(svc,run)['status']=='completed'
        assert len(remote.requests)==2
        assert len(svc.core.requests)==2,'Retry review without rerunning implementation.'
    finally: svc.shutdown()


def test_quota_failure_pauses_and_explicit_resume_continues_same_review(tmp_path):
    svc,remote=environment(tmp_path,lambda data,cancel:ValueError('OpenRouter free quota limited.'))
    try:
        goal=svc.goal_create({'text':'Compose a greeting.'})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        run=svc.jobs.start({'text':'Do the goal.','goal_id':goal['id']})
        assert finished(svc,run)['status']=='paused'
        review_id=svc.store.goal(goal['id'])['review']['review_run_id']
        assert len(remote.requests)==1
        remote.handler=lambda data,cancel:verdict(data)
        svc.jobs.resume(run['id'])
        assert finished(svc,run)['status']=='completed'
        assert svc.store.goal(goal['id'])['review']['review_run_id']==review_id
        assert len(svc.core.requests)==2 and len(remote.requests)==2
    finally: svc.shutdown()


def test_returned_review_cannot_approve_after_consent_revocation_and_is_not_replayed(tmp_path):
    holder={}
    def handler(data,cancel):
        svc=holder['service'];config=svc.store.entity('providers','openrouter')
        svc.store.save_entity('providers',{**config,'remote_consent':False})
        return verdict(data)
    svc,remote=environment(tmp_path,handler);holder['service']=svc
    try:
        goal=svc.goal_create({'text':'Compose a greeting.'})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        run=svc.jobs.start({'text':'Do the goal.','goal_id':goal['id']})
        assert finished(svc,run)['status']=='paused'
        saved=svc.store.run(run['id']);assert saved['review']['status']=='error'
        assert not saved['review_candidate'].get('invalid') and len(remote.requests)==1
        assert not any((r.get('producer_type')=='independent_review' and r.get('passed')) for r in
                       saved.get('verification_feedback',{}).get('receipts',{}).values())
        config=svc.store.entity('providers','openrouter')
        svc.store.save_entity('providers',{**config,'remote_consent':True})
        svc.jobs.resume(run['id'])
        assert finished(svc,run)['status']=='completed'
        assert len(remote.requests)==1,'A completed cloud response was replayed after consent was restored.'
    finally:svc.shutdown()


def test_parent_stop_cancels_independent_review_without_completing_goal(tmp_path):
    entered=threading.Event()
    def handler(data,cancel):
        entered.set(); cancel.wait(10)
        return verdict(data)
    svc,remote=environment(tmp_path,handler)
    try:
        goal=svc.goal_create({'text':'Compose a greeting.'})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        run=svc.jobs.start({'text':'Do the goal.','goal_id':goal['id']})
        assert entered.wait(10)
        svc.jobs.cancel(run['id'],pause=True)
        assert finished(svc,run)['status']=='paused'
        assert svc.store.goal(goal['id'])['status']=='paused'
        child=next(r for r in svc.store.runs() if r.get('parent_id')==run['id'])
        assert finished(svc,child)['status']=='paused'
        assert not any(e['type']=='complete' for e in svc.jobs.poll(run['id'])['events'])
    finally: svc.shutdown()


def test_repeated_review_rejections_pause_at_configured_limit(tmp_path):
    svc,remote=environment(tmp_path,lambda data,cancel:verdict(data,'needs_changes'))
    try:
        svc.store.update_settings({'goal_review_max_revisions':1})
        goal=svc.goal_create({'text':'Compose a greeting.'})
        svc.core.rounds=[complete_tasks(svc,goal),{'content':'Hello.'}]
        run=svc.jobs.start({'text':'Do the goal.','goal_id':goal['id']})
        assert finished(svc,run)['status']=='paused'
        assert 'revision limit' in svc.store.run(run['id'])['recovery']
        assert len(remote.requests)==1 and len(svc.store.goal(goal['id'])['tasks'])==2
    finally: svc.shutdown()
