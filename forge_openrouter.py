"""Explicitly consented, zero-price OpenRouter inference and local connection UI.

Only the official HTTPS endpoint receives the OS-vault API key. Saving a key or
reading status is local; connection tests fetch metadata without a prompt.
"""
from decimal import Decimal, InvalidOperation
import re
import threading
import time

import httpx

from forge_credentials import CredentialVault
from forge_inference import CompatibleProvider, validated_response_format


OPENROUTER_URL = 'https://openrouter.ai/api/v1'
FREE_ROUTER = 'openrouter/free'
PRICE_FIELDS = frozenset(('prompt', 'completion', 'request', 'image'))
REQUIRED_PRICE_FIELDS = frozenset(('prompt', 'completion'))
CONDITION_FIELDS = frozenset(('min_prompt_tokens', 'utc_start', 'utc_end', 'utc_days'))


class OpenRouterFailure(ValueError):
    """A local, scrubbed error; remote error messages never gain this type."""


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
        title = item.get('name')
        title = title[:200] if isinstance(title, str) else name
        result.append({'name': name, 'model': name, 'title': title, 'provider': 'openrouter',
                       'capabilities': capabilities, 'context_length': context, 'free': True,
                       'pricing': {k: '0' for k in PRICE_FIELDS}})
    return sorted(result, key=lambda m: (m['name'] != FREE_ROUTER, m['name']))


class OpenRouterProvider(CompatibleProvider):
    remote = True
    remote_inference = True

    def __init__(self, config, vault=None, http_client=None):
        if config.get('url', OPENROUTER_URL).rstrip('/') != OPENROUTER_URL:
            raise OpenRouterFailure('Free-only OpenRouter uses its fixed official HTTPS endpoint.')
        super().__init__({**config, 'url': OPENROUTER_URL}, vault)
        self.http_client = http_client
        self.catalog = None
        self.catalog_at = 0
        self.catalog_lock = threading.RLock()

    def _headers(self):
        try:
            token = self.vault.get(self.config.get('credential_ref')) if self.vault else None
        except Exception:
            raise OpenRouterFailure('OpenRouter key is unavailable in the OS credential store.') from None
        if not token:
            raise OpenRouterFailure('OpenRouter needs an API key saved in Forge Settings.')
        return {'Authorization': 'Bearer ' + token, 'X-OpenRouter-Title': 'Forge Local AI'}

    @staticmethod
    def _http_error(status):
        if status in (401, 403):
            return 'OpenRouter rejected the API key. Update it in Forge Settings.'
        if status == 429:
            return 'OpenRouter free quota or capacity is limited. No automatic retry was made; check quota and retry later.'
        if status == 402:
            return 'OpenRouter rejected this free-only request. No paid model or credit purchase is required by Forge.'
        return 'OpenRouter metadata is unavailable. Check the connection and retry later.'

    def _get(self, suffix):
        headers = self._headers()
        try:
            if self.http_client:
                response = self.http_client.get(OPENROUTER_URL + suffix, headers=headers)
            else:
                with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                    response = client.get(OPENROUTER_URL + suffix, headers=headers)
            if not 200 <= response.status_code < 300:
                raise OpenRouterFailure(self._http_error(response.status_code))
            value = response.json()
            if not isinstance(value, dict):
                raise OpenRouterFailure('OpenRouter returned invalid metadata.')
            return value
        except httpx.HTTPError:
            raise OpenRouterFailure('OpenRouter connection failed. No automatic retry was made.') from None
        except (ValueError, TypeError, RecursionError) as exc:
            if isinstance(exc, OpenRouterFailure):
                raise
            raise OpenRouterFailure('OpenRouter returned invalid metadata.') from None

    def models(self, refresh=False):
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
                'context_length': info['context_length'], 'free': True, 'remote': True}

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
        if 'response_format' in body:
            if 'structured_outputs' not in info['capabilities']:
                raise OpenRouterFailure('Free-only OpenRouter model does not advertise structured output support.')
            safe['response_format'] = validated_response_format(body['response_format'])
        safe['provider'] = {'max_price': {k: 0 for k in PRICE_FIELDS}, 'require_parameters': True,
                            'data_collection': policy, 'allow_fallbacks': False}
        return safe

    def generate(self, data, cancel):
        try:
            yield from super().generate(data, cancel)
        except ValueError as exc:
            message = str(exc)
            if isinstance(exc, OpenRouterFailure):
                raise
            match = re.search(r'HTTP (\d{3})', message)
            if match:
                raise OpenRouterFailure(self._http_error(int(match[1]))) from None
            if cancel is not None and cancel.is_set():
                raise OpenRouterFailure('OpenRouter inference was cancelled; partial output is retained.') from None
            # Provider error bodies may echo prompts, tool results or credentials.
            raise OpenRouterFailure('OpenRouter inference failed or no free provider matched the requested privacy and capabilities. Partial output is retained; no automatic retry or paid fallback was made.') from None

    def account(self):
        data = self._get('/key').get('data')
        if not isinstance(data, dict):
            raise OpenRouterFailure('OpenRouter returned invalid account metadata.')
        quota = data.get('free_model_daily_requests')
        cleaned = None
        if isinstance(quota, dict) and all(type(quota.get(k)) is int and quota[k] >= 0 for k in ('used', 'limit', 'remaining')):
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
        return {**config, 'configured': bool(config.get('credential_ref'))}

    def save(self, data):
        config = self._config() or {}
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
            config['credential_ref'] = self.vault.put(key)
            config.pop('account', None)
            config.pop('models', None)
            config['connected'] = False
        if config['enabled'] and not config.get('credential_ref'):
            raise OpenRouterFailure('Save an OpenRouter API key before enabling it.')
        model = data.get('model', config.get('model', FREE_ROUTER))
        if not isinstance(model, str) or not (model == FREE_ROUTER or re.fullmatch(r'[A-Za-z0-9_./-]+:free', model)):
            raise OpenRouterFailure('Choose the free router or an advertised :free model.')
        config['model'] = model
        config.setdefault('context_limit', 32768)
        saved = self.store.save_entity('providers', config)
        return self._public(saved)

    def test(self):
        config = self._config()
        if not config or not config.get('credential_ref'):
            raise OpenRouterFailure('Save an OpenRouter API key before testing the connection.')
        provider = OpenRouterProvider(config, self.vault, self.http_client)
        account = provider.account()
        models = provider.models(refresh=True)
        saved = self.store.save_entity('providers', {**config, 'connected': True, 'account': account, 'models': models})
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
