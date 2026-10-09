"""Actual coordinator/outbox paths, using a fake Telegram transport only."""
import json
from unittest.mock import Mock
import pytest
from forge_channels import TELEGRAM_COMMANDS, telegram_chunks
from forge_builder_guide import accepted_brief
from forge_service import ForgeService
from test_forge_channels import env, configured, inbound, update
from test_forge_workspaces import ScriptedEngine


def request(env, config, number=1, text='Hello'):
    env.manager.receive_telegram(config['id'], update(number, text=text))
    env.manager.tick(poll=False)
    row=inbound(env,config['id'],number)
    return env.service.store.run(row['run_id']) if row['run_id'] else row


def complete(env, run, answer, owned=True):
    env.service.store.add_message(run['chat_id'],'assistant',answer,**({'interaction_run_id':run['id']} if owned else {}))
    env.service.store.update_run(run['id'],status='completed')
    env.service.store.event(run['id'],'done',status='completed')


def test_native_command_menu_is_registered_on_explicit_connect(env):
    config=configured(env)
    menu=next(body for method,body,_ in env.bot.calls if method=='setMyCommands')
    assert menu['commands']==TELEGRAM_COMMANDS
    assert env.service.store.entity('channels',config['id'])['commands_ready'] is True


def test_late_completion_exports_own_response_in_order_and_preserves_emoji(env):
    config=configured(env,include_content=True)
    first=request(env,config)
    answer='Here is your answer. '+('✨🚀 useful output\n'*550)
    complete(env,first,answer)
    # A later completed turn already exists when the older event is journalled.
    second=env.service.jobs.start({'chat_id':first['chat_id'],'text':'Later turn','model':'fixture'})
    env.service.store.add_message(first['chat_id'],'assistant','UNRELATED FUTURE RESPONSE',interaction_run_id=second['id'])
    env.manager.tick(poll=False)
    sends=[body['text'] for method,body,_ in env.bot.calls if method=='sendMessage']
    assert ''.join(sends)==answer
    assert len(sends)>1 and all(len(text.encode('utf-16-le'))//2<=3500 for text in sends)
    env.manager.tick(poll=False)
    assert [body['text'] for method,body,_ in env.bot.calls if method=='sendMessage']==sends


def test_legacy_result_is_bounded_to_request_not_later_conversation(env):
    config=configured(env,include_content=True); first=request(env,config)
    complete(env,first,'Original legacy answer',owned=False)
    later=env.service.jobs.start({'chat_id':first['chat_id'],'text':'Later','model':'fixture'})
    env.service.store.add_message(later['chat_id'],'assistant','Private future answer')
    env.manager.tick(poll=False)
    sends=[body['text'] for method,body,_ in env.bot.calls if method=='sendMessage']
    assert sends==['Original legacy answer']


def test_uncertain_first_chunk_blocks_remaining_chunks_without_replay(env):
    config=configured(env,include_content=True); run=request(env,config)
    complete(env,run,'Long reply. '*800)
    env.manager._journal_events()
    outbox=env.manager.outbox()
    first=next(row for row in outbox if ':part:' not in row.get('kind','') and row['kind']=='done' and not json.loads(next(r['payload'] for r in _rows(env) if r['id']==row['id'])).get('_forge_previous'))
    env.manager._outcome(first['id'],'outcome_unknown')
    env.manager.tick(poll=False)
    assert not any(method=='sendMessage' for method,_,_ in env.bot.calls)
    assert any(row['status']=='pending' for row in env.manager.outbox())


def _rows(env):
    with env.service.store._connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM channel_outbox')]


def test_unknown_command_replies_to_approved_sender_but_not_strangers(env):
    config=configured(env)
    request(env,config,text='/missing')
    request(env,config,2,text='/plan')
    env.manager.receive_telegram(config['id'],update(3,sender=99,text='/missing'))
    env.manager.tick(poll=False)
    messages=[body for method,body,_ in env.bot.calls if method=='sendMessage']
    assert len(messages)==2 and all(body['chat_id']=='42' for body in messages)
    assert 'Unknown command' in messages[0]['text'] and '/plan followed' in messages[1]['text']
    assert not env.service.store.runs()


def test_builder_coder_profile_is_readonly_and_survives_turns_restart_and_off(env):
    profile=env.service.agent_save({'name':'Fixture coder','role':'coder','enabled':True,'model':'fixture'})
    config=configured(env,agent_profile_id=profile['id'])
    first=request(env,config,text='/builder A playful habit tracker')
    assert first['readonly'] and first['builder_guided'] and first['mode']=='builder'
    assert 'at most two' in first['instructions']
    complete(env,first,'Who is this for: just you, or a group?')
    second=request(env,config,2,text='Just me, with colourful progress.')
    assert second['readonly'] and second['builder_guided']
    complete(env,second,'Proposed brief: personal habit tracker with accessible progress cards and keyboard navigation.')
    assert 'Just me' in accepted_brief(env.service.store,second['chat_id'])
    restarted=ForgeService(core=ScriptedEngine(),data_dir=env.service.store.home)
    restarted.jobs._launch=Mock()
    try:
        next_run=restarted.jobs.start({'chat_id':second['chat_id'],'text':'Add a weekly view','model':'fixture'})
        assert restarted.store.run(next_run['id'])['builder_guided']
        restarted.store.update_run(next_run['id'],status='completed')
        restarted.command({'chat_id':second['chat_id'],'text':'/builder off'})
        normal=restarted.jobs.start({'chat_id':second['chat_id'],'text':'Hello','model':'fixture'})
        assert not restarted.store.run(normal['id'])['builder_guided']
    finally: restarted.shutdown()


def test_builder_build_requires_explicit_acceptance_and_a_connected_project(env,tmp_path):
    folder=tmp_path/'project'; folder.mkdir(); project=env.service.store.create_project('Demo',str(folder))
    config=configured(env,project_id=project['id'])
    run=request(env,config,text='/builder A notes app')
    assert not env.service.store.entities('goals')
    complete(env,run,'Proposed brief: create a notes app with search, empty states and keyboard navigation.')
    build=request(env,config,2,text='/build')
    assert build['mode']=='goal' and not build['readonly']
    assert build['permission_ceiling']=='full_access'
    assert 'notes app' in build['request'] and 'keyboard navigation' in build['request']
    assert not env.service.store.entity('builder_guides',run['chat_id'])['active']
    assert env.manager.receive_telegram(config['id'],update(2,text='/build'))['duplicate']
    assert len(env.service.store.entities('goals'))==1


def test_accepting_builder_excludes_earlier_unrelated_chat_content(env):
    chat=env.service.store.create_chat(None,'Old conversation','fixture')
    env.service.store.add_message(chat['id'],'user','Unrelated private older discussion')
    run=env.service.command({'chat_id':chat['id'],'text':'/builder A garden journal','model':'fixture'})
    saved=env.service.store.run(run['id']);complete(env,saved,'Proposed brief: a garden journal with search.')
    brief=accepted_brief(env.service.store,chat['id'])
    assert 'garden journal' in brief and 'Unrelated private' not in brief


def test_chunking_keeps_unicode_and_text_exactly():
    text='🚀'*5000+' Done'
    assert ''.join(telegram_chunks(text))==text
    assert all(len(part.encode('utf-16-le'))//2<=3500 for part in telegram_chunks(text))


def test_pending_questions_and_plain_text_answers_work_without_desktop(env):
    config=configured(env,include_content=True); run=request(env,config)
    interaction=env.service.get_interaction()
    env.service.store.invocation('question-call',run['id'],'request_user_input',{})
    question=interaction.ask(run,{'questions':[{'id':'audience','header':'Audience','question':'Who will use it?',
        'options':[{'label':'Just me'},{'label':'A team'}]}]},'question-call')
    env.service.store.update_run(run['id'],status='waiting_question')
    interaction.publish_pending(run['id'])
    env.manager.tick(poll=False)
    messages=[body['text'] for method,body,_ in env.bot.calls if method=='sendMessage']
    assert any('Who will use it?' in text and '/answer' in text for text in messages)
    request(env,config,2,text='Just me')
    saved=env.service.store.entity('questions',question['question_id'])
    assert saved['status']=='answered' and saved['answers']['audience']['text']=='Just me'
    assert len(env.service.store.runs())==1
