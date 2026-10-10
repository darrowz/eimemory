"""Dependency-free release impact classification for lineage and deployment."""

from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any


DOMAINS = (
    "memory.recall",
    "memory.governance",
    "channel.delivery",
    "storage.integrity",
    "deployment.runtime",
    "code.evolution",
)

# Persisted/wire models and shared identity normalization are consumed by all
# six domains; changes must invalidate dependent evidence rather than inherit it.
_SHARED_MODEL_PATHS = ("eimemory/models", "eimemory/identity.py", "eimemory/core/key_components.py")

DOMAIN_PATHS: dict[str, tuple[str, ...]] = {
    "memory.recall": (
        "deploy/check_hermes_recall_identity.py",
        "eimemory/adapters/runtime/sources.py",
        "eimemory/governance/learning/effect_policy.py",
        "eimemory/governance/learning/effect_learning.py",
        "eimemory/governance/learning/effect_dataset.py",
        "eimemory/governance/learning/effect_hypotheses.py",
        "eimemory/core/budgets.py",
        "eimemory/llm/gateway_pool.py",
        "eimemory/llm/openclaw_gateway.mjs",
        "eimemory/llm/bridge_pool.py",
        "eimemory/llm/openclaw_adapter.py",
        "eimemory/raw/retrieval.py",
        "deploy/backfill_hermes_scope.py",
        "eimemory/api/memory.py",
        "eimemory/knowledge/l1_conflict.py",
        "eimemory/knowledge/l1_pipeline.py",
        "eimemory/knowledge/l1_prompts.py",
        "eimemory/knowledge/l1_queue.py",
        "eimemory/knowledge/sediment.py",
        "eimemory/persona/correction.py",
        "eimemory/embeddings",
        "integrations/hermes/eimemory/__init__.py",
        "eimemory/recall",
        "eimemory/retrieval",
        "eimemory/scoring",
        "eimemory/storage/runtime_store.py",
        "eimemory/storage/sqlite_store.py",
        "eimemory/storage/recall_deadline.py",
        "eimemory/llm/command_client.py",
        "eimemory/llm/hermes_tool_free.py",
        "eimemory/llm/completion_timing.py",
        "deploy/luna_bridge",
        "eimemory/adapters/hermes/provider_core.py",
        "eimemory/adapters/hermes/effect_observer.py",
        *_SHARED_MODEL_PATHS,
    ),
    "memory.governance": (
        "eimemory/adapters/runtime/sources.py",
        "eimemory/api/evolution.py",
        "eimemory/cli/l1_worker.py",
        "eimemory/knowledge/l1_conflict.py",
        "eimemory/knowledge/l1_pipeline.py",
        "eimemory/knowledge/l1_prompts.py",
        "eimemory/knowledge/l1_queue.py",
        "eimemory/knowledge/sediment.py",
        "eimemory/llm/hermes_tool_free.py",
        "eimemory/ops/backfill_capability_v3.py",
        "eimemory/persona/correction.py",
        "eimemory/persona/store.py",
        "scripts/reflective_replay.py",
        "eimemory/llm/openclaw_adapter.py",
        "eimemory/intake/closure.py",
        "eimemory/intake/connectors.py",
        "eimemory/judgment.py",
        "deploy/run_with_governance_env.py",
        "eimemory/security_screening.py",
        "eimemory/adapters/eibrain/rpc.py",
        "eimemory/scheduler/result_contract.py",
        "eimemory/api/runtime.py",
        "eimemory/evaluation",
        "eimemory/experience",
        "eimemory/governance",
        "eimemory/contracts",
        "deploy/independent-evidence.env.example",
        "integrations/hermes/eimemory_hook/__init__.py",
        "eimemory/adapters/hermes/provider_core.py",
        "eimemory/adapters/hermes/effect_observer.py",
        *_SHARED_MODEL_PATHS,
    ),
    "channel.delivery": (
        "deploy/check_hermes_recall_identity.py",
        "eimemory/core/budgets.py",
        "scripts/openclaw_loop.py",
        "eimemory/adapters/eibrain/rpc.py",
        "deploy/ensure_hermes_sync_snapshot.py",
        "integrations/hermes/host/memory_sync_snapshot.py",
        "deploy/install_hermes_integration.py",
        "deploy/openclaw",
        "deploy/ensure_openclaw",
        "deploy/ensure_openclaw_bundled_bridge.py",
        "deploy/ensure_openclaw_bridge_config.py",
        "deploy/install_immutable_release.sh",
        "deploy/patch_openclaw",
        "deploy/systemd/openclaw-",
        "deploy/verify_openclaw",
        "deploy/verify_openclaw_plugin_runtime.py",
        "deploy/wait_openclaw_gateway_ready.py",
        "deploy/verify_hermes_integration.py",
        "eimemory/adapters/hermes/channel_delivery.py",
        "eimemory/adapters/openclaw",
        "eimemory/persona/correction.py",
        "eimemory/persona/store.py",
        "eimemory/adapters/runtime",
        "eimemory/intake/safe_transport.py",
        "eimemory/ei_bridge",
        "eimemory/ops/openclaw_loop.py",
        "eimemory/governance/external_channel_acceptance.py",
        "eimemory/governance/l5/external_channel_acceptance.py",
        "integrations/hermes/eimemory_hook",
        "integrations/openclaw",
        "eimemory/adapters/hermes/provider_core.py",
        "eimemory/adapters/hermes/effect_observer.py",
        *_SHARED_MODEL_PATHS,
    ),
    "storage.integrity": (
        "eimemory/compatibility/migration_helpers.py",
        "deploy/backfill_hermes_scope.py",
        "deploy/migrate_storage_release.py",
        "deploy/install_immutable_release.sh",
        "deploy/storage",
        "deploy/storage_release_transaction.py",
        "deploy/systemd/eimemory-storage",
        "deploy/verify_storage_release.py",
        "deploy/eimemory_backup.py",
        "eimemory/storage",
        *_SHARED_MODEL_PATHS,
    ),
    "deployment.runtime": (
        "deploy/check_hermes_recall_identity.py",
        "deploy/inspect_release_pollution.py",
        "scripts/openclaw_loop.py",
        "deploy/deployment_attempt_result.py",
        "deploy/run_with_governance_env.py",
        "deploy/ensure_hermes_sync_snapshot.py",
        "integrations/hermes/host/memory_sync_snapshot.py",
        "deploy/bootstrap_production_recall.py",
        "deploy/capture_prior_health",
        "deploy/eimemory_backup.py",
        "deploy/discover_python_runtime_units.sh",
        "deploy/ensure_evidence_receipt",
        "deploy/install_hermes_integration.py",
        "deploy/install_immutable_release.sh",
        "deploy/install_managed_systemd_dropin.py",
        "deploy/record_deployment_receipt.py",
        "deploy/record_release_closure_incident.py",
        "deploy/rerun_release_closure.sh",
        "deploy/release_impact.py",
        "deploy/refresh_release_scope_bindings.py",
        "deploy/summarize_release_closure.py",
        "deploy/record_release_lineage.py",
        "deploy/runtime_identity_policy.py",
        "deploy/systemd/eimemory-",
        "deploy/systemd/hermes-",
        "deploy/verify_hermes_integration.py",
        "deploy/verify_release_health.py",
        "eimemory/adapters/eibrain/rpc_server.py",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/governance/release/deployment_receipt.py",
        "eimemory/governance/release_impact.py",
        "eimemory/governance/release/release_impact.py",
        "eimemory/governance/release_lineage.py",
        "eimemory/governance/release/release_lineage.py",
        "eimemory/ops/runtime_identity_drift.py",
        "eimemory/ops/timer_monitor.py",
        "eimemory/runtime_identity.py",
        "integrations/hermes/eimemory/__init__.py",
        "integrations/hermes/eimemory_hook/__init__.py",
        "eimemory/adapters/hermes/provider_core.py",
        "eimemory/adapters/hermes/effect_observer.py",
        *_SHARED_MODEL_PATHS,
    ),
    "code.evolution": (
        "eimemory/governance/capability/hypothesis_producer.py",
        "eimemory/api/evolution.py",
        "deploy/code-automation-policy.v2.commit-push-only.example",
        "deploy/code-automation-policy.v2.full.example",
        "deploy/run_with_governance_env.py",
        "eimemory/core/wiring_audit.py",
        "eimemory/scheduler/result_contract.py",
        "eimemory/contracts",
        "eimemory/adapters/hermes/code_implementation.py",
        "eimemory/capabilities/code_implementation_bootstrap.py",
        "eimemory/capabilities/data/code_implementation.v2.json",
        "eimemory/evaluation/hongtu_code_implementation.py",
        "eimemory/governance/autonomous_evolution.py",
        "eimemory/governance/evolution/autonomous_evolution.py",
        "eimemory/governance/autonomous_learning.py",
        "eimemory/governance/learning/autonomous_learning.py",
        "eimemory/governance/code_automation_policy.py",
        "eimemory/governance/evolution/code_automation_policy.py",
        "eimemory/governance/code_maintenance.py",
        "eimemory/governance/evolution/code_maintenance.py",
        "eimemory/governance/code_evolution_bridge.py",
        "eimemory/governance/evolution/code_evolution_bridge.py",
        "eimemory/governance/code_evolution_effects.py",
        "eimemory/governance/evolution/code_evolution_effects.py",
        "eimemory/governance/code_evolution_observation.py",
        "eimemory/governance/evolution/code_evolution_observation.py",
        "eimemory/governance/code_evolution_repository.py",
        "eimemory/governance/evolution/code_evolution_repository.py",
        "eimemory/governance/code_evolution_test_plans.py",
        "eimemory/governance/evolution/code_evolution_test_plans.py",
        "eimemory/governance/code_evolution_transaction.py",
        "eimemory/governance/evolution/code_evolution_transaction.py",
        "eimemory/governance/code_patch_command_policy.py",
        "eimemory/governance/evolution/code_patch_command_policy.py",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/governance/release/deployment_receipt.py",
        "eimemory/governance/l5_product_completion.py",
        "eimemory/governance/l5/l5_product_completion.py",
        "eimemory/ops/backfill_capability_v3.py",
        "eimemory/governance/l5_reader.py",
        "eimemory/governance/l5/l5_reader.py",
        "eimemory/governance/promotion_watch.py",
        "eimemory/governance/promotion/promotion_watch.py",
        "eimemory/governance/release_closure_gate_evidence.py",
        "eimemory/governance/release/release_closure_gate_evidence.py",
        "eimemory/governance/release_closure_lineage.py",
        "eimemory/governance/release/release_closure_lineage.py",
        "eimemory/governance/release_pre_observation.py",
        "eimemory/governance/release/release_pre_observation.py",
        "eimemory/governance/release_impact.py",
        "eimemory/governance/release/release_impact.py",
        "eimemory/governance/release_lineage.py",
        "eimemory/governance/release/release_lineage.py",
        "eimemory/governance/system_code_repair.py",
        "eimemory/governance/evolution/system_code_repair.py",
        "eimemory/ops/release_closure_failure.py",
        "eimemory/ops/system_code_repair_failure.py",
        "eimemory/cli/main.py",
        "eimemory/scheduler/jobs.py",
        "eimemory/storage/code_evolution_store.py",
        "eimemory/storage/migrations/code_evolution_transactions.py",
        "deploy/code-automation-policy.v2.json.example",
        "deploy/governance.env.example",
        "deploy/install_hermes_integration.py",
        "deploy/install_immutable_release.sh",
        "deploy/record_release_closure_incident.py",
        "deploy/rerun_release_closure.sh",
        "deploy/release_impact.py",
        "deploy/summarize_release_closure.py",
        "deploy/runtime_identity_policy.py",
        "deploy/systemd/eimemory-learn-watch.service",
        "deploy/systemd/eimemory-learn-watch.timer",
        "deploy/systemd/hermes-gateway-eimemory.conf",
        "deploy/verify_hermes_integration.py",
        "integrations/hermes/eimemory_hook/__init__.py",
        "integrations/hermes/eimemory_hook/plugin.yaml",
        "eimemory/adapters/hermes/provider_core.py",
        "eimemory/adapters/hermes/effect_observer.py",
        *_SHARED_MODEL_PATHS,
    ),
}

