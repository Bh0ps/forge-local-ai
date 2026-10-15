"""Build-time acquisition only. Runtime installs never run an upstream setup script."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import urllib.request
import zipfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(lock, destination, cache):
    destination.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    lock_digest=hashlib.sha256(json.dumps(lock,sort_keys=True).encode()).hexdigest()
    if any(destination.iterdir()):
        existing=destination/'manifest.json'
        if existing.is_file():
            saved=json.loads(existing.read_text(encoding='utf-8'))
            if saved.get('lock_sha256')==lock_digest:
                actual={p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file() and p!=existing}
                if actual==set(saved['files']) and all((destination/p).stat().st_size==f['size'] and digest(destination/p)==f['sha256'] for p,f in saved['files'].items()):
                    return
        raise ValueError('Use a fresh native bundle destination. An existing bundle was retained.')
    manifest = {'schema': 1, 'lock_sha256':lock_digest,'engines': {}, 'files': {}}
    shared = lock.get('layout') == 'shared-cuda13'
    for item in lock['artifacts']:
        archive = cache / (item['sha256'] + '.zip')
        if not archive.exists():
            part = archive.with_suffix('.part')
            request = urllib.request.Request(item['url'], headers={'User-Agent': 'Forge-build'})
            with urllib.request.urlopen(request, timeout=90) as response, part.open('wb') as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
            if part.stat().st_size != item['size'] or digest(part) != item['sha256']:
                raise ValueError('Native runtime download failed its pinned digest.')
            part.replace(archive)
        if archive.stat().st_size != item['size'] or digest(archive) != item['sha256']:
            raise ValueError('Native runtime cache failed its pinned digest.')
        root = destination / item['engine']
        root.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            total = 0
            for info in source.infolist():
                path = PurePosixPath(info.filename)
                total += info.file_size
                if (path.is_absolute() or '..' in path.parts or '\\' in info.filename
                        or ':' in info.filename or total > 2 * 1024**3
                        or ((info.external_attr >> 16) & 0o170000) == 0o120000):
                    raise ValueError('Unsafe native runtime archive.')
                if info.is_dir():
                    continue
                if shared:
                    # Ship the two workers, their actual shared libraries and notices.
                    # Command-line utilities and a second copy of CUDA are not needed.
                    if item['engine']=='strata' and path.name not in ('strata.exe','BUILD.json'):
                        continue
                    if item['engine']=='llama-cpp' and path.suffix.lower()=='.exe' and path.name!='llama-server.exe':
                        continue
                    if path.suffix.lower() in ('.lib','.h','.hpp','.a'):
                        continue
                    if item['engine']=='llama-cpp' and path.name in ('cublas64_13.dll','cublasLt64_13.dll','cudart64_13.dll'):
                        root=destination/'shared';root.mkdir(exist_ok=True)
                    else:
                        root=destination/item['engine']
                target = root.joinpath(*path.parts)
                if not target.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Native runtime extraction escaped its root.')
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as inp, target.open('wb') as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
        manifest['engines'][item['engine']] = item['version']
        print('Verified ' + item['engine'] + ' ' + item['version'], flush=True)
    notice_root=Path(__file__).resolve().parents[1]/'forge_vendor'/'native_notices'
    if notice_root.is_dir():
        shutil.copytree(notice_root,destination/'notices',dirs_exist_ok=True)
    for path in sorted(destination.rglob('*')):
        if path.is_file() and path.name != 'manifest.json':
            manifest['files'][path.relative_to(destination).as_posix()] = {
                'size': path.stat().st_size, 'sha256': digest(path)}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    lock_path = Path(__file__).resolve().parents[1] / 'native-runtime.lock.json'
    fetch(json.loads(lock_path.read_text(encoding='utf-8')), args.destination, args.cache)
