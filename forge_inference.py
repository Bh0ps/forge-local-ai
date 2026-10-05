"""Normalized local inference, a foreground queue and per-request accounting."""
from contextlib import contextmanager
import base64
import heapq
import json
import os
import subprocess
import threading
import time
from typing import Protocol
from uuid import uuid4

import httpx
from core import Core, AGENT_SYSTEM_PROMPT
from context_window import estimated_prompt_tokens

class InferenceProvider(Protocol):
    def models(self): ...
    def capabilities(self,model): ...
    def generate(self,data,cancel): ...
    def estimate_tokens(self,messages,tools): ...

class InferenceQueue:
    """One GPU lease, with foreground requests ahead of queued background work."""
    def __init__(self):
        self.condition=threading.Condition(); self.pending=[]; self.active=False; self.sequence=0

    @contextmanager
    def lease(self,cancel,background=False):
        with self.condition:
            self.sequence+=1; ticket=(1 if background else 0,self.sequence)
            heapq.heappush(self.pending,ticket)
            try:
                while self.active or self.pending[0]!=ticket:
                    if cancel.is_set(): raise ValueError('Inference cancelled while queued.')
                    self.condition.wait(.1)
                if cancel.is_set(): raise ValueError('Inference cancelled while queued.')
                heapq.heappop(self.pending); self.active=True
            except BaseException:
                if ticket in self.pending: self.pending.remove(ticket); heapq.heapify(self.pending)
                self.condition.notify_all(); raise
        try: yield
        finally:
            with self.condition: self.active=False; self.condition.notify_all()

class OllamaProvider:
    def __init__(self,core): self.core=core
    def models(self): return self.core.dispatch('models',{}).get('models',[])
    def capabilities(self,model): return self.core.dispatch('show',{'model':model})
    def estimate_tokens(self,messages,tools): return estimated_prompt_tokens(messages,tools,AGENT_SYSTEM_PROMPT)
    def generate(self,data,cancel):
        payload=Core._agent_payload(data)
        payload['messages'][0]['content']=AGENT_SYSTEM_PROMPT.replace('Sidekick','Forge')
        keep_alive=data.get('keep_alive','10m')
        payload['keep_alive']=('10m' if keep_alive else 0) if type(keep_alive) is bool else keep_alive
        if hasattr(self.core,'_stream_payload'):
            yield from self.core._stream_payload(payload,cancel)
        else: yield from self.core.stream_agent(data,cancel)

class CompatibleProvider:
    def __init__(self,config,vault=None):
        self.config=config; self.base=config['url'].rstrip('/'); self.vault=vault
    def _headers(self):
        token=self.vault.get(self.config['credential_ref']) if self.vault and self.config.get('credential_ref') else None
        return {'Authorization':'Bearer '+token} if token else {}
    def models(self):
        with httpx.Client(timeout=10) as client:
            result=client.get(self.base+'/models',headers=self._headers()); result.raise_for_status()
            return [{'name':m['id'],'model':m['id'],'capabilities':self.config.get('capabilities',[])} for m in result.json().get('data',[])]
    def capabilities(self,model):
        return {'capabilities':self.config.get('capabilities',[]),'model_info':{'local.context_length':self.config.get('context_limit',32768)}}
    def estimate_tokens(self,messages,tools): return estimated_prompt_tokens(messages,tools,AGENT_SYSTEM_PROMPT)
    def generate(self,data,cancel):
        # Validate the same context and tool contract before adapting protocols.
        payload=Core._agent_payload(data)
        messages=payload['messages']
        pending=[]
        for index,message in enumerate(messages):
            if message.get('images'):
                message['content']=[{'type':'text','text':message['content']}]+[
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+v}} for v in message.pop('images')]
            for call in message.get('tool_calls',[]):
                call.setdefault('id','forge_call_'+str(index)+'_'+str(len(pending)))
                call.setdefault('type','function')
                pending.append((call['function']['name'],call['id']))
                arguments=call['function']['arguments']
                if not isinstance(arguments,str): call['function']['arguments']=json.dumps(arguments,ensure_ascii=False)
            if message['role']=='tool':
                name=message.pop('tool_name','')
                match=next((entry for entry in pending if entry[0]==name),None)
                if match: message['tool_call_id']=match[1]; pending.remove(match)
                else: raise ValueError('Tool response has no matching assistant call ID.')
        body=dict(model=data['model'],messages=messages,stream=True,stream_options={'include_usage':True},
                  max_tokens=payload['options']['num_predict'],temperature=data.get('temperature',.3))
        if payload.get('tools'): body['tools']=payload['tools']
        final={}; started=time.monotonic(); completed=False
        with httpx.Client(timeout=httpx.Timeout(600,connect=10)) as client:
            with client.stream('POST',self.base+'/chat/completions',json=body,headers=self._headers()) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if cancel.is_set(): return
                    if not line.startswith('data:'): continue
                    raw=line[5:].strip()
                    if raw=='[DONE]': completed=True; break
                    packet=json.loads(raw); usage=packet.get('usage')
                    if usage: final['usage']=usage
                    for choice in packet.get('choices',[]):
                        delta=choice.get('delta',{})
                        yield {'message':{'content':delta.get('content') or '',
                                          'thinking':delta.get('reasoning_content') or delta.get('reasoning') or '',
                                          'tool_calls':delta.get('tool_calls') or []}}
                        if choice.get('finish_reason'):
                            final['done_reason']='length' if choice['finish_reason']=='length' else 'stop'; completed=True
        if not completed: raise ValueError('Provider stream ended before completion. Partial content is saved; tool calls were not executed.')
        yield {**final,'done':True,'message':{},'total_duration':int((time.monotonic()-started)*1e9)}

