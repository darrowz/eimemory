from __future__ import annotations

import json
import ipaddress
import socket
import time
import tracemalloc
from dataclasses import asdict, is_dataclass, replace
from datetime import date as date_type
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from eimemory.api.evolution import EvolutionAPI
from eimemory.api.memory import MemoryAPI
from eimemory.core.clock import now_iso
from eimemory.intake.registry import SourceRegistry
from eimemory.intake.papers.sources import ingest_paper_source
from eimemory.intake.title_normalization import strip_candidate_title_prefixes
from eimemory.knowledge.compiler import KnowledgeCompilation, compile_paper_knowledge
from eimemory.knowledge.extract import PaperMemoryExtraction, extract_paper_memory
from eimemory.knowledge.projectors import project_operational_knowledge
from eimemory.knowledge.synthesis import build_research_digest, digest_to_record
from eimemory.config.defaults import default_root
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.raw.store import RawEvidenceAPI
from eimemory.storage.runtime_store import RuntimeStore


MAX_FETCH_BYTES = 2_000_000
ALLOWED_FETCH_CONTENT_TYPES = (
    "application/atom+xml",
    "application/json",
    "application/rss+xml",
    "application/xml",
    "text/",
)


def _supervisor_count(value: Any, field_names: set[str]) -> int:
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key in field_names:
                total += _non_negative_int(item)
            elif isinstance(item, (dict, list, tuple)):
                total += _supervisor_count(item, field_names)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_supervisor_count(item, field_names) for item in value)
    return 0


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class Runtime:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self.memory = MemoryAPI(store)
        self.evolution = EvolutionAPI(store)
        self.raw = RawEvidenceAPI(store)
        self.sources = SourceRegistry(self.store.root / "state" / "source_registry.json")

    @classmethod
    def create(cls, *, root: str | Path | None = None) -> "Runtime":
        final_root = default_root(root)
        return cls(RuntimeStore(final_root))

    def close(self) -> None:
        self.store.close()

    def record_memory_usage(
        self,
        *,
        query_id: str,
        scope: dict,
        used_record_ids: list[str] | None = None,
        rejected_record_ids: list[str] | None = None,
        query: str = "",
        source: str = "openclaw.gateway",
        meta: dict | None = None,
        persist: bool = True,
    ) -> RecordEnvelope:
        return self.memory.record_memory_usage(
            query_id=query_id,
            scope=scope,
            used_record_ids=used_record_ids,
            rejected_record_ids=rejected_record_ids,
            query=query,
            source=source,
            meta=meta,
            persist=persist,
        )

    def knowledge_intake_loop(self):
        from eimemory.intake.loop import KnowledgeIntakeLoop

        return KnowledgeIntakeLoop(self.sources, self.store)

    def run_knowledge_intake(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        source_kind: str | None = None,
        limit: int | None = None,
    ) -> dict:
        return self.knowledge_intake_loop().run(
            scope,
            persist=persist,
            source_kind=source_kind,
            limit=limit,
        )

    def collect_external_sources(
        self,
        *,
        source_kind: str | None = None,
        limit: int | None = None,
        fetch_text=None,
        fetch: bool = False,
        persist: bool = False,
        scope: dict | None = None,
    ) -> dict:
        from dataclasses import asdict

        from eimemory.intake.connectors import FetchResult, collect_from_source_entry

        sources = self.sources.list_sources(enabled=True, source_kind=source_kind or None)
        if limit is not None:
            sources = sources[: max(0, int(limit))]
        if fetch and fetch_text is None:
            fetch_text = _default_fetch_text
        results = []
        scanned_at = now_iso()
        item_budget = max(0, int(limit)) if limit is not None else None
        item_count = 0
        written_count = 0
        skipped_existing_count = 0
        quarantined_count = 0
        rejected_count = 0
        persisted_record_ids: list[str] = []
        scope_ref = ScopeRef.from_dict(scope)
        for source in sources:
            result = collect_from_source_entry(source, fetch_text=fetch_text)
            if fetch and fetch_text is not None and str(source.source_kind or "").lower() == "rss":
                result = _enrich_rss_result_with_fulltext(result, fetch_text=fetch_text)
            source_item_limit = _source_max_items(source)
            allowed_items = result.items[:source_item_limit]
            if item_budget is not None:
                allowed_items = allowed_items[: max(0, item_budget - item_count)]
            if len(allowed_items) != len(result.items):
                result = FetchResult(
                    ok=result.ok,
                    items=list(allowed_items),
                    error=result.error,
                    metadata={**dict(result.metadata or {}), "truncated": True, "max_items": len(allowed_items)},
                )
            payload = asdict(result)
            payload["source_id"] = source.source_id
            payload["source_kind"] = source.source_kind
            item_count += len(result.items)
            source_written_count = 0
            source_skipped_existing_count = 0
            if persist:
                for item in result.items:
                    record = _collected_item_record(
                        item,
                        source_id=source.source_id,
                        source_kind=source.source_kind,
                        fetch_metadata=dict(result.metadata or {}),
                        scope=scope_ref,
                    )
                    if record.status == "quarantined":
                        quarantined_count += 1
                    elif record.status == "rejected":
                        rejected_count += 1
                    if self.store.get_by_id(record.record_id, scope=record.scope) is not None:
                        skipped_existing_count += 1
                        source_skipped_existing_count += 1
                        continue
                    self.store.append(record)
                    written_count += 1
                    source_written_count += 1
                    persisted_record_ids.append(record.record_id)
            self.sources.mark_source_scanned(
                source.source_id,
                scanned_at=scanned_at,
                status="ok" if result.ok else "error",
                item_count=len(result.items),
                written_count=source_written_count,
                skipped_existing_count=source_skipped_existing_count,
                error=result.error,
            )
            results.append(payload)
            if item_budget is not None and item_count >= item_budget:
                break
        # Aggregate per-source ok flags. An empty source list is a
        # vacuous success (no failures, no work to do). A non-empty
        # list requires every source payload to report ok=True for
        # the overall run to succeed. ``failed_sources`` carries the
        # source_id of any source that returned ok=False so callers
        # (and the CLI) can react.
        ok = all(bool(r.get("ok")) for r in results) if results else True
        failed_sources: list[str] = [
            str(r.get("source_id", ""))
            for r in results
            if not bool(r.get("ok"))
        ]
        return {
            "ok": ok,
            "persist": bool(persist),
            "source_count": len(sources),
            "item_count": item_count,
            "written_count": written_count,
            "skipped_existing_count": skipped_existing_count,
            "quarantined_count": quarantined_count,
            "rejected_count": rejected_count,
            "persisted_record_ids": persisted_record_ids,
            "failed_sources": failed_sources,
            "error_count": len(failed_sources),
            "results": results,
        }

    def promote_paper_candidate(self, record_or_payload, *, scope: dict | None = None) -> dict:
        from eimemory.intake.pipeline import promote_paper_candidate

        return promote_paper_candidate(self, record_or_payload, scope)

    def promote_collected_paper_candidates(self, *, scope: dict | None = None, limit: int = 100, auto: bool = False) -> dict:
        from eimemory.intake.pipeline import promote_collected_paper_candidates

        return promote_collected_paper_candidates(self, scope, limit=limit, auto=auto)

    def review_pending_research_closures(
        self,
        *,
        scope: dict | None = None,
        limit: int = 20,
        review_model: str = "gpt-5.5",
        executor=None,
    ) -> dict:
        from eimemory.intake.closure_review import review_pending_research_closures

        return review_pending_research_closures(
            self,
            scope=scope,
            limit=limit,
            review_model=review_model,
            executor=executor,
        )

    def list_intake_review_queue(self, *, scope: dict | None = None, status=None, limit: int = 100) -> list[dict]:
        from eimemory.intake.review import list_review_queue

        return list_review_queue(self, scope, status=status, limit=limit)

    def explain_intake_candidate(self, *, record_id: str, scope: dict | None = None) -> dict:
        from eimemory.intake.review import explain_candidate

        return explain_candidate(self, record_id, scope=scope)

    def review_intake_candidate(
        self,
        *,
        record_id: str,
        decision: str,
        reviewer: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import review_candidate

        return review_candidate(self, record_id, decision, reviewer, note=note, scope=scope)

    def promote_intake_candidate(
        self,
        *,
        record_id: str,
        promoter: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import promote_candidate

        return promote_candidate(self, record_id, promoter, note=note, scope=scope)

    def merge_intake_candidates(
        self,
        *,
        source_record_id: str,
        target_record_id: str,
        reviewer: str,
        note: str = "",
        scope: dict | None = None,
    ) -> RecordEnvelope:
        from eimemory.intake.review import merge_candidates

        return merge_candidates(self, source_record_id, target_record_id, reviewer, note=note, scope=scope)

    def source_quality_report(self, *, scope: dict | None = None) -> dict:
        from eimemory.intake.policy import build_source_quality_report

        return build_source_quality_report(self, scope or {})

    def collection_policy(self, *, scope: dict | None = None, topic_gaps: list[str] | None = None) -> dict:
        from eimemory.intake.policy import recommend_collection_policy

        return recommend_collection_policy(self, scope or {}, topic_gaps=topic_gaps or [])

    def expand_sources_autonomously(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        evaluator=None,
        max_apply: int = 3,
        min_score: float = 0.7,
    ) -> dict:
        from eimemory.intake.autonomous_sources import run_autonomous_source_expansion

        return run_autonomous_source_expansion(
            self,
            scope=scope or {},
            apply=apply,
            evaluator=evaluator,
            max_apply=max_apply,
            min_score=min_score,
        )

    def latest_source_expansion(self, *, scope: dict | None = None, limit: int = 20) -> dict:
        from eimemory.intake.autonomous_sources import latest_autonomous_source_expansion

        return latest_autonomous_source_expansion(self, scope=scope or {}, limit=limit)

    def discover_sources(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        gap_queries: list[str] | None = None,
        recent_titles: list[str] | None = None,
    ) -> dict:
        from eimemory.intake.source_discovery import discover_source_proposals

        scope_ref = ScopeRef.from_dict(scope)
        scope_payload = asdict(scope_ref)
        policy = self.collection_policy(scope=scope_payload, topic_gaps=gap_queries or [])
        recent_records = _list_all_runtime_records(
            self,
            kinds=["knowledge_candidate", "paper_source", "knowledge_page", "memory", "unknown"],
            scope=scope_ref,
            limit=300,
        )
        proposal_recent_titles = list(recent_titles or [])
        proposal_recent_titles.extend(record.title or record.summary for record in recent_records[:80])
        proposals = discover_source_proposals(
            gap_queries=list(policy.get("gap_queries") or []),
            sources=self.sources.list_sources(enabled=True),
            recent_titles=proposal_recent_titles,
        )
        persisted_record_ids: list[str] = []
        skipped_existing_count = 0
        if persist:
            for proposal in proposals:
                record = _source_discovery_record(proposal, scope=scope_ref)
                existing = self.store.get_by_id(record.record_id, scope=scope_ref)
                if existing is not None:
                    skipped_existing_count += 1
                    continue
                self.store.append(record)
                persisted_record_ids.append(record.record_id)
        return {
            "ok": True,
            "persist": bool(persist),
            "scope": scope_payload,
            "proposal_count": len(proposals),
            "approve_count": sum(1 for item in proposals if item.get("decision") == "approve"),
            "needs_review_count": sum(1 for item in proposals if item.get("decision") == "needs_review"),
            "persisted_record_ids": persisted_record_ids,
            "skipped_existing_count": skipped_existing_count,
            "proposals": proposals,
        }

    def build_daily_brief(
        self,
        *,
        scope: dict | None = None,
        date: str | date_type | None = None,
        persist: bool = False,
        channel: str = "feishu",
        research_lookback_days: int = 1,
    ) -> dict:
        from eimemory.knowledge.daily_brief import build_daily_brief, build_daily_brief_delivery_payload

        scope_ref = ScopeRef.from_dict(scope)
        day: str | date_type = date if date is not None else now_iso()[:10]
        records = _list_all_runtime_records(self, kinds=None, scope=scope_ref, limit=2500)
        brief = build_daily_brief(records, date=day, research_lookback_days=research_lookback_days)
        delivery = build_daily_brief_delivery_payload(brief, channel=channel)
        persisted_record_id = ""
        if persist:
            record = _daily_brief_record(brief, delivery, scope=scope_ref)
            self.store.append(record)
            persisted_record_id = record.record_id
        return {
            **brief,
            "delivery": delivery,
            "persisted": bool(persist),
            "persisted_record_id": persisted_record_id,
        }

    def run_rule_evolution(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        min_roi: float = 0.0,
        replay_datasets: dict[str, list[dict]] | None = None,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.governance.rule_evolution import run_rule_evolution_loop

        scope_ref = ScopeRef.from_dict(scope)
        scope_payload = asdict(scope_ref)
        replayed_rule_ids: list[str] = []
        for rule_id, dataset in dict(replay_datasets or {}).items():
            if not dataset:
                continue
            rule = self.store.get_by_id(str(rule_id), scope=scope_ref)
            if rule is None or rule.kind != "rule":
                continue
            self.evolution.replay_rule(record_id=rule.record_id, dataset=dataset)
            replayed_rule_ids.append(rule.record_id)

        report = run_rule_evolution_loop(self, scope_payload, apply=apply, min_roi=min_roi)
        report["replayed_rule_ids"] = replayed_rule_ids
        persisted_record_id = ""
        if persist_report:
            record = _rule_evolution_report_record(report, scope=scope_ref)
            self.store.append(record)
            persisted_record_id = record.record_id
        return {**report, "persisted": bool(persist_report), "persisted_record_id": persisted_record_id}

    def run_autonomous_evolution(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        max_apply: int = 3,
        web_hypotheses: list[dict] | None = None,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.governance.autonomous_evolution import run_autonomous_evolution

        return run_autonomous_evolution(
            self,
            scope=scope,
            apply=apply,
            max_apply=max_apply,
            web_hypotheses=web_hypotheses,
            persist_report=persist_report,
        )

    def run_autonomous_learning_cycle(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        dry_run: bool = False,
        full: bool = True,
        force: bool = False,
        max_goals: int = 3,
        max_promotions: int | None = None,
        allow_network: bool | None = None,
    ) -> dict:
        from eimemory.governance.autonomous_learning import run_autonomous_learning_cycle

        return run_autonomous_learning_cycle(
            self,
            scope=scope,
            apply=apply,
            dry_run=dry_run,
            full=full,
            force=force,
            max_goals=max_goals,
            max_promotions=max_promotions,
            allow_network=allow_network,
        )

    def run_isolated_evaluator_harness(
        self,
        *,
        scope: dict | None = None,
        loop_id: str = "isolated_evaluator_smoke",
        generator_model: str | None = None,
        evaluator_model: str | None = None,
        stop_judge_model: str | None = None,
        replay_gate: dict | None = None,
        real_task_replay: dict | None = None,
    ) -> dict:
        from eimemory.governance.isolated_evaluator import run_isolated_evaluator_harness

        return run_isolated_evaluator_harness(
            self,
            scope=scope,
            loop_id=loop_id,
            generator_model=generator_model,
            evaluator_model=evaluator_model,
            stop_judge_model=stop_judge_model,
            replay_gate=replay_gate,
            real_task_replay=real_task_replay,
        )

    def run_autonomy_cycle(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        dry_run: bool = False,
        full: bool = True,
        force: bool = False,
        max_goals: int = 3,
        policy: dict | None = None,
        smoke: bool = False,
    ) -> dict:
        from eimemory.governance.autonomy_controller import run_autonomy_cycle

        return run_autonomy_cycle(
            self,
            scope=scope,
            apply=apply,
            dry_run=dry_run,
            full=full,
            force=force,
            max_goals=max_goals,
            policy=policy,
            smoke=smoke,
        )

    def list_learning_loops(self, *, scope: dict | None = None, limit: int = 10) -> list[dict]:
        from eimemory.governance.autonomous_learning import list_learning_loops

        return list_learning_loops(self, scope=scope, limit=limit)

    def list_learning_goals(self, *, scope: dict | None = None, limit: int = 10) -> list[dict]:
        from eimemory.governance.autonomous_learning import list_learning_goals

        return list_learning_goals(self, scope=scope, limit=limit)

    def list_learning_candidates(self, *, scope: dict | None = None, limit: int = 10) -> list[dict]:
        from eimemory.governance.autonomous_learning import list_learning_candidates

        return list_learning_candidates(self, scope=scope, limit=limit)

    def learning_ledger(
        self,
        *,
        scope: dict | None = None,
        limit: int = 500,
        since: str | None = None,
        until: str | None = None,
        attribute_outcomes: bool = False,
    ) -> dict:
        from eimemory.governance.capability_ledger import build_capability_ledger

        return build_capability_ledger(
            self,
            scope=scope,
            limit=limit,
            since=since,
            until=until,
            attribute_outcomes=attribute_outcomes,
        )

    def build_autonomy_goal_queue(
        self,
        *,
        scope: dict | None = None,
        max_goals: int = 3,
        persist: bool = False,
        capabilities: list[str] | None = None,
    ) -> dict:
        from eimemory.governance.autonomy_goal_queue import build_autonomy_goal_queue

        return build_autonomy_goal_queue(
            self,
            scope=scope,
            max_goals=max_goals,
            persist=persist,
            capabilities=capabilities,
        )

    def build_goal_graph_loop(
        self,
        *,
        scope: dict | None = None,
        max_goals: int = 3,
        persist: bool = False,
        capabilities: list[str] | None = None,
        loop_id: str = "goal_graph_1_6_9",
    ) -> dict:
        from eimemory.governance.goal_graph import build_goal_graph_loop

        return build_goal_graph_loop(
            self,
            scope=scope,
            max_goals=max_goals,
            persist=persist,
            capabilities=capabilities,
            loop_id=loop_id,
        )

    def observe_goal_graph_node(
        self,
        *,
        graph: dict,
        node_id: str,
        status: str,
        reward: float,
        ledger_refs: list[str] | None = None,
        rollback_refs: list[str] | None = None,
        persist: bool = False,
        scope: dict | None = None,
        loop_id: str = "goal_graph_observe_1_6_9",
    ) -> dict:
        from eimemory.governance.goal_graph import observe_goal_graph_node

        return observe_goal_graph_node(
            self,
            graph=graph,
            node_id=node_id,
            status=status,
            reward=reward,
            ledger_refs=ledger_refs,
            rollback_refs=rollback_refs,
            persist=persist,
            scope=scope,
            loop_id=loop_id,
        )

    def observe_coding_memory(
        self,
        observation: dict,
        *,
        scope: dict | None = None,
    ) -> dict:
        from eimemory.governance.coding_memory_contract import observe_coding_memory

        return observe_coding_memory(self, observation, scope=scope)

    def query_coding_memory_graph(
        self,
        query: str,
        *,
        scope: dict | None = None,
        limit: int = 5,
    ) -> dict:
        from eimemory.governance.coding_memory_contract import query_coding_memory_graph

        return query_coding_memory_graph(self, query, scope=scope, limit=limit)

    def run_coding_graph_replay(
        self,
        *,
        query: str,
        expected_relations: list[str] | None = None,
        scope: dict | None = None,
        persist: bool = False,
    ) -> dict:
        from eimemory.governance.coding_memory_contract import run_coding_graph_replay

        return run_coding_graph_replay(
            self,
            query=query,
            expected_relations=expected_relations,
            scope=scope,
            persist=persist,
        )

    def audit_coding_memory_contract(
        self,
        *,
        scope: dict | None = None,
        limit: int = 50,
    ) -> dict:
        from eimemory.governance.coding_memory_contract import audit_coding_memory_contract

        return audit_coding_memory_contract(self, scope=scope, limit=limit)

    def record_user_correction_replay(
        self,
        correction: dict,
        *,
        scope: dict | None = None,
        persist: bool = True,
    ) -> dict:
        from eimemory.governance.correction_replay import record_user_correction_replay

        return record_user_correction_replay(self, correction, scope=scope, persist=persist)

    def build_ground_truth_pre_answer_gate(
        self,
        *,
        query: str = "",
        scope: dict | None = None,
        persist: bool = True,
        limit: int = 100,
    ) -> dict:
        from eimemory.governance.correction_replay import build_ground_truth_pre_answer_gate

        return build_ground_truth_pre_answer_gate(self, query=query, scope=scope, persist=persist, limit=limit)

    def build_world_model(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        loop_id: str = "l5_world_model",
        limit: int = 500,
    ) -> dict:
        from eimemory.governance.l5_loop import build_world_model

        return build_world_model(self, scope=scope, persist=persist, loop_id=loop_id, limit=limit)

    def build_strategic_roadmap(
        self,
        *,
        scope: dict | None = None,
        world_model: dict | None = None,
        horizon_days: int = 180,
        persist: bool = False,
        loop_id: str = "l5_roadmap",
    ) -> dict:
        from eimemory.governance.l5_loop import build_strategic_roadmap

        return build_strategic_roadmap(
            self,
            scope=scope,
            world_model=world_model,
            horizon_days=horizon_days,
            persist=persist,
            loop_id=loop_id,
        )

    def run_l5_cycle(
        self,
        *,
        scope: dict | None = None,
        apply: bool = False,
        force: bool = False,
        max_goals: int = 1,
        max_promotions: int = 0,
        allow_network: bool | None = True,
        loop_id: str = "",
        persist: bool = True,
        autonomous_learning_report: dict | None = None,
    ) -> dict:
        from eimemory.governance.l5_loop import run_l5_cycle

        return run_l5_cycle(
            self,
            scope=scope,
            apply=apply,
            force=force,
            max_goals=max_goals,
            max_promotions=max_promotions,
            allow_network=allow_network,
            loop_id=loop_id,
            persist=persist,
            autonomous_learning_report=autonomous_learning_report,
        )

    def assess_l5_closed_loop(
        self,
        *,
        scope: dict | None = None,
        loop_report: dict | None = None,
        persist: bool = False,
        loop_id: str = "l5_assess",
    ) -> dict:
        from eimemory.governance.l5_loop import assess_l5_closed_loop

        return assess_l5_closed_loop(self, scope=scope, loop_report=loop_report, persist=persist, loop_id=loop_id)

    def build_l5_readiness_report(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        limit: int = 500,
        loop_id: str = "l5_readiness",
    ) -> dict:
        from eimemory.governance.l5_readiness import build_l5_readiness_report

        return build_l5_readiness_report(self, scope=scope, persist=persist, limit=limit, loop_id=loop_id)

    def run_l5_closure_rehearsal(
        self,
        *,
        scope: dict | None = None,
        persist: bool = True,
    ) -> dict:
        from eimemory.governance.closure_rehearsal import run_l5_closure_rehearsal

        return run_l5_closure_rehearsal(self, scope=scope, persist=persist)

    def run_live_task_acceptance(
        self,
        *,
        scope: dict | None = None,
        repo_root: str,
        current_link: str,
        health_url: str,
        prior_commit: str = "",
    ) -> dict:
        from eimemory.governance.live_task_acceptance import run_live_task_acceptance

        return run_live_task_acceptance(
            self,
            scope=scope,
            repo_root=repo_root,
            current_link=current_link,
            health_url=health_url,
            prior_commit=prior_commit,
        )

    def run_capability_acceptance(
        self,
        *,
        scope: dict | None = None,
        persist: bool = True,
        execution_id: str = "",
    ) -> dict:
        from eimemory.governance.capability_acceptance import run_capability_acceptance

        return run_capability_acceptance(
            self,
            scope=scope,
            persist=persist,
            execution_id=execution_id,
        )

    def build_capability_replay_packs(
        self,
        *,
        scope: dict | None = None,
        capabilities: list[str] | None = None,
        persist: bool = False,
        loop_id: str = "capability_replay_1_6_9",
        acceptance_execution_id: str = "",
        acceptance_probe_ids_by_case: dict[str, str] | None = None,
    ) -> dict:
        from eimemory.governance.capability_replay_packs import build_capability_replay_packs

        return build_capability_replay_packs(
            self,
            scope=scope,
            capabilities=capabilities,
            persist=persist,
            loop_id=loop_id,
            acceptance_execution_id=acceptance_execution_id,
            acceptance_probe_ids_by_case=acceptance_probe_ids_by_case,
        )

    def run_capability_replay_case(self, case: dict) -> dict:
        from eimemory.governance.capability_replay_executor import execute_capability_replay_case

        return execute_capability_replay_case(self, case)

    def run_safety_boundary_replay(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        loop_id: str = "safety_boundary_replay_1_6_9",
    ) -> dict:
        from eimemory.governance.safety_replay import run_safety_boundary_replay

        return run_safety_boundary_replay(self, scope=scope, persist=persist, loop_id=loop_id)

    def promote_repeated_sops_to_skill_candidates(
        self,
        *,
        scope: dict | None = None,
        min_repeats: int = 3,
        persist: bool = False,
        limit: int = 500,
    ) -> dict:
        from eimemory.governance.skill_sedimentation import promote_repeated_sops_to_skill_candidates

        return promote_repeated_sops_to_skill_candidates(
            self,
            scope=scope,
            min_repeats=min_repeats,
            persist=persist,
            limit=limit,
        )

    def list_eiskills(self, *, scope: dict | None = None, limit: int = 100) -> dict:
        from eimemory.governance.skill_sedimentation import list_eiskills

        return list_eiskills(self, scope=scope, limit=limit)

    def call_eiskill(
        self,
        *,
        skill_id: str,
        scope: dict | None = None,
        context: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> dict:
        from eimemory.governance.skill_sedimentation import call_eiskill

        return call_eiskill(self, skill_id=skill_id, scope=scope, context=context, persist=persist)

    def build_capability_dashboard_metrics(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        limit: int = 500,
        loop_id: str = "capability_dashboard_1_6_9",
    ) -> dict:
        from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics

        return build_capability_dashboard_metrics(self, scope=scope, persist=persist, limit=limit, loop_id=loop_id)

    def verify_and_record_deployment(
        self,
        *,
        scope: dict | None = None,
        repo_root: str,
        current_link: str,
        health_url: str,
        prior_commit: str = "",
    ) -> dict:
        from eimemory.governance.deployment_receipt import verify_and_record_deployment

        return verify_and_record_deployment(
            self,
            scope=scope,
            repo_root=repo_root,
            current_link=current_link,
            health_url=health_url,
            prior_commit=prior_commit,
        )

    def ensure_capability_seeded(self, *, scope: dict | None = None) -> dict:
        from eimemory.governance.capability_seeding import ensure_all_seeded

        return ensure_all_seeded(self, scope=scope)

    def generate_learning_thoughts(self, *, scope: dict | None = None, persist: bool = True, max_items: int = 20) -> dict:
        from eimemory.governance.goal_registry import load_goal_registry
        from eimemory.governance.self_model import build_self_model
        from eimemory.governance.signal_intake import rank_learning_signals
        from eimemory.governance.supervisor import persist_supervisor_summary, supervisor_summary
        from eimemory.governance.thoughts import generate_thoughts
        from eimemory.governance.world_watchers import collect_world_signals, default_watches

        started_at = time.perf_counter()
        started_tracing = tracemalloc.is_tracing()
        if not started_tracing:
            tracemalloc.start()
        try:
            watch_report = collect_world_signals(self, scope=scope, watches=default_watches(), dry_run=True, loop_id="think")
            self_model = build_self_model(self, scope=scope, persist=persist, loop_id="think")
            ranked = rank_learning_signals(watch_report.get("signals") or [], self_model, [], max_items=max_items)
            registry = load_goal_registry()
            report = generate_thoughts(
                self,
                signals=ranked,
                self_model=self_model,
                goals=list(registry.get("long_term") or []),
                scope=scope,
                loop_id="think",
                persist=persist,
                max_items=max_items,
            )
            summary = supervisor_summary(
                command="learn-think",
                ok=bool(report.get("ok", True)),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                memory_peak=int(tracemalloc.get_traced_memory()[1]) if tracemalloc.is_tracing() else 0,
                produced_count=_supervisor_count(report, {"persisted_count", "thought_count", "record_count"}),
                promoted_count=0,
                rolled_back_count=0,
                error=str(report.get("error") or ""),
            )
            report["supervisor_summary"] = summary
            if persist:
                persist_supervisor_summary(self, scope=scope, summary=summary)
            return report
        except Exception as exc:
            summary = supervisor_summary(
                command="learn-think",
                ok=False,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                memory_peak=int(tracemalloc.get_traced_memory()[1]) if tracemalloc.is_tracing() else 0,
                error=str(exc),
            )
            if persist:
                persist_supervisor_summary(self, scope=scope, summary=summary)
            raise
        finally:
            if not started_tracing and tracemalloc.is_tracing():
                tracemalloc.stop()

    def build_replay_dataset(
        self,
        *,
        scope: dict | None = None,
        limit: int = 50,
        persist: bool = True,
        include_built_in_regressions: bool = False,
    ) -> dict:
        from eimemory.governance.replay_dataset import build_replay_dataset

        return build_replay_dataset(
            self,
            scope=scope,
            limit=limit,
            persist=persist,
            loop_id="cli",
            include_built_in_regressions=include_built_in_regressions,
        )

    def build_learning_dashboard(
        self,
        *,
        scope: dict | None = None,
        week_start: str | None = None,
        persist: bool = True,
        output_path: str | None = None,
        weekly: bool = False,
    ) -> dict:
        from eimemory.governance.learning_dashboard import build_weekly_dashboard

        return build_weekly_dashboard(self, scope=scope, week_start=week_start, persist=persist, output_path=output_path, weekly=weekly)

    def compact_learning_records(self, *, scope: dict | None = None, dry_run: bool = True) -> dict:
        from eimemory.governance.learning_retention import compact_learning_records

        return compact_learning_records(self, scope=scope, dry_run=dry_run)

    def build_learning_daily_report(self, *, scope: dict | None = None, persist: bool = True, report_date: str | None = None) -> dict:
        from eimemory.governance.learning_report import build_learning_daily_report

        return build_learning_daily_report(self, scope=scope, persist=persist, report_date=report_date)

    def run_code_sandbox(
        self,
        *,
        incident: dict[str, Any],
        scope: dict | None = None,
        create_worktree: bool = False,
        persist_report: bool = False,
        runner: object | None = None,
        worktree_root: str | Path | None = None,
    ) -> dict:
        from eimemory.governance.code_evolution import run_code_sandbox

        return run_code_sandbox(
            self,
            incident=incident,
            scope=scope,
            create_worktree=create_worktree,
            persist_report=persist_report,
            runner=runner,
            worktree_root=worktree_root,
        )

    def propose_code_patch(
        self,
        *,
        incident: dict[str, Any],
        scope: dict | None = None,
        create_worktree: bool = False,
        persist_report: bool = False,
        runner: object | None = None,
        worktree_root: str | Path | None = None,
    ) -> dict:
        from eimemory.governance.code_evolution_bridge import propose_code_patch

        return propose_code_patch(
            self,
            incident=incident,
            scope=scope,
            create_worktree=create_worktree,
            persist_report=persist_report,
            runner=runner,
            worktree_root=worktree_root,
        )

    def scout_web_learning(
        self,
        *,
        scope: dict | None = None,
        urls: list[str] | None = None,
        evidence: list[dict] | None = None,
        timeout_seconds: int = 8,
    ) -> dict:
        from eimemory.governance.web_learning import scout_web_learning

        return scout_web_learning(
            self,
            scope=scope,
            urls=urls,
            evidence=evidence,
            timeout_seconds=timeout_seconds,
        )

    def run_evaluation(
        self,
        dataset: dict | list,
        *,
        scope: dict | None = None,
        task_type: str = "",
        profile: str = "balanced",
        seed: bool = True,
    ) -> dict:
        from eimemory.evaluation import run_evaluation

        return run_evaluation(
            self,
            dataset,
            scope=scope,
            task_type=task_type,
            profile=profile,
            seed=seed,
        )

    def run_memory_eval_ci(
        self,
        dataset: dict | list,
        *,
        scope: dict | None = None,
        emit_incidents: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_memory_eval_ci
        if scope is not None and not isinstance(scope, dict):
            raise TypeError("scope must be a mapping")
        if scope is not None and isinstance(dataset, dict):
            dataset = {**dataset, "scope": {**dict(dataset.get("scope") or {}), **dict(scope)}}
        if scope is not None and isinstance(dataset, list):
            dataset = {"name": "memory_eval_ci", "scope": dict(scope), "cases": dataset}
        return run_memory_eval_ci(self, dataset, emit_incidents=emit_incidents)

    def run_longmemeval(
        self,
        dataset: dict | list,
        *,
        mode: str = "raw",
        granularity: str = "session",
        limit: int = 10,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_longmemeval

        return run_longmemeval(
            self,
            dataset,
            mode=mode,
            granularity=granularity,
            limit=limit,
            persist_report=persist_report,
        )

    def run_locomo(
        self,
        dataset: dict | list,
        *,
        mode: str = "raw",
        granularity: str = "turn",
        limit: int = 10,
    ) -> dict:
        from eimemory.evaluation import run_locomo

        return run_locomo(self, dataset, mode=mode, granularity=granularity, limit=limit)

    def run_public_memory_benchmark(
        self,
        dataset: dict | list,
        *,
        suite: str,
        mode: str = "raw",
        granularity: str = "",
        limit: int = 10,
    ) -> dict:
        from eimemory.evaluation import run_public_memory_benchmark

        return run_public_memory_benchmark(
            dataset,
            suite=suite,
            mode=mode,
            granularity=granularity,
            limit=limit,
        )

    def run_livingmem_eval(
        self,
        dataset: dict | list,
        *,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_livingmem_eval

        return run_livingmem_eval(
            self,
            dataset,
            persist_report=persist_report,
        )

    def run_actionable_memory_eval(
        self,
        dataset: dict | list,
        *,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_actionable_memory_eval

        return run_actionable_memory_eval(self, dataset, persist_report=persist_report)

    def run_production_recall_eval(
        self,
        dataset: dict | list,
        *,
        seed: bool = True,
        scope: dict | None = None,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_production_recall_eval

        return run_production_recall_eval(self, dataset, seed=seed, scope=scope, persist_report=persist_report)

    def run_real_task_replay(
        self,
        dataset: dict | list,
        *,
        seed: bool = True,
        persist_report: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_real_task_replay

        return run_real_task_replay(self, dataset, seed=seed, persist_report=persist_report)

    def enrich_living_memory(self, *, scope: dict | None = None, limit: int = 100) -> dict:
        from eimemory.living.operations import enrich_memory_records

        return enrich_memory_records(self, scope=scope or {}, limit=limit)

    def build_living_timeline(self, *, scope: dict | None = None, limit: int = 100) -> dict:
        from eimemory.living.operations import build_living_timeline

        return build_living_timeline(self, scope=scope or {}, limit=limit)

    def recommend_action_posture(self, query: str, *, scope: dict | None = None, limit: int = 5) -> dict:
        from eimemory.living.operations import recommend_action_posture

        return recommend_action_posture(self, query, scope=scope or {}, limit=limit)

    def record_skill_trace(self, payload: dict, *, scope: dict | None = None) -> dict:
        from eimemory.experience import record_skill_trace

        return record_skill_trace(self, payload, scope=scope)

    def record_experience_item(self, payload: dict, *, scope: dict | None = None) -> dict:
        from eimemory.experience import record_experience_item

        return record_experience_item(self, payload, scope=scope)

    def record_outcome_trace(self, payload: dict, *, scope: dict | None = None) -> dict:
        from eimemory.experience import record_outcome_trace

        return record_outcome_trace(self, payload, scope=scope)

    def record_event(self, payload: dict, *, scope: dict | None = None) -> dict:
        return self.store.record_event(payload, scope=scope)

    def record_outcome(self, event_id: str, payload: dict, *, scope: dict | None = None) -> dict:
        recorded = self.store.record_outcome(event_id, payload, scope=scope)
        from eimemory.governance.promotion_watch import record_outcome_observations

        watch_reports = record_outcome_observations(self, event_id=event_id, outcome_payload=recorded, scope=scope)
        if watch_reports:
            recorded["post_promotion_watch"] = watch_reports
        return recorded

    def run_judgment_evaluation(
        self,
        scope: dict | None = None,
        *,
        since: str | None = None,
        limit: int | None = 200,
        persist_playbook: bool = False,
    ) -> dict:
        from eimemory.judgment import run_judgment_evaluation

        return run_judgment_evaluation(
            self,
            scope=scope,
            since=since,
            limit=limit,
            persist_playbook=persist_playbook,
        )

    def upsert_intent_pattern(self, payload: dict, *, scope: dict | None = None) -> dict:
        return self.store.upsert_intent_pattern(payload, scope=scope)

    def search_policy(
        self,
        user_phrase: str,
        *,
        scope: dict | None = None,
        context: dict | None = None,
        limit: int = 5,
    ) -> dict:
        return self.store.search_policy(user_phrase, scope=scope, context=context, limit=limit)

    def get_policy_rollout_ledger(
        self,
        *,
        scope: dict | None = None,
        action: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        return self.store.get_policy_rollout_ledger(scope=scope, action=action, limit=limit)

    def rollback_intent_pattern(
        self,
        pattern_id: str,
        *,
        scope: dict | None = None,
        reason: str = "",
        auto: bool = False,
    ) -> dict:
        return self.store.rollback_intent_pattern(pattern_id, scope=scope, reason=reason, auto=auto)

    def export_knowledge_pack(self, path: str | Path, *, scope: dict | None = None, include_candidates: bool = False) -> dict:
        from eimemory.intake.packs import export_knowledge_pack

        return export_knowledge_pack(self, path, scope or {}, include_candidates=include_candidates)

    def import_knowledge_pack(self, path: str | Path, *, scope: dict | None = None, dry_run: bool = False) -> dict:
        from eimemory.intake.packs import import_knowledge_pack

        return import_knowledge_pack(self, path, scope or {}, dry_run=dry_run)

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def ingest_paper_source(self, paper_input: dict, *, scope: dict | None = None) -> RecordEnvelope:
        return ingest_paper_source(self.store, paper_input, scope=scope)

    def ingest_knowledge_source(
        self,
        payload: dict[str, Any],
        *,
        scope: dict[str, Any] | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        from eimemory.knowledge.ingest import ingest_knowledge_source

        return ingest_knowledge_source(self, payload, scope=scope, persist=persist)

    def extract_paper_memory(self, paper_input: dict, *, scope: dict | None = None) -> PaperMemoryExtraction:
        result = extract_paper_memory(
            paper_source_id=str(paper_input["paper_source_id"]),
            title=str(paper_input.get("title", "")),
            abstract=str(paper_input.get("abstract", "")),
            body=str(paper_input.get("body", "")),
            metadata=dict(paper_input.get("metadata") or {}),
            provenance=dict(paper_input.get("provenance") or {}),
        )
        for record in result.to_records(scope=scope):
            self.store.append(record)
        return result

    def compile_paper_knowledge(
        self,
        *,
        extraction: PaperMemoryExtraction,
        scope: dict | None = None,
    ) -> KnowledgeCompilation:
        result = compile_paper_knowledge(extraction=extraction)
        for record in result.to_records(scope=scope):
            self.store.append(record)
        return result

    def project_operational_knowledge(self, *, scope: dict | None = None, limit: int = 100) -> dict:
        return project_operational_knowledge(self.store, scope=scope, limit=limit)

    def extract_skill_candidates(
        self,
        *,
        knowledge_units: list[Any] | None = None,
        scope: dict | None = None,
        persist: bool = False,
        limit: int = 100,
    ) -> dict:
        from eimemory.governance.skill_candidate import extract_skill_candidates

        return extract_skill_candidates(
            self.store,
            knowledge_units=knowledge_units,
            scope=scope,
            persist=persist,
            limit=limit,
        )

    def validate_skill_candidate(
        self,
        *,
        candidate_id: str | None = None,
        scope: dict | None = None,
        candidate: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> dict:
        from eimemory.governance.skill_validation import validate_skill_candidate

        return validate_skill_candidate(
            self.store,
            candidate_id=candidate_id,
            scope=scope,
            candidate=candidate,
            persist=persist,
        )

    def record_skill_candidate_observation(
        self,
        *,
        candidate_id: str,
        scope: dict | None = None,
        outcome: str,
        observation_id: str = "",
        observation_kind: str = "real",
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict:
        from eimemory.governance.skill_validation import record_skill_candidate_observation

        return record_skill_candidate_observation(
            self.store,
            candidate_id=candidate_id,
            scope=scope,
            outcome=outcome,
            observation_id=observation_id,
            observation_kind=observation_kind,
            reason=reason,
            details=details,
        )

    def build_research_digest(
        self,
        *,
        scope: dict | None = None,
        persist: bool = False,
        limit: int = 5,
        digest_date: str | None = None,
    ) -> dict:
        paper_sources = self.store.list_records(kinds=["paper_source"], scope=scope, limit=1000)
        claim_cards = self.store.list_records(kinds=["claim_card"], scope=scope, limit=1000)
        knowledge_pages = self.store.list_records(kinds=["knowledge_page"], scope=scope, limit=1000)
        candidates = self.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=1000)
        digest = build_research_digest(
            paper_sources=paper_sources,
            claim_cards=claim_cards,
            knowledge_pages=knowledge_pages,
            candidates=candidates,
            limit=limit,
            digest_date=digest_date,
        )
        if persist and digest["ok"]:
            record = digest_to_record(digest, scope=scope)
            self.store.append(record)
            digest = {**digest, "persisted": True, "persisted_page_id": record.record_id}
        return digest

    def filter_answer_evidence(
        self,
        records: list[Any],
        *,
        task_type: str = "",
    ) -> dict:
        from eimemory.knowledge.evidence_gate import filter_answer_evidence

        return filter_answer_evidence(records, task_type=task_type)


def _list_all_runtime_records(
    runtime: Runtime,
    *,
    kinds: list[str] | None,
    scope: ScopeRef,
    limit: int,
    page_size: int = 500,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    max_count = max(0, int(limit))
    while len(records) < max_count:
        page = runtime.store.list_records(
            kinds=kinds,
            scope=scope,
            limit=min(page_size, max_count - len(records)),
            offset=offset,
        )
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def _daily_brief_record(brief: dict[str, Any], delivery: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    day = str(brief.get("date") or now_iso()[:10])
    record_id = f"daily_brief_{day.replace('-', '')}_{_scope_hash(scope)}"
    summary = (
        f"Daily brief for {day}: "
        f"{int((brief.get('conversation_summary') or {}).get('message_count') or 0)} conversation memories, "
        f"{len(brief.get('decisions') or [])} decisions, "
        f"{len(brief.get('followups') or [])} followups."
    )
    return RecordEnvelope(
        record_id=record_id,
        kind="reflection",
        status="active",
        title=f"Daily experience brief {day}",
        summary=summary,
        detail=summary,
        content={
            "brief": _json_safe(brief),
            "delivery": _json_safe(delivery),
        },
        tags=["daily-brief", "experience-brief", "delivery-prepared"],
        links=[],
        evidence=[],
        source="eimemory.daily_brief",
        scope=scope,
        time=TimeRef.now(),
        provenance={"report_type": "daily_brief", "date": day, "channel": str(delivery.get("channel") or "")},
        meta={
            "report_type": "daily_brief",
            "date": day,
            "delivery_channel": str(delivery.get("channel") or ""),
            "delivery_status": str((delivery.get("outbox") or {}).get("status") or "prepared"),
        },
    )


def _source_discovery_record(proposal: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    proposal_id = str(proposal.get("proposal_id") or sha256(json.dumps(proposal, sort_keys=True).encode("utf-8")).hexdigest()[:16])
    decision = str(proposal.get("decision") or "needs_review")
    record_id = f"{proposal_id}_{_scope_hash(scope)}"
    return RecordEnvelope(
        record_id=record_id,
        kind="source_candidate",
        status="candidate",
        title=str(proposal.get("title") or "Source discovery candidate"),
        summary=str(proposal.get("reason") or ""),
        detail=str(proposal.get("uri") or ""),
        content={"proposal": _json_safe(proposal)},
        tags=["source-discovery", decision, *[str(tag) for tag in (proposal.get("tags") or [])]],
        links=[],
        evidence=[str(item) for item in ((proposal.get("metadata") or {}).get("evidence") or [])][:10],
        source="eimemory.source_discovery",
        scope=scope,
        time=TimeRef.now(),
        provenance={
            "proposal_id": proposal_id,
            "scan_kind": "source_discovery",
            "source_uri": str(proposal.get("uri") or ""),
            "source_kind": str(proposal.get("source_kind") or ""),
        },
        meta={
            "proposal_id": proposal_id,
            "source_kind": str(proposal.get("source_kind") or ""),
            "source_uri": str(proposal.get("uri") or ""),
            "source_family": str((proposal.get("metadata") or {}).get("source_family") or ""),
            "decision": decision,
            "score": float(proposal.get("score") or 0.0),
        },
    )


def _rule_evolution_report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    generated_at = now_iso()
    record_id = f"rule_evolution_{generated_at[:10].replace('-', '')}_{_scope_hash(scope)}"
    state_prefix = "Rule evolution steady state" if bool(report.get("steady_state")) else "Rule evolution"
    summary = (
        f"{state_prefix}: {int(report.get('candidate_count') or 0)} candidates, "
        f"{int(report.get('promoted_count') or 0)} promotions, "
        f"{int(report.get('active_rule_count') or 0)} active rules, "
        f"{int(report.get('replay_count') or 0)} replay results."
    )
    return RecordEnvelope(
        record_id=record_id,
        kind="reflection",
        status="active",
        title="Rule evolution loop report",
        summary=summary,
        detail=summary,
        content={"report": _json_safe(report)},
        tags=["rule-evolution", "feedback-rule-replay-roi"],
        links=[],
        evidence=[],
        source="eimemory.rule_evolution_loop",
        scope=scope,
        time=TimeRef.now(),
        provenance={"report_type": "rule_evolution", "generated_at": generated_at},
        meta={
            "report_type": "rule_evolution",
            "candidate_count": int(report.get("candidate_count") or 0),
            "promoted_count": int(report.get("promoted_count") or 0),
            "active_rule_count": int(report.get("active_rule_count") or 0),
            "replay_count": int(report.get("replay_count") or 0),
            "steady_state": bool(report.get("steady_state")),
            "no_op_reason": str(report.get("no_op_reason") or ""),
        },
    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _default_fetch_text(url: str) -> str:
    _validate_fetch_url(url)
    request = Request(
        url,
        headers={
            "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            "User-Agent": "eimemory/1.0 (+https://github.com/darrowz/eimemory)",
        },
    )
    opener = build_opener(_SafeRedirectHandler)
    with opener.open(request, timeout=30) as response:
        final_url = response.geturl()
        _validate_fetch_url(final_url)
        content_type = str(response.headers.get_content_type() or "").lower()
        if content_type and not any(content_type.startswith(prefix) for prefix in ALLOWED_FETCH_CONTENT_TYPES):
            raise ValueError("unsupported content type")
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read(MAX_FETCH_BYTES + 1)
        if len(raw) > MAX_FETCH_BYTES:
            raise ValueError("response too large")
        return raw.decode(charset, errors="replace")


def _enrich_rss_result_with_fulltext(result: Any, *, fetch_text) -> Any:
    from eimemory.intake.connectors import FetchResult
    from eimemory.intake.fulltext import parse_fulltext_document

    if not getattr(result, "ok", False) or not getattr(result, "items", None):
        return result

    enriched_items = []
    attempted_count = 0
    success_count = 0
    error_count = 0
    for item in result.items:
        enriched_item = item
        item_url = str(getattr(item, "url", "") or "").strip()
        if item_url:
            attempted_count += 1
            try:
                _validate_fetch_url(item_url)
                document = parse_fulltext_document(fetch_text(item_url), url=item_url, source_kind="web")
                original_content = str(getattr(item, "content", "") or "")
                if document.ok and len(document.text) > len(original_content):
                    metadata = dict(getattr(item, "metadata", {}) or {})
                    metadata["rss_summary"] = original_content
                    metadata["fulltext"] = {
                        "ok": True,
                        "quality_score": document.quality_score,
                        "byline": document.byline,
                        "date": document.date,
                        "canonical_url": document.canonical_url,
                        "image_count": len(document.images),
                    }
                    enriched_item = replace(
                        item,
                        title=document.title or item.title,
                        url=document.canonical_url or item.url,
                        content=document.text,
                        published_at=document.date or item.published_at,
                        metadata=metadata,
                    )
                    success_count += 1
            except Exception:
                error_count += 1
        enriched_items.append(enriched_item)

    return FetchResult(
        ok=result.ok,
        items=enriched_items,
        error=result.error,
        metadata={
            **dict(result.metadata or {}),
            "rss_fulltext": {
                "attempted_count": attempted_count,
                "success_count": success_count,
                "error_count": error_count,
            },
        },
    )


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        _validate_fetch_url(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_fetch_url(url: str) -> None:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported fetch URL scheme")
    if parsed.username or parsed.password:
        raise ValueError("credentials in fetch URL are not allowed")
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise ValueError("missing fetch URL host")
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("unsafe fetch URL host")
    addresses: set[str] = set()
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
    except socket.gaierror as exc:
        raise ValueError("fetch URL host could not be resolved") from exc
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("unsafe fetch URL host")


def _source_max_items(source: Any) -> int:
    metadata = dict(getattr(source, "metadata", {}) or {})
    try:
        return max(0, int(metadata.get("max_items", 10)))
    except (TypeError, ValueError):
        return 10


def _collected_item_record(
    item: Any,
    *,
    source_id: str,
    source_kind: str,
    fetch_metadata: dict[str, Any],
    scope: ScopeRef,
) -> RecordEnvelope:
    fingerprint = str(getattr(item, "fingerprint", "") or "")
    item_source_kind = str(getattr(item, "source_kind", "") or "")
    original_title = str(getattr(item, "title", "") or "")
    title = strip_candidate_title_prefixes(original_title, default="Fetched knowledge candidate")
    content = str(getattr(item, "content", "") or "")
    item_url = str(getattr(item, "url", "") or "")
    metadata = dict(getattr(item, "metadata", {}) or {})
    status = _collected_item_status(metadata)
    record_kind = _collected_item_record_kind(source_kind=source_kind, item_source_kind=item_source_kind, metadata=metadata)
    if record_kind == "news" and status == "candidate":
        status = "active"
    summary = _summary_from_content(content)
    content_excerpt = _content_excerpt(content)
    provenance = {
        "source_id": str(source_id or ""),
        "source_kind": str(source_kind or ""),
        "item_url": item_url,
        "fingerprint": fingerprint,
        "original_title": original_title,
        "fetch_source": item_source_kind,
        "fetch_metadata": dict(fetch_metadata or {}),
        "published_at": str(getattr(item, "published_at", "") or ""),
    }
    content_payload = {
        "source_id": str(source_id or ""),
        "source_kind": str(source_kind or ""),
        "fetch_source": item_source_kind,
        "item_url": item_url,
        "fingerprint": fingerprint,
        "original_title": original_title,
        "title": title,
        "summary": summary,
        "content_excerpt": content_excerpt,
        "metadata": metadata,
        "published_at": str(getattr(item, "published_at", "") or ""),
    }
    return RecordEnvelope(
        record_id=_collected_item_record_id(
            fingerprint,
            source_id=str(source_id or ""),
            scope=scope,
            record_kind=record_kind,
            item_url=item_url,
            title=title,
        ),
        kind=record_kind,
        status=status,
        title=f"{_collected_item_title_prefix(record_kind)}: {title}",
        summary=summary,
        detail=content_excerpt,
        content=content_payload,
        tags=_collected_item_tags(record_kind),
        links=[],
        evidence=[],
        source="eimemory.news.collect" if record_kind == "news" else "eimemory.intake.collect",
        scope=scope,
        time=TimeRef.now(),
        provenance=provenance,
        meta={
            "intake_decision": status,
            "source_id": str(source_id or ""),
            "source_kind": str(source_kind or ""),
            "item_url": item_url,
            "fingerprint": fingerprint,
            "original_title": original_title,
            "fetch_source": item_source_kind,
            "safety": dict(metadata.get("safety") or {}) if isinstance(metadata.get("safety"), dict) else {},
        },
    )


def _collected_item_record_kind(*, source_kind: str, item_source_kind: str, metadata: dict[str, Any]) -> str:
    source_markers = {str(source_kind or "").strip().lower(), str(item_source_kind or "").strip().lower()}
    if source_markers & {"news", "rss"}:
        return "news"
    if metadata.get("feed_url"):
        return "news"
    return "knowledge_candidate"


def _collected_item_title_prefix(record_kind: str) -> str:
    return "News item" if record_kind == "news" else "Knowledge candidate"


def _collected_item_tags(record_kind: str) -> list[str]:
    return ["news", "external"] if record_kind == "news" else []


def _collected_item_record_id(
    fingerprint: str,
    *,
    source_id: str,
    scope: ScopeRef,
    record_kind: str = "knowledge_candidate",
    item_url: str = "",
    title: str = "",
) -> str:
    fallback = "\x1f".join([source_id, item_url, title])
    stable = fingerprint or sha256(fallback.encode("utf-8", errors="ignore")).hexdigest()
    prefix = "news_fetch" if record_kind == "news" else "kc_fetch"
    return f"{prefix}_{stable[:12]}_{_scope_hash(scope)}"


def _scope_hash(scope: ScopeRef) -> str:
    payload = {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]


def _collected_item_status(metadata: dict[str, Any]) -> str:
    safety = metadata.get("safety") if isinstance(metadata, dict) else None
    if not isinstance(safety, dict) or not safety:
        return "candidate"
    if safety.get("prompt_injection"):
        return "quarantined"
    return "rejected"


def _summary_from_content(content: str) -> str:
    return " ".join(str(content or "").split())[:240]


def _content_excerpt(content: str) -> str:
    return str(content or "").strip()[:1200]
