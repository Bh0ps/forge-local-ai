"""Crash and rapid-answer scenarios preserve complete tool protocol rounds."""
import threading

import pytest

from test_forge_core import Engine, call, finished, service
from test_forge_interaction import QUESTIONS, pending


def test_crash_after_asking_repairs_protocol_before_answer_becomes_available(tmp_path):
    svc=service(tmp_path,Engine([{'content':'Using the selected approach.'}]))
    launch=svc.jobs._launch
    svc.jobs._launch=lambda run:None
    try:
        accepted=svc.jobs.start({'text':'Ask for my approach.'})
        question_call=call('request_user_input',QUESTIONS)
        svc.store.add_message(accepted['chat_id'],'assistant','',tool_calls=[question_call])
        run=svc.store.update_run(accepted['id'],rounds=1,pending_calls=[question_call],next_tool=0,status='waiting_question')
        invocation=run['id']+':1:0'
        svc.store.invocation(invocation,run['id'],'request_user_input',QUESTIONS)
        svc.store.invocation_state(invocation,'running')
        result=svc.get_interaction().ask(run,QUESTIONS,invocation)
        svc.store.recover()
        assert not svc.store.unknown_actions(run['id'])
        assert svc.dispatch('pending_questions',{'run_id':run['id']})['questions']==[]
        with pytest.raises(ValueError,match='tool round'):
            svc.dispatch('answer_question',{'question_id':result['question_id'],'answers':{'approach':{'option':'Small change'}}})
        svc.jobs._launch=launch
        resumed=svc.jobs.resume(run['id'])
        question=pending(svc,resumed)[0]
        assert question['id']==result['question_id']
        assert [message['role'] for message in svc.store.get_chat(run['chat_id'])['messages']]==['user','assistant','tool']
        svc.dispatch('answer_question',{'question_id':question['id'],'answers':{'approach':{'option':'Small change'}}})
        assert finished(svc,resumed)['status']=='completed'
        messages=svc.core.requests[0]['messages']
        assert [message['role'] for message in messages if message['role']!='system']==['user','assistant','tool','user']
        with svc.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?',(run['id'],)).fetchone()[0]==1
        assert len(svc.store.entities('questions'))==1
    finally:svc.shutdown()


def test_fast_answer_cannot_interleave_before_remaining_tool_results(tmp_path):
    engine=Engine([{'tool_calls':[call('request_user_input',QUESTIONS),call('web_fetch',{'url':'https://example.com/'})]},
        {'content':'Using your preference.'}])
    svc=service(tmp_path,engine)
    reached=threading.Event(); release=threading.Event()
    tool_message=svc.store.tool_message
    def delayed_result(invocation,index,name,content):
        if index==1:
            reached.set()
            assert release.wait(5),'Second tool result was not released.'
        return tool_message(invocation,index,name,content)
    svc.store.tool_message=delayed_result
    svc.web_fetch=lambda url:pytest.fail('The mixed question round must not execute its later web action.')
    try:
        run=svc.jobs.start({'text':'Ask which approach I prefer.'})
        assert reached.wait(5)
        question=svc.store.entities('questions')[0]
        assert not question.get('ready')
        assert svc.dispatch('pending_questions',{'run_id':run['id']})['questions']==[]
        with pytest.raises(ValueError,match='tool round'):
            svc.dispatch('answer_question',{'question_id':question['id'],'answers':{'approach':{'option':'Small change'}}})
        release.set()
        ready=pending(svc,run)[0]
        assert ready['id']==question['id']
        svc.dispatch('answer_question',{'question_id':ready['id'],'answers':{'approach':{'option':'Small change'}}})
        assert finished(svc,run)['status']=='completed'
        messages=engine.requests[1]['messages']
        assert [message['role'] for message in messages if message['role']!='system']==['user','assistant','tool','tool','user']
    finally:
        release.set()
        svc.shutdown()
