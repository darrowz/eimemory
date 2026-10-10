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
    # ProactiveRecallService.decide emits pd:<32 lowercase hex>; pending
    # capture validation (1.14.44) rejects any other capture_ref shape.
    decision_id = "pd:" + sha256(f"decision-auto-{index}".encode()).hexdigest()[:32]
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
                      relevance=[semantic], off_topic=off_topic, duplicates=False, unanswered=off_topic,
                      decision_surface=task_type, channel=CHANNEL)
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


@pytest.mark.parametrize("seed_kwargs, close_reason", [
    ({"with_candidate": False}, "no_recall"),
    ({"delivered": False, "semantic": None}, "not_delivered"),
])
def test_structural_gap_closes_review_without_accepting_or_inactivating(runtime, seed_kwargs, close_reason):
    _seed(runtime, 9, **seed_kwargs)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0
    assert report["open_review_count"] == 0
    assert report["closed_not_evaluable_count"] == 1
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    pending = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
        meta_value="production_recall_pending_case", status="active", limit=10) or []
    assert len(pending) == 1 and pending[0].status == "active"
    terminals = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
        meta_value="production_recall_evaluation_terminal", status="active", limit=10) or []
    assert len(terminals) == 1
    assert terminals[0].content["close_reason"] == close_reason
    assert terminals[0].content["does_not_certify_answer"] is True
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0


def test_empty_recall_closes_review_without_accepting_or_inactivating(runtime):
    test_structural_gap_closes_review_without_accepting_or_inactivating(
        runtime, {"with_candidate": False}, "no_recall")


def test_delivered_case_without_semantic_has_non_passing_conclusion(runtime):
    _seed(runtime, 10, semantic=None)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0
    assert report["open_review_count"] == 0 and report["pending_count"] == 0
    assert report["not_passed_count"] == 1
    assert report["review_results"][0]["review_verdict"] == "fail"
    assert report["closed_not_evaluable_count"] == 0


@pytest.mark.parametrize("seed_kwargs, reason", [
    ({"semantic": None}, "semantic_judgment_missing"),
    ({"proof": False, "state": "not_used"}, "independent_signal_agreement_missing"),
    ({"query": "memory recall"}, "query_features_low_signal"),
])
def test_insufficient_evidence_has_non_passing_conclusion_with_reason(runtime, seed_kwargs, reason):
    _seed(runtime, 3, **seed_kwargs)
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0 and report["pending_count"] == 0
    assert report["not_passed_count"] == 1 and report["open_review_count"] == 0
    assert reason in report["reason_counts"]["not_passed"]
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0


def test_semantic_only_without_second_signal_does_not_pass(runtime):
    _seed(runtime, 4, proof=False, state="not_used")
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["pending_count"] == 0 and report["not_passed_count"] == 1
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
    assert disabled["status"] == "disabled" and disabled["scanned_count"] == 1
    assert disabled["not_passed_count"] == 1 and disabled["passed_count"] == 0
    assert disabled["reason_counts"]["not_passed"] == {"auto_review_disabled": 1}
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
    assert rerun["already_accepted_count"] == 0
    assert rerun["not_passed_count"] == 1 and rerun["passed_count"] == 0
    assert rerun["reason_counts"]["not_passed"] == {"auto_review_revoked": 1}
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


def test_missing_receipt_key_blocks_approval_and_records_non_passing_conclusion(runtime, monkeypatch):
    _seed(runtime, 12)
    _collect(runtime)
    monkeypatch.delenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY")
    monkeypatch.setattr("eimemory.governance.tool_receipts.receipt_key_set", lambda: None)
    blocked = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert blocked["ok"] is False and blocked["blocked_reason"] == "auto_review_attestation_key_unavailable"
    assert blocked["not_passed_count"] == 1 and blocked["accepted_count"] == 0
    assert blocked["pending_count"] == blocked["open_review_count"] == 0
    saved = runtime.store.get_by_id(blocked["review_results"][0]["review_receipt_id"])
    assert saved.content["review_verdict"] == "fail" and saved.content["review_complete"] is True
    assert saved.content["key_id"] == saved.content["signature"] == ""
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


def test_semantic_result_is_reported_without_certification(runtime):
    from eimemory.evaluation.production_query_auto_review import assess_pending_case
    _seed(runtime, 901, proof=False, state="not_used")
    _collect(runtime)
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    pending = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
        meta_value="production_recall_pending_case", status="active", limit=10)[0]
    result = assess_pending_case(runtime, pending, exact_scope=exact, channel=CHANNEL)
    assert result["semantic_quality"]["verdict"] == "relevant"
    assert result["semantic_quality"]["certified"] is False
    assert result["disposition"] == "pending" and result["labels"] == []


