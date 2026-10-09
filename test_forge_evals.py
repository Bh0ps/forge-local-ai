"""Acceptance-oracle and provenance tests; never make local/cloud model calls."""
import copy
import json
from pathlib import Path

import pytest

from forge_evals import ALLOWED_TOOLS, CONTROLS, TRANSPORT_SHIM_VERSION, bounded_oracle, completed_effect, confined_path, digest, effect_targets, evaluation_schema_adapter, evaluation_contract_factory, install_transport_shim, load_suite, materialize, normalized_controls, oracle, registered_contracts_supported, source_hash, summarize, task_admission, thinking_contract


def test_suite_has_exact_requested_mix_and_identical_repetition_inputs():
    suite = load_suite()
    assert len(suite["tasks"]) == 20
    assert {name:sum(task["category"] == name for task in suite["tasks"]) for name in ("app","broader","continuity")} == {"app":12,"broader":4,"continuity":4}
    assert {task.get("injection") for task in suite["tasks"] if task["category"] == "continuity"} == {"restart","steer","unknown","compact"}
    for task in suite["tasks"]:
        assert len({digest(task["files"]) for _ in range(3)}) == 1
        assert task["prompt"] and task["oracle"] and task["reference_files"]


def test_source_fingerprint_excludes_generated_profiles_preserves_authored_assets(tmp_path):
    source = tmp_path/"forge_service.py"; source.write_text("AUTHORED = 1\n")
    skill = tmp_path/"assets/starter-library/skills/fixture/SKILL.md"; skill.parent.mkdir(parents=True); skill.write_text("Use the fixture recipe.\n")
    resource = skill.parent/"references/data/schema.json"; resource.parent.mkdir(parents=True); resource.write_text('{"version":1}')
    fixture = tmp_path/"assets/evaluations/suite.json"; fixture.parent.mkdir(parents=True); fixture.write_text('{"schema_version":1}')
    initial = source_hash(tmp_path)
    generated = ["outputs/native-smoke/profile/config.json", "screenshots/session/metadata.json", "release/profile.json", "workspace/state.json",
                 "data/settings.json", "app-data/settings.json", "exports/chat.json", "backups/state.json", "webview/preferences.json", "models/manifest.json",
                 "nested/.forge/settings.json", "nested/.sidekick/chats.json", "frontend/dist/manifest.json", "benchmarks/local/probe/result.json"]
    for name in generated:
        target = tmp_path/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_text('{"synthetic_profile":1}')
    assert source_hash(tmp_path) == initial
    for name in generated: (tmp_path/name).write_text('{"synthetic_profile":2}')
    assert source_hash(tmp_path) == initial
    for target, changed in ((source,"AUTHORED = 2\n"),(skill,"Read the updated recipe.\n"),(resource,'{"version":2}'),(fixture,'{"schema_version":2}')):
        before = source_hash(tmp_path); target.write_text(changed)
        assert source_hash(tmp_path) != before


def test_evaluation_adapter_drives_candidate_generation_catalog_and_validation(tmp_path):
    from test_forge_core import Engine, call, finished, service
    engine = Engine([{'tool_calls':[call('list_files',{'path':'.','recursive':False})]},
                     {'tool_calls':[call('tools_search',{'query':'read_file'})]},
                     {'tool_calls':[call('tools_load',{'names':['read_file']})]},
                     {'tool_calls':[call('read_file',{'path':'fixture.txt'})]}, {'content':'Verified the fixture file.'}])
    svc = service(tmp_path,engine)
    try:
        folder=tmp_path/'project'; folder.mkdir(); (folder/'fixture.txt').write_text('Fixture content.\n')
        project=svc.create_project({'name':'Adapter fixture','path':str(folder)})
        svc.store.update_settings({'permission_profile':'full_access','web':False,'browser_tools':False,'computer_tools':False})
        original=svc.jobs.registry.schemas; catalog_options=[]
        def selected_runtime(run,capabilities,*,all_tools=False):
            catalog_options.append(all_tools)
            return original(run,capabilities,all_tools=all_tools)
        svc.jobs.registry.schemas=evaluation_schema_adapter(selected_runtime)
        run=svc.jobs.start({'text':'Read fixture.txt and verify its content.','project_id':project['id'],'agent_tools':sorted(ALLOWED_TOOLS)})
        result=finished(svc,run)
        assert result['status']=='completed',result
        assert any(catalog_options),'Candidate full catalog path was not exercised.'
        assert len(engine.requests)>=5
        assert all({item['function']['name'] for item in request['tools']} <= ALLOWED_TOOLS for request in engine.requests)
        with svc.store._connection() as db:
            calls=[dict(row) for row in db.execute('select name,status,result from invocations where run_id=?',(run['id'],))]
        assert {'tools_search','tools_load','read_file'} <= {item['name'] for item in calls if item['status']=='completed'}
        assert any(item['name']=='read_file' and 'Fixture content.' in item['result'] for item in calls)
        assert not any(event['type']=='tool_validation' for event in result['events'])
    finally: svc.shutdown()


