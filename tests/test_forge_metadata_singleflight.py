from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from forge_inference import OllamaProvider, ProviderPool
from forge_store import ForgeStore


@pytest.mark.parametrize('method', ['models', 'capabilities'])
def test_concurrent_metadata_calls_share_io_and_detach_results(tmp_path, method):
    entered = threading.Event(); release = threading.Event(); calls = []
    def read(*args):
        calls.append(args); entered.set(); assert release.wait(3)
        return [{'name': 'fixture'}] if method == 'models' else {'capabilities': ['tools']}
    core = SimpleNamespace(base='http://fixture.invalid/api', dispatch=lambda action, data: {'models': read()} if action == 'models' else read())
    pool = ProviderPool(core, ForgeStore(tmp_path))
    def request(): return pool.models() if method == 'models' else pool.capabilities('ollama', 'fixture')
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(request) for _ in range(8)]
        assert entered.wait(2); release.set()
        results = [future.result(3) for future in futures]
    assert len(calls) == 1
    if method == 'models':
        results[0][0]['name'] = 'changed'
        assert request()[0]['name'] == 'fixture'
    else:
        results[0]['capabilities'].clear()
        assert request()['capabilities'] == ['tools']
    assert not pool.metadata_pending


def test_invalidate_during_io_rejects_reply_and_cannot_revive_thinking_cache(tmp_path):
    entered = threading.Event(); release = threading.Event()
    def dispatch(action, data):
        entered.set(); assert release.wait(3)
        return {'capabilities': ['thinking'], 'details': {'family': 'qwen3'}}
    pool = ProviderPool(SimpleNamespace(base='fixture', dispatch=dispatch), ForgeStore(tmp_path))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.capabilities, 'ollama', 'fixture')
        assert entered.wait(2); pool.invalidate('ollama'); release.set()
        with pytest.raises(ValueError, match='configuration changed'): future.result(3)
    assert not pool.capability_cache and not pool.ollama.thinking_cache and not pool.metadata_pending
    assert pool.capabilities('ollama', 'fixture')['capabilities'] == ['thinking']


def test_failed_metadata_is_retryable_and_wait_is_bounded(tmp_path):
    entered = threading.Event(); release = threading.Event(); calls = []
    def dispatch(action, data):
        calls.append(action)
        if len(calls) == 1:
            entered.set(); assert release.wait(3); raise ValueError('Synthetic engine failure')
        return {'models': [{'name': 'recovered'}]}
    pool = ProviderPool(SimpleNamespace(base='fixture', dispatch=dispatch), ForgeStore(tmp_path))
    pool.metadata_wait_seconds = .02
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.models)
        assert entered.wait(2)
        with pytest.raises(ValueError, match='took too long'): pool.models()
        release.set()
        with pytest.raises(ValueError, match='Synthetic engine failure'): future.result(3)
    assert pool.models()[0]['name'] == 'recovered' and len(calls) == 2


def test_changed_provider_identity_during_io_is_rejected(tmp_path):
    entered = threading.Event(); release = threading.Event()
    old = SimpleNamespace(models=lambda: (entered.set(), release.wait(3), [{'name': 'old'}])[-1])
    new = SimpleNamespace(models=lambda: [{'name': 'new'}])
    pool = ProviderPool(SimpleNamespace(base='fixture'), ForgeStore(tmp_path))
    pool.instances['fixture'] = {'signature': 'old', 'provider': old}
    pool.provider = lambda _: pool.instances['fixture']['provider']
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.models, 'fixture')
        assert entered.wait(2)
        with pool.lock: pool.instances['fixture'] = {'signature': 'new', 'provider': new}
        release.set()
        with pytest.raises(ValueError, match='configuration changed'): future.result(3)
    assert not pool.model_cache
    assert pool.models('fixture')[0]['name'] == 'new'


def test_refresh_reads_new_values_and_caches_remain_bounded(tmp_path):
    count = [0]
    def dispatch(action, data):
        count[0] += 1
        return {'models': [{'name': str(count[0])}]} if action == 'models' else {'capabilities': [], 'value': count[0]}
    pool = ProviderPool(SimpleNamespace(base='fixture', dispatch=dispatch), ForgeStore(tmp_path))
    assert pool.models()[0]['name'] == '1'
    assert pool.models()[0]['name'] == '1'
    assert pool.models(refresh=True)[0]['name'] == '2'
    for index in range(140): pool.capabilities('ollama', str(index))
    assert len(pool.capability_cache) == 128


def test_invalidation_is_scoped_to_the_selected_provider(tmp_path):
    pool = ProviderPool(SimpleNamespace(base='fixture', dispatch=lambda *args: {'models': []}), ForgeStore(tmp_path))
    pool.models()
    pool.invalidate('different')
    assert pool.model_cache
    pool.invalidate()
    assert not pool.model_cache


def test_supplied_stable_protocol_adapter_is_bound_without_a_registry_entry(tmp_path):
    calls=[]
    def models():calls.append(1);return [{'name':'fixture'}]
    provider=SimpleNamespace(models=models,capabilities=lambda model:{'capabilities':['tools']})
    pool=ProviderPool(SimpleNamespace(base='fixture'),ForgeStore(tmp_path))
    current=[provider];pool.provider=lambda identifier:current[0]
    assert pool.models('supplied')==[{'name':'fixture'}]
    assert pool.models('supplied')==[{'name':'fixture'}] and len(calls)==1
    assert pool.capabilities('supplied','fixture')=={'capabilities':['tools']}
    current[0]=SimpleNamespace(models=lambda:[{'name':'replacement'}])
    assert pool.models('supplied')==[{'name':'replacement'}]
    pool.invalidate('supplied')
    assert not pool.model_cache and not pool.capability_cache


def test_waiter_rechecks_provider_identity_after_shared_publication(tmp_path):
    entered=threading.Event();release=threading.Event();following=threading.Event()
    def read():entered.set();assert release.wait(3);return [{'name':'old'}]
    old=SimpleNamespace(models=read);new=SimpleNamespace(models=lambda:[{'name':'new'}])
    current=[old];pool=ProviderPool(SimpleNamespace(base='fixture'),ForgeStore(tmp_path))
    pool.provider=lambda identifier:current[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner=executor.submit(pool.models,'supplied');assert entered.wait(2)
        with pool.lock:
            pending=next(iter(pool.metadata_pending.values()));event=pending['event']
            class Transition:
                def set(self):event.set()
                def wait(self,timeout):
                    following.set();done=event.wait(timeout)
                    if done:current[0]=new
                    return done
            pending['event']=Transition()
        follower=executor.submit(pool.models,'supplied');assert following.wait(2);release.set()
        assert owner.result(3)==[{'name':'old'}]
        assert follower.result(3)==[{'name':'new'}]
    assert pool.models('supplied')==[{'name':'new'}]
