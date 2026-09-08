import json
import os
from pathlib import Path
import shutil
import subprocess
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
      || !params.disableMessageTool || params.timeout > 8 || params.timeout < 1
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
