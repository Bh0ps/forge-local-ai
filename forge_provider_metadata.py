"""Bounded read-only advertised facts; no model aliases, loads or capability grants."""
import json
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
    """LM Studio v1 declarations only; unavailable/missing facts remain unknown.

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
