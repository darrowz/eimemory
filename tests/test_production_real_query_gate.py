from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import gc
from threading import Barrier
import tracemalloc

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.governance import correction_replay
from eimemory.governance.l5_readiness import build_l5_readiness_report, readiness_gate_status
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import MAX_PRODUCTION_RECALL_DATASET_BYTES, _load_json_dataset
from eimemory.evaluation import real_query_gate
from eimemory.evaluation.production_recall import (
    PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
    PRODUCTION_REAL_QUERY_SCHEMA,
    bootstrap_production_recall_baseline,
    evaluate_labeled_ranking_at_5,
    freeze_production_recall_dataset,
    run_production_recall_eval,
    verify_current_production_recall_gate,
)


BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "main",
    "workspace_id": "production",
    "user_id": "darrow",
}
RELEASE = ReleaseIdentity(
    commit="a" * 40,
    version="1.9.80",
    receipt_id="receipt-current",
    session_id="deployment-session-current",
)


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _features(channel: str, index: int = 0) -> dict:
    return {
        "terms": ["deployment", channel, "receipt", f"case-{index}"],
        "intent": "release verification",
    }


def _case(channel: str, source_id: str, record_id: str, *, grade: int = 3, index: int = 0) -> dict:
    features = _features(channel, index)
    scope = dict(BASE_SCOPE)
    if channel != "openclaw":
        scope["workspace_id"] += f"::channel::{channel}"
    return {
        "case_id": f"real-{channel}-{index}",
        "collection_window": {
            "started_at": "2026-07-20T00:00:00+00:00",
            "ended_at": "2026-07-21T00:00:00+00:00",
        },
        "channel": channel,
        "source_id": source_id,
        "scope": scope,
        "query_features": features,
        "query_digest": _digest(features),
        "corpus_result_capacity": 5,
        "labels": [
            {
                "record_ref": record_id,
                "grade": grade,
                "accepted": True,
                "provenance": {
                    "labeler": "operator",
                    "labelled_at": "2026-07-20T12:00:00+00:00",
                    "evidence_ref": f"label-{channel}",
                },
            }
        ],
        "provenance": {
            "collector": "production_capture",
            "capture_ref": f"capture-{channel}",
        },
    }


def _dataset(record_ids: dict[str, str]) -> dict:
    payload = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-release-gate",
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "cases": [
            _case(channel, f"source-{channel}", record_ids[channel], index=index)
            for channel in ("openclaw", "codex", "hermes")
            for index in range(5)
        ],
        "baseline_report_id": "prg_baseline_previous_release",
    }
    _refresh_dataset_evidence(payload)
    return payload


def _refresh_dataset_evidence(dataset: dict) -> None:
    canonical = {
        key: value
        for key, value in dataset.items()
        if key not in {"_secure_dataset_evidence", "secure_dataset_evidence"}
    }
    dataset["_secure_dataset_evidence"] = {
        "schema": "secure_dataset_fingerprint.v1",
        "digest": "f" * 64,
        "canonical_digest": _digest(canonical),
        "size": 4096,
        "device": 1,
        "inode": 1,
    }


def _trusted_baseline(dataset: dict) -> dict:
    frozen = freeze_production_recall_dataset(dataset)
    results = {
        case["case_id"]: [case["labels"][0]["record_ref"]]
        for case in frozen["cases"]
    }
    return {
        "report_id": "prg_baseline_previous_release",
        "release_identity": {
            "release_commit": "b" * 40,
            "release_version": "1.9.79",
            "deployment_receipt_id": "receipt-baseline",
            "release_session_id": "deployment-session-baseline",
        },
        "dataset_digest": frozen["dataset_digest"],
        "secure_dataset_evidence": frozen["secure_dataset_evidence"],
        "engine_digest": "c" * 64,
        "fusion_digest": "d" * 64,
        "policy_digest": real_query_gate.production_real_query_policy_digest(),
        "result_digest": _digest(results),
        "result_refs": results,
        "metrics": {
            "recall_at_5": 1.0,
            "precision_at_5": 1.0,
            "mrr": 1.0,
            "ndcg_at_5": 1.0,
            "top1_stability": 1.0,
            "jaccard_at_5": 1.0,
        },
    }


def _forge_semantic_ranking_evidence(
    report: dict,
    *,
    baseline: dict,
    ranking_ref: str,
    grade: int,
) -> dict:
    forged = deepcopy(report)
    ranking_results: dict[str, list[str]] = {}
    rankings: list[dict[str, float]] = []
    for sample in forged["samples"]:
        sample["label_ranking_refs"] = [ranking_ref]
        sample["label_ranking_grades"] = [grade]
        sample["returned_ranking_refs"] = [ranking_ref]
        sample["baseline_ranking_refs"] = [ranking_ref]
        ranking = evaluate_labeled_ranking_at_5(
            candidate_refs=[ranking_ref],
            labels=[{"record_ref": ranking_ref, "grade": grade}],
            corpus_result_capacity=int(sample["corpus_result_capacity"]),
            baseline_refs=[ranking_ref],
        )
        sample.update(
            {key: round(float(value), 6) for key, value in ranking.items()}
        )
        ranking_results[str(sample["case_id"])] = [ranking_ref]
        rankings.append(ranking)
    forged["ranking_result_refs"] = ranking_results
    forged["ranking_result_digest"] = _digest(ranking_results)
    metric_fields = {
        "recall_at_5": "recall_at_5",
        "precision_at_5": "precision_at_5",
        "mrr": "mrr",
        "ndcg_at_5": "ndcg_at_5",
        "top1_stability": "top1_stable",
        "jaccard_at_5": "jaccard_at_5",
    }
    for name, field in metric_fields.items():
        value = round(
            sum(float(item[field]) for item in rankings) / len(rankings),
            6,
        )
        forged["metrics"][name] = value
        forged[name] = value
    forged["threshold_gate"] = real_query_gate._real_query_threshold_gate(
        forged["metrics"],
        baseline_metrics=dict(baseline["metrics"]),
        cross_channel_leakage=int(forged["cross_channel_leakage_count"]),
        source_filter_leakage=int(forged["source_filter_leakage_count"]),
        has_baseline=True,
        engine_identity_valid=forged["engine_identity_valid"] is True,
        memory_measurement_ok=forged["memory_measurement"]["ok"] is True,
    )
    return forged


def _record(record_id: str, channel: str, source_id: str) -> RecordEnvelope:
    scope = dict(BASE_SCOPE)
    if channel != "openclaw":
        scope["workspace_id"] += f"::channel::{channel}"
    record = RecordEnvelope.create(
        kind="memory",
        title=f"{channel} deployment receipt",
        summary="secret body must never be persisted by the evaluator",
        detail="password=hunter2 token=top-secret",
        content={"text": "raw result body"},
        source=f"{channel}.memory",
        source_id=source_id,
        scope=ScopeRef.from_dict(scope),
        meta={"force_capture": True},
    )
    record.record_id = record_id
    return record


def _rule_record(record_id: str, channel: str, source_id: str, *, index: int = 0) -> RecordEnvelope:
    scope = dict(BASE_SCOPE)
    if channel != "openclaw":
        scope["workspace_id"] += f"::channel::{channel}"
    record = RecordEnvelope.create(
        kind="rule",
        title=f"{channel} ground truth behavior {index}",
        summary="When capability is missing, create a concrete plan, replay gate, and rollback boundary.",
        content={
            "trigger_condition": "不要说做不到，要补能力解决",
            "expected_behavior": "build the missing capability path with replay and rollback",
        },
        source="eimemory.correction_replay",
        source_id=source_id,
        scope=ScopeRef.from_dict(scope),
        status="active",
        meta={"force_capture": True, "report_type": "ground_truth_behavior_rule"},
    )
    record.record_id = record_id
    return record


def _label_evidence(channel: str) -> RecordEnvelope:
    scope = dict(BASE_SCOPE)
    if channel != "openclaw":
        scope["workspace_id"] += f"::channel::{channel}"
    record = RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Trusted operator label evidence for {channel}",
        summary="Operator accepted the exact relevance label.",
        source="eimemory.production_recall.label_evidence",
        source_id=f"source-{channel}",
        scope=ScopeRef.from_dict(scope),
        status="active",
        content={
            "evidence_class": "operator_relevance_label",
            "labeler": "operator",
            "operator_packet_evidence": {
                "schema": "secure_dataset_fingerprint.v1",
                "digest": "e" * 64,
                "size": 512,
                "device": 1,
                "inode": 1,
            },
        },
        meta={
            "report_type": "production_recall_label_evidence",
            "authoritative": True,
            "operator_packet_digest": "e" * 64,
        },
    )
    record.record_id = f"label-{channel}"
    return record


def _append_records_and_label_evidence(runtime: Runtime, records: dict[str, RecordEnvelope]) -> None:
    for channel, record in records.items():
        runtime.store.append(record)
        runtime.store.append(_label_evidence(channel))


