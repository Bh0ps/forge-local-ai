"""Update integrity, quiescence, rollback and privacy use disposable local state."""
import asyncio
from hashlib import sha256
import json
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import zipfile
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from forge_store import ForgeStore, atomic_text, encode
from forge_updates import (UpdateManager, REPOSITORY, _get_bytes, authenticode, file_hash,
                           helper_main, load_operation, regular_tree, require_trust,
                           safe_relative, version)

PUBLISHER = 'CN=Example verified developer, C=ZZ'
PACKAGE = b'Disposable signed installer fixture 4.2.0'


def signature(path):
    data = Path(path).read_bytes()
    return dict(status='Valid', publisher=PUBLISHER, issuer='Example CA', thumbprint='0'*40,
                version='4.2.0' if b'4.2.0' in data else '4.1.1', timestamped=True)


def metadata(data=PACKAGE):
    name = 'Forge-4.2.0-Setup.exe'
    return {'tag_name': 'v4.2.0', 'draft': False, 'prerelease': False,
        'html_url': 'https://github.com/' + REPOSITORY + '/releases/tag/v4.2.0',
        'assets': [{'name': name, 'browser_download_url': 'https://github.com/' + REPOSITORY + '/releases/download/v4.2.0/' + name,
                    'digest': 'sha256:' + sha256(data).hexdigest(), 'size': len(data)}]}


@pytest.fixture
def setup(tmp_path):
    store = ForgeStore(tmp_path / 'forge-data')
    app = tmp_path / 'Programs/Forge4'; app.mkdir(parents=True)
    for name in ('Forge.exe', 'ForgeBrowserHost.exe'): (app / name).write_bytes(b'Old application 4.1.1')
    (store.home / 'config/preferences.json').write_text('{"model":"preserved"}', encoding='utf-8')
    (store.home / 'attachments/example.txt').write_text('Original attachment', encoding='utf-8')
    goal = store.home / 'state/goals/example'; goal.mkdir(parents=True)
    (goal / 'TODO.md').write_text('- [x] Preserve progress', encoding='utf-8')
    service = SimpleNamespace(store=store, jobs=SimpleNamespace(lock=threading.RLock()))
    def factory():
        def response(request):
            return httpx.Response(200, json=metadata()) if request.url.path.endswith('/latest') else httpx.Response(200, content=PACKAGE)
        return httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False)
    manager = UpdateManager(service, '4.1.1', app, (PUBLISHER,), factory, signature)
    return manager, app


def ready(manager):
    manager.check(); manager.download(); manager.thread.join(5)
    assert manager.status()['state'] == 'ready'


def prepare(manager, monkeypatch):
    ready(manager)
    result = manager.apply()
    # Emulate the parent having exited; never stop another process.
    monkeypatch.setattr('forge_updates.psutil.pid_exists', lambda pid: False)
    return result


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a\\b', 'a:b', 'a//b', './file', 'a/../file', 'a./file', 'nul.txt', 'a/COM1', 'plugins/run.exe', '.forge/state', 'a\x00b'])
def test_package_path_rejects_escape_and_windows_aliases(path):
    with pytest.raises(ValueError): safe_relative(path)


@pytest.mark.parametrize('value', ['4.2.0-rc1', '04.2.0', '4.2', '4.2.0.0', 'v4.2.0/evil', None])
def test_only_stable_versions_can_be_offered(value):
    with pytest.raises(ValueError): version(value)


@pytest.mark.parametrize('change', ['repository', 'digest', 'url', 'duplicate', 'prerelease', 'size'])
def test_check_rejects_wrong_origin_and_missing_integrity(setup, change):
    manager, _ = setup; value = metadata()
    if change == 'repository': value['html_url'] = 'https://github.com/other/project/releases/tag/v4.2.0'
    if change == 'digest': value['assets'][0].pop('digest')
    if change == 'url': value['assets'][0]['browser_download_url'] = 'https://example.invalid/installer.exe'
    if change == 'duplicate': value['assets'] *= 2
    if change == 'prerelease': value['prerelease'] = True
    if change == 'size': value['assets'][0]['size'] = True
    manager.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=value)))
    with pytest.raises(ValueError): manager.check()
    assert manager.status()['state'] == 'error'