# These paths own execution policy or release admission. Classifying them
# only as memory.governance can otherwise inherit stale code/deployment gates.
_RELEASE_GATE_PATHS = (
    "eimemory/governance/release/closure_blockers.py",
    "eimemory/cli/capability_selection.py",
    "eimemory/evaluation/selection_diagnostics.py",
    "deploy/collect_release_health.py",
    "deploy/verify_release_health.py",
    "deploy/check_user_systemd_owner.sh",
    "eimemory/core/python_invocation.py",
    "eimemory/governance/promotion",
    "eimemory/governance/l5/closure_rehearsal.py",
    "eimemory/governance/release/release_closure.py",
    "eimemory/governance/release/release_closure_pending.py",
    "eimemory/governance/release/closure_contracts.py",
    "eimemory/governance/l5/live_task_acceptance.py",
    "eimemory/storage/atomic_file.py",
    "eimemory/storage/private_file.py",
    "eimemory/scheduler/result_contract.py",
)
for _domain in ("memory.governance", "code.evolution", "deployment.runtime"):
    DOMAIN_PATHS[_domain] = (*DOMAIN_PATHS[_domain], *_RELEASE_GATE_PATHS)
DOMAIN_PATHS["memory.recall"] = (*DOMAIN_PATHS["memory.recall"], "eimemory/storage/record_export.py")