def _receipt(runtime: Runtime, *, commit: str, version: str, prior_commit: str) -> ReleaseIdentity:
    release_path = f"/opt/eimemory/releases/{commit}"
    record = RecordEnvelope.create(
        kind="promotion_request",
        title=f"Deployment {commit[:8]}",
        summary="verified deployment",
        scope=ScopeRef.from_dict(BASE_SCOPE),
        source="eimemory.deployment_receipt",
        status="deployed",
        content={
            "report_type": "deployment_receipt",
            "promotion_target": "code_patch",
            "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": {
                "ok": True,
                "production_applied": True,
                "deployment_executed": True,
                "verification": {"ok": True, "skipped": False, "prior_commit": prior_commit},
                "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                "post_deploy_health": {
                    "ok": True,
                    "skipped": False,
                    "commit": commit,
                    "version": version,
                    "release_path": release_path,
                },
                "commit": {"commit_sha": commit},
                "release": {"version": version, "release_path": release_path},
                "rollback_evidence": {
                    "prior_commit_sha": prior_commit,
                    "rollback_command": "verified rollback",
                },
            },
        },
        meta={"report_type": "deployment_receipt"},
    )
    runtime.store.append(record)
    return ReleaseIdentity(commit=commit, version=version, receipt_id=record.record_id, session_id=record.record_id)


def _persist_baseline(runtime: Runtime, dataset: dict, release: ReleaseIdentity) -> str:
    baseline = _trusted_baseline(dataset)
    baseline["release_identity"] = {
        "release_commit": release.commit,
        "release_version": release.version,
        "deployment_receipt_id": release.receipt_id,
        "release_session_id": release.session_id,
    }
    report = {
        "ok": True,
        "accepted": True,
        "gate_status": "accepted",
        "blocked_reason": "",
        "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "report_type": "production_recall_gate",
        "report_id": "prg_baseline_previous_release",
        "generated_at": "2026-07-21T00:00:00+00:00",
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "release_identity": baseline["release_identity"],
        "deployment_receipt_id": release.receipt_id,
        "dataset_digest": baseline["dataset_digest"],
        "secure_dataset_evidence": baseline["secure_dataset_evidence"],
        "engine_digest": baseline["engine_digest"],
        "fusion_digest": baseline["fusion_digest"],
        "policy_digest": baseline["policy_digest"],
        "policy_schema": real_query_gate.PRODUCTION_REAL_QUERY_POLICY,
        "result_digest": baseline["result_digest"],
        "result_refs": baseline["result_refs"],
        "baseline_identity": {},
        "sample_count": 15,
        "metrics": baseline["metrics"],
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
        "proactive_metrics": {key: 0 for key in ("volunteered", "injected", "used", "not_used", "rejected", "control")},
        "threshold_gate": {
            "ok": True,
            "schema": real_query_gate.PRODUCTION_REAL_QUERY_POLICY,
            "blocked_reason": "",
            "thresholds": dict(real_query_gate.PRODUCTION_REAL_QUERY_THRESHOLDS),
            "blocking_metrics": {},
        },
        "eligibility": {"ok": True},
        "samples": [],
    }
    record = real_query_gate._real_query_report_record(report, scope=ScopeRef.from_dict(BASE_SCOPE))
    runtime.store.append(record)
    return record.record_id


def test_production_recall_contract_requires_fifteen_cases_and_five_per_channel() -> None:
    dataset = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})

    frozen = freeze_production_recall_dataset(dataset)

    assert frozen["eligibility"]["ok"] is True
    assert frozen["eligibility"]["required_case_count"] == 15
    assert frozen["eligibility"]["required_label_count"] == 15
    assert frozen["eligibility"]["required_channels"] == ["codex", "hermes", "openclaw"]
    assert frozen["eligibility"]["required_per_channel"] == 5
    assert frozen["eligibility"]["per_channel_case_count"] == {
        "codex": 5,
        "hermes": 5,
        "openclaw": 5,
    }


def test_production_recall_contract_rejects_five_five_four_without_inference() -> None:
    dataset = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})
    dataset["cases"] = [
        case
        for case in dataset["cases"]
        if not (case["channel"] == "hermes" and str(case["case_id"]).endswith("-4"))
    ]
    _refresh_dataset_evidence(dataset)

    frozen = freeze_production_recall_dataset(dataset)

    assert frozen["eligibility"]["ok"] is False
    assert frozen["eligibility"]["required_case_count"] == 15
    assert frozen["eligibility"]["required_per_channel"] == 5
    assert frozen["eligibility"]["per_channel_case_count"]["hermes"] == 4
    assert "required_channel_coverage_missing" in frozen["eligibility"]["blocked_reasons"]
    assert "minimum_case_count_missing" in frozen["eligibility"]["blocked_reasons"]


def test_formula_contract_at_k5_uses_all_relevant_and_capacity_denominator() -> None:
    metrics = evaluate_labeled_ranking_at_5(
        candidate_refs=["r3", "noise", "r1"],
        labels=[{"record_ref": "r3", "grade": 3}, {"record_ref": "r2", "grade": 2}, {"record_ref": "r1", "grade": 1}],
        corpus_result_capacity=5,
        baseline_refs=["r3", "r2", "r1"],
    )

    assert metrics["recall_at_5"] == pytest.approx(2 / 3, abs=1e-6)
    assert metrics["precision_at_5"] == pytest.approx(2 / 5, abs=1e-6)
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg_at_5"] == pytest.approx((7.0 + 1.0 / 2.0) / (7.0 + 3.0 / 1.5849625007 + 1.0 / 2.0), rel=1e-5)
    assert metrics["top1_stable"] == 1.0
    assert metrics["jaccard_at_5"] == pytest.approx(0.5)

    duplicate_results = evaluate_labeled_ranking_at_5(
        candidate_refs=["r1", "r1", "r2", "r3", "r4", "r5"],
        labels=[{"record_ref": "r1", "grade": 3}, {"record_ref": "r2", "grade": 2}],
        corpus_result_capacity=5,
        baseline_refs=["r1", "r2", "r3", "r4", "r5"],
    )
    assert duplicate_results == {
        "recall_at_5": 1.0,
        "precision_at_5": 0.4,
        "mrr": 1.0,
        "ndcg_at_5": 1.0,
        "top1_stable": 1.0,
        "jaccard_at_5": 1.0,
    }


def test_dynamic_knowledge_tail_churn_requires_strong_quality_noninferiority() -> None:
    baseline = {
        "recall_at_5": 0.994475,
        "precision_at_5": 0.2,
        "mrr": 0.994475,
        "ndcg_at_5": 0.991647,
        "top1_stability": 0.994475,
        "jaccard_at_5": 1.0,
    }
    evolved = {
        "recall_at_5": 0.994475,
        "precision_at_5": 0.2,
        "mrr": 0.98895,
        "ndcg_at_5": 0.989147,
        "top1_stability": 0.98895,
        "jaccard_at_5": 0.69245,
        "latency_ms_p95": 2149.679,
        "peak_memory_bytes": 16_617_956,
    }

    gate = real_query_gate._real_query_threshold_gate(
        evolved,
        baseline_metrics=baseline,
        cross_channel_leakage=0,
        source_filter_leakage=0,
        has_baseline=True,
    )

    assert gate["ok"] is True
    assert gate["blocking_metrics"] == {}
    assert gate["policy_semantics"]["dynamic_knowledge_stability"][
        "tail_churn_admitted"
    ] is True

    for mutation in (
        {"jaccard_at_5": 0.64},
        {"top1_stability": 0.97},
        {"recall_at_5": 0.98},
        {"mrr": 0.97},
    ):
        rejected = real_query_gate._real_query_threshold_gate(
            {**evolved, **mutation},
            baseline_metrics=baseline,
            cross_channel_leakage=0,
            source_filter_leakage=0,
            has_baseline=True,
        )
        assert rejected["ok"] is False
        assert rejected["blocking_metrics"]


def test_prior_recall_policy_keeps_exact_baseline_regression_semantics() -> None:
    baseline = {
        "recall_at_5": 1.0,
        "precision_at_5": 0.2,
        "mrr": 1.0,
        "ndcg_at_5": 1.0,
        "top1_stability": 1.0,
        "jaccard_at_5": 1.0,
    }
    metrics = {
        **baseline,
        "mrr": 0.999,
        "latency_ms_p95": 100.0,
        "peak_memory_bytes": 1024,
    }

    gate = real_query_gate._real_query_threshold_gate(
        metrics,
        baseline_metrics=baseline,
        cross_channel_leakage=0,
        source_filter_leakage=0,
        has_baseline=True,
        policy_schema="production_recall_gate_policy.v2",
    )

    assert gate["ok"] is False
    assert "mrr_regression" in gate["blocking_metrics"]
    assert "policy_semantics" not in gate


