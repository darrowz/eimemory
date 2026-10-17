import pytest
from dataclasses import replace
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
    # The live planner must include observations from this same second.
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


def test_missing_case_on_one_binding_does_not_prevent_independent_diagnostic_on_another(registered):
    runtime, profile, original = registered
    definition = _definition()
    revision = _revision(definition)
    missing_id = "binding.dynamic.uncatalogued:v1"
    runtime.capabilities.bind(replace(_binding(definition, revision, binding_id=missing_id),
                                     implementation_digest="b" * 64,
                                     environment_fingerprint={"runtime": "untested"}), runtime_scope=SCOPE)
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="eimemory.eval.dynamic-catalog", revision="v1",
                              handler=lambda *_: {"decision": "traceable", "evidence_count": 1})
    selected_id = _binding(definition, revision).binding_id
    catalog.register_case(replace(original.list_cases()[0], binding_selector={"binding_ids": [selected_id]}))
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert report["created"] == [] and report["status"] == "diagnosed"
    diagnostic = report["diagnostics"][0]
    assert diagnostic["passed"] is True and diagnostic["gap_closed"] is False
    assert diagnostic["evaluated_provider_binding_ids"] == [selected_id]
    assert diagnostic["missing_provider_binding_ids"] == [missing_id]
    assert diagnostic["blocked_reason"] == "profile_selected_evaluation_case_missing"
    saved = runtime.store.get_by_id(diagnostic["record_id"], scope=SCOPE)
    assert saved.content["target_binding_verified"] is True
    assert saved.content["code_changes_authorized"] is False
    after = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert {item["provider_binding_id"] for item in after["work_items"]} == {missing_id}
    assert list_capability_hypotheses(runtime, runtime_scope=SCOPE) == []


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


def test_renamed_agent_projects_acceptance_from_its_exact_registry_partition(registered, monkeypatch):
    import eimemory.identity as identity
    from eimemory.capabilities.projector import CapabilityStateProjector
    from eimemory.capabilities.observations import CapabilityObservations
    from eimemory.capabilities.profiles import CapabilityProfileError
    from eimemory.capabilities.registry import CapabilityRegistry

    runtime, profile, catalog = registered
    canonical = {**SCOPE, "agent_id": "renamed-agent"}
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", canonical["agent_id"])
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", SCOPE["agent_id"])
    before = build_dynamic_capability_evolution_plan(
        runtime, profile_key=profile.profile_key, runtime_scope=canonical, catalog=catalog)
    assert before["work_items"][0]["reason"] == "hypothesis_missing_or_ambiguous"
    report = produce_capability_hypotheses(
        runtime, profile_key=profile.profile_key, runtime_scope=canonical, catalog=catalog)
    assert report["diagnostics"][0]["passed"] and report["diagnostics"][0]["gap_closed"]
    observations = CapabilityObservations(runtime.store)
    assert observations.list(runtime_scope=SCOPE, capability_scope="global")
    assert observations.list(runtime_scope=canonical, capability_scope="global") == []
    projection = CapabilityStateProjector(runtime.store).project(
        profile.profile_key, runtime_scope=canonical, capability_scope="global", persist=True,
        at_time=datetime.now(timezone.utc).isoformat()).to_dict()
    assert projection["ok"] and projection["snapshots"]
    assert build_dynamic_capability_evolution_plan(
        runtime, profile_key=profile.profile_key, runtime_scope=canonical, catalog=catalog)["work_items"] == []
    # A configured agent rename never opens another workspace's partition.
    with pytest.raises(CapabilityProfileError):
        CapabilityStateProjector(runtime.store).project(
            profile.profile_key, runtime_scope={**canonical, "workspace_id": "foreign"}, capability_scope="global")
    # Once a separate canonical registry exists, legacy profile/evidence may
    # not fill missing inputs in that independently owned partition.
    CapabilityRegistry(runtime.store).register_definition(_definition(), runtime_scope=canonical)
    with pytest.raises(CapabilityProfileError):
        CapabilityStateProjector(runtime.store).project(
            profile.profile_key, runtime_scope=canonical, capability_scope="global")


