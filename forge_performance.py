"""Measured local performance controls; never changes host-wide engine settings."""
from datetime import datetime, timezone
import asyncio
import base64
import io
import os
import platform
import threading
import time
import statistics
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psutil

from context_window import validate_context, advertised_context_limit, require_model_context
from forge_inference import memory_telemetry, OllamaProvider, ENGINE_PROFILES
from tool_calls import ToolCallAccumulator

_binding_lock=threading.RLock()
_binding_hardware=None
_engine_versions={}

def calibration_binding(store,providers,settings):
    global _binding_hardware
    identifier=settings.get('provider_id','ollama')
    provider=providers.provider(identifier)
    configs=providers.configurations()
    config=next((p for p in configs if p['id']==identifier),{})
    safe={k:config.get(k) for k in ('id','kind','url','managed','runtime_id','context_limit','capabilities','resource_id','independent_resource')}
    key=json.dumps(safe,sort_keys=True)
    with _binding_lock:
        now=time.monotonic()
        if not _binding_hardware or now-_binding_hardware[0]>30:
            raw=hardware()
            _binding_hardware=(now,{'cpu':raw['cpu']['name'],'cores':raw['cpu']['physical_cores'],
                'ram':raw['ram']['total_bytes'],'gpus':[(g['name'],g['total_bytes']) for g in raw['gpus']]})
        cached=_engine_versions.get(key)
        engine=cached[1] if cached and now-cached[0]<30 else None
        if not cached or now-cached[0]>=30:
            if isinstance(provider,OllamaProvider) and hasattr(provider.core,'request'):
                try: engine=provider.core.request('GET','/version').get('version')
                except Exception: engine=None
            _engine_versions[key]=(now,engine)
        provider.calibration_engine_version=engine
        return hashlib.sha256(json.dumps({'provider':safe,'engine_version':engine,
            'hardware':_binding_hardware[1]},sort_keys=True).encode()).hexdigest()


def adaptive_settings(store,data,current,providers=None):
    """Only human-approved, still-bound calibrations may alter an unlocked run."""
    settings={**current,**data}
    if not settings.get('adaptive_enabled') or data.get('parent_id'): return settings
    approved=set(settings.get('adaptive_profile_ids') or [])
    candidates=[p for p in store.entities('calibrations') if p['id'] in approved and p.get('validated') and
        p.get('immutable_identity') is not False and p.get('provider_id')==settings.get('provider_id','ollama') and
        (not settings.get('adaptive_model_locked',True) or p.get('model')==settings.get('model')) and
        (not settings.get('adaptive_context_locked',True) or p.get('context')==settings.get('context'))]
    if providers and candidates:
        try:
            available=providers.models(settings.get('provider_id','ollama'))
            candidates=[p for p in candidates if any((m.get('name') or m.get('model'))==p['model'] and
                (not p.get('model_digest') or m.get('digest')==p['model_digest']) for m in available)]
            bound=[p for p in candidates if p.get('binding')]
            if bound:
                current_binding=calibration_binding(store,providers,settings)
                candidates=[p for p in candidates if p.get('binding')==current_binding]
        except Exception: return settings
    if not candidates: return settings
    profile=min(candidates,key=lambda p:p.get('median_total_seconds',float('inf')))
    proposed=profile.get('settings',{})
    for key in ('model','context','thinking','num_thread','keep_alive'):
        if key not in proposed: continue
        if key=='model' and settings.get('adaptive_model_locked',True): continue
        if key=='context' and settings.get('adaptive_context_locked',True): continue
        # Explicit per-run preferences are never overridden; composer sends inherited
        # values too, so its lock flags remain the authority for model/context.
        if key not in ('model','context') and key in data and data[key]!=current.get(key): continue
        settings[key]=proposed[key]
    settings['adaptive_applied_profile']=profile['id']
    return settings