class ProviderPool:
    def __init__(self,core,store,vault=None):
        self.core=core; self.store=store; self.vault=vault; self.queue=InferenceQueue(); self.ollama=OllamaProvider(core)
        self.instances={}; self.lock=threading.RLock()
    def provider(self,identifier):
        if not identifier or identifier=='ollama': return self.ollama
        config=self.store.entity('providers',identifier)
        signature=json.dumps(config,sort_keys=True)
        with self.lock:
            if self.instances.get(identifier,{}).get('signature')!=signature:
                base=config['url'].rstrip('/')
                provider=OllamaProvider(Core(base=base if base.endswith('/api') else base+'/api')) if config.get('kind')=='ollama' else CompatibleProvider(config,self.vault)
                self.instances[identifier]={'signature':signature,'provider':provider}
            return self.instances[identifier]['provider']
    def configurations(self):
        return [dict(id='ollama',name='Ollama',kind='ollama',url=self.core.base,managed=False)]+self.store.entities('providers')
    def generate(self,data,cancel,run,purpose='main',background=False):
        identifier=uuid4().hex; provider_id=data.get('provider_id','ollama'); provider=self.provider(provider_id)
        final={}; text=''; start=time.monotonic(); first=None
        try:
            with self.queue.lease(cancel,background):
                start=time.monotonic()
                for packet in provider.generate(data,cancel):
                    content=packet.get('message',{}).get('content') or packet.get('message',{}).get('thinking') or ''
                    if content and first is None: first=time.monotonic()
                    text+=content
                    if packet.get('done'): final=packet
                    yield packet
        finally:
            duration=max(0,time.monotonic()-start); usage=final.get('usage') or {}
            prompt=final.get('prompt_eval_count',usage.get('prompt_tokens'))
            output=final.get('eval_count',usage.get('completion_tokens'))
            cached=final.get('prompt_eval_cached_count',usage.get('prompt_tokens_details',{}).get('cached_tokens',0))
            decode=final.get('eval_duration',0)/1e9
            estimated=prompt is None or output is None
            self.store.record_usage(dict(id=identifier,run_id=run.get('id'),project_id=run.get('project_id'),
                provider=provider_id,model=data['model'],purpose=purpose,
                input_tokens=prompt if prompt is not None else provider.estimate_tokens(data['messages'],data.get('tools',[])),
                cached_input_tokens=cached,output_tokens=output if output is not None else (len(text.encode('utf-8'))+2)//3,
                estimated=estimated,decode_seconds=decode,total_seconds=duration,cancelled=cancel.is_set()))
            speed=output/decode if output is not None and decode>0 else None
            if run.get('id'):
                self.store.event(run['id'],'usage',request_id=identifier,input_tokens=prompt,output_tokens=output,
                                 tps=speed,estimated=estimated,ttft=first-start if first else None)

def memory_telemetry():
    result={'gpus':[],'ram_available':None}
    try:
        output=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu',
                               '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,
                              creationflags=0x08000000 if os.name=='nt' else 0)
        if output.returncode==0:
            for line in output.stdout.splitlines():
                name,total,used,util=[p.strip() for p in line.split(',')]
                result['gpus'].append(dict(name=name,total_mb=int(total),used_mb=int(used),utilization=int(util)))
    except (OSError,ValueError,subprocess.TimeoutExpired): pass
    try:
        import psutil
        memory=psutil.virtual_memory(); result.update(ram_available=memory.available,ram_total=memory.total)
    except ImportError: pass
    return result

ENGINE_PROFILES=[
    dict(id='ollama-balanced',name='Ollama balanced',engine='ollama',version='0.35.1',
         settings={'OLLAMA_FLASH_ATTENTION':'1','OLLAMA_KV_CACHE_TYPE':'q8_0','OLLAMA_NUM_PARALLEL':'1','OLLAMA_MAX_LOADED_MODELS':'1'},
         validation_required=True,description='Compare q8 KV cache with f16 on your exact model before enabling.'),
    dict(id='llama-cuda',name='llama.cpp CUDA',engine='llama.cpp',version='b11413',
         arguments=['--flash-attn','on','--cache-type-k','q8_0','--cache-type-v','q8_0','--parallel','1'],validation_required=True),
    dict(id='vllm-blackwell',name='vLLM Blackwell FP4',engine='vllm',deployment='docker',
         validation_required=True,requirements=['WSL2 or Linux','compatible SM120/121 checkpoint','vision projector and tool parser']),
    dict(id='sglang',name='SGLang',engine='sglang',deployment='docker',validation_required=True),
]
