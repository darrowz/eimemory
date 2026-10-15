"""Bounded numeric diagnostics; absent measurements are never synthesized as zero."""
from contextlib import contextmanager
from math import isfinite
from time import monotonic
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BRIDGE_TIMING_FIELDS = (
    'bridge_import_ms', 'bridge_client_setup_ms', 'provider_response_ms',
    'bridge_response_validation_ms', 'bridge_elapsed_ms', 'provider_retry_wait_ms',
)
CHILD_TIMING_FIELDS = BRIDGE_TIMING_FIELDS
TIMING_FIELDS = CHILD_TIMING_FIELDS + (
    'command_spawn_ms', 'command_io_ms', 'command_decode_ms',
)
VERIFICATION_STAGES = ('independent_evidence', 'client_setup', 'evidence_projection', 'completion', 'proof_validation')
FAILURE_SCHEMA = 'eimemory.command-failure.v1'
MAX_FAILURE_BYTES = 4096
FAILURE_CATEGORIES = frozenset({
    'bridge_import_failed', 'bridge_client_setup_failed', 'provider_request_failed',
    'bridge_response_validation_failed', 'bridge_output_invalid', 'bridge_failed',
})
PROVIDER_FAILURE_SCHEMA = 'provider_failure.v1'
PROVIDER_FAILURE_CLASSES = frozenset({
    'authentication_rejected', 'permission_rejected', 'rate_limited',
    'request_rejected', 'provider_server_error', 'request_timeout',
    'connection_failed', 'unknown',
})
PROVIDER_SDK_ERROR_TYPES = frozenset({
    'AuthenticationError', 'PermissionDeniedError', 'RateLimitError',
    'BadRequestError', 'NotFoundError', 'UnprocessableEntityError',
    'InternalServerError', 'APIStatusError', 'HTTPStatusError',
    'APITimeoutError', 'APIConnectionError', 'TimeoutError', 'ConnectionError',
    'TimeoutException', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout',
    'PoolTimeout', 'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError',
    'ServiceUnavailableError', '_StreamErrorEvent', 'unknown',
})
PROVIDER_RETRY_STOPS = frozenset({
    'not_retryable', 'attempt_limit', 'deadline_budget',
    'retry_after_exceeds_bound', 'completion_received',
    'sdk_retries_already_configured', 'sdk_retry_policy_unverified',
    'provider_identity_changed', 'sdk_retry_policy_changed',
    'retry_after_unavailable', 'response_validation_failed', 'review_validation_failed',
    'client_cleanup_failed',
})
_TRANSIENT_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_TIMEOUT_TYPES = frozenset({'APITimeoutError', 'TimeoutError', 'TimeoutException',
    'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'})
_CONNECTION_TYPES = frozenset({'APIConnectionError', 'ConnectionError',
    'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError'})
MAX_PROVIDER_RETRY_AFTER_MS = 3_600_000
# Responses-API backends (Codex) report upstream failures as a ``type=error``
# SSE frame on an HTTP 200 stream; Hermes raises ``_StreamErrorEvent`` with the
# frame's typed ``code``. Only this fixed vocabulary is read or kept.
STREAM_ERROR_EVENT_TYPE = '_StreamErrorEvent'
STREAM_ERROR_CODE_CLASSES = {
    'rate_limit_exceeded': 'rate_limited',
    'rate_limit_error': 'rate_limited',
    'server_error': 'provider_server_error',
    'internal_error': 'provider_server_error',
    'internal_server_error': 'provider_server_error',
    'server_overloaded': 'provider_server_error',
    'overloaded': 'provider_server_error',
    'overloaded_error': 'provider_server_error',
    'service_unavailable': 'provider_server_error',
    'timeout': 'request_timeout',
    'request_timeout': 'request_timeout',
    'invalid_request_error': 'request_rejected',
    'invalid_prompt': 'request_rejected',
    'context_length_exceeded': 'request_rejected',
    'insufficient_quota': 'request_rejected',
    'usage_limit_reached': 'rate_limited',
}
_RETRYABLE_STREAM_CLASSES = frozenset({'rate_limited', 'provider_server_error', 'request_timeout'})


