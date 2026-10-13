"""Composer modes reuse authoritative commands without granting permissions."""
import pytest

from test_forge_core import service


@pytest.mark.parametrize('selected,mode,guided', [('do', 'chat', False),
    ('plan', 'plan', False), ('builder', 'builder', True)])
def test_selected_mode_accepts_clean_text_without_dropping_first_word(tmp_path, selected, mode, guided):
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        request = '  Inspect parser 🌍.\nKeep all existing behavior.  '
        result = svc.dispatch('command', {'command_mode': selected, 'text': request,
            'permission_profile': 'always_ask'})
        run = svc.store.run(result['id'])
        assert run['mode'] == mode and run['request'] == request.strip()
        assert run['builder_guided'] is guided
        assert run['settings']['permission_profile'] == 'always_ask'
        assert 'command_mode' not in run and 'command_mode' not in run['settings']
    finally:
        svc.shutdown()


@pytest.mark.parametrize('selected', ['goal', 'todo'])
def test_selected_checklist_mode_uses_saved_goal_contract(tmp_path, selected):
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        request = '1. Inspect parser.\n2. Verify behavior.'
        result = svc.dispatch('command', {'command_mode': selected, 'text': request})
        run = svc.store.run(result['id']); goal = svc.store.goal(run['goal_id'])
        assert run['mode'] == 'goal' and goal['request'] == request
        assert [task['text'] for task in goal['tasks']] == ['Inspect parser.', 'Verify behavior.']
        assert run['settings']['permission_profile'] == svc.store.get_settings()['permission_profile']
    finally:
        svc.shutdown()


@pytest.mark.parametrize('selected', ['goal', 'plan', 'do', 'builder', 'todo'])
def test_explicit_action_command_takes_precedence_without_starting_selected_work(tmp_path, selected):
    svc = service(tmp_path)
    try:
        result = svc.dispatch('command', {'command_mode': selected, 'text': '/new'})
        assert result['navigate'] == 'chat' and svc.store.get_chat(result['chat']['id'])
        assert not svc.store.runs() and not svc.store.entities('goals')
    finally:
        svc.shutdown()


def test_explicit_slash_mode_takes_precedence_and_legacy_strings_remain_supported(tmp_path):
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        planned = svc.dispatch('command', {'command_mode': 'goal', 'text': '/plan Inspect parser'})
        assert svc.store.run(planned['id'])['mode'] == 'plan'
        for data in ({'text': '/do Inspect parser'}, {'text': 'do Inspect parser'},
                {'text': '/do Inspect parser', 'command_mode': None}):
            done = svc.dispatch('command', data)
            assert svc.store.run(done['id'])['request'] == 'Inspect parser'
            assert svc.store.run(done['id'])['mode'] == 'chat'
        assert 'do' in {command['name'] for command in svc.dispatch('commands')['commands']}
    finally:
        svc.shutdown()


@pytest.mark.parametrize('selected', ['unknown', 'PLAN', '', 1, False, [], {}])
def test_invalid_selected_mode_has_no_run_chat_or_goal_side_effects(tmp_path, selected):
    svc = service(tmp_path)
    try:
        with pytest.raises(ValueError, match='Choose Goal'):
            svc.dispatch('command', {'command_mode': selected, 'text': '/new'})
        assert not svc.store.runs() and not svc.store.list_chats() and not svc.store.entities('goals')
    finally:
        svc.shutdown()


def test_selected_mode_does_not_change_submission_identity_or_update_admission(tmp_path):
    from types import SimpleNamespace
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        data = {'command_mode': 'do', 'text': 'Inspect parser', 'client_submission_id': 'fixture-submit'}
        first = svc.dispatch('command', data)
        assert svc.dispatch('command', data)['id'] == first['id']
        with pytest.raises(ValueError, match='different content'):
            svc.dispatch('command', {**data, 'command_mode': 'plan'})
        svc.update_manager = SimpleNamespace(applying=True)
        with pytest.raises(ValueError, match='Changes are paused'):
            svc.dispatch('command', {'command_mode': 'do', 'text': 'Another request'})
        assert len(svc.store.runs()) == 1
    finally:
        svc.update_manager = None; svc.shutdown()


@pytest.mark.parametrize('text', ['/src/file.py inspect this', '/unknown literal prompt', '/README.md'])
@pytest.mark.parametrize('selected,mode', [('do', 'chat'), ('plan', 'plan')])
def test_selected_mode_keeps_unknown_slash_heads_as_exact_prompt_text(tmp_path, text, selected, mode):
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        result = svc.dispatch('command', {'command_mode': selected, 'text': text})
        run = svc.store.run(result['id'])
        assert run['request'] == text and run['mode'] == mode
    finally:
        svc.shutdown()


@pytest.mark.parametrize('text', ['/src/file.py inspect this', '/unknown literal prompt'])
def test_legacy_unknown_slash_heads_without_selected_mode_still_reject(tmp_path, text):
    svc = service(tmp_path)
    try:
        with pytest.raises(ValueError, match='Unknown command'):
            svc.dispatch('command', {'text': text})
        assert not svc.store.runs() and not svc.store.list_chats()
    finally:
        svc.shutdown()


def test_selected_mode_retains_status_plan_and_todo_alias_precedence(tmp_path):
    svc = service(tmp_path); svc.jobs._launch = lambda run: None
    try:
        assert svc.dispatch('command', {'command_mode': 'goal', 'text': '/status'}) == {
            'run': None, 'message': 'No active run.'}
        planned = svc.dispatch('command', {'command_mode': 'do', 'text': '/plan Inspect parser'})
        assert svc.store.run(planned['id'])['mode'] == 'plan'
        assert svc.store.run(planned['id'])['request'] == 'Inspect parser'
        todo = svc.dispatch('command', {'command_mode': 'do', 'text': '/to-do 1. Inspect parser.'})
        assert svc.store.run(todo['id'])['mode'] == 'goal'
        assert svc.store.goal(todo['goal_id'])['request'] == '1. Inspect parser.'
    finally:
        svc.shutdown()
