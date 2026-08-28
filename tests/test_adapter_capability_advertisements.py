from __future__ import annotations

from pathlib import Path

from eimemory.adapters.runtime.capability import (
    IMPLEMENTATION_FINGERPRINT_REVISIONS,
    AdapterCapabilityService,
)
from eimemory.api.runtime import Runtime
from eimemory.capabilities.models import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityRevision,
)


SCOPE = {
    "tenant_id": "tenant-advertisement",
    "agent_id": "agent-advertisement",
    "workspace_id": "workspace-advertisement",
    "user_id": "user-advertisement",
}
STAMP = "2020-08-20T00:00:00+00:00"
FRESH_AT = "2020-08-20T00:05:00+00:00"


def test_code_implementation_fingerprint_policy_preserves_prior_revision() -> None:
    assert {
        "code.implementation:v7",
        "code.implementation:v8",
    } <= IMPLEMENTATION_FINGERPRINT_REVISIONS


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="memory.adapter_protocol",
        display_name="Adapter protocol",
        description="An explicitly advertised adapter protocol capability.",
        owner="adapter-test",
        risk_tier="bounded_read",
        tags=("adapter",),
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )


def _revision(definition: CapabilityDefinition) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id="memory.adapter_protocol:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["declared_provider_operation"],
            "failure_invariants": ["unsupported_host_event_explicit"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_read",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision, *, provider: str) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=f"binding.{provider}.adapter-protocol:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind=provider,
        provider_instance_id=f"{provider}-instance-a",
        implementation_digest=("a" if provider == "codex" else "b") * 64,
        operations=("recall",),
        limits={"max_context_chars": 7200},
        environment_fingerprint={"runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=(f"artifact://{provider}/binding.json",),
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )


def _advertisement_context(
    binding: CapabilityBinding,
    revision: CapabilityRevision,
    *,
    advertisement_id: str = "advertisement.codex.adapter-protocol:v1",
) -> dict:
    return {
        "advertisement_id": advertisement_id,
        "advertisement_revision": "v1",
        "binding_id": binding.binding_id,
        "capability_revision_id": revision.revision_id,
        "provider_instance_id": binding.provider_instance_id,
        "contract_digest": revision.contract_digest,
        "operations": ["recall"],
        "limits": {"max_context_chars": 7200},
        "side_effect_class": "none",
        "host_event_types": ["SessionStart", "Stop"],
        "environment_fingerprint": {
            "hostname": "untrusted-host-a",
            "api_token": "Bearer secret-should-not-persist",
            "runtime_version": "test-v1",
        },
        "applicability": {"channel": "codex"},
        "evidence_refs": ["artifact://codex/advertisement-v1.json"],
        "advertised_at": STAMP,
        "expires_at": "2020-08-20T01:00:00+00:00",
        "created_at": STAMP,
        "capability_scope": "global",
        "provenance": {"source": "adapter-advertisement-test"},
    }


def _registered_runtime(tmp_path: Path, *, provider: str = "codex") -> tuple[Runtime, CapabilityBinding, CapabilityRevision]:
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision, provider=provider)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition")
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE, request_key="revision")
    runtime.capabilities.bind(binding, runtime_scope=SCOPE, request_key=f"binding:{provider}")
    return runtime, binding, revision


def test_advertisement_is_provider_bound_secret_safe_and_fresh(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        service = AdapterCapabilityService(runtime, adapter_id="codex")
        receipt = service.advertise_capabilities(
            _advertisement_context(binding, revision),
            runtime_scope=SCOPE,
            now=STAMP,
        )

        assert receipt["ok"] is True
        rows = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=SCOPE,
            capability_scope="global",
            adapter_id="codex",
            binding_id=binding.binding_id,
            at_time=FRESH_AT,
            fresh_at=FRESH_AT,
        )
        assert len(rows) == 1
        descriptor = rows[0]["descriptor"]
        assert descriptor["capability_revision_id"] == revision.revision_id
        assert descriptor["environment_fingerprint"]["hostname"].startswith("sha256:")
        assert descriptor["environment_fingerprint"]["api_token"] == "[REDACTED]"
        assert "secret-should-not-persist" not in str(descriptor)
        assert rows[0]["freshness"] == {
            "checked_at": "2020-08-20T00:05:00.000000Z",
            "advertised_at": "2020-08-20T00:00:00.000000Z",
            "expires_at": "2020-08-20T01:00:00.000000Z",
            "is_fresh": True,
        }

        health = service.capability_health(
            binding.binding_id,
            runtime_scope=SCOPE,
            at_time=FRESH_AT,
        )
        assert health["readiness"] == "ready"
        assert health["fresh_advertisement_count"] == 1
    finally:
        runtime.close()


