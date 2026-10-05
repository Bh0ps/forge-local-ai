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
