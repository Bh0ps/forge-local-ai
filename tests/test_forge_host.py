"""Host security and lifecycle tests never inspect or control real apps."""
import base64
import io
import os
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from computer_tools import ComputerBroker
from dictation import Dictation
from forge_host import EmergencyHotkey, HostLifecycle, PairingAuthority, SingleCoordinator
from model_manager import create_app


class FakeService:
    def __init__(self, directory):
        self.store = SimpleNamespace(home=directory)
        self.calls = []
        self.stopped = 0
        self.closed = 0

    def dispatch(self, action, data):
        self.calls.append((action, data))
        if action == 'poll':
            return {'events': [{'seq': 2, 'type': 'text', 'text': 'Hello'}] if data['after'] < 2 else [],
                    'finished': True, 'status': 'completed', 'next_cursor': 2}
        return {'ok': True}

    def emergency_stop(self):
        self.stopped += 1

    def shutdown(self):
        self.closed += 1


@pytest.fixture
def browser(tmp_path):
    service = FakeService(tmp_path)
    auth = PairingAuthority()
    frontend = tmp_path / 'frontend'
    frontend.mkdir()
    (frontend / 'index.html').write_text('<title>Forge</title>', encoding='utf-8')
    (frontend / 'bundle.js').write_text('console.log("Forge")', encoding='utf-8')
    client = TestClient(create_app(service=service, auth=auth, assets_dir=frontend), base_url='http://127.0.0.1')
    return client, auth, service


def pair(client, authority):
    code = authority.issue()['code']
    response = client.post('/api/v1/pair', json={'code': code})
    assert response.status_code == 200
    client.headers['X-Forge-Client-Proof'] = response.json()['client_proof']
    return response


def test_pairing_expiry_single_use_and_revocation():
    now = [0]
    auth = PairingAuthority(clock=lambda: now[0])
    code = auth.issue()['code']
    token = auth.pair(code)
    assert auth.valid(token)
    with pytest.raises(ValueError, match='Invalid'):
        auth.pair(code)
    auth.revoke(token)
    assert not auth.valid(token)
    code = auth.issue()['code']
    now[0] = 181
    with pytest.raises(ValueError, match='expired'):
        auth.pair(code)


def test_pairing_attempt_limit():
    auth = PairingAuthority()
    for _ in range(8):
        with pytest.raises(ValueError):
            auth.pair('INVALID')
    with pytest.raises(ValueError, match='Too many'):
        auth.pair(auth.issue()['code'])


def test_api_requires_pairing_for_all_aliases(browser):
    client, auth, service = browser
    for path in ('/api/v1/models', '/api/models'):
        assert client.post(path, json={}).status_code == 401
    assert service.calls == []
    response = pair(client, auth)
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert client.post('/api/v1/models', json={}).json() == {'ok': True}
    assert client.post('/api/v1/unpair', json={}).status_code == 200
    assert client.post('/api/v1/models', json={}).status_code == 401


