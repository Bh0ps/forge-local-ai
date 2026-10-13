"""Private, lazy authentication for an exact saved Ollama connection."""
import hashlib
import json


class EngineAccessFailure(ValueError):
    """A refusal before HTTP admission; public text contains no private values."""
    def __init__(self, category):
        self.category = category
        self.not_executed = True
        messages = {
            'configuration': 'The engine connection changed. Retry with the current saved engine. No request was sent.',
            'authentication': 'Saved engine credentials could not be read or are invalid. Save a valid key and retry. No request was sent.',
            'cancelled': 'The engine request was cancelled before it was sent.',
        }
        super().__init__(messages[category])

    def diagnostics(self):
        return {'provider_id': 'ollama', 'category': self.category, 'not_executed': True}


class EngineRunBindingFailure(EngineAccessFailure):
    def __init__(self):
        super().__init__('configuration')
        self.args = ('The original engine connection cannot be verified for this saved task. '
                     'Review its checkpoint and start a fresh request. Completed actions were retained '
                     'and will not be replayed.',)


def _signature(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False).encode('utf-8')).digest()


class BoundOllamaAuth:
    """Retain binding facts only; never retain a fetched credential."""
    __slots__ = ('_store', '_vault', '_identifier', '_signature', '_reference',
                 '_urls', '_current_generation', '_stopped')

    def __init__(self, store, vault, identifier, config, *, current_generation, stopped=None):
        from forge_provider_probe import validate_endpoint
        try:
            if config.get('id') != identifier or config.get('kind') != 'ollama':
                raise ValueError()
            base = validate_endpoint(config.get('url'))
            base = base if base.endswith('/api') else base + '/api'
            reference = config.get('credential_ref')
            if reference not in (None, '') and (not isinstance(reference, str)
                    or len(reference) > 512 or any(ord(char) < 32 for char in reference)):
                raise ValueError()
            signature = _signature(config)
        except Exception:
            raise EngineAccessFailure('configuration') from None
        self._store, self._vault, self._identifier = store, vault, identifier
        self._signature, self._reference = signature, reference
        self._urls = frozenset(base + path for path in
            ('/tags', '/show', '/chat', '/ps', '/version', '/generate', '/pull', '/delete'))
        self._current_generation = current_generation
        self._stopped = stopped or (lambda: False)

    def _signals(self, cancel):
        try:
            cancelled = cancel is not None and cancel.is_set()
            stopped = self._stopped() is not False
        except Exception:
            cancelled = True
            stopped = True
        if cancelled or stopped:
            raise EngineAccessFailure('cancelled')
        try:
            current = self._current_generation() is True
        except Exception:
            current = False
        if not current:
            raise EngineAccessFailure('configuration')

    def _check(self, url, cancel):
        self._signals(cancel)
        try:
            profile = self._store.entity('providers', self._identifier)
            accepted = url in self._urls and _signature(profile) == self._signature
        except Exception:
            accepted = False
        self._signals(cancel)
        if not accepted:
            raise EngineAccessFailure('configuration')

    def resolve(self, url, cancel=None):
        self._check(url, cancel)
        if not self._reference:
            self._check(url, cancel)
            return {}
        try:
            token = self._vault.get(self._reference) if self._vault is not None else None
            if (not isinstance(token, str) or not token or len(token) > 8192
                    or any(ord(char) < 32 or ord(char) > 126 for char in token)):
                raise ValueError()
        except Exception:
            # Cancellation/configuration changes take precedence over vault failure.
            self._check(url, cancel)
            raise EngineAccessFailure('authentication') from None
        self._check(url, cancel)
        return {'Authorization': 'Bearer ' + token}

    def binding_signature(self):
        self._check(next(iter(self._urls)), None)
        return self._signature.hex()
