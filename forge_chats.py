"""Explicit chat lifecycle actions, preserving usage and durable project history."""
from __future__ import annotations

import json
import time

from forge_store import TERMINAL, encode
from storage import _now


def _related(service, chat_id):
    runs=service.store.runs()
    identifiers={r['id'] for r in runs if r['chat_id']==chat_id}
    return _descendants(runs,identifiers)


def _descendants(runs,identifiers):
    while True:
        expanded=identifiers|{r['id'] for r in runs if r.get('parent_id') in identifiers}
        if expanded==identifiers: break
        identifiers=expanded
    return [r for r in runs if r['id'] in identifiers]


def _live(service, runs):
    return [service.jobs.jobs[r['id']]['thread'] for r in runs if r['id'] in service.jobs.jobs
            and service.jobs.jobs[r['id']]['thread'].is_alive()]


def _quiesce(service, chat_id, timeout=3):
    """Stop first, join outside the jobs lock, then revalidate under that lock."""
    with service.jobs.lock:
        service.store.get_chat(chat_id,limit=1)
        runs=_related(service,chat_id)
    _quiesce_runs(service,runs,timeout)


def _quiesce_runs(service,runs,timeout=3):
    with service.jobs.lock:
        for run in runs:
            if run['status'] not in ('completed','cancelled'):
                service.jobs.cancel(run['id'],pause=False)
        threads=_live(service,runs)
    deadline=time.monotonic()+timeout
    for thread in threads:
        thread.join(timeout=max(0,deadline-time.monotonic()))
    # The caller holds this lock until its database transaction commits. Starting
    # a new chat run cannot race the final checks and mutation.


def _verify_stopped(service, chat_id):
    runs=_related(service,chat_id)
    if _live(service,runs) or any(r['status'] not in TERMINAL for r in runs):
        raise ValueError('A chat operation is still stopping. Wait for the run to finish before moving or deleting it.')
    for run in runs:
        if service.store.unknown_actions(run['id']):
            raise ValueError('Inspect and resolve outcome-unknown actions before moving or deleting this chat. Its journal is preserved.')
    return runs


def _archive(service,chat_id,archived):
    if type(archived) is not bool: raise ValueError('Archived must be true or false.')
    with service.jobs.lock:
        with service.store._connection(transaction='write') as db:
            chat=service.store._chat(db,chat_id)
            runs=_related(service,chat_id)
            if archived and (_live(service,runs) or any(r['status'] not in TERMINAL for r in runs)):
                raise ValueError('Pause or stop active work before archiving this chat.')
            db.execute('UPDATE chats SET archived=?,updated_at=? WHERE id=?',(int(archived),_now(),chat_id))
            chat=service.store._chat(db,chat_id)
    return {'ok':True,'chat':chat,'archived':archived}


def _move(service,chat_id,project_id):
    if project_id is not None: service.store.get_project(project_id)
    _quiesce(service,chat_id)
    with service.jobs.lock:
        _verify_stopped(service,chat_id)
        with service.store._connection(transaction='write') as db:
            chat=service.store._chat(db,chat_id)
            if project_id is not None: service.store._project(db,project_id)
            db.execute('UPDATE chats SET project_id=?,updated_at=? WHERE id=?',(project_id,_now(),chat_id))
            # Existing runs/goals/plans keep their original project identities.
            # Only future chat runs adopt the newly selected project.
            chat=service.store._chat(db,chat_id)
    return {'ok':True,'chat':chat,'message':'Chat moved. Saved runs retain their original project; unfinished work was stopped.'}


