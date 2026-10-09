"""Owned current checks save repeated diagnostics without retiring evidence."""
from copy import deepcopy
import json
import threading
import time

import pytest

from agent_runtime import tool_schema
from context_window import estimated_prompt_tokens, prompt_budget, response_budget
from core import AGENT_SYSTEM_PROMPT
from forge_store import encode
from prompt_compiler import registered_check_context, verification_packet
from test_forge501_cadence import cadence_case, staged_run, three_writes, execute
from test_forge_core import call, service
from tool_routing import wire_schemas


# Exact failed six-check report captured in the disposable R5 theme diagnostic.
# Preserved here for portable regressions; no live browser/model is invoked.
CAPTURED_THEME_RESULT = r'''{
  "passed": false,
  "checks": [
    {
      "requirement": "Default light theme",
      "passed": false,
      "detail": "Expected body data-theme=light; observed {\"bodyTheme\":null,\"storageTheme\":null,\"buttonText\":\"Switch theme\",\"buttonAriaLabel\":null}"
    },
    {
      "requirement": "Dark switch persists theme",
      "passed": false,
      "detail": "Expected body data-theme=dark and storage theme=dark; observed {\"bodyTheme\":null,\"storageTheme\":null,\"buttonText\":\"Switch theme\",\"buttonAriaLabel\":null}"
    },
    {
      "requirement": "Reload restores theme",
      "passed": false,
      "detail": "Expected saved dark theme on body after reload; observed {\"bodyTheme\":null,\"storageTheme\":\"dark\",\"buttonText\":\"Switch theme\",\"buttonAriaLabel\":null}"
    },
    {
      "requirement": "Switch back and accessible action",
      "passed": false,
      "detail": "Expected body data-theme=light and action name containing dark; observed {\"bodyTheme\":null,\"storageTheme\":null,\"buttonText\":\"Switch theme\",\"buttonAriaLabel\":null}"
    },
    {
      "requirement": "No browser execution errors",
      "passed": true,
      "detail": ""
    },
    {
      "requirement": "Dark theme text contrast",
      "passed": false,
      "detail": "{\"expected_minimum\": 4.5, \"actual_ratio\": 16.26848, \"foreground\": \"rgb(23, 32, 51)\", \"background\": \"rgb(255, 255, 255)\", \"body_theme\": null, \"status\": \"failed\", \"diagnostics\": {\"blocked_requests\": 0, \"blocked_urls\": [], \"page_errors\": [], \"console\": [], \"extra_pages_closed\": 0, \"detail\": \"The public theme toggle did not produce dark body state.\", \"timed_out\": false, \"browser_started\": true, \"worke"
    }
  ],
  "oracle_backend": {
    "behavior": {
      "name": "jsdom-behavior",
      "oracle_version": "2",
      "node_version": "v24.18.0",
      "jsdom_version": "30.1.2",
      "verified": true
    },
    "contrast": {
      "name": "playwright-chromium",
      "browser_version": "154.0.8037.98",
      "probe_version": "forge-body-contrast-1",
      "verified": true
    }
  },
  "oracle_verified": true,
  "contrast_probe": {
    "status": "failed",
    "passed": false,
    "threshold": 4.5,
    "contrast_ratio": 16.26848,
    "foreground": "rgb(23, 32, 51)",
    "background": "rgb(255, 255, 255)",
    "theme_body_state": null,
    "backend": {
      "name": "playwright-chromium",
      "browser_version": "154.0.8037.98",
      "probe_version": "forge-body-contrast-1",
      "verified": true
    },
    "diagnostics": {
      "blocked_requests": 0,
      "blocked_urls": [],
      "page_errors": [],
      "console": [],
      "extra_pages_closed": 0,
      "detail": "The public theme toggle did not produce dark body state.",
      "timed_out": false,
      "browser_started": true,
      "worker_phases": [
        "server_ready",
        "browser_ready",
        "page_loaded",
        "colors_sampled",
        "closing"
      ]
    },
    "cleanup": {
      "server_stopped": true,
      "browser_closed": true,
      "profile_removed": true,
      "profile_name": "forge-contrast-ilfn1cz6",
      "owned_worker_exit": 0
    },
    "scope": "Computed body text contrast only; no human visual review.",
    "computed": {
      "color": "rgb(23, 32, 51)",
      "backgroundColor": "rgb(255, 255, 255)",
      "rootBackgroundColor": "rgba(0, 0, 0, 0)",
      "canvasColor": "rgb(255, 255, 255)",
      "foreground_rgb": [
        23,
        32,
        51
      ],
      "background_rgb": [
        255,
        255,
        255
      ],
      "theme_body_state": null,
      "local_storage_theme": null,
      "bridge_exposed": false
    }
  },
  "visual_review": "Full visual review not performed; DOM behavior/structure is checked and theme/landing text contrast uses a real browser."
}'''


