import hashlib
import io
import re
import threading

import pytest

from forge_native_download import pinned_download


class Response(io.BytesIO):
    def __init__(self, data, start, end, total):
        super().__init__(data)
        self.status=206
        self.url='https://cdn-lfs.huggingface.co/synthetic'
        self.headers={'Content-Range':f'bytes {start}-{end}/{total}'}


def fixture(monkeypatch, payload):
    calls=[]
    def open_(request, timeout):
        start,end=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',request.get_header('Range')).groups())
        calls.append((start,end))
        return Response(payload[start:end+1],start,end,len(payload))
    monkeypatch.setattr('urllib.request.urlopen',open_)
    model={'repo':'synthetic/model','revision':'a'*40}
    item={'path':'weights/model.gguf','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
    return model,item,calls


def test_ranged_download_resumes_exact_prefix(monkeypatch,tmp_path):
    payload=b'synthetic weights'*600000
    model,item,calls=fixture(monkeypatch,payload)
    partial=tmp_path/'weights/model.gguf.forge-partial'
    partial.parent.mkdir()
    partial.write_bytes(payload[:127])
    completed=[]
    path=pinned_download(model,item,tmp_path,progress=lambda done,total:completed.append(done))
    assert path.read_bytes()==payload
    assert min(start for start,end in calls)==127
    assert completed[-1]==len(payload)
    assert not partial.exists()


def test_ignoring_ranges_never_completes(monkeypatch,tmp_path):
    model,item,_=fixture(monkeypatch,b'synthetic weights')
    def ignored(*args,**kwargs):
        response=Response(b'synthetic weights',0,16,17)
        response.status=200
        return response
    monkeypatch.setattr('urllib.request.urlopen',ignored)
    monkeypatch.setattr(threading.Event,'wait',lambda *a:False)
    with pytest.raises(ValueError,match='exact byte range'):
        pinned_download(model,item,tmp_path)
    assert not (tmp_path/item['path']).exists()


def test_corrupt_prefix_fails_digest(monkeypatch,tmp_path):
    model,item,_=fixture(monkeypatch,b'synthetic weights')
    partial=tmp_path/'weights/model.gguf.forge-partial'
    partial.parent.mkdir();partial.write_bytes(b'wrong')
    with pytest.raises(ValueError,match='SHA-256'):
        pinned_download(model,item,tmp_path)
    assert not (tmp_path/item['path']).exists()


def test_cancel_retains_resumable_partial(monkeypatch,tmp_path):
    model,item,calls=fixture(monkeypatch,b'synthetic weights')
    cancel=threading.Event();cancel.set()
    with pytest.raises(ValueError,match='paused'):
        pinned_download(model,item,tmp_path,cancel=cancel)
    assert calls==[] and not (tmp_path/item['path']).exists()


def test_official_tensor_range_is_bound_to_offset_size_and_digest(monkeypatch,tmp_path):
    shard=b'unrelated-header'+b'trusted-tensor'+b'unrelated-tail'
    model,item,calls=fixture(monkeypatch,shard)
    tensor=b'trusted-tensor';start=len(b'unrelated-header')
    item.update(path='mtp/tensors/tensor.bin',source_path='model-001-of-002.safetensors',
        source_offset=start,source_size=len(shard),size=len(tensor),sha256=hashlib.sha256(tensor).hexdigest())
    assert pinned_download(model,item,tmp_path).read_bytes()==tensor
    assert calls==[(start,start+len(tensor)-1)]


@pytest.mark.parametrize('changes',[{'source_offset':-1},{'source_size':1},{'source_path':'../escape'},{'source_offset':True}])
def test_invalid_source_ranges_are_refused_before_network(monkeypatch,tmp_path,changes):
    model,item,calls=fixture(monkeypatch,b'synthetic weights');item.update(changes)
    with pytest.raises(ValueError):pinned_download(model,item,tmp_path)
    assert calls==[]