def test_trusted_capacity_uses_exact_scope_source_index_without_offset(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    for index in range(6):
        runtime.store.append(_record(f"capacity-{index}", "openclaw", "alpha"))
    runtime.store.append(_record("other-source", "openclaw", "beta"))
    runtime.store.append(_record("other-channel", "codex", "alpha"))
    monkeypatch.setattr(
        runtime.store,
        "list_records",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("capacity must not page or OFFSET")),
    )

    assert real_query_gate._trusted_result_capacity(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        source_id="alpha",
    ) == 5
    kind_placeholders = ",".join("?" for _ in real_query_gate._RECALL_CORPUS_KINDS)
    plan = runtime.store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM (SELECT 1 FROM records WHERE "
        "tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND source_id IN (?) "
        f"AND status=? AND kind IN ({kind_placeholders}) LIMIT ?)",
        ("default", "main", "production", "darrow", "alpha", "active", *real_query_gate._RECALL_CORPUS_KINDS, 5),
    ).fetchall()
    assert any("idx_records_scope_source_status_kind" in str(row[3]) for row in plan)
    runtime.close()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda data: data.update({"dataset_kind": "synthetic"}), "dataset_not_production"),
        (
            lambda data: (
                data.update({"cases": [case for case in data["cases"] if case["channel"] == "openclaw"][:4]}),
                _refresh_dataset_evidence(data),
            ),
            "minimum_case_count_missing",
        ),
        (lambda data: data["cases"][0].update({"labels": []}), "accepted_labels_missing"),
        (lambda data: data["cases"][0].update({"source_id": "*"}), "exact_source_required"),
        (lambda data: data["cases"][0]["query_features"].update({"raw_query": "password=hunter2"}), "query_features_not_redacted"),
        (lambda data: data.pop("_secure_dataset_evidence"), "secure_dataset_fingerprint_missing"),
        (lambda data: data["cases"][0]["labels"][0]["provenance"].update({"labeler": "model"}), "accepted_labeler_untrusted"),
        (lambda data: data["cases"][0]["labels"][0]["provenance"].update({"labelled_at": "2026-07-22T00:00:00+00:00"}), "accepted_label_time_outside_collection_window"),
        (lambda data: data["cases"][0]["provenance"].update({"collector": "unknown"}), "case_collector_untrusted"),
    ],
)
def test_eligible_dataset_fails_closed_without_real_labels_and_boundaries(mutation, reason) -> None:
    data = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})
    mutation(data)

    frozen = freeze_production_recall_dataset(data)

    assert frozen["eligibility"]["ok"] is False
    assert reason in frozen["eligibility"]["blocked_reasons"]


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "operator@example.com",
        "+1 (415) 555-0123",
        "AKIAIOSFODNN7EXAMPLE",
        "accessToken=credential-canary-value",
        "Dr. Alice Smith",
        "张三先生",
    ],
)
def test_query_features_reject_high_confidence_pii_secret_and_person_entities(unsafe_value: str) -> None:
    features, reason = real_query_gate._bounded_query_features(
        {"terms": ["deployment", "receipt"], "entities": [unsafe_value], "intent": "release verification"}
    )

    assert reason == "query_features_not_redacted"
    assert features["terms"] == ["deployment", "receipt"]


@pytest.mark.parametrize(
    "query_features",
    [
        {
            "terms": ["release", "notes"],
            "entities": [],
            "intent": "find Dr. Alice Smith release notes",
        },
        {
            "terms": ["find", "Dr.", "Alice", "Smith", "release"],
            "entities": [],
            "intent": "release verification",
        },
        {
            "terms": ["find", "张三", "先生"],
            "entities": [],
            "intent": "release verification",
        },
    ],
)
def test_query_features_reject_person_pii_hidden_in_intent_or_split_terms(query_features: dict) -> None:
    _features, reason = real_query_gate._bounded_query_features(query_features)

    assert reason == "query_features_not_redacted"


def test_query_features_allow_function_terms_and_constrained_person_placeholders() -> None:
    features, reason = real_query_gate._bounded_query_features(
        {
            "terms": ["deployment", "postgresql", "receipt", "memory.recall"],
            "entities": ["OpenAI", "person_ref:operator-17"],
            "intent": "release verification",
            "language": "en",
        }
    )

    assert reason == ""
    assert features["entities"] == ["OpenAI", "person_ref:operator-17"]


def test_query_features_allow_known_honorific_product_entity() -> None:
    features, reason = real_query_gate._bounded_query_features(
        {
            "terms": ["product", "catalog"],
            "entities": ["Dr. Pepper"],
            "intent": "lookup product",
            "language": "en",
        }
    )

    assert reason == ""
    assert features["entities"] == ["Dr. Pepper"]


def test_query_features_allow_title_case_product_place_entity_and_iso_date() -> None:
    features, reason = real_query_gate._bounded_query_features(
        {
            "terms": ["model", "context", "protocol", "2026-07-22"],
            "entities": ["Model Context Protocol", "New York"],
            "intent": "release verification on 2026-07-22",
            "language": "en",
        }
    )

    assert reason == ""
    assert features["entities"] == ["Model Context Protocol", "New York"]
    assert features["terms"][-1] == "2026-07-22"


@pytest.mark.parametrize(
    "safe_value",
    [
        "192.168.1.1",
        "192.168.100.200",
        "build 1234-5678",
        "release 12345678",
        "build 1234567890",
        "order 20000000000",
        "build4155550123hash",
        "record_4155550123_id",
        "order 2345678901",
        "2026072212",
        "2026-07-22 12",
        "2026-07-22",
    ],
)
def test_query_features_allow_non_phone_numeric_identifiers(safe_value: str) -> None:
    assert real_query_gate._looks_like_secret(safe_value) is False


@pytest.mark.parametrize(
    "phone_value",
    [
        "+1 (415) 555-0123",
        "415-555-0123",
        "4155550123",
        "prefix 4155550123 suffix",
        "13800138000",
        "prefix 13800138000 suffix",
        "联系电话13800138000",
        "请联系13800138000谢谢",
        "１３８００１３８０００",
        "138-00138000",
        "415-5550123",
        "8613800138000",
        "008613800138000",
        "٤١٥٥٥٥٠١٢٣",
        "+44 20 7946 0958",
    ],
)
def test_query_features_reject_high_confidence_phone_shapes(phone_value: str) -> None:
    assert real_query_gate._looks_like_secret(phone_value) is True


@pytest.mark.parametrize(
    ("enabled", "configured", "postgres", "expected_valid"),
    [
        (
            True,
            False,
            {"state": "not_configured", "circuit": "closed", "index_verified": False, "query_valid": False},
            False,
        ),
        (
            True,
            True,
            {"state": "bypassed", "circuit": "open", "index_verified": False, "query_valid": False},
            False,
        ),
        (
            True,
            True,
            {"state": "available", "circuit": "closed", "index_verified": True, "query_valid": True},
            True,
        ),
        (
            False,
            False,
            {"state": "disabled", "circuit": "closed", "index_verified": False, "query_valid": False},
            True,
        ),
    ],
)
def test_real_query_identity_blocks_enabled_postgres_bypass_without_blocking_disabled_optional_backend(
    tmp_path, monkeypatch, enabled: bool, configured: bool, postgres: dict, expected_valid: bool
) -> None:
    runtime = Runtime.create(root=tmp_path)
    engine = runtime.memory.recall_engine
    engine_identity = {
        "engine_type": "GovernedRecallEngine",
        "name": "governed",
        "policy_version": "governed-recall.v1",
        "fusion_version": "governed-rrf.v1",
        "candidate_source": {
            "candidate_source_type": "PostgresVectorCandidateSource",
            "name": "postgres-vector",
            "policy_version": "postgres-vector-candidates.v1",
            "sqlite_authority": True,
            "authority_revision": "1",
            "enabled": enabled,
            "configured": configured,
            "postgres": dict(postgres),
        },
    }
    engine_identity["identity_digest"] = real_query_gate._engine_identity_digest(engine_identity)
    monkeypatch.setattr(engine, "effective_identity", lambda: dict(engine_identity))

    retrieval_identity = real_query_gate._retrieval_identity(runtime, samples=[])

    assert retrieval_identity["engine_identity_valid"] is expected_valid
    assert bool(retrieval_identity["engine_digest"]) is expected_valid
    runtime.close()


def test_conflicting_accepted_label_grade_fails_eligibility_and_same_grade_duplicate_normalizes() -> None:
    dataset = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})
    original = deepcopy(dataset["cases"][0]["labels"][0])
    same_grade = deepcopy(dataset)
    same_grade["cases"][0]["labels"].append(deepcopy(original))
    _refresh_dataset_evidence(same_grade)
    normalized = freeze_production_recall_dataset(same_grade)
    assert normalized["eligibility"]["ok"] is True
    assert len(normalized["cases"][0]["labels"]) == 1

    conflicting = deepcopy(dataset)
    conflicting_label = deepcopy(original)
    conflicting_label["grade"] = 1
    conflicting["cases"][0]["labels"].append(conflicting_label)
    _refresh_dataset_evidence(conflicting)
    rejected = freeze_production_recall_dataset(conflicting)
    assert rejected["eligibility"]["ok"] is False
    assert "accepted_label_grade_conflict" in rejected["eligibility"]["blocked_reasons"]


def test_duplicate_case_id_is_blocked_without_inflating_frozen_metrics_or_digest() -> None:
    dataset = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})
    baseline = freeze_production_recall_dataset(dataset)
    dataset["cases"].append(deepcopy(dataset["cases"][0]))
    _refresh_dataset_evidence(dataset)

    duplicate = freeze_production_recall_dataset(dataset)

    assert "duplicate_case_id" in duplicate["eligibility"]["blocked_reasons"]
    assert duplicate["cases"] == baseline["cases"]
    assert duplicate["dataset_digest"] == baseline["dataset_digest"]
    assert duplicate["eligibility"]["case_count"] == baseline["eligibility"]["case_count"]
    assert duplicate["eligibility"]["accepted_label_count"] == baseline["eligibility"]["accepted_label_count"]


