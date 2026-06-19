from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_recall import evaluate_production_recall_quality_gate
from eimemory.intake.loop import candidates_to_records
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


OUTCOME_RULE_SOURCES = {"diagnosis_pattern", "operator_gap", "visual_evidence_gap", "world_state_mismatch"}


def run_nightly_jobs(
    runtime: Runtime,
    *,
    scope: dict,
    replay_datasets: dict[str, list[dict]] | None = None,
    external_fetch_text: Callable[[str], str] | None = None,
) -> dict:
    roi = runtime.evolution.build_roi_report(scope=scope)
    active_rules = runtime.store.list_records(kinds=["rule"], scope=scope, status="active", limit=500)
    promotion_candidates = runtime.store.list_records(kinds=["rule"], scope=scope, status="accepted", limit=500)
    memories = runtime.store.list_records(kinds=["memory", "multimodal_memory"], scope=scope, limit=500)
    paper_sources = runtime.store.list_records(kinds=["paper_source"], scope=scope, limit=1000)
    claim_cards = runtime.store.list_records(kinds=["claim_card"], scope=scope, limit=1000)
    knowledge_pages = runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=1000)
    knowledge_report = runtime.evolution.reconcile_knowledge(scope=scope)
    quality_report = runtime.evolution.memory_quality_report(scope=scope)
    source_expansion_report = runtime.expand_sources_autonomously(scope=scope, apply=True, max_apply=3)
    news_source_promotion_report = _promote_news_rss_source_candidates(runtime, scope=scope)
    intake_report = runtime.run_knowledge_intake(scope=scope, persist=True, limit=100)
    external_collection_report = _run_external_collection(
        runtime,
        scope=scope,
        limit=100,
        fetch_text=external_fetch_text,
    )
    paper_promotion_report = _run_paper_candidate_promotion(
        runtime,
        scope=scope,
        candidate_records=external_collection_report.get("_candidate_records", []),
    )
    operational_projection_report = _run_operational_projection(runtime, scope=scope)
    research_digest_report = _run_research_digest(runtime, scope=scope)
    external_collection_report.pop("_candidate_records", None)
    source_quality_report = runtime.source_quality_report(scope=scope)
    collection_policy = runtime.collection_policy(scope=scope)
    source_discovery_report = _run_source_discovery(runtime, scope=scope)
    replay_datasets = replay_datasets or {}
    replay_reports = []
    for rule in active_rules:
        dataset = replay_datasets.get(rule.record_id)
        if dataset:
            replay_reports.append(runtime.evolution.replay_rule(record_id=rule.record_id, dataset=dataset))
    rule_evolution_report = _run_rule_evolution(
        runtime,
        scope=scope,
        replay_datasets=replay_datasets,
    )
    memory_eval_ci_report = _run_memory_eval_ci(runtime, scope=scope)
    production_recall_report = _run_production_recall_eval(runtime, scope=scope)
    daily_brief_report = _run_daily_brief(runtime, scope=scope)
    judgment_evaluation_report = _run_judgment_evaluation(runtime, scope=scope)
    autonomous_evolution_report = _run_autonomous_evolution(runtime, scope=scope)
    autonomous_learning_report = _run_autonomous_learning(runtime, scope=scope)
    autonomous_learning_daily_report = _run_autonomous_learning_daily_report(runtime, scope=scope)
    autonomous_learning_dashboard = _run_autonomous_learning_dashboard(runtime, scope=scope)
    outcome_evolution_report = _run_outcome_evolution_summary(runtime, scope=scope)
    return {
        "ok": True,
        "active_rule_count": len(active_rules),
        "promotion_candidate_count": len(promotion_candidates),
        "memory_count": len(memories),
        "knowledge": {
            "paper_source_count": len(paper_sources),
            "claim_card_count": len(claim_cards),
            "knowledge_page_count": len(knowledge_pages),
            "contradiction_count": knowledge_report["contradiction_count"],
            "refreshed_page_count": knowledge_report["page_refresh_count"],
        },
        "replay": {
            "executed": len(replay_reports),
            "pass_count": sum(1 for report in replay_reports if report.meta.get("verdict") == "pass"),
            "fail_count": sum(1 for report in replay_reports if report.meta.get("verdict") == "fail"),
        },
        "memory_quality": quality_report,
        "source_expansion": {
            "ok": bool(source_expansion_report.get("ok", True)),
            "proposal_count": int(source_expansion_report.get("proposal_count") or 0),
            "approved_count": int(source_expansion_report.get("approved_count") or 0),
            "rejected_count": int(source_expansion_report.get("rejected_count") or 0),
            "duplicate_count": int(source_expansion_report.get("duplicate_count") or 0),
            "applied_count": int(source_expansion_report.get("applied_count") or 0),
            "updated_source_ids": list(source_expansion_report.get("updated_source_ids") or []),
            "audit_record_ids": list(source_expansion_report.get("audit_record_ids") or []),
        },
        "news_source_promotion": news_source_promotion_report,
        "knowledge_intake": {
            "scanned_count": intake_report["scanned_count"],
            "candidate_count": intake_report["candidate_count"],
            "rejected_count": intake_report["rejected_count"],
            "quarantined_count": intake_report["quarantined_count"],
            "written_count": intake_report["written_count"],
            "skipped_existing_count": intake_report.get("skipped_existing_count", 0),
        },
        "external_collection": external_collection_report,
        "paper_promotion": paper_promotion_report,
        "operational_projection": operational_projection_report,
        "research_digest": research_digest_report,
        "daily_brief": daily_brief_report,
        "rule_evolution": rule_evolution_report,
        "autonomous_evolution": autonomous_evolution_report,
        "autonomous_learning": autonomous_learning_report,
        "autonomous_learning_daily_report": autonomous_learning_daily_report,
        "autonomous_learning_dashboard": autonomous_learning_dashboard,
        "outcome_evolution": outcome_evolution_report,
        "memory_eval_ci": memory_eval_ci_report,
        "production_recall": production_recall_report,
        "recall_quality": production_recall_report,
        "recall_quality_gate": production_recall_report.get("quality_gate") or {
            "ok": False,
            "blocked_reason": production_recall_report.get("eval_skipped_reason")
            or production_recall_report.get("error")
            or "recall_quality_unavailable",
            "blocking_metrics": {},
        },
        "judgment_evaluation": judgment_evaluation_report,
        "source_discovery": source_discovery_report,
        "source_quality": {
            "source_count": source_quality_report["source_count"],
            "run_now": collection_policy["run_now"],
            "pause": collection_policy["pause"],
            "lower_frequency": collection_policy["lower_frequency"],
            "gap_query_count": len(collection_policy["gap_queries"]),
        },
        "roi": roi,
    }