def test_unsigned_preview_cannot_bootstrap_trust_from_settings(setup):
    manager, _ = setup; manager.publishers = ()
    manager.store.save_entity('providers', {'publisher': PUBLISHER})
    manager.check()
    assert manager.status()['signing_setup_required']
    with pytest.raises(ValueError, match='disabled'): manager.download()
    assert manager.thread is None


@pytest.mark.parametrize('change', ['status', 'publisher', 'timestamped', 'version'])
def test_matching_hash_never_substitutes_publisher_trust(setup, change):
    manager, _ = setup
    def bad(path):
        value = signature(path); value[change] = {'status':'NotSigned', 'publisher':'CN=Other', 'timestamped':False, 'version':'4.3.0'}[change]
        return value
    manager.verifier = bad; manager.check(); manager.download(); manager.thread.join(5)
    assert manager.status()['state'] == 'error'
    assert not manager.status()['verified']
    assert not list(manager.root.rglob('installer.exe'))


def test_tampered_staging_and_stored_url_do_not_execute(setup):
    manager, app = setup; ready(manager)
    Path(manager.state['downloaded']).write_bytes(b'Tampered')
    with pytest.raises(ValueError, match='modified'): manager.apply()
    assert (app / 'Forge.exe').read_bytes() == b'Old application 4.1.1'
    manager.state['release']['url'] = 'http://127.0.0.1/internal'
    with pytest.raises(ValueError, match='metadata'): manager.download()
    assert not manager.applying


def test_update_gate_blocks_active_run_and_unknown_side_effect(setup):
    manager, _ = setup; ready(manager)
    chat = manager.store.create_chat()
    run = manager.store.create_run({'chat_id': chat['id']})
    with pytest.raises(ValueError, match='active'): manager.apply()
    manager.store.update_run(run['id'], status='paused')
    manager.store.invocation('unknown-fixture', run['id'], 'files.write', {'path':'example.txt'})
    manager.store.invocation_state('unknown-fixture', 'outcome_unknown')
    with pytest.raises(ValueError, match='unknown'): manager.apply()
    assert not manager.applying


def test_preparation_has_consistent_database_and_exact_replay_guard(setup, monkeypatch):
    manager, app = setup
    result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    assert Path(result['command'][0]) == Path(data['backup']) / 'installation/Forge.exe'
    with sqlite3.connect(Path(data['backup']) / 'forge.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    calls = []
    def install(args):
        calls.append(args)
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'): (app / name).write_bytes(b'New application 4.2.0')
        return SimpleNamespace(returncode=0)
    outcome = helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                          publishers=(PUBLISHER,), verifier=signature, runner=install)
    assert outcome['installed'] and len(calls) == 1
    with pytest.raises(ValueError, match='consumed'):
        helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                    publishers=(PUBLISHER,), verifier=signature, runner=install)
    assert len(calls) == 1


def test_installer_failure_restores_code_database_checklist_and_attachment(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def install(args):
        (app / 'Forge.exe').write_bytes(b'Incomplete application')
        return SimpleNamespace(returncode=1)
    outcome = helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                          publishers=(PUBLISHER,), verifier=signature, runner=install)
    assert outcome['rolled_back'] and outcome['version'] == '4.1.1'
    assert (app / 'Forge.exe').read_bytes() == b'Old application 4.1.1'
    assert (manager.home / 'attachments/example.txt').read_text() == 'Original attachment'
    assert '[x]' in (manager.home / 'state/goals/example/TODO.md').read_text()
    assert list(Path(data['backup']).glob('failed-installation-*'))
    assert (Path(data['backup']) / 'post-update-data/attachments/example.txt').read_text() == 'Original attachment'


