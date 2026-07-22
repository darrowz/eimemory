from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from time import perf_counter
from typing import Any, Mapping, Protocol

from eimemory.governance.memory_graph import build_evidence_refs, build_timeline, graph_route_for_query
from eimemory.identity import extract_user_aliases, hongtu_query_scopes, hongtu_query_scopes_with_aliases
from eimemory.knowledge.views import build_recall_view, choose_view_type, records_from_view
from eimemory.metadata import business_metadata
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import DEFAULT_SOURCE_ID
from eimemory.models.identity_aliases import normalize_identity_text
from eimemory.raw.retrieval import authoritative_raw_payload, search_raw_chunks
from eimemory.recall import RecallIntent, analyze_lexical_signal, classify_recall_intent
from eimemory.storage.runtime_store import RuntimeStore

from .contracts import (
    CandidateHit,
    CandidateRequest,
    CandidateSource,
    ExactScope,
    RecallPipelineSnapshot,
    freeze_value,
)
from .sqlite_source import SQLiteCandidateSource
from .fusion import FUSION_POLICY_VERSION, fuse_ranked_components, page_pool_key
from .postgres_vector import (
    PostgresVectorCandidateSource,
    _canonical_timestamp,
    candidate_record_projection_digest,
)


class RecallCallbacks(Protocol):
    """Frozen callback surface required by the governed recall orchestrator."""

    def _positive_int(self, value: object) -> int: ...
    def _prioritize_fast_query_scopes(
        self, scopes: list[ScopeRef], *, primary_scope: ScopeRef
    ) -> list[ScopeRef]: ...
    def _resolve_recall_profile(
        self, *, task_context: dict, retrieval_policy: dict
    ) -> tuple[str, str]: ...
    def _recall_profile_config(self, recall_profile: str) -> dict[str, object]: ...
    def _source_weights(self, value: object) -> dict[str, float]: ...
    def _recall_filters_from_task_context(self, task_context: dict) -> dict[str, object]: ...
    def _merge_recall_intent_filters(
        self, recall_filters: dict, recall_intent: RecallIntent
    ) -> None: ...
    def _is_report_query(self, query: str, task_context: dict) -> bool: ...
    def _allows_operational_recall(self, query: str, task_context: dict) -> bool: ...
    def _string_list(self, value: object) -> list[str]: ...
    def _default_blocked_recall_lanes(self) -> tuple[str, ...]: ...
    def _pipeline_snapshot(
        self,
        *,
        search_limit: int,
        raw_hybrid: bool,
        recall_profile: str,
        recall_profile_source: str,
        recall_intent_name: str,
        graph_depth: int,
        query_scope_count: int,
        report_query: bool,
        operational_recall_allowed: bool,
    ) -> RecallPipelineSnapshot: ...
    def _search_kinds_for_recall_intent(
        self,
        *,
        recall_intent: RecallIntent,
        report_query: bool,
        operational_recall_allowed: bool = False,
    ) -> list[str]: ...
    def _diagnostic_blocked_operational_counts(
        self,
        *,
        query: str,
        query_scope_refs: list[ScopeRef],
        recall_filters: dict,
        operational_recall_allowed: bool,
        source_ids: tuple[str, ...] | None = None,
    ) -> Counter[str]: ...
    def _filter_default_suppressed_items(
        self,
        items: list[RecordEnvelope],
        task_context: dict,
        *,
        allow_operational_recall: bool,
    ) -> tuple[list[RecordEnvelope], Counter[str]]: ...
    def _is_recallable_report_record(self, item: RecordEnvelope) -> bool: ...
    def _is_preference_query(
        self,
        query: str,
        task_context: dict,
        *,
        recall_intent: RecallIntent | None = None,
    ) -> bool: ...
    def _is_preference_recall_candidate(self, item: RecordEnvelope, query: str) -> bool: ...
    def _expand_graph_items(
        self,
        *,
        base_items: list[RecordEnvelope],
        scopes: list[ScopeRef],
        graph_depth: int,
        source_ids: tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]: ...
    def _expand_memory_edge_items(
        self,
        *,
        base_items: list[RecordEnvelope],
        scopes: list[ScopeRef],
        edge_types: list[str],
        limit: int,
        source_ids: tuple[str, ...] | None = None,
    ) -> tuple[list[RecordEnvelope], list[object]]: ...
    def _apply_hard_recall_filters_with_counts(
        self, items: list[RecordEnvelope], recall_filters: dict
    ) -> tuple[list[RecordEnvelope], Counter[str]]: ...
    def _apply_online_recall_pollution_gate(
        self, items: list[RecordEnvelope], *, allow_operational_recall: bool
    ) -> tuple[list[RecordEnvelope], Counter[str]]: ...
    def _memory_usage_adjustments(
        self, scope: ScopeRef, *, source_ids: tuple[str, ...] | None = None
    ) -> dict[tuple[str, str, str, str, str, str], dict[str, object]]: ...
    def _apply_memory_usage_feedback(
        self,
        items: list[RecordEnvelope],
        adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]],
    ) -> list[RecordEnvelope]: ...
    def _dedupe_records(self, items: list[RecordEnvelope]) -> list[RecordEnvelope]: ...
    def _matching_active_rule_recall_items(
        self,
        *,
        active_rules: list[RecordEnvelope],
        query: str,
        recall_intent: RecallIntent,
        limit: int,
    ) -> list[RecordEnvelope]: ...
    def _quality_summary(self, items: list[RecordEnvelope]) -> dict[str, object]: ...
    def _recall_pipeline_summary(
        self,
        snapshot: RecallPipelineSnapshot,
        *,
        retrieved_count: int,
        candidate_count: int,
        selected_count: int,
        blocked_counts: Counter[str],
    ) -> dict[str, object]: ...
    def _recall_intent_summary(self, recall_intent: RecallIntent) -> dict[str, object]: ...
    def _scope_dict(self, scope: ScopeRef) -> dict[str, str]: ...
    def _scoring_for_items(
        self,
        items: list[RecordEnvelope],
        search_report: dict,
        *,
        memory_usage_adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]] | None = None,
    ) -> list[dict[str, object]]: ...
    def _selected_record_summaries(self, items: list[RecordEnvelope]) -> list[dict[str, object]]: ...
    def _source_composition(self, items: list[RecordEnvelope]) -> dict[str, object]: ...
    def _memory_usage_summary(
        self,
        items: list[RecordEnvelope],
        adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]],
    ) -> dict[str, object]: ...
    def _event_graph_summary(
        self, items: list[RecordEnvelope], edges: list[object]
    ) -> dict[str, object]: ...


