"""Native browser delegate preserves permissions context and unknown outcomes."""
import threading

import pytest

from forge_integrations import IntegrationHub


class FakeBrowser:
    def __init__(self): self.calls=[]
    def execute(self, name, arguments, context=None):
        self.calls.append((name,arguments,context))
        if arguments.get('after_effect'): raise RuntimeError('Navigation interrupted after starting')
        if arguments.get('stale'): return {'ok':False,'error':'Stale snapshot','not_executed':True}
        return {'ok':True,'backend':'native'}
    def native_action(self, action, data): self.calls.append((action,data)); return {'ok':True,'action':action}
    def status(self, backend=None):
        self.calls.append(('status',backend)); return {'available':True,'backend':backend or 'native'}
    def stop(self): self.calls.append('stop')
    def reset(self): self.calls.append('reset')
    def shutdown(self): self.calls.append('shutdown')


def test_native_browser_receives_run_and_cancel_context_and_dispatch(tmp_path):
    hub=IntegrationHub(tmp_path)
    hub.browser.shutdown(); browser=hub.browser=FakeBrowser()
    context={'run_id':'fixture','cancel':threading.Event()}
    assert hub.execute('browser_navigate',{'url':'https://example.test'},context)['backend']=='native'
    assert browser.calls[0][2] is context
    assert hub.dispatch('browser_native_open',{'url':'https://example.test'})['action']=='open'
    hub.stop();hub.reset();hub.shutdown()
    assert browser.calls[-3:]==['stop','reset','shutdown']


@pytest.mark.parametrize('action',['browser_navigate','browser_select','browser_key','browser_scroll'])
def test_native_browser_preflight_failure_is_known_but_started_effect_is_unknown(tmp_path,action):
    hub=IntegrationHub(tmp_path)
    hub.browser.shutdown();hub.browser=FakeBrowser()
    preflight=hub.execute('browser_click',{'stale':True},{'run_id':'fixture'})
    assert preflight['not_executed'] and not preflight.get('outcome_unknown')
    interrupted=hub.execute(action,{'after_effect':True},{'run_id':'fixture'})
    assert interrupted['outcome_unknown']
    hub.shutdown()


def test_browser_status_explicit_isolated_diagnostic_keeps_lightweight_default(tmp_path):
    hub=IntegrationHub(tmp_path)
    hub.browser.shutdown();browser=hub.browser=FakeBrowser()
    try:
        assert hub.dispatch('browser_status',{})['backend']=='native'
        assert hub.dispatch('browser_status',{'backend':'isolated'})['backend']=='isolated'
        assert browser.calls==[('status',None),('status','isolated')]
    finally:
        hub.shutdown()
