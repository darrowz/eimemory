from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRevision,
)
from eimemory.capabilities.consumer_views import (
    dynamic_evaluation_view,
    resolve_explicit_capability_attribution,
)
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog, CatalogCase
from eimemory.governance.autonomy_goal_queue import build_autonomy_goal_queue
from eimemory.governance.capability_dashboard import build_dynamic_capability_dashboard
from eimemory.governance.learning_dashboard import build_weekly_dashboard
from eimemory.governance.replay_dataset import build_replay_dataset
from eimemory.governance.capability_replay_packs import build_capability_replay_packs
from eimemory.governance.self_model import build_self_model
from eimemory.governance.thoughts import generate_thoughts
from eimemory.governance.world_watchers import SourceWatch, _normalize_signal, collect_world_signals


SCOPE = {
    "tenant_id": "tenant-dynamic-consumer",
    "agent_id": "agent-dynamic-consumer",
    "workspace_id": "workspace-dynamic-consumer",
    "user_id": "user-dynamic-consumer",
}
STAMP = "2020-08-20T00:00:00Z"


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="consumer.dynamic",
        display_name="Dynamic Consumer",
        description="A capability registered only by the test control plane.",
        owner="governance",
        created_at=STAMP,
        provenance={"source": "dynamic-consumer-test"},
    )


def _revision(definition: CapabilityDefinition) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id="consumer.dynamic:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object", "required": ["request"]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_is_traceable"],
            "failure_invariants": ["blocked_input"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=STAMP,
        provenance={"source": "dynamic-consumer-test"},
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id="binding.consumer.dynamic:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="consumer-local",
        implementation_digest="b" * 64,
        operations=("evaluate",),
        limits={"max_requests": 8},
        environment_fingerprint={"runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://dynamic/consumer-advertisement.json",),
        provenance={"source": "dynamic-consumer-test"},
        created_at=STAMP,
    )


def _profile(definition: CapabilityDefinition) -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="profile.consumer.dynamic:v1",
        profile_key="profile.consumer.dynamic",
        requirements={definition.capability_id: {"minimum_maturity": "evaluated"}},
        created_at=STAMP,
        provenance={
            "source": "dynamic-consumer-test",
            "capability_aliases": {"legacy.consumer": definition.capability_id},
        },
    )


def _catalog() -> CapabilityEvaluationCatalog:
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(
        executor_id="eimemory.eval.consumer-dynamic",
        revision="v1",
        handler=lambda _input, _fixture, _runtime: {"decision": "traceable", "evidence_count": 1},
    )
    catalog.register_case(
        CatalogCase(
            case_id="consumer_dynamic_contract",
            capability_id="consumer.dynamic",
            executor_id="eimemory.eval.consumer-dynamic",
            input_data={"request": "rehearse dynamic consumer"},
            fixture={"fixture_id": "consumer-dynamic-v1"},
            expected_invariants=[
                {"field": "decision", "op": "eq", "value": "traceable"},
                {"field": "evidence_count", "op": "min", "value": 1},
            ],
            binding_selector={"operations_all": ["evaluate"]},
        )
    )
    return catalog


def _runtime_with_catalog(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision)
    profile = _profile(definition)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(binding, runtime_scope=SCOPE)
    runtime.capabilities.register_profile(profile, runtime_scope=SCOPE)
    return runtime, definition, revision, binding, profile, _catalog()


def test_dynamic_consumer_view_and_dashboard_keep_exact_revision_binding(tmp_path) -> None:
    runtime, definition, revision, binding, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        view = dynamic_evaluation_view(
            runtime,
            scope=SCOPE,
            capability_scope="global",
            profile_key=profile.profile_key,
            catalog=catalog,
        )
        dashboard = build_dynamic_capability_dashboard(
            runtime,
            scope=SCOPE,
            capability_scope="global",
            profile_key=profile.profile_key,
            catalog=catalog,
            page=1,
            page_size=10,
        )
    finally:
        runtime.close()

    assert view["ok"] is True
    assert view["cases"][0]["target"]["capability_id"] == definition.capability_id
    assert view["cases"][0]["target"]["capability_revision_id"] == revision.revision_id
    assert view["cases"][0]["target"]["provider_binding_id"] == binding.binding_id
    assert dashboard["ok"] is True
    assert dashboard["items"][0]["capability_id"] == definition.capability_id
    assert dashboard["items"][0]["evaluation_targets"][0]["provider_binding_id"] == binding.binding_id


