import pytest
from test_forge_core import Engine, service, finished

def test_plan_is_saved_and_build_preserves_order_and_exact_request(tmp_path):
    engine=Engine([{'content':'## Plan\n1. Inspect the existing parser.\n2. Implement Unicode handling.\n3. Validate the behavior.'}])
    svc=service(tmp_path,engine)
    try:
        request='Update the parser 🌍. Keep existing files.'
        run=svc.command({'text':'/plan\n'+request})
        assert finished(svc,run)['status']=='completed'
        plan=svc.dispatch('plan_get',{'id':run['id']})
        assert plan['request']==request and plan['status']=='ready'
        assert [t['text'] for t in plan['tasks']]==['Inspect the existing parser.','Implement Unicode handling.','Validate the behavior.']
        assert __import__('pathlib').Path(plan['path']).read_text(encoding='utf-8')==plan['markdown']
        built=svc.dispatch('plan_build',{'run_id':run['id']})
        finished(svc,built)
        goal=svc.store.goal(built['goal_id'])
        assert goal['request']==request and [{k:v for k,v in t.items() if k!='id'} for t in goal['tasks']]==plan['tasks']
        assert svc.store.run(built['id'])['instructions'].endswith(plan['markdown'])
        execution=engine.requests[1]
        current_request=next(m['content'] for m in reversed(execution['messages']) if m['role']=='user')
        assert current_request.startswith('Start implementing the reviewed plan now.')
        assert current_request!=request and current_request.endswith(request)
        assert 'Implementation is starting.' in '\n'.join(m['content'] for m in execution['messages'] if m['role']=='system')
        assert 'Execution has not started.' not in '\n'.join(m['content'] for m in execution['messages'] if m['role']=='system')
        assert goal['reviewed_plan']==plan['markdown'] and goal['plan_id']==plan['id']
        assert svc.dispatch('plan_build',{'run_id':run['id']})['id']==built['id']
    finally: svc.shutdown()

@pytest.mark.parametrize('answer', [{'content':'','thinking':'Only thinking.'}, {'content':'Partial plan','done_reason':'length'}])
def test_empty_or_truncated_plan_does_not_offer_build(tmp_path,answer):
    def response(data,cancel):
        yield {'message':{k:v for k,v in answer.items() if k!='done_reason'},'done':True,'done_reason':answer.get('done_reason','stop')}
    svc=service(tmp_path,Engine([response,response,response]))
    try:
        run=svc.command({'text':'/plan Inspect parser'})
        assert finished(svc,run)['status']=='paused'
        assert svc.dispatch('plans',{})['plans']==[]
        with pytest.raises(ValueError):svc.dispatch('plan_build',{'run_id':run['id']})
    finally:svc.shutdown()


@pytest.mark.parametrize('text', ['/goal', '/goal Validate the parser'])
def test_goal_after_plan_starts_reviewed_checklist_instead_of_replanning(tmp_path,text):
    svc=service(tmp_path,Engine([{'content':'1. Inspect input.\n2. Validate output.'}]))
    try:
        planned=svc.command({'text':'/plan Validate the parser'})
        finished(svc,planned)
        built=svc.command({'text':text,'chat_id':planned['chat_id']})
        finished(svc,built)
        goal=svc.store.goal(built['goal_id'])
        assert [task['text'] for task in goal['tasks']]==['Inspect input.','Validate output.']
        assert goal['request']=='Validate the parser'
        assert svc.store.run(built['id'])['mode']=='goal'
        assert svc.store.run(built['id'])['request'].startswith('Start implementing the reviewed plan now.')
        assert len(svc.store.entities('goals'))==1
    finally:svc.shutdown()