def failed_check_result():
    return json.loads(CAPTURED_THEME_RESULT)


def receipt_for(wrapped):
    return {'schema_version':2,'kind':'checks','complete':True,'available':True,'current':True,
        'passed':False,'artifact_id':wrapped['artifact']}


def owned_check(tmp_path, *, result=None):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    svc.store.update_settings({'permission_profile':'full_access','thinking':False,'tokens':2048,
        'memory_enabled':False,'memory_suggestions':False,'goal_cloud_guidance':False,
        'goal_review_enabled':False,'auto_delegate':False})
    root=tmp_path/'project';root.mkdir()
    for name,text in {'index.html':'<button id="theme">Switch theme</button>',
        'app.js':'// unfinished theme implementation','style.css':'body { color:#172033; background:white; }'}.items():
        (root/name).write_text(text,encoding='utf-8')
    project=svc.create_project({'name':'Owned check context','path':str(root)})
    goal=svc.goal_create({'project_id':project['id'],'text':'Implement and verify the theme toggle.',
        'tasks':[{'id':'theme-task','requirement_id':'theme-requirement','text':'Implement and verify the theme toggle.'}]})
    result=failed_check_result() if result is None else result
    schema=tool_schema('registered_ui_acceptance','Inspect registered actual UI acceptance checks.',{})
    schema.update(capability='read',verification_contract={'id':'ui-acceptance-v7',
        'expected_checks':[item['requirement'] for item in failed_check_result()['checks']],
        'task_ids':['theme-task'],'requirement_ids':['theme-requirement'],
        'required_sources':['app.js','index.html','style.css']})
    old_schemas=svc.extra_tool_schemas;old_execute=svc.execute_extra_tool
    svc.extra_tool_schemas=lambda run:[deepcopy(schema)]+old_schemas(run)
    svc.execute_extra_tool=lambda run,name,args,cancel:deepcopy(result) if name==schema['function']['name'] else old_execute(run,name,args,cancel)
    accepted=svc.jobs.start({'mode':'goal','text':goal['request'],'goal_id':goal['id'],'project_id':project['id']})
    run=svc.store.update_run(accepted['id'],status='running',rounds=1)
    schemas=svc.jobs.registry.schemas(run,['tools'],all_tools=True)
    names={item['function']['name']:item for item in schemas}
    called=call(schema['function']['name'],{})
    svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[called])
    wrapped=svc.jobs._execute_call(run,{'cancel':threading.Event()},called,0,names,{'capabilities':['tools']},time.monotonic())
    svc.jobs._tool_result(run,called,0,wrapped)
    svc.jobs._check_verification(run,[(schema['function']['name'],{},wrapped)],names)
    run=svc.jobs.workflow.refresh(svc.store.run(run['id']))
    return svc,root,goal,run,schemas,wrapped


def projected_context(svc,run,schemas):
    messages=svc.jobs._context(run,schemas)
    result=json.loads(next(message['content'] for message in messages if message['role']=='tool'))
    failure=next(message['content'] for message in messages if message['role']=='system' and message['content'].startswith('Current verification failures'))
    return messages,result,failure


