from copy import deepcopy
from dataclasses import asdict
import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.test_rule_quarantine import SCHEMA, quarantine_test_rules
from eimemory.knowledge.evidence_contracts import versioned_record_ref


@pytest.fixture
def rules(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="tester", workspace_id="workspace", user_id="owner")
    def create(**kw):
        return runtime.store.append(RecordEnvelope.create(kind="rule", title="Identical rule", summary="same",
            source="evolution.rule", scope=scope, **kw))
    business = create()
    legacy_test = create()
    marked = create(meta={"acceptance_generated": True})
    yield runtime, scope, business, legacy_test, marked
    runtime.close()


def manifest(scope, record):
    return {"schema": SCHEMA, "scope": asdict(scope), "source_id": "default",
            "rules": [{k: versioned_record_ref(record)[k] for k in ("record_id", "version_digest")}]}


def test_preview_only_and_identical_content_never_proves_test_provenance(rules):
    runtime, scope, business, legacy, marked = rules
    assert marked.status == "archived"
    report = quarantine_test_rules(runtime.store, scope=scope)
    assert report["eligible"] == 0
    assert len(report["unclassified"]) == 2
    assert report["manifest_template"]["rules"] == []
    assert runtime.store.get_by_id(business.record_id, scope=scope).status == "active"
    with pytest.raises(ValueError, match="test_rule_cannot_promote"):
        runtime.evolution.promote_rule(record_id=marked.record_id, scope=asdict(scope), promoter="test")
    generated = runtime.evolution.store_rule(title="Deployment fixture", summary="same",
        task_type="memory.recall", retrieval_policy={}, scope=asdict(scope), status="active",
        acceptance_generated=True)
    assert generated.status == "archived"
    from eimemory.governance.promotion.promotion_manager import _apply_memory_rule_candidate
    candidate = RecordEnvelope.create(kind="capability_candidate", title="Test promotion", scope=scope,
        meta={"acceptance_generated": True, "target_capability": "memory.recall"})
    before = len(runtime.evolution.list_rules(scope=asdict(scope)))
    result = _apply_memory_rule_candidate(runtime, candidate, {}, scope=scope)
    assert result["blocked_reason"] == "test_rule_promotion_forbidden"
    assert len(runtime.evolution.list_rules(scope=asdict(scope))) == before


def test_reviewed_manifest_is_atomic_reversible_and_blocks_resurrection(rules):
    runtime, scope, business, legacy, marked = rules
    plan = manifest(scope, legacy)
    assert quarantine_test_rules(runtime.store, scope=scope, manifest=plan)["eligible"] == 1
    assert runtime.store.get_by_id(legacy.record_id, scope=scope).status == "active"
    result = quarantine_test_rules(runtime.store, scope=scope, manifest=plan, apply=True)
    assert result["changed"] == 1
    current = runtime.store.get_by_id(legacy.record_id, scope=scope)
    assert current.status == "deprecated"
    assert runtime.store.get_by_id(business.record_id, scope=scope).status == "active"
    with pytest.raises(ValueError, match="quarantined_test_rule"):
        runtime.store.append(deepcopy(legacy))
    assert quarantine_test_rules(runtime.store, scope=scope)["eligible"] == 0
    assert quarantine_test_rules(runtime.store, scope=scope, revert=True)["eligible"] == 1
    quarantine_test_rules(runtime.store, scope=scope, revert=True, apply=True)
    assert runtime.store.get_by_id(legacy.record_id, scope=scope).to_dict() == legacy.to_dict()
    audits = runtime.store.list_records(kinds=["reflection"], scope=scope)
    assert len([r for r in audits if r.source == "storage.test_rule_quarantine"]) == 2


def test_stale_missing_wrong_scope_and_incomplete_manifest_fail_closed(rules):
    runtime, scope, business, legacy, marked = rules
    plan = manifest(scope, legacy)
    edited = deepcopy(legacy)
    edited.summary = "real change"
    runtime.store.append(edited)
    with pytest.raises(ValueError, match="stale"):
        quarantine_test_rules(runtime.store, scope=scope, manifest=plan, apply=True)
    wrong = {**plan, "scope": {**plan["scope"], "user_id": "wrong"}}
    with pytest.raises(ValueError, match="manifest_invalid"):
        quarantine_test_rules(runtime.store, scope=scope, manifest=wrong, apply=True)
    with pytest.raises(ValueError, match="scan_incomplete"):
        quarantine_test_rules(runtime.store, scope=scope, limit=1, apply=True)
    assert runtime.store.get_by_id(business.record_id, scope=scope).status == "active"
