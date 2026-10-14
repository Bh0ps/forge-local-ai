"""Bounded read-only advertised facts; no model aliases, loads or capability grants."""
import json
import ipaddress
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from forge_provider_presets import validate_preset


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_METADATA_MODELS = 256
MAX_LOADED_INSTANCES = 64
REASONING_OPTIONS = ('off', 'on', 'low', 'medium', 'high')


class ProviderProbeRevoked(ValueError):
    pass


class DockerModelRunnerMetadataInvalid(ValueError):
    def __init__(self):
        super().__init__('Docker Model Runner could not verify its local catalog. Check the loopback llama.cpp endpoint and local models, then refresh models.')


def docker_model_runner_endpoint(url):
    """An explicit local route, never a guessed cloud/default backend or alias."""
    from forge_provider_probe import validate_endpoint
    try:
        url = validate_endpoint(url)
        parsed = urlsplit(url)
        local = parsed.hostname == 'localhost'
        if not local:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        if not local or parsed.path != '/engines/llama.cpp/v1':
            raise ValueError()
        return url
    except (ValueError, TypeError):
        raise DockerModelRunnerMetadataInvalid() from None


def docker_model_runner_catalog(value):
    """Validate the local OpenAI list. Package hints grant no serving facts.

    Docker's explicit llama.cpp route resolves GetLocal before inference. The
    catalog is only an advertised local package list, not a served-model proof.
    Unknown routing extensions (including cloud descriptors) are refused.
    """
    invalid = DockerModelRunnerMetadataInvalid
    if (not isinstance(value, dict) or set(value) != {'object', 'data'}
            or value.get('object') != 'list' or not isinstance(value.get('data'), list)
            or len(value['data']) > MAX_METADATA_MODELS):
        raise invalid()
    models, seen = [], set()
    for entry in value['data']:
        if (not isinstance(entry, dict) or set(entry) - {'id', 'object', 'created', 'owned_by', 'dmr'}
                or not {'id', 'object', 'created', 'owned_by'} <= set(entry)
                or not _identifier(entry['id']) or any(ord(char) == 127 for char in entry['id'])
                or entry['id'] in seen or entry['object'] != 'model' or entry['owned_by'] != 'docker'
                or type(entry['created']) is not int or not 0 <= entry['created'] <= 2**63 - 1):
            raise invalid()
        if 'dmr' in entry:
            hints = entry['dmr']
            if (not isinstance(hints, dict)
                    or set(hints) - {'context_window', 'architecture', 'parameters', 'quantization', 'size'}):
                raise invalid()
            context = hints.get('context_window')
            if 'context_window' in hints and (type(context) is not int
                    or not -2**31 <= context <= 2**31 - 1):
                raise invalid()
            for field in ('architecture', 'parameters', 'quantization', 'size'):
                if field in hints and (not isinstance(hints[field], str) or len(hints[field]) > 512
                        or any(ord(char) < 32 or ord(char) == 127 for char in hints[field])):
                    raise invalid()
        seen.add(entry['id'])
        models.append({'id': entry['id']})
    return models


def docker_model_runner_facts(models, model=None):
    # Maximum and active context are deliberately unknown: dmr.context_window
    # is package configuration, and runtime overrides can change it.
    return {'state': 'available', 'source': 'advertised', 'models': [
        {'model': entry['id'], 'max_context_length': None, 'loaded_context_lengths': [],
         'loaded_instances': [], 'vision': None, 'trained_for_tool_use': None, 'reasoning_options': []}
        for entry in models if model is None or entry['id'] == model]}


def require_current(cancel=None, current=None):
    if cancel is not None:
        try:
            cancelled = cancel.is_set()
        except Exception:
            cancelled = True
        if cancelled:
            raise ProviderProbeRevoked('The connection test was cancelled.')
    if current is not None:
        try:
            accepted = current() is True
        except Exception:
            accepted = False
        if not accepted:
            raise ProviderProbeRevoked('The engine configuration changed. Test the current connection again.')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Invalid engine metadata.')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Invalid engine metadata.')


def fetch_json(url, *, token=None, cancel=None, current=None, client_factory=None,
               limit=MAX_JSON_BYTES,headers_factory=None):
    """Read the exact supplied endpoint; never follow its URLs or redirects."""
    require_current(cancel, current)
    deadline = time.monotonic() + 10
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    factory = client_factory or httpx.Client
    with factory(timeout=httpx.Timeout(10, connect=3, read=2), trust_env=False,
                 follow_redirects=False) as client:
        require_current(cancel,current)
        if headers_factory is not None:headers=headers_factory(url)
        require_current(cancel,current)
        with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            declared = response.headers.get('content-length', '')
            if declared.isdecimal() and int(declared) > limit:
                raise ValueError('Invalid engine metadata.')
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=4096):
                require_current(cancel, current)
                if time.monotonic() >= deadline or len(raw) + len(chunk) > limit:
                    raise ValueError('Invalid engine metadata.')
                raw.extend(chunk)
    require_current(cancel, current)
    if time.monotonic() >= deadline:
        raise ValueError('Invalid engine metadata.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError('Invalid engine metadata.')
    return value


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(char) < 32 for char in value)


