"""Isolated, configured-default Hermes library completion; no CLI chat route.

The child is deliberately tied to the installed Hermes API. Missing APIs or
failed no-tool/privacy checks are unavailable, never a fallback to safe-mode.
"""
import json
import os
from pathlib import Path
import shutil


def runtime_command():
    from eimemory.llm.command_client import run_bounded_command
    binary = os.environ.get('EIMEMORY_HERMES_BIN') or shutil.which('hermes')
    if not binary:
        raise RuntimeError('hermes_unavailable')
    code, out, _ = run_bounded_command(
        [binary, '--print-runtime-command', '--module', 'runpy'],
        b'', timeout_seconds=10)
    argv = json.loads(out) if code == 0 else None
    entry = "runpy.run_module('runpy', run_name='__main__', alter_sys=True)"
    if (not isinstance(argv, list) or len(argv) != 4
            or not all(isinstance(s, str) and s for s in argv)
            or argv[1:3] != ['-I', '-c'] or not argv[3].endswith(entry)):
        raise RuntimeError('hermes_runtime_unavailable')
    # Preserve the installation's interpreter, root, home and dependency lease.
    # Fail closed on a changed launcher contract; never import via cwd/PYTHONPATH.
    script = str(Path(__file__).resolve())
    argv[3] = argv[3][:-len(entry)] + f"runpy.run_path({script!r}, run_name='__main__')"
    return argv


def complete(system, user):
    from eimemory.llm.command_client import run_bounded_command
    code, out, _ = run_bounded_command(
        runtime_command(), json.dumps({'system': system, 'user': user}).encode(), timeout_seconds=90)
    value = json.loads(out) if code == 0 else None
    if (not isinstance(value, dict) or set(value) != {'text', 'tools'} or value['tools'] != []
            or not isinstance(value['text'], str) or not 0 < len(value['text']) <= 8192):
        raise RuntimeError('tool_free_proof_failed')
    return value['text']


def configured_runtime():
    # AIAgent(model='') does not adopt the router's resolved model or wire mode.
    # Resolve the configured default explicitly, as Hermes' own CLI does.
    from hermes_cli.runtime_provider import _get_model_config, resolve_runtime_provider
    config = _get_model_config()
    model = config.get('default')
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError('configured_model_unavailable')
    runtime = resolve_runtime_provider(requested=config.get('provider'), target_model=model)
    return dict(model=model, **{key: runtime.get(key) for key in (
        'api_key', 'base_url', 'provider', 'requested_provider', 'api_mode', 'credential_pool')})