def test_evaluation_adapter_drops_only_options_unsupported_by_baseline_signature():
    seen=[]
    def baseline(run,capabilities):
        seen.append((run,capabilities))
        return [{'function':{'name':'read_file'}},{'function':{'name':'run_command'}}]
    values=evaluation_schema_adapter(baseline)({'id':'fixture'},['tools'],all_tools=True,future_option=True)
    assert seen==[({'id':'fixture'},['tools'])]
    assert {item['function']['name'] for item in values}=={'evaluation_check','read_file'}


def test_tracked_task_admission_is_identical_and_preserves_original_continuity_stages():
    for task in load_suite()['tasks']:
        old=task_admission(task,'same-disposable-project',tracked_tasks=True)
        new=task_admission(copy.deepcopy(task),'same-disposable-project',tracked_tasks=True)
        assert old==new and old['text']==task['prompt']
        assert [t['text'] for t in old['tasks']]==(task.get('tasks') or [task['prompt']])
        assert all(len(t['id'])==32 for t in old['tasks'])
        assert len({t['id'] for t in old['tasks']})==len(old['tasks'])
        default=task_admission(task,'same-disposable-project')
        if task['category']=='continuity':
            assert [t['text'] for t in default['tasks']]==task['tasks'] and all('id' not in t for t in default['tasks'])
        else:assert default is None


def test_contract_metadata_is_omitted_on_legacy_and_never_enters_candidate_wire(tmp_path):
    from test_forge_core import service
    from tool_routing import wire_schemas
    svc=service(tmp_path);svc.jobs._launch=lambda r:None
    task=next(t for t in load_suite()['tasks'] if t['id']=='api-pagination')
    try:
        folder=tmp_path/'project';materialize(task,folder)
        project=svc.create_project({'path':str(folder)})
        goal=svc.goal_create(task_admission(task,project['id'],tracked_tasks=True))
        run=svc.jobs.start({'text':task['prompt'],'project_id':project['id'],'goal_id':goal['id'],'mode':'goal'})
        saved=svc.store.run(run['id']);factory=evaluation_contract_factory(task,svc.store)
        original=svc.jobs.registry.schemas
        assert registered_contracts_supported(original)
        metadata=evaluation_schema_adapter(original,factory)(saved,['tools'])[0]
        contract=metadata['verification_contract']
        assert contract['task_ids']==[t['id'] for t in goal['tasks']]
        assert contract['required_sources']==sorted(set(task['files'])|set(task['reference_files']))
        assert contract['scope_path']=='.' and contract['expected_checks']
        def legacy(run,capabilities):return []
        assert not registered_contracts_supported(legacy)
        older=evaluation_schema_adapter(legacy,factory)(saved,['tools'])[0]
        assert 'verification_contract' not in older
        assert wire_schemas([metadata])==wire_schemas([older])
        assert not {'expected_checks','required_sources','task_ids'} & wire_schemas([metadata])[0].keys()
    finally:svc.shutdown()


def test_contracts_are_complete_and_match_reference_oracles_without_sending_reference_text(tmp_path):
    from types import SimpleNamespace
    for task in load_suite()['tasks']:
        factory=evaluation_contract_factory(task,SimpleNamespace(goal=lambda _:None))
        registered=factory({'goal_id':'synthetic','goal_initial':{'tasks':[{'id':'original'}]}})
        assert registered['id'].startswith('evaluation:'+task['id']+':'+task['oracle'])
        assert registered['task_ids']==['original']
        assert 'reference_files' not in json.dumps(registered)
        if task['oracle'] not in {'todo','signup','catalog','cart','theme','dialog','tabs','landing'}:
            folder=tmp_path/task['id'];materialize(task,folder,True)
            result=bounded_oracle(task,folder)
            assert sorted(registered['expected_checks'])==sorted(row['requirement'] for row in result['checks'])


def test_mismatched_tracked_admission_is_excluded_from_comparison():
    old,new=row('baseline'),row('candidate')
    old['tracked_tasks']=False;new['tracked_tasks']=True
    assert summarize([old,new])['comparable_pairs']==0


