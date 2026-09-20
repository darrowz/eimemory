"""Only explicit caller kinds may suppress the active-rule view."""
import pytest
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.mark.parametrize('kinds,expected', [(None, True), (['memory'], False), (['memory', 'rule'], True)])
def test_active_rules_respect_explicit_not_inferred_kinds(tmp_path, kinds, expected):
    runtime = Runtime.create(root=tmp_path)
    try:
        scope = {'agent_id': 'test', 'workspace_id': 'isolated'}
        rule = runtime.store.append(RecordEnvelope.create(
            kind='rule', title='Ground truth behavior: proactive.judgment',
            summary='When a capability is missing, create a concrete plan, replay, gated implementation path, and rollback boundary',
            scope=ScopeRef(**scope), source='probe', status='active'))
        context = {'source_ids': ['default'], 'target_source_id': 'default', 'recall_profile': 'precision'}
        if kinds is not None:
            context['kinds'] = kinds
        bundle = runtime.memory.recall(
            query='capability gap implementation path replay gate rollback concrete plan proactive judgment',
            scope=scope, task_context=context, limit=5)
        assert (rule.record_id in {r.record_id for r in bundle.rules}) is expected
    finally:
        runtime.close()
