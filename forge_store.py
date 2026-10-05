"""Forge's ordered local migrations, run journal, checkpoints and usage ledger."""
from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo

from storage import Store, _context_setting, _now

DEFAULTS = dict(context=32768, model='', provider_id='ollama', permission_profile='always_ask',
                permission_overrides={}, theme='system', tokens=4096, temperature=0.3,
                thinking=True, web=True, timezone='UTC', performance='balanced', keep_alive='10m',
                auto_compact=True, auto_delegate=False, startup=False, dictation_model='base',allow_edits=True,
                computer_tools=False,browser_tools=True,num_thread=4,
                memory_enabled=True,memory_suggestions=True,memory_semantic=False,memory_model_path='',
                goal_limits={'minutes':60, 'tokens':100000, 'rounds':128, 'tools':256})
TERMINAL = {'completed', 'cancelled', 'failed', 'paused', 'interrupted'}

def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

def validate_goal_limits(limits):
    if not isinstance(limits,dict) or set(limits)-set(DEFAULTS['goal_limits']):
        raise ValueError('Goal limits must contain minutes, tokens, rounds and tools.')
    result={**DEFAULTS['goal_limits'],**limits}
    for key,value in result.items():
        if type(value) is not int or value<1 or value>1_000_000_000:
            raise ValueError('Goal '+key+' limit must be a positive integer.')
    return result

