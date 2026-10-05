"""Measured local performance controls; never changes host-wide engine settings."""
from datetime import datetime, timezone
import asyncio
import base64
import io
import os
import platform
import threading
import time
from uuid import uuid4

import httpx
import psutil

from context_window import validate_context, advertised_context_limit, require_model_context
from forge_inference import memory_telemetry, OllamaProvider, ENGINE_PROFILES
from tool_calls import ToolCallAccumulator


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
        for model in provider.models()[:24]:
            name=model.get('name') or model.get('model')
            if not name: continue
            try: info=provider.capabilities(name)
            except Exception: info={}
            values.append({**model,'name':name,'capabilities':info.get('capabilities',model.get('capabilities',[])),
                           'context_limit':advertised_context_limit(info) or 32768})
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
        result=[]
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
        telemetry=hardware(); settings=self.store.get_settings()
        engine=self.engine(settings['provider_id'])
        return {'telemetry':telemetry,'selected':{k:settings.get(k) for k in ('provider_id','model','context','thinking','keep_alive','num_thread')},
                'profiles':ENGINE_PROFILES,
                'engine':engine,'recommendations':self.recommendations(telemetry,settings),
                'benchmarks':self.store.entities('benchmarks')[-30:][::-1],
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
        if case in ('first_request','warm_request'):
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
                with self.service.providers.queue.lease(cancel,True):
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
                if data.get('tools',True) and 'tools' in info.get('capabilities',[]): cases.append('tools')
                if data.get('vision',True) and 'vision' in info.get('capabilities',[]): cases.append('vision')
                for case in cases:
                    job.update(phase=case);self.store.save_entity('benchmarks',job)
                    result=self._probe(settings,case,cancel);job['results'].append(result);self.store.save_entity('benchmarks',job)
                job.update(summary='Quick probes finished. These short prompts do not validate a filled context window.')
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
            async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=5),trust_env=False) as client:
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
    def dispatch(self,action,data):
        if action=='performance':return self.snapshot()
        if action=='performance_apply':return self.apply(data['id'])
        if action=='performance_benchmarks':return {'benchmarks':self.store.entities('benchmarks')[-30:][::-1]}
        if action=='performance_benchmark':return self.start(data)
        if action=='performance_cancel':return self.cancel(data['id'])
        if action in ('performance_warm','performance_release'):return self.start(data,action.removeprefix('performance_'))
        raise ValueError('Unknown performance action.')
