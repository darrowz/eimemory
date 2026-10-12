"""Native source/registry/approval/review persistence; no live model calls."""
from dataclasses import replace
import json
import sys

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability import knowledge_gap_review as review
from eimemory.governance.capability.hypothesis_producer import (
    produce_capability_hypotheses, revoke_produced_hypothesis, revoked_hypothesis_ids,
)
from eimemory.governance.capability.capability_hypotheses import list_capability_hypotheses, hypothesis_behavior_gate
from eimemory.governance.evolution.dynamic_capability_evolution import build_dynamic_capability_evolution_plan
from eimemory.intake.papers.artifacts import materialize_paper_artifacts
from eimemory.intake.review import mark_candidate_paper_promoted, review_candidate
from eimemory.knowledge.capabilities import list_registered_knowledge_links
from eimemory.knowledge.source_trust import resolve_source_trust
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_dynamic_capability_evals import SCOPE, _definition, _revision, _binding, _profile, _catalog
from test_paper_pdf_pipeline import _minimal_pdf


TEXT = "Canonical operational evidence shows that bounded decisions preserve traceable source references."


@pytest.fixture
def registered(tmp_path):
    pytest.importorskip("pypdf")
    runtime = Runtime.create(root=tmp_path / "runtime")
    definition = _definition()
    revision = _revision(definition)
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(_binding(definition, revision), runtime_scope=SCOPE)
    runtime.capabilities.register_profile(_profile(definition), runtime_scope=SCOPE)
    yield runtime, _profile(definition), _catalog(), tmp_path
    runtime.close()


def _evidence(registered, *, suffix="1", approved=True, scope=None, context_revision=None):
    runtime, profile, catalog, path = registered
    scope = ScopeRef.from_dict(scope or SCOPE)
    uri = f"https://paper.example.test/{suffix}"
    source = runtime.sources.add_source({"source_id": f"source-{suffix}", "source_kind": "paper",
                                        "uri": uri, "enabled": True,
                                        "metadata": {"connector_id": "test.native", "trust": 0.85}})
    candidate = RecordEnvelope.create(kind="knowledge_candidate", title=f"Paper {suffix}",
        summary=TEXT, content={"source_id": source.source_id, "source_kind": "paper", "source_uri": uri},
        scope=scope, source="test.native", status="candidate")
    decision = resolve_source_trust(candidate.to_dict(), registry=runtime.sources, connector_id="test.native")
    assert decision.reasons == ("registry_verified",)
    candidate.meta["source_trust_decision"] = decision.to_dict()
    runtime.store.append(candidate)
    if approved:
        candidate = review_candidate(runtime, candidate.record_id, "approve", "native-reviewer", scope=scope)
    pdf = path / f"paper-{suffix}.pdf"
    pdf.write_bytes(_minimal_pdf(TEXT))
    artifacts = materialize_paper_artifacts(runtime.store.root, {"pdf_file": str(pdf)})
    paper = PaperSource(paper_source_id=f"paper-{suffix}", source_kind="pdf", title=f"Paper {suffix}",
        pdf_blob_ref=artifacts["pdf_blob_ref"], normalized_text_ref=artifacts["normalized_text_ref"],
        metadata={"artifact": artifacts["artifact"]},
        provenance={"input_kind": "paper_intake_pipeline", "record_id": candidate.record_id}).to_record(scope=scope)
    runtime.store.append(paper)
    candidate = mark_candidate_paper_promoted(runtime, candidate, {"paper_source_id": paper.record_id, "record_ids": [paper.record_id]})
    claim = RecordEnvelope.create(kind="claim_card", title="Canonical claim", summary=TEXT,
        content={"paper_source_id": paper.record_id, "claim_text": TEXT}, scope=scope, source="test.native")
    if context_revision is not None:
        claim.content["metadata"] = {"capability_context": {"capability_id": "dynamic.catalog",
            "capability_revision_id": context_revision, "capability_scope": "global", "relation_type": "supports"}}
    runtime.store.append(claim)
    return claim, paper, candidate


