"""Review receipt privacy/currentness gates with real isolated journals, no inference."""
from copy import deepcopy
import hashlib
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from forge_goal_review import (GoalReview,assignment_files,current_review_evidence_ids,
    local_assignment_files,parse_verdict,review_response_format)
from forge_store import encode
from project_tools import ProjectTools
from test_forge50_collaboration import setup


PRIVATE='SYNTHETIC_PRIVATE_RECEIPT_CONTENT'


@pytest.fixture
def receipt_case(tmp_path,monkeypatch):
    svc=setup(tmp_path)
    svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,
        'memory_suggestions':False,'goal_review_enabled':True,'goal_cloud_guidance':False})
    previous=svc.providers.provider
    monkeypatch.setattr(svc.providers,'provider',lambda name:SimpleNamespace(
        capabilities=lambda model:{'capabilities':['tools','structured_outputs']})
        if name=='openrouter' else previous(name))
    root=tmp_path/'project';root.mkdir()
    (root/'public.js').write_text('export const selector = "#safe-button";\n',encoding='utf-8')
    project=svc.create_project({'name':'Disposable receipt fixture','path':str(root)})
    goal=svc.goal_create({'text':'Provide the exact accepted result.','project_id':project['id'],
        'tasks':[{'id':'owned-task','text':'Provide the exact accepted result.','status':'completed',
                  'evidence':['The local candidate is ready.']}]})
    accepted=svc.jobs.start({'text':goal['request'],'goal_id':goal['id'],'project_id':project['id'],'mode':'goal'})
    run=svc.store.update_run(accepted['id'],execution_mode='guided',status='running',rounds=1)
    yield svc,run,root
    assert not svc.core.requests
    svc.shutdown()


def file_result(case,name,args):
    svc,run,root=case
    result=ProjectTools(root,svc.store.home/'backups').execute(name,args,threading.Event())
    assert result.get('ok') is not False,result
    artifact=svc.store.artifact({'name':name,'arguments':args,'result':result})
    identifier=run['id']+':receipt:'+artifact
    svc.store.invocation(identifier,run['id'],name,args)
    svc.store.invocation_state(identifier,'completed',{'artifact':artifact,'result':result})
    return artifact


def verdict(evidence):
    return {'verdict':'complete','summary':'The exact task was verified.','feedback':[],
        'verified_tasks':[{'task_id':'owned-task','evidence_ids':[evidence]}]}


def saved_child(svc,run,packet,proof,*,cached=False):
    packet_id=svc.store.artifact(packet)
    artifacts=[record['artifact'] for record in packet.get('evidence',[]) if record.get('artifact')]
    child=svc.jobs.start({'text':'Inspect the assigned safe evidence.','provider_id':'openrouter',
        'model':'openrouter/free','parent_id':run['id'],'project_id':run['project_id'],'goal_id':run['goal_id'],
        'mode':'goal_review','readonly':True,'agent_tools':['read_file','artifact_read','goal_read'],
        'review_artifacts':artifacts+[packet_id],'review_evidence_ids':artifacts+[proof],
        'cloud_scope':{'files':assignment_files(svc.store,{run['id']}),'artifacts':artifacts+[packet_id],
            'attachments':[],'goal_id':run['goal_id'],'web':False}})
    value=verdict(proof)
    svc.store.add_message(child['chat_id'],'assistant',encode(value))
    child=svc.store.update_run(child['id'],status='completed',result_consumed=True)
    pending={'fingerprint':hashlib.sha256(encode(packet).encode()).hexdigest(),'answer':PRIVATE,
        'attempt':1,'review_run_id':child['id'],'evidence_artifact':packet_id,
        'workflow_snapshot':svc.jobs.workflow.review_snapshot(run,local_assignment_files(svc.store,{run['id']}))}
    if cached:pending.update(verdict=value,revision=1,review={'status':'complete'})
    run=svc.store.update_run(run['id'],review_candidate=pending,review_pending=True)
    return run,child,pending,value


