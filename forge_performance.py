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
import math
import re
from contextlib import contextmanager, nullcontext
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psutil

from context_window import validate_context, advertised_context_limit, require_model_context
from forge_inference import memory_telemetry, OllamaProvider, ENGINE_PROFILES
from tool_calls import ToolCallAccumulator
from core import thinking_metadata, thinking_values
from forge_tuning import (selected, thread_candidates, headroom_reason, compare, REPETITIONS,
                          MAX_REQUESTS, MAX_SECONDS, MIN_DECODE_GAIN)

class PerformanceUnavailable(ValueError):
    def __init__(self,reason,message):
        super().__init__(message);self.reason=reason

def _public_performance_failure(error,cancelled=False,metadata=False):
    category=getattr(error,'category',None)
    category=category if category in ('authentication','quota','consent_required','access_denied','protocol','refusal') else 'metadata_unavailable' if metadata else 'operation_failed'
    if cancelled:category='cancelled'
    messages={'authentication':'Check the selected provider authentication before retrying.',
        'quota':'The selected provider quota is unavailable. Retry after it resets.',
        'consent_required':'Confirm the selected provider consent before retrying.',
        'access_denied':'Current permissions do not allow this performance operation.',
        'protocol':'The selected provider returned an unusable response. Check its compatibility before retrying.',
        'refusal':'The selected provider declined this synthetic performance probe.',
        'metadata_unavailable':'Model metadata is unavailable. Check the selected engine and retry.',
        'operation_failed':'The performance operation could not finish. Check the selected engine and retry.',
        'cancelled':'The performance operation was cancelled. Completed evidence was retained.'}
    return category,messages[category]

def _provider_signature(providers,identifier):
    config=next((p for p in providers.configurations() if p['id']==identifier),{})
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


def calibration_facts(store,providers,settings,telemetry=None):
    """Read authoritative facts afresh; cached display records grant no authority."""
    identifier=settings.get('provider_id','ollama')
    provider=providers.provider(identifier)
    signature=_provider_signature(providers,identifier)
    if not isinstance(provider,OllamaProvider) or getattr(provider,'remote_inference',False):
        raise ValueError('unsupported_engine')
    hostname=urlsplit(provider.core.base).hostname
    if hostname not in ('localhost','127.0.0.1','::1'):
        raise ValueError('server_hardware_unknown')
    models=providers.models(identifier,refresh=True)
    model=next((m for m in models if (m.get('name') or m.get('model'))==settings.get('model')),None)
    if model is None: raise ValueError('model_unavailable')
    digest=model.get('digest')
    if not isinstance(digest,str) or not re.fullmatch(r'(?:sha256:)?[0-9a-fA-F]{64}',digest):
        raise ValueError('immutable_identity_missing')
    info=providers.capabilities(identifier,settings['model'],refresh=True)
    try:require_model_context(settings['context'],info)
    except ValueError:raise ValueError('context_unavailable') from None
    requested=settings.get('thinking',False)
    values=thinking_values(settings['model'],thinking_metadata(info))
    if values is None and requested or values is not None and not any(type(value) is bool and value is requested or requested and isinstance(value,str) for value in values):
        raise ValueError('thinking_control_unsupported')
    engine=provider.core.request('GET','/version').get('version')
    if not isinstance(engine,str) or not 0<len(engine)<=128: raise ValueError('immutable_identity_missing')
    telemetry=telemetry if telemetry is not None else hardware()
    if telemetry.get('stale') or telemetry.get('probe_state') in ('failed','unavailable','refreshing'):
        raise ValueError('hardware_unknown')
    identity={'cpu':telemetry['cpu'].get('name'),'physical':telemetry['cpu']['physical_cores'],
        'logical':telemetry['cpu']['logical_cores'],'ram':telemetry['ram']['total_bytes'],
        'gpus':[(g.get('name'),g['total_bytes']) for g in telemetry['gpus']]}
    if not identity['gpus']: raise ValueError('gpu_telemetry_unknown')
    if (not identity['cpu'] or any(type(value) is not int or value<=0 for value in (identity['physical'],identity['logical'],identity['ram']))
            or any(not name or type(total) not in (int,float) or not math.isfinite(total) or total<=0 for name,total in identity['gpus'])):
        raise ValueError('hardware_unknown')
    scope={k:settings.get(k) for k in ('provider_id','model','context','thinking','tokens','temperature')}
    binding=hashlib.sha256(json.dumps({'schema':2,'provider':signature,'engine_version':engine,
        'model_digest':digest,'hardware':identity,'selected':scope,'thinking_control':thinking_metadata(info)},sort_keys=True).encode()).hexdigest()
    if signature!=_provider_signature(providers,identifier): raise ValueError('provider_changed')
    return {'binding':binding,'provider_signature':signature,'model':model,'model_digest':digest,
            'engine_version':engine,'info':info,'telemetry':telemetry,'selected':scope}


