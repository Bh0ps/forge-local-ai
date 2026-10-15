"""Resumable fixed-revision model transfers with bounded, verified HTTP ranges."""
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import threading
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from forge_native_models import file_hash
from filelock import FileLock


def pinned_download(model, item, directory, *, cancel=None, progress=None):
    relative=Path(item['path'])
    root=Path(directory).resolve()
    if (relative.is_absolute() or '..' in relative.parts or '\\' in item['path'] or ':' in item['path']
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',model['repo'])
            or type(item['size']) is not int or not 0<item['size']<=150*1024**3
            or not re.fullmatch('[0-9a-f]{64}',item['sha256'])):
        raise ValueError('Invalid pinned model download.')
    lock_path=root/relative.with_suffix(relative.suffix+'.forge-download.lock')
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    if lock_path.is_symlink() or not lock_path.resolve().is_relative_to(root):
        raise ValueError('Unsafe model download lock.')
    with FileLock(lock_path,timeout=0):
        return _pinned_download(model,item,directory,cancel=cancel,progress=progress)


def _pinned_download(model, item, directory, *, cancel=None, progress=None):
    cancel = cancel or threading.Event()
    relative = Path(item['path'])
    if relative.is_absolute() or '..' in relative.parts or '\\' in item['path'] or ':' in item['path']:
        raise ValueError('Unsafe pinned model path.')
    if not re.fullmatch('[0-9a-f]{40}', model['revision']):
        raise ValueError('A fixed model revision is required.')
    root = Path(directory).resolve()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root):
        raise ValueError('Model download target escaped its directory.')
    if target.exists():
        if target.stat().st_size == item['size'] and file_hash(target) == item['sha256']:
            return target
        raise ValueError('The existing model does not match its pinned download.')
    part = target.with_suffix(target.suffix + '.forge-partial')
    if part.is_symlink() or not part.resolve().is_relative_to(root):
        raise ValueError('Unsafe model download partial.')
    url = 'https://huggingface.co/' + model['repo'] + '/resolve/' + model['revision'] + '/' + quote(item['path'], safe='/')
    offset = part.stat().st_size if part.exists() else 0
    if offset > item['size']:
        raise ValueError('The partial model exceeds its pinned size.')
    chunk = 8 * 1024**2
    def fetch(start):
        end = min(start + chunk, item['size']) - 1
        if cancel.is_set():
            raise ValueError('Model download paused.')
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={'Range':f'bytes={start}-{end}', 'User-Agent':'Forge-model-download'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    expected = f'bytes {start}-{end}/{item["size"]}'
                    if response.status != 206 or response.headers.get('Content-Range') != expected or urlparse(response.url).scheme != 'https':
                        raise ValueError('The model server did not honor the exact byte range.')
                    data = response.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise ValueError('The model server returned an incomplete byte range.')
                break
            except (OSError, ValueError):
                if attempt == 3: raise
                if cancel.wait(min(2**attempt, 4)):
                    raise ValueError('Model download paused.') from None
        return data
    with part.open('ab') as output, ThreadPoolExecutor(max_workers=4, thread_name_prefix='Forge-model-range') as pool:
        pending = deque()
        next_offset = offset
        while offset < item['size']:
            while len(pending) < 4 and next_offset < item['size']:
                pending.append(pool.submit(fetch, next_offset))
                next_offset += chunk
            data = pending.popleft().result()
            if cancel.is_set():
                raise ValueError('Model download paused.')
            output.write(data)
            output.flush()
            offset += len(data)
            if progress: progress(offset, item['size'])
        output.flush()
    if file_hash(part) != item['sha256']:
        raise ValueError('The model download failed its pinned SHA-256 verification.')
    part.replace(target)
    return target
