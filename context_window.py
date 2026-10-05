"""Shared context settings and conservative prompt-budget estimates.

Estimates trigger compaction; Ollama remains the final tokenizer and rejects
overflow because agent requests disable truncation and context shifting.
"""
import json
import math


DEFAULT_CONTEXT = 8192
MIN_CONTEXT = 2048
MAX_CONTEXT = 262144
CONTEXT_STEP = 2048
PROMPT_MARGIN = 128


def validate_context(value=DEFAULT_CONTEXT):
    if type(value) is not int or not MIN_CONTEXT <= value <= MAX_CONTEXT or value % CONTEXT_STEP:
        raise ValueError('Context must be 2,048–262,144 tokens in steps of 2,048.')
    return value


def response_budget(context, requested=2048):
    validate_context(context)
    if type(requested) is not int or requested not in (1024, 2048, 4096, 8192):
        raise ValueError('Invalid response length')
    return min(requested, context // 4)


def history_character_limit(context):
    return 60000 * validate_context(context) // DEFAULT_CONTEXT


def history_message_limit(context):
    return max(250, 1000 * validate_context(context) // DEFAULT_CONTEXT)


def estimated_prompt_tokens(messages, tools, system_prompt):
    # Ignore base64 image bytes, but reserve space for the actual image tokens.
    plain, images = [], 0
    for message in messages:
        plain.append({key: value for key, value in message.items() if key != 'images'})
        images += len(message.get('images') or [])
    serialized = json.dumps({'system': system_prompt, 'messages': plain, 'tools': tools},
                            ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return math.ceil(len(serialized.encode('utf-8')) / 3) + 12 * (len(messages) + 1) + images * 2048


def prompt_budget(context, response_tokens):
    return validate_context(context) - response_tokens - PROMPT_MARGIN


def advertised_context_limit(info):
    metadata = info.get('model_info', {}) if isinstance(info, dict) else {}
    if not isinstance(metadata, dict):
        return None
    architecture = metadata.get('general.architecture')
    explicit = metadata.get(str(architecture) + '.context_length') if architecture else None
    if type(explicit) is int and explicit > 0:
        return explicit
    limits = [value for key, value in metadata.items()
              if key.endswith('.context_length') and type(value) is int and value > 0]
    return min(limits) if limits else None


def require_model_context(context, info):
    context = validate_context(context)
    limit = advertised_context_limit(info)
    if limit is not None and context > limit:
        raise ValueError(f'This model advertises a maximum context of {limit:,} tokens; '
                         f'you selected {context:,}. Lower Context or select a model with a larger window.')
    return limit
