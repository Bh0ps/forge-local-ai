from forge_service import ForgeService
from test_forge_workspaces import ScriptedEngine


def test_workspace_root_empty_path_lists_and_reads_existing_project(tmp_path):
    folder=tmp_path/'existing'; folder.mkdir(); (folder/'notes.md').write_text('Saved project text',encoding='utf-8')
    svc=ForgeService(core=ScriptedEngine(),data_dir=tmp_path/'state')
    try:
        project=svc.create_project({'name':'Existing','path':str(folder)})
        listed=svc.dispatch('workspace_files',{'project_id':project['id'],'path':''})
        assert listed['ok'] and listed['entries']==[{'path':'notes.md','type':'file','size':18,'name':'notes.md','is_dir':False}]
        assert svc.dispatch('workspace_read',{'project_id':project['id'],'path':'notes.md'})['content']=='Saved project text'
        assert svc.dispatch('workspace_read',{'project_id':project['id'],'path':'../outside.md'})['ok'] is False
        assert svc.dispatch('workspace_read',{'project_id':project['id'],'path':''})['ok'] is False
    finally: svc.shutdown()
