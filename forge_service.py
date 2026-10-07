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
import time
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
 ('plan','Inspect and prepare a plan; Build starts implementation'),('todo','Run the saved plan as a durable Markdown checklist'),
 ('goal','Work through a saved checklist'),('pause','Pause the active run'),('resume','Continue a saved run'),
 ('status','Show current progress'),('compact','Save a continuity checkpoint'),('new','Create a new chat'),
 ('project','Select or create a project'),('model','Select a model'),('agents','Configure or launch an agent'),
 ('worktree','Create or inspect an isolated Git workspace'),('skill','Select a skill'),('mcp','Manage tool connections'),
 ('schedule','Schedule a workflow'),('help','Show slash commands'))]

# Reads stay responsive while a consistent update snapshot drains mutations.
READ_ACTIONS=frozenset(('bootstrap','commands','projects','chats','get_chat','attachment','tasks',
    'runs','poll','plans','plan_get','unknown_actions','goals','goal_get','spaces','agents','schedules',
    'usage','providers','models','show','permission_overrides','storage_info','setup_status',
    'openrouter_status','github_status','github_repos','channel_list','channel_notifications',
    'memory_list','memory_export','update_status','browser_status','browser_native_status',
    'dictation_status','runtime_status','hf_status','performance_status','pending_questions',
    'skills','skill_read','skill_preview','plugins','catalogs','catalog_discover','integrations','tools'))