def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as out:
            out.write(text); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class ForgeStore(Store):
    def __init__(self, home=None):
        explicit = home is not None or bool(os.getenv('FORGE_DATA_DIR') or os.getenv('SIDEKICK_DATA_DIR'))
        self.home = Path(home or os.getenv('FORGE_DATA_DIR') or os.getenv('SIDEKICK_DATA_DIR')
                         or Path.home()/'.forge').expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        for directory in ('config','state','attachments','backups','projects','worktrees',
                          'skills','plugins','runtimes','artifacts'):
            (self.home/directory).mkdir(exist_ok=True)
        destination = self.home/'state'/'forge.sqlite3'
        existing_data=destination.exists()
        if not explicit and not destination.exists():
            self._import_sidekick(destination)
            existing_data=destination.exists()
        self.installation_origin='upgraded' if existing_data else 'new'
        super().__init__(self.home/'state', db_name='forge.sqlite3')
        self._goal_lock = threading.RLock()
        self._event_listeners = []
        self._listener_lock = threading.RLock()
        self._migrate()

    def _import_sidekick(self, destination):
        local = os.getenv('LOCALAPPDATA')
        old = Path(local)/'Sidekick' if local else Path.home()/'.local/share/sidekick'
        source = old/'sidekick.sqlite3'
        if not source.is_file(): return
        backup = self.home/'backups'/('pre-upgrade-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
        backup.mkdir(parents=True)
        # SQLite backup includes a consistent WAL snapshot; never copy a live DB file.
        with sqlite3.connect(source) as src, sqlite3.connect(backup/'sidekick.sqlite3') as dst:
            src.backup(dst)
        for name in ('attachments','backups'):
            folder = old/name
            if folder.is_dir():
                for file in folder.rglob('*'):
                    if file.is_symlink(): continue
                    if file.is_file() and file.resolve().is_relative_to(old.resolve()):
                        relative = file.relative_to(folder)
                        target = backup/name/relative; target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file,target)
                        target = self.home/name/relative; target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file,target)
        with sqlite3.connect(backup/'sidekick.sqlite3') as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        atomic_text(backup/'manifest.json',encode({'version':1,'created_at':_now(),
                    'database':'sidekick.sqlite3','source':str(old),'restore':'Stop Forge before restoring this snapshot.'}))

    def _migrate(self):
        with self._connection() as db:
            present=db.execute("SELECT 1 FROM sqlite_master WHERE name='forge_migrations'").fetchone()
            previous=db.execute('SELECT COALESCE(MAX(version),0) FROM forge_migrations').fetchone()[0] if present else 0
        if 0<previous<5:
            backup=self.home/'backups'/('pre-schema-5-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
            backup.mkdir()
            with sqlite3.connect(self.db_path) as src, sqlite3.connect(backup/'forge.sqlite3') as dst: src.backup(dst)
        with self._connection(transaction='write') as db:
            db.execute('CREATE TABLE IF NOT EXISTS forge_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
            version = db.execute('SELECT COALESCE(MAX(version),0) FROM forge_migrations').fetchone()[0]
            if version > 5: raise ValueError('This workspace requires a newer Forge version.')
            if version < 1:
                for statement in (
                    'CREATE TABLE forge_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                    'CREATE TABLE entities(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL, PRIMARY KEY(kind,id))',
                    'CREATE TABLE runs(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,parent_id TEXT,goal_id TEXT,status TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
                    'CREATE INDEX runs_chat ON runs(chat_id,status)',
                    'CREATE TABLE run_events(run_id TEXT NOT NULL REFERENCES runs(id),seq INTEGER NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,seq))',
                    'CREATE TABLE invocations(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),name TEXT NOT NULL,arguments TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
                    'CREATE TABLE usage(id TEXT PRIMARY KEY,run_id TEXT,project_id TEXT,provider TEXT NOT NULL,model TEXT NOT NULL,purpose TEXT NOT NULL,input_tokens INTEGER NOT NULL,cached_input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,estimated INTEGER NOT NULL,decode_seconds REAL NOT NULL,total_seconds REAL NOT NULL,cancelled INTEGER NOT NULL,created_at TEXT NOT NULL)',
                    'CREATE INDEX usage_date ON usage(created_at)',
                    'CREATE TABLE occurrences(schedule_id TEXT NOT NULL,slot TEXT NOT NULL,run_id TEXT,PRIMARY KEY(schedule_id,slot))',
                ): db.execute(statement)
                db.execute('INSERT INTO forge_settings VALUES(?,?)',('tracked_since',encode(_now())))
                db.execute('INSERT INTO forge_migrations VALUES(1,?)',(_now(),))
            if version < 2:
                db.execute('CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,invocation_id TEXT NOT NULL,action_hash TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL)')
                db.execute('INSERT INTO forge_migrations VALUES(2,?)',(_now(),))
            if version < 3:
                db.execute('ALTER TABLE invocations ADD COLUMN message_id INTEGER REFERENCES messages(id)')
                db.execute('INSERT INTO forge_migrations VALUES(3,?)',(_now(),))
            if version < 4:
                db.execute('ALTER TABLE chats ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))')
                db.execute('CREATE INDEX chats_archived_updated ON chats(archived,updated_at)')
                db.execute('INSERT INTO forge_migrations VALUES(4,?)',(_now(),))
            if version < 5:
                from forge_memory import MEMORY_SCHEMA
                from forge_channels import CHANNEL_SCHEMA
                for statement in (*MEMORY_SCHEMA, *CHANNEL_SCHEMA,
                    'CREATE TABLE request_keys(source_key TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id))'):
                    db.execute(statement)
                db.execute('INSERT INTO forge_settings VALUES(?,?) ON CONFLICT(key) DO NOTHING',('setup_origin',encode(self.installation_origin)))
                db.execute('INSERT INTO forge_migrations VALUES(5,?)',(_now(),))
        if not self.entities('agents'):
            for role, instruction in (
                ('Researcher','Research using primary sources. Cite sources and explain uncertainty. Do not edit the project.'),
                ('Coder','Implement the assigned task in your isolated workspace. Preserve existing changes and verify your work.'),
                ('Reviewer','Inspect changes and identify actionable correctness problems. Do not modify files.')):
                self.save_entity('agents',dict(id=role.lower(),name=role,role=role.lower(),instructions=instruction,
                    model='',context=32768,tools=[],skills=[],enabled=True,tokens=25000,rounds=32))

    @staticmethod
    def _chat(connection,chat_id):
        chat=Store._chat(connection,chat_id)
        chat['archived']=bool(chat.get('archived',False))
        return chat

    def list_chats(self,project_id=None,*,archived=False):
        if archived is not None and type(archived) is not bool:
            raise ValueError('Archived filter must be true, false or null.')
        with self._connection() as db:
            where=[]; parameters=[]
            if project_id is not None:
                self._project(db,project_id); where.append('project_id=?'); parameters.append(project_id)
            if archived is not None:
                where.append('archived=?'); parameters.append(int(archived))
            query='SELECT * FROM chats'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY updated_at DESC,id'
            return [{**dict(row),'archived':bool(row['archived'])} for row in db.execute(query,parameters)]

    def store_images(self,images):
        if not isinstance(images,list) or len(images)>1: raise ValueError('Attach one image per turn.')
        references=[]
        for value in images:
            if value.startswith('forge-attachment:'):
                self.hydrate_images([value]); references.append(value); continue
            if not isinstance(value,str) or len(value)>8_000_000: raise ValueError('Image attachment exceeds the limit.')
            try: raw=base64.b64decode(value.split(',',1)[-1],validate=True)
            except (ValueError,TypeError): raise ValueError('Invalid image encoding.') from None
            if len(raw)>6_000_000: raise ValueError('Image attachment exceeds the limit.')
            # Verify pixels before accepting a model attachment; ignore misleading file extensions.
            from PIL import Image
            import io
            try:
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.width*picture.height>20_000_000: raise ValueError('Image dimensions exceed the limit.')
                    picture.verify()
            except (OSError,Image.DecompressionBombError): raise ValueError('Unsupported or invalid image.') from None
            identifier=sha256(raw).hexdigest(); path=self.home/'attachments'/(identifier+'.image')
            if not path.exists():
                temporary=path.with_suffix('.tmp'); temporary.write_bytes(raw); os.replace(temporary,path)
            references.append('forge-attachment:'+identifier)
        return references

    def hydrate_images(self,images):
        hydrated=[]
        for value in images:
            if value.startswith('forge-attachment:'):
                identifier=value.split(':',1)[1]
                if len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier): raise ValueError('Invalid image reference.')
                value=base64.b64encode((self.home/'attachments'/(identifier+'.image')).read_bytes()).decode('ascii')
            hydrated.append(value)
        return hydrated

    def add_message(self,chat_id,role,content,**metadata):
        if metadata.get('images'): metadata['images']=self.store_images(metadata['images'])
        return super().add_message(chat_id,role,content,**metadata)

    def get_chat(self,chat_id,*,limit=None):
        chat=super().get_chat(chat_id,limit=limit)
        for message in chat['messages']:
            if message.get('images'): message['images']=self.store_images(message['images'])
        return chat

    def run_chat(self,chat_id,boundary):
        with self._connection(transaction='read') as db:
            chat=self._chat(db,chat_id)
            chat['messages']=[self._message(row) for row in db.execute('SELECT * FROM messages WHERE chat_id=? AND id>? ORDER BY id',(chat_id,boundary))]
        return chat

    def get_settings(self):
        result = json.loads(encode(DEFAULTS))
        try:
            import psutil
            result['num_thread']=min(64,psutil.cpu_count(logical=False) or 4)
        except ImportError: pass
        try:
            from tzlocal import get_localzone_name
            result['timezone']=get_localzone_name()
        except (ImportError,ValueError,KeyError): pass
        with self._connection() as db:
            result.update(self._settings(db))
            result.update({row['key']:json.loads(row['value']) for row in db.execute('SELECT * FROM forge_settings')})
        return result

    def update_settings(self, settings):
        if not isinstance(settings,dict): raise ValueError('Settings must be an object.')
        if 'context' in settings: _context_setting(settings['context'])
        if 'permission_profile' in settings and settings['permission_profile'] not in ('always_ask','full_access','deny_access'):
            raise ValueError('Unknown permission profile.')
        if 'timezone' in settings: ZoneInfo(settings['timezone'])
        if 'tokens' in settings and settings['tokens'] not in (1024,2048,4096,8192): raise ValueError('Invalid response length.')
        if 'num_thread' in settings and (type(settings['num_thread']) is not int or not 1<=settings['num_thread']<=64): raise ValueError('CPU threads must be between 1 and 64.')
        if 'temperature' in settings and (type(settings['temperature']) not in (float,int) or not 0<=settings['temperature']<=1): raise ValueError('Invalid temperature.')
        for key in ('memory_enabled','memory_suggestions','memory_semantic'):
            if key in settings and type(settings[key]) is not bool: raise ValueError('Memory preferences must be true or false.')
        if 'memory_model_path' in settings and (not isinstance(settings['memory_model_path'],str) or len(settings['memory_model_path'])>1000): raise ValueError('Invalid local embedding directory.')
        if 'goal_limits' in settings: settings={**settings,'goal_limits':validate_goal_limits(settings['goal_limits'])}
        if 'permission_overrides' in settings:
            overrides=settings['permission_overrides']
            if not isinstance(overrides,dict) or any(not isinstance(k,str) or not k.startswith(('tool:','server:','app:','project:')) or v not in ('always_ask','full_access','deny_access') for k,v in overrides.items()):
                raise ValueError('Invalid permission override.')
        encoded=encode(settings)
        if len(encoded)>100000: raise ValueError('Settings are too large.')
        # Secrets are referenced through Credential Manager, never stored here.
        if any(key.lower() in ('token','api_key','password','secret') for key in settings): raise ValueError('Use provider credential storage.')
        with self._connection(transaction='write') as db:
            for key,value in settings.items():
                db.execute('INSERT INTO forge_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(value)))
        return self.get_settings()

    def entities(self,kind):
        with self._connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM entities WHERE kind=? ORDER BY rowid',(kind,))]

    def entity(self,kind,identifier):
        with self._connection() as db:
            row=db.execute('SELECT data FROM entities WHERE kind=? AND id=?',(kind,identifier)).fetchone()
            if not row: raise ValueError(kind.rstrip('s').capitalize()+' not found.')
            return json.loads(row[0])

    def save_entity(self,kind,data):
        data=dict(data); identifier=data.get('id') or uuid4().hex
        if not isinstance(identifier,str) or len(identifier)>64 or not all(c.isalnum() or c in '-_' for c in identifier): raise ValueError('Invalid entity ID.')
        data.update(id=identifier,updated_at=_now())
        if len(encode(data))>2000000: raise ValueError('Entity is too large.')
        with self._connection(transaction='write') as db:
            db.execute('INSERT INTO entities VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',(kind,identifier,encode(data)))
        return data

    def delete_entity(self,kind,identifier):
        with self._connection(transaction='write') as db: db.execute('DELETE FROM entities WHERE kind=? AND id=?',(kind,identifier))

    def create_run(self,data):
        stamp=_now(); data={**data,'id':data.get('id') or uuid4().hex,'status':'queued','created_at':stamp,'updated_at':stamp}
        with self._connection(transaction='write') as db:
            active=db.execute("SELECT id FROM runs WHERE chat_id=? AND status NOT IN ('completed','cancelled','failed','paused','interrupted')",(data['chat_id'],)).fetchone()
            if active: raise ValueError('This chat already has an active run.')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',(data['id'],data['chat_id'],data.get('parent_id'),data.get('goal_id'),'queued',encode(data),stamp,stamp))
        return data

    def accept_request(self,data,chat_id=None,*,source_key=None,channel=None):
        """Commit chat, original request and run together before inference starts."""
        stamp=_now(); data=dict(data)
        images=data.get('images',[])
        metadata=self._metadata({'images':images} if images else {})
        with self._goal_lock, self._connection(transaction='write') as db:
            if source_key:
                if not isinstance(source_key,str) or len(source_key)>500: raise ValueError('Invalid request identity.')
                prior=db.execute('SELECT run_id FROM request_keys WHERE source_key=?',(source_key,)).fetchone()
                if prior:
                    run=json.loads(db.execute('SELECT data FROM runs WHERE id=?',(prior[0],)).fetchone()[0])
                    return run,False
            if chat_id:
                chat=self._chat(db,chat_id)
                if chat.get('archived'): raise ValueError('Restore this archived chat before starting a run.')
                if chat.get('project_id')!=data.get('project_id'): raise ValueError('The chat project changed. Refresh before starting.')
            else:
                if data.get('project_id'): self._project(db,data['project_id'])
                chat_id=uuid4().hex
                db.execute('INSERT INTO chats(id,project_id,title,model,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                    (chat_id,data.get('project_id'),data['request'].splitlines()[0][:70],data['settings']['model'],stamp,stamp))
                chat=self._chat(db,chat_id)
            active=db.execute("SELECT id FROM runs WHERE chat_id=? AND status NOT IN ('completed','cancelled','failed','paused','interrupted')",(chat_id,)).fetchone()
            if active: raise ValueError('This chat already has an active run.')
            if data.get('goal_id') and not data.get('parent_id'):
                row=db.execute("SELECT data FROM entities WHERE kind='goals' AND id=?",(data['goal_id'],)).fetchone()
                if not row: raise ValueError('Goal not found.')
                goal=json.loads(row[0])
                if goal.get('project_missing') or goal.get('project_id')!=data.get('project_id'):
                    raise ValueError('The goal belongs to another or removed project.')
                if goal.get('chat_id') and goal['chat_id']!=chat_id:
                    raise ValueError('This goal already belongs to another conversation.')
                # Bind the goal, chat, request and run in one admission commit.
                # TODO.md contains no chat identity, so its checksum stays valid.
                goal.update(chat_id=chat_id,updated_at=stamp)
                db.execute("UPDATE entities SET data=? WHERE kind='goals' AND id=?",(encode(goal),goal['id']))
            cursor=db.execute('INSERT INTO messages(chat_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)',
                (chat_id,'user',data['request'],metadata,stamp))
            data.update(id=data.get('id') or uuid4().hex,chat_id=chat_id,request_message_id=cursor.lastrowid,
                boundary=chat.get('compacted_through',0),status='queued',created_at=stamp,updated_at=stamp,source_key=source_key)
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',(data['id'],chat_id,data.get('parent_id'),data.get('goal_id'),'queued',encode(data),stamp,stamp))
            db.execute('UPDATE chats SET model=?,updated_at=? WHERE id=?',(data['settings']['model'],stamp,chat_id))
            if source_key: db.execute('INSERT INTO request_keys VALUES(?,?)',(source_key,data['id']))
            if channel:
                changed=db.execute("UPDATE channel_inbound SET status='dispatched',run_id=?,updated_at=? WHERE channel_id=? AND external_id=? AND status IN ('pending','dispatching')",
                    (data['id'],stamp,*channel)).rowcount
                if changed!=1: raise ValueError('The channel request is no longer pending.')
        return data,True

    def run(self,identifier):
        with self._connection() as db:
            row=db.execute('SELECT data FROM runs WHERE id=?',(identifier,)).fetchone()
            if not row: raise ValueError('Run not found.')
            return json.loads(row[0])

    def runs(self,limit=None):
        with self._connection() as db:
            query='SELECT data FROM runs ORDER BY created_at DESC'
            rows=db.execute(query+' LIMIT ?',(limit,)) if limit else db.execute(query)
            return [json.loads(r[0]) for r in rows]

    def update_run(self,identifier,**changes):
        with self._connection(transaction='write') as db:
            row=db.execute('SELECT data FROM runs WHERE id=?',(identifier,)).fetchone()
            if not row: raise ValueError('Run not found.')
            data=json.loads(row[0]); data.update(changes,updated_at=_now())
            db.execute('UPDATE runs SET status=?,data=?,updated_at=? WHERE id=?',(data['status'],encode(data),data['updated_at'],identifier))
        return data

    def finish_run(self,identifier,changes,payload):
        """Publish terminal state and its final event in one journal transaction."""
        with self._connection(transaction='write') as db:
            row=db.execute('SELECT data FROM runs WHERE id=?',(identifier,)).fetchone()
            if not row: raise ValueError('Run not found.')
            data=json.loads(row[0]); data.update(changes,updated_at=_now())
            db.execute('UPDATE runs SET status=?,data=?,updated_at=? WHERE id=?',(data['status'],encode(data),data['updated_at'],identifier))
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?',(identifier,)).fetchone()[0]
            db.execute('INSERT INTO run_events VALUES(?,?,?,?,?)',(identifier,seq,'done',encode(payload),_now()))
            db.execute('INSERT INTO channel_event_journal(run_id,seq) VALUES(?,?)',(identifier,seq))
        event=dict(payload,seq=seq,type='done',run_id=identifier)
        with self._listener_lock: listeners=list(self._event_listeners)
        for listener in listeners:
            try: listener(event)
            except Exception: pass
        return data

    def event(self,identifier,event_type,**payload):
        with self._connection(transaction='write') as db:
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?',(identifier,)).fetchone()[0]
            db.execute('INSERT INTO run_events VALUES(?,?,?,?,?)',(identifier,seq,event_type,encode(payload),_now()))
            db.execute('INSERT INTO channel_event_journal(run_id,seq) VALUES(?,?)',(identifier,seq))
        event=dict(payload,seq=seq,type=event_type,run_id=identifier)
        # Listeners journal local notifications only; never perform network I/O here.
        with self._listener_lock: listeners=list(self._event_listeners)
        for listener in listeners:
            try: listener(event)
            except Exception: pass
        return event

    def subscribe_events(self,listener):
        with self._listener_lock:
            if listener not in self._event_listeners: self._event_listeners.append(listener)

    def unsubscribe_events(self,listener):
        with self._listener_lock:
            if listener in self._event_listeners: self._event_listeners.remove(listener)

    def events(self,identifier,after=0,limit=500):
        if type(after) is not int or after<0: raise ValueError('Invalid event cursor.')
        with self._connection() as db:
            rows=db.execute('SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',(identifier,after,min(1000,limit))).fetchall()
            return [dict(json.loads(r['payload']),seq=r['seq'],type=r['type'],run_id=identifier) for r in rows]

    def invocation(self,identifier,run_id,name,arguments):
        with self._connection(transaction='write') as db:
            row=db.execute('SELECT * FROM invocations WHERE id=?',(identifier,)).fetchone()
            if row: return dict(row)
            stamp=_now()
            db.execute('INSERT INTO invocations(id,run_id,name,arguments,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(identifier,run_id,name,encode(arguments),'prepared',None,stamp,stamp))
        return dict(id=identifier,status='prepared')

    def invocation_state(self,identifier,status,result=None):
        with self._connection(transaction='write') as db:
            db.execute('UPDATE invocations SET status=?,result=?,updated_at=? WHERE id=?',(status,encode(result) if result is not None else None,_now(),identifier))

    def tool_message(self,invocation_id,index,name,content):
        """Append a protocol result and advance its cursor in the same transaction."""
        with self._connection(transaction='write') as db:
            invocation=db.execute('SELECT * FROM invocations WHERE id=?',(invocation_id,)).fetchone()
            if not invocation: raise ValueError('Invocation not found.')
            row=db.execute('SELECT data FROM runs WHERE id=?',(invocation['run_id'],)).fetchone()
            run=json.loads(row[0]); stamp=_now()
            if invocation['message_id'] is None:
                cursor=db.execute('INSERT INTO messages(chat_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)',
                    (run['chat_id'],'tool',content,encode({'tool_name':name,'status':'complete'}),stamp))
                db.execute('UPDATE invocations SET message_id=? WHERE id=?',(cursor.lastrowid,invocation_id))
                db.execute('UPDATE chats SET updated_at=? WHERE id=?',(stamp,run['chat_id']))
            run.update(next_tool=max(index+1,run.get('next_tool',0)),updated_at=stamp)
            db.execute('UPDATE runs SET data=?,updated_at=? WHERE id=?',(encode(run),stamp,run['id']))

    def recover(self):
        with self._connection(transaction='write') as db:
            db.execute("UPDATE invocations SET status='outcome_unknown' WHERE status='running'")
            rows=db.execute("SELECT id,data FROM runs WHERE status NOT IN ('completed','cancelled','failed','paused','interrupted')").fetchall()
            for row in rows:
                data=json.loads(row['data']); data.update(status='interrupted',recovery='Inspect any outcome-unknown tool actions, then Resume.',updated_at=_now())
                db.execute('UPDATE runs SET status=?,data=?,updated_at=? WHERE id=?',('interrupted',encode(data),_now(),row['id']))
        return [r['id'] for r in rows]

    def unknown_actions(self,run_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM invocations WHERE run_id=? AND status='outcome_unknown'",(run_id,))]

    def artifact(self,value):
        identifier=uuid4().hex
        path=self.home/'artifacts'/(identifier+'.json')
        atomic_text(path,encode(value)); return identifier

    def read_artifact(self,identifier,start=0,limit=8000):
        if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier): raise ValueError('Invalid artifact ID.')
        if type(start) is not int or start<0 or type(limit) is not int or not 1<=limit<=16000: raise ValueError('Invalid artifact range.')
        text=(self.home/'artifacts'/(identifier+'.json')).read_text(encoding='utf-8')
        return {'artifact':identifier,'text':text[start:start+limit],'total_characters':len(text),'next':min(start+limit,len(text))}

    def goal(self,identifier):
        result=self.entity('goals',identifier)
        path=self.home/'state/goals'/identifier/'TODO.md'
        text=path.read_text(encoding='utf-8') if path.exists() else ''
        result.update(path=str(path),markdown=text,external_edits=sha256(text.encode()).hexdigest()!=result.get('markdown_hash'))
        return result

    def active_goals(self):
        """List live goal runs whose conversation still exists, retaining history elsewhere."""
        with self._connection() as db:
            rows=db.execute("SELECT e.data,r.id AS run_id,r.status,r.chat_id FROM entities e "
                "JOIN runs r ON r.goal_id=e.id JOIN chats c ON c.id=r.chat_id "
                "WHERE e.kind='goals' AND r.parent_id IS NULL AND c.archived=0 "
                "AND r.status NOT IN ('completed','cancelled','failed','paused','interrupted') "
                "ORDER BY r.created_at DESC").fetchall()
        result=[]; seen=set()
        for row in rows:
            goal=json.loads(row['data'])
            if goal.get('status')=='completed' or goal.get('project_missing') or goal.get('chat_id')!=row['chat_id'] or goal['id'] in seen: continue
            seen.add(goal['id'])
            result.append({**goal,'run_id':row['run_id'],'chat_id':row['chat_id'],'status':row['status']})
        return result

    def save_goal(self,data,*,reconcile=False):
        with self._goal_lock:
            identifier=data.get('id') or uuid4().hex
            # Validate before constructing a path.
            if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier): raise ValueError('Invalid goal ID.')
            try: previous=self.goal(identifier)
            except ValueError: previous={}
            if previous.get('external_edits') and not reconcile: raise ValueError('TODO.md was edited externally. Reconcile or import it before updating.')
            merged={**previous,**data,'id':identifier}; merged.pop('markdown',None); merged.pop('external_edits',None)
            tasks=merged.get('tasks') or []
            text='# '+str(merged.get('title','Goal')).replace('\n',' ')+'\n\n'
            for index,task in enumerate(tasks,1):
                task.setdefault('id',uuid4().hex); task.setdefault('status','pending')
                text+=f"{index}. [{'x' if task['status']=='completed' else ' '}] {task['text']}\n"
                for evidence in task.get('evidence',[]): text+='   - Evidence: '+str(evidence).replace('\n',' ')+'\n'
            text+='\n## Checkpoint\n'+str(merged.get('checkpoint','Not started.'))+'\n\n## Blockers\n'+str(merged.get('blockers','None.'))+'\n\n## Next action\n'+str(merged.get('next_action','Review the ordered tasks.'))+'\n'
            path=self.home/'state/goals'/identifier/'TODO.md'
            merged.update(tasks=tasks,markdown_hash=sha256(text.encode()).hexdigest(),path=str(path))
            # The database journal commits first. Atomic replacement makes recovery detectable.
            self.save_entity('goals',merged); atomic_text(path,text)
            return self.goal(identifier)

    def record_usage(self,record):
        row={**dict(input_tokens=0,cached_input_tokens=0,output_tokens=0,estimated=True,
                 decode_seconds=0,total_seconds=0,cancelled=False,created_at=_now(),project_id=None,run_id=None),**record}
        fields=('id','run_id','project_id','provider','model','purpose','input_tokens','cached_input_tokens','output_tokens','estimated','decode_seconds','total_seconds','cancelled','created_at')
        for key in ('input_tokens','cached_input_tokens','output_tokens'):
            row[key]=max(0,int(row[key] or 0))
        with self._connection(transaction='write') as db:
            db.execute('INSERT OR IGNORE INTO usage VALUES('+','.join('?' for _ in fields)+')',tuple(row[k] for k in fields))

    def usage(self,period='all',timezone_name='UTC',model=None,project_id=None,now=None):
        zone=ZoneInfo(timezone_name); current=(now or datetime.now(timezone.utc)).astimezone(zone)
        with self._connection() as db: rows=[dict(r) for r in db.execute('SELECT * FROM usage ORDER BY created_at')]
        totals=dict(input_tokens=0,output_tokens=0,cached_input_tokens=0,requests=0,estimated_requests=0)
        daily={}; monthly={}; by_model={}; decode=0; output=0
        for row in rows:
            if model and row['model']!=model or project_id and row['project_id']!=project_id: continue
            stamp=datetime.fromisoformat(row['created_at']).astimezone(zone)
            if period=='day' and stamp.date()!=current.date(): continue
            if period=='month' and (stamp.year,stamp.month)!=(current.year,current.month): continue
            buckets=[totals,daily.setdefault(stamp.strftime('%Y-%m-%d'),dict(totals.__class__())),
                     monthly.setdefault(stamp.strftime('%Y-%m'),{}),by_model.setdefault(row['model'],{})]
            for bucket in buckets:
                for key in ('input_tokens','output_tokens','cached_input_tokens'): bucket[key]=bucket.get(key,0)+row[key]
                bucket['requests']=bucket.get('requests',0)+1
                bucket['estimated_requests']=bucket.get('estimated_requests',0)+int(row['estimated'])
            if row['decode_seconds']>0: decode+=row['decode_seconds']; output+=row['output_tokens']
        return dict(totals=totals,daily=[dict(date=k,**v) for k,v in daily.items()],
                    monthly=[dict(month=k,**v) for k,v in monthly.items()],by_model=[dict(model=k,**v) for k,v in by_model.items()],
                    timezone=timezone_name,tracked_since=self.get_settings()['tracked_since'],average_tps=output/decode if decode else None)
