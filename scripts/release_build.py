"""Windows release stages: build, sign app externally, installer, sign, package."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge_updates import authenticode, file_hash, regular_tree, require_trust, safe_relative, version


def run(args, cwd):
    result = subprocess.run([str(v) for v in args], cwd=cwd, check=False)
    if result.returncode: raise RuntimeError('Release stage failed: ' + str(args[0]))


def publisher():
    value = os.environ.get('FORGE_PUBLISHER_SUBJECT', '').strip()
    return (value,) if value else ()


def verify_app(app, number, required):
    regular_tree(app)
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
    parts = (*version(args.version), 0)
    text = "VSVersionInfo(ffi=FixedFileInfo(filevers=%r,prodvers=%r,mask=0x3f,flags=0,OS=0x40004,fileType=0x1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('040904b0',[StringStruct('ProductName','Forge'),StringStruct('ProductVersion',%r),StringStruct('FileVersion',%r),StringStruct('FileDescription','Forge local AI workspace')])]),VarFileInfo([VarStruct('Translation',[1033,1200])])])" % (parts, parts, args.version, args.version)
    (stage / 'version-info.py').write_text(text, encoding='utf-8')
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--noupx', '--distpath', work / 'dist',
              '--workpath', work / 'build', '--specpath', stage, '--version-file', stage / 'version-info.py']
    command = common + ['--onedir', '--windowed', '--name', 'Forge', '--icon', 'assets/forge.ico']
    for value in ('frontend/dist;frontend/dist', 'assets;assets', 'browser-extension;browser-extension', 'LICENSE;.', 'THIRD_PARTY_NOTICES.md;.'):
        command += ['--add-data', value]
    for module in ('ddgs', 'tzdata', 'faster_whisper', 'av', 'huggingface_hub', 'hf_xet', 'gguf', 'sounddevice', 'pystray', 'webview', 'playwright'):
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
    verify_app(app, args.version, False)
    print('Application built. Sign Forge.exe and ForgeBrowserHost.exe before the installer stage for a public signed release.')


def installer(args):
    app, stage = args.work.resolve() / 'dist/Forge', args.work.resolve() / 'source'
    verify_app(app, args.version, args.require_signed)
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
    verify_app(app, args.version, args.require_signed)
    setup = output / ('Forge-' + args.version + '-Setup.exe')
    if not setup.is_file(): raise ValueError('Build the installer first.')
    if args.require_signed: require_trust(setup, args.version, publisher())
    if args.require_signed:
        policy = (work / 'source/forge_release_policy.py').read_text(encoding='utf-8')
        if policy != 'TRUSTED_PUBLISHERS = ' + repr(publisher()) + '\n':
            raise ValueError('The application must be compiled with its protected publisher policy before signing.')
    files = regular_tree(app)
    archive = output / ('Forge-' + args.version + '-Portable.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for entry in files:
            data = (app / safe_relative(entry['path'])).read_bytes()
            info = zipfile.ZipInfo(entry['path'], date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o100644 << 16
            target.writestr(info, data)
    manifest = {'schema': 1, 'version': args.version, 'repository': 'Bh0ps/forge-local-ai',
        'signing': 'verified-authenticode' if args.require_signed else 'unsigned-development-preview',
        'publisher_policy_embedded': bool(args.require_signed),
        'files': files, 'artifacts': [{'name': p.name, 'size': p.stat().st_size, 'sha256': file_hash(p)} for p in (setup, archive)]}
    (output / 'Forge-release.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    assets = [setup, archive, output / 'Forge-release.json']
    (output / 'SHA256SUMS.txt').write_text(''.join(file_hash(p) + '  ' + p.name + '\n' for p in assets), encoding='utf-8')
    print('Packages prepared:', manifest['signing'])


def verify_downloaded(args):
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
            if total > 2 * 1024**3: raise ValueError('Archive expands beyond its permitted size.')
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
    args = parser.parse_args(); version(args.version)
    {'build': build, 'installer': installer, 'package': package, 'verify-downloaded': verify_downloaded}[args.stage](args)


if __name__ == '__main__': main()
