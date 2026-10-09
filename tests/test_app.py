import json
import pytest
import httpx
from fastapi.testclient import TestClient
from core import Core
from model_manager import create_app

class Recorder(Core):
    def request(self, method, path, payload=None):
        self.last = (method,path,payload)
        return {'models':[], 'message':{'content':'Test answer'}}

@pytest.fixture
def core(): return Recorder()

def test_pull_and_delete_reach_upstream(core):
    core.dispatch('pull',{'model':'test'})
    assert core.last == ('POST','/pull',{'model':'test','stream':False})
    core.dispatch('delete',{'model':'test'})
    assert core.last[0:2] == ('DELETE','/delete')

def test_settings_and_history_reach_model(core):
    messages=[{'role':'user','content':'hello'},{'role':'assistant','content':'hi'},{'role':'user','content':'code'}]
    core.dispatch('chat',{'model':'test','messages':messages,'temperature':0.2,'tokens':4096})
    payload=core.last[2]
    assert payload['messages'][1:]==messages
    assert payload['messages'][0]['role']=='system'
    assert payload['options']=={'temperature':0.2,'num_predict':4096,'num_ctx':8192}

@pytest.mark.parametrize('data',[{}, {'model':'x','messages':[]}, {'model':'x','messages':[{'role':'system','content':'bad'}]}, {'model':'x','messages':[{'role':'user','content':'hi'}],'temperature':'oops'}, {'model':'x','messages':[{'role':'user','content':'hi'}],'tokens':True}])
def test_invalid_input(core,data):
    with pytest.raises(ValueError): core.dispatch('chat',data)

def test_web_http_contract(core):
    client=TestClient(create_app(core))
    page=client.get('/')
    assert page.status_code==200 and 'text/html' in page.headers['content-type']
    assert client.post('/api/models',json={}).json()=={'models':[], 'message':{'content':'Test answer'}}
    assert client.post('/api/delete',json={}).status_code==400
    assert client.post('/api/models',json={},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/models',json={},headers={'Host':'evil.test'}).status_code==403
    assert client.post('/api/models',content='{}').status_code==415
    assert client.post('/api/models',content='{',headers={'Content-Type':'application/json'}).status_code==400
    assert client.get('/missing').status_code==404

def test_actual_http_errors(monkeypatch):
    real=httpx.Client
    def mock_handler(request): return httpx.Response(404,json={'error':'model missing'})
    monkeypatch.setattr(httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(mock_handler)))
    with pytest.raises(ValueError,match='model missing'): Core().dispatch('show',{'model':'missing'})

def test_research_sources(monkeypatch):
    import ddgs
    class Search:
        def __init__(self,**kw): pass
        def text(self,*args,**kwargs): return [{'title':'Source','href':'https://example.com','body':'Fact'},{'title':'bad','href':'javascript:alert(1)','body':'bad'}]
    monkeypatch.setattr(ddgs,'DDGS',Search)
    result=Core().dispatch('research',{'query':'test'})
    assert len(result['sources'])==1
    assert result['sources'][0]['url']=='https://example.com'

def test_research_plans_bounded_queries(monkeypatch):
    import ddgs
    seen=[]
    class Search:
        def __init__(self,**kw): pass
        def text(self,term,**kw):
            seen.append(term)
            return [{'href':'https://example.com','body':'result'}]
    class Planner(Core):
        def request(self,*args,**kw):
            return {'message':{'content':json.dumps({'queries':['first','second','third']})}}
    monkeypatch.setattr(ddgs,'DDGS',Search)
    result=Planner().dispatch('research',{'query':'question','model':'test','plan_queries':True})
    assert seen==['first','second']
    assert len(result['sources'])==1

def test_screenshot_preview_restores_window(monkeypatch):
    from PIL import Image, ImageGrab
    from desktop import Bridge
    import base64,io
    actions=[]
    class Window:
        def hide(self): actions.append('hide')
        def show(self): actions.append('show')
    bridge=Bridge()
    bridge._window=Window()
    monkeypatch.setattr(ImageGrab,'grab',lambda:Image.new('RGB',(2000,1000)))
    result=bridge.screenshot()
    assert actions==['hide','show']
    assert Image.open(io.BytesIO(base64.b64decode(result['image']))).size==(1600,800)

def test_screenshot_failure_restores_window(monkeypatch):
    from PIL import ImageGrab
    from desktop import Bridge
    class Window:
        shown=False
        def hide(self): pass
        def show(self): self.shown=True
    def fail(): raise OSError('capture failed')
    bridge=Bridge(); bridge._window=Window()
    monkeypatch.setattr(ImageGrab,'grab',fail)
    assert bridge.screenshot()['error']=='capture failed'
    assert bridge._window.shown