def textgen_catalog(value):
    """Textgen advertises only its current loaded model, never loader authority."""
    models = value.get('data') if isinstance(value, dict) else None
    if (not isinstance(models, list) or len(models) > 1
            or any(not isinstance(item, dict) or not _identifier(item.get('id')) for item in models)):
        raise ValueError('Textgen returned an invalid loaded-model catalog.')
    return [{'id': item['id']} for item in models]


def _context(value):
    return value if type(value) is int and 1 <= value <= 2**31 - 1 else None


def _boolean(value):
    return value if type(value) is bool else None


def _model(entry):
    if not isinstance(entry, dict) or entry.get('type') != 'llm' or not _identifier(entry.get('key')):
        return None
    loaded = entry.get('loaded_instances')
    if not isinstance(loaded, list) or len(loaded) > MAX_LOADED_INSTANCES:
        loaded = []
    instances = []
    for item in loaded:
        if not isinstance(item, dict) or not _identifier(item.get('id')):
            continue
        config = item.get('config')
        instances.append({'model': item['id'], 'context_length': _context(config.get('context_length'))
            if isinstance(config, dict) else None})
    # A duplicate instance ID cannot assert an unambiguous loaded context.
    seen, duplicates = set(), set()
    for item in instances:
        if item['model'] in seen:
            duplicates.add(item['model'])
        seen.add(item['model'])
    instances = [item for item in instances if item['model'] not in duplicates]
    capabilities = entry.get('capabilities')
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    reasoning = capabilities.get('reasoning')
    allowed = reasoning.get('allowed_options') if isinstance(reasoning, dict) else None
    options = ([option for option in REASONING_OPTIONS if option in allowed]
        if isinstance(allowed, list) and len(allowed) <= len(REASONING_OPTIONS)
            and all(type(option) is str and option in REASONING_OPTIONS for option in allowed) else [])
    return {'model': entry['key'], 'max_context_length': _context(entry.get('max_context_length')),
        'loaded_context_lengths': sorted({item['context_length'] for item in instances if item['context_length'] is not None}),
        'loaded_instances': instances, 'vision': _boolean(capabilities.get('vision')),
        'trained_for_tool_use': _boolean(capabilities.get('trained_for_tool_use')), 'reasoning_options': options}


def fetch_provider_metadata(config, *, token=None, model=None, cancel=None, current=None, client_factory=None,headers_factory=None):
    """Read bounded declarations; unavailable/missing serving facts stay unknown.

    `model` matches an exact catalog key or loaded instance ID. Loaded context
    remains instance-specific; callers must not select an arbitrary minimum or
    maximum when more than one instance can serve the requested identifier.
    """
    from forge_provider_probe import validate_api_key, validate_endpoint
    from forge_engine_auth import protocol_kind
    identifier = validate_preset(config.get('preset_id'), protocol_kind(config.get('kind')))
    unknown = {'state': 'unknown', 'source': 'unknown', 'models': []}
    require_current(cancel, current)
    if model is not None and not _identifier(model):
        raise ValueError('Select an exact model identifier.')
    if identifier == 'docker-model-runner':
        url = docker_model_runner_endpoint(config.get('url'))
        models = docker_model_runner_catalog(fetch_json(url + '/models', token=token, cancel=cancel,
            current=current, client_factory=client_factory, headers_factory=headers_factory))
        require_current(cancel, current)
        return docker_model_runner_facts(models, model)
    if identifier != 'lm-studio':
        return {'state': 'unsupported', 'source': 'unknown', 'models': []}
    url = validate_endpoint(config.get('url'))
    parsed = urlsplit(url)
    # A reverse-proxy prefix is not evidence that its root hosts this API.
    if parsed.path != '/v1':
        return unknown
    metadata_url = urlunsplit((parsed.scheme, parsed.netloc, '/api/v1/models', '', ''))
    token = validate_api_key(token)
    try:
        value = fetch_json(metadata_url, token=token, cancel=cancel, current=current, client_factory=client_factory,headers_factory=headers_factory)
        entries = value.get('models')
        if not isinstance(entries, list) or len(entries) > MAX_METADATA_MODELS:
            return unknown
        models = [fact for entry in entries if (fact := _model(entry)) is not None]
        if len({fact['model'] for fact in models}) != len(models):
            return unknown
        if model is not None:
            models = [fact for fact in models if fact['model'] == model or any(
                item['model'] == model for item in fact['loaded_instances'])]
            if len(models) != 1:
                return unknown
        require_current(cancel, current)
        return {'state': 'available', 'source': 'advertised', 'models': models}
    except ProviderProbeRevoked:
        raise
    except Exception as exc:
        from forge_engine_auth import EngineAccessFailure
        if isinstance(exc,EngineAccessFailure):raise
        require_current(cancel,current)
        return unknown
