"""Workspace cosmetics and read-only activity projections over the durable journal."""
from __future__ import annotations

import json
import re
from copy import deepcopy


ACCENTS = ('copper', 'sage', 'blue', 'violet', 'rose')
ICONS = ('folder', 'code', 'globe', 'book', 'flask', 'palette')
TABS = ('context', 'browser', 'activity', 'goals', 'files')
DEFAULT_APPEARANCE = {'name': 'Forge', 'accent': 'copper', 'motion': 'system'}
DEFAULT_LAYOUT = {'sidebar_open': True, 'sidebar_width': 260, 'right_panel_open': False,
                  'right_panel_width': 320, 'right_panel_tab': 'activity', 'collapsed_project_ids': []}
SEMANTIC_EVENTS = ('tool', 'agent', 'specialist', 'skill', 'status', 'error', 'outcome_unknown',
                   'approval', 'question', 'question_answered', 'goal_review', 'verification',
                   'progress_nudge', 'planner', 'steer', 'complete', 'done')
EVENT_FIELDS = frozenset(('name', 'state', 'text', 'invocation_id', 'arguments', 'result', 'artifact',
    'child_run_id', 'agent_id', 'specialty', 'reason', 'selection_reason', 'digest', 'passed',
    'failed_count', 'review', 'assignment', 'summary', 'cancelled', 'approval_id', 'question_id'))


def workspace_appearance(value, current=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_APPEARANCE):
        raise ValueError('Choose a workspace name, accent and motion preference.')
    result = {**DEFAULT_APPEARANCE, **(current or {}), **value}
    name = result['name']
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60 or any(ord(c) < 32 for c in name):
        raise ValueError('Workspace names must contain 1–60 printable characters.')
    if result['accent'] not in ACCENTS:
        raise ValueError('Choose a supported accent palette.')
    if result['motion'] not in ('system', 'reduced'):
        raise ValueError('Choose system or reduced motion.')
    return {**result, 'name': name.strip()}


def project_appearance(value):
    if not isinstance(value, dict) or set(value) - {'icon', 'accent'}:
        raise ValueError('Choose a project icon and optional accent.')
    icon = value.get('icon', 'folder')
    if icon not in ICONS or value.get('accent') is not None and value['accent'] not in ACCENTS:
        raise ValueError('Choose a supported project icon and accent.')
    return {'icon': icon, **({'accent': value['accent']} if value.get('accent') else {})}


def _client_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Use a stable workspace client identity.')
    return value


