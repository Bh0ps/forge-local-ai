"""Retired computer activity uses disposable journals and no host/provider calls."""
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from forge_computer_activity import MAX_STEPS, SUMMARY_FIELDS, LATEST_FIELDS
import forge_computer_view as view
from forge_helper_cleanup import cleanup_helpers
from forge_runs import RunManager
from forge_store import ForgeStore
from test_forge_computer_view import record
from test_forge_helper_cleanup import assignment, fixture


def computer_steps(store, child):
    value = SimpleNamespace(store=store, parent=child['id'])
    inspected, image = record(value, suffix='inspect')
    latest, entered = record(value, name='computer_set_value', result={'ok': True,
        'private_value': 'synthetic-private-entered'}, suffix='value')
    return inspected, latest, image, entered


def retire(store):
    return RunManager._cleanup_helpers(SimpleNamespace(store=store, lock=threading.RLock(), jobs={}))


def summary(store, child):
    return store.run(child['id'])['helper_cleanup']['computer_activity']


def save_summary(store, child, change):
    run = store.run(child['id'])
    retired = deepcopy(run['helper_cleanup'])
    change(retired)
    store.update_run(child['id'], helper_cleanup=retired)


@pytest.mark.parametrize('parent_action', [False, True])
@pytest.mark.parametrize('selected', [False, True])
def test_actual_cleanup_retains_helper_only_and_mixed_counts_after_reopen(
        tmp_path, monkeypatch, parent_action, selected):
    store, parent, child = fixture(tmp_path)
    _, latest, image, entered = computer_steps(store, child)
    if parent_action:
        record(SimpleNamespace(store=store, parent=parent['id']), name='computer_windows',
            result={'windows': []}, suffix='parent')
    foreign = assignment(store)
    record(SimpleNamespace(store=store, parent=foreign['id']), suffix='foreign')
    store.add_message(parent['chat_id'], 'assistant', 'Synthetic evidence reference ' + image)
    paths = [store.home / 'artifacts' / (identity + '.json') for identity in (image, entered)]
    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    data = {'chat_id': parent['chat_id'], **({'run_id': parent['id']} if selected else {})}
    assert view.computer_view_state(store, data)['action_count'] == 2 + parent_action
    monkeypatch.setattr(store, 'artifact', lambda *args: pytest.fail('Cleanup must not create or replay evidence'))
    assert retire(store) == [child['id']]
    retained = summary(store, child)
    assert set(retained) == SUMMARY_FIELDS and set(retained['latest']) == LATEST_FIELDS
    assert retained['action_count'] == 2 and retained['latest']['invocation_id'] == latest
    assert retained['parent_run_id'] == parent['id'] and retained['parent_chat_id'] == parent['chat_id']
    assert 'synthetic-private' not in json.dumps(retained)
    # No retired inspect artifact can supply pixels, even though files remain.
    monkeypatch.setattr(view, '_record', lambda *args: pytest.fail('Retired artifact must not be read'))
    for opened in (store, ForgeStore(tmp_path)):
        projected = view.computer_view_state(opened, data)
        assert projected['action_count'] == 2 + parent_action and not projected['action_count_truncated']
        assert projected['retired_action_count'] == 2 and not projected['retired_action_count_truncated']
        assert projected['run_id'] == parent['id'] and projected['status'] == 'completed'
        assert projected['snapshot'] is None and projected['evidence_mode'] == 'none'
        assert 'synthetic-private' not in json.dumps(projected)
        assert retire(opened) == []
        with opened._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?', (child['id'],)).fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM invocations WHERE run_id=?', (foreign['id'],)).fetchone()[0] == 1
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == digests
    assert summary(store, child) == retained


def test_current_parent_inspection_stays_readable_but_retired_child_supplies_no_pixels(tmp_path):
    store, parent, child = fixture(tmp_path)
    parent_inspect, _ = record(SimpleNamespace(store=store, parent=parent['id']), suffix='surviving')
    _, latest, _, _ = computer_steps(store, child)
    assert retire(store) == [child['id']]
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['tool'] == 'computer_set_value' and projected['invocation_id'] == latest
    assert projected['action_count'] == 3 and projected['snapshot']['invocation_id'] == parent_inspect
    child_only = view.computer_view_state(store, {'chat_id': parent['chat_id'], 'run_id': child['id']})
    assert child_only['action_count'] == 2 and child_only['snapshot'] is None
    next_run = store.create_run({'chat_id': parent['chat_id']})
    current = view.computer_view_state(store, {'chat_id': parent['chat_id'], 'run_id': next_run['id']})
    assert current['action_count'] == 0 and current['retired_action_count'] == 0
    assert current['snapshot'] is None and current['status'] == 'queued'