def test_exact_check_core_and_primary_diagnostics_are_retained_without_mutating_raw():
    wrapped={'artifact':'a'*32,'result':failed_check_result(),
        'verification_context':{'before':{'fingerprint':'b'*64},'after':{'fingerprint':'b'*64}}}
    original=deepcopy(wrapped);projected=registered_check_context(wrapped,receipt_for(wrapped))
    assert wrapped==original and projected['result']['checks']==original['result']['checks']
    assert projected['artifact']==original['artifact'] and projected['verification_context']==original['verification_context']
    probe=projected['result']['contrast_probe'];raw=original['result']['contrast_probe']
    for key in ('status','passed','threshold','contrast_ratio','foreground','background','theme_body_state','diagnostics','cleanup','scope'):
        assert probe[key]==raw[key]
    assert projected['result']['oracle_backend']==original['result']['oracle_backend']
    assert 'backend' not in probe and probe['computed']=={'theme_body_state':None,'local_storage_theme':None,'bridge_exposed':False}
    assert projected['context_projection']['retrieval']=={'tool':'artifact_read','id':wrapped['artifact']}
    assert len(encode(projected).encode())<len(encode(original).encode())


@pytest.mark.parametrize('key,value',[(key,True) for key in ('error','denied','not_executed','outcome_unknown',
    'cancelled','timed_out','partial','stale','unavailable','interrupted','rolled_back','truncated','output_truncated')]+[('ok',False),('complete',False),('available',False),('status','running')])
def test_uncertain_or_unavailable_check_results_never_project(key,value):
    wrapped={'artifact':'a'*32,'result':failed_check_result()};wrapped['result'][key]=value
    assert registered_check_context(wrapped,receipt_for(wrapped)) is None


@pytest.mark.parametrize('key,value',[('schema_version',1),('complete',False),('available',False),('current',False),
    ('invalidated',True),('superseded',True),('passed',True),('artifact_id','b'*32)])
def test_legacy_stale_partial_or_unrelated_receipts_never_project(key,value):
    wrapped={'artifact':'a'*32,'result':failed_check_result()};receipt=receipt_for(wrapped);receipt[key]=value
    assert registered_check_context(wrapped,receipt) is None


@pytest.mark.parametrize('change',['unknown-computed','different-color','bridge-exposed','different-backend','missing-primary','no-actionable-detail'])
def test_unknown_or_distinct_diagnostics_remain_available(change):
    wrapped={'artifact':'a'*32,'result':failed_check_result()};probe=wrapped['result']['contrast_probe']
    if change=='unknown-computed':probe['computed']['layout_error']='Unrecognized actionable detail'
    elif change=='different-color':probe['computed']['color']='rgb(1, 2, 3)'
    elif change=='bridge-exposed':probe['computed']['bridge_exposed']=True
    elif change=='different-backend':probe['backend']['browser_version']='another-browser'
    elif change=='missing-primary':probe.pop('contrast_ratio')
    elif change=='no-actionable-detail':wrapped['result']['checks'][0]['detail']=''
    projected=registered_check_context(wrapped,receipt_for(wrapped))
    if change=='different-backend':assert projected['result']['contrast_probe']['backend']==probe['backend']
    else:assert projected['result']['contrast_probe']['computed']==probe['computed']


def test_current_owned_registered_context_deduplicates_only_present_exact_failures(tmp_path):
    svc,root,goal,run,schemas,wrapped=owned_check(tmp_path)
    try:
        before=deepcopy(run['verification_feedback']);messages,projected,failure=projected_context(svc,run,schemas)
        assert projected['result']['checks']==wrapped['result']['checks']
        assert projected['verification_context']==wrapped['verification_context']
        assert 'complete_checks_in_tool_message' in failure
        assert wrapped['result']['checks'][0]['detail'] not in failure
        assert json.loads(next(m['content'] for m in messages if m['role']=='tool'))['result']['checks'][0]['detail']==wrapped['result']['checks'][0]['detail']
        receipt=next(iter(before['receipts'].values()))
        assert set(value['path'] for value in receipt['source_snapshot']['files'])=={'app.js','index.html','style.css'}
        assert svc.store.run(run['id'])['verification_feedback']==before
        with svc.store._connection() as db:
            row=db.execute('SELECT arguments,result,message_id FROM invocations WHERE run_id=?',(run['id'],)).fetchone()
            assert json.loads(row['arguments'])=={} and json.loads(row['result'])==wrapped
            assert json.loads(db.execute('SELECT content FROM messages WHERE id=?',(row['message_id'],)).fetchone()[0])==wrapped
        artifact=json.loads((svc.store.home/'artifacts'/(wrapped['artifact']+'.json')).read_text())
        assert artifact['result']==wrapped['result'] and artifact['arguments']=={}
        loaded=svc.jobs.registry.execute(run,'artifact_read',{'id':wrapped['artifact'],'limit':16000},threading.Event())
        assert json.loads(loaded['text'])==artifact
        assert not svc.core.requests
    finally:svc.shutdown()