@pytest.mark.parametrize("identity", [task["id"] for task in load_suite()["tasks"] if task["oracle"] not in {"todo","signup","catalog","cart","theme","dialog","tabs","landing"}])
def test_behavioral_oracles_reject_unfinished_and_accept_reference(identity, tmp_path):
    task = next(row for row in load_suite()["tasks"] if row["id"] == identity)
    materialize(task, tmp_path)
    assert not oracle(task, tmp_path)["passed"]
    materialize(task, tmp_path, True)
    result = oracle(task, tmp_path)
    assert result["passed"], result


def test_browser_oracle_checks_interaction_and_rejects_false_success(tmp_path):
    task = next(row for row in load_suite()["tasks"] if row["id"] == "todo-flow")
    materialize(task, tmp_path, True)
    reference = bounded_oracle(task, tmp_path)
    assert reference["passed"], reference
    (tmp_path/"app.js").write_text("document.querySelector('#remaining').textContent='Completed successfully';")
    result = bounded_oracle(task, tmp_path)
    assert not result["passed"] and any(not item["passed"] for item in result["checks"])


def test_money_oracle_rejects_non_discounting_implementation(tmp_path):
    task = next(row for row in load_suite()["tasks"] if row["id"] == "pricing-boundaries")
    materialize(task,tmp_path,True)
    (tmp_path/"pricing.py").write_text("def total_cents(items,coupon=None):\n    return sum(item['unit_cents']*item['quantity'] for item in items)\n")
    assert not oracle(task,tmp_path)["passed"]


def test_oracle_execution_cannot_import_os_or_open_outside_project(tmp_path):
    task = next(row for row in load_suite()["tasks"] if row["id"] == "api-pagination")
    materialize(task,tmp_path,True)
    (tmp_path/"service.py").write_text("import os\ndef paginate(rows,page,size):\n    return {}\n")
    result = bounded_oracle(task,tmp_path)
    assert not result["passed"] and "unsupported import" in result["checks"][-1]["detail"]
    (tmp_path/"service.py").write_text("open('outside.txt','w').write('effect')\ndef paginate(rows,page,size):\n    return {}\n")
    assert not bounded_oracle(task,tmp_path)["passed"]
    assert not (tmp_path/"outside.txt").exists()


@pytest.mark.parametrize("name", ["../outside", "C:\\outside.txt", "/outside.txt"])
def test_fixture_materialization_stays_confined(tmp_path,name):
    with pytest.raises(ValueError): confined_path(tmp_path,name,existing=False)


def row(variant, success=True, seconds=10, model_digest="a"*64):
    return dict(task="todo-flow",repetition=1,variant=variant,success=success,wall_seconds=seconds,fixture_hash="same",controls=CONTROLS,
                harness_hash="e"*64,execution_mode="both",pair_order=["baseline","candidate"],engine={"model":"fixture","digest":model_digest},actual_model_requests=2,input_tokens=100,output_tokens=20,
                control_valid=True,transport_shim=TRANSPORT_SHIM_VERSION,normalized_controls=normalized_controls({'model':'fixture'},CONTROLS),
                oracle_verified=True,oracle_backend={'behavior':{'name':'fixture','version':'1','verified':True}})


@pytest.mark.parametrize('metadata',[{}, {'capabilities':['tools']},{'capabilities':'thinking'},{'thinking':{'values':['low','high']},'capabilities':['thinking']},{'thinking':{'values':[True]}},{'thinking':{'values':[0]}}])
def test_thinking_control_refuses_unverified_or_false_unsupported_models(metadata):
    with pytest.raises(ValueError): thinking_contract(metadata)


def test_transport_shim_changes_only_think_field_and_guards_controls():
    from types import SimpleNamespace
    sent=[]; trace=[]; errors=[]
    def transport(payload,cancel_event=None):
        sent.append(copy.deepcopy(payload)); yield {'done':True}
    engine={'model':'verified-alias','thinking_control':thinking_contract({'capabilities':['thinking']})}
    core=install_transport_shim(SimpleNamespace(_stream_payload=transport),engine,CONTROLS,trace,errors)
    payload={'model':'verified-alias','messages':[{'role':'user','content':'Fixture'}],'tools':[{'function':{'name':'fixture'}}],
             'stream':True,'truncate':False,'shift':False,'options':{'num_ctx':8192,'temperature':0,'num_predict':1024,'num_thread':3}}
    original=copy.deepcopy(payload); list(core._stream_payload(payload))
    assert payload==original and sent[0]=={**original,'think':False}
    assert trace[0]['other_fields_unchanged'] and trace[0]['wire_think'] is False
    assert trace[0]['normalizer_changed'] and not trace[0]['native_controls']['think_present']
    bad={**payload,'options':{**payload['options'],'num_ctx':4096}}
    with pytest.raises(ValueError,match='context'): list(core._stream_payload(bad))
    assert len(sent)==1 and errors
    with pytest.raises(ValueError): install_transport_shim(SimpleNamespace(_stream_payload=transport),engine,{**CONTROLS,'thinking':True},[],[])