_OFFLINE_REVIEW_PATHS = (
    "deploy/offline_state_review.py",
    "deploy/explain_acceptance_failure.py",
    "deploy/rebuild_projection_snapshot.py",
)
for _domain in ("storage.integrity", "deployment.runtime", "code.evolution"):
    DOMAIN_PATHS[_domain] = (*DOMAIN_PATHS[_domain], *_OFFLINE_REVIEW_PATHS)

_RECALL_RELEASE_BOUNDARIES = (
    "eimemory/__init__.py",
    "eimemory/core/release_source_guard.py",
    "eimemory/evaluation/recall_quality_contract.py",
    "eimemory/scheduler/result_contract.py",
    "eimemory/scheduler/jobs.py",
    "eimemory/governance/learning/supervisor.py",
    "deploy/verify_python_sources.py",
    "deploy/release_source_checkpoint.py",
    "deploy/verify_release_mount.py",
    "deploy/diagnose_recall_release.py",
)
for _domain in DOMAINS:
    DOMAIN_PATHS[_domain] = (*DOMAIN_PATHS[_domain], *_RECALL_RELEASE_BOUNDARIES)

# Every tracked production file must belong to at least one domain.  An
# unclassified path marks the release ``unknown_production_paths``, which
# (correctly) refuses code-evolution auto-authorization and leaves the whole
# lineage incompatible.  1.14.46 shipped with these owners missing, so its
# post-deploy closure failed with ``release_lineage_not_compatible``.  Exact
# files only: a *new* unregistered file must still fail closed as unknown.
_PRODUCTION_SURFACE_PATHS: dict[str, tuple[str, ...]] = {
    "memory.recall": (
        "deploy/activate_reranker_artifact.py",
        "deploy/prepare_qwen_reranker.py",
        "deploy/provision_postgres_vector.py",
        "deploy/provision_reranker.py",
        "deploy/quantize_reranker.py",
        "deploy/recall.env.example",
        "eimemory/adapters/hermes/__init__.py",
        "eimemory/adapters/hermes/durable_handoff.py",
        "eimemory/adapters/hermes/host_context.py",
        "eimemory/adapters/hermes/native_memory.py",
        "eimemory/adapters/hermes/provider_registry.py",
        "eimemory/config/defaults.py",
        "eimemory/config/loader.py",
        "eimemory/config/schema.py",
        "eimemory/config/trusted.py",
        "eimemory/core/clock.py",
        "eimemory/core/ids.py",
        "eimemory/core/record_ids.py",
        "eimemory/core/strict_json.py",
        "eimemory/core/untrusted.py",
        "eimemory/knowledge/__init__.py",
        "eimemory/knowledge/capabilities.py",
        "eimemory/knowledge/claims.py",
        "eimemory/knowledge/compiler.py",
        "eimemory/knowledge/daily_brief.py",
        "eimemory/knowledge/evidence_contracts.py",
        "eimemory/knowledge/evidence_gate.py",
        "eimemory/knowledge/extract.py",
        "eimemory/knowledge/ingest.py",
        "eimemory/knowledge/pages.py",
        "eimemory/knowledge/projectors.py",
        "eimemory/knowledge/refresh.py",
        "eimemory/knowledge/relations.py",
        "eimemory/knowledge/safety.py",
        "eimemory/knowledge/source_trust.py",
        "eimemory/knowledge/synthesis.py",
        "eimemory/knowledge/turn_context.py",
        "eimemory/knowledge/views.py",
        "eimemory/living/__init__.py",
        "eimemory/living/operations.py",
        "eimemory/living/posture.py",
        "eimemory/living/schema.py",
        "eimemory/llm/__init__.py",
        "eimemory/llm/hermes_adapter.py",
        "eimemory/metadata.py",
        "eimemory/persona/__init__.py",
        "eimemory/persona/cli.py",
        "eimemory/persona/context_router.py",
        "eimemory/persona/evals/__init__.py",
        "eimemory/persona/evals/persona_cases.jsonl",
        "eimemory/persona/evals/run_persona_eval.py",
        "eimemory/persona/evolver.py",
        "eimemory/persona/feedback_safety.py",
        "eimemory/persona/prompt.py",
        "eimemory/persona/schema.py",
        "eimemory/persona/state.py",
        "eimemory/raw/__init__.py",
        "eimemory/raw/boundary.py",
        "eimemory/raw/chunks.py",
        "eimemory/raw/store.py",
        "eimemory/raw/synthetic.py",
    ),
    "memory.governance": (
        "benchmarks/__init__.py",
        "benchmarks/l5_v3_baseline.py",
        "deploy/run_memory_l5_fused_closure.sh",
        "eimemory/adapters/create_safety_gate.py",
        "eimemory/capabilities/__init__.py",
        "eimemory/capabilities/applicability.py",
        "eimemory/capabilities/consumer_views.py",
        "eimemory/capabilities/contracts.py",
        "eimemory/capabilities/data/legacy_capabilities.v1.json",
        "eimemory/capabilities/models.py",
        "eimemory/capabilities/observations.py",
        "eimemory/capabilities/profile_bootstrap.py",
        "eimemory/capabilities/profiles.py",
        "eimemory/capabilities/projector.py",
        "eimemory/capabilities/registry.py",
        "eimemory/capabilities/seed_manifest.py",
        "eimemory/capabilities/service.py",
        "eimemory/config/defaults.py",
        "eimemory/config/loader.py",
        "eimemory/config/schema.py",
        "eimemory/config/trusted.py",
        "eimemory/core/clock.py",
        "eimemory/core/ids.py",
        "eimemory/core/record_ids.py",
        "eimemory/core/strict_json.py",
        "eimemory/core/untrusted.py",
        "eimemory/events.py",
        "eimemory/identity_ops.py",
        "eimemory/intake/__init__.py",
        "eimemory/intake/autonomous_sources.py",
        "eimemory/intake/closure_review.py",
        "eimemory/intake/fulltext.py",
        "eimemory/intake/loop.py",
        "eimemory/intake/packs.py",
        "eimemory/intake/papers/__init__.py",
        "eimemory/intake/papers/artifacts.py",
        "eimemory/intake/papers/metadata.py",
        "eimemory/intake/papers/normalize.py",
        "eimemory/intake/papers/pdf_parse.py",
        "eimemory/intake/papers/sources.py",
        "eimemory/intake/pipeline.py",
        "eimemory/intake/policy.py",
        "eimemory/intake/registry.py",
        "eimemory/intake/review.py",
        "eimemory/intake/source_discovery.py",
        "eimemory/intake/title_normalization.py",
        "eimemory/knowledge/__init__.py",
        "eimemory/knowledge/capabilities.py",
        "eimemory/knowledge/claims.py",
        "eimemory/knowledge/compiler.py",
        "eimemory/knowledge/daily_brief.py",
        "eimemory/knowledge/evidence_contracts.py",
        "eimemory/knowledge/evidence_gate.py",
        "eimemory/knowledge/extract.py",
        "eimemory/knowledge/ingest.py",
        "eimemory/knowledge/pages.py",
        "eimemory/knowledge/projectors.py",
        "eimemory/knowledge/refresh.py",
        "eimemory/knowledge/relations.py",
        "eimemory/knowledge/safety.py",
        "eimemory/knowledge/source_trust.py",
        "eimemory/knowledge/synthesis.py",
        "eimemory/knowledge/turn_context.py",
        "eimemory/knowledge/views.py",
        "eimemory/living/__init__.py",
        "eimemory/living/operations.py",
        "eimemory/living/posture.py",
        "eimemory/living/schema.py",
        "eimemory/llm/__init__.py",
        "eimemory/llm/hermes_adapter.py",
        "eimemory/metadata.py",
        "eimemory/ops/__init__.py",
        "eimemory/persona/__init__.py",
        "eimemory/persona/cli.py",
        "eimemory/persona/context_router.py",
        "eimemory/persona/evals/__init__.py",
        "eimemory/persona/evals/persona_cases.jsonl",
        "eimemory/persona/evals/run_persona_eval.py",
        "eimemory/persona/evolver.py",
        "eimemory/persona/feedback_safety.py",
        "eimemory/persona/prompt.py",
        "eimemory/persona/schema.py",
        "eimemory/persona/state.py",
        "examples/evaluation/actionable_memory_smoke.json",
        "examples/evaluation/living_memory_smoke.json",
        "examples/evaluation/locomo_smoke.json",
        "examples/evaluation/longmemeval_smoke.json",
        "examples/evaluation/memory_ci.json",
        "examples/evaluation/production_recall_smoke.json",
        "examples/evaluation/real_task_replay_smoke.json",
        "examples/standalone/basic_usage.py",
        "goals/long_term.json",
        "scripts/audit_business_closure.py",
        "scripts/convert_locomo_to_eimemory.py",
        "scripts/convert_longmemeval_to_eimemory.py",
        "scripts/record_outcome_trace.py",
        "scripts/run_full_eval.py",
        "scripts/smoke_test_converted_data.py",
        "state/autonomous_learning/active/.gitkeep",
        "state/autonomous_learning/canary/.gitkeep",
        "state/autonomous_learning/rolled_back/.gitkeep",
    ),
    "channel.delivery": (
        "eimemory/adapters/codex/__init__.py",
        "eimemory/adapters/codex/hook.py",
        "eimemory/adapters/codex/mcp_server.py",
        "eimemory/adapters/eibrain/sdk.py",
        "eimemory/adapters/hermes/__init__.py",
        "eimemory/adapters/hermes/durable_handoff.py",
        "eimemory/adapters/hermes/host_context.py",
        "eimemory/adapters/hermes/native_memory.py",
        "eimemory/adapters/hermes/provider_registry.py",
        "eimemory/core/rpc_probe_auth.py",
        "integrations/codex/.agents/plugins/marketplace.json",
        "integrations/codex/eimemory/.mcp.json",
        "integrations/codex/eimemory/README.md",
        "integrations/codex/eimemory/hooks/hooks.json",
        "integrations/hermes/eimemory/README.md",
        "integrations/hermes/eimemory/release_path.py",
        "integrations/hermes/host-patches/memory-sync-snapshot.json",
        "integrations/hermes/host-patches/memory-sync-snapshot.patch",
    ),
    "storage.integrity": (
        "eimemory/core/clock.py",
        "eimemory/core/ids.py",
        "eimemory/core/record_ids.py",
        "eimemory/core/strict_json.py",
        "eimemory/core/untrusted.py",
        "eimemory/events.py",
        "eimemory/identity_ops.py",
        "eimemory/metadata.py",
        "eimemory/raw/chunks.py",
        "eimemory/raw/store.py",
    ),
    "deployment.runtime": (
        "deploy/activate_reranker_artifact.py",
        "deploy/capture_prior_health_snapshot.py",
        "deploy/clean_release_bytecode.py",
        "deploy/eimemory-deploy-worker",
        "deploy/ensure_attestation_profile.py",
        "deploy/ensure_evidence_receipt_key.py",
        "deploy/ensure_rpc_auth.py",
        "deploy/find_prior_immutable_release.py",
        "deploy/hold_parent_bound_lock.py",
        "deploy/prepare_qwen_reranker.py",
        "deploy/provision_postgres_vector.py",
        "deploy/provision_reranker.py",
        "deploy/quantize_reranker.py",
        "deploy/recall.env.example",
        "deploy/rotate_console_token.py",
        "deploy/run_memory_l5_fused_closure.sh",
        "eimemory/cli/doctor.py",
        "eimemory/config/defaults.py",
        "eimemory/config/loader.py",
        "eimemory/config/schema.py",
        "eimemory/config/trusted.py",
        "eimemory/core/rpc_probe_auth.py",
        "integrations/hermes/eimemory/README.md",
        "integrations/hermes/eimemory/release_path.py",
        "integrations/hermes/host-patches/memory-sync-snapshot.json",
        "integrations/hermes/host-patches/memory-sync-snapshot.patch",
    ),
    "code.evolution": (
        "eimemory/adapters/create_safety_gate.py",
        "eimemory/capabilities/__init__.py",
        "eimemory/capabilities/applicability.py",
        "eimemory/capabilities/consumer_views.py",
        "eimemory/capabilities/contracts.py",
        "eimemory/capabilities/data/legacy_capabilities.v1.json",
        "eimemory/capabilities/models.py",
        "eimemory/capabilities/observations.py",
        "eimemory/capabilities/profile_bootstrap.py",
        "eimemory/capabilities/profiles.py",
        "eimemory/capabilities/projector.py",
        "eimemory/capabilities/registry.py",
        "eimemory/capabilities/seed_manifest.py",
        "eimemory/capabilities/service.py",
        "eimemory/ops/code_implementation_owner.py",
        "scripts/audit_business_closure.py",
        "state/autonomous_learning/active/.gitkeep",
        "state/autonomous_learning/canary/.gitkeep",
        "state/autonomous_learning/rolled_back/.gitkeep",
    ),
}
for _domain, _paths in _PRODUCTION_SURFACE_PATHS.items():
    DOMAIN_PATHS[_domain] = (*DOMAIN_PATHS[_domain], *_paths)