def _delete(service,chat_id):
    _quiesce(service,chat_id)
    with service.jobs.lock:
        _verify_stopped(service,chat_id)
        with service.store._connection(transaction='write') as db:
            service.store._chat(db,chat_id)
            identifiers=[row[0] for row in db.execute('SELECT id FROM runs WHERE chat_id=?',(chat_id,))]
            # Usage is an independent historical ledger; keep totals and filters.
            # No inference is repeated because a schedule occurrence loses its run.
            for identifier in identifiers:
                db.execute('UPDATE usage SET run_id=NULL WHERE run_id=?',(identifier,))
                db.execute('UPDATE occurrences SET run_id=NULL WHERE run_id=?',(identifier,))
                db.execute('DELETE FROM approvals WHERE run_id=?',(identifier,))
                db.execute('DELETE FROM run_events WHERE run_id=?',(identifier,))
                db.execute('DELETE FROM invocations WHERE run_id=?',(identifier,))
            removed=set(identifiers)
            for row in db.execute('SELECT id,parent_id,data FROM runs').fetchall():
                if row['id'] not in removed and row['parent_id'] in removed:
                    data=json.loads(row['data']);data.update(parent_id=None,detached_parent_id=row['parent_id'])
                    db.execute('UPDATE runs SET parent_id=NULL,data=? WHERE id=?',(encode(data),row['id']))
            for row in db.execute('SELECT kind,id,data FROM entities').fetchall():
                data=json.loads(row['data']); changed=False
                if data.get('chat_id')==chat_id:
                    data.update(chat_id=None,detached_chat_id=chat_id);changed=True
                if chat_id in (data.get('chat_ids') or []):
                    data['chat_ids']=[v for v in data['chat_ids'] if v!=chat_id];changed=True
                for field in ('run_id','last_run_id'):
                    if data.get(field) in removed:
                        data['detached_'+field]=data[field];data[field]=None;changed=True
                if changed:
                    data['updated_at']=_now()
                    db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?',(encode(data),row['kind'],row['id']))
            db.execute('DELETE FROM runs WHERE chat_id=?',(chat_id,))
            db.execute('DELETE FROM messages WHERE chat_id=?',(chat_id,))
            db.execute('DELETE FROM chats WHERE id=?',(chat_id,))
    return {'ok':True,'deleted':chat_id,'message':'Chat deleted. Usage totals and detached goal/plan artifacts are preserved.'}


def _project_delete(service,project_id):
    project=service.store.get_project(project_id)
    def worktree_guard():
        for worktree in service.store.entities('worktrees'):
            if project_id in (worktree.get('parent_project_id'),worktree.get('project_id')) and worktree.get('status','active')=='active':
                raise ValueError('Review and integrate active worktrees before removing this project registration. Worktree folders will be preserved.')
    worktree_guard()
    def affected_runs():
        chats={c['id'] for c in service.store.list_chats(project_id,archived=None)}
        runs=service.store.runs()
        identifiers={r['id'] for r in runs if r.get('project_id')==project_id or r['chat_id'] in chats}
        return _descendants(runs,identifiers)
    _quiesce_runs(service,affected_runs())
    with service.jobs.lock:
        worktree_guard()
        affected=affected_runs()
        if _live(service,affected) or any(r['status'] not in TERMINAL for r in affected) or any(service.store.unknown_actions(r['id']) for r in affected):
            raise ValueError('Stop and inspect original project work before removing its registration.')
        with service.store._connection(transaction='write') as db:
            service.store._project(db,project_id)
            tasks=[dict(row) for row in db.execute('SELECT * FROM tasks WHERE project_id=?',(project_id,))]
            archive={'id':project_id,'project':project,'tasks':tasks,'removed_at':_now(),'folder_preserved':True}
            db.execute('INSERT INTO entities(kind,id,data) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',
                       ('project_archives',project_id,encode(archive)))
            db.execute('UPDATE chats SET project_id=NULL,updated_at=? WHERE project_id=?',(_now(),project_id))
            for row in db.execute('SELECT kind,id,data FROM entities').fetchall():
                if row['kind']=='project_archives': continue
                data=json.loads(row['data']);changed=False
                for field in ('project_id','parent_project_id'):
                    if data.get(field)==project_id:
                        data['detached_'+field]=project_id;data[field]=None;changed=True
                        if row['kind'] in ('goals','plans','schedules'):
                            data['project_missing']=True
                            data['blockers']='Original project registration was removed. Reconnect the original folder or export this artifact before continuing.'
                            if row['kind']=='schedules': data['enabled']=False
                if project_id in (data.get('project_ids') or []):
                    data['project_ids']=[value for value in data['project_ids'] if value!=project_id];changed=True
                if changed:
                    data['updated_at']=_now()
                    db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?',(encode(data),row['kind'],row['id']))
            db.execute('DELETE FROM tasks WHERE project_id=?',(project_id,))
            db.execute('DELETE FROM projects WHERE id=?',(project_id,))
    return {'ok':True,'deleted':project_id,'folder_preserved':True,
            'message':'Project registration removed. Its folder and chats are preserved; saved goals/plans require their original project before continuing.'}


def dispatch_chat(service,action,data):
    if action=='project_delete':
        project_id=data.get('project_id') or data.get('id')
        if not isinstance(project_id,str) or not project_id: raise ValueError('Select a project.')
        return _project_delete(service,project_id)
    chat_id=data.get('chat_id') or data.get('id')
    if not isinstance(chat_id,str) or not chat_id: raise ValueError('Select a chat.')
    if action=='chat_archive': return _archive(service,chat_id,data.get('archived',True))
    if action=='chat_move': return _move(service,chat_id,data.get('project_id'))
    if action=='chat_delete': return _delete(service,chat_id)
    raise ValueError('Unknown chat action.')