def test_candidate_native_preflight_cannot_hide_a_missing_or_enabled_think_field():
    from types import SimpleNamespace
    sent=[]; trace=[]; errors=[]
    def transport(payload,cancel_event=None): sent.append(payload); yield {'done':True}
    engine={'model':'verified-alias','thinking_control':thinking_contract({'capabilities':['thinking']})}
    core=install_transport_shim(SimpleNamespace(_stream_payload=transport),engine,CONTROLS,trace,errors,require_native_false=True)
    payload={'model':engine['model'],'options':{'num_ctx':8192,'temperature':0,'num_predict':2048},'stream':True}
    for value in ({}, {'think':True}):
        with pytest.raises(ValueError,match='native payload'): list(core._stream_payload({**payload,**value}))
    assert not sent and len(errors)==2 and all(not row['control_valid'] for row in trace)
    list(core._stream_payload({**payload,'think':False}))
    assert sent[0]['think'] is False and trace[-1]['normalizer_changed'] is False


def test_baseline_and_candidate_serialize_identical_wire_false_without_source_edits(monkeypatch):
    import hashlib
    import importlib.util
    import httpx
    import inference_stream
    from core import Core
    baseline_path=Path(__file__).parent.parent/'forge-5.0-baseline/core.py'
    if not baseline_path.is_file(): pytest.skip('Original baseline checkout is unavailable.')
    before=hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    spec=importlib.util.spec_from_file_location('evaluation_original_core',baseline_path); baseline_module=importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline_module)
    wire=[]
    def handler(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200,content=json.dumps({'message':{'content':'Fixture done.'},'done':True})+'\n')
    original_client=httpx.AsyncClient
    monkeypatch.setattr(inference_stream.httpx,'AsyncClient',lambda *args,**options:original_client(*args,**options,transport=httpx.MockTransport(handler)))
    engine={'model':'hf.co/fixture/Capable-Alias:fixture','thinking_control':thinking_contract({'capabilities':['thinking']})}
    data={'model':engine['model'],'context':8192,'tokens':2048,'temperature':0,'thinking':False,'messages':[{'role':'user','content':'Fixture.'}],
          'model_metadata':{'capabilities':['thinking']},'tools':[]}
    traces=[]
    for cls in (baseline_module.Core,Core):
        trace=[]; core=install_transport_shim(cls(),engine,CONTROLS,trace,[])
        assert list(core.stream_agent(data))[-1]['done']
        traces.append(trace)
    assert [item['think'] for item in wire]==[False,False]
    assert wire[0]['options']==wire[1]['options']=={'num_ctx':8192,'temperature':0,'num_predict':2048}
    assert not traces[0][0]['original_think_present'] and traces[1][0]['original_think'] is False
    assert all(trace[0]['other_fields_unchanged'] for trace in traces)
    assert hashlib.sha256(baseline_path.read_bytes()).hexdigest()==before


def test_transport_shim_or_normalized_control_mismatch_prevents_comparison():
    old,new=row('baseline'),row('candidate'); new['transport_shim']='different'
    assert summarize([old,new])['comparable_pairs']==0
    new['transport_shim']=TRANSPORT_SHIM_VERSION; new['normalized_controls']={**new['normalized_controls'],'think':True}
    assert summarize([old,new])['comparable_pairs']==0
    new['normalized_controls']=old['normalized_controls']; new['control_valid']=False
    assert summarize([old,new])['comparable_pairs']==0


def test_oracle_backend_changes_or_unverified_browser_cannot_enter_comparison():
    old,new=row('baseline'),row('candidate')
    new['oracle_backend']={'behavior':{'name':'fixture','version':'2','verified':True}}
    assert summarize([old,new])['comparable_pairs']==0
    new['oracle_backend']=old['oracle_backend']; new['oracle_verified']=False
    assert summarize([old,new])['comparable_pairs']==0


def test_unavailable_contrast_never_confirms_task_success(tmp_path,monkeypatch):
    import forge_evals
    task=next(row for row in load_suite()['tasks'] if row['oracle']=='theme'); materialize(task,tmp_path,True)
    monkeypatch.setattr(forge_evals,'rendered_contrast',lambda *_:{'status':'unverified','contrast_ratio':None,'backend':{'name':'playwright-edge','probe_version':'1','verified':True},'diagnostics':{'error':'Paint unsupported despite available browser'}})
    result=oracle(task,tmp_path)
    assert not result['passed'] and not result['oracle_verified']
    assert next(item for item in result['checks'] if item['requirement']=='Dark theme text contrast')['passed'] is False


