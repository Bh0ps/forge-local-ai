"""Editable engine setup suggestions, never inference or resource authority."""
from types import MappingProxyType


_PRESETS = tuple(MappingProxyType(dict(id=identifier, name=name, kind=kind, url=url,
    setup_url=setup, description=description, hosting=hosting,
    metadata_strategy={'lm-studio': 'lmstudio', 'docker-model-runner': 'dmr'}.get(identifier)))
    for identifier, name, kind, url, setup, description, hosting in (
        ('ollama', 'Ollama', 'ollama', 'http://127.0.0.1:11434/api',
         'https://docs.ollama.com/quickstart', 'Connect to your running Ollama server.', 'desktop'),
        ('lm-studio', 'LM Studio', 'openai-compatible', 'http://127.0.0.1:1234/v1',
         'https://lmstudio.ai/docs/developer/openai-compat',
         'Start LM Studio\'s local server. Model capabilities depend on the selected model.', 'desktop'),
        ('llama-cpp', 'llama.cpp', 'openai-compatible', 'http://127.0.0.1:8080/v1',
         'https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md',
         'Connect to llama-server with your chosen model and server configuration.', 'desktop'),
        ('jan-desktop', 'Jan Desktop', 'openai-compatible', 'http://127.0.0.1:1337/v1',
         'https://www.jan.ai/docs/desktop/api-server',
         'Start Jan\'s Local API Server; leave server-side tool execution off for Forge.', 'desktop'),
        ('localai', 'LocalAI', 'openai-compatible', 'http://127.0.0.1:8080/v1',
         'https://localai.io/',
         'Connect to your separately configured LocalAI server or container.', 'container'),
        ('vllm', 'vLLM', 'openai-compatible', 'http://127.0.0.1:8000/v1',
         'https://docs.vllm.ai/en/latest/serving/openai_compatible_server/',
         'Connect to an external or WSL server; tools require its configured parser and template.', 'external'),
        ('sglang', 'SGLang', 'openai-compatible', 'http://127.0.0.1:30000/v1',
         'https://docs.sglang.io/docs/basic_usage/openai_api_completions',
         'Connect to an external or WSL server; controls depend on its model and parser.', 'external'),
        ('koboldcpp', 'KoboldCpp', 'openai-compatible', 'http://127.0.0.1:5001/v1',
         'https://github.com/LostRuins/koboldcpp/wiki',
         'Connect to KoboldCpp\'s compatible API; test the selected model separately.', 'desktop'),
        ('textgen', 'Textgen (Oobabooga)', 'openai-compatible', 'http://127.0.0.1:5000/v1',
         'https://github.com/oobabooga/textgen/wiki/12-%E2%80%90-OpenAI-API',
         'Start Textgen with --api and load a model in the engine. Forge uses its OpenAI-compatible API.', 'desktop'),
        ('docker-model-runner', 'Docker Model Runner', 'openai-compatible',
         'http://127.0.0.1:12434/engines/llama.cpp/v1',
         'https://docs.docker.com/ai/model-runner/get-started/',
         'Enable Docker host TCP access and prepare a local GGUF model. Use the loopback llama.cpp route; serving context and tuning are not reported.', 'desktop'),
    ))
_BY_ID = MappingProxyType({preset['id']: preset for preset in _PRESETS})


def provider_presets():
    return {'presets': [dict(preset) for preset in _PRESETS]}


def provider_preset(identifier):
    if not isinstance(identifier, str) or identifier not in _BY_ID:
        raise ValueError('Choose a known engine preset or use a custom endpoint.')
    return _BY_ID[identifier]


def validate_preset(identifier, kind):
    if identifier is None:
        return None
    if provider_preset(identifier)['kind'] != kind:
        raise ValueError('The engine preset does not match the selected protocol.')
    return identifier
