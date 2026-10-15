"""Bounded preparation of the pinned official MTP head for Strata's private worker."""
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

import numpy as np

from forge_native_models import atomic_json, file_hash, read_json
from forge_vendor.strata.mtp import q2_0, q8_0, blob_of, Q8, H, FF, NE


def support_rows(model):
    selected=next((m for m in read_json(Path(__file__).parent/'native-models.lock.json')['models']
                   if m['id']==model['id'] and m['engine']=='strata'),None)
    support=model.get('support',{})
    if not selected or not selected.get('support') or any(support.get(k)!=selected['support'][k] for k in ('repo','revision','license')):
        raise ValueError('Complete the pinned Strata model download, including its MTP draft head.')
    wanted={row['name']:row for row in selected['support']['files']}
    rows=support.get('files',[])
    if len(rows)!=len(wanted) or len({r.get('name') for r in rows})!=len(wanted):
        raise ValueError('The Strata draft head is incomplete.')
    for row in rows:
        expected=wanted.get(row.get('name'))
        if not expected or any(row.get(k)!=expected[k] for k in ('size','sha256','shape')):
            raise ValueError('The Strata draft head does not match its pinned checkpoint.')
    return rows


def prepare(root, model, check):
    rows=support_rows(model)
    root=Path(root).resolve()
    binding=hashlib.sha256(json.dumps({r['name']:r['sha256'] for r in rows},sort_keys=True).encode()).hexdigest()
    target=root/model['id']/'mtp'/'rt'
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('The prepared Strata draft directory is outside Forge model storage.')
    if target.exists():
        proof=read_json(target/'forge-mtp-proof.json')
        if proof.get('source_binding')!=binding or set(proof.get('files',{}))!={'experts.bin','dense.bin','dense.txt','draft_vocab.bin'}:
            raise ValueError('The prepared Strata draft head needs a fresh verified setup.')
        for name,entry in proof['files'].items():
            path=target/name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size!=entry['size'] or file_hash(path,check=check)!=entry['sha256']:
                raise ValueError('A prepared Strata draft file changed after verification.')
        license_source=Path(__file__).parent/'forge_vendor/strata/QWEN_MTP_LICENSE'
        if file_hash(license_source)!='a0dc422560841fd68e06d974907f8b4c709bca44a67daad2b528437bdf676c08':
            raise ValueError('The pinned Qwen draft license notice is unavailable.')
        notice=target.parent/'QWEN_MTP_LICENSE'
        if notice.is_symlink() or not notice.resolve().is_relative_to(root):
            raise ValueError('The Qwen draft notice is outside Forge model storage.')
        if notice.exists() and file_hash(notice)!=file_hash(license_source):
            raise ValueError('The Qwen draft license notice changed after setup.')
        if not notice.exists():shutil.copyfile(license_source,notice)
        return target
    check()
    if shutil.disk_usage(root).free<2*1024**3:
        raise ValueError('Strata draft preparation needs another 2 GB of free model storage.')
    temporary=target.with_name('rt-building-'+uuid4().hex)
    if temporary.is_symlink() or not temporary.resolve().is_relative_to(root):
        raise ValueError('The Strata draft directory is outside Forge model storage.')
    temporary.mkdir(parents=True)
    license_source=Path(__file__).parent/'forge_vendor/strata/QWEN_MTP_LICENSE'
    if file_hash(license_source)!='a0dc422560841fd68e06d974907f8b4c709bca44a67daad2b528437bdf676c08':
        raise ValueError('The pinned Qwen draft license notice is unavailable.')
    shutil.copyfile(license_source,target.parent/'QWEN_MTP_LICENSE')
    tensors={}
    for row in rows:
        path=root/row['path']
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('A Strata source tensor is outside Forge model storage.')
        tensors[row['name']]=np.memmap(path,dtype='<u2',mode='r',shape=tuple(row['shape']))
    gu=tensors['mtp.layers.0.mlp.experts.gate_up_proj']
    dn=tensors['mtp.layers.0.mlp.experts.down_proj']
    with (temporary/'experts.bin').open('xb') as output:
        for expert in range(NE):
            check()
            gate=(gu[expert].astype(np.uint32)<<16).view(np.float32)
            down=(dn[expert].astype(np.uint32)<<16).view(np.float32)
            output.write(blob_of(q2_0(gate).reshape(2*FF,H//64*18),
                                 q2_0(down).reshape(H,FF//64*18)))
    lines=[];offset=0
    with (temporary/'dense.bin').open('xb') as output:
        for row in sorted(rows,key=lambda r:r['name']):
            check();name=row['name']
            if '.experts.' in name:continue
            short=name.removeprefix('mtp.').removeprefix('layers.0.')
            data=tensors[name]
            if len(row['shape'])==1:
                values=(data.astype(np.uint32)<<16).view(np.float32)+1.0
                raw,kind,nrows,cols=values.tobytes(),'f32',1,values.size
            else:
                nrows,cols=data.shape
                if short in Q8:
                    raw=q8_0((data.astype(np.uint32)<<16).view(np.float32));kind='q8_0'
                else:raw=data.tobytes();kind='bf16'
            padding=(-offset)%256;output.write(b'\0'*padding);offset+=padding
            output.write(raw);lines.append(f'{short} {kind} {nrows} {cols} {offset} {len(raw)}');offset+=len(raw)
    (temporary/'dense.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    shutil.copyfile(Path(__file__).parent/'forge_vendor/strata/draft_vocab.bin',temporary/'draft_vocab.bin')
    files={path.name:{'size':path.stat().st_size,'sha256':file_hash(path,check=check)} for path in temporary.iterdir()}
    atomic_json(temporary/'forge-mtp-proof.json',{'schema':1,'source_binding':binding,'files':files})
    check();temporary.rename(target)
    return target