def test_production_dataset_loader_rejects_symlink_and_oversized_file(
    tmp_path,
    monkeypatch,
    trusted_dataset_path_ancestors,
) -> None:
    target = tmp_path / "dataset.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "dataset-link.json"
    link.write_text("{}", encoding="utf-8")
    real_is_symlink = type(link).is_symlink
    monkeypatch.setattr(type(link), "is_symlink", lambda self: self == link or real_is_symlink(self))
    with pytest.raises(ValueError, match="symlink"):
        _load_json_dataset(str(link))

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_PRODUCTION_RECALL_DATASET_BYTES + 1)
    oversized.chmod(0o600)
    with pytest.raises(ValueError, match="size limit"):
        _load_json_dataset(str(oversized))


def test_accepted_labels_require_resolvable_authoritative_evidence(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"evidence-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    for record in records.values():
        runtime.store.append(record)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)

    missing = run_production_recall_eval(runtime, dataset, seed=False, persist_report=False)
    assert missing["blocked_reason"] == "accepted_label_evidence_missing"

    for channel in records:
        evidence = _label_evidence(channel)
        if channel == "openclaw":
            evidence.meta["authoritative"] = False
        runtime.store.append(evidence)
    untrusted = run_production_recall_eval(runtime, dataset, seed=False, persist_report=False)
    assert untrusted["blocked_reason"] == "accepted_label_evidence_untrusted"
    runtime.close()


def test_production_cli_output_never_contains_raw_query_canary(
    tmp_path,
    monkeypatch,
    capsys,
    trusted_dataset_path_ancestors,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset = _dataset({channel: f"record-{channel}" for channel in ("openclaw", "codex", "hermes")})
    dataset["cases"][0]["query_features"]["raw_query"] = "password=RAW-CANARY-SECRET"
    dataset_path = tmp_path / "production.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    # Match the trusted-loader contract every other dataset test follows;
    # permissive umasks would otherwise trip the group/world-writable guard
    # before the gate ever sees the redaction violation.
    dataset_path.chmod(0o600)

    exit_code = cli_main(["eval", "production-recall", str(dataset_path), "--no-seed", "--output", str(output_path)])

    rendered = capsys.readouterr().out + output_path.read_text(encoding="utf-8")
    assert exit_code == 1
    assert "RAW-CANARY-SECRET" not in rendered


@pytest.mark.parametrize(
    "raw",
    [
        b'{"cases":["DATASET-DECODE-CANARY"',
        b'\xffDATASET-DECODE-CANARY',
        b'{"cases":["\\ud800DATASET-UNICODE-CANARY"]}',
        b'null',
    ],
    ids=["malformed-json", "invalid-unicode", "escaped-surrogate", "invalid-top-level"],
)
def test_production_cli_preserves_invalid_dataset_json_without_leaking_input(
    tmp_path,
    monkeypatch,
    capsys,
    trusted_dataset_path_ancestors,
    raw: bytes,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "invalid-production.json"
    dataset_path.write_bytes(raw)
    dataset_path.chmod(0o600)

    exit_code = cli_main(["eval", "production-recall", str(dataset_path), "--no-seed"])
    rendered = capsys.readouterr().out

    assert exit_code == 2
    assert json.loads(rendered) == {"ok": False, "error": "invalid_dataset_json"}
    assert "DATASET-DECODE-CANARY" not in rendered
    assert "DATASET-UNICODE-CANARY" not in rendered


@pytest.mark.parametrize("failure_kind", ["missing-path", "unsafe-mode", "unknown-home"])
def test_production_cli_dataset_unreadable_is_detail_free_for_path_and_security_failures(
    tmp_path,
    monkeypatch,
    capsys,
    trusted_dataset_path_ancestors,
    failure_kind: str,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset_path = tmp_path / "DATASET-PATH-CANARY.json"
    if failure_kind == "unsafe-mode":
        dataset_path.write_text('{"cases": []}', encoding="utf-8")
        dataset_path.chmod(0o666)
    elif failure_kind == "unknown-home":
        dataset_path = "~eimemory-definitely-no-such-user/DATASET-PATH-CANARY.json"

    exit_code = cli_main(["eval", "production-recall", str(dataset_path), "--no-seed"])
    rendered = capsys.readouterr().out

    assert exit_code == 2
    assert json.loads(rendered) == {"ok": False, "error": "dataset_unreadable"}
    assert "DATASET-PATH-CANARY" not in rendered


def test_production_cli_binds_open_time_dataset_evidence(
    tmp_path,
    monkeypatch,
    capsys,
    trusted_dataset_path_ancestors,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "cases": [],
    }
    dataset_path = tmp_path / "production.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    dataset_path.chmod(0o600)
    captured: dict[str, object] = {}

    def fake_eval(_runtime, loaded, **_kwargs):
        captured["dataset"] = loaded
        return {"accepted": False, "gate_status": "not_run"}

    monkeypatch.setattr("eimemory.evaluation.run_production_recall_eval", fake_eval)

    exit_code = cli_main(["eval", "production-recall", str(dataset_path), "--no-seed"])

    capsys.readouterr()
    assert exit_code == 1
    evidence = captured["dataset"]["_secure_dataset_evidence"]  # type: ignore[index]
    assert evidence["schema"] == "secure_dataset_fingerprint.v1"  # type: ignore[index]
    assert evidence["inode"] == dataset_path.stat().st_ino  # type: ignore[index]


def test_real_query_gate_is_bound_sanitized_and_deterministic(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"record-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})
    _append_records_and_label_evidence(runtime, records)
    requested: list[tuple[str, dict, dict]] = []

    def recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        requested.append((query, dict(scope), dict(task_context)))
        transient = bytearray(2 * 1024 * 1024)
        del transient
        channel = "openclaw"
        if str(scope["workspace_id"]).endswith("::channel::codex"):
            channel = "codex"
        elif str(scope["workspace_id"]).endswith("::channel::hermes"):
            channel = "hermes"
        return RecallBundle(
            items=[records[channel], records[channel]],
            rules=[],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={
                "retrieval_mode": "hybrid",
                "recall_profile": "balanced",
                "fusion": {"policy_version": "rrf-page-pool.v1"},
            },
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (_trusted_baseline(dataset), ""),
    )
    first = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    second = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)

    assert first["accepted"] is True, first
    assert first["gate_status"] == "accepted"
    assert first["schema"] == PRODUCTION_REAL_QUERY_REPORT_SCHEMA
    assert first["report_id"] == second["report_id"]
    assert first["persisted_record_id"] == second["persisted_record_id"]
    assert first["release_identity"] == {
        "release_commit": RELEASE.commit,
        "release_version": RELEASE.version,
        "deployment_receipt_id": RELEASE.receipt_id,
        "release_session_id": RELEASE.session_id,
    }
    assert first["dataset_digest"] == first["baseline_identity"]["dataset_digest"]
    assert first["cross_channel_leakage_count"] == 0
    assert first["source_filter_leakage_count"] == 0
    assert all(len(sample["returned_refs"]) == 1 for sample in first["samples"])
    assert first["peak_memory_bytes"] >= 0
    assert first["peak_memory_bytes"] >= 2 * 1024 * 1024
    assert first["memory_measurement"] == {
        "schema": "production_recall_memory_measurement.v1",
        "ok": True,
        "mode": "isolated_tracemalloc",
        "sample_count": 15,
        "captures_released_peak": True,
    }
    assert set(first["proactive_metrics"]) >= {
        "volunteered", "injected", "used", "not_used", "rejected", "control"
    }
    assert len(requested) == 60
    assert requested[:15] == requested[15:30]
    assert requested[30:45] == requested[45:60]
    assert all(call[2]["source_ids"] == [call[2]["target_source_id"]] for call in requested)

    stored = runtime.store.get_by_id(first["persisted_record_id"], scope=BASE_SCOPE)
    assert stored is not None
    serialized = json.dumps(stored.to_dict(), ensure_ascii=False)
    for forbidden in ("hunter2", "top-secret", "raw result body", "returned_text", "raw_query", "conversation"):
        assert forbidden not in serialized
    old_policy_report = deepcopy(stored.content["report"])
    old_policy_report["policy_schema"] = "production_recall_gate_policy.v1"
    old_policy_report["policy_digest"] = real_query_gate._stable_digest(
        {
            "schema": "production_recall_gate_policy.v1",
            "thresholds": real_query_gate.PRODUCTION_REAL_QUERY_THRESHOLDS,
            "k": 5,
        }
    )
    old_policy_report["threshold_gate"] = {
        **old_policy_report["threshold_gate"],
        "schema": "production_recall_gate_policy.v1",
    }
    assert not real_query_gate._validate_persisted_real_query_report(
        old_policy_report,
        expected_release=RELEASE,
    )
    assert not real_query_gate._independent_real_query_metrics_valid(
        old_policy_report,
        baseline=_trusted_baseline(dataset),
    )
    historical_exact_report = deepcopy(stored.content["report"])
    for key in (
        "ranking_identity_schema",
        "ranking_result_digest",
        "ranking_result_refs",
    ):
        historical_exact_report.pop(key, None)
    for sample in historical_exact_report["samples"]:
        for key in (
            "label_ranking_refs",
            "label_ranking_grades",
            "returned_ranking_refs",
            "baseline_ranking_refs",
        ):
            sample.pop(key, None)
    assert real_query_gate._validate_persisted_real_query_report(
        historical_exact_report,
        expected_release=RELEASE,
        allow_legacy_exact_ranking=True,
    )
    assert not real_query_gate._validate_persisted_real_query_report(
        historical_exact_report,
        expected_release=RELEASE,
    )
    assert not real_query_gate._independent_real_query_metrics_valid(
        historical_exact_report,
        baseline=_trusted_baseline(dataset),
    )
    assert real_query_gate._independent_real_query_metrics_valid(
        historical_exact_report,
        baseline=_trusted_baseline(dataset),
        allow_legacy_exact_ranking=True,
    )
    malformed_ranking_report = deepcopy(historical_exact_report)
    malformed_ranking_report["ranking_identity_schema"] = "unknown-ranking.v1"
    assert not real_query_gate._validate_persisted_real_query_report(
        malformed_ranking_report,
        expected_release=RELEASE,
        allow_legacy_exact_ranking=True,
    )
    assert not real_query_gate._independent_real_query_metrics_valid(
        malformed_ranking_report,
        baseline=_trusted_baseline(dataset),
    )
    verification = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE, release=RELEASE)
    assert verification["ok"] is False
    assert verification["reason"] == "current_deployment_receipt_invalid"
    runtime.close()


def test_real_query_gate_scores_rule_labels_against_policy_rule_lane(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    rules = {
        f"{channel}-{index}": _rule_record(
            f"rule-{channel}-{index}", channel, f"source-{channel}", index=index
        )
        for channel in ("openclaw", "codex", "hermes")
        for index in range(5)
    }
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-rule-lane",
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "cases": [
            _case(channel, f"source-{channel}", rules[f"{channel}-{index}"].record_id, index=index)
            for channel in ("openclaw", "codex", "hermes")
            for index in range(5)
        ],
        "baseline_report_id": "prg_baseline_previous_release",
    }
    for case in dataset["cases"]:
        index = int(str(case["case_id"]).rsplit("-", 1)[1])
        features = {
            "terms": ["missing", "capability", "replay", "rollback", f"case-{index}"],
            "intent": "living_posture",
        }
        case["query_features"] = features
        case["query_digest"] = _digest(features)
    _refresh_dataset_evidence(dataset)
    for rule in rules.values():
        runtime.store.append(rule)
    for channel in ("openclaw", "codex", "hermes"):
        runtime.store.append(_label_evidence(channel))
    requested: list[dict] = []

    def recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        requested.append(dict(task_context))
        channel = str(task_context["runtime_channel"])
        index = next((number for number in range(5) if f"case-{number}" in query), 0)
        return RecallBundle(
            items=[],
            rules=[rules[f"{channel}-{index}"]],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    baseline = _trusted_baseline(dataset)
    baseline["metrics"] = {**baseline["metrics"], "precision_at_5": 0.2}
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (baseline, ""),
    )

    report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)

    assert report["accepted"] is True, report
    assert all(sample["returned_refs"][0].startswith("rule-") for sample in report["samples"])
    assert all(call["recall_profile"] == "precision" for call in requested)
    assert all(
        call["candidate_limit"] == real_query_gate._REAL_QUERY_RECALL_DEPTH == 64
        for call in requested
    )
    assert all(call["intent"] == "living_posture" for call in requested)
    runtime.close()


def test_real_query_gate_semantic_duplicate_uses_shared_ground_truth_ranking_identity(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    labelled = {}
    returned = {}
    for channel in ("openclaw", "codex", "hermes"):
        labelled[channel] = _rule_record(
            f"rule-labelled-{channel}", channel, f"source-{channel}", index=0
        )
        returned[channel] = _rule_record(
            f"rule-returned-{channel}", channel, f"source-{channel}", index=0
        )
        labelled[channel].content.update(
            {
                "lesson_record_id": f"lesson-labelled-{channel}",
                "replay_record_id": f"replay-labelled-{channel}",
            }
        )
        returned[channel].content.update(
            {
                "lesson_record_id": f"lesson-returned-{channel}",
                "replay_record_id": f"replay-returned-{channel}",
            }
        )
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-semantic-rule-ranking",
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "cases": [
            _case(
                channel,
                f"source-{channel}",
                labelled[channel].record_id,
                index=index,
            )
            for channel in ("openclaw", "codex", "hermes")
            for index in range(5)
        ],
        "baseline_report_id": "prg_baseline_previous_release",
    }
    for index, case in enumerate(dataset["cases"]):
        features = {
            "terms": ["missing", "capability", "replay", "rollback", f"case-{index}"],
            "intent": "living_posture",
        }
        case["query_features"] = features
        case["query_digest"] = _digest(features)
    _refresh_dataset_evidence(dataset)
    for channel in ("openclaw", "codex", "hermes"):
        runtime.store.append(labelled[channel])
        runtime.store.append(returned[channel])
        runtime.store.append(_label_evidence(channel))
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **kwargs: RecallBundle(
            items=[],
            rules=[returned[str(kwargs["task_context"]["runtime_channel"])]],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        ),
    )
    monkeypatch.setattr(
        real_query_gate,
        "current_release_identity",
        lambda *_args, **_kwargs: RELEASE,
    )
    baseline = _trusted_baseline(dataset)
    baseline["metrics"] = {**baseline["metrics"], "precision_at_5": 0.2}
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (baseline, ""),
    )

    report = run_production_recall_eval(
        runtime,
        dataset,
        seed=False,
        persist_report=True,
    )

    assert report["accepted"] is True, report
    assert report["ranking_identity_schema"] == "production_ranking_identity.v2"
    assert all(sample["label_refs"] == [labelled[sample["channel"]].record_id] for sample in report["samples"])
    assert all(sample["returned_refs"] == [returned[sample["channel"]].record_id] for sample in report["samples"])
    assert all(sample.get("label_ranking_refs") for sample in report["samples"])
    assert all(
        sample["label_ranking_refs"] == sample["returned_ranking_refs"]
        for sample in report["samples"]
    )
    assert report["mrr"] == 1.0
    assert report["ndcg_at_5"] == 1.0
    assert report["top1_stability"] == 1.0
    assert report["jaccard_at_5"] == 1.0
    assert real_query_gate._independent_real_query_metrics_valid(
        report,
        baseline=baseline,
        runtime=runtime,
        scope=BASE_SCOPE,
    )
    for forged_ref in ("forged-ranking-ref", f"gtr_{'0' * 64}"):
        forged = _forge_semantic_ranking_evidence(
            report,
            baseline=baseline,
            ranking_ref=forged_ref,
            grade=1,
        )
        assert not real_query_gate._independent_real_query_metrics_valid(
            forged,
            baseline=baseline,
            runtime=runtime,
            scope=BASE_SCOPE,
        )
    runtime.close()