# Shared report decisions and their capture/registration boundary change
# admission semantics; do not inherit prior code/deployment evidence.
for _domain in ("memory.governance", "code.evolution", "deployment.runtime"):
    DOMAIN_PATHS[_domain] = (*DOMAIN_PATHS[_domain],
        "eimemory/governance/release/closure_verdict.py",
        "eimemory/ops/closure_capture.py",
        "eimemory/ops/release_closure_failure.py",
    )

IGNORED_PATH_PREFIXES = ("docs/", "tests/", ".github/")
IGNORED_PATHS = {
    ".gitignore",
    ".superpowers/sdd/task-6-postgres-source-report.md",
    ".superpowers/sdd/task-7-proactive-recall-report.md",
    "_cli_cmds.txt",
    "_cli_toplevel.txt",
    "scripts/download_longmemeval.py",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "FAQ.md",
    "LICENSE",
    "deploy/systemd/README.md",
    "scripts/test_openclaw_loop.py",
}
INTEGRATION_VERSION_PATHS = {
    "integrations/codex/eimemory/.codex-plugin/plugin.json",
    "integrations/hermes/eimemory/plugin.yaml",
    "integrations/hermes/eimemory_hook/plugin.yaml",
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)


class ReleaseImpactError(ValueError):
    """Raised when an exact release impact cannot be established."""


