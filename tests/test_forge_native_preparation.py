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