def test_helper_tamper_and_wrong_target_never_invoke_installer(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id']); called = []
    Path(data['installer']).write_bytes(b'Tampered after approval')
    with pytest.raises(ValueError, match='modified'):
        helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                    publishers=(PUBLISHER,), verifier=signature, runner=lambda args: called.append(args))
    with pytest.raises(ValueError, match='target'):
        helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app.parent / 'Other',
                    publishers=(PUBLISHER,), verifier=signature, runner=lambda args: called.append(args))
    assert called == [] and load_operation(manager.home, result['operation_id'])['state'] == 'prepared'


def test_backup_tamper_blocks_rollback_before_any_mutation(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    (Path(data['backup']) / 'data/attachments/example.txt').write_text('Changed snapshot')
    with pytest.raises(ValueError, match='snapshot'):
        helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                    publishers=(PUBLISHER,), verifier=signature, runner=lambda args: pytest.fail('must not execute'))
    assert (app / 'Forge.exe').read_bytes() == b'Old application 4.1.1'


def test_explicit_rollback_restores_previous_version_once(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def install(args):
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'): (app / name).write_bytes(b'New application 4.2.0')
        return SimpleNamespace(returncode=0)
    helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                publishers=(PUBLISHER,), verifier=signature, runner=install)
    restarted = UpdateManager(manager.service, '4.2.0', app, (PUBLISHER,), verifier=signature)
    assert restarted.status()['state'] == 'installed'
    rollback = restarted.rollback(); rollback_data = load_operation(manager.home, rollback['operation_id'])
    assert rollback_data['parent_created'] > 0
    outcome = helper_main(manager.home, rollback['operation_id'], rollback_data['parent_pid'], install_dir=app,
                          publishers=(PUBLISHER,), verifier=signature, runner=lambda args: pytest.fail('rollback must not execute installer'))
    assert outcome['rolled_back'] and (app / 'Forge.exe').read_bytes() == b'Old application 4.1.1'


def test_diagnostics_is_allowlisted_even_if_settings_contain_private_text(setup, monkeypatch):
    manager, _ = setup
    secret = 'private-account@example.invalid C:/Users/PrivatePerson/private-project secret-token'
    manager.store.create_chat(title=secret)
    manager.store.save_entity('providers', {'url': secret, 'api_key':secret})
    original = manager.store.get_settings()
    monkeypatch.setattr(manager.store, 'get_settings', lambda: {**original, 'context':secret, 'thinking':secret,
                        'permission_profile':secret, 'performance':secret, 'model':secret})
    manager.state.update(error=secret, state=secret, downloaded=secret, signature={'publisher':secret})
    result = manager.diagnostics_export()
    serialized = json.dumps(result['diagnostics'])
    assert 'private-account' not in serialized and 'PrivatePerson' not in serialized and 'secret-token' not in serialized
    assert result['diagnostics']['counts']['chats'] == 1
    assert result['diagnostics']['updates']['state'] == 'unknown'
    assert (manager.home / 'artifacts' / (result['artifact'] + '.json')).is_file()


def test_real_stalling_http_download_cancellation_closes_request_promptly():
    started = threading.Event(); release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Length', '1000000'); self.end_headers()
            started.set(); release.wait(4)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    cancelled = threading.Event(); error = []
    async def request():
        async with httpx.AsyncClient(trust_env=False) as client:
            await _get_bytes(client, 'http://127.0.0.1:' + str(server.server_port), 1000000, cancelled)
    def run():
        try: asyncio.run(request())
        except ValueError as exc: error.append(str(exc))
    thread = threading.Thread(target=run); thread.start()
    try:
        assert started.wait(3)
        before = time.monotonic(); cancelled.set(); thread.join(1.5)
        assert not thread.is_alive() and time.monotonic() - before < 1.5
        assert error == ['Update download cancelled.']
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(3)


