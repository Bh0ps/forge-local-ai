"""Same API for native desktop and Docker/browser clients."""
import os
from pathlib import Path
from core import Core
from storage import Store
from agent_runtime import AgentJobs

class Service:
    def __init__(self, core=None, store=None):
        self.core = core or Core()
        self.store = store or Store()
        self.jobs = AgentJobs(self.core, self.store)

    def dispatch(self, action, data=None):
        data = {} if data is None else data
        if not isinstance(data, dict): raise ValueError('Expected an object')
        if action == 'settings':
            return self.store.update_settings(data) if data else self.store.get_settings()
        if action == 'projects': return {'projects': self.store.list_projects()}
        if action == 'create_project':
            if not isinstance(data.get('path'),str) or not data['path'].strip(): raise ValueError('Choose a project folder.')
            path = Path(data.get('path','')).expanduser().resolve()
            allowed = os.getenv('SIDEKICK_PROJECTS_ROOT')
            if allowed and not path.is_relative_to(Path(allowed).resolve()):
                raise ValueError('Choose a directory inside the mounted workspace.')
            return self.store.create_project(data.get('name') or path.name, str(path))
        if action == 'chats': return {'chats': self.store.list_chats(data.get('project_id'))}
        if action == 'get_chat': return self.store.get_chat(data.get('id'), limit=data.get('limit'))
        if action == 'rename_chat': return self.store.rename_chat(data.get('id'), data.get('title')) or {'ok':True}
        if action == 'create_chat': return self.store.create_chat(data.get('project_id'), model=data.get('model',''))
        if action == 'tasks': return {'tasks': self.store.list_tasks(data.get('project_id'))}
        if action == 'create_task': return self.store.create_task(data.get('project_id'), data.get('title'))
        if action == 'update_task': return self.store.update_task(data.get('project_id'), data.get('id'), data.get('status'))
        if action == 'start_chat': return self.jobs.start(data)
        if action == 'poll': return self.jobs.poll(data.get('id'))
        if action == 'cancel': return self.jobs.cancel(data.get('id'))
        if action == 'approve': return self.jobs.approve(data.get('job_id'), data.get('approval_id'), data.get('allowed'))
        return self.core.dispatch(action, data)
