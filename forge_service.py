"""Authoritative coordinator shared by native, HUD and authenticated HTTP clients."""
from datetime import datetime,timedelta,timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from core import Core
from forge_inference import ProviderPool, memory_telemetry, ENGINE_PROFILES
from forge_runs import RunManager
from forge_store import ForgeStore, TERMINAL, atomic_text, encode, validate_goal_limits
from project_tools import ProjectTools
from storage import _now

COMMANDS=[dict(name=name,description=description) for name,description in (
 ('plan','Inspect and prepare a plan; Build starts implementation'),('todo','Create or open an ordered Markdown checklist'),
 ('goal','Work through a saved checklist'),('pause','Pause the active run'),('resume','Continue a saved run'),
 ('status','Show current progress'),('compact','Save a continuity checkpoint'),('new','Create a new chat'),
 ('project','Select or create a project'),('model','Select a model'),('agents','Configure or launch an agent'),
 ('worktree','Create or inspect an isolated Git workspace'),('skill','Select a skill'),('mcp','Manage tool connections'),
 ('schedule','Schedule a workflow'),('help','Show slash commands'))]

class ForgeService:
    def __init__(self,core=None,store=None,data_dir=None):
        self.core=core or Core(); self.store=store or ForgeStore(data_dir); self.computer_broker=None; self.host_dictation=None
        try:
            from forge_credentials import CredentialVault
            from forge_integrations import IntegrationHub
            self.vault=CredentialVault(); self.integrations=IntegrationHub(self.store.home,vault=self.vault)
        except ImportError:
            self.vault=None; self.integrations=None
        self.providers=ProviderPool(self.core,self.store,self.vault)
        self.jobs=RunManager(self); self.stop_event=threading.Event(); self.background=None; self.runtime=None
        self.worktree_lock=threading.RLock()
        self.schedule_lock=threading.RLock()

    def start_background(self):
        if self.background and self.background.is_alive(): return
        self.background=threading.Thread(target=self._scheduler,daemon=True,name='Forge-scheduler'); self.background.start()

    def shutdown(self):
        self.stop_event.set(); self.emergency_stop()
        with self.jobs.lock: threads=[j['thread'] for j in self.jobs.jobs.values()]
        for thread in threads: thread.join(timeout=2)
        if self.integrations: self.integrations.shutdown()
        if self.runtime and hasattr(self.runtime,'shutdown'): self.runtime.shutdown()
        if self.host_dictation and hasattr(self.host_dictation,'shutdown'): self.host_dictation.shutdown()
        if self.computer_broker and hasattr(self.computer_broker,'shutdown'): self.computer_broker.shutdown()
        if self.background: self.background.join(timeout=2)

    def emergency_stop(self):
        for run in self.store.runs():
            if run['status'] not in TERMINAL: self.jobs.cancel(run['id'],pause=True)
        if self.computer_broker and hasattr(self.computer_broker,'stop'): self.computer_broker.stop()
        return {'ok':True}

    def bootstrap(self):
        return dict(version='4.0.0',name='Forge',settings=self.store.get_settings(),projects=self.store.list_projects(),
                    chats=self.store.list_chats(),runs=self.store.runs(limit=200),goals=self.store.entities('goals'),
                    spaces=self.store.entities('spaces'),schedules=self.store.entities('schedules'),agents=self.store.entities('agents'),
                    providers=self.providers.configurations(),commands=COMMANDS,
                    capabilities=dict(durable_runs=True,goals=True,usage=True,spaces=True,worktrees=True,schedules=True,
                       mcp=bool(self.integrations),skills=bool(self.integrations),plugins=bool(self.integrations),
                       browser=bool(self.integrations),computer=os.name=='nt',dictation=os.name=='nt',managed_runtimes=True))

    def dispatch(self,action,data=None):
        data=data or {}
        if not isinstance(data,dict): raise ValueError('Expected an object.')
        if action=='bootstrap': return self.bootstrap()
        if action=='settings': return self.store.update_settings(data) if data else self.store.get_settings()
        if action=='permission_overrides': return {'overrides':self.store.get_settings().get('permission_overrides',{})}
        if action=='permission_override_save':
            if data.get('scope') not in ('tool','server','app','project') or data.get('profile') not in ('always_ask','full_access','deny_access'): raise ValueError('Invalid permission override.')
            overrides=self.store.get_settings().get('permission_overrides',{})
            overrides[data['scope']+':'+data['target']]=data['profile']; self.store.update_settings({'permission_overrides':overrides})
            return {'overrides':overrides}
        if action=='storage_info':
            return {'home':str(self.store.home),'database':str(self.store.db_path),'attachments':str(self.store.home/'attachments'),
                    'backups':str(self.store.home/'backups'),'projects':str(self.store.home/'projects'),'worktrees':str(self.store.home/'worktrees')}
        if action=='open_storage':
            if os.name!='nt': raise ValueError('Open the storage path in your file manager.')
            os.startfile(str(self.store.home)); return {'ok':True}
        if action=='commands': return {'commands':COMMANDS}
        if action=='command': return self.command(data)
        if action=='projects': return {'projects':self.store.list_projects()}
        if action=='create_project': return self.create_project(data)
        if action=='chats': return {'chats':self.store.list_chats(data.get('project_id'))}
        if action=='get_chat': return self.store.get_chat(data['id'],limit=data.get('limit',200))
        if action=='attachment':
            image=self.store.hydrate_images([data['id']])[0]
            import base64,io
            from PIL import Image
            with Image.open(io.BytesIO(base64.b64decode(image))) as picture: mime=picture.get_format_mimetype() or 'image/png'
            return {'image':image,'mime_type':mime}
        if action=='create_chat': return self.store.create_chat(data.get('project_id'),data.get('title','New chat'),data.get('model',''))
        if action=='rename_chat': self.store.rename_chat(data['id'],data['title']); return {'ok':True}
        if action=='tasks': return {'tasks':self.store.list_tasks(data['project_id'])}
        if action=='create_task': return self.store.create_task(data['project_id'],data['title'])
        if action=='update_task': return self.store.update_task(data['project_id'],data['task_id'],data['status'])
        if action=='start_chat': return self.jobs.start(data)
        if action=='runs': return {'runs':self.store.runs(limit=200)}
        if action=='poll': return self.jobs.poll(data['id'],data.get('after',0))
        if action in ('pause','cancel'): return self.jobs.cancel(data['id'],pause=action=='pause')
        if action=='resume': return self.jobs.resume(data['id'],data)
        if action=='approve': return self.jobs.approve(data.get('job_id') or data.get('run_id') or data['id'],data['approval_id'],data['allowed'])
        if action=='compact': return self.jobs.compact(data['id'])
        if action=='unknown_actions': return {'actions':self.store.unknown_actions(data['run_id'])}
        if action=='resolve_action': return self.resolve_action(data)
        if action=='goals': return {'goals':self.store.entities('goals')}
        if action=='goal_get':
            goal=self.store.goal(data['id']); return {'goal':goal,**goal}
        if action=='goal_create': return self.goal_create(data)
        if action=='goal_update': return self.store.save_goal({**self.store.goal(data['id']),**data})
        if action=='goal_reconcile': return self.goal_reconcile(data)
        if action=='goal_export': return self.goal_export(data)
        if action=='goal_resume': return self.goal_resume(data)
        if action=='goal_pause':
            runs=[r for r in self.store.runs() if r.get('goal_id')==data['id']]
            for run in runs:
                if run['status'] not in TERMINAL: self.jobs.cancel(run['id'],pause=True)
            return {'ok':True}
        if action in ('spaces','agents','schedules'): return {action:self.store.entities(action)}
        if action=='space_save': return self.store.save_entity('spaces',data)
        if action=='space_delete': self.store.delete_entity('spaces',data['id']); return {'ok':True}
        if action=='agent_save': return self.agent_save(data)
        if action=='agent_delete': self.store.delete_entity('agents',data['id']); return {'ok':True}
        if action=='agent_start': return self.agent_start(data)
        if action=='schedule_save': return self.schedule_save(data)
        if action=='schedule_delete': self.store.delete_entity('schedules',data['id']); return {'ok':True}
        if action=='schedule_run': return self.schedule_run(self.store.entity('schedules',data['id']))
        if action=='usage': return self.store.usage(data.get('period','all'),data.get('timezone',self.store.get_settings()['timezone']),data.get('model'),data.get('project_id'))
        if action=='providers': return {'providers':self.providers.configurations()}
        if action=='provider_save': return self.provider_save(data)
        if action=='provider_test': return {'models':self.providers.provider(data['id']).models(),'ok':True}
        if action=='models': return {'models':self.providers.provider(data.get('provider_id',self.store.get_settings()['provider_id'])).models()}
        if action in ('show','pull','delete'):
            provider=self.providers.provider(data.get('provider_id',self.store.get_settings()['provider_id']))
            if action=='show': return provider.capabilities(data.get('model') or data['name'])
            if not hasattr(provider,'core'): raise ValueError('Manage this endpoint model through its runtime. Ollama pull/delete are available on Ollama providers.')
            return provider.core.dispatch(action,{**data,'model':data.get('model') or data.get('name')})
        if action=='performance': return self.performance()
        if action.startswith('runtime_'):
            if self.runtime is None:
                from forge_runtime import RuntimeManager
                self.runtime=RuntimeManager(self.store.home)
            result=self.runtime.dispatch(action,data)
            if action=='runtime_start':
                runtime=self.runtime.configs[data['id']]
                provider_id='runtime-'+runtime['id']
                validation=runtime.get('validation') or {}
                validated=bool(validation.get('promoted') and validation.get('binding')==self.runtime._model_binding(data)
                    and validation.get('executable_sha256')==runtime.get('executable_sha256'))
                provider=self.provider_save(dict(id=provider_id,name='Managed '+runtime['engine'],kind='ollama' if runtime['engine']=='ollama' else 'openai-compatible',
                    url=result['url'],managed=True,runtime_id=runtime['id'],context_limit=data.get('context',32768),
                    capabilities=['tools','vision'] if validated else []))
                result={**result,'provider_id':provider_id,'provider':provider}
            return result
        if action=='worktrees': return {'worktrees':[w for w in self.store.entities('worktrees') if not data.get('project_id') or w['parent_project_id']==data['project_id']]}
        if action=='worktree_create': return self.worktree_create(data)
        if action=='worktree_diff': return self.worktree_diff(data)
        if action=='worktree_integrate': return self.worktree_integrate(data)
        if action in ('files','file_read','file_search','workspace_files','workspace_read'):
            project=self.store.get_project(data['project_id']); tool={'files':'list_files','file_read':'read_file','file_search':'search_files','workspace_files':'list_files','workspace_read':'read_file'}[action]
            args={k:v for k,v in data.items() if k!='project_id'}
            result=ProjectTools(project['path'],self.store.home/'backups').execute(tool,args)
            if action=='workspace_files':
                entries=result.get('entries',result.get('files',[]))
                return {**result,'entries':[dict(v,name=Path(v['path']).name,is_dir=v.get('type')=='directory') for v in entries]}
            return result
        if self.integrations and action.startswith(('mcp_','skill','plugin','catalog','browser_')) or action in ('integrations','tools'):
            if not self.integrations: raise ValueError('Install the integrations dependencies to use this feature.')
            if data.get('project_id'): data={**data,'project':self.store.get_project(data['project_id'])}
            return self.integrations.dispatch(action,data)
        if action=='emergency_stop': return self.emergency_stop()
        if action=='startup':
            from forge_host import set_startup
            result=set_startup(data['enabled']); self.store.update_settings({'startup':result['enabled']}); return result
        if action in ('pull','delete') and 'name' in data and 'model' not in data: data={**data,'model':data['name']}
        return self.core.dispatch(action,data)

    def create_project(self,data):
        name=data.get('name','New project'); path=data.get('path')
        if not path:
            folder=self.store.home/'projects'/uuid4().hex; folder.mkdir()
            if data.get('git',True): self._git(folder,['init','-b','main'])
            path=str(folder)
        folder=Path(path).expanduser().resolve(strict=True)
        configured=os.getenv('FORGE_PROJECTS_ROOT') or os.getenv('SIDEKICK_PROJECTS_ROOT')
        if configured and not folder.is_relative_to(Path(configured).resolve()): raise ValueError('Project must be within the coordinator workspace mount.')
        existing=next((p for p in self.store.list_projects() if Path(p['path'])==folder),None)
        return existing or self.store.create_project(name,str(folder))

    def goal_create(self,data):
        text=data.get('text') or data.get('title') or 'New goal'
        tasks=data.get('tasks')
        if not tasks:
            lines=[re.sub(r'^\s*(?:[-*]|\d+[.)])\s*(?:\[[ xX]\]\s*)?','',line).strip() for line in text.splitlines() if line.strip()]
            tasks=[dict(text=line,status='pending',evidence=[]) for line in lines]
        return self.store.save_goal(dict(title=data.get('title',text.splitlines()[0][:120]),request=text,project_id=data.get('project_id'),chat_id=data.get('chat_id'),
                                         tasks=tasks,status='ready',checkpoint='Checklist created. Execution has not started.',next_action='Review tasks, then start the goal.'))
    def goal_resume(self,data):
        goal=self.store.goal(data['id'])
        if goal['external_edits']: raise ValueError('Reconcile the edited checklist first.')
        previous=next((r for r in self.store.runs() if r.get('goal_id')==goal['id']),None)
        if previous and previous['status'] in ('paused','interrupted','failed'): return self.jobs.resume(previous['id'],data)
        if previous and previous['status'] not in TERMINAL: return {'id':previous['id'],'chat_id':previous['chat_id'],'status':previous['status']}
        result=self.jobs.start({**data,'text':goal['request'],'project_id':goal.get('project_id'),'chat_id':goal.get('chat_id'),'goal_id':goal['id'],'mode':'goal'})
        self.store.save_goal({**goal,'status':'running','chat_id':result['chat_id']}); return result
    def goal_reconcile(self,data):
        goal=self.store.goal(data['id'])
        if data.get('mode')=='import':
            tasks=[]
            for line in goal['markdown'].splitlines():
                match=re.match(r'^\s*(?:\d+[.)]|[-*])\s*\[([ xX])\]\s*(.+)',line)
                if match: tasks.append(dict(text=match[2],status='completed' if match[1].lower()=='x' else 'pending',evidence=[]))
            if not tasks: raise ValueError('No Markdown checklist tasks were found.')
            goal['tasks']=tasks
        elif data.get('mode')!='overwrite': raise ValueError('Choose import or overwrite.')
        return self.store.save_goal(goal,reconcile=True)
    def goal_export(self,data):
        goal=self.store.goal(data['id']); project=self.store.get_project(data.get('project_id') or goal['project_id'])
        return ProjectTools(project['path'],self.store.home/'backups').execute('write_file',{'path':data.get('path','FORGE_TODO.md'),'content':goal['markdown']})

    def command(self,data):
        text=data.get('text','').strip(); head,_,argument=text.partition(' '); name=head.lstrip('/').lower()
        if name not in {c['name'] for c in COMMANDS}: raise ValueError('Unknown command. Use /help.')
        arguments={**data,'text':argument.strip()}
        if name=='help': return {'commands':COMMANDS,'message':'Choose a slash command in the composer.'}
        if name=='new': return {'chat':self.store.create_chat(data.get('project_id'),model=data.get('model','')),'navigate':'chat'}
        if name=='todo':
            if not argument: return {'navigate':'goals','goals':self.store.entities('goals')}
            goal=self.goal_create(arguments); return {'goal':goal,'navigate':'goals','message':'Checklist saved. Execution has not started.'}
        if name=='goal':
            if not argument: return {'navigate':'goals'}
            goal=self.goal_create(arguments); return self.goal_resume({**data,'id':goal['id']})
        if name=='plan':
            if not argument: return {'message':'Use /plan followed by the task.'}
            return self.jobs.start({**arguments,'mode':'plan'})
        current=next((r for r in self.store.runs() if r['chat_id']==data.get('chat_id') and (r['status'] not in TERMINAL if name in ('pause','status') else True)),None)
        if name=='status': return {'run':current,'message':current.get('checkpoint','No active run.') if current else 'No active run.'}
        if name in ('pause','resume','compact'):
            if not current: raise ValueError('Select a chat with a saved run.')
            if name=='pause': return self.jobs.cancel(current['id'],pause=True)
            if name=='resume': return self.jobs.resume(current['id'],data)
            return self.jobs.compact(current['id'])
        navigation={'project':'projects','model':'models','agents':'agents','worktree':'worktrees','skill':'plugins','mcp':'mcp','schedule':'scheduled'}
        if name=='skill' and argument:
            selected=self.store.get_settings().get('skills',[])
            self.store.update_settings({'skills':list(dict.fromkeys(selected+[argument.strip()]))})
        return {'navigate':navigation[name],'query':argument}

    def agent_save(self,data):
        previous=self.store.entity('agents',data['id']) if data.get('id') else {}
        profile={**previous,**data}; profile.setdefault('name','Agent'); profile.setdefault('role','researcher')
        profile.setdefault('enabled',True); profile.setdefault('context',32768); profile.setdefault('instructions','')
        from context_window import validate_context
        validate_context(profile['context'])
        if profile['role'] not in ('researcher','coder','reviewer'): raise ValueError('Unknown agent role.')
        if 'goal_limits' in profile: profile['goal_limits']=validate_goal_limits(profile['goal_limits'])
        for key in ('rounds','tokens'):
            if key in profile and (type(profile[key]) is not int or not 1<=profile[key]<=1_000_000_000): raise ValueError('Agent '+key+' limit must be a positive integer.')
        return self.store.save_entity('agents',profile)
    def agent_start(self,data):
        profile=self.store.entity('agents',data['agent_id'])
        if not profile.get('enabled',True): raise ValueError('Enable this agent profile first.')
        parent=self.store.run(data['parent_id']) if data.get('parent_id') else None
        settings=parent['settings'] if parent else self.store.get_settings()
        if not parent and profile.get('goal_limits'): settings={**settings,'goal_limits':profile['goal_limits']}
        project_id=data.get('project_id') or (parent or {}).get('project_id')
        readonly=profile.get('role') in ('researcher','reviewer')
        worktree=None
        if project_id and not readonly:
            project=self.store.get_project(project_id)
            if self._git(Path(project['path']),['rev-parse','--is-inside-work-tree'],check=False).returncode==0:
                worktree=self.worktree_create({'project_id':project_id}); project_id=worktree['project']['id']
            elif parent and parent.get('project_id')==project_id:
                raise ValueError('Writing delegation requires a Git worktree. Initialize and commit this project, or run the agent after the main writer pauses.')
        result=self.jobs.start({**settings,'context':profile.get('context',settings['context']),
            'model':profile.get('model') or settings['model'],'text':data.get('text') or data.get('prompt') or 'Inspect the project.',
            'project_id':project_id,'parent_id':data.get('parent_id'),'readonly':readonly,
            'instructions':profile.get('instructions',''),'agent_id':profile['id'],'agent_tools':profile.get('tools',[]),'skills':profile.get('skills',[]),
            'worktree_id':worktree['worktree']['id'] if worktree else None,
            'goal_id':parent.get('goal_id') if parent else None})
        self.store.update_run(result['id'],instructions=profile.get('instructions',''),agent_id=profile['id'],
            agent_tools=profile.get('tools',[]),skills=profile.get('skills',[]),worktree_id=worktree['worktree']['id'] if worktree else None)
        return {**result,'worktree':worktree}

    def provider_save(self,data):
        data={**data,'url':data.get('url') or data.get('base_url',''),'kind':data.get('kind') or data.get('type','openai-compatible')}
        url=data.get('url',''); parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Enter a provider HTTP(S) endpoint without embedded credentials, query parameters or fragments. Use Credential Manager for authentication.')
        allowed={'id','name','kind','url','base_url','type','credential_ref','capabilities','context_limit','managed','runtime_id','enabled','updated_at'}
        profile={k:v for k,v in data.items() if k in allowed}
        if data.get('token') or data.get('api_key'):
            if not self.vault: raise ValueError('OS credential storage is unavailable.')
            profile['credential_ref']=self.vault.put(data.get('token') or data['api_key'])
        profile.setdefault('kind','openai-compatible'); profile.setdefault('name','Local endpoint')
        profile['base_url']=profile['url']; profile['type']=profile['kind']
        return self.store.save_entity('providers',profile)
    def performance(self):
        telemetry=memory_telemetry(); notes=['Explicit model and context selections are never changed automatically.',
            'Flash Attention and cache flags affect Forge-managed processes only. Validate images and tools before promotion.']
        return {'telemetry':telemetry,'profiles':ENGINE_PROFILES,'recommendations':[
            {'id':'balanced','label':'Balanced','context':32768,'requires_acceptance':True,
             'description':'Choose a tool/vision model that leaves GPU memory for KV cache and image processing.'}], 'notes':notes}

    @staticmethod
    def _git(folder,args,check=True):
        environment=os.environ.copy(); environment.update(GIT_AUTHOR_NAME='Forge contributors',GIT_AUTHOR_EMAIL='contributors@forge.invalid',
            GIT_COMMITTER_NAME='Forge contributors',GIT_COMMITTER_EMAIL='contributors@forge.invalid')
        result=subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-c','commit.gpgsign=false',*args],cwd=folder,
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60,env=environment,
            creationflags=0x08000000 if os.name=='nt' else 0)
        if check and result.returncode: raise ValueError(result.stderr.strip()[:2000] or 'Git operation failed.')
        return result
    def worktree_create(self,data):
        with self.worktree_lock:
            parent=self.store.get_project(data['project_id']); root=Path(parent['path'])
            if self._git(root,['rev-parse','HEAD'],check=False).returncode: raise ValueError('Commit the initial project state before creating a worktree.')
            identifier=uuid4().hex; path=self.store.home/'worktrees'/identifier; branch='forge/'+identifier[:12]
            ref=data.get('ref','HEAD')
            if not isinstance(ref,str) or ref.startswith('-') or len(ref)>300: raise ValueError('Invalid Git reference.')
            self._git(root,['worktree','add','-b',branch,str(path),ref])
            project=self.store.create_project(data.get('name',parent['name']+' · agent'),str(path))
            base=self._git(root,['rev-parse',ref]).stdout.strip()
            worktree=self.store.save_entity('worktrees',dict(id=identifier,parent_project_id=parent['id'],project_id=project['id'],path=str(path),branch=branch,base=base,status='active'))
            return {'project':project,'worktree':worktree}
    def worktree_diff(self,data):
        worktree=self.store.entity('worktrees',data['id']); path=Path(worktree['path'])
        return {'diff':self._git(path,['diff',worktree['base']]).stdout[:200000],
                'status':self._git(path,['status','--short']).stdout,'worktree':worktree}
    def worktree_integrate(self,data):
        with self.worktree_lock:
            worktree=self.store.entity('worktrees',data['id']); parent=self.store.get_project(worktree['parent_project_id']); root=Path(parent['path']); child=Path(worktree['path'])
            if self._git(root,['status','--porcelain']).stdout.strip(): raise ValueError('Parent project has uncommitted changes. Preserve or commit them before integration.')
            if self._git(child,['status','--porcelain']).stdout.strip():
                self._git(child,['add','-A']); self._git(child,['commit','-m','Forge agent changes'])
            result=self._git(root,['merge','--no-ff','--no-edit',worktree['branch']],check=False)
            if result.returncode:
                conflicts=self._git(root,['diff','--name-only','--diff-filter=U']).stdout.splitlines()
                self.store.save_entity('worktrees',{**worktree,'status':'conflict','conflicts':conflicts})
                return {'ok':False,'conflicts':conflicts,'error':result.stderr or result.stdout}
            self.store.save_entity('worktrees',{**worktree,'status':'integrated'})
            return {'ok':True,'message':'Changes integrated. Worktree retained for review and rollback.'}
    def resolve_action(self,data):
        evidence=str(data.get('evidence','')).strip()
        if not evidence: raise ValueError('Record inspection evidence before resolving an unknown outcome.')
        if data.get('outcome') not in ('completed','not_executed'): raise ValueError('Choose an inspected outcome.')
        with self.store._connection() as db:
            row=db.execute("SELECT * FROM invocations WHERE id=? AND status='outcome_unknown'",(data['invocation_id'],)).fetchone()
        if not row: raise ValueError('Unknown action not found.')
        self.store.invocation_state(data['invocation_id'],'completed',{'inspection':evidence,'outcome':data['outcome'],'replay':False})
        return {'ok':True}

    @staticmethod
    def web_fetch(url):
        parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password: raise ValueError('Enter a public HTTP(S) URL.')
        # Research cannot be used to probe private services or cloud metadata.
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80))
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses): raise ValueError('Research URLs must resolve to public addresses.')
        with httpx.Client(timeout=15,follow_redirects=False) as client:
            with client.stream('GET',url,headers={'User-Agent':'Forge/4.0'}) as response:
                response.raise_for_status()
                if response.is_redirect: raise ValueError('Open the final public URL after the redirect.')
                content=bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content)>2_000_000: raise ValueError('Page exceeds the research limit.')
                text=content.decode(response.encoding or 'utf-8',errors='replace')
        text=re.sub(r'<(script|style)\b[^>]*>.*?</\1>','',text,flags=re.S|re.I)
        text=re.sub(r'<[^>]+>',' ',text); text=re.sub(r'\s+',' ',text)
        return {'url':url,'text':text,'untrusted':True}

    def schedule_save(self,data):
        previous=self.store.entity('schedules',data['id']) if data.get('id') else {}
        schedule={**previous,**data}; schedule.setdefault('name','Scheduled task'); schedule.setdefault('enabled',True)
        schedule.setdefault('timezone',self.store.get_settings()['timezone']); ZoneInfo(schedule['timezone'])
        schedule.setdefault('recurrence','daily'); schedule.setdefault('time','09:00'); schedule.setdefault('permission_profile','always_ask')
        if schedule['recurrence'] not in ('once','hourly','daily','weekly'): raise ValueError('Choose once, hourly, daily or weekly.')
        datetime.strptime(schedule['time'],'%H:%M')
        if not schedule.get('prompt'): raise ValueError('Enter a schedule prompt.')
        changed=bool(previous) and any(key in data and data[key]!=previous.get(key) for key in ('recurrence','time','weekday','timezone'))
        if changed or 'next_run' not in schedule: schedule['next_run']=self.next_occurrence(schedule).isoformat()
        return self.store.save_entity('schedules',schedule)
    @staticmethod
    def next_occurrence(schedule,now=None):
        current=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo(schedule['timezone']))
        hour,minute=map(int,schedule['time'].split(':'))
        if schedule['recurrence']=='hourly':
            # Step through UTC minutes so both folds of a repeated hour remain
            # chronological and nonexistent local times are skipped.
            candidate=current.astimezone(timezone.utc).replace(second=0,microsecond=0)+timedelta(minutes=1)
            for _ in range(181):
                if candidate.astimezone(ZoneInfo(schedule['timezone'])).minute==minute: return candidate
                candidate+=timedelta(minutes=1)
            raise ValueError('Could not resolve the next hourly occurrence.')
        candidate=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if schedule['recurrence']=='weekly':
            weekday=int(schedule.get('weekday',current.weekday())); candidate+=timedelta(days=(weekday-candidate.weekday())%7)
            if candidate<=current: candidate+=timedelta(days=7)
        elif candidate<=current: candidate+=timedelta(days=1)
        # UTC roundtrip normalizes nonexistent spring-forward local times.
        return candidate.astimezone(timezone.utc)
    def schedule_run(self,schedule):
        with self.schedule_lock: return self._schedule_run(schedule)
    def _schedule_run(self,schedule):
        existing=next((r for r in self.store.runs() if r.get('schedule_id')==schedule['id'] and r['status'] not in TERMINAL),None)
        if existing: return {'id':existing['id'],'chat_id':existing['chat_id'],'queued':False,'message':'This occurrence is already active.'}
        settings=self.store.get_settings(); profile=schedule.get('permission_profile','always_ask')
        # Schedules can tighten policy; they cannot override a global deny.
        if settings['permission_profile']=='deny_access': profile='deny_access'
        elif settings['permission_profile']=='always_ask' and profile=='full_access': profile='always_ask'
        data={**settings,'text':schedule['prompt'],'project_id':schedule.get('project_id'),'model':schedule.get('model') or settings['model'],'permission_profile':profile,'schedule_id':schedule['id']}
        if schedule.get('agent_id'): result=self.agent_start({**data,'agent_id':schedule['agent_id']})
        else: result=self.jobs.start(data)
        self.store.update_run(result['id'],schedule_id=schedule['id']); return result
    def tick_schedules(self,now=None):
        current=now or datetime.now(timezone.utc)
        for schedule in self.store.entities('schedules'):
            if not schedule.get('enabled'): continue
            due=datetime.fromisoformat(schedule['next_run'])
            if due>current: continue
            slot=due.isoformat()
            with self.store._connection(transaction='write') as db:
                if db.execute('SELECT 1 FROM occurrences WHERE schedule_id=? AND slot=?',(schedule['id'],slot)).fetchone(): continue
                db.execute('INSERT INTO occurrences VALUES(?,?,NULL)',(schedule['id'],slot))
            try:
                result=self.schedule_run(schedule)
                with self.store._connection(transaction='write') as db: db.execute('UPDATE occurrences SET run_id=? WHERE schedule_id=? AND slot=?',(result['id'],schedule['id'],slot))
                schedule.update(last_run=current.isoformat(),last_run_id=result['id'],last_error=None)
            except Exception as exc: schedule['last_error']=str(exc)[:1000]
            if schedule['recurrence']=='once': schedule['enabled']=False
            else: schedule['next_run']=self.next_occurrence(schedule,current).isoformat()
            self.store.save_entity('schedules',schedule)
    def _scheduler(self):
        while not self.stop_event.is_set():
            try: self.tick_schedules()
            except Exception: pass
            self.stop_event.wait(5)