def test_untrusted_redirect_and_overlarge_payload_stop_before_consumer():
    chunks = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={'location':'https://example.invalid/evil'}))) as client:
            with pytest.raises(ValueError, match='untrusted'): await _get_bytes(client, 'https://github.com/example', 10, consume=chunks.append)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'12345'))) as client:
            with pytest.raises(ValueError, match='size'): await _get_bytes(client, 'https://github.com/example', 4, consume=chunks.append)
    asyncio.run(run()); assert chunks == []


def test_windows_authenticode_rejects_unsigned_fixture(tmp_path):
    path = tmp_path / 'unsigned.exe'; path.write_bytes(b'Unsigned disposable fixture')
    result = authenticode(path)
    assert result['status'] != 'Valid'
    with pytest.raises(ValueError, match='signature'): require_trust(path, '4.2.0', (PUBLISHER,))


def test_helper_does_not_close_a_live_coordinator(setup, monkeypatch):
    manager, app = setup; ready(manager); result = manager.apply()
    data = load_operation(manager.home, result['operation_id'])
    monkeypatch.setattr('forge_updates.psutil.pid_exists', lambda pid: True)
    monkeypatch.setattr('forge_updates.psutil.Process', lambda pid: SimpleNamespace(create_time=lambda: data['parent_created']))
    with pytest.raises(ValueError, match='did not quit'):
        helper_main(manager.home, result['operation_id'], data['parent_pid'], install_dir=app,
                    publishers=(PUBLISHER,), verifier=signature, runner=lambda args: pytest.fail('must not install'), wait_seconds=0)
    assert load_operation(manager.home, result['operation_id'])['state'] == 'prepared'


@pytest.mark.parametrize('entry', ['../escape.exe', 'C:/escape.exe', 'a\\escape.exe', 'plugins/run.exe', 'CON.txt'])
def test_signed_portable_validation_rejects_malicious_paths(tmp_path, monkeypatch, entry):
    source = Path(__file__).resolve().parents[1] / 'scripts/release_build.py'
    spec = importlib.util.spec_from_file_location('release_fixture', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'require_trust', lambda *args: None)
    (tmp_path / 'Forge-4.2.0-Setup.exe').write_bytes(PACKAGE)
    with zipfile.ZipFile(tmp_path / 'Forge-4.2.0-Portable.zip', 'w') as target: target.writestr(entry, b'payload')
    with pytest.raises(ValueError): module.verify_downloaded(SimpleNamespace(work=tmp_path, version='4.2.0'))
    assert not (tmp_path.parent / 'escape.exe').exists()


def test_publication_and_pr_workflows_keep_credentials_separate():
    import re
    import yaml
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    ci = yaml.load((root / 'windows-ci.yml').read_text(), Loader=yaml.BaseLoader)
    signed = yaml.load((root / 'signed-release.yml').read_text(), Loader=yaml.BaseLoader)
    assert ci['permissions'] == {'contents':'read'}
    assert 'pull_request' in ci['on'] and 'pull_request_target' not in ci['on']
    assert 'secrets.' not in (root / 'windows-ci.yml').read_text()
    assert signed['on'].keys() == {'workflow_dispatch'}
    assert signed['jobs']['verify-and-publish']['environment'] == 'forge-release'
    assert 'id-token' not in signed['jobs']['verify-and-publish']['permissions']
    for workflow in (ci, signed):
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if 'uses' in step: assert re.fullmatch(r'[\w/-]+@[0-9a-f]{40}', step['uses'])


