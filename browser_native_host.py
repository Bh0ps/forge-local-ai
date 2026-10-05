"""Native-messaging entry with a registered-origin allowlist; stdout is protocol only."""
import json
import os
from pathlib import Path
import re
import sys


def validate_origin(origin, manifest):
    if not isinstance(origin,str) or not re.fullmatch(r'chrome-extension://[a-p]{32}/', origin):
        raise ValueError('Native messaging requires a registered Chrome/Edge extension origin.')
    if manifest.get('name') != 'org.forge.browser' or manifest.get('type') != 'stdio' or origin not in manifest.get('allowed_origins', []):
        raise ValueError('This extension has not been registered with Forge.')
    return origin


def main(argv=None, home=None):
    arguments = sys.argv[1:] if argv is None else argv
    root = Path(home or os.environ.get('FORGE_HOME') or Path.home()/'.forge')
    try:
        manifest = json.loads((root/'config/native-messaging/org.forge.browser.json').read_text(encoding='utf-8'))
        validate_origin(arguments[0] if arguments else '', manifest)
        if getattr(sys,'frozen',False) and Path(manifest.get('path','')).resolve() != Path(sys.executable).resolve():
            raise ValueError('Native messaging host differs from the registered installation.')
    except (ValueError, OSError, TypeError):
        raise SystemExit(1) from None
    from browser_tools import native_host_main
    native_host_main(root)


if __name__ == '__main__': main()
