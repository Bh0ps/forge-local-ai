"""Storage behavior that matters for recovery, project isolation and compaction."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data')


@pytest.fixture
def project(store, tmp_path):
    folder = tmp_path / 'project'
    folder.mkdir()
    return store.create_project('Example', folder)


def test_reopening_preserves_chat_and_message_metadata(store, project):
    chat = store.create_chat(project['id'], title='Build a feature', model='local-model')
    user = store.add_message(chat['id'], 'user', '  Preserve my whitespace.\n', images=['encoded image'])
    assistant = store.add_message(chat['id'], 'assistant', '', thinking='reasoning output',
                                  tool_calls=[{'function': {'name': 'read_file', 'arguments': {'path': 'a.py'}}}])
    tool = store.add_message(chat['id'], 'tool', 'print(1)', tool_name='read_file', status='complete')
    final = store.add_message(chat['id'], 'assistant', 'Done',
                             sources=[{'title': 'Docs', 'url': 'https://example.com'}], status='completed')
    reopened = Store(store.data_dir)
    restored = reopened.get_chat(chat['id'])
    assert restored['project_id'] == project['id']
    assert restored['model'] == 'local-model'
    assert restored['messages'] == [user, assistant, tool, final]
    assert user['content'] == '  Preserve my whitespace.\n'
    assert user['id'] < assistant['id'] < tool['id'] < final['id']
    assert reopened.get_project(project['id']) == project
    assert 'messages' not in reopened.list_chats()[0]


def test_compaction_keeps_all_raw_messages_and_tracks_context_boundary(store):
    chat = store.create_chat()
    messages = [store.add_message(chat['id'], 'user' if i % 2 == 0 else 'assistant', f'Message {i}')
                for i in range(8)]
    store.set_summary(chat['id'], 'Earlier conversation summary', messages[3]['id'])
    restored = Store(store.data_dir).get_chat(chat['id'])
    assert restored['summary'] == 'Earlier conversation summary'
    assert restored['messages'] == messages
    assert [m for m in restored['messages'] if m['id'] > restored['compacted_through']] == messages[4:]
    with pytest.raises(ValueError, match='backwards'):
        store.set_summary(chat['id'], 'Stale summary', messages[1]['id'])
    assert store.get_chat(chat['id'])['summary'] == 'Earlier conversation summary'


def test_compaction_rejects_another_chats_message(store):
    one, two = store.create_chat(), store.create_chat()
    foreign = store.add_message(two['id'], 'user', 'Other conversation')
    with pytest.raises(ValueError, match='does not belong'):
        store.set_summary(one['id'], 'Wrong conversation', foreign['id'])
    assert store.get_chat(one['id'])['compacted_through'] == 0


def test_projects_isolate_task_updates_and_chat_lists(store, project, tmp_path):
    other_path = tmp_path / 'other'
    other_path.mkdir()
    other = store.create_project('Other', other_path)
    task = store.create_task(project['id'], 'Fix the parser')
    assert task['status'] == 'pending'
    assert store.update_task(project['id'], task['id'], 'in_progress')['status'] == 'in_progress'
    with pytest.raises(ValueError, match='not found'):
        store.update_task(other['id'], task['id'], 'completed')
    assert store.list_tasks(project['id'])[0]['status'] == 'in_progress'
    assert store.list_tasks(other['id']) == []
    one = store.create_chat(project['id'])
    two = store.create_chat(other['id'])
    standalone = store.create_chat()
    assert [chat['id'] for chat in store.list_chats(project['id'])] == [one['id']]
    assert {chat['id'] for chat in store.list_chats()} == {one['id'], two['id'], standalone['id']}


def test_rename_and_model_update_preserve_saved_messages(store):
    chat = store.create_chat()
    message = store.add_message(chat['id'], 'user', 'Hello')
    store.rename_chat(chat['id'], 'A useful conversation')
    store.update_chat_model(chat['id'], 'qwen3.5:9b')
    current = store.get_chat(chat['id'])
    assert current['title'] == 'A useful conversation'
    assert current['model'] == 'qwen3.5:9b'
    assert current['messages'] == [message]


def test_concurrent_connections_do_not_lose_messages(store):
    chat = store.create_chat()
    stores = [Store(store.data_dir) for _ in range(4)]
    def save(index):
        return stores[index % 4].add_message(chat['id'], 'assistant', f'Reply {index}')
    with ThreadPoolExecutor(max_workers=8) as workers:
        messages = list(workers.map(save, range(80)))
    restored = store.get_chat(chat['id'])['messages']
    assert len(restored) == 80
    assert len({message['id'] for message in restored}) == 80
    assert restored == sorted(messages, key=lambda message: message['id'])
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('operation', [
    lambda s: s.get_project('missing'),
    lambda s: s.create_chat('missing'),
    lambda s: s.list_chats('missing'),
    lambda s: s.get_chat('missing'),
    lambda s: s.add_message('missing', 'user', 'hello'),
    lambda s: s.rename_chat('missing', 'title'),
    lambda s: s.update_chat_model('missing', 'model'),
    lambda s: s.set_summary('missing', '', 0),
    lambda s: s.list_tasks('missing'),
    lambda s: s.create_task('missing', 'task'),
    lambda s: s.update_task('missing', 'missing', 'completed'),
])
def test_missing_entities_raise_value_error(store, operation):
    with pytest.raises(ValueError, match='not found'):
        operation(store)


@pytest.mark.parametrize('role, content, fields', [
    ('invented', 'text', {}),
    ('user', None, {}),
    ('user', 'x' * 1_000_001, {}),
    ('user', 'text', {'unexpected': 1}),
    ('assistant', 'text', {'thinking': []}),
    ('assistant', 'text', {'tool_calls': {}}),
    ('assistant', 'text', {'sources': [{'score': float('nan')}]}),
    ('assistant', 'text', {'status': 'made-up'}),
    ('user', 'text', {'images': [123]}),
    ('user', 'text', {'images': ['x'] * 5}),
], ids=['bad-role', 'no-content', 'oversized-content', 'unknown-field', 'bad-thinking',
        'bad-tool-calls', 'non-json-sources', 'bad-status', 'bad-image', 'too-many-images'])
def test_invalid_messages_are_rejected_without_partial_saves(store, role, content, fields):
    chat = store.create_chat()
    with pytest.raises(ValueError):
        store.add_message(chat['id'], role, content, **fields)
    assert store.get_chat(chat['id'])['messages'] == []


def test_names_paths_and_statuses_are_validated(store, project, tmp_path):
    for name in ('', ' ', 'x' * 161, None):
        with pytest.raises(ValueError):
            store.create_project(name, tmp_path)
    with pytest.raises(ValueError):
        store.create_project('Missing', tmp_path / 'not-created')
    assert not (tmp_path / 'not-created').exists()
    file = tmp_path / 'file.txt'
    file.write_text('Keep this file', encoding='utf-8')
    with pytest.raises(ValueError):
        store.create_project('A file', file)
    with pytest.raises(ValueError):
        store.create_chat(title='x' * 201)
    with pytest.raises(ValueError):
        store.create_chat(model='x' * 301)
    with pytest.raises(ValueError):
        store.create_task(project['id'], '')
    task = store.create_task(project['id'], 'Task')
    with pytest.raises(ValueError):
        store.update_task(project['id'], task['id'], 'unknown')
    assert file.read_text(encoding='utf-8') == 'Keep this file'


def test_sql_like_values_are_data(store, tmp_path):
    title = "Robert'); DROP TABLE chats;--"
    project = store.create_project(title, tmp_path)
    chat = store.create_chat(project['id'], title=title)
    assert store.get_chat(chat['id'])['title'] == title
    with pytest.raises(ValueError):
        store.get_chat("' OR 1=1 --")
    assert len(store.list_chats()) == 1


def test_environment_data_directory_wins(monkeypatch, tmp_path):
    configured = tmp_path / 'persistent-volume'
    monkeypatch.setenv('SIDEKICK_DATA_DIR', str(configured))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    assert Store().data_dir == configured.resolve()


def test_windows_default_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv('SIDEKICK_DATA_DIR', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert Store().data_dir == (tmp_path / 'Sidekick').resolve()


def test_container_default_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv('SIDEKICK_DATA_DIR', raising=False)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert Store().data_dir == (tmp_path / '.local' / 'share' / 'sidekick').resolve()


def test_invalid_data_directory_is_friendly_value_error(tmp_path):
    file = tmp_path / 'not-a-directory'
    file.write_text('Keep me', encoding='utf-8')
    with pytest.raises(ValueError, match='Cannot open'):
        Store(file)
    assert file.read_text(encoding='utf-8') == 'Keep me'


def test_invalid_unicode_and_whitespace_ids_are_friendly_errors(store):
    chat = store.create_chat()
    message = store.add_message(chat['id'], 'user', 'Preserved')
    with pytest.raises(ValueError, match='Unicode'):
        store.add_message(chat['id'], 'assistant', '\ud800')
    with pytest.raises(ValueError, match='Invalid chat id'):
        store.get_chat(' ' + chat['id'])
    assert store.get_chat(chat['id'])['messages'] == [message]


def test_limited_chat_returns_latest_messages_in_order_with_full_count(store, monkeypatch):
    chat = store.create_chat()
    messages = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant',
                                  f'Turn {index}', images=['old attachment'] if index == 0 else [])
                for index in range(12)]
    decoded = []
    decode = store._message
    def tracked(row):
        decoded.append(row['id'])
        return decode(row)
    monkeypatch.setattr(store, '_message', tracked)
    tail = store.get_chat(chat['id'], limit=3)
    assert tail['messages'] == messages[-3:]
    assert tail['total_messages'] == 12
    assert decoded == [message['id'] for message in messages[-3:]]
    assert store.get_chat(chat['id'], limit=20)['messages'] == messages
    assert store.get_chat(chat['id'])['messages'] == messages
    assert store.get_chat(chat['id'])['total_messages'] == 12
    assert store.get_chat(chat['id'], limit=1)['messages'] == messages[-1:]


@pytest.mark.parametrize('limit', [0, -1, 10_001, True, False, 1.0, '10'])
def test_limited_chat_rejects_invalid_limits(store, limit):
    chat = store.create_chat()
    with pytest.raises(ValueError, match='Message limit'):
        store.get_chat(chat['id'], limit=limit)


def test_active_chat_loads_only_uncompacted_messages_and_preserves_originals(store, monkeypatch):
    chat = store.create_chat()
    messages = [store.add_message(chat['id'], 'user' if index % 2 == 0 else 'assistant', f'Turn {index}')
                for index in range(10)]
    assert store.get_active_chat(chat['id'])['messages'] == messages
    store.set_summary(chat['id'], 'First six messages summarized', messages[5]['id'])
    decoded = []
    decode = store._message
    def tracked(row):
        decoded.append(row['id'])
        return decode(row)
    monkeypatch.setattr(store, '_message', tracked)
    active = store.get_active_chat(chat['id'])
    assert active['messages'] == messages[6:]
    assert active['total_messages'] == 10
    assert active['summary'] == 'First six messages summarized'
    assert active['compacted_through'] == messages[5]['id']
    assert decoded == [message['id'] for message in messages[6:]]
    assert Store(store.data_dir).get_chat(chat['id'])['messages'] == messages
    store.set_summary(chat['id'], 'All messages summarized', messages[-1]['id'])
    assert store.get_active_chat(chat['id'])['messages'] == []
    assert store.get_active_chat(chat['id'])['total_messages'] == 10


def test_empty_chat_retrieval_counts_and_missing_chat_errors(store):
    chat = store.create_chat()
    for saved in (store.get_chat(chat['id']), store.get_chat(chat['id'], limit=1), store.get_active_chat(chat['id'])):
        assert saved['messages'] == []
        assert saved['total_messages'] == 0
    with pytest.raises(ValueError, match='not found'):
        store.get_chat('missing', limit=1)
    with pytest.raises(ValueError, match='not found'):
        store.get_active_chat('missing')


def test_sql_memory_search_is_scoped_and_reads_compacted_messages_without_decoding_metadata(store, project, tmp_path, monkeypatch):
    other_path = tmp_path / 'other-project'
    other_path.mkdir()
    other = store.create_project('Other', other_path)
    own = store.create_chat(project['id'], title='Own decisions')
    old = store.add_message(own['id'], 'user', 'The ORCHID parser must preserve whitespace.', images=['preserved image'])
    store.add_message(own['id'], 'tool', 'ORCHID tool output should not match.')
    store.add_message(own['id'], 'system', 'ORCHID system message should not match.')
    store.set_summary(own['id'], 'Parser decision saved', old['id'])
    foreign = store.create_chat(other['id'])
    store.add_message(foreign['id'], 'assistant', 'ORCHID foreign project secret')
    standalone = store.create_chat(title='Standalone decisions')
    store.add_message(standalone['id'], 'assistant', 'ORCHID standalone decision')
    def forbidden(row):
        pytest.fail('Memory search decoded full message metadata')
    monkeypatch.setattr(store, '_message', forbidden)
    assert store.search_memory(project['id'], 'orchid') == [
        {'chat_id': own['id'], 'title': 'Own decisions', 'snippet': 'The ORCHID parser must preserve whitespace.'}]
    assert store.search_memory(None, 'ORCHID') == [
        {'chat_id': standalone['id'], 'title': 'Standalone decisions', 'snippet': 'ORCHID standalone decision'}]
    assert Store(store.data_dir).get_chat(own['id'])['messages'][0] == old


def test_sql_memory_search_bounds_results_snippets_and_recent_chat_scan(store, project):
    old = store.create_chat(project['id'], title='Old chat')
    store.add_message(old['id'], 'user', 'needle in old chat')
    recent = store.create_chat(project['id'], title='Recent chat')
    for index in range(5):
        store.add_message(recent['id'], 'assistant', ('x' * 5000) + f' NEEDLE {index} ' + ('y' * 5000))
    matches = store.search_memory(project['id'], 'needle', limit=2)
    assert len(matches) == 2
    assert all(match['chat_id'] == recent['id'] and len(match['snippet']) <= 920 for match in matches)
    assert 'NEEDLE 0' in matches[0]['snippet']
    assert 'NEEDLE 1' in matches[1]['snippet']
    assert len(store.search_memory(project['id'], 'needle', limit=10, chat_limit=1)) == 5
    assert len(store.search_memory(project['id'], 'needle', limit=10, chat_limit=2)) == 6


def test_sql_memory_search_handles_unicode_and_treats_sql_as_literal_data(store):
    chat = store.create_chat()
    store.add_message(chat['id'], 'assistant', "ÉCOLE uses the literal value ' OR 1=1 -- in a sample.")
    assert len(store.search_memory(None, 'école')) == 1
    assert len(store.search_memory(None, "' OR 1=1 --")) == 1
    assert store.search_memory(None, 'missing%_value') == []


@pytest.mark.parametrize('query, options', [
    ('', {}), ('   ', {}), (None, {}), ('x' * 1001, {}),
    ('valid', {'limit': 0}), ('valid', {'limit': 101}), ('valid', {'limit': True}),
    ('valid', {'chat_limit': 0}), ('valid', {'chat_limit': 1001}), ('valid', {'chat_limit': 1.0}),
], ids=['empty', 'spaces', 'nontext', 'long-query', 'zero-results', 'many-results',
        'bool-results', 'zero-chats', 'many-chats', 'float-chats'])
def test_sql_memory_search_validates_bounds(store, query, options):
    with pytest.raises(ValueError):
        store.search_memory(None, query, **options)


def test_sql_memory_search_rejects_unknown_project(store):
    with pytest.raises(ValueError, match='not found'):
        store.search_memory('missing', 'anything')


def test_context_preference_default_is_read_only_and_survives_restart(store):
    assert store.get_settings() == {'context': 32_768}
    assert store.update_settings({}) == {'context': 32_768}
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM settings').fetchone()[0] == 0
    assert store.update_settings({'context': 65_536}) == {'context': 65_536}
    assert Store(store.data_dir).get_settings() == {'context': 65_536}


@pytest.mark.parametrize('context', [2_048, 4_096, 32_768, 65_536, 131_072, 262_144])
def test_context_preference_accepts_exact_step_values(store, context):
    assert store.update_settings({'context': context}) == {'context': context}
    assert store.get_settings() == {'context': context}


@pytest.mark.parametrize('settings', [
    {'context': 0}, {'context': 2_047}, {'context': 262_145}, {'context': 3_072},
    {'context': True}, {'context': False}, {'context': 32_768.0}, {'context': '32768'},
    {'context': None}, {'context': []}, {'context': {}}, {'context': float('nan')},
    {'context': 65_536, 'unknown': 'must not save'}, {'unknown': 1}, [], None, False,
], ids=['zero', 'below-min', 'above-max', 'wrong-step', 'true', 'false', 'float', 'string',
        'none-value', 'list-value', 'dict-value', 'nan', 'mixed-unknown', 'unknown',
        'list-settings', 'no-settings', 'bool-settings'])
def test_invalid_context_settings_do_not_modify_saved_preference(store, settings):
    store.update_settings({'context': 8_192})
    with pytest.raises(ValueError):
        store.update_settings(settings)
    assert Store(store.data_dir).get_settings() == {'context': 8_192}
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute('SELECT key, value FROM settings').fetchall() == [('context', 8_192)]


def test_additive_settings_upgrade_preserves_history_tasks_summary_and_backups(store, project):
    chat = store.create_chat(project['id'])
    message = store.add_message(chat['id'], 'user', 'Keep the existing project.')
    store.set_summary(chat['id'], 'Previously saved continuity', message['id'])
    task = store.create_task(project['id'], 'Keep the saved task')
    backup = store.data_dir / 'backups' / 'existing.bin'
    backup.parent.mkdir()
    backup.write_bytes(b'Existing undo backup')
    before = store.get_chat(chat['id'])
    # Simulate the previous schema in this isolated temporary test database.
    with sqlite3.connect(store.db_path) as connection:
        connection.execute('DROP TABLE settings')
    upgraded = Store(store.data_dir)
    assert upgraded.get_settings() == {'context': 32_768}
    upgraded.update_settings({'context': 49_152})
    assert upgraded.get_chat(chat['id']) == before
    assert upgraded.get_project(project['id']) == project
    assert upgraded.list_tasks(project['id']) == [task]
    assert backup.read_bytes() == b'Existing undo backup'
