"""Native gap evidence/repository/policy through the strict Hermes bridge."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.hermes import code_implementation as provider
from eimemory.capabilities import CapabilityDefinition, CapabilityProfile
from eimemory.capabilities.code_implementation_bootstrap import code_implementation_revision, code_implementation_binding
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog, CatalogCase
from eimemory.governance.capability.capability_hypotheses import hypothesis_behavior_gate
from eimemory.governance.evolution import code_evolution_dynamic_context as context
from eimemory.governance.evolution import dynamic_capability_evolution as evolution
from eimemory.governance.evolution.code_evolution_repository import protected_paths_digest, remote_url_digest
from eimemory.governance.evolution.code_evolution_test_plans import DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID, protected_test_plan_digest
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef(tenant_id="test-dynamic-gap", agent_id="agent", workspace_id="workspace", user_id="user")
PROFILE = "test.dynamic.gap"
PATH = "eimemory/ops/bounded_gap_fixture.py"
TEXT = "Reviewed operational evidence supports a bounded fixture repair with independent verification."


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
        text=True, timeout=20).stdout.strip()


@pytest.fixture
def native_gap(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--initial-branch=master")
    git(root, "config", "user.name", "Native test")
    git(root, "config", "user.email", "native@example.invalid")
    (root / PATH).parent.mkdir(parents=True)
    (root / PATH).write_text("def value():\n    return 1\n", encoding="utf-8")
    git(root, "add", PATH)
    git(root, "commit", "-m", "native base")
    git(root, "remote", "add", "origin", "https://example.invalid/native.git")
    base = git(root, "rev-parse", "HEAD")
    monkeypatch.setenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", str(root))
    monkeypatch.setenv("EIMEMORY_TRUSTED_BRANCH", "master")
    monkeypatch.setenv("EIMEMORY_CODE_EVOLUTION_KILL_SWITCH", str(tmp_path / "absent.disabled"))
    policy = dict(schema_version="code_automation_policy.v2", policy_id="native-gap-policy",
        not_before="2026-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z", max_transactions=1,
        incident={"class": context.INCIDENT_CLASS, "detector_id": context.DETECTOR_ID},
        capability=dict(profile_key=PROFILE, capability_id=provider.CAPABILITY_ID,
            revision_id=provider.REVISION_ID, binding_id=provider.BINDING_ID,
            implementation_digest=provider.IMPLEMENTATION_DIGEST, operation=provider.OPERATION),
        repository=dict(root=str(root), remote="origin", branch="master", base_commit=base,
            base_tree_digest=protected_paths_digest(root, [PATH]),
            remote_url_digest=remote_url_digest(git(root, "remote", "get-url", "origin"))),
        patch=dict(allowed_files=[PATH], allowed_path_globs=["eimemory/ops/**"],
            max_files=1, max_file_bytes=49152, max_total_bytes=98304, max_changed_lines=40, max_diff_bytes=262144),
        verification=dict(test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
            test_plan_digest=protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID), full_suite_required=True),
        effects=dict(commit=True, push=True, deployment=True, rollback=True, sedimentation=True),
        deployment=dict(installer_digest="f" * 64, current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health", observation_seconds=28800))
    policy_path = tmp_path / "policy.json"

    def save_policy():
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        policy_path.chmod(0o600)

    save_policy()
    monkeypatch.setenv("EIMEMORY_CODE_AUTOMATION_POLICY_PATH", str(policy_path))
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.capabilities.register_definition(CapabilityDefinition(capability_id=provider.CAPABILITY_ID,
        display_name="Code implementation", description="Produce a bounded implementation artifact from a declared contract.",
        owner="eimemory", created_at="2020-01-01T00:00:00Z", provenance={"source": "native-test"}), runtime_scope=SCOPE)
    runtime.capabilities.register_revision(code_implementation_revision(), runtime_scope=SCOPE)
    runtime.capabilities.bind(code_implementation_binding(), runtime_scope=SCOPE)
    runtime.capabilities.register_profile(CapabilityProfile(profile_id="native-gap-profile:v1", profile_key=PROFILE,
        requirements={provider.CAPABILITY_ID: {"minimum_maturity": "evaluated"}},
        created_at="2020-01-01T00:00:00Z", provenance={"source": "native-test"}), runtime_scope=SCOPE)
    bounds = context.dynamic_patch_candidate_bounds(runtime, runtime_scope=SCOPE, profile_key=PROFILE,
        capability_id=provider.CAPABILITY_ID, capability_revision_id=provider.REVISION_ID,
        provider_binding_ids=[provider.BINDING_ID])
    assert bounds["ok"] is True, bounds
    from eimemory.intake.papers.artifacts import materialize_paper_artifacts
    from eimemory.intake.review import review_candidate, mark_candidate_paper_promoted
    from eimemory.knowledge.source_trust import resolve_source_trust
    from test_paper_pdf_pipeline import _minimal_pdf
    uri = "https://paper.example.test/native-gap"
    registry_source = runtime.sources.add_source(dict(source_id="native-gap-source", source_kind="paper",
        uri=uri, enabled=True, metadata={"connector_id": "test.native", "trust": .85}))
    intake = RecordEnvelope.create(kind="knowledge_candidate", scope=SCOPE, title="Bounded gap evidence",
        summary=TEXT, content=dict(source_id=registry_source.source_id, source_kind="paper", source_uri=uri),
        source="test.native", status="candidate")
    trust = resolve_source_trust(intake.to_dict(), registry=runtime.sources, connector_id="test.native")
    intake.meta["source_trust_decision"] = trust.to_dict()
    runtime.store.append(intake)
    intake = review_candidate(runtime, intake.record_id, "approve", "native-reviewer", scope=SCOPE)
    pdf = tmp_path / "native-paper.pdf"
    pdf.write_bytes(_minimal_pdf(TEXT))
    artifacts = materialize_paper_artifacts(runtime.store.root, {"pdf_file": str(pdf)})
    source = runtime.store.append(PaperSource(paper_source_id="native_gap_paper", source_kind="pdf",
        title="Bounded gap evidence", pdf_blob_ref=artifacts["pdf_blob_ref"], normalized_text_ref=artifacts["normalized_text_ref"],
        metadata={"artifact": artifacts["artifact"]},
        provenance={"input_kind": "paper_intake_pipeline", "record_id": intake.record_id}).to_record(scope=SCOPE))
    intake = mark_candidate_paper_promoted(runtime, intake, {"paper_source_id": source.record_id, "record_ids": [source.record_id]})
    claim = runtime.store.append(RecordEnvelope.create(kind="claim_card", scope=SCOPE,
        title="Known bounded gap", summary=TEXT, content=dict(paper_source_id=source.record_id, claim_text=TEXT),
        provenance=dict(paper_source_id=source.record_id), source="test.native"))
    def applicability(_system, prompt):
        value = json.loads(prompt)
        return json.dumps(dict(input_digest=value["input_digest"], capability_id=value["target"]["capability_id"],
            capability_revision_id=value["target"]["capability_revision_id"], applicability="applicable",
            relation_type="informs_eval", quotes=[TEXT]))
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", applicability)
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="eimemory.eval.native-gap", revision="v1",
        handler=lambda _input, _fixture, _runtime: {"valid": True})
    for index in range(2):
        catalog.register_case(CatalogCase(case_id=f"native_gap_case_{index}", capability_id=provider.CAPABILITY_ID,
            executor_id="eimemory.eval.native-gap", input_data={"request": "bounded check"}, fixture={"fixture_id": "native-gap"},
            expected_invariants=[{"field": "valid", "op": "eq", "value": True}],
            binding_selector={"operations_all": [provider.OPERATION]}))
    from eimemory.governance.capability.hypothesis_producer import produce_capability_hypotheses
    produced = produce_capability_hypotheses(runtime, profile_key=PROFILE, runtime_scope=SCOPE, catalog=catalog)
    assert produced["status"] == "produced", produced
    hypothesis = runtime.store.get_by_id(produced["created"][0]["hypothesis_id"], scope=SCOPE)
    assert hypothesis.status == "candidate" and hypothesis.content["candidate_bounds"]["max_changes"] == 1
    plan = evolution.build_dynamic_capability_evolution_plan(runtime, profile_key=PROFILE,
        runtime_scope=SCOPE, catalog=catalog)
    assert len(plan["work_items"]) == 1 and plan["work_items"][0]["status"] == "ready_for_independent_evaluation", plan
    item = plan["work_items"][0]
    report = evolution.execute_dynamic_capability_evolution(runtime, profile_key=PROFILE,
        runtime_scope=SCOPE, catalog=catalog, auto_propose_code_patch=False)
    assert report["results"][0].get("suite_passed") is True, json.dumps(report.get("evidence_collection"), indent=2)
    # The execution owner rebuilds its plan at its own exact evidence cutoff.
    # Use that real owner's reported work item coordinate, never a guessed
    # join to the earlier read-only preview's watermark.
    item = {**item, "work_item_id": report["results"][0]["work_item_id"]}
    gate = hypothesis_behavior_gate(runtime, runtime_scope=SCOPE, hypothesis_id=hypothesis.record_id)
    assert gate["allowed"] is True, gate
    # Receipt ownership is separately tested by evidence_contract. A local
    # native repository is bound to that exact test release, never a fake SHA.
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=base))
    yield SimpleNamespace(runtime=runtime, root=root, base=base, policy=policy, policy_path=policy_path,
        save_policy=save_policy, hypothesis=hypothesis, item=item, gate=gate, bounds=bounds, catalog=catalog,
        review_id=produced["knowledge_link_reviews"][0]["review_receipt_ids"][0],
        claim=claim, paper=source, intake=intake)
    runtime.close()


class AttestedProvider:
    def __init__(self):
        self.requests = []

    def propose_patch_v2(self, request):
        request = provider.validate_request(request)
        self.requests.append(request)
        source = request["allowed_files"][0]
        response = dict(schema="code_implementation_response.v2", request_id=request["request_id"],
            request_digest=request["request_digest"], file_updates=[dict(path=source["path"],
                prior_sha256=source["sha256"], content="def value():\n    return 2\n")],
            rationale="A bounded fixture fix with independent evidence.", assumptions=[])
        return dict(ok=True, operation="propose_patch_v2", response=response,
            attestation=provider.build_attestation(request, response, completed_at="2026-10-12T00:00:00Z", nonce=request["nonce"]))


def provider_boundary(monkeypatch):
    edge = AttestedProvider()
    monkeypatch.setattr(provider, "resolve_code_implementation_provider", lambda *_args, **_kwargs: dict(
        ok=True, provider_ready=True, provider=edge, implementation_digest=provider.IMPLEMENTATION_DIGEST,
        provider_instance_id=provider.PROVIDER_INSTANCE_ID, advertisement_id="native-advertisement",
        advertisement_digest="c" * 64, catalog_case_id="native-catalog", catalog_snapshot_digest="d" * 64))
    return edge


def proposal(native_gap, monkeypatch):
    edge = provider_boundary(monkeypatch)
    opportunity, report = context.propose_dynamic_patch(native_gap.runtime, scope=SCOPE,
        item=native_gap.item, gate=native_gap.gate)
    assert report["ok"] is True, report
    return opportunity["code_evolution_proposal"], edge


def test_native_catalog_policy_repository_and_bridge_yield_known_maintenance(native_gap, monkeypatch):
    before = git(native_gap.root, "rev-parse", "HEAD")
    value, edge = proposal(native_gap, monkeypatch)
    assert value["repository"]["base_commit"] == before == native_gap.base
    assert value["test_plan"]["id"] == DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID
    assert edge.requests[0]["bounds"]["maximum_files"] == 1
    assert value["origin"] == "user_reported" and value["known_before_detection"] is True
    assert value["manual_bootstrap"] is False
    assert context.dynamic_patch_proposal_authority_error(native_gap.runtime, value, scope=SCOPE) == ""
    assert git(native_gap.root, "status", "--porcelain") == ""
    assert git(native_gap.root, "rev-parse", "HEAD") == before
    assert native_gap.runtime.store.list_records(kinds=["capability_candidate"], scope=SCOPE, limit=20) == []
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager, qualification_report
    result = CodeEvolutionTransactionManager(native_gap.runtime).submit_proposal(value, scope=asdict(SCOPE), apply=False)
    assert result["transaction"]["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"
    assert qualification_report(result["transaction"])["qualifies_for_product_completion"] is False


@pytest.mark.parametrize("mutation,reason", [
    ("missing_policy", "policy_file_missing"),
    ("missing_root", "trusted_repository_root_unset"),
    ("dirty_repo", "repository_worktree_not_clean"),
    ("wrong_base", "dynamic_patch_hypothesis_revoked"),
    ("zero_bounds", "dynamic_patch_hypothesis_context_mismatch"),
    ("missing_hypothesis", "dynamic_patch_hypothesis_evidence_unavailable"),
])
def test_context_missing_authority_never_calls_provider(native_gap, monkeypatch, mutation, reason):
    item = deepcopy(native_gap.item)
    if mutation == "missing_policy":
        native_gap.policy_path.unlink()
    elif mutation == "missing_root":
        monkeypatch.delenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT")
        monkeypatch.delenv("EIMEMORY_DEPLOYMENT_REPO_ROOT", raising=False)
        monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: (_ for _ in ()).throw(RuntimeError()))
    elif mutation == "dirty_repo":
        (native_gap.root / PATH).write_text("changed\n")
    elif mutation == "wrong_base":
        native_gap.policy["repository"]["base_commit"] = "a" * 40
        native_gap.save_policy()
    elif mutation == "zero_bounds":
        item["candidate_bounds"]["max_changes"] = 0
    elif mutation == "missing_hypothesis":
        item["hypothesis_id"] = "missing"
    edge = provider_boundary(monkeypatch)
    opportunity, report = context.propose_dynamic_patch(native_gap.runtime, scope=SCOPE, item=item, gate=native_gap.gate)
    assert opportunity is None and report["reason"] == reason, report
    assert edge.requests == []


def test_provider_unavailable_is_explicit_after_real_context(native_gap, monkeypatch):
    monkeypatch.setattr(provider, "resolve_code_implementation_provider", lambda *_args, **_kwargs:
        dict(ok=False, reason="provider_unavailable"))
    opportunity, report = context.propose_dynamic_patch(native_gap.runtime, scope=SCOPE,
        item=native_gap.item, gate=native_gap.gate)
    assert opportunity is None and report["reason"] == "provider_unavailable"


def test_exact_incident_pin_can_be_added_without_cyclic_hypothesis_identity(native_gap, monkeypatch):
    before = context.build_dynamic_patch_context(native_gap.runtime, scope=SCOPE,
        item=native_gap.item, gate=native_gap.gate)
    assert before["ok"] is True, before
    old_bounds = deepcopy(native_gap.hypothesis.content["candidate_bounds"])
    native_gap.policy["incident"]["incident_digest"] = before["incident"]["incident_digest"]
    native_gap.save_policy()
    current_bounds = context.dynamic_patch_candidate_bounds(native_gap.runtime, runtime_scope=SCOPE,
        profile_key=PROFILE, capability_id=provider.CAPABILITY_ID,
        capability_revision_id=provider.REVISION_ID, provider_binding_ids=[provider.BINDING_ID])
    assert current_bounds["ok"] is True
    assert current_bounds["candidate_bounds"]["machine_authority_digest"] == old_bounds["machine_authority_digest"]
    pinned = context.build_dynamic_patch_context(native_gap.runtime, scope=SCOPE,
        item=native_gap.item, gate=native_gap.gate)
    assert pinned["ok"] is True, pinned
    assert pinned["incident"] == before["incident"]
    assert pinned["policy_digest"] != before["policy_digest"]
    value, _edge = proposal(native_gap, monkeypatch)
    assert value["dynamic_capability_context"]["policy_digest"] == pinned["policy_digest"]
    assert value["incident"]["incident_digest"] == native_gap.policy["incident"]["incident_digest"]
    native_gap.policy["incident"]["incident_digest"] = "0" * 64
    native_gap.save_policy()
    rejected = context.build_dynamic_patch_context(native_gap.runtime, scope=SCOPE,
        item=native_gap.item, gate=native_gap.gate)
    assert rejected["reason"] == "dynamic_patch_policy_incident_digest_mismatch"


def _existing_catalog_coordinates(native_gap):
    rows = native_gap.runtime.store.read_capabilities(lambda repository: repository.list_evaluation_runs(
        scope=SCOPE, capability_scope="global", capability_revision_id=provider.REVISION_ID,
        provider_binding_id=provider.BINDING_ID))
    row = next(row for row in rows if row["payload"]["provenance"]["case_id"] == "native_gap_case_0")
    case = next(case for case in native_gap.item["evaluation_cases"] if case["artifact"]["case_id"] == "native_gap_case_0")
    exact = context._evidence_context(native_gap.runtime, SCOPE, native_gap.item, native_gap.gate)
    selected = next(value for value in exact["evidence"] if value["case_id"] == "native_gap_case_0")
    return row, dict(artifact=case["artifact"], target=case["target"], runtime_scope=SCOPE,
        capability_scope="global", evidence_ref=selected["probe_id"],
        probe_id=selected["probe_id"], trace_record_id=selected["trace_record_id"],
        execution_id=row["payload"]["provenance"]["execution_id"])


def test_catalog_revalidation_reuses_exact_run_and_observation(native_gap, monkeypatch):
    row, coordinates = _existing_catalog_coordinates(native_gap)
    monkeypatch.setattr("eimemory.evaluation.capability_catalog._utc_now", lambda: "2098-01-01T00:00:00.000000Z")
    first = native_gap.catalog.execute_and_persist(native_gap.runtime, **coordinates)
    again = native_gap.catalog.execute_and_persist(native_gap.runtime, **coordinates)
    assert first["run"].run_id == again["run"].run_id == row["run_id"]
    assert first["run"].run_digest == again["run"].run_digest == row["run_digest"]
    assert first["run"].started_at == row["payload"]["started_at"]
    assert first["observation_receipt"].entity_id == again["observation_receipt"].entity_id
    assert first["run_receipt"].idempotent is True and again["run_receipt"].idempotent is True


@pytest.mark.parametrize("mutation", ["valid_digest_wrong_metrics", "wrong_scope", "tampered_case"])
def test_catalog_same_run_id_cannot_bypass_exact_input_scope_or_verdict(native_gap, mutation):
    from dataclasses import fields
    from eimemory.capabilities.models import EvaluationRun
    from eimemory.evaluation.capability_catalog import CatalogResolutionError
    row, coordinates = _existing_catalog_coordinates(native_gap)
    if mutation == "valid_digest_wrong_metrics":
        original = EvaluationRun(**{field.name: row["payload"][field.name] for field in fields(EvaluationRun)
            if field.init and field.name in row["payload"]})
        forged = replace(original, metrics={**dict(original.metrics), "pass_rate": .5})
        # Native storage corruption fixture: changing a normalized digest too
        # must not let a same-id run substitute different evaluated metrics.
        def corrupt(sql):
            with sql.conn:
                sql.conn.execute("UPDATE evaluation_runs SET payload_json=?, run_digest=? WHERE run_id=? AND tenant_id=?",
                    (json.dumps(forged.to_dict()), forged.run_digest, row["run_id"], SCOPE.tenant_id))
        native_gap.runtime.store.run_locked(corrupt)
    elif mutation == "wrong_scope":
        coordinates["runtime_scope"] = replace(SCOPE, user_id="other")
    else:
        coordinates["artifact"] = deepcopy(coordinates["artifact"])
        coordinates["artifact"]["input"]["request"] = "tampered"
    with pytest.raises(CatalogResolutionError):
        native_gap.catalog.execute_and_persist(native_gap.runtime, **coordinates)


@pytest.mark.parametrize("path", [
    "tests/test_dynamic_code_patch_context.py", "deploy/install.sh",
    "eimemory/governance/evolution/code_evolution_dynamic_context.py",
    "eimemory/governance/evolution/dynamic_capability_evolution.py",
    "eimemory/governance/capability/hypothesis_producer.py",
    "eimemory/governance/capability/knowledge_gap_review.py",
    "eimemory/governance/capability/capability_hypotheses.py", "eimemory/knowledge/capabilities.py",
])
def test_registered_gap_plan_cannot_rewrite_tests_or_its_evidence_authority(path):
    from eimemory.governance.evolution.code_evolution_test_plans import path_allowed_for_incident
    assert not path_allowed_for_incident(context.INCIDENT_CLASS, path, test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID)


def _native_pending_transaction(native_gap, monkeypatch, value):
    from eimemory.governance.evolution import code_evolution_effects as effects
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
    manager = CodeEvolutionTransactionManager(native_gap.runtime, owner_id="native-gap-test")
    # Capture only the effect entry boundary; the durable manager and marker
    # are real. Each test then calls the real owner against that pending row.
    monkeypatch.setattr(effects, "execute_code_evolution_effects", lambda *_args, **_kwargs: dict(ok=True))
    manager.submit_proposal(value, scope=asdict(SCOPE), effects_enabled=True, apply=True)
    return manager


@pytest.mark.parametrize("mutation,reason", [
    ("remove_context", "dynamic_patch_durable_context_missing"),
    ("tamper_context", "dynamic_patch_durable_context_digest_mismatch"),
    ("revoke_hypothesis", "dynamic_patch_hypothesis_revoked"),
    ("revoke_non_anchor_trace", "dynamic_patch_independent_trace_invalid"),
    ("revoke_review", "dynamic_patch_hypothesis_revoked"),
    ("change_source", "dynamic_patch_hypothesis_revoked"),
    ("withdraw_approval", "dynamic_patch_hypothesis_revoked"),
])
def test_effect_owner_rejects_missing_or_revoked_context_before_materializing(native_gap, monkeypatch, mutation, reason):
    from eimemory.governance.evolution.code_evolution_effects import CodeEvolutionEffectOwner
    value, _edge = proposal(native_gap, monkeypatch)
    if mutation == "remove_context":
        value.pop("dynamic_capability_context")
    elif mutation == "tamper_context":
        value["dynamic_capability_context"]["base_commit"] = "a" * 40
    elif mutation == "revoke_hypothesis":
        from eimemory.governance.capability.hypothesis_producer import revoke_produced_hypothesis
        revoke_produced_hypothesis(native_gap.runtime, runtime_scope=SCOPE,
            hypothesis_id=native_gap.hypothesis.record_id, reason="Reviewed evidence revoked")
    elif mutation == "revoke_non_anchor_trace":
        evidence = context._evidence_context(native_gap.runtime, SCOPE, native_gap.item, native_gap.gate)
        trace_id = evidence["evidence"][0]["trace_record_id"]
        trace = native_gap.runtime.store.get_by_id(trace_id, scope=SCOPE)
        trace.status = "rejected"
        native_gap.runtime.store.rewrite(trace)
    elif mutation == "revoke_review":
        from eimemory.governance.capability.knowledge_gap_review import revoke_knowledge_gap_review
        revoke_knowledge_gap_review(native_gap.runtime, runtime_scope=SCOPE,
            review_receipt_id=native_gap.review_id, reason="Withdraw applicability")
    elif mutation == "change_source":
        native_gap.claim.summary = "Source claim was corrected after this review."
        native_gap.runtime.store.rewrite(native_gap.claim)
    elif mutation == "withdraw_approval":
        native_gap.intake.meta["review"] = {"decision": "reject", "reviewer": "native-reviewer"}
        native_gap.intake.content["review"] = {"decision": "reject", "reviewer": "native-reviewer"}
        native_gap.runtime.store.rewrite(native_gap.intake)
    manager = _native_pending_transaction(native_gap, monkeypatch, value)

    class NoEffects:
        def __getattr__(self, name):
            raise AssertionError("No adapter calls are allowed: " + name)

    owner = CodeEvolutionEffectOwner(native_gap.runtime, owner_id="native-gap-test", adapter=NoEffects(),
        policy_loader=context._load_policy, policy_consumer=lambda **_: (_ for _ in ()).throw(AssertionError("no consumption")))
    result = owner.execute(value["transaction_id"])
    assert result["blocked_reason"] == reason, result
    assert manager.store.get_transaction(value["transaction_id"])["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"


def test_recovered_commit_stops_forward_effects_when_hypothesis_revoked(native_gap, monkeypatch):
    from eimemory.governance.evolution.code_evolution_effects import CodeEvolutionEffectOwner
    from eimemory.governance.capability.hypothesis_producer import revoke_produced_hypothesis
    value, _edge = proposal(native_gap, monkeypatch)
    manager = _native_pending_transaction(native_gap, monkeypatch, value)
    for state in ("CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED"):
        manager.record_result(value["transaction_id"], step=state.lower(), result_state=state, output_data={"test_only": True})
    manager.begin_intent(value["transaction_id"], step="commit", intent_state="COMMIT_INTENT", input_data={})
    manager.record_result(value["transaction_id"], step="commit", result_state="COMMITTED", output_data={}, updates={"candidate_commit": "c" * 40})
    revoke_produced_hypothesis(native_gap.runtime, runtime_scope=SCOPE,
        hypothesis_id=native_gap.hypothesis.record_id, reason="Reviewed evidence revoked")
    owner = CodeEvolutionEffectOwner(native_gap.runtime, owner_id="native-gap-test", adapter=object(),
        policy_loader=context._load_policy, policy_consumer=lambda **_: {})
    result = owner.execute(value["transaction_id"])
    assert result["blocked_reason"] == "dynamic_patch_hypothesis_revoked", result
    assert manager.store.get_transaction(value["transaction_id"])["current_state"] == "COMMITTED"
    assert result["transaction"]["candidate_commit"] == "c" * 40