def test_channel_drain_precedes_database_snapshot(setup):
    manager, _ = setup; ready(manager)
    manager.service.admission_lock = threading.RLock()
    manager.service.channel_manager = SimpleNamespace(lock=threading.RLock())
    started, release = threading.Event(), threading.Event()
    def delivery():
        with manager.service.channel_manager.lock:
            started.set(); release.wait(3)
            manager.store.save_entity('fixture', {'id':'channel-result','status':'sent'})
    channel = threading.Thread(target=delivery); channel.start(); assert started.wait(2)
    result=[]; updater=threading.Thread(target=lambda: result.append(manager.apply()));updater.start()
    time.sleep(.1)
    assert not manager.applying and result==[]
    release.set();channel.join(2);updater.join(3)
    assert result and manager.applying
    operation=load_operation(manager.home,result[0]['operation_id'])
    with sqlite3.connect(Path(operation['backup'])/'forge.sqlite3') as db:
        saved=json.loads(db.execute("SELECT data FROM entities WHERE kind='fixture' AND id='channel-result'").fetchone()[0])
    assert saved['status']=='sent'


@pytest.mark.parametrize('worker', ['setup','runtime','memory','model','performance','channel_unknown'])
def test_non_chat_work_and_unknown_channel_delivery_block_update(setup, worker):
    manager, _ = setup; ready(manager)
    if worker=='setup': manager.service.setup_manager=SimpleNamespace(jobs={'fixture':{}})
    if worker=='runtime': manager.service.runtime=SimpleNamespace(_validation={'state':'running'})
    if worker=='memory': manager.service.jobs.jobs={'finished-run':{'thread':SimpleNamespace(is_alive=lambda:True)}}
    if worker=='model': manager.service.model_manager=SimpleNamespace(jobs={'fixture':{}},active={'fixture':{}})
    if worker=='performance': manager.service.performance_manager=SimpleNamespace(jobs={'fixture':{}})
    if worker=='channel_unknown':
        with manager.store._connection(transaction='write') as db:
            db.execute('INSERT INTO channel_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('fixture','channel','recipient','done','{}','outcome_unknown',1,0,None,None,'2026-01-01','2026-01-01'))
    with pytest.raises(ValueError): manager.apply()
    assert not manager.applying


def test_interrupted_installer_blocks_new_operation_and_rollback(setup, monkeypatch):
    manager, app=setup; result=prepare(manager,monkeypatch)
    operation=load_operation(manager.home,result['operation_id'])
    operation['state']='applying'
    atomic_text(manager.root/'operations'/(operation['id']+'.json'),encode(operation))
    restored=UpdateManager(manager.service,'4.1.1',app,(PUBLISHER,),verifier=signature)
    assert restored.status()['inspection_required'] and restored.status()['state']=='interrupted'
    assert not restored.status()['verified']
    for action in (restored.apply, restored.rollback):
        with pytest.raises(ValueError,match='inspection'): action()
    assert len(list((manager.root/'operations').glob('*.json')))==1
    assert (app/'Forge.exe').read_bytes()==b'Old application 4.1.1'


def test_corrupt_update_journal_cannot_enable_another_install(setup, monkeypatch):
    manager, app=setup;result=prepare(manager,monkeypatch)
    path=manager.root/'operations'/(result['operation_id']+'.json')
    path.write_text('{}',encoding='utf-8')
    restored=UpdateManager(manager.service,'4.1.1',app,(PUBLISHER,),verifier=signature)
    assert restored.status()['inspection_required']
    with pytest.raises(ValueError,match='inspection'): restored.apply()


def test_completed_model_history_does_not_block_update_or_rewind_unknown_import(setup, monkeypatch):
    manager, app = setup
    manager.service.model_manager = SimpleNamespace(jobs={
        'completed': {'status': 'completed'}, 'unknown': {'status': 'import_unknown'}}, active={})
    journal = manager.home / 'state/model-jobs.json'
    journal.write_text(encode(manager.service.model_manager.jobs), encoding='utf-8')
    original = journal.read_bytes()
    result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    outcome = helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
        publishers=(PUBLISHER,), verifier=signature, runner=lambda args: SimpleNamespace(returncode=1))
    assert outcome['rolled_back'] and journal.read_bytes() == original