def test_acceptance_closes_gap_as_verified_noop_through_nightly_contract(registered, monkeypatch):
    from eimemory.scheduler.jobs import _run_dynamic_capability_evolution
    from eimemory.scheduler.result_contract import _nightly_step, nightly_result_diagnostics

    runtime, profile, catalog = registered
    runtime.capability_catalog = catalog
    before = runtime.build_dynamic_capability_evolution_plan(profile_key=profile.profile_key, scope=SCOPE)
    assert before["projection_ok"] is False and before["work_items"]
    acceptance = runtime.run_capability_acceptance(
        profile_key=profile.profile_key, scope=SCOPE, runtime_scope=SCOPE, persist=True)
    assert acceptance["ok"] and acceptance["trace_count"] == 1
    after = runtime.build_dynamic_capability_evolution_plan(profile_key=profile.profile_key, scope=SCOPE)
    assert after["ok"] and after["projection_ok"]
    assert after["projection_snapshot_count"] == 1 and after["projection_blocked_count"] == 0
    assert after["work_items"] == []
    monkeypatch.setattr("eimemory.governance.evolution.dynamic_capability_evolution.run_autonomous_evolution",
                        lambda *args, **kwargs: pytest.fail("a complete reliable profile needs no change"))
    execution = runtime.execute_dynamic_capability_evolution(profile_key=profile.profile_key, scope=SCOPE)
    assert execution["ok"] and execution["status"] == "no_dynamic_gaps"
    assert execution["results"] == [] and execution["applied_count"] == 0
    assert execution["evidence_collection"] == {}
    assert execution["profile_projection"]["projection_ok"]
    assert list_capability_hypotheses(runtime, runtime_scope=SCOPE) == []
    monkeypatch.setattr("eimemory.scheduler.jobs._capability_v3_profile_key", lambda: profile.profile_key)
    steps = []
    report = _nightly_step(steps, "dynamic_capability_evolution",
                          lambda: _run_dynamic_capability_evolution(runtime, scope=SCOPE))
    assert report["ok"] and report["status"] == "no_dynamic_gaps"
    assert report["work_item_count"] == report["evaluated_work_item_count"] == report["blocked_work_item_count"] == 0
    diagnostics = nightly_result_diagnostics({"dynamic_capability_evolution": report}, steps)
    assert diagnostics["execution_ok"] and diagnostics["failed_steps"] == [] and diagnostics["evidence_waits"] == []


def test_verified_noop_requires_catalog_and_untruncated_current_evidence(registered):
    from eimemory.core.clock import now_iso
    runtime, profile, catalog = registered
    runtime.capability_catalog = catalog
    assert runtime.run_capability_acceptance(
        profile_key=profile.profile_key, scope=SCOPE, runtime_scope=SCOPE, persist=True)["ok"]
    # Missing selected catalog cases cannot be called a verified no-op even
    # when observations independently satisfy the profile.
    no_catalog = runtime.execute_dynamic_capability_evolution(
        profile_key=profile.profile_key, scope=SCOPE, catalog=CapabilityEvaluationCatalog())
    assert not no_catalog["ok"] and no_catalog["status"] == "blocked"
    truncated = runtime.execute_dynamic_capability_evolution(
        profile_key=profile.profile_key, scope=SCOPE, observation_limit=1)
    assert not truncated["ok"] and truncated["status"] == "blocked"
    assert not truncated["profile_projection"]["projection_ok"]
    binding = runtime.capabilities.binding_context("binding.dynamic.catalog:v1", runtime_scope=SCOPE,
                                                   capability_scope="global")
    runtime.capabilities.transition_status(entity_type="binding", entity_id=binding["entity_id"],
        entity_digest=binding["entity_digest"], target_status="stale", runtime_scope=SCOPE,
        capability_scope="global", expected_state_version=binding["state_version"],
        expected_state_digest=binding["state_digest"], effective_at=now_iso(),
        reason="operator_staled", provenance={"source": "operator"})
    stale = runtime.execute_dynamic_capability_evolution(profile_key=profile.profile_key, scope=SCOPE)
    assert not stale["ok"] and stale["status"] == "blocked"


@pytest.mark.parametrize("projection", [
    {"ok": True, "snapshots": [], "blocked": []},
    {"snapshots": [{"snapshot_id": "unknown"}], "blocked": []},
    {"ok": False, "snapshots": [{"snapshot_id": "failed"}], "blocked": []},
])
def test_incomplete_projection_cannot_certify_no_dynamic_gaps(registered, monkeypatch, projection):
    from types import SimpleNamespace
    runtime, profile, catalog = registered
    monkeypatch.setattr("eimemory.governance.evolution.dynamic_capability_evolution.CapabilityStateProjector.project",
                        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: projection))
    result = runtime.execute_dynamic_capability_evolution(profile_key=profile.profile_key, scope=SCOPE, catalog=catalog)
    assert result["results"] == [] and not result["ok"] and result["status"] == "blocked"


def test_unavailable_catalog_and_empty_profile_never_certify_noop(registered, monkeypatch):
    from eimemory.capabilities.contracts import CapabilityContractError
    from eimemory.evaluation.capability_catalog import CatalogResolutionError
    from eimemory.governance.evolution.dynamic_capability_evolution import DynamicCapabilityEvolutionError

    runtime, profile, _ = registered
    with pytest.raises(CapabilityContractError):
        replace(profile, requirements={})
    runtime.capability_catalog = None
    def unavailable():
        raise CatalogResolutionError("catalog_not_configured")
    monkeypatch.setattr("eimemory.evaluation.capability_catalog.application_capability_catalog", unavailable)
    with pytest.raises(DynamicCapabilityEvolutionError, match="evaluation_catalog_untrusted"):
        runtime.execute_dynamic_capability_evolution(profile_key=profile.profile_key, scope=SCOPE)