def _model(_system, prompt):
    value = json.loads(prompt)
    return json.dumps({"input_digest": value["input_digest"],
        "capability_id": value["target"]["capability_id"],
        "capability_revision_id": value["target"]["capability_revision_id"],
        "applicability": "applicable", "relation_type": "informs_eval", "quotes": [TEXT]})


def _run(registered, executor=_model):
    runtime, _, _, _ = registered
    return review.review_gap_knowledge(runtime, runtime_scope=SCOPE, capability_scope="global",
        capability_id="dynamic.catalog", revision_id="dynamic.catalog:v1",
        binding_ids=["binding.dynamic.catalog:v1"], executor=executor)


def test_approved_canonical_review_links_native_current_gap_to_one_candidate(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    _evidence(registered)
    # Exercise the real bounded command/tool-free envelope validation with a
    # local synthetic provider. Discovery alone is replaced; completion,
    # subprocess transport, structured output and durable evidence are native.
    script = registered[3] / "synthetic_tool_free_provider.py"
    script.write_text("import json, sys\n"
        "request = json.loads(sys.stdin.buffer.read())\n"
        "value = json.loads(request['user'])\n"
        "output = dict(input_digest=value['input_digest'], capability_id=value['target']['capability_id'], "
        "capability_revision_id=value['target']['capability_revision_id'], applicability='applicable', "
        "relation_type='informs_eval', quotes=[value['source']['canonical_window']])\n"
        "sys.stdout.write(json.dumps(dict(text=json.dumps(output), tools=[])))\n", encoding="utf-8")
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.runtime_command", lambda _env: [sys.executable, "-I", "-B", str(script)])
    report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert len(report["created"]) == 1
    assert report["knowledge_link_reviews"][0]["applicable_count"] == 1
    link_id = report["created"][0]["link_id"]
    receipts = report["knowledge_link_reviews"][0]["review_receipt_ids"]
    stored = runtime.store.get_by_id(receipts[0], scope=ScopeRef.from_dict(SCOPE))
    assert stored.content["code_changes_authorized"] is False
    assert stored.content["quote_refs"][0]["length"] == len(TEXT)
    assert TEXT not in json.dumps(stored.to_dict())
    hypotheses = list_capability_hypotheses(runtime, runtime_scope=SCOPE)
    assert len(hypotheses) == 1 and hypotheses[0].status == "candidate"
    assert hypotheses[0].content["expected_metric"]["gap_evidence"]["knowledge_review_receipt_id"] == receipts[0]
    assert hypotheses[0].content["link_id"] == link_id
    assert hypothesis_behavior_gate(runtime, runtime_scope=SCOPE, hypothesis_id=hypotheses[0].record_id)["allowed"] is False
    assert hypotheses[0].content["candidate_bounds"]["max_changes"] == 0


@pytest.mark.parametrize("condition", ["unreviewed", "expired", "contradicted", "foreign", "stale_revision", "trust_disabled"])
def test_ineligible_knowledge_never_calls_model_or_creates_link(registered, condition):
    runtime, _, _, _ = registered
    claim, paper, candidate = _evidence(registered,
        approved=condition != "unreviewed", scope={**SCOPE, "user_id": "foreign"} if condition == "foreign" else None,
        context_revision="dynamic.catalog:old" if condition == "stale_revision" else None)
    if condition == "expired":
        claim.content["temporal_validity"] = {"expires_at": "2020-01-01T00:00:00Z"}
        runtime.store.rewrite(claim, previous_scope=claim.scope)
    elif condition == "contradicted":
        claim.content["contradiction_ids"] = ["contradiction-real"]
        runtime.store.rewrite(claim, previous_scope=claim.scope)
    elif condition == "trust_disabled":
        current = next(item for item in runtime.sources.list_sources() if item.source_id == "source-1")
        runtime.sources.add_source({**current.to_dict(), "enabled": False})
    calls = []
    report = _run(registered, lambda *args: calls.append(args))
    assert calls == [] and report["links"] == []
    assert list_registered_knowledge_links(runtime, runtime_scope=SCOPE, capability_scope="global") == []


def test_multiple_applicable_sources_are_not_chosen_by_order_or_volume(registered):
    runtime, _, _, _ = registered
    _evidence(registered, suffix="1")
    _evidence(registered, suffix="2")
    report = _run(registered)
    assert report["applicable_count"] == 2 and report["links"] == []
    assert report["reason_counts"]["ambiguous_reviewed_knowledge_for_gap_revision"] == 1
    assert list_registered_knowledge_links(runtime, runtime_scope=SCOPE, capability_scope="global") == []


def test_retry_is_idempotent_and_review_revocation_cannot_be_automatically_revived(registered):
    runtime, _, _, _ = registered
    _evidence(registered)
    first = _run(registered)
    again = _run(registered, lambda *_: pytest.fail("cache should avoid model call"))
    assert first["links"] == again["links"] and again["reviewed_count"] == 0
    receipt = first["review_receipt_ids"][0]
    review.revoke_knowledge_gap_review(runtime, runtime_scope=SCOPE, review_receipt_id=receipt, reason="not applicable")
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt) is False
    revoked = _run(registered, lambda *_: pytest.fail("revoked inputs must not be rereviewed"))
    assert not revoked["links"] and revoked["reason_counts"]["applicability_review_revoked"] == 1


