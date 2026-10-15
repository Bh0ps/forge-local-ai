"""Explicitly consented, zero-price OpenRouter inference and local connection UI.

Only the official HTTPS endpoint receives the OS-vault API key. Saving a key or
reading status is local; connection tests fetch metadata without a prompt.
"""
from decimal import Decimal, InvalidOperation
import json
import re
import threading
import time

import httpx

from forge_credentials import CredentialVault
from forge_inference import CompatibleProvider, ProviderStreamFailure, validated_response_format
from forge_provider_state import reasoning_details, state_size, INCOMPLETE_PROVIDER_RECOVERY
from forge_openrouter_errors import classify, message as failure_message, recovery_diagnostics, capacity
from core import AGENT_SYSTEM_PROMPT, Core
from context_window import estimated_prompt_tokens, prompt_budget, response_budget, require_model_context, history_character_limit
from inference_stream import InferenceTimeoutError
from forge_engine_auth import (BoundSavedProviderAuth,EngineAccessFailure,provider_authority,
    provider_save_snapshot,discard_uncommitted_provider_credential)
from storage import _now


OPENROUTER_URL = 'https://openrouter.ai/api/v1'
FREE_ROUTER = 'openrouter/free'
PRICE_FIELDS = frozenset(('prompt', 'completion', 'request', 'image'))
REQUIRED_PRICE_FIELDS = frozenset(('prompt', 'completion'))
CONDITION_FIELDS = frozenset(('min_prompt_tokens', 'utc_start', 'utc_end', 'utc_days'))


class OpenRouterFailure(ValueError):
    """A local, scrubbed error; remote error messages never gain this type."""
    def __init__(self,message,*,category='configuration',http_status=None,
                 continuation_reason=None,requested_context=None,model_context_limit=None,recovery=None):
        super().__init__(message)
        self.category=category;self.http_status=http_status
        self.recovery=recovery_diagnostics(recovery)
        from forge_cloud_failure import continuation_diagnostics
        self.continuation=continuation_diagnostics({'category':category,'continuation_reason':continuation_reason,
            'requested_context':requested_context,'model_context_limit':model_context_limit})

    def diagnostics(self):
        return {'provider_id':'openrouter','category':self.category,
            **({'http_status':self.http_status} if self.http_status is not None else {}),**self.continuation,**self.recovery}


def _zero(value):
    if type(value) not in (str, int, float):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and number == 0
    except InvalidOperation:
        return False


def zero_pricing(pricing):
    if not isinstance(pricing, dict) or not REQUIRED_PRICE_FIELDS <= pricing.keys():
        return False
    for key, value in pricing.items():
        if key == 'overrides':
            if not isinstance(value, list):
                return False
            for override in value:
                if not isinstance(override, dict) or any(not _zero(v) for k, v in override.items() if k not in CONDITION_FIELDS):
                    return False
        elif not _zero(value):
            return False
    return True


def free_catalog(raw):
    result = []
    if not isinstance(raw, list) or len(raw) > 10000:
        raise OpenRouterFailure('OpenRouter returned an invalid model catalog.')
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get('id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,300}', name):
            continue
        if name != FREE_ROUTER and not name.endswith(':free'):
            continue
        if not zero_pricing(item.get('pricing')):
            continue
        context = item.get('context_length')
        if type(context) is not int or not 2048 <= context <= 10000000:
            continue
        architecture = item.get('architecture') or {}
        modalities = architecture.get('input_modalities', []) if isinstance(architecture, dict) else []
        parameters = item.get('supported_parameters') or []
        capabilities = []
        if 'tools' in parameters or name == FREE_ROUTER:
            capabilities.append('tools')
        if 'image' in modalities or name == FREE_ROUTER:
            capabilities.append('vision')
        if 'response_format' in parameters or 'structured_outputs' in parameters or name == FREE_ROUTER:
            capabilities.append('structured_outputs')
        if 'reasoning' in parameters or 'reasoning_effort' in parameters:capabilities.append('thinking')
        title = item.get('name')
        title = title[:200] if isinstance(title, str) else name
        result.append({'name': name, 'model': name, 'title': title, 'provider': 'openrouter',
                       'capabilities': capabilities, 'context_length': context, 'free': True,
                       'pricing': {k: '0' for k in PRICE_FIELDS},
                       'reasoning':{key:value for key,value in (item.get('reasoning') or {}).items() if key=='mandatory' and type(value) is bool} if isinstance(item.get('reasoning'),dict) else {}})
    return sorted(result, key=lambda m: (m['name'] != FREE_ROUTER, m['name']))


