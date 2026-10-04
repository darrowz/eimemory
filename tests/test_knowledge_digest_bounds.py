"""Finite leaf-level P0/D1 checks; no Runtime/provider initialization."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest

from eimemory.knowledge.evidence_contracts import (
    assess_confidence_values, assess_record_confidence, declared_confidences,
    finite_confidence_floor, record_version_digest, versioned_record_ref,
)
from eimemory.knowledge.evidence_gate import filter_answer_evidence, grade_research_evidence
from eimemory.knowledge.projectors import _candidate_from_record
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef

SCOPE = ScopeRef(tenant_id="synthetic", agent_id="research", workspace_id="digest", user_id="owner")
TEXT = "The runtime memory policy must prioritize verified tenant scoped evidence for operational recall."


def record(identifier="claim_valid", *, kind="claim_card", confidence=.87, status="active"):
    value = RecordEnvelope.create(kind=kind, title=identifier, summary=TEXT, scope=SCOPE,
        source="test.research", status=status,
        content={"claim_text": TEXT, "confidence": confidence, "source_url": "https://example.test/source",
                 "published_at": "2026-10-01", "paper_source_id": "paper_test"},
        meta={"confidence": confidence})
    value.record_id = identifier
    value.time = TimeRef("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z")
    return value


def digest(claims):
    return build_research_digest(paper_sources=[], claim_cards=claims, knowledge_pages=[], limit=20)


def test_missing_and_explicit_null_are_distinct_in_pure_helper():
    missing = {"content": {}, "meta": {}}
    assert declared_confidences(missing) == ()
    assert assess_record_confidence(missing).state == "missing"
    assert assess_record_confidence(missing).value is None
    for container, key in (("meta", "reliability"), ("meta", "confidence"), ("content", "confidence")):
        payload = {"content": {}, "meta": {}}
        payload[container][key] = None
        assert declared_confidences(payload) == (None,)
        assert assess_record_confidence(payload).state == "invalid"
        assert assess_record_confidence(payload).value is None


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), float("-inf"),
    "NaN", "Infinity", "-Infinity", "unknown", "", [], {}, -0.01, 1.01])
def test_present_invalid_values_cannot_fall_back_to_valid_alternatives(value):
    assessment = assess_confidence_values(.87, value, .95)
    assert assessment.state == "invalid" and assessment.value is None
    assert finite_confidence_floor(.87, value, .95) == 0.0


@pytest.mark.parametrize("values,expected", [((0.0, .87), 0.0), ((.75, .87), .75), (("0.72", .91), .72), ((1,), 1.0)])
def test_multiple_valid_declarations_use_conservative_minimum(values, expected):
    assessment = assess_confidence_values(*values)
    assert assessment.state == "valid" and assessment.value == expected
    assert finite_confidence_floor(*values) == expected


def test_ref_and_digest_match_projector_v2_canonical_encoding():
    src = record()
    src.content["unicode"] = "研究"
    src.source_id = "research-a"
    encoded = json.dumps(src.to_dict(), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    expected_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    assert record_version_digest(src) == expected_digest
    assert versioned_record_ref(src) == {"record_id": src.record_id, "kind": src.kind,
        "scope": asdict(SCOPE), "source_id": "research-a", "version_digest": expected_digest}
    src.summary += " A new version."
    assert record_version_digest(src) != expected_digest


@pytest.mark.parametrize("kind", ["claim_card", "paper_source", "knowledge_page"])
@pytest.mark.parametrize("status", ["candidate", "rejected", "quarantined", "rolled_back", "deprecated", "superseded",
    "archived", "deleted", "blocked", "conflicted", "needs_refresh", "unknown", "ACTIVE", ""])
def test_research_gate_rejects_every_nonactive_status(kind, status):
    src = record(kind=kind, status=status)
    gate = grade_research_evidence(src)
    assert not gate["ok"] and "inactive_status" in gate["reasons"]
    assert gate["conflict_check"] != "clear"


def test_missing_status_on_raw_mapping_is_not_assumed_active():
    payload = record().to_dict()
    del payload["status"]
    assert grade_research_evidence(payload)["reason"] == "inactive_status"


@pytest.mark.parametrize("kind", ["claim_card", "paper_source", "knowledge_page"])
@pytest.mark.parametrize("container,key", [("meta", "reliability"), ("meta", "confidence"), ("content", "confidence")])
def test_gate_explicit_null_rejects_despite_other_valid_fields(kind, container, key):
    src = record(kind=kind)
    getattr(src, container)[key] = None
    gate = grade_research_evidence(src)
    assert not gate["ok"]
    assert gate["confidence_state"] == "invalid" and "invalid_confidence" in gate["reasons"]
    assert gate["evidence_tier"] == "unknown"


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf"), "unknown", -1, 2])
def test_gate_invalid_confidence_is_json_safe_and_never_high_trust(value):
    src = record()
    src.content["confidence"] = value
    gate = grade_research_evidence(src)
    assert not gate["ok"] and gate["confidence_state"] == "invalid"
    assert gate["confidence"] == 0.0 and gate["evidence_tier"] == "unknown"
    json.dumps(gate, allow_nan=False)


def test_missing_confidence_remains_unknown_not_measured_zero():
    src = record()
    src.meta.pop("confidence"); src.content.pop("confidence")
    gate = grade_research_evidence(src)
    assert not gate["ok"] and gate["confidence_state"] == "missing"
    assert gate["evidence_tier"] == "unknown"
    assert gate["reasons"] == ["missing_confidence"]


@pytest.mark.parametrize("container", ["content", "meta", "provenance"])
@pytest.mark.parametrize("field", ["conflict", "contradiction_ids", "contradiction_claim_ids", "deprecated"])
def test_gate_honors_conflict_and_deprecation_across_supported_containers(container, field):
    src = record()
    getattr(src, container)[field] = ["conflict_1"] if field.endswith("ids") else True
    gate = grade_research_evidence(src)
    assert not gate["ok"]
    assert ("deprecated_source" if field == "deprecated" else "conflict_unresolved") in gate["reasons"]
    if field != "deprecated": assert gate["conflict_check"] == "unresolved"


def test_actual_reconciliation_shape_is_not_republished_as_clear_evidence():
    src = record(confidence=.95, status="conflicted")
    src.meta["reliability"] = .712
    src.content["confidence"] = .712
    src.meta["contradiction_claim_ids"] = ["claim_other"]
    src.content["contradiction_claim_ids"] = ["claim_other"]
    result = digest([src])
    assert result["notable_claims"] == [] and result["claim_count"] == 0
    assert result["scanned_claim_count"] == 1 and result["excluded_claim_count"] == 1
    assert "conflict_unresolved" in result["evidence_gate"]["excluded"][0]["reasons"]


def test_resolved_audit_history_is_not_a_direct_unresolved_conflict():
    src = record()
    for container in (src.content, src.meta, src.provenance):
        container["resolved_contradiction_ids"] = ["old_audit"]
    assert grade_research_evidence(src)["ok"]


@pytest.mark.parametrize("confidence,eligible", [(0.0, False), (.499, False), (.5, True), (.72, True), (.75, True), (1.0, True)])
def test_research_point_five_threshold_remains_distinct_from_operational(confidence, eligible):
    src = record(confidence=confidence)
    assert grade_research_evidence(src)["ok"] is eligible
    candidate, reason = _candidate_from_record(src)
    assert (candidate is not None) is (confidence >= .75)
    if confidence < .75: assert reason == "low_confidence"


def test_ranking_display_counts_share_exact_same_eligible_normalized_set():
    low_but_raw_high = record("claim_low", confidence=.95)
    low_but_raw_high.meta["reliability"] = .51
    better = record("claim_better", confidence=.6)
    blocked = record("claim_blocked", confidence=.99, status="rejected")
    malformed = record("claim_bad", confidence="unknown")
    null = record("claim_null")
    null.content["confidence"] = None
    missing = record("claim_missing")
    missing.content.pop("confidence"); missing.meta.pop("confidence")
    zero = record("claim_zero", confidence=.87)
    zero.content["confidence"] = 0.0
    result = digest([low_but_raw_high, better, blocked, malformed, null, missing, zero])
    assert [item["claim_id"] for item in result["notable_claims"]] == ["claim_better", "claim_low"]
    assert [item["confidence"] for item in result["notable_claims"]] == [.6, .51]
    assert result["claim_count"] == 2
    assert result["scanned_claim_count"] == 7 and result["excluded_claim_count"] == 5
    assert result["evidence_gate"]["excluded_count"] == 5
    counts = result["skipped_low_confidence"]
    assert counts["low_confidence_claim_count"] == 1
    assert counts["invalid_confidence_claim_count"] == 2
    assert counts["missing_confidence_claim_count"] == 1
    assert counts["inactive_claim_count"] == 1
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad_value", ["garbage", None, True, float("nan"), float("inf"), {}, [], -1, 2])
def test_one_bad_excluded_record_cannot_crash_or_poison_viable_digest(bad_value):
    good = record("claim_good", confidence=.72)
    bad = record("claim_bad")
    bad.content["confidence"] = bad_value
    result = digest([bad, good])
    assert result["ok"] and result["claim_count"] == 1 and result["excluded_claim_count"] == 1
    assert [item["claim_id"] for item in result["notable_claims"]] == [good.record_id]
    assert result["skipped_low_confidence"]["invalid_confidence_claim_count"] == 1
    json.dumps(result, allow_nan=False)


def test_all_excluded_claims_do_not_make_a_digest_ok_or_count_as_eligible():
    result = digest([record(status="rejected"), record("claim_other", confidence="unknown")])
    assert not result["ok"] and result["claim_count"] == 0
    assert result["scanned_claim_count"] == result["excluded_claim_count"] == 2
    assert result["notable_claims"] == []


def test_open_questions_and_source_count_exclude_inactive_claims():
    src = record(status="rejected")
    src.summary = "Open question: should the rejected runtime policy be trusted?"
    result = digest([src])
    assert result["open_questions"] == [] and result["source_ids"] == [] and result["paper_count"] == 0


def test_final_answer_research_filter_uses_same_active_confidence_contract():
    good = record("claim_good", confidence=.72)
    bad = record("claim_bad", status="rolled_back")
    invalid = record("claim_null")
    invalid.meta["reliability"] = None
    result = filter_answer_evidence([good, bad, invalid], task_type="research.answer")
    assert result["records"] == [good]
    assert result["evidence_gate"]["excluded_count"] == 2


def test_native_paper_unknown_confidence_still_waits_for_d2_descriptive_policy():
    src = PaperSource(paper_source_id="paper_native", source_kind="url", title="Native paper",
        canonical_url="https://example.test/paper", published_at="2026-10-01").to_record(scope=SCOPE)
    result = build_research_digest(paper_sources=[src], claim_cards=[], knowledge_pages=[])
    assert result["top_papers"] == [] and not result["ok"]
    assert result["evidence_gate"]["excluded"][0]["reason"] == "missing_confidence"


def test_native_claim_point_72_not_inflated_by_shared_helper():
    src = ClaimCard(claim_card_id="claim_native", paper_source_id="paper_native", paper_extract_id="pex_native",
        claim_text=TEXT, confidence=.72, provenance={"published_at": "2026-10-01"}).to_record(scope=SCOPE)
    result = digest([src])
    assert result["notable_claims"][0]["confidence"] == .72
    assert _candidate_from_record(src)[0] is None
