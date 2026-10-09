"""Checker registration exercises real routing/cadence with no model requests."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from forge_evals import (ALLOWED_TOOLS, evaluation_checker_schema,
                         evaluation_contract_factory, evaluation_schema_adapter,
                         install_evaluation_schemas, load_suite, materialize,
                         prerouting_evaluation_supported, task_admission)
from test_forge_core import Engine, service
from test_forge501_cadence import execute
from tool_routing import wire_schemas


def fixture(tmp_path):
    task = next(item for item in load_suite()['tasks'] if item['id'] == 'todo-flow')
    svc = service(tmp_path)
    svc.jobs._launch = lambda run: None
    svc.store.update_settings({'permission_profile': 'full_access', 'web': False,
        'memory_enabled': False, 'memory_suggestions': False, 'auto_delegate': False,
        'goal_cloud_guidance': False, 'goal_review_enabled': False, 'guided_execution': True})
    root = tmp_path / 'project'
    materialize(task, root)
    project = svc.create_project({'name': 'Routing fixture', 'path': str(root)})
    goal = svc.goal_create(task_admission(task, project['id'], tracked_tasks=True))
    accepted = svc.jobs.start({'text': task['prompt'], 'project_id': project['id'],
        'goal_id': goal['id'], 'mode': 'goal', 'agent_tools': sorted(ALLOWED_TOOLS)})
    run = svc.store.update_run(accepted['id'], status='running')
    return svc, task, root, run


def test_checker_is_registered_before_real_routing_and_contract_metadata_stays_host_only(tmp_path):
    svc, task, _, run = fixture(tmp_path)
    try:
        factory = evaluation_contract_factory(task, svc.store)
        original = svc.jobs.registry.schemas
        fallback = evaluation_schema_adapter(original, factory)(run, ['tools'])[0]
        assert prerouting_evaluation_supported(svc, original)
        assert install_evaluation_schemas(svc, factory) == {'mode': 'prerouting', 'registered_contracts': True}
        schemas = svc.jobs.registry.schemas(run, ['tools'], all_tools=True)
        checker = next(item for item in schemas if item['function']['name'] == 'evaluation_check')
        assert checker == fallback
        assert checker['verification_contract']['task_ids'] == [item['id'] for item in svc.store.goal(run['goal_id'])['tasks']]
        # all_tools is discovery; normal routing must see the contract before its refresh.
        selected = svc.jobs.registry.schemas(run, ['tools'])
        state = svc.store.run(run['id'])['verification_cadence']
        assert state['check_tool'] == 'evaluation_check' and state['contract_id'] == checker['verification_contract']['id']
        assert {item['function']['name'] for item in schemas + selected} <= ALLOWED_TOOLS
        public = wire_schemas([checker])
        assert public == wire_schemas([evaluation_checker_schema(run)])
        serialized = json.dumps(public)
        for private in ('verification_contract', 'expected_checks', 'required_sources', 'task_ids', 'complete_field'):
            assert private not in serialized
        for reference in task['reference_files'].values():
            assert reference not in serialized
        assert not svc.core.requests
    finally:
        svc.shutdown()


def test_three_real_mutations_pin_registered_checker_and_block_fourth_without_inference(tmp_path):
    svc, task, root, run = fixture(tmp_path)
    try:
        install_evaluation_schemas(svc, evaluation_contract_factory(task, svc.store))
        for index in range(3):
            run, outcome = execute(svc, run, 'write_file', {'path': 'fixture-' + str(index) + '.txt', 'content': 'committed fixture edit'})
            assert outcome['result']['ok']
        selected = svc.jobs.registry.schemas(run, ['tools'])
        state = svc.store.run(run['id'])['verification_cadence']
        assert state['check_tool'] == 'evaluation_check' and state['required']
        assert 'evaluation_check' in {item['function']['name'] for item in selected}
        run, outcome = execute(svc, run, 'write_file', {'path': 'fourth.txt', 'content': 'must not execute'})
        assert outcome['result']['not_executed'] and not (root / 'fourth.txt').exists()
        assert not svc.core.requests
    finally:
        svc.shutdown()


def test_reinstall_and_real_restart_restore_checker_without_duplicates_or_resetting_cadence(tmp_path):
    svc, task, _, run = fixture(tmp_path)
    try:
        factory = evaluation_contract_factory(task, svc.store)
        install_evaluation_schemas(svc, factory)
        install_evaluation_schemas(svc, factory)
        for index in range(3):
            run, _ = execute(svc, run, 'write_file', {'path': 'fixture-' + str(index) + '.txt', 'content': 'committed fixture edit'})
        svc.jobs.registry.schemas(run, ['tools'])  # Next preparation consumes the committed journal.
        before = svc.store.run(run['id'])['verification_cadence']
        assert before['required']
        assert sum(item['function']['name'] == 'evaluation_check' for item in svc.extra_tool_schemas(run)) == 1
        svc.store.update_run(run['id'], status='paused')
        assert not svc.core.requests
    finally:
        svc.shutdown()
    restored = service(tmp_path, Engine())
    restored.jobs._launch = lambda run: None
    try:
        assert not hasattr(restored, '_evaluation_schema_registration')
        install_evaluation_schemas(restored, evaluation_contract_factory(task, restored.store))
        saved = restored.store.run(run['id'])
        selected = restored.jobs.registry.schemas(saved, ['tools'])
        after = restored.store.run(run['id'])['verification_cadence']
        assert after['required'] and after['mutations_since_check'] == before['mutations_since_check'] == 3
        assert after['contract_id'] == before['contract_id']
        assert sum(item['function']['name'] == 'evaluation_check' for item in selected) == 1
        assert not restored.core.requests
    finally:
        restored.shutdown()


def test_native_registration_never_reinserts_a_checker_deferred_by_routing(tmp_path, monkeypatch):
    import forge_runs
    svc, task, _, run = fixture(tmp_path)
    try:
        install_evaluation_schemas(svc, evaluation_contract_factory(task, svc.store))
        original = forge_runs.select_schemas
        def defer_checker(*args, **kwargs):
            return [item for item in original(*args, **kwargs)
                    if item['function']['name'] != 'evaluation_check']
        monkeypatch.setattr(forge_runs, 'select_schemas', defer_checker)
        assert 'evaluation_check' not in {item['function']['name']
                                         for item in svc.jobs.registry.schemas(run, ['tools'])}
        assert 'evaluation_check' in {item['function']['name']
                                     for item in svc.jobs.registry.schemas(run, ['tools'], all_tools=True)}
        assert not svc.core.requests
    finally:
        svc.shutdown()


@pytest.mark.parametrize('has_unused_hook', [False, True])
def test_historical_api_fallback_adapts_signature_and_does_not_call_contract_factory(has_unused_hook):
    seen = []
    def historical(run, capabilities):
        seen.append((run, capabilities))
        return [{'function': {'name': 'read_file'}}, {'function': {'name': 'run_command'}}]
    svc = SimpleNamespace(jobs=SimpleNamespace(registry=SimpleNamespace(schemas=historical)))
    if has_unused_hook:
        svc.extra_tool_schemas = lambda run: []  # Existence alone is not registration support.
    def forbidden_factory(run):
        raise AssertionError('Historical runtimes cannot consume private verification contracts.')
    assert install_evaluation_schemas(svc, forbidden_factory) == {'mode': 'compatibility', 'registered_contracts': False}
    values = svc.jobs.registry.schemas({'id': 'old'}, ['tools'], all_tools=True, future_option=True)
    assert seen == [({'id': 'old'}, ['tools'])]
    assert [item['function']['name'] for item in values] == ['evaluation_check', 'read_file']
    assert 'verification_contract' not in values[0]
    assert values[0]['function']['parameters'] == {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    install_evaluation_schemas(svc, forbidden_factory)
    assert sum(item['function']['name'] == 'evaluation_check' for item in svc.jobs.registry.schemas({'id': 'old'}, ['tools'])) == 1


def test_current_registration_respects_native_capabilities_permissions_and_agent_tool_restriction(tmp_path):
    svc, task, _, run = fixture(tmp_path)
    try:
        install_evaluation_schemas(svc, evaluation_contract_factory(task, svc.store))
        assert svc.jobs.registry.schemas(run, []) == []
        denied = deepcopy(run)
        denied['settings']['permission_profile'] = 'deny_access'
        assert svc.jobs.registry.schemas(denied, ['tools']) == []
        restricted = {**run, 'agent_tools': ['read_file']}
        assert 'evaluation_check' not in {item['function']['name'] for item in svc.jobs.registry.schemas(restricted, ['tools'], all_tools=True)}
        assert not svc.core.requests
    finally:
        svc.shutdown()