def _attribute(value, name):
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _stream_error_code(value):
    return value if isinstance(value, str) and value in STREAM_ERROR_CODE_CLASSES else ''


def _provider_classification(status, sdk_type, stream_code=''):
    if status is None and sdk_type == STREAM_ERROR_EVENT_TYPE:
        category = STREAM_ERROR_CODE_CLASSES.get(stream_code, 'unknown')
        return category, category in _RETRYABLE_STREAM_CLASSES
    if status == 401:
        category = 'authentication_rejected'
    elif status == 403:
        category = 'permission_rejected'
    elif status == 408:
        category = 'request_timeout'
    elif status == 429:
        category = 'rate_limited'
    elif status is not None and status >= 500:
        category = 'provider_server_error'
    elif status is not None:
        category = 'request_rejected'
    elif sdk_type in _TIMEOUT_TYPES:
        category = 'request_timeout'
    elif sdk_type in _CONNECTION_TYPES:
        category = 'connection_failed'
    else:
        category = {'AuthenticationError': 'authentication_rejected',
            'PermissionDeniedError': 'permission_rejected', 'RateLimitError': 'rate_limited',
            'BadRequestError': 'request_rejected', 'NotFoundError': 'request_rejected',
            'UnprocessableEntityError': 'request_rejected',
            'InternalServerError': 'provider_server_error',
            'ServiceUnavailableError': 'provider_server_error'}.get(sdk_type, 'unknown')
    retryable = (status in _TRANSIENT_HTTP_STATUS if status is not None else
        sdk_type in (_TIMEOUT_TYPES | _CONNECTION_TYPES))
    return category, retryable


def provider_failure(exc, *, attempt_count=1, retry_stop='', outcome='failed'):
    """Read typed SDK attributes only; exception text, bodies and routes are never read."""
    sdk_type = 'unknown'
    for error_class in type(exc).__mro__:
        name = error_class.__name__
        if name in PROVIDER_SDK_ERROR_TYPES:
            sdk_type = name
            break
    status = _attribute(exc, 'status_code')
    response = _attribute(exc, 'response')
    if type(status) is not int or not 400 <= status <= 599:
        status = _attribute(response, 'status_code')
    if type(status) is not int or not 400 <= status <= 599:
        status = None
    stream_code = _stream_error_code(_attribute(exc, 'code')) if sdk_type == STREAM_ERROR_EVENT_TYPE else ''
    category, retryable = _provider_classification(status, sdk_type, stream_code)
    result = dict(schema=PROVIDER_FAILURE_SCHEMA, phase='provider_request',
        failure_class=category, sdk_error_type=sdk_type, retryable=retryable,
        bridge_attempt_count=max(1, min(2, attempt_count)), outcome=outcome)
    if status is not None:
        result['http_status'] = status
    if stream_code:
        result['stream_error_code'] = stream_code
    # Read only the explicit delay field. Neither the headers container nor
    # response/error bodies leave this process. Malformed delays stay
    # unmeasured and prevent an early retry against an unknown server hint.
    delay = _attribute(exc, 'retry_after')
    if delay is None:
        headers = _attribute(response, 'headers')
        try:
            delay = headers.get('retry-after') if headers is not None else None
            if delay is None and headers is not None:
                delay = headers.get('Retry-After')
        except Exception:
            delay = None
    supplied_delay = delay is not None
    if isinstance(delay, str):
        if len(delay) <= 20 and re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,3})?', delay):
            delay = float(delay)
        else:
            try:
                retry_at = parsedate_to_datetime(delay) if len(delay) <= 128 else None
                delay = max(0, (retry_at - datetime.now(timezone.utc)).total_seconds()) if retry_at and retry_at.tzinfo else None
            except (TypeError, ValueError, OverflowError):
                delay = None
    if (type(delay) is int or (type(delay) is float and isfinite(delay))) and delay >= 0:
        result['retry_after_ms'] = round(min(MAX_PROVIDER_RETRY_AFTER_MS, delay * 1000), 3)
        if delay * 1000 > MAX_PROVIDER_RETRY_AFTER_MS:
            result['retry_after_truncated'] = True
    elif supplied_delay:
        result['retry_after_unparsed'] = True
    if retry_stop in PROVIDER_RETRY_STOPS:
        result['retry_stop'] = retry_stop
    return safe_provider_failure(result)


