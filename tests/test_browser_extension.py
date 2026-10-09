"""Real native-message framing and isolated sessions use synthetic selected tabs."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

import pytest

from browser_native_host import validate_origin
from browser_tools import BrowserTools, ExtensionBridge

ORIGIN = 'chrome-extension://' + 'a'*32 + '/'


@pytest.mark.parametrize('origin', ['', 'https://example.com/', 'chrome-extension://'+'b'*32+'/', 'chrome-extension://'+'a'*32+'/evil'])
def test_host_accepts_only_exact_registered_extension_origin(origin):
    with pytest.raises(ValueError): validate_origin(origin, {'name':'org.forge.browser','type':'stdio','allowed_origins':[ORIGIN]})


@pytest.fixture
def messaging(tmp_path):
    bridge = ExtensionBridge(tmp_path)
    manifest = tmp_path/'config/native-messaging/org.forge.browser.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'name':'org.forge.browser','type':'stdio','allowed_origins':[ORIGIN], 'path':'fixture.exe'}))
    children = []
    def start(origin=ORIGIN):
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1]/'browser_native_host.py'), origin],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ,'FORGE_HOME':str(tmp_path)})
        children.append(child); return child
    yield bridge, start
    for child in children:
        if child.poll() is None:
            child.stdin.close()
            child.wait(timeout=5)
        child.stdout.close(); child.stderr.close()
    bridge.shutdown()


def poll(child, results=None):
    data=json.dumps({'tabs':[{'id':10,'title':'Disposable fixture','url':'https://example.com/fixture'}], 'results':results or []}).encode()
    child.stdin.write(struct.pack('<I',len(data))+data); child.stdin.flush()
    header=child.stdout.read(4); assert len(header)==4
    return json.loads(child.stdout.read(struct.unpack('<I',header)[0]))


def wait_pending(bridge):
    deadline=time.monotonic()+3
    while not bridge.pending and time.monotonic()<deadline: time.sleep(.01)
    assert bridge.pending


def test_native_messages_isolate_chrome_edge_tab_ids_and_results(messaging):
    bridge,start=messaging; chrome=start(); edge=start()
    assert poll(chrome)['ok']; assert poll(edge)['ok']
    ids=[item['id'] for item in bridge.tabs()]
    assert len(ids)==2 and ids[0]!=ids[1] and all(value.endswith(':10') for value in ids)
    result=[]
    thread=threading.Thread(target=lambda: result.append(bridge.command(ids[0],'inspect',{})))
    thread.start(); wait_pending(bridge)
    command=poll(chrome)['commands'][0]
    assert command['tab_id']==10 and poll(edge)['commands']==[]
    # A second native-host session cannot satisfy a future it does not own.
    poll(edge,[{'id':command['id'],'result':{'ok':True,'text':'wrong session'}}]); assert result==[]
    poll(chrome,[{'id':command['id'],'result':{'ok':True,'text':'correct session'}}]);thread.join(3)
    assert result==[{'ok':True,'text':'correct session'}]
    assert poll(chrome)['commands']==[]


def test_registered_origin_is_checked_before_ipc(messaging):
    bridge,start=messaging; child=start('chrome-extension://'+'b'*32+'/')
    assert child.wait(timeout=3)==1 and bridge.tabs()==[]
    assert child.stdout.read()==b''


def test_connected_tab_snapshot_is_run_bound_and_actions_are_not_replayed(messaging, tmp_path):
    bridge,start=messaging; child=start(); poll(child)
    tools=BrowserTools(tmp_path); tools.bridge=bridge
    tab_id=bridge.tabs()[0]['id']; inspected=[]
    worker=threading.Thread(target=lambda: inspected.append(tools.execute('browser_inspect',{'tab_id':tab_id},{'run_id':'one'})))
    worker.start();wait_pending(bridge); command=poll(child)['commands'][0]
    response={'ok':True,'snapshot_id':'fixture-snapshot','url':'https://example.com/fixture','targets':[{'selector':'button'}]}
    poll(child,[{'id':command['id'],'result':response}]);worker.join(3)
    args={'tab_id':tab_id,'snapshot_id':'fixture-snapshot','selector':'button'}
    try:
        assert tools.execute('browser_click',args,{'run_id':'two'})['not_executed']
        result=[]
        worker=threading.Thread(target=lambda: result.append(tools.execute('browser_click',args,{'run_id':'one'})))
        worker.start();wait_pending(bridge);command=poll(child)['commands'][0]
        poll(child,[{'id':command['id'],'result':{'ok':True,'inspect_again':True}}]);worker.join(3)
        assert result[0]['ok'] and tools.execute('browser_click',args,{'run_id':'one'})['not_executed']
    finally:
        # Fixture owns the shared bridge; tool cleanup must not shut it down twice.
        tools.bridge=None;tools.shutdown()


def test_cancel_before_dispatch_is_safe_but_cancel_after_dispatch_requires_inspection(messaging):
    bridge,start=messaging;child=start();poll(child);tab_id=bridge.tabs()[0]['id']
    cancel=threading.Event();cancel.set()
    assert bridge.command(tab_id,'click',{}, {'cancel':cancel})['not_executed']
    assert poll(child)['commands']==[]
    cancel.clear();error=[]
    def request():
        try:bridge.command(tab_id,'click',{}, {'cancel':cancel})
        except RuntimeError as exc:error.append(str(exc))
    worker=threading.Thread(target=request);worker.start();wait_pending(bridge)
    assert len(poll(child)['commands'])==1
    cancel.set();worker.join(1)
    assert error and 'dispatched' in error[0]
    assert not bridge.pending and poll(child)['commands']==[]


def test_stop_prevents_undelivered_commands(messaging):
    bridge,start=messaging;child=start();poll(child);tab_id=bridge.tabs()[0]['id']
    bridge.stop()
    assert bridge.command(tab_id,'click',{})['not_executed']
    assert poll(child)['commands']==[]
    bridge.reset()
    assert not bridge.stopped.is_set()
