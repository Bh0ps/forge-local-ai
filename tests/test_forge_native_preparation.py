import threading
import pytest
from forge_native_jobs import download_space_required
from forge_vendor.strata.iq_pack import main


def test_disk_space_credits_only_expected_downloads(tmp_path):
    selected={'files':[{'path':'weights/model.gguf','size':100}], 'preparation_bytes':50}
    (tmp_path/'unrelated-cache').write_bytes(b'x'*300)
    expected=4*1024**3+150
    assert download_space_required(selected,tmp_path)==expected
    target=tmp_path/'weights'/'model.gguf.forge-partial'
    target.parent.mkdir()
    target.write_bytes(b'x'*25)
    assert download_space_required(selected,tmp_path)==expected-25
    target.with_name('model.gguf').write_bytes(b'x'*100)
    assert download_space_required(selected,tmp_path)==expected-100


def test_cancelled_pack_does_not_read_or_create_model_files(tmp_path):
    cancel=threading.Event();cancel.set()
    with pytest.raises(ValueError,match='cancelled'):
        main(['--gguf',str(tmp_path/'missing.gguf'),'--out',str(tmp_path/'pack')],cancel=cancel)
    assert not (tmp_path/'pack').exists()


def test_strata_rechecks_memory_before_launch_after_preparation(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import psutil
    import forge_inference
    import forge_native
    import forge_native_mtp
    from forge_native import NativeRuntime
    from forge_vendor.strata.tokenizer import Tokenizer
    runtime=NativeRuntime(tmp_path/'home',bundle=tmp_path/'bundle')
    root=runtime.catalog.root
    pack=root/'pack';(pack/'tokenizer').mkdir(parents=True)
    for name in ('native_experts.txt','experts.bin'):(pack/name).touch()
    (pack/'tokenizer'/'chat_template.jinja').write_text('{{ messages }}',encoding='utf-8')
    model={'id':'coder','engine':'strata','context':8192,
           'files':[{'path':'shard1.gguf','sha256':'fixture'},{'path':'shard2.gguf','sha256':'fixture'}]}
    samples=iter([{'gpus':[{'total_mb':16384,'used_mb':0}]},
                  {'gpus':[{'total_mb':16384,'used_mb':0}]},
                  {'gpus':[{'total_mb':16384,'used_mb':5000}]}])
    monkeypatch.setattr(forge_inference,'memory_telemetry',lambda:next(samples))
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=20*1024**3))
    monkeypatch.setattr(forge_native,'file_hash',lambda path,**kwargs:'fixture')
    monkeypatch.setattr(runtime,'_executable',lambda engine:tmp_path/'strata.exe')
    monkeypatch.setattr(forge_native_mtp,'prepare',lambda *args:tmp_path/'mtp')
    monkeypatch.setattr(Tokenizer,'from_gguf',classmethod(lambda cls,path:object()))
    launched=[]
    worker={'process':SimpleNamespace(poll=lambda:None)}
    monkeypatch.setattr(runtime,'_spawn',lambda exe,args,**kwargs:launched.append(args) or worker)
    monkeypatch.setattr(runtime,'_line',lambda *args:'READY 8192 stop')
    monkeypatch.setattr(runtime,'_dispose',lambda worker:None)
    try:
        runtime._start(model,8192,threading.Event())
        assert '--mmap-experts' in launched[0] and '--resident-experts' not in launched[0]
        assert launched[0][launched[0].index('--spec')+1]=='4' and '--mtp' in launched[0]
    finally:runtime.close()


def test_model_verification_can_stop_between_bounded_reads(tmp_path):
    from forge_native_models import file_hash
    source=tmp_path/'weights.gguf';source.write_bytes(b'x'*(3*1024**2))
    checks=[]
    def check():
        checks.append(True)
        if len(checks)==2:raise RuntimeError('Synthetic stop')
    with pytest.raises(RuntimeError,match='Synthetic stop'):file_hash(source,check=check)
    assert len(checks)==2