def release_impact(
    repo: Path,
    *,
    ancestor: str,
    current: str,
) -> dict[str, Any]:
    """Return a bounded JSON-safe classification for two exact commits."""

    repository = Path(repo)
    if not repository.is_dir():
        raise ReleaseImpactError("repository is not a directory")
    for label, commit in (("prior", ancestor), ("current", current)):
        if COMMIT_RE.fullmatch(commit) is None:
            raise ReleaseImpactError(f"{label} commit must be an exact 40-character SHA")
        if _git_bytes(repository, "cat-file", "-e", f"{commit}^{{commit}}") is None:
            raise ReleaseImpactError(f"{label} commit is unavailable")

    change = _release_change_summary(
        repository,
        ancestor=ancestor,
        current=current,
    )
    if change is None:
        raise ReleaseImpactError("changed paths could not be read")
    changed_paths, classified, unknown = change
    unknown_set = set(unknown)
    paths: list[dict[str, Any]] = []
    affected_domains: set[str] = set()
    for path in changed_paths:
        domains = sorted(classified[path])
        affected_domains.update(domains)
        if domains:
            classification = "classified"
        elif path in unknown_set:
            classification = "unknown_production"
        else:
            classification = "ignored"
        paths.append(
            {
                "path": path,
                "domains": domains,
                "classification": classification,
            }
        )
    if unknown:
        reason = "unknown_production_change"
    elif affected_domains:
        reason = "classified_production_change"
    else:
        reason = "lightweight_release"
    return {
        "requires_closure": bool(unknown or affected_domains),
        "reason": reason,
        "affected_domains": sorted(affected_domains),
        "unknown_production_paths": unknown,
        "paths": paths,
    }