@pytest.mark.parametrize('source',['memory','private-file'])
@pytest.mark.parametrize('required',[False,True])
def test_excluded_only_candidate_is_unavailable_before_cloud_admission(receipt_case,monkeypatch,source,required):
    svc,run,_=receipt_case
    if source=='memory':run=svc.store.update_run(run['id'],private_memory_supplied=True)
    else:file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    if required:
        run=svc.store.update_run(run['id'],settings={**run['settings'],'cloud_required_review':True})
    admissions=[]
    monkeypatch.setattr(svc.jobs,'start',lambda data:admissions.append(data))
    reviewer=GoalReview(svc.jobs)
    _,packet,_,artifacts=reviewer.snapshot(run,PRIVATE)
    assert not artifacts and 'id' not in packet['candidate_answer']
    assert packet['candidate_answer']['available'] is False and PRIVATE not in encode(packet)
    with pytest.raises(ValueError,match='no safe scoped evidence'):
        reviewer.check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic())
    assert not admissions
    stored=svc.store.run(run['id'])
    assert not stored['review_pending'] and stored['review']['status']=='unavailable'
    assert svc.jobs.workflow.completion_issues(stored)
    assert svc.store.goal(run['goal_id'])['status']!='completed'
    assert reviewer.required(stored)==required


def test_saved_placeholder_id_is_excluded_from_schema_parser_and_receipt(receipt_case):
    svc,run,_=receipt_case
    public=file_result(receipt_case,'read_file',{'path':'public.js'})
    run=svc.store.update_run(run['id'],private_memory_supplied=True)
    goal,packet,_,_=GoalReview(svc.jobs).snapshot(run,PRIVATE)
    proof='answer:'+run['id']+':1'
    # Exact old packet shape: exclusion notice retained an apparently valid ID.
    packet['candidate_answer'].pop('available')
    packet['candidate_answer']['id']=proof
    run,child,pending,value=saved_child(svc,run,packet,proof,cached=True)
    allowed=current_review_evidence_ids(svc.store,child,svc.jobs.registry.permission)
    assert public in allowed and proof not in allowed
    schema=review_response_format(svc.store,child)
    enum=schema['json_schema']['schema']['properties']['verified_tasks']['items']['properties']['evidence_ids']['items']['enum']
    assert public in enum and proof not in enum
    with pytest.raises(ValueError,match='missing evidence'):parse_verdict(encode(value),goal['tasks'],set(allowed))
    with pytest.raises(ValueError,match='missing evidence'):svc.jobs.workflow.accept_review(run,child['id'],pending,value)
    assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']))
    assert not svc.store.entities('verification_reviews')
    # A restored saved complete verdict cannot bypass fresh packet identity.
    assert GoalReview(svc.jobs)._check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic(),resume=True)=='continue'
    assert svc.store.goal(run['goal_id'])['status']!='completed'


def test_restored_placeholder_only_complete_verdict_cannot_create_receipt(receipt_case):
    svc,run,_=receipt_case
    run=svc.store.update_run(run['id'],private_memory_supplied=True)
    _,packet,_,_=GoalReview(svc.jobs).snapshot(run,PRIVATE)
    proof='answer:'+run['id']+':1'
    packet['candidate_answer']={'id':proof,'text':packet['candidate_answer']['text']}
    run,child,pending,value=saved_child(svc,run,packet,proof,cached=True)
    with pytest.raises(ValueError,match='no safe scoped evidence'):review_response_format(svc.store,child)
    with pytest.raises(ValueError,match='missing evidence'):svc.jobs.workflow.accept_review(run,child['id'],pending,value)
    with pytest.raises(ValueError,match='no safe scoped evidence'):
        GoalReview(svc.jobs)._check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic(),resume=True)
    assert not svc.store.entities('verification_reviews')
    assert svc.jobs.workflow.progress(svc.store.run(run['id']))['verified_task_ids']==[]


def test_actual_protected_write_human_acceptance_is_local_and_external_edit_stales(receipt_case):
    svc,run,root=receipt_case
    private=file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    assert local_assignment_files(svc.store,{run['id']})==['.env']
    assert assignment_files(svc.store,{run['id']})==[]
    goal=svc.store.goal(run['goal_id'])
    accepted=svc.jobs.workflow.accept_human(run,['owned-task'],goal['revision'],
        'I inspected and accepted the private configuration locally.',[private])
    assert not svc.jobs.workflow.completion_issues(accepted)
    bundle=svc.store.entities('verification_reviews')[0]
    snapshot=bundle['snapshot']
    assert snapshot['source_contract']['required_sources']==['.env']
    assert snapshot['source_snapshot']['files']==[{'path':'.env','sha256':hashlib.sha256(PRIVATE.encode()).hexdigest()}]
    assert snapshot['source_snapshot']['files'][0]['sha256'] not in encode(GoalReview(svc.jobs).snapshot(accepted,PRIVATE)[1])
    (root/'.env').write_text('External edit after explicit acceptance.',encoding='utf-8')
    assert svc.jobs.workflow.completion_issues(accepted)
    assert svc.jobs.workflow.progress(svc.store.run(run['id']))['verified_task_ids']==[]


