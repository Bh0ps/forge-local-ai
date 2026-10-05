"""Create the Windows CPython 3.12 dependency lock from a validated build venv."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
from pathlib import Path
import re

import httpx
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='requirements-ci.lock')
    parser.add_argument('--build-output', default='requirements-build.lock')
    args = parser.parse_args()
    tags = set(sys_tags())
    packages = sorted((dist.metadata['Name'], dist.version) for dist in importlib.metadata.distributions()
                      if dist.metadata['Name'].lower() != 'pip')
    def line(item):
        name, version = item
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
            raise ValueError('Non-registry dependency cannot enter the public lock.')
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.get('https://pypi.org/pypi/' + name + '/' + version + '/json')
            response.raise_for_status()
        hashes = []
        for entry in response.json()['urls']:
            if entry['filename'].endswith('.whl') and tags.intersection(parse_wheel_filename(entry['filename'])[3]):
                hashes.append(entry['digests']['sha256'])
        if not hashes:
            hashes = [entry['digests']['sha256'] for entry in response.json()['urls'] if entry['packagetype'] == 'sdist']
        if not hashes: raise ValueError('No compatible registry wheel for ' + name)
        return name + '==' + version + ''.join(' \\\n    --hash=sha256:' + value for value in sorted(set(hashes)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        lines = list(pool.map(line, packages))
    Path(args.output).write_text('# Windows x64 / CPython 3.12; regenerate only after validating changed dependencies.\n' + '\n'.join(lines) + '\n', encoding='utf-8')
    bootstrap = [value for value in lines if value.split('==', 1)[0].lower() in ('setuptools', 'wheel', 'packaging')]
    Path(args.build_output).write_text('# Pinned build bootstrap; install before --no-build-isolation.\n' + '\n'.join(bootstrap) + '\n', encoding='utf-8')
    print('Locked', len(lines), 'registry packages with compatible wheel SHA256 hashes.')


if __name__ == '__main__': main()