def _changed_paths(repo: Path, ancestor: str, current: str) -> list[str] | None:
    # A rename must invalidate both the old and new production domains. Git's
    # name-only rename output otherwise retains only the destination path.
    raw = _git_bytes(repo, "diff", "--no-renames", "--name-only", "-z", f"{ancestor}..{current}")
    if raw is None:
        return None
    return sorted(
        path.decode("utf-8", errors="surrogateescape")
        for path in raw.split(b"\0")
        if path
    )


def _release_change_summary(
    repo: Path,
    *,
    ancestor: str,
    current: str,
) -> tuple[list[str], dict[str, set[str]], list[str]] | None:
    changed_paths = _changed_paths(repo, ancestor, current)
    if changed_paths is None:
        return None
    classified = {
        path: _domains_for_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
        for path in changed_paths
    }
    unknown = sorted(
        path
        for path, domains in classified.items()
        if not domains
        and not _ignored_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
    )
    return changed_paths, classified, unknown


def _domains_for_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> set[str]:
    if path in {"pyproject.toml", "eimemory/version.py"}:
        return set() if _version_metadata_only_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        ) else set(DOMAINS)
    if path in INTEGRATION_VERSION_PATHS and _integration_version_only_change(
        repo,
        path=path,
        ancestor=ancestor,
        current=current,
    ):
        return set()
    return {
        domain
        for domain, rules in DOMAIN_PATHS.items()
        if any(_path_matches_rule(path, rule) for rule in rules)
    }


