"""OpenRouter recovery metadata: fixed categories, never remote prose or bodies."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math


ERROR_CATEGORIES = {
    'authentication': 'authentication', 'permission_denied': 'access_denied',
    'refusal': 'refusal', 'content_policy_violation': 'access_denied',
    'rate_limit_exceeded': 'rate_limit', 'provider_overloaded': 'provider_overloaded',
    'provider_unavailable': 'provider_unavailable', 'timeout': 'timeout',
    'context_length_exceeded': 'context', 'max_tokens_exceeded': 'output_limit',
    'token_limit_exceeded': 'quota', 'payment_required': 'free_route',
    'invalid_request': 'invalid_request', 'invalid_prompt': 'invalid_request',
    'not_found': 'free_route', 'payload_too_large': 'context',
    'string_too_long': 'context', 'unprocessable': 'invalid_request', 'server': 'provider_unavailable',
}
LIMIT_SOURCES = frozenset(('upstream_provider_shared_pool', 'upstream_provider_byok',
    'openrouter_free_tier_daily', 'openrouter_free_tier_per_minute',
    'openrouter_guardrail_budget', 'openrouter_workspace_budget', 'openrouter_key_limit',
    'openrouter_in_flight_budget', 'openrouter_credits'))
STOP_CATEGORIES = frozenset(ERROR_CATEGORIES.values()) | frozenset(('protocol', 'network',
    'cancelled', 'configuration', 'metadata', 'incomplete_provider_state', 'empty_result',
    'unusable_result', 'assignment_blocked', 'continuation_unavailable', 'consent_required'))
QUOTA_TTL_SECONDS = 300


def recovery_diagnostics(value):
    """Safe for public activity, exports, parent results and exception metadata."""
    if not isinstance(value, dict): return {}
    result = {}
    if isinstance(value.get('error_type'), str) and value['error_type'] in ERROR_CATEGORIES: result['error_type'] = value['error_type']
    if isinstance(value.get('limit_source'), str) and value['limit_source'] in LIMIT_SOURCES: result['limit_source'] = value['limit_source']
    retry = value.get('retry_after_seconds')
    if type(retry) is int and 1 <= retry <= 86400: result['retry_after_seconds'] = retry
    if isinstance(value.get('cause_category'), str) and value['cause_category'] in STOP_CATEGORIES: result['cause_category'] = value['cause_category']
    return result


def _retry_after(value):
    if not isinstance(value, str) or len(value) > 100: return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None: return None
            seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError): return None
    return math.ceil(seconds) if math.isfinite(seconds) and 0 < seconds <= 86400 else None


def classify(status, error=None, headers=None):
    """Never infer authentication, account exhaustion or refusal from raw text."""
    metadata = error.get('metadata') if isinstance(error, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    details = recovery_diagnostics(metadata)
    details.pop('cause_category',None)  # Only the local incomplete-state handler can assign a cause.
    retry = _retry_after((headers or {}).get('Retry-After'))
    if retry is not None: details['retry_after_seconds'] = retry
    category = ('authentication' if status == 401 else 'access_denied' if status == 403
        else 'rate_limit' if status == 429 else 'free_route' if status in (402, 404)
        else 'timeout' if status == 408 else 'invalid_request' if status == 400 else 'provider_unavailable')
    # An actual HTTP authentication failure cannot be rewritten by provider data.
    if status != 401 and details.get('error_type') in ERROR_CATEGORIES:
        category = ERROR_CATEGORIES[details['error_type']]
    if status == 401: return category, details
    source = details.get('limit_source')
    if source == 'openrouter_free_tier_daily': category = 'quota'
    elif source in ('openrouter_guardrail_budget', 'openrouter_workspace_budget', 'openrouter_key_limit'):
        category = 'access_denied'
    elif status == 429 and source in ('upstream_provider_shared_pool', 'upstream_provider_byok'):
        category = 'provider_overloaded'
    return category, details


def message(category, status=None, *, inference=True):
    text = {
        'authentication': 'OpenRouter rejected the API key. Update it in Settings.',
        'access_denied': 'OpenRouter denied this request under its permissions, policy or configured budget. Inspect the connection restrictions before retrying.',
        'refusal': 'The OpenRouter model declined this assignment. Reword the same bounded task for a fresh attempt.',
        'quota': 'OpenRouter reported an exhausted request or token allowance. Check its reset before retrying.',
        'rate_limit': 'OpenRouter temporarily rate-limited this request. Wait before retrying; this does not establish that the daily allowance is exhausted.',
        'provider_overloaded': 'The OpenRouter model provider is temporarily busy. Wait before retrying; this is not an exhausted API key.',
        'timeout': 'OpenRouter inference timed out. Saved work remains local; inspect the connection before resuming.',
        'free_route': 'No permitted free OpenRouter route accepted this request. Check the model and connection policy; Forge will not use a paid fallback.',
        'invalid_request': 'OpenRouter rejected the inference request. Check model, reasoning and tool-history compatibility before resuming.',
        'context': 'The OpenRouter request exceeds a provider context or input limit. Reduce the scoped input or select a compatible context.',
        'output_limit': 'The OpenRouter response reached its output limit. Inspect the saved output and response budget before resuming.',
    }.get(category)
    if text: return text
    return ('OpenRouter inference is unavailable' if inference else 'OpenRouter metadata is unavailable') + (f' (HTTP {status})' if status else '') + '. Saved work remains local; check the connection and retry later.'


def capacity(config, *, now=None):
    """A bounded metadata snapshot is a hint, never permission to block inference."""
    now = now or datetime.now(timezone.utc)
    stamp = config.get('account_checked_at')
    checked = None
    try:
        if not isinstance(stamp,str) or len(stamp)>64: raise ValueError()
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        checked = parsed.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError): pass
    stamp = checked.isoformat() if checked else None
    account = config.get('account')
    quota = account.get('free_model_daily_requests') if isinstance(account,dict) else None
    result = {'state': 'unknown', 'remaining': None, 'checked_at': stamp,
        'note': 'Account metadata does not guarantee inference capacity.'}
    if not isinstance(quota, dict) or any(type(quota.get(k)) is not int or not 0 <= quota[k] <= 2_147_483_647 for k in ('used', 'limit', 'remaining')):
        return result
    if checked is None: return result
    age = (now - checked).total_seconds()
    if not 0 <= age <= QUOTA_TTL_SECONDS or checked.date() != now.astimezone(timezone.utc).date():
        return {**result, 'state': 'stale', 'note': 'Refresh account metadata to see the current daily allowance.'}
    return {**result, 'state': 'available' if quota['remaining'] else 'exhausted',
        'remaining': quota['remaining'], 'used': quota['used'], 'limit': quota['limit'],
        'expires_at':min(checked+timedelta(seconds=QUOTA_TTL_SECONDS),
            checked.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).isoformat()}