def test_replay_dataset_defaults_to_catalog_cases_without_fixed_capability_list(tmp_path) -> None:
    runtime, definition, revision, binding, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = build_replay_dataset(
            runtime,
            scope=SCOPE,
            persist=False,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
    finally:
        runtime.close()

    catalog_cases = [case for case in report["cases"] if case.get("source") == "capability_evaluation_catalog"]
    assert report["ok"] is True
    assert report["legacy_compatibility"] is False
    assert report["include_catalog_cases"] is True
    assert catalog_cases
    assert catalog_cases[0]["target_capability"] == definition.capability_id
    assert catalog_cases[0]["capability_revision_id"] == revision.revision_id
    assert catalog_cases[0]["provider_binding_id"] == binding.binding_id


def test_replay_pack_defaults_to_active_catalog_without_mutating_custom_catalog(tmp_path) -> None:
    runtime, definition, revision, binding, _profile_value, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = build_capability_replay_packs(
            runtime,
            scope=SCOPE,
            persist=False,
            capability_scope="global",
            runtime_scope=SCOPE,
            catalog=catalog,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["legacy_compatibility"] is False
    assert report["capabilities"] == [definition.capability_id]
    assert report["packs"][0]["cases"][0]["capability_revision_id"] == revision.revision_id
    assert report["packs"][0]["cases"][0]["provider_binding_id"] == binding.binding_id
    assert [case.case_id for case in catalog.list_cases()] == ["consumer_dynamic_contract"]


def test_dynamic_goal_queue_and_self_model_expose_catalog_targets(tmp_path) -> None:
    runtime, definition, revision, binding, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        queue = build_autonomy_goal_queue(
            runtime,
            scope=SCOPE,
            max_goals=1,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
        model = build_self_model(
            runtime,
            scope=SCOPE,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
    finally:
        runtime.close()

    assert queue.get("ok", True) is True
    assert queue["goals"][0]["capability"] == definition.capability_id
    assert queue["goals"][0]["evaluation_targets"][0]["capability_revision_id"] == revision.revision_id
    assert queue["goals"][0]["evaluation_targets"][0]["provider_binding_id"] == binding.binding_id
    assert model.get("ok", True) is True
    assert model["capabilities"][0]["evaluation_targets"][0]["case_id"] == "consumer_dynamic_contract"


def test_dynamic_thoughts_keep_unknown_signals_unclassified(tmp_path) -> None:
    runtime, _definition_value, _revision_value, _binding_value, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = generate_thoughts(
            runtime,
            scope=SCOPE,
            persist=False,
            signals=[{"summary": "unmapped prose should not pick a capability", "impact": 0.7}],
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["legacy_compatibility"] is False
    assert report["thoughts"][0]["target_capability"] == "unclassified"


def test_weekly_dashboard_defaults_to_dynamic_catalog_selection(tmp_path) -> None:
    runtime, definition, revision, binding, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = build_weekly_dashboard(
            runtime,
            scope=SCOPE,
            persist=False,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["legacy_compatibility"] is False
    assert report["dynamic_capability_dashboard"]["items"][0]["capability_id"] == definition.capability_id
    target = report["dynamic_capability_dashboard"]["items"][0]["evaluation_targets"][0]
    assert target["capability_revision_id"] == revision.revision_id
    assert target["provider_binding_id"] == binding.binding_id


def test_structured_alias_is_allowed_but_prose_is_not_a_dynamic_attribution() -> None:
    context = {"allowed_capability_ids": ("consumer.dynamic",), "aliases": {"legacy.consumer": "consumer.dynamic"}}
    explicit = resolve_explicit_capability_attribution(
        [{"capability_attribution": {"capability_id": "legacy.consumer", "rule_id": "migration"}}],
        **context,
    )
    missing = resolve_explicit_capability_attribution(
        [{"summary": "a consumer capability issue"}],
        **context,
    )

    assert explicit["status"] == "classified"
    assert explicit["capability_id"] == "consumer.dynamic"
    assert missing["capability_id"] == "unclassified"


def test_dynamic_world_signal_requires_structured_attribution() -> None:
    context = {"allowed_capability_ids": ("consumer.dynamic",), "aliases": {}}
    watch = SourceWatch(name="dynamic", kind="local_eval", enabled=True)
    unclassified = _normalize_signal(
        {"title": "legacy target", "summary": "prose", "target_capability": "consumer.dynamic"},
        watch=watch,
        attribution_context=context,
    )
    classified = _normalize_signal(
        {
            "title": "declared target",
            "summary": "structured policy",
            "capability_attribution": {"capability_id": "consumer.dynamic", "rule_id": "watch-rule-v1"},
        },
        watch=watch,
        attribution_context=context,
    )

    assert unclassified["target_capability"] == "unclassified"
    assert classified["target_capability"] == "consumer.dynamic"


def test_world_watch_defaults_to_dynamic_catalog_selection(tmp_path) -> None:
    runtime, _definition_value, _revision_value, _binding_value, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = collect_world_signals(
            runtime,
            scope=SCOPE,
            watches=[SourceWatch(name="disabled", kind="local_state", enabled=False)],
            dry_run=True,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["legacy_compatibility"] is False
    assert report["capability_evaluation_view"]["cases"][0]["target"]["provider_binding_id"] == "binding.consumer.dynamic:v1"


def test_legacy_consumer_alias_requires_explicit_compatibility_flag(tmp_path) -> None:
    runtime, _definition_value, _revision_value, _binding_value, _profile_value, _catalog_value = _runtime_with_catalog(tmp_path)
    try:
        report = generate_thoughts(
            runtime,
            scope=SCOPE,
            persist=False,
            dynamic_capabilities=False,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["reason"] == "legacy_compatibility_required"
    assert report["legacy_compatibility"] is False


def test_dynamic_replay_dataset_rejects_legacy_fixture_without_explicit_mode(tmp_path) -> None:
    runtime, _definition_value, _revision_value, _binding_value, profile, catalog = _runtime_with_catalog(tmp_path)
    try:
        report = build_replay_dataset(
            runtime,
            scope=SCOPE,
            persist=False,
            profile_key=profile.profile_key,
            capability_scope="global",
            catalog=catalog,
            include_built_in_regressions=True,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["reason"] == "legacy_regression_fixture_requires_explicit_legacy_compatibility"
