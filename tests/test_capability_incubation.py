from __future__ import annotations

from eimemory.adapters.runtime.capability import AdapterCapabilityService
from eimemory.api.runtime import Runtime
from eimemory.capabilities.models import CapabilityBinding, CapabilityDefinition, CapabilityRevision
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog, CatalogCase
from eimemory.governance.capability_incubation import (
    build_capability_incubation_plan,
    execute_capability_incubation,
)
from eimemory.scheduler.jobs import _run_capability_incubation


SCOPE = {
    "tenant_id": "tenant-incubation",
    "agent_id": "agent-incubation",
    "workspace_id": "workspace-incubation",
    "user_id": "user-incubation",
}
CAPABILITY_SCOPE = "global"
STAMP = "2026-08-22T00:00:00+00:00"
FRESH_AT = "2026-08-22T00:05:00+00:00"
EXPIRES = "2026-08-24T00:00:00+00:00"


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="office.incubation_probe",
        display_name="Office incubation probe",
        description="A discovered capability used to verify evidence-gated incubation.",
        owner="incubation-tests",
        created_at=STAMP,
        status="discovered",
        scope=CAPABILITY_SCOPE,
        risk_tier="bounded_read",
        tags=("incubation",),
        provenance={"source": "test"},
    )


def _revision(definition: CapabilityDefinition) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id="office.incubation_probe:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["decision_is_traceable"],
            "failure_invariants": ["failure_is_explicit"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_read",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=STAMP,
        status="active",
        scope=CAPABILITY_SCOPE,
        provenance={"source": "test"},
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id="binding.hermes.office-incubation:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="hermes",
        provider_instance_id="hermes-incubation-test",
        implementation_digest="a" * 64,
        operations=("inspect",),
        limits={"max_results": 8},
        environment_fingerprint={"runtime": "test"},
        created_at=STAMP,
        advertised_at=STAMP,
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://incubation/provider.py",),
        provenance={"source": "test"},
    )


def _catalog(*, passes: bool = True) -> CapabilityEvaluationCatalog:
    catalog = CapabilityEvaluationCatalog()
    registration = catalog.register_executor(
        executor_id="incubation.eval.office",
        revision="v1",
        handler=lambda _input, _fixture, _runtime: {"decision": "traceable" if passes else "blocked"},
    )
    catalog.register_case(
        CatalogCase(
            case_id="incubation_office_contract_v1",
            capability_id="office.incubation_probe",
            executor_id=registration.executor_id,
            executor_revision=registration.revision,
            executor_contract_digest=registration.contract_digest,
            input_data={"request": "inspect"},
            fixture={"fixture_id": "incubation-v1"},
            expected_invariants=[{"field": "decision", "op": "eq", "value": "traceable"}],
            binding_selector={"binding_ids": ["binding.hermes.office-incubation:v1"]},
        )
    )
    return catalog.seal()


def _runtime(tmp_path, *, with_binding: bool = True, with_advertisement: bool = True) -> Runtime:
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition")
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE, request_key="revision")
    runtime.ensure_default_l5_profile(scope=SCOPE, capability_scope=CAPABILITY_SCOPE)
    if with_binding:
        binding = _binding(definition, revision)
        runtime.capabilities.bind(binding, runtime_scope=SCOPE, request_key="binding")
        if with_advertisement:
            receipt = AdapterCapabilityService(runtime, adapter_id="hermes", provider_kind="hermes").advertise_capabilities(
                {
                    "advertisement_id": "advertisement.hermes.office-incubation:v1",
                    "advertisement_revision": "v1",
                    "binding_id": binding.binding_id,
                    "capability_revision_id": revision.revision_id,
                    "provider_instance_id": binding.provider_instance_id,
                    "contract_digest": revision.contract_digest,
                    "operations": ["inspect"],
                    "limits": {"max_results": 8},
                    "side_effect_class": "none",
                    "host_event_types": ["SessionStart", "Stop"],
                    "environment_fingerprint": {"runtime": "test"},
                    "applicability": {"scope": "global"},
                    "evidence_refs": ["artifact://incubation/provider.py"],
                    "advertised_at": STAMP,
                    "expires_at": EXPIRES,
                    "created_at": STAMP,
                    "capability_scope": CAPABILITY_SCOPE,
                    "provenance": {"source": "test"},
                },
                runtime_scope=SCOPE,
                now=STAMP,
            )
            assert receipt["ok"] is True
    return runtime


