"""Exact live native admission, with synthetic settings and no Windows calls."""
from types import SimpleNamespace

import pytest

from forge_runs import ToolRegistry


@pytest.mark.parametrize('original,latest,approved,expected', [
    ('allow','full_access',True,True), ('allow','always_ask',True,False),
    ('ask','always_ask',True,True), ('ask','full_access',True,True),
    ('ask','deny_access',True,False), ('ask','always_ask',False,False),
    ('deny','full_access',True,False), ('allow','full_access',1,False)])
def test_exact_admission_retains_approved_ask_but_never_new_ask(original,latest,approved,expected):
    live={'permission_profile':latest,'computer_tools':True,
          'permission_overrides':{'app:synthetic-app':latest}}
    registry=ToolRegistry(SimpleNamespace(store=SimpleNamespace(get_settings=lambda:live)))
    check=registry.computer_authority({'id':'synthetic-run','settings':{}},
        {'function':{'name':'computer_set_value'}},{'value':'synthetic'},{'app':'synthetic-app'},original,approved)
    assert check() is expected
    live['computer_tools']=False
    assert check() is False


def test_admitted_app_identity_is_private_and_immutable():
    live={'permission_profile':'full_access','computer_tools':True,'permission_overrides':{'app:original-app':'deny_access'}}
    registry=ToolRegistry(SimpleNamespace(store=SimpleNamespace(get_settings=lambda:live)))
    target={'app':'original-app'}
    check=registry.computer_authority({'id':'synthetic-run','settings':{}},
        {'function':{'name':'computer_set_value'}},{},target,'allow',True)
    target['app']='different-app'
    assert check() is False


def test_authority_failure_never_exports_private_store_error():
    def failure(): raise RuntimeError('SYNTHETIC_PRIVATE_AUTHORITY_DETAIL')
    registry=ToolRegistry(SimpleNamespace(store=SimpleNamespace(get_settings=failure)))
    check=registry.computer_authority({'id':'synthetic-run','settings':{}},
        {'function':{'name':'computer_set_value'}},{},{},'allow',True)
    assert check() is False


def test_native_binding_covers_run_tool_snapshot_and_literal_without_type_coercion():
    registry=ToolRegistry(SimpleNamespace())
    args={'snapshot_id':'admitted-snapshot','value':'literal','include_image':False}
    check=registry.computer_authority({'id':'admitted-run','settings':{}},
        {'function':{'name':'computer_set_value'}},args,{},'ask',True)
    assert check.matches('admitted-run','computer_set_value',dict(args)) is True
    for identifier,name,changed in [
        ('other-run','computer_set_value',args), ('admitted-run','computer_type',args),
        ('admitted-run','computer_set_value',{**args,'snapshot_id':'other-snapshot'}),
        ('admitted-run','computer_set_value',{**args,'value':'different literal'}),
        ('admitted-run','computer_set_value',{**args,'include_image':0})]:
        assert check.matches(identifier,name,changed) is False