def test_fast_completed_goal_is_not_reset_to_running_after_start(tmp_path):
    svc=service(tmp_path,Engine())
    original=svc.jobs.start
    svc.jobs._launch=lambda run:None
    def complete_before_return(data):
        result=original(data)
        goal=svc.store.goal(data['goal_id'])
        tasks=[{**task,'status':'completed','evidence':['Input inspected.']} for task in goal['tasks']]
        svc.store.save_goal({**goal,'tasks':tasks,'status':'completed','checkpoint':'All ordered tasks completed.'})
        svc.store.update_run(result['id'],status='completed')
        return result
    svc.jobs.start=complete_before_return
    try:
        goal=svc.goal_create({'text':'Inspect input.'})
        built=svc.goal_resume({'id':goal['id']})
        saved=svc.store.goal(goal['id'])
        assert saved['status']=='completed' and saved['chat_id']==built['chat_id']
        assert svc.dispatch('goals',{'active_only':True})['goals']==[]
        with pytest.raises(ValueError,match='completed'):svc.goal_resume({'id':goal['id']})
    finally:svc.shutdown()


def test_failed_goal_admission_restores_review_checkpoint(tmp_path):
    svc=service(tmp_path)
    try:
        goal=svc.goal_create({'text':'Inspect input.'})
        svc.store.update_settings({'model':''})
        with pytest.raises(ValueError,match='Select an installed model'):svc.goal_resume({'id':goal['id']})
        saved=svc.store.goal(goal['id'])
        assert saved['status']=='ready' and saved['checkpoint']==goal['checkpoint']
        assert saved['next_action']==goal['next_action'] and svc.store.runs()==[]
    finally:svc.shutdown()


def test_active_goals_require_live_run_and_existing_chat_without_erasing_history(tmp_path):
    svc=service(tmp_path)
    try:
        chat=svc.store.create_chat()
        goal=svc.goal_create({'text':'Inspect input.','chat_id':chat['id']})
        run=svc.store.create_run({'chat_id':chat['id'],'goal_id':goal['id']})
        svc.store.save_goal({**goal,'status':'paused'})
        active=svc.dispatch('goals',{'active_only':True})['goals']
        assert len(active)==1 and active[0]['id']==goal['id']
        assert active[0]['status']=='queued' and active[0]['run_id']==run['id']
        assert svc.bootstrap()['goals']==active
        svc.store.update_run(run['id'],status='completed')
        assert svc.dispatch('goals',{'active':True})['goals']==[]
        assert len(svc.dispatch('goals',{})['goals'])==1
        svc.store.update_run(run['id'],status='running')
        svc.store.save_goal({**goal,'status':'completed'})
        assert svc.store.active_goals()==[]
        svc.store.save_goal({**goal,'status':'running'})
        with svc.store._connection(transaction='write') as db:
            db.execute('DELETE FROM chats WHERE id=?',(chat['id'],))
        assert svc.store.active_goals()==[]
        assert len(svc.dispatch('goals',{})['goals'])==1
        svc.store.update_run(run['id'],status='completed')
    finally:svc.shutdown()


def test_command_discovery_only_advertises_canonical_todo(tmp_path):
    svc=service(tmp_path)
    try:
        names=[command['name'] for command in svc.dispatch('commands',{})['commands']]
        assert names.count('todo')==1 and 'to-do' not in names
    finally:svc.shutdown()


def test_build_and_goal_resume_select_main_run_when_child_inherits_goal(tmp_path):
    svc=service(tmp_path,Engine([{'content':'1. Inspect input.'}]))
    try:
        planned=svc.command({'text':'/plan Validate input'})
        finished(svc,planned)
        svc.jobs._launch=lambda run:None
        main=svc.plan_build({'run_id':planned['id']})
        child_chat=svc.store.create_chat()
        svc.store.create_run({'chat_id':child_chat['id'],'goal_id':main['goal_id'],'parent_id':main['id']})
        assert svc.plan_build({'run_id':planned['id']})['id']==main['id']
        assert svc.goal_resume({'id':main['goal_id']})['id']==main['id']
        assert [goal['run_id'] for goal in svc.store.active_goals()]==[main['id']]
    finally:
        for run in svc.store.runs():svc.store.update_run(run['id'],status='completed')
        svc.shutdown()


