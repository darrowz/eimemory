"""TEST FIXTURE ONLY, not a copy of the user's production Luna bridge."""
from __future__ import annotations
import json
import sys


def main():
    request = json.load(sys.stdin)
    from agent.auxiliary_client import resolve_provider_client
    client = resolve_provider_client('openai-codex', model='gpt-5.6-luna')
    response = client.chat.completions.create(
        messages=[{'role': 'system', 'content': request['system_prompt']},
                  {'role': 'user', 'content': request['user_prompt']}],
        reasoning_effort='low', model='gpt-5.6-luna', timeout=2.5)
    if response.model != 'gpt-5.6-luna':
        raise ValueError('private_model_mismatch_message')
    message = response.choices[0].message
    if message.tool_calls:
        raise ValueError('private_tool_call_message')
    text = message.content
    if not isinstance(text, str) or not text:
        raise ValueError('private_empty_response_message')
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) != {'selected'} or not isinstance(payload['selected'], list):
        raise ValueError('private_invalid_json_message')
    print(json.dumps({'text': text, 'provider_id': 'openai-codex', 'model_id': response.model}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        sys.stderr.write(type(exc).__name__ + '\n')
        sys.exit(1)