@pytest.mark.parametrize(
    ("field", "left_value", "right_value"),
    [
        ("priority", "T0", "T1"),
        ("must_use", True, False),
        ("target_capability", "safe-replay", "unsafe-replay"),
    ],
)
def test_ground_truth_semantic_identity_tracks_effective_meta_behavior(
    field,
    left_value,
    right_value,
) -> None:
    left = _rule_record("rule-left", "openclaw", "source-openclaw")
    right = _rule_record("rule-right", "openclaw", "source-openclaw")
    left.meta[field] = left_value
    right.meta[field] = right_value

    assert real_query_gate._record_ranking_ref(
        left
    ) != real_query_gate._record_ranking_ref(right)


def test_ground_truth_semantic_identity_canonicalizes_content_meta_fallback() -> None:
    from_meta = _rule_record("rule-meta", "openclaw", "source-openclaw")
    from_content = _rule_record("rule-content", "openclaw", "source-openclaw")
    from_meta.meta.update(
        {
            "priority": "T0",
            "must_use": True,
            "target_capability": "safe-replay",
        }
    )
    from_content.content.update(
        {
            "priority": "T0",
            "must_use": True,
            "target_capability": "safe-replay",
        }
    )

    assert real_query_gate._record_ranking_ref(
        from_meta
    ) == real_query_gate._record_ranking_ref(from_content)


def test_ground_truth_semantic_identity_distinguishes_priority_authority_conflict() -> None:
    meta_authoritative = _rule_record(
        "rule-meta-authoritative",
        "openclaw",
        "source-openclaw",
    )
    content_only = _rule_record(
        "rule-content-only",
        "openclaw",
        "source-openclaw",
    )
    for record in (meta_authoritative, content_only):
        record.content.update({"priority": "T1", "must_use": True})
    meta_authoritative.meta["priority"] = "T0"
    content_only.meta["priority"] = "T1"

    assert correction_replay._is_ground_truth_rule(meta_authoritative) is True
    assert correction_replay._is_ground_truth_rule(content_only) is False
    assert (
        correction_replay._rule_payload(meta_authoritative)["priority"]
        == correction_replay._rule_payload(content_only)["priority"]
        == "T1"
    )
    assert real_query_gate._record_ranking_ref(
        meta_authoritative
    ) != real_query_gate._record_ranking_ref(content_only)


def test_memory_clone_dedupe_and_ranking_use_the_same_authoritative_identity() -> None:
    labelled = _record("memory-labelled", "hermes", "source-hermes")
    returned_clone = _record("memory-returned", "hermes", "source-hermes")
    returned_clone.summary = "A later deployment run produced different receipt details."
    other_partition = _record("memory-other-source", "hermes", "other-source")

    assert real_query_gate._record_ranking_ref(
        labelled
    ) == real_query_gate._record_ranking_ref(returned_clone)
    assert real_query_gate._record_ranking_ref(
        labelled
    ) != real_query_gate._record_ranking_ref(other_partition)


