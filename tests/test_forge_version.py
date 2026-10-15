"""Release metadata must agree across backend and npm packaging boundaries."""
import json
from pathlib import Path

from forge_version import VERSION


def test_backend_and_frontend_release_metadata_agree():
    frontend = Path(__file__).resolve().parents[1] / 'frontend'
    package = json.loads((frontend / 'package.json').read_text(encoding='utf-8'))
    lock = json.loads((frontend / 'package-lock.json').read_text(encoding='utf-8'))
    assert VERSION == package['version'] == lock['version'] == lock['packages']['']['version']
