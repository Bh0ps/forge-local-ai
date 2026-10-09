from concurrent.futures import ThreadPoolExecutor
import pytest

from forge_drafts import DraftManager
from forge_store import ForgeStore


def test_draft_restart_clear_and_compare_and_swap(tmp_path):
    store = ForgeStore(tmp_path)
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    first = DraftManager(store)
    assert first.get(scope)['revision'] == 0
    saved = first.save({**scope, 'expected_revision': 0, 'content': {'text': 'User work'}})
    restored = DraftManager(ForgeStore(tmp_path)).get(scope)
    assert restored['content']['text'] == 'User work'
    assert restored['revision'] == saved['revision'] == 1
    with pytest.raises(ValueError, match='another window'):
        first.save({**scope, 'expected_revision': 0, 'content': {'text': 'stale overwrite'}})
    assert first.get(scope)['content']['text'] == 'User work'
    cleared = first.dispatch('draft_clear', {**scope, 'expected_revision': 1})
    assert cleared['revision'] == 2 and cleared['content']['text'] == ''
    assert first.dispatch('draft_clear', {**scope, 'expected_revision': 2})['revision'] == 3
    with pytest.raises(ValueError, match='another window'):
        first.save({**scope, 'expected_revision': 1, 'content': {'text': 'late upload'}})


def test_scope_ids_separate_chat_and_projects(tmp_path):
    store = ForgeStore(tmp_path)
    folder = tmp_path / 'project'; folder.mkdir()
    project = store.create_project('Project', str(folder))
    chat = store.create_chat(project['id'], 'Chat', '')
    drafts = DraftManager(store)
    scope = {'scope_kind': 'chat', 'project_id': project['id'], 'chat_id': chat['id']}
    drafts.save({**scope, 'expected_revision': 0, 'content': {'text': 'Private project draft'}})
    assert drafts.get({'scope_kind': 'new_chat', 'project_id': None})['content']['text'] == ''
    with pytest.raises(ValueError, match='another project'):
        drafts.get({**scope, 'project_id': None})


def test_draft_refs_are_retained_but_stale_and_images_rejected(tmp_path):
    store = ForgeStore(tmp_path)
    drafts = DraftManager(store, lambda _: [{'id': 'skill-enabled', 'enabled': True}, {'id': 'skill-disabled', 'enabled': False}])
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    saved = drafts.save({**scope, 'expected_revision': 0, 'content': {'text': 'Keep this', 'document_ids': ['gone'],
                        'skills': ['skill-enabled', 'skill-disabled'], 'space_ids': ['gone-note']}})
    assert saved['content']['document_ids'] == ['gone']
    assert {r['id'] for r in saved['stale_refs']} == {'gone', 'gone-note', 'skill-disabled'}
    with pytest.raises(ValueError, match='unsent images'):
        drafts.save({**scope, 'expected_revision': 1, 'content': {'text': '', 'images': ['base64']}})


def test_builder_draft_is_not_saved_brief_or_gate_evidence(tmp_path):
    store = ForgeStore(tmp_path)
    builder = store.save_entity('builders', {'project_id': None, 'title': 'Saved', 'revision': 4, 'gates': {'functional': {'status': 'failed'}}})
    scope = {'scope_kind': 'builder', 'project_id': None, 'builder_id': builder['id']}
    drafts = DraftManager(store)
    drafts.save({**scope, 'expected_revision': 0, 'content': {'brief': {'title': 'Unsent', 'revision': 4}, 'base_revision': 4}})
    assert store.entity('builders', builder['id'])['title'] == 'Saved'
    assert store.entity('builders', builder['id'])['gates']['functional']['status'] == 'failed'
    with pytest.raises(ValueError, match='runtime/check evidence'):
        drafts.save({**scope, 'expected_revision': 1, 'content': {'brief': {'gates': {'functional': {'status': 'passed'}}}}})


def test_draft_concurrent_windows_cannot_both_commit_same_revision(tmp_path):
    store = ForgeStore(tmp_path)
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    def save(text):
        try:
            return DraftManager(store).save({**scope, 'expected_revision': 0, 'content': {'text': text}})['revision']
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, ['First', 'Second']))
    assert sorted(map(str, result)) == ['1', 'conflict']


@pytest.mark.parametrize('data', [
    {'scope_kind': 'other'}, {'scope_kind': 'chat', 'chat_id': '../escape'},
    {'scope_kind': 'new_chat', 'chat_id': 'unrelated'}, {'scope_kind': 'builder', 'builder_id': ''},
])
def test_draft_rejects_invalid_scope(tmp_path, data):
    with pytest.raises(ValueError):
        DraftManager(ForgeStore(tmp_path)).get(data)


def test_draft_rejects_nonfinite_large_and_bool_revisions(tmp_path):
    manager = DraftManager(ForgeStore(tmp_path))
    scope = {'scope_kind': 'new_chat', 'project_id': None}
    for bad in (True, -1, '0'):
        with pytest.raises(ValueError):
            manager.save({**scope, 'expected_revision': bad, 'content': {'text': 'hello'}})
    with pytest.raises(ValueError, match='72,000'):
        manager.save({**scope, 'expected_revision': 0, 'content': {'text': 'x' * 72001}})
