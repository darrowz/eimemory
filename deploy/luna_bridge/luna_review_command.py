"""Tool-free Luna completion using the configured Hermes credential router."""
from time import perf_counter_ns as _luna_clock
_luna_started_ns = _luna_clock()
from luna_observability import BridgeTrace as _LunaTrace
_luna_trace = _LunaTrace(_luna_started_ns)
with _luna_trace.session(active=__name__ == '__main__'):
    import json
    import sys
    import time
    sys.path.insert(0, str(__import__('pathlib').Path.home() / '.hermes' / 'hermes-agent'))
    with _luna_trace.stage('bridge_import_ms'):
        from agent.auxiliary_client import resolve_provider_client


    def complete(request):
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
        provider = str(request.get('provider') or 'openai-codex').strip()
        model_name = str(request.get('model') or 'gpt-5.6-luna').strip()
        fallback_model = str(request.get('fallback_model') or '').strip()
        fallback_provider = str(request.get('fallback_provider') or '').strip()
        with _luna_trace.stage('bridge_client_setup_ms'):
            client, model = resolve_provider_client(provider, model=model_name)
        if client is None or model != model_name:
            raise RuntimeError('model_unavailable')
        remaining = provider_deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('deadline_expired')
        with _luna_trace.stage('provider_response_ms'):
            try:
                result = client.chat.completions.create(model=model, messages=[
                    {'role': 'system', 'content': system}, {'role': 'user', 'content': user}
                ], reasoning_effort='low', timeout=min(90, remaining))
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
                result = client.chat.completions.create(model=model, messages=[
                    {'role': 'system', 'content': system}, {'role': 'user', 'content': user}
                ], timeout=min(90, remaining))
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

    if __name__ == '__main__':
        try:
            request = json.loads(sys.stdin.buffer.read(131073))
            print(json.dumps(complete(request)))
        except Exception as error:
            print(type(error).__name__, file=sys.stderr)
            sys.exit(1)