@pytest.mark.parametrize('case',['legacy','unregistered','missing-retrieval','denied-retrieval','unknown-journal',
    'changed-source','changed-artifact','changed-message','changed-receipt','missing-snapshot','covered-message'])
def test_projection_requires_live_registration_exact_owned_evidence_and_current_permission(tmp_path,monkeypatch,case):
    svc,root,goal,run,schemas,wrapped=owned_check(tmp_path)
    try:
        invocation=run['id']+':1:0'
        if case=='legacy':run={**run,'execution_mode':'legacy'}
        elif case=='unregistered':schemas=[{key:value for key,value in schema.items() if key!='verification_contract'} for schema in schemas]
        elif case=='missing-retrieval':schemas=[schema for schema in schemas if schema['function']['name']!='artifact_read']
        elif case=='denied-retrieval':
            original=svc.jobs.registry.permission
            monkeypatch.setattr(svc.jobs.registry,'permission',lambda current,schema,args:'deny' if schema['function']['name']=='artifact_read' else original(current,schema,args))
        elif case=='unknown-journal':svc.store.invocation_state(invocation,'outcome_unknown',wrapped)
        elif case=='changed-source':(root/'app.js').write_text('// changed outside this check')
        elif case=='changed-artifact':
            path=svc.store.home/'artifacts'/(wrapped['artifact']+'.json');value=json.loads(path.read_text());value['arguments']={'forged':True};path.write_text(encode(value))
        elif case=='changed-message':
            with svc.store._connection(transaction='write') as db:
                row=db.execute('SELECT message_id FROM invocations WHERE id=?',(invocation,)).fetchone()
                db.execute('UPDATE messages SET content=? WHERE id=?',(encode({**wrapped,'artifact':'b'*32}),row['message_id']))
        elif case=='changed-receipt':
            state=deepcopy(run['verification_feedback']);next(iter(state['receipts'].values()))['failed_count']=99
            run=svc.store.update_run(run['id'],verification_feedback=state)
        elif case=='missing-snapshot':
            state=deepcopy(run['verification_feedback']);next(iter(state['receipts'].values()))['source_snapshot']=None
            run=svc.store.update_run(run['id'],verification_feedback=state)
        elif case=='covered-message':run={**run,'boundary':next(iter(run['verification_feedback']['receipts'].values()))['message_id']}
        values,supplied=svc.jobs._verification_context_results(run,schemas,svc.store.run_chat(run['chat_id'],0)['messages'])
        assert values=={} and supplied=={} and not svc.core.requests
    finally:svc.shutdown()


def test_packet_never_deduplicates_partial_stale_or_mismatched_details(tmp_path):
    svc,root,goal,run,schemas,wrapped=owned_check(tmp_path)
    try:
        invocation=run['id']+':1:0';checks=wrapped['result']['checks']
        supplied={invocation:{'checks':checks,'receipt':next(iter(run['verification_feedback']['receipts'].values()))}}
        baseline=verification_packet(run);assert verification_packet(run,supplied_checks=supplied)!=baseline
        for field,value in [('complete',False),('available',False),('current',False),('schema_version',1),('version',100),('failed_count',99),('artifact_id','b'*32),('tool','unrelated_check')]:
            changed=deepcopy(run);next(iter(changed['verification_feedback']['pending'].values()))[field]=value
            assert verification_packet(changed,supplied_checks=supplied)==verification_packet(changed)
        wrong=deepcopy(supplied);wrong[invocation]['checks'][0]['detail']='Another failure'
        assert verification_packet(run,supplied_checks=wrong)==baseline
        assert not svc.core.requests
    finally:svc.shutdown()