def run_agent(agent_class, get_tool_definitions, system, user, *, runtime, observe=lambda _: None):
    """Check real assembly, block execution before it happens, inspect all history."""
    observe('definitions')
    selection = dict(enabled_toolsets=[], disabled_toolsets=['*'], quiet_mode=True)
    if get_tool_definitions(**selection) != []:
        raise RuntimeError('tool_free_proof_failed')
    observe('initialize')
    agent = agent_class(**runtime, **selection, skip_memory=True, skip_context_files=True,
                        save_trajectories=False, max_iterations=1, skip_background_review=True)
    attempted = []
    def deny_tools(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError('tool_execution_rejected')
    try:
        if agent.tools != []:
            raise RuntimeError('tool_free_proof_failed')
        observe('tools_empty')
        protocol = getattr(agent, 'api_mode', None)
        observe(protocol if protocol in ('chat_completions', 'codex_responses',
                'anthropic_messages', 'bedrock_converse', 'codex_app_server', 'acp')
                else 'protocol_other')
        agent._execute_tool_calls = deny_tools
        agent._persist_disabled = True
        observe('conversation')
        result = agent.run_conversation(user, system_message=system)
        observe('result_dict' if isinstance(result, dict) else 'result_other')
        if isinstance(result, dict):
            observe('result_failed' if result.get('failed') is True else 'result_not_failed')
            observe('result_completed' if result.get('completed') is True else 'result_incomplete')
        def has_tools(value):
            if isinstance(value, dict):
                return (bool(value.get('tool_calls')) or value.get('role') == 'tool'
                        or value.get('type') in ('tool_use', 'function_call')
                        or any(has_tools(v) for v in value.values()))
            return isinstance(value, list) and any(has_tools(v) for v in value)
        if (attempted or agent.tools != [] or not isinstance(result, dict) or has_tools(result)
                or result.get('completed') is not True or result.get('failed') is not False
                or not isinstance(result.get('messages'), list) or not result['messages']
                or not isinstance(result.get('final_response'), str)):
            raise RuntimeError('tool_free_proof_failed')
        observe('validated')
        return result['final_response']
    finally:
        agent.close()


def _child():
    # Only this short-lived process is modified. No monkeypatches leak into the
    # release worker, and neither stdout nor stderr from Hermes is persisted.
    import logging
    import sys
    sys.dont_write_bytecode = True
    output = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    logging.disable(logging.CRITICAL)

    probe = False
    stage = 'request'
    blocked = set()
    def observe(value):
        nonlocal stage
        stage = value
        if probe:
            os.write(output, (json.dumps({'stage': value}) + '\n').encode())

    def readonly(event, args):
        if event == 'open':
            _, mode, flags = args
            if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                blocked.add('write')
                raise PermissionError('private_completion_readonly')
        if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.exec', 'os.forkpty',
                     'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.truncate',
                     'os.link', 'os.symlink', 'sqlite3.connect'):
            if event == 'sqlite3.connect':
                blocked.add('database')
            elif event in ('os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.truncate',
                           'os.link', 'os.symlink'):
                blocked.add('filesystem')
            else:
                blocked.add('process')
            raise PermissionError('private_completion_readonly')
    sys.addaudithook(readonly)
    try:
        request = json.loads(sys.stdin.buffer.read(2_000_001))
        probe = request == {'synthetic_probe': True}
        if probe:
            request = {'system': 'Return only JSON with one boolean field ok.',
                       'user': 'Return {"ok":true}.'}
        observe('imports')
        import hermes_logging
        hermes_logging.setup_logging = lambda *a, **k: None
        from hermes_cli import plugins, lifecycle
        plugins.discover_plugins = lambda *a, **k: None
        lifecycle._observe = lambda *a, **k: None
        lifecycle.has_hook = lambda *a, **k: False
        lifecycle.invoke_hook = lambda *a, **k: []
        from run_agent import AIAgent
        from model_tools import get_tool_definitions
        if request == {'check_tools': True}:
            definitions = get_tool_definitions(enabled_toolsets=[], disabled_toolsets=['*'], quiet_mode=True)
            if definitions != []:
                raise ValueError('tool_free_proof_failed')
            os.write(output, b'{"tools": []}')
            return 0
        observe('resolve_runtime')
        runtime = configured_runtime()
        text = run_agent(AIAgent, get_tool_definitions, request['system'], request['user'],
                         runtime=runtime, observe=observe)
        if not text or len(text) > 8192:
            raise ValueError('invalid_response')
        if probe:
            value = json.loads(text)
            ok = isinstance(value, dict) and set(value) == {'ok'} and value['ok'] is True
            observe('synthetic_ok' if ok else 'synthetic_mismatch')
            os.write(output, (json.dumps({'blocked': sorted(blocked)}) + '\n').encode())
            return 0 if ok else 1
        else:
            os.write(output, json.dumps({'text': text, 'tools': []}).encode())
        return 0
    except BaseException as exc:
        if probe:
            category = ('readonly' if isinstance(exc, PermissionError) else
                        'api_contract' if isinstance(exc, (TypeError, AttributeError)) else
                        'unavailable')
            os.write(output, (json.dumps({'stage': stage, 'failure': category,
                                         'blocked': sorted(blocked)}) + '\n').encode())
        # Never serialize exceptions, queries, history, or unvalidated provider output.
        return 1
    finally:
        os.close(output)


if __name__ == '__main__':
    raise SystemExit(_child())