def test_host_origin_and_content_guards(browser):
    client, auth, _ = browser
    pair(client, auth)
    assert client.post('/api/v1/models', json={}, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    assert client.post('/api/v1/models', json={}, headers={'Host': 'attacker.invalid'}).status_code == 403
    assert client.post('/api/v1/models', content='{}').status_code == 415
    assert client.post('/api/v1/models', content='[1]', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.get('/').status_code == 200
    assert client.get('/bundle.js').status_code == 200
    assert client.get('/%2e%2e/test_forge_host.py').status_code == 404
    health = client.get('/api/v1/health').json()
    assert health['app'] == 'Forge' and health['version'] == '5.0.4'


def test_cursor_events_json_and_sse(browser):
    client, auth, _ = browser
    pair(client, auth)
    assert client.get('/api/v1/runs/run/events?after=1').json()['events'][0]['seq'] == 2
    assert client.get('/api/v1/runs/run/events?after=2').json()['events'] == []
    assert client.get('/api/v1/runs/run/events?after=-1').status_code == 400
    stream = client.get('/api/v1/runs/run/events?after=1', headers={'Accept': 'text/event-stream'})
    assert 'id: 2' in stream.text
    assert 'event: finished' in stream.text
    resumed = client.get('/api/v1/runs/run/events?after=0', headers={'Accept': 'text/event-stream', 'Last-Event-ID': '2'})
    assert 'id: 2' not in resumed.text and 'event: finished' in resumed.text


def test_quit_is_idempotent_and_revokes_sessions(tmp_path):
    service = FakeService(tmp_path)
    auth = PairingAuthority()
    token = auth.pair(auth.issue()['code'])
    server = SimpleNamespace(should_exit=False)
    lifecycle = HostLifecycle(service, auth, server)
    lifecycle.quit()
    lifecycle.quit()
    assert service.stopped == service.closed == 1
    assert server.should_exit and not auth.valid(token)


def test_single_coordinator_and_shortcut(tmp_path):
    first, second = SingleCoordinator(tmp_path), SingleCoordinator(tmp_path)
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        second.close()
        first.close()
    assert EmergencyHotkey.parse('Ctrl+Alt+Shift+S') == (0x4007, ord('S'))
    with pytest.raises(ValueError):
        EmergencyHotkey.parse('S')


class FakeElement:
    handle = 101
    name = 'Disposable test window'
    def __init__(self, password=False):
        self.element_info = SimpleNamespace(runtime_id=[1, 2], automation_id='target', control_type='Button', is_password=password)
        self.box = SimpleNamespace(left=10, top=20, right=310, bottom=220)
        self.clicked = 0
        self.focused = 0
    def window_text(self): return self.name
    def process_id(self): return 55
    def rectangle(self): return self.box
    def is_visible(self): return True
    def is_enabled(self): return True
    def wrapper_object(self): return self
    def descendants(self): return []
    def capture_as_image(self): return Image.new('RGB', (300, 200))
    def click_input(self): self.clicked += 1
    def set_focus(self): self.focused += 1


def broker_for(window, clock=None, foreground=None, available=None, process_identity=None):
    desktop = SimpleNamespace(windows=lambda **kwargs: [window], window=lambda **kwargs: window)
    return ComputerBroker(desktop_factory=lambda: desktop, clock=clock or time.monotonic,
                          foreground=foreground or (lambda: 101), session_check=available or (lambda: True),
                          process_identity=process_identity)


def assert_not_executed(result, message):
    assert result['ok'] is False and result['not_executed'] is True
    assert message in result['error']


def test_computer_actions_require_fresh_same_run_snapshot():
    window = FakeElement()
    now = [0]
    broker = broker_for(window, clock=lambda: now[0])
    try:
        snapshot = broker.execute('computer_inspect', {'window_id': 101}, {'run_id': 'first'})
        args = {'snapshot_id': snapshot['snapshot_id'], 'element_id': 0}
        assert_not_executed(broker.execute('computer_click', args, {'run_id': 'second'}), 'Unknown snapshot')
        assert window.clicked == 0
        broker.execute('computer_click', args, {'run_id': 'first'})
        assert window.clicked == 1
        assert_not_executed(broker.execute('computer_click', args, {'run_id': 'first'}), 'Unknown snapshot')
        snapshot = broker.execute('computer_inspect', {'window_id': 101})
        now[0] = 31
        assert_not_executed(broker.execute('computer_focus', {'snapshot_id': snapshot['snapshot_id']}), 'expired')
        assert window.focused == 0
    finally:
        broker.close()


def test_computer_focus_movement_session_and_password_guards():
    window = FakeElement(password=True)
    active = [101]
    available = [True]
    broker = broker_for(window, foreground=lambda: active[0], available=lambda: available[0])
    try:
        snapshot = broker.execute('computer_inspect', {'window_id': 101})
        args = {'snapshot_id': snapshot['snapshot_id'], 'element_id': 0}
        active[0] = 999
        assert_not_executed(broker.execute('computer_click', args), 'not focused')
        active[0] = 101
        assert_not_executed(broker.execute('computer_click', args), 'Password')
        window.box.left += 1
        assert_not_executed(broker.execute('computer_click', args), 'moved')
        available[0] = False
        assert_not_executed(broker.execute('computer_windows'), 'unavailable')
        assert not window.clicked
    finally:
        broker.close()


def test_computer_pre_effect_validation_and_cancellation():
    window = FakeElement()
    broker = broker_for(window)
    try:
        snapshot = broker.execute('computer_inspect', {'window_id': 101})
        args = {'snapshot_id': snapshot['snapshot_id'], 'element_id': 0}
        assert_not_executed(broker.execute('computer_type', {**args, 'text': 123}), 'Text is limited')
        assert_not_executed(broker.execute('computer_key', {**args, 'key': 'alt+f4'}), 'Unsupported key')
        assert_not_executed(broker.execute('computer_coordinate_click', {**args, 'x': -1, 'y': 0}), 'Coordinates')
        cancel = threading.Event()
        cancel.set()
        assert_not_executed(broker.execute('computer_click', args, {'cancel': cancel}), 'cancelled')
        assert window.clicked == window.focused == 0
    finally:
        broker.close()


def test_computer_post_effect_failure_is_never_labeled_not_executed():
    class AmbiguousWindow(FakeElement):
        def click_input(self):
            self.clicked += 1
            raise TimeoutError('Action may already have been delivered')
    window = AmbiguousWindow()
    active = [101]
    broker = broker_for(window, foreground=lambda: active[0])
    try:
        snapshot = broker.execute('computer_inspect', {'window_id': 101})
        args = {'snapshot_id': snapshot['snapshot_id'], 'element_id': 0}
        with pytest.raises(TimeoutError, match='already have been delivered'):
            broker.execute('computer_click', args)
        assert window.clicked == 1
        def focus_then_lose_target():
            window.focused += 1
            active[0] = 999
        window.set_focus = focus_then_lose_target
        with pytest.raises(ValueError, match='focus changed') as exc:
            broker.execute('computer_type', {**args, 'text': 'hello'})
        assert exc.value.not_executed is False
        assert window.focused == 1
    finally:
        broker.close()


def test_computer_approval_identity_is_read_only_stable_and_run_scoped(tmp_path):
    window = FakeElement()
    now = [0]
    observed = []
    app = tmp_path / 'apps' / 'editor.exe'
    def identify(pid):
        observed.append((pid, threading.current_thread().name))
        return str(app)
    broker = broker_for(window, clock=lambda: now[0], foreground=lambda: 999, process_identity=identify)
    try:
        target = broker.describe_target({'window_id': 101}, {'run_id': 'first'})
        assert target['app'] == os.path.normcase(str(app.resolve()))
        assert target['window_id'] == 101 and 'Disposable test window' in target['target']
        assert observed[0][0] == 55 and observed[0][1].startswith('forge-computer')
        snapshot = broker.execute('computer_inspect', {'window_id': 101}, {'run_id': 'first'})
        args = {'snapshot_id': snapshot['snapshot_id']}
        assert broker.describe_target(args, {'run_id': 'first'}) == target
        calls = len(observed)
        assert_not_executed(broker.describe_target(args, {'run_id': 'other'}), 'Unknown snapshot')
        assert len(observed) == calls
        now[0] = 31
        assert_not_executed(broker.describe_target(args, {'run_id': 'first'}), 'expired')
        assert len(observed) == calls
        assert window.focused == window.clicked == 0
    finally:
        broker.close()


def test_computer_approval_identity_requires_available_session():
    window = FakeElement()
    observed = []
    broker = broker_for(window, available=lambda: False, process_identity=lambda pid: observed.append(pid))
    try:
        assert_not_executed(broker.describe_target({'window_id': 101}), 'unavailable')
        assert observed == [] and window.focused == window.clicked == 0
    finally:
        broker.close()


def wait_dictation(dictation):
    deadline = time.monotonic() + 3
    while dictation.status()['state'] == 'transcribing' and time.monotonic() < deadline:
        time.sleep(.01)
    return dictation.status()


def test_dictation_is_cpu_int8_memory_only_and_cancel_discards(tmp_path):
    seen = {}
    def model_factory(name, **options):
        seen.update(options)
        class Model:
            def transcribe(self, audio, **kwargs):
                assert isinstance(audio, io.BytesIO)
                assert len(audio.read()) > 0
                seen['audio'] = audio
                return iter([SimpleNamespace(text=' Hello Forge')]), SimpleNamespace(language='en')
        return Model()
    dictation = Dictation(tmp_path, model_factory=model_factory)
    dictation.upload(base64.b64encode(b'fake audio').decode())
    assert wait_dictation(dictation)['text'] == 'Hello Forge'
    assert seen['device'] == 'cpu' and seen['compute_type'] == 'int8'
    assert seen['local_files_only'] is True and seen['audio'].closed
    assert not list(tmp_path.rglob('*'))
    dictation._chunks = [bytearray(b'raw audio')]
    reference = dictation._chunks[0]
    dictation.cancel()
    assert not dictation._chunks and not any(reference)


def test_cancelled_dictation_never_delivers_late_transcript(tmp_path):
    proceed = threading.Event()
    def model_factory(*args, **kwargs):
        class Model:
            def transcribe(self, audio, **options):
                proceed.wait(2)
                return iter([SimpleNamespace(text=' late')]), SimpleNamespace(language='en')
        return Model()
    dictation = Dictation(tmp_path, model_factory=model_factory)
    dictation.upload(base64.b64encode(b'fake audio').decode())
    dictation.cancel()
    proceed.set()
    time.sleep(.05)
    status = dictation.status()
    assert status['state'] == 'cancelled' and 'text' not in status and 'id' not in status


def test_dictation_reports_missing_setup_before_recording(tmp_path):
    capture = []
    def missing_model(*args, **kwargs):
        raise FileNotFoundError('Missing cached speech weights')
    dictation = Dictation(tmp_path, model_factory=missing_model, stream_factory=lambda **kwargs: capture.append(kwargs))
    result = dictation.start()
    assert result['state'] == 'error'
    assert 'Settings' in result['error'] and result['setup_action'] == 'dictation_install'
    assert not capture and not dictation._chunks


def test_dictation_cached_model_status_does_not_load_or_record(tmp_path):
    directory = tmp_path / 'runtimes/whisper/models--Systran--faster-whisper-tiny/snapshots/local'
    directory.mkdir(parents=True)
    (directory / 'model.bin').write_bytes(b'fixture weights')
    dictation = Dictation(tmp_path, model='tiny')
    status = dictation.status()
    assert status['model_installed'] and not status['setup_required'] and status['model'] == 'tiny'
    assert status['device'] == 'cpu' and status['compute_type'] == 'int8'
    assert dictation._model is None and dictation._stream is None


def test_dictation_real_codec_decodes_wave_in_memory():
    # Decode through faster-whisper itself: fake model tests miss PyAV API
    # incompatibilities, which otherwise fail every real microphone transcript.
    from faster_whisper.audio import decode_audio
    audio = io.BytesIO()
    with __import__('wave').open(audio, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b'\0\0' * 1600)
    audio.seek(0)
    decoded = decode_audio(audio)
    assert len(decoded) == 1600 and decoded.dtype.name == 'float32'
    assert not decoded.any() and not audio.closed
    audio.close()


def test_dictation_record_stop_transcribes_and_discards_pcm(tmp_path):
    observed = {}
    def model_factory(*args, **options):
        class Model:
            def transcribe(self, audio, **settings):
                with __import__('wave').open(audio, 'rb') as reader:
                    observed['rate'] = reader.getframerate()
                    observed['pcm'] = reader.readframes(reader.getnframes())
                return iter([SimpleNamespace(text=' Fixture transcript')]), SimpleNamespace(language='en')
        return Model()
    class Stream:
        def __init__(self, **settings): self.callback = settings['callback']; self.closed = False
        def start(self): self.callback(b'\1\0' * 1600, 1600, None, None)
        def stop(self): observed['stopped'] = True
        def close(self): self.closed = True; observed['closed'] = True
    dictation = Dictation(tmp_path, model_factory=model_factory, stream_factory=Stream)
    assert dictation.start()['state'] == 'recording'
    chunk = dictation._chunks[0]
    dictation.stop()
    assert wait_dictation(dictation)['text'] == 'Fixture transcript'
    assert observed['rate'] == 16000 and observed['pcm'] == b'\1\0' * 1600
    assert observed['stopped'] and observed['closed'] and not any(chunk)
    assert not dictation._chunks and not list(tmp_path.rglob('*'))


def test_dictation_selection_resets_cached_model_and_preserves_error(tmp_path):
    dictation = Dictation(tmp_path)
    dictation._model = object()
    dictation.select_model('tiny')
    assert dictation.model_name == 'tiny' and dictation._model is None
    dictation._state = {'state': 'error', 'error': 'Microphone permission denied'}
    assert dictation.status()['message'] == 'Microphone permission denied'
    dictation._state = {'state': 'recording'}
    with pytest.raises(ValueError, match='Finish'):
        dictation.select_model('small')
    assert dictation.model_name == 'tiny'


def test_changed_dictation_preference_never_blocks_active_stop_or_cancel(tmp_path):
    from desktop import Bridge
    settings = {'dictation_model': 'base'}
    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text=' Active model transcript')]), SimpleNamespace(language='en')
    class Stream:
        def __init__(self, **kwargs): self.callback = kwargs['callback']
        def start(self): self.callback(b'\0\0' * 1600, 1600, None, None)
        def stop(self): pass
        def close(self): pass
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model(), stream_factory=Stream)
    service = SimpleNamespace(host_dictation=dictation, store=SimpleNamespace(get_settings=lambda: settings))
    bridge = Bridge(service)
    bridge._ensure_service = lambda: service
    assert bridge.call('dictation_start')['state'] == 'recording'
    settings['dictation_model'] = 'tiny'
    assert bridge.call('dictation_status')['state'] == 'recording'
    assert bridge.call('dictation_stop').get('state') in ('transcribing', 'done')
    assert wait_dictation(dictation)['text'] == 'Active model transcript'
    assert dictation.model_name == 'base'
    assert bridge.call('dictation_status')['text'] == 'Active model transcript'
    assert bridge.call('dictation_start')['model'] == 'tiny'
    settings['dictation_model'] = 'small'
    assert bridge.call('dictation_cancel')['state'] == 'cancelled'


