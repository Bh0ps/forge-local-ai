"""Real, isolated browser evidence; no live model or external service is used."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PROBE_PATH = Path(__file__).parent / 'assets/evaluations/contrast_probe.py'
spec = importlib.util.spec_from_file_location('evaluation_contrast_probe', PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def browser():
    pytest.importorskip('playwright.sync_api')
    path = probe._browser_executable()
    if not path:
        pytest.skip('An existing browser is required; tests never download one.')
    return path


def fixture(folder, css, js='', theme=False):
    folder.mkdir(exist_ok=True)
    (folder / 'index.html').write_text('<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="styles.css"></head><body><main><p>Readable body text</p>'
        + ('<button id="theme-button">Switch theme</button>' if theme else '')
        + '</main><script src="app.js"></script></body></html>', encoding='utf-8')
    (folder / 'styles.css').write_text(css, encoding='utf-8')
    (folder / 'app.js').write_text(js, encoding='utf-8')
    return folder


THEME = ("document.body.dataset.theme='light';document.querySelector('#theme-button').onclick=()=>{"
         "document.body.dataset.theme='dark';localStorage.setItem('theme','dark');};")


def assert_clean(result):
    assert all(result['cleanup'][key] for key in ('server_stopped', 'browser_closed', 'profile_removed')), result
    assert not (Path(tempfile.gettempdir()) / result['cleanup']['profile_name']).exists()


@pytest.mark.parametrize('case_id', ['theme', 'landing'])
@pytest.mark.parametrize('high', [True, False])
def test_css_variables_use_real_computed_contrast_and_keep_actual_theme(tmp_path, browser, case_id, high):
    colors = '--fg:#fff;--bg:#172033' if high else '--fg:#777;--bg:#888'
    folder = fixture(tmp_path, ':root{' + colors + '}body{color:var(--fg);background:var(--bg)}',
                     THEME if case_id == 'theme' else '', case_id == 'theme')
    result = probe.probe_contrast(folder, case_id, browser_executable=browser)
    assert result['status'] == ('verified' if high else 'failed'), result
    assert result['passed'] is high and result['backend']['verified']
    assert result['backend']['browser_version'] and result['backend']['probe_version'] == probe.PROBE_VERSION
    assert 'var(' not in result['foreground'] and 'var(' not in result['background']
    assert (result['contrast_ratio'] >= 4.5) is high
    assert not result['computed']['bridge_exposed']
    if case_id == 'theme':
        assert result['theme_body_state'] == 'dark' and result['computed']['local_storage_theme'] == 'dark'
    assert_clean(result)


@pytest.mark.parametrize('css', [
    'html{background:white}body{background:transparent;color:rgba(0,0,0,.5)}',
    'html{background:white}body{background:rgba(0,0,0,.5);color:white}',
])
def test_transparent_paint_is_composited_instead_of_claiming_black_white_contrast(tmp_path, browser, css):
    result = probe.probe_contrast(fixture(tmp_path, css), browser_executable=browser)
    assert result['status'] == 'failed' and 3.9 < result['contrast_ratio'] < 4.1, result
    assert result['backend']['verified']
    assert_clean(result)


def test_canvas_conversion_supports_css_color_formats_and_actual_dark_canvas(tmp_path, browser):
    colors = probe.probe_contrast(fixture(tmp_path, 'body{color:color(srgb 0 0 0);background:lab(100% 0 0)}'), browser_executable=browser)
    assert colors['status'] == 'verified' and colors['contrast_ratio'] > 20.9, colors
    assert_clean(colors)
    dark = probe.probe_contrast(fixture(tmp_path, 'html{color-scheme:dark}body{color:white;background:transparent}'), browser_executable=browser)
    assert dark['status'] == 'verified' and max(dark['computed']['background_rgb']) < 30, dark
    assert_clean(dark)


def test_nonuniform_background_and_broken_toggle_are_never_verified(tmp_path, browser):
    gradient = probe.probe_contrast(fixture(tmp_path, 'body{color:black;background:linear-gradient(white,black)}'), browser_executable=browser)
    assert gradient['status'] == 'unverified' and not gradient['backend']['verified'], gradient
    assert 'Nonuniform' in gradient['diagnostics']['detail']
    assert_clean(gradient)
    broken = probe.probe_contrast(fixture(tmp_path, 'body{color:white;background:black}',
        "document.body.dataset.theme='light';", True), 'theme', browser_executable=browser)
    assert broken['status'] == 'failed' and broken['theme_body_state'] == 'light', broken
    assert_clean(broken)


def test_requests_outside_fixture_origin_and_websocket_are_blocked(tmp_path, browser):
    incoming = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            incoming.append(self.path); self.send_response(200); self.end_headers()
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        target = f'127.0.0.1:{server.server_port}'
        script = f"fetch('http://{target}/must-not-arrive').catch(()=>{{}});new WebSocket('ws://{target}/must-not-connect');console.log('bounded diagnostic');"
        result = probe.probe_contrast(fixture(tmp_path, 'body{color:black;background:white}', script), browser_executable=browser)
        assert result['status'] == 'unverified' and not result['passed'], result
        assert 'WebSocket' in result['diagnostics']['detail']
        assert result['diagnostics']['blocked_requests'] >= 2
        assert len(result['diagnostics']['blocked_urls']) <= probe.MAX_DIAGNOSTICS
        assert incoming == []
        assert_clean(result)
    finally:
        server.shutdown(); server.server_close(); worker.join(1)


def test_deadline_kills_owned_browser_tree_and_server_even_for_infinite_page_script(tmp_path, browser):
    result = probe.probe_contrast(fixture(tmp_path, 'body{color:black;background:white}', 'while(true){}'),
                                  timeout_seconds=3, browser_executable=browser)
    assert result['status'] == 'unverified' and result['diagnostics']['timed_out'], result
    assert result['diagnostics']['browser_started'], result
    assert not result['backend']['verified']
    assert_clean(result)
    # Verify only this fresh profile's processes; never terminate or adopt a PID.
    psutil = pytest.importorskip('psutil')
    for process in psutil.process_iter(['cmdline']):
        try:
            assert result['cleanup']['profile_name'] not in ' '.join(process.info['cmdline'] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass


def test_unavailable_browser_is_explicitly_unverified_without_downloading(tmp_path):
    unavailable = probe.probe_contrast(tmp_path, browser_executable=str(tmp_path / 'missing-browser.exe'))
    assert unavailable['status'] == 'unverified' and not unavailable['passed'] and not unavailable['backend']['verified']


def test_oversize_fixture_is_explicitly_unverified_before_browser_launch(tmp_path, browser):
    folder = fixture(tmp_path, 'body{color:black;background:white}')
    (folder / 'oversized.txt').write_bytes(b'x' * (probe.MAX_FILE_BYTES + 1))
    result = probe.probe_contrast(folder, browser_executable=browser)
    assert result['status'] == 'unverified' and not result['diagnostics']['browser_started'], result
    assert 'bounded' in result['diagnostics']['detail']
    assert_clean(result)


def test_cli_is_a_single_json_protocol_and_invalid_cases_never_pass(tmp_path, browser):
    folder = fixture(tmp_path, 'body{color:black;background:white}', "console.log('真实颜色 ✓');")
    output = subprocess.run([sys.executable, '-B', str(PROBE_PATH)], input=json.dumps({'fixture_dir': str(folder),
        'case_id': 'landing', 'timeout_seconds': 10}), capture_output=True, text=True, timeout=14)
    assert output.returncode == 0 and len(output.stdout.splitlines()) == 1, output
    result = json.loads(output.stdout)
    assert result['status'] == 'verified'
    assert '真实颜色 ✓' in result['diagnostics']['console']
    assert_clean(result)
    assert probe.probe_contrast(folder, 'other')['status'] == 'unverified'
