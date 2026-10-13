"""Private, lazy authentication for an exact saved Ollama connection."""
import hashlib
import json

COMPATIBLE_KINDS=frozenset(('openai-compatible','openai','compatible'))

def protocol_kind(kind):return 'openai-compatible' if kind in COMPATIBLE_KINDS else kind


class EngineAccessFailure(ValueError):
    """A refusal before HTTP admission; public text contains no private values."""
    def __init__(self, category, *, provider_id='ollama'):
        self.category = category
        self.provider_id = provider_id if provider_id in ('ollama','openai-compatible','openrouter') else 'openai-compatible'
        self.not_executed = True
        messages = {
            'configuration': 'The engine connection changed. Retry with the current saved engine. No request was sent.',
            'authentication': 'Saved engine credentials could not be read or are invalid. Save a valid key and retry. No request was sent.',
            'cancelled': 'The engine request was cancelled before it was sent.',
            'consent': 'OpenRouter cloud consent was revoked. Enable explicit cloud consent before starting a fresh request. No request was sent.',
        }
        super().__init__(messages[category])

    def diagnostics(self):
        return {'provider_id': self.provider_id, 'category': self.category, 'not_executed': True}


class EngineRunBindingFailure(EngineAccessFailure):
    def __init__(self):
        super().__init__('configuration')
        self.args = ('The original engine connection cannot be verified for this saved task. '
                     'Review its checkpoint and start a fresh request. Completed actions were retained '
                     'and will not be replayed.',)


def _signature(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False).encode('utf-8')).digest()


def provider_authority(config):
    """Configured authority only; catalog/account/test/display facts grant nothing."""
    fields = ('id','kind','url','credential_ref','enabled','remote_consent','data_collection',
              'free_only','capabilities','context_limit','preset_id','resource_id','independent_resource',
              'managed','runtime_id')
    return {key:config.get(key) for key in fields}


def authority_signature(config,revision=0):
    # Existing version5 Ollama journals retain their exact original algorithm.
    return _signature(config if config.get('kind')=='ollama' else {'profile':provider_authority(config),'revision':revision})


def provider_save_snapshot(store,identifier,previous,current):
    """Validate local authority before minting a credential; retain no key bytes."""
    kind=current.get('kind')
    try:
        validator=getattr(store,'_provider_authority_revision',None)
        connection=getattr(store,'_connection',None)
        if any(profile.get('kind') in (*COMPATIBLE_KINDS,'openrouter') for profile in (previous,current)) and callable(validator):
            with connection(transaction='read') as db:validator(db,identifier)
        profiles=store.entities('providers')
        if not isinstance(profiles,list) or any(not isinstance(profile,dict) for profile in profiles):raise ValueError()
        # The unchanged readback is a conservative proof of an uncommitted save.
        return json.loads(json.dumps(profiles,sort_keys=True,allow_nan=False))
    except EngineAccessFailure:raise
    except Exception:raise EngineAccessFailure('configuration',provider_id=kind) from None


def discard_uncommitted_provider_credential(store,vault,reference,before):
    """Delete only our fresh, unreferenced key after a conclusively failed save."""
    import re
    try:
        if not isinstance(reference,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',reference):return
        if any(profile.get('credential_ref')==reference for profile in before):return
        after=store.entities('providers')
        if not isinstance(after,list) or any(not isinstance(profile,dict) for profile in after):return
        if any(profile.get('credential_ref')==reference for profile in after):return
        # A committed write, another saved owner, or uncertain readback retains it.
        if sorted(json.dumps(profile,sort_keys=True,allow_nan=False) for profile in after)!=sorted(
                json.dumps(profile,sort_keys=True,allow_nan=False) for profile in before):return
        vault.delete(reference)
    except Exception:
        # Credential cleanup cannot replace the fixed original failure.
        return


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
            raise self._failure('cancelled')
        try:
            current = self._current_generation() is True
        except Exception:
            current = False
        if not current:
            raise self._failure('configuration')

    def _failure(self,category):return EngineAccessFailure(category)

    def _check(self, url, cancel):
        self._signals(cancel)
        try:
            profile = self._store.entity('providers', self._identifier)
            accepted = url in self._urls and _signature(profile) == self._signature
        except Exception:
            accepted = False
        self._signals(cancel)
        if not accepted:
            raise self._failure('configuration')

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
            raise self._failure('authentication') from None
        self._check(url, cancel)
        return {'Authorization': 'Bearer ' + token}

    def binding_signature(self):
        self._check(next(iter(self._urls)), None)
        return self._signature.hex()


class BoundSavedProviderAuth(BoundOllamaAuth):
    """Private runtime authority for a saved Compatible or fixed OpenRouter profile."""
    __slots__ = ('_kind','_admission_url')

    def __init__(self,store,vault,identifier,config,*,current_generation,stopped=None,revision=0):
        from forge_provider_probe import validate_endpoint
        from urllib.parse import urlsplit,urlunsplit
        try:
            kind=config.get('kind')
            if config.get('id')!=identifier or kind not in (*COMPATIBLE_KINDS,'openrouter'):raise ValueError()
            base=validate_endpoint(config.get('url'))
            if kind=='openrouter' and base!='https://openrouter.ai/api/v1':raise ValueError()
            reference=config.get('credential_ref')
            if reference not in (None,'') and (not isinstance(reference,str) or len(reference)>512
                    or any(ord(char)<32 for char in reference)):raise ValueError()
            signature=authority_signature(config,revision)
            urls={base+'/models',base+'/chat/completions'}
            if kind=='openrouter':urls.add(base+'/key')
            elif config.get('preset_id')=='lm-studio' and urlsplit(base).path=='/v1':
                parsed=urlsplit(base);urls.add(urlunsplit((parsed.scheme,parsed.netloc,'/api/v1/models','','')))
        except Exception:raise EngineAccessFailure('configuration',provider_id='openai-compatible') from None
        self._store,self._vault,self._identifier=store,vault,identifier
        self._signature,self._reference=signature,reference
        self._urls=frozenset(urls);self._kind=kind;self._admission_url=base+'/chat/completions'
        self._current_generation=current_generation;self._stopped=stopped or (lambda:False)

    def _failure(self,category):return EngineAccessFailure(category,provider_id=self._kind)

    def _check(self,url,cancel):
        self._signals(cancel)
        try:
            snapshot=getattr(self._store,'_provider_authority_snapshot',None)
            profile,revision=snapshot(self._identifier) if snapshot else (self._store.entity('providers',self._identifier),0)
        except Exception:raise self._failure('configuration') from None
        self._signals(cancel)
        if self._kind=='openrouter' and url==self._admission_url and profile.get('remote_consent') is not True:
            raise self._failure('consent')
        try:accepted=url in self._urls and authority_signature(profile,revision)==self._signature
        except Exception:accepted=False
        self._signals(cancel)
        if not accepted or url==self._admission_url and profile.get('enabled',self._kind!='openrouter') is not True:
            raise self._failure('configuration')
        if self._kind=='openrouter' and not self._reference:raise self._failure('authentication')

    def binding_signature(self):
        self._check(self._admission_url,None)
        return self._signature.hex()
