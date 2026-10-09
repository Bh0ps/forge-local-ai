"""Successful diff excerpts free context without changing executed evidence."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import threading

import pytest

from context_window import estimated_prompt_tokens, prompt_budget, response_budget
from core import AGENT_SYSTEM_PROMPT
from forge_evals import evaluation_contract_factory, evaluation_schema_adapter, load_suite
from forge_store import encode
from prompt_compiler import PromptCompiler, mutation_diff_context
from test_forge_core import Engine, call, finished, service
from test_forge5_checkpoints import journal


# Exact 2,762-byte app.js written by the repaired guided Todo diagnostic.
CAPTURED_TODO_WRITE='''// Task list implementation. Preserves public element IDs:
// task-form, task-input, filter, task-list, remaining.

const STORAGE_KEY = 'todo-flow.tasks';

const els = {
  form: document.getElementById('task-form'),
  input: document.getElementById('task-input'),
  filter: document.getElementById('filter'),
  list: document.getElementById('task-list'),
  remaining: document.getElementById('remaining'),
};

let tasks = load();
let filter = 'all';

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) { /* ignore corrupt storage */ }
  return [];
}

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
  } catch (e) { /* storage may be unavailable; ignore */ }
}

function addTask(text) {
  tasks.push({ id: Date.now().toString(36) + Math.random().toString(36).slice(2), text, done: false });
  save();
  render();
}

function toggleTask(id, done) {
  const task = tasks.find((t) => t.id === id);
  if (task) {
    task.done = done;
    save();
    render();
  }
}

function clearDone() {
  tasks = tasks.filter((t) => !t.done);
  save();
  render();
}

function render() {
  const list = els.list;
  list.innerHTML = '';

  const visible = tasks.filter((t) => {
    if (filter === 'active') return !t.done;
    if (filter === 'done') return t.done;
    return true;
  });

  visible.forEach((t) => {
    const li = document.createElement('li');
    li.className = 'task' + (t.done ? ' done' : '');
    li.setAttribute('data-id', t.id);

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'task-check';
    checkbox.checked = t.done;
    checkbox.setAttribute('aria-label', 'Mark "' + t.text + '" as done');
    checkbox.addEventListener('change', (e) => toggleTask(t.id, e.target.checked));

    const label = document.createElement('label');
    label.className = 'task-label';
    label.htmlFor = 'task-' + t.id;
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(' ' + t.text));

    li.appendChild(label);
    list.appendChild(li);
  });

  const remaining = tasks.filter((t) => !t.done).length;
  els.remaining.textContent = remaining + ' remaining';
}

els.form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = els.input.value.trim();
  if (!text) return; // blank tasks must not be added
  addTask(text);
  els.input.value = '';
  els.input.focus();
});

els.filter.addEventListener('change', (e) => {
  filter = els.filter.value;
  render();
});

els.filter.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    filter = els.filter.value;
    render();
  }
});

render();
'''


def successful_result():
    return {'artifact':'a'*32,'result':{'ok':True,'path':'app.js','sha256':'b'*64,
        'previous_sha256':'c'*64,'bytes':4096,'backup_id':'d'*32,'backup_path':'owned-backup.bin',
        'diff':'+漢字🌍 new code\n'*400}}


@pytest.mark.parametrize('name',['write_file','edit_file','apply_patch','patch_files','restore_file'])
def test_projection_bounds_only_successful_diffs_and_preserves_all_file_identities(name):
    value=successful_result();original=deepcopy(value)
    if name in ('apply_patch','patch_files'):value={'artifact':value['artifact'],'result':{'ok':True,'changes':[value['result'],{**value['result'],'path':'styles.css'}]}}
    original=deepcopy(value);projected=mutation_diff_context(name,value)
    assert value==original and projected is not None
    entries=projected['result'].get('changes',[projected['result']])
    saved=original['result'].get('changes',[original['result']])
    assert sum(len(row['diff'].encode('utf-8')) for row in entries)<=512
    for row,raw in zip(entries,saved):
        assert row['diff_excerpt']['total_characters']==len(raw['diff'])
        assert row['diff_excerpt']['total_bytes']==len(raw['diff'].encode('utf-8'))
        assert raw['diff'].startswith(row['diff'])
        assert {k:v for k,v in row.items() if k not in ('diff','diff_excerpt')}=={k:v for k,v in raw.items() if k!='diff'}
    assert projected['context_projection']['retrieval']['id']==original['artifact']


@pytest.mark.parametrize('field,value',[(key,True) for key in
    ('denied','not_executed','outcome_unknown','cancelled','timed_out','partial','stale','unavailable','rolled_back','interrupted','truncated','output_truncated')]+
    [('ok',False),('error','Action failed'),('complete',False),('available',False),('status','running')])
def test_failed_uncertain_unavailable_and_partial_results_are_never_projected(field,value):
    wrapped=successful_result();wrapped['result'][field]=value;original=deepcopy(wrapped)
    assert mutation_diff_context('write_file',wrapped) is None and wrapped==original


@pytest.mark.parametrize('name',['read_file','read_files','evaluation_check','run_command','document_read','artifact_read'])
def test_reads_and_necessary_check_details_are_never_projected(name):
    wrapped=successful_result();wrapped['result']['content']='Full fresh source body';wrapped['result']['checks']=[{'detail':'Necessary failure'}]
    assert mutation_diff_context(name,wrapped) is None


def test_partial_batch_and_unregistered_artifact_remain_unmodified():
    wrapped=successful_result();wrapped['result']={'ok':True,'changes':[wrapped['result'],{'ok':False,'path':'failed.txt','diff':'x'*900}]}
    assert mutation_diff_context('apply_patch',wrapped) is None
    wrapped=successful_result();wrapped['artifact']='not-a-saved-id'
    assert mutation_diff_context('write_file',wrapped) is None


def test_context_requires_completed_owned_journal_and_matching_published_artifact(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    try:
        svc.store.update_settings({'permission_profile':'full_access','memory_enabled':False,'memory_suggestions':False})
        root=tmp_path/'project';root.mkdir();project=svc.create_project({'name':'Journal projection gate','path':str(root)})
        accepted=svc.jobs.start({'project_id':project['id'],'text':'Write and verify an owned source file.'})
        run=svc.store.run(accepted['id']);args={'path':'app.js','content':'const value = 1;\n'*300}
        svc.store.add_message(run['chat_id'],'assistant','',tool_calls=[call('write_file',args)])
        result=svc.jobs.registry.execute(run,'write_file',args,threading.Event())
        invocation,artifact=journal(svc,run,1,0,'write_file',args,result);wrapped={'artifact':artifact,'result':result}
        raw=encode(wrapped)
        def published():return next(m['content'] for m in reversed(svc.jobs._context(run,[])) if m['role']=='tool')
        assert json.loads(published())['context_projection']['retrieval']['id']==artifact
        for status in ('prepared','running','outcome_unknown','failed'):
            svc.store.invocation_state(invocation,status,wrapped)
            assert published()==raw
        svc.store.invocation_state(invocation,'completed',wrapped)
        forged=encode({**wrapped,'artifact':'0'*32})
        with svc.store._connection(transaction='write') as db:
            message_id=db.execute('SELECT message_id FROM invocations WHERE id=?',(invocation,)).fetchone()[0]
            db.execute('UPDATE messages SET content=? WHERE id=?',(forged,message_id))
        assert published()==forged
        assert (root/'app.js').read_text()==args['content']
    finally:svc.shutdown()


def test_captured_todo_write_reaches_next_verification_inference_at_8k(tmp_path,monkeypatch):
    assert len(CAPTURED_TODO_WRITE.encode())==2762
    assert sha256(CAPTURED_TODO_WRITE.encode()).hexdigest()=='e4640967c5779968d3e8b5f8e73d7aed765fa10424d806710b7946b0bc9aa06e'
    observed=[]
    def verify(data,cancel):
        observed.append(deepcopy(data))
        yield {'message':{'tool_calls':[call('evaluation_check',{})]},'done':True}
    task=next(t for t in load_suite()['tasks'] if t['id']=='todo-flow')
    engine=Engine([{'tool_calls':[call('list_files',{})]},
        {'tool_calls':[call('read_file',{'path':name}) for name in task['files']]},
        {'tool_calls':[call('write_file',{'path':'app.js','content':CAPTURED_TODO_WRITE})]},verify,{'content':'Verified completed task.'}])
    svc=service(tmp_path,engine)
    try:
        svc.store.update_settings({'tokens':2048,'permission_profile':'full_access','thinking':False,
            'memory_enabled':False,'memory_suggestions':False,'web':False,'browser_tools':False,'computer_tools':False,
            'auto_delegate':False,'goal_review_enabled':False})
        root=tmp_path/'project';root.mkdir()
        for name,text in task['files'].items():(root/name).write_text(text,encoding='utf-8',newline='')
        project=svc.create_project({'name':'Captured Todo mutation','path':str(root)})
        goal=svc.goal_create({'project_id':project['id'],'text':task['prompt']})
        factory=evaluation_contract_factory(task,svc.store)
        svc.jobs.registry.schemas=evaluation_schema_adapter(svc.jobs.registry.schemas,factory)
        execute=svc.jobs.registry.execute
        def checked(run,name,args,cancel,**kwargs):
            if name!='evaluation_check':return execute(run,name,args,cancel,**kwargs)
            assert (root/'app.js').read_text(encoding='utf-8')==CAPTURED_TODO_WRITE
            return {'passed':True,'oracle_verified':True,'checks':[{'requirement':label,'passed':True} for label in factory(run)['expected_checks']]}
        monkeypatch.setattr(svc.jobs.registry,'execute',checked)
        recipe=(Path(__file__).parent/'assets/starter-library/skills/plan-and-build/SKILL.md').read_text(encoding='utf-8')
        guidance='Source: skill captured-plan (Plan & build)\n'+recipe+'\n'+(
            'Supporting workflow: Build the frontend. Read its complete recipe with skills_read before using it.\n'
            'Supporting workflow: Make UI responsive and accessible. Read its complete recipe with skills_read before using it.\n')
        monkeypatch.setattr(svc.jobs,'_skill_guidance',lambda run,schemas:svc.store.update_run(run['id'],skill_instructions=guidance))
        accepted=svc.jobs.start({'project_id':project['id'],'goal_id':goal['id'],'text':task['prompt'],'mode':'goal',
            'instructions':'Work only in the provided disposable project. No commands, network, cloud, computer or publishing tools are available. Use evaluation_check for acceptance evidence and repair failures. Complete the requested task; do not ask for unspecified optional preferences.'})
        assert finished(svc,accepted)['status']=='completed'
        assert len(observed)==1 and len(engine.requests)==5
        data=observed[0];budget=prompt_budget(8192,response_budget(8192,2048))
        assert estimated_prompt_tokens(data['messages'],data['tools'],AGENT_SYSTEM_PROMPT)<=budget
        assistant=next(m for m in reversed(data['messages']) if m.get('tool_calls'))
        assert assistant['tool_calls'][0]['function']['arguments']=={'path':'app.js','content':CAPTURED_TODO_WRITE}
        wire=json.loads(next(m['content'] for m in reversed(data['messages']) if m['role']=='tool'))
        assert wire['result']['diff_excerpt']['total_characters']>2500
        assert wire['context_projection']['retrieval']=={'tool':'artifact_read','id':wire['artifact'],'field':'result.diff'}
        assert recipe in '\n'.join(m['content'] for m in data['messages'] if m['role']=='system')
        assert any(m['role']=='user' and m['content']==task['prompt'] for m in data['messages'])
        with svc.store._connection() as db:
            row=db.execute("SELECT result,message_id FROM invocations WHERE run_id=? AND name='write_file'",(accepted['id'],)).fetchone()
            raw=json.loads(row['result']);message=db.execute('SELECT content FROM messages WHERE id=?',(row['message_id'],)).fetchone()[0]
            assert db.execute("SELECT COUNT(*) FROM invocations WHERE run_id=? AND name='write_file'",(accepted['id'],)).fetchone()[0]==1
        artifact=json.loads((svc.store.home/'artifacts'/(wire['artifact']+'.json')).read_text(encoding='utf-8'))
        assert artifact['result']==raw['result']==json.loads(message)['result']
        assert artifact['arguments']==assistant['tool_calls'][0]['function']['arguments']
        assert raw['result']['diff'].startswith(wire['result']['diff']) and len(raw['result']['diff'])==wire['result']['diff_excerpt']['total_characters']
        assert {k:v for k,v in wire['result'].items() if k not in ('diff','diff_excerpt')}=={k:v for k,v in raw['result'].items() if k!='diff'}
        unprojected=deepcopy(data['messages'])
        next(m for m in reversed(unprojected) if m['role']=='tool')['content']=encode(raw)
        assert estimated_prompt_tokens(unprojected,data['tools'],AGENT_SYSTEM_PROMPT)>budget
        contexts=[e for e in svc.store.events(accepted['id']) if e['type']=='context']
        assert next(e for e in contexts if e['round']==4)['token_breakdown']==PromptCompiler.metrics(data['messages'],data['tools'])
        assert svc.store.goal(goal['id'])['tasks'][0]['status']=='completed'
    finally:svc.shutdown()