def test_cloud_snapshot_omits_protected_local_source_manifest(receipt_case,monkeypatch):
    svc,run,_=receipt_case
    public=file_result(receipt_case,'read_file',{'path':'public.js'})
    file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    goal=svc.store.goal(run['goal_id'])
    run=svc.jobs.workflow.accept_human(run,['owned-task'],goal['revision'],
        'I inspected the private source locally and accepted the exact task.')
    captured=[]
    def capture(data):captured.append(data);raise RuntimeError('OFFLINE_CAPTURE')
    monkeypatch.setattr(svc.jobs,'start',capture)
    with pytest.raises(RuntimeError,match='OFFLINE_CAPTURE'):
        GoalReview(svc.jobs)._check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic())
    child=captured[0]
    assert child['cloud_scope']['files']==['public.js']
    assert child['review_evidence_ids']==[public]
    private_hash=hashlib.sha256(PRIVATE.encode()).hexdigest()
    assert '.env' not in child['text'] and PRIVATE not in encode(child) and private_hash not in encode(child)
    snapshot=svc.store.run(run['id'])['review_candidate']['workflow_snapshot']
    assert set(snapshot['source_contract']['required_sources'])=={'public.js','.env'}


def test_safe_evidence_cannot_override_unverified_protected_work(receipt_case,monkeypatch):
    svc,run,_=receipt_case
    public=file_result(receipt_case,'read_file',{'path':'public.js'})
    file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    admissions=[]
    monkeypatch.setattr(svc.jobs,'start',lambda data:admissions.append(data))
    with pytest.raises(ValueError,match='Protected local sources require fresh registered task checks'):
        GoalReview(svc.jobs)._check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic())
    assert not admissions and not svc.store.entities('verification_reviews')
    assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']))


@pytest.mark.parametrize('producer',['human','registered-check'])
def test_cloud_review_of_safe_evidence_preserves_verified_private_work_and_currentness(receipt_case,monkeypatch,producer):
    from agent_runtime import tool_schema
    svc,run,root=receipt_case
    public=file_result(receipt_case,'read_file',{'path':'public.js'})
    file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    if producer=='human':
        goal=svc.store.goal(run['goal_id'])
        run=svc.jobs.workflow.accept_human(run,['owned-task'],goal['revision'],
            'I inspected and accepted the private result locally.')
    else:
        check={'passed':(root/'.env').read_text(encoding='utf-8')==PRIVATE,
               'checks':[{'name':'Private result matches','passed':True}]}
        schema=tool_schema('private_check','Host-registered private result verification',{})
        schema['verification_contract']={'id':'private-result-v1','task_ids':['owned-task'],
            'expected_checks':['Private result matches'],'required_sources':['.env']}
        artifact=svc.store.artifact({'name':'private_check','arguments':{},'result':check})
        invocation=run['id']+':1:0'
        svc.store.invocation(invocation,run['id'],'private_check',{})
        svc.store.invocation_state(invocation,'completed',{'artifact':artifact,'result':check})
        svc.store.tool_message(invocation,0,'private_check',encode({'artifact':artifact,'result':check}))
        svc.jobs._check_verification(run,[('private_check',{}, {'artifact':artifact,'result':check})],{'private_check':schema})
        run=svc.store.run(run['id'])
    original=svc.jobs.start;captured=[]
    def completed(data):
        captured.append(deepcopy(data));child=original(data)
        svc.store.add_message(child['chat_id'],'assistant',encode(verdict(public)))
        svc.store.update_run(child['id'],status='completed')
        return child
    monkeypatch.setattr(svc.jobs,'start',completed)
    assert GoalReview(svc.jobs)._check(run,PRIVATE,{'cancel':threading.Event()},time.monotonic())=='complete'
    assert not svc.jobs.workflow.completion_issues(svc.store.run(run['id']))
    assert '.env' not in encode(captured) and PRIVATE not in encode(captured)
    (root/'.env').write_text('External private modification.',encoding='utf-8')
    assert svc.jobs.workflow.completion_issues(svc.store.run(run['id']))
    assert svc.jobs.workflow.progress(svc.store.run(run['id']))['verified_task_ids']==[]


