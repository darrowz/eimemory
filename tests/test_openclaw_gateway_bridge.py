import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
from threading import Thread
import time

import pytest


def test_gateway_bridge_reuses_scoped_client_and_stops_it(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    sdk = tmp_path / 'client.mjs'
    sdk.write_text('''
export class t {
 constructor(opts) {
  this.opts=opts;
  if (opts.url !== 'ws://127.0.0.1:18789' || opts.sharedStateMode !== 'read-only'
      || opts.scopes.join() !== 'operator.write') throw Error('unsafe options');
 }
 start() { queueMicrotask(()=>this.opts.onHelloOk()); }
 async request(method, params, options) {
  if (method !== 'agent' || !params.modelRun || params.promptMode !== 'none'
      || !params.disableMessageTool || params.timeout > 9 || params.timeout < 1
      || params.timeout !== Math.ceil(options.timeoutMs / 1000)
      || params.sessionEffects || !options.expectFinal) throw Error('unsafe inference');
  return {result:{payloads:[{text:'{"selected":[]}'}],meta:{agentMeta:{provider:'xai',model:'grok-4.6'}}}};
 }
 stop() { process.stderr.write('client_stopped'); }
}
''', encoding='utf-8')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'gateway': {'auth': {'token': 'fixture-private-token'}, 'port': 18789}}))
    env = {**os.environ, 'EIMEMORY_OPENCLAW_GATEWAY_MODE': 'client',
        'EIMEMORY_OPENCLAW_GATEWAY_MODULE': str(sdk), 'EIMEMORY_OPENCLAW_GATEWAY_EXPORT': 't',
        'EIMEMORY_OPENCLAW_GATEWAY_CONFIG': str(config)}
    request = json.dumps({'system_prompt': 'policy', 'user_prompt': 'data',
        'deadline_unix_ms': int((time.time()+7)*1000)})
    result = subprocess.run([node, str(Path('eimemory/llm/openclaw_gateway.mjs').resolve())],
        input=request, text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['model_id'] == 'xai/grok-4.6'
    assert result.stderr == 'client_stopped'
    assert 'fixture-private-token' not in result.stdout + result.stderr


@pytest.mark.parametrize('connected,stage', [(False, 'gateway_connect'), (True, 'gateway_response')])
def test_gateway_timeout_reports_phase_without_private_data(tmp_path, connected, stage):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    sdk = tmp_path / 'client.mjs'
    sdk.write_text("await new Promise(resolve=>setTimeout(resolve,500)); "
        "process.stderr.write('gateway_fixture_ready\\n'); "
        'export class t { constructor(opts) {this.opts=opts;} '
        + ('start() {queueMicrotask(()=>this.opts.onHelloOk());} ' if connected else 'start() {} ')
        + 'request() {return new Promise(()=>{});} stop() {} }')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'gateway': {'auth': {'token': 'fixture-private-token'}}}))
    env = {**os.environ, 'EIMEMORY_OPENCLAW_GATEWAY_MODE': 'client',
        'EIMEMORY_OPENCLAW_GATEWAY_MODULE': str(sdk), 'EIMEMORY_OPENCLAW_GATEWAY_EXPORT': 't',
        'EIMEMORY_OPENCLAW_GATEWAY_CONFIG': str(config)}
    # This tests time spent in a gateway phase. Cold Node/SDK startup must not
    # consume its 1.5s request window and exercise the distinct pre-client
    # budget rejection instead. A deliberately slow import verifies the split.
    command = [node, str(Path('eimemory/llm/openclaw_gateway.mjs').resolve())]
    with subprocess.Popen(command, text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        ready = Queue()
        reader = Thread(target=lambda: ready.put(process.stderr.readline()), daemon=True)
        reader.start()
        try:
            assert ready.get(timeout=4) == 'gateway_fixture_ready\n'
            reader.join(timeout=4)
            assert not reader.is_alive()
            stdout, stderr = process.communicate(
                input=json.dumps({'system_prompt': 'private policy', 'user_prompt': 'private query',
                                  'deadline_unix_ms': int((time.time() + 1.5) * 1000)}),
                timeout=4)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=4)
            reader.join(timeout=4)
    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error['reason'] == 'timeout' and error['gateway_stage'] == stage
    assert 900 <= error['gateway_elapsed_ms'] <= 1600
    assert error['verification_session_id'].startswith('eimemory-verification-')
    assert 'private' not in result.stderr


def test_gateway_rejects_insufficient_budget_before_creating_client(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    constructed = tmp_path / 'client-constructed'
    sdk = tmp_path / 'client.mjs'
    sdk.write_text("import {writeFileSync} from 'node:fs'; "
        'export class t { constructor() {writeFileSync('
        + json.dumps(str(constructed)) + ", 'constructed');} "
        "start() {throw Error('private client must not start');} stop() {} }")
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'gateway': {'auth': {'token': 'fixture-private-token'}}}))
    env = {**os.environ, 'EIMEMORY_OPENCLAW_GATEWAY_MODE': 'client',
        'EIMEMORY_OPENCLAW_GATEWAY_MODULE': str(sdk), 'EIMEMORY_OPENCLAW_GATEWAY_EXPORT': 't',
        'EIMEMORY_OPENCLAW_GATEWAY_CONFIG': str(config)}
    result = subprocess.run([node, str(Path('eimemory/llm/openclaw_gateway.mjs').resolve())],
        input=json.dumps({'system_prompt': 'private policy', 'user_prompt': 'private query',
                          'deadline_unix_ms': int((time.time() + 0.5) * 1000)}),
        text=True, capture_output=True, env=env, timeout=4)

    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error['reason'] == 'timeout'
    assert not constructed.exists()
    assert 'gateway_stage' not in error
    assert 'gateway_elapsed_ms' not in error
    assert 'verification_session_id' not in error
    assert 'private' not in result.stdout + result.stderr