@pytest.mark.parametrize('generation', ['Forge4', 'Forge5'])
def test_production_helper_accepts_only_owned_per_user_generation_paths(setup, monkeypatch, generation):
    manager, app = setup
    monkeypatch.setenv('LOCALAPPDATA', str(app.parent.parent))
    if app.name != generation:
        renamed = app.with_name(generation); app.rename(renamed); app = renamed
        manager.install_dir = app
    result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def install(args):
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'): (app / name).write_bytes(b'New application 4.2.0')
        return SimpleNamespace(returncode=0)
    assert helper_main(manager.home, data['id'], data['parent_pid'], publishers=(PUBLISHER,),
        verifier=signature, runner=install)['installed']


def test_production_helper_rejects_arbitrary_prepared_target(setup, monkeypatch):
    manager, app = setup
    monkeypatch.setenv('LOCALAPPDATA', str(app.parent.parent))
    target = app.with_name('Other'); app.rename(target); manager.install_dir = target
    result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    with pytest.raises(ValueError, match='target'):
        helper_main(manager.home, data['id'], data['parent_pid'], publishers=(PUBLISHER,),
            verifier=signature, runner=lambda args: pytest.fail('must not execute'))
    assert (target / 'Forge.exe').read_bytes() == b'Old application 4.1.1'


def test_helper_rechecks_retained_publisher_before_mutating_installation(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def invalid_previous(path):
        value = signature(path)
        if Path(path).parent.name == 'installation': value['publisher'] = 'CN=Unapproved'
        return value
    with pytest.raises(ValueError, match='signature'):
        helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
            publishers=(PUBLISHER,), verifier=invalid_previous, runner=lambda args: pytest.fail('must not execute'))
    assert load_operation(manager.home, data['id'])['state'] == 'prepared'


def install_ready_fixture(manager, app, monkeypatch):
    result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def install(args):
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'): (app / name).write_bytes(b'New application 4.2.0')
        return SimpleNamespace(returncode=0)
    helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
                publishers=(PUBLISHER,), verifier=signature, runner=install)
    return data, UpdateManager(manager.service, '4.2.0', app, (PUBLISHER,), verifier=signature)


def test_rollback_cannot_rewind_completed_effect_to_prepared_invocation(setup, monkeypatch, tmp_path):
    from project_tools import ProjectTools
    manager, app = setup
    project = tmp_path / 'owned-project'; project.mkdir()
    chat = manager.store.create_chat(); run = manager.store.create_run({'chat_id': chat['id']})
    manager.store.update_run(run['id'], status='paused')
    manager.store.invocation('effect', run['id'], 'write_file', {'path': 'once.txt', 'content': 'Done once'})
    data, restarted = install_ready_fixture(manager, app, monkeypatch)
    result = ProjectTools(project, manager.home / 'backups').execute('write_file', {'path':'once.txt', 'content':'Done once'})
    assert result['ok']
    manager.store.invocation_state('effect', 'completed', result)
    with pytest.raises(ValueError, match='newer user work'): restarted.rollback()
    blocked = load_operation(manager.home, data['id'])
    assert blocked['state'] == 'rollback_blocked' and restarted.status()['inspection_required']
    assert (app / 'Forge.exe').read_bytes() == b'New application 4.2.0'
    assert (project / 'once.txt').read_text() == 'Done once'
    for database in (manager.store.db_path, Path(blocked['current_snapshot']) / 'forge.sqlite3'):
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT status FROM invocations WHERE id='effect'").fetchone()[0] == 'completed'
    with sqlite3.connect(Path(data['backup']) / 'forge.sqlite3') as db:
        assert db.execute("SELECT status FROM invocations WHERE id='effect'").fetchone()[0] == 'prepared'


