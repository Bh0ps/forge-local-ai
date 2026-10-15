"""Owned local inference workers. Forge remains the only agent/tool coordinator."""
from __future__ import annotations

import codecs
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx

from forge_native_models import NativeCatalog, file_hash, read_json


class NativeFailure(ValueError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category

    def diagnostics(self):
        return {'provider_id': 'forge-native', 'category': self.category}


class NativeRuntime:
    def __init__(self, home, *, bundle=None, busy=None, cpu_only=False):
        self.home = Path(home)
        self.bundle = Path(bundle or Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'native-runtimes')
        self.catalog = NativeCatalog(home)
        self.busy = busy or (lambda: False)
        self.lock = threading.RLock()
        self.worker = None
        self.state = 'stopped'
        self.current = None
        self.closed = False
        self.cpu_only = cpu_only
        self.generation=0
        self.idle_timer=None

    def status(self):
        with self.lock:
            return {'state': self.state, 'model': self.current,
                    'bundled': (self.bundle / 'manifest.json').is_file(),
                    'catalog': self.catalog.public()}

    def _executable(self, engine):
        manifest = read_json(self.bundle / 'manifest.json')
        name = 'llama-server.exe' if engine == 'llama-cpp' else 'strata.exe'
        candidates = []
        for relative, info in manifest['files'].items():
            path = self.bundle / relative
            if not relative.startswith((engine + '/', 'shared/')):
                continue
            if not path.resolve().is_relative_to(self.bundle.resolve()) or not path.is_file():
                raise NativeFailure('integrity', 'A bundled runtime file is missing or outside its directory. Repair Forge.')
            if path.stat().st_size != info['size'] or file_hash(path) != info['sha256']:
                raise NativeFailure('integrity', 'A bundled runtime failed verification. Repair Forge before loading a model.')
            if path.name == name:
                candidates.append(path)
        if len(candidates) != 1:
            raise NativeFailure('installation', 'This Forge installation is missing its bundled model runtime.')
        return candidates[0]

    def _spawn(self, executable, arguments, *, stdin=False):
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(('LLAMA_','GGML_','STRATA_','CUDA_')):
                environment.pop(key)
        directories = sorted({str(p.parent) for p in executable.parent.rglob('*.dll')})
        if (self.bundle/'shared').is_dir(): directories.append(str(self.bundle/'shared'))
        system = Path(environment.get('SystemRoot','C:/Windows'))
        environment['PATH'] = os.pathsep.join([str(executable.parent), *directories, str(system/'System32'), str(system)])
        process = subprocess.Popen([str(executable), *arguments], cwd=str(executable.parent), env=environment,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=0x08000000 if os.name == 'nt' else 0, start_new_session=os.name != 'nt')
        from project_tools import _WindowsProcessJob
        job = _WindowsProcessJob(process)
        if os.name == 'nt' and job.handle is None:
            process.terminate()
            process.wait(timeout=10)
            raise NativeFailure('lifecycle', 'Windows could not attach the native model worker to Forge. Restart Forge before retrying.')
        lines = queue.Queue(maxsize=1024)
        def drain():
            try:
                while True:
                    line = process.stdout.readline(8193)
                    if not line:
                        break
                    if len(line) > 8192:
                        process.terminate()
                        break
                    # No prompts, generated text, keys or raw engine logs are persisted.
                    if stdin:
                        while process.poll() is None:
                            try:
                                lines.put(line, timeout=.1)
                                break
                            except queue.Full:
                                continue
            finally:
                process.stdout.close()
                try: lines.put_nowait(None)
                except queue.Full: pass
        threading.Thread(target=drain, daemon=True, name='Forge-native-output').start()
        return {'process': process, 'job': job, 'lines': lines}

    def _line(self, worker, cancel, deadline):
        while True:
            if cancel.is_set() or self.closed:
                raise NativeFailure('cancelled', 'Native inference was cancelled. Completed tool actions remain saved.')
            if time.monotonic() >= deadline:
                raise NativeFailure('timeout', 'The native model stopped making progress. Resume after checking memory and model settings.')
            try:
                line = worker['lines'].get(timeout=.1)
            except queue.Empty:
                if worker['process'].poll() is not None:
                    raise NativeFailure('worker_exited', 'The native model worker exited. Check available memory, then resume.')
                continue
            if line is None:
                raise NativeFailure('worker_exited', 'The native model worker exited. Check available memory, then resume.')
            return line.strip()

    def stop(self):
        with self.lock:
            self.generation+=1
            if self.idle_timer: self.idle_timer.cancel()
            self.idle_timer=None
            worker, self.worker = self.worker, None
            self.current = None
            self.state = 'stopped'
        self._dispose(worker)

    @staticmethod
    def _dispose(worker):
        if worker:
            process = worker['process']
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            worker['job'].close()
            for stream in (process.stdin,):
                if stream:
                    try: stream.close()
                    except OSError: pass
            key_path = worker.get('key_path')
            if key_path: key_path.unlink(missing_ok=True)

    def close(self):
        self.closed = True
        self.stop()

    def _start(self, model, context, cancel, threads=None):
        if self.closed:
            raise NativeFailure('cancelled', 'Forge is shutting down.')
        if self.busy():
            raise NativeFailure('resource_busy', 'Existing work is using another local engine. Let it finish before starting a Forge model.')
        if threads is not None and (type(threads) is not int or not 1<=threads<=64):
            raise NativeFailure('configuration','CPU threads must be from 1 to 64.')
        binding = (model['id'], context, model['engine'],threads)
        with self.lock:
            self.generation+=1
            if self.idle_timer: self.idle_timer.cancel()
            self.idle_timer=None
            if self.worker and self.worker['process'].poll() is None and self.current == binding:
                return self.worker
        self.stop()
        with self.lock:
            launch_generation=self.generation
            self.state = 'verifying'
        def current():
            if self.closed or cancel.is_set() or launch_generation!=self.generation:
                raise NativeFailure('cancelled','Model loading was cancelled.')
        def adopt(worker):
            try:
                with self.lock:
                    current()
                    self.worker=worker
            except BaseException:
                self._dispose(worker)
                raise
        exe = self._executable(model['engine'])
        for entry in model['files'] + model.get('auxiliary', []):
            current()
            if file_hash(self.catalog.root / entry['path']) != entry['sha256']:
                raise NativeFailure('integrity', 'A model file failed verification. Import it again before use.')
        path = self.catalog.root / model['files'][0]['path']
        if context > model['context']:
            raise NativeFailure('context', 'The selected context exceeds the model’s advertised limit. Reduce Context.')
        with self.lock:
            current()
            self.state = 'loading'
        if model['engine'] == 'llama-cpp':
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            key = secrets.token_urlsafe(48)
            directory = self.home / 'state' / 'native-workers'
            directory.mkdir(parents=True, exist_ok=True)
            key_path = directory / (secrets.token_hex(16) + '.key')
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream: stream.write(key)
            import psutil
            arguments = ['--model', str(path), '--alias', model['id'], '--host', '127.0.0.1', '--port', str(port),
                '--api-key-file', str(key_path), '--ctx-size', str(context), '--parallel', '1', '--jinja',
                '--n-gpu-layers', 'auto', '--flash-attn', 'auto', '--threads', str(threads or min(64, psutil.cpu_count(logical=False) or 4)),
                '--no-webui', '--no-context-shift', '--log-disable']
            if self.cpu_only:
                arguments += ['--device', 'none', '--n-gpu-layers', '0']
            projector = model.get('projector')
            if projector:
                arguments += ['--mmproj', str(self.catalog.root / projector)]
            current()
            try: worker = self._spawn(exe, arguments)
            except BaseException:
                key_path.unlink(missing_ok=True)
                raise
            worker.update(base=f'http://127.0.0.1:{port}/v1', key=key, key_path=key_path)
            adopt(worker)
            deadline = time.monotonic() + 600
            with httpx.Client(timeout=1, trust_env=False) as client:
                while time.monotonic() < deadline:
                    current()
                    if worker['process'].poll() is not None:
                        raise NativeFailure('worker_exited', 'The bundled model worker could not load this GGUF. Check model compatibility and memory.')
                    try:
                        response = client.get(worker['base'] + '/models', headers={'Authorization': 'Bearer ' + key})
                        if response.status_code == 200:
                            entries = response.json().get('data', [])
                            if any(m.get('id') == model['id'] for m in entries): break
                    except (httpx.HTTPError, ValueError): pass
                    cancel.wait(.15)
                else:
                    raise NativeFailure('timeout', 'Model loading exceeded ten minutes. Check memory and model compatibility.')
        else:
            import psutil
            from forge_inference import memory_telemetry
            from forge_native_memory import strata_memory_plan
            facts=memory_telemetry()
            if facts.get('stale') or facts.get('probe_state') in ('failed','unavailable','refreshing'):
                raise NativeFailure('memory','GPU memory could not be measured. Retry Strata after the hardware check finishes.')
            free=max([max(0,g.get('total_mb',0)-g.get('used_mb',0))*1024**2 for g in facts.get('gpus',[])] or [0])
            try:plan=strata_memory_plan(psutil.virtual_memory().available,free,context)
            except ValueError:
                raise NativeFailure('memory','Strata Coder needs more free RAM or GPU memory. Finish other model work, then retry.') from None
            from forge_vendor.strata.iq_pack import main as prepare
            from forge_vendor.strata.tokenizer import Tokenizer
            from forge_vendor.strata.frontend import ChatTemplate
            pack = path.parent / 'pack'
            if not (pack / 'native_experts.txt').is_file():
                if prepare(['--gguf', str(path), '--out', str(pack)], cancel=cancel) != 0:
                    raise NativeFailure('model', 'Strata model preparation failed. The original model files were retained.')
            if plan['mode']!='arena' and not (pack/'experts.bin').is_file():
                if shutil.disk_usage(pack).free<plan['prepared_expert_bytes']+4*1024**3:
                    raise NativeFailure('storage','Strata memory-saving preparation needs another 28 GB of free space in the model directory.')
                current()
                if prepare(['--gguf',str(path),'--out',str(pack),'--experts-bin'], cancel=cancel)!=0:
                    raise NativeFailure('model','Strata memory-saving preparation failed. Model originals were retained.')
            arguments = ['--serve', '--pack', str(pack), '--native', str(path),
                '--ple-gguf', str(self.catalog.root / model['files'][1]['path']),
                '--expert-profile', str(Path(__file__).parent / 'forge_vendor' / 'strata' / 'expert-profile-coder.bin'),
                '--expert-cache', 'auto', '--prefill', 'auto', '--spec', '0', '--max-context', str(context)]
            if plan['mode']!='arena':
                arguments+=['--resident-experts' if plan['mode']=='resident' else '--mmap-experts']
            if context>8192: arguments+=['--kv','int8']
            if threads is not None:
                arguments += ['--pool-workers',str(max(1,threads-1))]
                if threads==1: arguments += ['--no-host-worker']
            current()
            worker = self._spawn(exe, arguments, stdin=True)
            adopt(worker)
            worker.update(tokenizer=Tokenizer.from_gguf(path), template=ChatTemplate(pack / 'tokenizer' / 'chat_template.jinja'))
            deadline = time.monotonic() + 600
            while True:
                current()
                line = self._line(worker, cancel, deadline)
                if line.startswith('READY '):
                    fields = line.split()
                    if int(fields[1]) != context or 'stop' not in fields[2:]:
                        raise NativeFailure('protocol', 'The bundled Strata worker reported incompatible context or cancellation support.')
                    break
        with self.lock:
            current()
            self.current = binding
            self.state = 'ready'
        return worker

    def generate(self, data, cancel):
        from core import Core
        Core._agent_payload(data)
        model = self.catalog.model(data['model'])
        defaults = model.get('origin', {}).get('defaults', {})
        if defaults.get('system'):
            data = {**data, 'messages': [{'role': 'system', 'content': defaults['system']}, *data['messages']]}
        try:
            worker = self._start(model, data.get('context', 8192), cancel,data.get('num_thread'))
            with self.lock: self.state = 'generating'
            if model['engine'] == 'llama-cpp':
                from forge_inference import CompatibleProvider
                class OwnedProvider(CompatibleProvider):
                    def _headers(inner, url=None, cancel=None):
                        if worker['process'].poll() is not None:
                            raise NativeFailure('worker_exited', 'The native model worker exited. Resume to reload it.')
                        return {'Authorization': 'Bearer ' + worker['key']}
                    def prepare_body(inner, request, body):
                        for message in body['messages']:
                            if 'thinking' in message:
                                message['reasoning_content']=message.pop('thinking')
                        if model.get('reasoning_control'):
                            body['chat_template_kwargs'] = {'enable_thinking': bool(request.get('thinking', False))}
                        stops = defaults.get('params', {}).get('stop')
                        if isinstance(stops, list) and len(stops) <= 32 and all(isinstance(s, str) and len(s)<=200 for s in stops):
                            body['stop'] = stops
                        return body
                provider = OwnedProvider({'url': worker['base'], 'kind': 'openai-compatible'})
                yield from provider.generate(data, cancel)
            else:
                yield from self._strata_generate(worker, data, cancel)
        except BaseException:
            self.stop()
            raise
        finally:
            if cancel.is_set(): self.stop()
            with self.lock:
                if self.worker: self.state = 'ready'
            self._retain(data.get('keep_alive','10m'))

    def _retain(self, value):
        if value==0:
            self.stop()
            return
        if value in (-1,'-1'): return
        match=re.fullmatch(r'(\d{1,5})([smh]?)',str(value))
        seconds=min(86400,int(match[1])*{'':1,'s':1,'m':60,'h':3600}[match[2]]) if match else 600
        with self.lock:
            if not self.worker: return
            generation=self.generation
            def expire():
                with self.lock:
                    if generation==self.generation and self.state=='ready': self.stop()
            self.idle_timer=threading.Timer(seconds,expire)
            self.idle_timer.daemon=True
            self.idle_timer.start()

    def warm(self,settings,cancel):
        model=self.catalog.model(settings['model'])
        try:self._start(model,settings['context'],cancel,settings.get('num_thread'))
        except BaseException:
            self.stop()
            raise
        self._retain(settings.get('keep_alive','10m'))

    def _strata_generate(self, worker, data, cancel):
        from core import Core, MAX_STREAM_CHARACTERS
        from forge_vendor.strata.frontend import openai_to_messages, OutputParser
        payload = Core._agent_payload(data)
        for message in payload['messages']:
            if 'thinking' in message:
                message['reasoning_content']=message.pop('thinking')
        if any(m.get('images') for m in payload['messages']) or data.get('response_format'):
            raise NativeFailure('capability', 'This Strata adapter supports text and tools. Select a validated vision or structured-output model for this request.')
        messages, tools, kwargs = openai_to_messages({'messages': payload['messages'], 'tools': payload.get('tools', [])})
        kwargs['enable_thinking'] = bool(data.get('thinking', False))
        prompt = worker['template'].render(messages, tools, **kwargs)
        tokenizer = worker['tokenizer']
        ids = tokenizer.encode(prompt, parse_special=True)
        limit = payload['options']['num_predict']
        if len(ids) + limit > data.get('context', 8192):
            raise NativeFailure('context', 'The tokenized request exceeds the selected context. Compact the conversation or increase Context.')
        parser = OutputParser(thinking=bool(data.get('thinking', False)), tools=tools)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        worker['process'].stdin.write(f'GEN {limit} temperature={float(data.get("temperature", .3))} ' + ','.join(map(str, ids)) + '\n')
        worker['process'].stdin.flush()
        characters = 0
        def emit(events):
            nonlocal characters
            for event in events:
                characters += len(event.text)
                if characters > MAX_STREAM_CHARACTERS:
                    raise NativeFailure('protocol', 'Native output exceeded the request limit.')
                if event.kind == 'tool_call':
                    yield {'message': {'tool_calls': [{'id': event.call.id, 'type': 'function',
                        'function': {'name': event.call.name, 'arguments': event.call.arguments}}]}}
                elif event.kind == 'reasoning': yield {'message': {'thinking': event.text}}
                elif event.kind == 'content': yield {'message': {'content': event.text}}
        deadline = time.monotonic() + 3600
        while True:
            line = self._line(worker, cancel, deadline)
            if line.startswith('T '):
                deadline = time.monotonic() + 180
                token = int(line.split()[1])
                if not 0 <= token < len(tokenizer.tokens):
                    raise NativeFailure('protocol', 'Strata returned an invalid token identity.')
                yield from emit(parser.feed(decoder.decode(tokenizer.token_bytes(token))))
            elif line.startswith('DONE '):
                fields = line.split()
                if len(fields)<6 or fields[5] not in ('stop','length'):
                    raise NativeFailure('protocol', 'Strata did not complete the request. Partial output remains saved; no tool call was executed.')
                yield from emit(parser.feed(decoder.decode(b'', final=True)))
                yield from emit(parser.finish())
                yield {'done': True, 'prompt_eval_count': int(fields[2]), 'eval_count': int(fields[1]),
                       'prompt_eval_duration': int(float(fields[3]) * 1e6), 'eval_duration': int(float(fields[4]) * 1e6),
                       'provider_finish_reason': 'length' if fields[5] == 'length' else 'stop',
                       'done_reason': 'length' if fields[5] == 'length' else 'stop',
                       'message': {}, 'provider_model': data['model'], 'response_provider': 'Strata'}
                break
            elif line.startswith('ERR'):
                raise NativeFailure('protocol', 'Strata refused this request. Check context and model settings before resuming.')


class NativeProvider:
    def __init__(self, runtime):
        self.runtime = runtime

    def models(self):
        return [{'name': m['id'], 'model': m['id'], 'display_name': m['name'],
                 'digest':hashlib.sha256(json.dumps([f['sha256'] for f in m['files']+m.get('auxiliary',[]) ]).encode()).hexdigest(),
                 'size': sum(f['size'] for f in m['files']), 'capabilities': m['capabilities']}
                for m in self.runtime.catalog.public()['models']]

    def capabilities(self, model):
        item = self.runtime.catalog.model(model)
        control=item['engine']=='strata' or item.get('reasoning_control',False)
        return {'capabilities': item['capabilities'], 'model_info': {'local.context_length': item['context']},
                'context_limit_source': 'gguf', 'reasoning': {'control_supported': control},
                **({'thinking':{'values':[False,True],'default':False}} if control else {})}

    def estimate_tokens(self, messages, tools):
        from context_window import estimated_prompt_tokens
        from core import AGENT_SYSTEM_PROMPT
        return estimated_prompt_tokens(messages, tools, AGENT_SYSTEM_PROMPT)

    def generate(self, data, cancel):
        yield from self.runtime.generate(data, cancel)
