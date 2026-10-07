"""Offline contract tests; import is lazy and never imports project code."""
import ast
import builtins
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

def load_complete(tree, path, clock, resolver, trace):
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == 'complete']
    if len(functions) != 1:
        raise AssertionError('expected exactly one complete function')
    isolated = ast.Module(body=[functions[0]], type_ignores=[])
    fake_os = NS(environ={})

    def isolated_import(name, *args, **kwargs):
        if name == 'os':
            return fake_os
        raise AssertionError('unexpected extracted-function import: ' + name)

    safe_builtins = dict(vars(builtins), __import__=isolated_import)
    namespace = {'__builtins__': safe_builtins, 'time': clock, 'json': json,
                 'resolve_provider_client': resolver, '_luna_trace': trace}
    exec(compile(isolated, str(path), 'exec'), namespace)
    return namespace['complete']


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0


class Trace:
    def __init__(self):
        self.validations = 0

    def stage(self, name):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin_response_validation(self):
        self.validations += 1


class LimitError(Exception):
    status_code = 429


class Harness:
    def __init__(self, tree, path, fallback_delay=0, primary_delay=0, primary_call_delay=0,
                 failure=None, fallback_invalid=None, fallback_error=None):
        self.clock, self.trace = Clock(), Trace()
        self.calls, self.resolutions = [], []
        self.fallback_delay, self.primary_delay = fallback_delay, primary_delay
        self.primary_call_delay = primary_call_delay
        self.failure = failure if failure is not None else LimitError('usage_limit_reached')
        self.fallback_invalid, self.fallback_error = fallback_invalid, fallback_error
        self.complete = load_complete(tree, path, self.clock, self.resolve, self.trace)

    def resolve(self, provider, model):
        self.resolutions.append((provider, model))
        self.clock.now += self.primary_delay if provider == 'primary' else self.fallback_delay
        if provider == 'fallback' and self.fallback_error:
            raise self.fallback_error
        if provider == 'fallback' and self.fallback_invalid == 'none':
            return None, model
        returned_model = 'wrong' if provider == 'fallback' and self.fallback_invalid == 'model' else model

        def create(**kwargs):
            self.calls.append((provider, kwargs))
            if provider == 'primary':
                self.clock.now += self.primary_call_delay
                if self.failure is not False:
                    raise self.failure
            return NS(model=model, choices=[NS(message=NS(content='ok', tool_calls=None))])

        return NS(chat=NS(completions=NS(create=create))), returned_model

    def run(self, **overrides):
        request = dict(system_prompt='system', user_prompt='user', deadline_unix_ms=1010000,
                       provider='primary', model='primary-model', fallback_provider='fallback',
                       fallback_model='fallback-model', reasoning_effort='low')
        request.update(overrides)
        return self.complete(request)


class DeadlineContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = Path(__file__).resolve().parents[1] / 'deploy/luna_bridge/luna_review_command.py'
        cls.source_tree = ast.parse(cls.source_path.read_text(encoding='utf-8'), filename=str(cls.source_path))

    def harness(self, **kwargs):
        return Harness(self.source_tree, self.source_path, **kwargs)

    def test_fallback_resolution_consumes_budget(self):
        h = self.harness(fallback_delay=3, primary_delay=1, primary_call_delay=2)
        result = h.run()
        self.assertEqual([call[1]['timeout'] for call in h.calls], [9, 4])
        self.assertEqual(result, dict(text='ok', model_id='fallback-model', provider_id='fallback'))
        self.assertEqual(h.calls[1][1]['reasoning_effort'], 'low')
        self.assertEqual(h.calls[1][1]['messages'], [{'role': 'system', 'content': 'system'},
                                                  {'role': 'user', 'content': 'user'}])
        self.assertEqual(h.trace.validations, 1)

    def test_fallback_expires_during_resolution(self):
        for delay in (10, 11):
            with self.subTest(delay=delay):
                h = self.harness(fallback_delay=delay)
                with self.assertRaisesRegex(ValueError, '^deadline_expired$'):
                    h.run()
                self.assertEqual(len(h.calls), 1)
                self.assertEqual(h.resolutions, [('primary', 'primary-model'), ('fallback', 'fallback-model')])
                self.assertEqual(h.trace.validations, 0)

    def test_expired_before_fallback_skips_resolver(self):
        h = self.harness(primary_call_delay=10)
        with self.assertRaisesRegex(ValueError, '^deadline_expired$'):
            h.run()
        self.assertEqual(h.resolutions, [('primary', 'primary-model')])
        self.assertEqual(len(h.calls), 1)

    def test_primary_success_retains_budget_refresh(self):
        h = self.harness(primary_delay=3, failure=False)
        self.assertEqual(h.run()['provider_id'], 'primary')
        self.assertEqual(h.calls[0][1]['timeout'], 7)
        self.assertEqual(len(h.resolutions), 1)

    def test_primary_resolution_expiry_skips_create(self):
        h = self.harness(primary_delay=10)
        with self.assertRaisesRegex(ValueError, '^deadline_expired$'):
            h.run()
        self.assertEqual(h.calls, [])

    def test_initial_expiry_skips_resolver(self):
        h = self.harness()
        with self.assertRaisesRegex(ValueError, '^deadline_expired$'):
            h.run(deadline_unix_ms=1000000)
        self.assertEqual(h.resolutions, [])

    def test_fallback_invalid_client_precedes_expiry(self):
        for invalid in ('none', 'model'):
            with self.subTest(invalid=invalid):
                h = self.harness(fallback_delay=11, fallback_invalid=invalid)
                with self.assertRaisesRegex(RuntimeError, '^model_unavailable$'):
                    h.run()
                self.assertEqual(len(h.calls), 1)

    def test_resolver_error_precedes_expiry(self):
        error = LookupError('fake resolver error')
        h = self.harness(fallback_delay=11, fallback_error=error)
        with self.assertRaises(LookupError) as caught:
            h.run()
        self.assertIs(caught.exception, error)
        self.assertEqual(len(h.calls), 1)

    def test_ineligible_failure_preserved(self):
        for error in (RuntimeError('usage_limit_reached'), LimitError('different reason')):
            with self.subTest(error=str(error)):
                h = self.harness(failure=error)
                with self.assertRaises(type(error)) as caught:
                    h.run()
                self.assertIs(caught.exception, error)
                self.assertEqual(len(h.resolutions), 1)

    def test_missing_fallback_configuration_preserves_error(self):
        for key in ('fallback_model', 'fallback_provider'):
            with self.subTest(key=key):
                h = self.harness()
                with self.assertRaises(LimitError) as caught:
                    h.run(**{key: ''})
                self.assertIs(caught.exception, h.failure)
                self.assertEqual(len(h.resolutions), 1)

    def test_timeout_cap_retained(self):
        h = self.harness(fallback_delay=3)
        h.run(deadline_unix_ms=1200000)
        self.assertEqual([call[1]['timeout'] for call in h.calls], [90, 90])