class GovernedRecallEngine:
    """Single owner of recall hydration, governance, ordering, and packaging."""

    name = "governed"
    policy_version = "governed-recall.v1"
    _minimum_candidate_budget = 360
    _safe_raw_boosts = frozenset(
        {
            "keyword_overlap",
            "quoted_phrase",
            "proper_noun",
            "benchmark_session_match",
            "benchmark_turn_match",
            "entity_overlap",
            "temporal_hint",
            "speaker_role",
            "preference_pattern",
            "current_fact",
            "conflict_marker",
            "temporal_currentness",
            "turn_context_neighbor",
        }
    )

    def __init__(
        self,
        *,
        store: RuntimeStore,
        candidate_source: CandidateSource,
        callbacks: RecallCallbacks | None = None,
    ) -> None:
        self.store = store
        self.candidate_source = candidate_source
        self._callbacks = callbacks

    def bind(self, callbacks: RecallCallbacks) -> None:
        if self._callbacks is not None and self._callbacks is not callbacks:
            raise ValueError("recall engine is already bound to another MemoryAPI instance")
        self._callbacks = callbacks

    def effective_identity(self) -> dict[str, object]:
        """Return the effective secret-free retrieval contract used by gates."""
        identity_fn = getattr(self.candidate_source, "effective_identity", None)
        raw_source: object = {}
        if callable(identity_fn):
            try:
                raw_source = identity_fn()
            except Exception:
                raw_source = {}
        payload: dict[str, object] = {
            "engine_type": type(self).__name__,
            "name": self.name,
            "policy_version": self.policy_version,
            "fusion_version": FUSION_POLICY_VERSION,
            "candidate_source": _sanitize_candidate_identity(
                raw_source,
                source=self.candidate_source,
            ),
        }
        payload["identity_digest"] = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    def recall(self, request: CandidateRequest) -> RecallBundle:
        started = perf_counter()
        memory = self._callbacks
        if memory is None:
            raise RuntimeError("GovernedRecallEngine must be bound to MemoryAPI before recall")
        normalized_query = request.query
        limit = request.limit
        task_context = request.task_context_dict()
        source_ids = request.source_ids
        recall_mode = str(task_context.get("recall_mode") or "").strip().lower()
        raw_hybrid = recall_mode == "raw_hybrid"
        scope_ref = request.scope.to_scope_ref()
        recall_scope_aliases = extract_user_aliases(task_context)
        exact_scope_only = task_context.get("exact_scope_only") is True
        query_scope_refs = (
            [scope_ref]
            if exact_scope_only
            else hongtu_query_scopes_with_aliases(scope_ref, aliases=recall_scope_aliases)
        )
        query_scope_limit = memory._positive_int(task_context.get("query_scope_limit"))
        if recall_mode == "fast" and query_scope_limit:
            query_scope_refs = memory._prioritize_fast_query_scopes(
                query_scope_refs,
                primary_scope=scope_ref,
            )[:query_scope_limit]
        policy_scope_ref = query_scope_refs[0] if query_scope_refs else scope_ref
        candidate_scope_groups: list[list[ScopeRef]] = []
        authorized_exact_scopes: set[ExactScope] = set()
        scope_group_by_exact: dict[ExactScope, int] = {}
        for logical_scope in query_scope_refs:
            group: list[ScopeRef] = []
            physical_scopes = [logical_scope] if exact_scope_only else hongtu_query_scopes(logical_scope)
            for physical_scope in physical_scopes:
                visible_scopes = [physical_scope] if exact_scope_only else self._visible_exact_scopes([physical_scope])
                for exact_scope_ref in visible_scopes:
                    exact_scope = ExactScope.from_scope(exact_scope_ref)
                    if exact_scope in authorized_exact_scopes:
                        continue
                    authorized_exact_scopes.add(exact_scope)
                    scope_group_by_exact[exact_scope] = len(candidate_scope_groups)
                    group.append(exact_scope_ref)
            if group:
                candidate_scope_groups.append(group)
        task_type = str(task_context.get("task_type") or "")
        if not normalized_query:
            return RecallBundle(
                items=[],
                rules=[],
                reflections=[],
                confidence=0.0,
                next_action_hint="",
                explanation={
                    "query": normalized_query,
                    "task_context": task_context,
                    "invalid_request": "empty_query",
                },
            )
        if limit <= 0:
            return RecallBundle(
                items=[],
                rules=[],
                reflections=[],
                confidence=0.0,
                next_action_hint="",
                explanation={
                    "query": normalized_query,
                    "task_context": task_context,
                    "invalid_request": "non_positive_limit",
                },
            )
        if source_ids == ():
            return RecallBundle(
                items=[],
                rules=[],
                reflections=[],
                confidence=0.0,
                next_action_hint="",
                explanation={
                    "query": normalized_query,
                    "task_context": task_context,
                    "selected_count": 0,
                    "recall_mode": "raw_hybrid" if raw_hybrid else (recall_mode or "structured"),
                    "engine_diagnostics": {
                        "engine_name": self.name,
                        "source_names": [],
                        "candidate_count": 0,
                        "candidate_limit": limit,
                        "elapsed_ms": round(max(0.0, (perf_counter() - started) * 1000.0), 3),
                        "drops": {},
                        "fallback": False,
                        "fallback_reason": "empty_source_allowlist",
                        "policy_version": self.policy_version,
                    },
                },
            )
        active_policy = {"retrieval_policy": {}, "response_policy": {}}
        if task_type:
            active_policy = self.store.get_active_policy(
                task_type=task_type,
                scope=policy_scope_ref,
                source_ids=source_ids,
            )
        policy_search = self.store.search_policy(
            normalized_query,
            scope=policy_scope_ref,
            context=task_context,
            limit=5,
            source_ids=source_ids,
        )
        retrieval_policy = dict(active_policy.get("retrieval_policy") or {})
        recall_profile, recall_profile_source = memory._resolve_recall_profile(
            task_context=task_context,
            retrieval_policy=retrieval_policy,
        )
        profile_config = memory._recall_profile_config(recall_profile)
        search_limit = max(limit * profile_config["search_multiplier"], limit)
        recall_intent = classify_recall_intent(normalized_query, task_context)
        graph_route = graph_route_for_query(
            normalized_query,
            intent_name=recall_intent.name,
            task_context=task_context,
        )
        report_query = memory._is_report_query(normalized_query, task_context)
        recall_filters = memory._recall_filters_from_task_context(task_context)
        policy_source_weights = memory._source_weights(retrieval_policy.get("source_weights"))
        if policy_source_weights:
            recall_filters["source_weights"] = {
                **policy_source_weights,
                **dict(recall_filters.get("source_weights") or {}),
            }
        memory._merge_recall_intent_filters(recall_filters, recall_intent)
        recall_filters["scoring_profile"] = recall_profile
        operational_recall_allowed = report_query or memory._allows_operational_recall(normalized_query, task_context)
        if operational_recall_allowed:
            recall_filters["include_evidence_only"] = True
            recall_filters["include_report_records"] = True
        else:
            recall_filters["blocked_recall_lanes"] = list(
                dict.fromkeys(
                    [
                        *memory._string_list(recall_filters.get("blocked_recall_lanes")),
                        *memory._default_blocked_recall_lanes(),
                    ]
                )
            )
        pipeline_snapshot = memory._pipeline_snapshot(
            search_limit=search_limit,
            raw_hybrid=raw_hybrid,
            recall_profile=recall_profile,
            recall_profile_source=recall_profile_source,
            recall_intent_name=recall_intent.name,
            graph_depth=int(profile_config["graph_depth"]),
            query_scope_count=len(query_scope_refs),
            report_query=report_query,
            operational_recall_allowed=operational_recall_allowed,
        )
        engine_drops: Counter[str] = Counter()
        raw_evidence: list[dict] = []
        if raw_hybrid and source_ids != ():
            seen_raw_refs: set[tuple[str, ExactScope, str]] = set()
            for query_scope_ref in query_scope_refs:
                for evidence in search_raw_chunks(
                    self.store,
                    query=normalized_query,
                    scope=query_scope_ref,
                    task_context=task_context,
                    source_ids=source_ids,
                    limit=max(limit, 1),
                ):
                    record_payload = evidence.get("record") if isinstance(evidence, dict) else {}
                    record_id = str(record_payload.get("record_id") or "") if isinstance(record_payload, dict) else ""
                    raw_source_id = str(record_payload.get("source_id") or "") if isinstance(record_payload, dict) else ""
                    raw_scope_payload = record_payload.get("scope") if isinstance(record_payload, dict) else None
                    if source_ids is not None and raw_source_id not in source_ids:
                        engine_drops["raw_source_not_allowed"] += 1
                        continue
                    if not record_id or not raw_source_id or not isinstance(raw_scope_payload, dict):
                        engine_drops["raw_ref_missing"] += 1
                        continue
                    raw_scope = ExactScope.from_scope(raw_scope_payload)
                    visible_scopes = {ExactScope.from_scope(item) for item in self._visible_exact_scopes([query_scope_ref])}
                    if raw_scope not in visible_scopes:
                        engine_drops["raw_scope_not_allowed"] += 1
                        continue
                    raw_record = self.store.get_by_exact_ref(
                        record_id,
                        scope=raw_scope.to_scope_ref(),
                        source_id=raw_source_id,
                    )
                    if (
                        raw_record is None
                        or raw_record.status != "active"
                        or ExactScope.from_scope(raw_record.scope) != raw_scope
                        or raw_record.source_id != raw_source_id
                    ):
                        engine_drops["raw_not_authoritative"] += 1
                        continue
                    raw_ref_key = (record_id, raw_scope, raw_source_id)
                    if raw_ref_key in seen_raw_refs:
                        continue
                    seen_raw_refs.add(raw_ref_key)
                    safe_evidence = {
                        "record": authoritative_raw_payload(raw_record),
                        "base_score": self._safe_float(evidence.get("base_score")),
                        "final_score": self._safe_float(evidence.get("final_score")),
                    }
                    boosts = evidence.get("boosts") if isinstance(evidence, dict) else {}
                    if isinstance(boosts, dict):
                        safe_evidence["boosts"] = {
                            str(key): self._safe_float(value)
                            for key, value in boosts.items()
                            if str(key) in self._safe_raw_boosts
                        }
                    raw_evidence.append(safe_evidence)
            raw_evidence = raw_evidence[:limit]
        report_query = report_query or operational_recall_allowed
        items: list[RecordEnvelope] = []
        seen_item_refs: set[tuple[str, ExactScope, str]] = set()
        seen_candidate_refs: set[tuple[str, ExactScope, str]] = set()
        report_items_direct = self._report_records_from_query(
            normalized_query,
            query_scope_refs,
            source_ids=source_ids,
        )
        for item in report_items_direct:
            seen_item_refs.add(self._record_key(item))
            items.append(item)
        search_kinds = memory._search_kinds_for_recall_intent(
            recall_intent=recall_intent,
            report_query=report_query,
            operational_recall_allowed=operational_recall_allowed,
        )
        source_reports: list[dict[str, Any]] = []
        scored_items: list[dict[str, Any]] = []
        component_hints_by_ref: dict[tuple[str, ExactScope, str], dict[str, Any]] = {}
        identity_evidence_by_ref: dict[tuple[str, ExactScope, str], set[str]] = {}
        pending_hits: list[tuple[CandidateRequest, int, int, int, CandidateHit]] = []
        for group_index, scope_group in enumerate(candidate_scope_groups):
            for scope_index, query_scope_ref in enumerate(scope_group):
                candidate_budget = max(
                    search_limit,
                    memory._positive_int(recall_filters.get("candidate_limit"))
                    or max(self._minimum_candidate_budget, search_limit * 36),
                )
                provider_limit = search_limit
                if isinstance(self.candidate_source, SQLiteCandidateSource) or (
                    getattr(self.candidate_source, "sqlite_authority", False) is True
                ):
                    provider_limit = min(candidate_budget, max(search_limit, self._minimum_candidate_budget))
                source_request = replace(
                    request,
                    scope=ExactScope.from_scope(query_scope_ref),
                    kinds=tuple(search_kinds),
                    limit=provider_limit,
                    budget=candidate_budget,
                    recall_filters=freeze_value(recall_filters),
                )
                batch = self.candidate_source.search(source_request)
                source_reports.append(batch.diagnostic_dict())
                indexed_hits = list(enumerate(batch.hits))
                indexed_hits.sort(key=lambda pair: (pair[1].source_rank, -pair[1].source_score, pair[0]))
                bounded_hits = indexed_hits[: source_request.limit]
                if len(indexed_hits) > len(bounded_hits):
                    engine_drops["provider_over_limit"] += len(indexed_hits) - len(bounded_hits)
                pending_hits.extend(
                    (source_request, group_index, scope_index, provider_index, hit)
                    for provider_index, hit in bounded_hits
                )
        pending_hits.sort(
            key=lambda entry: (
                entry[1],
                -self._safe_float(entry[4].source_score),
                entry[4].source_rank,
                entry[2],
                entry[3],
            )
        )
        for source_request, _group_index, _scope_index, _provider_index, hit in pending_hits:
            candidate_key = (hit.ref.record_id, hit.ref.scope, hit.ref.source_id)
            component_hints = component_hints_by_ref.setdefault(candidate_key, {})
            component_hints.update(hit.component_dict())
            prior_provider_rank = self._safe_int(component_hints.get("_provider_rank"), default=hit.source_rank)
            component_hints["_provider_rank"] = min(prior_provider_rank, hit.source_rank)
            identity_evidence_by_ref.setdefault(candidate_key, set()).update(
                evidence
                for evidence in hit.evidence_hints
                if evidence in {"alias_hit", "exact_title"}
            )
            if candidate_key in seen_candidate_refs:
                continue
            seen_candidate_refs.add(candidate_key)
            if hit.ref.scope not in authorized_exact_scopes:
                engine_drops["scope_not_allowed"] += 1
                continue
            if source_ids is not None and hit.ref.source_id not in source_ids:
                engine_drops["source_not_allowed"] += 1
                continue
            record = self.store.get_by_exact_ref(
                hit.ref.record_id,
                scope=hit.ref.scope.to_scope_ref(),
                source_id=hit.ref.source_id,
            )
            if record is None:
                engine_drops["missing_or_corrupt_record"] += 1
                continue
            if not self._record_matches_ref(record, hit.ref):
                engine_drops["ref_mismatch"] += 1
                continue
            candidate_digest = str(component_hints.get("_candidate_projection_digest") or "")
            candidate_updated_at = str(component_hints.get("_candidate_authoritative_updated_at") or "")
            candidate_text_chars = self._safe_int(
                component_hints.get("_candidate_projection_text_chars"), default=16_000
            )
            projection_mismatch = candidate_digest and (
                candidate_updated_at != _canonical_timestamp(record.time.updated_at)
                or candidate_digest
                != candidate_record_projection_digest(
                    record,
                    max_text_chars=candidate_text_chars,
                )
            )
            if projection_mismatch:
                engine_drops["candidate_projection_digest_mismatch"] += 1
                if component_hints.get("_candidate_sqlite_authority_duplicate") is True:
                    component_hints.pop("vector_score", None)
                    for private_key in tuple(component_hints):
                        if str(private_key).startswith("_candidate_"):
                            component_hints.pop(private_key, None)
                else:
                    continue
            if record.status != "active":
                engine_drops["inactive_record"] += 1
                continue
            quality = business_metadata(record.meta).get("quality") if isinstance(record.meta, dict) else {}
            if isinstance(quality, dict) and quality.get("capture_decision") == "reject":
                engine_drops["quality_rejected"] += 1
                continue
            if source_request.kinds and record.kind not in source_request.kinds:
                engine_drops["kind_not_allowed"] += 1
                continue
            record_key = self._record_key(record)
            if record_key in seen_item_refs:
                continue
            seen_item_refs.add(record_key)
            items.append(record)
            scored_items.append(
                {
                    "record_id": record.record_id,
                    "scope": {
                        "tenant_id": record.scope.tenant_id,
                        "agent_id": record.scope.agent_id,
                        "workspace_id": record.scope.workspace_id,
                        "user_id": record.scope.user_id,
                    },
                    "source_id": record.source_id,
                    **{
                        key: value
                        for key, value in component_hints.items()
                        if not str(key).startswith("_")
                    },
                }
            )
        search_report = self._merge_source_reports(source_reports, scored_items=scored_items)
        blocked_counts: Counter[str] = Counter(dict(search_report.get("blocked_counts") or {}))
        diagnostic_blocked_counts = memory._diagnostic_blocked_operational_counts(
            query=normalized_query,
            query_scope_refs=query_scope_refs,
            recall_filters=recall_filters,
            operational_recall_allowed=operational_recall_allowed,
            source_ids=source_ids,
        )
        blocked_counts.update(diagnostic_blocked_counts)
        items, suppressed_counts = memory._filter_default_suppressed_items(
            items,
            task_context,
            allow_operational_recall=operational_recall_allowed,
        )
        blocked_counts.update(suppressed_counts)
        report_items = [item for item in items if report_query and memory._is_recallable_report_record(item)]
        preference_query = memory._is_preference_query(
            normalized_query,
            task_context,
            recall_intent=recall_intent,
        ) and not report_query
        if preference_query:
            items = [item for item in items if memory._is_preference_recall_candidate(item, normalized_query)]
        graph_expanded = 0
        graph_edge_refs = []
        related_ids: list[str] = []
        base_items = list(items)
        base_ids = {self._record_key(item) for item in base_items}
        for item in base_items:
            for link in item.links:
                if link.target_kind in {"memory", "multimodal_memory"}:
                    related_ids.append(link.target_id)
        if related_ids and profile_config["graph_depth"] > 0:
            items = memory._expand_graph_items(
                base_items=base_items,
                scopes=query_scope_refs,
                graph_depth=profile_config["graph_depth"],
                source_ids=source_ids,
            )
            items, graph_suppressed_counts = memory._filter_default_suppressed_items(
                items,
                task_context,
                allow_operational_recall=operational_recall_allowed,
            )
            blocked_counts.update(graph_suppressed_counts)
            if preference_query:
                items = [item for item in items if memory._is_preference_recall_candidate(item, normalized_query)]
        if base_items and profile_config["graph_depth"] > 0:
            edge_items, graph_edge_refs = memory._expand_memory_edge_items(
                base_items=base_items,
                scopes=query_scope_refs,
                edge_types=list(graph_route.get("edge_types") or []),
                limit=max(limit * 2, limit),
                source_ids=source_ids,
            )
            if edge_items:
                items = memory._dedupe_records([*items, *edge_items])
                items, edge_suppressed_counts = memory._filter_default_suppressed_items(
                    items,
                    task_context,
                    allow_operational_recall=operational_recall_allowed,
                )
                blocked_counts.update(edge_suppressed_counts)
                if preference_query:
                    items = [item for item in items if memory._is_preference_recall_candidate(item, normalized_query)]
        active_rules = self.store.list_records(
            kinds=["rule"],
            scope=scope_ref,
            status="active",
            limit=100,
            source_ids=source_ids,
        )
        active_rules = [item for item in active_rules if self._record_is_exact_and_active(item)]
        active_rules = [
            item for item in active_rules if ExactScope.from_scope(item.scope) in authorized_exact_scopes
        ]
        active_rules, rule_hard_filter_counts = memory._apply_hard_recall_filters_with_counts(
            active_rules,
            recall_filters,
        )
        blocked_counts.update(rule_hard_filter_counts)
        active_rules, rule_online_gate_counts = memory._apply_online_recall_pollution_gate(
            active_rules,
            allow_operational_recall=operational_recall_allowed,
        )
        blocked_counts.update(rule_online_gate_counts)
        rules = [
            rule
            for rule in active_rules
            if not task_type or str(business_metadata(rule.meta).get("task_type") or "") == task_type
        ][:50]
        rule_recall_items = memory._matching_active_rule_recall_items(
            active_rules=active_rules,
            query=normalized_query,
            recall_intent=recall_intent,
            limit=limit,
        )

        claims = [item for item in items if item.kind == "claim_card"]
        pages = [item for item in items if item.kind == "knowledge_page"]
        memories = [item for item in items if item.kind in {"memory", "rule"}]
        view = build_recall_view(
            view_type=choose_view_type(task_context),
            claims=claims,
            pages=pages,
            memories=memories,
            query=normalized_query,
        )
        view_items = records_from_view(view, items, limit=min(5000, max(search_limit, len(items))))
        if operational_recall_allowed:
            view_items = memory._dedupe_records([*view_items, *items])
        view_items = memory._dedupe_records([*report_items, *rule_recall_items, *view_items])
        items, hard_filter_counts = memory._apply_hard_recall_filters_with_counts(
            view_items,
            recall_filters,
        )
        blocked_counts.update(hard_filter_counts)
        items, online_gate_counts = memory._apply_online_recall_pollution_gate(
            items,
            allow_operational_recall=operational_recall_allowed,
        )
        blocked_counts.update(online_gate_counts)
        memory_usage_adjustments = memory._memory_usage_adjustments(scope_ref, source_ids=source_ids)
        items = memory._apply_memory_usage_feedback(items, memory_usage_adjustments)
        items, fusion_state = self._fuse_and_pool_items(
            items=items,
            query=normalized_query,
            request=request,
            task_context=task_context,
            scope_group_by_exact=scope_group_by_exact,
            component_hints_by_ref=component_hints_by_ref,
            identity_evidence_by_ref=identity_evidence_by_ref,
            base_ids=base_ids,
            memory_usage_adjustments=memory_usage_adjustments,
        )
        items = items[:limit]
        graph_expanded = sum(1 for item in items if self._record_key(item) not in base_ids)
        selected_refs = {self._record_key(item) for item in items}
        rule_recall_promoted_count = sum(
            1 for item in rule_recall_items if self._record_key(item) in selected_refs
        )
        memory_telemetry_summary = memory._memory_usage_summary(items, memory_usage_adjustments)
        final_view = build_recall_view(
            view_type=view.view_type,
            claims=[item for item in items if item.kind == "claim_card"],
            pages=[item for item in items if item.kind == "knowledge_page"],
            memories=[item for item in items if item.kind in {"memory", "rule"}],
            query=normalized_query,
        )
        reflection_items = self.store.search(
            query=normalized_query,
            kinds=["reflection"],
            scope=scope_ref,
            limit=3,
            source_ids=source_ids,
        )
        reflections = [
            item
            for item in reflection_items
            if self._record_is_exact_and_active(item)
            and ExactScope.from_scope(item.scope) in authorized_exact_scopes
        ][:3]
        confidence = 0.0
        if items:
            confidence = 0.92 if active_policy.get("retrieval_policy", {}).get("route_hint") == "task_context_first" else 0.81
        next_hint = ""
        response_policy = dict(active_policy.get("response_policy") or {})
        if response_policy.get("next_action_hint"):
            next_hint = str(response_policy["next_action_hint"])
        elif items:
            next_hint = items[0].title.lower()
        gap = None
        retrieval_policy = dict(active_policy.get("retrieval_policy") or {})
        gap_source_allowed = source_ids is None or DEFAULT_SOURCE_ID in source_ids
        if not items and gap_source_allowed and retrieval_policy.get("open_unknown_on_low_confidence"):
            from eimemory.api.evolution import EvolutionAPI

            gap = EvolutionAPI(self.store).capture_recall_gap(
                query=normalized_query,
                task_context=task_context,
                scope=memory._scope_dict(scope_ref),
                policy=retrieval_policy,
            )
            reflections = [gap["reflection"], *reflections][:3]
        reflections, reflection_hard_filter_counts = memory._apply_hard_recall_filters_with_counts(
            reflections,
            recall_filters,
        )
        blocked_counts.update(reflection_hard_filter_counts)
        reflections, reflection_online_gate_counts = memory._apply_online_recall_pollution_gate(
            reflections,
            allow_operational_recall=operational_recall_allowed,
        )
        blocked_counts.update(reflection_online_gate_counts)
        if blocked_counts:
            recall_filters["blocked_counts"] = dict(sorted(blocked_counts.items()))
        event_graph_summary = memory._event_graph_summary(items, graph_edge_refs)
        engine_diagnostics = self._engine_diagnostics(
            source_reports=source_reports,
            drops=engine_drops,
            limit=limit,
            elapsed_ms=(perf_counter() - started) * 1000.0,
        )
        return RecallBundle(
            items=items,
            rules=rules,
            reflections=reflections,
            confidence=confidence,
            next_action_hint=next_hint,
            explanation={
                "query": normalized_query,
                "task_context": task_context,
                "recall_profile": recall_profile,
                "recall_profile_source": recall_profile_source,
                "recall_profile_params": profile_config,
                "selected_count": len(items),
                "active_policy": dict(active_policy.get("retrieval_policy") or {}),
                "policy_first": bool(policy_search.get("policy_suggestions")),
                "policy_suggestions": list(policy_search.get("policy_suggestions") or []),
                "matched_event_type": str(policy_search.get("matched_event_type") or ""),
                "rule_count": len(rules),
                "rule_recall_promoted_count": rule_recall_promoted_count,
                "unknown_record_id": gap["unknown"].record_id if gap else "",
                "graph_expanded": graph_expanded,
                "graph_route": graph_route,
                "event_graph": event_graph_summary,
                "retrieval_mode": str(search_report.get("retrieval_mode") or "hybrid"),
                "vector_hits": int(search_report.get("vector_hits") or 0),
                "quality_summary": memory._quality_summary(items),
                "source_composition": memory._source_composition(items),
                "selected_records": memory._selected_record_summaries(items),
                "evidence_refs": build_evidence_refs(items, graph_edge_refs),
                "timeline": build_timeline(items),
                "pipeline": memory._recall_pipeline_summary(
                    pipeline_snapshot,
                    retrieved_count=len(search_report.get("scored_items") or []),
                    candidate_count=len(base_items),
                    selected_count=len(items),
                    blocked_counts=blocked_counts,
                ),
                "memory_telemetry": memory_telemetry_summary,
                "online_recall_gate": {
                    "ok": True,
                    "mode": "bypassed" if operational_recall_allowed else "enforced",
                    "blocked_counts": dict(sorted(online_gate_counts.items())),
                },
                "scoring": memory._scoring_for_items(
                    items,
                    search_report,
                    memory_usage_adjustments=memory_usage_adjustments,
                ),
                "fusion": self._fusion_explanation(items, fusion_state),
                "recall_intent": memory._recall_intent_summary(recall_intent),
                "query_scopes": [memory._scope_dict(item) for item in query_scope_refs],
                "recall_scope_aliases": recall_scope_aliases,
                "recall_filters": recall_filters,
                "recall_mode": "raw_hybrid" if raw_hybrid else (recall_mode or "structured"),
                **({"raw_evidence": raw_evidence} if raw_hybrid else {}),
                "preference_query": preference_query,
                "report_query": report_query,
                "recall_view": final_view.to_dict(),
                "engine_diagnostics": engine_diagnostics,
            },
        )

    def _fuse_and_pool_items(
        self,
        *,
        items: list[RecordEnvelope],
        query: str,
        request: CandidateRequest,
        task_context: dict[str, Any],
        scope_group_by_exact: dict[ExactScope, int],
        component_hints_by_ref: dict[tuple[str, ExactScope, str], dict[str, Any]],
        identity_evidence_by_ref: dict[tuple[str, ExactScope, str], set[str]],
        base_ids: set[tuple[str, ExactScope, str]],
        memory_usage_adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]],
    ) -> tuple[list[RecordEnvelope], dict[str, Any]]:
        pre_pool_items = list(items)[:5000]
        by_token = {self._fusion_record_token(item): item for item in pre_pool_items}
        evidence_by_ref: dict[tuple[str, ExactScope, str], set[str]] = {}
        strong_evidence_by_ref: dict[tuple[str, ExactScope, str], set[str]] = {}
        group_items: dict[int, list[RecordEnvelope]] = {}
        for item in pre_pool_items:
            exact_scope = ExactScope.from_scope(item.scope)
            group = scope_group_by_exact.get(exact_scope, 10_000)
            group_items.setdefault(group, []).append(item)
            ref = self._record_key(item)
            identity_evidence = set(identity_evidence_by_ref.get(ref) or ())
            normalized_query = normalize_identity_text(query)
            evidence: set[str] = set()
            if "exact_title" in identity_evidence and normalize_identity_text(item.title) == normalized_query:
                evidence.add("exact_title")
            if "alias_hit" in identity_evidence and normalized_query in item.aliases:
                evidence.add("alias_hit")
            hints = component_hints_by_ref.get(ref) or {}
            if self._keyword_exact_match(query, item):
                evidence.add("keyword_exact")
            vector_score = self._safe_float(hints.get("vector_score"))
            if vector_score >= 0.12:
                evidence.add("vector_match")
            if ref not in base_ids:
                evidence.add("graph_path")
            evidence_by_ref[ref] = evidence
            strong_evidence = evidence & {"keyword_exact", "graph_path"}
            if vector_score >= 0.7:
                strong_evidence.add("vector_match")
            strong_evidence_by_ref[ref] = strong_evidence

        fused: list[RecordEnvelope] = []
        detail_by_ref: dict[tuple[str, ExactScope, str], dict[str, Any]] = {}
        configured_weights = task_context.get("recall_rrf_weights")
        if not isinstance(configured_weights, dict):
            configured_weights = None
        fusion_k = task_context.get("recall_rrf_k", 60)
        empty_components = [
            (name, [])
            for name in ("exact_title", "exact_alias", "keyword", "vector", "graph", "living", "usage")
        ]
        policy = fuse_ranked_components(
            empty_components,
            weights=configured_weights,
            rrf_k=fusion_k,
            limit=0,
        )
        effective_weights: dict[str, float] = dict(policy.weights)
        effective_rrf_k = policy.rrf_k
        for group in sorted(group_items):
            group_records = group_items[group]
            alias_counts = Counter(
                item.source_id
                for item in group_records
                if "alias_hit" in evidence_by_ref.get(self._record_key(item), set())
            )
            exact_title = [
                self._fusion_record_token(item)
                for item in group_records
                if "exact_title" in evidence_by_ref.get(self._record_key(item), set())
            ]
            exact_alias = [
                self._fusion_record_token(item)
                for item in group_records
                if "alias_hit" in evidence_by_ref.get(self._record_key(item), set())
                and alias_counts[item.source_id] == 1
            ]
            keyword = self._rank_component(
                group_records,
                score=lambda item: self._keyword_component_score(
                    component_hints_by_ref.get(self._record_key(item)) or {}
                ),
                eligible=lambda item: self._keyword_component_eligible(
                    component_hints_by_ref.get(self._record_key(item)) or {}
                ),
            )
            vector = self._rank_component(
                group_records,
                score=lambda item: self._safe_float(
                    (component_hints_by_ref.get(self._record_key(item)) or {}).get("vector_score")
                ),
                eligible=lambda item: self._safe_float(
                    (component_hints_by_ref.get(self._record_key(item)) or {}).get("vector_score")
                ) > 0,
            )
            graph = [
                self._fusion_record_token(item)
                for item in group_records
                if self._record_key(item) not in base_ids
            ]
            living = sorted(
                (self._fusion_record_token(item) for item in group_records),
                key=lambda token: self._living_component_key(
                    by_token[token], component_hints_by_ref.get(self._record_key(by_token[token])) or {}
                ),
            )
            usage = self._rank_component(
                group_records,
                score=lambda item: self._safe_float(
                    (memory_usage_adjustments.get(self._memory_usage_key(item)) or {}).get("adjustment")
                ),
                eligible=lambda item: bool(memory_usage_adjustments.get(self._memory_usage_key(item))),
            )
            result = fuse_ranked_components(
                [
                    ("exact_title", exact_title),
                    ("exact_alias", exact_alias),
                    ("keyword", keyword),
                    ("vector", vector),
                    ("graph", graph),
                    ("living", living),
                    ("usage", usage),
                ],
                weights=configured_weights,
                rrf_k=fusion_k,
                limit=len(group_records),
            )
            effective_weights.update(result.weights)
            effective_rrf_k = result.rrf_k
            for fused_item in result.items:
                record = by_token.get(fused_item.record_id)
                if record is None:
                    continue
                fused.append(record)
                detail_by_ref[self._record_key(record)] = {
                    "score": round(fused_item.score, 12),
                    "ranks": dict(fused_item.ranks),
                    "contributions": {
                        name: round(value, 12)
                        for name, value in fused_item.contributions.items()
                    },
                }

        pooled: list[RecordEnvelope] = []
        pool_members: dict[tuple[str, ExactScope, str], list[RecordEnvelope]] = {}
        representative_by_page: dict[str, tuple[str, ExactScope, str]] = {}
        for item in fused:
            page_key = page_pool_key(item)
            representative_ref = representative_by_page.get(page_key)
            if representative_ref is None:
                representative_ref = self._record_key(item)
                representative_by_page[page_key] = representative_ref
                pooled.append(item)
                pool_members[representative_ref] = []
            pool_members[representative_ref].append(item)

        target_source_id = request.target_source_id
        ambiguity_reasons: list[str] = []
        target_records = [item for item in pre_pool_items if item.source_id == target_source_id]
        target_identity = [
            item
            for item in target_records
            if evidence_by_ref.get(self._record_key(item), set()) & {"exact_title", "alias_hit"}
        ]
        target_identity_refs = {self._record_key(item) for item in target_identity}
        if target_source_id is None:
            create_safety = "unknown"
            ambiguity_reasons.append("target_source_omitted")
        elif request.source_ids is not None and target_source_id not in request.source_ids:
            create_safety = "unknown"
            ambiguity_reasons.append("target_source_not_searched")
        elif len(target_identity_refs) == 1:
            create_safety = "exists"
        elif len(target_identity_refs) > 1:
            create_safety = "probable"
            ambiguity_reasons.append("ambiguous_identity")
        elif any(
            strong_evidence_by_ref.get(self._record_key(item), set())
            for item in target_records
        ):
            create_safety = "probable"
            ambiguity_reasons.append("strong_non_identity_evidence")
        else:
            create_safety = "unknown"
            ambiguity_reasons.append("no_identity_evidence")

        return pooled, {
            "policy_version": FUSION_POLICY_VERSION,
            "rrf_k": effective_rrf_k,
            "weights": effective_weights,
            "pre_pool_count": len(pre_pool_items),
            "post_pool_count": len(pooled),
            "detail_by_ref": detail_by_ref,
            "evidence_by_ref": evidence_by_ref,
            "pool_members": pool_members,
            "create_safety": create_safety,
            "target_source_id": target_source_id,
            "target_identity_refs": target_identity_refs,
            "ambiguity_reasons": ambiguity_reasons,
        }

    def _fusion_explanation(self, items: list[RecordEnvelope], state: dict[str, Any]) -> dict[str, Any]:
        evidence_order = ("alias_hit", "exact_title", "keyword_exact", "vector_match", "graph_path")
        detail_by_ref = state.get("detail_by_ref") or {}
        evidence_by_ref = state.get("evidence_by_ref") or {}
        pool_members = state.get("pool_members") or {}
        target_identity_refs = state.get("target_identity_refs") or set()
        target_source_id = state.get("target_source_id")
        selected: list[dict[str, Any]] = []
        for item in items:
            ref = self._record_key(item)
            members = list(pool_members.get(ref) or [item])
            member_refs = [self._record_key(member) for member in members]
            evidence = {
                value
                for member_ref in member_refs
                for value in evidence_by_ref.get(member_ref, set())
                if value in evidence_order
            }
            aggregate_contributions: Counter[str] = Counter()
            aggregate_score = 0.0
            for member_ref in member_refs:
                member_detail = detail_by_ref.get(member_ref) or {}
                aggregate_score += self._safe_float(member_detail.get("score"))
                aggregate_contributions.update(
                    {
                        str(name): self._safe_float(value)
                        for name, value in dict(member_detail.get("contributions") or {}).items()
                    }
                )
            if target_source_id is None or item.source_id != target_source_id:
                item_safety = "unknown"
            elif state.get("create_safety") == "exists" and any(
                member_ref in target_identity_refs for member_ref in member_refs
            ):
                item_safety = "exists"
            elif state.get("create_safety") == "probable" and evidence:
                item_safety = "probable"
            else:
                item_safety = "unknown"
            detail = detail_by_ref.get(ref) or {}
            selected.append(
                {
                    "record_id": item.record_id,
                    "source_id": item.source_id,
                    "page_key": page_pool_key(item),
                    "evidence": [value for value in evidence_order if value in evidence],
                    "create_safety": item_safety,
                    "score": self._safe_float(detail.get("score")),
                    "ranks": dict(detail.get("ranks") or {}),
                    "contributions": dict(detail.get("contributions") or {}),
                    "chunk_count": len(members),
                    "member_record_ids": sorted(member.record_id for member in members)[:64],
                    "member_record_ids_truncated": max(0, len(members) - 64),
                    "aggregate_score": round(aggregate_score, 12),
                    "aggregate_contributions": {
                        name: round(aggregate_contributions[name], 12)
                        for name in sorted(aggregate_contributions)
                    },
                }
            )
        return {
            "policy_version": str(state.get("policy_version") or FUSION_POLICY_VERSION),
            "rrf_k": int(state.get("rrf_k") or 60),
            "weights": dict(sorted(dict(state.get("weights") or {}).items())),
            "ranking_change": "intentional_rrf_replaces_hand_weight_order",
            "pre_pool_count": int(state.get("pre_pool_count") or 0),
            "post_pool_count": int(state.get("post_pool_count") or 0),
            "create_safety": str(state.get("create_safety") or "unknown"),
            "target_source_id": str(target_source_id or ""),
            "ambiguity_reasons": list(state.get("ambiguity_reasons") or ()),
            "selected": selected,
        }

    @staticmethod
    def _keyword_exact_match(query: str, record: RecordEnvelope) -> bool:
        if not normalize_identity_text(query):
            return False
        bounded_text = " ".join(
            str(value or "")[:2048]
            for value in (
                record.title,
                record.summary,
                record.detail,
                record.content.get("text") if isinstance(record.content, dict) else "",
                record.content.get("excerpt") if isinstance(record.content, dict) else "",
            )
        )
        signal = analyze_lexical_signal(
            query,
            bounded_text,
            record_kind=record.kind,
            record_source=record.source,
        )
        query_signal = analyze_lexical_signal(query, query)
        required_terms = set(query_signal.token_hits) | set(query_signal.entity_hits) | set(query_signal.version_hits)
        matched_terms = set(signal.token_hits) | set(signal.entity_hits) | set(signal.version_hits)
        return bool(required_terms) and required_terms.issubset(matched_terms)

    def _rank_component(self, items, *, score, eligible) -> list[str]:
        ranked = [item for item in items if eligible(item)]
        ranked.sort(key=lambda item: (-self._safe_float(score(item)), self._fusion_record_token(item)))
        return [self._fusion_record_token(item) for item in ranked]

    def _living_component_key(self, item: RecordEnvelope, hints: dict[str, Any]) -> tuple[float, float, float, str]:
        living = hints.get("living_score_adjustments")
        living_score = self._safe_float(living.get("total_adjustment")) if isinstance(living, dict) else 0.0
        quality_score = self._safe_float(hints.get("quality_score"))
        try:
            parsed = datetime.fromisoformat(str(item.time.updated_at or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            timestamp_rank = (
                parsed.toordinal() * 86_400_000_000
                + parsed.hour * 3_600_000_000
                + parsed.minute * 60_000_000
                + parsed.second * 1_000_000
                + parsed.microsecond
            )
        except (TypeError, ValueError, OverflowError, OSError):
            timestamp_rank = 0
        return (-living_score, -quality_score, -float(timestamp_rank), self._fusion_record_token(item))

    def _keyword_component_score(self, hints: dict[str, Any]) -> float:
        lexical_score = self._safe_float(hints.get("lexical_score"))
        if lexical_score > 0:
            return lexical_score
        provider_rank = self._safe_int(hints.get("_provider_rank"), default=0)
        return (1.0 / provider_rank) if self._keyword_component_eligible(hints) and provider_rank else 0.0

    def _keyword_component_eligible(self, hints: dict[str, Any]) -> bool:
        if self._safe_float(hints.get("lexical_score")) > 0:
            return True
        return (
            self._safe_int(hints.get("_provider_rank"), default=0) > 0
            and "vector_score" not in hints
            and not bool(hints.get("identity_indexed"))
        )

    @staticmethod
    def _fusion_record_token(record: RecordEnvelope) -> str:
        scope = record.scope
        canonical = json.dumps(
            [
                str(record.record_id or ""),
                str(scope.tenant_id or "default"),
                str(scope.agent_id or ""),
                str(scope.workspace_id or ""),
                str(scope.user_id or ""),
                str(record.source_id or "default"),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "exact-ref.v1:" + sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _record_key(record: RecordEnvelope) -> tuple[str, ExactScope, str]:
        return (record.record_id, ExactScope.from_scope(record.scope), record.source_id)

    @staticmethod
    def _memory_usage_key(record: RecordEnvelope) -> tuple[str, str, str, str, str, str]:
        scope = record.scope
        return (
            record.record_id,
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            record.source_id,
        )

    @staticmethod
    def _record_matches_ref(record: RecordEnvelope, ref: Any) -> bool:
        return (
            record.record_id == ref.record_id
            and ExactScope.from_scope(record.scope) == ref.scope
            and record.source_id == ref.source_id
        )

    def _record_is_exact_and_active(self, record: RecordEnvelope) -> bool:
        if record.status != "active":
            return False
        hydrated = self.store.get_by_exact_ref(
            record.record_id,
            scope=record.scope,
            source_id=record.source_id,
        )
        return hydrated is not None and self._record_key(hydrated) == self._record_key(record) and hydrated.status == "active"

    def _visible_exact_scopes(self, scopes: list[ScopeRef]) -> list[ScopeRef]:
        visible: list[ScopeRef] = []
        seen: set[tuple[str, str, str, str]] = set()
        for scope in scopes:
            candidates = [scope]
            if scope.user_id:
                candidates.append(
                    ScopeRef(
                        tenant_id=scope.tenant_id,
                        agent_id=scope.agent_id,
                        workspace_id=scope.workspace_id,
                        user_id="",
                    )
                )
            for candidate in candidates:
                key = (candidate.tenant_id, candidate.agent_id, candidate.workspace_id, candidate.user_id)
                if key not in seen:
                    seen.add(key)
                    visible.append(candidate)
        return visible

    def _resolve_visible_record(
        self,
        record_id: str,
        scopes: list[ScopeRef],
        source_ids: tuple[str, ...] | None,
    ) -> RecordEnvelope | None:
        if not record_id or source_ids == ():
            return None
        for exact_scope in self._visible_exact_scopes(scopes):
            for record in self.store.list_by_record_id_exact_scope(
                record_id,
                scope=exact_scope,
                source_ids=source_ids,
            ):
                if self._record_is_exact_and_active(record):
                    return record
        return None

    def _report_records_from_query(
        self,
        query: str,
        scopes: list[ScopeRef],
        *,
        source_ids: tuple[str, ...] | None,
    ) -> list[RecordEnvelope]:
        import re

        records: list[RecordEnvelope] = []
        seen: set[tuple[str, ExactScope, str]] = set()
        for record_id in re.findall(r"rule_evolution_[A-Za-z0-9_-]+", str(query or "")):
            record = self._resolve_visible_record(record_id, scopes, source_ids)
            if record is None or not self._callbacks._is_recallable_report_record(record):
                continue
            key = self._record_key(record)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        return records

    def _merge_source_reports(self, source_reports: list[dict[str, Any]], *, scored_items: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "retrieval_mode": "recall_index_hybrid",
            "vector_hits": 0,
            "blocked_counts": {},
            "scored_items": scored_items,
        }
        trusted_diagnostics = isinstance(self.candidate_source, SQLiteCandidateSource)
        for report in source_reports:
            if not trusted_diagnostics:
                continue
            retrieval_mode = str(report.get("retrieval_mode") or "")
            if retrieval_mode in {"recall_index_hybrid", "hybrid", "hybrid_vector", "structured"}:
                merged["retrieval_mode"] = retrieval_mode
            merged["vector_hits"] += self._safe_nonnegative_int(report.get("vector_hits"))
            for key, value in dict(report.get("drops") or {}).items():
                safe_key = self._safe_diagnostic_label(key)
                if not safe_key:
                    continue
                merged["blocked_counts"][safe_key] = self._safe_nonnegative_int(
                    merged["blocked_counts"].get(safe_key)
                ) + self._safe_nonnegative_int(value)
        return merged

    def _engine_diagnostics(
        self,
        *,
        source_reports: list[dict[str, Any]],
        drops: Counter[str],
        limit: int,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        fallback_reports = [item for item in source_reports if bool(item.get("fallback"))]
        source_name = self._safe_diagnostic_label(getattr(self.candidate_source, "name", ""))
        if not source_name:
            source_name = type(self.candidate_source).__name__[:64]
        trusted_diagnostics = isinstance(self.candidate_source, SQLiteCandidateSource)
        fallback_reason = ""
        if fallback_reports:
            reported_reason = str(fallback_reports[0].get("fallback_reason") or "")
            fallback_reason = reported_reason if trusted_diagnostics and reported_reason == "legacy_scan" else "candidate_source_fallback"
        return {
            "engine_name": self.name,
            "source_names": [source_name] if source_reports else [],
            "candidate_count": sum(self._safe_nonnegative_int(item.get("candidate_count")) for item in source_reports),
            "candidate_limit": max(
                [self._safe_nonnegative_int(item.get("candidate_limit")) for item in source_reports] or [limit]
            ),
            "elapsed_ms": round(max(0.0, elapsed_ms), 3),
            "drops": {str(key): int(value) for key, value in list(sorted(drops.items()))[:8]},
            "fallback": bool(fallback_reports),
            "fallback_reason": fallback_reason,
            "policy_version": self.policy_version,
        }

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_nonnegative_int(value: Any) -> int:
        try:
            return max(0, min(1_000_000, int(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _safe_diagnostic_label(value: Any) -> str:
        label = str(value or "")
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", label):
            return label
        return ""


def _sanitize_candidate_identity(raw: object, *, source: CandidateSource) -> dict[str, object]:
    trusted_source = isinstance(source, (SQLiteCandidateSource, PostgresVectorCandidateSource))
    value = raw if trusted_source and isinstance(raw, Mapping) else {}
    result: dict[str, object] = {
        "candidate_source_type": type(source).__name__[:64],
        "name": _public_identity_label(getattr(source, "name", ""), maximum=64) if trusted_source else "",
        "policy_version": _public_identity_label(getattr(source, "policy_version", ""), maximum=64) if trusted_source else "",
        "sqlite_authority": value.get("sqlite_authority") is True,
        "authority_revision": _numeric_identity(value.get("authority_revision")),
    }
    config_fingerprint = _sha256_identity(value.get("config_fingerprint"))
    if config_fingerprint:
        result["config_fingerprint"] = config_fingerprint
    projection = _sha256_identity(value.get("projection_fingerprint"))
    if projection:
        result["projection_fingerprint"] = projection
    for flag in ("enabled", "configured"):
        if flag in value:
            result[flag] = value.get(flag) is True
    embedding = value.get("embedding")
    if isinstance(embedding, Mapping):
        result["embedding"] = {
            "provider_type": _public_identity_label(embedding.get("provider_type"), maximum=64),
            "model": _public_identity_label(embedding.get("model"), maximum=256),
            "fingerprint": _sha256_identity(embedding.get("fingerprint")),
        }
    postgres = value.get("postgres")
    if isinstance(postgres, Mapping):
        state = str(postgres.get("state") or "")
        circuit = str(postgres.get("circuit") or "")
        result["postgres"] = {
            "state": state if state in {"available", "bypassed", "disabled", "not_configured"} else "bypassed",
            "committed_watermark": _public_identity_label(postgres.get("committed_watermark"), maximum=256),
            "index_revision": _numeric_identity(postgres.get("index_revision")),
            "circuit": circuit if circuit in {"closed", "open", "half_open"} else "open",
            "bypass_reason": _public_identity_label(postgres.get("bypass_reason"), maximum=80),
        }
    return result


def _public_identity_label(value: object, *, maximum: int) -> str:
    text = str(value or "")[:maximum]
    return text if re.fullmatch(r"[A-Za-z0-9_.:/-]{0," + str(maximum) + r"}", text) else ""


def _sha256_identity(value: object) -> str:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _numeric_identity(value: object) -> str:
    text = str(value or "")[:64]
    return text if text.isdigit() else ""
