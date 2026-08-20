from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRevision,
)
from eimemory.capabilities.profiles import CapabilityProfileError, CapabilityProfiles
from eimemory.capabilities.registry import CapabilityRegistry
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


RUNTIME_SCOPE = ScopeRef(
    tenant_id="tenant-profile",
    agent_id="agent-profile",
    workspace_id="workspace-profile",
    user_id="user-profile",
)
CAPABILITY_SCOPE = "global"
STAMP = "2020-08-20T00:00:00+00:00"


def _definition(
    capability_id: str,
    *,
    tags: tuple[str, ...] = ("planning",),
    created_at: str = STAMP,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        display_name=capability_id.replace(".", " ").title(),
        description=f"A dynamic capability for {capability_id}.",
        owner="profile-tests",
        risk_tier="bounded_read",
        tags=tags,
        provenance={"source": "test"},
        created_at=created_at,
        scope=CAPABILITY_SCOPE,
    )


def _revision(definition: CapabilityDefinition, *, created_at: str = STAMP) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=f"{definition.capability_id}:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["outcome_is_recorded"],
            "failure_invariants": ["unsupported_input_is_reported"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_read",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        provenance={"source": "test"},
        created_at=created_at,
        scope=CAPABILITY_SCOPE,
    )


def _binding(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    *,
    created_at: str = STAMP,
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=f"binding.{definition.capability_id}:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="profile-runtime",
        implementation_digest="a" * 64,
        operations=("inspect", "plan"),
        limits={"max_items": 32},
        environment_fingerprint={"runtime": "isolated"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=(f"artifact://advertisements/{definition.capability_id}.json",),
        provenance={"source": "test"},
        created_at=created_at,
        scope=CAPABILITY_SCOPE,
    )


def _register_capability(store: RuntimeStore, definition: CapabilityDefinition) -> None:
    revision = _revision(definition, created_at=definition.created_at)
    binding = _binding(definition, revision, created_at=definition.created_at)
    store.mutate_capabilities_atomically(
        lambda repository: (
            repository.register_definition(definition, scope=RUNTIME_SCOPE),
            repository.register_revision(revision, scope=RUNTIME_SCOPE),
            repository.register_binding(binding, scope=RUNTIME_SCOPE),
        )
    )


def _profile(
    profile_id: str,
    requirements: dict[str, Any],
    *,
    profile_key: str = "profile.dynamic",
    revision: str = "r1",
    created_at: str = STAMP,
) -> CapabilityProfile:
    return CapabilityProfile(
        profile_id=profile_id,
        profile_key=profile_key,
        requirements=requirements,
        provenance={"source": "test"},
        created_at=created_at,
        scope=CAPABILITY_SCOPE,
        revision=revision,
    )


def _requirement_by_capability(result: dict[str, Any], capability_id: str) -> dict[str, Any]:
    return next(item for item in result["requirements"] if item["capability_id"] == capability_id)


def _contains_exact_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_exact_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_key(item, key) for item in value)
    return False


