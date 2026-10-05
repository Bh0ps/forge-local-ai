"""Project boundary, conflict detection, undo integrity and bounded process tests."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from project_tools import MAX_COMMAND_OUTPUT, MAX_FILE_BYTES, MAX_LIST_ENTRIES, ProjectTools


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return ProjectTools(root, tmp_path / 'backups')


def call(project, name, **args):
    result = project.execute(name, args)
    assert result['ok'], result
    return result


def test_schemas_have_unique_names_and_no_freeform_shell():
    schemas = ProjectTools.schemas()
    names = [item['function']['name'] for item in schemas]
    assert len(set(names)) == len(names) == 9
    command = next(item['function'] for item in schemas if item['function']['name'] == 'run_command')
    assert command['parameters']['required'] == ['argv']
    assert command['parameters']['properties']['argv']['type'] == 'array'


@pytest.mark.parametrize('path', ['../outside.txt', 'sub/../../outside.txt', 'file.txt:secret',
                                  r'\\server\share\file', r'\\?\C:\Windows\file',
                                  r'\\.\PhysicalDrive0', 'C:relative.txt', 'CON.txt',
                                  'x/NUL', 'trailing.', 'trailing ', '.git/config', '.GIT/HEAD'])
def test_invalid_paths_cannot_be_read_or_written(project, path):
    assert not project.execute('read_file', {'path': path})['ok']
    assert not project.execute('write_file', {'path': path, 'content': 'bad'})['ok']


def test_absolute_project_path_is_allowed_but_sibling_is_denied(project):
    target = project.root / 'hello.txt'
    target.write_text('inside', encoding='utf-8')
    assert call(project, 'read_file', path=str(target))['content'] == 'inside'
    sibling = project.root.parent / 'outside.txt'
    sibling.write_text('outside', encoding='utf-8')
    assert not project.execute('read_file', {'path': str(sibling)})['ok']
    assert not project.execute('write_file', {'path': str(sibling), 'content': 'bad'})['ok']
    assert sibling.read_text() == 'outside'


def test_symlink_or_junction_escape_is_rejected(project):
    outside = project.root.parent / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('private', encoding='utf-8')
    link = project.root / 'escape'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation requires privileges on this Windows installation')
    assert not project.execute('read_file', {'path': 'escape/private.txt'})['ok']
    assert not project.execute('write_file', {'path': 'escape/private.txt', 'content': 'bad'})['ok']
    assert not project.execute('make_directory', {'path': 'escape/new'})['ok']
    assert (outside / 'private.txt').read_text() == 'private'


def test_hardlinked_files_are_not_read_or_overwritten(project):
    original = project.root.parent / 'outside.txt'
    original.write_text('outside', encoding='utf-8')
    try:
        os.link(original, project.root / 'inside.txt')
    except OSError:
        pytest.skip('Hard links unavailable')
    assert not project.execute('read_file', {'path': 'inside.txt'})['ok']
    assert not project.execute('write_file', {'path': 'inside.txt', 'content': 'bad'})['ok']
    assert original.read_text() == 'outside'


def test_windows_reparse_point_flag_is_denied_without_symlink_privileges(project, monkeypatch):
    folder = project.root / 'junction'
    folder.mkdir()
    (folder / 'text.txt').write_text('contents', encoding='utf-8')
    original_lstat = Path.lstat

    def reparse_lstat(path, *args, **kwargs):
        result = original_lstat(path, *args, **kwargs)
        if path == folder:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, 'lstat', reparse_lstat)
    result = project.execute('read_file', {'path': 'junction/text.txt'})
    assert not result['ok']
    assert 'junctions' in result['error']


def test_read_file_returns_exact_lines_and_hash(project):
    contents = b'first\r\nsecond\r\nthird\r\n'
    (project.root / 'text.py').write_bytes(contents)
    result = call(project, 'read_file', path='text.py', start_line=2, end_line=2)
    assert result['content'] == 'second\r\n'
    assert result['total_lines'] == 3
    assert result['sha256'] == hashlib.sha256(contents).hexdigest()
    assert not project.execute('read_file', {'path': 'text.py', 'start_line': 0})['ok']
    assert not project.execute('read_file', {'path': 'text.py', 'end_line': 0})['ok']


def test_binary_and_oversize_files_are_rejected(project):
    for filename, contents in [('binary', b'hello\x00world'), ('utf16', 'hello'.encode('utf-16')),
                               ('large', b'x' * (MAX_FILE_BYTES + 1))]:
        (project.root / filename).write_bytes(contents)
        assert not project.execute('read_file', {'path': filename})['ok']
        assert not project.execute('write_file', {'path': filename, 'content': 'replacement'})['ok']
        assert (project.root / filename).read_bytes() == contents
    assert not project.execute('write_file', {'path': 'new', 'content': 'x' * (MAX_FILE_BYTES + 1)})['ok']


def test_list_and_search_skip_dependencies_and_git(project):
    for folder in ['src', '.git', 'node_modules']:
        (project.root / folder).mkdir()
        (project.root / folder / 'code.py').write_text('needle\nmore needle\n', encoding='utf-8')
    listing = call(project, 'list_files', recursive=True)
    assert {item['path'] for item in listing['entries']} == {'src', 'src/code.py'}
    matches = call(project, 'search_files', query='needle')['matches']
    assert [(item['path'], item['line']) for item in matches] == [('src/code.py', 1), ('src/code.py', 2)]
    assert call(project, 'search_files', query='needle', path='src/code.py')['files_searched'] == 1


def test_listing_and_search_results_are_bounded(project):
    for index in range(MAX_LIST_ENTRIES + 10):
        (project.root / f'{index}.txt').write_text('needle\n', encoding='utf-8')
    listing = call(project, 'list_files')
    assert len(listing['entries']) == MAX_LIST_ENTRIES
    assert listing['truncated']
    search = call(project, 'search_files', query='needle')
    assert len(search['matches']) == 100
    assert search['truncated']


def test_write_edit_and_restore_have_verified_backups(project):
    created = call(project, 'write_file', path='main.py', content='old\nvalue\n', expected_sha256='missing')
    first_hash = created['sha256']
    edited = call(project, 'edit_file', path='main.py', old_text='old', new_text='new', expected_sha256=first_hash)
    assert '-old' in edited['diff'] and '+new' in edited['diff']
    assert not Path(edited['backup_path']).is_relative_to(project.root)
    assert Path(edited['backup_path']).read_bytes() == b'old\nvalue\n'
    restored = call(project, 'restore_file', path='main.py', backup_id=edited['backup_id'])
    assert restored['sha256'] == first_hash
    assert (project.root / 'main.py').read_bytes() == b'old\nvalue\n'
    assert restored['backup_id'] != edited['backup_id']
    assert not list(project.root.glob('.sidekick-*'))
    call(project, 'restore_file', path='main.py', backup_id=created['backup_id'])
    assert not (project.root / 'main.py').exists()


def test_edit_fails_for_missing_or_non_unique_match(project):
    call(project, 'write_file', path='main.py', content='same same')
    for old in ['same', '', 'absent']:
        assert not project.execute('edit_file', {'path': 'main.py', 'old_text': old, 'new_text': 'bad'})['ok']
    assert (project.root / 'main.py').read_text() == 'same same'


def test_stale_hash_and_concurrent_edit_do_not_overwrite(project, monkeypatch):
    original = call(project, 'write_file', path='main.py', content='original')
    (project.root / 'main.py').write_text('user change', encoding='utf-8')
    rejected = project.execute('write_file', {'path': 'main.py', 'content': 'bad', 'expected_sha256': original['sha256']})
    assert not rejected['ok']
    backup = project._backup

    def concurrent_change(*args):
        result = backup(*args)
        (project.root / 'main.py').write_text('concurrent user change', encoding='utf-8')
        return result

    monkeypatch.setattr(project, '_backup', concurrent_change)
    assert not project.execute('write_file', {'path': 'main.py', 'content': 'bad'})['ok']
    assert (project.root / 'main.py').read_text() == 'concurrent user change'
    assert not list(project.root.glob('.sidekick-*'))


def test_restore_refuses_other_files_projects_invalid_ids_and_modified_payload(project):
    call(project, 'write_file', path='a.txt', content='old')
    update = call(project, 'write_file', path='a.txt', content='new')
    assert not project.execute('restore_file', {'path': 'b.txt', 'backup_id': update['backup_id']})['ok']
    assert not project.execute('restore_file', {'path': 'a.txt', 'backup_id': '../outside'})['ok']
    other_root = project.root.parent / 'other-project'
    other_root.mkdir()
    other = ProjectTools(other_root, project.data_dir)
    assert not other.execute('restore_file', {'path': 'a.txt', 'backup_id': update['backup_id']})['ok']
    Path(update['backup_path']).write_text('tampered', encoding='utf-8')
    assert not project.execute('restore_file', {'path': 'a.txt', 'backup_id': update['backup_id']})['ok']
    assert (project.root / 'a.txt').read_text() == 'new'


def test_move_never_overwrites_existing_destination(project):
    call(project, 'write_file', path='a.txt', content='a')
    call(project, 'write_file', path='b.txt', content='b')
    assert not project.execute('move_file', {'source': 'a.txt', 'destination': 'b.txt'})['ok']
    call(project, 'make_directory', path='folder/nested')
    result = call(project, 'move_file', source='a.txt', destination='folder/nested/a.txt')
    assert not (project.root / 'a.txt').exists()
    assert (project.root / 'folder/nested/a.txt').read_text() == 'a'
    call(project, 'restore_file', path='a.txt', backup_id=result['backup_id'])
    assert (project.root / 'a.txt').read_text() == 'a'
    assert (project.root / 'b.txt').read_text() == 'b'


def test_backup_root_inside_project_is_refused(project):
    with pytest.raises(ValueError, match='outside'):
        ProjectTools(project.root, project.root / '.backups')


def test_command_has_literal_arguments_project_cwd_and_exit_status(project):
    code = 'import os,sys; print(os.getcwd()); print(sys.argv[1]); sys.exit(7)'
    result = call(project, 'run_command', argv=[sys.executable, '-c', code, 'hello & echo bad'], timeout=5)
    assert result['exit_code'] == 7
    assert str(project.root) in result['output']
    assert 'hello & echo bad' in result['output']
    assert not result['cancelled'] and not result['timed_out']


def test_command_output_and_runtime_are_bounded(project):
    output = call(project, 'run_command', argv=[sys.executable, '-c', 'print("x" * 200000)'], timeout=5)
    assert len(output['output']) == MAX_COMMAND_OUTPUT
    assert output['output_truncated']
    start = time.monotonic()
    timed = call(project, 'run_command', argv=[sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.2)
    assert timed['timed_out']
    assert time.monotonic() - start < 6


def test_running_command_cancellation_stops_process(project):
    cancellation = threading.Event()
    timer = threading.Timer(0.2, cancellation.set)
    timer.start()
    try:
        result = project.execute('run_command', {'argv': [sys.executable, '-c', 'import time; time.sleep(10)'], 'timeout': 5}, cancellation)
    finally:
        timer.cancel()
    assert result['cancelled']
    assert result['exit_code'] is not None


@pytest.mark.parametrize('parent_sleeps', [False, True])
def test_command_completion_and_timeout_do_not_leave_child_processes(project, parent_sleeps):
    # Only harmless Python processes are launched; their sole side effect would
    # be writing this marker inside pytest's temporary project.
    child = "import pathlib,time; time.sleep(0.8); pathlib.Path('child-survived').write_text('bad')"
    parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); print("spawned", flush=True)'
    if parent_sleeps:
        parent += '; time.sleep(10)'
    result = call(project, 'run_command', argv=[sys.executable, '-c', parent], timeout=0.3)
    assert 'spawned' in result['output']
    assert result['timed_out'] is parent_sleeps
    time.sleep(1)
    assert not (project.root / 'child-survived').exists()


def test_invalid_arguments_and_pre_cancelled_requests_are_json_errors(project):
    for name, args in [('unknown', {}), ('read_file', {'path': 'a', 'arbitrary': 1}),
                       ('run_command', {'argv': 'echo hi'}),
                       ('run_command', {'argv': [sys.executable], 'timeout': 90}),
                       ('run_command', {'argv': [sys.executable], 'cwd': '..'})]:
        result = project.execute(name, args)
        assert not result['ok']
        json.dumps(result)
    event = threading.Event()
    event.set()
    assert project.execute('write_file', {'path': 'cancelled', 'content': 'x'}, event)['cancelled']
    assert not (project.root / 'cancelled').exists()
