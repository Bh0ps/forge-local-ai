"""Bounded, private provider protocol state; never a public message field."""
import json

MAX_PROVIDER_STATE_BYTES = 262144
INCOMPLETE_PROVIDER_RECOVERY = ('Resume cannot safely replay this incomplete signed exchange. '
    'Inspect the saved work and make an explicit new request when ready. '
    'A new unrelated child does not automatically satisfy a required prior assignment; no partial tool call will execute.')


def reasoning_details(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError('Provider returned invalid reasoning state.')
    def bounded(item, depth=0):
        if depth > 24:
            raise ValueError('Provider reasoning state is too deeply nested.')
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('Provider returned invalid reasoning state.')
            for child in item.values(): bounded(child, depth + 1)
        elif isinstance(item, list):
            for child in item: bounded(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('Provider returned invalid reasoning state.')
    try:
        bounded(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > MAX_PROVIDER_STATE_BYTES:
            raise ValueError('Provider reasoning state exceeded its safe size limit.')
        return json.loads(encoded)
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError('Provider returned invalid reasoning state.') from None


def state_size(messages):
    return sum(len(json.dumps(message.get('_provider_state', {}).get('reasoning_details', []),
        ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        for message in messages if message.get('_provider_state'))
