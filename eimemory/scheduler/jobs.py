from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from eimemory.api.runtime import Runtime
from eimemory.intake.loop import candidates_to_records
from eimemory.models.records import RecordEnvelope, ScopeRef


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
        "memory_eval_ci": memory_eval_ci_report,
        "production_recall": production_recall_report,
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


def _run_production_recall_eval(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    dataset_path = str(os.environ.get("EIMEMORY_PRODUCTION_RECALL_DATASET") or "").strip()
    if not dataset_path:
        return {
            "ok": True,
            "configured": False,
            "eval_skipped_reason": "production_recall_dataset_unconfigured",
        }
    run_eval = getattr(runtime, "run_production_recall_eval", None)
    if not callable(run_eval):
        return {
            "ok": False,
            "configured": True,
            "eval_skipped_reason": "run_production_recall_eval_unavailable",
        }
    try:
        with Path(dataset_path).open("r", encoding="utf-8") as handle:
            dataset = json.load(handle)
        report = _json_safe(run_eval(dataset, seed=False, scope=scope))
        if isinstance(report, dict):
            return {**report, "configured": True, "seeded": False}
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


def _run_memory_eval_ci(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_eval = getattr(runtime, "run_memory_eval_ci", None)
    if not callable(run_eval):
        return {
            "ok": False,
            "pass_rate": 0.0,
            "passed_threshold": False,
            "eval_skipped_reason": "run_memory_eval_ci_unavailable",
        }
    dataset = {
        "name": "nightly-memory-ci-smoke",
        "scope": scope,
        "threshold": 0.0,
        "seed": [],
        "cases": [],
    }
    try:
        report = _json_safe(run_eval(dataset, emit_incidents=False))
        if isinstance(report, dict):
            record = _memory_eval_report_record(report, scope=ScopeRef.from_dict(scope))
            runtime.store.append(record)
            return {**report, "persisted": True, "persisted_record_id": record.record_id}
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
