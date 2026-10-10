import pytest
from datetime import datetime, timezone

from eimemory.api.runtime import Runtime
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.governance.capability.hypothesis_producer import produce_capability_hypotheses
from eimemory.governance.capability.capability_hypotheses import list_capability_hypotheses
from eimemory.governance.evolution.dynamic_capability_evolution import build_dynamic_capability_evolution_plan
from test_dynamic_capability_evals import SCOPE, _definition, _revision, _binding, _profile, _catalog


@pytest.fixture
def registered(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(_binding(definition, revision), runtime_scope=SCOPE)
    profile = _profile(definition)
    runtime.capabilities.register_profile(profile, runtime_scope=SCOPE)
    yield runtime, profile, _catalog()
    runtime.close()


def test_missing_link_collects_real_independent_evidence_without_change_authority(registered, monkeypatch):
    runtime, profile, catalog = registered
    before = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert before["work_items"][0]["reason"] == "hypothesis_missing_or_ambiguous"
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert report["created"] == []
    assert report["status"] == "diagnosed"
    diagnostic = report["diagnostics"][0]
    assert diagnostic["passed"] is True
    assert diagnostic["gap_closed"] is True
    record = runtime.store.get_by_id(diagnostic["record_id"], scope=SCOPE)
    assert record.kind == "reflection" and record.status == "archived"
    assert record.content["evaluation"]["trace_count"] == 1
    assert record.content["evaluation"]["results"][0]["evaluation_run_id"]
    assert record.content["target_binding_verified"] is True
    assert record.content["behavior_influence"]["allowed"] is False
    assert not record.content["code_changes_authorized"] and not record.content["certifies_l5"]
    assert list_capability_hypotheses(runtime, runtime_scope=SCOPE) == []
    # Observe at the precise present: the ordinary nightly's seconds cutoff
    # includes these microsecond observations on its subsequent run.
    monkeypatch.setattr("eimemory.capabilities.projector.now_iso",
                        lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    after = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert after["work_items"] == []
    again = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert again["status"] == "no_gaps" and not again["diagnostics"]


def test_forged_or_stale_plan_cannot_trigger_evaluation(registered):
    runtime, profile, catalog = registered
    plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    plan["work_items"][0]["provider_binding_id"] = "forged-binding"
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=plan)
    assert not report["diagnostics"]
    assert report["skipped"][0]["diagnostic"]["reason"] == "diagnostic_gap_not_current"


def test_failed_case_preserves_gap_and_does_not_claim_improvement(registered):
    runtime, profile, original = registered
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="eimemory.eval.dynamic-catalog", revision="v1",
                              handler=lambda *_: {"decision": "wrong", "evidence_count": 0})
    catalog.register_case(original.list_cases()[0])
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert not report["created"]
    assert report["diagnostics"][0]["passed"] is False
    record = runtime.store.get_by_id(report["diagnostics"][0]["record_id"], scope=SCOPE)
    assert record.content["evaluation"]["results"][0]["validator_passed"] is False
    assert report["diagnostics"][0]["gap_closed"] is False
    assert not record.content["certifies_improvement"]
    after = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert after["work_items"]


def test_missing_registered_cases_do_not_generate_evidence(registered):
    runtime, profile, _ = registered
    catalog = CapabilityEvaluationCatalog()
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert not report["diagnostics"] and not report["created"]
    assert report["skipped"][0]["diagnostic"]["reason"] == "profile_has_no_catalog_cases"


def test_untrusted_catalog_payload_cannot_supply_executable_cases(registered):
    runtime, profile, catalog = registered
    plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE,
                                          catalog={"executor": "arbitrary command"}, plan=plan)
    assert not report["diagnostics"] and not report["created"]
    assert report["skipped"][0]["diagnostic"]["reason"] == "diagnostic_unavailable"


def test_nightly_producer_executes_the_registered_diagnostic_path(registered, monkeypatch):
    from eimemory.scheduler.jobs import _run_capability_hypothesis_producer
    runtime, profile, catalog = registered
    monkeypatch.setenv("EIMEMORY_CAPABILITY_PROFILE_KEY", profile.profile_key)
    monkeypatch.setattr("eimemory.scheduler.jobs._capability_v3_profile_key", lambda: profile.profile_key)
    monkeypatch.setattr("eimemory.evaluation.capability_catalog.application_capability_catalog", lambda: catalog)
    report = _run_capability_hypothesis_producer(runtime, scope=SCOPE)
    assert report["status"] == "diagnosed"
    assert report["diagnostics"][0]["passed"] and report["diagnostics"][0]["gap_closed"]
