"""Collect installed license files without embedding filesystem/user metadata."""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
import re
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


NOTICE_PREFIXES = ('license', 'licence', 'copying', 'notice', 'copyright', 'authors')
MAX_NOTICE_BYTES = 2 * 1024 * 1024


def is_notice(item):
    """Copy notices, never installed source or bytecode inside a licenses package."""
    path = Path(item)
    if path.suffix.lower() in {'.py', '.pyi', '.pyc', '.pyo', '.exe', '.dll', '.pyd', '.so', '.a', '.lib'}:
        return False
    if path.name.lower().startswith(NOTICE_PREFIXES):
        return True
    return (any(part.lower().startswith(('license', 'licence')) for part in path.parts[:-1])
            and path.suffix.lower() in {'.txt', '.md', '.rst', '.html', '.htm', '.xml', '.rtf', '.json', ''})


def notice_bytes(path):
    """Bounded text only: a filename resembling a notice cannot copy binary code."""
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_NOTICE_BYTES:
        raise ValueError('Missing, empty or oversized dependency notice: ' + path.name)
    data = path.read_bytes()
    if b'\0' in data:
        raise ValueError('Binary dependency notice: ' + path.name)
    try:
        data.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise ValueError('Dependency notice is not UTF-8 text: ' + path.name) from error
    return data


def pinned_notices(root, name, version):
    """Use reviewed exact-version upstream bytes offline, never license templates."""
    assets = Path(root) / 'assets' / 'third-party-licenses'
    manifest_path = assets / 'manifest.json'
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f'Full upstream notices required for {name}=={version}; no pinned fallback manifest')
    manifest = json.loads(notice_bytes(manifest_path))
    if manifest.get('schema') != 1 or not isinstance(manifest.get('distributions'), list):
        raise ValueError('Unsupported pinned dependency-notice manifest')
    matches = [entry for entry in manifest['distributions']
               if canonicalize_name(entry['name']) == canonicalize_name(name)
               and entry.get('version') == version]
    if len(matches) != 1 or not matches[0].get('files'):
        raise ValueError(f'Full upstream notices required for {name}=={version}; no unique exact-version fallback')
    entry = matches[0]
    loaded = []
    names = set()
    for item in entry['files']:
        relative = item['path']
        # Check both separator spellings even when collection runs on another OS.
        parts = relative.replace('\\', '/').split('/')
        if not parts or any(part in ('', '.', '..') or ':' in part for part in parts):
            raise ValueError('Invalid pinned dependency-notice path')
        path = assets.joinpath(*parts)
        if assets.is_symlink() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != assets.parent):
            raise ValueError('Linked pinned dependency-notice path')
        data = notice_bytes(path)
        digest = sha256(data).hexdigest()
        if digest != item.get('sha256'):
            raise ValueError(f'Pinned dependency-notice checksum mismatch for {name}=={version}')
        provenance = item.get('provenance')
        if not isinstance(provenance, dict) or not str(provenance.get('url', '')).startswith('https://'):
            raise ValueError('Missing upstream dependency-notice provenance')
        filename = '__'.join(parts[1:]) if len(parts) > 1 else parts[0]
        if filename in names:
            raise ValueError('Duplicate pinned dependency-notice filename')
        names.add(filename)
        loaded.append((filename, data, {'sha256': digest, 'provenance': provenance}))
    if not any(Path(filename).name.lower().startswith(('license', 'licence', 'copying'))
               or '__license' in filename.lower() or '__licence' in filename.lower()
               for filename, _, _ in loaded):
        raise ValueError('Pinned dependency notices contain no full license file')
    return entry['license'], loaded


def requirements(path, seen=None):
    seen = seen or set()
    path = Path(path).resolve()
    if path in seen:
        return []
    seen.add(path)
    result = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.extend(requirements(path.parent / line[3:].strip(), seen))
        else:
            result.append(Requirement(line))
    return result


