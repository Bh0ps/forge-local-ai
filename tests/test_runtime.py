"""Background-job contracts and native mode behavior without opening a window."""
import ast
import logging
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from runtime import Jobs
from forge_host import PairingAuthority


def request(**extra):
    return {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}], **extra}


class FakeCore:
    def __init__(self, chunks=()):
        self.chunks = chunks
        self.sent = None
        self.calls = []

    def stream_chat(self, data, cancelled):
        self.sent = data
        yield from self.chunks

    def dispatch(self, action, data):
        self.calls.append((action, data))
        if action == 'show':
            return {'capabilities': ['vision', 'thinking']}
        if action == 'research':
            return {'sources': [{'title': 'Example', 'url': 'https://example.com', 'snippet': 'A fact'}]}
        raise AssertionError(action)


def completed(jobs, job_id):
    """Wait for producer completion before draining to inspect coalescing."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with jobs.lock:
            finished = jobs.active['finished']
        if finished:
            return jobs.poll(job_id)
        time.sleep(0.001)
    pytest.fail('Background job did not finish')


def test_stream_tokens_are_batched_and_completion_is_preserved():
    core = FakeCore([{'message': {'content': 'x'}} for _ in range(4000)] +
                    [{'done': True, 'done_reason': 'length'}])
    jobs = Jobs(core)
    job_id = jobs.start(request())['id']
    result = completed(jobs, job_id)
    assert result['finished'] is True
    assert result['events'] == [
        {'type': 'status', 'text': 'Generating…'},
        {'type': 'token', 'text': 'x' * 4000},
        {'type': 'complete', 'reason': 'length'},
        {'type': 'done', 'cancelled': False},
    ]
    assert jobs.poll(job_id) == {'events': [], 'finished': True}


@pytest.mark.parametrize('options', [{}, {'thinking': False}])
def test_thinking_chunks_are_suppressed_unless_enabled(options):
    core = FakeCore([{'message': {'thinking': 'model reasoning'}} for _ in range(100)] +
                    [{'message': {'content': 'answer'}, 'done': True}])
    jobs = Jobs(core)
    result = completed(jobs, jobs.start(request(**options))['id'])
    assert sum(e.get('text') == 'Thinking…' for e in result['events']) == 1
    assert 'model reasoning' not in str(result)
    assert not any(e['type'] == 'thinking' for e in result['events'])
    assert {'type': 'token', 'text': 'answer'} in result['events']
    assert {'type': 'complete', 'reason': 'stop'} in result['events']


def test_enabled_thinking_is_batched_separately_from_answer():
    core = FakeCore([{'message': {'thinking': 'step '}} for _ in range(4000)] +
                    [{'message': {'content': 'answer'}, 'done': True}])
    jobs = Jobs(core)
    data = request(thinking=True)
    result = completed(jobs, jobs.start(data)['id'])
    assert [e for e in result['events'] if e['type'] == 'thinking'] == [
        {'type': 'thinking', 'text': 'step ' * 4000},
    ]
    assert [e for e in result['events'] if e['type'] == 'token'] == [
        {'type': 'token', 'text': 'answer'},
    ]
    assert {'type': 'complete', 'reason': 'stop'} in result['events']
    assert result['events'][-1] == {'type': 'done', 'cancelled': False}
    assert core.sent['thinking'] is True
    assert data == request(thinking=True)


def test_thinking_cap_preserves_answer_and_completion():
    core = FakeCore([
        {'message': {'thinking': 'a' * 40000}},
        {'message': {'thinking': 'b' * 20000}},
        {'message': {'thinking': 'discarded'}},
        {'message': {'content': 'answer'}, 'done': True},
    ])
    jobs = Jobs(core)
    result = completed(jobs, jobs.start(request(thinking=True))['id'])
    assert [e for e in result['events'] if e['type'] == 'thinking'] == [
        {'type': 'thinking', 'text': 'a' * 40000 + 'b' * 10000},
    ]
    assert {'type': 'token', 'text': 'answer'} in result['events']
    assert {'type': 'complete', 'reason': 'stop'} in result['events']
    assert not any(e['type'] == 'error' for e in result['events'])


def test_mixed_chunk_preserves_enabled_thinking_and_answer():
    core = FakeCore([{'message': {'thinking': 'reason', 'content': 'answer'}, 'done': True}])
    jobs = Jobs(core)
    result = completed(jobs, jobs.start(request(thinking=True))['id'])
    assert {'type': 'thinking', 'text': 'reason'} in result['events']
    assert {'type': 'token', 'text': 'answer'} in result['events']
    assert {'type': 'complete', 'reason': 'stop'} in result['events']


def test_stream_error_preserves_partial_output_and_finishes():
    class FailingCore(FakeCore):
        def stream_chat(self, data, cancelled):
            yield {'message': {'content': 'partial'}}
            raise OSError('upstream closed')

    jobs = Jobs(FailingCore())
    result = completed(jobs, jobs.start(request())['id'])
    assert {'type': 'token', 'text': 'partial'} in result['events']
    assert {'type': 'error', 'text': 'upstream closed'} in result['events']
    assert not any(e['type'] == 'complete' for e in result['events'])
    assert result['events'][-1] == {'type': 'done', 'cancelled': False}


def test_cancelled_job_discards_late_tokens_and_rejects_overlap():
    started, release = threading.Event(), threading.Event()

    class HeldCore(FakeCore):
        def stream_chat(self, data, cancelled):
            yield {'message': {'content': 'before'}}
            started.set()
            assert release.wait(2)
            yield {'message': {'content': 'after'}, 'done': True}

    jobs = Jobs(HeldCore())
    job_id = jobs.start(request())['id']
    try:
        assert started.wait(2)
        assert jobs.cancel(job_id) == {'ok': True}
        with pytest.raises(ValueError, match='stopping'):
            jobs.start(request())
    finally:
        release.set()
    result = completed(jobs, job_id)
    assert {'type': 'token', 'text': 'before'} in result['events']
    assert not any(e.get('text') == 'after' or e['type'] == 'complete' for e in result['events'])
    assert result['events'][-1] == {'type': 'done', 'cancelled': True}
    # Finished jobs no longer prevent a fresh request.
    next_id = jobs.start(request())['id']
    assert next_id != job_id
    assert completed(jobs, next_id)['finished'] is True


def test_cancellation_during_research_skips_generation():
    started, release = threading.Event(), threading.Event()

    class SearchCore(FakeCore):
        def dispatch(self, action, data):
            started.set()
            assert release.wait(2)
            return super().dispatch(action, data)

    core = SearchCore()
    jobs = Jobs(core)
    job_id = jobs.start(request(web=True))['id']
    try:
        assert started.wait(2)
        jobs.cancel(job_id)
    finally:
        release.set()
    result = completed(jobs, job_id)
    assert core.sent is None
    assert result['events'][-1] == {'type': 'done', 'cancelled': True}


def test_research_sources_are_emitted_and_input_is_not_mutated():
    data = request(web=True)
    core = FakeCore([{'message': {'content': 'answer'}, 'done': True}])
    jobs = Jobs(core)
    result = completed(jobs, jobs.start(data)['id'])
    assert data == request(web=True)
    assert core.calls == [('research', {'query': 'hello'})]
    assert 'Untrusted search snippets' in core.sent['messages'][-1]['content']
    assert 'https://example.com' in core.sent['messages'][-1]['content']
    assert 'web' not in core.sent
    assert next(e for e in result['events'] if e['type'] == 'sources')['sources'][0]['title'] == 'Example'


def test_nonvision_model_rejects_image_before_generation():
    class TextCore(FakeCore):
        def dispatch(self, action, data):
            assert action == 'show'
            return {'capabilities': ['completion']}

    core = TextCore()
    jobs = Jobs(core)
    data = request(messages=[{'role': 'user', 'content': 'describe', 'images': ['encoded']}])
    result = completed(jobs, jobs.start(data)['id'])
    assert core.sent is None
    assert {'type': 'error', 'text': 'Select a vision model to send an image.'} in result['events']


def test_response_size_limit_terminates_run_without_queueing_oversized_token():
    jobs = Jobs(FakeCore([{'message': {'content': 'x' * 250001}}]))
    result = completed(jobs, jobs.start(request())['id'])
    assert not any(e['type'] == 'token' for e in result['events'])
    assert any(e['type'] == 'error' and 'too long' in e['text'] for e in result['events'])


def test_unknown_job_id_cannot_drain_another_jobs_output():
    jobs = Jobs(FakeCore([{'message': {'content': 'answer'}, 'done': True}]))
    job_id = jobs.start(request())['id']
    assert jobs.poll('not-this-job') == {'events': [], 'finished': True}
    assert any(e['type'] == 'token' for e in completed(jobs, job_id)['events'])


@pytest.fixture
def bridge_class():
    # Load only the actual Bridge definition. No desktop import, mutex,
    # logging setup, WebView2 import, or native startup occurs in these tests.
    source = Path(__file__).resolve().parents[1] / 'desktop.py'
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Bridge')
    namespace = {'Core': FakeCore, 'Jobs': Jobs, 'threading': threading,
                 'PairingAuthority': PairingAuthority, 'Path': Path,
                 'log': logging.getLogger('forge-test')}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['Bridge']


@pytest.fixture
def native_bridge(monkeypatch, bridge_class):
    class Form:
        Left, Top, Width, Height = 500, 300, 1000, 760
        WindowState, TopMost, DeviceDpi = 'normal', False, 96

        def SetBounds(self, x, y, width, height):
            self.Left, self.Top, self.Width, self.Height = x, y, width, height

    area = SimpleNamespace(Left=0, Top=0, Right=1920, Bottom=1080, Width=1920, Height=1080)
    forms = ModuleType('System.Windows.Forms')
    forms.Screen = SimpleNamespace(FromControl=lambda _: SimpleNamespace(WorkingArea=area))
    forms.FormWindowState = SimpleNamespace(Normal='normal', Maximized='maximized', Minimized='minimized')
    monkeypatch.setitem(sys.modules, 'System', ModuleType('System'))
    monkeypatch.setitem(sys.modules, 'System.Windows', ModuleType('System.Windows'))
    monkeypatch.setitem(sys.modules, 'System.Windows.Forms', forms)
    drawing = ModuleType('System.Drawing')
    drawing.Size = lambda width, height: (width, height)
    monkeypatch.setitem(sys.modules, 'System.Drawing', drawing)
    bridge, form = bridge_class(), Form()
    bridge._native = lambda operation: operation(form)
    return bridge, form, area


def test_hud_always_on_top_expands_and_restores_full_bounds(native_bridge):
    bridge, form, _ = native_bridge
    original = (form.Left, form.Top, form.Width, form.Height)
    assert bridge.mode('hud') == {'mode': 'hud', 'expanded': False}
    assert (form.Width, form.Height, form.TopMost) == (620, 188, True)
    assert form.MinimumSize == (360, 150)
    assert bridge.mode('hud', True) == {'mode': 'hud', 'expanded': True}
    assert (form.Width, form.Height) == (620, 440)
    assert bridge.mode('full') == {'mode': 'full', 'expanded': False}
    assert (form.Left, form.Top, form.Width, form.Height) == original
    assert form.MinimumSize == (760, 560)
    assert form.TopMost is False


def test_hud_ignores_unpin_until_restored_and_retains_pin_preference(native_bridge):
    bridge, form, _ = native_bridge
    assert bridge.pin(True) == {'ok': True, 'pinned': True}
    bridge.mode('hud')
    bridge.mode('full')
    assert form.TopMost is True
    bridge.mode('hud')
    bridge.pin(False)
    assert form.TopMost is True
    bridge.mode('full')
    assert form.TopMost is False


def test_hud_restores_maximized_workspace_after_compact_and_expanded_modes(native_bridge):
    bridge, form, _ = native_bridge
    form.WindowState = 'maximized'
    assert 'error' not in bridge.mode('hud')
    assert form.WindowState == 'normal'
    assert (form.Width, form.Height, form.TopMost) == (620, 188, True)
    assert 'error' not in bridge.mode('hud', True)
    assert 'error' not in bridge.mode('full')
    assert form.WindowState == 'maximized'
    assert form.TopMost is False
    # Repeating the cycle does not replace workspace state with HUD bounds.
    bridge.mode('hud')
    bridge.mode('full')
    assert form.WindowState == 'maximized'


def test_show_preserves_maximized_workspace_and_restores_minimized_hud(native_bridge):
    bridge, form, _ = native_bridge
    shown, activated = [], []
    bridge._window = SimpleNamespace(show=lambda: shown.append(True))
    form.Activate = lambda: activated.append(True)
    form.WindowState = 'maximized'
    bridge.show()
    assert form.WindowState == 'maximized'
    form.WindowState = 'minimized'
    bridge.show()
    assert form.WindowState == 'maximized'
    bridge.mode('hud')
    form.WindowState = 'minimized'
    bridge.show()
    assert form.WindowState == 'normal' and form.TopMost
    assert len(shown) == len(activated) == 3


def test_repeated_full_mode_preserves_user_restored_window(native_bridge):
    bridge, form, _ = native_bridge
    original = (form.Left, form.Top, form.Width, form.Height)
    bridge.mode('hud')
    bridge.mode('full')
    bridge.mode('full')
    assert form.WindowState == 'normal'
    assert (form.Left, form.Top, form.Width, form.Height) == original


def test_hud_uses_dpi_and_clamps_to_current_monitor_work_area(native_bridge):
    bridge, form, area = native_bridge
    form.DeviceDpi = 144
    form.Left, form.Top = -500, 1500
    area.Left, area.Top, area.Right, area.Bottom = 100, 200, 900, 700
    area.Width, area.Height = 800, 500
    form.WindowState = 'maximized'
    assert 'error' not in bridge.mode('hud', True)
    assert form.WindowState == 'normal'
    assert (form.Left, form.Top, form.Width, form.Height) == (100, 200, 800, 500)


def test_invalid_mode_does_not_modify_window(native_bridge):
    bridge, form, _ = native_bridge
    assert bridge.mode('unexpected') == {'error': 'Unknown window mode'}
    assert bridge._mode == 'full'
    assert (form.Width, form.Height, form.TopMost) == (1000, 760, False)


def test_bridge_close_hides_and_quit_cancels_work_and_rejects_requests(bridge_class):
    bridge = bridge_class()
    calls = []
    bridge._window = SimpleNamespace(hide=lambda: calls.append('hide'), destroy=lambda: calls.append('destroy'))
    bridge._tray = SimpleNamespace(stop=lambda: calls.append('tray_stop'))
    bridge._lifecycle = SimpleNamespace(quit=lambda: calls.append('cancel_and_shutdown'))
    assert bridge._close() is False
    assert calls == ['hide'] and bridge._closing is False
    assert bridge.quit() == {'ok': True}
    assert calls == ['hide', 'cancel_and_shutdown', 'tray_stop', 'destroy']
    assert bridge.call('start_chat', request()) == {'error': 'Forge is shutting down.'}