def _failure(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('provider_id', 'category'):
        if isinstance(value.get(key), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value[key]):
            result[key] = value[key]
    if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        result['http_status'] = value['http_status']
    return result or None


def _layout(value, projects, current=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LAYOUT):
        raise ValueError('Unknown workspace layout preference.')
    result = {**deepcopy(DEFAULT_LAYOUT), **(current or {}), **value}
    for key in ('sidebar_open', 'right_panel_open'):
        if type(result[key]) is not bool:
            raise ValueError('Pane visibility must be true or false.')
    for key, lower, upper in (('sidebar_width', 220, 360), ('right_panel_width', 280, 520)):
        if type(result[key]) is not int:
            raise ValueError('Pane widths must be whole pixels.')
        result[key] = max(lower, min(upper, result[key]))
    if result['right_panel_tab'] not in TABS:
        raise ValueError('Choose a workspace panel tab.')
    ids = result['collapsed_project_ids']
    if not isinstance(ids, list) or len(ids) > 2000 or any(not isinstance(v, str) or len(v) > 80 for v in ids):
        raise ValueError('Invalid collapsed project selection.')
    result['collapsed_project_ids'] = list(dict.fromkeys(v for v in ids if v in projects))
    return result


class WorkspaceUI:
    def __init__(self, store):
        self.store = store

    def project(self, project):
        try:
            appearance = project_appearance(self.store.entity('project_appearance', project['id'])['appearance'])
        except (ValueError, KeyError, TypeError):
            appearance = {'icon': 'folder'}
        return {**project, 'appearance': appearance}

    def projects(self):
        return [self.project(project) for project in self.store.list_projects()]

    def save_project(self, data):
        project = self.store.get_project(data.get('project_id'))
        appearance = project_appearance(data.get('appearance'))
        self.store.save_entity('project_appearance', {'id': project['id'], 'appearance': appearance})
        return {**project, 'appearance': appearance}

    def layout(self, data, *, save=False):
        identifier = _client_id(data.get('client_id'))
        projects = {p['id'] for p in self.store.list_projects()}
        # Merge concurrent partial preference changes in one transaction.
        with self.store._connection(transaction='write' if save else 'read') as db:
            row = db.execute("SELECT data FROM entities WHERE kind='ui_layout' AND id=?", (identifier,)).fetchone()
            saved = json.loads(row[0]).get('layout', {}) if row else {}
            try:
                current = _layout(saved, projects)
            except (ValueError, TypeError, KeyError):
                current = deepcopy(DEFAULT_LAYOUT)
            result = _layout(data.get('layout', {}) if save else {}, projects, current)
            if save:
                payload = json.dumps({'id': identifier, 'layout': result}, ensure_ascii=False, allow_nan=False)
                db.execute("INSERT INTO entities(kind,id,data) VALUES('ui_layout',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                           (identifier, payload))
        return result

    @staticmethod
    def _groups(db, roots):
        if not roots:
            return []
        identifiers = list(dict.fromkeys(r['id'] for r in roots))
        placeholders = ','.join('?' for _ in identifiers)
        rows = db.execute('WITH RECURSIVE descendants(id,depth) AS ('
            'SELECT id,0 FROM runs WHERE id IN (' + placeholders + ') UNION ALL '
            'SELECT r.id,d.depth+1 FROM runs r JOIN descendants d ON r.parent_id=d.id WHERE d.depth<16) '
            'SELECT DISTINCT r.* FROM runs r JOIN descendants d ON d.id=r.id ORDER BY r.created_at,r.id', identifiers).fetchall()
        runs = {row['id']: json.loads(row['data']) for row in rows}
        ids = list(runs)
        counts, last_tools, statuses = {}, {}, {}
        if ids:
            marks = ','.join('?' for _ in ids)
            for row in db.execute('SELECT run_id,name,status,updated_at FROM invocations WHERE run_id IN (' + marks + ') ORDER BY updated_at', ids):
                if row['name'] not in ('delegate_agent', 'agent_result', 'tools_search', 'tools_load'):
                    counts[row['run_id']] = counts.get(row['run_id'], 0) + 1
                    last_tools[row['run_id']] = dict(row)
            for row in db.execute('SELECT e.run_id,e.payload FROM run_events e JOIN ('
                "SELECT run_id,MAX(seq) AS seq FROM run_events WHERE type='status' AND run_id IN (" + marks + ') GROUP BY run_id'
                ') latest ON e.run_id=latest.run_id AND e.seq=latest.seq', ids):
                statuses[row['run_id']] = json.loads(row['payload']).get('text')

        def group(run, ancestors=(), include_attempts=True):
            settings = run.get('settings') or {}
            progress = run.get('progress_summary') or {}
            tool = last_tools.get(run['id'])
            step = progress.get('activity') or statuses.get(run['id'])
            if not step and tool:
                labels = {'running': 'Using ', 'completed': 'Completed ', 'prepared': 'Preparing ',
                          'failed': 'Failed ', 'cancelled': 'Cancelled '}
                step = 'Inspect interrupted action' if tool['status'] == 'outcome_unknown' else labels.get(tool['status'], 'Inspect ') + tool['name'].replace('_', ' ')
            if run['status'] in ('completed', 'cancelled'):
                step = run['status'].capitalize()
            elif tool and tool['status'] == 'outcome_unknown':
                step = 'Inspect interrupted action'
            elif run['status'] in ('paused', 'failed', 'interrupted') and (not step or str(step).lower().startswith(('working', 'generating', 'loading', 'using ', 'preparing '))):
                step = run['status'].capitalize()
            assignments = {}
            for child in runs.values():
                if child.get('parent_id') != run['id'] or child['id'] in (*ancestors, run['id']):
                    continue
                key = child.get('assignment_id') or child['id']
                prior = assignments.get(key)
                if prior is None or child.get('attempt', 1) >= prior.get('attempt', 1):
                    assignments[key] = child
            children = [group(child, (*ancestors, run['id'])) for child in assignments.values()]
            previous = [item for item in runs.values() if include_attempts and run.get('assignment_id')
                        and item.get('assignment_id') == run['assignment_id']
                        and item.get('parent_id') == run.get('parent_id')
                        and item.get('attempt', 1) < run.get('attempt', 1)]
            attempts = [group(item, ancestors, False) for item in previous]
            return dict(run_id=run['id'], request_message_id=run.get('request_message_id'), status=run['status'],
                mode=run.get('mode', 'chat'), model=run.get('returned_model_last') or settings.get('model'),
                provider_id=settings.get('provider_id'), created_at=run.get('created_at'), updated_at=run.get('updated_at'),
                current_step=step or run['status'].replace('_', ' '), summary=progress.get('blocker') or run.get('recovery') or '',
                tool_count=counts.get(run['id'], 0) + sum(item['tool_count'] for item in attempts), helper_count=len(children), children=children,
                attempts=attempts,
                assignment_id=run.get('assignment_id'), attempt=run.get('attempt', 1), retry_count=run.get('retry_count', 0),
                failure=_failure(run.get('failure')), finish_reason=run.get('last_finish_reason'),
                response_provider=run.get('response_provider'))

        return [group(runs[r['id']]) for r in roots if r['id'] in runs]

    def chat(self, identifier, limit=200):
        chat = self.store.get_chat(identifier, limit=limit)
        messages = chat['messages']
        if not messages:
            chat['activity_groups'] = []
            return chat
        lower, upper = messages[0]['id'], messages[-1]['id']
        with self.store._connection(transaction='read') as db:
            rows = db.execute('SELECT id,parent_id,data FROM runs WHERE chat_id=? ORDER BY CAST(json_extract(data,\'$.request_message_id\') AS INTEGER),id', (identifier,)).fetchall()
            own_ids = {row['id'] for row in rows}
            roots = [json.loads(row['data']) for row in rows if row['parent_id'] not in own_ids]
            earlier = [run for run in roots if run.get('request_message_id', 0) < lower]
            visible = ([earlier[-1]] if earlier else []) + [run for run in roots if lower <= run.get('request_message_id', 0) <= upper]
            chat['activity_groups'] = self._groups(db, visible)
            associations = {row['message_id']: row for row in db.execute('SELECT i.id,i.run_id,i.message_id FROM invocations i JOIN messages m ON m.id=i.message_id WHERE m.chat_id=? AND m.id BETWEEN ? AND ?', (identifier, lower, upper))}
        for message in messages:
            if message['id'] in associations:
                invocation = associations[message['id']]
                message.update(invocation_id=invocation['id'], run_id=invocation['run_id'])
        return chat

    def activity(self, data):
        after, limit = data.get('after', 0), data.get('limit', 100)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Use a nonnegative activity cursor and a page size of 1–200.')
        with self.store._connection(transaction='read') as db:
            row = db.execute('SELECT data FROM runs WHERE id=?', (data.get('run_id'),)).fetchone()
            if not row:
                raise ValueError('Run not found.')
            run = json.loads(row[0])
            marks = ','.join('?' for _ in SEMANTIC_EVENTS)
            rows = db.execute('SELECT * FROM run_events WHERE run_id=? AND seq>? AND type IN (' + marks + ') ORDER BY seq LIMIT ?',
                              (run['id'], after, *SEMANTIC_EVENTS, limit + 1)).fetchall()
            events = []
            for event in rows[:limit]:
                value = json.loads(event['payload'])
                events.append({**{k: v for k, v in value.items() if k in EVENT_FIELDS}, 'type': event['type'],
                               'run_id': run['id'], 'seq': event['seq'], 'created_at': event['created_at']})
            summary = self._groups(db, [run])[0]
        return dict(run_id=run['id'], events=events, next_cursor=events[-1]['seq'] if events else after,
                    has_more=len(rows) > limit, status=run['status'], children=summary['children'])

    def diagnostics(self, data):
        run = self.store.run(data.get('run_id'))
        settings = run.get('settings') or {}
        with self.store._connection() as db:
            last = db.execute('SELECT name,status FROM invocations WHERE run_id=? ORDER BY updated_at DESC LIMIT 1', (run['id'],)).fetchone()
        return {'schema': 1, 'run_id': run['id'], 'status': run['status'], 'provider_id': settings.get('provider_id'),
                'requested_model': settings.get('model'), 'actual_model': run.get('returned_model_last'),
                'rounds': run.get('rounds', 0), 'tools': run.get('tools', 0),
                'last_tool': dict(last) if last else None, 'retry_count': run.get('retry_count', 0),
                'attempt': run.get('attempt', 1), 'stop_category': (run.get('failure') or {}).get('category'),
                'http_status': (run.get('failure') or {}).get('http_status'),
                'finish_reason': run.get('last_finish_reason'), 'generation_id': run.get('provider_generation_id'),
                'response_provider': run.get('response_provider')}
