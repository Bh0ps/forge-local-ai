"""Explicit workflow setup and durable end-to-end connection receipts.

Status is a local read. Metadata success and fixture inference are independent
checks; no account label, credential or unrelated conversation is returned.
"""
from hashlib import sha256
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from forge_collaboration import SPECIALTIES, setup_specialists
from forge_cloud_policy import cloud_policy
from forge_goal_review import actual_models
from forge_store import TERMINAL, encode
from storage import _now


def _registry_pythons():
    """Read existing per-user and machine Python registrations; never install."""
    if os.name!='nt':return []
    import winreg
    result=[]
    for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive,r'Software\Python\PythonCore',0,winreg.KEY_READ|view) as root:
                    versions=[]
                    for index in range(24):
                        try:versions.append(winreg.EnumKey(root,index))
                        except OSError:break
                    for version in sorted(versions,reverse=True):
                        try:
                            with winreg.OpenKey(root,version+r'\InstallPath') as key:
                                try:result.append(Path(winreg.QueryValueEx(key,'ExecutablePath')[0]))
                                except OSError:result.append(Path(winreg.QueryValue(key,None))/'python.exe')
                        except (OSError,TypeError,ValueError):continue
            except OSError:continue
    return result


def _python_candidates():
    candidates=[]
    if not getattr(sys,'frozen',False):candidates.append(Path(sys.executable))
    names=('python.exe','python3.exe','python3.12.exe') if os.name=='nt' else ('python3','python')
    # which alone can stop at the Store alias even with a real interpreter later
    # on PATH. Inspect a bounded number of directories without changing PATH.
    for name in names:
        found=shutil.which(name)
        if found:candidates.append(Path(found))
    for directory in os.get_exec_path()[:64]:
        if directory:candidates.extend(Path(directory)/name for name in names)
    candidates.extend(_registry_pythons())
    roots=[]
    if os.environ.get('UV_PYTHON_INSTALL_DIR'):roots.append(Path(os.environ['UV_PYTHON_INSTALL_DIR']))
    if os.name=='nt':
        for name in ('APPDATA','LOCALAPPDATA'):
            if os.environ.get(name):roots.append(Path(os.environ[name])/'uv'/'python')
        roots.append(Path.home()/'AppData'/'Roaming'/'uv'/'python')
    else:roots.append(Path.home()/'.local'/'share'/'uv'/'python')
    for root in roots:
        try:
            pattern='cpython-*/python.exe' if os.name=='nt' else 'cpython-*/bin/python3'
            candidates.extend(sorted(root.glob(pattern),reverse=True)[:24])
        except OSError:continue
    unique=[];seen=set()
    for candidate in candidates:
        try:
            path=candidate.resolve();identity=os.path.normcase(str(path))
            if (identity in seen or any(part.casefold()=='windowsapps' for part in path.parts)
                    or not path.is_file()):continue
            seen.add(identity);unique.append(path)
        except OSError:continue
    return unique[:32]


