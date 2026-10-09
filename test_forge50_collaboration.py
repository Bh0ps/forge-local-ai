import json
import threading
from types import SimpleNamespace

import pytest

from forge_collaboration import CloudCollaboration, setup_specialists
from forge_goal_review import GoalReview
from test_forge_core import service


def setup(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    svc.store.save_entity('providers',{'id':'openrouter','kind':'openrouter','url':'https://openrouter.ai/api/v1',
        'enabled':True,'remote_consent':True,'credential_ref':'synthetic-vault-reference'})
    return svc


def test_specialists_are_read_only_free_and_preserve_edited_profiles(tmp_path):
    svc=setup(tmp_path); result=setup_specialists(svc)
    assert len(result['created'])==6
    for profile in result['agents']:
        if profile.get('specialty'):
            assert profile['model']=='openrouter/free' and profile['role']=='reviewer'
            assert not {'write_file','run_command','delegate_agent'}&set(profile['tools'])
    svc.agent_save({'id':'openrouter-planner','instructions':'My accepted planning convention.'})
    again=setup_specialists(svc)
    assert not again['created'] and len(again['preserved'])==6
    assert svc.store.entity('agents','openrouter-planner')['instructions']=='My accepted planning convention.'
    svc.shutdown()


def test_planning_once_per_brief_failure_continues_locally_without_retry(tmp_path,monkeypatch):
    svc=setup(tmp_path);setup_specialists(svc)
    folder=tmp_path/'project';folder.mkdir();(folder/'README.md').write_text('Scoped project facts.')
    project=svc.create_project({'path':str(folder),'name':'fixture'})
    run=svc.jobs.start({'text':'Build the fixture','mode':'goal','project_id':project['id']})
    builder=svc.get_builder().save({'project_id':project['id'],'chat_id':run['chat_id'],'title':'Fixture',
        'objective':'Build useful behavior','directory':'.','requirements':[{'text':'Support a primary flow','acceptance':'Its action works'}]})
    calls=[]
    def reject(data): calls.append(data);raise ValueError('Free quota unavailable')
    monkeypatch.setattr(svc,'agent_start',reject)
    planner=CloudCollaboration(svc)
    updated=planner.prepare(svc.store.run(run['id']),threading.Event())
    assert updated['planner_assignment']['state']=='unavailable'
    planner.prepare(updated,threading.Event());assert len(calls)==1
    assert 'Scoped project facts.' in calls[0]['text']
    assert svc.store.entity('builders',builder['id'])['requirements']==builder['requirements']
    svc.shutdown()


def test_optional_builder_review_outage_never_claims_independent_verification(tmp_path,monkeypatch):
    svc=setup(tmp_path);folder=tmp_path/'p';folder.mkdir();project=svc.create_project({'path':str(folder)})
    run=svc.jobs.start({'text':'Build','project_id':project['id'],'mode':'goal'})
    builder=svc.get_builder().save({'project_id':project['id'],'chat_id':run['chat_id'],'objective':'Build','directory':'.','requirements':['A flow']})
    goal=svc.store.save_goal({'chat_id':run['chat_id'],'project_id':project['id'],'tasks':[{'text':'A flow',
        'requirement_id':builder['requirements'][0]['id'],'status':'completed','evidence':['Explicit human fixture acceptance']} ]})
    goal=svc.store.save_goal({**goal,'builder_id':builder['id'],'builder_revision':builder['revision']})
    svc.store.update_run(run['id'],goal_id=goal['id'])
    for gate in builder['gates']:
        svc.get_builder().check({'id':builder['id'],'expected_revision':builder['revision'],'gate':gate,
            'status':'passed','source':'manual','note':'Explicit human fixture acceptance'},human=True)
    reviewer=GoalReview(svc.jobs)
    monkeypatch.setattr(reviewer,'_check',lambda *a,**kw:(_ for _ in ()).throw(ValueError('OpenRouter free quota unavailable')))
    result=reviewer.check(svc.store.run(run['id']),'Done',{'cancel':threading.Event()},0)
    assert result=='complete'
    saved=svc.store.run(run['id']);assert saved['review']['status']=='unavailable' and not reviewer.enabled(saved)
    svc.store.update_run(run['id'],settings={**saved['settings'],'cloud_required_review':True})
    with pytest.raises(ValueError,match='quota'):reviewer.check(svc.store.run(run['id']),'Done',{'cancel':threading.Event()},0)
    svc.shutdown()


def test_completion_cannot_drop_original_goal_requirements(tmp_path):
    svc=setup(tmp_path)
    try:
        goal=svc.goal_create({'text':'Implement both required flows.','tasks':[
            {'id':'keep','text':'First flow','status':'completed','evidence':['Checked']},
            {'id':'lost','text':'Second flow','status':'pending'}]})
        run=svc.jobs.start({'text':'Finish the goal.','goal_id':goal['id']})
        original=svc.store.goal(goal['id'])
        updated=svc.store.update_goal_progress(goal['id'],{'tasks':[original['tasks'][0]]})
        assert [task['id'] for task in updated['tasks']]==['keep','lost']
        assert any('lost' in issue for issue in svc.validate_run_completion(svc.store.run(run['id'])))
        svc.store.save_goal({**original,'tasks':[{**task,'status':'completed','evidence':['Verified']} for task in original['tasks']]})
        assert svc.validate_run_completion(svc.store.run(run['id']))==[]
    finally: svc.shutdown()


def test_builder_completion_requires_current_requirement_mapping(tmp_path):
    svc=setup(tmp_path)
    try:
        folder=tmp_path/'p';folder.mkdir();project=svc.create_project({'path':str(folder)})
        chat=svc.store.create_chat(project['id'])
        builder=svc.get_builder().save({'project_id':project['id'],'chat_id':chat['id'],'directory':'.',
            'objective':'Implement two flows','requirements':['First flow','Second flow']})
        goal=svc.store.save_goal({'chat_id':chat['id'],'project_id':project['id'],'tasks':[
            {'id':'first','text':'First flow','requirement_id':builder['requirements'][0]['id'],'status':'completed','evidence':['Checked']},
            {'id':'second','text':'Second flow','requirement_id':builder['requirements'][1]['id'],'status':'completed','evidence':['Checked']}]})
        goal=svc.store.save_goal({**goal,'builder_id':builder['id'],'builder_revision':builder['revision']})
        run=svc.jobs.start({'text':'Finish','goal_id':goal['id'],'chat_id':chat['id']})
        for gate in builder['gates']:
            svc.get_builder().check({'id':builder['id'],'expected_revision':builder['revision'],'gate':gate,
                'status':'passed','source':'manual','note':'Explicit fixture acceptance'},human=True)
        assert svc.validate_run_completion(svc.store.run(run['id']))==[]
        svc.store.save_goal({**goal,'tasks':[goal['tasks'][0]]})
        issues=svc.validate_run_completion(svc.store.run(run['id']))
        assert any(builder['requirements'][1]['id'] in issue for issue in issues)
        # A model may omit the requirement field but retain the original mapped ID.
        svc.store.save_goal({**goal,'tasks':[{k:v for k,v in task.items() if k!='requirement_id'} for task in goal['tasks']]})
        assert svc.validate_run_completion(svc.store.run(run['id']))==[]
    finally: svc.shutdown()


@pytest.mark.parametrize('requirement_source',['run','live_setting'])
def test_required_builder_review_cannot_be_bypassed_by_general_switch(tmp_path,requirement_source):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    try:
        folder=tmp_path/'p';folder.mkdir();project=svc.create_project({'path':str(folder)})
        chat=svc.store.create_chat(project['id'])
        builder=svc.get_builder().save({'project_id':project['id'],'chat_id':chat['id'],'directory':'.',
            'objective':'Build','requirements':['Primary flow']})
        goal=svc.store.save_goal({'chat_id':chat['id'],'project_id':project['id'],'request':'Build','status':'ready','tasks':[
            {'id':'flow','text':'Primary flow','requirement_id':builder['requirements'][0]['id'],'status':'completed','evidence':['Checked']}]})
        run=svc.jobs.start({'text':'Finish','goal_id':goal['id'],'chat_id':chat['id'],
                            'goal_review_enabled':False,'cloud_required_review':requirement_source=='run'})
        if requirement_source=='live_setting': svc.store.update_settings({'cloud_required_review':True,'goal_review_enabled':False})
        reviewer=GoalReview(svc.jobs); saved=svc.store.run(run['id'])
        assert reviewer.enabled(saved)
        with pytest.raises(ValueError,match='consented OpenRouter'):
            reviewer.check(saved,'Complete',{'cancel':threading.Event()},0)
        assert svc.store.goal(goal['id'])['status']!='completed'
        assert not svc.store.run(run['id']).get('review_unavailable')
        # A prior optional outage must not satisfy the explicit requirement.
        svc.store.update_run(run['id'],review_unavailable=True)
        assert reviewer.enabled(svc.store.run(run['id']))
        assert not reviewer.required({**saved,'parent_id':'parent'})
    finally: svc.shutdown()


def test_attached_notes_are_bounded_retrievable_and_cannot_escape_scope(tmp_path):
    svc=service(tmp_path);svc.jobs._launch=lambda run:None
    try:
        selected=[svc.store.save_entity('spaces',{'name':'Notes '+str(n),'notes':('Unicode reference 🌍 '+str(n)+'\n')*3000}) for n in range(5)]
        unrelated=svc.store.save_entity('spaces',{'name':'Private unrelated','notes':'Do not expose this.'})
        run=svc.jobs.start({'text':'Use the attached references.','space_ids':[space['id'] for space in selected],'context':8192})
        saved=svc.store.run(run['id']); context=svc.extra_run_context(saved)
        assert len(context.encode('utf-8'))<=4096 and 'space_note_read' in context
        assert 'Do not expose' not in context
        schema=next(s for s in svc.extra_tool_schemas(saved) if s['function']['name']=='space_note_read')
        assert unrelated['id'] not in schema['function']['parameters']['properties']['id']['enum']
        page=svc.execute_extra_tool(saved,'space_note_read',{'id':selected[0]['id'],'start':9000,'limit':1000},threading.Event())
        assert page['text']==selected[0]['notes'][9000:10000] and page['has_more']
        with pytest.raises(ValueError,match='not attached'):
            svc.execute_extra_tool(saved,'space_note_read',{'id':unrelated['id']},threading.Event())
    finally: svc.shutdown()