def test_plan_exposes_discovered_definition_outside_active_profile(tmp_path) -> None:
    runtime = _runtime(tmp_path, with_binding=False)
    try:
        plan = build_capability_incubation_plan(
            runtime,
            runtime_scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            catalog=_catalog(),
            fresh_at=FRESH_AT,
        )
    finally:
        runtime.close()

    assert plan["discovered_count"] == 1
    assert plan["work_items"][0]["capability_id"] == "office.incubation_probe"
    assert plan["work_items"][0]["status"] == "blocked"
    assert "active_provider_binding_missing" in plan["work_items"][0]["reasons"]


def test_plan_blocks_stale_or_missing_advertisement(tmp_path) -> None:
    runtime = _runtime(tmp_path, with_advertisement=False)
    try:
        plan = build_capability_incubation_plan(
            runtime,
            runtime_scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            catalog=_catalog(),
            fresh_at=FRESH_AT,
        )
    finally:
        runtime.close()

    item = plan["work_items"][0]
    assert item["status"] == "blocked"
    assert item["reasons"] == ["fresh_advertised_catalog_target_missing"]


def test_complete_prerequisites_activate_and_enter_normal_acceptance(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    try:
        report = execute_capability_incubation(
            runtime,
            runtime_scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            catalog=_catalog(),
            max_activate=1,
            preflight_passes=2,
        )
        current = next(
            row
            for row in runtime.capabilities.list_definitions(
                runtime_scope=SCOPE,
                capability_scope=CAPABILITY_SCOPE,
                status=None,
                limit=10,
            )
            if row["entity_id"] == "office.incubation_probe"
        )
        observation_count = runtime.store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM capability_observations WHERE capability_id='office.incubation_probe'"
        ).fetchone()[0]
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["activated_count"] == 1
    assert report["results"][0]["result"] == "activated"
    assert len(report["results"][0]["preflight"]["results"][0]["passes"]) == 2
    assert current["status"] == "active"
    assert observation_count == 1


def test_failed_preflight_never_activates_definition(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    try:
        report = execute_capability_incubation(
            runtime,
            runtime_scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            catalog=_catalog(passes=False),
            max_activate=1,
            preflight_passes=2,
        )
        current = next(
            row
            for row in runtime.capabilities.list_definitions(
                runtime_scope=SCOPE,
                capability_scope=CAPABILITY_SCOPE,
                status=None,
                limit=10,
            )
            if row["entity_id"] == "office.incubation_probe"
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["activated_count"] == 0
    assert report["results"][0]["result"] == "preflight_failed"
    assert current["status"] == "discovered"


def test_nightly_wrapper_executes_bounded_incubation(monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_L5_V3_PROFILE", "l5.default")
    calls = []

    class FakeRuntime:
        def execute_capability_incubation(self, **kwargs):
            calls.append(kwargs)
            return {
                "schema": "capability.incubation.v1",
                "ok": True,
                "status": "waiting",
                "discovered_count": 10,
                "activated_count": 0,
                "results": [],
            }

    report = _run_capability_incubation(FakeRuntime(), scope=SCOPE)  # type: ignore[arg-type]

    assert report["ok"] is True
    assert report["enabled"] is True
    assert report["discovered_count"] == 10
    assert calls == [
        {
            "scope": SCOPE,
            "capability_scope": "global",
            "max_candidates": 100,
            "max_activate": 3,
            "preflight_passes": 2,
            "persist_report": True,
        }
    ]