def test_theme_state_failures_explain_expected_and_observed_public_contract(tmp_path,monkeypatch):
    import forge_evals
    task=next(row for row in load_suite()['tasks'] if row['oracle']=='theme'); materialize(task,tmp_path,True)
    (tmp_path/'app.js').write_text("document.documentElement.dataset.theme='light';document.querySelector('#theme-button').onclick=()=>{document.documentElement.dataset.theme='dark';localStorage.setItem('appearance','dark')}")
    monkeypatch.setattr(forge_evals,'rendered_contrast',lambda *_:{'status':'failed','contrast_ratio':1.0,'foreground':'rgb(255,255,255)','background':'rgb(255,255,255)','backend':{'name':'playwright-edge','probe_version':'1','verified':True}})
    result=oracle(task,tmp_path)
    failures={item['requirement']:item['detail'] for item in result['checks'] if not item['passed']}
    assert 'bodyTheme' in failures['Default light theme'] and 'Expected' in failures['Default light theme']
    assert 'storageTheme' in failures['Dark switch persists theme']
    assert 'saved dark theme' in failures['Reload restores theme']
    assert 'actual_ratio' in failures['Dark theme text contrast'] and 'rgb(255,255,255)' in failures['Dark theme text contrast']


@pytest.mark.parametrize('kind',['theme','landing'])
@pytest.mark.parametrize('low_contrast',[False,True])
def test_real_contrast_accepts_css_variables_and_rejects_low_values(kind,low_contrast,tmp_path):
    task=next(row for row in load_suite()['tasks'] if row['oracle']==kind); materialize(task,tmp_path,True)
    color='#777777' if low_contrast else '#ffffff'; background='#888888' if low_contrast else '#172033'
    existing=(tmp_path/'styles.css').read_text()
    (tmp_path/'styles.css').write_text(existing+f'\n:root{{--surface:{background};--ink:{color};}} body{{background:var(--surface);color:var(--ink);}}')
    if kind=='theme':
        (tmp_path/'app.js').write_text("const action=document.querySelector('#theme-button');function show(value){document.body.dataset.theme=value;action.textContent=value==='dark'?'Switch to light theme':'Switch to dark theme'}show(localStorage.getItem('theme')==='dark'?'dark':'light');action.onclick=()=>{const value=document.body.dataset.theme==='dark'?'light':'dark';localStorage.setItem('theme',value);show(value)};")
    result=oracle(task,tmp_path); probe=result['contrast_probe']
    if probe['status']=='unverified':
        assert not result['passed'] and not result['oracle_verified']
        pytest.skip('Real browser unavailable: '+json.dumps(probe.get('diagnostics')))
    check=next(item for item in result['checks'] if item['requirement'] in ('Dark theme text contrast','Text contrast meets 4.5:1'))
    assert check['passed'] is (not low_contrast),result
    assert result['oracle_backend']['contrast']['verified']
    assert result['passed'] is (not low_contrast),result


def test_speed_is_reported_only_for_matched_successful_comparable_pairs():
    assert summarize([row("baseline")])["matched_success_wall_ratio"] is None
    assert summarize([row("baseline"),row("candidate",False,1)])["matched_success_wall_ratio"] is None
    assert summarize([row("baseline"),row("candidate",True,5)])["matched_success_wall_ratio"] == .5
    result = summarize([row("baseline"),row("candidate",True,1,"b"*64)])
    assert result["comparable_pairs"] == 0 and result["matched_success_wall_ratio"] is None


def test_mutated_control_or_fixture_disables_comparison():
    old, new = row("baseline"), row("candidate")
    new["controls"] = {**CONTROLS,"context":16384}
    assert summarize([old,new])["comparable_pairs"] == 0
    new["controls"] = CONTROLS; new["fixture_hash"] = "different"
    assert summarize([old,new])["comparable_pairs"] == 0


def test_zero_baseline_failures_and_zero_mutual_success_metrics_are_unavailable():
    value = summarize([row("baseline"),row("candidate")])
    assert value["comparable_failure_metrics"]["relative_failed_task_reduction"] is None
    value = summarize([row("baseline",False),row("candidate",False)])
    assert value["mutual_success_median_wall"]["baseline_seconds"] is None
    assert value["mutual_success_median_wall"]["ratio"] is None
    assert value["mutual_success_input_tokens"]["reduction_fraction"] is None