def collect(output, root):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    queue = []
    for filename in ("requirements-dev.txt", "requirements-integrations.txt"):
        queue.extend(requirements(root / filename))
    visited = set()
    records = []
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        key = canonicalize_name(requirement.name)
        if key in visited:
            continue
        visited.add(key)
        try:
            dist = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            records.append({"name": requirement.name, "status": "not installed"})
            continue
        directory = output / re.sub(r"[^A-Za-z0-9_.-]", "_", dist.metadata["Name"])
        directory.mkdir(exist_ok=True)
        copied = []
        file_records = []
        declared = dist.metadata.get_all('License-File') if hasattr(dist.metadata, 'get_all') else []
        declared = [Path(name).as_posix().lower() for name in declared or []]
        has_license = False
        for item in dist.files or []:
            item_path = Path(item)
            declared_license = any(item_path.as_posix().lower().endswith('/' + name)
                                   or item_path.as_posix().lower() == name for name in declared)
            if is_notice(item) or declared_license:
                source = Path(dist.locate_file(item))
                if source.is_file():
                    data = notice_bytes(source)
                    name = re.sub(r"[^A-Za-z0-9_.-]", "_", "__".join(item_path.parts[-3:]))
                    # Some native wheels contain notices for several vendors with
                    # the same final path components. Preserve each distinct text.
                    if name in copied or len(name) > 180:
                        name = name[:140] + '__' + sha256(item_path.as_posix().encode()).hexdigest()[:16] + '.txt'
                    if name in copied:
                        raise ValueError('Duplicate installed dependency-notice member for ' + dist.metadata['Name'])
                    (directory / name).write_bytes(data)
                    copied.append(name)
                    file_records.append({'file': name, 'sha256': sha256(data).hexdigest(),
                                         'provenance': {'kind': 'installed-distribution-file', 'member': item_path.as_posix()}})
                    has_license = has_license or declared_license or item_path.name.lower().startswith(('license', 'licence', 'copying'))
        metadata_license = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See upstream license"
        license_value = metadata_license
        notice_source = 'installed-distribution'
        if not has_license:
            license_value, fallback = pinned_notices(root, dist.metadata['Name'], dist.version)
            for name, data, record in fallback:
                if name in copied:
                    raise ValueError('Duplicate fallback dependency-notice filename')
                (directory / name).write_bytes(data)
                copied.append(name)
                file_records.append({'file': name, **record})
            notice_source = 'pinned-upstream-fallback'
        # A successful recollection must not retain the old identifier-only file.
        (directory / 'LICENSE-METADATA.txt').unlink(missing_ok=True)
        records.append({"name": dist.metadata["Name"], "version": dist.version, "license": license_value,
                        "metadata_license": metadata_license, "notice_source": notice_source,
                        "files": copied, "file_records": file_records})
        for raw in dist.requires or []:
            try:
                queue.append(Requirement(raw))
            except ValueError:
                continue
    modules = root / "frontend" / "node_modules"
    if modules.is_dir():
        candidates = list(modules.glob("*/package.json")) + list(modules.glob("@*/*/package.json"))
        for metadata in candidates:
            package = json.loads(metadata.read_text(encoding="utf-8"))
            directory = output / ("npm-" + re.sub(r"[^A-Za-z0-9_.-]", "_", package.get("name", metadata.parent.name)))
            copied = []
            for source in metadata.parent.iterdir():
                if source.is_file() and source.name.lower().startswith(NOTICE_PREFIXES):
                    directory.mkdir(exist_ok=True)
                    (directory / source.name).write_bytes(notice_bytes(source))
                    copied.append(source.name)
            if copied:
                records.append({"name": package.get("name"), "version": package.get("version"), "license": package.get("license"), "files": copied})
    # The interpreter's own license is already shipped with binary distributions.
    interpreter_license = Path(sys.base_prefix) / "LICENSE.txt"
    if interpreter_license.is_file():
        (output / "PYTHON-LICENSE.txt").write_bytes(interpreter_license.read_bytes())
    (output / "manifest.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist/third-party-licenses")
    options = parser.parse_args()
    count = collect(Path(options.output), Path(__file__).resolve().parents[1])
    print(f"Collected notices for {count} distributions")