def safe_provider_failure(value, *, _include_first=True):
    """Copy only a fixed, consistent provider diagnostic; never preserve arbitrary labels."""
    if (not isinstance(value, dict) or value.get('schema') != PROVIDER_FAILURE_SCHEMA
            or value.get('phase') != 'provider_request'
            or not isinstance(value.get('failure_class'), str) or value['failure_class'] not in PROVIDER_FAILURE_CLASSES
            or not isinstance(value.get('sdk_error_type'), str) or value['sdk_error_type'] not in PROVIDER_SDK_ERROR_TYPES
            or not isinstance(value.get('outcome'), str) or value['outcome'] not in {'failed', 'recovered'}):
        return {}
    status = value.get('http_status')
    status = status if type(status) is int and 400 <= status <= 599 else None
    stream_code = (_stream_error_code(value.get('stream_error_code'))
        if value['sdk_error_type'] == STREAM_ERROR_EVENT_TYPE else '')
    category, retryable = _provider_classification(status, value['sdk_error_type'], stream_code)
    attempts = value.get('bridge_attempt_count')
    if (value['failure_class'] != category or value.get('retryable') is not retryable
            or type(attempts) is not int or not 1 <= attempts <= 2):
        return {}
    result = dict(schema=PROVIDER_FAILURE_SCHEMA, phase='provider_request', failure_class=category,
        sdk_error_type=value['sdk_error_type'], retryable=retryable,
        bridge_attempt_count=attempts, outcome=value['outcome'])
    if status is not None:
        result['http_status'] = status
    if stream_code:
        result['stream_error_code'] = stream_code
    delay = value.get('retry_after_ms')
    if (type(delay) is int or (type(delay) is float and isfinite(delay))) and delay >= 0:
        result['retry_after_ms'] = round(min(MAX_PROVIDER_RETRY_AFTER_MS, delay), 3)
        if value.get('retry_after_truncated') is True or delay > MAX_PROVIDER_RETRY_AFTER_MS:
            result['retry_after_truncated'] = True
    if value.get('retry_after_unparsed') is True:
        result['retry_after_unparsed'] = True
    if isinstance(value.get('retry_stop'), str) and value['retry_stop'] in PROVIDER_RETRY_STOPS:
        result['retry_stop'] = value['retry_stop']
    retries = value.get('sdk_max_retries')
    if type(retries) is int and 0 <= retries <= 10:
        result['sdk_max_retries'] = retries
    if _include_first and attempts == 2:
        first = safe_provider_failure(value.get('first_failure'), _include_first=False)
        if first and first['bridge_attempt_count'] == 1 and first['outcome'] == 'failed':
            result['first_failure'] = first
    return result


def _numbers(value, fields):
    value = value if isinstance(value, dict) else {}
    result = {}
    for key in fields:
        number = value.get(key)
        if type(number) in (int, float) and number >= 0:
            if type(number) is float and not isfinite(number):
                continue
            # Clamp ints before conversion; even arbitrarily large JSON ints are safe.
            bounded = min(1_000_000, number)
            if isfinite(bounded):
                result[key] = round(bounded, 3)
    return result


def safe_child_timing(value):
    """A child cannot forge parent spawn/IO measurements or preparation status."""
    result = _numbers(value, CHILD_TIMING_FIELDS)
    provider = safe_provider_failure(value.get('provider_failure')) if isinstance(value, dict) else {}
    if provider:
        result['provider_failure'] = provider
    return result


def safe_timing(value):
    result = _numbers(value, TIMING_FIELDS)
    if isinstance(value, dict) and type(value.get('command_prepared')) is bool:
        result['command_prepared'] = value['command_prepared']
    provider = safe_provider_failure(value.get('provider_failure')) if isinstance(value, dict) else {}
    if provider:
        result['provider_failure'] = provider
    return result


def failure_category(value):
    return value if isinstance(value, str) and value in FAILURE_CATEGORIES else ''


@contextmanager
def measure(timings, key):
    started = monotonic()
    try:
        yield
    finally:
        timings[key] = (monotonic() - started) * 1000