def test_semantic_only_review_closes_but_cannot_build_certified_dataset(runtime):
    _seed(runtime, 902, proof=False, state="not_used")
    _collect(runtime)
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report["accepted_count"] == 0 and report["open_review_count"] == 0
    assert report["closed_without_label_count"] == 1
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    terminals = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
        meta_value="production_recall_evaluation_terminal", status="active", limit=10)
    assert terminals[0].content["disposition"] == "closed_without_label"
    assert terminals[0].content["semantic_quality"]["certified"] is False
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0


@pytest.mark.parametrize("tamper", ["scope", "digest"])
def test_semantic_observation_boundary_and_integrity_fail_closed(runtime, tamper, monkeypatch):
    from eimemory.evaluation.production_query_auto_review import assess_pending_case
    _seed(runtime, 903, proof=False, state="not_used")
    _collect(runtime)
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    observation = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type", meta_value=SEM_VERSION,
        status="active", limit=10)[0]
    if tamper == "scope":
        observation.scope = ScopeRef.from_dict({**asdict(exact), "user_id": "other"})
    else:
        observation.meta["semantic_monitor_digest"] = "0" * 64
    lister = runtime.store.list_records_by_meta_value
    def tampered_list(**kwargs):
        records = lister(**kwargs)
        return [observation if record.record_id == observation.record_id else record for record in records]
    monkeypatch.setattr(runtime.store, "list_records_by_meta_value", tampered_list)
    pending = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=exact, meta_key="report_type",
        meta_value="production_recall_pending_case", status="active", limit=10)[0]
    assessment = assess_pending_case(runtime, pending, exact_scope=exact, channel=CHANNEL)
    assert assessment["semantic_quality"]["status"] == "missing"
    assert assessment["labels"] == []


def test_missing_query_input_closes_review_without_certification(runtime):
    _, decision_id = _seed(runtime, 904)
    _collect(runtime)
    with runtime.store.locked() as db:
        db.execute("DELETE FROM proactive_query_input_vault WHERE decision_id = ?", (decision_id,))
        db.commit()
    result = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert result["accepted_count"] == 0
    assert result["closed_not_evaluable_count"] == 1 and result["open_review_count"] == 0
    assert "original_query_input_boundary_mismatch" in result["reason_counts"]["not_passed"]
    assert build_production_query_dataset(runtime, scope=BASE_SCOPE)["progress"]["accepted_case_count"] == 0


@pytest.mark.parametrize("reason", ["unverified_delivery", "query_unavailable", "query_unverified", "empty_delivery", "delivery_too_large", "original_query_input_unavailable"])
def test_missing_semantic_input_closes_without_noise_label(runtime, monkeypatch, reason):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 905, semantic=None)
    _collect(runtime)
    monkeypatch.setattr(review, "_semantic_observation", lambda *args:
                        {"status": "unknown", "reason": reason})
    result = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert result["accepted_count"] == 0 and result["rejected_count"] == 0
    assert result["closed_not_evaluable_count"] == 1 and result["open_review_count"] == 0


def test_nightly_diagnostics_do_not_treat_uncertified_as_open_work():
    report = {"pending_count": 1, "open_review_count": 0,
              "closed_without_label_count": 1, "closed_not_evaluable_count": 0}
    diagnostics = nightly_result_diagnostics({"production_recall_auto_review": report}, [])
    review = diagnostics["recall_label_auto_review"]
    assert review["pending_count"] == 1
    assert review["open_review_count"] == 0
    assert review["closed_without_label_count"] == 1


@pytest.mark.parametrize("reason", ["tool_free_transport_unavailable", "completion_unavailable", "malformed_verdict"])
def test_transient_semantic_failure_has_retryable_non_passing_conclusion(runtime, monkeypatch, reason):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 906, semantic=None)
    _collect(runtime)
    monkeypatch.setattr(review, "_semantic_observation", lambda *args: {"status": "unknown", "reason": reason})
    result = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert result["open_review_count"] == 0 and result["pending_count"] == 0
    assert result["not_passed_count"] == 1
    assert result["closed_not_evaluable_count"] == 0
    assert result["accepted_count"] == result["rejected_count"] == 0


@pytest.mark.parametrize('query,reason', [(None, 'query_unavailable'),
    ('a different original query', 'query_unverified'),
    ('kubernetes ingress certificate rotation schedule', 'unverified_delivery')])
