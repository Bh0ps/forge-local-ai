"""Explicitly managed engines, verified installs and measured profile promotion.

External engines are never reconfigured or stopped. Downloaded executables are
not run until the user selects Start. Benchmarks run only on owned processes.
"""
from __future__ import annotations

import base64
import ast
from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

import httpx

from forge_inference import ENGINE_PROFILES, memory_telemetry, InferenceQueue
from inference_stream import cancellable_inference, bounded_lines
from core import Core
import statistics
from tool_calls import ToolCallAccumulator


class RuntimeManager:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.directory = self.home / 'runtimes'
        self.config_path = self.directory / 'managed.json'
        self._lock = threading.RLock()
        self._processes = {}
        self._logs = {}
        self._validation = {'state': 'idle'}
        self._cancel = threading.Event()
        self.configs = {}
        self.inference_queue = InferenceQueue()
        self.usage_store = None
        if self.config_path.is_file():
            try:
                self.configs = json.loads(self.config_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                raise ValueError('Managed runtime configuration is damaged. Restore its backup.') from None

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.configs, indent=2), encoding='utf-8')
        os.replace(temporary, self.config_path)

    def _config(self, identifier):
        if identifier not in self.configs:
            raise ValueError('Choose a registered managed runtime.')
        return self.configs[identifier]

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _version(executable):
        flags = 0x08000000 if os.name == 'nt' else 0
        result = subprocess.run([str(executable), '--version'], capture_output=True, text=True,
                                timeout=15, creationflags=flags)
        return (result.stdout + result.stderr).strip()[:2000]

    def install(self, data, cancel=None):
        engine = data.get('engine')
        profiles = [p for p in ENGINE_PROFILES if p['engine'] == engine and p.get('version')]
        if not profiles:
            raise ValueError('Managed desktop engines are Ollama and llama.cpp. Use Docker profiles for vLLM/SGLang.')
        expected_version = str(data.get('version') or profiles[0]['version'])
        if expected_version != str(profiles[0]['version']):
            raise ValueError('Choose the pinned runtime version ' + str(profiles[0]['version']) + '.')
        identifier = str(data.get('id') or uuid4().hex)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
            raise ValueError('Invalid runtime identifier.')
        executable = data.get('executable')
        downloaded = False
        if executable:
            executable = Path(executable).expanduser().resolve(strict=True)
            if not executable.is_file():
                raise ValueError('Select a runtime executable file.')
            report = self._version(executable)
            expected_number = expected_version.lstrip('b')
            if not re.search(r'(?<![0-9])' + re.escape(expected_number) + r'(?![0-9])', report):
                raise ValueError('Executable version does not match the pinned version: ' + report[:300])
        else:
            url, digest = data.get('url', ''), data.get('sha256', '')
            parsed = urlsplit(url)
            repository = 'ollama/ollama' if engine == 'ollama' else 'ggml-org/llama.cpp'
            if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password or
                not parsed.path.startswith('/' + repository + '/releases/download/' + ('v' if engine == 'ollama' else '') + expected_version + '/')):
                raise ValueError('Use an official GitHub release asset URL for the pinned version.')
            if not re.fullmatch('[0-9a-fA-F]{64}', str(digest)):
                raise ValueError('An official SHA256 checksum is required before downloading a runtime.')
            target = self.directory / identifier
            if target.exists():
                raise ValueError('That runtime directory already exists. Choose another identifier.')
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='forge-runtime-', dir=self.directory) as stage:
                archive = Path(stage) / 'download.zip'
                count = 0
                with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                    current_url=url
                    for hop in range(5):
                        parsed_download=urlsplit(current_url)
                        if (parsed_download.scheme!='https' or parsed_download.hostname not in
                            {'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com','github-releases.githubusercontent.com'}
                            or parsed_download.username or parsed_download.password):
                            raise ValueError('Runtime download redirected outside the official release hosts.')
                        if cancel is not None and cancel.is_set(): raise ValueError('Runtime installation cancelled.')
                        with client.stream('GET',current_url) as response:
                            if response.status_code in (301,302,303,307,308):
                                from urllib.parse import urljoin
                                current_url=urljoin(current_url,response.headers.get('location',''))
                                continue
                            response.raise_for_status()
                            with open(archive,'wb') as output:
                                for chunk in response.iter_bytes(1024*1024):
                                    if cancel is not None and cancel.is_set(): raise ValueError('Runtime installation cancelled.')
                                    count+=len(chunk)
                                    if count>4*1024**3: raise ValueError('Runtime download exceeds 4 GB.')
                                    output.write(chunk)
                            break
                    else: raise ValueError('Too many runtime download redirects.')
                if self._hash(archive) != str(digest).lower():
                    raise ValueError('Runtime checksum does not match. Nothing was installed.')
                extracted = Path(stage) / 'files'
                extracted.mkdir()
                with zipfile.ZipFile(archive) as package:
                    total = 0; names=set()
                    from forge_updates import safe_relative
                    for item in package.infolist():
                        try: relative=safe_relative(item.filename.rstrip('/'))
                        except ValueError: raise ValueError('Runtime archive contains unsafe paths or unsupported links.') from None
                        key=str(relative).casefold()
                        if key in names: raise ValueError('Runtime archive has colliding paths.')
                        names.add(key)
                        total += item.file_size
                        destination = (extracted / item.filename).resolve()
                        if (not destination.is_relative_to(extracted) or (item.external_attr >> 16) & 0o170000 == 0o120000 or
                            total > 8 * 1024**3):
                            raise ValueError('Runtime archive contains unsafe paths or unsupported links.')
                    for item in package.infolist():
                        if cancel is not None and cancel.is_set(): raise ValueError('Runtime installation cancelled.')
                        destination=extracted/safe_relative(item.filename.rstrip('/'))
                        if item.is_dir(): destination.mkdir(parents=True,exist_ok=True); continue
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        with package.open(item) as source,destination.open('xb') as output:
                            while True:
                                if cancel is not None and cancel.is_set(): raise ValueError('Runtime installation cancelled.')
                                chunk=source.read(1024*1024)
                                if not chunk: break
                                output.write(chunk)
                if cancel is not None and cancel.is_set(): raise ValueError('Runtime installation cancelled.')
                name = ('ollama' if engine == 'ollama' else 'llama-server') + ('.exe' if os.name == 'nt' else '')
                candidates = list(extracted.rglob(name))
                if len(candidates) != 1:
                    raise ValueError('Archive must contain exactly one ' + name + '.')
                relative = candidates[0].relative_to(extracted)
                shutil.move(str(extracted), str(target))
                executable = target / relative
                if os.name != 'nt':
                    executable.chmod(executable.stat().st_mode | 0o100)
                downloaded = True
            report = 'Not executed yet; verified official archive ' + str(digest).lower()
        config = {'id': identifier, 'engine': engine, 'version': expected_version,
                  'executable': str(executable), 'executable_sha256': self._hash(executable),
                  'version_report': report, 'downloaded': downloaded, 'validation': None,
                  'model_directory': str(data.get('model_directory') or self.home / 'models' / 'ollama')}
        with self._lock:
            if identifier in self.configs:
                raise ValueError('That runtime is already registered.')
            self.configs[identifier] = config
            self._save()
        return config

    @staticmethod
    def _available_port(preferred):
        if type(preferred) is not int or not 1024 <= preferred <= 65535:
            raise ValueError('Choose a port from 1024 to 65535.')
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', preferred))
            except OSError:
                raise ValueError('Port is already in use. Forge will not replace an external engine.') from None
        return preferred

    def advanced_capabilities(self,identifier):
        config=self._config(identifier)
        if config['engine']!='llama.cpp':
            return {'spec_types':['none'],'cuda_graph':False,'fp4':False,
                    'reason':'This adapter exposes measured cache profiles. Native speculation requires a supporting llama.cpp build.'}
        result=subprocess.run([config['executable'],'--help'],capture_output=True,text=True,timeout=15,
                              creationflags=0x08000000 if os.name=='nt' else 0)
        help_text=result.stdout+result.stderr
        methods=['none']+[m for m in ('draft-simple','draft-eagle3','draft-mtp','draft-dflash','draft-dspark',
                 'ngram-simple','ngram-map-k','ngram-map-k4v','ngram-mod','ngram-cache') if m in help_text]
        gpus=memory_telemetry()['gpus']
        return {'spec_types':methods,'cuda_graph':bool(gpus),'fp4':bool(gpus),'validation_required':True}

    def _advanced_arguments(self,config,data):
        advanced=data.get('advanced') or {}
        if not advanced: return [],{}
        if set(advanced)-{'spec_type','draft_model_path','draft_tokens','cuda_graph_opt','fp4_checkpoint'}:
            raise ValueError('Unknown advanced runtime option.')
        capabilities=self.advanced_capabilities(config['id']); method=advanced.get('spec_type','none')
        if method not in capabilities['spec_types']: raise ValueError('This executable does not support the selected speculation method.')
        arguments=[]; environment={}
        if method!='none':
            arguments+=['--spec-type',method]
            tokens=advanced.get('draft_tokens',3)
            if type(tokens) is not int or not 1<=tokens<=32: raise ValueError('Draft tokens must be from 1 to 32.')
            if method.startswith('draft-'):
                arguments+=['--spec-draft-n-max',str(tokens)]
                draft=Path(str(advanced.get('draft_model_path',''))).expanduser().resolve(strict=True)
                if not draft.is_file() or draft.suffix.lower()!='.gguf': raise ValueError('Select the matching GGUF draft or MTP model.')
                arguments+=['--spec-draft-model',str(draft)]
        if advanced.get('cuda_graph_opt'):
            if not capabilities['cuda_graph']: raise ValueError('CUDA graph tuning requires an available CUDA GPU.')
            environment['GGML_CUDA_GRAPH_OPT']='1'
        if advanced.get('fp4_checkpoint'):
            if not capabilities['fp4']: raise ValueError('FP4 requires compatible GPU hardware.')
            from gguf import GGUFReader, GGMLQuantizationType
            model=GGUFReader(str(data['model_path']))
            if not any(t.tensor_type==GGMLQuantizationType.NVFP4 for t in model.tensors):
                raise ValueError('This checkpoint does not contain native NVFP4 tensors.')
            hardware=subprocess.run(['nvidia-smi','--query-gpu=compute_cap','--format=csv,noheader,nounits'],
                capture_output=True,text=True,timeout=5,creationflags=0x08000000 if os.name=='nt' else 0)
            if not any(v.strip() in ('10.0','10.3','12.0','12.1') for v in hardware.stdout.splitlines()):
                raise ValueError('This GPU has not been identified as a supported Blackwell device.')
        return arguments,environment

    def _model_binding(self,data):
        binding={key:data.get(key) for key in ('model','model_path','mmproj_path')}
        binding.update(context=data.get('context',32768),advanced=data.get('advanced') or {})
        for key in ('model_path','mmproj_path'):
            if data.get(key):
                path=Path(data[key]).resolve(); stat=path.stat()
                binding[key]={'path':str(path),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        draft=(data.get('advanced') or {}).get('draft_model_path')
        if draft:
            path=Path(draft).resolve(); stat=path.stat(); binding['draft']={'path':str(path),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        config=self.configs.get(data.get('id'),{})
        if config.get('engine')=='ollama' and data.get('model'):
            # A model tag can be repointed without changing its friendly name.
            # Bind to the managed store's manifest as well as that name.
            model=str(data['model']); name,separator,tag=model.rpartition(':')
            name,tag=(name,tag) if separator else (model,'latest')
            parts=name.split('/')
            if len(parts)==1: parts=['registry.ollama.ai','library']+parts
            elif '.' not in parts[0] and ':' not in parts[0]: parts=['registry.ollama.ai']+parts
            root=Path(config['model_directory']).resolve()/'manifests'
            manifest=root.joinpath(*parts,tag).resolve()
            if not manifest.is_relative_to(root) or not manifest.is_file(): binding['manifest_sha256']=None
            else: binding['manifest_sha256']=self._hash(manifest)
        return binding

    def start(self, data, validation=False):
        identifier = data.get('id')
        with self._lock:
            config = self._config(identifier)
            process = self._processes.get(identifier)
            if process and process.poll() is None:
                raise ValueError('Runtime is already running.')
            executable = Path(config['executable'])
            if not executable.is_file() or self._hash(executable) != config['executable_sha256']:
                raise ValueError('Runtime executable changed. Register and verify it again.')
            profile = data.get('profile', 'f16')
            if profile not in ('f16', 'q8_0'):
                raise ValueError('Choose f16 or q8_0. Advanced profiles require a separately validated engine adapter.')
            advanced=data.get('advanced') or {}
            advanced_args,advanced_env=self._advanced_arguments(config,data)
            if (advanced_args or advanced_env or advanced.get('fp4_checkpoint')) and not validation:
                approved=config.get('validation') or {}
                if not approved.get('promoted') or approved.get('binding')!=self._model_binding(data):
                    raise ValueError('Validate this exact advanced engine/model combination with images and tools before enabling it.')
            if profile == 'q8_0' and not validation and not (config.get('validation') or {}).get('promoted'):
                raise ValueError('Run f16/q8 validation on the exact model before enabling q8_0.')
            if not validation and profile == 'q8_0':
                validated = config['validation']
                current_model = data.get('model') if config['engine'] == 'ollama' else str(data.get('model_path', ''))
                if (current_model != validated.get('model') or data.get('context', 32768) != validated.get('context') or
                    data.get('mmproj_path') != validated.get('projector') or
                    config['executable_sha256'] != validated.get('executable_sha256') or
                    self._model_binding(data)!=validated.get('binding')):
                    raise ValueError('This exact model, projector, context and executable have not passed profile validation.')
            port = self._available_port(data.get('port', 11435 if config['engine'] == 'ollama' else 8082))
            environment = os.environ.copy()
            environment.update(advanced_env)
            arguments = [str(executable)]
            if config['engine'] == 'ollama':
                arguments += ['serve']
                Path(config['model_directory']).mkdir(parents=True, exist_ok=True)
                environment.update(OLLAMA_HOST='127.0.0.1:' + str(port), OLLAMA_MODELS=config['model_directory'],
                                   OLLAMA_FLASH_ATTENTION='1', OLLAMA_KV_CACHE_TYPE=profile,
                                   OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_KEEP_ALIVE='10m')
                base = 'http://127.0.0.1:' + str(port)
            else:
                model = Path(str(data.get('model_path', ''))).expanduser().resolve(strict=True)
                if not model.is_file() or model.suffix.lower() != '.gguf':
                    raise ValueError('Choose a local GGUF model file.')
                context = data.get('context', 32768)
                if type(context) is not int or not 2048 <= context <= 262144:
                    raise ValueError('Context must be from 2K to 256K.')
                arguments += ['--model', str(model), '--host', '127.0.0.1', '--port', str(port),
                              '--ctx-size', str(context), '--n-gpu-layers', '999', '--parallel', '1',
                              '--flash-attn', 'on', '--cache-type-k', profile, '--cache-type-v', profile,
                              '--jinja']
                arguments+=advanced_args
                projector = data.get('mmproj_path')
                if projector:
                    projector = Path(projector).expanduser().resolve(strict=True)
                    if not projector.is_file() or projector.suffix.lower() != '.gguf':
                        raise ValueError('Choose the matching GGUF vision projector.')
                    arguments += ['--mmproj', str(projector)]
                base = 'http://127.0.0.1:' + str(port) + '/v1'
            flags = 0x08000000 if os.name == 'nt' else 0
            process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                       creationflags=flags, start_new_session=os.name != 'nt')
            self._processes[identifier] = process
            recent = self._logs[identifier] = deque(maxlen=80)
            def drain():
                try:
                    for line in process.stdout:
                        recent.append(line.rstrip()[:1000])
                finally:
                    process.stdout.close()
            threading.Thread(target=drain, name='forge-runtime-log', daemon=True).start()
            config['last_launch'] = {**data, 'url': base, 'pid': process.pid, 'profile': profile}
            self._save()
            return {'id': identifier, 'url': base, 'pid': process.pid, 'profile': profile, 'state': 'starting'}

    def stop(self, identifier):
        with self._lock:
            process = self._processes.pop(identifier, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return {'id': identifier, 'state': 'stopped'}

    def status(self):
        with self._lock:
            values = []
            for identifier, config in self.configs.items():
                process = self._processes.get(identifier)
                values.append({**config, 'state': 'running' if process and process.poll() is None else 'stopped',
                               'recent_output': list(self._logs.get(identifier, []))[-8:]})
            return {'runtimes': values, 'validation': dict(self._validation), 'memory': memory_telemetry(),
                    'profiles': ENGINE_PROFILES}

    @staticmethod
    def _coding_valid(text):
        """Evaluate only a narrowly allowed pure expression, never arbitrary model code."""
        text = re.sub(r'^```(?:python)?\s*|\s*```$', '', text.strip(), flags=re.I)
        try:
            tree = ast.parse(text)
            if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef): return False
            function = tree.body[0]
            arguments = function.args
            if (len(function.body) != 1 or not isinstance(function.body[0], ast.Return) or function.decorator_list or
                function.returns or len(arguments.args) != 1 or arguments.args[0].annotation or arguments.defaults or
                arguments.posonlyargs or arguments.kwonlyargs or arguments.vararg or arguments.kwarg): return False
            expression = function.body[0].value
            if (not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name) or expression.func.id != 'sum' or
                len(expression.args) != 1 or expression.keywords): return False
            generator = expression.args[0]
            if not isinstance(generator, (ast.GeneratorExp, ast.ListComp)) or len(generator.generators) != 1: return False
            loop = generator.generators[0]
            if (loop.is_async or not isinstance(loop.target, ast.Name) or not isinstance(loop.iter, ast.Name) or
                loop.iter.id != arguments.args[0].arg or len(loop.ifs) > 2): return False
            allowed = (ast.GeneratorExp, ast.ListComp, ast.comprehension, ast.Name, ast.Load, ast.Store, ast.Constant,
                       ast.BinOp, ast.Mod, ast.Add, ast.Sub, ast.Mult, ast.Compare, ast.Eq, ast.NotEq,
                       ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub)
            nodes = list(ast.walk(generator))
            names = {arguments.args[0].arg, loop.target.id}
            if len(nodes) > 60 or any(not isinstance(node, allowed) for node in nodes): return False
            if any(isinstance(node, ast.Name) and node.id not in names for node in nodes): return False
            if any(isinstance(node, ast.Constant) and (type(node.value) not in (int, float, bool) or abs(node.value) > 1000) for node in nodes): return False
            namespace = {'__builtins__': {'sum': sum}}
            exec(compile(tree, '<forge-pure-expression-validation>', 'exec'), namespace)
            callback = namespace[function.name]
            cases = [([], 0), ([1, 3, 5], 0), ([1, 2, -4, 6], 4), ([0, 8, -2, 7], 6)]
            return all(callback(values) == expected for values, expected in cases)
        except (ValueError, SyntaxError, TypeError, NameError, ArithmeticError):
            return False

    @staticmethod
    def _vision_image():
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (256, 128), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 100, 100), fill='red')
        draw.ellipse((140, 20, 220, 100), fill='blue')
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    def _validation_lines(self, url, body):
        async def consume(response, publish):
            async for line in bounded_lines(response):
                if self._cancel.is_set(): return
                progress={}
                raw=line[5:].strip() if line.startswith('data:') else line
                try:
                    value=json.loads(raw)
                    if isinstance(value,dict):
                        progress=value.get('message') or {}
                        choices=value.get('choices') or []
                        if choices and isinstance(choices[0],dict):
                            delta=choices[0].get('delta') or {}
                            progress={'content':delta.get('content') or '',
                                'thinking':delta.get('reasoning_content') or '',
                                'tool_calls':delta.get('tool_calls') or []}
                except ValueError: pass
                await publish({'line':line,'message':progress})
        with self.inference_queue.lease(self._cancel, True):
            for packet in cancellable_inference(url, body, self._cancel, consume,
                timeouts=Core._inference_timeouts(connect=10), provider='Managed runtime validation'):
                yield packet['line']

    def _probe(self, base, engine, model, purpose, image=None, context=32768):
        tools = [{'type': 'function', 'function': {'name': 'forge_validation_echo',
                  'description': 'Echo a validation marker.', 'parameters': {'type': 'object',
                  'properties': {'marker': {'type': 'string'}}, 'required': ['marker']}}}]
        text = ('Call forge_validation_echo with marker FORGE_TOOL_OK. Do not answer with prose.' if purpose == 'tools'
                else 'Which shape is blue? Answer with one word.' if purpose == 'vision'
                else 'Write sum_even(values), a Python function that sums even integers. Use one return statement with sum and a single generator comprehension. No imports, annotations or docstrings. Include only code.')
        message = {'role': 'user', 'content': text}
        if engine == 'ollama':
            if image:
                message['images'] = [image]
            body = {'model': model, 'messages': [message], 'stream': True, 'think': False,
                    'options': {'temperature': 0, 'num_ctx': context, 'num_predict': 512}, 'keep_alive': '10m'}
            url = base + '/api/chat'
        else:
            if image:
                message['content'] = [{'type': 'text', 'text': text}, {'type': 'image_url',
                                      'image_url': {'url': 'data:image/png;base64,' + image}}]
            body = {'model': model, 'messages': [message], 'stream': True, 'max_tokens': 512,
                    'temperature': 0, 'stream_options': {'include_usage': True}}
            url = base + '/chat/completions'
        if purpose == 'tools':
            body['tools'] = tools
        start, first, output, final = time.monotonic(), None, '', {}
        completed=False; truncated=False
        accumulator = ToolCallAccumulator()
        try:
            for line in self._validation_lines(url, body):
                if self._cancel.is_set():
                    raise ValueError('Validation cancelled.')
                if not line:
                    continue
                if engine != 'ollama':
                    if not line.startswith('data:'):
                        continue
                    line = line[5:].strip()
                    if line == '[DONE]':
                        break
                packet = json.loads(line)
                if packet.get('error'): raise ValueError('Managed validation provider returned an error.')
                if engine == 'ollama':
                    delta = packet.get('message', {})
                    if packet.get('done'):
                        final = packet
                        completed=packet.get('done_reason') in (None,'stop','tool_calls','function_call')
                        truncated=not completed
                else:
                    choices = packet.get('choices', [])
                    delta = choices[0].get('delta', {}) if choices else {}
                    if choices and choices[0].get('finish_reason'):
                        completed=choices[0]['finish_reason'] in ('stop','tool_calls','function_call')
                        truncated=not completed
                    if packet.get('usage'):
                        final['usage'] = packet['usage']
                    if packet.get('timings'):
                        final['timings'] = packet['timings']
                content = delta.get('content') or ''
                if (content or delta.get('tool_calls')) and first is None:
                    first = time.monotonic()
                output += content
                if len(output) > 100000:
                    raise ValueError('Validation response exceeded its output limit.')
                if delta.get('tool_calls'):
                    accumulator.add(delta['tool_calls'])
        finally:
            if self.usage_store is not None:
                counts=final.get('usage') or {}
                count=final.get('eval_count',counts.get('completion_tokens'))
                prompt=final.get('prompt_eval_count',counts.get('prompt_tokens'))
                elapsed=time.monotonic()-start
                self.usage_store.record_usage({'id':uuid4().hex,'provider':'managed-validation','model':model,
                    'purpose':'runtime-validation-'+purpose,'input_tokens':prompt or 0,
                    'output_tokens':count if count is not None else (len(output.encode('utf-8'))+2)//3,
                    'estimated':prompt is None or count is None,'cancelled':self._cancel.is_set(),
                    'decode_seconds':final.get('eval_duration',0)/1e9,'total_seconds':elapsed,
                    'phase_timings':{'first_output_seconds':first-start if first is not None else None,
                        'total_seconds':elapsed,'provenance':{'first_output':'measured'}}})
        elapsed = time.monotonic() - start
        count = final.get('eval_count', (final.get('usage') or {}).get('completion_tokens'))
        decode = final.get('eval_duration', 0) / 1e9
        if not decode:
            decode = (final.get('timings') or {}).get('predicted_ms', 0) / 1000
        calls = accumulator.finish()
        valid = (any(c['function']['name'] == 'forge_validation_echo' and
                     c['function']['arguments'].get('marker') == 'FORGE_TOOL_OK' for c in calls)
                 if purpose == 'tools' else 'circle' in output.lower() if purpose == 'vision'
                 else self._coding_valid(output))
        return {'purpose': purpose, 'passed': valid and completed and not truncated, 'completed':completed,
                'truncated':truncated,'output_tokens': count, 'decode_seconds': decode,
                'tps': count / decode if count is not None and decode > 0 else None,
                'ttft_seconds': first - start if first else None, 'total_seconds': elapsed}

    def validate(self, data):
        identifier = data.get('id')
        config = self._config(identifier)
        model = data.get('model') if config['engine'] == 'ollama' else data.get('model_path')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Choose the exact model to validate.')
        with self._lock:
            if self._validation.get('state') == 'running':
                raise ValueError('A validation is already running.')
            if identifier in self._processes and self._processes[identifier].poll() is None:
                raise ValueError('Stop this managed runtime before comparing profiles.')
            self._cancel.clear()
            self._validation = {'state': 'running', 'runtime_id': identifier, 'model': model, 'phase': 'f16'}
        def worker():
            records = {}
            error = None
            try:
                for profile in ('f16', 'q8_0'):
                    with self._lock:
                        self._validation['phase'] = profile
                    launched = self.start({**data, 'profile': profile, 'advanced':data.get('advanced',{})}, validation=True)
                    base = launched['url']
                    deadline = time.monotonic() + 120
                    with httpx.Client(timeout=2) as client:
                        while time.monotonic() < deadline:
                            if self._cancel.is_set():
                                raise ValueError('Validation cancelled.')
                            process = self._processes[identifier]
                            if process.poll() is not None:
                                raise ValueError('Managed engine exited: ' + '\n'.join(self._logs.get(identifier, []))[-1500:])
                            try:
                                response = client.get(base + ('/api/tags' if config['engine'] == 'ollama' else '/models'))
                                if response.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            time.sleep(.5)
                        else:
                            raise ValueError('Managed engine did not become ready.')
                    before = memory_telemetry()
                    samples, sampling_stop = [], threading.Event()
                    def sample_memory():
                        while not sampling_stop.is_set():
                            samples.append(memory_telemetry())
                            if len(samples) > 1000: samples.pop(0)
                            sampling_stop.wait(.5)
                    sampler = threading.Thread(target=sample_memory, name='forge-runtime-memory', daemon=True)
                    sampler.start()
                    try:
                        probes = [self._probe(base, config['engine'], model, 'coding', context=data.get('context', 32768))]
                        probes.extend(self._probe(base, config['engine'], model, 'coding', context=data.get('context', 32768)) for _ in range(5))
                        probes.append(self._probe(base, config['engine'], model, 'tools', context=data.get('context', 32768)))
                        probes.append(self._probe(base, config['engine'], model, 'vision', self._vision_image(), data.get('context', 32768)))
                    finally:
                        sampling_stop.set()
                        sampler.join(timeout=6)
                    peaks = {}
                    for sample in samples:
                        for index, gpu in enumerate(sample.get('gpus', [])):
                            peaks[index] = max(peaks.get(index, 0), gpu['used_mb'])
                    records[profile] = {'probes': probes, 'memory_before': before, 'memory_after': memory_telemetry(),
                                        'sampled_peak_gpu_mb': peaks, 'memory_samples': len(samples)}
                    self.stop(identifier)
                f16, q8 = records['f16']['probes'], records['q8_0']['probes']
                baseline = [p['tps'] for p in f16[1:6] if p['tps']]
                optimized = [p['tps'] for p in q8[1:6] if p['tps']]
                baseline_speed = statistics.median(baseline) if baseline else None
                optimized_speed = statistics.median(optimized) if optimized else None
                preserved = all(p['passed'] for p in f16 + q8)
                faster = bool(baseline_speed and optimized_speed and optimized_speed >= baseline_speed * 1.05)
                result = {'model': model, 'engine_version': config['version'], 'executable_sha256': config['executable_sha256'],
                          'projector': data.get('mmproj_path'), 'context': data.get('context', 32768),
                          'profiles': records, 'promoted': preserved and faster,
                          'binding':self._model_binding(data),'advanced':data.get('advanced') or {},
                          'baseline_tps': baseline_speed, 'optimized_tps': optimized_speed, 'warm_repetitions':5, 'minimum_gain':0.05,
                          'reason': 'Vision and streamed tools passed; measured decoding improved.' if preserved and faster
                                    else 'Profile was not promoted: capability checks failed or decoding did not improve.'}
                with self._lock:
                    config['validation'] = result
                    self._save()
                    self._validation = {'state': 'done', 'runtime_id': identifier, **result}
            except Exception as exc:
                error = str(exc)[:2000]
            finally:
                self.stop(identifier)
                if error:
                    with self._lock:
                        self._validation = {'state': 'error', 'runtime_id': identifier, 'error': error, 'profiles': records}
        threading.Thread(target=worker, name='forge-runtime-validation', daemon=True).start()
        return dict(self._validation)

    def close(self):
        self._cancel.set()
        for identifier in list(self._processes):
            self.stop(identifier)

    shutdown = close

    def dispatch(self, action, data=None):
        data = data or {}
        if action == 'runtime_status': return self.status()
        if action == 'runtime_capabilities': return self.advanced_capabilities(data['id'])
        if action == 'runtime_install': return self.install(data)
        if action == 'runtime_start': return self.start(data)
        if action == 'runtime_stop': return self.stop(data.get('id'))
        if action == 'runtime_validate': return self.validate(data)
        raise ValueError('Unknown runtime action.')