def test_summary_distinguishes_median_sum_failed_reduction_and_token_scopes():
    values = []
    for index,(old_seconds,new_seconds,old_success,new_success) in enumerate(((1,1,True,True),(3,2,True,True),(100,3,True,True),(10,20,False,True),(15,30,False,False))):
        old,new=row("baseline",old_success,old_seconds),row("candidate",new_success,new_seconds)
        old["task"]=new["task"]="task-"+str(index); new["input_tokens"]=50; values.extend((old,new))
    result=summarize(values)
    assert result["comparable_failure_metrics"]["relative_failed_task_reduction"] == .5
    assert result["mutual_success_median_wall"]["baseline_seconds"] == 3
    assert result["mutual_success_median_wall"]["candidate_seconds"] == 2
    assert result["mutual_success_median_wall"]["ratio"] == pytest.approx(2/3)
    assert result["matched_success_summed_wall_ratio"] == pytest.approx(6/104)
    assert result["comparable_all_run_input_tokens"]["baseline_input_tokens"] == 500
    assert result["mutual_success_input_tokens"]["baseline_input_tokens"] == 300


def test_diagnostic_batches_mismatched_pair_order_or_long_gaps_are_not_speed_evidence():
    old,new=row("baseline"),row("candidate")
    old["execution_mode"]="baseline"
    assert summarize([old,new])["comparable_pairs"] == 0
    old["execution_mode"]="both"; new["pair_order"]=["candidate","baseline"]
    assert summarize([old,new])["comparable_pairs"] == 0
    new["pair_order"]=old["pair_order"]; old.update(started_at=1,finished_at=10); new.update(started_at=500,finished_at=510)
    assert summarize([old,new])["matched_success_wall_ratio"] is None


def test_effect_matching_accepts_path_aliases_and_rejects_failed_proposals(tmp_path):
    assert effect_targets(tmp_path,{"path":"./normalized.json"},"normalized.json")
    assert effect_targets(tmp_path,{"path":str(tmp_path/"normalized.json")},"normalized.json")
    assert not effect_targets(tmp_path,{"path":"../normalized.json"},"normalized.json")
    assert completed_effect(json.dumps({"result":{"ok":True}}))
    assert not completed_effect(json.dumps({"result":{"ok":False,"not_executed":True}}))


def offline_worker_config(tmp_path,identity):
    import forge_evals
    task=copy.deepcopy(next(row for row in load_suite()['tasks'] if row['id']==identity))
    folder=tmp_path/'project'; materialize(task,folder)
    return {'repo':str(forge_evals.ROOT),'workspace':str(folder),'profile':str(tmp_path/'profile'),'task':task,
            'engine':{'model':'fixture','base':'http://127.0.0.1:11434/api','thinking_control':thinking_contract({'capabilities':['thinking']})},
            'controls':{**CONTROLS,'max_seconds':15}}