def test_capture_start_callback_and_failure_cleanup_do_not_deadlock(tmp_path):
    completed = threading.Event()
    capture = {}
    class Stream:
        def __init__(self, **kwargs): self.callback = kwargs['callback']
        def start(self):
            def invoke():
                self.callback(b'\1\0' * 160, 160, None, None)
                completed.set()
            self.thread = threading.Thread(target=invoke)
            self.thread.start()
            if not completed.wait(1):
                raise AssertionError('Callback state lock blocked during capture start')
            raise RuntimeError('Disposable microphone start failure')
        def stop(self): self.thread.join(1); capture['stopped'] = True
        def close(self): capture['closed'] = True
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: object(), stream_factory=Stream)
    result = dictation.start()
    assert completed.is_set() and result['state'] == 'error'
    assert 'Disposable microphone start failure' in result['error']
    assert capture == {'stopped': True, 'closed': True} and not dictation._chunks


def test_cancel_during_capture_creation_prevents_late_microphone_start(tmp_path):
    entered, proceed = threading.Event(), threading.Event()
    seen = []
    class Stream:
        def __init__(self, **kwargs): entered.set(); proceed.wait(2)
        def start(self): seen.append('start')
        def stop(self): seen.append('stop')
        def close(self): seen.append('close')
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: object(), stream_factory=Stream)
    worker = threading.Thread(target=dictation.start)
    worker.start()
    assert entered.wait(1)
    cancel = threading.Thread(target=dictation.cancel)
    cancel.start()
    deadline = time.monotonic() + 1
    while dictation.status()['state'] != 'cancelled' and time.monotonic() < deadline: time.sleep(.005)
    proceed.set()
    worker.join(2); cancel.join(2)
    assert not worker.is_alive() and not cancel.is_alive()
    assert dictation.status()['state'] == 'cancelled' and seen == ['close']
    assert dictation._stream is None and not dictation._chunks


def test_cancel_between_capture_stop_and_transcription_cannot_restart_job(tmp_path):
    observed = []
    class Model:
        def transcribe(self, *args, **kwargs): observed.append('transcribed'); return [], None
    class Stream:
        def __init__(self, **kwargs): self.callback = kwargs['callback']
        def start(self): self.callback(b'\0\0' * 160, 160, None, None)
        def stop(self): pass
        def close(self): pass
    dictation = Dictation(tmp_path, model_factory=lambda *args, **kwargs: Model(), stream_factory=Stream)
    transcribe = dictation._transcribe
    def cancel_then_transcribe(audio, language=None, expected_generation=None):
        dictation.cancel()
        return transcribe(audio, language, expected_generation)
    dictation._transcribe = cancel_then_transcribe
    assert dictation.start()['state'] == 'recording'
    assert dictation.stop()['state'] == 'cancelled'
    assert observed == [] and not dictation._chunks
