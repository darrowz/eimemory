"""Selected run_watch control flow with every effectful operation replaced.
No ledger, service, environment, network or project import is touched.
"""
import ast
import hashlib
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/ops/openclaw_loop.py'
tree = ast.parse(SOURCE.read_text())
names = {'run_watch','_stale_summary','_stale_work_items','_repair_summary'}
nodes = [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
assert {n.name for n in nodes} == names
code = compile(ast.Module(body=nodes,type_ignores=[]), '<EA641-selected-watch-control-flow>', 'exec')

class WatchCounts(unittest.TestCase):
    def run_scenario(self, *, drift_ok, existing):
        writes = []
        repairs = []
        snapshots = iter([[{'task_id':'work','stale_reason':'lease_expired'}],[]])
        def repair(stale, **kwargs):
            created = kwargs.get('existing_task') is None
            repairs.append(created)
            return {'created':created}
        ns=dict(Any=object, Path=Path, hashlib=hashlib,
                WATCH_AUTO_RECONCILE_GRACE_SECONDS=900, WATCH_STALE_SAMPLE_LIMIT=20,
                WATCH_REPAIR_SOURCE='openclaw.loop_watch.repair',
                check_config_drift=lambda **kw:{'ok':drift_ok,'codes':[] if drift_ok else ['synthetic']},
                find_stale_tasks=lambda:next(snapshots),
                _run_watch_repair=repair,
                _active_watch_repair_tasks=lambda:[{'task_id':'existing'}] if existing else [],
                new_id=lambda prefix:prefix+'_synthetic',iso_ts=lambda:'synthetic-time',
                append_jsonl=lambda name,row:writes.append((name,row)),
                create_task=lambda **kw:{'task_id':'finding','reused':False},
                record_action=lambda *a,**kw:None, record_verification=lambda *a,**kw:None,
                finish_task=lambda *a,**kw:None)
        exec(code,ns)
        result=ns['run_watch'](run_live_checks=False)
        self.assertEqual(repairs,[True,False] if existing else [True])
        self.assertEqual(len(writes),1)
        self.assertEqual(result['tasks_created'],1 if drift_ok else 2)
        return result

    def test_success_after_existing_repair_preserves_new_count(self):
        self.run_scenario(drift_ok=True,existing=True)
    def test_failure_after_existing_repair_counts_finding_and_new_repair(self):
        self.run_scenario(drift_ok=False,existing=True)
    def test_no_existing_repair_preserves_existing_behavior(self):
        self.run_scenario(drift_ok=True,existing=False)

if __name__ == '__main__': unittest.main()