class AgentLaunchRejected(ValueError):
    """Preflight failed before any worktree, child or remote request was created."""
    not_executed=True

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
        self.performance_manager=None; self.model_manager=None; self.plan_lock=threading.RLock(); self.manager_lock=threading.RLock()
        self.worktree_lock=threading.RLock()
        self.agent_lock=threading.RLock()
        self.schedule_lock=threading.RLock()
        self.admission_lock=threading.RLock()
        self.memory_manager=None; self.channel_manager=None; self.setup_manager=None; self.update_manager=None; self.openrouter_manager=None; self.github_manager=None
        self.interaction_manager=None

    def get_interaction(self):
        with self.manager_lock:
            if self.interaction_manager is None:
                from forge_interaction import RunInteraction
                self.interaction_manager=RunInteraction(self)
            return self.interaction_manager

    def get_memory(self):
        with self.manager_lock:
            if self.memory_manager is None:
                from forge_memory import ForgeMemory
                settings=self.store.get_settings()
                self.memory_manager=ForgeMemory(self.store,semantic_enabled=settings.get('memory_semantic',False),model_path=settings.get('memory_model_path') or None)
            return self.memory_manager

    def get_channels(self):
        with self.manager_lock:
            if self.channel_manager is None:
                from forge_channels import ChannelManager
                self.channel_manager=ChannelManager(self,self.vault)
                self.store.subscribe_events(self.channel_manager.on_run_event)
            return self.channel_manager

    def get_github(self):
        with self.manager_lock:
            if self.github_manager is None:
                from forge_github import GitHubConnection
                self.github_manager=GitHubConnection(self)
            return self.github_manager

    def start_background(self):
        if self.background and self.background.is_alive(): return
        self.get_channels().start()
        self.background=threading.Thread(target=self._scheduler,daemon=True,name='Forge-scheduler'); self.background.start()

    def shutdown(self):
        self.stop_event.set(); self.emergency_stop()
        if self.channel_manager: self.channel_manager.shutdown()
        if self.setup_manager: self.setup_manager.shutdown()
        if self.update_manager and hasattr(self.update_manager,'shutdown'): self.update_manager.shutdown()
        with self.jobs.lock: threads=[j['thread'] for j in self.jobs.jobs.values()]
        for thread in threads: thread.join(timeout=2)
        if self.integrations: self.integrations.shutdown()
        if self.runtime and hasattr(self.runtime,'shutdown'): self.runtime.shutdown()
        if self.performance_manager: self.performance_manager.shutdown()
        if self.model_manager: self.model_manager.shutdown()
        if self.host_dictation and hasattr(self.host_dictation,'shutdown'): self.host_dictation.shutdown()
        if self.computer_broker and hasattr(self.computer_broker,'shutdown'): self.computer_broker.shutdown()
        if self.background: self.background.join(timeout=2)

    def emergency_stop(self):
        for run in self.store.runs():
            if run['status'] not in TERMINAL: self.jobs.cancel(run['id'],pause=True)
        if self.computer_broker and hasattr(self.computer_broker,'stop'): self.computer_broker.stop()
        if self.integrations and hasattr(self.integrations,'stop'): self.integrations.stop()
        if self.performance_manager:
            with self.performance_manager.lock:
                for job in self.performance_manager.jobs.values(): job['cancel'].set()
        return {'ok':True}

    def bootstrap(self):
        return dict(version='4.2.3',name='Forge',settings=self.store.get_settings(),projects=self.store.list_projects(),
                    chats=self.store.list_chats(),runs=self.store.runs(limit=200),goals=self.store.active_goals(),
                    spaces=self.store.entities('spaces'),schedules=self.store.entities('schedules'),agents=self.store.entities('agents'),
                    providers=self.providers.configurations(),commands=COMMANDS,
                    capabilities=dict(durable_runs=True,goals=True,usage=True,spaces=True,worktrees=True,schedules=True,
                       mcp=bool(self.integrations),skills=bool(self.integrations),plugins=bool(self.integrations),
                       browser=bool(self.integrations),computer=os.name=='nt',dictation=os.name=='nt',managed_runtimes=True,
                       setup=True,memory=True,channels=True,updates=True))

    def dispatch(self,action,data=None):
        data={} if data is None else data
        if not isinstance(data,dict): raise ValueError('Expected an object.')
        if action in READ_ACTIONS or action in ('pause','cancel','goal_pause','approve') or action=='settings' and not data:
            return self._dispatch(action,data)
        # The updater acquires this same gate before its snapshot; a request
        # which began earlier either commits first or sees the prepared state.
        with self.admission_lock:
            if self.update_manager and self.update_manager.applying and action not in ('quit','emergency_stop'):
                raise ValueError('Forge is preparing an update. Changes are paused until restart.')
            return self._dispatch(action,data)

    def _dispatch(self,action,data):
        if action=='bootstrap': return self.bootstrap()
        if action=='settings':
            result=self.store.update_settings(data) if data else self.store.get_settings()
            if data and any(k in data for k in ('memory_semantic','memory_model_path')):
                with self.manager_lock: self.memory_manager=None
            return result
        if action.startswith('setup_'):
            with self.manager_lock:
                if self.setup_manager is None:
                    from forge_setup import SetupManager
                    self.setup_manager=SetupManager(self)
            return self.setup_manager.dispatch(action,data)
        if action=='openrouter_setup_agents': return self.openrouter_setup_agents()
        if action.startswith('openrouter_'):
            with self.manager_lock:
                if self.openrouter_manager is None:
                    from forge_openrouter import OpenRouterConnection
                    self.openrouter_manager=OpenRouterConnection(self)
            return self.openrouter_manager.dispatch(action,data)
        if action.startswith('github_'):
            return self.get_github().dispatch(action,data)
        if action.startswith(('update_','diagnostics_')):
            with self.manager_lock:
                if self.update_manager is None:
                    from forge_updates import UpdateManager
                    self.update_manager=UpdateManager(self,current_version='4.2.3')
            return self.update_manager.dispatch(action,data)
        if action.startswith(('channel_','notification_')):
            return self.get_channels().dispatch(action,data)
        if action.startswith('memory_') or action in ('skill_suggest','skill_promote'):
            return self.get_memory().dispatch(action,data,human=True,project_id=data.get('project_id'),agent_id=data.get('agent_id'))
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
        if action=='chats': return {'chats':self.store.list_chats(data.get('project_id'),archived=None if data.get('archived')=='all' else data.get('archived',False))}
        if action in ('chat_move','chat_archive','chat_delete'):
            from forge_chats import dispatch_chat
            return dispatch_chat(self,action,data)
        if action=='project_delete':
            from forge_chats import dispatch_chat
            return dispatch_chat(self,action,data)
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
        if action=='run_steer': return self.jobs.steer(data['run_id'],data.get('text'),data.get('client_id'))
        if action=='pending_questions': return {'questions':self.get_interaction().pending(data.get('chat_id'),data.get('run_id'))}
        if action=='answer_question': return self.get_interaction().answer(data['question_id'],data.get('answers'))
        if action=='runs': return {'runs':self.store.runs(limit=200)}
        if action=='poll': return self.jobs.poll(data['id'],data.get('after',0))
        if action in ('pause','cancel'): return self.jobs.cancel(data['id'],pause=action=='pause')
        if action=='resume': return self.jobs.resume(data['id'],data)
        if action=='approve': return self.jobs.approve(data.get('job_id') or data.get('run_id') or data['id'],data['approval_id'],data['allowed'])
        if action=='compact': return self.jobs.compact(data['id'])
        if action=='plans': return {'plans':self.store.entities('plans')}
        if action=='plan_get': return self.store.entity('plans',data['id'])
        if action=='plan_build': return self.plan_build(data)
        if action=='unknown_actions': return {'actions':self.store.unknown_actions(data['run_id'])}
        if action=='resolve_action': return self.resolve_action(data)
        if action=='goals': return {'goals':self.store.active_goals() if data.get('active_only') or data.get('active') else self.store.entities('goals')}
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
        if action.startswith('performance'):
            with self.manager_lock:
                if self.performance_manager is None:
                    from forge_performance import PerformanceManager
                    self.performance_manager=PerformanceManager(self)
            return self.performance_manager.dispatch(action,data)
        if action.startswith('hf_'):
            with self.manager_lock:
                if self.model_manager is None:
                    from forge_models import ModelManager
                    self.model_manager=ModelManager(self.store,self.providers,self.vault)
            return self.model_manager.dispatch(action,data)
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
            if tool=='list_files' and not args.get('path'): args['path']='.'
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

    def channel_start(self,data,channel_id,external_id):
        """The paired gateway binds identity and scope; commit its request once."""
        source_key='channel:'+channel_id+':'+external_id
        with self.jobs.lock:
            with self.store._connection() as db:
                row=db.execute('SELECT run_id FROM request_keys WHERE source_key=?',(source_key,)).fetchone()
            if row:
                run=self.store.run(row[0]); return {'id':run['id'],'chat_id':run['chat_id'],'status':run['status']}
            current=self.store.get_settings()
            from forge_channels import permission_ceiling
            data={**data,'source_key':source_key,
                'permission_profile':permission_ceiling(data.get('permission_profile','always_ask'),current['permission_profile']),
                'permission_ceiling':permission_ceiling(data.get('permission_profile','always_ask'),current['permission_profile'])}
            if data.get('agent_profile_id'):
                profile=self.store.entity('agents',data['agent_profile_id'])
                if not profile.get('enabled'): raise ValueError('Enable the configured agent first.')
                data.update(agent_id=profile['id'],instructions=profile.get('instructions',''),
                    agent_tools=profile.get('tools',[]),skills=profile.get('skills',[]),
                    context=profile.get('context',current['context']),model=profile.get('model') or data.get('model') or current['model'],
                    readonly=profile.get('role') in ('researcher','reviewer'))
                if profile.get('provider_id'): data['provider_id']=profile['provider_id']
                if data.get('project_id') and not data['readonly']:
                    from hashlib import sha256
                    key=sha256((channel_id+':'+data['chat_id']).encode()).hexdigest()
                    with self.worktree_lock:
                        workspace=next((w for w in self.store.entities('channel_workspaces') if w['id']==key),None)
                        if workspace:
                            self.store.get_project(workspace['workspace_project_id'])
                            data.update(workspace_project_id=workspace['workspace_project_id'],worktree_id=workspace['worktree_id'])
                        else:
                            project=self.store.get_project(data['project_id'])
                            if self._git(Path(project['path']),['rev-parse','--is-inside-work-tree'],check=False).returncode==0:
                                worktree=self.worktree_create({'project_id':project['id']})
                                workspace=self.store.save_entity('channel_workspaces',dict(id=key,channel_id=channel_id,
                                    chat_id=data['chat_id'],workspace_project_id=worktree['project']['id'],worktree_id=worktree['worktree']['id']))
                                data.update(workspace_project_id=workspace['workspace_project_id'],worktree_id=workspace['worktree_id'])
                            # Non-Git projects retain the coordinator's writer lock.
            return self.jobs.start(data,channel=(channel_id,external_id))

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

    def save_plan(self,run,markdown):
        tasks=[]
        for line in markdown.splitlines():
            match=re.match(r'^\s*(?:\d+[.)]|[-*]\s+\[[ xX]\])\s*(?:\[[ xX]\]\s*)?(.+)',line)
            if match: tasks.append({'text':match[1].strip(),'status':'pending','evidence':[]})
        # The complete model plan is preserved even if it did not use checklist syntax.
        if not tasks: tasks=[{'text':run['request'],'status':'pending','evidence':[]}]
        path=self.store.home/'state'/'plans'/run['id']/'PLAN.md'
        atomic_text(path,markdown)
        plan=self.store.save_entity('plans',{'id':run['id'],'run_id':run['id'],'chat_id':run['chat_id'],
            'project_id':run.get('project_id'),'request':run['request'],'status':'ready','markdown':markdown,
            'tasks':tasks,'path':str(path)})
        self.store.event(run['id'],'plan',plan_id=plan['id'],text='Plan saved. Review it, then select Build.')
        return plan

    def plan_build(self,data):
        with self.plan_lock, self.jobs.lock:
            identifier=data.get('run_id') or data.get('id')
            try: plan=self.store.entity('plans',identifier)
            except ValueError:
                older=self.store.run(identifier)
                if older.get('mode')!='plan' or older['status']!='completed': raise ValueError('Finish the plan before running /todo or selecting Build.') from None
                # Completed plans from the previous installation have messages
                # but no plan entity. Recover only the final visible plan from
                # that run's message interval, preserving later chat turns.
                next_request=min([r.get('request_message_id',2**63-1) for r in self.store.runs()
                    if r['chat_id']==older['chat_id'] and r.get('request_message_id',0)>older.get('request_message_id',0)] or [2**63-1])
                with self.store._connection() as db:
                    rows=db.execute("SELECT content,metadata FROM messages WHERE chat_id=? AND role='assistant' AND id>? AND id<? ORDER BY id DESC",
                        (older['chat_id'],older.get('request_message_id',0),next_request)).fetchall()
                answer=next((row['content'] for row in rows if row['content'].strip() and not json.loads(row['metadata']).get('tool_calls') and json.loads(row['metadata']).get('status','complete')=='complete'),None)
                if not answer: raise ValueError('No visible saved plan was found. Resume planning or create a new /plan.')
                plan=self.save_plan(older,answer)
            if plan.get('project_missing'): raise ValueError('Reconnect the original project or create a new plan before building.')
            if not plan.get('chat_id') or not plan.get('run_id'): raise ValueError('The original chat was deleted. Open the saved plan and start a new goal in its project.')
            if self.store.get_chat(plan['chat_id'],limit=1).get('project_id')!=plan.get('project_id'): raise ValueError('This chat moved to another project. Create a new plan in the selected project.')
            run=self.store.run(plan['run_id'])
            if plan.get('goal_id'):
                existing=next((r for r in self.store.runs() if r.get('goal_id')==plan['goal_id'] and
                    not r.get('parent_id') and r['chat_id']==plan['chat_id']),None)
                if existing: return {'id':existing['id'],'chat_id':existing['chat_id'],'status':existing['status'],'goal_id':plan['goal_id'],
                    'mode':existing.get('mode','goal'),'request':existing['request'],'request_message_id':existing.get('request_message_id')}
            if run['mode']!='plan' or run['status']!='completed' or plan['status']!='ready': raise ValueError('Finish and review a saved plan before selecting Build.')
            if self.store.get_chat(plan['chat_id'],limit=1).get('archived'): raise ValueError('Restore this chat before building the plan.')
            if not plan.get('goal_id'):
                goal=self.goal_create({'text':plan['request'],'tasks':plan['tasks'],'project_id':plan.get('project_id'),'chat_id':plan['chat_id']})
                self.store.save_goal({**goal,'plan_id':plan['id'],'reviewed_plan':plan['markdown']})
                plan=self.store.save_entity('plans',{**plan,'goal_id':goal['id']})
            else:
                goal=self.store.goal(plan['goal_id'])
                self.store.save_goal({**goal,'plan_id':plan['id'],'reviewed_plan':plan['markdown']})
            result=self.goal_resume({**data,'id':plan['goal_id']})
            return {**result,'goal_id':plan['goal_id']}
    def goal_resume(self,data):
        with self.jobs.lock: return self._goal_resume(data)
    def _goal_resume(self,data):
        goal=self.store.goal(data['id'])
        if goal.get('project_missing'): raise ValueError('Reconnect the original project or export this checklist into a new goal before continuing.')
        if goal['external_edits']: raise ValueError('Reconcile the edited checklist first.')
        if goal.get('chat_id') and self.store.get_chat(goal['chat_id'],limit=1).get('project_id')!=goal.get('project_id'):
            raise ValueError('The chat moved to another project. Export this checklist into the intended project and create a new goal.')
        if goal.get('status')=='completed': raise ValueError('This goal is completed. Create a new goal for additional work.')
        previous=next((r for r in self.store.runs() if r.get('goal_id')==goal['id'] and
            not r.get('parent_id') and r['chat_id']==goal.get('chat_id')),None)
        if previous and previous['status'] in ('paused','interrupted','failed'):
            # Retain tool outcomes and the exact accepted request on Resume.
            legacy_checkpoint=goal.get('checkpoint')=='Checklist created. Execution has not started.'
            if legacy_checkpoint:
                next_task=next((task['text'] for task in goal.get('tasks',[]) if task.get('status')!='completed'),None)
                self.store.save_goal({**goal,'checkpoint':'Implementation is continuing from the saved checklist.',
                    'next_action':('Execute the first unfinished task: '+next_task) if next_task else 'Review completion evidence and finish the goal.'})
            try: result=self.jobs.resume(previous['id'],data)
            except Exception:
                if legacy_checkpoint:self.store.save_goal(goal)
                raise
            with self.store._goal_lock:
                fresh=self.store.goal(goal['id'])
                if fresh.get('status')!='completed' and self.store.run(previous['id'])['status'] not in TERMINAL:
                    self.store.save_goal({**fresh,'status':'running'})
            return {**result,'goal_id':goal['id']}
        if previous and previous['status'] not in TERMINAL:
            return {'id':previous['id'],'chat_id':previous['chat_id'],'status':previous['status'],'goal_id':goal['id'],
                'mode':previous.get('mode','goal'),'request':previous['request'],'request_message_id':previous.get('request_message_id')}
        next_task=next((task['text'] for task in goal.get('tasks',[]) if task.get('status')!='completed'),None)
        starting=previous is None or goal.get('checkpoint')=='Checklist created. Execution has not started.'
        checkpoint=('The reviewed plan was accepted. Implementation is starting.' if goal.get('reviewed_plan') else
            'Goal execution is starting.') if starting else goal.get('checkpoint','Continue from the saved progress.')
        active={**goal,'status':'running','checkpoint':checkpoint,
            'next_action':('Execute the first unfinished task: '+next_task) if next_task else 'Review completion evidence and finish the goal.'}
        # Persist the execution checkpoint before inference can read TODO.md.
        self.store.save_goal(active)
        request=('Start implementing the reviewed plan now.' if goal.get('reviewed_plan') else 'Start executing this goal now.')+(
            '\nThis action starts implementation. Continue the existing workflow; do not treat the objective below as a resubmitted planning prompt.'
            '\nFollow the saved ordered checklist, update completion evidence, and continue until complete, blocked, paused or limited.'
            '\nGoal objective:\n'+goal['request'])
        instructions=data.get('instructions','')
        if goal.get('reviewed_plan'):
            instructions=(instructions+'\n' if instructions else '')+'Implement the user-reviewed plan:\n'+goal['reviewed_plan']
        try:
            result=self.jobs.start({**data,'text':request,'instructions':instructions,'project_id':goal.get('project_id'),
                'chat_id':goal.get('chat_id'),'goal_id':goal['id'],'mode':'goal'})
        except Exception:
            self.store.save_goal(goal)
            raise
        with self.store._goal_lock:
            fresh=self.store.goal(goal['id'])
            if fresh.get('chat_id')!=result['chat_id']:
                self.store.save_goal({**fresh,'chat_id':result['chat_id']})
        return {**result,'goal_id':goal['id']}
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
        text=data.get('text','').strip(); parts=text.split(maxsplit=1)
        head=parts[0] if parts else ''; argument=parts[1] if len(parts)>1 else ''; name=head.lstrip('/').lower()
        if name=='to-do': name='todo'
        if name not in {c['name'] for c in COMMANDS}: raise ValueError('Unknown command. Use /help.')
        arguments={**data,'text':argument.strip()}
        if name=='help': return {'commands':COMMANDS,'message':'Choose a slash command in the composer.'}
        if name=='new': return {'chat':self.store.create_chat(data.get('project_id'),model=data.get('model','')),'navigate':'chat'}
        if name=='todo':
            if not argument:
                latest=next((r for r in self.store.runs() if r['chat_id']==data.get('chat_id')),None)
                if latest and latest.get('mode')=='plan': return self.plan_build({'run_id':latest['id']})
                return {'navigate':'goals','goals':self.store.entities('goals'),'message':'Use /plan first, or /todo followed by an ordered checklist to start.'}
            goal=self.goal_create(arguments); result=self.goal_resume({**data,'id':goal['id']})
            return {**result,'goal_id':goal['id']}
        if name=='goal':
            # /goal after a reviewed /plan is the same explicit Build action.
            plans=[plan for plan in self.store.entities('plans') if plan.get('chat_id')==data.get('chat_id') and plan.get('status')=='ready']
            latest_plan=plans[-1] if plans else None
            if latest_plan and (not argument or argument.strip()==latest_plan.get('request','').strip()):
                return self.plan_build({**data,'run_id':latest_plan['run_id']})
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
        if profile.get('provider_id') and profile['provider_id']!='ollama': self.store.entity('providers',profile['provider_id'])
        if 'goal_limits' in profile: profile['goal_limits']=validate_goal_limits(profile['goal_limits'])
        for key in ('rounds','tokens'):
            if key in profile and (type(profile[key]) is not int or not 1<=profile[key]<=1_000_000_000): raise ValueError('Agent '+key+' limit must be a positive integer.')
        return self.store.save_entity('agents',profile)
    def agent_start(self,data):
        with self.admission_lock,self.agent_lock:
            if self.update_manager and self.update_manager.applying:
                raise AgentLaunchRejected('Forge is preparing an update. Resume delegation after restart.')
            return self._agent_start(data)
    def _agent_start(self,data):
        source_key=data.get('source_key')
        if source_key:
            if not isinstance(source_key,str) or len(source_key)>500: raise AgentLaunchRejected('Invalid delegation request identity.')
            with self.store._connection() as db:
                prior=db.execute('SELECT run_id FROM request_keys WHERE source_key=?',(source_key,)).fetchone()
            if prior:
                run=self.store.run(prior[0])
                if run.get('parent_id')!=data.get('parent_id') or run.get('agent_id')!=data.get('agent_id'):
                    raise AgentLaunchRejected('This delegation request already belongs to another helper.')
                return {**{key:run.get(key) for key in ('id','chat_id','status','mode','request','settings','request_message_id','goal_id','project_id','parent_id')},
                    'worktree':None,'reused':True}
        try:
            profile=self.store.entity('agents',data['agent_id'])
            parent=self.store.run(data['parent_id']) if data.get('parent_id') else None
        except (ValueError,KeyError): raise AgentLaunchRejected('The agent profile or parent run is unavailable.') from None
        if not profile.get('enabled',True): raise AgentLaunchRejected('Enable this agent profile first.')
        if parent and parent.get('parent_id'): raise AgentLaunchRejected('Helper agents cannot recursively delegate. Return findings to the main agent.')
        if parent and parent['status'] in TERMINAL: raise AgentLaunchRejected('The parent run is no longer active.')
        if parent:
            with self.jobs.lock: parent_job=self.jobs.jobs.get(parent['id'])
            if parent_job and parent_job['cancel'].is_set(): raise AgentLaunchRejected('The parent run is stopping; no helper was started.')
        if parent and data.get('project_id') and data['project_id']!=parent.get('project_id'):
            raise AgentLaunchRejected('A helper must use its parent project.')
        settings=parent['settings'] if parent else self.store.get_settings()
        if not parent and profile.get('goal_limits'): settings={**settings,'goal_limits':profile['goal_limits']}
        project_id=parent.get('project_id') if parent else data.get('project_id')
        provider_id=profile.get('provider_id') or settings['provider_id']
        model=profile.get('model') or settings['model']
        if provider_id=='openrouter':
            try: config=self.store.entity('providers','openrouter')
            except ValueError: raise AgentLaunchRejected('Connect OpenRouter before launching a cloud helper.') from None
            if not config.get('enabled') or not config.get('remote_consent') or not config.get('credential_ref'):
                raise AgentLaunchRejected('Enable OpenRouter with cloud consent and a saved key before launching a cloud helper.')
            model=profile.get('model') or config.get('model') or 'openrouter/free'
            if model!='openrouter/free' and not re.fullmatch(r'[A-Za-z0-9_./-]+:free',model):
                raise AgentLaunchRejected('OpenRouter helpers require the free router or a :free model.')
        readonly=profile.get('role') in ('researcher','reviewer')
        if parent and (parent.get('readonly') or parent.get('mode')=='plan') and not readonly:
            raise AgentLaunchRejected('Read-only runs can only launch read-only helpers.')
        text=data.get('text') or data.get('prompt') or 'Inspect the project.'
        if not isinstance(text,str) or not text.strip() or len(text)>1_000_000:
            raise AgentLaunchRejected('Enter a helper task of 1–1,000,000 characters.')
        if not model: raise AgentLaunchRejected('Select a model for this agent profile.')
        from context_window import validate_context
        try: validate_context(profile.get('context',settings['context']))
        except ValueError as exc: raise AgentLaunchRejected(str(exc)) from None
        from forge_channels import permission_ceiling
        ceiling=permission_ceiling((parent or {}).get('permission_ceiling') or settings['permission_profile'],
            data.get('permission_ceiling') or settings['permission_profile'])
        ceiling=permission_ceiling(ceiling,settings['permission_profile'])
        tools=profile.get('tools',[])
        if parent and parent.get('agent_tools'):
            allowed=set(parent['agent_tools'])
            tools=[name for name in (tools or parent['agent_tools']) if name in allowed]
            # An empty list conventionally means unrestricted; retain a harmless
            # explicit scope when the two profiles have no tool intersection.
            if not tools: tools=['artifact_read']
        worktree=None
        if project_id and not readonly:
            project=self.store.get_project(project_id)
            if self._git(Path(project['path']),['rev-parse','--is-inside-work-tree'],check=False).returncode==0:
                worktree=self.worktree_create({'project_id':project_id})
            elif parent and parent.get('project_id')==project_id:
                raise AgentLaunchRejected('Writing delegation requires a Git worktree. Initialize and commit this project, or run the agent after the main writer pauses.')
        result=self.jobs.start({**settings,'context':profile.get('context',settings['context']),
            'provider_id':provider_id,'auto_delegate':False,
            'model':model,'text':text,
            'project_id':project_id,'parent_id':data.get('parent_id'),'readonly':readonly,
            'instructions':profile.get('instructions',''),'agent_id':profile['id'],'agent_tools':tools,'skills':profile.get('skills',[]),
            'worktree_id':worktree['worktree']['id'] if worktree else None,
            'workspace_project_id':worktree['project']['id'] if worktree else (parent or {}).get('workspace_project_id'),
            'permission_ceiling':ceiling,
            'goal_id':parent.get('goal_id') if parent else None,'source_key':source_key})
        return {**result,'worktree':worktree}

    def delegation_profiles(self,run):
        """Bounded local discovery; no provider requests or unrelated chat reads."""
        if run.get('parent_id') or not run['settings'].get('auto_delegate'): return []
        settings=self.store.get_settings()
        if (not settings.get('auto_delegate') and run.get('initial_preferences',{}).get('auto_delegate')) or settings.get('permission_profile')=='deny_access': return []
        providers={p['id']:p for p in self.store.entities('providers')}
        available=[]
        for profile in self.store.entities('agents'):
            if not profile.get('enabled',True): continue
            provider_id=profile.get('provider_id') or run['settings']['provider_id']
            if provider_id!='ollama':
                config=providers.get(provider_id)
                if not config or config.get('enabled',True) is False: continue
                if config.get('kind')=='openrouter' and not (config.get('remote_consent') and config.get('credential_ref')): continue
            else: config={}
            readonly=profile.get('role') in ('researcher','reviewer')
            if (run.get('readonly') or run.get('mode')=='plan') and not readonly: continue
            tools=profile.get('tools',[])
            if run.get('agent_tools'):
                tools=[name for name in (tools or run['agent_tools']) if name in run['agent_tools']]
            available.append(dict(id=profile['id'],name=str(profile.get('name','Agent'))[:100],
                role=profile.get('role','researcher'),provider_id=provider_id,
                model=profile.get('model') or (config.get('model') or 'openrouter/free' if config.get('kind')=='openrouter' else run['settings']['model']),readonly=readonly,
                cloud=config.get('kind')=='openrouter',free_only=config.get('kind')=='openrouter',
                context=profile.get('context',run['settings']['context']),tools=tools[:40],
                tool_scope='read-only enabled project tools' if not tools and readonly else ('enabled project tools' if not tools else 'listed tools only')))
            if len(available)>=20: break
        return available

    def agent_result(self,parent,args,cancel):
        """Wait for one direct child using journal notifications, without model polling."""
        identifier=args['run_id']; child=self.store.run(identifier)
        if child.get('parent_id')!=parent['id']: raise ValueError('This is not a child of the current run.')
        seconds=args.get('wait_seconds',120)
        if type(seconds) not in (int,float) or not 0<=seconds<=300: raise ValueError('Wait must be between 0 and 300 seconds.')
        wake=threading.Event()
        def changed(event):
            if event.get('run_id')==identifier and event.get('type') in ('done','approval','question','error','status'):
                wake.set()
        self.store.subscribe_events(changed)
        try:
            deadline=time.monotonic()+seconds
            child=self.store.run(identifier)
            with self.jobs.lock: live=identifier in self.jobs.jobs
            while live and child['status'] not in TERMINAL and child['status'] not in ('awaiting_approval','waiting_question'):
                if cancel.is_set() or time.monotonic()>=deadline: break
                # Cancellation stays responsive; only journal changes trigger a
                # database read, rather than every token or elapsed interval.
                if wake.wait(min(.25,max(0,deadline-time.monotonic()))):
                    wake.clear(); child=self.store.run(identifier)
            child=self.store.run(identifier)
        finally: self.store.unsubscribe_events(changed)
        with self.store._connection() as db:
            later=db.execute("SELECT MIN(CAST(json_extract(data,'$.request_message_id') AS INTEGER)) FROM runs WHERE chat_id=? AND id<>? AND CAST(json_extract(data,'$.request_message_id') AS INTEGER)>?",
                (child['chat_id'],child['id'],child['request_message_id'])).fetchone()[0]
            rows=db.execute("SELECT * FROM messages WHERE chat_id=? AND id>=? AND (? IS NULL OR id<?) AND role IN ('user','assistant') ORDER BY id DESC LIMIT 12",
                (child['chat_id'],child['request_message_id'],later,later)).fetchall()
            messages=[dict(id=row['id'],role=row['role'],content=row['content']) for row in reversed(rows)]
        assistant=next((message['content'] for message in reversed(messages) if message['role']=='assistant'),'')
        public={key:child.get(key) for key in ('id','chat_id','parent_id','agent_id','project_id','workspace_project_id','worktree_id',
            'status','readonly','checkpoint','recovery','rounds','tools','output_tokens')}
        public['settings']={key:child['settings'].get(key) for key in ('provider_id','model','context')}
        return {'run':public,'messages':messages,'response':assistant,'finished':child['status'] in TERMINAL,
            'needs_attention':child['status'] in ('paused','interrupted','awaiting_approval','waiting_question'),
            'waiting':child['status'] not in TERMINAL,'cancelled_wait':cancel.is_set(),
            'next_action':'Use the helper evidence to continue the task.' if child['status']=='completed' else
                'Inspect the helper activity or pending approval/question; call agent_result to wait again. Do not treat unfinished work as complete.'}

    def openrouter_setup_agents(self):
        """Explicit one-click setup preserves all previously edited profiles."""
        config=self.store.entity('providers','openrouter')
        if not config.get('enabled') or not config.get('remote_consent') or not config.get('credential_ref'):
            raise ValueError('Connect and enable OpenRouter with cloud consent before setting up cloud helpers.')
        tools=['web_search','web_fetch','list_files','read_file','search_files','skills_read','artifact_read']
        definitions=[dict(id='openrouter-researcher',name='OpenRouter Researcher',role='researcher',
            instructions='Research the assigned bounded question using primary sources. Cite evidence and explain uncertainty. Read only the connected project when needed. Return concise findings to the local orchestrator. Do not edit files or delegate.'),
            dict(id='openrouter-assistant',name='OpenRouter Assistant',role='reviewer',
            instructions='Help the local orchestrator with the assigned bounded analysis, debugging or review question. Inspect relevant connected-project evidence, state actionable findings and concrete next steps, then return them to the local agent. Do not edit files, claim the overall goal complete or delegate.')]
        created=[]; preserved=[]
        with self.store._connection(transaction='write') as db:
            for profile in definitions:
                if db.execute("SELECT 1 FROM entities WHERE kind='agents' AND id=?",(profile['id'],)).fetchone():
                    preserved.append(profile['id']); continue
                profile.update(provider_id='openrouter',model='openrouter/free',context=32768,tools=tools,
                    skills=[],enabled=True,tokens=25000,rounds=32,updated_at=_now())
                db.execute('INSERT INTO entities VALUES(?,?,?)',('agents',profile['id'],encode(profile))); created.append(profile['id'])
            for key in ('auto_delegate','goal_review_enabled'):
                db.execute('INSERT INTO forge_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(True)))
        return {'agents':self.store.entities('agents'),'settings':self.store.get_settings(),'created':created,'preserved':preserved}

    def provider_save(self,data):
        if data.get('kind')=='openrouter': return self.dispatch('openrouter_save',data)
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
        return self.dispatch('performance')

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
            with client.stream('GET',url,headers={'User-Agent':'Forge/4.1'}) as response:
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
        if schedule.get('project_missing'): raise ValueError('Reconnect the original project and review this schedule before running it.')
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