@pytest.mark.parametrize('identity',['restart-checkpoint','steering-retained','unknown-outcome','compaction-retained'])
@pytest.mark.parametrize('guided',[False,True])
def test_tracked_worker_actually_exercises_every_continuity_intervention(tmp_path,monkeypatch,identity,guided):
    import csv,io,sqlite3
    import core,forge_evals,forge_service
    from test_forge_core import Engine,call
    config=offline_worker_config(tmp_path,identity)
    config.update(tracked_tasks=True,guided_execution=guided)
    holder={};task=config['task'];folder=Path(config['workspace'])
    original=forge_service.ForgeService
    def factory(*args,**kwargs):
        svc=original(*args,**kwargs);holder['service']=svc;return svc
    monkeypatch.setattr(forge_service,'ForgeService',factory)
    class OfflineCore(Engine):
        def __init__(self,base=None):super().__init__()
        def _stream_payload(self,payload,cancel_event=None):
            if not payload.get('tools') and any('continuity summary' in m['content'] for m in payload['messages'] if m['role']=='system'):
                response={'content':'Normalized source rows are saved. Continue totals and acceptance checks.'}
            elif not (folder/'normalized.json').is_file():
                response={'tool_calls':[call('write_file',{'path':'normalized.json','content':task['reference_files']['normalized.json']})]}
            elif not (folder/'totals.json').is_file():
                response={'tool_calls':[call('write_file',{'path':'totals.json','content':task['reference_files']['totals.json']})]}
            else:
                svc=holder['service'];run=next(r for r in svc.store.runs() if not r.get('parent_id'))
                with svc.store._connection() as db:
                    rows=db.execute("SELECT result FROM invocations WHERE run_id=? AND name='evaluation_check' AND status='completed'",(run['id'],)).fetchall()
                verified=any(json.loads(row[0] or '{}').get('result',{}).get('passed') for row in rows)
                goal=svc.store.goal(run['goal_id'])
                if not verified:
                    offered={schema['function']['name'] for schema in payload.get('tools',[])}
                    # A legacy-mode run uses native discovery when its current
                    # budget defers the registered checker. Never call an absent tool.
                    response={'tool_calls':[call('evaluation_check',{})] if 'evaluation_check' in offered
                        else [call('tools_load',{'names':['evaluation_check']})]}
                elif any(t['status']!='completed' for t in goal['tasks']):
                    response={'tool_calls':[call('goal_update',{'tasks':[{**t,'status':'completed','evidence':['Exact host acceptance checks passed.']} for t in goal['tasks']],
                        'checkpoint':'Both ordered stages passed the exact host checks.','next_action':'Report verified completion.'})]}
                else:response={'content':'Both ordered stages completed and verified.'}
            yield {'message':response,'done':True,'done_reason':'stop','eval_count':12,'prompt_eval_count':250,'eval_duration':1000000000}
    monkeypatch.setattr(core,'Core',OfflineCore)
    result=forge_evals.run_worker(config)
    assert result['control_valid'] and result['tracked_tasks'] and result['registered_contracts'],result
    assert result['continuity_exercised'] and result['injection']['kind']==task['injection'],result
    if task['injection']!='steer':
        assert result['injection'].get('resumed'),result
        assert next(c for c in result['oracle']['checks'] if c['requirement']=='Completed stage was not repeated')['passed']
    if task['injection']=='compact':assert result['injection'].get('compacted'),result
    if task['injection']=='unknown':assert result['injection'].get('resume_blocked') and result['injection'].get('inspected'),result
    assert result['success'],json.dumps({k:result.get(k) for k in ('status','recovery','injection','rounds','tool_calls','tool_errors','oracle','checkpoint')})
    assert result['workflow_execution']==('guided' if guided else 'legacy')
    assert result['task_admission']['text']==task['prompt']


@pytest.mark.parametrize('stage',['start','compact','resume'])
def test_worker_measures_normal_app_refusals_with_oracle_and_usage(tmp_path,monkeypatch,stage):
    import csv,io
    import core,forge_evals,forge_service
    from test_forge_core import Engine,call
    config=offline_worker_config(tmp_path,'api-pagination' if stage=='start' else 'compaction-retained' if stage=='compact' else 'restart-checkpoint')
    normalized=list(csv.DictReader(io.StringIO(config['task']['files']['orders.csv']))) if stage!='start' else None
    class OfflineCore(Engine):
        def __init__(self,base=None):
            super().__init__([{'tool_calls':[call('write_file',{'path':'normalized.json','content':json.dumps(normalized)})]}] if normalized else [])
        def _stream_payload(self,payload,cancel_event=None):
            yield from super().stream_agent(payload,cancel_event)
    monkeypatch.setattr(core,'Core',OfflineCore)
    original=forge_service.ForgeService
    def factory(*args,**kwargs):
        svc=original(*args,**kwargs)
        def refused(*_args,**_kwargs): raise ValueError('The request and tool schemas cannot fit. Resume with more context.')
        setattr(svc.jobs,stage,refused)
        return svc
    monkeypatch.setattr(forge_service,'ForgeService',factory)
    result=forge_evals.run_worker(config)
    assert result['status']=='paused' and not result['success'],result
    assert result['task_refusal'] and result['task_refusal']['stage']==stage and 'schemas cannot fit' in result['task_refusal']['error'],{key:result.get(key) for key in ('recovery','injection','rounds','tool_calls','actual_model_requests','task_refusal')}
    assert result['control_valid'] and not result['control_errors']
    assert result['oracle_verified'] and result['oracle']['checks']
    assert 'input_tokens' in result and 'output_tokens' in result and 'reported_requests' in result
    if stage=='start': assert result['actual_model_requests']==0 and result['input_tokens']==0
    else:
        assert result['actual_model_requests']>=1 and result['reported_requests']>=1
        assert json.loads((Path(config['workspace'])/'normalized.json').read_text())==normalized


def test_worker_configuration_failure_stays_invalid_without_task_classification(tmp_path,monkeypatch):
    import core,forge_evals
    from test_forge_core import Engine
    config=offline_worker_config(tmp_path,'api-pagination'); config['engine']['thinking_control']={'permits_false':False}
    class OfflineCore(Engine):
        def __init__(self,base=None): super().__init__()
        def _stream_payload(self,*_args,**_kwargs): raise AssertionError('No transport allowed.')
    monkeypatch.setattr(core,'Core',OfflineCore)
    result=forge_evals.run_worker(config)
    assert result['status']=='harness_error' and not result['control_valid']
    assert not result['success'] and result['actual_model_requests']==0
    assert 'task_refusal' not in result and 'Verified explicit thinking-off' in result['error']