def test_monitor_real_query_failure_closes_auto_review(runtime, monkeypatch, query, reason):
    from eimemory.evaluation import semantic_relevance_monitor as monitor
    from eimemory.evaluation import query_input_vault
    from eimemory.governance.release import evidence_contract
    _record, decision_id = _seed(runtime, 907, semantic=None, proof=False, state='not_used')
    _collect(runtime)
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    decision = runtime.store.load_proactive_decision(decision_id)
    monkeypatch.setattr(evidence_contract, 'deployment_receipt_for_scope', lambda *args: object())
    monkeypatch.setattr(evidence_contract, 'verified_deployment_receipt_identity', lambda *args: object())
    monkeypatch.setattr(evidence_contract, 'release_identity_payload', lambda *args: decision['release_identity'])
    monkeypatch.setattr(query_input_vault, 'load_query_input', lambda *args, **kwargs: {'query': query})
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *args: pytest.fail('query must validate first'))
    observed, findings = monitor.monitor_deliveries(runtime, scope=exact)
    assert observed['provider_calls'] == 0 and not findings
    result = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert result['closed_not_evaluable_count'] == 1 and result['open_review_count'] == 0
    assert result['accepted_count'] == result['rejected_count'] == 0
    assert result['reason_counts']['not_passed'][reason] == 1


def test_recovered_semantic_result_supersedes_transient_observation(runtime, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    _record, decision_id = _seed(runtime, 908, proof=False, state='not_used')
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    decision = runtime.store.load_proactive_decision(decision_id)
    observations = runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=exact,
        meta_key='report_type', meta_value=SEM_VERSION, status='active', limit=10)
    from copy import deepcopy
    failure = deepcopy(observations[0])
    failure.content.update(verdict='unknown', reason='tool_free_transport_unavailable', relevance=[],
                           off_topic=False, duplicates=False, unanswered='unknown')
    failure.meta['semantic_monitor_digest'] = _digest(failure.content)
    monkeypatch.setattr(runtime.store, 'list_records_by_meta_value', lambda **kwargs: [failure, *observations])
    result = review._semantic_observation(runtime, decision, exact)
    assert result['status'] == 'evaluated' and result['verdict'] == 'relevant'


def test_real_monitor_provider_failure_then_recovery_never_certifies(runtime, monkeypatch):
    from eimemory.evaluation import semantic_relevance_monitor as monitor
    from eimemory.governance.release import evidence_contract
    from eimemory.retrieval.proactive import ProactiveRecallService
    record, decision_id = _seed(runtime, 909, semantic=None, proof=False, state='not_used')
    exact = ScopeRef.from_dict(resolve_channel_scope(CHANNEL, BASE_SCOPE))
    digest = ProactiveRecallService._render_snapshot_digest(record.title, ProactiveRecallService._record_text(record))
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decision_items SET render_digest=? WHERE decision_id=?', (digest, decision_id))
        db.conn.commit()
    _collect(runtime)
    decision = runtime.store.load_proactive_decision(decision_id)
    monkeypatch.setattr(evidence_contract, 'deployment_receipt_for_scope', lambda *args: object())
    monkeypatch.setattr(evidence_contract, 'verified_deployment_receipt_identity', lambda *args: object())
    monkeypatch.setattr(evidence_contract, 'release_identity_payload', lambda *args: decision['release_identity'])
    def fail(*args):
        raise monitor.ToolFreeUnavailable('fixture-only')
    monkeypatch.setattr(monitor, '_complete_tool_free', fail)
    observed, findings = monitor.monitor_deliveries(runtime, scope=exact)
    assert observed['provider_calls'] == 1 and not findings
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report['open_review_count'] == 0 and report['closed_not_evaluable_count'] == 0
    assert report['not_passed_count'] == 1
    assert report['reason_counts']['not_passed']['tool_free_transport_unavailable'] == 1
    assert report['accepted_count'] == report['rejected_count'] == 0
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *args: json.dumps({
        'relevance': ['relevant'], 'off_topic': False, 'duplicates': False, 'unanswered': False}))
    recovered, findings = monitor.monitor_deliveries(runtime, scope=exact)
    assert recovered['provider_calls'] == 1 and recovered['verdict_counts']['relevant'] == 1
    assert not findings
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert report['open_review_count'] == 0 and report['closed_without_label_count'] == 1
    assert report['accepted_count'] == report['rejected_count'] == 0