@pytest.mark.parametrize('retired_is_latest', [False, True])
def test_latest_candidate_uses_recorded_completion_time_and_current_parent_status(tmp_path, retired_is_latest):
    store, parent, child = fixture(tmp_path)
    _, child_last, _, _ = computer_steps(store, child)
    parent_last, _ = record(SimpleNamespace(store=store, parent=parent['id']),
        name='computer_windows', result={'windows': []}, suffix='parent')
    with store._connection(transaction='write') as db:
        db.execute('UPDATE invocations SET updated_at=? WHERE run_id=?', ('2025-01-01T00:00:00+00:00', child['id']))
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?',
            ('2025-01-03T00:00:00+00:00' if retired_is_latest else '2025-01-02T00:00:00+00:00', child_last))
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('2025-01-02T12:00:00+00:00', parent_last))
    assert retire(store) == [child['id']]
    store.update_run(parent['id'], status='running')
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['status'] == 'running' and projected['run_id'] == parent['id']
    assert projected['invocation_id'] == (child_last if retired_is_latest else parent_last)


@pytest.mark.parametrize('name,status,expected', [('computer_key', 'completed', 1),
    ('computer_key', 'failed', 0), ('computer_key', 'cancelled', 0), ('read_file', 'completed', 0)])
def test_summary_counts_only_acknowledged_allowlisted_steps(tmp_path, name, status, expected):
    store, parent, child = fixture(tmp_path)
    record(SimpleNamespace(store=store, parent=child['id']), name=name, status=status,
        result={'private_value': 'synthetic-private-payload'}, suffix='classified')
    assert retire(store) == [child['id']]
    assert summary(store, child)['action_count'] == expected
    assert view.computer_view_state(store, {'chat_id': parent['chat_id']})['action_count'] == expected


@pytest.mark.parametrize('current_name,current_status,expected', [
    ('computer_set_value', 'completed', 1), ('computer_set_value', 'prepared', 0), ('read_file', 'completed', 0)])
def test_surviving_journal_excludes_the_same_helpers_retained_count(tmp_path, current_name, current_status, expected):
    store, parent, child = fixture(tmp_path)
    _, latest, _, _ = computer_steps(store, child)
    assert retire(store) == [child['id']]
    store.invocation(latest, child['id'], current_name, {})
    store.invocation_state(latest, current_status, {'ok': True})
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == expected and projected['retired_action_count'] == 0


