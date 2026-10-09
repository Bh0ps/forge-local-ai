import pytest

from test_forge_core import service


def tracked(svc):
    svc.jobs._launch=lambda run:None
    goal=svc.goal_create({'text':'Original accepted work','tasks':[
        {'id':'first','text':'First requirement','status':'completed','evidence':['Original evidence']},
        {'id':'second','text':'Second requirement','status':'pending'}]})
    launch=svc.jobs.start({'text':'Execute the accepted goal','goal_id':goal['id'],'mode':'goal'})
    return svc.store.goal(goal['id']),svc.store.run(launch['id'])


def test_user_scope_revision_uses_current_preserved_requirements(tmp_path):
    svc=service(tmp_path)
    try:
        goal,run=tracked(svc)
        revised=svc.dispatch('goal_update',{**goal,'request':'Accepted revised work',
            'tasks':[{'id':'replacement','text':'Revised requirement','status':'pending'}]})
        assert revised['scope_revision']==goal['scope_revision']+1
        assert len(revised['requirement_history'])==2
        issues=svc.validate_run_completion(run)
        assert any('replacement' in issue for issue in issues) and not any('second' in issue for issue in issues)
        svc.dispatch('goal_task_update',{'id':goal['id'],'task_id':'replacement',
            'expected_revision':revised['revision'],'status':'completed','evidence':['Current checked result']})
        assert svc.validate_run_completion(run)==[]
    finally:svc.shutdown()


def test_model_progress_cannot_replace_accepted_tasks_or_history(tmp_path):
    svc=service(tmp_path)
    try:
        goal,run=tracked(svc)
        with pytest.raises(ValueError):
            svc.store.update_goal_progress(goal['id'],{'tasks':[{'id':'replacement','text':'Weaker replacement','status':'completed'}]})
        with pytest.raises(ValueError):
            svc.store.update_goal_progress(goal['id'],{'requirement_history':[]})
        assert any('second' in issue for issue in svc.validate_run_completion(run))
        assert svc.store.goal(goal['id'])['requirement_history']==goal['requirement_history']
    finally:svc.shutdown()


def test_task_api_rejects_stale_revision_and_unknown_id(tmp_path):
    svc=service(tmp_path)
    try:
        goal,_=tracked(svc)
        data={'id':goal['id'],'task_id':'second','expected_revision':goal['revision'],'status':'in_progress'}
        updated=svc.dispatch('goal_task_update',data)
        assert updated['scope_revision']==goal['scope_revision']
        with pytest.raises(ValueError,match='revision|changed'):
            svc.dispatch('goal_task_update',data)
        with pytest.raises(ValueError,match='identity|task|Task'):
            svc.dispatch('goal_task_update',{**data,'task_id':'missing','expected_revision':updated['revision']})
    finally:svc.shutdown()


def test_bootstrap_and_goal_views_share_guidance_and_progress(tmp_path):
    svc=service(tmp_path)
    try:
        goal,run=tracked(svc)
        svc.store.update_run(run['id'],planner_assignment={'state':'unavailable','reason':'Advisory deadline'})
        active=svc.dispatch('goals',{'active_only':True})['goals']
        assert svc.bootstrap()['version']=='5.0.2' and svc.bootstrap()['goals']==active
        detailed=svc.dispatch('goal_get',{'id':goal['id']})
        assert detailed['planner_assignment']['state']=='unavailable'
        assert detailed['progress_summary']['total']==2 and detailed['progress_summary']['verified']==0
    finally:svc.shutdown()


def test_human_acceptance_is_explicit_scoped_and_absent_from_model_tools(tmp_path):
    svc=service(tmp_path)
    try:
        goal,run=tracked(svc)
        names={schema['function']['name'] for schema in svc.jobs.registry.schemas(run,['tools'],all_tools=True)}
        assert 'goal_task_accept' not in names
        data={'id':goal['id'],'run_id':run['id'],'task_id':'second','expected_revision':goal['revision']}
        with pytest.raises(ValueError,match='reviewed'):
            svc.dispatch('goal_task_accept',data)
        result=svc.dispatch('goal_task_accept',{**data,'note':'I reviewed the second requirement and accept its current result.'})
        assert 'second' in result['goal']['verified_task_ids']
        assert result['goal']['tasks'][1]['status']=='completed'
        with pytest.raises(ValueError,match='changed'):
            svc.dispatch('goal_task_accept',{**data,'note':'Stale acceptance must not overwrite newer evidence.'})
    finally:svc.shutdown()


def test_human_acceptance_cannot_use_an_old_attempt_or_unknown_outcome(tmp_path):
    svc=service(tmp_path)
    try:
        goal,run=tracked(svc)
        svc.store.update_run(run['id'],status='paused')
        latest=svc.jobs.start({'text':'Current attempt','goal_id':goal['id'],'chat_id':run['chat_id'],'mode':'goal'})
        data={'id':goal['id'],'run_id':run['id'],'task_id':'second','expected_revision':svc.store.goal(goal['id'])['revision'],
              'note':'Reviewed the current result.'}
        with pytest.raises(ValueError,match='main run'):
            svc.dispatch('goal_task_accept',data)
        svc.store.invocation('unknown',latest['id'],'write_file',{'path':'result.txt','content':'unknown'})
        svc.store.invocation_state('unknown','outcome_unknown')
        with pytest.raises(ValueError,match='unknown|unresolved|interrupted'):
            svc.dispatch('goal_task_accept',{**data,'run_id':latest['id']})
        assert not svc.store.entities('human_reviews')
    finally:svc.shutdown()


def test_new_goal_in_builder_chat_does_not_inherit_an_unrelated_brief(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        folder=tmp_path/'project';folder.mkdir()
        project=svc.create_project({'path':str(folder),'name':'Project'})
        chat=svc.store.create_chat(project['id'])
        old=svc.get_builder().save({'project_id':project['id'],'chat_id':chat['id'],
            'objective':'Unrelated saved Builder work','requirements':['Other requirement']})
        goal=svc.goal_create({'chat_id':chat['id'],'project_id':project['id'],'text':'New ordinary goal'})
        accepted=svc.jobs.start({'text':'New ordinary goal','chat_id':chat['id'],'project_id':project['id'],
            'goal_id':goal['id'],'mode':'goal'})
        run=svc.store.run(accepted['id'])
        assert svc.associated_builder(run) is None
        assert not any(schema.get('verification_contract',{}).get('builder_id')==old['id'] for schema in svc.extra_tool_schemas(run))
        packet,_,builder=svc.get_collaboration().scope_packet(run)
        assert builder is None and 'brief' not in packet
        assert all('Other requirement' not in issue for issue in svc.validate_run_completion(run))
    finally:svc.shutdown()
