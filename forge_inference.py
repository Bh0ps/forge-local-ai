"""Normalized local inference, a foreground queue and per-request accounting."""
from contextlib import contextmanager
import base64
import heapq
import json
import math
import os
import re
import subprocess
import threading
import time
from typing import Protocol
from uuid import uuid4

import httpx
from core import Core, AGENT_SYSTEM_PROMPT, MAX_STREAM_CHARACTERS, _tool_calls, thinking_metadata
from context_window import estimated_prompt_tokens, prompt_budget
from inference_stream import cancellable_inference, bounded_lines
from forge_provider_state import reasoning_details
from forge_engine_auth import (BoundOllamaAuth,BoundSavedProviderAuth,EngineAccessFailure,
                               EngineRunBindingFailure,authority_signature)
from forge_engine_auth import COMPATIBLE_KINDS


class ProviderStreamFailure(ValueError):
    def __init__(self,message,*,category='protocol',http_status=None):
        super().__init__(message)
        self.category=category;self.http_status=http_status
    def diagnostics(self):
        return {'provider_id':'openai-compatible','category':self.category,
            **({'http_status':self.http_status} if self.http_status is not None else {})}


def validated_response_format(value):
    """Copy a bounded strict schema; provider extras never enter this envelope."""
    invalid = 'Invalid structured response schema.'
    if (not isinstance(value, dict) or set(value) != {'type', 'json_schema'}
            or value.get('type') != 'json_schema'):
        raise ValueError(invalid)
    specification = value.get('json_schema')
    if (not isinstance(specification, dict)
            or not {'name', 'strict', 'schema'} <= set(specification)
            or set(specification) - {'name', 'strict', 'schema', 'description'}
            or not isinstance(specification.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', specification['name'])
            or specification.get('strict') is not True):
        raise ValueError(invalid)
    if 'description' in specification and (not isinstance(specification['description'], str)
            or len(specification['description']) > 1024):
        raise ValueError(invalid)
    schema = specification.get('schema')
    if (not isinstance(schema, dict) or schema.get('type') != 'object'
            or not isinstance(schema.get('properties'), dict)
            or schema.get('additionalProperties') is not False
            or not isinstance(schema.get('required'), list)
            or any(not isinstance(key, str) or key not in schema['properties'] for key in schema['required'])
            or len(set(schema['required'])) != len(schema['required'])):
        raise ValueError(invalid)
    # Detached JSON values prevent mutation after validation. Keep parsing and
    # provider serialization bounded independently of the model's context.
    def bounded(item, depth=0):
        if depth > 24:
            raise ValueError(invalid)
        if isinstance(item, dict):
            if len(item) > 256 or any(not isinstance(key, str) for key in item):
                raise ValueError(invalid)
            for child in item.values(): bounded(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 256: raise ValueError(invalid)
            for child in item: bounded(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError(invalid)
    try:
        bounded(value)
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(serialized.encode('utf-8')) > 24000: raise ValueError(invalid)
        return json.loads(serialized)
    except (TypeError, ValueError, RecursionError):
        raise ValueError(invalid) from None

class InferenceProvider(Protocol):
    def models(self): ...
    def capabilities(self,model): ...
    def generate(self,data,cancel): ...
    def estimate_tokens(self,messages,tools): ...

class InferenceQueue:
    """One GPU lease, with foreground requests ahead of queued background work."""
    def __init__(self):
        self.condition=threading.Condition(); self.pending=[]; self.active=False; self.sequence=0
        self.foreground_streak=0; self.enqueued={}

    def _next(self):
        background=[t for t in self.pending if t[0]]
        oldest=min(background,key=lambda t:t[1]) if background else None
        if oldest and (self.foreground_streak>=4 or time.monotonic()-self.enqueued[oldest]>=30):
            return oldest
        return min(self.pending) if self.pending else None

    @contextmanager
    def lease(self,cancel,background=False):
        with self.condition:
            self.sequence+=1; ticket=(1 if background else 0,self.sequence)
            heapq.heappush(self.pending,ticket)
            self.enqueued[ticket]=time.monotonic()
            try:
                while self.active or self._next()!=ticket:
                    if cancel.is_set(): raise ValueError('Inference cancelled while queued.')
                    self.condition.wait(.1)
                if cancel.is_set(): raise ValueError('Inference cancelled while queued.')
                self.pending.remove(ticket); heapq.heapify(self.pending); self.enqueued.pop(ticket,None); self.active=True
                self.foreground_streak=0 if background else self.foreground_streak+1
            except BaseException:
                if ticket in self.pending: self.pending.remove(ticket); heapq.heapify(self.pending)
                self.enqueued.pop(ticket,None)
                self.condition.notify_all(); raise
        try: yield
        finally:
            with self.condition: self.active=False; self.condition.notify_all()

class OllamaProvider:
    def __init__(self,core):
        self.core=core; self.thinking_cache={}; self.metadata_lock=threading.RLock(); self.metadata_generation=0
    def models(self): return self.core.dispatch('models',{}).get('models',[])
    def capabilities(self,model):
        with self.metadata_lock: generation=self.metadata_generation
        info=self.core.dispatch('show',{'model':model})
        with self.metadata_lock:
            if generation==self.metadata_generation:
                self.thinking_cache.pop(model,None)
                self.thinking_cache[model]=thinking_metadata(info)
                if len(self.thinking_cache)>64: self.thinking_cache.pop(next(iter(self.thinking_cache)))
        return info
    def invalidate_metadata(self):
        with self.metadata_lock:
            self.metadata_generation+=1
            self.thinking_cache.clear()
    def estimate_tokens(self,messages,tools): return estimated_prompt_tokens(messages,tools,AGENT_SYSTEM_PROMPT)
    def generate(self,data,cancel):
        with self.metadata_lock: cached=self.thinking_cache.get(data['model'])
        compiled={**data,'model_metadata':cached} if cached is not None else data
        payload=Core._agent_payload(compiled)
        payload['messages'][0]['content']=payload['messages'][0]['content'].replace('You are Sidekick','You are Forge',1)
        keep_alive=data.get('keep_alive','10m')
        payload['keep_alive']=('10m' if keep_alive else 0) if type(keep_alive) is bool else keep_alive
        if hasattr(self.core,'_stream_payload'):
            yield from self.core._stream_payload(payload,cancel)
        else: yield from self.core.stream_agent(compiled,cancel)

class CompatibleProvider:
    def __init__(self,config,vault=None,*,authority=None):
        self.config=json.loads(json.dumps(config)); self.base=config['url'].rstrip('/'); self.vault=vault
        self._authority=authority
    def _headers(self,url=None,cancel=None):
        if self._authority is not None:return self._authority.resolve(url,cancel)
        if cancel is not None and cancel.is_set():raise EngineAccessFailure('cancelled',provider_id=self.config.get('kind'))
        reference=self.config.get('credential_ref')
        if not reference:return {}
        try:
            token=self.vault.get(reference) if self.vault else None
            if not isinstance(token,str) or not token or len(token)>8192 or any(ord(char)<32 or ord(char)>126 for char in token):raise ValueError()
        except Exception:raise EngineAccessFailure('authentication',provider_id=self.config.get('kind')) from None
        if cancel is not None and cancel.is_set():raise EngineAccessFailure('cancelled',provider_id=self.config.get('kind'))
        return {'Authorization':'Bearer '+token}
    def models(self):
        from forge_provider_metadata import (fetch_json, DockerModelRunnerMetadataInvalid,
            docker_model_runner_endpoint, docker_model_runner_catalog)
        try:
            if self.config.get('preset_id')=='docker-model-runner':
                docker_model_runner_endpoint(self.base)
            result=fetch_json(self.base+'/models',headers_factory=self._headers)
            if self.config.get('preset_id')=='textgen':
                from forge_provider_metadata import textgen_catalog
                entries=textgen_catalog(result)
            elif self.config.get('preset_id')=='docker-model-runner':
                entries=docker_model_runner_catalog(result)
            else:entries=result.get('data',[])
            return [{'name':m['id'],'model':m['id'],'capabilities':self.config.get('capabilities',[])} for m in entries]
        except EngineAccessFailure:raise
        except DockerModelRunnerMetadataInvalid as exc:
            raise ProviderStreamFailure(str(exc),category='metadata') from None
        except httpx.HTTPError as exc:
            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
            raise ProviderStreamFailure('The engine model catalog is unavailable. Check the connection and saved credentials.',
                category='authentication' if status in (401,403) else 'metadata',http_status=status) from None
        except (ValueError,TypeError,KeyError):
            raise ProviderStreamFailure('The engine returned invalid model metadata.',category='metadata') from None
        except OSError:
            if self.config.get('preset_id')=='docker-model-runner':
                raise ProviderStreamFailure(str(DockerModelRunnerMetadataInvalid()),category='metadata') from None
            raise ProviderStreamFailure('The engine model catalog is unavailable. Check the connection and saved credentials.',category='metadata') from None
        except Exception:
            if self.config.get('preset_id')=='docker-model-runner':
                raise ProviderStreamFailure(str(DockerModelRunnerMetadataInvalid()),category='metadata') from None
            raise
    def capabilities(self,model):
        limit=self.config.get('context_limit')
        configured=type(limit) is int and limit>0
        result={'capabilities':self.config.get('capabilities',[]),
                'model_info':{'local.context_length':limit} if configured else {},
                'context_limit_source':'configured' if configured else 'unknown'}
        if self.config.get('preset_id')=='lm-studio':
            from forge_provider_metadata import fetch_provider_metadata
            facts=fetch_provider_metadata(self.config,model=model,headers_factory=self._headers)
            result['metadata_state']=facts['state']
            if facts['state']=='available' and len(facts['models'])==1:
                fact=facts['models'][0]
                result['advertised_metadata']=fact
                instances=fact['loaded_instances']
                exact=[item for item in instances if item['model']==model]
                matching=exact
                effective=matching[0]['context_length'] if len(matching)==1 else None
                if effective is not None:
                    result['model_info']['local.context_length']=min(limit,effective) if configured else effective
                    result['context_limit_source']='loaded_instance'
                options=fact['reasoning_options']
                if options:
                    # These declarations belong to LM Studio's native API.
                    # Its chat-completions wire does not advertise a reasoning
                    # control; never send guessed parameters or enable tools.
                    result['reasoning']={'control_supported':False, 'advertised_options':options,
                        'mandatory':'off' not in options and any(option!='off' for option in options)}
        elif self.config.get('preset_id')=='docker-model-runner':
            from forge_provider_metadata import (fetch_provider_metadata, ProviderProbeRevoked,
                DockerModelRunnerMetadataInvalid)
            try:
                facts=fetch_provider_metadata(self.config,model=model,headers_factory=self._headers)
            except (EngineAccessFailure,ProviderProbeRevoked):raise
            except httpx.HTTPError as exc:
                status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
                raise ProviderStreamFailure('Docker Model Runner model metadata is unavailable. Check the connection and saved credentials.',
                    category='authentication' if status in (401,403) else 'metadata',http_status=status) from None
            except Exception:
                raise ProviderStreamFailure(str(DockerModelRunnerMetadataInvalid()),category='metadata') from None
            result['metadata_state']=facts['state']
            if len(facts['models'])==1:
                result['advertised_metadata']=facts['models'][0]
            # Forge's compatible wire has no DMR reasoning control. Neither
            # the catalog nor a GGUF filename proves an on/off/required mode.
            result['reasoning']={'control_supported':False}
        return result
    def estimate_tokens(self,messages,tools): return estimated_prompt_tokens(messages,tools,AGENT_SYSTEM_PROMPT)
    def prepare_body(self,data,body): return body
    def generate(self,data,cancel):
        # Validate the same context and tool contract before adapting protocols.
        cancel = cancel if cancel is not None else threading.Event()
        url=self.base+'/chat/completions'
        guard=data.get('_request_guard')
        def headers(target=url):
            from forge_cloud_policy import CloudPolicyRejected
            from forge_openrouter import OpenRouterFailure
            try:
                if guard is not None:guard()
                result=self._headers(target,cancel)
                if guard is not None:guard()
                return result
            except (EngineAccessFailure,CloudPolicyRejected,OpenRouterFailure):raise
            except Exception:raise EngineAccessFailure('configuration',provider_id=self.config.get('kind')) from None
        exact_textgen=self.config.get('preset_id')=='textgen'
        if self.config.get('preset_id')=='docker-model-runner':
            from forge_provider_metadata import (fetch_json, ProviderProbeRevoked,
                DockerModelRunnerMetadataInvalid, docker_model_runner_endpoint, docker_model_runner_catalog)
            from forge_cloud_policy import CloudPolicyRejected
            from forge_openrouter import OpenRouterFailure
            try:
                base=docker_model_runner_endpoint(self.base)
                catalog=docker_model_runner_catalog(fetch_json(base+'/models',cancel=cancel,headers_factory=headers))
            except (EngineAccessFailure,CloudPolicyRejected,OpenRouterFailure):raise
            except ProviderProbeRevoked:
                raise EngineAccessFailure('cancelled',provider_id=self.config.get('kind')) from None
            except httpx.HTTPStatusError as exc:
                status=exc.response.status_code
                raise ProviderStreamFailure('Docker Model Runner model verification failed. Check the connection and saved credentials.',
                    category='authentication' if status in (401,403) else 'metadata',http_status=status) from None
            except Exception:
                raise ProviderStreamFailure(str(DockerModelRunnerMetadataInvalid()),category='metadata') from None
            if not any(entry['id']==data.get('model') for entry in catalog):
                raise ProviderStreamFailure('The selected Docker Model Runner model is not in its local catalog. Prepare it in Docker and refresh models.',category='metadata')
        if exact_textgen:
            from forge_provider_metadata import fetch_json,textgen_catalog,ProviderProbeRevoked
            from forge_cloud_policy import CloudPolicyRejected
            from forge_openrouter import OpenRouterFailure
            try:
                catalog=textgen_catalog(fetch_json(self.base+'/models',cancel=cancel,headers_factory=headers))
            except (EngineAccessFailure,CloudPolicyRejected,OpenRouterFailure):raise
            except ProviderProbeRevoked:
                raise EngineAccessFailure('cancelled',provider_id=self.config.get('kind')) from None
            except httpx.HTTPStatusError as exc:
                status=exc.response.status_code
                raise ProviderStreamFailure('Textgen model verification failed. Check the connection and saved credentials.',
                    category='authentication' if status in (401,403) else 'metadata',http_status=status) from None
            except Exception:
                raise ProviderStreamFailure('Textgen could not verify its loaded model. Refresh models after checking the engine.',category='protocol') from None
            if len(catalog)!=1 or catalog[0]['id']!=data.get('model'):
                raise ProviderStreamFailure('The selected Textgen model is not currently loaded. Load it in Textgen and refresh models.',category='protocol')
        payload=Core._agent_payload(data)
        messages=payload['messages']
        pending=[]
        for index,message in enumerate(messages):
            if message.get('images'):
                from forge_images import provider_image_url
                message['content']=[{'type':'text','text':message['content']}]+[
                    {'type':'image_url','image_url':{'url':provider_image_url(v)}} for v in message.pop('images')]
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
        if 'response_format' in data:
            body['response_format']=validated_response_format(data['response_format'])
            schema_tokens=math.ceil(len(json.dumps(body['response_format'],ensure_ascii=False,separators=(',', ':')).encode('utf-8'))/3)
            if estimated_prompt_tokens(data['messages'],data.get('tools',[]),AGENT_SYSTEM_PROMPT)+schema_tokens > prompt_budget(data.get('context',8192),payload['options']['num_predict']):
                raise ValueError('The structured response schema exceeds the selected context budget. Increase Context or reduce the review evidence.')
        body=self.prepare_body(data,body)
        started=time.monotonic()

        async def consume(response,publish):
            final={}; completed=False; characters=0; private_details=[]
            async for line in bounded_lines(response):
                if cancel.is_set(): return
                if not line.startswith('data:'): continue
                raw=line[5:].strip()
                if raw=='[DONE]': break
                if not raw: continue
                try: packet=json.loads(raw)
                except ValueError: raise ValueError('Provider returned an invalid response stream.') from None
                if not isinstance(packet,dict): raise ValueError('Provider returned an invalid response packet.')
                if exact_textgen and packet.get('error'):
                    # Error-only SSE packets need not have model identity. They
                    # never publish deltas; preserve their established recovery.
                    error=packet['error']
                    code=error.get('code') if isinstance(error,dict) else None
                    raise ProviderStreamFailure('Provider rejected the streamed response.',category='provider_error',
                        http_status=code if type(code) is int and 400<=code<=599 else None)
                if exact_textgen and packet.get('model')!=data.get('model'):
                    raise ProviderStreamFailure('Textgen changed or omitted the selected model identity. Refresh models before retrying.',category='protocol')
                if isinstance(packet.get('model'),str) and len(packet['model'])<=300:
                    if final.get('provider_model') and final['provider_model']!=packet['model']:
                        raise ProviderStreamFailure('Provider changed model identity within one response.')
                    final['provider_model']=packet['model']
                if isinstance(packet.get('provider'),str) and len(packet['provider'])<=300: final['response_provider']=packet['provider']
                if isinstance(packet.get('id'),str) and len(packet['id'])<=300:final['provider_generation_id']=packet['id']
                if packet.get('error'):
                    error=packet['error']
                    code=error.get('code') if isinstance(error,dict) else None
                    raise ProviderStreamFailure('Provider rejected the streamed response.',category='provider_error',
                        http_status=code if type(code) is int and 400<=code<=599 else None)
                usage=packet.get('usage')
                if usage is not None and not isinstance(usage,dict): raise ValueError('Provider returned invalid usage counters.')
                if usage:
                    for key in ('prompt_tokens','completion_tokens','total_tokens'):
                        if usage.get(key) is not None and (type(usage[key]) is not int or usage[key]<0):
                            raise ValueError('Provider returned invalid usage counters.')
                    details=usage.get('prompt_tokens_details')
                    if details is not None and not isinstance(details,dict):
                        raise ValueError('Provider returned invalid cached usage counters.')
                    cached=(details or {}).get('cached_tokens')
                    if cached is not None and (type(cached) is not int or cached<0):
                        raise ValueError('Provider returned invalid cached usage counters.')
                    final['usage']=usage
                timings=packet.get('timings')
                if timings is None: timings={}
                if not isinstance(timings,dict): raise ValueError('Provider returned invalid generation timings.')
                predicted_ms=timings.get('predicted_ms'); predicted_n=timings.get('predicted_n')
                for source,target in (('prompt_ms','prompt_eval_duration'),('load_ms','load_duration')):
                    value=timings.get(source)
                    if value is not None:
                        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                            raise ValueError('Provider returned invalid generation timings.')
                        final[target]=int(value*1e6)
                if timings.get('prompt_n') is not None:
                    value=timings['prompt_n']
                    if type(value) is not int or value<0: raise ValueError('Provider returned invalid usage counters.')
                    final['prompt_eval_count']=value
                if predicted_ms is not None:
                    if type(predicted_ms) not in (int,float) or not math.isfinite(predicted_ms) or predicted_ms<0:
                        raise ValueError('Provider returned invalid generation timings.')
                    final['eval_duration']=int(predicted_ms*1e6)
                if predicted_n is not None:
                    if type(predicted_n) is not int or predicted_n<0: raise ValueError('Provider returned invalid usage counters.')
                    if not usage: final.setdefault('eval_count',predicted_n)
                choices=packet.get('choices',[])
                if not isinstance(choices,list) or len(choices)>32: raise ValueError('Provider returned invalid choices.')
                for choice in choices:
                    if not isinstance(choice,dict): raise ValueError('Provider returned an invalid choice.')
                    delta=choice.get('delta')
                    if delta is None: delta={}
                    if not isinstance(delta,dict) or any(delta.get(key) is not None and not isinstance(delta[key],str)
                        for key in ('content','reasoning_content','reasoning')):
                        raise ValueError('Provider returned an invalid message.')
                    detail_chunk=reasoning_details(delta['reasoning_details']) if delta.get('reasoning_details') is not None else []
                    if detail_chunk:
                        private_details=reasoning_details(private_details+detail_chunk)
                        # Private deltas survive a later disconnect/cancellation.
                        # They never enter public message or telemetry fields.
                        await publish({'message':{},'_provider_progress':True,'_provider_reasoning_details_delta':detail_chunk,
                            **{key:final[key] for key in ('provider_model','response_provider','provider_generation_id') if key in final}})
                    if delta.get('refusal'):
                        raise ProviderStreamFailure('Provider refused the response. Partial content is saved; tool calls were not executed.',category='refusal')
                    calls=delta.get('tool_calls')
                    if calls is None: calls=[]
                    # SSE may first emit only an index/ID, followed by function
                    # fields. Validate the same bounded partial-call contract.
                    if not isinstance(calls,list) or any(not isinstance(call,dict) for call in calls):
                        raise ValueError('Provider returned invalid tool calls.')
                    normalized=[dict(call,function=call.get('function',{})) for call in calls]
                    calls,call_characters=_tool_calls(normalized,partial=True)
                    message={'content':delta.get('content') or '',
                             'thinking':delta.get('reasoning_content') or delta.get('reasoning') or '',
                             'tool_calls':calls}
                    characters+=len(message['content'])+len(message['thinking'])+call_characters
                    if characters>MAX_STREAM_CHARACTERS: raise ValueError('Response is too large. Ask for a shorter answer.')
                    if any(message.values()) or not detail_chunk:
                        await publish({'message':message,
                            **{key:final[key] for key in ('provider_model','response_provider','provider_generation_id') if key in final}})
                    if choice.get('finish_reason') is not None:
                        reason=choice['finish_reason']
                        if not isinstance(reason,str): raise ValueError('Provider returned an invalid completion reason.')
                        if reason not in ('stop','tool_calls','function_call','length','max_tokens'):
                            raise ProviderStreamFailure('Provider did not complete the response successfully. Partial content is saved; tool calls were not executed.',
                                category='refusal' if reason=='content_filter' else 'protocol')
                        final['provider_finish_reason']=reason
                        final['done_reason']=reason
                        completed=True
            if cancel.is_set(): return
            if not completed: raise ValueError('Provider stream ended before completion. Partial content is saved; tool calls were not executed.')
            if private_details:final['_provider_reasoning_details']=private_details
            await publish({**final,'done':True,'message':{},'total_duration':int((time.monotonic()-started)*1e9)})

        try:
            yield from cancellable_inference(url,body,cancel,consume,
                                             headers_factory=headers,timeouts=Core._inference_timeouts(connect=10),
                                             provider='Local provider',require_success=True)
        except httpx.ConnectError:
            raise ValueError('The local provider is offline. Start its engine, then resume the run.') from None
        except httpx.TimeoutException:
            raise ValueError('The local provider took too long to connect or receive the request. Check engine status, then resume the run.') from None
        except httpx.HTTPError as exc:
            detail=f'HTTP {exc.response.status_code}' if isinstance(exc,httpx.HTTPStatusError) else 'connection interrupted'
            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
            raise ProviderStreamFailure(f'Local provider {detail}. Partial output is retained; check engine status and resume the run.',
                category='authentication' if status in (401,403) else 'rate_limit' if status==429 else 'provider_unavailable',
                http_status=status) from None

class ProviderPool:
    def __init__(self,core,store,vault=None,*,stopped=None):
        self.core=core; self.store=store; self.vault=vault; self.queue=InferenceQueue(); self.remote_queue=InferenceQueue(); self.ollama=OllamaProvider(core)
        self.instances={}; self.lock=threading.RLock(); self.resource_queues={}
        self.capability_cache={}
        self.model_cache={}
        self.metadata_epoch=0; self.metadata_revisions={}; self.metadata_pending={}
        self.authority_epoch=0;self.authority_revisions={}
        self.metadata_wait_seconds=15
        self.stopped=stopped or (lambda: False)

    def models(self,identifier='ollama',refresh=False):
        return self._metadata(identifier or 'ollama', None, refresh)

    def capabilities(self,identifier,model,refresh=False):
        return self._metadata(identifier or 'ollama', model, refresh)

    def _metadata(self,identifier,model,refresh):
        """Share concurrent reads; an invalidated reply cannot revive old state."""
        for _ in range(4):
            provider=self.provider(identifier)
            with self.lock:
                instance=self.instances.get(identifier,{})
                if identifier!='ollama' and instance and instance.get('provider') is not provider:
                    continue
                signature=instance.get('signature','default')
                generation=(self.metadata_epoch,self.metadata_revisions.get(identifier,0))
                cache=self.model_cache if model is None else self.capability_cache
                key=(identifier,signature) if model is None else (identifier,model,signature)
                cached=cache.get(key)
                if not refresh and cached and cached[2] is provider and time.monotonic()-cached[0]<(30 if model is None else 60):
                    return json.loads(json.dumps(cached[1]))
                # A supplied stable protocol adapter may have no configuration
                # registry entry. Bind both cache and flight to its exact object
                # while retaining signature/generation checks for real profiles.
                pending_key=(model is not None,key,generation,id(provider))
                pending=self.metadata_pending.get(pending_key)
                owner=pending is None
                if owner:
                    if len(self.metadata_pending)>=64:
                        raise ValueError('Model metadata is busy. Retry shortly.')
                    pending={'event':threading.Event(),'provider':provider}
                    self.metadata_pending[pending_key]=pending
            if not owner:
                if not pending['event'].wait(self.metadata_wait_seconds):
                    raise ValueError('Model metadata took too long. Retry after checking the engine.')
                try:current=self.provider(identifier)
                except Exception:
                    raise ValueError('Provider configuration changed during metadata loading. Retry with the current engine.') from None
                with self.lock:
                    if current is not provider or generation!=(self.metadata_epoch,self.metadata_revisions.get(identifier,0)):
                        continue
                if 'error' in pending: raise pending['error']
                if 'result' in pending: return json.loads(json.dumps(pending['result']))
                continue
            try:
                result=provider.models() if model is None else provider.capabilities(model)
                result=json.loads(json.dumps(result))
                try:current=self.provider(identifier)
                except Exception:
                    raise ValueError('Provider configuration changed during metadata loading. Retry with the current engine.') from None
                with self.lock:
                    if current is not provider or generation!=(self.metadata_epoch,self.metadata_revisions.get(identifier,0)):
                        raise ValueError('Provider configuration changed during metadata loading. Retry with the current engine.')
                    cache[key]=(time.monotonic(),result,provider)
                    while len(cache)>(32 if model is None else 128): cache.pop(next(iter(cache)))
                    pending['result']=result
                return json.loads(json.dumps(result))
            except BaseException as exc:
                pending['error']=exc
                raise
            finally:
                with self.lock:
                    self.metadata_pending.pop(pending_key,None)
                    pending['event'].set()
        raise ValueError('Provider configuration changed during metadata loading. Retry with the current engine.')

    def invalidate(self,identifier=None):
        with self.lock:
            if identifier: self.metadata_revisions[identifier]=self.metadata_revisions.get(identifier,0)+1
            else: self.metadata_epoch+=1; self.metadata_revisions.clear()
            for key,pending in list(self.metadata_pending.items()):
                if not identifier or key[1][0]==identifier:
                    self.metadata_pending.pop(key,None)
                    pending['event'].set()
            self.capability_cache={k:v for k,v in self.capability_cache.items() if identifier and k[0]!=identifier}
            self.model_cache={k:v for k,v in self.model_cache.items() if identifier and k[0]!=identifier}
            providers=([self.ollama] if not identifier or identifier=='ollama' else [])+[
                item['provider'] for key,item in self.instances.items() if not identifier or key==identifier]
            for provider in providers:
                if isinstance(provider,OllamaProvider): provider.invalidate_metadata()

    def queue_for(self,identifier,provider=None):
        provider=provider or self.provider(identifier)
        if getattr(provider,'remote_inference',False): return self.remote_queue
        if identifier and identifier!='ollama':
            config=self.store.entity('providers',identifier)
            resource=config.get('resource_id')
            if resource and config.get('independent_resource') is True:
                with self.lock: return self.resource_queues.setdefault(resource,InferenceQueue())
        return self.queue
    def authority_changed(self,identifier=None):
        with self.lock:
            if identifier:self.authority_revisions[identifier]=self.authority_revisions.get(identifier,0)+1
            else:self.authority_epoch+=1;self.authority_revisions.clear()
        self.invalidate(identifier)

    def _saved_config(self,identifier):
        config=self.store.entity('providers',identifier);revision=0
        snapshot=getattr(self.store,'_provider_authority_snapshot',None)
        if config.get('kind') in (*COMPATIBLE_KINDS,'openrouter') and snapshot:config,revision=snapshot(identifier)
        return config,revision
    def provider(self,identifier):
        if not identifier or identifier=='ollama': return self.ollama
        config,revision=self._saved_config(identifier)
        signature=authority_signature(config,revision).hex()
        with self.lock:
            generation=(self.authority_epoch,self.authority_revisions.get(identifier,0))
            previous=self.instances.get(identifier,{})
            if previous.get('signature')!=signature or previous.get('binding_generation',generation)!=generation:
                base=config['url'].rstrip('/')
                if config.get('kind')=='openrouter':
                    from forge_openrouter import OpenRouterProvider
                    auth=BoundSavedProviderAuth(self.store,self.vault,identifier,config,revision=revision,
                        current_generation=lambda:self._generation_current(identifier,generation),stopped=self.stopped)
                    provider=OpenRouterProvider(config,self.vault,authority=auth)
                elif config.get('kind')=='ollama':
                    auth=BoundOllamaAuth(self.store,self.vault,identifier,config,
                        current_generation=lambda:self._generation_current(identifier,generation),stopped=self.stopped)
                    provider=OllamaProvider(Core(base=base if base.endswith('/api') else base+'/api',request_headers=auth.resolve))
                else:
                    auth=BoundSavedProviderAuth(self.store,self.vault,identifier,config,revision=revision,
                        current_generation=lambda:self._generation_current(identifier,generation),stopped=self.stopped)
                    provider=CompatibleProvider(config,self.vault,authority=auth)
                self.instances[identifier]={'signature':signature,'provider':provider,'binding_generation':generation}
            return self.instances[identifier]['provider']
    def _generation_current(self,identifier,generation):
        with self.lock:return generation==(self.authority_epoch,self.authority_revisions.get(identifier,0))
    def run_binding_signature(self,identifier,provider):
        try:
            if self.provider(identifier) is not provider:raise EngineRunBindingFailure()
            with self.lock:generation=(self.authority_epoch,self.authority_revisions.get(identifier,0))
            owner=getattr(provider,'_authority',None) or getattr(getattr(getattr(provider,'core',None),'_headers_callback',None),'__self__',None)
            config,revision=self._saved_config(identifier)
            signature=owner.binding_signature() if isinstance(owner,BoundOllamaAuth) else authority_signature(config,revision).hex()
            if not self._generation_current(identifier,generation) or self.provider(identifier) is not provider:
                raise EngineRunBindingFailure()
            return signature
        except EngineRunBindingFailure:raise
        except Exception:raise EngineRunBindingFailure() from None
    def configurations(self):
        return [dict(id='ollama',name='Ollama',kind='ollama',url=self.core.base,managed=False)]+self.store.entities('providers')
    def generate(self,data,cancel,run,purpose='main',background=False):
        from forge_cloud_policy import CloudPolicyRejected
        identifier=uuid4().hex; provider_id=data.get('provider_id','ollama'); admitted=data.get('_admitted_provider')
        try:provider=self.provider(provider_id)
        except Exception:
            if admitted is not None:raise EngineAccessFailure('configuration') from None
            raise
        final={}; output_bytes=0; encoding_error=None; call_bytes=0; submitted=time.monotonic(); start=None; first=None; visible=None
        from forge_speed import TokenSpeedEstimator
        meter=TokenSpeedEstimator(); last_speed=0
        try:
            if admitted is not None and admitted is not provider:
                raise EngineAccessFailure('configuration')
            queue=self.queue_for(provider_id,provider)
            with queue.lease(cancel,background):
                if getattr(provider,'remote_inference',False):
                    def cloud_guard():
                        from forge_cloud_policy import CloudPolicyRejected,cloud_policy
                        if cancel.is_set() or self.stopped():raise EngineAccessFailure('cancelled',provider_id='openrouter')
                        try:config=self.store.entity('providers',provider_id)
                        except Exception:raise EngineAccessFailure('configuration',provider_id='openrouter') from None
                        if not (config.get('enabled') is True and config.get('remote_consent') is True and config.get('credential_ref')):
                            raise EngineAccessFailure('consent',provider_id='openrouter')
                        if self.store.get_settings().get('permission_profile')=='deny_access':
                            raise CloudPolicyRejected('Current permissions deny cloud inference. No request was sent.')
                        if run.get('agent_id') and not self.store.entity('agents',run['agent_id']).get('enabled',True):
                            raise CloudPolicyRejected('This specialist was disabled before cloud admission. No request was sent.')
                        if run.get('cloud_scope') and run.get('id') and hasattr(self.store,'run'):
                            current=self.store.run(run['id'])
                            if current.get('cloud_scope')!=run['cloud_scope']:
                                raise CloudPolicyRejected('The cloud assignment scope changed. Start a fresh scoped request; no request was sent.')
                            service=getattr(self,'service',None)
                            if service is not None:cloud_policy(service).prepare_round(current,data.get('messages',[]),data.get('tools',[]),cancel)
                    cloud_guard()
                    data={**data,'_request_guard':cloud_guard}
                if cancel.is_set(): raise ValueError('Inference cancelled before sending the request.')
                start=time.monotonic()
                for packet in provider.generate(data,cancel):
                    message=packet.get('message',{})
                    content=(message.get('content') or '')+(message.get('thinking') or '')
                    if (content or message.get('tool_calls') or packet.get('_provider_progress')) and first is None: first=time.monotonic()
                    if message.get('content') and visible is None: visible=time.monotonic()
                    if message.get('tool_calls'):
                        call_bytes=min(2_000_000,call_bytes+len(json.dumps(message['tool_calls'],ensure_ascii=False).encode('utf-8')))
                    if content:
                        try:
                            fragment_bytes=len(content.encode('utf-8'))
                        except UnicodeEncodeError as error:
                            # Preserve malformed-output failure and final accounting
                            # behavior without keeping the prior valid transcript.
                            encoding_error=error
                            meter.observe(content)
                        else:
                            output_bytes+=fragment_bytes
                            meter.observe_bytes(fragment_bytes)
                        moment=time.monotonic(); estimate=meter.estimate(moment)
                        if estimate is not None and run.get('id') and moment-last_speed>=.5:
                            self.store.event(run['id'],'stream_speed',tps=estimate,estimated=True,
                                request_id=identifier,window_seconds=meter.window,source='backend_output_bytes')
                            last_speed=moment
                    for key in ('provider_model','response_provider','provider_generation_id'):
                        if packet.get(key):final[key]=packet[key]
                    if packet.get('done'): final={**final,**packet}
                    yield packet
        except (EngineAccessFailure,CloudPolicyRejected):
            final={'prompt_eval_count':0,'eval_count':0}
            raise
        finally:
            finished=time.monotonic(); duration=max(0,finished-submitted); usage=final.get('usage') or {}
            if not isinstance(usage,dict): usage={}
            prompt=final.get('prompt_eval_count',usage.get('prompt_tokens'))
            output=final.get('eval_count',usage.get('completion_tokens'))
            if start is None: prompt=0; output=0
            if output is None and encoding_error is not None: raise encoding_error
            details=usage.get('prompt_tokens_details')
            details=details if isinstance(details,dict) else {}
            cached=final.get('prompt_eval_cached_count',details.get('cached_tokens',0))
            if type(cached) is not int or cached<0: cached=0
            def reported_seconds(key):
                value=final.get(key)
                return value/1e9 if type(value) in (int,float) and math.isfinite(value) and value>=0 else None
            decode=reported_seconds('eval_duration') or 0
            estimated=prompt is None or output is None
            phase_timings={'queue_seconds':max(0,(start or finished)-submitted),
                'load_seconds':reported_seconds('load_duration'),
                'prefill_seconds':reported_seconds('prompt_eval_duration'),
                'first_output_seconds':first-submitted if first is not None else None,
                'first_visible_seconds':visible-submitted if visible is not None else None,
                'decode_seconds':decode if final.get('eval_duration') is not None else None,
                'inference_seconds':finished-start if start is not None else None,
                'total_seconds':duration,'provenance':{'queue':'measured','first_output':'measured',
                    'load':'reported' if reported_seconds('load_duration') is not None else 'unavailable',
                    'prefill':'reported' if reported_seconds('prompt_eval_duration') is not None else 'unavailable',
                    'decode':'reported' if reported_seconds('eval_duration') is not None else 'unavailable'}}
            self.store.record_usage(dict(id=identifier,run_id=run.get('id'),project_id=run.get('project_id'),
                provider=provider_id,model=final.get('provider_model') or data['model'],purpose=purpose,
                input_tokens=prompt if prompt is not None else provider.estimate_tokens(data['messages'],data.get('tools',[])),
                cached_input_tokens=cached,output_tokens=output if output is not None else (output_bytes+call_bytes+2)//3,
                estimated=estimated,decode_seconds=decode,total_seconds=duration,cancelled=cancel.is_set(),
                phase_timings=phase_timings,token_breakdown=data.get('token_breakdown',{})))
            speed=output/decode if output is not None and decode>0 else None
            if run.get('id'):
                self.store.event(run['id'],'usage',request_id=identifier,input_tokens=prompt,output_tokens=output,
                                 tps=speed,estimated=estimated,ttft=first-submitted if first is not None else None,
                                 phase_timings=phase_timings,token_breakdown=data.get('token_breakdown',{}),
                                 requested_model=data['model'],actual_model=final.get('provider_model') or data['model'],response_provider=final.get('response_provider'))

def _sample_memory_telemetry():
    result={'gpus':[],'ram_available':None,'probe_state':'unavailable'}
    try:
        output=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu',
                               '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,
                              creationflags=0x08000000 if os.name=='nt' else 0)
        if output.returncode==0:
            for line in output.stdout.splitlines():
                name,total,used,util=[p.strip() for p in line.split(',')]
                result['gpus'].append(dict(name=name,total_mb=int(total),used_mb=int(used),utilization=int(util)))
            result['probe_state']='ok' if result['gpus'] else 'unavailable'
        else: result['probe_state']='failed'
    except OSError: pass
    except (ValueError,subprocess.TimeoutExpired): result['probe_state']='failed'; result['gpus']=[]
    try:
        import psutil
        memory=psutil.virtual_memory(); result.update(ram_available=memory.available,ram_total=memory.total)
    except (ImportError,OSError): pass
    return result


class MemoryTelemetrySampler:
    """Throttle GPU subprocesses without serializing callers behind refresh I/O."""
    def __init__(self, sample=None, clock=None):
        self.sample=sample or _sample_memory_telemetry; self.clock=clock or time.monotonic
        self.lock=threading.Lock(); self.cached=None; self.sampled_at=0; self.pending=None

    def read(self):
        with self.lock:
            age=max(0,self.clock()-self.sampled_at)
            if self.cached is not None and age<5:
                return {**json.loads(json.dumps(self.cached)), 'sample_age_seconds':age, 'stale':False}
            if self.pending is not None:
                if self.cached is not None:
                    # Stale GPU values never masquerade as a fresh hardware
                    # decision while another caller samples the current state.
                    return {**json.loads(json.dumps(self.cached)), 'probe_state':'refreshing',
                            'sample_age_seconds':age, 'stale':True}
                pending=self.pending; owner=False
            else:
                pending=self.pending=threading.Event(); owner=True
        if not owner:
            if not pending.wait(6):
                return {'gpus':[], 'ram_available':None, 'probe_state':'refreshing',
                        'sample_age_seconds':None, 'stale':True}
            return self.read()
        result=None
        try:
            result=self.sample()
            result=json.loads(json.dumps(result))
        except Exception:
            result={'gpus':[], 'ram_available':None, 'probe_state':'failed'}
        finally:
            with self.lock:
                # Publication and the pending flag change together. No caller
                # observes a finished sample before its timestamp is committed.
                if result is not None: self.cached=result; self.sampled_at=self.clock()
                self.pending=None; pending.set()
        return {**json.loads(json.dumps(result)), 'sample_age_seconds':0, 'stale':False}


_memory_telemetry_sampler=MemoryTelemetrySampler()


def memory_telemetry():
    return _memory_telemetry_sampler.read()

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
