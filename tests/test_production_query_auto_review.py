"""Deterministic auto-review of pending production-query recall labels."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.cli.main import _build_parser
from eimemory.evaluation.label_authority import label_authority_error
from eimemory.evaluation.production_query_auto_review import (
    CRITERIA_VERSION,
    RECEIPT_SOURCE,
    auto_review_pending_production_queries,
    auto_review_receipt_error,
    derive_query_features,
    revoke_auto_reviewed_production_query,
)
from eimemory.evaluation.production_query_dataset import (
    LABEL_EVIDENCE_SOURCE,
    accept_pending_production_query,
    accepted_production_query_validation_error,
    build_production_query_dataset,
    collect_pending_production_queries,
)
from eimemory.evaluation.production_query_repair import repair_production_query_channel_scopes
from eimemory.evaluation.query_input_vault import capture_query_input
from eimemory.evaluation.real_query_gate import freeze_production_recall_dataset
from eimemory.evaluation.real_query_schema import PRODUCTION_RECALL_AUTO_REVIEW_FLAG
from eimemory.evaluation.semantic_relevance_monitor import SOURCE as SEM_SOURCE, VERSION as SEM_VERSION, _digest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.query_identity import effective_query_digest, query_text_digest
from eimemory.retrieval.relevance import record_digest
from eimemory.scheduler.result_contract import nightly_result_diagnostics

BASE_SCOPE = {"tenant_id": "default", "agent_id": "main", "workspace_id": "production", "user_id": "darrow"}
CHANNEL = "hermes"
LABEL_PACKET_EVIDENCE = {"schema": "secure_dataset_fingerprint.v1", "digest": "d" * 64, "size": 512,
                         "device": 1, "inode": 1}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("EIMEMORY_CAPTURE_ORIGINAL_QUERY", "1")
    monkeypatch.delenv("EIMEMORY_CAPTURE_QUERY_SCOPES", raising=False)
    monkeypatch.delenv(PRODUCTION_RECALL_AUTO_REVIEW_FLAG, raising=False)


@pytest.fixture
def runtime(tmp_path):
    rt = Runtime.create(root=tmp_path / "runtime")
    try:
        yield rt
    finally:
        rt.close()


def _seed(runtime, index, *, delivered=True, state="used", proof=True, semantic="relevant",
          query="kubernetes ingress certificate rotation schedule", with_candidate=True):
    scope = resolve_channel_scope(CHANNEL, BASE_SCOPE)
    exact = ScopeRef.from_dict(scope)
    source_id = "hermes"
    record = RecordEnvelope.create(kind="memory", title=f"ingress cert rotation note {index}",
                                   summary="rotate ingress certificates every 60 days",
                                   source="hermes.memory", source_id=source_id, scope=exact,
                                   meta={"force_capture": True})
    runtime.store.append(record)
    stored = runtime.store.get_by_id(record.record_id, scope=exact)
    task_type = "research.task"
    decision_id = f"decision-auto-{index}"
    render = ({"format": "verified-parent-span.v1", "record_id": record.record_id,
               "record_digest": record_digest(stored), "quote_digest": "q" * 64,
               "span_start": 0, "span_end": 10, "window_start": 0, "window_end": 10} if proof else {})
    items = ([{"citation": "M1", "record_id": record.record_id, "source_id": source_id, "confidence": 0.9,
               "order": 0, "render_digest": "r" * 64, "state": "injected" if delivered else "volunteered",
               "render_evidence": render}] if with_candidate else [])
    runtime.store.record_proactive_decision({
        "decision_id": decision_id, "channel": CHANNEL, "scope": scope,
        "source_key": sha256(source_id.encode()).hexdigest(), "source_ids": [source_id],
        "session_id": f"s-{index}", "turn_id": f"t-{index}", "query_id": f"q-{index}",
        "query_digest": query_text_digest(query),
        "effective_query_digest": effective_query_digest(task_type, query),
        "task_type": task_type, "policy_version": "proactive.test.v1",
        "release_identity": {"release_commit": "a" * 40, "release_version": "1.14.16",
                             "deployment_receipt_id": "receipt", "release_session_id": "session"},
        "release_bound": True, "control_cohort": False, "pair_id": f"p-{index}",
    }, items, [])
    if with_candidate and delivered and state in {"used", "rejected", "not_used"}:
        with runtime.store.locked() as db:
            db.transition_proactive_items(decision_id, {"M1": state})
    assert capture_query_input(runtime, decision_id=decision_id, query=query, effective_query=query,
                               explanation={"retrieval_status": "evidence_found"})["status"] == "captured"
    if semantic and with_candidate and delivered:
        with runtime.store.locked() as db:
            decision = db.load_proactive_decision(decision_id)
        delivered_items = [item for item in decision["items"] if item["ever_injected"]]
        identity = _digest(dict(
            version=SEM_VERSION, decision_id=decision_id, scope=decision["scope"],
            source_ids=decision["source_ids"], query_digest=decision["query_digest"],
            release_identity=decision["release_identity"],
            delivered=[(i["record_id"], i.get("source_id"), i.get("render_digest")) for i in delivered_items]))
        off_topic = semantic == "unrelated"
        report = dict(version=SEM_VERSION, scope=scope, evaluation_identity=identity,
                      decision_digest=_digest(decision_id),
                      record_digests=[_digest(i["record_id"]) for i in delivered_items],
                      verdict="off_topic" if off_topic else "relevant", reason="evaluated",
                      relevance=[semantic], off_topic=off_topic, duplicates=False, unanswered=off_topic)
        runtime.store.append(RecordEnvelope.create(
            kind="evaluation_packet", title="Semantic relevance observation", summary="evaluated",
            content=report, scope=exact, source=SEM_SOURCE,
            meta={"semantic_monitor_digest": _digest(report), "report_type": SEM_VERSION,
                  "semantic_monitor_identity": identity}))
    return record, decision_id


def _collect(runtime):
    result = collect_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    assert result["ok"] is True
    return result["pending_record_ids"]


def test_derive_query_features_is_redacted_and_bounded():
    features, reason = derive_query_features("How do I rotate the Kubernetes ingress certificate?")
    assert reason == ""
    assert features == {"terms": ["rotate", "kubernetes", "ingress", "certificate"]}
    assert derive_query_features("memory recall")[1] == "query_features_low_signal"
    assert derive_query_features("")[1] == "original_query_input_unavailable"
    long_cjk = "我想知道上周部署服务器的时候到底发生了什么问题"
    assert derive_query_features(long_cjk)[1] == "query_features_low_signal"


def test_auto_accepts_only_with_independent_signal_agreement(runtime):
    record, _ = _seed(runtime, 1)
    pending = _collect(runtime)
    dry = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, dry_run=True)
    assert dry["would_accept_pending_ids"] == pending and dry["accepted_count"] == 1
    assert runtime.store.sqlite.conn.execute(
        "SELECT COUNT(*) FROM records WHERE source IN (?,?)", (LABEL_EVIDENCE_SOURCE, RECEIPT_SOURCE)).fetchone()[0] == 0

    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["ok"] is True and report["accepted_count"] == 1
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    accepted = runtime.store.get_by_id(report["accepted_record_ids"][0], scope=exact)
    case = accepted.content["case"]
    assert accepted.meta["label_authority"] == "auto_review"
    assert case["labels"][0]["record_ref"] == record.record_id
    assert case["labels"][0]["grade"] == 3  # semantic + verified proof + host used
    assert case["labels"][0]["provenance"]["labeler"] == "auto_review"
    assert case["query_features"] == {"terms": ["kubernetes", "ingress", "certificate", "rotation", "schedule"]}
    assert accepted_production_query_validation_error(runtime, accepted, exact_scope=exact, channel=CHANNEL) == ""

    evidence = runtime.store.get_by_id(case["labels"][0]["provenance"]["evidence_ref"], scope=exact)
    assert evidence.content["evidence_class"] == "auto_review_relevance_label"
    packet = evidence.content["auto_review_packet"]
    assert packet["criteria_version"] == CRITERIA_VERSION and len(packet["inputs_digest"]) == 64
    assert packet["signals"][record.record_id] == {"semantic_relevant": True, "verified_proof": True, "host_used": True}
    assert evidence.content["auto_review_authority"]["signature"]
    assert "operator_packet_evidence" not in evidence.content

    receipts = runtime.store.list_records_by_meta_value(kinds=["evaluation_packet"], scope=exact,
        meta_key="report_type", meta_value="production_recall_auto_review", limit=10)
    assert [r.content["disposition"] for r in receipts] == ["accepted"]
    assert auto_review_receipt_error(receipts[0]) == ""

    built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert built["progress"]["accepted_by_authority"] == {"human": 0, "auto_review": 1}
    assert built["progress"]["auto_review_policy"]["enabled"] is True
    # Idempotent: a second run does not create a second accepted case.
    again = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert again["accepted_count"] == 0 and again["already_accepted_count"] == 1


def test_auto_label_signature_tamper_is_rejected(runtime):
    _seed(runtime, 2)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    accepted = runtime.store.get_by_id(report["accepted_record_ids"][0], scope=exact)
    label = accepted.content["case"]["labels"][0]
    evidence = runtime.store.get_by_id(label["provenance"]["evidence_ref"], scope=exact)
    kwargs = dict(scope=exact, source_id="hermes", pending_id=accepted.evidence[0],
                  record_ref=label["record_ref"], grade=label["grade"], labeler="auto_review")
    assert label_authority_error(evidence, **kwargs) == ""
    evidence.content["auto_review_packet"] = {**evidence.content["auto_review_packet"], "inputs_digest": "0" * 64}
    evidence.meta["auto_review_packet_digest"] = __import__(
        "eimemory.evaluation.real_query_schema", fromlist=["_stable_digest"])._stable_digest(
        evidence.content["auto_review_packet"])
    assert label_authority_error(evidence, **kwargs) == "auto_review_label_identity_mismatch"


@pytest.mark.parametrize("seed_kwargs, reason", [
    ({"semantic": None}, "semantic_judgment_missing"),
    ({"proof": False, "state": "not_used"}, "independent_signal_agreement_missing"),
    ({"delivered": False, "semantic": None}, "no_candidate_delivered"),
    ({"with_candidate": False}, "no_candidate_refs"),
    ({"query": "memory recall"}, "query_features_low_signal"),
])
def test_insufficient_evidence_stays_pending_with_reason(runtime, seed_kwargs, reason):
    _seed(runtime, 3, **seed_kwargs)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0 and report["pending_count"] == 1
    assert reason in report["reason_counts"]["pending"]
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0


def test_semantic_only_without_second_signal_stays_pending(runtime):
    _seed(runtime, 4, proof=False, state="not_used")
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["pending_count"] == 1
    assert report["signal_counts"]["semantic_relevant"] == 1
    assert report["signal_counts"]["verified_proof"] == 0


@pytest.mark.parametrize("seed_kwargs, reason", [
    ({"semantic": "unrelated"}, "semantic_off_topic"),
    ({"state": "rejected", "semantic": None}, "host_rejected_all_candidates"),
])
def test_contradicted_cases_are_rejected_not_labelled(runtime, seed_kwargs, reason):
    _seed(runtime, 5, **seed_kwargs)
    pending = _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["rejected_count"] == 1 and report["reason_counts"]["rejected"] == {reason: 1}
    # The pending observation itself is untouched and remains available to humans.
    assert runtime.store.get_by_id(pending[0]).status == "active"


def test_stale_proof_digest_is_not_evidence(runtime):
    record, _ = _seed(runtime, 6, state="not_used")
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    stored = runtime.store.get_by_id(record.record_id, scope=exact)
    stored.summary = "edited after delivery"
    with runtime.store.locked() as db:
        db.rewrite(stored)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0 and report["signal_counts"]["verified_proof"] == 0


def test_flag_off_disables_review_and_excludes_auto_labels(runtime, monkeypatch):
    _seed(runtime, 7)
    _collect(runtime)
    auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert built["progress"]["accepted_by_authority"]["auto_review"] == 1
    frozen_on = freeze_production_recall_dataset(built["dataset"])
    assert frozen_on["eligibility"]["label_authority_counts"] == {"human": 0, "auto_review": 1}

    monkeypatch.setenv(PRODUCTION_RECALL_AUTO_REVIEW_FLAG, "0")
    off = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert off["progress"]["accepted_case_count"] == 0
    assert off["progress"]["auto_review_policy"] == {
        "flag": PRODUCTION_RECALL_AUTO_REVIEW_FLAG, "enabled": False, "excluded_by_policy": 1}
    frozen_off = freeze_production_recall_dataset(built["dataset"])
    assert "accepted_labeler_untrusted" in frozen_off["eligibility"]["blocked_reasons"]
    disabled = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert disabled["status"] == "disabled" and disabled["scanned_count"] == 0
    # Policy-off auto labels are excluded, not a scope-repair conflict.
    repair = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, complete_scan=True,
                                                    persist_receipt=False)
    assert repair["ok"] is True and repair["conflicts"] == []


def test_revocation_withdraws_auto_labels_and_blocks_reacceptance(runtime):
    _seed(runtime, 8)
    pending = _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    accepted = runtime.store.get_by_id(report["accepted_record_ids"][0], scope=exact)
    revoked = revoke_auto_reviewed_production_query(runtime, pending_record_id=pending[0], scope=BASE_SCOPE,
                                                    reason="operator_disagrees")
    assert revoked["revoked_accepted_record_ids"] == [accepted.record_id]
    assert accepted_production_query_validation_error(
        runtime, accepted, exact_scope=exact, channel=CHANNEL) == "auto_review_label_revoked"
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0
    rerun = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert rerun["accepted_count"] == 0
    assert rerun["already_accepted_count"] == 1  # the revoked record still exists, it just does not count
    repair = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, complete_scan=True,
                                                    persist_receipt=False)
    assert repair["ok"] is True


def test_human_label_takes_precedence_and_counts_are_split(runtime):
    record, _ = _seed(runtime, 9, semantic=None, proof=False, state="not_used")
    human_pending = _collect(runtime)[0]
    accept_pending_production_query(runtime, pending_record_id=human_pending,
        query_features={"terms": ["archive", "routing", "destination"], "intent": "memory recall"},
        labels=[{"record_ref": record.record_id, "grade": 3}], labeler="operator",
        operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)
    _seed(runtime, 10)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["already_accepted_count"] == 1 and report["accepted_count"] == 1
    built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert built["progress"]["accepted_by_authority"] == {"human": 1, "auto_review": 1}
    frozen = freeze_production_recall_dataset(built["dataset"])
    assert frozen["eligibility"]["label_authority_counts"] == {"human": 1, "auto_review": 1}
    # Signed operator labels no longer produce a false scope-repair conflict.
    repair = repair_production_query_channel_scopes(runtime, scope=BASE_SCOPE, complete_scan=True,
                                                    persist_receipt=False)
    assert repair["ok"] is True and repair["conflicts"] == []


def test_operator_accept_path_cannot_mint_auto_review_labels(runtime):
    _seed(runtime, 11)
    pending = _collect(runtime)
    with pytest.raises(ValueError, match="trusted operator labeler required"):
        accept_pending_production_query(runtime, pending_record_id=pending[0],
            query_features={"terms": ["archive", "routing"]}, labels=[], labeler="auto_review",
            operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)


def test_missing_receipt_key_blocks_writes_but_not_dry_run(runtime, monkeypatch):
    _seed(runtime, 12)
    _collect(runtime)
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY")
    monkeypatch.setattr("eimemory.governance.tool_receipts.receipt_key_set", lambda: None)
    blocked = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert blocked["ok"] is False and blocked["blocked_reason"] == "auto_review_attestation_key_unavailable"
    assert auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, dry_run=True)["ok"] is True


def test_cli_auto_review_commands_parse():
    parsed = _build_parser().parse_args(["eval", "production-query", "auto-review", "--dry-run"])
    assert parsed.production_query_command == "auto-review" and parsed.dry_run is True
    parsed = _build_parser().parse_args(["eval", "production-query", "auto-review-revoke", "prqp_x",
                                         "--reason", "operator_disagrees"])
    assert parsed.reason == "operator_disagrees"


def test_nightly_runs_auto_review_before_recall_gate_and_reports_authority_counts(runtime, monkeypatch):
    from eimemory.scheduler import jobs

    _seed(runtime, 13)
    _collect(runtime)
    report = jobs._run_production_recall_auto_review(runtime, scope=BASE_SCOPE)
    assert report["ok"] is True and report["accepted_count"] == 1
    assert report["dataset_progress"]["accepted_by_authority"] == {"human": 0, "auto_review": 1}
    diagnostics = nightly_result_diagnostics({"production_recall_auto_review": report}, [])
    assert diagnostics["recall_label_auto_review"]["accepted_cases_by_authority"] == {"human": 0, "auto_review": 1}
    assert diagnostics["recall_label_auto_review"]["enabled"] is True
    source = open(jobs.__file__, encoding="utf-8").read()
    assert source.index('"production_recall_auto_review"') < source.index('"production_recall",')


def test_gate_hydration_admits_auto_labels_only_under_policy(runtime, monkeypatch):
    from eimemory.evaluation.real_query_gate import _hydrate_real_query_labels

    _seed(runtime, 14)
    _collect(runtime)
    auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    cases = build_production_query_dataset(runtime, scope=BASE_SCOPE)["dataset"]["cases"]
    ok, reason, capacities = _hydrate_real_query_labels(runtime, cases)
    assert (ok, reason) == (True, "") and len(capacities) == 1
    monkeypatch.setenv(PRODUCTION_RECALL_AUTO_REVIEW_FLAG, "off")
    ok, reason, _ = _hydrate_real_query_labels(runtime, cases)
    assert ok is False and reason