def _path_matches_rule(path: str, rule: str) -> bool:
    return bool(
        path == rule
        or path.startswith(rule.rstrip("/") + "/")
        or (rule.endswith(("-", "_")) and path.startswith(rule))
    )


def _ignored_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    return (
        path in IGNORED_PATHS
        or path.startswith(IGNORED_PATH_PREFIXES)
        or path.startswith("README")
        or path.startswith("CHANGELOG")
        or path.startswith("FAQ")
        or path.startswith("CONTRIBUTING")
        or path == "LICENSE"
        or (
            path in {"pyproject.toml", "eimemory/version.py"}
            and _version_metadata_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
        or (
            path in INTEGRATION_VERSION_PATHS
            and _integration_version_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
    )


def _version_metadata_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path == "pyproject.toml":
            before_payload = deepcopy(tomllib.loads(before.decode("utf-8")))
            after_payload = deepcopy(tomllib.loads(after.decode("utf-8")))
            for payload in (before_payload, after_payload):
                project = payload.get("project")
                if isinstance(project, dict):
                    project.pop("version", None)
            return _metadata_equal(before_payload, after_payload)
        return _normalized_version_module(before) == _normalized_version_module(after)
    except (SyntaxError, UnicodeError, ValueError, TypeError):
        return False


def _integration_version_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path.endswith(".json"):
            before_payload = json.loads(before.decode("utf-8"))
            after_payload = json.loads(after.decode("utf-8"))
            if not isinstance(before_payload, dict) or not isinstance(after_payload, dict):
                return False
            before_payload.pop("version", None)
            after_payload.pop("version", None)
            return _metadata_equal(before_payload, after_payload)
        version_line = re.compile(r"^version\s*:")

        def normalized_lines(raw: bytes) -> tuple[str, ...]:
            return tuple(
                line.rstrip()
                for line in raw.decode("utf-8").splitlines()
                if version_line.match(line) is None
            )

        return normalized_lines(before) == normalized_lines(after)
    except (UnicodeError, ValueError, TypeError):
        return False


def _metadata_equal(before: Any, after: Any) -> bool:
    """Compare parsed metadata without equating booleans with numbers."""
    if before is after:
        return True
    if isinstance(before, bool) != isinstance(after, bool):
        return False
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(
            _metadata_equal(value, after[key]) for key, value in before.items()
        )
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _metadata_equal(left, right) for left, right in zip(before, after)
        )
    return before == after


def _version_metadata_declaration(tree: ast.Module) -> ast.Assign | ast.AnnAssign | None:
    declarations: list[ast.Assign | ast.AnnAssign] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in targets
            ):
                declarations.append(node)
    # Only one plain, top-level string declaration is release metadata. Calls,
    # chained assignments, nested names and repeated assignments can change
    # executable behavior and must remain visible to both impact and lineage.
    if len(declarations) == 1:
        node = declarations[0]
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if (
            len(targets) == 1
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node
    return None


def _normalized_version_module(raw: bytes) -> str:
    tree = ast.parse(raw.decode("utf-8"))
    declaration = _version_metadata_declaration(tree)
    if declaration is not None:
        declaration.value = ast.Constant(value="<release-version>")
    return ast.dump(tree, include_attributes=False)


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None
