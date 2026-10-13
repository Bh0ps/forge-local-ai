"""Goal project scopes and exports use disposable data and no inference/network."""
from pathlib import Path

import pytest

from forge_service import ForgeService
from test_forge_core import Engine


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    class Vault:
        def get(self, reference): pytest.fail('Goal scope checks need no credentials')
    monkeypatch.setattr('forge_credentials.CredentialVault', Vault)
    service=ForgeService(core=Engine(),data_dir=tmp_path/'disposable')
    service.store.update_settings({'model':'synthetic'})
    service.jobs._launch=lambda run:None
    projects=[]
    for name in ('alpha','beta'):
        folder=tmp_path/name;folder.mkdir()
        (folder/'FORGE_TODO.md').write_text('Original '+name,encoding='utf-8')
        projects.append(service.create_project({'name':name,'path':str(folder)}))
    goals=[]
    for index,owner in enumerate((projects[0]['id'],projects[1]['id'],None)):
        chat=service.store.create_chat(owner,'Synthetic '+str(index))
        goal=service.goal_create({'title':'Synthetic goal '+str(index),'text':'Private synthetic work '+str(index),
            'project_id':owner,'chat_id':chat['id'],'tasks':[{'text':'Verify synthetic task','status':'pending'}]})
        service.jobs.start({'text':'Execute synthetic task','chat_id':chat['id'],'project_id':owner,'goal_id':goal['id'],'mode':'goal'})
        goals.append(service.store.goal(goal['id']))
    yield service,projects,goals
    service.shutdown()


@pytest.mark.parametrize('flags',[{}, {'active_only':True}, {'active':True}])
def test_omitted_scope_is_global_but_exact_project_and_null_are_filtered(workspace,flags):
    service,projects,goals=workspace
    ids=lambda data:{goal['id'] for goal in service.dispatch('goals',data)['goals']}
    assert ids(flags)=={goal['id'] for goal in goals}
    assert ids({**flags,'project_id':projects[0]['id']})=={goals[0]['id']}
    assert ids({**flags,'project_id':projects[1]['id']})=={goals[1]['id']}
    assert ids({**flags,'project_id':None})=={goals[2]['id']}
    assert ids({**flags,'project_id':'not-a-saved-project'})==set()


@pytest.mark.parametrize('bad',[False,0,[],{},'', ' padded ', 'x'*65, 'bad\x00scope'])
@pytest.mark.parametrize('action',['goals','goal_get','goal_export'])
def test_invalid_provided_project_scope_is_never_an_unscoped_fallback(workspace,bad,action):
    service,projects,goals=workspace
    with pytest.raises(ValueError,match='valid project scope'):
        service.dispatch(action,{'id':goals[0]['id'],'project_id':bad})
    assert (Path(projects[0]['path'])/'FORGE_TODO.md').read_text()=='Original alpha'
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'


def test_optional_goal_get_scope_checks_owner_before_goal_view(workspace,monkeypatch):
    service,projects,goals=workspace
    for goal in goals:
        assert service.dispatch('goal_get',{'id':goal['id']})['goal']['id']==goal['id']
        assert service.dispatch('goal_get',{'id':goal['id'],'project_id':goal['project_id']})['goal']['id']==goal['id']
    monkeypatch.setattr(service,'goal_view',lambda *args:pytest.fail('Foreign goal must not reach its view'))
    for project_id in (projects[1]['id'],None):
        with pytest.raises(ValueError,match='another project'):
            service.dispatch('goal_get',{'id':goals[0]['id'],'project_id':project_id})


@pytest.mark.parametrize('destination',['foreign','null'])
def test_foreign_export_is_refused_before_file_tools_or_backup_side_effects(workspace,monkeypatch,destination):
    service,projects,goals=workspace
    before={str(path.relative_to(service.store.home/'backups')):path.read_bytes()
        for path in (service.store.home/'backups').rglob('*') if path.is_file()}
    monkeypatch.setattr('forge_service.ProjectTools',lambda *args:pytest.fail('Foreign export must create no file tool'))
    with pytest.raises(ValueError,match='another project'):
        service.dispatch('goal_export',{'id':goals[0]['id'],
            'project_id':projects[1]['id'] if destination=='foreign' else None})
    assert (Path(projects[0]['path'])/'FORGE_TODO.md').read_text()=='Original alpha'
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'
    assert before=={str(path.relative_to(service.store.home/'backups')):path.read_bytes()
        for path in (service.store.home/'backups').rglob('*') if path.is_file()}


@pytest.mark.parametrize('explicit',[False,True])
def test_export_uses_authoritative_owner_with_default_and_relative_destination(workspace,explicit):
    service,projects,goals=workspace
    payload={'id':goals[0]['id']}
    if explicit:
        (Path(projects[0]['path'])/'notes').mkdir()
        payload.update(project_id=projects[0]['id'],path='notes/checklist.md')
    result=service.dispatch('goal_export',payload)
    path=Path(projects[0]['path'])/('notes/checklist.md' if explicit else 'FORGE_TODO.md')
    assert result['ok'] is True and path.read_text(encoding='utf-8')==goals[0]['markdown']
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'


def test_goal_owner_change_after_selection_rejects_stale_export_destination(workspace):
    service,projects,goals=workspace
    service.dispatch('goal_update',{'id':goals[0]['id'],'project_id':projects[1]['id']})
    with pytest.raises(ValueError,match='another project'):
        service.dispatch('goal_export',{'id':goals[0]['id'],'project_id':projects[0]['id']})
    assert (Path(projects[0]['path'])/'FORGE_TODO.md').read_text()=='Original alpha'
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'


def test_export_relative_path_cannot_escape_authoritative_owner(workspace):
    service,projects,goals=workspace
    result=service.dispatch('goal_export',{'id':goals[0]['id'],'project_id':projects[0]['id'],
        'path':'../beta/FORGE_TODO.md'})
    assert result['ok'] is False
    assert (Path(projects[0]['path'])/'FORGE_TODO.md').read_text()=='Original alpha'
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'


@pytest.mark.parametrize('destination',['omitted','null','project'])
def test_unassigned_goal_cannot_be_exported_into_a_selected_foreign_project(workspace,destination):
    service,projects,goals=workspace;payload={'id':goals[2]['id']}
    if destination!='omitted':payload['project_id']=None if destination=='null' else projects[0]['id']
    with pytest.raises(ValueError,match='project'):
        service.dispatch('goal_export',payload)
    assert (Path(projects[0]['path'])/'FORGE_TODO.md').read_text()=='Original alpha'
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'


def test_removed_project_goal_stays_preserved_and_cannot_write_into_another_project(workspace):
    service,projects,goals=workspace
    # Existing lifecycle stops the synthetic owned run and preserves its goal.
    service.dispatch('project_delete',{'id':projects[0]['id']})
    goal=service.store.goal(goals[0]['id'])
    assert goal['project_missing'] and goal['project_id'] is None
    assert Path(goal['path']).read_text()==goals[0]['markdown']
    assert goals[0]['id'] in {item['id'] for item in service.dispatch('goals',{'project_id':None})['goals']}
    for payload in ({'id':goal['id']},{'id':goal['id'],'project_id':None},
        {'id':goal['id'],'project_id':projects[1]['id']}):
        with pytest.raises(ValueError,match='project'):service.dispatch('goal_export',payload)
    assert (Path(projects[1]['path'])/'FORGE_TODO.md').read_text()=='Original beta'
