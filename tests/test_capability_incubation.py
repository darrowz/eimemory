from __future__ import annotations

import pytest

import eimemory.capabilities.code_implementation_bootstrap as bootstrap_module
import eimemory.evaluation.hongtu_code_implementation as code_catalog_module
import eimemory.governance.capability_incubation as incubation_module
from eimemory.adapters.hermes.code_implementation import (
    BINDING_ID as CODE_BINDING_ID,
    IMPLEMENTATION_DIGEST as CODE_IMPLEMENTATION_DIGEST,
    OPERATION as CODE_OPERATION,
    PROVIDER_INSTANCE_ID as CODE_PROVIDER_INSTANCE_ID,
    REVISION_ID as CODE_REVISION_ID,
    build_attestation,
)
from eimemory.adapters.runtime.capability import AdapterCapabilityService
from eimemory.api.runtime import Runtime
from eimemory.capabilities.models import CapabilityBinding, CapabilityDefinition, CapabilityRevision
from eimemory.evaluation.capability_catalog import (
    ApplicationCatalogBootstrap,
    CapabilityEvaluationCatalog,
    CatalogCase,
)
from eimemory.evaluation.hongtu_code_implementation import (
    CATALOG_CASE_ID as CODE_CATALOG_CASE_ID,
    install_code_implementation_catalog,
    validate_code_implementation_catalog_receipt,
)
from eimemory.governance.capability_incubation import (
    build_capability_incubation_plan,
    execute_capability_incubation,
)
from eimemory.ops.code_implementation_owner import (
    CODE_IMPLEMENTATION_REFRESH_SERVICE,
    CODE_IMPLEMENTATION_REFRESH_TIMER,
    PRODUCTION_RUNTIME_SCOPE,
    inspect_code_implementation_owner,
    refresh_code_implementation_owner,
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


@pytest.fixture(autouse=True)
def _fixed_incubation_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incubation_module, "now_iso", lambda: FRESH_AT)


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


class _LiveCodeImplementationProvider:
    def __init__(self, *, socket_path: object = None, timeout_seconds: float = 15.0) -> None:
        # Mirror the real socket-client signature; the catalog pass now
        # constructs clients with an explicit bounded completion budget.
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def health(self, *, nonce: str) -> dict[str, object]:
        return {
            "ok": True,
            "operation": "health",
            "nonce": nonce,
            "provider_instance_id": CODE_PROVIDER_INSTANCE_ID,
            "implementation_digest": CODE_IMPLEMENTATION_DIGEST,
        }

    def propose_patch_v2(self, request: dict) -> dict:
        response = {
            "schema": "code_implementation_response.v2",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "file_updates": [
                {
                    "path": request["allowed_files"][0]["path"],
                    "prior_sha256": request["allowed_files"][0]["sha256"],
                    "content": "VALUE = 2\n",
                }
            ],
            "rationale": "bounded catalog fixture repair",
            "assumptions": [],
        }
        return {
            "ok": True,
            "operation": CODE_OPERATION,
            "attestation": build_attestation(
                request,
                response,
                completed_at="2026-08-23T00:10:00Z",
                nonce=request["nonce"],
            ),
            "response": response,
        }