def test_current_source_update_invalidates_old_receipt_and_planner_candidate(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    claim, _, _ = _evidence(registered)
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", _model)
    first = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    hypothesis_id = first["created"][0]["hypothesis_id"]
    receipt = first["knowledge_link_reviews"][0]["review_receipt_ids"][0]
    claim.content["contradiction_ids"] = ["new-contradiction"]
    runtime.store.rewrite(claim, previous_scope=claim.scope)
    assert review.knowledge_gap_review_current(runtime, runtime_scope=SCOPE, review_receipt_id=receipt) is False
    assert hypothesis_id in revoked_hypothesis_ids(runtime, runtime_scope=SCOPE)
    plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert plan["work_items"][0]["detail"]["candidate_hypothesis_count"] == 0
    next_report = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    assert next_report["created"] == []


def test_quote_and_revision_binding_are_verified_not_model_assertions(registered):
    _evidence(registered)
    def forged(system, prompt):
        result = json.loads(_model(system, prompt))
        result["quotes"] = ["Invented source text has never existed in this canonical paper."]
        return json.dumps(result)
    report = _run(registered, forged)
    assert not report["links"] and report["reason_counts"]["applicability_review_quote_invalid"] == 1


def test_source_change_during_review_does_not_certify_the_old_snapshot(registered):
    runtime, _, _, _ = registered
    claim, _, _ = _evidence(registered)
    def racing(system, prompt):
        claim.status = "stale"
        runtime.store.rewrite(claim, previous_scope=claim.scope)
        return _model(system, prompt)
    report = _run(registered, racing)
    assert not report["links"] and report["review_receipt_ids"] == []


def test_transport_failure_is_visible_and_does_not_print_private_error(registered):
    _evidence(registered)
    def unavailable(*_):
        raise RuntimeError("private model credential and business source")
    report = _run(registered, unavailable)
    assert report["reason_counts"]["applicability_review_transport_unavailable"] == 1
    assert "private" not in json.dumps(report) and report["links"] == []


def test_stale_plan_does_not_create_duplicate_or_revive_revoked_hypothesis(registered, monkeypatch):
    runtime, profile, catalog, _ = registered
    _evidence(registered)
    monkeypatch.setattr("eimemory.llm.hermes_tool_free.complete", _model)
    stale_plan = build_dynamic_capability_evolution_plan(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog)
    first = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    stale_plan["projection_digest"] = "new-projection"
    second = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    assert len(first["created"]) == 1 and second["created"] == [] and len(second["reused"]) == 1
    hypothesis_id = first["created"][0]["hypothesis_id"]
    revoke_produced_hypothesis(runtime, runtime_scope=SCOPE, hypothesis_id=hypothesis_id, reason="do not retry")
    third = produce_capability_hypotheses(runtime, profile_key=profile.profile_key, runtime_scope=SCOPE, catalog=catalog, plan=stale_plan)
    assert not third["created"] and not third["reused"]
    assert third["skipped"][0]["reason"] == "produced_hypothesis_revoked"
    assert len(list_capability_hypotheses(runtime, runtime_scope=SCOPE)) == 1


@pytest.mark.parametrize("declaration", ["claim_meta_expired", "source_meta_expired", "claim_future",
                                       "source_malformed", "conflicting_environment"])
def test_every_source_and_claim_validity_declaration_is_respected(registered, declaration):
    runtime, _, _, _ = registered
    claim, paper, _ = _evidence(registered)
    if declaration == "claim_meta_expired":
        claim.meta["valid_until"] = "2020-01-01T00:00:00Z"
    elif declaration == "source_meta_expired":
        paper.meta["expires_at"] = "2020-01-01T00:00:00Z"
    elif declaration == "claim_future":
        claim.provenance["valid_from"] = "2099-01-01T00:00:00Z"
    elif declaration == "source_malformed":
        paper.content["metadata"]["temporal_validity"] = {"valid_until": "invalid-private-date"}
    elif declaration == "conflicting_environment":
        paper.meta["environment_constraints"] = {"required": {"runtime": "other"}}
        claim.meta["environment_constraints"] = {"required": {"runtime": "test"}}
    runtime.store.rewrite(claim, previous_scope=claim.scope)
    runtime.store.rewrite(paper, previous_scope=paper.scope)
    calls = []
    report = _run(registered, lambda *args: calls.append(args))
    assert not calls and not report["links"] and report["eligible_count"] == 0
    assert "invalid-private-date" not in json.dumps(report)


def test_more_than_four_inputs_progress_across_runs_without_claiming_partial_uniqueness(registered):
    runtime, _, _, _ = registered
    claims = [_evidence(registered, suffix=str(index))[0] for index in range(5)]
    positive_id = min(claim.record_id for claim in claims)
    calls = []
    def discriminate(system, prompt):
        value = json.loads(prompt)
        calls.append(value["source"]["knowledge_record_id"])
        result = json.loads(_model(system, prompt))
        if value["source"]["knowledge_record_id"] != positive_id:
            result["applicability"] = "not_applicable"
            result["quotes"] = []
        return json.dumps(result)
    first = _run(registered, discriminate)
    assert first["eligible_count"] == 5 and first["reviewed_count"] == 4
    assert first["completed_count"] == 4 and first["deferred_count"] == 1
    assert first["applicable_count"] == 1 and first["links"] == []
    assert len(calls) == 4
    second = _run(registered, discriminate)
    assert second["reviewed_count"] == 1 and second["completed_count"] == 5 and second["deferred_count"] == 0
    assert second["applicable_count"] == 1 and len(second["links"]) == 1
    assert len(calls) == 5 and len(set(calls)) == 5
    third = _run(registered, lambda *_: pytest.fail("all terminal inputs should be cached"))
    assert third["links"] == second["links"] and third["reviewed_count"] == 0


def test_strict_revocation_inventory_is_required_before_forward_authority(registered, monkeypatch):
    runtime, _, _, _ = registered
    def unavailable(**_):
        raise RuntimeError("private database credentials")
    monkeypatch.setattr(runtime.store, "list_records_by_meta_value", unavailable)
    with pytest.raises(RuntimeError, match="^hypothesis_revocation_inventory_unavailable$"):
        revoked_hypothesis_ids(runtime, runtime_scope=SCOPE, strict=True)


def test_strict_fallback_rejects_truncated_raw_inventory_before_filtering(registered, monkeypatch):
    runtime, _profile, _catalog, _path = registered
    for index in range(501):
        runtime.store.append(RecordEnvelope.create(kind="reflection", title=f"Ordinary event {index}",
            content={"report_type": "ordinary_event"}, scope=ScopeRef.from_dict(SCOPE), source="ordinary.events"))
    monkeypatch.setattr(runtime.store, "list_records_by_meta_value", None)
    # A generic reflection window cannot establish that older revocations are
    # absent merely because the visible rows have another report type.
    with pytest.raises(RuntimeError, match="^hypothesis_revocation_inventory_truncated$"):
        revoked_hypothesis_ids(runtime, runtime_scope=SCOPE, strict=True)