def test_profile_selector_expands_new_tagged_capability_without_source_edit(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    profiles = CapabilityProfiles(store)
    try:
        _register_capability(store, _definition("planning.alpha"))
        profiles.register(
            _profile(
                "profile.dynamic:r1",
                {
                    "planning-selector": {
                        "selector": {"tags_all": ["planning"]},
                        "minimum_maturity": "evaluated",
                        "min_pass_rate": 0.8,
                    }
                },
            ),
            runtime_scope=RUNTIME_SCOPE,
        )

        first = profiles.resolve(
            "profile.dynamic",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            max_candidates=8,
        )
        assert [item["capability_id"] for item in first["requirements"]] == ["planning.alpha"]
        assert _requirement_by_capability(first, "planning.alpha")["selection"]["kind"] == "selector"

        _register_capability(store, _definition("planning.beta"))
        second = profiles.resolve(
            "profile.dynamic",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            max_candidates=8,
        )
        assert [item["capability_id"] for item in second["requirements"]] == [
            "planning.alpha",
            "planning.beta",
        ]
        assert second["resolution_digest"] != first["resolution_digest"]
        assert second["registry_watermark"] != first["registry_watermark"]
        assert second["lifecycle_watermark"] != first["lifecycle_watermark"]
    finally:
        store.close()


def test_profile_exact_requirement_precedes_matching_selector(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    profiles = CapabilityProfiles(store)
    try:
        _register_capability(store, _definition("planning.exact"))
        profiles.register(
            _profile(
                "profile.dynamic:r1",
                {
                    "planning.exact": {
                        "minimum_maturity": "reliable",
                        "min_pass_rate": 0.95,
                    },
                    "broad-planning": {
                        "selector": {"tags_all": ["planning"]},
                        "minimum_maturity": "observed",
                        "min_pass_rate": 0.1,
                        "priority": 999,
                    },
                },
            ),
            runtime_scope=RUNTIME_SCOPE,
        )

        result = profiles.resolve(
            "profile.dynamic",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
        resolved = _requirement_by_capability(result, "planning.exact")
        assert resolved["selection"] == {
            "kind": "exact",
            "rule_ids": ["planning.exact"],
            "priority": None,
            "matched_revision_ids": ["planning.exact:v1"],
            "matched_binding_ids": ["binding.planning.exact:v1"],
        }
        assert resolved["requirement"] == {
            "minimum_maturity": "reliable",
            "min_pass_rate": 0.95,
        }
    finally:
        store.close()


def test_provider_selector_narrows_revisions_to_the_matching_bindings(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    profiles = CapabilityProfiles(store)
    try:
        definition = _definition("planning.provider_bound")
        revision_one = _revision(definition)
        revision_two_contract = dict(revision_one.contract)
        revision_two_contract["success_invariants"] = [
            "outcome_is_recorded",
            "second_revision_selected",
        ]
        revision_two = replace(
            revision_one,
            revision_id="planning.provider_bound:v2",
            contract=revision_two_contract,
        )
        module_binding = _binding(definition, revision_one)
        hermes_binding = replace(
            module_binding,
            binding_id="binding.planning.provider_bound:hermes-v2",
            capability_revision_id=revision_two.revision_id,
            provider_kind="hermes",
            provider_instance_id="profile-runtime-hermes",
            implementation_digest="b" * 64,
        )
        store.mutate_capabilities_atomically(
            lambda repository: (
                repository.register_definition(definition, scope=RUNTIME_SCOPE),
                repository.register_revision(revision_one, scope=RUNTIME_SCOPE),
                repository.register_revision(revision_two, scope=RUNTIME_SCOPE),
                repository.register_binding(module_binding, scope=RUNTIME_SCOPE),
                repository.register_binding(hermes_binding, scope=RUNTIME_SCOPE),
            )
        )
        profiles.register(
            _profile(
                "profile.provider-bound:r1",
                {
                    "module-only": {
                        "selector": {"provider_kinds_any": ["module"]},
                        "minimum_maturity": "observed",
                    }
                },
                profile_key="profile.provider-bound",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )

        result = profiles.resolve(
            "profile.provider-bound",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
        selected = _requirement_by_capability(result, definition.capability_id)
        assert selected["selection"]["matched_revision_ids"] == [revision_one.revision_id]
        assert selected["selection"]["matched_binding_ids"] == [module_binding.binding_id]
        assert [item["entity_id"] for item in selected["revisions"]] == [revision_one.revision_id]
        assert [item["entity_id"] for item in selected["bindings"]] == [module_binding.binding_id]
    finally:
        store.close()


def test_profile_resolution_fails_closed_for_conflict_and_missing_exact_requirement(tmp_path) -> None:
    conflict_store = RuntimeStore(tmp_path / "conflict")
    conflict_profiles = CapabilityProfiles(conflict_store)
    try:
        _register_capability(conflict_store, _definition("planning.conflict"))
        conflict_profiles.register(
            _profile(
                "profile.conflict:r1",
                {
                    "rule-a": {
                        "selector": {"tags_all": ["planning"]},
                        "minimum_maturity": "observed",
                        "priority": 2,
                    },
                    "rule-b": {
                        "selector": {"tags_all": ["planning"]},
                        "minimum_maturity": "reliable",
                        "priority": 2,
                    },
                },
                profile_key="profile.conflict",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )
        with pytest.raises(CapabilityProfileError, match="conflicting same-priority"):
            conflict_profiles.resolve(
                "profile.conflict",
                runtime_scope=RUNTIME_SCOPE,
                capability_scope=CAPABILITY_SCOPE,
            )
    finally:
        conflict_store.close()

    missing_store = RuntimeStore(tmp_path / "missing")
    missing_profiles = CapabilityProfiles(missing_store)
    try:
        missing_profiles.register(
            _profile(
                "profile.missing:r1",
                {"planning.missing": {"minimum_maturity": "evaluated"}},
                profile_key="profile.missing",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )
        with pytest.raises(CapabilityProfileError, match="no active capability definition"):
            missing_profiles.resolve(
                "profile.missing",
                runtime_scope=RUNTIME_SCOPE,
                capability_scope=CAPABILITY_SCOPE,
            )
    finally:
        missing_store.close()


def test_profile_resolution_enforces_candidate_limit_without_truncation(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    profiles = CapabilityProfiles(store)
    try:
        _register_capability(store, _definition("planning.one"))
        _register_capability(store, _definition("planning.two"))
        profiles.register(
            _profile(
                "profile.limited:r1",
                {
                    "planning-selector": {
                        "selector": {"tags_all": ["planning"]},
                        "minimum_maturity": "observed",
                    }
                },
                profile_key="profile.limited",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )
        with pytest.raises(CapabilityProfileError, match="candidate count exceeds max_candidates"):
            profiles.resolve(
                "profile.limited",
                runtime_scope=RUNTIME_SCOPE,
                capability_scope=CAPABILITY_SCOPE,
                max_candidates=1,
            )
    finally:
        store.close()


def test_profile_revision_resolves_as_of_time_without_fabricating_maturity(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    profiles = CapabilityProfiles(store)
    try:
        _register_capability(store, _definition("planning.history"))
        profiles.register(
            _profile(
                "profile.history:r1",
                {"planning.history": {"minimum_maturity": "observed", "min_pass_rate": 0.5}},
                profile_key="profile.history",
                revision="r1",
                created_at="2020-08-20T00:00:10+00:00",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )
        profiles.register(
            _profile(
                "profile.history:r2",
                {"planning.history": {"minimum_maturity": "reliable", "min_pass_rate": 0.9}},
                profile_key="profile.history",
                revision="r2",
                created_at="2020-08-20T01:00:00+00:00",
            ),
            runtime_scope=RUNTIME_SCOPE,
        )

        historical = profiles.resolve(
            "profile.history",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            at_time="2020-08-20T00:30:00+00:00",
        )
        current = profiles.resolve(
            "profile.history",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
        assert historical["profile"]["profile_id"] == "profile.history:r1"
        assert current["profile"]["profile_id"] == "profile.history:r2"
        assert _requirement_by_capability(historical, "planning.history")["requirement"]["minimum_maturity"] == "observed"
        assert _requirement_by_capability(current, "planning.history")["requirement"]["minimum_maturity"] == "reliable"
        assert not _contains_exact_key(historical, "maturity")
        assert not _contains_exact_key(current, "maturity")
        assert historical["resolution_digest"] != current["resolution_digest"]

        # An as-of query may see a profile revision that was retired later,
        # but must not choose it after its retirement became effective.
        CapabilityRegistry(store).transition_status(
            entity_type="profile",
            entity_id=current["profile"]["profile_id"],
            entity_digest=current["profile"]["profile_digest"],
            target_status="retired",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            expected_state_version=current["profile"]["state_version"],
            expected_state_digest=current["profile"]["state_digest"],
            effective_at="2020-08-20T02:00:00+00:00",
            reason="replaced by the prior active profile revision",
            provenance={"policy_id": "profile-history-test"},
            request_key="profile.history:r2:retire",
        )
        before_retirement = profiles.resolve(
            "profile.history",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            at_time="2020-08-20T01:30:00+00:00",
        )
        after_retirement = profiles.resolve(
            "profile.history",
            runtime_scope=RUNTIME_SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            at_time="2020-08-20T03:00:00+00:00",
        )
        assert before_retirement["profile"]["profile_id"] == "profile.history:r2"
        assert after_retirement["profile"]["profile_id"] == "profile.history:r1"
    finally:
        store.close()
