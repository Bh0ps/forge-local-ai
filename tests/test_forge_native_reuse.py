import hashlib
import os
import threading
from types import SimpleNamespace

import pytest

from forge_native_jobs import reuse_installed_file, download_space_required


def fixture(tmp_path):
    source = tmp_path / 'original/weights.gguf'
    source.parent.mkdir()
    data = b'synthetic verified weights'
    source.write_bytes(data)
    item = {'path': 'weights.gguf', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    catalog = SimpleNamespace(root=tmp_path, public=lambda: {'models': [{'files': [{**item, 'path': 'original/weights.gguf'}]}]})
    return catalog, source, item


def test_reuses_only_verified_bytes_and_preserves_the_original(tmp_path):
    catalog, source, item = fixture(tmp_path)
    target = tmp_path / 'replacement'
    assert reuse_installed_file(catalog,item,target,threading.Event())
    assert os.path.samefile(source,target/item['path'])
    assert source.read_bytes() == (target/item['path']).read_bytes()
    assert not reuse_installed_file(catalog,item,target,threading.Event())


def test_bad_local_bytes_are_not_credited_as_the_pinned_model(tmp_path):
    catalog, source, item = fixture(tmp_path)
    source.write_bytes(b'X' * item['size'])
    assert not reuse_installed_file(catalog,item,tmp_path/'replacement',threading.Event())
    assert not (tmp_path/'replacement').exists()


def test_cancel_and_path_escape_do_not_create_targets(tmp_path):
    catalog, source, item = fixture(tmp_path)
    cancel=threading.Event();cancel.set()
    with pytest.raises(ValueError,match='paused'):
        reuse_installed_file(catalog,item,tmp_path/'replacement',cancel)
    with pytest.raises(ValueError,match='outside'):
        reuse_installed_file(catalog,{**item,'path':'../../escape.gguf'},tmp_path/'replacement',threading.Event())
    assert source.is_file() and not (tmp_path/'replacement').exists()


def test_projection_is_included_in_download_space_and_resume_credit(tmp_path):
    item={'path':'projection.gguf','size':100}
    selected={'files':[],'projection':{'file':item}}
    assert download_space_required(selected,tmp_path)==4*1024**3+100
    (tmp_path/item['path']).write_bytes(b'x'*100)
    assert download_space_required(selected,tmp_path)==4*1024**3


@pytest.mark.parametrize('enabled', [False, True])
def test_strata_request_projection_comes_from_verified_worker_not_prompt_options(tmp_path, monkeypatch, enabled):
    import io
    from forge_native import NativeRuntime
    runtime=NativeRuntime(tmp_path/'home')
    output=io.StringIO()
    worker={'projection_enabled':enabled, 'process':SimpleNamespace(stdin=output),
            'template':SimpleNamespace(render=lambda *a,**k:'Synthetic prompt'),
            'tokenizer':SimpleNamespace(encode=lambda *a,**k:[0],tokens=[b'x'])}
    monkeypatch.setattr(runtime,'_line',lambda *args:'DONE 0 1 0 0 stop')
    packets=list(runtime._strata_generate(worker,{'model':'synthetic','context':8192,'tokens':1024,
        'messages':[{'role':'user','content':'Synthetic request'}], 'cvec':not enabled},threading.Event()))
    assert packets[-1]['done'] is True
    assert ('cvec=1 ' in output.getvalue()) is enabled