def test_compaction_keeps_registration_and_due_cadence_with_wire_only_budget(tmp_path,monkeypatch):
    svc,root,goal,schema=cadence_case(tmp_path)
    try:
        run=staged_run(svc,goal)
        svc.store.add_message(run['chat_id'],'assistant','Earlier eligible discussion. '*450)
        for name in ('a.txt','b.txt','c.txt'):
            svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[call('write_file',{'path':name,'content':'initial '+name})])
            run,_=execute(svc,run,'write_file',{'path':name,'content':'initial '+name})
        schemas=svc.jobs.registry.schemas(run,['tools'])
        original=svc.jobs._context;seen=[]
        def context(current,registered):
            seen.append(deepcopy(registered));return original(current,registered)
        monkeypatch.setattr(svc.jobs,'_context',context)
        job={'cancel':threading.Event()};current=svc.jobs._compact(svc.store.run(run['id']),schemas,job,force=True)
        assert len(seen)>=2 and all(any(s.get('verification_contract',{}).get('id')=='exact-selector-v1' for s in rows) for rows in seen)
        assert any('Verification cadence: three file mutations' in message['content'] for message in job['context_messages'])
        assert current['verification_cadence']['required'] and current['verification_cadence']['check_tool']=='trusted_check'
        assert all(set(item)=={'type','function'} for item in wire_schemas(schemas))
        assert estimated_prompt_tokens(job['context_messages'],wire_schemas(schemas),AGENT_SYSTEM_PROMPT)<=prompt_budget(8192,response_budget(8192,2048))
        current,blocked=execute(svc,svc.store.run(run['id']),'write_file',{'path':'fourth.txt','content':'must wait'})
        assert blocked['result']['not_executed'] and not (root/'fourth.txt').exists() and not svc.core.requests
    finally:svc.shutdown()


def test_compaction_keeps_registered_metadata_for_cloud_guard_repreparation(tmp_path,monkeypatch):
    svc,root,goal,run,schemas,wrapped=owned_check(tmp_path)
    try:
        current=svc.store.update_run(run['id'],cloud_scope={'assignment_id':'synthetic-no-request'},execution_mode='legacy')
        schemas=[schema for schema in schemas if schema['function']['name'] in ('registered_ui_acceptance','artifact_read')]
        svc.store.add_message(run['chat_id'],'assistant','Earlier scoped diagnostic. '*200)
        svc.store.add_message(run['chat_id'],'assistant','Latest bounded advice.')
        calls=[]
        def prepare(candidate,messages,registered,cancel):
            calls.append(deepcopy(registered));return messages,{'synthetic_guard':True}
        monkeypatch.setattr(svc,'prepare_cloud_round',prepare)
        job={'cancel':threading.Event(),'compaction_summary_attempted':True}
        svc.jobs._compact(current,schemas,job,force=True)
        assert calls and all(any(s.get('verification_contract',{}).get('id')=='ui-acceptance-v7' for s in rows) for rows in calls)
        assert not svc.core.requests
    finally:svc.shutdown()


def test_oversized_complete_check_core_still_pauses_without_retiring_fresh_exchange(tmp_path):
    result=failed_check_result();result['checks'][0]['detail']='Exact necessary Unicode failure 漢字🌍 '*1500
    svc,root,goal,run,schemas,wrapped=owned_check(tmp_path,result=result)
    try:
        before=svc.store.run(run['id']);messages=svc.jobs._context(run,schemas)
        assert json.loads(next(message['content'] for message in messages if message['role']=='tool'))['result']['checks']==result['checks']
        with pytest.raises(ValueError,match='newest tool exchange.*Increase Context'):
            svc.jobs._compact(run,schemas,{'cancel':threading.Event()},messages=messages)
        after=svc.store.run(run['id'])
        assert after.get('boundary',0)==before.get('boundary',0)
        assert after.get('continuity_checkpoint_id')==before.get('continuity_checkpoint_id')
        assert not svc.store.entities('checkpoints') and not svc.core.requests
    finally:svc.shutdown()