class OpenRouterProvider(CompatibleProvider):
    remote = True
    remote_inference = True

    def __init__(self, config, vault=None, http_client=None,*,authority=None):
        if config.get('url', OPENROUTER_URL).rstrip('/') != OPENROUTER_URL:
            raise OpenRouterFailure('Free-only OpenRouter uses its fixed official HTTPS endpoint.')
        super().__init__({**config, 'url': OPENROUTER_URL}, vault,authority=authority)
        self.http_client = http_client
        self.catalog = None
        self.catalog_at = 0
        self.catalog_lock = threading.RLock()

    def _headers(self,url=None,cancel=None):
        headers=super()._headers(url,cancel)
        if 'Authorization' not in headers:raise EngineAccessFailure('authentication',provider_id='openrouter')
        return {**headers,'X-OpenRouter-Title':'Forge Local AI'}

    @staticmethod
    def _http_error(status,*,inference=False):
        return failure_message(classify(status)[0],status,inference=inference)

    @staticmethod
    def _http_category(status):
        return classify(status)[0]

    @staticmethod
    def _failure(status,error=None,headers=None,*,inference=True):
        category,details=classify(status,error,headers)
        return OpenRouterFailure(failure_message(category,status,inference=inference),
            category=category,http_status=status,recovery=details)

    def response_error(self,response):
        try:value=response.json()
        except (ValueError,TypeError,RecursionError):value={}
        raise self._failure(response.status_code,value.get('error') if isinstance(value,dict) else None,response.headers)

    def stream_error(self,error,response):
        code=error.get('code') if isinstance(error,dict) else None
        raise self._failure(code if type(code) is int and 400<=code<=599 else None,error,response.headers)

    def _get(self, suffix):
        url=OPENROUTER_URL+suffix
        def read(client):
            with client.stream('GET',url,headers=self._headers(url)) as response:
                bound=8*1024*1024 if response.is_success else 65536
                content=bytearray()
                for chunk in response.iter_bytes():
                    if len(content)+len(chunk)>bound:
                        raise OpenRouterFailure('OpenRouter returned oversized metadata.',category='metadata')
                    content.extend(chunk)
                return httpx.Response(response.status_code,headers=response.headers,content=bytes(content),request=response.request)
        try:
            if self.http_client:
                response = read(self.http_client)
            else:
                with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                    response = read(client)
            if not 200 <= response.status_code < 300:
                try:value=response.json()
                except (ValueError,TypeError,RecursionError):value={}
                raise self._failure(response.status_code,value.get('error') if isinstance(value,dict) else None,response.headers,inference=False)
            if self._authority is not None:self._authority._check(url,None)
            value = response.json()
            if not isinstance(value, dict):
                raise OpenRouterFailure('OpenRouter returned invalid metadata.')
            return value
        except EngineAccessFailure:raise
        except httpx.HTTPError:
            raise OpenRouterFailure('OpenRouter connection failed. No automatic retry was made.',category='network') from None
        except (ValueError, TypeError, RecursionError) as exc:
            if isinstance(exc, OpenRouterFailure):
                raise
            raise OpenRouterFailure('OpenRouter returned invalid metadata.',category='metadata') from None

    def models(self, refresh=False):
        if self._authority is not None:self._authority._check(OPENROUTER_URL+'/models',None)
        with self.catalog_lock:
            if refresh or self.catalog is None or time.monotonic() - self.catalog_at > 60:
                self.catalog = free_catalog(self._get('/models').get('data'))
                self.catalog_at = time.monotonic()
            return [dict(model) for model in self.catalog]

    def capabilities(self, model):
        if self.config.get('enabled') is not True or self.config.get('remote_consent') is not True:
            raise OpenRouterFailure('Free-only OpenRouter requires enabled cloud consent before sending prompts or tool results.')
        info = next((m for m in self.models() if m['name'] == model), None)
        if info is None:
            raise OpenRouterFailure('Free-only OpenRouter model is unavailable or does not advertise zero pricing.')
        return {'capabilities': info['capabilities'], 'model_info': {'openrouter.context_length': info['context_length']},
                'context_length': info['context_length'], 'free': True, 'remote': True,'reasoning':info.get('reasoning',{})}

    def validate_continuation(self,model,context,required=()):
        """A router response cannot authorize guessing a paid or aliased model."""
        entry=next((m for m in self.models(refresh=True) if m['name']==model and model!=FREE_ROUTER),None)
        if entry is None:
            raise OpenRouterFailure('The free router returned no exact advertised free model for this reasoning continuation. Start a fresh scoped assignment; no tool from this response ran.',category='continuation_unavailable',continuation_reason='model_unavailable')
        info={'capabilities':entry['capabilities'],'model_info':{'openrouter.context_length':entry['context_length']}}
        try:require_model_context(context,info)
        except ValueError:
            raise OpenRouterFailure('The selected Context exceeds the routed model limit. Start a fresh scoped assignment with a compatible free model; no tool from this response ran.',category='continuation_unavailable',continuation_reason='context_limit',requested_context=context,model_context_limit=entry['context_length']) from None
        if set(required)-set(info['capabilities']):
            raise OpenRouterFailure('The routed free model lacks a required continuation capability. Start a fresh scoped assignment; no tool from this response ran.',category='continuation_unavailable',continuation_reason='capabilities')
        return model

    def estimate_tokens(self,messages,tools):
        projected=[]
        for message in messages:
            value={key:child for key,child in message.items() if key!='_provider_state'}
            if message.get('_provider_state'):value['reasoning_details']=message['_provider_state']['reasoning_details']
            projected.append(value)
        return super().estimate_tokens(projected,tools)

    def prepare_body(self, data, body):
        if self.config.get('enabled') is not True or self.config.get('remote_consent') is not True:
            raise OpenRouterFailure('Free-only OpenRouter requires enabled cloud consent before sending prompts or tool results.')
        if any(key in data or key in body for key in ('models', 'plugins', 'server_tools', 'preset', 'route', 'transforms')):
            raise OpenRouterFailure('Free-only OpenRouter does not allow model fallbacks, plugins or server tools.')
        info = self.capabilities(body.get('model'))
        tools = body.get('tools') or []
        if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('type') != 'function' or not isinstance(t.get('function'), dict) for t in tools):
            raise OpenRouterFailure('Free-only OpenRouter accepts Forge client function tools only.')
        if tools and 'tools' not in info['capabilities']:
            raise OpenRouterFailure('Free-only OpenRouter model does not advertise tool calling.')
        policy = self.config.get('data_collection', 'deny')
        if policy not in ('allow', 'deny'):
            raise OpenRouterFailure('Free-only OpenRouter has an invalid data collection policy.')
        safe = {k: v for k, v in body.items() if k in ('model', 'messages', 'stream', 'stream_options', 'max_tokens', 'temperature', 'tools')}
        original=[message for message in data.get('messages',[]) if message.get('role')=='assistant']
        normalized=[message for message in safe.get('messages',[]) if message.get('role')=='assistant']
        if len(original)!=len(normalized):raise OpenRouterFailure('Provider continuation message binding failed.',category='protocol')
        for source,message in zip(original,normalized):
            private=source.get('_provider_state')
            thinking=message.pop('thinking',None)
            if private:
                if private.get('complete') is False:
                    raise OpenRouterFailure(INCOMPLETE_PROVIDER_RECOVERY,category='incomplete_provider_state')
                if private.get('provider_id')!='openrouter' or private.get('model_id')!=safe['model']:
                    raise OpenRouterFailure('This reasoning continuation belongs to another model. Start a fresh assignment instead of replaying provider state.',category='continuation_unavailable')
                message['reasoning_details']=reasoning_details(private['reasoning_details'])
            elif thinking:message['reasoning']=thinking
        if state_size(data.get('messages',[])):
            plain=[{key:value for key,value in message.items() if key not in ('images','_provider_state')} for message in data['messages']]
            if len(json.dumps(plain,ensure_ascii=False).encode('utf-8'))+state_size(data['messages'])>history_character_limit(data.get('context',8192)):
                raise OpenRouterFailure('The complete provider reasoning state exceeds this request size limit. Increase Context or start a fresh assignment; required state was not truncated.',category='context')
            total=self.estimate_tokens(data['messages'],data.get('tools',[]))
            if total>prompt_budget(data.get('context',8192),response_budget(data.get('context',8192),data.get('tokens',2048))):
                raise OpenRouterFailure('The complete provider reasoning state cannot fit this Context. Increase Context or start a fresh assignment; required state was not truncated.',category='context')
        if type(data.get('thinking')) is bool and (safe.get('model')==FREE_ROUTER or 'thinking' in info['capabilities']):
            if info.get('reasoning',{}).get('mandatory') and data['thinking'] is False:
                raise OpenRouterFailure('This free model requires reasoning. Enable reasoning or choose another advertised free model.',category='configuration')
            safe['reasoning']={'enabled':data['thinking']}
        if 'response_format' in body:
            if 'structured_outputs' not in info['capabilities']:
                raise OpenRouterFailure('Free-only OpenRouter model does not advertise structured output support.')
            safe['response_format'] = validated_response_format(body['response_format'])
        safe['provider'] = {'max_price': {k: 0 for k in PRICE_FIELDS}, 'require_parameters': True,
                            'data_collection': policy, 'allow_fallbacks': False}
        return safe

    def generate(self, data, cancel):
        try:Core._agent_payload(data)
        except ValueError as exc:raise OpenRouterFailure(str(exc),category='context') from None
        try:
            yield from super().generate(data, cancel)
        except ValueError as exc:
            message = str(exc)
            from forge_cloud_policy import CloudPolicyRejected
            if isinstance(exc,(EngineAccessFailure,CloudPolicyRejected)):raise
            if isinstance(exc, OpenRouterFailure):
                raise
            match = re.search(r'HTTP (\d{3})', message)
            if match:
                code=int(match[1])
                raise OpenRouterFailure(self._http_error(code,inference=True),category=self._http_category(code),http_status=code) from None
            if cancel is not None and cancel.is_set():
                raise OpenRouterFailure('OpenRouter inference was cancelled; partial output is retained.',category='cancelled') from None
            if isinstance(exc,ProviderStreamFailure):
                if exc.http_status:
                    raise OpenRouterFailure(self._http_error(exc.http_status,inference=True),category=self._http_category(exc.http_status),http_status=exc.http_status) from None
                raise OpenRouterFailure('OpenRouter '+('refused this response.' if exc.category=='refusal' else 'returned an invalid or incomplete inference stream.')+' Saved tool work remains local.',category=exc.category) from None
            if isinstance(exc,InferenceTimeoutError):
                raise OpenRouterFailure('OpenRouter inference timed out. Saved work remains local; inspect the connection and resume after resolving the timeout.',category='timeout') from None
            # Provider error bodies may echo prompts, tool results or credentials.
            raise OpenRouterFailure('OpenRouter inference failed or returned invalid protocol data. Partial output is retained; no automatic retry or paid fallback was made.',category='protocol') from None

    def account(self):
        data = self._get('/key').get('data')
        if not isinstance(data, dict):
            raise OpenRouterFailure('OpenRouter returned invalid account metadata.')
        quota = data.get('free_model_daily_requests')
        cleaned = None
        if isinstance(quota, dict) and all(type(quota.get(k)) is int and 0 <= quota[k] <= 2_147_483_647 for k in ('used', 'limit', 'remaining')):
            cleaned = {k: quota[k] for k in ('used', 'limit', 'remaining')}
        return {'free_model_daily_requests': cleaned,
                'is_free_tier': data['is_free_tier'] if type(data.get('is_free_tier')) is bool else None,
                'quota_note': 'Free-model quota and availability can change. Account metadata does not guarantee free capacity.'}


