import pytest

from forge_drafts import DraftManager
from forge_store import ForgeStore


@pytest.mark.parametrize('mode', ['goal', 'plan', 'do', 'builder', 'todo', None])
def test_mode_draft_survives_restart_and_has_its_own_confirmation_digest(tmp_path, mode):
    store = ForgeStore(tmp_path)
    scope = {'scope_kind': 'new_chat'}
    drafts = DraftManager(store)
    saved = drafts.save({**scope, 'expected_revision': 0, 'content': {'text': 'Normal task text', 'command_mode': mode}})
    assert DraftManager(ForgeStore(tmp_path)).get(scope)['content']['command_mode'] == mode
    before = drafts.confirmation_state({**scope, 'expected_revision': saved['revision']})['digest']
    changed = drafts.save({**scope, 'expected_revision': 1, 'content': {'text': 'Normal task text', 'command_mode': None if mode else 'do'}})
    assert drafts.confirmation_state({**scope, 'expected_revision': changed['revision']})['digest'] != before
    assert 'command_mode' not in drafts.save({**scope, 'expected_revision': 2}, clear=True)['content']


@pytest.mark.parametrize('mode', ['pause', 'unknown', {}, [], True, 1])
def test_mode_validation_preserves_existing_draft_atomically(tmp_path, mode):
    drafts = DraftManager(ForgeStore(tmp_path))
    scope = {'scope_kind': 'new_chat'}
    before = drafts.save({**scope, 'expected_revision': 0, 'content': {'text': 'Keep me', 'command_mode': 'plan'}})
    with pytest.raises(ValueError, match='supported chat command mode'):
        drafts.save({**scope, 'expected_revision': 1, 'content': {'text': 'Rejected', 'command_mode': mode}})
    assert drafts.get(scope) == before


def test_builder_brief_draft_cannot_inherit_chat_mode(tmp_path):
    store = ForgeStore(tmp_path)
    builder = store.save_entity('builders', {'project_id': None})
    with pytest.raises(ValueError, match='supported chat command mode'):
        DraftManager(store).save({'scope_kind': 'builder', 'builder_id': builder['id'], 'expected_revision': 0,
                                 'content': {'command_mode': 'goal'}})
