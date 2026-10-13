"""Bounded public continuation diagnostics, independent of private provider state."""
from context_window import MAX_CONTEXT, MIN_CONTEXT


CONTINUATION_REASONS = frozenset(('model_unavailable', 'context_limit', 'capabilities', 'model_changed'))


def continuation_diagnostics(value):
    if not isinstance(value, dict) or value.get('category') != 'continuation_unavailable':
        return {}
    reason = value.get('continuation_reason')
    if not isinstance(reason, str) or reason not in CONTINUATION_REASONS:
        return {}
    result = {'continuation_reason': reason}
    if reason == 'context_limit':
        requested, limit = value.get('requested_context'), value.get('model_context_limit')
        if (type(requested) is int and MIN_CONTEXT <= requested <= MAX_CONTEXT
                and type(limit) is int and 1 <= limit <= 16_777_216 and requested > limit):
            result.update(requested_context=requested, model_context_limit=limit)
    return result
