"""Preview status patches preserve fresh SQL scope without native/network I/O."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from forge_builder import BuilderManager
from forge_chats import dispatch_chat
from forge_previews import PreviewManager
from forge_store import ForgeStore


@pytest.fixture
def preview(tmp_path, monkeypatch):
    store = ForgeStore(tmp_path / 'disposable')
    folder = tmp_path / 'project'
    folder.mkdir()
    (folder / 'index.html').write_text('Synthetic retained preview')
    project = store.create_project('Synthetic project', str(folder))
    service = SimpleNamespace(store=store, admission_lock=threading.RLock(),
        jobs=SimpleNamespace(lock=threading.RLock(), jobs={}, cancel=lambda *args, **kwargs: None))
    calls = []
    service.command_sessions = SimpleNamespace(status=lambda key: {'status': 'running', 'pid': 1},
        stop=lambda key: calls.append(key))
    manager = PreviewManager(service)
    record = store.save_entity('previews', {'id': 'b' * 32, 'project_id': project['id'],
        'builder_id': 'synthetic-builder', 'run_id': None, 'mode': 'fastapi', 'cwd': '.',
        'status': 'starting', 'session_id': 'synthetic-owned-session', 'url': 'http://127.0.0.1:12345/'})
    store.save_entity('builders', {'id': record['builder_id'], 'project_id': project['id'], 'status': 'ready'})
    manager.active[record['id']] = {'record': deepcopy(record), 'port': 12345}
    process = SimpleNamespace(children=lambda **kwargs: [], net_connections=lambda **kwargs:
        [SimpleNamespace(laddr=SimpleNamespace(port=12345), status='LISTEN')])
    monkeypatch.setattr('psutil.Process', lambda *args: process)
    value = SimpleNamespace(store=store, service=service, manager=manager, record=record,
        project=project, folder=folder, calls=calls, respond=lambda: None)
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs):
            assert not service.admission_lock._is_owned()
            value.respond()
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr('forge_previews.httpx.Client', Client)
    return value


def detach(value):
    assert dispatch_chat(value.service, 'project_delete', {'id': value.project['id']})['ok']


def assert_detached(value, status):
    result = value.store.entity('previews', value.record['id'])
    assert result['project_id'] is None and result['detached_project_id'] == value.project['id']
    assert result['status'] == status
    assert value.folder.is_dir() and (value.folder / 'index.html').read_text() == 'Synthetic retained preview'
    return result


def test_ready_response_after_project_deletion_cannot_restore_owner_or_lose_cleanup_identity(preview):
    value = preview
    value.respond = lambda: detach(value)
    value.manager._ready(value.record['id'])
    assert_detached(value, 'starting')
    assert value.manager.active[value.record['id']]['record']['project_id'] == value.project['id']
    builder = object.__new__(BuilderManager)
    builder.store, builder.previews = value.store, value.manager
    builder.close_project(value.project['id'])
    assert_detached(value, 'stopped')
    assert value.calls == [value.record['session_id']] and not value.manager.active
    saved = value.store.entity('builders', value.record['builder_id'])
    assert saved['project_id'] is None and saved['project_missing'] and saved['status'] == 'project_missing'


@pytest.mark.parametrize('reader', ['ready-failure', 'status'])
def test_exited_process_status_preserves_detachment_and_uses_owned_session(preview, reader):
    value = preview
    inspected = []
    def exited(key):
        inspected.append(key)
        detach(value)
        return {'status': 'completed', 'pid': 1}
    value.service.command_sessions.status = exited
    if reader == 'ready-failure': value.manager._ready(value.record['id'])
    else: value.manager.status(value.record['id'])
    result = assert_detached(value, 'failed')
    assert result['url'] is None and inspected == [value.record['session_id']]
    assert not value.manager.active
    assert value.calls == ([value.record['session_id']] if reader == 'ready-failure' else [])


def test_orphan_status_read_cannot_restore_project_from_stale_record(preview, monkeypatch):
    value = preview
    value.manager.active.clear()
    entity = value.store.entity
    first = True
    def read(kind, identifier):
        nonlocal first
        result = entity(kind, identifier)
        if kind == 'previews' and first:
            first = False
            detach(value)
        return result
    monkeypatch.setattr(value.store, 'entity', read)
    value.manager.status(value.record['id'])
    assert_detached(value, 'interrupted')


def test_stop_completion_preserves_scope_changed_during_process_wait(preview):
    value = preview
    def stopped(key):
        assert not value.manager.lock._is_owned()
        value.calls.append(key)
        detach(value)
    value.service.command_sessions.stop = stopped
    value.manager.stop(value.record['id'])
    result = assert_detached(value, 'stopped')
    assert result['url'] is None and value.calls == [value.record['session_id']]


@pytest.mark.parametrize('field', ['session_id', 'url', 'project_id', 'builder_id', 'run_id', 'mode', 'cwd'])
def test_ready_callback_cannot_adopt_replaced_operation_identity(preview, field):
    value = preview
    replacement = {**value.record, field: 'synthetic-replacement'}
    value.respond = lambda: value.store.save_entity('previews', replacement)
    value.manager._ready(value.record['id'])
    saved = value.store.entity('previews', value.record['id'])
    assert saved[field] == 'synthetic-replacement' and saved['status'] == 'starting'
    assert not value.calls


def test_stop_and_status_cannot_redirect_to_a_replaced_saved_session(preview):
    value = preview
    replacement = value.store.save_entity('previews', {**value.record,
        'session_id': 'synthetic-foreign-session', 'project_id': 'synthetic-foreign-project'})
    inspected = []
    value.service.command_sessions.status = lambda key: inspected.append(key) or {'status': 'running'}
    value.manager.status(value.record['id'])
    value.manager.stop(value.record['id'])
    assert inspected == value.calls == [value.record['session_id']]
    assert value.store.entity('previews', value.record['id']) == replacement


@pytest.mark.parametrize('when', ['ready-response', 'before-stop', 'during-stop'])
def test_late_status_writes_never_recreate_a_removed_record(preview, when):
    value = preview
    remove = lambda: value.store.delete_entity('previews', value.record['id'])
    if when == 'ready-response':
        value.respond = remove
        value.manager._ready(value.record['id'])
    else:
        if when == 'before-stop': remove()
        else: value.service.command_sessions.stop = lambda key: (value.calls.append(key), remove())
        with pytest.raises(ValueError, match='Preview not found'):
            value.manager.stop(value.record['id'])
        assert value.calls == [value.record['session_id']]
    assert value.store.entities('previews') == []


def test_ready_status_and_revoked_url_cannot_be_overwritten_by_late_response(preview):
    value = preview
    revoked = value.store.save_entity('previews', {**value.record, 'status': 'stopped', 'url': None})
    value.manager._ready(value.record['id'])
    assert value.store.entity('previews', value.record['id']) == revoked


def test_status_patch_reads_and_updates_scope_in_one_sql_transaction(preview, monkeypatch):
    value = preview
    selected, release, deleting, deleted = [threading.Event() for _ in range(4)]
    connection = value.store._connection
    armed = True
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def fetchone(self):
            row = self.cursor.fetchone()
            selected.set()
            assert release.wait(3)
            return row
    class Database:
        def __init__(self, db): self.db = db
        def execute(self, statement, params=()):
            nonlocal armed
            cursor = self.db.execute(statement, params)
            if armed and statement.startswith("SELECT data FROM entities WHERE kind='previews'"):
                armed = False
                return Cursor(cursor)
            return cursor
    @contextmanager
    def wrapped(*args, **kwargs):
        with connection(*args, **kwargs) as db:
            yield Database(db)
    monkeypatch.setattr(value.store, '_connection', wrapped)
    errors = []
    def promote():
        try: value.manager._patch_status(value.record, {'status': 'ready'}, states=('starting',), detached=False)
        except BaseException as exc: errors.append(exc)
    def delete():
        deleting.set()
        try: detach(value)
        except BaseException as exc: errors.append(exc)
        finally: deleted.set()
    promotion = threading.Thread(target=promote)
    deletion = threading.Thread(target=delete)
    promotion.start()
    try:
        assert selected.wait(1)
        deletion.start()
        assert deleting.wait(1) and not deleted.wait(.1)
    finally: release.set()
    promotion.join(3); deletion.join(3)
    assert not promotion.is_alive() and not deletion.is_alive() and not errors
    assert_detached(value, 'ready')