def test_real_query_gate_semantic_behavior_change_remains_distinct(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    labelled = {}
    returned = {}
    for channel in ("openclaw", "codex", "hermes"):
        labelled[channel] = _rule_record(
            f"rule-labelled-{channel}", channel, f"source-{channel}", index=0
        )
        returned[channel] = _rule_record(
            f"rule-distinct-{channel}", channel, f"source-{channel}", index=0
        )
        labelled[channel].content["lesson_record_id"] = f"lesson-labelled-{channel}"
        returned[channel].content["lesson_record_id"] = f"lesson-returned-{channel}"
        returned[channel].content["expected_behavior"] = "claim the capability exists without replay"
    dataset = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": "production-redacted-distinct-rule-ranking",
        "dataset_kind": "production",
        "scope": BASE_SCOPE,
        "cases": [
            _case(
                channel,
                f"source-{channel}",
                labelled[channel].record_id,
                index=index,
            )
            for channel in ("openclaw", "codex", "hermes")
            for index in range(5)
        ],
        "baseline_report_id": "prg_baseline_previous_release",
    }
    for index, case in enumerate(dataset["cases"]):
        features = {
            "terms": ["missing", "capability", "replay", "rollback", f"case-{index}"],
            "intent": "living_posture",
        }
        case["query_features"] = features
        case["query_digest"] = _digest(features)
    _refresh_dataset_evidence(dataset)
    for channel in ("openclaw", "codex", "hermes"):
        runtime.store.append(labelled[channel])
        runtime.store.append(returned[channel])
        runtime.store.append(_label_evidence(channel))
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **kwargs: RecallBundle(
            items=[],
            rules=[returned[str(kwargs["task_context"]["runtime_channel"])]],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        ),
    )
    monkeypatch.setattr(
        real_query_gate,
        "current_release_identity",
        lambda *_args, **_kwargs: RELEASE,
    )
    baseline = _trusted_baseline(dataset)
    baseline["metrics"] = {**baseline["metrics"], "precision_at_5": 0.2}
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (baseline, ""),
    )

    report = run_production_recall_eval(
        runtime,
        dataset,
        seed=False,
        persist_report=True,
    )

    assert report["accepted"] is False
    assert all(sample.get("label_ranking_refs") for sample in report["samples"])
    assert all(
        sample["label_ranking_refs"] != sample["returned_ranking_refs"]
        for sample in report["samples"]
    )
    assert report["mrr"] == 0.0
    assert report["top1_stability"] == 0.0
    runtime.close()


def test_external_tracemalloc_is_never_reset_or_stopped_and_cannot_accept_memory_gate(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"external-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    _append_records_and_label_evidence(runtime, records)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (_trusted_baseline(dataset), ""),
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **kwargs: RecallBundle(
            items=[records[str(kwargs["task_context"]["runtime_channel"])]],
            rules=[], reflections=[], confidence=1.0, next_action_hint="", explanation={},
        ),
    )
    tracemalloc.start()
    historical = bytearray(8 * 1024 * 1024)
    del historical
    gc.collect()
    peak_before = tracemalloc.get_traced_memory()[1]
    try:
        report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
        assert tracemalloc.is_tracing() is True
        assert tracemalloc.get_traced_memory()[1] >= peak_before
    finally:
        tracemalloc.stop()
    assert report["accepted"] is False
    assert report["memory_measurement"]["mode"] == "external_tracer_unavailable"
    assert "peak_memory_measurement" in report["threshold_gate"]["blocking_metrics"]
    runtime.close()


def test_latency_measurement_excludes_isolated_tracemalloc_probe_overhead(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"isolated-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    _append_records_and_label_evidence(runtime, records)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (_trusted_baseline(dataset), ""),
    )
    monkeypatch.setattr(
        runtime.memory,
        "recall",
        lambda **kwargs: RecallBundle(
            items=[records[str(kwargs["task_context"]["runtime_channel"])]],
            rules=[],
            reflections=[],
            confidence=1.0,
            next_action_hint="",
            explanation={},
        ),
    )
    clock = 0.0

    def traced_clock() -> float:
        nonlocal clock
        clock += 1.0 if tracemalloc.is_tracing() else 0.001
        return clock

    monkeypatch.setattr(real_query_gate, "perf_counter", traced_clock)

    report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)

    assert report["memory_measurement"]["mode"] == "isolated_tracemalloc"
    assert report["latency_ms_p95"] == pytest.approx(1.0)
    runtime.close()


