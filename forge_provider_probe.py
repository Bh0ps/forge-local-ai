"""Test an engine draft without saving configuration or credentials."""
from urllib.parse import urlparse

import httpx


def validate_endpoint(url):
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ValueError('Enter an engine HTTP(S) base URL.')
    url = url.strip().rstrip('/')
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and parsed.port != 0
                 and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                 and not any(ord(character) < 32 for character in url))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use an HTTP(S) endpoint without embedded credentials, query parameters or fragments.')
    return url


def validate_api_key(value):
    if value is not None and (not isinstance(value, str) or len(value) > 8192
            or any(ord(character) < 32 for character in value)):
        raise ValueError('Enter a valid API key.')
    return value or None


def probe_provider(service, data):
    from forge_engine_auth import EngineAccessFailure
    from forge_provider_metadata import ProviderProbeRevoked, fetch_json, fetch_provider_metadata, require_current
    from forge_provider_presets import validate_preset

    draft = data.get('draft')
    if not isinstance(draft, dict) or draft.get('kind') not in ('ollama', 'openai-compatible'):
        raise ValueError('Choose Ollama or an OpenAI-compatible engine to test.')
    url = validate_endpoint(draft.get('url'))
    token = validate_api_key(data.get('api_key'))
    if 'metadata' in data and type(data['metadata']) is not bool:
        raise ValueError('Metadata testing must be true or false.')
    saved = None
    identifier = data.get('id')
    if identifier:
        identifier = data['id']
        if not isinstance(identifier, str) or len(identifier) > 255:
            raise ValueError('Select an existing engine or test a new endpoint.')
        saved = (dict(kind='ollama', url=service.providers.core.base) if identifier == 'ollama'
                 else service.store.entity('providers', identifier))
    preset = draft.get('preset_id', saved.get('preset_id') if saved else None)
    # Old editors may change protocol without knowing about saved presets.
    if 'preset_id' not in draft and saved and saved.get('kind') != draft['kind']:
        preset = None
    preset = validate_preset(preset, draft['kind'])

    def current():
        if saved is None:
            return True
        try:
            now = (dict(kind='ollama', url=service.providers.core.base) if identifier == 'ollama'
                   else service.store.entity('providers', identifier))
            return now == saved
        except Exception:
            return False

    cancel = getattr(service, 'stop_event', None)
    require_current(cancel, current)
    if not token and saved:
        # A key saved for one endpoint must never be forwarded to an edited one.
        if (saved.get('kind') == draft['kind'] and saved.get('url', '').rstrip('/') == url
                and saved.get('credential_ref')):
            if not service.vault:
                raise EngineAccessFailure('authentication')
            try:
                token = validate_api_key(service.vault.get(saved['credential_ref']))
            except Exception:
                require_current(cancel, current)
                raise EngineAccessFailure('authentication') from None
            require_current(cancel, current)
            if not token or any(ord(char) > 126 or ord(char) == 127 for char in token):
                raise EngineAccessFailure('authentication')
    config = dict(url=url, kind=draft['kind'], preset_id=preset)
    catalog_url = ((url if url.endswith('/api') else url + '/api') + '/tags'
                   if draft['kind'] == 'ollama' else url + '/models')
    try:
        catalog = fetch_json(catalog_url, token=token, cancel=cancel, current=current)
        models = catalog.get('models' if draft['kind'] == 'ollama' else 'data')
        if not isinstance(models, list) or len(models) > 10000:
            raise ValueError('Invalid catalog')
        for item in models:
            if not isinstance(item, dict):
                raise ValueError('Invalid catalog')
            name = (item.get('name', item.get('model')) if draft['kind'] == 'ollama' else item.get('id'))
            if not isinstance(name, str) or not name or len(name) > 512 or any(ord(char) < 32 for char in name):
                raise ValueError('Invalid catalog')
    except ProviderProbeRevoked:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = ('Authentication failed. Check the API key.' if status in (401, 403) else
                   'Model catalog not found. Check the engine base URL.' if status == 404 else
                   'The engine could not provide its model catalog. Check its status and try again.')
        raise ValueError(message) from None
    except httpx.TimeoutException:
        raise ValueError('The connection timed out. Start the engine and try again.') from None
    except httpx.RequestError:
        raise ValueError('Could not connect. Check the engine address and that it is running.') from None
    except Exception:
        raise ValueError('The engine returned an invalid model catalog. Check its status and base URL.') from None
    # Counts are sufficient for setup. Remote responses and raw errors are not diagnostics.
    result = {'ok': True, 'model_count': len(models)}
    if data.get('metadata') is True:
        result['metadata'] = fetch_provider_metadata(config, token=token, cancel=cancel, current=current)
    require_current(cancel, current)
    return result
