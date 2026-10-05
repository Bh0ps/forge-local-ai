"""Resumable first-run setup. No downloads, warm-ups or preference changes at startup."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

import httpx

from context_window import advertised_context_limit, require_model_context, validate_context
from forge_inference import ENGINE_PROFILES, OllamaProvider
from forge_performance import hardware
from forge_store import TERMINAL
from inference_stream import bounded_lines
from storage import _now


MODEL_CATALOG = (
    dict(name='qwen3.5:0.8b', size_bytes=1_400_000_000, title='Quick setup test', description='Small vision and tool model; limited coding ability.'),
    dict(name='qwen3.5:4b', size_bytes=4_000_000_000, title='Light local workspace', description='Vision and tools with a smaller memory footprint.'),
    dict(name='qwen3.5:9b', size_bytes=7_600_000_000, title='Balanced local workspace', description='More room for coding quality; requires more memory.'),
)


class SetupManager:
    def __init__(self, service, client_factory=None, runtime_client_factory=None, browser_factory=None):
        self.service=service; self.store=service.store; self.lock=threading.RLock(); self.jobs={}
        self.client_factory=client_factory or httpx.AsyncClient
        self.runtime_client_factory=runtime_client_factory or httpx.Client
        self.browser_factory=browser_factory
        for job in self.store.entities('setup_jobs'):
            if job.get('status') in ('queued','running','cancelling'):
                self.store.save_entity('setup_jobs',{**job,'status':'interrupted','error':'Setup stopped. Retry this step; your selected settings are retained.'})

    def status(self):
        states=self.store.entities('setup')
        state=next((s for s in states if s['id']=='first-run'),{})
        settings=self.store.get_settings()
        origin=state.get('origin') or settings.get('setup_origin')
        if origin not in ('new','upgraded'):
            origin='upgraded' if self.store.list_chats(archived=None) or settings.get('model') else 'new'
        jobs=sorted(self.store.entities('setup_jobs'),key=lambda job:job.get('created_at',''),reverse=True)[:20]
        return {'state':state,'completed':bool(state.get('completed_at')),
            'first_run':origin=='new' and not state.get('completed_at') and not state.get('dismissed_at'),
            'origin':origin,'resume_available':bool(state) and not state.get('completed_at'),
            'jobs':jobs, 'catalog':list(MODEL_CATALOG),
            'permission_profile':settings['permission_profile']}

    def _save(self, **changes):
        with self.lock:
            state=next((s for s in self.store.entities('setup') if s['id']=='first-run'),{})
            origin=state.get('origin') or self.status()['origin']
            return self.store.save_entity('setup',{**state,'id':'first-run','origin':origin,**changes})

    def _job(self, identifier, **changes):
        with self.lock:
            current=self.store.entity('setup_jobs',identifier)
            return self.store.save_entity('setup_jobs',{**current,**changes})

    @staticmethod
    def _check_cancel(cancel):
        if cancel.is_set(): raise ValueError('Setup cancelled.')

    @staticmethod
    def _request(operation,data):
        if not isinstance(data,dict): raise ValueError('Setup arguments must be an object.')
        allowed={'scan':set(),'install_engine':{'engine'},'download_model':{'model','provider_id'},
                 'verify':{'model','context','provider_id'},'browser_verify':set()}[operation]
        if set(data)-allowed: raise ValueError('Unknown setup step argument.')
        result=dict(data)
        if 'context' in result: validate_context(result['context'])
        for key in ('model','provider_id','engine'):
            if key in result and (not isinstance(result[key],str) or not result[key].strip() or len(result[key])>300):
                raise ValueError('Invalid setup '+key+'.')
        return result

    def start(self, operation, data):
        if operation not in ('scan','install_engine','download_model','verify','browser_verify'): raise ValueError('Unknown setup step.')
        data=self._request(operation,data)
        with self.lock:
            if self.jobs: raise ValueError('Finish or cancel the current setup step first.')
            if operation!='scan' and any(r['status'] not in TERMINAL for r in self.store.runs()):
                raise ValueError('Pause active runs before installing or checking models.')
            settings=self.store.get_settings()
            if operation in ('verify','download_model'):
                data.setdefault('provider_id',settings['provider_id'])
            if operation=='verify':
                data.setdefault('model',settings['model']);data.setdefault('context',settings['context'])
            job=self.store.save_entity('setup_jobs',dict(id=uuid4().hex,operation=operation,status='queued',
                request=data,created_at=_now(),results=[]))
            cancel=threading.Event()
            thread=threading.Thread(target=self._worker,args=(job,dict(data),cancel),daemon=True,name='Forge-setup')
            self.jobs[job['id']]={'cancel':cancel,'thread':thread}; thread.start()
        return job

    def _worker(self, job, data, cancel):
        changes={}
        try:
            job=self._job(job['id'],status='running')
            self._check_cancel(cancel)
            operation=job['operation']
            if operation=='scan': result=self.scan(cancel)
            elif operation=='install_engine': result=self.install_engine(data,cancel)
            elif operation=='download_model': result=self.download_model(data,job,cancel)
            elif operation=='verify': result=self.verify(data,cancel,job=job)
            else: result=self.browser_verify(cancel)
            if cancel.is_set(): changes={'status':'cancelled'}
            else:
                self._save(**{operation:result})
                changes={'status':'completed','result':result}
        except Exception as exc:
            # Endpoint failures can contain URL credentials. Exported diagnostics
            # have a stricter allowlist; UI errors still never retain credentials.
            from forge_channels import redact
            changes={'status':'cancelled' if cancel.is_set() else 'failed','error':redact(str(exc))[:500]}
        finally:
            with self.lock:
                self._job(job['id'],**changes,finished_at=_now())
                self.jobs.pop(job['id'],None)

    def scan(self, cancel):
        telemetry=hardware(); engines=[]; models=[]
        settings=self.store.get_settings()
        for config in self.service.providers.configurations()[:16]:
            if cancel.is_set(): break
            try:
                provider=self.service.providers.provider(config['id'])
                available=provider.models()[:24]
                engines.append(dict(id=config['id'],name=config['name'],online=True,model_count=len(available)))
                if config['id']==settings['provider_id']:
                    for model in available:
                        if cancel.is_set(): break
                        name=model.get('name') or model.get('model')
                        if not isinstance(name,str) or not name: continue
                        try: info=provider.capabilities(name)
                        except Exception: info={}
                        models.append({**model,'name':name,'capabilities':info.get('capabilities',[]),
                            'advertised_context_limit':advertised_context_limit(info),
                            'capability_evidence':'engine-advertised','measured':False,'provider_id':config['id']})
            except Exception:
                engines.append(dict(id=config['id'],name=config['name'],online=False,
                    error='Cannot reach this engine. Start it or check its local endpoint.',repair_action='provider_test'))
        capacity=max([g['total_bytes'] for g in telemetry['gpus']] or [telemetry['ram']['total_bytes']*.5])
        catalog=[{**m,'fits_weights':m['size_bytes']<capacity*.75,'requires_acceptance':True,
            'capabilities':['vision','tools'],'capability_evidence':'publisher-advertised','measured':False,
            'source':'https://ollama.com/library/qwen3.5'} for m in MODEL_CATALOG]
        recommendations=self._recommendations(models,telemetry)
        return dict(hardware=telemetry,engines=engines,models=models,catalog=catalog,
            recommendations=recommendations,
            selected={k:settings[k] for k in ('model','context','provider_id','permission_profile')},
            note='Model weights fitting memory does not guarantee a large context fits. Verification keeps your selected context.')

    @staticmethod
    def _recommendations(models,telemetry):
        capacity=max([g['total_bytes'] for g in telemetry['gpus']] or [telemetry['ram']['total_bytes']*.5])
        free=max([g.get('free_bytes',0) for g in telemetry['gpus']] or [telemetry['ram'].get('available_bytes',0)])
        eligible=[m for m in models if all(c in m.get('capabilities',[]) for c in ('tools','vision'))
                  and type(m.get('size')) in (int,float) and 0<m['size']<capacity*.75
                  and (m.get('advertised_context_limit') is None or m['advertised_context_limit']>=2048)]
        eligible.sort(key=lambda model:(model['size'],model['name']))
        return [{'model':m['name'],'provider_id':m['provider_id'],'size_bytes':m['size'],
                 'requires_acceptance':True,'weights_fit_estimate':True,
                 'memory_available_now':m['size']<free*.75,'capability_evidence':'engine-advertised',
                 'suggested_start_context':min(8192,(m.get('advertised_context_limit') or 8192)//2048*2048),
                 'context_qualified':False,'reason':'Installed model advertises tools and vision and leaves estimated room for context; verify before use.'}
                for m in eligible[:3]]

    @staticmethod
    def _runtime_asset(client, engine):
        profile=next((p for p in ENGINE_PROFILES if p['engine']==engine and p.get('version')),None)
        if not profile or engine!='ollama' or os.name!='nt':
            raise ValueError('Guided installation currently supports Windows Ollama. Add llama.cpp in Models → managed runtimes.')
        version=profile['version']; repo='ollama/ollama'; tag='v'+version
        response=client.get(f'https://api.github.com/repos/{repo}/releases/tags/{tag}',headers={'Accept':'application/vnd.github+json'})
        response.raise_for_status(); release=response.json()
        if release.get('tag_name')!=tag or release.get('draft') or release.get('prerelease'): raise ValueError('Pinned engine release is unavailable.')
        asset=next((a for a in release.get('assets',[]) if a.get('name')=='ollama-windows-amd64.zip'),None)
        digest=(asset or {}).get('digest','')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',digest): raise ValueError('The official runtime asset has no verifiable checksum. Connect your existing engine instead.')
        expected=f'https://github.com/{repo}/releases/download/{tag}/ollama-windows-amd64.zip'
        if asset.get('browser_download_url')!=expected: raise ValueError('Unexpected runtime download URL.')
        return dict(engine=engine,version=version,url=expected,sha256=digest[7:])

    def install_engine(self, data, cancel):
        self._check_cancel(cancel)
        with self.runtime_client_factory(timeout=15,follow_redirects=False,trust_env=False) as client:
            config=self._runtime_asset(client,data.get('engine','ollama'))
        if cancel.is_set(): raise ValueError('Setup cancelled.')
        if self.service.runtime is None:
            from forge_runtime import RuntimeManager
            self.service.runtime=RuntimeManager(self.store.home)
        runtime=self.service.runtime.install(config,cancel=cancel)
        if cancel.is_set(): return {'runtime':runtime,'started':False}
        # Forge owns this process and directory; external engines are untouched.
        started=self.service.dispatch('runtime_start',{'id':runtime['id'],'context':self.store.get_settings()['context']})
        return {**started,'runtime_id':runtime['id'],'requires_selection':True,
                'next_action':'Select the returned managed provider before downloading a model. Your current provider remains selected.'}

    def download_model(self, data, job, cancel):
        self._check_cancel(cancel)
        name=data.get('model')
        if name not in {m['name'] for m in MODEL_CATALOG}: raise ValueError('Choose a model from the setup catalog, or use Models to download another model.')
        provider_id=data.get('provider_id') or self.store.get_settings()['provider_id']
        provider=self.service.providers.provider(provider_id)
        if not isinstance(provider,OllamaProvider): raise ValueError('Choose an Ollama engine for this download, or use Hugging Face for GGUF files.')
        async def pull():
            complete=False
            async with self.client_factory(timeout=httpx.Timeout(60,connect=3),trust_env=False) as client:
                async with client.stream('POST',provider.core.base+'/pull',json={'name':name,'stream':True}) as response:
                    response.raise_for_status()
                    async for line in bounded_lines(response):
                        if cancel.is_set(): return
                        if not line: continue
                        packet=json.loads(line)
                        if not isinstance(packet,dict): raise ValueError('Engine returned invalid download progress.')
                        if packet.get('error'): raise ValueError(str(packet['error'])[:200])
                        complete=packet.get('status')=='success'
                        from forge_channels import redact
                        progress={'model':name,'phase':redact(str(packet.get('status','Downloading')))[:120]}
                        for key,target in (('completed','completed_bytes'),('total','total_bytes')):
                            if key in packet:
                                value=packet[key]
                                if type(value) is not int or not 0<=value<=100*1024**4:
                                    raise ValueError('Engine returned invalid download byte counts.')
                                progress[target]=value
                        self._job(job['id'],**progress)
            if not complete and not cancel.is_set(): raise ValueError('Model download ended early. Retry to resume the engine download.')
        async def controlled():
            task=asyncio.create_task(pull())
            try:
                while not task.done():
                    if cancel.is_set(): task.cancel(); break
                    await asyncio.sleep(.05)
                try: await task
                except asyncio.CancelledError: pass
            finally:
                if not task.done(): task.cancel()
        asyncio.run(controlled())
        return {'model':name,'provider_id':provider_id,'requires_selection':True}

    def _performance(self):
        with getattr(self.service,'manager_lock',nullcontext()):
            manager=getattr(self.service,'performance_manager',None)
            if manager is None:
                from forge_performance import PerformanceManager
                manager=PerformanceManager(self.service)
                self.service.performance_manager=manager
        with manager.lock:
            if manager.jobs: raise ValueError('Finish or cancel the performance check before setup verification.')
        return manager

    def _context_probe(self,settings,cancel):
        """Bounded real request; report observed prompt size, not full-window fit."""
        budget=min(8192,max(256,settings['context']-1536))
        filler=' '.join('row'+str(index)+' ordinary local setup context.' for index in range(budget//16))
        text='Remember marker FORGE_CONTEXT_OK_482.\n'+filler+'\nReturn only FORGE_CONTEXT_OK_482.'
        payload={**settings,'tokens':1024,'thinking':False,'temperature':0,'tools':[],
                 'messages':[{'role':'user','content':text}]}
        began=time.monotonic();first=None;answer='';final={}
        for packet in self.service.providers.generate(payload,cancel,{},'setup_context',True):
            self._check_cancel(cancel)
            message=packet.get('message') or {}
            piece=message.get('content') or ''
            if piece and first is None:first=time.monotonic()
            answer+=piece
            if len(answer)>12_000: raise ValueError('Context probe response exceeds its bounded output budget.')
            if packet.get('done'):final=packet
        passed=bool(final.get('done') and final.get('done_reason') not in ('length','max_tokens')
                    and 'FORGE_CONTEXT_OK_482' in answer)
        usage=final.get('usage') or {}
        actual=final.get('prompt_eval_count',usage.get('prompt_tokens'))
        return {'case':'context','status':'passed' if passed else 'failed','context':settings['context'],
                'input_tokens':actual,'target_token_budget':budget,'filled_context':False,
                'estimated':actual is None,'ttft_seconds':first-began if first else None,
                'total_seconds':time.monotonic()-began,
                'evidence':'Actual bounded marker-recall request; the selected full context window is not qualified.'}

    def verify(self,data,cancel,job=None):
        self._check_cancel(cancel)
        settings=self.store.get_settings()
        settings={**settings,**{k:data[k] for k in ('model','context','provider_id') if k in data}}
        if not settings['model']: raise ValueError('Select a model before verification.')
        provider=self.service.providers.provider(settings['provider_id']); info=provider.capabilities(settings['model'])
        self._check_cancel(cancel)
        require_model_context(settings['context'],info)
        manager=self._performance()
        cases=['first_request','warm_request']
        for capability in ('tools','vision'):
            if capability in info.get('capabilities',[]): cases.append(capability)
        results=[]
        for case in cases:
            self._check_cancel(cancel)
            if job:self._job(job['id'],phase=case)
            try:result=manager._probe(settings,case,cancel)
            except Exception:
                self._check_cancel(cancel)
                result={'case':case,'status':'failed','repair':'Check the engine and selected model, then rerun verification.'}
            results.append(result)
            if job:self._job(job['id'],results=results)
        self._check_cancel(cancel)
        if job:self._job(job['id'],phase='context')
        try:context_result=self._context_probe(settings,cancel)
        except Exception:
            self._check_cancel(cancel)
            context_result={'case':'context','status':'failed','context':settings['context'],'filled_context':False,
                            'repair':'Check available memory or select a smaller context, then rerun the check.'}
        results.append(context_result)
        for capability in ('tools','vision'):
            if capability not in cases: results.append({'case':capability,'status':'unsupported','repair':'Choose a model that advertises '+capability+'.'})
        dictation=self.service.host_dictation
        if dictation is None:
            from dictation import Dictation
            self.service.host_dictation=dictation=Dictation(self.store.home,settings.get('dictation_model','base'))
        speech=dictation.status(); microphone=False
        try:
            import sounddevice
            microphone=any(d.get('max_input_channels',0)>0 for d in sounddevice.query_devices())
        except Exception: pass
        if job:self._job(job['id'],results=results)
        native=getattr(getattr(getattr(self.service,'integrations',None),'browser',None),'native',None)
        native_status=native.status() if native else {'available':False,'running':False}
        verified=next((s for s in self.store.entities('setup') if s['id']=='first-run'),{}).get('dictation_check',{})
        speech={key:value for key,value in speech.items() if key in
                ('state','model','model_installed','setup_required','message','device','compute_type')}
        return dict(results=results,model=settings['model'],provider_id=settings['provider_id'],context=settings['context'],
            model_digest=info.get('digest'),capabilities=info.get('capabilities',[]),
            qualification={'model':settings['model'],'provider_id':settings['provider_id'],
                'context':settings['context'],'advertised_context_limit':advertised_context_limit(info),
                'status':'short-probes-passed' if all(r['status']=='passed' for r in results) else 'needs-attention',
                'filled_context':False,'input_tokens_measured':context_result.get('input_tokens'),
                'evidence':'Short coding, tool, image and bounded context probes. No claim of full-context capacity.'},
            dictation={**speech,'input_available':microphone,
                'verified':bool(verified.get('verified') and verified.get('model')==speech.get('model')),
                'next_action':'Use the microphone to record a short phrase and confirm the editable transcript.'},
            browser={'native':bool(native_status.get('available')),'verified':False,
                'next_action':'Click Test browser to run a disposable local navigation and inspection check.'},checked_at=_now())

    def browser_verify(self,cancel):
        """Explicit disposable loopback check; never navigate an existing page."""
        native=getattr(getattr(getattr(self.service,'integrations',None),'browser',None),'native',None)
        if native is None or not native.status().get('available'):
            return {'status':'unsupported','verified':False,'next_action':'Open Forge desktop to test its native browser.'}
        self._check_cancel(cancel)
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                content=b'<!doctype html><title>Forge setup check</title><h1>FORGE SETUP BROWSER 482</h1><button>Local test button</button>'
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        thread=threading.Thread(target=server.serve_forever,daemon=True,name='Forge-setup-browser-page')
        clone=None;temporary=None
        try:
            thread.start()
            temporary=tempfile.TemporaryDirectory(prefix='forge-setup-browser-')
            directory=temporary.name
            if directory:
                if self.browser_factory:
                    clone=self.browser_factory(directory,native)
                else:
                    from native_browser import NativeBrowser
                    clone=NativeBrowser(directory,ui_dispatch=native.ui_dispatch,
                        view_factory=native.view_factory,session_check=native.session_check)
                url='http://127.0.0.1:'+str(server.server_port)+'/'
                context={'run_id':'setup-browser-check','cancel':cancel}
                navigation=clone.execute('browser_navigate',{'url':url},context)
                if not navigation.get('ok'):raise ValueError('The disposable browser could not navigate.')
                deadline=time.monotonic()+10
                while clone.status().get('loading') and time.monotonic()<deadline:
                    if cancel.wait(.05):self._check_cancel(cancel)
                self._check_cancel(cancel)
                inspection=clone.execute('browser_inspect',{},context)
                passed=bool(inspection.get('ok') and inspection.get('url')==url and
                            'FORGE SETUP BROWSER 482' in inspection.get('text',''))
                return {'status':'passed' if passed else 'failed','verified':passed,'checked_at':_now(),
                        'evidence':'Disposable native window navigated to and inspected a generated local page.',
                        'external_network':False,'existing_page_changed':False}
        finally:
            if clone:clone.shutdown()
            if temporary:temporary.cleanup()
            server.shutdown();server.server_close();thread.join(timeout=2)

    def dictation_confirm(self,data):
        if data.get('accepted') is not True:raise ValueError('Confirm the editable transcript after testing the microphone.')
        dictation=getattr(self.service,'host_dictation',None)
        if dictation is None:raise ValueError('Record and transcribe a short phrase first.')
        current=dictation.status()
        if current.get('state')!='done' or current.get('id')!=data.get('id') or not current.get('text','').strip():
            raise ValueError('A completed microphone transcript is required before confirmation.')
        check={'verified':True,'model':current.get('model',getattr(dictation,'model_name',None)),
               'checked_at':_now(),'transcript_characters':len(current['text']),
               'evidence':'User confirmed an editable local transcript; audio and text are not retained by setup.'}
        self._save(dictation_check=check)
        return {'ok':True,'dictation':check}

    def dispatch(self,action,data):
        if data is None:data={}
        if not isinstance(data,dict):raise ValueError('Setup arguments must be an object.')
        if action=='setup_status': return self.status()
        if action=='setup_cancel':
            with self.lock:
                job=self.jobs.get(data['id'])
                if job:
                    job['cancel'].set()
                    self._job(data['id'],status='cancelling')
            return {'ok':True}
        if action=='setup_retry':
            previous=self.store.entity('setup_jobs',data['id'])
            if previous.get('status') not in ('interrupted','cancelled','failed'):
                raise ValueError('Only an interrupted, cancelled or failed setup step can be retried.')
            return self.start(previous['operation'],previous.get('request',{}))
        if action=='setup_dictation_confirm':return self.dictation_confirm(data)
        if action=='setup_complete':
            with self.lock:
                if self.jobs:raise ValueError('Finish or cancel the current setup step first.')
            settings=self.store.get_settings()
            if not settings.get('model'): raise ValueError('Select a model or use Skip setup for now.')
            return self._save(completed_at=_now(),completed_selection={k:settings[k] for k in ('model','provider_id','context')})
        if action=='setup_skip':
            with self.lock:
                for identifier,job in self.jobs.items():
                    job['cancel'].set();self._job(identifier,status='cancelling')
            return self._save(dismissed_at=_now())
        operations={'setup_scan':'scan','setup_install_engine':'install_engine','setup_download_model':'download_model',
                    'setup_verify':'verify','setup_browser_verify':'browser_verify'}
        if action in operations: return self.start(operations[action],data)
        raise ValueError('Unknown setup action.')

    def shutdown(self):
        with self.lock: jobs=list(self.jobs.values())
        for job in jobs: job['cancel'].set()
        for job in jobs: job['thread'].join(timeout=2)
