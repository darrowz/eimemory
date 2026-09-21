"""One-shot Luna command diagnostics. No provider configuration or inference changes.

Failure channel: one <=4096-byte JSON object on stdout, exit status 1.
The parent must only parse it on nonzero exit. Stderr is never a protocol.
Missing fields mean unobserved, not zero. This helper has no network operations.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from math import isfinite
import sys
from time import perf_counter_ns

SCHEMA = 'eimemory.command-failure.v1'
MAX_FAILURE_BYTES = 4096
MAX_OUTPUT_BYTES = 2_000_000
FIELDS = ('bridge_import_ms', 'bridge_client_setup_ms', 'provider_response_ms',
          'bridge_response_validation_ms', 'bridge_elapsed_ms')
STAGE_ERRORS = {
    'bridge_import_ms': 'bridge_import_failed',
    'bridge_client_setup_ms': 'bridge_client_setup_failed',
    'provider_response_ms': 'provider_request_failed',
    'bridge_response_validation_ms': 'bridge_response_validation_failed',
}
ERRORS = frozenset(STAGE_ERRORS.values()) | {'bridge_output_invalid', 'bridge_failed'}


class _Discard(io.TextIOBase):
    """Discard Python library diagnostics without buffering exception/secret text."""
    def writable(self):
        return True

    def write(self, text):
        return len(text)

    def flush(self):
        pass


class _BoundedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self._bytes = 0

    def write(self, text):
        self._bytes += len(text.encode('utf-8'))
        if self._bytes > MAX_OUTPUT_BYTES:
            raise ValueError('bridge_output_bound')
        return super().write(text)


def _safe(values):
    result = {}
    for key in FIELDS:
        value = values.get(key)
        if type(value) not in (int, float) or value < 0:
            continue
        if type(value) is float and not isfinite(value):
            continue
        result[key] = round(min(1_000_000, value), 3)
    return result


class BridgeTrace:
    def __init__(self, started_ns=None, *, clock=perf_counter_ns):
        self.clock = clock
        self.started = self.clock() if started_ns is None else started_ns
        self.timings = {}
        self.failed = ''
        self.validation_started = None

    @contextmanager
    def stage(self, field):
        if field not in STAGE_ERRORS:
            raise ValueError('unrecognized_bridge_stage')
        started = self.clock()
        try:
            yield
        except BaseException:
            self.failed = STAGE_ERRORS[field]
            raise
        finally:
            self.timings[field] = (self.clock() - started) / 1_000_000

    def begin_response_validation(self):
        """Called after the API returns, before original response validation.

        Measured region extends through original postprocessing and the final
        outer-envelope check. It is not pure JSON parsing or provider inference.
        """
        if self.validation_started is not None:
            raise ValueError('repeated_validation_stage')
        self.validation_started = self.clock()

    def _finish(self):
        now = self.clock()
        if self.validation_started is not None:
            self.timings['bridge_response_validation_ms'] = (
                now - self.validation_started) / 1_000_000
            self.validation_started = None
        self.timings['bridge_elapsed_ms'] = (now - self.started) / 1_000_000
        return _safe(self.timings)

    def _failure(self, category):
        data = {'schema': SCHEMA,
                'error': category if category in ERRORS else 'bridge_failed',
                'diagnostics': self._finish()}
        encoded = json.dumps(data, separators=(',', ':'), allow_nan=False) + '\n'
        if len(encoded.encode('utf-8')) > MAX_FAILURE_BYTES:
            # Constants and bounded fields should make this unreachable.
            encoded = '{"schema":"' + SCHEMA + '","error":"bridge_failed","diagnostics":{}}\n'
        return encoded

    @contextmanager
    def session(self, *, active=True):
        """Run unchanged bridge code, adding diagnostics to its outer JSON only.

        Python stdout is privately bounded until the existing validation succeeds.
        Python stderr is discarded, never scraped for JSON or exposed to parent.
        Native writes bypassing sys streams are not intercepted: mixed stdout is
        rejected by parent and all native stderr is still ignored there.
        """
        if not active:
            yield self
            return
        output = sys.stdout
        capture = _BoundedOutput()
        failure = ''
        phase = 'execution'
        try:
            with redirect_stdout(capture), redirect_stderr(_Discard()):
                try:
                    yield self
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise
            phase = 'outer_validation'
            # Preserve original text/provider/model values; don't reconstruct them.
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate_outer_key')
                    result[key] = value
                return result
            def reject_constant(_):
                raise ValueError('non_finite_outer_json')
            payload = json.loads(capture.getvalue(), object_pairs_hook=pairs,
                                 parse_constant=reject_constant)
            if (not isinstance(payload, dict)
                    or set(payload) not in ({'text', 'provider_id', 'model_id'},
                                           {'text', 'provider_id', 'model_id', 'diagnostics'})
                    or any(not isinstance(payload.get(key), str) or not payload[key].strip()
                           for key in ('text', 'provider_id', 'model_id'))):
                raise ValueError('bridge_outer_protocol_invalid')
            payload['diagnostics'] = self._finish()
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n'
            if len(encoded.encode('utf-8')) > MAX_OUTPUT_BYTES:
                raise ValueError('bridge_output_bound')
        except BaseException:
            failure = self.failed or ('bridge_response_validation_failed'
                                      if self.validation_started is not None
                                      else 'bridge_failed' if phase == 'execution'
                                      else 'bridge_output_invalid')
            encoded = self._failure(failure)
        finally:
            capture.close()
        try:
            output.write(encoded)
            output.flush()
        except (BrokenPipeError, OSError):
            # Parent may already have terminated; never echo a traceback.
            raise SystemExit(1) from None
        if failure:
            raise SystemExit(1) from None