def test_advertisement_cannot_expand_operations_or_numeric_limits_beyond_its_binding(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        context = _advertisement_context(
            binding,
            revision,
            advertisement_id="advertisement.codex.operation-escalation:v1",
        )
        context["operations"] = ["remember"]

        rejected = AdapterCapabilityService(runtime, adapter_id="codex").advertise_capabilities(
            context,
            runtime_scope=SCOPE,
            now=STAMP,
        )

        assert rejected["ok"] is False
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == "advertisement_rejected"

        limit_escalation = _advertisement_context(
            binding,
            revision,
            advertisement_id="advertisement.codex.limit-escalation:v1",
        )
        limit_escalation["limits"] = {"max_context_chars": 7201}
        rejected_limit = AdapterCapabilityService(runtime, adapter_id="codex").advertise_capabilities(
            limit_escalation,
            runtime_scope=SCOPE,
            now=STAMP,
        )
        assert rejected_limit["ok"] is False
        assert rejected_limit["reason"] == "advertisement_rejected"
    finally:
        runtime.close()


def test_host_and_version_changes_create_ad_history_without_changing_binding_identity(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        service = AdapterCapabilityService(runtime, adapter_id="codex")
        first_context = _advertisement_context(
            binding,
            revision,
            advertisement_id="advertisement.codex.host-a:v1",
        )
        second_context = _advertisement_context(
            binding,
            revision,
            advertisement_id="advertisement.codex.host-b:v2",
        )
        second_context["advertisement_revision"] = "v2"
        second_context["environment_fingerprint"] = {
            "hostname": "untrusted-host-b",
            "runtime_version": "test-v2",
        }

        assert service.advertise_capabilities(first_context, runtime_scope=SCOPE, now=STAMP)["ok"] is True
        assert service.advertise_capabilities(second_context, runtime_scope=SCOPE, now=STAMP)["ok"] is True
        rows = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=SCOPE,
            capability_scope="global",
            adapter_id="codex",
            binding_id=binding.binding_id,
            fresh_at=FRESH_AT,
        )

        assert {row["descriptor"]["binding_id"] for row in rows} == {binding.binding_id}
        assert {row["descriptor"]["capability_revision_id"] for row in rows} == {revision.revision_id}
        assert {row["entity_id"] for row in rows} == {
            "advertisement.codex.host-a:v1",
            "advertisement.codex.host-b:v2",
        }
        host_hashes = {
            row["descriptor"]["environment_fingerprint"]["hostname"] for row in rows
        }
        assert len(host_hashes) == 2
        assert all(host_hash.startswith("sha256:") for host_hash in host_hashes)
        assert {
            row["descriptor"]["environment_fingerprint"]["runtime_version"] for row in rows
        } == {"test-v1", "test-v2"}
    finally:
        runtime.close()


def test_advertisement_rejects_stale_and_unsigned_when_policy_requires_it(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        context = _advertisement_context(binding, revision)
        context["expires_at"] = "2020-08-20T00:01:00+00:00"
        stale = AdapterCapabilityService(runtime, adapter_id="codex").advertise_capabilities(
            context,
            runtime_scope=SCOPE,
            now=FRESH_AT,
        )
        assert stale["ok"] is False
        assert stale["reason"] == "advertisement_stale"

        backdated = _advertisement_context(
            binding,
            revision,
            advertisement_id="advertisement.codex.backdated:v1",
        )
        backdated["created_at"] = "2020-08-19T00:00:00+00:00"
        rejected_backdate = AdapterCapabilityService(runtime, adapter_id="codex").advertise_capabilities(
            backdated,
            runtime_scope=SCOPE,
            now=STAMP,
        )
        assert rejected_backdate["ok"] is False
        assert rejected_backdate["reason"] == "advertisement_schema_invalid"

        unsigned = AdapterCapabilityService(
            runtime,
            adapter_id="codex",
            require_signature=True,
            signature_verifier=lambda _advertisement: True,
        ).advertise_capabilities(
            _advertisement_context(binding, revision),
            runtime_scope=SCOPE,
            now=STAMP,
        )
        assert unsigned["ok"] is False
        assert unsigned["reason"] == "advertisement_signature_required"

        secret_key = "Bearer secret-must-never-appear-in-a-receipt"
        malformed = AdapterCapabilityService(runtime, adapter_id="codex").advertise_capabilities(
            {secret_key: "untrusted"},
            runtime_scope=SCOPE,
            now=STAMP,
        )
        assert malformed["ok"] is False
        assert malformed["reason"] == "advertisement_schema_invalid"
        assert secret_key not in str(malformed)
    finally:
        runtime.close()


def test_outcomes_require_a_fresh_advertised_binding_and_declared_host_event(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        service = AdapterCapabilityService(runtime, adapter_id="codex")
        service.advertise_capabilities(
            _advertisement_context(binding, revision),
            runtime_scope=SCOPE,
            now=STAMP,
        )
        normalized = service.normalize_capability_outcome(
            {
                "capability_outcome": {
                    "binding_id": binding.binding_id,
                    "capability_revision_id": revision.revision_id,
                    "event_id": "codex-stop-1",
                    "occurred_at": FRESH_AT,
                    "verdict": "pass",
                    "evidence_refs": ["artifact://codex/stop-1.json"],
                    "metrics": {"hostname": "untrusted-host-a"},
                    "summary": "Authorization: Bearer secret-must-not-persist",
                }
            },
            runtime_scope=SCOPE,
            event_type="Stop",
        )
        assert normalized["status"] == "normalized"
        assert normalized["ok"] is True
        assert normalized["diagnostic_metadata"] == {}
        assert normalized["metrics"]["hostname"].startswith("sha256:")
        assert normalized["summary"] == "[REDACTED]"

        unsupported = service.normalize_capability_outcome(
            {"capability_outcome": {"binding_id": binding.binding_id}},
            runtime_scope=SCOPE,
            event_type="UnknownHostEvent",
        )
        assert unsupported["status"] == "unsupported"
        assert unsupported["reason"] == "outcome_schema_invalid"
    finally:
        runtime.close()


def test_advertisement_lifecycle_is_separate_from_binding_history(tmp_path: Path) -> None:
    runtime, binding, revision = _registered_runtime(tmp_path)
    try:
        service = AdapterCapabilityService(runtime, adapter_id="codex")
        accepted = service.advertise_capabilities(
            _advertisement_context(binding, revision),
            runtime_scope=SCOPE,
            now=STAMP,
        )
        rows = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=SCOPE,
            capability_scope="global",
            binding_id=binding.binding_id,
            adapter_id="codex",
            status="active",
            at_time=FRESH_AT,
        )
        advertisement = rows[0]
        transition = runtime.capabilities.transition_status(
            entity_type="advertisement",
            entity_id=accepted["advertisement_id"],
            entity_digest=advertisement["entity_digest"],
            target_status="stale",
            runtime_scope=SCOPE,
            capability_scope="global",
            expected_state_version=advertisement["state_version"],
            expected_state_digest=advertisement["state_digest"],
            effective_at="2020-08-20T00:10:00+00:00",
            reason="host capability declaration expired",
            provenance={"source": "adapter-advertisement-test"},
            request_key="advertisement-stale",
        )
        assert transition.status == "stale"
        lifecycle_view = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=SCOPE,
            capability_scope="global",
            binding_id=binding.binding_id,
            adapter_id="codex",
            status=None,
            at_time="2020-08-20T00:15:00+00:00",
        )
        assert lifecycle_view[0]["status"] == "stale"
        assert lifecycle_view[0]["descriptor"]["status"] == "active"
        assert lifecycle_view[0]["freshness"]["is_fresh"] is False
        health = service.capability_health(
            binding.binding_id,
            runtime_scope=SCOPE,
            at_time="2020-08-20T00:15:00+00:00",
        )
        assert health["readiness"] == "degraded"
        assert health["reason"] == "advertisement_stale_or_inactive"
    finally:
        runtime.close()


def test_code_implementation_v2_advertisement_must_match_binding_fingerprint(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    definition = CapabilityDefinition(
        capability_id="code.implementation",
        display_name="Code implementation",
        description="Strict provider proposal capability.",
        owner="code-evolution",
        risk_tier="bounded_write",
        tags=("code",),
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )
    revision = CapabilityRevision(
        revision_id="code.implementation:v2",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["strict_attestation"],
            "failure_invariants": ["unknown_provider_state"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_write",
            "side_effect_class": "network",
        },
        compatibility="incompatible",
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )
    binding = CapabilityBinding(
        binding_id="binding.hermes.code-implementation:v2",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="hermes",
        provider_instance_id="hermes.eimemory.code-implementation.production",
        implementation_digest="a" * 64,
        operations=("propose_patch_v2",),
        limits={"max_files": 4},
        environment_fingerprint={"implementation_digest": "a" * 64},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://code-implementation/binding.json",),
        provenance={"source": "adapter-advertisement-test"},
        created_at=STAMP,
    )
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="code-definition")
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE, request_key="code-revision")
    runtime.capabilities.bind(binding, runtime_scope=SCOPE, request_key="code-binding")
    context = {
        "advertisement_id": "advertisement.hermes.code-implementation:v2",
        "advertisement_revision": "v2",
        "binding_id": binding.binding_id,
        "capability_revision_id": revision.revision_id,
        "provider_instance_id": binding.provider_instance_id,
        "contract_digest": revision.contract_digest,
        "operations": ["propose_patch_v2"],
        "limits": {"max_files": 4},
        "side_effect_class": "network",
        "host_event_types": ["CodeImplementationProposal"],
        "environment_fingerprint": {"implementation_digest": "b" * 64},
        "applicability": {"scope": "global"},
        "evidence_refs": ["artifact://code-implementation/advertisement.json"],
        "advertised_at": STAMP,
        "expires_at": "2020-08-20T01:00:00+00:00",
        "created_at": STAMP,
        "capability_scope": "global",
        "provenance": {"source": "adapter-advertisement-test"},
    }
    try:
        receipt = AdapterCapabilityService(runtime, adapter_id="hermes", provider_kind="hermes").advertise_capabilities(
            context,
            runtime_scope=SCOPE,
            now=STAMP,
        )
        assert receipt["ok"] is False
        assert receipt["reason"] == "implementation_digest_mismatch"
    finally:
        runtime.close()
