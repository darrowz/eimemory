"""Bounded numeric diagnostics; absent measurements are never synthesized as zero."""
from contextlib import contextmanager
from math import isfinite
from time import monotonic

BRIDGE_TIMING_FIELDS = (
    'bridge_import_ms', 'bridge_client_setup_ms', 'provider_response_ms',
    'bridge_response_validation_ms', 'bridge_elapsed_ms',
)
CHILD_TIMING_FIELDS = BRIDGE_TIMING_FIELDS
TIMING_FIELDS = CHILD_TIMING_FIELDS + (
    'command_spawn_ms', 'command_io_ms', 'command_decode_ms',
)
VERIFICATION_STAGES = ('client_setup', 'evidence_projection', 'completion', 'proof_validation')
FAILURE_SCHEMA = 'eimemory.command-failure.v1'
MAX_FAILURE_BYTES = 4096
FAILURE_CATEGORIES = frozenset({
    'bridge_import_failed', 'bridge_client_setup_failed', 'provider_request_failed',
    'bridge_response_validation_failed', 'bridge_output_invalid', 'bridge_failed',
})


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
    return _numbers(value, CHILD_TIMING_FIELDS)


def safe_timing(value):
    result = _numbers(value, TIMING_FIELDS)
    if isinstance(value, dict) and type(value.get('command_prepared')) is bool:
        result['command_prepared'] = value['command_prepared']
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