@pytest.mark.parametrize('invalid_control',[False,True])
def test_runtime_pause_is_measured_but_actual_wire_control_failure_stays_invalid(tmp_path,monkeypatch,invalid_control):
    import core,forge_evals,forge_inference
    from test_forge_core import Engine
    config=offline_worker_config(tmp_path,'api-pagination')
    class OfflineCore(Engine):
        def __init__(self,base=None): super().__init__()
        def _stream_payload(self,payload,cancel_event=None):
            raise ValueError('The model context cannot fit this request.')
            yield
    monkeypatch.setattr(core,'Core',OfflineCore)
    if invalid_control:
        original=forge_inference.Core._agent_payload
        def wrong(data,*args,**kwargs):
            payload=original(data,*args,**kwargs); payload['options']['num_ctx']=4096; return payload
        monkeypatch.setattr(forge_inference.Core,'_agent_payload',staticmethod(wrong))
    result=forge_evals.run_worker(config)
    assert result['status']=='paused' and not result['success'],result
    assert result['oracle_verified'] and result['oracle']['checks']
    assert result['control_valid'] is (not invalid_control)
    assert bool(result['control_errors']) is invalid_control
    assert result['actual_model_requests']==1 and 'input_tokens' in result


def test_successful_worker_controls_and_effects_match_preserved_harness_with_native_discovery(tmp_path,monkeypatch):
    import core,forge_evals,importlib.util,sqlite3
    from test_forge_core import Engine,call
    previous=Path(__file__).parent.parent/'forge-5.0-evaluation-matched/classification-repair/raw-harness/forge_evals.py'
    if not previous.is_file(): pytest.skip('Preserved old evaluator is unavailable.')
    spec=importlib.util.spec_from_file_location('evaluation_before_classification_repair',previous); old=importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    task=next(row for row in load_suite()['tasks'] if row['id']=='api-pagination')
    class OfflineCore(Engine):
        def __init__(self,base=None):
            super().__init__([{'tool_calls':[call('write_file',{'path':'service.py','content':task['reference_files']['service.py']})]},
                              {'tool_calls':[call('evaluation_check',{})]},{'content':'Implemented and verified.'}])
        def _stream_payload(self,payload,cancel_event=None):
            offered={schema['function']['name'] for schema in payload.get('tools',[])}
            next_calls=self.rounds[0].get('tool_calls',[]) if self.rounds else []
            if next_calls and next_calls[0]['function']['name']=='evaluation_check' and 'evaluation_check' not in offered:
                self.requests.append(payload)
                yield {'message':{'tool_calls':[call('tools_load',{'names':['evaluation_check']})]},
                    'done':True,'done_reason':'stop','eval_count':12,'prompt_eval_count':250,
                    'prompt_eval_cached_count':100,'eval_duration':2000000000}
                return
            yield from super().stream_agent(payload,cancel_event)
    monkeypatch.setattr(core,'Core',OfflineCore)
    outcomes=[]; calls=[]; effects=[]
    for label,worker in (('before',old.run_worker),('after',forge_evals.run_worker)):
        config=offline_worker_config(tmp_path/label,'api-pagination'); result=worker(config); outcomes.append(result)
        effects.append((Path(config['workspace'])/'service.py').read_text())
        with sqlite3.connect(Path(config['profile'])/'state/forge.sqlite3') as db:
            calls.append(db.execute('select name,status from invocations order by rowid').fetchall())
    assert all(result['success'] and result['control_valid'] and result['oracle_verified'] for result in outcomes),outcomes
    assert effects[0]==effects[1]==task['reference_files']['service.py']
    assert calls[0]==[('write_file','completed'),('evaluation_check','completed')]
    assert calls[1]==[('write_file','completed'),('tools_load','completed'),('evaluation_check','completed')]
    # Native registration preserves transport controls and effects while
    # accounting truthfully for the extra real discovery round.
    for field, extra in (('rounds',1),('tool_calls',1),('input_tokens',250),('output_tokens',12),('reported_requests',1)):
        assert outcomes[1][field]==outcomes[0][field]+extra,field
    assert outcomes[0]['estimated_requests']==outcomes[1]['estimated_requests']==0
    assert outcomes[1]['requests'][:1]+outcomes[1]['requests'][2:]==outcomes[0]['requests']
    assert all(row['wire_think'] is False for result in outcomes for row in result['wire_requests'])