def test_legacy_retired_helper_remains_unknown_without_inference_from_report_or_generic_count(tmp_path):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    assert retire(store) == [child['id']]
    save_summary(store, child, lambda retired: retired.pop('computer_activity'))
    save_summary(store, child, lambda retired: retired.update(tool_count=999,
        summary='Synthetic report mentions computer_click and 999 desktop actions.'))
    projected = view.computer_view_state(ForgeStore(tmp_path), {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 0 and projected['retired_action_count'] == 0
    assert projected['snapshot'] is None and projected['tool'] is None


BAD_SUMMARIES = [
    ('unknown-version', lambda x: x.update(version=2)),
    ('boolean-version', lambda x: x.update(version=True)),
    ('boolean-count', lambda x: x.update(action_count=True)),
    ('negative-count', lambda x: x.update(action_count=-1)),
    ('oversize-count', lambda x: x.update(action_count=MAX_STEPS + 1)),
    ('integer-truncation', lambda x: x.update(action_count_truncated=1)),
    ('dishonest-truncation', lambda x: x.update(action_count_truncated=True)),
    ('foreign-run', lambda x: x.update(run_id='foreign-synthetic-run')),
    ('foreign-parent', lambda x: x.update(parent_run_id='foreign-synthetic-parent')),
    ('foreign-chat', lambda x: x.update(chat_id='foreign-synthetic-chat')),
    ('foreign-parent-chat', lambda x: x.update(parent_chat_id='foreign-synthetic-parent-chat')),
    ('invalid-status', lambda x: x.update(status='running')),
    ('nontext-status', lambda x: x.update(status={})),
    ('private-field', lambda x: x.update(arguments={'text': 'synthetic-private-entered'})),
    ('snapshot-field', lambda x: x.update(snapshot='synthetic-private-pixels')),
    ('missing-latest', lambda x: x.update(latest=None)),
    ('invalid-invocation', lambda x: x['latest'].update(invocation_id='synthetic-private/path')),
    ('invalid-tool', lambda x: x['latest'].update(tool='web_fetch')),
    ('nontext-tool', lambda x: x['latest'].update(tool={})),
    ('invalid-time', lambda x: x['latest'].update(completed_at='synthetic-private-time')),
    ('naive-time', lambda x: x['latest'].update(completed_at='2025-01-01T00:00:00')),
    ('latest-private-field', lambda x: x['latest'].update(value='synthetic-private-entered')),
]


@pytest.mark.parametrize('change', [change for _, change in BAD_SUMMARIES], ids=[name for name, _ in BAD_SUMMARIES])
def test_invalid_or_foreign_summary_is_never_projected(tmp_path, monkeypatch, change):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    assert retire(store) == [child['id']]
    save_summary(store, child, lambda retired: change(retired['computer_activity']))
    monkeypatch.setattr(view, '_record', lambda *args: pytest.fail('No retired artifact read'))
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 0 and projected['retired_action_count'] == 0
    assert projected['invocation_id'] is None and projected['snapshot'] is None
    assert 'synthetic-private' not in json.dumps(projected)


@pytest.mark.parametrize('change', ['oversize-json', 'nonobject', 'encoded-object', 'unknown-cleanup', 'boolean-cleanup', 'unretired'])
def test_unrecognized_retirement_contract_is_not_used(tmp_path, change):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    assert retire(store) == [child['id']]
    if change == 'unretired': store.update_run(child['id'], phase='working')
    else:
        def alter(retired):
            if change == 'oversize-json': retired['computer_activity'] = 'x' * 2049
            elif change == 'nonobject': retired['computer_activity'] = ['synthetic-private-payload']
            elif change == 'encoded-object': retired['computer_activity'] = json.dumps(retired['computer_activity'])
            elif change == 'unknown-cleanup': retired['version'] = 2
            else: retired['version'] = True
        save_summary(store, child, alter)
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 0 and projected['retired_action_count'] == 0


def test_bounded_summary_and_merged_total_use_honest_lower_bound(tmp_path):
    store, parent, child = fixture(tmp_path)
    stamp = '2025-01-01T00:00:00+00:00'
    with store._connection(transaction='write') as db:
        db.executemany('INSERT INTO invocations(id,run_id,name,arguments,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            ((child['id'] + ':' + str(index), child['id'], 'computer_key', '{}', 'completed', '{}', stamp, stamp)
             for index in range(MAX_STEPS + 1)))
    record(SimpleNamespace(store=store, parent=parent['id']), name='computer_windows', result={'windows': []})
    assert retire(store) == [child['id']]
    retained = summary(store, child)
    assert retained['action_count'] == MAX_STEPS and retained['action_count_truncated'] is True
    assert len(json.dumps(retained)) < 2048
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == MAX_STEPS and projected['action_count_truncated'] is True
    assert projected['retired_action_count'] == MAX_STEPS and projected['retired_action_count_truncated'] is True


def test_summary_and_journal_retirement_roll_back_together(tmp_path):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    before = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    with store._connection(transaction='write') as db:
        db.execute("CREATE TRIGGER refuse_retire BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'synthetic refusal'); END")
    with pytest.raises(ValueError, match='synthetic refusal'): retire(store)
    assert 'helper_cleanup' not in store.run(child['id'])
    assert store.get_chat(child['chat_id'])['messages']
    after = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert after['action_count'] == before['action_count'] == 2 and after['snapshot'] is not None
    assert after['retired_action_count'] == 0


def test_unrepresentable_coordinator_metadata_keeps_the_full_helper_journal(tmp_path):
    store, _, child = fixture(tmp_path)
    inspected, _, _, _ = computer_steps(store, child)
    with store._connection(transaction='write') as db:
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('invalid-recorded-time', inspected))
    assert retire(store) == []
    assert not store.run(child['id']).get('helper_cleanup')
    assert store.get_chat(child['chat_id'])['messages']