def _run_outcome_evolution_summary(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    traces: list[RecordEnvelope] = []
    limit = 500
    offset = 0
    while True:
        records = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=limit, offset=offset)
        traces.extend(record for record in records if _is_outcome_trace(record))
        if len(records) < limit:
            break
        offset += limit
    outcome_rules = [
        rule
        for rule in runtime.store.list_records(kinds=["rule"], scope=scope, limit=500)
        if str(business_metadata(rule.meta).get("evolution_source_type") or "") in OUTCOME_RULE_SOURCES
    ]
    rollout_ledger: list[dict[str, Any]] = []
    get_ledger = getattr(runtime, "get_policy_rollout_ledger", None)
    if callable(get_ledger):
        try:
            rollout_ledger = list(get_ledger(scope=scope, limit=200))
        except Exception:
            rollout_ledger = []
    return _outcome_evolution_summary(traces, rules=outcome_rules, rollout_ledger=rollout_ledger)


def _is_outcome_trace(record: RecordEnvelope) -> bool:
    meta = business_metadata(record.meta)
    return (
        str(meta.get("report_type") or "").strip() == "outcome_trace"
        and str(meta.get("schema_version") or "").strip() == "outcome_trace.v1"
    )


def _outcome_evolution_summary(
    records: list[RecordEnvelope],
    *,
    rules: list[RecordEnvelope] | None = None,
    rollout_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    label_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    bad_outcome_count = 0
    operator_gap_count = 0
    visual_gap_count = 0
    world_state_mismatch_count = 0

    for record in records:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
        primary_label = str(meta.get("primary_label") or "").strip()
        if primary_label:
            label_counts[primary_label] += 1
            if primary_label != "success":
                bad_outcome_count += 1
        signals = _diagnosis_signals(meta.get("diagnosis_signals") or meta.get("signals"))
        signal_counts.update(signals)
        signal_set = set(signals)
        operator_gap = _dict_first(content.get("operator_gap"), payload.get("operator_gap"))
        visual_evidence = _dict_first(content.get("visual_evidence"), payload.get("visual_evidence"))
        world_state = _dict_first(content.get("world_state"), payload.get("world_state"))
        if "operator_gap" in signal_set or _has_operator_gap(operator_gap):
            operator_gap_count += 1
        if _has_visual_gap(signal_set, visual_evidence):
            visual_gap_count += 1
        if "world_state_mismatch" in signal_set or _has_world_state_mismatch(world_state):
            world_state_mismatch_count += 1

    outcome_trace_count = len(records)
    bad_outcome_rate = round(bad_outcome_count / outcome_trace_count, 3) if outcome_trace_count else 0.0
    rule_items = list(rules or [])
    generated_rule_count = len(rule_items)
    shadow_rule_count = sum(1 for rule in rule_items if str(rule.status or "") == "shadow")
    promoted_rule_count = sum(1 for rule in rule_items if str(rule.status or "") == "active")
    ledger_items = list(rollout_ledger or [])
    rolled_back_count = sum(1 for item in ledger_items if str(item.get("action_type") or "") == "rollback")
    return {
        "outcome_trace_count": outcome_trace_count,
        "bad_outcome_count": bad_outcome_count,
        "bad_outcome_rate": bad_outcome_rate,
        "top_primary_labels": _top_counter_items(label_counts, "label"),
        "top_signals": _top_counter_items(signal_counts, "signal"),
        "operator_gap_count": operator_gap_count,
        "visual_gap_count": visual_gap_count,
        "world_state_mismatch_count": world_state_mismatch_count,
        "generated_rule_count": generated_rule_count,
        "shadow_rule_count": shadow_rule_count,
        "promoted_rule_count": promoted_rule_count,
        "rolled_back_count": rolled_back_count,
    }


def _diagnosis_signals(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _has_visual_gap(signals: set[str], visual_evidence: dict[str, Any]) -> bool:
    if {"missing_visual_evidence", "visual_gap"} & signals:
        return True
    status = str(visual_evidence.get("status") or visual_evidence.get("state") or "").strip().lower()
    if status in {"missing", "absent", "unavailable", "insufficient"}:
        return True
    required = _truthy(visual_evidence.get("required")) or _truthy(visual_evidence.get("expected"))
    unavailable = visual_evidence.get("available") is False or visual_evidence.get("present") is False
    return required and unavailable


def _has_operator_gap(value: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("detected") is True:
        return True
    for key in ("missing", "missing_confirmation", "missing_info", "needs_operator", "approval_missing"):
        item = value.get(key)
        if isinstance(item, str) and item.strip().lower() in {"", "none", "no", "false", "0"}:
            continue
        if _truthy(item):
            return True
    expected = _first_text(value.get("expected"), value.get("expected_behavior"), value.get("required_behavior"))
    observed = _first_text(value.get("actual"), value.get("observed"), value.get("observed_behavior"))
    return bool(expected and observed and expected != observed)


def _has_world_state_mismatch(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("mismatch") is True:
        return True
    status = str(value.get("status") or value.get("state") or "").strip().lower()
    if status in {"mismatch", "mismatched", "stale"}:
        return True
    expected = _first_text(value.get("expected"), value.get("target"), value.get("state_after"))
    observed = _first_text(value.get("observed"), value.get("actual"), value.get("current"))
    return bool(expected and observed and expected != observed)


def _top_counter_items(counter: Counter[str], key: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {key: item, "count": count}
        for item, count in sorted(counter.items(), key=lambda entry: (-entry[1], entry[0]))[:limit]
    ]


def _dict_first(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _run_production_recall_eval(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_eval = getattr(runtime, "run_production_recall_eval", None)
    if not callable(run_eval):
        return {
            "ok": False,
            "configured": True,
            "eval_skipped_reason": "run_production_recall_eval_unavailable",
        }
    try:
        dataset, configured, dataset_source, skipped_reason = _production_recall_dataset(runtime, scope=scope)
        if not _dataset_cases(dataset):
            return {
                "ok": True,
                "configured": configured,
                "seeded": False,
                "eval_skipped_reason": skipped_reason,
            }
        try:
            report = _json_safe(run_eval(dataset, seed=False, scope=scope, persist_report=True))
        except TypeError as exc:
            if "persist_report" not in str(exc):
                raise
            report = _json_safe(run_eval(dataset, seed=False, scope=scope))
        if isinstance(report, dict):
            if "quality_gate" not in report:
                report["quality_gate"] = evaluate_production_recall_quality_gate(report)
                report["passed_threshold"] = bool(report["quality_gate"].get("ok"))
                report["gate_ok"] = bool(report["quality_gate"].get("ok"))
                report["blocked_reason"] = "" if report["gate_ok"] else str(report["quality_gate"].get("blocked_reason") or "")
            return {**report, "configured": True, "seeded": False, "dataset_source": dataset_source}
        return {
            "ok": False,
            "configured": True,
            "eval_skipped_reason": "",
            "error": "invalid_production_recall_report",
        }
    except Exception as exc:
        return {
            "ok": False,
            "configured": True,
            "eval_skipped_reason": "",
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def _run_judgment_evaluation(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    evaluate = getattr(runtime, "run_judgment_evaluation", None)
    if not callable(evaluate):
        return {
            "ok": False,
            "scanned_event_count": 0,
            "playbook_entry_count": 0,
            "persisted": False,
            "persisted_record_id": "",
            "evaluation_skipped_reason": "run_judgment_evaluation_unavailable",
        }
    try:
        report = _json_safe(evaluate(scope=scope, limit=300, persist_playbook=True))
        return {
            "ok": bool(report.get("ok", True)),
            "scanned_event_count": int(report.get("scanned_event_count") or 0),
            "outcome_counts": dict(report.get("outcome_counts") or {}),
            "repeated_failure_count": len(report.get("repeated_failures") or []),
            "user_correction_count": len(report.get("user_corrections") or []),
            "reliable_path_count": len(report.get("reliable_paths") or []),
            "noise_signal_count": len(report.get("noise_signals") or []),
            "temporary_fix_count": len(report.get("temporary_fixes") or []),
            "playbook_entry_count": len(report.get("playbook_entries") or []),
            "playbook_entries": list(report.get("playbook_entries") or []),
            "persisted": bool(report.get("persisted")),
            "persisted_record_id": str(report.get("persisted_record_id") or ""),
            "persisted_policy_ids": list(report.get("persisted_policy_ids") or []),
            "evaluation_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "scanned_event_count": 0,
            "playbook_entry_count": 0,
            "persisted": False,
            "persisted_record_id": "",
            "error": type(exc).__name__,
            "detail": str(exc),
            "evaluation_skipped_reason": "",
        }


def _run_memory_eval_ci(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_eval = getattr(runtime, "run_memory_eval_ci", None)
    if not callable(run_eval):
        return {
            "ok": False,
            "pass_rate": 0.0,
            "passed_threshold": False,
            "eval_skipped_reason": "run_memory_eval_ci_unavailable",
        }
    try:
        dataset, configured, dataset_source = _memory_eval_ci_dataset(runtime, scope=scope)
        if not _dataset_cases(dataset):
            return {
                "ok": True,
                "configured": configured,
                "persisted": False,
                "eval_skipped_reason": "memory_eval_dataset_empty",
            }
        report = _json_safe(run_eval(dataset, emit_incidents=True))
        if isinstance(report, dict):
            record = _memory_eval_report_record(report, scope=ScopeRef.from_dict(scope))
            runtime.store.append(record)
            return {
                **report,
                "configured": True,
                "dataset_source": dataset_source,
                "persisted": True,
                "persisted_record_id": record.record_id,
            }
        return report
    except Exception as exc:
        return {
            "ok": False,
            "pass_rate": 0.0,
            "passed_threshold": False,
            "eval_skipped_reason": "",
            "error": type(exc).__name__,
            "detail": str(exc),
        }


def _memory_eval_ci_dataset(runtime: Runtime, *, scope: dict) -> tuple[dict[str, Any] | list[Any], bool, str]:
    dataset_path = str(os.environ.get("EIMEMORY_MEMORY_EVAL_DATASET") or "").strip()
    if dataset_path:
        return _load_json_dataset(dataset_path), True, "env"

    build_replay_dataset = getattr(runtime, "build_replay_dataset", None)
    if callable(build_replay_dataset):
        replay_report = build_replay_dataset(scope=scope, persist=False)
        if isinstance(replay_report, dict):
            cases = [dict(case) for case in _dataset_cases(replay_report) if isinstance(case, dict)]
            if cases:
                return (
                    {
                        "name": "nightly-memory-ci-smoke",
                        "scope": scope,
                        "threshold": 0.0,
                        "seed": [],
                        "cases": cases,
                    },
                    True,
                    "replay_dataset",
                )

    return {
        "name": "nightly-memory-ci-smoke",
        "scope": scope,
        "threshold": 0.0,
        "seed": [],
        "cases": [],
    }, False, "none"


def _production_recall_dataset(
    runtime: Runtime,
    *,
    scope: dict,
) -> tuple[dict[str, Any] | list[Any], bool, str, str]:
    dataset_path = str(os.environ.get("EIMEMORY_PRODUCTION_RECALL_DATASET") or "").strip()
    if dataset_path:
        return _load_json_dataset(dataset_path), True, "env", "production_recall_dataset_empty"

    conventional_path = _production_recall_conventional_path(runtime)
    if conventional_path is not None:
        return _load_json_dataset(str(conventional_path)), True, "conventional_path", "production_recall_dataset_empty"

    build_dataset = getattr(runtime, "build_production_recall_dataset", None)
    if callable(build_dataset):
        dataset = build_dataset(scope=scope, persist=False)
        return dataset, bool(_dataset_cases(dataset)), "runtime_generated", "production_recall_dataset_empty"

    generated = _production_recall_smoke_dataset(runtime, scope=scope)
    if _dataset_cases(generated):
        return generated, True, "generated_records", "production_recall_dataset_empty"

    return {
        "name": "nightly-production-recall-smoke",
        "scope": scope,
        "cases": [],
    }, False, "none", "production_recall_dataset_unconfigured"


def _production_recall_conventional_path(runtime: Runtime) -> Path | None:
    root = getattr(getattr(runtime, "store", None), "root", None)
    if root is None:
        return None
    for relative in (
        Path("evaluation") / "production_recall.json",
        Path("eval") / "production_recall.json",
        Path("production_recall.json"),
    ):
        candidate = Path(root) / relative
        if candidate.is_file():
            return candidate
    return None


def _production_recall_smoke_dataset(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    store = getattr(runtime, "store", None)
    list_records = getattr(store, "list_records", None)
    if not callable(list_records):
        return {"name": "nightly-production-recall-smoke", "scope": scope, "cases": []}
    records = list_records(
        kinds=["memory", "multimodal_memory", "knowledge_page", "claim_card"],
        scope=scope,
        status="active",
        limit=5,
    )
    cases = []
    for record in records:
        query = _first_text(record.title, record.summary, _record_content_text(record))
        if not query:
            continue
        expected_text = _first_text(record.summary, _record_content_text(record), record.title)
        cases.append(
            {
                "case_id": f"generated-{record.record_id}",
                "query": query[:160],
                "expected_record_ids": [record.record_id],
                "expected_titles": [record.title],
                "expected_text": [expected_text] if expected_text else [],
                "topk": 5,
                "scope": scope,
            }
        )
    return {
        "name": "nightly-production-recall-smoke",
        "scope": scope,
        "cases": cases,
    }


def _record_content_text(record: RecordEnvelope) -> str:
    content = record.content if isinstance(record.content, dict) else {}
    return _first_text(content.get("text"), content.get("summary"), content.get("detail"))


def _load_json_dataset(path: str) -> dict[str, Any] | list[Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        dataset = json.load(handle)
    if isinstance(dataset, (dict, list)):
        return dataset
    raise ValueError("dataset must be a JSON object or list")


def _dataset_cases(dataset: Any) -> list[Any]:
    if isinstance(dataset, list):
        return list(dataset)
    if isinstance(dataset, dict):
        raw_cases = dataset.get("cases") or dataset.get("samples") or []
        return list(raw_cases) if isinstance(raw_cases, list) else []
    return []


def _memory_eval_report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    name = str(report.get("name") or "memory_eval_ci")
    pass_rate = float(report.get("pass_rate") or 0.0)
    fail_count = int(report.get("fail_count") or 0)
    summary = f"Memory eval CI {name}: pass_rate={pass_rate:.3f}, failures={fail_count}."
    return RecordEnvelope.create(
        kind="reflection",
        title=f"Memory eval CI: {name}",
        summary=summary,
        detail=summary,
        content={"report": _json_safe(report)},
        tags=["memory-eval-ci", "nightly"],
        source="eimemory.memory_eval_ci",
        scope=scope,
        meta={
            "report_type": "memory_eval_ci",
            "name": name,
            "pass_rate": pass_rate,
            "passed_threshold": bool(report.get("passed_threshold")),
            "fail_count": fail_count,
            "incident_count": len(report.get("incident_record_ids") or []),
        },
    )


def _run_external_collection(
    runtime: Runtime,
    *,
    scope: dict,
    limit: int,
    fetch_text: Callable[[str], str] | None,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    remaining = max(0, int(limit))
    errors: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    runtime_persisted_counts = {
        "candidate_count": 0,
        "rejected_count": 0,
        "quarantined_count": 0,
        "written_count": 0,
        "skipped_existing_count": 0,
    }
    source_count = 0
    fetched_item_count = 0

    for source_kind in ("news", "rss", "url", "paper"):
        if remaining <= 0:
            break
        report = _collect_external_source_kind(
            runtime,
            source_kind=source_kind,
            limit=remaining,
            fetch_text=fetch_text,
            scope=scope,
        )
        reports.append(report)
        source_count += int(report.get("source_count") or 0)
        remaining -= int(report.get("source_count") or 0)
        fetched_item_count += int(report.get("item_count") or 0)
        errors.extend(_collection_errors(report))
        if report.pop("_runtime_persisted", False):
            for key in runtime_persisted_counts:
                runtime_persisted_counts[key] += int(report.get(key) or 0)
        else:
            all_candidates.extend(_candidates_from_collection_report(report))

    persist_report = _persist_external_candidates(runtime, scope=scope, candidates=all_candidates, limit=limit)
    errors.extend(persist_report["errors"])
    error_count = len(errors)
    return {
        "ok": error_count == 0,
        "source_count": source_count,
        "fetched_item_count": fetched_item_count,
        "candidate_count": runtime_persisted_counts["candidate_count"] + persist_report["candidate_count"],
        "rejected_count": runtime_persisted_counts["rejected_count"] + persist_report["rejected_count"],
        "quarantined_count": runtime_persisted_counts["quarantined_count"] + persist_report["quarantined_count"],
        "written_count": runtime_persisted_counts["written_count"] + persist_report["written_count"],
        "skipped_existing_count": runtime_persisted_counts["skipped_existing_count"]
        + persist_report["skipped_existing_count"],
        "error_count": error_count,
        "errors": errors,
        "source_reports": reports,
        "_candidate_records": persist_report["candidate_records"],
    }


def _promote_news_rss_source_candidates(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    records = runtime.store.list_records(kinds=["source_candidate"], scope=scope, status="candidate", limit=500)
    promoted_source_ids: list[str] = []
    skipped_count = 0
    error_count = 0
    errors: list[dict[str, Any]] = []
    existing_uris = {
        str(source.uri or "").strip()
        for source in runtime.sources.list_sources(enabled=None)
        if str(source.uri or "").strip()
    }
    for record in records:
        proposal = record.content.get("proposal") if isinstance(record.content.get("proposal"), dict) else {}
        source_kind = str(proposal.get("source_kind") or record.meta.get("source_kind") or "").strip().lower()
        source_family = str((proposal.get("metadata") or {}).get("source_family") or record.meta.get("source_family") or "")
        uri = str(proposal.get("uri") or record.meta.get("source_uri") or "").strip()
        tags = {str(tag).lower() for tag in (proposal.get("tags") or record.tags or [])}
        is_news_rss = source_kind == "rss" and ("news" in tags or source_family == "news_rss")
        if not is_news_rss or not uri:
            skipped_count += 1
            continue
        if uri in existing_uris:
            skipped_count += 1
            continue
        try:
            source = runtime.sources.add_source(
                {
                    "source_kind": "rss",
                    "title": str(proposal.get("title") or record.title or "News RSS"),
                    "uri": uri,
                    "tags": sorted({"news", "rss", "auto-promoted", *tags}),
                    "enabled": True,
                    "metadata": {
                        "frequency": "daily",
                        "max_items": int((proposal.get("metadata") or {}).get("max_items") or 10),
                        "source_family": "news_rss",
                        "promoted_from_record_id": record.record_id,
                    },
                }
            )
            existing_uris.add(uri)
            promoted_source_ids.append(source.source_id)
        except Exception as exc:
            error_count += 1
            errors.append({"record_id": record.record_id, "error": type(exc).__name__, "detail": str(exc)})
    return {
        "ok": error_count == 0,
        "scanned_count": len(records),
        "promoted_count": len(promoted_source_ids),
        "skipped_count": skipped_count,
        "error_count": error_count,
        "errors": errors,
        "promoted_source_ids": promoted_source_ids,
    }


def _collect_external_source_kind(
    runtime: Runtime,
    *,
    source_kind: str,
    limit: int,
    fetch_text: Callable[[str], str] | None,
    scope: dict,
) -> dict[str, Any]:
    collect = getattr(runtime, "collect_external_sources", None)
    if collect is None:
        return {
            "ok": False,
            "source_kind": source_kind,
            "source_count": 0,
            "item_count": 0,
            "results": [],
            "error": "collect_external_sources_unavailable",
        }
    kwargs: dict[str, Any] = {
        "source_kind": source_kind,
        "limit": limit,
        "fetch": True,
    }
    if fetch_text is not None:
        kwargs["fetch_text"] = fetch_text

    try:
        report = _json_safe(collect(**{**kwargs, "scope": scope, "persist": True}))
        if isinstance(report, dict):
            report["_runtime_persisted"] = True
        return report
    except TypeError as exc:
        if "scope" not in str(exc) and "persist" not in str(exc) and "unexpected keyword" not in str(exc):
            return _collection_exception_report(source_kind, exc)
    except Exception as exc:
        return _collection_exception_report(source_kind, exc)

    try:
        return _json_safe(collect(**kwargs))
    except Exception as exc:
        return _collection_exception_report(source_kind, exc)


def _persist_external_candidates(
    runtime: Runtime,
    *,
    scope: dict,
    candidates: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    candidate_count = 0
    rejected_count = 0
    quarantined_count = 0
    for candidate in candidates:
        decision = str(candidate.get("decision") or "").strip().lower()
        if decision == "candidate":
            fingerprint = str(candidate.get("fingerprint") or "")
            if fingerprint in seen_fingerprints:
                rejected_count += 1
                continue
            seen_fingerprints.add(fingerprint)
            candidate_count += 1
            if len(accepted) < limit:
                accepted.append(candidate)
        elif decision == "quarantined":
            quarantined_count += 1
        else:
            rejected_count += 1

    written_count = 0
    skipped_existing_count = 0
    errors: list[dict[str, Any]] = []
    candidate_records = []
    for record in candidates_to_records(accepted, scope):
        try:
            existing = runtime.store.get_by_id(record.record_id, scope=record.scope)
            if existing is not None and existing.status != "candidate":
                skipped_existing_count += 1
                continue
            runtime.store.append(record)
            written_count += 1
            candidate_records.append(record)
        except Exception as exc:
            errors.append({"record_id": record.record_id, "error": type(exc).__name__, "detail": str(exc)})

    return {
        "candidate_count": candidate_count,
        "rejected_count": rejected_count,
        "quarantined_count": quarantined_count,
        "written_count": written_count,
        "skipped_existing_count": skipped_existing_count,
        "errors": errors,
        "candidate_records": candidate_records,
    }


def _run_paper_candidate_promotion(
    runtime: Runtime,
    *,
    scope: dict,
    candidate_records: list[Any],
) -> dict[str, Any]:
    promote_collected = getattr(runtime, "promote_collected_paper_candidates", None)
    if promote_collected is not None:
        try:
            report = _json_safe(promote_collected(scope=scope, limit=100, auto=True))
            return {
                "ok": bool(report.get("ok", True)),
                "attempted_count": int(report.get("scanned") or 0),
                "promoted_count": int(report.get("promoted") or 0),
                "skipped_count": int(report.get("skipped") or 0),
                "error_count": 0,
                "errors": [],
                "reports": report.get("promoted_reports") or [],
                "reasons": dict(report.get("reasons") or {}),
                "promotion_skipped_reason": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "attempted_count": 0,
                "promoted_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"error": type(exc).__name__, "detail": str(exc)}],
                "reports": [],
                "reasons": {},
                "promotion_skipped_reason": "",
            }

    promote = getattr(runtime, "promote_paper_candidate", None)
    if promote is None:
        return _paper_promotion_skipped("promote_paper_candidate_unavailable")

    paper_candidates = [
        record
        for record in candidate_records
        if str(record.meta.get("source_kind") or record.content.get("source_kind") or "").strip().lower() in {"paper", "url"}
    ]
    if not paper_candidates:
        return _paper_promotion_skipped("no_paper_candidates")

    reports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    promoted_count = 0
    skipped_count = 0
    for record in paper_candidates:
        try:
            report = _json_safe(promote(record, scope=scope))
        except Exception as exc:
            errors.append({"record_id": record.record_id, "error": type(exc).__name__, "detail": str(exc)})
            continue
        reports.append(report)
        if report.get("ok"):
            promoted_count += 1
        else:
            skipped_count += 1

    return {
        "ok": not errors,
        "attempted_count": len(paper_candidates),
        "promoted_count": promoted_count,
        "skipped_count": skipped_count,
        "error_count": len(errors),
        "errors": errors,
        "reports": reports,
        "promotion_skipped_reason": "",
    }


def _paper_promotion_skipped(reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "attempted_count": 0,
        "promoted_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "errors": [],
        "reports": [],
        "reasons": {},
        "promotion_skipped_reason": reason,
    }


def _run_operational_projection(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    project = getattr(runtime, "project_operational_knowledge", None)
    if project is None:
        return {
            "ok": True,
            "projected_count": 0,
            "skipped_count": 0,
            "projection_skipped_reason": "project_operational_knowledge_unavailable",
        }
    try:
        report = _json_safe(project(scope=scope, limit=100))
        return {
            "ok": bool(report.get("ok", True)),
            "scanned_count": int(report.get("scanned_count") or 0),
            "projected_count": int(report.get("projected_count") or 0),
            "skipped_count": int(report.get("skipped_count") or 0),
            "projected_ids": list(report.get("projected_ids") or []),
            "skipped": list(report.get("skipped") or []),
            "projection_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "scanned_count": 0,
            "projected_count": 0,
            "skipped_count": 0,
            "projected_ids": [],
            "skipped": [],
            "error": type(exc).__name__,
            "detail": str(exc),
            "projection_skipped_reason": "",
        }


def _run_source_discovery(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    discover = getattr(runtime, "discover_sources", None)
    if discover is None:
        return {
            "ok": True,
            "proposal_count": 0,
            "approve_count": 0,
            "needs_review_count": 0,
            "persisted_count": 0,
            "skipped_existing_count": 0,
            "discovery_skipped_reason": "discover_sources_unavailable",
        }
    try:
        report = _json_safe(discover(scope=scope, persist=True))
        return {
            "ok": bool(report.get("ok", True)),
            "proposal_count": int(report.get("proposal_count") or 0),
            "approve_count": int(report.get("approve_count") or 0),
            "needs_review_count": int(report.get("needs_review_count") or 0),
            "persisted_count": len(report.get("persisted_record_ids") or []),
            "persisted_record_ids": list(report.get("persisted_record_ids") or []),
            "skipped_existing_count": int(report.get("skipped_existing_count") or 0),
            "discovery_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "proposal_count": 0,
            "approve_count": 0,
            "needs_review_count": 0,
            "persisted_count": 0,
            "persisted_record_ids": [],
            "skipped_existing_count": 0,
            "error": type(exc).__name__,
            "detail": str(exc),
            "discovery_skipped_reason": "",
        }


def _run_rule_evolution(
    runtime: Runtime,
    *,
    scope: dict,
    replay_datasets: dict[str, list[dict]],
) -> dict[str, Any]:
    evolve = getattr(runtime, "run_rule_evolution", None)
    if evolve is None:
        return {
            "ok": True,
            "candidate_count": 0,
            "promoted_count": 0,
            "replay_count": 0,
            "created_rule_count": 0,
            "persisted": False,
            "persisted_record_id": "",
            "evolution_skipped_reason": "run_rule_evolution_unavailable",
        }
    try:
        report = _json_safe(
            evolve(
                scope=scope,
                apply=True,
                min_roi=0.0,
                replay_datasets=replay_datasets,
                persist_report=True,
            )
        )
        record_ids = report.get("record_ids") if isinstance(report.get("record_ids"), dict) else {}
        return {
            "ok": bool(report.get("ok", True)),
            "candidate_count": int(report.get("candidate_count") or 0),
            "promoted_count": int(report.get("promoted_count") or 0),
            "replay_count": int(report.get("replay_count") or 0),
            "created_rule_count": len(record_ids.get("created_rules") or []),
            "promotion_candidate_count": len(record_ids.get("promotion_candidates") or []),
            "replayed_rule_ids": list(report.get("replayed_rule_ids") or []),
            "persisted": bool(report.get("persisted")),
            "persisted_record_id": str(report.get("persisted_record_id") or ""),
            "evolution_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "candidate_count": 0,
            "promoted_count": 0,
            "replay_count": 0,
            "created_rule_count": 0,
            "promotion_candidate_count": 0,
            "replayed_rule_ids": [],
            "persisted": False,
            "persisted_record_id": "",
            "error": type(exc).__name__,
            "detail": str(exc),
            "evolution_skipped_reason": "",
        }


def _run_autonomous_evolution(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_autonomous = getattr(runtime, "run_autonomous_evolution", None)
    if run_autonomous is None:
        return {
            "ok": False,
            "report_type": "autonomous_evolution",
            "autonomous_evolution_skipped_reason": "run_autonomous_evolution_unavailable",
        }
    try:
        report = _json_safe(
            run_autonomous(
                scope=scope,
                apply=False,
                max_apply=3,
                web_hypotheses=None,
                persist_report=True,
            )
        )
        if isinstance(report, dict):
            return report
    except Exception as exc:
        return {
            "ok": False,
            "report_type": "autonomous_evolution",
            "autonomous_evolution_skipped_reason": "run_autonomous_evolution_failed",
            "error": type(exc).__name__,
            "detail": str(exc),
        }
    return {
        "ok": False,
        "report_type": "autonomous_evolution",
        "autonomous_evolution_skipped_reason": "invalid_autonomous_evolution_report",
    }


def _run_autonomous_learning(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_learning = getattr(runtime, "run_autonomous_learning_cycle", None)
    if run_learning is None:
        return {
            "ok": False,
            "report_type": "autonomous_learning",
            "configured": False,
            "enabled": False,
            "learning_skipped_reason": "run_autonomous_learning_cycle_unavailable",
        }
    enabled = _env_bool("EIMEMORY_AUTONOMOUS_LEARNING_ENABLED", default=False)
    required = _env_bool("EIMEMORY_REQUIRE_AUTONOMOUS_LEARNING", default=False)
    if not enabled:
        if required:
            return {
                "ok": False,
                "report_type": "autonomous_learning",
                "configured": True,
                "enabled": False,
                "required": True,
                "requires_enable_env": "EIMEMORY_AUTONOMOUS_LEARNING_ENABLED=1",
                "apply_env": "EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1",
                "learning_skipped_reason": "autonomous_learning_required_but_disabled",
            }
        return {
            "ok": True,
            "report_type": "autonomous_learning",
            "configured": False,
            "enabled": False,
            "required": False,
            "requires_enable_env": "EIMEMORY_AUTONOMOUS_LEARNING_ENABLED=1",
            "apply_env": "EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1",
            "dry_run": True,
            "apply": False,
            "goal_count": 0,
            "candidate_count": 0,
            "applied_count": 0,
            "learning_skipped_reason": "autonomous_learning_disabled",
        }
    apply_changes = _env_bool("EIMEMORY_AUTONOMOUS_LEARNING_APPLY", default=False)
    dry_run = _env_bool("EIMEMORY_AUTONOMOUS_LEARNING_DRY_RUN", default=not apply_changes)
    force = _env_bool("EIMEMORY_AUTONOMOUS_LEARNING_FORCE", default=False)
    max_goals = _env_int("EIMEMORY_AUTONOMOUS_LEARNING_MAX_GOALS", default=3, minimum=1, maximum=20)
    max_promotions = _env_int("EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS", default=3, minimum=0, maximum=20)
    timeout_seconds = _env_int("EIMEMORY_AUTONOMOUS_LEARNING_TIMEOUT_SECONDS", default=900, minimum=30, maximum=7200)
    try:
        started = time.monotonic()
        report = _json_safe(
            run_learning(
                scope=scope,
                apply=apply_changes,
                dry_run=dry_run,
                full=True,
                force=force,
                max_goals=max_goals,
                max_promotions=max_promotions,
            )
        )
        elapsed_seconds = round(time.monotonic() - started, 3)
        if isinstance(report, dict):
            status = {
                "ok": bool(report.get("ok", False)),
                "report_type": "autonomous_learning",
                "configured": True,
                "enabled": True,
                "required": bool(required),
                "requires_enable_env": "EIMEMORY_AUTONOMOUS_LEARNING_ENABLED=1",
                "apply_env": "EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1",
                "dry_run": bool(report.get("dry_run", dry_run)),
                "apply": bool(report.get("apply", apply_changes)),
                "force": bool(force),
                "max_goals": max_goals,
                "max_promotions": max_promotions,
                "timeout_seconds": timeout_seconds,
                "elapsed_seconds": elapsed_seconds,
                "timeout_exceeded": elapsed_seconds > timeout_seconds,
                "loop_id": str(report.get("loop_id") or ""),
                "goal_count": int(report.get("goal_count") or 0),
                "thought_count": int(report.get("thought_count") or 0),
                "candidate_count": len(report.get("candidate_ids") or ([] if not report.get("candidate_id") else [report.get("candidate_id")])),
                "applied_count": sum(1 for item in (report.get("promotions") or []) if item.get("applied")) or (1 if (report.get("promotion") or {}).get("applied") else 0),
                "replay_case_count": int((report.get("replay_dataset") or {}).get("case_count") or 0),
                "eval_verdict": str(report.get("eval_verdict") or ""),
                "capability_score_id": str(report.get("capability_score_id") or ""),
                "regressed": bool((report.get("regression_watch") or {}).get("regressed")),
                "retention_disabled_count": int((report.get("retention") or {}).get("disabled_count") or 0),
                "learning_skipped_reason": "",
            }
            return _with_query_first_evidence(status, report)
    except Exception as exc:
        return {
            "ok": False,
            "report_type": "autonomous_learning",
            "configured": True,
            "enabled": True,
            "learning_skipped_reason": "run_autonomous_learning_cycle_failed",
            "error": type(exc).__name__,
            "detail": str(exc),
        }
    return {
        "ok": False,
        "report_type": "autonomous_learning",
        "configured": True,
        "enabled": True,
        "learning_skipped_reason": "invalid_autonomous_learning_report",
    }


def _run_autonomous_learning_daily_report(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    build_report = getattr(runtime, "build_learning_daily_report", None)
    if not callable(build_report):
        return {
            "ok": False,
            "report_type": "autonomous_learning_daily_report",
            "learning_report_skipped_reason": "build_learning_daily_report_unavailable",
        }
    try:
        report = _json_safe(build_report(scope=scope, persist=True))
        if isinstance(report, dict):
            status = {
                "ok": bool(report.get("ok", False)),
                "report_type": "autonomous_learning_daily_report",
                "date": str(report.get("date") or ""),
                "persisted": bool(report.get("persisted")),
                "persisted_record_id": str(report.get("persisted_record_id") or ""),
                "learned_count": len(report.get("learned") or []),
                "applied_count": len(report.get("applied") or []),
                "blocked_count": len(report.get("blocked") or []),
                "next_validation_count": len(report.get("next_validation") or []),
                "summary": str(report.get("summary") or ""),
                "learning_report_skipped_reason": "",
            }
            return _with_query_first_evidence(status, report)
    except Exception as exc:
        return {
            "ok": False,
            "report_type": "autonomous_learning_daily_report",
            "learning_report_skipped_reason": "build_learning_daily_report_failed",
            "error": type(exc).__name__,
            "detail": str(exc),
        }
    return {
        "ok": False,
        "report_type": "autonomous_learning_daily_report",
        "learning_report_skipped_reason": "invalid_learning_daily_report",
    }


def _run_autonomous_learning_dashboard(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    build_dashboard = getattr(runtime, "build_learning_dashboard", None)
    if not callable(build_dashboard):
        return {
            "ok": False,
            "report_type": "autonomous_learning_dashboard",
            "dashboard_skipped_reason": "build_learning_dashboard_unavailable",
        }
    enabled = _env_bool("EIMEMORY_AUTONOMOUS_LEARNING_DASHBOARD_ENABLED", default=True)
    if not enabled:
        return {
            "ok": True,
            "report_type": "autonomous_learning_dashboard",
            "enabled": False,
            "dashboard_skipped_reason": "dashboard_disabled",
        }
    try:
        report = _json_safe(build_dashboard(scope=scope, persist=True))
        if isinstance(report, dict):
            status = {
                "ok": bool(report.get("ok", False)),
                "report_type": str(report.get("report_type") or "autonomous_learning_dashboard"),
                "enabled": True,
                "period_type": str(report.get("period_type") or ""),
                "period_start": str(report.get("period_start") or report.get("week_start") or ""),
                "week_start": str(report.get("week_start") or ""),
                "persisted": bool(report.get("persisted")),
                "persisted_record_id": str(report.get("persisted_record_id") or ""),
                "capability_count": len((report.get("ledger") or {}).get("capabilities") or {}),
                "dashboard_skipped_reason": "",
            }
            return _with_query_first_evidence(status, report)
    except Exception as exc:
        return {
            "ok": False,
            "report_type": "autonomous_learning_dashboard",
            "enabled": True,
            "dashboard_skipped_reason": "build_learning_dashboard_failed",
            "error": type(exc).__name__,
            "detail": str(exc),
        }
    return {
        "ok": False,
        "report_type": "autonomous_learning_dashboard",
        "enabled": True,
        "dashboard_skipped_reason": "invalid_learning_dashboard_report",
    }


def _with_query_first_evidence(status: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    if "query_first_evidence" in report:
        status["query_first_evidence"] = _json_safe(report.get("query_first_evidence"))
    return status


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except ValueError:
        value = int(default)
    return max(minimum, min(maximum, value))

def _delivery_is_pending(delivery: dict[str, Any]) -> bool:
    status = str(((delivery.get("outbox") or {}).get("status") or delivery.get("status") or "")).strip().lower()
    return status in {"pending", "pending_delivery", "queued"}


def _run_daily_brief(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    build_brief = getattr(runtime, "build_daily_brief", None)
    if build_brief is None:
        return {
            "ok": True,
            "date": "",
            "message_count": 0,
            "decision_count": 0,
            "followup_count": 0,
            "research_item_count": 0,
            "news_item_count": 0,
            "persisted": False,
            "persisted_record_id": "",
            "brief_skipped_reason": "build_daily_brief_unavailable",
        }
    try:
        report = _json_safe(build_brief(scope=scope, persist=True, channel="feishu"))
        conversation_summary = report.get("conversation_summary") if isinstance(report.get("conversation_summary"), dict) else {}
        research_digest = report.get("research_digest") if isinstance(report.get("research_digest"), dict) else {}
        news_digest = report.get("news_digest") if isinstance(report.get("news_digest"), dict) else {}
        return {
            "ok": bool(report.get("ok", True)),
            "date": str(report.get("date") or ""),
            "message_count": int(conversation_summary.get("message_count") or 0),
            "decision_count": len(report.get("decisions") or []),
            "followup_count": len(report.get("followups") or []),
            "research_item_count": len(research_digest.get("items") or []),
            "news_item_count": len(news_digest.get("items") or []),
            "delivery_channel": str((report.get("delivery") or {}).get("channel") or ""),
            "delivery_pending": _delivery_is_pending(report.get("delivery") or {}),
            "persisted": bool(report.get("persisted")),
            "persisted_record_id": str(report.get("persisted_record_id") or ""),
            "brief_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "date": "",
            "message_count": 0,
            "decision_count": 0,
            "followup_count": 0,
            "research_item_count": 0,
            "news_item_count": 0,
            "delivery_channel": "",
            "delivery_pending": False,
            "persisted": False,
            "persisted_record_id": "",
            "error": type(exc).__name__,
            "detail": str(exc),
            "brief_skipped_reason": "",
        }


def _run_research_digest(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    build_digest = getattr(runtime, "build_research_digest", None)
    if build_digest is None:
        return {
            "ok": True,
            "paper_count": 0,
            "claim_count": 0,
            "knowledge_page_count": 0,
            "candidate_count": 0,
            "summary": "",
            "persisted": False,
            "persisted_page_id": "",
            "digest_skipped_reason": "build_research_digest_unavailable",
        }
    try:
        report = _json_safe(build_digest(scope=scope, persist=True, limit=5))
        return {
            "ok": bool(report.get("ok", True)),
            "digest_date": str(report.get("digest_date") or ""),
            "paper_count": int(report.get("paper_count") or 0),
            "claim_count": int(report.get("claim_count") or 0),
            "knowledge_page_count": int(report.get("knowledge_page_count") or 0),
            "candidate_count": int(report.get("candidate_count") or 0),
            "summary": str(report.get("summary") or ""),
            "themes": list(report.get("themes") or []),
            "notable_claim_count": len(report.get("notable_claims") or []),
            "open_question_count": len(report.get("open_questions") or []),
            "skipped_low_confidence": dict(report.get("skipped_low_confidence") or {}),
            "persisted": bool(report.get("persisted")),
            "persisted_page_id": str(report.get("persisted_page_id") or ""),
            "digest_skipped_reason": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "paper_count": 0,
            "claim_count": 0,
            "knowledge_page_count": 0,
            "candidate_count": 0,
            "summary": "",
            "persisted": False,
            "persisted_page_id": "",
            "error": type(exc).__name__,
            "detail": str(exc),
            "digest_skipped_reason": "",
        }


def _candidates_from_collection_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        for item in result.get("items") or []:
            if isinstance(item, dict):
                candidates.append(_candidate_from_collected_item(result, item))
    return candidates


def _candidate_from_collected_item(result: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {})
    source_id = str(result.get("source_id") or "")
    source_kind = str(result.get("source_kind") or item.get("source_kind") or "").strip().lower()
    item_kind = str(item.get("source_kind") or source_kind).strip().lower()
    title = str(item.get("title") or source_id or "External knowledge item")
    content = str(item.get("content") or "")
    url = str(item.get("url") or "")
    safety = metadata.get("safety") if isinstance(metadata.get("safety"), dict) else {}
    has_identity = bool(title.strip() or url.strip())
    has_content = len("".join(char for char in content if char.isalnum())) >= 32
    if safety:
        decision = "quarantined"
        reason = "safety_redacted"
    elif not has_identity:
        decision = "rejected"
        reason = "missing_identity"
    elif not has_content:
        decision = "rejected"
        reason = "content_too_short"
    else:
        decision = "candidate"
        reason = "external_fetch"
    fingerprint = str(item.get("fingerprint") or "")
    return {
        "source_id": source_id,
        "source_kind": source_kind,
        "title": title,
        "uri": url,
        "summary": content[:240],
        "content_excerpt": content[:1200],
        "decision": decision,
        "reason": reason,
        "fingerprint": fingerprint,
        "provenance": {
            "source_id": source_id,
            "source_kind": source_kind,
            "source_uri": url,
            "published_at": str(item.get("published_at") or ""),
            "scan_kind": "external_collection",
            "collector_source_kind": item_kind,
        },
        "quality": {
            "score": 0.8 if decision == "candidate" else 0.0,
            "content_length": len("".join(char for char in content if char.isalnum())),
            "has_excerpt": bool(content),
            "source_enabled": True,
            "decision": decision,
            "reason": reason,
        },
        "metadata": metadata,
    }


def _collection_errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if report.get("error"):
        errors.append(
            {
                "source_kind": str(report.get("source_kind") or ""),
                "error": str(report.get("error") or ""),
            }
        )
    for result in report.get("results") or []:
        if not isinstance(result, dict) or result.get("ok", True):
            continue
        errors.append(
            {
                "source_id": str(result.get("source_id") or ""),
                "source_kind": str(result.get("source_kind") or ""),
                "error": str(result.get("error") or "collection_failed"),
                "metadata": dict(result.get("metadata") or {}),
            }
        )
    return errors


def _collection_exception_report(source_kind: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "source_kind": source_kind,
        "source_count": 0,
        "item_count": 0,
        "results": [],
        "error": type(exc).__name__,
        "detail": str(exc),
    }


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