def calibration_binding(store,providers,settings):
    return calibration_facts(store,providers,settings)['binding']


def fresh_profile(store,providers,settings,profile,facts=None,telemetry=None):
    if profile.get('validated') is not True or profile.get('immutable_identity') is not True or not profile.get('binding'):
        raise ValueError('recalibration_required')
    if any(profile.get(key)!=settings.get(key) for key in ('provider_id','model','context','thinking','tokens','temperature')):
        raise ValueError('selection_changed')
    knobs=profile.get('settings',{})
    if not isinstance(knobs,dict) or 'num_thread' in knobs and (type(knobs['num_thread']) is not int or not 1<=knobs['num_thread']<=64):
        raise ValueError('recalibration_required')
    if profile.get('selected_model_tuning'):
        benchmark=store.entity('benchmarks',profile.get('benchmark_id',''))
        if benchmark.get('status')!='completed' or benchmark.get('completed_requests')!=benchmark.get('total_requests') or not profile.get('measurement',{}).get('accepted'):
            raise ValueError('measurement_incomplete')
    facts=facts if facts is not None else calibration_facts(store,providers,settings,telemetry)
    if profile.get('model_digest')!=facts['model_digest'] or profile['binding']!=facts['binding']:
        raise ValueError('binding_changed')
    return facts


def adaptive_settings(store,data,current,providers=None):
    """Fresh approved per-request knobs preserve the selected answer controls."""
    settings={**current,**data}
    settings.pop('adaptive_applied_profile',None)
    if not settings.get('adaptive_enabled') or data.get('parent_id'): return settings
    approved=set(settings.get('adaptive_profile_ids') or [])
    candidates=[]
    if not providers: return settings
    try:
        facts=None
        for profile in store.entities('calibrations'):
            if profile['id'] not in approved: continue
            try:
                facts=fresh_profile(store,providers,settings,profile,facts)
                candidates.append(profile)
            except ValueError: continue
    except Exception: return settings
    if not candidates: return settings
    profile=min(candidates,key=lambda p:p.get('median_total_seconds',float('inf')))
    proposed=profile.get('settings',{})
    for key in ('num_thread','keep_alive'):
        if key not in proposed: continue
        # Explicit per-run knob overrides remain authoritative. Model, context
        # and answer controls are never drawn from an automatic profile.
        if key in data and data[key]!=current.get(key): continue
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
        'probe_state':raw.get('probe_state','ok'),'sample_age_seconds':raw.get('sample_age_seconds',0),'stale':raw.get('stale',False),
        'cpu':{'name':name,'physical_cores':psutil.cpu_count(logical=False),
               'logical_cores':psutil.cpu_count(),'utilization_percent':psutil.cpu_percent()},
        'ram':{'total_bytes':ram.total,'available_bytes':ram.available,'used_bytes':ram.total-ram.available,'percent_used':ram.percent},
        'gpus':[{'name':g['name'],'total_bytes':g['total_mb']*1024**2,'used_bytes':g['used_mb']*1024**2,
                 'free_bytes':max(0,g['total_mb']-g['used_mb'])*1024**2,'utilization_percent':g['utilization']} for g in raw['gpus']]}


