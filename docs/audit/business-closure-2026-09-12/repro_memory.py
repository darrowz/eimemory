"""Read-only product audit: synthetic stores only. Run from the repository root."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import subprocess
import sys

REPO = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
sys.path.insert(0, str(REPO))

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.models.records import ScopeRef

SCOPE = {'tenant_id': 'audit', 'agent_id': 'agent', 'workspace_id': 'ws', 'user_id': 'alice'}


def rpc(bridge, method, **params):
    return bridge.handle({'method': method, 'params': params})


def default_title_collision():
    with TemporaryDirectory(prefix='eimemory-audit-title-') as root:
        with closing(Runtime.create(root=root)) as runtime:
            bridge = EIBrainRPCBridge(runtime)
            common = dict(channel='codex', scope=SCOPE, force_capture=True)
            first = rpc(bridge, 'adapter.remember', **common, event_id='fact-1',
                        text='The project uses PostgreSQL as its primary application database.')
            assert first['ok'] is True
            rid = first['result']['record']['record_id']
            scope = resolve_channel_scope('codex', SCOPE)
            query = 'PostgreSQL primary application database'
            before = runtime.memory.recall(query=query, scope=scope, limit=10)
            before_found = rid in {r.record_id for r in before.items}
            second = rpc(bridge, 'adapter.remember', **common, event_id='fact-2',
                         text='Production deployments require explicit approval from the release manager.')
            assert second['ok'] is True
        with closing(Runtime.create(root=root)) as reopened:
            record = reopened.store.get_by_exact_ref(rid, scope=ScopeRef.from_dict(scope), source_id='codex')
            after = reopened.memory.recall(query=query, scope=scope, limit=10)
            result = {'before_recalled': before_found, 'after_recalled': rid in {r.record_id for r in after.items},
                      'first_status_after_restart': record.status,
                      'same_default_title': first['result']['record']['title'] == second['result']['record']['title']}
            assert result == {'before_recalled': True, 'after_recalled': False,
                              'first_status_after_restart': 'superseded', 'same_default_title': True}
            return result


def shared_scope_mutation():
    with TemporaryDirectory(prefix='eimemory-audit-shared-') as root:
        shared_scope = {**SCOPE, 'user_id': ''}
        with closing(Runtime.create(root=root)) as runtime:
            bridge = EIBrainRPCBridge(runtime)
            common = dict(channel='hermes', target='memory', source_id='hermes',
                          provenance={'write_origin': 'hermes.memory_write'})
            content = 'Shared rule: deploy only after passing the integration tests.'
            added = rpc(bridge, 'adapter.mutate_memory', **common, scope=shared_scope, action='add',
                        content=content, idempotency_key='shared-add')
            assert added['ok'] is True
            rid = added['result']['record']['record_id']
            removed = rpc(bridge, 'adapter.mutate_memory', **common, scope=SCOPE, action='remove', content='',
                          target_record_id=rid, expected_revision=added['result']['content_revision'],
                          idempotency_key='alice-remove-shared')
        with closing(Runtime.create(root=root)) as reopened:
            record = reopened.store.get_by_exact_ref(rid,
                scope=ScopeRef.from_dict(resolve_channel_scope('hermes', shared_scope)), source_id='hermes')
            result = {'rpc_ok': removed['ok'], 'requested_user_id': SCOPE['user_id'],
                      'mutated_user_id': record.scope.user_id, 'status_after_restart': record.status}
            assert result == {'rpc_ok': True, 'requested_user_id': 'alice',
                              'mutated_user_id': '', 'status_after_restart': 'removed'}
            return result


def queue_crash_recovery():
    with TemporaryDirectory(prefix='eimemory-audit-queue-') as root:
        path = Path(root) / 'queue.json'
        queue = L1ExtractQueue(path)
        original = queue.enqueue({'episode_id': 'synthetic-episode'})
        code = ('import os,sys; from pathlib import Path; '
                'from eimemory.knowledge.l1_queue import L1ExtractQueue; '
                'L1ExtractQueue(Path(sys.argv[1])).drain_report(lambda job: os._exit(17), limit=1)')
        crashed = subprocess.run([sys.executable, '-c', code, str(path)], cwd=str(REPO))
        restarted = L1ExtractQueue(path)
        handled = []
        report = restarted.drain_report(lambda job: handled.append(job), limit=5)
        retry = restarted.enqueue({'episode_id': 'synthetic-episode'})
        result = {'worker_exit': crashed.returncode, 'restart_report': report,
                  'handler_calls': len(handled), 'reenqueue_same_job': retry['job_id'] == original['job_id'],
                  'job_status': retry['status']}
        assert crashed.returncode == 17 and report['processed'] == 0 and report['pending'] == 1
        assert not handled and retry['job_id'] == original['job_id'] and retry['status'] == 'running'
        return result


if __name__ == '__main__':
    print(json.dumps({'default_title_collision': default_title_collision(),
                      'shared_scope_mutation': shared_scope_mutation(),
                      'queue_crash_recovery': queue_crash_recovery()}, indent=2))