def _resolve_python():
    """Return a tested installed CPython for disposable stdlib fixture checks."""
    deadline=time.monotonic()+8
    probe="import json,sys;print(json.dumps({'implementation':sys.implementation.name,'version':list(sys.version_info[:3]),'executable':sys.executable}))"
    for candidate in _python_candidates():
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        try:
            result=subprocess.run([str(candidate),'-I','-S','-c',probe],capture_output=True,text=True,
                timeout=min(2,remaining),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
            if result.returncode or len(result.stdout)>4096:continue
            value=json.loads(result.stdout)
            if not isinstance(value,dict):continue
            version=value.get('version')
            if (value.get('implementation')!='cpython' or not isinstance(version,list) or len(version)!=3
                    or any(type(v) is not int for v in version) or tuple(version)<(3,10,0)):continue
            actual=Path(value['executable']).resolve()
            if actual==candidate and actual.is_file():return str(actual)
        except (OSError,ValueError,TypeError,KeyError,subprocess.TimeoutExpired):continue
    raise ValueError('Workflow verification needs a working installed Python 3.10 or newer for its independent checks. Windows Store aliases are not interpreters; install Python or expose an existing installation, then retry verification.')


class AIWorkflow:
    def __init__(self,service):
        self.service=service;self.store=service.store;self.lock=threading.RLock();self.jobs={}
        for item in self.store.entities('ai_workflow_tests'):
            if item.get('state') in ('running','waiting','awaiting_approval'):
                self.store.save_entity('ai_workflow_tests',{**item,'state':'interrupted',
                    'error':'Verification stopped during restart. Saved runs and evidence are retained; no requests were replayed.'})

    def identities(self):
        config=next((p for p in self.store.entities('providers') if p['id']=='openrouter'),{})
        profiles=[p for p in self.store.entities('agents') if p.get('provider_id')=='openrouter']
        selection={k:self.store.get_settings().get(k) for k in ('provider_id','model','context')}
        safe_config={k:config.get(k) for k in ('id','kind','url','enabled','remote_consent','credential_ref','data_collection','model')}
        return {'configuration_fingerprint':sha256(encode(safe_config).encode()).hexdigest(),
                'profiles_fingerprint':sha256(encode(profiles).encode()).hexdigest(),'local_selection':selection}

    def status(self):
        config=next((p for p in self.store.entities('providers') if p['id']=='openrouter'),{})
        settings=self.store.get_settings()
        profiles=[p for p in self.store.entities('agents') if p.get('provider_id')=='openrouter']
        ready=[role for role in SPECIALTIES if any(p.get('specialty')==role and p.get('enabled',True)
               and p.get('role') in ('researcher','reviewer') and
               (p.get('model')=='openrouter/free' or str(p.get('model','')).endswith(':free')) for p in profiles)]
        quota=(config.get('account') or {}).get('free_model_daily_requests') or {}
        remaining=quota.get('remaining') if type(quota.get('remaining')) is int else None
        sessions=sorted(self.store.entities('ai_workflow_tests'),key=lambda v:v.get('created_at',''),reverse=True)
        last=sessions[0] if sessions else {}
        workflow={'state':'unverified','checked_at':None,'receipt_id':None,'checks':[]}
        if last:
            workflow={'state':last.get('state','unverified'),'checked_at':last.get('finished_at'),
                      'receipt_id':last['id'],'checks':last.get('checks',[]),'error':last.get('error')}
            if workflow['state']=='verified' and any(last.get(k)!=v for k,v in self.identities().items()):
                workflow.update(state='stale',error='Connection, profile or local selection changed after verification. Prior evidence remains saved.')
        return {'version':1,'guidance_enabled':settings.get('goal_cloud_guidance',False),
            'configuration':{'state':'configured' if config.get('credential_ref') and config.get('enabled') and config.get('remote_consent') else
                             ('disabled' if config.get('credential_ref') else 'missing'),
                             'enabled':bool(config.get('enabled')),'consent':bool(config.get('remote_consent'))},
            'authentication':config.get('metadata_auth') or {'state':'unverified','checked_at':None},
            'free_capacity':{'state':'unknown' if remaining is None else ('available' if remaining>0 else 'exhausted'),
                             'remaining':remaining,'checked_at':config.get('account_checked_at'),
                             'note':'Metadata does not guarantee inference capacity.'},
            'catalog':{'state':'validated' if config.get('catalog_verified_at') else 'unverified',
                       'free_models':len(config.get('models') or []),'checked_at':config.get('catalog_verified_at'),
                       'error':config.get('catalog_error')},
            'profiles':{'state':'ready' if len(ready)==len(SPECIALTIES) else 'incomplete','ready':ready,
                        'missing':[role for role in SPECIALTIES if role not in ready],
                        'checked_at':max([p.get('updated_at','') for p in profiles],default=None)},
            'workflow':workflow,'sessions':[{'id':v['id'],'state':v.get('state'),'created_at':v.get('created_at')} for v in sessions[:10]]}

    def setup(self,data=None):
        data=data or {}
        if set(data)-{'goal_guidance'}:raise ValueError('Workflow setup accepts only the explicit goal guidance switch.')
        enabled=data.get('goal_guidance',True)
        if type(enabled) is not bool:raise ValueError('Goal guidance must be true or false.')
        cloud_policy(self.service).consent()
        result=setup_specialists(self.service)
        self.store.update_settings({'goal_cloud_guidance':enabled})
        return {**result,'settings':self.store.get_settings(),'status':self.status()}

    def _save(self,identifier,**changes):
        with self.lock:
            current=self.store.entity('ai_workflow_tests',identifier)
            return self.store.save_entity('ai_workflow_tests',{**current,**changes})

    def self_test(self,data=None):
        data=data or {}
        if set(data)-{'client_submission_id'}:raise ValueError('Unknown workflow verification argument.')
        client=data.get('client_submission_id')
        if client is not None and (not isinstance(client,str) or not 1<=len(client)<=128):raise ValueError('Invalid verification request identity.')
        cloud_policy(self.service).consent()
        if not self.store.get_settings().get('goal_cloud_guidance'):raise ValueError('Enable OpenRouter guidance before workflow verification.')
        if self.status()['profiles']['state']!='ready':raise ValueError('Set up the required specialist profiles before verification.')
        with self.lock:
            previous=next((v for v in self.store.entities('ai_workflow_tests') if client and v.get('client_submission_id')==client),None)
            if previous:return self._public_receipt(previous)
            if self.jobs:raise ValueError('A workflow verification is already active.')
            if any(r.get('status') not in TERMINAL for r in self.store.runs()):
                raise ValueError('Pause active work before verifying AI workflows with disposable goals.')
            identifier=uuid4().hex
            record=self.store.save_entity('ai_workflow_tests',{'id':identifier,'version':1,'state':'running',
                'client_submission_id':client,'created_at':_now(),'checks':[],'runs':[],
                **self.identities(),
                'note':'Disposable fixtures test connections and goal flow; this is not a general model-quality benchmark.'})
            cancel=threading.Event();thread=threading.Thread(target=self._worker,args=(identifier,cancel),daemon=True,name='Forge-AI-workflow')
            self.jobs[identifier]={'cancel':cancel,'thread':thread};thread.start()
            return record

    def self_test_status(self,identifier):
        return self._public_receipt(self.store.entity('ai_workflow_tests',identifier))

    @staticmethod
    def _public_receipt(record):
        value=dict(record)
        if value.get('error'):value['failure_reason']=value.pop('error')
        value['stages']=value.get('checks',[])
        return value

    def cancel(self,identifier):
        record=self.self_test_status(identifier)
        with self.lock:
            job=self.jobs.get(identifier)
            if job:job['cancel'].set()
        for item in record.get('runs',[]):
            run=self.store.run(item['run_id'])
            if run['status'] not in TERMINAL:self.service.jobs.cancel(run['id'],pause=True)
        return self._public_receipt(self._save(identifier,state='cancelled',finished_at=_now()))

    def _wait(self,identifier,run_id,cancel,seconds=600):
        deadline=time.monotonic()+seconds
        while not cancel.is_set():
            run=self.store.run(run_id)
            if run['status'] in TERMINAL:return run
            state='awaiting_approval' if run['status'] in ('awaiting_approval','waiting_question') else 'running'
            current=self.self_test_status(identifier)
            if current.get('state')!=state:self._save(identifier,state=state)
            if time.monotonic()>=deadline:
                self.service.jobs.cancel(run_id,pause=True)
                raise ValueError('The bounded verification deadline elapsed. Saved evidence remains unverified; no automatic retry was made.')
            cancel.wait(.2)
        raise ValueError('Workflow verification was cancelled.')

    def _receipt(self,run):
        planner=run.get('planner_assignment') or {};review=run.get('review') or {}
        contexts=[];cursor=0
        for _ in range(20):
            events=self.store.events(run['id'],cursor,limit=500)
            contexts.extend(e for e in events if e.get('type')=='context')
            if not events or len(events)<500:break
            cursor=events[-1]['seq']
        supplied=any((e.get('cloud_guidance') or {}).get('guidance_hash')==planner.get('guidance_hash')
                     and (e.get('cloud_guidance') or {}).get('assignment_id')==planner.get('key')
                     and (e.get('cloud_guidance') or {}).get('child_run_id')==planner.get('run_id')
                     and planner.get('guidance_hash') for e in contexts)
        with self.store._connection() as db:
            local=[];command_results=[]
            for row in db.execute("SELECT id,name,status,result FROM invocations WHERE run_id=? AND status='completed'",(run['id'],)):
                wrapped=json.loads(row['result'] or '{}');result=wrapped.get('result') or {}
                if isinstance(result,dict) and (result.get('error') or result.get('not_executed') or result.get('outcome_unknown') or result.get('ok') is False):continue
                local.append({k:row[k] for k in ('id','name','status')})
                if row['name'] in ('run_command','command_wait','command_read'):
                    command_results.append((row['name'],result))
        effect=any(r['name'] in ('write_file','edit_file','patch_files','apply_patch','document_create') for r in local)
        checked=False
        try:fixture=self.store.entity('ai_workflow_fixtures',run['project_id'])
        except (ValueError,KeyError):fixture={}
        if fixture:
            root=Path(fixture['root']).resolve();checker=root/'check_fixture.py'
            for name,result in command_results:
                if name=='run_command':session=result
                else:
                    try:session=self.service.command_sessions.status(result['id'])
                    except (ValueError,KeyError):continue
                    if session.get('run_id')!=run['id'] or session.get('status')!='completed':continue
                if session.get('exit_code')!=0 or session.get('cancelled') or session.get('timed_out'):continue
                argv=session.get('argv') or []
                cwd=Path(session.get('cwd','.'))
                if not cwd.is_absolute():cwd=root/cwd
                if len(argv)!=2 or cwd.resolve()!=root:continue
                executable=Path(argv[0]).resolve()
                target=Path(argv[1]);target=target if target.is_absolute() else cwd/target
                if (executable==Path(fixture['python']).resolve() and target.resolve()==checker and checker.is_file()
                        and sha256(checker.read_bytes()).hexdigest()==fixture['checker_sha256']):checked=True
        planner_models=actual_models(self.store,planner['run_id']) if planner.get('run_id') else []
        review_models=actual_models(self.store,review['review_run_id']) if review.get('review_run_id') else []
        checks={'completed':run['status']=='completed','planner_response':planner.get('state')=='consumed',
                'guidance_supplied':bool(supplied),'local_implementation':effect,'local_checks':checked,
                'independent_review':review.get('status')=='complete' and bool(review.get('review_run_id')),
                'planner_model_reported':bool(planner_models),'review_model_reported':bool(review_models)}
        return {'run_id':run['id'],'goal_id':run.get('goal_id'),'chat_id':run['chat_id'],
                'planner_run_id':planner.get('run_id'),'planner_identity':planner.get('key'),
                'guidance_hash':planner.get('guidance_hash'),'review_run_id':review.get('review_run_id'),
                'planner_actual_models':planner_models,'review_actual_models':review_models,
                'checks':checks,'invocations':local,'passed':all(checks.values()),'review_verdict':review.get('status')}

    def _fixture(self,identifier,kind):
        folder=self.store.home/'projects'/'ai-workflow-verification'/identifier/kind
        folder.mkdir(parents=True,exist_ok=True)
        if not folder.resolve().is_relative_to((self.store.home/'projects').resolve()):raise ValueError('Verification fixture escaped the managed project directory.')
        expected='connection,status\nopenrouter,verified\n'
        python=_resolve_python()
        check=("from pathlib import Path\nimport csv\nrows=list(csv.reader(Path('proof.csv').read_text(encoding='utf-8').splitlines()))\n"
               "assert rows==[['connection','status'],['openrouter','verified']], repr(rows)\nprint('Exact workflow fixture passed')\n")
        (folder/'README.md').write_text('Disposable connection fixture. Create proof.csv with exactly two CSV rows: connection,status and openrouter,verified. Do not modify check_fixture.py.\n',encoding='utf-8')
        (folder/'check_fixture.py').write_text(check,encoding='utf-8',newline='\n')
        project=self.service.create_project({'path':str(folder),'name':'AI verification '+kind})
        self.store.save_entity('ai_workflow_fixtures',{'id':project['id'],'root':str(folder),'python':python,
            'checker_sha256':sha256((folder/'check_fixture.py').read_bytes()).hexdigest()})
        instruction=('Create proof.csv containing exactly the CSV rows connection,status and openrouter,verified. '
                     'Do not modify check_fixture.py. Inspect README.md, implement the file, run the supplied check, '
                     'using command_start or run_command with argv '+json.dumps([python,'check_fixture.py'])+' and cwd ".". '
                     'and complete the existing task identities with genuine evidence. Independent OpenRouter review must complete.')
        if kind=='ordinary':
            goal=self.service.goal_create({'title':'OpenRouter ordinary goal verification','project_id':project['id'],
                'text':instruction,'tasks':[{'id':'fixture-file','text':'Create the exact proof.csv rows and verify them with check_fixture.py.'}]})
            launch={'id':goal['id'],'cloud_required_review':True,'source_key':'ai-workflow:'+identifier+':'+kind}
        else:
            builder=self.service.get_builder().save({'project_id':project['id'],'directory':'.','title':'OpenRouter Builder verification',
                'objective':instruction,'deliverable':'documents','outputs':['proof.csv'],
                'check_commands':{'functional':[python,'check_fixture.py']},
                'requirements':[{'id':'fixture-rows','text':'Export the exact two CSV rows.','acceptance':'check_fixture.py exits successfully and proof.csv is structurally verified.'}]})
            chat=self.store.create_chat(project['id'],'AI verification Builder',self.store.get_settings().get('model',''))
            tasks=[{'id':'fixture-file','text':'Export and verify proof.csv using the brief checks.','requirement_id':'fixture-rows'},
                   {'id':'fixture-checks','text':'Record fresh functional session and artifact gate evidence.','builder_checks':True}]
            goal=self.service.goal_create({'title':builder['title'],'text':self.service.get_builder().brief_text(builder),
                'project_id':project['id'],'chat_id':chat['id'],'tasks':tasks})
            self.store.save_goal({**goal,'builder_id':builder['id'],'builder_revision':builder['revision']})
            self.store.save_entity('builders',{**builder,'chat_id':chat['id'],'goal_id':goal['id'],'status':'building'})
            launch={'id':goal['id'],'cloud_required_review':True,'source_key':'ai-workflow:'+identifier+':'+kind}
        return folder,project,goal,launch,sha256(check.encode()).hexdigest(),expected

    def _worker(self,identifier,cancel):
        checks=[];runs=[]
        try:
            for kind in ('ordinary','builder'):
                folder,project,goal,launch,script_hash,expected=self._fixture(identifier,kind)
                self._save(identifier,stage=kind,fixture={'project_id':project['id'],'goal_id':goal['id'],'path':str(folder)})
                result=self.service.goal_resume(launch)
                runs.append({'kind':kind,'run_id':result['id'],'goal_id':goal['id'],'project_id':project['id']})
                self._save(identifier,runs=runs)
                run=self._wait(identifier,result['id'],cancel)
                receipt=self._receipt(run)
                output=folder/'proof.csv'
                receipt['kind']=kind
                receipt['checks']['fixture_unchanged']=sha256((folder/'check_fixture.py').read_bytes()).hexdigest()==script_hash
                receipt['checks']['exact_output']=output.is_file() and output.read_text(encoding='utf-8').replace('\r\n','\n')==expected
                receipt['passed']=all(receipt['checks'].values())
                checks.append(receipt);self._save(identifier,checks=checks)
                if not receipt['passed']:raise ValueError(kind.title()+' goal flow did not verify every connection stage. Inspect saved planner, local checks and review evidence; no automatic retry was made.')
            # Exercise each remaining specialty separately using explicit text
            # context only; no project, attachments or private memory is assigned.
            for specialty in ('design','coding-advice','diagnosis','research','review'):
                if cancel.is_set():raise ValueError('Workflow verification was cancelled.')
                profile=next(p for p in self.store.entities('agents') if p.get('specialty')==specialty and p.get('provider_id')=='openrouter' and p.get('enabled',True))
                result=self.service.agent_start({'agent_id':profile['id'],'text':
                    'Connection verification only. Analyze this synthetic fixture: a local CSV export must contain headers connection,status and row openrouter,verified. Return one bounded '+specialty+' finding. Do not use tools or infer observations about real users.',
                    'cloud_scope':{'files':[],'artifacts':[],'attachments':[],'web':False},
                    'source_key':'ai-workflow:'+identifier+':specialty:'+specialty})
                runs.append({'kind':specialty,'run_id':result['id']});self._save(identifier,runs=runs,stage=specialty)
                run=self._wait(identifier,result['id'],cancel,seconds=120)
                cloud_policy(self.service).consent()
                with self.store._connection() as db:
                    row=db.execute("SELECT content FROM messages WHERE chat_id=? AND role='assistant' AND id>? ORDER BY id DESC LIMIT 1",(run['chat_id'],run['request_message_id'])).fetchone()
                receipt={'kind':specialty,'run_id':run['id'],'actual_models':actual_models(self.store,run['id']),
                         'passed':run['status']=='completed' and bool(row and row[0].strip()) and bool(actual_models(self.store,run['id'])),
                         'status':run['status']}
                checks.append(receipt);self._save(identifier,checks=checks)
                if not receipt['passed']:raise ValueError(specialty+' connection assignment did not complete; no automatic retry was made.')
            self._save(identifier,state='verified',finished_at=_now(),stage='complete',checks=checks)
        except Exception as exc:
            self._save(identifier,state='cancelled' if cancel.is_set() else 'failed',finished_at=_now(),error=str(exc)[:1000],checks=checks)
        finally:
            with self.lock:self.jobs.pop(identifier,None)

    def dispatch(self,action,data=None):
        data=data or {}
        if action=='ai_workflow_status':return self.status()
        if action=='ai_workflow_setup':return self.setup(data)
        if action=='ai_workflow_self_test':return self.self_test(data)
        if action=='ai_workflow_self_test_status':return self.self_test_status(data['id'])
        if action=='ai_workflow_self_test_cancel':return self.cancel(data['id'])
        raise ValueError('Unknown AI workflow operation.')

    def shutdown(self):
        for identifier in list(self.jobs):self.cancel(identifier)