class PerformanceManager:
    def __init__(self,service):
        self.service=service; self.store=service.store; self.lock=threading.RLock(); self.jobs={}; self.cache={}
        self.metadata_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='Forge-model-metadata')
        self.status_cache={}; self.cache_generation=0; self.selection_generation=0
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
            return {'online':False,'provider_id':provider_id,'resident_models':[],'residency_supported':False,'error':'The selected engine is unavailable. Refresh after checking its connection.'}
    def invalidate(self,provider_id=None):
        with self.lock:
            self.cache_generation+=1
            if provider_id:
                self.cache.pop(provider_id,None);self.status_cache.pop('engine:'+provider_id,None)
            else:self.cache.clear();self.status_cache.clear()
    def changed(self,reason='selection_changed'):
        with self.lock:
            self.selection_generation+=1;self.cache_generation+=1
            for job in self.jobs.values():
                if job.get('operation')=='tune':job['stop_reason']=reason;job['cancel'].set()
    def _cached(self,key,seconds,loader,signature=None):
        with self.lock:
            cached=self.status_cache.get(key); generation=self.cache_generation
            if cached and time.monotonic()-cached[0]<seconds and cached[2]==signature:return cached[1]
        value=loader()
        with self.lock:
            if self.cache_generation==generation:self.status_cache[key]=(time.monotonic(),value,signature)
        return value
    def _models(self,provider_id,selected_model=None,refresh=False):
        signature=_provider_signature(self.service.providers,provider_id)
        with self.lock:
            cached=self.cache.get(provider_id); generation=self.cache_generation
            if not refresh and cached and time.monotonic()-cached[0]<45 and cached[2]==(signature,selected_model):return cached[1]
        provider=self.service.providers.provider(provider_id); values=[]
        def describe(model):
            name=model.get('name') or model.get('model')
            if not name: return None
            try: info=self.service.providers.capabilities(provider_id,name) if hasattr(self.service.providers,'capabilities') else provider.capabilities(name)
            except Exception: info={}
            return {**model,'name':name,'capabilities':info.get('capabilities',model.get('capabilities',[])),
                           'context_limit':advertised_context_limit(info) or 32768}
        catalog=self.service.providers.models(provider_id,refresh=refresh)
        visible=catalog[:24]
        chosen=next((model for model in catalog if (model.get('name') or model.get('model'))==selected_model),None)
        if chosen and chosen not in visible:visible=[*visible,chosen]
        values=[m for m in self.metadata_pool.map(describe,visible) if m]
        if signature==_provider_signature(self.service.providers,provider_id):
            with self.lock:
                if generation==self.cache_generation:self.cache[provider_id]=(time.monotonic(),values,(signature,selected_model))
        return values
    def _profile_views(self,settings,telemetry=None):
        values=[]; facts=None
        for profile in self.store.entities('calibrations'):
            valid=False;reason='recalibration_required'
            try:
                facts=fresh_profile(self.store,self.service.providers,settings,profile,facts,telemetry)
                valid=True;reason='fresh_measurement'
            except ValueError as error:
                reason=str(error) if str(error) in ('recalibration_required','measurement_incomplete','selection_changed','binding_changed','provider_changed','model_unavailable','immutable_identity_missing','thinking_control_unsupported','hardware_unknown','gpu_telemetry_unknown','unsupported_engine','server_hardware_unknown') else 'identity_unavailable'
            except Exception:reason='identity_unavailable'
            values.append({**profile,'stored_validated':profile.get('validated') is True,'validated':valid,'calibrated':valid,'eligibility_reason':reason})
        return values
    def recommendations(self,telemetry,settings,profiles=None,refresh=False):
        try: models=self._models(settings['provider_id'],settings.get('model'),refresh)
        except Exception: models=[]
        capacity=max([g['total_bytes'] for g in telemetry['gpus']] or [telemetry['ram']['total_bytes']*.30])
        eligible=[m for m in models if 'tools' in m['capabilities'] and 0<m.get('size',0)<capacity*.75 and m['context_limit']>=2048]
        calibrated=profiles if profiles is not None else self._profile_views(settings,telemetry)
        result=[{'id':p['id'],'title':'Measured '+p.get('title',p['model']),
            'description':f"{p['model']} · {p['context']//1024}K · {p['median_total_seconds']:.2f}s median probe",
            'settings':{k:v for k,v in p['settings'].items() if k in ('num_thread','keep_alive')},'requires_acceptance':True,'calibrated':True,
            'evidence_kind':'measured','reason':'Fresh exact-model calibration. Speed gains are limited to its reported probe evidence.'} for p in calibrated if p['calibrated']]
        if not eligible: return result
        # Preserve tool and image support when an installed model offers both.
        vision=[m for m in eligible if 'vision' in m['capabilities']]
        eligible=vision or eligible
        fast=min(eligible,key=lambda m:m.get('size',0))
        quality=max(eligible,key=lambda m:m.get('size',0))
        threads=min(64,telemetry['cpu'].get('physical_cores') or settings.get('num_thread') or 4)
        for identifier,title,model,context,thinking in (
            ('speed','Fast local workspace',fast,16384,False),
            ('balanced','Balanced coding and vision',fast,32768,False),
            ('quality','More deliberate responses',quality,32768,True)):
            context=min(context,model['context_limit']); context=max(2048,context//2048*2048)
            delta={'performance':identifier,'model':model['name'],'context':context,'tokens':2048 if identifier!='quality' else 4096,
                   'thinking':thinking,'keep_alive':'10m','num_thread':threads}
            result.append({'id':identifier,'title':title,'description':f"{model['name']} · {context//1024}K context",
                'settings':delta,'requires_acceptance':True,'calibrated':False,'evidence_kind':'hardware_estimate',
                'reason':'An installed tool'+('/vision' if vision else '')+' model with room left for context and image processing. Benchmark before assuming a speed improvement.'})
        return result
    def snapshot(self):
        settings=self.store.get_settings()
        telemetry=self._cached('hardware',4,hardware)
        signature=_provider_signature(self.service.providers,settings['provider_id'])
        engine=self._cached('engine:'+settings['provider_id'],10,lambda:self.engine(settings['provider_id']),signature)
        profiles=self._profile_views(settings,telemetry)
        tuning,_=self._tuning_contract(settings,telemetry)
        return {'telemetry':telemetry,'selected':{k:settings.get(k) for k in ('provider_id','model','context','thinking','keep_alive','num_thread')},
                'profiles':ENGINE_PROFILES,
                'engine':engine,'recommendations':self.recommendations(telemetry,settings,profiles),'tuning':tuning,
                'benchmarks':self.store.entities('benchmarks')[-30:][::-1],
                'calibrations':profiles,
                'adaptive':{k:settings.get(k) for k in ('adaptive_enabled','adaptive_model_locked','adaptive_context_locked','adaptive_profile_ids')},
                'capabilities':{'benchmark':True,'tune':tuning['eligible'],'warm_model':engine.get('residency_supported',False),'release_model':engine.get('residency_supported',False)},
                'notes':['RAM and GPU sizes are bytes in the API and GiB in the interface.',
                    'Benchmarks use the same inference queue as chats; foreground work has priority.',
                    'Server cache and Flash Attention flags apply only to Forge-managed processes. External engines receive guidance.',
                    'A larger supported context window does not guarantee that it fits your GPU.']}
    @contextmanager
    def _admission(self):
        with getattr(self.service,'admission_lock',nullcontext()),self.service.jobs.lock,self.lock:
            if getattr(self.service,'stop_event',None) and self.service.stop_event.is_set():
                raise PerformanceUnavailable('stopped','Forge stopped. Reopen it before changing performance.')
            update=getattr(self.service,'update_manager',None)
            if update and (getattr(update,'applying',False) or getattr(update,'inspection_required',False) or getattr(getattr(update,'native_handoff',None),'active',None) is not None):
                raise PerformanceUnavailable('native_handoff','Finish or inspect the native update before changing performance.')
            yield
    def _selection_current(self,settings,facts=None):
        if selected(self.store.get_settings())!=selected(settings):raise ValueError('Selected settings changed. Refresh Performance.')
        if facts and _provider_signature(self.service.providers,settings['provider_id'])!=facts['provider_signature']:
            raise ValueError('The provider changed. Refresh Performance.')
    def _busy_reason(self,own=None):
        update=getattr(self.service,'update_manager',None)
        if update and (getattr(update,'applying',False) or getattr(update,'inspection_required',False) or getattr(getattr(update,'native_handoff',None),'active',None) is not None):return 'native_handoff'
        if getattr(self.service,'_native_io_pending',0)-getattr(self.service,'_performance_io_pending',0)>0:return 'foreground_work'
        if any(not job['cancel'].is_set() for job in self.service.jobs.jobs.values()):return 'foreground_work'
        if any(run['status'] not in ('completed','cancelled','paused','interrupted','failed') for run in self.store.runs()):return 'foreground_work'
        for name in ('model_manager','setup_manager','ai_workflow_manager'):
            manager=getattr(self.service,name,None)
            if getattr(manager,'active',{}) or getattr(manager,'jobs',{}) and name!='model_manager' or getattr(manager,'import_reservations',set()):return 'foreground_work'
        if getattr(getattr(self.service,'runtime',None),'_validation',{}).get('state')=='running':return 'foreground_work'
        dictation=getattr(self.service,'host_dictation',None)
        if dictation and hasattr(dictation,'native_idle') and not dictation.native_idle():return 'foreground_work'
        if any(identifier!=own for identifier in self.jobs):return 'performance_busy'
        queue=self.service.providers.queue_for(self.store.get_settings().get('provider_id','ollama'))
        with queue.condition:
            if queue.active or queue.pending:return 'foreground_work'
        return None
    def _tuning_contract(self,settings,telemetry=None):
        contract={'eligible':False,'reason':'unsupported_engine','phase':'unsupported','advisory_only':True,
            'selected':selected(settings),'candidate_threads':[],'baseline_threads':settings.get('num_thread'),
            'max_repetitions':REPETITIONS,'max_requests':MAX_REQUESTS,'max_seconds':MAX_SECONDS,
            'minimum_decode_gain_fraction':MIN_DECODE_GAIN,'requires_approval':True,
            'changes':['num_thread'],'scope':'Exact selected-model probes; context fit and task success are not guaranteed.'}
        facts=None
        try:
            facts=calibration_facts(self.store,self.service.providers,settings,telemetry)
            reason=headroom_reason(facts['telemetry'],facts['model'])
            if reason:raise ValueError(reason)
            if 'tools' not in facts['info'].get('capabilities',[]):raise ValueError('tools_unavailable')
            candidates=thread_candidates(facts['telemetry'],settings.get('num_thread'))
            if not candidates:raise ValueError('no_thread_candidates')
            contract.update(eligible=True,reason='ready_to_measure',phase='idle',advisory_only=False,candidate_threads=candidates)
            busy=self._busy_reason()
            if busy:contract.update(eligible=False,reason=busy,phase='waiting')
        except ValueError as error:
            reason=str(error)
            contract['reason']=reason if reason in ('unsupported_engine','server_hardware_unknown','model_unavailable','immutable_identity_missing','thinking_control_unsupported','hardware_unknown','gpu_telemetry_unknown','provider_changed','ram_headroom_low','gpu_headroom_low','model_size_unknown','tools_unavailable','no_thread_candidates') else 'identity_unavailable'
        except Exception:contract['reason']='identity_unavailable'
        return contract,facts
    def apply(self,identifier):
        with self._admission():
            settings=self.store.get_settings(); generation=self.selection_generation
        facts=None
        profile=next((p for p in self.store.entities('calibrations') if p['id']==identifier),None)
        if profile:
            try:facts=fresh_profile(self.store,self.service.providers,settings,profile)
            except Exception:raise ValueError('This measured profile changed or needs recalibration. Settings were retained.') from None
            recommendation={'id':profile['id'],'settings':{k:v for k,v in profile['settings'].items() if k in ('num_thread','keep_alive')},'calibrated':True,'evidence_kind':'measured'}
        else:recommendation=next((r for r in self.recommendations(hardware(),settings,refresh=True) if r['id']==identifier),None)
        if not recommendation: raise ValueError('This recommendation is no longer available. Refresh Performance.')
        with self._admission():
            if generation!=self.selection_generation:raise ValueError('Selected settings changed during profile preparation. Refresh Performance.')
            self._selection_current(settings,facts)
            if self.jobs:raise ValueError('Finish the performance operation before applying a profile.')
            if any(not j['cancel'].is_set() for j in self.service.jobs.jobs.values()):
                raise ValueError('Pause active runs before applying a model profile.')
            if self._busy_reason():raise ValueError('Wait for active model and foreground work before applying a profile.')
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
        with self._admission():
            generation=self.selection_generation
            settings={**self.store.get_settings(),**{k:v for k,v in data.items() if k in ('model','provider_id','context')}}
        validate_context(settings['context'])
        if not settings.get('model'): raise ValueError('Select an installed model first.')
        try:provider=self.service.providers.provider(settings['provider_id'])
        except Exception as error:
            category,message=_public_performance_failure(error,metadata=True)
            raise PerformanceUnavailable(category,message) from None
        if operation=='calibrate':
            if getattr(provider,'remote_inference',False): raise ValueError('Adaptive calibration is for local engines.')
        if operation in ('warm','release') and not isinstance(provider,OllamaProvider): raise ValueError('This engine does not expose model residency controls.')
        try:info=provider.capabilities(settings['model'])
        except Exception as error:
            category,message=_public_performance_failure(error,metadata=True)
            raise PerformanceUnavailable(category,message) from None
        if not isinstance(info,dict):raise PerformanceUnavailable('metadata_unavailable','Model metadata is unavailable. Check the selected engine and retry.')
        require_model_context(settings['context'],info)
        tuning=None; facts=None
        if operation=='tune':
            if selected(settings)!=selected(self.store.get_settings()):raise ValueError('Tune the currently selected model and context. Refresh Performance.')
            tuning,facts=self._tuning_contract(settings)
            if not tuning['eligible']:raise ValueError('Selected-model tuning is unavailable: '+tuning['reason']+'. Manual settings remain available.')
        with self._admission():
            if operation=='tune':
                if generation!=self.selection_generation:raise ValueError('Selected settings changed during tuning preparation. Review them before starting a new check.')
                self._selection_current(settings,facts)
                if self._busy_reason():raise ValueError('Wait for foreground work before tuning.')
            if self.jobs: raise ValueError('A performance operation is already running.')
            if any(r['status'] not in ('completed','cancelled','paused','interrupted','failed') for r in self.store.runs()):
                raise ValueError('Pause active chats before running a performance check.')
            job=self.store.save_entity('benchmarks',{'id':uuid4().hex,'status':'queued','operation':operation,
                'provider_id':settings['provider_id'],'model':settings['model'],'context':settings['context'],
                'thinking':settings.get('thinking'),'selection':selected(settings),'phase':'waiting','completed_requests':0,
                'results':[],'created_at':datetime.now(timezone.utc).isoformat()})
            automatic=getattr(getattr(self.service,'update_manager',None),'automatic_downloads',None)
            if automatic:automatic.changed('active_work')
            cancel=threading.Event(); self.jobs[job['id']]={'cancel':cancel,'operation':operation,'generation':self.selection_generation}
            if operation=='tune':data={**data,'tuning_contract':tuning,'initial_facts':facts,'generation':self.selection_generation}
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
        data={**settings,'messages':messages,'tools':tools}
        first=None; start=time.monotonic(); answer=''; final={}; calls=ToolCallAccumulator()
        for packet in self.service.providers.generate(data,cancel,{},'benchmark',True):
            message=packet.get('message',{}); piece=message.get('content') or ''
            if first is None and (piece or message.get('thinking') or message.get('tool_calls')): first=time.monotonic()
            answer+=piece; calls.add(message.get('tool_calls') or [])
            if packet.get('done'): final=packet
            if cancel.is_set(): raise ValueError('Performance check cancelled.')
        duration=time.monotonic()-start; output=final.get('eval_count',(final.get('usage') or {}).get('completion_tokens'))
        output=output if type(output) is int and output>=0 else None
        input_tokens=final.get('prompt_eval_count',(final.get('usage') or {}).get('prompt_tokens'))
        input_tokens=input_tokens if type(input_tokens) is int and input_tokens>=0 else None
        reported_decode=final.get('eval_duration')
        decode=reported_decode/1e9 if type(reported_decode) in (int,float) and math.isfinite(reported_decode) and reported_decode>0 else 0
        prefill=final.get('prompt_eval_duration')
        passed=bool(final.get('done') and final.get('done_reason') not in ('length','max_tokens') and answer.strip())
        if case in ('first_request','warm_request','long_prefill','repeated_prefix'):
            from forge_runtime import RuntimeManager
            passed=passed and RuntimeManager._coding_valid(answer)
        if case=='tools': passed=bool(final.get('done') and final.get('done_reason') not in ('length','max_tokens') and any(c['function']['name']=='forge_validation_echo' and c['function']['arguments'].get('marker')=='FORGE_TOOL_OK' for c in calls.finish()))
        if case=='vision': passed=passed and all(word in answer.upper() for word in ('FORGE','VISION','482'))
        return {'case':case,'status':'passed' if passed else 'failed','ttft_seconds':first-start if first else None,
                'total_seconds':duration,'input_tokens':input_tokens,
                'output_tokens':output,'tps':output/decode if output is not None and decode>0 else None,'estimated':output is None or decode<=0,
                'decode_seconds':decode if decode>0 else None,'prefill_seconds':prefill/1e9 if type(prefill) in (int,float) and math.isfinite(prefill) and prefill>=0 else None,
                'timing_provenance':'engine_reported' if decode>0 and prefill is not None else 'unavailable',
                'ttft_provenance':'observed_first_output','selected_controls':selected(settings),
                'vision':case=='vision','tools':case=='tools','response_excerpt':answer[:2000]}
    def _tune(self,job,settings,data,cancel):
        initial=data['initial_facts']; contract=data['tuning_contract']; generation=data['generation']
        controls=['long_prefill','tools']+(['vision'] if 'vision' in initial['info'].get('capabilities',[]) else [])
        total=len(contract['candidate_threads'])*(2+REPETITIONS*2+len(controls)*2)
        assert total<=MAX_REQUESTS
        job.update(phase='checking',total_requests=total,completed_requests=0,
            tuning={'outcome':'checking','reason':'measuring','candidates':[],
                    'requires_approval':True,'minimum_decode_gain_fraction':MIN_DECODE_GAIN})
        def stop_budget():
            with self.lock:
                owned=self.jobs.get(job['id'])
                if owned:owned['stop_reason']='budget_exhausted'
            cancel.set()
        timer=threading.Timer(MAX_SECONDS,stop_budget);timer.daemon=True;timer.start()
        def guard():
            with self._admission():
                owned=self.jobs.get(job['id'],{})
                if cancel.is_set():raise ValueError(owned.get('stop_reason','cancelled'))
                if generation!=self.selection_generation or selected(self.store.get_settings())!=selected(settings):
                    owned['stop_reason']='selection_changed';cancel.set();raise ValueError('selection_changed')
                if _provider_signature(self.service.providers,settings['provider_id'])!=initial['provider_signature']:
                    owned['stop_reason']='provider_changed';cancel.set();raise ValueError('provider_changed')
                reason=self._busy_reason(job['id'])
                if reason:owned['stop_reason']=reason;cancel.set();raise ValueError(reason)
        def probe(threads,case,group,repetition=None):
            guard()
            if len(job['results'])>=MAX_REQUESTS:raise ValueError('budget_exhausted')
            pressure=headroom_reason(hardware(),initial['model'])
            if pressure:raise ValueError(pressure)
            result=self._probe({**settings,'num_thread':threads},case,cancel)
            result.update(group=group,num_thread=threads,repetition=repetition)
            job['results'].append(result);job['completed_requests']=len(job['results'])
            self.store.save_entity('benchmarks',job)
            guard()
            return result
        try:
            proposals=[]
            for threads in contract['candidate_threads']:
                guard()
                if calibration_facts(self.store,self.service.providers,settings)['binding']!=initial['binding']:
                    raise ValueError('binding_changed')
                # Warm each exact request configuration before interleaved matched samples.
                if any(result['status']!='passed' for result in (
                        probe(settings['num_thread'],'warm_request','warmup'),probe(threads,'warm_request','warmup'))):
                    raise ValueError('quality_regression')
                baseline=[]; candidate=[]
                for repetition in range(REPETITIONS):
                    # Alternate pair order to avoid granting either configuration a cache/time advantage.
                    order=[('baseline',settings['num_thread']),('candidate',threads)]
                    if repetition%2:order.reverse()
                    pair={}
                    for group,count in order:pair[group]=probe(count,'warm_request',group,repetition)
                    baseline.append(pair['baseline']);candidate.append(pair['candidate'])
                paired_controls=[]
                for case in controls:
                    paired_controls.append((probe(settings['num_thread'],case,'baseline_control'),probe(threads,case,'candidate_control')))
                evaluation={**compare(baseline,candidate,paired_controls),'num_thread':threads}
                job['tuning']['candidates'].append(evaluation)
                if evaluation['accepted']:proposals.append(evaluation)
                self.store.save_entity('benchmarks',job)
            guard()
            final=calibration_facts(self.store,self.service.providers,settings)
            if final['binding']!=initial['binding']:raise ValueError('binding_changed')
            if not proposals:
                job.update(phase='measured',summary='Measurements finished. No safe repeatable thread improvement met the acceptance checks.')
                job['tuning'].update(outcome='no_improvement',reason='no_accepted_candidate')
                return
            best=max(proposals,key=lambda value:value['decode_gain_fraction'])
            with self._admission():
                guard()
                self._selection_current(settings,final)
                profile=self.store.save_entity('calibrations',{'id':'cal-'+job['id'],'title':settings['model'],
                    **final['selected'],'model_digest':final['model_digest'],'immutable_identity':True,
                    'binding':final['binding'],'binding_schema':2,'validated':True,'probes_passed':True,
                    'benchmark_id':job['id'],'selected_model_tuning':True,
                    'settings':{'num_thread':best['num_thread']},'median_total_seconds':best['candidate_median_total_seconds'],
                    'measurement':best,'probed_capabilities':sorted(set(controls)&{'tools','vision'}),
                    'quality_scope':'Five warm paired coding probes, bounded prefix and advertised tool/image controls. Not a task-success or filled-context guarantee.'})
            job.update(calibration_id=profile['id'],phase='measured',summary='A measured thread proposal is ready. Approve it explicitly before automatic use.')
            job['tuning'].update(outcome='proposal',reason='measured_gain',proposed_settings={'num_thread':best['num_thread']},
                measurement=best,calibration_id=profile['id'],requires_approval=True)
        finally:timer.cancel()
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
            elif job['operation']=='tune':
                self._tune(job,settings,data,cancel)
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
                    try:facts=calibration_facts(self.store,self.service.providers,settings)
                    except Exception:facts=None
                    model=facts['model'] if facts else {}
                    binding=facts['binding'] if facts else None
                    immutable=facts is not None
                    profile=self.store.save_entity('calibrations',{'id':'cal-'+job['id'],'title':settings['model'],
                        'provider_id':settings['provider_id'],'model':settings['model'],'model_digest':model.get('digest'),
                        'context':settings['context'],'thinking':settings.get('thinking'),'tokens':settings.get('tokens'),
                        'temperature':settings.get('temperature'),'validated':passed and immutable,'probes_passed':passed,
                        'immutable_identity':immutable,'benchmark_id':job['id'],
                        'binding':binding,'binding_schema':2,
                        'probed_capabilities':sorted(tested&{'tools','vision'}),
                        'median_total_seconds':statistics.median(warm) if warm else 0,
                        'settings':{k:settings.get(k) for k in ('model','context','thinking','num_thread','keep_alive')},
                        'quality_scope':'Coding and '+', '.join(sorted(tested&{'tools','vision'}))+' probes; not a task-success guarantee'})
                    job.update(calibration_id=profile['id'],summary='Repeated calibration '+('passed' if passed else 'failed')+
                        ('. Explicitly approve the profile before adaptive use.' if immutable else '. Automatic adoption is unavailable without immutable engine/model identity; retain manual settings.'))
            if job['operation']=='tune' and cancel.is_set():raise ValueError('cancelled')
            job.update(status='completed')
        except Exception as exc:
            if job['operation']=='tune':
                reason=self.jobs.get(job['id'],{}).get('stop_reason') or getattr(exc,'reason',None) or str(exc)
                reason=reason if reason in ('cancelled','stopped','selection_changed','provider_changed','binding_changed','foreground_work','performance_busy','native_handoff','budget_exhausted','ram_headroom_low','gpu_headroom_low','hardware_unknown','gpu_telemetry_unknown','quality_regression') else 'inference_failed'
                job.update(status='cancelled' if cancel.is_set() else 'failed',phase='failed',stop_reason=reason,
                    error='Selected-model tuning stopped: '+reason+'. Completed evidence was retained; settings were not changed.')
                job.setdefault('tuning',{}).update(outcome='unavailable',reason=reason,requires_approval=True)
            else:
                category,message=_public_performance_failure(exc,cancelled=cancel.is_set())
                job.update(status='cancelled' if cancel.is_set() else 'failed',stop_category=category,error=message)
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
            if identifier in self.jobs:self.jobs[identifier]['stop_reason']='cancelled';self.jobs[identifier]['cancel'].set()
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
        if action=='performance_tune':return self.start(data,'tune')
        if action=='performance_calibrations':return {'calibrations':self._profile_views(self.store.get_settings())}
        if action=='performance_profile_accept':
            profile=self.store.entity('calibrations',data['id'])
            with self._admission():
                settings=self.store.get_settings(); generation=self.selection_generation
            try:facts=fresh_profile(self.store,self.service.providers,settings,profile)
            except Exception:raise ValueError('The measured profile changed or needs recalibration before approval.') from None
            with self._admission():
                if generation!=self.selection_generation:raise ValueError('Selected settings changed during profile approval. Refresh Performance.')
                self._selection_current(settings,facts)
                ids=list(dict.fromkeys(settings.get('adaptive_profile_ids',[])+[profile['id']]))
                return {'settings':self.store.update_settings({'adaptive_profile_ids':ids}), 'profile':profile}
        if action=='performance_cancel':return self.cancel(data['id'])
        if action in ('performance_warm','performance_release'):return self.start(data,action.removeprefix('performance_'))
        raise ValueError('Unknown performance action.')
