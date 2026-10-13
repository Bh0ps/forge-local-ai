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
    from core import Core
    from forge_inference import CompatibleProvider, OllamaProvider

    draft = data.get('draft')
    if not isinstance(draft, dict) or draft.get('kind') not in ('ollama', 'openai-compatible'):
        raise ValueError('Choose Ollama or an OpenAI-compatible engine to test.')
    url = validate_endpoint(draft.get('url'))
    token = validate_api_key(data.get('api_key'))
    if not token and data.get('id'):
        identifier = data['id']
        if not isinstance(identifier, str) or len(identifier) > 255:
            raise ValueError('Select an existing engine or test a new endpoint.')
        saved = (dict(kind='ollama', url=service.providers.core.base) if identifier == 'ollama'
                 else service.store.entity('providers', identifier))
        # A key saved for one endpoint must never be forwarded to an edited one.
        if (saved.get('kind') == draft['kind'] and saved.get('url', '').rstrip('/') == url
                and saved.get('credential_ref')):
            if not service.vault:
                raise ValueError('Saved credentials are unavailable. Enter a key to test this connection.')
            try:
                token = validate_api_key(service.vault.get(saved['credential_ref']))
            except Exception:
                raise ValueError('Saved credentials could not be read. Enter a key to test this connection.') from None

    class EphemeralCredential:
        def get(self, reference):
            return token if reference == 'probe' else None

    config = dict(url=url, kind=draft['kind'], credential_ref='probe' if token else None)
    provider = (OllamaProvider(Core(base=url if url.endswith('/api') else url + '/api'))
                if draft['kind'] == 'ollama' else CompatibleProvider(config, EphemeralCredential()))
    try:
        models = provider.models()
        if not isinstance(models, list) or len(models) > 10000:
            raise ValueError('Invalid catalog')
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
    return {'ok': True, 'model_count': len(models)}