def test_multiple_owned_helpers_count_once_but_foreign_retired_work_is_excluded(tmp_path):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    second = assignment(store, parent)
    computer_steps(store, second)
    store.save_assistant_turn(second['id'], 'Useful second synthetic report')
    store.update_run(second['id'], status='completed', result_consumed=True)
    foreign_parent = assignment(store)
    foreign_child = assignment(store, foreign_parent)
    computer_steps(store, foreign_child)
    store.save_assistant_turn(foreign_child['id'], 'Useful foreign synthetic report')
    store.update_run(foreign_parent['id'], status='completed')
    store.update_run(foreign_child['id'], status='completed', result_consumed=True)
    assert set(retire(store)) == {child['id'], second['id'], foreign_child['id']}
    opened = ForgeStore(tmp_path)
    projected = view.computer_view_state(opened, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == projected['retired_action_count'] == 4
    assert projected['invocation_id'].startswith(second['id'] + ':')
    assert retire(opened) == []
    with pytest.raises(ValueError, match='does not belong'):
        view.computer_view_state(opened, {'chat_id': parent['chat_id'], 'run_id': foreign_child['id']})


@pytest.mark.parametrize('change', ['parent', 'parent_chat', 'status'])
def test_summary_must_match_current_sql_provenance_and_status(tmp_path, change):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    assert retire(store) == [child['id']]
    with store._connection(transaction='write') as db:
        if change == 'parent': db.execute('UPDATE runs SET parent_id=NULL WHERE id=?', (child['id'],))
        elif change == 'parent_chat': db.execute('UPDATE runs SET chat_id=? WHERE id=?', ('changed-synthetic-chat', parent['id']))
        else: db.execute('UPDATE runs SET status=? WHERE id=?', ('cancelled', child['id']))
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 0 and projected['retired_action_count'] == 0


def test_latest_time_compares_offsets_instead_of_lexical_clock_strings(tmp_path):
    store, parent, child = fixture(tmp_path)
    _, child_last, _, _ = computer_steps(store, child)
    parent_last, _ = record(SimpleNamespace(store=store, parent=parent['id']),
        name='computer_windows', result={'windows': []})
    with store._connection(transaction='write') as db:
        db.execute('UPDATE invocations SET updated_at=? WHERE run_id=?', ('2025-01-01T00:00:00+00:00', child['id']))
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('2025-01-01T15:00:00+05:00', child_last))
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('2025-01-01T11:00:00Z', parent_last))
    assert retire(store) == [child['id']]
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['invocation_id'] == parent_last and projected['action_count'] == 3


def test_captured_latest_tool_compares_timezone_offsets_within_one_helper(tmp_path):
    store, parent, child = fixture(tmp_path)
    inspected, child_last, _, _ = computer_steps(store, child)
    with store._connection(transaction='write') as db:
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('2025-01-01T15:00:00+05:00', inspected))
        db.execute('UPDATE invocations SET updated_at=? WHERE id=?', ('2025-01-01T11:00:00Z', child_last))
    assert retire(store) == [child['id']]
    assert summary(store, child)['latest']['invocation_id'] == child_last
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['invocation_id'] == child_last and projected['tool'] == 'computer_set_value'


def test_malformed_sql_json_drops_only_unverifiable_summary_without_reading_artifacts(tmp_path, monkeypatch):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    parent_last, _ = record(SimpleNamespace(store=store, parent=parent['id']),
        name='computer_windows', result={'windows': []})
    assert retire(store) == [child['id']]
    with store._connection(transaction='write') as db:
        db.execute('UPDATE runs SET data=? WHERE id=?', ('{synthetic malformed JSON', child['id']))
    monkeypatch.setattr(view, '_record', lambda *args: pytest.fail('No retired pixel authority'))
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 1 and projected['retired_action_count'] == 0
    assert projected['invocation_id'] == parent_last and projected['snapshot'] is None


def test_failure_on_second_helper_rolls_back_first_summary_and_all_deleted_rows(tmp_path):
    store, parent, child = fixture(tmp_path)
    computer_steps(store, child)
    second = assignment(store, parent)
    computer_steps(store, second)
    store.save_assistant_turn(second['id'], 'Useful synthetic report')
    store.update_run(second['id'], status='completed', result_consumed=True)
    with store._connection(transaction='write') as db:
        db.execute("CREATE TRIGGER refuse_second BEFORE UPDATE ON runs WHEN OLD.id='" + second['id'] +
            "' BEGIN SELECT RAISE(ABORT,'synthetic second refusal'); END")
    with pytest.raises(ValueError, match='synthetic second refusal'): retire(store)
    for helper in (child, second):
        assert 'helper_cleanup' not in store.run(helper['id'])
        assert store.get_chat(helper['chat_id'])['messages']
    projected = view.computer_view_state(store, {'chat_id': parent['chat_id']})
    assert projected['action_count'] == 4 and projected['retired_action_count'] == 0