def test_resume_legacy_goal_updates_stale_checkpoint_without_changing_request(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        chat=svc.store.create_chat()
        goal=svc.goal_create({'text':'Inspect input.','chat_id':chat['id']})
        run=svc.jobs.start({'text':goal['request'],'chat_id':chat['id'],'goal_id':goal['id'],'mode':'goal'})
        svc.store.update_run(run['id'],status='paused')
        svc.goal_resume({'id':goal['id']})
        assert svc.store.run(run['id'])['request']==goal['request']
        assert svc.store.goal(goal['id'])['checkpoint']=='Implementation is continuing from the saved checklist.'
        context=svc.jobs._context(svc.store.run(run['id']),[])
        assert 'Execution has not started.' not in '\n'.join(message['content'] for message in context)
        assert any('authorized implementation' in message['content'] for message in context)
    finally:
        for run in svc.store.runs():svc.store.update_run(run['id'],status='completed')
        svc.shutdown()


def test_goal_chat_identity_is_committed_with_request_before_launch_and_restart(tmp_path):
    svc=service(tmp_path)
    restored=None
    try:
        svc.jobs._launch=lambda run:None
        goal=svc.goal_create({'text':'Inspect input.'})
        run=svc.jobs.start({'text':goal['request'],'goal_id':goal['id'],'mode':'goal'})
        admitted=svc.store.goal(goal['id'])
        assert admitted['chat_id']==run['chat_id'] and not admitted['external_edits']
        # Recover immediately from the admission checkpoint, without the service's
        # post-launch save that used to establish the conversation identity.
        svc.shutdown()
        restored=service(tmp_path)
        restored.jobs._launch=lambda run:None
        resumed=restored.goal_resume({'id':goal['id']})
        assert resumed['id']==run['id'] and resumed['chat_id']==run['chat_id']
        assert len(restored.store.runs())==1 and len(restored.store.list_chats())==1
    finally:
        for current in (svc,restored):
            if current:
                for run in current.store.runs():current.store.update_run(run['id'],status='completed')
                current.shutdown()


def test_child_cannot_rebind_goal_and_other_main_chat_is_rejected_atomically(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        goal=svc.goal_create({'text':'Inspect input.'})
        main=svc.jobs.start({'text':'Start inspecting input.','goal_id':goal['id'],'mode':'goal'})
        child=svc.jobs.start({'text':'Research input.','goal_id':goal['id'],'parent_id':main['id'],'readonly':True})
        assert svc.store.goal(goal['id'])['chat_id']==main['chat_id']
        assert child['chat_id']!=main['chat_id']
        before=[chat['id'] for chat in svc.store.list_chats()]
        with pytest.raises(ValueError,match='another conversation'):
            svc.jobs.start({'text':'Try another main chat.','goal_id':goal['id'],'mode':'goal'})
        assert [chat['id'] for chat in svc.store.list_chats()]==before
        assert len(svc.store.runs())==2
    finally:
        for run in svc.store.runs():svc.store.update_run(run['id'],status='completed')
        svc.shutdown()

def test_todo_runs_the_saved_plan_and_alias_is_supported(tmp_path):
    svc=service(tmp_path,Engine([{'content':'1. Inspect input.\n2. Validate output.'}]))
    try:
        planned=svc.command({'text':'/plan Validate the parser'})
        finished(svc,planned)
        built=svc.command({'text':'/to-do','chat_id':planned['chat_id']})
        finished(svc,built)
        goal=svc.store.goal(built['goal_id'])
        assert [t['text'] for t in goal['tasks']]==['Inspect input.','Validate output.']
        assert __import__('pathlib').Path(goal['path']).name=='TODO.md'
    finally:svc.shutdown()

def test_plan_continues_truncated_round_without_losing_visible_steps(tmp_path):
    def truncated(data,cancel):
        yield {'message':{'content':'1. Inspect input.'},'done':True,'done_reason':'length','eval_count':1024}
    svc=service(tmp_path,Engine([truncated,{'content':'2. Validate output.'}]))
    try:
        run=svc.command({'text':'/plan Validate the parser'})
        assert finished(svc,run)['status']=='completed'
        plan=svc.dispatch('plan_get',{'id':run['id']})
        assert [t['text'] for t in plan['tasks']]==['Inspect input.','Validate output.']
        assert not svc.store.entities('goals')
    finally:svc.shutdown()