class OpenRouterConnection:
    def __init__(self, service, http_client=None):
        self.service = service
        self.store = service.store
        self.vault = getattr(service, 'vault', None) or CredentialVault()
        self.http_client = http_client

    def _config(self):
        return next((p for p in self.store.entities('providers') if p['id'] == 'openrouter'), None)

    @staticmethod
    def _public(config):
        if not config:
            return {'id': 'openrouter', 'name': 'OpenRouter free', 'kind': 'openrouter', 'url': OPENROUTER_URL,
                    'configured': False, 'enabled': False, 'remote_consent': False, 'data_collection': 'deny', 'free_only': True, 'model': FREE_ROUTER}
        return {**config, 'configured': bool(config.get('credential_ref')), 'free_capacity':capacity(config)}

    def save(self, data):
        config = self._config() or {};previous=dict(config)
        if 'url' in data and str(data['url']).rstrip('/') != OPENROUTER_URL:
            raise OpenRouterFailure('Free-only OpenRouter uses its fixed official HTTPS endpoint.')
        config = {**config, 'id': 'openrouter', 'name': 'OpenRouter free', 'kind': 'openrouter', 'url': OPENROUTER_URL, 'free_only': True}
        for field, default in (('enabled', False), ('remote_consent', False)):
            value = data.get(field, config.get(field, default))
            if type(value) is not bool:
                raise OpenRouterFailure('OpenRouter consent and enable controls must be true or false.')
            config[field] = value
        config['data_collection'] = data.get('data_collection', config.get('data_collection', 'deny'))
        if config['data_collection'] not in ('allow', 'deny'):
            raise OpenRouterFailure('Choose the OpenRouter data collection policy explicitly.')
        if config['enabled'] and not config['remote_consent']:
            raise OpenRouterFailure('Consent to sending prompts, images and tool results to OpenRouter before enabling it.')
        key = data.get('api_key', data.get('key'))
        if key is not None:
            if not isinstance(key, str) or not 16 <= len(key) <= 1024 or any(c.isspace() for c in key):
                raise OpenRouterFailure('Enter a valid OpenRouter API key in the password field.')
        if config['enabled'] and not config.get('credential_ref') and key is None:
            raise OpenRouterFailure('Save an OpenRouter API key before enabling it.')
        model = data.get('model', config.get('model', FREE_ROUTER))
        if not isinstance(model, str) or not (model == FREE_ROUTER or re.fullmatch(r'[A-Za-z0-9_./-]+:free', model)):
            raise OpenRouterFailure('Choose the free router or an advertised :free model.')
        config['model'] = model
        config.setdefault('context_limit', 32768)
        credential=None;before=None
        if key is not None:
            before=provider_save_snapshot(self.store,'openrouter',previous,config)
            try:credential=config['credential_ref']=self.vault.put(key)
            except Exception:
                raise OpenRouterFailure('Credentials could not be saved in the operating system store. Try saving the connection again.',category='authentication') from None
            config.pop('account', None)
            config.pop('models', None)
            config['connected'] = False
            config['metadata_auth'] = {'state':'unverified','checked_at':None}
            config['catalog_verified_at'] = None
        try:saved = self.store.save_entity('providers', config)
        except Exception as exc:
            if credential is not None:discard_uncommitted_provider_credential(self.store,self.vault,credential,before)
            if isinstance(exc,(EngineAccessFailure,OpenRouterFailure)):raise
            raise OpenRouterFailure('The OpenRouter connection could not be saved. Try saving the connection again.') from None
        pool=getattr(self.service,'providers',None)
        if pool:
            if provider_authority(previous)!=provider_authority(saved):pool.authority_changed('openrouter')
            else:pool.invalidate('openrouter')
        return self._public(saved)

    def test(self):
        config = self._config()
        if not config or not config.get('credential_ref'):
            raise OpenRouterFailure('Save an OpenRouter API key before testing the connection.')
        pool=getattr(self.service,'providers',None)
        authority=None
        if pool:
            snapshot=getattr(self.store,'_provider_authority_snapshot',None)
            revision=snapshot('openrouter')[1] if snapshot else 0
            with pool.lock:generation=(pool.authority_epoch,pool.authority_revisions.get('openrouter',0))
            authority=BoundSavedProviderAuth(self.store,self.vault,'openrouter',config,revision=revision,
                current_generation=lambda:pool._generation_current('openrouter',generation),stopped=pool.stopped)
        provider = OpenRouterProvider(config, self.vault, self.http_client,authority=authority)
        stamp=_now()
        authenticated=False
        try:
            account = provider.account()
            authenticated=True
            config={**config,'account':account,'account_checked_at':stamp,
                    'metadata_auth':{'state':'verified','checked_at':stamp}}
            models = provider.models(refresh=True)
        except OpenRouterFailure as exc:
            self.store.save_entity('providers',{**config,'connected':False,
                'metadata_auth':{'state':'verified' if authenticated else 'error','checked_at':stamp,
                                 **({} if authenticated else {'error':str(exc)})},
                'catalog_error':str(exc) if authenticated else None,'connection_checked_at':stamp})
            raise
        saved = self.store.save_entity('providers', {**config, 'connected': True, 'account': account, 'models': models,
            'metadata_auth':{'state':'verified','checked_at':stamp},'account_checked_at':stamp,
            'catalog_verified_at':stamp,'connection_checked_at':stamp,'catalog_error':None})
        return self._public(saved)

    def dispatch(self, action, data=None):
        data = data or {}
        if action == 'openrouter_status':
            return {'provider': self._public(self._config())}
        if action == 'openrouter_save':
            return {'provider': self.save(data)}
        if action == 'openrouter_test':
            return {'provider': self.test()}
        raise OpenRouterFailure('Unknown OpenRouter connection action.')
