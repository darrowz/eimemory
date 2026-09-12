"""Offline adapter closure counterexamples; product files are never modified."""
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()))
from eimemory.adapters.codex.hook import CodexHookAdapter
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService

class BridgeClient:
    def __init__(self, runtime):
        self.bridge = EIBrainRPCBridge(runtime)
        self.calls = []
    def call_or_bypass(self, method, params):
        result = dict(self.bridge.handle({'method': method, 'params': params}))
        self.calls.append((method, params, result))
        return result

def codex_collision(root):
    runtime = Runtime.create(root=root)
    try:
        client = BridgeClient(runtime)
        scope = {'tenant_id': 'audit', 'agent_id': 'audit', 'workspace_id': 'audit', 'user_id': 'audit'}
        adapter = CodexHookAdapter(client=client, scope=scope)
        for call_id, tool, result in [('call-1', 'Read', 'source read complete'), ('call-2', 'Bash', '2 passed')]:
            adapter.handle('PostToolUse', {'session_id': 'session-1', 'turn_id': 'turn-1',
                'tool_call_id': call_id, 'tool_name': tool, 'tool_input': {'command': call_id}, 'tool_response': result})
        calls = [(params, result['result']) for method, params, result in client.calls if method == 'adapter.sync_turn']
        assert len(calls) == 2
        assert calls[0][0]['assistant_text'] != calls[1][0]['assistant_text']
        assert calls[0][1]['record']['record_id'] == calls[1][1]['record']['record_id']
        assert calls[1][1]['idempotent'] is True
        print(json.dumps({'finding': 'codex_distinct_tool_calls_collide', 'rpc_turn_ids': [p['turn_id'] for p, _ in calls],
            'same_record_id': True, 'second_idempotent': True}))
    finally:
        runtime.close()

def hermes_collision(root):
    runtime = Runtime.create(root=root)
    runtime.proactive = ProactiveRecallService(runtime, release_identity={
        'release_commit': 'a' * 40, 'release_version': 'audit', 'deployment_receipt_id': 'audit-receipt',
        'release_session_id': 'audit-release'}, control_percent=0)
    client = BridgeClient(runtime)
    provider = HermesMemoryProviderCore(client=client)
    try:
        provider.initialize('write-session', agent_workspace='audit', user_id='audit', agent_context='primary')
        provider.on_memory_write('add', 'memory', 'Hermes project Borealis requires primary-source citations.', {'event_id': 'borealis-write'})
        provider.shutdown()
        provider.initialize('recall-session', agent_workspace='audit', user_id='audit', agent_context='primary')
        query = 'What does project Borealis require?'
        contexts = []
        for host_turn in ['host-turn-1', 'host-turn-2']:
            context = provider.prefetch(query, session_id='recall-session')
            contexts.append(context)
            citation = re.search(r'pm:[0-9a-f]{20}', context)
            assert citation is not None
            provider.on_pre_llm_call(user_message=query, session_id='recall-session', turn_id=host_turn)
            answer = 'No citation used this turn.' if host_turn.endswith('1') else 'It requires citations [' + citation.group(0) + ']'
            provider.on_post_llm_call(user_message=query, assistant_message=answer,
                session_id='recall-session', turn_id=host_turn)
        recalls = [r['result'] for m, _, r in client.calls if m == 'adapter.proactive_prefetch']
        acknowledgements = [r['result'] for m, _, r in client.calls if m == 'adapter.proactive_ack']
        terminals = [r['result'] for m, _, r in client.calls if m == 'adapter.proactive_terminal']
        assert recalls[0]['decision_id'] == recalls[1]['decision_id']
        assert recalls[1]['idempotent'] is True
        assert acknowledgements[0]['changed'] == 1 and acknowledgements[1]['changed'] == 0
        assert terminals[0]['terminal_changed'] == 1
        assert terminals[1]['feedback_changed'] == 0 and terminals[1]['terminal_changed'] == 0
        print(json.dumps({'finding': 'hermes_new_host_turn_reuses_completed_decision', 'same_decision_id': True,
            'second_idempotent': True, 'both_contexts_nonempty': all(contexts),
            'ack_changed': [r['changed'] for r in acknowledgements], 'terminal_results': terminals}))
    finally:
        provider.shutdown()
        runtime.close()

with TemporaryDirectory(prefix='eimemory-adapter-audit-') as work:
    codex_collision(Path(work) / 'codex')
    hermes_collision(Path(work) / 'hermes')
