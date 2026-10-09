"""Installed-app workflow checks find existing Python without Store launches."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import forge_ai_workflow as workflow
from test_forge50_collaboration import setup


def executable(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('Synthetic executable candidate',encoding='utf-8')
    return path.resolve()


def valid_probe(path):
    return SimpleNamespace(returncode=0,stdout=json.dumps({'implementation':'cpython',
        'version':[3,12,14],'executable':str(path)}))


@pytest.mark.skipif(os.name!='nt',reason='Windows installed-app discovery')
def test_frozen_launch_skips_store_alias_and_uses_existing_uv_python(tmp_path,monkeypatch):
    alias=executable(tmp_path/'WindowsApps'/'python.exe')
    managed=executable(tmp_path/'Roaming'/'uv'/'python'/'cpython-3.12.14-windows-x86_64-none'/'python.exe')
    application=executable(tmp_path/'Forge.exe')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(application))
    monkeypatch.setattr(workflow.shutil,'which',lambda _:str(alias))
    monkeypatch.setattr(workflow.os,'get_exec_path',lambda: [str(alias.parent)])
    monkeypatch.setattr(workflow,'_registry_pythons',lambda: [])
    monkeypatch.setenv('APPDATA',str(tmp_path/'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    monkeypatch.delenv('UV_PYTHON_INSTALL_DIR',raising=False)
    monkeypatch.setattr(workflow.Path,'home',lambda:tmp_path)
    calls=[]
    def probe(argv,**kwargs):
        calls.append((argv,kwargs));return valid_probe(managed)
    monkeypatch.setattr(workflow.subprocess,'run',probe)
    environment=dict(os.environ)
    assert workflow._resolve_python()==str(managed)
    assert len(calls)==1 and calls[0][0][0]==str(managed)
    assert calls[0][0][1:4]==['-I','-S','-c']
    assert 0<calls[0][1]['timeout']<=2
    assert calls[0][1]['creationflags']==subprocess.CREATE_NO_WINDOW
    assert dict(os.environ)==environment


@pytest.mark.skipif(os.name!='nt',reason='Windows Store alias')
def test_path_scan_finds_real_install_after_first_store_alias(tmp_path,monkeypatch):
    alias=executable(tmp_path/'WindowsApps'/'python.exe')
    real=executable(tmp_path/'Python312'/'python.exe')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(workflow.shutil,'which',lambda _:str(alias))
    monkeypatch.setattr(workflow.os,'get_exec_path',lambda:[str(alias.parent),str(real.parent)])
    monkeypatch.setattr(workflow,'_registry_pythons',lambda:[])
    monkeypatch.setattr(workflow.Path,'home',lambda:tmp_path)
    monkeypatch.delenv('APPDATA',raising=False);monkeypatch.delenv('LOCALAPPDATA',raising=False)
    monkeypatch.delenv('UV_PYTHON_INSTALL_DIR',raising=False)
    assert workflow._python_candidates()==[real]


@pytest.mark.skipif(os.name!='nt',reason='Windows Python registry')
def test_registry_discovers_executable_and_legacy_install_path(tmp_path,monkeypatch):
    current=tmp_path/'Python312'/'python.exe';legacy=tmp_path/'Python311'/'python.exe'
    class Key:
        def __init__(self,path):self.path=path
        def __enter__(self):return self
        def __exit__(self,*_):pass
    def enum(key,index):
        if index>=2:raise OSError('No more versions')
        return ['3.12','3.11'][index]
    def query(key,name):
        if '3.12' in key.path:return str(current),1
        raise OSError('Legacy install has no ExecutablePath')
    registry=SimpleNamespace(HKEY_CURRENT_USER='user',HKEY_LOCAL_MACHINE='machine',KEY_READ=1,
        KEY_WOW64_64KEY=2,KEY_WOW64_32KEY=4,
        OpenKey=lambda parent,path,*_:Key(path),EnumKey=enum,QueryValueEx=query,
        QueryValue=lambda key,_:str(legacy.parent))
    monkeypatch.setitem(sys.modules,'winreg',registry)
    assert set(workflow._registry_pythons())=={current,legacy}


@pytest.mark.parametrize('response',[
    SimpleNamespace(returncode=1,stdout='Bad executable'),
    SimpleNamespace(returncode=0,stdout='Windows Store'),
    SimpleNamespace(returncode=0,stdout='[]'),
    SimpleNamespace(returncode=0,stdout=json.dumps({'implementation':'cpython','version':[3,9,0],'executable':'ignored'})),
    SimpleNamespace(returncode=0,stdout=json.dumps({'implementation':'cpython','version':[3,12,0],'executable':'wrong.exe'})),
    subprocess.TimeoutExpired(['python.exe'],2),
])
def test_failed_probe_does_not_claim_a_working_interpreter(tmp_path,monkeypatch,response):
    candidate=executable(tmp_path/'python.exe')
    monkeypatch.setattr(workflow,'_python_candidates',lambda:[candidate])
    def probe(*_,**kwargs):
        if isinstance(response,Exception):raise response
        return response
    monkeypatch.setattr(workflow.subprocess,'run',probe)
    with pytest.raises(ValueError,match='working installed Python 3.10'):
        workflow._resolve_python()


def test_total_probe_budget_stops_before_another_candidate(tmp_path,monkeypatch):
    candidates=[executable(tmp_path/f'python{index}.exe') for index in range(3)]
    monkeypatch.setattr(workflow,'_python_candidates',lambda:candidates)
    times=iter([100,101,109]);monkeypatch.setattr(workflow.time,'monotonic',lambda:next(times))
    calls=[]
    def probe(argv,**kwargs):
        calls.append(argv);raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
    monkeypatch.setattr(workflow.subprocess,'run',probe)
    with pytest.raises(ValueError,match='working installed Python'):workflow._resolve_python()
    assert len(calls)==1


@pytest.mark.parametrize('frozen',[False,True])
def test_installed_interpreter_runs_the_actual_independent_fixture(tmp_path,monkeypatch,frozen):
    service=setup(tmp_path)
    try:
        checks=workflow.AIWorkflow(service)
        monkeypatch.setattr(sys,'frozen',frozen,raising=False)
        folder,project,_,_,_,expected=checks._fixture('python-resolution','ordinary')
        selected=service.store.entity('ai_workflow_fixtures',project['id'])['python']
        if not frozen:assert Path(selected).resolve()==Path(sys.executable).resolve()
        (folder/'proof.csv').write_text(expected,encoding='utf-8',newline='\n')
        result=subprocess.run([selected,'check_fixture.py'],cwd=folder,capture_output=True,
            text=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert result.returncode==0 and 'Exact workflow fixture passed' in result.stdout
        (folder/'proof.csv').write_text('connection,status\nopenrouter,unverified\n',encoding='utf-8')
        failed=subprocess.run([selected,'check_fixture.py'],cwd=folder,capture_output=True,
            text=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert failed.returncode!=0
    finally:service.shutdown()
