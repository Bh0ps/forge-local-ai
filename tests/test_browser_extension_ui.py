"""Mock Chrome/DOM regressions execute the shipped extension JavaScript."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_extension_connection_and_guarded_actions():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the extension JavaScript tests.')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('browser_extension.test.mjs'))],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
