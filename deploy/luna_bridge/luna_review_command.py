"""Tool-free Luna completion using the configured Hermes credential router."""
from time import perf_counter_ns as _luna_clock
_luna_started_ns = _luna_clock()
# Hermes may re-exec scripts via isolated runpy, which omits the script directory.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from luna_observability import BridgeTrace as _LunaTrace
_luna_trace = _LunaTrace(_luna_started_ns)
_luna_serve = __name__ == '__main__' and sys.argv[1:] == ['--serve']
with _luna_trace.session(active=__name__ == '__main__' and not _luna_serve):
    import json
    import sys
    import time

    def _hermes_agent_root():
        import os
        from pathlib import Path
        explicit = (os.environ.get('EIMEMORY_HERMES_AGENT_ROOT') or '').strip()
        if explicit:
            return Path(explicit).expanduser()
        home = (os.environ.get('EIMEMORY_HERMES_HOME') or os.environ.get('HERMES_HOME') or '').strip()
        if home:
            root = Path(home).expanduser()
            nested = root / 'hermes-agent'
            if nested.is_dir():
                return nested
            return root
        return Path.home() / '.hermes' / 'hermes-agent'

    sys.path.insert(0, str(_hermes_agent_root()))
    with _luna_trace.stage('bridge_import_ms'):
        # Follow the host entrypoint contract: upgrades may move dependencies
        # out of the legacy venv. Bootstrap before importing provider modules,
        # and before reading stdin so a host-managed re-exec keeps the request.
        import hermes_bootstrap  # noqa: F401
        from agent.auxiliary_client import resolve_provider_client


    def complete(request):
        def _configured(key, env_names):
            value = request.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            import os
            for name in env_names:
                raw = os.environ.get(name) or ''
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            return ''

        system, user = request['system_prompt'], request['user_prompt']
        if not isinstance(system, str) or not isinstance(user, str):
            raise ValueError('invalid_prompt')
        if len((system + user).encode()) > 131072:
            raise ValueError('prompt_too_large')
        # Convert the parent's remaining budget once to a monotonic deadline.
        # Interpreter startup/import have already consumed wall-clock budget.
        budget_started = time.monotonic()
        remaining = request.get('deadline_unix_ms', time.time()*1000+90000)/1000-time.time()
        provider_deadline = budget_started + remaining
        if remaining <= 0:
            raise ValueError('deadline_expired')
        provider = _configured('provider', ('EIMEMORY_LUNA_PROVIDER', 'EIMEMORY_RECALL_PROVIDER'))
        model_name = _configured('model', ('EIMEMORY_LUNA_MODEL', 'EIMEMORY_RECALL_EXPECTED_MODEL'))
        fallback_model = _configured('fallback_model', ('EIMEMORY_LUNA_FALLBACK_MODEL',))
        fallback_provider = _configured('fallback_provider', ('EIMEMORY_LUNA_FALLBACK_PROVIDER',))
        reasoning_effort = _configured('reasoning_effort', ('EIMEMORY_LUNA_REASONING_EFFORT',))
        if not provider or not model_name:
            raise RuntimeError('model_unavailable')
        with _luna_trace.stage('bridge_client_setup_ms'):
            client, model = resolve_provider_client(provider, model=model_name)
        if client is None or model != model_name:
            raise RuntimeError('model_unavailable')
        remaining = provider_deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('deadline_expired')

        def _completion_kwargs(active_model, budget):
            payload = {'model': active_model, 'messages': [
                {'role': 'system', 'content': system}, {'role': 'user', 'content': user}
            ], 'timeout': min(90, budget)}
            if reasoning_effort:
                payload['reasoning_effort'] = reasoning_effort
            return payload

        with _luna_trace.stage('provider_response_ms'):
            try:
                result = client.chat.completions.create(**_completion_kwargs(model, remaining))
                provider_id = provider
            except Exception as exc:
                status = getattr(exc, 'status_code', None)
                if (status != 429 or 'usage_limit_reached' not in str(exc)
                        or not fallback_model or not fallback_provider):
                    raise
                remaining = provider_deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('deadline_expired')
                client, model = resolve_provider_client(fallback_provider, model=fallback_model)
                if client is None or model != fallback_model:
                    raise RuntimeError('model_unavailable')
                result = client.chat.completions.create(**_completion_kwargs(model, remaining))
                provider_id = fallback_provider
        _luna_trace.begin_response_validation()
        if getattr(result, 'model', None) != model:
            raise RuntimeError('response_model_mismatch')
        message = result.choices[0].message
        if getattr(message, 'tool_calls', None):
            raise RuntimeError('unexpected_tool_call')
        text = message.content
        if not isinstance(text, str) or not text.strip():
            raise ValueError('empty_response')
        if request.get('json_mode'):
            json.loads(text)
        return {'text': text, 'model_id': result.model, 'provider_id': provider_id}

    def serve():
        """Persistent JSON-line worker for the eimemory bridge pool.

        Same ``complete`` as the one-shot path (same provider, model, prompts,
        deadline and validation); only interpreter start, Hermes bootstrap/import
        and provider client setup are paid once per worker instead of per call.
        The protocol stream is a private duplicate of fd 1; fd 1 itself and
        Python stdout/stderr are discarded so library output can never corrupt
        or leak into a response. One request at a time; idle and request-count
        bounds make the worker exit on its own.
        """
        import io
        import os
        import re
        import select
        from contextlib import redirect_stderr, redirect_stdout

        global _luna_trace, resolve_provider_client
        protocol = os.fdopen(os.dup(1), 'w', encoding='utf-8')
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
        idle_seconds = 1800.0
        max_requests = 500
        client_ttl_seconds = 120.0
        resolved = {}
        original_resolve = resolve_provider_client

        def cached_resolve(provider_name, model=None):
            key = (provider_name, model)
            hit = resolved.get(key)
            if hit is not None and time.monotonic() - hit[0] < client_ttl_seconds:
                return hit[1]
            value = original_resolve(provider_name, model=model)
            if value and value[0] is not None:
                resolved[key] = (time.monotonic(), value)
            return value

        resolve_provider_client = cached_resolve

        class _Quiet(io.TextIOBase):
            def writable(self):
                return True

            def write(self, text):
                return len(text)

        stdin = sys.stdin.buffer
        for _ in range(max_requests):
            ready, _w, _x = select.select([stdin], [], [], idle_seconds)
            if not ready:
                return 0
            line = stdin.readline(131073 + 256)
            if not line:
                return 0
            request_id = ''
            try:
                request = json.loads(line)
                request_id = request.get('request_id') if isinstance(request, dict) else ''
                if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id):
                    return 1
            except ValueError:
                return 1
            _luna_trace = _LunaTrace(_luna_clock())
            try:
                with redirect_stdout(_Quiet()), redirect_stderr(_Quiet()):
                    result = complete(request)
                result = dict(result)
                result['diagnostics'] = _luna_trace._finish()
                response = {'request_id': request_id, 'result': result}
            except Exception:
                # Credentials may have rotated; never reuse a client after a failure.
                resolved.clear()
                response = {'request_id': request_id, 'error': True,
                            'reason': _luna_trace.failed or 'bridge_failed',
                            'diagnostics': _luna_trace._finish()}
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
            try:
                protocol.write(encoded + '\n')
                protocol.flush()
            except (BrokenPipeError, OSError):
                return 1
        return 0

    if __name__ == '__main__' and _luna_serve:
        sys.exit(serve())
    elif __name__ == '__main__':
        try:
            request = json.loads(sys.stdin.buffer.read(131073))
            print(json.dumps(complete(request)))
        except Exception as error:
            print(type(error).__name__, file=sys.stderr)
            sys.exit(1)
