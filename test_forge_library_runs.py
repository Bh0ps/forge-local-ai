"""Library controls reach the real coordinator without widening permissions."""
import threading

from test_forge_core import Engine, call, finished, service
from test_forge_interaction import QUESTIONS, pending


def install_rule(svc, tmp_path):
    package=tmp_path/'fixture-skill'; package.mkdir()
    (package/'SKILL.md').write_text('---\nname: fixture-rule\ndescription: Guidance for a fixture investigation.\n---\nKeep the fixture investigation evidence exact.\n',encoding='utf-8')
    result=svc.dispatch('skill_install',{'source':str(package)})
    assert result['ok'],result
    return next(item for item in svc.dispatch('skills')['skills'] if item['name']=='fixture-rule')


def guidance(request):
    return '\n'.join(message['content'] for message in request['messages']
                     if message['role']=='system' and 'Selected skill guidance' in message['content'])


def test_explicit_skill_reaches_model_and_cannot_expand_plan_permissions(tmp_path):
    svc=service(tmp_path,Engine())
    try:
        skill=install_rule(svc,tmp_path)
        folder=tmp_path/'project'; folder.mkdir()
        project=svc.create_project({'name':'Fixture','path':str(folder)})
        run=svc.jobs.start({'text':'Plan the fixture investigation.','mode':'plan',
                            'project_id':project['id'],'skills':[skill['id']]})
        assert finished(svc,run)['status']=='completed'
        assert 'Keep the fixture investigation evidence exact.' in guidance(svc.core.requests[0])
        assert 'cannot expand tool permissions' in guidance(svc.core.requests[0])
        assert not {'write_file','run_command'}&{s['function']['name'] for s in svc.core.requests[0]['tools']}
        assert svc.store.run(run['id'])['settings']['permission_profile']=='always_ask'
        assert skill['id'] in svc.store.run(run['id'])['active_skills']
    finally:
        svc.shutdown()


def test_turning_off_explicit_skill_removes_it_before_next_round(tmp_path):
    svc=service(tmp_path,Engine([{'tool_calls':[call('request_user_input',QUESTIONS)]},{'content':'Finished.'}]))
    try:
        skill=install_rule(svc,tmp_path)
        run=svc.jobs.start({'text':'Ask which investigation I prefer.','skills':[skill['id']]})
        question=pending(svc,run)[0]
        assert 'fixture investigation evidence' in guidance(svc.core.requests[0])
        assert svc.dispatch('skill_toggle',{'id':skill['id'],'enabled':False})['ok']
        svc.dispatch('answer_question',{'question_id':question['id'],'answers':{'approach':{'option':'Small change'}}})
        assert finished(svc,run)['status']=='completed'
        assert 'fixture investigation evidence' not in guidance(svc.core.requests[-1])
        assert skill['id'] not in svc.store.run(run['id'])['active_skills']
        assert sum(e['type']=='skill' and e.get('skill_id')==skill['id']
                   for e in svc.jobs.poll(run['id'])['events'])==1
    finally:
        svc.shutdown()


def test_large_explicit_skill_has_bounded_unicode_excerpt_and_retrieval_id(tmp_path):
    svc=service(tmp_path,Engine())
    try:
        skill=install_rule(svc,tmp_path)
        from pathlib import Path
        Path(skill['path']).write_text('---\nname: fixture-rule\n---\nKeep evidence exact.\n'+'漢字🌍 '*6000,encoding='utf-8')
        run=svc.jobs.start({'text':'Use my selected investigation rules.','skills':[skill['id']],'context':8192})
        assert finished(svc,run)['status']=='completed'
        text=svc.store.run(run['id'])['skill_instructions']
        assert 'Keep evidence exact.' in text and '漢字🌍' in text
        assert len(text.encode())<=4096
        assert 'skills_read with id '+skill['id'] in text
        assert skill['id'] in svc.store.run(run['id'])['active_skills']
    finally:
        svc.shutdown()


def test_selection_uses_current_request_and_goal_objective(tmp_path,monkeypatch):
    svc=service(tmp_path,Engine([{'tool_calls':[call('goal_update',{
        'tasks':[{'text':'Investigate the fixture regression.','status':'completed','evidence':['Fixture verified.']}],
        'checkpoint':'Fixture verified.','next_action':'Complete.'})]},{'content':'Finished.'}]))
    captured=[]
    try:
        original=svc.integrations.active_skill_instructions
        def select(project,explicit,query=''):
            captured.append(query)
            return original(project,explicit,query=query)
        monkeypatch.setattr(svc.integrations,'active_skill_instructions',select)
        goal=svc.goal_create({'text':'Investigate the fixture regression.'})
        run=svc.jobs.start({'text':'Continue the saved checkpoint.','goal_id':goal['id']})
        result=finished(svc,run)
        assert result['status']=='completed',result
        assert captured and 'Continue the saved checkpoint.' in captured[0]
        assert 'Investigate the fixture regression.' in captured[0]
    finally:
        svc.shutdown()


def test_long_original_request_keeps_recent_steering_in_skill_selection(tmp_path):
    svc=service(tmp_path)
    try:
        svc.jobs._launch=lambda run:None
        accepted=svc.jobs.start({'text':'x'*18000})
        svc.store.add_message(accepted['chat_id'],'user','Research the latest primary sources.',interaction_run_id=accepted['id'])
        updated=svc.jobs._skill_guidance(svc.store.run(accepted['id']))
        research=next(skill for skill in svc.integrations.discover_skills() if skill.get('library_id')=='research')
        assert research['id'] in updated['active_skills']
        assert 'Research with sources' in updated['skill_instructions']
    finally:
        svc.shutdown()


def test_catalog_loading_does_not_hold_coordinator_mutation_gate(tmp_path,monkeypatch):
    svc=service(tmp_path)
    entered=threading.Event(); release=threading.Event(); saved=threading.Event()
    failures=[]
    original=svc.integrations.dispatch
    def blocked(action,data):
        if action=='catalog_discover':
            entered.set(); release.wait(5)
            return {'ok':True,'entries':[]}
        return original(action,data)
    monkeypatch.setattr(svc.integrations,'dispatch',blocked)
    def discover():
        try: svc.dispatch('catalog_discover')
        except Exception as exc: failures.append(exc)
    def save():
        try: svc.dispatch('settings',{'context':16384}); saved.set()
        except Exception as exc: failures.append(exc)
    loader=threading.Thread(target=discover); writer=threading.Thread(target=save)
    try:
        loader.start(); assert entered.wait(3)
        writer.start(); assert saved.wait(1),'Catalog loading blocked unrelated settings.'
        assert svc.store.get_settings()['context']==16384
    finally:
        release.set(); loader.join(6)
        if writer.ident: writer.join(6)
        svc.shutdown()
    assert not failures
