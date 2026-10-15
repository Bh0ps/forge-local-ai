import json
import os
from pathlib import Path
import numpy as np
import pytest
from forge_vendor.strata.mtp import q2_0, q8_0, blob_of, BLOB, H, FF
from forge_native_mtp import support_rows,prepare


def test_mtp_q2_layout_matches_exact_four_level_fixture():
    values=np.tile(np.array([-1,0,1,2],dtype=np.float32),16)
    assert q2_0(values).tobytes()==np.float16(1).tobytes()+bytes([0b11100100])*16


def test_mtp_q8_matches_ggml_reference():
    from gguf import quantize,GGMLQuantizationType
    values=np.linspace(-2,3,256,dtype=np.float32).reshape(4,64)
    assert q8_0(values)==quantize(values,GGMLQuantizationType.Q8_0).tobytes()


def test_expert_relayout_preserves_codes_and_scale_planes():
    gate=np.zeros((2*FF,H//64*18),dtype=np.uint8)
    down=np.zeros((H,FF//64*18),dtype=np.uint8)
    gate.reshape(2*FF,H//64,18)[:,:,2:]=7
    down.reshape(H,FF//64,18)[:,:,2:]=9
    blob=blob_of(gate,down)
    assert len(blob)==BLOB
    assert blob[:2*FF*H//64*16]==bytes([7])*(2*FF*H//64*16)
    assert blob[-(2*FF*H//64+H*FF//64)*2:]==bytes((2*FF*H//64+H*FF//64)*2)


def test_draft_metadata_is_pinned_and_cancel_does_not_create_runtime(tmp_path):
    import forge_native_mtp
    spec=json.loads((Path(forge_native_mtp.__file__).parent/'native-models.lock.json').read_text(encoding='utf-8'))['models'][0]
    model={'id':spec['id'],'support':spec['support']}
    assert len(support_rows(model))==31
    def stopped():raise RuntimeError('Synthetic stop')
    with pytest.raises(RuntimeError,match='Synthetic stop'):prepare(tmp_path,model,stopped)
    assert not (tmp_path/model['id']).exists()
    model['support']['files'][0]['shape']=[1]
    with pytest.raises(ValueError,match='pinned checkpoint'):support_rows(model)


def test_draft_sources_bind_catalog_and_reject_changed_files(tmp_path):
    import hashlib
    from forge_native_models import NativeCatalog
    from test_forge_native import gguf
    catalog=NativeCatalog(tmp_path/'home')
    record=catalog.add([gguf(tmp_path/'model.gguf')],identifier='fixture')
    before=catalog.binding();path=catalog.root/'fixture/mtp/tensor.bin'
    path.parent.mkdir();path.write_bytes(b'synthetic tensor')
    support={'repo':'synthetic/model','revision':'a'*40,'files':[{'path':'mtp/tensor.bin',
        'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'shape':[8]}]}
    catalog.publish(catalog.attach_support(record,support))
    assert catalog.binding()!=before
    catalog.model('fixture')
    # A same-tick touch can keep the same NTFS timestamp on Windows runners.
    verified = path.stat()
    os.utime(path, ns=(verified.st_atime_ns, verified.st_mtime_ns + 2_000_000_000))
    with pytest.raises(ValueError,match='changed after verification'):catalog.model('fixture')
