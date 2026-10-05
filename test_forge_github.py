"""GitHub fixtures never touch a real account, vault, remote repo or checkout."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from forge_github import GitHubConnection, API_VERSION
from forge_store import ForgeStore, encode

TOKEN='github_pat_fixture_secret_value'


class Vault:
    def __init__(self):self.values={};self.count=0
    def put(self,value,reference=None):
        self.count+=1;reference=reference or 'reference'+str(self.count);self.values[reference]=value;return reference
    def get(self,reference):return self.values.get(reference)
    def delete(self,reference):self.values.pop(reference,None)


def repo(name='example/project',**changes):
    return {'id':42,'full_name':name,'private':True,'default_branch':'main',
            'permissions':{'pull':True,'push':True},**changes}


@pytest.fixture
def github(tmp_path):
    store=ForgeStore(tmp_path/'forge')
    vault=Vault();requests=[];route={'handler':None}
    def response(request):
        requests.append(request)
        if route['handler']:return route['handler'](request)
        if request.url.path=='/user':return httpx.Response(200,json={'login':'fixture-account','email':'private@example.test'})
        if request.url.path=='/user/repos':return httpx.Response(200,json=[repo()])
        if request.url.path=='/repos/example/project':return httpx.Response(200,json=repo())
        if '/contents/' in request.url.path:
            return httpx.Response(200,json={'type':'file','encoding':'base64','size':7,'sha':'fixture-sha',
                                           'content':base64.b64encode(b'Hello\n').decode()})
        if request.url.path.endswith('/pulls'):
            if request.method=='POST':return httpx.Response(201,json={'number':17,'title':'Approved draft','draft':True,'html_url':'https://github.com/example/project/pull/17'})
            return httpx.Response(200,json=[])
        if request.url.path.endswith('/issues/9'):return httpx.Response(200,json={'number':9,'title':'Read me','body':'Untrusted issue text'})
        return httpx.Response(404,json={'message':TOKEN})
    factory=lambda **kwargs:httpx.Client(transport=httpx.MockTransport(response),**kwargs)
    connection=GitHubConnection(SimpleNamespace(store=store,vault=vault),client_factory=factory)
    return connection,vault,requests,route


def connected(github):
    connection=github[0]
    connection.dispatch('github_connect',{'token':TOKEN})
    return connection


def project(connection,name):
    folder=connection.store.home/name;folder.mkdir()
    return connection.store.create_project(name,str(folder))


def selected(connection,proj=None):
    return connection.dispatch('github_select_repo',{'full_name':'example/project','project_id':proj['id'] if proj else None})['repository']


def approved(connection,args,proj=None):
    chat=connection.store.create_chat(proj['id'] if proj else None)
    run=connection.store.create_run({'chat_id':chat['id'],'project_id':proj['id'] if proj else None})
    invocation='approved-invocation'
    connection.store.invocation(invocation,run['id'],'github__create_pull_request',args)
    action={'tool':'github__create_pull_request','arguments':args,'target':'GitHub example/project'}
    target=connection.describe_target('github__create_pull_request',args,{'project_id':proj['id'] if proj else None})
    action['scope']={key:target[key] for key in ('repository','connection_generation','binding_generation')}
    digest=sha256(encode(action).encode()).hexdigest()
    with connection.store._connection(transaction='write') as db:
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',('approved',run['id'],invocation,digest,'approved',encode(action)))
    return {'run_id':run['id'],'project_id':run.get('project_id'),'human_approved':True},invocation


def test_construction_status_and_schema_discovery_have_no_network_or_secret_read(github):
    connection,vault,requests,_=github
    assert connection.status()['connected'] is False
    assert connection.schemas()==[] and requests==[] and vault.values=={}


def test_connect_only_sends_account_check_and_persists_os_vault_reference(github):
    connection=connected(github)
    requests=github[2]
    assert len(requests)==1 and requests[0].url.path=='/user'
    assert requests[0].headers['X-GitHub-Api-Version']==API_VERSION
    assert requests[0].headers['Authorization']=='Bearer '+TOKEN
    assert TOKEN not in json.dumps(connection.status())
    assert TOKEN.encode() not in connection.store.db_path.read_bytes()
    assert 'private@example.test' not in json.dumps(connection.status())


def test_account_import_captures_token_without_secret_arguments_or_output(github,monkeypatch):
    connection,vault,requests,_=github
    calls=[]
    monkeypatch.setattr('forge_github.shutil.which',lambda name:'installed-gh')
    def runner(argv,**kwargs):calls.append(argv);return SimpleNamespace(returncode=0,stdout=TOKEN+'\n',stderr='')
    connection.gh_runner=runner
    result=connection.connect({'import_gh':True})
    assert result['login']=='fixture-account' and TOKEN not in json.dumps(calls)
    assert calls[0][1:]==['auth','token','--hostname','github.com']


def test_bad_authentication_keeps_previous_connection_and_never_logs_response_token(github):
    connection=connected(github)
    previous=connection._config()['credential_ref']
    github[3]['handler']=lambda request:httpx.Response(401,json={'message':TOKEN})
    with pytest.raises(ValueError,match='access denied') as error:
        connection.connect({'token':'replacement_fixture_token'})
    assert TOKEN not in str(error.value) and connection._config()['credential_ref']==previous
    assert github[1].get(previous)==TOKEN


def test_reconnect_replaces_secret_and_disconnect_revokes_bindings(github):
    connection=connected(github);selected(connection)
    previous=connection._config()['credential_ref']
    connection.connect({'token':'replacement_fixture_token'})
    assert previous not in github[1].values
    assert connection.status()['repositories']
    connection.disconnect()
    assert not github[1].values and not connection.status()['repositories']
    with pytest.raises(ValueError,match='disconnected'):
        connection.execute('github__read_file',{'repo':'example/project','path':'README.md'})


def test_repository_scopes_prevent_cross_project_access_and_global_account_listing(github):
    connection=connected(github)
    alpha=project(connection,'alpha');beta=project(connection,'beta')
    selected(connection,alpha)
    assert connection.schemas(alpha)
    assert connection.schemas(beta)==[]
    before=len(github[2])
    with pytest.raises(ValueError,match='not selected'):
        connection.execute('github__read_file',{'repo':'example/project','path':'README.md'},{'project_id':beta['id']})
    assert len(github[2])==before
    assert connection.execute('github__list_repos',{}, {'project_id':beta['id']})['repositories']==[]
    assert len(connection.execute('github__list_repos',{}, {'project_id':alpha['id']})['repositories'])==1
    assert TOKEN not in json.dumps(connection.execute('github__list_repos',{}, {'project_id':alpha['id']}))


@pytest.mark.parametrize('name',['https://evil.example/repo','example/../other','example/project?token=x','example\\project','example/..'])
def test_repository_names_cannot_choose_endpoint_or_traverse(github,name):
    connection=connected(github);before=len(github[2])
    with pytest.raises(ValueError):connection.select_repository({'full_name':name})
    assert len(github[2])==before


@pytest.mark.parametrize('path',['../secret.txt','a/../../secret.txt','/etc/passwd','C:\\secret','dir\\secret','file:secret'])
def test_file_paths_cannot_escape_repository(github,path):
    connection=connected(github);selected(connection);before=len(github[2])
    with pytest.raises(ValueError,match='relative repository'):
        connection.execute('github__read_file',{'repo':'example/project','path':path})
    assert len(github[2])==before


def test_contents_are_read_from_fixed_api_not_returned_download_url(github):
    connection=connected(github);selected(connection)
    github[3]['handler']=lambda request:httpx.Response(200,json={'type':'file','encoding':'base64','size':7,
        'content':base64.b64encode(b'Hello\n').decode(),'download_url':'https://evil.example/steal'})
    result=connection.execute('github__read_file',{'repo':'example/project','path':'README.md','ref':'feature/new'})
    assert result['content']=='Hello\n'
    assert github[2][-1].url.host=='api.github.com'
    assert github[2][-1].url.params['ref']=='feature/new'


def test_redirects_and_missing_files_do_not_forward_credentials_or_echo_server_error(github):
    connection=connected(github);selected(connection)
    github[3]['handler']=lambda request:httpx.Response(302,headers={'Location':'https://evil.example/'+TOKEN})
    before=len(github[2])
    with pytest.raises(ValueError,match='redirected') as error:
        connection.execute('github__read_file',{'repo':'example/project','path':'README.md'})
    assert len(github[2])==before+1 and TOKEN not in str(error.value)
    github[3]['handler']=lambda request:httpx.Response(404,json={'message':TOKEN})
    with pytest.raises(ValueError,match='not found') as error:
        connection.execute('github__read_file',{'repo':'example/project','path':'missing.txt'})
    assert TOKEN not in str(error.value)


def test_code_search_cannot_expand_repository_and_filters_server_items(github):
    connection=connected(github);selected(connection)
    with pytest.raises(ValueError,match='restricted'):
        connection.execute('github__search_code',{'repo':'example/project','query':'secret OR repo:other/private'})
    github[3]['handler']=lambda request:httpx.Response(200,json={'total_count':2,'items':[
        {'path':'allowed.py','repository':{'full_name':'example/project'}},
        {'path':'private.py','repository':{'full_name':'other/private'}}]})
    result=connection.execute('github__search_code',{'repo':'example/project','query':'function'})
    assert [value['path'] for value in result['items']]==['allowed.py']
    assert github[2][-1].url.params['q']=='function repo:example/project'


def test_pr_requires_exact_recorded_approval_and_is_draft_once(github):
    connection=connected(github);selected(connection)
    args={'repo':'example/project','title':'Approved draft','body':'Reviewed body','head':'feature','base':'main'}
    denied=connection.execute('github__create_pull_request',args,{'human_approved':True})
    assert denied['not_executed'] and not any(r.method=='POST' for r in github[2])
    context,invocation=approved(connection,args)
    changed=connection.execute('github__create_pull_request',{**args,'body':'Changed after review'},context,invocation_id=invocation)
    assert changed['not_executed'] and not any(r.method=='POST' for r in github[2])
    created=connection.execute('github__create_pull_request',args,context,invocation_id=invocation)
    assert created['pull_request']['draft'] is True
    assert json.loads(github[2][-1].content)['draft'] is True
    again=connection.execute('github__create_pull_request',args,context,invocation_id=invocation)
    assert again['not_executed'] and len([r for r in github[2] if r.method=='POST'])==1


def test_model_arguments_cannot_claim_approval_and_wrong_project_cannot_send(github):
    connection=connected(github);alpha=project(connection,'alpha');beta=project(connection,'beta');selected(connection,alpha)
    args={'repo':'example/project','title':'Draft','head':'feature','base':'main'}
    result=connection.execute('github__create_pull_request',{**args,'human_approved':True},{'project_id':alpha['id']})
    assert result['not_executed']
    context,invocation=approved(connection,args,alpha)
    result=connection.execute('github__create_pull_request',args,{**context,'project_id':beta['id']},invocation_id=invocation)
    assert result['not_executed'] and not any(r.method=='POST' for r in github[2])


def test_post_connection_failure_reports_unknown_outcome_and_consumes_approval(github):
    connection=connected(github);selected(connection)
    args={'repo':'example/project','title':'Draft','head':'feature','base':'main'}
    context,invocation=approved(connection,args)
    def fail(request):raise httpx.ReadError(TOKEN,request=request)
    github[3]['handler']=fail
    result=connection.execute('github__create_pull_request',args,context,invocation_id=invocation)
    assert result['outcome_unknown'] and TOKEN not in json.dumps(result)
    with connection.store._connection() as db:
        assert db.execute('SELECT status FROM approvals').fetchone()[0]=='used'


def test_approved_action_cannot_survive_reconnection_even_at_direct_tool_boundary(github):
    connection=connected(github);selected(connection)
    args={'repo':'example/project','title':'Draft','head':'feature','base':'main'}
    context,invocation=approved(connection,args)
    connection.connect({'token':'replacement_fixture_token'})
    result=connection.execute('github__create_pull_request',args,context,invocation_id=invocation)
    assert result['not_executed'] and not any(r.method=='POST' for r in github[2])


def test_clone_secret_environment_fixed_helpers_no_hooks_and_preserved_license(github,monkeypatch):
    connection=connected(github);calls=[];helpers=[]
    monkeypatch.setattr('forge_github.shutil.which',lambda name:'trusted-git')
    def runner(argv,**kwargs):
        calls.append((argv,kwargs))
        helper=Path(kwargs['env']['GIT_ASKPASS']);helpers.append(str(helper))
        assert TOKEN not in helper.read_text(encoding='utf-8')
        if 'clone' in argv:
            target=Path(argv[-1]);(target/'.git').mkdir();(target/'LICENSE').write_text('MIT\n',encoding='utf-8')
        return SimpleNamespace(returncode=0,stdout='',stderr=TOKEN)
    connection.git_runner=runner
    result=connection.clone({'full_name':'example/project'})
    assert result['project']['id']==result['repository']['project_id']
    assert (Path(result['path'])/'LICENSE').read_text()=='MIT\n'
    assert all(TOKEN not in json.dumps(argv) for argv,_ in calls)
    assert all(kwargs['env']['FORGE_GITHUB_TOKEN']==TOKEN for _,kwargs in calls)
    first=calls[0][0]
    assert '--no-recurse-submodules' in first and '--no-checkout' in first
    assert 'protocol.file.allow=never' in first and 'http.followRedirects=false' in first
    assert all(not Path(path).exists() for path in helpers)
    assert TOKEN.encode() not in connection.store.db_path.read_bytes()


def test_clone_failure_redacts_subprocess_output_and_cleans_only_new_target(github,monkeypatch):
    connection=connected(github)
    alpha=project(connection,'alpha');original=selected(connection,alpha)
    existing=connection.store.home/'projects/existing';existing.mkdir();(existing/'keep.txt').write_text('Keep me')
    monkeypatch.setattr('forge_github.shutil.which',lambda name:'trusted-git')
    connection.git_runner=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout=TOKEN,stderr=TOKEN)
    with pytest.raises(ValueError,match='clone failed') as error:
        connection.clone({'full_name':'example/project'})
    assert TOKEN not in str(error.value)
    assert list((connection.store.home/'projects').iterdir())==[existing]
    assert [p['id'] for p in connection.store.list_projects()]==[alpha['id']]
    assert connection.store.entity('github_repositories',original['id'])['project_id']==alpha['id']


def test_clone_collision_preserves_existing_directory(github,monkeypatch):
    connection=connected(github)
    monkeypatch.setattr('forge_github.uuid4',lambda:SimpleNamespace(hex='a'*32))
    monkeypatch.setattr('forge_github.shutil.which',lambda name:'trusted-git')
    target=connection.store.home/'projects'/('project-'+'a'*12);target.mkdir();(target/'keep.txt').write_text('Keep')
    with pytest.raises(FileExistsError):connection.clone({'full_name':'example/project'})
    assert (target/'keep.txt').read_text()=='Keep'


def test_public_mode_requires_owner_and_cannot_access_private_repository(github):
    connection=github[0]
    connection.connect({'public':True})
    assert github[2]==[] and github[1].values=={}
    with pytest.raises(ValueError,match='owner'):
        connection.repositories({})
    with pytest.raises(ValueError,match='Private'):
        selected(connection)
    assert 'Authorization' not in github[2][-1].headers


def test_public_mode_exposes_read_tools_only_and_cannot_dispatch_remote_writes(github):
    connection=github[0];connection.connect({'public':True})
    github[3]['handler']=lambda request:httpx.Response(200,json=repo(private=False))
    selected(connection)
    assert len(connection.schemas())==5
    assert all(schema['capability']=='read' for schema in connection.schemas())
    result=connection.execute('github__create_pull_request',{'repo':'example/project','title':'Draft','head':'feature','base':'main'})
    assert result['not_executed'] and not any(r.method=='POST' for r in github[2])