@pytest.mark.parametrize('folder', ['config', 'attachments', 'state/goals'])
def test_rollback_preserves_newer_file_state_and_consistent_snapshot(setup, monkeypatch, folder):
    manager, app = setup; data, restarted = install_ready_fixture(manager, app, monkeypatch)
    target = manager.home / folder / 'new-user-edit.txt'
    target.write_text('Keep current privacy or user content', encoding='utf-8')
    with pytest.raises(ValueError, match='newer user work'): restarted.rollback()
    blocked = load_operation(manager.home, data['id'])
    snapshot = Path(blocked['current_snapshot'])
    assert target.read_text() == (snapshot / 'data' / folder / target.name).read_text()
    with sqlite3.connect(snapshot / 'forge.sqlite3') as db: assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert (app / 'Forge.exe').read_bytes() == b'New application 4.2.0'
    assert UpdateManager(manager.service, '4.2.0', app, (PUBLISHER,), verifier=signature).status()['inspection_required']


def test_unknown_effect_arriving_after_rollback_admission_blocks_helper_rewind(setup, monkeypatch):
    manager, app = setup
    chat = manager.store.create_chat(); run = manager.store.create_run({'chat_id':chat['id']})
    manager.store.update_run(run['id'], status='paused')
    manager.store.invocation('uncertain', run['id'], 'run_command', {'argv':['python','owned.py']})
    data, restarted = install_ready_fixture(manager, app, monkeypatch)
    accepted = restarted.rollback(); data = load_operation(manager.home, accepted['operation_id'])
    manager.store.invocation_state('uncertain', 'outcome_unknown', {'error':'Stopped after effect began'})
    with pytest.raises(ValueError, match='newer user work'):
        helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
            publishers=(PUBLISHER,), verifier=signature, runner=lambda args: pytest.fail('must not execute'))
    with manager.store._connection() as db:
        assert db.execute("SELECT status FROM invocations WHERE id='uncertain'").fetchone()[0] == 'outcome_unknown'
    assert (app / 'Forge.exe').read_bytes() == b'New application 4.2.0'
    assert load_operation(manager.home, data['id'])['state'] == 'rollback_blocked'


def test_failed_installer_with_new_work_keeps_current_code_data_and_blocks_replay(setup, monkeypatch):
    manager, app = setup; result = prepare(manager, monkeypatch)
    data = load_operation(manager.home, result['operation_id'])
    def install(args):
        (app / 'Forge.exe').write_bytes(b'Partial new application requiring repair')
        (manager.home / 'attachments/example.txt').write_text('New user content', encoding='utf-8')
        manager.store.save_entity('fixture', {'id':'after', 'value':'new work'})
        return SimpleNamespace(returncode=1)
    with pytest.raises(ValueError, match='newer user work'):
        helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
            publishers=(PUBLISHER,), verifier=signature, runner=install)
    assert (app / 'Forge.exe').read_bytes() == b'Partial new application requiring repair'
    assert manager.store.entity('fixture','after')['value'] == 'new work'
    blocked = load_operation(manager.home, data['id'])
    assert (Path(blocked['current_snapshot']) / 'data/attachments/example.txt').read_text() == 'New user content'
    assert not list(Path(data['backup']).glob('failed-installation-*'))


def test_schema_only_additions_do_not_falsely_count_as_new_user_work(setup, monkeypatch):
    manager, app = setup; data, restarted = install_ready_fixture(manager, app, monkeypatch)
    with manager.store._connection(transaction='write') as db:
        db.execute("ALTER TABLE usage ADD COLUMN future_timing TEXT NOT NULL DEFAULT '{}'")
        db.execute('CREATE TABLE future_empty_records(id TEXT PRIMARY KEY,data TEXT)')
        db.execute("INSERT INTO forge_migrations VALUES(7,'2026-10-08')")
    result = restarted.rollback(); data = load_operation(manager.home, result['operation_id'])
    outcome = helper_main(manager.home, data['id'], data['parent_pid'], install_dir=app,
        publishers=(PUBLISHER,), verifier=signature, runner=lambda args: pytest.fail('no installer on rollback'))
    assert outcome['rolled_back'] and (app / 'Forge.exe').read_bytes() == b'Old application 4.1.1'
    with sqlite3.connect(manager.store.db_path) as db:
        assert db.execute('SELECT MAX(version) FROM forge_migrations').fetchone()[0] == 6