def hardware():
    raw=memory_telemetry(); ram=psutil.virtual_memory(); name=platform.processor() or platform.machine()
    if os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                name=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
        except OSError: pass
    return {'timestamp':datetime.now(timezone.utc).isoformat(),
        'cpu':{'name':name,'physical_cores':psutil.cpu_count(logical=False) or 1,
               'logical_cores':psutil.cpu_count() or 1,'utilization_percent':psutil.cpu_percent()},
        'ram':{'total_bytes':ram.total,'available_bytes':ram.available,'used_bytes':ram.total-ram.available,'percent_used':ram.percent},
        'gpus':[{'name':g['name'],'total_bytes':g['total_mb']*1024**2,'used_bytes':g['used_mb']*1024**2,
                 'free_bytes':max(0,g['total_mb']-g['used_mb'])*1024**2,'utilization_percent':g['utilization']} for g in raw['gpus']]}


class PerformanceManager:
    def __init__(self,service):
        self.service=service; self.store=service.store; self.lock=threading.RLock(); self.jobs={}; self.cache={}
        self.metadata_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='Forge-model-metadata')
        self.status_cache={}
        for job in self.store.entities('benchmarks'):
            if job.get('status') in ('queued','running'):
                self.store.save_entity('benchmarks',{**job,'status':'interrupted','error':'The coordinator stopped. Run the check again.'})
    def engine(self,provider_id):
        try:
            provider=self.service.providers.provider(provider_id)
            if isinstance(provider,OllamaProvider) and hasattr(provider.core,'request'):
                result=provider.core.request('GET','/ps')
                return {'online':True,'provider_id':provider_id,'residency_supported':True,'resident_models':[
                    {'name':m.get('name') or m.get('model'),'size_bytes':m.get('size'), 'size_vram_bytes':m.get('size_vram'),
                     'context_length':m.get('context_length'),'expires_at':m.get('expires_at')} for m in result.get('models',[])]}
            provider.models()
            return {'online':True,'provider_id':provider_id,'resident_models':[],'residency_supported':False}
        except Exception as exc:
            return {'online':False,'provider_id':provider_id,'resident_models':[],'residency_supported':False,'error':str(exc)[:500]}
    def _models(self,provider_id):
        cached=self.cache.get(provider_id)
        if cached and time.monotonic()-cached[0]<45: return cached[1]
        provider=self.service.providers.provider(provider_id); values=[]
        def describe(model):
            name=model.get('name') or model.get('model')
            if not name: return None
            try: info=self.service.providers.capabilities(provider_id,name) if hasattr(self.service.providers,'capabilities') else provider.capabilities(name)
            except Exception: info={}
            return {**model,'name':name,'capabilities':info.get('capabilities',model.get('capabilities',[])),
                           'context_limit':advertised_context_limit(info) or 32768}
        values=[m for m in self.metadata_pool.map(describe,provider.models()[:24]) if m]
        self.cache[provider_id]=(time.monotonic(),values); return values
    def recommendations(self,telemetry,settings):
        try: models=self._models(settings['provider_id'])
        except Exception: models=[]
        capacity=max([g['total_bytes'] for g in telemetry['gpus']] or [telemetry['ram']['total_bytes']*.30])
        eligible=[m for m in models if 'tools' in m['capabilities'] and 0<m.get('size',0)<capacity*.75 and m['context_limit']>=2048]
        if not eligible: return []
        # Preserve tool and image support when an installed model offers both.
        vision=[m for m in eligible if 'vision' in m['capabilities']]
        eligible=vision or eligible
        fast=min(eligible,key=lambda m:m.get('size',0))
        quality=max(eligible,key=lambda m:m.get('size',0))
        threads=min(64,telemetry['cpu']['physical_cores'])
        calibrated=[p for p in self.store.entities('calibrations') if p.get('validated') and p.get('provider_id')==settings['provider_id']]
        result=[{'id':p['id'],'title':'Measured '+p.get('title',p['model']),
            'description':f"{p['model']} · {p['context']//1024}K · {p['median_total_seconds']:.2f}s median probe",
            'settings':p['settings'],'requires_acceptance':True,'calibrated':True,
            'reason':'Five warm repetitions and required capability probes passed. Task-level gains require the evaluation suite.'} for p in calibrated]
        for identifier,title,model,context,thinking in (
            ('speed','Fast local workspace',fast,16384,False),
            ('balanced','Balanced coding and vision',fast,32768,False),
            ('quality','More deliberate responses',quality,32768,True)):
            context=min(context,model['context_limit']); context=max(2048,context//2048*2048)
            delta={'performance':identifier,'model':model['name'],'context':context,'tokens':2048 if identifier!='quality' else 4096,
                   'thinking':thinking,'keep_alive':'10m','num_thread':threads}
            result.append({'id':identifier,'title':title,'description':f"{model['name']} · {context//1024}K context",
                'settings':delta,'requires_acceptance':True,
                'reason':'An installed tool'+('/vision' if vision else '')+' model with room left for context and image processing. Benchmark before assuming a speed improvement.'})
        return result
    def snapshot(self):
        settings=self.store.get_settings()
        now=time.monotonic()
        with self.lock:
            telemetry_entry=self.status_cache.get('hardware')
            telemetry=telemetry_entry[1] if telemetry_entry and now-telemetry_entry[0]<4 else hardware()
            self.status_cache['hardware']=(now,telemetry)
            engine_key='engine:'+settings['provider_id']; engine_entry=self.status_cache.get(engine_key)
            engine=engine_entry[1] if engine_entry and now-engine_entry[0]<10 else self.engine(settings['provider_id'])
            self.status_cache[engine_key]=(now,engine)
        return {'telemetry':telemetry,'selected':{k:settings.get(k) for k in ('provider_id','model','context','thinking','keep_alive','num_thread')},
                'profiles':ENGINE_PROFILES,
                'engine':engine,'recommendations':self.recommendations(telemetry,settings),
                'benchmarks':self.store.entities('benchmarks')[-30:][::-1],
                'calibrations':self.store.entities('calibrations'),
                'adaptive':{k:settings.get(k) for k in ('adaptive_enabled','adaptive_model_locked','adaptive_context_locked','adaptive_profile_ids')},
                'capabilities':{'benchmark':True,'warm_model':engine.get('residency_supported',False),'release_model':engine.get('residency_supported',False)},
                'notes':['RAM and GPU sizes are bytes in the API and GiB in the interface.',
                    'Benchmarks use the same inference queue as chats; foreground work has priority.',
                    'Server cache and Flash Attention flags apply only to Forge-managed processes. External engines receive guidance.',
                    'A larger supported context window does not guarantee that it fits your GPU.']}
    def apply(self,identifier):
        settings=self.store.get_settings(); recommendation=next((r for r in self.recommendations(hardware(),settings) if r['id']==identifier),None)
        if not recommendation: raise ValueError('This recommendation is no longer available. Refresh Performance.')
        with self.service.jobs.lock:
            if any(not j['cancel'].is_set() for j in self.service.jobs.jobs.values()):
                raise ValueError('Pause active runs before applying a model profile.')
            updated=self.store.update_settings(recommendation['settings'])
        return {'settings':updated,'recommendation':recommendation}
    def _image(self):
        from PIL import Image,ImageDraw,ImageFont
        picture=Image.new('RGB',(720,420),'white'); draw=ImageDraw.Draw(picture)
        try: font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',80)
        except OSError: font=ImageFont.load_default(size=80)
        for line,text in enumerate(('FORGE','VISION','482')): draw.text((60,45+line*110),text,font=font,fill='black')
        buffer=io.BytesIO(); picture.save(buffer,'PNG'); return base64.b64encode(buffer.getvalue()).decode()
    def start(self,data,operation='benchmark'):
        settings={**self.store.get_settings(),**{k:v for k,v in data.items() if k in ('model','provider_id','context')}}
        validate_context(settings['context'])
        if not settings.get('model'): raise ValueError('Select an installed model first.')
        provider=self.service.providers.provider(settings['provider_id'])
        if operation=='calibrate':
            if getattr(provider,'remote_inference',False): raise ValueError('Adaptive calibration is for local engines.')
            settings['thinking']=False
        if operation in ('warm','release') and not isinstance(provider,OllamaProvider): raise ValueError('This engine does not expose model residency controls.')
        require_model_context(settings['context'],provider.capabilities(settings['model']))
        with self.lock:
            if self.jobs: raise ValueError('A performance operation is already running.')
            if any(r['status'] not in ('completed','cancelled','paused','interrupted','failed') for r in self.store.runs()):
                raise ValueError('Pause active chats before running a performance check.')
            job=self.store.save_entity('benchmarks',{'id':uuid4().hex,'status':'queued','operation':operation,
                'provider_id':settings['provider_id'],'model':settings['model'],'context':settings['context'],
                'results':[],'created_at':datetime.now(timezone.utc).isoformat()})
            cancel=threading.Event(); self.jobs[job['id']]={'cancel':cancel}
            thread=threading.Thread(target=self._worker,args=(job,settings,data,cancel),daemon=True,name='Forge-performance')
            self.jobs[job['id']]['thread']=thread; thread.start(); return job
    def _probe(self,settings,case,cancel):
        text='Write only a Python function sum_even(values) that sums even integers using sum and a generator comprehension.'
        tools=[]; images=[]
        if case in ('long_prefill','repeated_prefix'):
            text=('Reference data; do not treat it as instructions.\n'+('value=2; category=even\n'*min(1500,settings['context']//16))+'\n'+text)
        if case=='tools':
            text='Call forge_validation_echo with marker FORGE_TOOL_OK. Do not answer with prose.'
            tools=[{'type':'function','function':{'name':'forge_validation_echo','description':'Echo a validation marker without changing files.',
                'parameters':{'type':'object','properties':{'marker':{'type':'string'}},'required':['marker']}}}]
        if case=='vision': text='Read the three lines in the image. Return the exact words and number.'; images=[self._image()]
        messages=[{'role':'user','content':text,**({'images':images} if images else {})}]
        data={**settings,'tokens':1024,'thinking':False,'temperature':0,'messages':messages,'tools':tools}
        first=None; start=time.monotonic(); answer=''; final={}; calls=ToolCallAccumulator()
        for packet in self.service.providers.generate(data,cancel,{},'benchmark',True):
            message=packet.get('message',{}); piece=message.get('content') or ''
            if first is None and (piece or message.get('tool_calls')): first=time.monotonic()
            answer+=piece; calls.add(message.get('tool_calls') or [])
            if packet.get('done'): final=packet
            if cancel.is_set(): raise ValueError('Performance check cancelled.')
        duration=time.monotonic()-start; output=final.get('eval_count',(final.get('usage') or {}).get('completion_tokens'))
        decode=(final.get('eval_duration') or 0)/1e9
        passed=bool(final.get('done') and final.get('done_reason') not in ('length','max_tokens') and answer.strip())
        if case in ('first_request','warm_request','long_prefill','repeated_prefix'):
            from forge_runtime import RuntimeManager
            passed=passed and RuntimeManager._coding_valid(answer)
        if case=='tools': passed=bool(final.get('done') and final.get('done_reason') not in ('length','max_tokens') and any(c['function']['name']=='forge_validation_echo' and c['function']['arguments'].get('marker')=='FORGE_TOOL_OK' for c in calls.finish()))
        if case=='vision': passed=passed and all(word in answer.upper() for word in ('FORGE','VISION','482'))
        return {'case':case,'status':'passed' if passed else 'failed','ttft_seconds':first-start if first else None,
                'total_seconds':duration,'input_tokens':final.get('prompt_eval_count',(final.get('usage') or {}).get('prompt_tokens')),
                'output_tokens':output,'tps':output/decode if output is not None and decode>0 else None,'estimated':output is None or decode<=0,
                'vision':case=='vision','tools':case=='tools','response_excerpt':answer[:2000]}
    def _worker(self,job,settings,data,cancel):
        samples=[]; sample_stop=threading.Event()
        def sample():
            while not sample_stop.is_set():
                samples.append(hardware()); samples[:max(0,len(samples)-1200)]=[]; sample_stop.wait(.75)
        sampler=threading.Thread(target=sample,daemon=True,name='Forge-performance-memory'); sampler.start()
        try:
            job=self.store.save_entity('benchmarks',{**job,'status':'running'})
            if job['operation'] in ('warm','release'):
                provider=self.service.providers.provider(settings['provider_id']); keep=0 if job['operation']=='release' else settings['keep_alive']
                with self.service.providers.queue_for(settings['provider_id']).lease(cancel,True):
                    if cancel.is_set(): raise ValueError('Performance operation cancelled.')
                    began=time.monotonic()
                    final={}
                    try:
                        final=self._resident_request(provider.core.base+'/generate',{'model':settings['model'],'keep_alive':keep,'stream':False,
                            'options':{'num_ctx':settings['context'],'num_thread':settings['num_thread']}},cancel)
                    finally:
                        self.store.record_usage({'id':uuid4().hex,'provider':settings['provider_id'],'model':settings['model'],'purpose':job['operation'],
                            'input_tokens':final.get('prompt_eval_count',0),'output_tokens':final.get('eval_count',0),
                            'estimated':final.get('prompt_eval_count') is None or final.get('eval_count') is None,'cancelled':cancel.is_set(),
                            'total_seconds':time.monotonic()-began})
                    if cancel.is_set(): raise ValueError('Performance operation cancelled. The engine may finish an already requested model load.')
                job.update(summary='Model released.' if job['operation']=='release' else 'Model warmed and retained.')
            else:
                provider=self.service.providers.provider(settings['provider_id']); info=provider.capabilities(settings['model'])
                cases=['first_request','warm_request']
                if job['operation']=='calibrate': cases=['first_request']+['warm_request']*5+['long_prefill','repeated_prefix']
                if data.get('tools',True) and 'tools' in info.get('capabilities',[]): cases.append('tools')
                if data.get('vision',True) and 'vision' in info.get('capabilities',[]): cases.append('vision')
                for case in cases:
                    job.update(phase=case);self.store.save_entity('benchmarks',job)
                    result=self._probe(settings,case,cancel);job['results'].append(result);self.store.save_entity('benchmarks',job)
                job.update(summary='Quick probes finished. These short prompts do not validate a filled context window.')
                if job['operation']=='calibrate':
                    warm=[r['total_seconds'] for r in job['results'] if r['case']=='warm_request']
                    tested={r['case'] for r in job['results']}
                    passed=all(r['status']=='passed' for r in job['results']) and len(warm)>=5 and 'tools' in tested
                    model=next((m for m in provider.models() if (m.get('name') or m.get('model'))==settings['model']),{})
                    binding=calibration_binding(self.store,self.service.providers,settings)
                    immutable=bool(isinstance(provider,OllamaProvider) and model.get('digest') and
                        getattr(provider,'calibration_engine_version',None))
                    profile=self.store.save_entity('calibrations',{'id':'cal-'+job['id'],'title':settings['model'],
                        'provider_id':settings['provider_id'],'model':settings['model'],'model_digest':model.get('digest'),
                        'context':settings['context'],'validated':passed and immutable,'probes_passed':passed,
                        'immutable_identity':immutable,'benchmark_id':job['id'],
                        'binding':binding,
                        'probed_capabilities':sorted(tested&{'tools','vision'}),
                        'median_total_seconds':statistics.median(warm) if warm else 0,
                        'settings':{k:settings.get(k) for k in ('model','context','thinking','num_thread','keep_alive')},
                        'quality_scope':'Coding and '+', '.join(sorted(tested&{'tools','vision'}))+' probes; not a task-success guarantee'})
                    job.update(calibration_id=profile['id'],summary='Repeated calibration '+('passed' if passed else 'failed')+
                        ('. Explicitly approve the profile before adaptive use.' if immutable else '. Automatic adoption is unavailable without immutable engine/model identity; retain manual settings.'))
            job.update(status='completed')
        except Exception as exc: job.update(status='cancelled' if cancel.is_set() else 'failed',error=str(exc)[:1000])
        finally:
            sample_stop.set();sampler.join(timeout=6)
            job['peak_gpu_bytes']=max([g['used_bytes'] for s in samples for g in s['gpus']] or [0])
            job['peak_ram_bytes']=max([s['ram']['used_bytes'] for s in samples] or [0])
            self.store.save_entity('benchmarks',job)
            with self.lock:self.jobs.pop(job['id'],None)
    @staticmethod
    def _resident_request(url,payload,cancel):
        async def request():
            from inference_stream import FIRST_RESPONSE_TIMEOUT_SECONDS
            async with httpx.AsyncClient(timeout=httpx.Timeout(FIRST_RESPONSE_TIMEOUT_SECONDS,connect=5),trust_env=False) as client:
                task=asyncio.create_task(client.post(url,json=payload))
                try:
                    while not task.done():
                        if cancel.is_set():
                            task.cancel()
                            raise ValueError('Performance operation cancelled. The engine may finish an already requested model load.')
                        await asyncio.wait({task},timeout=.05)
                    response=await task;response.raise_for_status();return response.json()
                finally:
                    if not task.done():task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
        return asyncio.run(request())
    def cancel(self,identifier):
        with self.lock:
            if identifier in self.jobs:self.jobs[identifier]['cancel'].set()
        return {'ok':True}
    def shutdown(self):
        with self.lock: jobs=list(self.jobs.values())
        for job in jobs:job['cancel'].set()
        for job in jobs:job['thread'].join(timeout=2)
        self.metadata_pool.shutdown(wait=False,cancel_futures=True)
    def dispatch(self,action,data):
        if action=='performance':return self.snapshot()
        if action=='performance_apply':return self.apply(data['id'])
        if action=='performance_benchmarks':return {'benchmarks':self.store.entities('benchmarks')[-30:][::-1]}
        if action=='performance_benchmark':return self.start(data)
        if action=='performance_calibrate':return self.start(data,'calibrate')
        if action=='performance_calibrations':return {'calibrations':self.store.entities('calibrations')}
        if action=='performance_profile_accept':
            profile=self.store.entity('calibrations',data['id'])
            if not profile.get('validated'): raise ValueError('Only a passing repeated calibration can be approved.')
            provider=self.service.providers.provider(profile['provider_id'])
            current=next((m for m in provider.models() if (m.get('name') or m.get('model'))==profile['model']),None)
            if current is None or profile.get('model_digest') and current.get('digest')!=profile['model_digest']:
                raise ValueError('The calibrated model changed. Calibrate it again.')
            if profile.get('binding')!=calibration_binding(self.store,self.service.providers,self.store.get_settings()|{'provider_id':profile['provider_id']}):
                raise ValueError('The engine or hardware changed. Calibrate again before approval.')
            settings=self.store.get_settings()
            ids=list(dict.fromkeys(settings.get('adaptive_profile_ids',[])+[profile['id']]))
            return {'settings':self.store.update_settings({'adaptive_profile_ids':ids}), 'profile':profile}
        if action=='performance_cancel':return self.cancel(data['id'])
        if action in ('performance_warm','performance_release'):return self.start(data,action.removeprefix('performance_'))
        raise ValueError('Unknown performance action.')
