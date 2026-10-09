"""Revisioned Builder briefs, runnable starters and verifiable completion gates."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from uuid import uuid4

from browser_tools import function_schema
from forge_documents import DocumentManager
from forge_previews import PreviewManager
from forge_templates import template_files
from project_tools import ProjectTools

GATES = ('build', 'functional', 'preview', 'artifact')
SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', 'dist', 'build', '.forge'}


def _text(value, label, maximum=12000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{label} must be text of at most {maximum} characters.')
    return value.strip()


def _excerpt_bytes(value, maximum):
    return str(value).encode('utf-8')[:maximum].decode('utf-8', errors='ignore')


BUILDER_REFERENCE_POLICY = (
    'Partial Builder reference, not the full brief. Read acceptance via '
    'builder_read(id,requirement_id,start,limit) before acting; follow next until has_more=false. '
    'Reload after revision changes. Preview: start/wait ready/open/inspect with current snapshots. '
    'Gates need observed evidence; structure does not prove visual/factual quality.\n'
)


class BuilderManager:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.lock = threading.RLock()
        self.previews = PreviewManager(service)
        self.documents = DocumentManager(self.store)

    def _save(self, old, new):
        revision = old.get('revision', 0) + 1
        record = {**new, 'revision': revision}
        snapshot = {**record, 'builder_id': record['id'], 'id': record['id'] + '_' + str(revision)}
        self.store.save_entity('builder_revisions', snapshot)
        return self.store.save_entity('builders', record)

    @staticmethod
    def gate_defaults(builder):
        document = builder.get('deliverable') == 'documents'
        requested_artifact = document or bool(builder.get('outputs')) or any(
            re.search(r'\b(?:export|deliver|generate|create)\b.{0,100}\b(?:pdf|docx|xlsx|csv|document|spreadsheet|report)\b',
                      r['text'] + ' ' + r['acceptance'], re.I) for r in builder.get('requirements', []))
        return {key: {'status': 'not_applicable' if
                     key == 'artifact' and not requested_artifact or
                     key == 'preview' and document or
                     key == 'build' and builder.get('template', 'static') == 'static' and not builder.get('check_commands', {}).get('build')
                     else 'pending', 'evidence': []} for key in GATES}

    def build_requirement(self, builder, previews=None):
        """Discover build applicability from scoped data, without running config."""
        if builder.get('check_commands', {}).get('build'):
            return True, 'The brief declares a build check command.', 'configured'
        if builder.get('template') == 'vite':
            return True, 'The selected app template requires a build check.', 'node'
        backend = builder.get('template') == 'fastapi'
        project = self.store.get_project(builder['project_id'])
        tools = ProjectTools(project['path'], self.store.home / 'backups')
        runtime = None
        try:
            folder = tools._path(builder.get('directory', '.'))
            if not folder.exists():
                return (True, 'The selected app template requires a build check.', 'python') if backend else (
                    False, 'No app build configuration is present.', None)
            folder = tools._path(str(folder), directory=True)
            previews = previews if previews is not None else self.previews.status(project_id=builder['project_id'])['previews']
            for preview in previews:
                if (preview.get('builder_id') == builder['id'] and preview.get('mode') == 'vite' and
                        preview.get('status') in ('starting', 'ready') and
                        tools._path(preview.get('cwd', '.')) == folder):
                    return True, 'The current app uses a Vite preview.', 'node'
            package = tools._path(str(folder / 'package.json'))
            if package.exists():
                runtime = 'node'
                if not package.is_file() or package.stat().st_size > 48000:
                    return True, 'App package configuration needs a bounded build check review.', runtime
                result = tools.read_file(str(package))
                if result['truncated']:
                    return True, 'App package configuration needs a bounded build check review.', runtime
                config = json.loads(result['content'])
                if not isinstance(config, dict):
                    return True, 'App package configuration needs a build check review.', runtime
                scripts = config.get('scripts', {})
                if isinstance(scripts, dict) and isinstance(scripts.get('build'), str) and scripts['build'].strip():
                    return True, 'App package.json declares a build script.', runtime
                for key in ('dependencies', 'devDependencies'):
                    dependencies = config.get(key, {})
                    if isinstance(dependencies, dict) and 'vite' in dependencies:
                        return True, 'App package.json declares a Vite app dependency.', runtime
            for name in ('tsconfig.json', 'vite.config.ts', 'vite.config.js', 'vite.config.mts',
                         'vite.config.mjs', 'vite.config.cts', 'vite.config.cjs'):
                if tools._path(str(folder / name)).is_file():
                    return True, 'App build configuration is present: ' + name + '.', 'node'
        except (OSError, ValueError, UnicodeError):
            # Unreadable or invalid configuration cannot silently waive a build.
            return True, 'App configuration could not be safely inspected; review the build check.', runtime
        return (True, 'The selected app template requires a build check.', 'python') if backend else (
            False, 'No app build configuration is present.', None)

    def refresh_build_gate(self, builder, previews=None):
        required, reason, runtime = self.build_requirement(builder, previews)
        gate = builder['gates']['build']
        updated = {**gate, 'required': required, 'applicability_reason': reason, 'runtime': runtime}
        # Preserve an explicit user waiver until its scoped evidence changes.
        # Automatic defaults must become pending when newly written app files
        # establish a build requirement, even if the brief itself is unchanged.
        if required and gate['status'] == 'not_applicable' and not gate.get('evidence'):
            updated.update(status='pending', evidence=[])
        if updated == gate:
            return builder
        status = 'building' if builder.get('goal_id') else 'draft'
        return {**builder, 'gates': {**builder['gates'], 'build': updated},
                'status': status if updated['status'] == 'pending' else builder['status']}

    def fingerprint(self, builder):
        project = self.store.get_project(builder['project_id'])
        tools = ProjectTools(project['path'], self.store.home / 'backups')
        root = tools._path(builder.get('directory', '.'), directory=True)
        digest = hashlib.sha256()
        count = size = 0
        latest_modified = 0
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                path = Path(folder) / name
                if path.suffix.lower() in ('.sqlite3', '.db', '.log', '.pyc'):
                    continue
                path = tools._path(str(path))
                count += 1
                size += path.stat().st_size
                latest_modified = max(latest_modified, path.stat().st_mtime)
                if count > 5000 or size > 64 * 1024 * 1024:
                    raise ValueError('Builder evidence scope exceeds 5000 files or 64 MiB. Select a smaller app directory.')
                digest.update(path.relative_to(root).as_posix().encode())
                digest.update(b'\0')
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return {'sha256': digest.hexdigest(), 'files': count, 'bytes': size,
                'latest_modified': latest_modified, 'directory': builder.get('directory', '.')}

    def save(self, data):
        with self.lock:
            old = self.store.entity('builders', data['id']) if data.get('id') else {}
            if old and data.get('expected_revision') != old['revision']:
                raise ValueError('Builder brief changed. Reload before saving this revision.')
            project_id = old.get('project_id') or data.get('project_id')
            if not project_id:
                raise ValueError('Select a connected project for this builder.')
            self.store.get_project(project_id)
            if old and data.get('project_id', project_id) != project_id:
                raise ValueError('A builder cannot move to a different project.')
            chat_id = data.get('chat_id', old.get('chat_id'))
            if chat_id and self.store.get_chat(chat_id, limit=1)['project_id'] != project_id:
                raise ValueError('Select a chat belonging to this builder project.')
            requirements = data.get('requirements', old.get('requirements', []))
            if not isinstance(requirements, list) or len(requirements) > 40:
                raise ValueError('Provide at most 40 requirements.')
            normalized, identifiers = [], set()
            for item in requirements:
                if isinstance(item, str):
                    item = {'text': item, 'acceptance': ''}
                if not isinstance(item, dict):
                    raise ValueError('Each requirement needs text and acceptance criteria.')
                identifier = item.get('id') or uuid4().hex[:12]
                if not isinstance(identifier, str) or len(identifier) > 64 or identifier in identifiers:
                    raise ValueError('Requirement IDs must be unique strings.')
                identifiers.add(identifier)
                text = _text(item.get('text', ''), 'Requirement', 2000)
                if not text:
                    raise ValueError('Requirement text is required.')
                normalized.append({'id': identifier, 'text': text,
                    'acceptance': _text(item.get('acceptance', ''), 'Acceptance criteria', 3000)})
            record = {**old, 'id': old.get('id') or uuid4().hex, 'project_id': project_id, 'chat_id': chat_id,
                'title': _text(data.get('title', old.get('title', 'New app')), 'Title', 120) or 'New app',
                'objective': _text(data.get('objective', old.get('objective', '')), 'Objective'),
                'audience': _text(data.get('audience', old.get('audience', '')), 'Audience', 2000),
                'constraints': _text(data.get('constraints', old.get('constraints', '')), 'Constraints', 6000),
                'requirements': normalized, 'status': 'draft', 'created_at': old.get('created_at') or datetime.now(timezone.utc).isoformat(),
                'gates': {key: {'status': 'pending', 'evidence': []} for key in GATES},
                'checks': old.get('checks', []), 'template': old.get('template')}
            deliverable = data.get('deliverable', old.get('deliverable', 'website'))
            if deliverable not in ('website', 'application', 'documents'):
                raise ValueError('Choose website, application or documents deliverable.')
            design = data.get('design', old.get('design', {}))
            if not isinstance(design, dict) or set(design) - {'style', 'colors', 'typography', 'layout', 'reference_notes'}:
                raise ValueError('Use known design brief fields.')
            design = {key: _text(value, 'Design field', 2000) for key, value in design.items()}
            outputs = data.get('outputs', old.get('outputs', []))
            if not isinstance(outputs, list) or len(outputs) > 30 or any(not isinstance(value, str) or len(value) > 1000 for value in outputs):
                raise ValueError('Provide at most 30 expected output paths.')
            output_scope = ProjectTools(self.store.get_project(project_id)['path'], self.store.home / 'backups')
            for output in outputs:
                path = output_scope._path(output)
                if path.is_dir():
                    raise ValueError('Expected outputs must name files, not directories.')
            commands = data.get('check_commands', old.get('check_commands', {}))
            if not isinstance(commands, dict) or set(commands) - {'build', 'functional'} or any(
                not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(not isinstance(part, str) or len(part) > 4000 for part in argv)
                for argv in commands.values()):
                raise ValueError('Check commands must be bounded argument arrays for build and functional gates.')
            directory = data.get('directory', old.get('directory', 'forge-app'))
            ProjectTools(self.store.get_project(project_id)['path'], self.store.home / 'backups')._path(directory)
            record.update(deliverable=deliverable, design=design, outputs=outputs, check_commands=commands,
                          directory=directory, brief_updated_at=datetime.now(timezone.utc).isoformat())
            # Runtime/configuration discovery also establishes build applicability
            # for existing apps and files written after the brief is saved.
            if not record.get('template'):
                record['template'] = 'static'
            brief_keys = ('project_id', 'chat_id', 'title', 'objective', 'audience',
                          'constraints', 'requirements', 'deliverable', 'design',
                          'outputs', 'check_commands', 'directory', 'template')
            if old and all(old.get(key) == record.get(key) for key in brief_keys):
                # Starting an unchanged saved brief must retain its evidence,
                # revision and planner identity.
                return old
            record['gates'] = self.gate_defaults(record)
            record = self.refresh_build_gate(record)
            if old.get('goal_id'):
                record['goal_id'] = old['goal_id']
                goal = self.store.goal(old['goal_id'])
                if goal.get('external_edits'):
                    raise ValueError('Reconcile the externally edited goal checklist before revising this Builder brief.')
            saved = self._save(old, record)
            if old.get('goal_id'):
                prior = {r['id']: r for r in old['requirements']}
                tasks = {task.get('requirement_id'): task for task in goal.get('tasks', []) if task.get('requirement_id')}
                revised = []
                for requirement in saved['requirements']:
                    previous = tasks.get(requirement['id'], {})
                    unchanged = prior.get(requirement['id']) == requirement and old['objective'] == saved['objective'] and old['constraints'] == saved['constraints']
                    revised.append({**previous, 'requirement_id': requirement['id'],
                        'text': requirement['text'] + (' — acceptance: ' + requirement['acceptance'] if requirement['acceptance'] else ''),
                        'status': previous.get('status', 'pending') if unchanged else 'pending',
                        'evidence': previous.get('evidence', []) if unchanged else []})
                revised.append({'text': 'Run relevant checks, inspect the preview where applicable, and attach concrete completion evidence.',
                                'status': 'pending', 'evidence': [], 'builder_checks': True})
                self.store.save_goal({**goal, 'title': saved['title'], 'request': self.brief_text(saved),
                    'tasks': revised, 'status': 'ready' if goal.get('status') == 'completed' else goal.get('status', 'ready'),
                    'builder_id': saved['id'], 'builder_revision': saved['revision'],
                    'checkpoint': f"Builder brief revision {saved['revision']} saved. Preserve unchanged completed requirements and recheck the current app.",
                    'next_action': 'Continue from the first unfinished requirement in the revised Builder brief.'})
            return saved

    def runtime_get(self, identifier):
        """Recorded status only: polling must not hash the project tree."""
        record = self.store.entity('builders', identifier)
        previews = [item for item in self.previews.status(project_id=record['project_id'])['previews']
                    if item.get('builder_id') == identifier]
        previews.sort(key=lambda item: (item.get('created_at', ''), item.get('id', '')), reverse=True)
        goal = self.store.goal(record['goal_id']) if record.get('goal_id') else None
        run = None
        if goal:
            # Admission does not require a denormalized goal.run_id. Read one
            # exact latest parent attempt instead of scanning every saved run.
            with self.store._connection() as db:
                row = db.execute('SELECT data FROM runs WHERE goal_id=? AND chat_id=? AND parent_id IS NULL '
                                 'ORDER BY created_at DESC,id DESC LIMIT 1',
                                 (goal['id'], record.get('chat_id') or goal.get('chat_id'))).fetchone()
            run = json.loads(row['data']) if row else None
            if run:
                if hasattr(self.service, 'goal_view'):
                    goal = self.service.goal_view(goal, [run])
                else:
                    goal = {**goal, 'run_id': run['id'], 'execution_mode': run.get('execution_mode'),
                            'planner_assignment': run.get('planner_assignment'),
                            'progress_summary': run.get('progress_summary')}
        return {'id': identifier, 'revision': record['revision'], 'status': record['status'],
                'chat_id': record.get('chat_id'), 'goal_id': record.get('goal_id'),
                'goal': goal, 'run': run, 'previews': previews,
                'gates': record.get('gates', {}), 'checks': record.get('checks', []),
                'freshness': 'recorded', 'freshness_note':
                'Recorded checks only. Refresh checks to verify current project files.'}

    def get(self, identifier):
        record = self.store.entity('builders', identifier)
        previews = self.previews.status(project_id=record['project_id'])['previews']
        refreshed = self.refresh_build_gate(record, previews)
        if refreshed != record:
            record = self.store.save_entity('builders', refreshed)
        revisions = [r for r in self.store.entities('builder_revisions') if r['builder_id'] == identifier]
        artifacts = [self.documents.get_artifact(r['id']) for r in self.store.entities('builder_artifacts') if r.get('builder_id') == identifier]
        if any(g.get('fingerprint') for g in record['gates'].values()):
            fingerprint = self.fingerprint(record)
            gates = {key: ({'status': 'pending', 'evidence': [], 'stale': True} if value.get('fingerprint') and
                          value['fingerprint'] != fingerprint['sha256'] else value) for key, value in record['gates'].items()}
            if gates != record['gates']:
                record = self.store.save_entity('builders', {**record, 'gates': gates, 'status': 'building' if record.get('goal_id') else 'draft'})
        artifact_gate = record['gates'].get('artifact', {})
        if artifact_gate.get('status') == 'passed':
            promised, missing = self.output_artifacts(record, artifacts)
            references = [ref for evidence in artifact_gate.get('evidence', []) for ref in evidence.get('artifacts', [evidence]) if ref.get('id')]
            current_ids = {item['id'] for item in artifacts if item.get('current_verified')}
            if missing or any(ref['id'] not in current_ids for ref in references):
                record = self.store.save_entity('builders', {**record, 'gates': {**record['gates'],
                    'artifact': {'status': 'pending', 'stale': True, 'evidence': [], 'missing_outputs': missing}},
                    'status': 'building' if record.get('goal_id') else 'draft'})
        return {**record, 'previews': [r for r in previews if r.get('builder_id') == identifier],
                'revisions': [{'revision': r['revision'], 'updated_at': r['updated_at']} for r in revisions][-20:],
                'artifacts': artifacts[-30:], 'complete': all(g['status'] in ('passed', 'not_applicable') for g in record['gates'].values())}

    def output_artifacts(self, builder, artifacts=None):
        project = self.store.get_project(builder['project_id'])
        tools = ProjectTools(project['path'], self.store.home / 'backups')
        artifacts = artifacts if artifacts is not None else [self.documents.get_artifact(item['id'])
            for item in self.store.entities('builder_artifacts') if item.get('builder_id') == builder['id']]
        by_path = {}
        for item in artifacts:
            if item.get('current_verified'):
                key = os.path.normcase(str(tools._path(item['path'])))
                by_path[key] = item
        selected, missing = [], []
        for output in builder.get('outputs', []):
            key = os.path.normcase(str(tools._path(output)))
            item = by_path.get(key)
            if item:
                selected.append(item)
            else:
                missing.append(output)
        return selected, missing

    def read(self, data):
        """Keep full-record compatibility; optional pages are explicit UTF-8 JSON text."""
        builder = self.get(data['id'])
        if data.get('document_ids'):
            # Model reads retain omitted run-reference IDs near the beginning of
            # a full-result artifact or brief page, without injecting contents.
            builder = {'document_ids': data['document_ids'], **builder}
        if not any(key in data for key in ('requirement_id', 'start', 'limit')):
            return builder
        requirement_id = data.get('requirement_id')
        if requirement_id is not None:
            if not isinstance(requirement_id, str):
                raise ValueError('Choose a requirement ID from the current Builder brief.')
            payload = next((item for item in builder['requirements'] if item['id'] == requirement_id), None)
            if payload is None:
                raise ValueError('Requirement ID is not present in this Builder revision.')
        else:
            payload = builder
        start, limit = data.get('start', 0), data.get('limit', 2000)
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 48000:
            raise ValueError('Builder pages need a nonnegative character offset and a limit from 1 to 48000.')
        content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        stop = min(len(content), start + limit)
        return {'id': builder['id'], 'builder_id': builder['id'], 'revision': builder['revision'],
                'requirement_id': requirement_id, 'format': 'json', 'scope': 'requirement' if requirement_id else 'brief',
                'start': start, 'next': stop, 'total_characters': len(content), 'has_more': stop < len(content),
                'partial': start > 0 or stop < len(content), 'text': content[start:stop]}

    def template(self, data):
        with self.lock:
            builder = self.store.entity('builders', data['id'])
            if data.get('expected_revision') != builder['revision']:
                raise ValueError('Builder brief changed. Reload before creating its starter.')
            kind = data.get('template', 'static')
            files = template_files(kind, builder['title'])
            project = self.store.get_project(builder['project_id'])
            tools = ProjectTools(project['path'], self.store.home / 'backups')
            directory = data.get('directory', 'forge-app')
            target = tools._path(directory)
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError('Choose an empty project directory. Templates never overwrite existing files.')
            tools.make_directory(directory)
            results = []
            for name, content in files.items():
                results.append(tools.write_file(str(target / name), content, expected_sha256='missing'))
            digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            record = self.store.save_entity('builders', {**builder, 'template': kind,
                'directory': str(target.relative_to(Path(project['path']).resolve())), 'template_sha256': digest,
                'gates': self.gate_defaults({**builder, 'template': kind})})
            return {'ok': True, 'builder': record, 'files': results,
                    'next_action': 'Start preview.' if kind == 'static' else 'Install the listed project dependencies explicitly, then start preview.'}

    def start_goal(self, data):
        with self.lock:
            builder = self.store.entity('builders', data['id'])
            if data.get('expected_revision') != builder['revision']:
                raise ValueError('Builder brief changed. Reload before starting its goal.')
            if not builder['objective'] or not builder['requirements']:
                raise ValueError('Save an objective and at least one requirement before building.')
            if builder.get('goal_id'):
                return self.service.goal_resume({'id': builder['goal_id'],
                    **{key: data[key] for key in ('source_key', 'client_submission_id') if data.get(key)}})
            chat_id = builder.get('chat_id')
            if not chat_id:
                chat = self.store.create_chat(builder['project_id'], builder['title'], self.store.get_settings().get('model', ''))
                chat_id = chat['id']
            tasks = [{'text': r['text'] + (' — acceptance: ' + r['acceptance'] if r['acceptance'] else ''), 'requirement_id': r['id'],
                      'status': 'pending', 'evidence': []} for r in builder['requirements']]
            tasks.append({'text': 'Run relevant checks, inspect the preview where applicable, and attach concrete completion evidence.',
                          'status': 'pending', 'evidence': [], 'builder_checks': True})
            goal = self.service.goal_create({'title': builder['title'], 'text': self.brief_text(builder),
                'tasks': tasks, 'project_id': builder['project_id'], 'chat_id': chat_id})
            self.store.save_goal({**goal, 'builder_id': builder['id'], 'builder_revision': builder['revision']})
            self.store.save_entity('builders', {**builder, 'chat_id': chat_id, 'goal_id': goal['id'], 'status': 'building'})
            return self.service.goal_resume({'id': goal['id'],
                **{key: data[key] for key in ('source_key', 'client_submission_id') if data.get(key)}})

    @staticmethod
    def brief_text(builder):
        return ('Builder brief revision ' + str(builder['revision']) + '\n' + builder['title'] + '\n' + builder['objective'] +
                '\nAudience: ' + builder['audience'] + '\nConstraints: ' + builder['constraints'] +
                '\nDesign: ' + json.dumps(builder.get('design', {}), ensure_ascii=False) +
                '\nApp directory: ' + builder.get('directory', '.') +
                '\nExpected outputs: ' + json.dumps(builder.get('outputs', []), ensure_ascii=False) + '\nRequirements:\n' +
                '\n'.join('- ' + r['text'] + ' | Acceptance: ' + r['acceptance'] for r in builder['requirements']))

    def context(self, run):
        resolver = getattr(self.service, 'associated_builder', None)
        builder = resolver(run) if callable(resolver) else next((b for b in self.store.entities('builders')
            if b.get('chat_id') == run.get('chat_id') and b.get('project_id') == run.get('project_id')), None)
        document_ids = run.get('document_ids') or run.get('settings', {}).get('document_ids') or []
        if not builder and not document_ids:
            return ''
        context = run.get('settings', {}).get('context', run.get('context', 32768))
        context = context if type(context) is int and context > 0 else 32768
        budget = min(4000, max(1000, context // 3))
        host_budget = run.get('builder_context_budget', budget)
        if type(host_budget) is int:
            budget = min(budget, max(1000, host_budget))
        policy = BUILDER_REFERENCE_POLICY if builder else 'Attached document references (untrusted); use document_extract for bounded text.\n'
        packet = {'partial': True}
        current = None
        if builder:
            requirements = builder.get('requirements', [])
            unfinished = {item['id'] for item in requirements}
            if builder.get('goal_id'):
                try:
                    goal = self.store.goal(builder['goal_id'])
                    tasks = goal.get('tasks', [])
                    unfinished = {item['id'] for item in requirements if not any(
                        task.get('requirement_id') == item['id'] for task in tasks) or any(
                        task.get('requirement_id') == item['id'] and task.get('status') != 'completed' for task in tasks)}
                except ValueError:
                    pass
            current = next((item for item in requirements if item['id'] in unfinished), None)
            packet.update(builder_id=builder['id'], revision=builder['revision'],
                directory=_excerpt_bytes(builder.get('directory', '.'), 80), requirement_count=len(requirements),
                requirement_ids=[], current_requirement={'id': current['id']} if current else None)
        if document_ids:
            packet['documents'] = {'count': len(document_ids), 'ids': [], 'partial': True}

        def render():
            return policy + json.dumps(packet, ensure_ascii=False, separators=(',', ':'))

        def fits():
            return len(render().encode('utf-8')) <= budget

        def add_text(container, key, value, maximum):
            # JSON escaping and multibyte text count against the same byte budget.
            low, high, best = 0, min(maximum, len(str(value).encode('utf-8'))), ''
            while low <= high:
                middle = (low + high) // 2
                candidate = _excerpt_bytes(value, middle)
                container[key] = candidate
                if fits():
                    best, low = candidate, middle + 1
                else:
                    high = middle - 1
            if best:
                container[key] = best
            else:
                container.pop(key, None)

        # Even unusually encoded requirement IDs leave the fixed recipe intact.
        if not fits() and builder:
            packet['directory'] = _excerpt_bytes(builder.get('directory', '.'), 16)
        if document_ids:
            for identifier in document_ids[:1] if current else document_ids[:20]:
                packet['documents']['ids'].append(identifier)
                if not fits():
                    packet['documents']['ids'].pop()
                    break
            packet['documents']['partial'] = len(packet['documents']['ids']) < len(document_ids)
        if current:
            add_text(packet['current_requirement'], 'acceptance_excerpt', current['acceptance'], budget // 5)
            add_text(packet['current_requirement'], 'text_excerpt', current['text'], budget // 12)
        if builder:
            packet['design_excerpt'] = {}
            if not fits():
                packet.pop('design_excerpt')
            else:
                for key, value in builder.get('design', {}).items():
                    add_text(packet['design_excerpt'], key, value, 80)
                if not packet['design_excerpt']:
                    packet.pop('design_excerpt')
            # Current identity is already pinned, so list remaining IDs in order.
            for item in requirements:
                packet['requirement_ids'].append(item['id'])
                if not fits():
                    packet['requirement_ids'].pop()
                    break
            packet['requirement_ids_partial'] = len(packet['requirement_ids']) < len(requirements)
            if not fits():
                packet.pop('requirement_ids_partial')
            add_text(packet, 'objective_excerpt', builder.get('objective', ''), 160)
        if document_ids and current:
            for identifier in document_ids[len(packet['documents']['ids']):20]:
                packet['documents']['ids'].append(identifier)
                if not fits():
                    packet['documents']['ids'].pop()
                    break
            packet['documents']['partial'] = len(packet['documents']['ids']) < len(document_ids)
        return render()

    def check(self, data, human=False):
        with self.lock:
            builder = self.store.entity('builders', data['id'])
            if data.get('expected_revision') != builder['revision']:
                raise ValueError('Builder brief changed. Run checks for its current revision.')
            builder = self.refresh_build_gate(builder)
            gate, status = data.get('gate'), data.get('status')
            if gate not in GATES or status not in ('passed', 'failed', 'not_applicable'):
                raise ValueError('Choose a known gate and passed, failed or not_applicable status.')
            source, reference = data.get('source'), data.get('reference')
            note = _text(data.get('note', ''), 'Check note', 3000)
            if source == 'manual':
                if not human or not note:
                    raise ValueError('Manual checks require a direct user action and an explanation.')
                evidence = {'source': source, 'note': note}
            elif source == 'session':
                session = self.service.command_sessions.status(reference)
                root = str(Path(self.store.get_project(builder['project_id'])['path']).resolve())
                session_root = str(Path(session.get('project_root', session.get('root', ''))).resolve())
                if session_root != root or session.get('status') != 'completed':
                    raise ValueError('Use a completed command session from this project.')
                if status == 'passed' and session.get('exit_code') != 0:
                    raise ValueError('A failed command cannot pass a gate.')
                if status == 'not_applicable':
                    raise ValueError('Only the user can mark a gate not applicable.')
                argv = session.get('argv', [])
                configured = builder.get('check_commands', {}).get(gate)
                executable = Path(argv[0]).stem.lower() if argv else ''
                tail = argv[1:]
                package_manager = executable in ('npm', 'pnpm', 'yarn', 'bun')
                package_build = package_manager and (tail[:2] == ['run', 'build'] or
                    executable != 'npm' and tail[:1] == ['build'])
                package_test = package_manager and (tail[:1] == ['test'] or tail[:2] == ['run', 'test'])
                compiler, compiler_args = executable, tail
                if executable == 'npx' and tail[:1] == ['--no-install'] and len(tail) > 1:
                    compiler, compiler_args = Path(tail[1]).stem.lower(), tail[2:]
                informational = any(part in ('--help', '-h', '--version', '-v', '--showConfig', '--listFilesOnly', '--init')
                                    for part in compiler_args)
                package_build = package_build and not informational
                package_test = package_test and not informational
                node_build = ((compiler == 'vite' and compiler_args[:1] == ['build']) or compiler == 'tsc') and not informational
                python_build = (executable.startswith('python') and tail[:3] == ['-m', 'compileall', '-q'] and
                    builder['gates']['build'].get('runtime') != 'node')
                known_build = gate == 'build' and (package_build or node_build or python_build)
                known_test = gate == 'functional' and ((executable.startswith('python') and tail[:2] == ['-m', 'pytest']) or
                    executable == 'pytest' or package_test or
                    executable == 'node' and tail[:1] == ['--test'] or executable in ('cargo', 'go') and tail[:1] == ['test'])
                if gate not in ('build', 'functional') or (configured and argv != configured) or (not configured and not known_build and not known_test):
                    raise ValueError('Use the brief-configured check command or a recognized build/test command for this gate.')
                brief_time = datetime.fromisoformat(builder['brief_updated_at']).timestamp()
                if not isinstance(session.get('created_at'), (int, float)) or session['created_at'] < brief_time:
                    raise ValueError('This command predates the saved brief. Run it again for the current revision.')
                working = Path(root) / session.get('cwd', '.')
                scope = Path(root) / builder.get('directory', '.')
                if not working.resolve().is_relative_to(scope.resolve()):
                    raise ValueError('Run the check inside the Builder app directory.')
                evidence = {'source': source, 'id': reference, 'exit_code': session.get('exit_code'), 'argv': session.get('argv')}
            elif source == 'preview':
                preview = self.previews.status(reference)
                if preview.get('builder_id') != builder['id'] or preview['status'] != 'ready' or gate != 'preview' or status != 'passed':
                    raise ValueError('A ready owned preview can pass only the preview availability gate.')
                evidence = {'source': source, 'id': reference, 'url': preview['url']}
            elif source == 'artifact':
                artifact = self.documents.get_artifact(reference)
                if artifact.get('builder_id') != builder['id'] or gate != 'artifact' or status != 'passed':
                    raise ValueError('Use a verified artifact belonging to this builder for the artifact gate.')
                if not artifact.get('current_verified'):
                    raise ValueError('Artifact changed. Verify it again before using this evidence.')
                evidence = {'source': source, 'id': reference, 'sha256': artifact['sha256'], 'visual_verified': False}
            else:
                raise ValueError('Choose manual, session, preview or artifact evidence.')
            if gate == 'artifact' and builder.get('outputs'):
                if status == 'not_applicable':
                    raise ValueError('This brief declares exported files. Revise its expected outputs before waiving the artifact gate.')
                if status == 'passed':
                    artifacts, missing = self.output_artifacts(builder)
                    if missing:
                        raise ValueError('Verify every expected output before passing the artifact gate: ' + ', '.join(missing))
                    evidence['artifacts'] = [{'id': item['id'], 'path': item['path'], 'sha256': item['sha256']} for item in artifacts]
            entry = {'gate': gate, 'status': status, 'revision': builder['revision'], 'evidence': evidence,
                     'note': note, 'created_at': datetime.now(timezone.utc).isoformat()}
            fingerprint = self.fingerprint(builder)
            if source == 'session' and fingerprint['latest_modified'] > session['created_at'] + .001:
                raise ValueError('App files changed after this check started. Run the check again before recording it.')
            evidence = {**evidence, 'kind': {'manual': 'human_inspection', 'session': 'command_result',
                'preview': 'preview_availability', 'artifact': 'document_structure'}[source],
                'project_id': builder['project_id'], 'builder_id': builder['id'], 'brief_revision': builder['revision'],
                'scope_sha256': fingerprint['sha256']}
            entry['evidence'] = evidence
            gates = {**builder['gates'], gate: {'status': status, 'evidence': [evidence], 'fingerprint': fingerprint['sha256']}}
            complete = all(value['status'] in ('passed', 'not_applicable') for value in gates.values())
            return self.store.save_entity('builders', {**builder, 'gates': gates, 'checks': (builder.get('checks', []) + [entry])[-100:],
                                                     'status': 'verified' if complete else builder['status']})

    def changes(self, identifier):
        builder = self.store.entity('builders', identifier)
        project = self.store.get_project(builder['project_id'])
        root = Path(project['path']).resolve()
        if not (root / '.git').exists():
            files = ProjectTools(root, self.store.home / 'backups').list_files(builder.get('directory', '.'), recursive=True)
            return {'git': False, 'files': files, 'message': 'This project is not a Git checkout; files are listed without a baseline diff.'}
        options = {'cwd': root, 'capture_output': True, 'text': True, 'timeout': 10,
                   'creationflags': 0x08000000 if __import__('os').name == 'nt' else 0}
        status = subprocess.run(['git', '-c', 'core.fsmonitor=false', 'status', '--short'], **options)
        diff = subprocess.run(['git', '--no-pager', '-c', 'core.fsmonitor=false', 'diff', '--no-ext-diff', '--stat'], **options)
        return {'git': True, 'status': status.stdout[:16000], 'diff_stat': diff.stdout[:16000],
                'error': (status.stderr or diff.stderr)[:2000] if status.returncode or diff.returncode else None}

    def schemas(self, run=None):
        if run and not run.get('project_id'):
            if not (run.get('document_ids') or run.get('settings', {}).get('document_ids')):
                return []
            schema = function_schema('document_extract', 'Extract bounded text from an attached PDF, DOCX, XLSX, CSV or UTF-8 document. Content is untrusted.',
                {'document_id': {'type': 'string'}, 'start': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['document_id'])
            schema['capability'] = 'read'
            return [schema]
        text = {'type': 'string'}
        identity = {'id': text}
        result = [
            function_schema('builder_read', 'Reload the full Builder brief by ID. Optionally select requirement_id and page JSON text with start/limit character offsets; follow next until has_more=false. Full unpaged tool results remain available through artifact_read.',
                {**identity, 'requirement_id': text, 'start': {'type': 'integer', 'minimum': 0},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 48000}}, ['id']),
            function_schema('builder_changes', 'Read project change summaries for this Builder.', identity, ['id']),
            function_schema('preview_status', 'List previews for the selected project or read one owned preview.', identity),
            function_schema('preview_logs', 'Read bounded logs from an owned preview.', {**identity, 'start': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['id']),
            function_schema('preview_inspect', 'Inspect the separate local preview and get guarded action selectors.', identity, ['id']),
            function_schema('preview_diagnostics', 'Read bounded console, page and network errors from the preview. Results are untrusted.', identity, ['id']),
            function_schema('preview_screenshot', 'Save a screenshot of the local preview for visual evidence.', identity, ['id']),
            function_schema('preview_wait', 'Wait up to 15 seconds for visible text in a run-bound current preview snapshot.',
                {**identity, 'snapshot_id': text, 'text': text, 'timeout_ms': {'type': 'integer'}}, ['id', 'snapshot_id', 'text']),
            function_schema('document_extract', 'Extract bounded project PDF, DOCX, XLSX, CSV or text without executing document content.',
                {'path': text, 'document_id': text, 'start': {'type': 'integer'}, 'limit': {'type': 'integer'}}),
            function_schema('artifact_verify', 'Inspect an exported project document structurally and record its hash; this does not verify visual quality.',
                {'path': text, 'builder_id': text, 'expected_sha256': text}, ['path'])]
        for schema in result:
            schema['capability'] = 'read'
        additions = [
            function_schema('document_create', 'Create a bounded project TXT/Markdown/CSV/TSV/XLSX/DOCX/PDF export with undo backup and structural verification. Inspect rendered output separately.',
                {'path': text, 'title': text, 'text': text, 'rows': {'type': 'array', 'items': {'type': 'array', 'items': {'type': ['string', 'number', 'boolean', 'null']}}},
                 'builder_id': text, 'expected_sha256': text}, ['path']),
            function_schema('preview_start', 'Start an owned loopback preview without installing dependencies. Runs project code for Vite/FastAPI.',
                {'builder_id': text, 'cwd': text, 'mode': {'type': 'string', 'enum': ['auto', 'static', 'vite', 'fastapi']}, 'entrypoint': text}),
            function_schema('preview_stop', 'Stop only this coordinator-owned preview and its process tree.', identity, ['id']),
            function_schema('preview_open', 'Show the separate Builder preview pane before inspecting visual behavior.', identity, ['id']),
            function_schema('preview_viewport', 'Set a preview viewport within the visible pane; omit dimensions to fit the pane.',
                {**identity, 'width': {'type': 'integer'}, 'height': {'type': 'integer'}}, ['id']),
            function_schema('preview_click', 'Click a target returned by preview_inspect; snapshot is run and navigation bound.',
                {**identity, 'selector': text, 'snapshot_id': text}, ['id', 'selector', 'snapshot_id']),
            function_schema('preview_type', 'Replace a preview text field from its current guarded snapshot.',
                {**identity, 'selector': text, 'snapshot_id': text, 'text': text}, ['id', 'selector', 'snapshot_id', 'text']),
            function_schema('preview_select', 'Choose an enabled dropdown option in the current guarded preview snapshot.',
                {**identity, 'selector': text, 'snapshot_id': text, 'value': text}, ['id', 'selector', 'snapshot_id', 'value']),
            function_schema('preview_key', 'Dispatch a supported key event to an inspected preview target; Enter submits its form where applicable.',
                {**identity, 'selector': text, 'snapshot_id': text, 'key': text}, ['id', 'selector', 'snapshot_id', 'key']),
            function_schema('preview_scroll', 'Scroll the current guarded preview by at most 1500 CSS pixels, then inspect again.',
                {**identity, 'snapshot_id': text, 'x': {'type': 'integer'}, 'y': {'type': 'integer'}}, ['id', 'snapshot_id']),
            function_schema('builder_record_check', 'Record a gate only using completed session, ready preview or verified artifact evidence.',
                {**identity, 'expected_revision': {'type': 'integer'}, 'gate': {'type': 'string', 'enum': list(GATES)},
                 'status': {'type': 'string', 'enum': ['passed', 'failed']}, 'source': {'type': 'string', 'enum': ['session', 'preview', 'artifact']},
                 'reference': text, 'note': text}, ['id', 'expected_revision', 'gate', 'status', 'source', 'reference'])]
        for schema in additions:
            schema['capability'] = 'journal' if schema['function']['name'] == 'builder_record_check' else 'write' if schema['function']['name'] == 'document_create' else 'command' if schema['function']['name'] in ('preview_start', 'preview_stop') else 'computer'
        schemas = result + additions
        if run and run.get('workspace_project_id') and run['workspace_project_id'] != run.get('project_id'):
            schemas = [schema for schema in schemas if not schema['function']['name'].startswith('preview_') and
                       schema['function']['name'] != 'builder_record_check']
        return schemas

    def execute(self, run, name, args, cancel=None):
        if cancel and cancel.is_set():
            return {'ok': False, 'not_executed': True, 'cancelled': True}
        data = {**args, 'project_id': run.get('project_id'), 'run_id': run.get('id')}
        workspace_id = run.get('workspace_project_id') or run.get('project_id')
        worktree = workspace_id != run.get('project_id')
        if worktree and (name.startswith('preview_') or name == 'builder_record_check'):
            raise ValueError('Worktree helpers cannot control the original project preview or completion gates. Return their evidence to the parent executor.')
        # Tool-supplied IDs cannot escape the connected project or act on
        # another run's preview. Interactive API calls are explicitly human.
        kind = 'builders' if name.startswith('builder_') else 'previews' if name.startswith('preview_') and args.get('id') else None
        if kind:
            record = self.store.entity(kind, args['id'])
            if record['project_id'] != run.get('project_id'):
                raise ValueError('This Builder or preview belongs to another project.')
        if args.get('builder_id'):
            builder = self.store.entity('builders', args['builder_id'])
            if builder['project_id'] != run.get('project_id'):
                raise ValueError('This Builder belongs to another project.')
        if args.get('document_id') and args['document_id'] not in (run.get('document_ids') or run.get('settings', {}).get('document_ids') or []):
            raise ValueError('This document is not attached to the current run.')
        if worktree and name in ('document_create', 'document_extract', 'artifact_verify'):
            data['project_id'] = workspace_id
            # A worktree artifact remains a worktree result until integration;
            # it must not masquerade as verified parent-Builder output.
            data.pop('builder_id', None)
            result = self.dispatch(name, data)
            return {**result, 'workspace_project_id': workspace_id, 'original_project_id': run.get('project_id')}
        if name in ('preview_inspect', 'preview_screenshot', 'preview_diagnostics', 'preview_click', 'preview_type', 'preview_key', 'preview_select', 'preview_scroll', 'preview_wait'):
            return self.previews.browser_action(name.removeprefix('preview_'), data, {'run_id': run['id'], 'cancel': cancel})
        if name in ('preview_open', 'preview_viewport'):
            return self.previews.browser_action('show' if name == 'preview_open' else 'viewport', data, {'run_id': run['id'], 'cancel': cancel})
        if name == 'builder_record_check':
            return self.check(data, human=False)
        if name == 'builder_read':
            return self.read({**data, 'document_ids': run.get('document_ids') or run.get('settings', {}).get('document_ids') or []})
        return self.dispatch('builder_get' if name == 'builder_read' else name, data)

    def dispatch(self, action, data=None):
        if action == 'builder_runtime_get': return self.runtime_get((data or {})['id'])
        data = data or {}
        if action == 'builder_list':
            return {'builders': [b for b in self.store.entities('builders') if not data.get('project_id') or b['project_id'] == data['project_id']]}
        if action in ('builder_get', 'builder_read'): return self.read(data)
        if action == 'builder_save': return self.save(data)
        if action == 'builder_template': return self.template(data)
        if action == 'builder_start_goal': return self.start_goal(data)
        if action in ('builder_check', 'builder_record_check'): return self.check(data, human=True)
        if action == 'builder_changes': return self.changes(data['id'])
        if action == 'preview_start':
            builder = self.store.entity('builders', data['builder_id']) if data.get('builder_id') else None
            return self.previews.start({**data, **({'project_id': builder['project_id'], 'cwd': data.get('cwd', builder.get('directory', '.'))} if builder else {})})
        if action == 'preview_stop': return self.previews.stop(data['id'])
        if action == 'preview_status': return self.previews.status(data.get('id'), data.get('project_id'))
        if action == 'preview_logs': return self.previews.logs(data['id'], data.get('start', 0), data.get('limit', 8000))
        if action.startswith('preview_browser_'):
            operation = action.removeprefix('preview_browser_')
            return self.previews.browser_action('screenshot' if operation == 'capture' else operation, data)
        if action == 'document_extract': return self.documents.extract(data)
        if action == 'document_upload': return self.documents.upload(data)
        if action == 'document_list': return self.documents.list(data)
        if action == 'document_create': return self.documents.create(data)
        if action == 'artifact_download': return self.documents.download(data)
        if action == 'artifact_list': return self.documents.artifacts(data)
        if action == 'artifact_get': return self.documents.get_artifact(data['id'])
        if action == 'artifact_verify': return self.documents.verify(data)
        raise ValueError('Unknown Builder operation.')

    def shutdown(self):
        self.previews.shutdown()

    def stop(self):
        for identifier in list(self.previews.active):
            self.previews.stop(identifier)
        if self.previews.browser:
            try:
                self.previews.browser.stop()
            except Exception:
                pass  # Owned processes have already been stopped; UI faults cannot abort emergency cleanup.
        return {'ok': True}

    def close_project(self, project_id):
        for identifier, item in list(self.previews.active.items()):
            if item['record']['project_id'] == project_id:
                self.previews.stop(identifier)
        for builder in self.store.entities('builders'):
            if builder['project_id'] == project_id:
                self.store.save_entity('builders', {**builder, 'status': 'project_missing', 'project_missing': True})
        return {'ok': True}
