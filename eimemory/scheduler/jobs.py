from __future__ import annotations

from eimemory.api.runtime import Runtime


def run_nightly_jobs(
    runtime: Runtime,
    *,
    scope: dict,
    replay_datasets: dict[str, list[dict]] | None = None,
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
    intake_report = runtime.run_knowledge_intake(scope=scope, persist=True, limit=100)
    source_quality_report = runtime.source_quality_report(scope=scope)
    collection_policy = runtime.collection_policy(scope=scope)
    replay_datasets = replay_datasets or {}
    replay_reports = []
    for rule in active_rules:
        dataset = replay_datasets.get(rule.record_id)
        if dataset:
            replay_reports.append(runtime.evolution.replay_rule(record_id=rule.record_id, dataset=dataset))
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
        "knowledge_intake": {
            "scanned_count": intake_report["scanned_count"],
            "candidate_count": intake_report["candidate_count"],
            "rejected_count": intake_report["rejected_count"],
            "quarantined_count": intake_report["quarantined_count"],
            "written_count": intake_report["written_count"],
            "skipped_existing_count": intake_report.get("skipped_existing_count", 0),
        },
        "source_quality": {
            "source_count": source_quality_report["source_count"],
            "run_now": collection_policy["run_now"],
            "pause": collection_policy["pause"],
            "lower_frequency": collection_policy["lower_frequency"],
            "gap_query_count": len(collection_policy["gap_queries"]),
        },
        "roi": roi,
    }