def test_release_refresh_feeds_nightly_exact_v2_incubation_receipts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(
        bootstrap_module,
        "CodeImplementationSocketClient",
        _LiveCodeImplementationProvider,
    )
    monkeypatch.setattr(
        code_catalog_module,
        "CodeImplementationSocketClient",
        _LiveCodeImplementationProvider,
    )
    monkeypatch.setattr(incubation_module, "now_iso", lambda: "2026-08-23T00:10:00Z")

    runtime = Runtime.create(root=authority)
    try:
        runtime.apply_capability_seed_manifest(scope=PRODUCTION_RUNTIME_SCOPE)
        runtime.ensure_default_l5_profile(scope=PRODUCTION_RUNTIME_SCOPE)
    finally:
        runtime.close()
    assert refresh_code_implementation_owner(now="2026-08-23T00:00:00Z")["ok"] is True

    catalog = CapabilityEvaluationCatalog()
    install_code_implementation_catalog(ApplicationCatalogBootstrap(catalog))
    catalog.seal()
    runtime = Runtime.create(root=authority)
    try:
        report = execute_capability_incubation(
            runtime,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            catalog=catalog,
            max_activate=1,
            preflight_passes=2,
            persist_report=False,
        )
        events = runtime.capabilities.list_lifecycle_events(
            entity_type="definition",
            entity_id="code.implementation",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            limit=32,
        )

        def systemctl_runner(args: list[str]) -> str:
            unit = args[args.index("show") + 1]
            if unit == CODE_IMPLEMENTATION_REFRESH_TIMER:
                return "\n".join(
                    (
                        "LoadState=loaded",
                        "ActiveState=active",
                        "SubState=waiting",
                        "UnitFileState=enabled",
                        "Result=success",
                    )
                )
            assert unit == CODE_IMPLEMENTATION_REFRESH_SERVICE
            return "\n".join(
                (
                    "LoadState=loaded",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "UnitFileState=static",
                    "Result=success",
                )
            )

        owner_status = inspect_code_implementation_owner(
            runtime,
            checked_at="2026-08-23T00:30:00Z",
            runner=systemctl_runner,
            kill_switch_path=tmp_path / "code-evolution.disabled",
            automation_policy_path=tmp_path / "code-automation-policy.v2.json",
        )
    finally:
        runtime.close()

    result = next(
        item for item in report["results"] if item["capability_id"] == "code.implementation"
    )
    assert result["result"] == "activated", (result.get("acceptance") or {}).get("dynamic_selection")
    assert result["binding_ids"] == [CODE_BINDING_ID]
    assert result["case_ids"] == [CODE_CATALOG_CASE_ID]
    active = next(event for event in reversed(events) if event["status"] == "active")
    provenance = active["provenance"]
    assert provenance["source"] == "eimemory.capability_incubation"
    assert provenance["binding_ids"] == [CODE_BINDING_ID]
    assert provenance["case_ids"] == [CODE_CATALOG_CASE_ID]
    assert provenance["preflight_passes"] == 2
    assert len(set(provenance["preflight_execution_digests"])) == 2
    assert len(set(provenance["provider_evaluation_receipt_digests"])) == 2
    for receipt, digest in zip(
        provenance["provider_evaluation_receipts"],
        provenance["provider_evaluation_receipt_digests"],
        strict=True,
    ):
        assert validate_code_implementation_catalog_receipt(receipt, receipt_digest=digest)
    assert CODE_REVISION_ID in result["revision_ids"]
    assert owner_status["catalog"]["valid_passes"] == 2
    assert owner_status["catalog"]["ready"] is True
    assert owner_status["provider_reader_ready"] is True
    assert owner_status["ok"] is True


def test_incompatible_provider_upgrade_revalidates_an_active_definition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = tmp_path / "production-authority"
    monkeypatch.setenv("EIMEMORY_ROOT", str(authority))
    monkeypatch.setattr(
        bootstrap_module,
        "CodeImplementationSocketClient",
        _LiveCodeImplementationProvider,
    )
    monkeypatch.setattr(
        code_catalog_module,
        "CodeImplementationSocketClient",
        _LiveCodeImplementationProvider,
    )
    monkeypatch.setattr(incubation_module, "now_iso", lambda: "2026-08-23T00:10:00Z")

    runtime = Runtime.create(root=authority)
    try:
        runtime.apply_capability_seed_manifest(scope=PRODUCTION_RUNTIME_SCOPE)
        runtime.ensure_default_l5_profile(scope=PRODUCTION_RUNTIME_SCOPE)
        definition = next(
            row
            for row in runtime.capabilities.list_definitions(
                runtime_scope=PRODUCTION_RUNTIME_SCOPE,
                capability_scope="global",
                status=None,
                limit=100,
            )
            if row["entity_id"] == "code.implementation"
        )
        runtime.capabilities.transition_status(
            entity_type="definition",
            entity_id="code.implementation",
            entity_digest=definition["entity_digest"],
            target_status="active",
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            expected_state_version=definition["state_version"],
            expected_state_digest=definition["state_digest"],
            effective_at="2026-08-22T00:05:00Z",
            reason="prior protected provider was activated",
            provenance={"source": "test.prior_provider_activation"},
            request_key=(
                "capability-incubation:activate:code.implementation:"
                f"{definition['entity_digest']}"
            ),
        )
    finally:
        runtime.close()

    assert refresh_code_implementation_owner(now="2026-08-23T00:00:00Z")["ok"] is True

    catalog = CapabilityEvaluationCatalog()
    install_code_implementation_catalog(ApplicationCatalogBootstrap(catalog))
    catalog.seal()
    runtime = Runtime.create(root=authority)
    try:
        plan = build_capability_incubation_plan(
            runtime,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            catalog=catalog,
            fresh_at="2026-08-23T00:10:00Z",
        )
        item = next(
            row for row in plan["work_items"]
            if row["capability_id"] == "code.implementation"
        )
        report = execute_capability_incubation(
            runtime,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            catalog=catalog,
            max_activate=1,
            preflight_passes=2,
            persist_report=False,
        )
        result = next(
            row for row in report["results"]
            if row["capability_id"] == "code.implementation"
        )
        repeated_plan = build_capability_incubation_plan(
            runtime,
            runtime_scope=PRODUCTION_RUNTIME_SCOPE,
            capability_scope="global",
            catalog=catalog,
            fresh_at="2026-08-23T00:10:00Z",
        )
    finally:
        runtime.close()

    assert item["status"] == "ready_for_revalidation"
    assert item["binding_ids"] == [CODE_BINDING_ID]
    assert item["case_ids"] == [CODE_CATALOG_CASE_ID]
    assert result["result"] == "revalidated"
    assert all(
        row["capability_id"] != "code.implementation"
        for row in repeated_plan["work_items"]
    )