def test_safe_legitimate_review_completes_with_fresh_owned_evidence(receipt_case,monkeypatch):
    svc,run,root=receipt_case
    public=file_result(receipt_case,'read_file',{'path':'public.js'})
    original=svc.jobs.start;captured=[]
    def completed(data):
        captured.append(deepcopy(data));child=original(data)
        svc.store.add_message(child['chat_id'],'assistant',encode(verdict(public)))
        svc.store.update_run(child['id'],status='completed')
        return child
    monkeypatch.setattr(svc.jobs,'start',completed)
    assert GoalReview(svc.jobs)._check(run,'The public selector is implemented.',
        {'cancel':threading.Event()},time.monotonic())=='complete'
    assert len(captured)==1 and public in captured[0]['review_evidence_ids']
    stored=svc.store.run(run['id'])
    assert not svc.jobs.workflow.completion_issues(stored)
    assert svc.jobs.workflow.progress(stored)['verified_task_ids']==['owned-task']
    (root/'public.js').write_text('external edit',encoding='utf-8')
    assert svc.jobs.workflow.completion_issues(stored)


@pytest.mark.parametrize('guard',['permission','link'])
def test_local_protected_snapshot_rechecks_current_permission_and_links(receipt_case,guard):
    svc,run,root=receipt_case
    if guard=='permission':
        file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
        svc.store.update_settings({'permission_overrides':{'tool:read_file':'deny_access'}})
    else:
        (root/'credentials').mkdir()
        file_result(receipt_case,'write_file',{'path':'credentials/private.txt','content':PRIVATE})
        source=root/'credentials';target=root/'owned-original'
        source.rename(target)
        if os.name=='nt':
            linked=subprocess.run(['cmd','/c','mklink','/J',str(source),str(target)],
                capture_output=True,text=True,timeout=10)
            assert linked.returncode==0,linked.stderr
            assert source.is_junction()
        else:source.symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match='Review sources cannot establish'):
        svc.jobs.workflow.review_snapshot(run,[])


def test_restored_cloud_filtered_source_snapshot_cannot_be_accepted(receipt_case):
    svc,run,_=receipt_case
    snapshot=svc.jobs.workflow.review_snapshot(run,[])
    file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
    with pytest.raises(ValueError,match='Assigned sources changed'):
        svc.jobs.workflow._store_review_receipts(run,snapshot,[{'task_id':'owned-task','evidence_ids':['legacy-proof']}],
            'human_review','legacy-fixture','Old manifest omitted the private source.')
    assert not svc.store.entities('verification_reviews')


@pytest.mark.parametrize('defect',['placeholder-proof','omitted-protected-source'])
def test_saved_old_false_receipts_are_invalidated_after_restart(receipt_case,defect):
    svc,run,root=receipt_case
    if defect=='placeholder-proof':
        file_result(receipt_case,'read_file',{'path':'public.js'})
        run=svc.store.update_run(run['id'],private_memory_supplied=True)
        _,packet,_,_=GoalReview(svc.jobs).snapshot(run,PRIVATE)
        proof='answer:'+run['id']+':1'
        packet['candidate_answer']={'id':proof,'text':packet['candidate_answer']['text']}
        run,child,pending,_=saved_child(svc,run,packet,proof,cached=True)
        snapshot=pending['workflow_snapshot'];producer='independent_review';reference='review:'+child['id']
    else:
        snapshot=svc.jobs.workflow.review_snapshot(run,[])
        file_result(receipt_case,'write_file',{'path':'.env','content':PRIVATE})
        proof='human:old-fixture';producer='human_review';reference=proof
    bundle=svc.store.save_entity('verification_reviews',{'run_id':run['id'],'producer':producer,
        'reference':reference,'snapshot':snapshot})
    receipt={'schema_version':2,'key':'old-receipt','contract_id':producer+'-task-v1','producer_type':producer,
        'scope_revision':snapshot['scope_revision'],'version':snapshot['version'],'task_ids':['owned-task'],
        'requirement_ids':[],'passed':True,'complete':True,'available':True,'current':True,
        'review_snapshot_ref':bundle['id'],'evidence_ids':[proof]}
    svc.store.update_run(run['id'],status='paused',verification_feedback={'version':snapshot['version'],
        'receipts':{'old-receipt':receipt}})
    svc.shutdown()
    restored=setup(root.parent)
    try:
        current=restored.jobs.workflow.refresh_freshness(restored.store.run(run['id']))
        assert current['verification_feedback']['receipts']['old-receipt']['invalidated']
        assert restored.jobs.workflow.progress(current)['verified_task_ids']==[]
        assert restored.jobs.workflow.completion_issues(current)
        assert restored.store.goal(run['goal_id'])['tasks'][0]['id']=='owned-task'
        assert not restored.core.requests
        with restored.store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='write_file'",(run['id'],)).fetchone()[0]==(defect=='omitted-protected-source')
    finally:restored.shutdown()
