"""Exercise a frozen windowed host's inert pipe protocol on disposable state.

This does not load an update intent, trust proof, coordinator or installer.
It checks real inherited handles and exact OS parent identity before packaging.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge_native_handoff import HelperTransport, HandoffRejected, peer_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    output = args.output.absolute()
    if os.name != 'nt' or not executable.is_file() or output.exists():
        parser.error('Use a Windows host and a new disposable output directory.')
    output.mkdir(parents=True)
    checks = {}
    for case in ('commit', 'abort', 'wrong_parent_identity'):
        env = os.environ.copy()
        env['FORGE_DATA_DIR'] = str(output / case / 'state')
        auth = {'kind': 'AUTH', 'protocol': 1, 'operation_id': secrets.token_hex(16),
            'home': 'synthetic-unused-home', 'nonce': secrets.token_hex(32),
            'session': secrets.token_hex(32), 'intent_sha256': secrets.token_hex(32),
            'parent_pid': os.getpid(), 'parent_created': psutil.Process().create_time(),
            'helper_version': '0.0.0', 'helper_source_commit': None}
        if case == 'wrong_parent_identity':
            auth['parent_created'] -= 1
        def launch(command, **kwargs):
            if command != [str(executable), '--forge-update']:
                raise ValueError('Unexpected fixture command.')
            return subprocess.Popen([str(executable), '--smoke-test', '--forge-update-protocol-smoke'],
                env=env, **kwargs)
        transport = None
        try:
            transport = HelperTransport(SimpleNamespace(command=(str(executable), '--forge-update')),
                auth, popen=launch, timeout=15)
            if case == 'wrong_parent_identity':
                try:
                    transport.await_ready()
                except HandoffRejected:
                    checks[case] = transport.process.wait(timeout=5) == 1
                else:
                    checks[case] = False
            else:
                ready = transport.await_ready()
                if case == 'commit':
                    transport.commit(ready, admit=lambda: True)
                else:
                    transport.channel.write(peer_frame(auth, 'ABORT', transport.process.pid))
                checks[case] = transport.process.wait(timeout=5) == 0
        except Exception as error:
            checks[case] = False
            checks[case + '_failure_class'] = type(error).__name__
        finally:
            if transport is not None and not transport.stop():
                checks[case] = False
    passed = all(checks.get(case) is True for case in ('commit', 'abort', 'wrong_parent_identity'))
    result = {'passed': passed, 'checks': checks, 'installer_executed': False}
    (output / 'protocol-smoke.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
