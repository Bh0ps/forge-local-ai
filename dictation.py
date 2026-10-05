"""Push-to-talk capture and local CPU INT8 transcription. Audio is memory-only."""
from __future__ import annotations

import base64
import io
from pathlib import Path
import threading
import uuid
import wave


class Dictation:
    def __init__(self, data_dir, model='base', model_factory=None, stream_factory=None):
        self.model_dir = Path(data_dir) / 'runtimes' / 'whisper'
        self.model_name = model
        self.model_factory = model_factory
        self.stream_factory = stream_factory
        self._lock = threading.RLock()
        self._chunks = []
        self._stream = None
        self._model = None
        self._generation = 0
        self._job = None
        self._state = {'state': 'idle'}
        self.max_seconds = 120
        self.sample_rate = 16000

    def status(self):
        with self._lock:
            return dict(self._state)

    def install(self, model=None):
        model = model or self.model_name
        if model not in ('tiny', 'base', 'small'):
            raise ValueError('Choose tiny, base or small for CPU dictation.')
        with self._lock:
            if self._state['state'] in ('recording', 'transcribing', 'installing'):
                raise ValueError('Finish the current dictation operation first.')
            self._state = {'state': 'installing', 'model': model}
        def worker():
            try:
                from faster_whisper.utils import download_model
                self.model_dir.mkdir(parents=True, exist_ok=True)
                download_model(model, cache_dir=str(self.model_dir))
                with self._lock:
                    self.model_name = model
                    self._model = None
                    self._state = {'state': 'ready', 'model': model}
            except Exception as exc:
                with self._lock:
                    self._state = {'state': 'error', 'error': str(exc)[:500]}
        threading.Thread(target=worker, name='forge-dictation-install', daemon=True).start()
        return self.status()

    def _load_model(self):
        if self._model is None:
            try:
                if self.model_factory:
                    factory = self.model_factory
                else:
                    from faster_whisper import WhisperModel
                    factory = WhisperModel
                self._model = factory(self.model_name, device='cpu', compute_type='int8',
                                      cpu_threads=4, download_root=str(self.model_dir), local_files_only=True)
            except ImportError:
                raise ValueError('Install requirements-host.txt to enable local dictation.') from None
            except Exception:
                raise ValueError('Dictation model is not installed. Use Settings → Dictation → Install model.') from None
        return self._model

    def start(self):
        with self._lock:
            if self._state['state'] in ('recording', 'transcribing', 'installing'):
                raise ValueError('Dictation is already active.')
            # Fail before recording a user's speech if the local model is absent.
            # This is the first microphone action, never app startup inference.
            self._load_model()
            self._clear_audio()
            self._generation += 1
            count = 0
            def callback(indata, frames, callback_time, status):
                nonlocal count
                with self._lock:
                    if self._state.get('state') != 'recording':
                        return
                    remaining = self.max_seconds * self.sample_rate - count
                    if remaining > 0:
                        self._chunks.append(bytearray(bytes(indata)[:remaining * 2]))
                        count += frames
                    self._state['seconds'] = min(count / self.sample_rate, self.max_seconds)
                    if count >= self.max_seconds * self.sample_rate:
                        self._state['limit_reached'] = True
            try:
                factory = self.stream_factory
                if factory is None:
                    import sounddevice
                    factory = sounddevice.RawInputStream
                self._stream = factory(samplerate=self.sample_rate, channels=1, dtype='int16', callback=callback)
                self._state = {'state': 'recording', 'seconds': 0, 'max_seconds': self.max_seconds}
                self._stream.start()
                return self.status()
            except ImportError:
                self._state = {'state': 'error', 'error': 'Install requirements-host.txt to enable microphone capture.'}
                return self.status()
            except Exception as exc:
                self._stop_capture()
                self._clear_audio()
                self._state = {'state': 'error', 'error': 'Microphone unavailable: ' + str(exc)[:400]}
                return self.status()

    def _stop_capture(self):
        stream, self._stream = self._stream, None
        if stream:
            try:
                stream.stop()
            finally:
                stream.close()

    def _clear_audio(self):
        for chunk in self._chunks:
            chunk[:] = b'\0' * len(chunk)
        self._chunks.clear()

    def stop(self, language=None):
        # Stop outside the callback lock; PortAudio joins its callback thread.
        with self._lock:
            if self._state['state'] != 'recording':
                raise ValueError('Dictation is not recording.')
            self._state['state'] = 'stopping'
        self._stop_capture()
        with self._lock:
            audio = io.BytesIO()
            with wave.open(audio, 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(self.sample_rate)
                for chunk in self._chunks:
                    writer.writeframesraw(chunk)
            self._clear_audio()
            audio.seek(0)
        return self._transcribe(audio, language)

    def upload(self, encoded, language=None):
        """Browser MediaRecorder bytes are decoded in memory, never as a file."""
        if not isinstance(encoded, str) or len(encoded) > 8_000_000:
            raise ValueError('Audio is limited to 6 MB.')
        try:
            audio = io.BytesIO(base64.b64decode(encoded, validate=True))
        except ValueError:
            raise ValueError('Invalid audio payload.') from None
        return self._transcribe(audio, language)

    def _transcribe(self, audio, language=None):
        with self._lock:
            if self._state['state'] in ('transcribing', 'installing', 'recording'):
                audio.close()
                raise ValueError('Dictation is already active.')
            self._generation += 1
            generation = self._generation
            self._job = uuid.uuid4().hex
            self._state = {'state': 'transcribing', 'id': self._job}
        def worker():
            result = {}
            try:
                model = self._load_model()
                segments, info = model.transcribe(audio, language=language or None,
                                                  beam_size=1, vad_filter=True)
                parts = []
                for segment in segments:
                    with self._lock:
                        if self._generation != generation:
                            return
                    parts.append(segment.text)
                result = {'state': 'done', 'text': ''.join(parts).strip(), 'language': info.language,
                          'id': self._job, 'device': 'cpu', 'compute_type': 'int8'}
            except Exception as exc:
                result = {'state': 'error', 'error': str(exc)[:500], 'id': self._job}
            finally:
                # BytesIO's own allocation is cleared before close. Python and
                # the decoder may have transient copies; no audio is persisted.
                try:
                    view = audio.getbuffer()
                    view[:] = b'\0' * len(view)
                    del view
                finally:
                    audio.close()
                with self._lock:
                    if self._generation == generation:
                        self._state = result
        threading.Thread(target=worker, name='forge-dictation', daemon=True).start()
        return self.status()

    def cancel(self):
        with self._lock:
            self._generation += 1
            self._state = {'state': 'cancelled'}
        self._stop_capture()
        with self._lock:
            self._clear_audio()
        return self.status()

    def dispatch(self, action, data=None):
        data = data or {}
        if action == 'start':
            return self.start()
        if action == 'stop':
            return self.stop(data.get('language'))
        if action == 'cancel':
            return self.cancel()
        if action == 'status':
            return self.status()
        if action == 'install':
            return self.install(data.get('model'))
        if action == 'upload':
            return self.upload(data.get('audio'), data.get('language'))
        raise ValueError('Unknown dictation action.')

    shutdown = cancel
