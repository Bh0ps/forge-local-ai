"""Windows release stages: build, sign app externally, installer, sign, package."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge_updates import authenticode, file_hash, regular_tree, require_trust, safe_relative, version
from forge_update_keys import TRUSTED_UPDATE_KEYS
from forge_update_proof import (BUILD_NAME, RELEASE_NAME, MAX_EXPANDED_BYTES, MAX_RECORD_BYTES, MAX_ARTIFACT_BYTES,
    bind_release_build, normalize_keys, plain_root, read_record, relative_path,
    strict_json, tree_files, validated_build_manifest, verify_artifact, verify_build, verify_release)


def run(args, cwd):
    result = subprocess.run([str(v) for v in args], cwd=cwd, check=False)
    if result.returncode: raise RuntimeError('Release stage failed: ' + str(args[0]))


def publisher():
    value = os.environ.get('FORGE_PUBLISHER_SUBJECT', '').strip()
    return (value,) if value else ()


def verify_app(app, number, required, *, require_proof=False):
    regular_tree(app)
    proof = app / BUILD_NAME
    if proof.exists() or proof.is_symlink() or require_proof:
        verify_build(read_record(proof), normalize_keys(TRUSTED_UPDATE_KEYS), root=app, expected_version=number)
    if required:
        for name in ('Forge.exe', 'ForgeBrowserHost.exe'):
            require_trust(app / name, number, publisher())
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'third-party-licenses/manifest.json'):
        if not (app / name).is_file(): raise ValueError('Required dependency notice is missing: ' + name)


def build(args):
    source, work = args.source.resolve(), args.work.resolve()
    stage = work / 'source'
    if stage.exists(): raise ValueError('Use a new empty release work folder; existing builds remain intact.')
    stage.mkdir(parents=True)
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=source, check=True, capture_output=True).stdout.decode().split('\0')
    for relative in filter(None, tracked):
        path = safe_relative(relative)
        original = source / path
        if original.is_symlink(): raise ValueError('Tracked symbolic links are not permitted in the release.')
        if original.is_file():
            target = stage / path; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(original, target)
    if not (source / 'frontend/dist/index.html').is_file(): raise ValueError('Build the frontend before packaging.')
    shutil.copytree(source / 'frontend/dist', stage / 'frontend/dist')
    (stage / 'forge_release_policy.py').write_text('TRUSTED_PUBLISHERS = ' + repr(publisher()) + '\n', encoding='utf-8')
    from scripts.fetch_native_runtimes import fetch as fetch_native
    fetch_native(json.loads((stage/'native-runtime.lock.json').read_text(encoding='utf-8')),
                 stage/'native-runtimes',work/'native-downloads')
    parts = (*version(args.version), 0)
    text = "VSVersionInfo(ffi=FixedFileInfo(filevers=%r,prodvers=%r,mask=0x3f,flags=0,OS=0x40004,fileType=0x1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('040904b0',[StringStruct('ProductName','Forge'),StringStruct('ProductVersion',%r),StringStruct('FileVersion',%r),StringStruct('FileDescription','Forge local AI workspace')])]),VarFileInfo([VarStruct('Translation',[1033,1200])])])" % (parts, parts, args.version, args.version)
    (stage / 'version-info.py').write_text(text, encoding='utf-8')
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--noupx', '--distpath', work / 'dist',
              '--workpath', work / 'build', '--specpath', stage, '--version-file', stage / 'version-info.py']
    command = common + ['--onedir', '--windowed', '--name', 'Forge', '--icon', 'assets/forge.ico']
    for value in ('frontend/dist;frontend/dist', 'assets;assets', 'browser-extension;browser-extension', 'LICENSE;.', 'THIRD_PARTY_NOTICES.md;.',
                  'native-runtimes;native-runtimes','native-models.lock.json;.',
                  'forge_vendor/strata/LICENSE;forge_vendor/strata','forge_vendor/strata/ORIGIN.json;forge_vendor/strata',
                  'forge_vendor/strata/expert-profile-coder.bin;forge_vendor/strata',
                  'forge_vendor/strata/draft_vocab.bin;forge_vendor/strata',
                  'forge_vendor/strata/QWEN_MTP_LICENSE;forge_vendor/strata'):
        command += ['--add-data', value]
    for module in ('ddgs', 'tzdata', 'faster_whisper', 'av', 'huggingface_hub', 'hf_xet', 'gguf', 'sounddevice', 'pystray', 'webview', 'playwright','jinja2','regex'):
        command += ['--collect-all', module]
    command += ['--copy-metadata', 'huggingface-hub', '--copy-metadata', 'mcp', '--collect-submodules', 'mcp.client',
                '--collect-submodules', 'mcp.shared', '--hidden-import', 'forge_release_policy', '--hidden-import', 'forge_updates',
                '--hidden-import', 'webview.platforms.edgechromium', '--hidden-import', 'pywinauto', '--hidden-import', 'forge_service', 'desktop.py']
    run(command, stage)
    run(common + ['--onefile', '--console', '--name', 'ForgeBrowserHost', '--exclude-module', 'playwright', 'browser_tools.py'], stage)
    app = work / 'dist/Forge'
    shutil.copy2(work / 'dist/ForgeBrowserHost.exe', app / 'ForgeBrowserHost.exe')
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'): shutil.copy2(stage / name, app / name)
    run([sys.executable, source / 'scripts/collect_licenses.py', '--output', app / 'third-party-licenses'], source)
    run([sys.executable, source / 'scripts/prune_license_cache.py', '--app', app], source)
    from scripts.prune_native_runtime_aliases import prune as prune_native_aliases
    print('Native runtime packaging:', prune_native_aliases(app))
    verify_app(app, args.version, False)
    print('Application built. Sign Forge.exe and ForgeBrowserHost.exe before the installer stage for a public signed release.')


def installer(args):
    app, stage = args.work.resolve() / 'dist/Forge', args.work.resolve() / 'source'
    verify_app(app, args.version, args.require_signed, require_proof=getattr(args, 'require_publisher_proof', False))
    text = (stage / 'installer.iss').read_text(encoding='utf-8')
    text = re.sub(r'#define AppVersion "[^"]+"', '#define AppVersion "' + args.version + '"', text)
    text = re.sub(r'OutputBaseFilename=.*', 'OutputBaseFilename=Forge-' + args.version + '-Setup', text)
    # Per-user automatic updates must not invoke Restart Manager to close
    # unrelated browser/model processes or a concurrently opened Forge window.
    text = re.sub(r'CloseApplications=.*', 'CloseApplications=no', text)
    target = stage / 'release-installer.iss'; target.write_text(text, encoding='utf-8')
    output = args.work.resolve() / 'release'; output.mkdir(exist_ok=True)
    run([args.iscc, '/DStageDir=' + str(app), '/O' + str(output), target], stage)


def package(args):
    work = args.work.resolve(); app = work / 'dist/Forge'; output = work / 'release'; output.mkdir(exist_ok=True)
    verify_app(app, args.version, args.require_signed, require_proof=getattr(args, 'require_publisher_proof', False))
    setup = output / ('Forge-' + args.version + '-Setup.exe')
    if not setup.is_file(): raise ValueError('Build the installer first.')
    if not 0 < setup.stat().st_size <= MAX_ARTIFACT_BYTES:
        raise ValueError('Installer exceeds the supported authenticated-artifact size bound.')
    if args.require_signed: require_trust(setup, args.version, publisher())
    if args.require_signed:
        policy = (work / 'source/forge_release_policy.py').read_text(encoding='utf-8')
        if policy != 'TRUSTED_PUBLISHERS = ' + repr(publisher()) + '\n':
            raise ValueError('The application must be compiled with its protected publisher policy before signing.')
    files = tree_files(app)
    archive = output / ('Forge-' + args.version + '-Portable.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for entry in files:
            data = (app / safe_relative(entry['path'])).read_bytes()
            info = zipfile.ZipInfo(entry['path'], date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o100644 << 16
            target.writestr(info, data)
    if not 0 < archive.stat().st_size <= MAX_ARTIFACT_BYTES:
        raise ValueError('Portable archive exceeds the supported authenticated-artifact size bound.')
    manifest = {'schema': 1, 'version': args.version, 'repository': 'Bh0ps/forge-local-ai',
        'signing': 'verified-authenticode' if args.require_signed else 'unsigned-development-preview',
        'publisher_policy_embedded': bool(args.require_signed),
        'files': files, 'artifacts': [{'name': p.name, 'size': p.stat().st_size, 'sha256': file_hash(p)} for p in (setup, archive)]}
    (output / 'Forge-release.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    assets = [setup, archive, output / 'Forge-release.json']
    (output / 'SHA256SUMS.txt').write_text(''.join(file_hash(p) + '  ' + p.name + '\n' for p in assets), encoding='utf-8')
    print('Packages prepared:', manifest['signing'])


def verify_publisher_downloaded(args):
    """Verify public release bytes without executing either native host."""
    root = plain_root(args.work)
    pins = normalize_keys(TRUSTED_UPDATE_KEYS)
    release = verify_release(read_record(root / RELEASE_NAME), pins, expected_version=args.version,
        expected_source=getattr(args, 'source_commit', None), expected_channel=getattr(args, 'channel', 'preview'),
        expected_tag='v' + args.version, now=int(time.time()), current_version='0.0.0')
    for entry in release['artifacts']:
        path = root / entry['name']
        item = path.lstat()
        if not stat.S_ISREG(item.st_mode) or path.is_symlink() or getattr(item, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Release artifacts must be regular files.')
        verify_artifact(path, release, entry['name'])
    if getattr(args, 'require_signed', False):
        require_trust(root / ('Forge-' + args.version + '-Setup.exe'), args.version, publisher())
    names = [item['name'] for item in release['artifacts']] + ['Forge-release.json', RELEASE_NAME]
    checksums = ''.join(file_hash(root / name) + '  ' + name + '\n' for name in names).encode('ascii')
    if read_record(root / 'SHA256SUMS.txt') != checksums:
        raise ValueError('Release checksum file differs from the verified package bytes.')
    archive = root / ('Forge-' + args.version + '-Portable.zip')
    destination = root / 'verified-portable'
    if destination.exists() or destination.is_symlink():
        raise ValueError('Verification destination must be new.')
    with zipfile.ZipFile(archive) as source:
        records = source.infolist()
        proof_rows = [row for row in records if row.filename == BUILD_NAME]
        if len(proof_rows) != 1 or not 0 < proof_rows[0].file_size <= MAX_RECORD_BYTES:
            raise ValueError('Portable package is missing its bounded build proof.')
        raw_build = source.read(proof_rows[0])
        build = validated_build_manifest(raw_build, pins, expected_version=args.version,
                                         expected_source=release['source_commit'])
        bind_release_build(release, raw_build, build)
        wanted = {entry['path']: entry for entry in build['files']}
        wanted[BUILD_NAME] = {'path': BUILD_NAME, 'size': len(raw_build), 'sha256': sha256(raw_build).hexdigest()}
        if len(records) != len(wanted):
            raise ValueError('Portable package has an unexpected file set.')
        seen, total = set(), 0
        for row in records:
            path = relative_path(row.filename)
            mode = row.external_attr >> 16
            if (row.is_dir() or path.casefold() in seen or path not in wanted
                    or mode & 0o170000 not in (0, stat.S_IFREG) or row.external_attr & 0x400):
                raise ValueError('Portable package contains an unsafe or unexpected entry.')
            seen.add(path.casefold())
            total += row.file_size
            if row.file_size != wanted[path]['size'] or total > MAX_EXPANDED_BYTES:
                raise ValueError('Portable package differs from its bounded file proof.')
        # All names and declared sizes are checked before creating a destination.
        for row in records:
            target = destination / row.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = sha256()
            with source.open(row) as data, target.open('xb') as output:
                for chunk in iter(lambda: data.read(1024 * 1024), b''):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != wanted[row.filename]['sha256']:
                raise ValueError('Portable bytes differ from the trusted build proof.')
    verify_app(destination, args.version, getattr(args, 'require_signed', False), require_proof=True)
    metadata = strict_json(read_record(root / 'Forge-release.json'))
    if (not isinstance(metadata, dict) or set(metadata) != {'schema', 'version', 'repository', 'source_commit',
            'signing', 'publisher_policy_embedded', 'files', 'artifacts', 'update_proof'}
            or metadata.get('schema') != 1 or type(metadata['schema']) is not int
            or metadata.get('repository') != release['repository'] or metadata.get('version') != args.version
            or metadata.get('source_commit') != release['source_commit']
            or metadata.get('signing') != 'ed25519-publisher-proof' or metadata.get('publisher_policy_embedded') is not True
            or metadata.get('files') != tree_files(destination) or metadata.get('artifacts') != release['artifacts']):
        raise ValueError('Release manifest differs from the authenticated package.')
    proof_meta = {'name': RELEASE_NAME, 'size': (root / RELEASE_NAME).stat().st_size,
                  'sha256': file_hash(root / RELEASE_NAME)}
    if metadata.get('update_proof') != proof_meta:
        raise ValueError('Release manifest has a different publisher proof identity.')
    print('Publisher signature, exact source, installer, portable tree and file manifest verified.')


def verify_downloaded(args):
    proof = args.work / RELEASE_NAME
    if proof.exists() or proof.is_symlink() or getattr(args, 'require_publisher_proof', False):
        return verify_publisher_downloaded(args)
    root = args.work.resolve(); setup = root / ('Forge-' + args.version + '-Setup.exe')
    require_trust(setup, args.version, publisher())
    archive = root / ('Forge-' + args.version + '-Portable.zip')
    destination = root / 'verified-portable'
    if destination.exists(): raise ValueError('Verification destination must be new.')
    seen, total = set(), 0
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            path = safe_relative(entry.filename.rstrip('/'))
            if entry.filename.casefold() in seen or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive contains duplicate paths or symbolic links.')
            seen.add(entry.filename.casefold()); total += entry.file_size
            if total > MAX_EXPANDED_BYTES: raise ValueError('Archive expands beyond its permitted size.')
            target = destination / path
            if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as data, target.open('xb') as output: shutil.copyfileobj(data, output)
    verify_app(destination, args.version, True)
    metadata = json.loads((root / 'Forge-release.json').read_text(encoding='utf-8'))
    if metadata.get('schema') != 1 or metadata.get('repository') != 'Bh0ps/forge-local-ai' or metadata.get('version') != args.version or metadata.get('signing') != 'verified-authenticode' or metadata.get('publisher_policy_embedded') is not True:
        raise ValueError('Release manifest does not describe a verified signed build.')
    if regular_tree(destination) != metadata['files']: raise ValueError('Portable package does not match its file manifest.')
    expected_assets = {setup.name, archive.name}
    if {entry.get('name') for entry in metadata['artifacts']} != expected_assets or len(metadata['artifacts']) != 2:
        raise ValueError('Release manifest includes unexpected artifacts.')
    for entry in metadata['artifacts']:
        path = root / safe_relative(entry['name'])
        if path.stat().st_size != entry['size'] or file_hash(path) != entry['sha256']: raise ValueError('Package hash mismatch.')
    print('Installer, both executable signatures, version, publisher and archive paths verified.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=('build', 'installer', 'package', 'verify-downloaded'))
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--iscc', default='iscc')
    parser.add_argument('--require-signed', action='store_true')
    parser.add_argument('--require-publisher-proof', action='store_true')
    parser.add_argument('--source-commit', help='Exact source identity expected when verifying publisher-authenticated downloads')
    parser.add_argument('--channel', choices=('stable', 'preview'), default='preview')
    args = parser.parse_args(); version(args.version)
    {'build': build, 'installer': installer, 'package': package, 'verify-downloaded': verify_downloaded}[args.stage](args)


if __name__ == '__main__': main()
