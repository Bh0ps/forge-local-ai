"""Scoped cloud fixtures never use personal state, credentials or inference."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from forge_ai_workflow import AIWorkflow
from forge_cloud_policy import CloudContextPolicy, CloudPolicyRejected, safe_relative, scrub
from forge_collaboration import CloudCollaboration, setup_specialists
from forge_goal_review import assignment_files
from test_forge50_collaboration import setup


def goal_run(svc,project_id=None):
    goal=svc.goal_create({'text':'Implement the accepted fixture.','project_id':project_id,
                         'tasks':[{'id':'stable','text':'Create the exact fixture.'}]})
    run=svc.jobs.start({'text':'Implement it.','mode':'goal','project_id':project_id,'goal_id':goal['id']})
    return svc.store.run(run['id'])


def test_ordinary_goal_without_project_plans_once_and_keeps_task_ids(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc);run=goal_run(svc);calls=[]
    def reject(data):calls.append(data);raise ValueError('Free quota unavailable')
    monkeypatch.setattr(svc,'agent_start',reject)
    collaboration=CloudCollaboration(svc)
    try:
        updated=collaboration.prepare(run,threading.Event())
        assert updated['planner_assignment']['state']=='unavailable'
        assert updated['planner_assignment']['scope_revision']==svc.store.goal(run['goal_id'])['scope_revision']
        assert type(updated['planner_assignment']['scope_revision']) is int
        first_key=updated['planner_assignment']['key']
        assert calls[0]['project_id'] is None and calls[0]['cloud_scope']['files']==[]
        assert '"id": "stable"' in calls[0]['text']
        legacy=svc.store.update_run(run['id'],planner_assignment={**updated['planner_assignment'],'scope_revision':first_key})
        restored=collaboration.prepare(legacy,threading.Event())
        assert restored['planner_assignment']['scope_revision']==updated['planner_assignment']['scope_revision']
        assert restored['planner_assignment']['key']==first_key
        goal=svc.store.goal(run['goal_id'])
        svc.store.save_goal({**goal,'tasks':[{**goal['tasks'][0],'status':'completed','evidence':['Progress only']}]})
        collaboration.prepare(svc.store.run(run['id']),threading.Event())
        assert len(calls)==1
        assert svc.store.run(run['id'])['planner_assignment']['key']==first_key
        svc.store.save_goal({**goal,'request':'Accepted objective changed.'})
        collaboration.prepare(svc.store.run(run['id']),threading.Event())
        assert len(calls)==2
        assignment=svc.store.run(run['id'])['planner_assignment']
        assert assignment['key']!=first_key and assignment['scope_revision']==svc.store.goal(goal['id'])['scope_revision']
    finally:svc.shutdown()


def test_initial_planning_is_nonblocking_and_effect_wait_uses_one_deadline(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc);run=goal_run(svc)
    collaboration=CloudCollaboration(svc)
    calls=[]
    def pending(parent,args,cancel):calls.append(args['wait_seconds']);return {'finished':False}
    monkeypatch.setattr(svc,'agent_result',pending)
    try:
        waiting=collaboration.prepare(run,threading.Event())
        assert waiting['planner_assignment']['state']=='waiting' and calls==[0]
        saved=waiting['planner_assignment']
        assert 58<saved['deadline_epoch']-__import__('time').time()<=60
        svc.store.update_run(run['id'],planner_assignment={**saved,'deadline_epoch':0})
        completed=collaboration.await_implementation(waiting,threading.Event())
        assert completed['planner_assignment']['state']=='unavailable'
        assert not completed.get('planner_guidance')
        assert svc.store.run(saved['run_id'])['advisory']
        assert len([r for r in svc.store.runs() if r.get('parent_id')==run['id']])==1
    finally:svc.shutdown()


def test_successful_planner_consumed_with_identity_and_file_edit_does_not_replan(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc)
    root=tmp_path/'project';root.mkdir();(root/'README.md').write_text('Existing framework')
    project=svc.create_project({'path':str(root)});run=goal_run(svc,project['id'])
    monkeypatch.setattr(svc,'agent_result',lambda *a,**k:{'finished':True,'successful':True,
        'response':json.dumps({'tasks':[{'id':'stable','text':'Implement exact fixture'}],'design':{},'risks':[],'questions':[]})})
    try:
        planner=CloudCollaboration(svc);saved=planner.prepare(run,threading.Event())
        assert saved['planner_assignment']['state']=='consumed'
        assert len(saved['planner_assignment']['guidance_hash'])==64
        assert planner.guidance(saved)
        blocked=planner.before_implementation(saved,threading.Event())
        assert blocked['not_executed'] and 'after this action was drafted' in blocked['error']
        supplied=svc.store.update_run(run['id'],context_snapshot={'cloud_guidance':{
            'guidance_hash':saved['planner_assignment']['guidance_hash'],
            'assignment_id':saved['planner_assignment']['key'],'child_run_id':saved['planner_assignment']['run_id']}})
        assert not planner.before_implementation(supplied,threading.Event()).get('not_executed')
        (root/'README.md').write_text('Facts changed')
        stale=planner.prepare(saved,threading.Event())
        assert stale['planner_assignment']['state']=='unavailable' and not planner.guidance(stale)
        planner.prepare(stale,threading.Event())
        assert len([r for r in svc.store.runs() if r.get('parent_id')==run['id']])==1
    finally:svc.shutdown()


@pytest.mark.parametrize('path',['.env','.env.local','../private.txt','C:/private.txt','.git/config','secrets/key.txt','config.json','folder/id_rsa','folder/private.pem'])
def test_protected_resource_names_are_excluded(path):
    assert safe_relative(path) is None


def test_cloud_round_rebuild_excludes_private_history_guidance_memory_and_secrets(tmp_path):
    svc=setup(tmp_path);policy=CloudContextPolicy(svc)
    try:
        chat=svc.store.create_chat(None)
        svc.store.add_message(chat['id'],'user','PRIVATE_OLD_CHAT')
        with policy.admission({'provider_id':'openrouter','model':'openrouter/free','text':'Synthetic task',
            'chat_id':chat['id'],'instructions':'Readonly specialist','cloud_scope':{'files':[]},
            'memory_enabled':True,'memory_suggestions':True,'skills':['private-skill'],'space_ids':['private-space']}) as data:
            run=svc.jobs.start(data)
        saved=svc.store.run(run['id'])
        assert saved['settings']['memory_enabled'] is False and saved['settings']['memory_suggestions'] is False
        assert saved['skills']==[] and saved['space_ids']==[]
        saved['cloud_scope']={'version':1,'files':[],'artifacts':[],'attachments':[],'goal_id':None,'web':False}
        prepared,schemas=policy.prepare_round(saved,[{'role':'system','content':'PRIVATE_GUIDANCE PRIVATE_MEMORY'}],[])
        assert 'PRIVATE' not in json.dumps(prepared)
        assert 'Synthetic task' in json.dumps(prepared)
        assert 'sk-or-v1-12345678901234567890' not in scrub('api_key=sk-or-v1-12345678901234567890')
        assert 'private-value-123' not in scrub('{"secret_key":"private-value-123"}')
        assert 'private-value-123' not in scrub({"client_secret":"private-value-123"}).values()
    finally:svc.shutdown()


def test_cloud_scoped_reads_enumeration_artifacts_and_permission_recheck(tmp_path):
    svc=setup(tmp_path);policy=CloudContextPolicy(svc)
    root=tmp_path/'project';root.mkdir()
    (root/'assigned.py').write_text('value = "fixture"\n');(root/'unrelated.py').write_text('PRIVATE_UNRELATED')
    (root/'.env').write_text('PRIVATE_SECRET')
    project=svc.create_project({'path':str(root)});run=goal_run(svc,project['id'])
    artifact=svc.store.artifact({'data':'Assigned result'});private=svc.store.artifact({'data':'PRIVATE'})
    run={**run,'settings':{**run['settings'],'provider_id':'openrouter'},'cloud_scope':{
        'version':1,'files':['assigned.py'],'artifacts':[artifact],'attachments':[], 'goal_id':run['goal_id'],'web':False}}
    try:
        listing=policy.execute_scoped(run,'list_files',{'path':'.','recursive':True})
        assert [f['path'] for f in listing['entries']]==['assigned.py']
        result=policy.execute_scoped(run,'search_files',{'query':'fixture'})
        assert result['matches'][0]['path']=='assigned.py'
        assert 'PRIVATE' not in json.dumps(result)
        for name,args in [('read_file',{'path':'unrelated.py'}),('read_file',{'path':'.env'}),
                          ('artifact_read',{'id':private}),('web_fetch',{'url':'https://example.com'}),
                          ('memory_search',{'query':'all'}),('run_command',{'command':'echo no'})]:
            with pytest.raises(CloudPolicyRejected):policy.guard(run,name,args)
        assert 'Assigned result' in policy.execute_scoped(run,'artifact_read',{'id':artifact})['text']
        config=svc.store.entity('providers','openrouter');svc.store.save_entity('providers',{**config,'remote_consent':False})
        with pytest.raises(CloudPolicyRejected,match='consent'):policy.prepare_round(run,[],[])
    finally:svc.shutdown()


def test_shared_assignment_cap_includes_reviewer_other_parents_and_reused_identity(tmp_path):
    svc=setup(tmp_path);policy=CloudContextPolicy(svc)
    base={'provider_id':'openrouter','model':'openrouter/free','text':'Synthetic assignment','cloud_scope':{}}
    try:
        ids=[]
        for index in range(2):
            with policy.admission({**base,'source_key':'scope-'+str(index),'mode':'goal_review' if index else 'chat'}) as data:
                ids.append(svc.jobs.start(data)['id'])
        with pytest.raises(CloudPolicyRejected,match='Two cloud'):
            with policy.admission(base):pass
        with policy.admission({**base,'source_key':'scope-0'}) as data:
            assert svc.jobs.start(data)['id']==ids[0]
        svc.store.update_run(ids[0],status='completed')
        with policy.admission(base):pass
    finally:svc.shutdown()


def test_status_and_setup_preserve_profile_edits_settings_and_do_not_claim_workflow_verification(tmp_path):
    svc=setup(tmp_path);workflow=AIWorkflow(svc)
    svc.store.update_settings({'model':'existing-local-model','context':8192,'memory_enabled':True})
    config=svc.store.entity('providers','openrouter')
    svc.store.save_entity('providers',{**config,'data_collection':'allow','connected':True,
        'account':{'free_model_daily_requests':{'remaining':1000}},'metadata_auth':{'state':'verified','checked_at':'synthetic-time'},
        'models':[{'name':'openrouter/free'}],'catalog_verified_at':'synthetic-time'})
    try:
        before=workflow.status()
        assert before['authentication']['state']=='verified' and before['workflow']['state']=='unverified'
        result=workflow.setup({'goal_guidance':True})
        assert len(result['created'])==6
        svc.agent_save({'id':'openrouter-design','instructions':'My edited design policy','enabled':False})
        again=workflow.setup({'goal_guidance':False})
        assert not again['created'] and len(again['preserved'])==6
        assert svc.store.entity('agents','openrouter-design')['instructions']=='My edited design policy'
        assert not svc.store.entity('agents','openrouter-design')['enabled']
        assert again['settings']['model']=='existing-local-model' and again['settings']['context']==8192
        assert svc.store.entity('providers','openrouter')['data_collection']=='allow'
        assert workflow.status()['profiles']['state']=='incomplete'
        assert workflow.status()['workflow']['state']=='unverified'
    finally:svc.shutdown()


def test_self_test_receipt_requires_guidance_actually_supplied_not_saved_assignment_only(tmp_path):
    svc=setup(tmp_path);run=goal_run(svc);workflow=AIWorkflow(svc)
    try:
        run=svc.store.update_run(run['id'],status='completed',planner_assignment={'state':'consumed','run_id':'missing','key':'scope','guidance_hash':'digest'})
        # A saved label is not proof that guidance reached the local model.
        receipt=workflow._receipt(run)
        assert not receipt['passed'] and not receipt['checks']['guidance_supplied']
    finally:svc.shutdown()


def test_interrupted_self_test_is_not_replayed_on_restart(tmp_path):
    svc=setup(tmp_path)
    try:
        old=svc.store.save_entity('ai_workflow_tests',{'state':'running','created_at':'synthetic','runs':[]})
        workflow=AIWorkflow(svc)
        assert workflow.self_test_status(old['id'])['state']=='interrupted'
        assert not workflow.jobs and not svc.store.runs()
    finally:svc.shutdown()


def test_assignment_files_excludes_denied_pending_and_non_file_arguments(tmp_path):
    svc=setup(tmp_path);run=goal_run(svc)
    try:
        entries=[('allowed','read_file',{'path':'allowed.py'},{'ok':True,'path':'allowed.py'}),
                 ('denied','read_file',{'path':'private.py'},{'not_executed':True,'error':'denied'}),
                 ('generic','web_fetch',{'path':'unrelated.py'},{'text':'not a file invocation'}),
                 ('secret','read_file',{'path':'.env'},{'ok':True,'content':'PRIVATE'}),
                 ('multi','read_files',{'files':[{'path':'partial.py'},{'path':'failed.py'}]},
                  {'files':[{'path':'partial.py','ok':True},{'path':'failed.py','ok':False,'error':'denied'}]})]
        for identifier,name,args,result in entries:
            svc.store.invocation(identifier,run['id'],name,args)
            svc.store.invocation_state(identifier,'completed',{'artifact':'synthetic','result':result})
        svc.store.invocation('pending',run['id'],'read_file',{'path':'pending.py'})
        assert assignment_files(svc.store,{run['id']})==['allowed.py','partial.py']
    finally:svc.shutdown()


def test_resume_admission_rechecks_cap_consent_and_memory_protection(tmp_path):
    svc=setup(tmp_path);policy=CloudContextPolicy(svc)
    base={'provider_id':'openrouter','model':'openrouter/free','text':'Synthetic assignment','cloud_scope':{}}
    try:
        runs=[]
        for _ in range(2):
            with policy.admission(base) as data:runs.append(svc.jobs.start(data)['id'])
        first=svc.store.update_run(runs[0],status='paused')
        with policy.admission(base) as data:third=svc.jobs.start(data)
        with pytest.raises(CloudPolicyRejected,match='Two cloud'):
            with policy.resume_admission(first):pass
        svc.store.update_run(third['id'],status='completed')
        candidate={**first,'settings':{**first['settings'],'memory_enabled':True,'memory_suggestions':True},'readonly':False}
        with policy.resume_admission(candidate) as protected:
            assert protected['readonly'] and not protected['settings']['memory_enabled'] and not protected['settings']['memory_suggestions']
        config=svc.store.entity('providers','openrouter');svc.store.save_entity('providers',{**config,'remote_consent':False})
        with pytest.raises(CloudPolicyRejected,match='consent'):
            with policy.resume_admission(first):pass
    finally:svc.shutdown()


def test_scoped_read_requires_matching_approval_for_ask_and_current_deny_wins(tmp_path,monkeypatch):
    from forge_store import encode
    from hashlib import sha256
    svc=setup(tmp_path);policy=CloudContextPolicy(svc)
    folder=tmp_path/'project';folder.mkdir();(folder/'assigned.py').write_text('approved fixture')
    project=svc.create_project({'path':str(folder)});local=goal_run(svc,project['id'])
    run={**local,'settings':{**local['settings'],'provider_id':'openrouter'},'cloud_scope':{
        'version':1,'files':['assigned.py'],'artifacts':[],'attachments':[],'goal_id':local['goal_id'],'web':False}}
    args={'path':'assigned.py'}
    try:
        monkeypatch.setattr(svc.jobs.registry,'permission',lambda *a,**kw:'ask')
        policy.prepare_round(run,[],[])  # No new file is read while preparing.
        with pytest.raises(CloudPolicyRejected):policy.execute_scoped(run,'read_file',args)
        svc.store.invocation('approved-read',run['id'],'read_file',args)
        svc.store.invocation_state('approved-read','running')
        action={'tool':'read_file','arguments':args,'target':str(folder)}
        with svc.store._connection(transaction='write') as db:
            db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',('approval',run['id'],'approved-read',sha256(encode(action).encode()).hexdigest(),'approved',encode(action)))
        assert 'approved fixture' in policy.execute_scoped(run,'read_file',args,invocation_id='approved-read')['content']
        with pytest.raises(CloudPolicyRejected):policy.execute_scoped(run,'read_file',{'path':'assigned.py','start_line':2},invocation_id='approved-read')
        monkeypatch.setattr(svc.jobs.registry,'permission',lambda *a,**kw:'deny')
        with pytest.raises(CloudPolicyRejected):policy.execute_scoped(run,'read_file',args,invocation_id='approved-read')
        with pytest.raises(CloudPolicyRejected):policy.prepare_round(run,[],[])
    finally:svc.shutdown()


def test_workflow_receipt_requires_the_actual_owned_fixture_command(tmp_path):
    import sys
    svc=setup(tmp_path);workflow=AIWorkflow(svc)
    try:
        folder,project,goal,launch,_,expected=workflow._fixture('synthetic-test','ordinary')
        (folder/'proof.csv').write_text(expected,encoding='utf-8')
        fixture=svc.store.entity('ai_workflow_fixtures',project['id'])
        svc.store.save_entity('ai_workflow_fixtures',{**fixture,'python':sys.executable})
        run=svc.jobs.start({'text':goal['request'],'goal_id':goal['id'],'project_id':project['id'],'mode':'goal'})
        read=svc.store.invocation('read-proof',run['id'],'read_file',{'path':'proof.csv'})
        svc.store.invocation_state('read-proof','completed',{'artifact':'synthetic','result':{'ok':True,'content':expected}})
        assert not workflow._receipt(svc.store.run(run['id']))['checks']['local_checks']
        command=svc.command_sessions.start(folder,[sys.executable,'check_fixture.py'],run_id=run['id'],max_seconds=10)
        completed=svc.command_sessions.wait(command['id'],timeout=5)
        assert completed['status']=='completed' and completed['exit_code']==0
        svc.store.invocation('checked-proof',run['id'],'command_wait',{'id':command['id']})
        svc.store.invocation_state('checked-proof','completed',{'artifact':'synthetic','result':completed})
        assert workflow._receipt(svc.store.run(run['id']))['checks']['local_checks']
        (folder/'check_fixture.py').write_text("print('Changed oracle')\n",encoding='utf-8')
        assert not workflow._receipt(svc.store.run(run['id']))['checks']['local_checks']
    finally:svc.shutdown()


def test_failed_receipt_stays_readable_without_top_level_transport_error(tmp_path):
    svc=setup(tmp_path)
    try:
        workflow=AIWorkflow(svc)
        record=svc.store.save_entity('ai_workflow_tests',{'state':'failed','error':'Cloud quota unavailable','checks':[{'kind':'ordinary','passed':False}]})
        public=workflow.self_test_status(record['id'])
        assert 'error' not in public and public['failure_reason']=='Cloud quota unavailable'
        assert public['checks']==record['checks'] and svc.store.entity('ai_workflow_tests',record['id'])['error']
    finally:svc.shutdown()


def test_planner_result_after_consent_revocation_is_never_supplied(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc);run=goal_run(svc)
    def returned(*args,**kwargs):
        config=svc.store.entity('providers','openrouter')
        svc.store.save_entity('providers',{**config,'remote_consent':False})
        return {'finished':True,'successful':True,'response':'{"tasks":[],"design":{},"risks":[],"questions":[]}'}
    monkeypatch.setattr(svc,'agent_result',returned)
    try:
        result=CloudCollaboration(svc).prepare(run,threading.Event())
        assert result['planner_assignment']['state']=='unavailable' and not result.get('planner_guidance')
        assert 'before guidance could be supplied' in result['planner_assignment']['reason']
    finally:svc.shutdown()


def test_explicit_replan_preserves_scope_tasks_and_deduplicates_without_inference(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc);run=goal_run(svc)
    try:
        planner=CloudCollaboration(svc);before=svc.store.goal(run['goal_id'])
        old_key=planner.scope_packet(run)[1]
        svc.store.update_run(run['id'],status='paused',planner_assignment={'key':old_key,'state':'unavailable','reason':'Saved failure'},planner_guidance='Old advice')
        monkeypatch.setattr(svc,'agent_start',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No inference before Resume.')))
        result=planner.replan(run['id'],before['revision'],'explicit-request')
        saved=svc.store.run(run['id']);after=svc.store.goal(run['goal_id'])
        assert result['state']=='requested' and result['planner_generation']==1
        assert result['planner_assignment']['scope_revision']==before['scope_revision']
        assert saved['status']=='paused' and not saved.get('planner_guidance')
        assert saved['planner_assignment_history'][0]['key']==old_key
        assert planner.scope_packet(saved)[1]!=old_key
        assert after['tasks']==before['tasks'] and after['scope_revision']==before['scope_revision'] and after['revision']==before['revision']
        assert planner.replan(run['id'],before['revision'],'explicit-request')['reused']
        assert svc.store.run(run['id'])['planner_generation']==1
        with pytest.raises(ValueError,match='already belongs'):
            planner.replan(run['id'],before['revision']+1,'explicit-request')
        with pytest.raises(ValueError,match='Goal changed'):
            planner.replan(run['id'],before['revision']+1,'new-request')
        svc.store.invocation('unknown-action',run['id'],'write_file',{'path':'proof.txt'})
        svc.store.invocation_state('unknown-action','outcome_unknown',{'error':'Inspect before continuing'})
        with pytest.raises(ValueError,match='outcome-unknown'):
            planner.replan(run['id'],before['revision'],'unknown-request')
    finally:svc.shutdown()


def test_ordinary_goal_guidance_control_is_independent_of_manual_delegation(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc)
    svc.store.update_settings({'auto_delegate':False})
    goal=svc.goal_create({'text':'Implement one fixture.','tasks':[{'id':'stable','text':'Implement the fixture.'}]})
    started=svc.jobs.start({'text':'Continue the accepted goal.','goal_id':goal['id']})
    calls=[]
    def reject(data):calls.append(data);raise ValueError('Synthetic unavailable')
    monkeypatch.setattr(svc,'agent_start',reject)
    try:
        result=CloudCollaboration(svc).prepare(svc.store.run(started['id']),threading.Event())
        assert len(calls)==1 and calls[0]['agent_id']=='openrouter-planner'
        assert result['planner_assignment']['state']=='unavailable'
    finally:svc.shutdown()