def test_poisoned_scope_or_source_is_an_unconditional_leakage_block(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    good = {
        channel: _record(f"record-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    poisoned = _record("poison", "codex", "source-codex")
    dataset = _dataset({channel: record.record_id for channel, record in good.items()})
    _append_records_and_label_evidence(runtime, good)

    def recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        return RecallBundle(
            items=[poisoned], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    monkeypatch.setattr(
        real_query_gate,
        "_resolve_trusted_baseline",
        lambda *_args, **_kwargs: (_trusted_baseline(dataset), ""),
    )
    report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)

    assert report["accepted"] is False
    assert report["gate_status"] == "blocked"
    assert report["cross_channel_leakage_count"] > 0
    assert report["source_filter_leakage_count"] > 0
    assert report["persisted"] is True
    runtime.close()


def test_diagnostic_and_baseline_mismatch_never_overwrite_accepted_report(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(real_query_gate, "current_release_identity", lambda *_args, **_kwargs: RELEASE)
    legacy = run_production_recall_eval(
        runtime,
        {"name": "runtime-generated-smoke", "scope": BASE_SCOPE, "cases": []},
        seed=False,
        persist_report=True,
    )
    assert legacy["accepted"] is False
    assert legacy["gate_status"] == "diagnostic"

    records = {
        channel: _record(f"record-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    _append_records_and_label_evidence(runtime, records)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})
    dataset["baseline"] = {"dataset_digest": freeze_production_recall_dataset(dataset)["dataset_digest"], "accepted": True}
    _refresh_dataset_evidence(dataset)
    report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    assert report["gate_status"] == "not_run"
    assert report["blocked_reason"] == "current_deployment_receipt_invalid"
    assert report["persisted"] is True
    runtime.close()


def test_low_signal_not_run_report_verifies_as_dataset_gap_not_contract_invalid(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        current = _receipt(runtime, commit="a" * 40, version="1.9.80", prior_commit="b" * 40)
        runtime._test_runtime_commit = current.commit
        frozen = freeze_production_recall_dataset(
            _dataset(
                {
                    "openclaw": "record-openclaw",
                    "codex": "record-codex",
                    "hermes": "record-hermes",
                }
            )
        )
        report = real_query_gate._not_run_real_query_report(
            frozen,
            "query_features_low_signal",
            release=current,
        )
        persisted = real_query_gate._persist_eligible_high_water(
            runtime,
            report,
            scope=ScopeRef.from_dict(BASE_SCOPE),
            persist=True,
        )

        verified = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    finally:
        runtime.close()

    assert persisted["persisted"] is True
    assert verified == {
        "ok": False,
        "status": "not_run",
        "reason": "query_features_low_signal",
        "record_id": persisted["persisted_record_id"],
    }


def test_trusted_prior_release_baseline_and_latest_blocked_high_water(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"trusted-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    _append_records_and_label_evidence(runtime, records)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})

    def recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        channel = "openclaw"
        if str(scope["workspace_id"]).endswith("::channel::codex"):
            channel = "codex"
        elif str(scope["workspace_id"]).endswith("::channel::hermes"):
            channel = "hermes"
        return RecallBundle(
            items=[records[channel]], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    prior = _receipt(runtime, commit="b" * 40, version="1.9.79", prior_commit="c" * 40)
    runtime._test_runtime_commit = prior.commit
    monkeypatch.setattr(
        real_query_gate,
        "_verified_live_prior_release",
        lambda *_args, **_kwargs: (prior, ""),
    )
    bootstrap_dataset = deepcopy(dataset)
    bootstrap_dataset["baseline_report_id"] = ""
    _refresh_dataset_evidence(bootstrap_dataset)
    bootstrap = bootstrap_production_recall_baseline(
        runtime,
        bootstrap_dataset,
        candidate_commit="a" * 40,
        prior_commit=prior.commit,
        persist_report=True,
    )
    assert bootstrap["bootstrap_status"] == "anchor_ready", bootstrap
    dataset["baseline_report_id"] = bootstrap["persisted_record_id"]
    _refresh_dataset_evidence(dataset)
    current = _receipt(runtime, commit="a" * 40, version="1.9.80", prior_commit=prior.commit)
    runtime._test_runtime_commit = current.commit

    baseline_record = runtime.store.get_by_id(dataset["baseline_report_id"], scope=BASE_SCOPE)
    assert baseline_record is not None
    baseline_original = deepcopy(baseline_record.content["report"])
    baseline_record.content["report"]["metrics"]["recall_at_5"] = 0.5
    runtime.store.rewrite(baseline_record)
    tampered_baseline, tampered_baseline_reason = real_query_gate._resolve_trusted_baseline(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        report_id=baseline_record.record_id,
        dataset_digest=freeze_production_recall_dataset(dataset)["dataset_digest"],
        current_release=current,
    )
    assert tampered_baseline is None
    assert tampered_baseline_reason == "baseline_report_payload_tampered"
    baseline_record.content["report"] = deepcopy(baseline_original)
    runtime.store.rewrite(baseline_record)

    baseline_record.content["report"].update(
        {
            "accepted": False,
            "gate_status": "blocked",
            "blocked_reason": "production_recall_gate_failed",
            "threshold_gate": {
                **baseline_record.content["report"]["threshold_gate"],
                "ok": False,
                "blocked_reason": "production_recall_gate_failed",
                "blocking_metrics": {"recall_at_5": {"actual": 0.0, "threshold": 0.9}},
            },
        }
    )
    baseline_record.meta["report_payload_digest"] = _digest(baseline_record.content["report"])
    runtime.store.rewrite(baseline_record)
    rejected_baseline, rejected_reason = real_query_gate._resolve_trusted_baseline(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        report_id=baseline_record.record_id,
        dataset_digest=freeze_production_recall_dataset(dataset)["dataset_digest"],
        current_release=current,
    )
    assert rejected_baseline is None
    assert rejected_reason == "baseline_report_not_qualified"
    baseline_record.content["report"] = baseline_original
    baseline_record.meta["report_payload_digest"] = _digest(baseline_original)
    runtime.store.rewrite(baseline_record)

    accepted = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    assert accepted["accepted"] is True
    verified = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert verified["ok"] is True, verified

    accepted_record = runtime.store.get_by_id(accepted["persisted_record_id"], scope=BASE_SCOPE)
    assert accepted_record is not None
    original_report = deepcopy(accepted_record.content["report"])
    accepted_record.content["report"]["metrics"]["recall_at_5"] = 0.0
    runtime.store.rewrite(accepted_record)
    tampered = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert tampered["ok"] is False
    assert tampered["reason"] == "production_recall_report_payload_tampered"
    accepted_record.content["report"] = original_report
    runtime.store.rewrite(accepted_record)
    assert verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)["ok"] is True

    blocked = {
        **accepted,
        "ok": False,
        "accepted": False,
        "gate_status": "blocked",
        "blocked_reason": "production_recall_gate_failed",
        "report_id": "prg_latest_blocked_attempt",
        "cross_channel_leakage_count": 1,
        "threshold_gate": {
            **accepted["threshold_gate"],
            "ok": False,
            "blocked_reason": "production_recall_gate_failed",
            "blocking_metrics": {
                "cross_channel_leakage_count": {"actual": 1, "threshold": 0, "operator": "=="}
            },
        },
    }
    persisted_blocked = real_query_gate._persist_eligible_high_water(
        runtime,
        blocked,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        persist=True,
    )
    invalidated = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert invalidated["ok"] is False
    assert invalidated["record_id"] == persisted_blocked["persisted_record_id"]
    assert invalidated["record_id"] != persisted_blocked["report_id"]

    next_release = _receipt(runtime, commit="d" * 40, version="1.9.81", prior_commit=current.commit)
    rejected_old_accept, old_accept_reason = real_query_gate._resolve_trusted_baseline(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
        report_id=accepted["persisted_record_id"],
        dataset_digest=freeze_production_recall_dataset(dataset)["dataset_digest"],
        current_release=next_release,
        dataset_evidence=freeze_production_recall_dataset(dataset)["secure_dataset_evidence"],
    )
    assert rejected_old_accept is None
    assert old_accept_reason == "baseline_report_not_latest_prior_high_water"

    run_production_recall_eval(
        runtime,
        {"name": "diagnostic", "scope": BASE_SCOPE, "cases": []},
        seed=False,
        persist_report=True,
    )
    after_diagnostic = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert after_diagnostic["record_id"] == persisted_blocked["persisted_record_id"]

    synthetic = deepcopy(dataset)
    synthetic["dataset_kind"] = "synthetic"
    _refresh_dataset_evidence(synthetic)
    synthetic_report = run_production_recall_eval(runtime, synthetic, seed=False, persist_report=True)
    assert synthetic_report["persisted"] is False
    assert verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)["record_id"] == persisted_blocked["persisted_record_id"]

    broken_production = deepcopy(dataset)
    broken_production["cases"] = broken_production["cases"][:2]
    _refresh_dataset_evidence(broken_production)
    broken = run_production_recall_eval(runtime, broken_production, seed=False, persist_report=True)
    assert broken["gate_status"] == "not_run"
    assert broken["persisted"] is True
    newest = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert newest["ok"] is False
    assert newest["record_id"] == broken["persisted_record_id"]

    recovered = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    assert recovered["accepted"] is True
    assert recovered["report_id"] == accepted["report_id"]
    assert recovered["persisted_record_id"] != accepted["persisted_record_id"]
    assert verify_current_production_recall_gate(runtime, scope=BASE_SCOPE) == {
        **verified,
        "record_id": recovered["persisted_record_id"],
    }

    idempotent_recovery = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    assert idempotent_recovery["persisted_record_id"] == recovered["persisted_record_id"]
    runtime.close()


def test_strict_bootstrap_capture_enables_next_release_and_rejects_predeploy_or_stale_evidence(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    records = {
        channel: _record(f"bootstrap-{channel}", channel, f"source-{channel}")
        for channel in ("openclaw", "codex", "hermes")
    }
    _append_records_and_label_evidence(runtime, records)
    dataset = _dataset({channel: record.record_id for channel, record in records.items()})

    def recall(*, query: str, scope: dict, task_context: dict, limit: int) -> RecallBundle:
        channel = "openclaw"
        if str(scope["workspace_id"]).endswith("::channel::codex"):
            channel = "codex"
        elif str(scope["workspace_id"]).endswith("::channel::hermes"):
            channel = "hermes"
        return RecallBundle(
            items=[records[channel]], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation={"fusion": {"policy_version": "rrf-page-pool.v1"}},
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    prior = _receipt(runtime, commit="b" * 40, version="1.9.79", prior_commit="c" * 40)
    runtime._test_runtime_commit = prior.commit
    monkeypatch.setattr(
        real_query_gate,
        "_verified_live_prior_release",
        lambda *_args, **_kwargs: (prior, ""),
    )
    bootstrap_dataset = deepcopy(dataset)
    bootstrap_dataset["baseline_report_id"] = ""
    _refresh_dataset_evidence(bootstrap_dataset)
    ordinary = run_production_recall_eval(runtime, bootstrap_dataset, seed=False, persist_report=True)
    assert ordinary["baseline_capture"] is False
    bootstrap = bootstrap_production_recall_baseline(
        runtime,
        bootstrap_dataset,
        candidate_commit="a" * 40,
        prior_commit=prior.commit,
        persist_report=True,
    )
    assert bootstrap["accepted"] is False
    assert bootstrap["gate_status"] == "not_run"
    assert bootstrap["blocked_reason"] == "pre_switch_bootstrap_anchor"
    assert bootstrap["baseline_capture"] is True
    assert bootstrap["bootstrap_status"] == "anchor_ready"
    repeated = bootstrap_production_recall_baseline(
        runtime,
        bootstrap_dataset,
        candidate_commit="a" * 40,
        prior_commit=prior.commit,
        persist_report=True,
    )
    assert repeated["persisted_record_id"] == bootstrap["persisted_record_id"]
    legacy_bootstrap_record = runtime.store.get_by_id(
        bootstrap["persisted_record_id"],
        scope=BASE_SCOPE,
    )
    assert legacy_bootstrap_record is not None
    legacy_bootstrap_report = legacy_bootstrap_record.content["report"]
    for key in (
        "ranking_identity_schema",
        "ranking_result_digest",
        "ranking_result_refs",
    ):
        legacy_bootstrap_report.pop(key, None)
    for sample in legacy_bootstrap_report["samples"]:
        for key in (
            "label_ranking_refs",
            "label_ranking_grades",
            "returned_ranking_refs",
            "baseline_ranking_refs",
        ):
            sample.pop(key, None)
    legacy_bootstrap_record.meta["report_payload_digest"] = _digest(
        legacy_bootstrap_report
    )
    runtime.store.rewrite(legacy_bootstrap_record)

    strict_replacement = bootstrap_production_recall_baseline(
        runtime,
        bootstrap_dataset,
        candidate_commit="a" * 40,
        prior_commit=prior.commit,
        persist_report=True,
    )
    assert (
        strict_replacement["persisted_record_id"]
        != legacy_bootstrap_record.record_id
    )
    assert (
        strict_replacement["ranking_identity_schema"]
        == "production_ranking_identity.v2"
    )
    bootstrap = strict_replacement

    current = _receipt(runtime, commit="a" * 40, version="1.9.80", prior_commit=prior.commit)
    runtime._test_runtime_commit = current.commit
    dataset["baseline_report_id"] = bootstrap["persisted_record_id"]
    _refresh_dataset_evidence(dataset)
    accepted = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
    assert accepted["accepted"] is True, accepted
    assert accepted["baseline_identity"]["persisted_record_id"] == bootstrap["persisted_record_id"]
    assert accepted["baseline_identity"]["logical_report_id"] == bootstrap["report_id"]
    assert verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)["ok"] is True
    descriptive_version_drift = ReleaseIdentity(
        commit=current.commit,
        version="9.9.999",
        receipt_id=current.receipt_id,
        session_id=current.session_id,
    )
    assert verify_current_production_recall_gate(
        runtime,
        scope=BASE_SCOPE,
        release=descriptive_version_drift,
    )["ok"] is True

    engine = runtime.memory.recall_engine
    original_identity = engine.effective_identity()
    changed_identity = {key: value for key, value in original_identity.items() if key != "identity_digest"}
    changed_identity["policy_version"] = "drifted-policy.v9"
    changed_identity["identity_digest"] = real_query_gate._engine_identity_digest(changed_identity)
    with monkeypatch.context() as identity_patch:
        identity_patch.setattr(type(engine), "effective_identity", lambda _self: dict(changed_identity))
        drift = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert drift["ok"] is False
    assert drift["reason"] == "retrieval_identity_mismatch"

    original_latest = runtime.store.latest_record_by_meta_value_exact_scope
    release_reads = 0

    def changing_latest(**kwargs):
        nonlocal release_reads
        record = original_latest(**kwargs)
        if kwargs.get("meta_key") == "release_high_water_key" and record is not None:
            release_reads += 1
            if release_reads % 2 == 0:
                changed = deepcopy(record)
                changed.record_id = f"changed-high-water-{release_reads}"
                return changed
        return record

    with monkeypatch.context() as high_water_patch:
        high_water_patch.setattr(runtime.store, "latest_record_by_meta_value_exact_scope", changing_latest)
        changed = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert changed["ok"] is False
    assert changed["reason"] == "production_recall_high_water_changed"
    assert release_reads == 4

    accepted_record = runtime.store.get_by_id(accepted["persisted_record_id"], scope=BASE_SCOPE)
    assert accepted_record is not None
    accepted_created_at = accepted_record.time.created_at
    accepted_record.time.created_at = "2000-01-01T00:00:00+00:00"
    runtime.store.rewrite(accepted_record)
    predeploy = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert predeploy["ok"] is False
    assert predeploy["reason"] == "production_recall_report_predeploy"
    accepted_record.time.created_at = accepted_created_at
    runtime.store.rewrite(accepted_record)

    monkeypatch.setattr(
        real_query_gate,
        "_verified_live_prior_release",
        lambda *_args, **_kwargs: (current, ""),
    )
    next_bootstrap = bootstrap_production_recall_baseline(
        runtime,
        dataset,
        candidate_commit="d" * 40,
        prior_commit=current.commit,
        persist_report=True,
    )
    assert next_bootstrap["bootstrap_status"] == "anchor_ready"
    assert next_bootstrap["baseline_capture"] is True
    assert next_bootstrap["evaluator_commit"] == "d" * 40
    next_state = real_query_gate._latest_bootstrap_state(
        runtime,
        scope=ScopeRef.from_dict(BASE_SCOPE),
    )
    assert next_state is not None
    assert next_state["state"] == "anchor_ready"
    assert next_state["candidate_commit"] == "d" * 40

    newer = _receipt(runtime, commit="d" * 40, version="1.9.81", prior_commit=current.commit)
    runtime._test_runtime_commit = newer.commit
    stale = verify_current_production_recall_gate(runtime, scope=BASE_SCOPE)
    assert stale["ok"] is False
    assert stale["reason"] == "current_release_production_recall_report_missing"
    runtime.close()


def test_attempt_high_water_is_transactional_and_adjacent_retry_is_idempotent(tmp_path) -> None:
    first_runtime = Runtime.create(root=tmp_path)
    second_runtime = Runtime.create(root=tmp_path)
    frozen = freeze_production_recall_dataset(
        _dataset({channel: f"missing-{channel}" for channel in ("openclaw", "codex", "hermes")})
    )
    repeated = real_query_gate._not_run_real_query_report(
        frozen,
        "accepted_label_record_missing",
        release=RELEASE,
    )
    start = Barrier(2)

    def persist(runtime: Runtime, report: dict) -> dict:
        start.wait(timeout=5)
        return real_query_gate._persist_eligible_high_water(
            runtime,
            deepcopy(report),
            scope=ScopeRef.from_dict(BASE_SCOPE),
            persist=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = list(
            executor.map(
                lambda pair: persist(*pair),
                ((first_runtime, repeated), (second_runtime, repeated)),
            )
        )
    assert same_results[0]["persisted_record_id"] == same_results[1]["persisted_record_id"]

    start = Barrier(2)
    changed_a = {**repeated, "report_id": "prg_atomic_a", "blocked_reason": "baseline_report_id_missing"}
    changed_b = {**repeated, "report_id": "prg_atomic_b", "blocked_reason": "verified_prior_release_unavailable"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        changed_results = list(
            executor.map(
                lambda pair: persist(*pair),
                ((first_runtime, changed_a), (second_runtime, changed_b)),
            )
        )
    changed_ids = {item["persisted_record_id"] for item in changed_results}
    assert len(changed_ids) == 2
    latest = first_runtime.store.latest_record_by_meta_value_exact_scope(
        kind="reflection",
        source="eimemory.evaluation.production_recall",
        status="active",
        scope=BASE_SCOPE,
        meta_key="report_type",
        meta_value="production_recall_gate",
    )
    assert latest is not None
    assert latest.record_id in changed_ids
    assert latest.content["report"]["previous_attempt_id"] in changed_ids
    assert latest.content["report"]["previous_attempt_id"] != latest.record_id
    count = first_runtime.store.sqlite.conn.execute(
        "SELECT COUNT(*) FROM records WHERE source='eimemory.evaluation.production_recall'"
    ).fetchone()[0]
    assert count == 3
    second_runtime.close()
    first_runtime.close()


def _ready_l5_payload() -> dict:
    return {
        "ok": True,
        "schema_version": "l5_readiness.v2",
        "capability_gaps": [],
        "current_stage": "L5",
        "readiness_score": 1.0,
        "latest_l5_assessment": {"trusted": True, "complete": True, "level": "L5"},
        "live_task_gate": {"ok": True, "current_deployment_verified_real_tasks": 10},
        "verified_replay": {
            "executed_count": 10,
            "weak_capabilities_missing": [],
            "manifest_rejection_reasons": {},
        },
        "verified_core_replay": {
            "executed_count": 15,
            "core_capabilities_missing": [],
            "manifest_rejection_reasons": {},
        },
        "production_recall_gate": {"ok": True, "status": "accepted"},
        "release_identity": {
            "release_commit": RELEASE.commit,
            "release_version": RELEASE.version,
            "deployment_receipt_id": RELEASE.receipt_id,
            "release_session_id": RELEASE.session_id,
        },
        "production_recall_strict_state": {
            "ok": True,
            "status": "strict_activated",
            "candidate_commit": RELEASE.commit,
        },
        "storage_migrations": {"ok": True, "status": "ready", "pending": []},
    }


def test_l5_v2_readiness_gate_status_without_runtime_fails_closed() -> None:
    ready = _ready_l5_payload()
    assert readiness_gate_status(ready) == ""

    for bad in (
        {},
        {"ok": False, "status": "not_run"},
        {"ok": False, "status": "blocked"},
        {"ok": True, "status": "diagnostic"},
    ):
        assert readiness_gate_status({**ready, "production_recall_gate": bad}) == ""
    assert readiness_gate_status({**ready, "schema_version": "l5_readiness.v1"}) == ""


def test_l5_report_surfaces_independent_real_query_evidence_lookup(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    expected = {
        "ok": False,
        "status": "not_run",
        "reason": "release_identity_unavailable",
        "record_id": "",
    }
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_gate",
        lambda *_args, **_kwargs: dict(expected),
    )
    monkeypatch.setattr(
        "eimemory.governance.l5_readiness._stage_for",
        lambda *_args, **_kwargs: {
            "stage": "L5",
            "label": "would otherwise be L5",
            "readiness_score": 1.0,
            "reason": "all non-recall evidence passed",
            "done_when": "keep all gates green",
            "risk_boundary": "read-only",
            "live_task_gate": {"ok": True, "current_deployment_verified_real_tasks": 10},
        },
    )

    report = build_l5_readiness_report(runtime, scope=BASE_SCOPE)

    assert report["schema_version"] == "l5_readiness.v2"
    for field, value in expected.items():
        assert report["production_recall_gate"][field] == value
    assert report["production_recall_gate"]["evidence_mode"] == "current_release"
    assert report["production_recall_gate"]["evidence_release_commit"] == ""
    assert report["production_recall_gate"]["current_release_commit"] == ""
    assert report["current_stage"] == "L4.5"
    assert report["readiness_score"] <= 0.8
    assert any("production recall" in action.lower() for action in report["next_actions"])
    assert readiness_gate_status(report) == ""
    runtime.close()


def test_l5_report_cannot_claim_l5_while_deferred_storage_migrations_are_pending(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_gate",
        lambda *_args, **_kwargs: {"ok": True, "status": "accepted", "record_id": "recall-gate"},
    )
    monkeypatch.setattr(runtime.store.sqlite, "pending_storage_migrations", lambda: ["records.payload_archive.v1"])
    monkeypatch.setattr(
        "eimemory.governance.l5_readiness._stage_for",
        lambda *_args, **_kwargs: {
            "stage": "L5",
            "label": "otherwise ready",
            "readiness_score": 1.0,
            "reason": "ready",
            "done_when": "keep green",
            "risk_boundary": "read-only",
            "live_task_gate": {"ok": True},
        },
    )

    report = build_l5_readiness_report(runtime, scope=BASE_SCOPE)

    assert report["current_stage"] == "L4.5"
    assert report["readiness_score"] <= 0.8
    assert report["storage_migrations"]["pending"] == ["records.payload_archive.v1"]
    assert readiness_gate_status(report) == ""
    runtime.close()
