from __future__ import annotations

import re

from eimemory.knowledge.views import build_recall_view, choose_view_type, records_from_view
from eimemory.identity import extract_user_aliases, hongtu_query_scopes_with_aliases
from eimemory.living import LIVING_MEMORY_META_KEY, enrich_living_memory
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecallBundle, RecordEnvelope, ScopeRef
from eimemory.raw.retrieval import search_raw_chunks
from eimemory.scoring import ScoreContext, evaluate_memory_score, extract_memory_score, with_score_metadata
from eimemory.storage.runtime_store import RuntimeStore


class MemoryAPI:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def ingest(
        self,
        *,
        text: str,
        memory_type: str,
        title: str,
        scope: dict,
        tags: list[str] | None = None,
        source: str = "runtime",
        force_capture: bool = False,
        meta: dict | None = None,
        content: dict | None = None,
        evidence: list[str] | None = None,
        links: list[LinkRef] | None = None,
    ) -> RecordEnvelope:
        memory_type = self._normalize_ingest_memory_type(memory_type=memory_type, text=text, title=title)
        meta_payload = {"memory_type": memory_type, "force_capture": force_capture}
        if meta:
            meta_payload.update(dict(meta))
        meta_payload["memory_type"] = memory_type
        content_payload = {"text": text, "memory_type": memory_type}
        if content:
            content_payload.update(dict(content))
            content_payload.setdefault("text", text)
        content_payload["memory_type"] = memory_type
        if not isinstance(meta_payload.get(LIVING_MEMORY_META_KEY), dict):
            meta_payload[LIVING_MEMORY_META_KEY] = enrich_living_memory(
                {
                    "title": title,
                    "summary": text,
                    "detail": "",
                    "content": content_payload,
                    "meta": meta_payload,
                }
            )
        record = RecordEnvelope.create(
            kind="memory",
            title=title,
            summary=text,
            content=content_payload,
            scope=ScopeRef.from_dict(scope),
            tags=tags or [],
            links=links or [],
            evidence=evidence or [],
            source=source,
            meta=meta_payload,
        )
        score = evaluate_memory_score(
            text=str(content_payload.get("text") or text),
            title=title,
            memory_type=memory_type,
            source=source,
            force_capture=force_capture,
            context=ScoreContext(
                activity="runtime.ingest",
                source="runtime.ingest",
                entity_id=record.record_id,
                force_capture=force_capture,
                inputs=[{"memory_type": memory_type}],
            ),
            legacy_quality=dict(business_metadata(record.meta).get("quality") or {}),
        )
        record.meta = with_score_metadata(record.meta, score, preserve_quality=False)
        if business_metadata(record.meta).get("quality", {}).get("capture_decision") == "reject":
            record.status = "rejected"
            return record
        return self.store.append(record)

    def recall(
        self,
        *,
        query: str,
        scope: dict,
        task_context: dict | None = None,
        limit: int = 8,
    ) -> RecallBundle:
        normalized_query = str(query or "").strip()
        limit = max(0, min(1000, int(limit)))
        task_context = dict(task_context or {})
        recall_mode = str(task_context.get("recall_mode") or "").strip().lower()
        raw_hybrid = recall_mode == "raw_hybrid"
        scope_ref = ScopeRef.from_dict(scope)
        recall_scope_aliases = extract_user_aliases(task_context)
        query_scope_refs = hongtu_query_scopes_with_aliases(scope_ref, aliases=recall_scope_aliases)
        policy_scope_ref = query_scope_refs[0] if query_scope_refs else scope_ref
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
        active_policy = {"retrieval_policy": {}, "response_policy": {}}
        if task_type:
            active_policy = self.store.get_active_policy(task_type=task_type, scope=policy_scope_ref)
        retrieval_policy = dict(active_policy.get("retrieval_policy") or {})
        recall_profile, recall_profile_source = self._resolve_recall_profile(
            task_context=task_context,
            retrieval_policy=retrieval_policy,
        )
        profile_config = self._recall_profile_config(recall_profile)
        search_limit = max(limit * profile_config["search_multiplier"], limit)
        recall_filters = self._recall_filters_from_task_context(task_context)
        recall_filters["scoring_profile"] = recall_profile
        raw_evidence: list[dict] = []
        if raw_hybrid:
            seen_raw_ids: set[str] = set()
            for query_scope_ref in query_scope_refs:
                for evidence in search_raw_chunks(
                    self.store,
                    query=normalized_query,
                    scope=query_scope_ref,
                    task_context=task_context,
                    limit=max(limit, 1),
                ):
                    record_payload = evidence.get("record") if isinstance(evidence, dict) else {}
                    record_id = str(record_payload.get("record_id") or "") if isinstance(record_payload, dict) else ""
                    if record_id and record_id in seen_raw_ids:
                        continue
                    if record_id:
                        seen_raw_ids.add(record_id)
                    raw_evidence.append(evidence)
            raw_evidence = raw_evidence[:limit]
        report_query = self._is_report_query(normalized_query, task_context)
        items: list[RecordEnvelope] = []
        search_reports: list[dict] = []
        seen_item_ids: set[str] = set()
        for item in self._report_records_from_query(normalized_query, query_scope_refs):
            seen_item_ids.add(item.record_id)
            items.append(item)
        search_kinds = ["memory", "claim_card", "knowledge_page"]
        if report_query:
            search_kinds.append("reflection")
        for query_scope_ref in query_scope_refs:
            page_items, page_report = self.store.search_with_diagnostics(
                query=normalized_query,
                kinds=search_kinds,
                scope=query_scope_ref,
                limit=search_limit,
                recall_filters=recall_filters,
            )
            search_reports.append(dict(page_report or {}))
            for item in page_items:
                if item.record_id in seen_item_ids:
                    continue
                seen_item_ids.add(item.record_id)
                items.append(item)
        search_report = self._merge_search_reports(search_reports)
        items = [
            item
            for item in items
            if not self._is_internal_audit_record(item)
            and not self._is_default_recall_suppressed_record(item, task_context)
        ]
        report_items = [item for item in items if report_query and self._is_recallable_report_record(item)]
        preference_query = self._is_preference_query(normalized_query, task_context) and not report_query
        if preference_query:
            items = [item for item in items if self._is_preference_recall_candidate(item, normalized_query)]
        graph_expanded = 0
        related_ids: list[str] = []
        base_items = list(items)
        base_ids = {item.record_id for item in base_items}
        for item in base_items:
            for link in item.links:
                if link.target_kind in {"memory", "multimodal_memory"}:
                    related_ids.append(link.target_id)
        if related_ids and profile_config["graph_depth"] > 0:
            items = self._expand_graph_items(
                base_items=base_items,
                scope=scope_ref,
                graph_depth=profile_config["graph_depth"],
            )
            items = [
                item
                for item in items
                if not self._is_internal_audit_record(item)
                and not self._is_default_recall_suppressed_record(item, task_context)
            ]
            if preference_query:
                items = [item for item in items if self._is_preference_recall_candidate(item, normalized_query)]
        claims = [item for item in items if item.kind == "claim_card"]
        pages = [item for item in items if item.kind == "knowledge_page"]
        memories = [item for item in items if item.kind == "memory"]
        view = build_recall_view(
            view_type=choose_view_type(task_context),
            claims=claims,
            pages=pages,
            memories=memories,
            query=normalized_query,
        )
        items = self._apply_hard_recall_filters(records_from_view(view, items, limit=limit), recall_filters)
        if report_items:
            items = self._dedupe_records([*report_items, *items])[:limit]
        final_view = build_recall_view(
            view_type=view.view_type,
            claims=[item for item in items if item.kind == "claim_card"],
            pages=[item for item in items if item.kind == "knowledge_page"],
            memories=[item for item in items if item.kind == "memory"],
            query=normalized_query,
        )
        graph_expanded = sum(1 for item in items if item.record_id not in base_ids)
        rules = [
            rule
            for rule in self.store.list_records(kinds=["rule"], scope=scope_ref, status="active", limit=50)
            if not task_type or str(business_metadata(rule.meta).get("task_type") or "") == task_type
        ]
        reflections = self.store.search(query=query, kinds=["reflection"], scope=scope_ref, limit=3)
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
        if not items and retrieval_policy.get("open_unknown_on_low_confidence"):
            from eimemory.api.evolution import EvolutionAPI

            gap = EvolutionAPI(self.store).capture_recall_gap(
                query=normalized_query,
                task_context=task_context,
                scope=scope,
                policy=retrieval_policy,
            )
            reflections = [gap["reflection"], *reflections][:3]
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
                "rule_count": len(rules),
                "unknown_record_id": gap["unknown"].record_id if gap else "",
                "graph_expanded": graph_expanded,
                "retrieval_mode": str(search_report.get("retrieval_mode") or "hybrid"),
                "vector_hits": int(search_report.get("vector_hits") or 0),
                "quality_summary": self._quality_summary(items),
                "source_composition": self._source_composition(items),
                "selected_records": self._selected_record_summaries(items),
                "scoring": self._scoring_for_items(items, search_report),
                "query_scopes": [self._scope_dict(item) for item in query_scope_refs],
                "recall_scope_aliases": recall_scope_aliases,
                "recall_filters": recall_filters,
                "recall_mode": "raw_hybrid" if raw_hybrid else "structured",
                **({"raw_evidence": raw_evidence} if raw_hybrid else {}),
                "preference_query": preference_query,
                "report_query": report_query,
                "recall_view": final_view.to_dict(),
            },
        )

    def _recall_filters_from_task_context(self, task_context: dict) -> dict:
        filters = {
            "allowed_sources": self._string_list(task_context.get("allowed_sources")),
            "blocked_sources": self._string_list(task_context.get("blocked_sources")),
            "allowed_memory_types": self._string_list(task_context.get("allowed_memory_types")),
            "preferred_modalities": self._string_list(task_context.get("preferred_modalities")),
            "organs": self._string_list(task_context.get("organs")),
            "source_weights": self._source_weights(task_context.get("source_weights")),
            "living_task_context_terms": self._living_task_context_terms(task_context),
        }
        return {key: value for key, value in filters.items() if value}

    def _apply_hard_recall_filters(self, items: list[RecordEnvelope], recall_filters: dict) -> list[RecordEnvelope]:
        if not recall_filters:
            return items
        return [item for item in items if self._record_allowed_by_recall_filters(item, recall_filters)]

    def _record_allowed_by_recall_filters(self, item: RecordEnvelope, recall_filters: dict) -> bool:
        labels = self._record_filter_labels(item)
        blocked_sources = set(recall_filters.get("blocked_sources") or [])
        if blocked_sources and labels["sources"] & blocked_sources:
            return False
        allowed_sources = set(recall_filters.get("allowed_sources") or [])
        if allowed_sources and not labels["sources"] & allowed_sources:
            return False
        allowed_memory_types = set(recall_filters.get("allowed_memory_types") or [])
        if allowed_memory_types and item.kind == "memory" and labels["memory_types"] and not labels["memory_types"] & allowed_memory_types:
            return False
        organs = set(recall_filters.get("organs") or [])
        if organs and labels["organs"] and not labels["organs"] & organs:
            return False
        return True

    def _record_filter_labels(self, item: RecordEnvelope) -> dict[str, set[str]]:
        meta = business_metadata(item.meta)
        content = item.content if isinstance(item.content, dict) else {}
        sources = {str(item.source or "").strip()}
        for key in ("source", "source_channel", "communication_channel"):
            value = meta.get(key) or content.get(key)
            if value:
                sources.add(str(value).strip())
        return {
            "sources": {item for item in sources if item},
            "memory_types": {str(meta.get("memory_type") or content.get("memory_type") or "").strip()} - {""},
            "organs": {str(meta.get("organ") or content.get("organ") or "").strip()} - {""},
        }

    def _is_internal_audit_record(self, item: RecordEnvelope) -> bool:
        labels = self._record_filter_labels(item)
        title = str(item.title or "").strip().lower()
        return (
            "audit" in labels["memory_types"]
            or "ei_bridge.openclaw_feishu" in labels["sources"]
            or title == "ei-bridge openclaw command audit"
        )

    def _is_default_recall_suppressed_record(self, item: RecordEnvelope, task_context: dict) -> bool:
        if self._include_digest_pages(task_context):
            return False
        page_type = str(business_metadata(item.meta).get("page_type") or item.content.get("page_type") or "").strip().lower()
        if item.kind == "knowledge_page" and page_type in {"digest", "synthesis"}:
            return True
        if item.kind == "knowledge_page" and str(item.source or "") == "eimemory.knowledge.synthesis":
            return True
        return False

    def _normalize_ingest_memory_type(self, *, memory_type: str, text: str, title: str) -> str:
        normalized = str(memory_type or "").strip()
        if normalized and normalized != "conversation":
            return normalized
        if self._looks_like_explicit_preference(f"{title}\n{text}"):
            return "preference"
        return normalized or "fact"

    def _is_preference_query(self, query: str, task_context: dict) -> bool:
        haystack = f"{query} " + " ".join(str(task_context.get(key) or "") for key in ("intent", "goal", "task_type"))
        lowered = haystack.lower()
        return any(
            marker in haystack
            for marker in ("沟通风格", "偏好", "喜欢", "讨厌", "废话", "简洁", "极简")
        ) or any(marker in lowered for marker in ("preference", "reply style", "communication style"))

    def _is_preference_recall_candidate(self, item: RecordEnvelope, query: str) -> bool:
        if item.kind != "memory":
            return False
        text = self._record_text(item)
        memory_type = str(business_metadata(item.meta).get("memory_type") or item.content.get("memory_type") or "").strip()
        if memory_type == "preference":
            return not self._looks_like_recall_diagnostic(text, query)
        if self._looks_like_recall_diagnostic(text, query):
            return False
        return self._looks_like_explicit_preference(text)

    def _report_records_from_query(
        self,
        query: str,
        scope_refs: list[ScopeRef],
    ) -> list[RecordEnvelope]:
        records: list[RecordEnvelope] = []
        seen: set[str] = set()
        for record_id in re.findall(r"rule_evolution_[A-Za-z0-9_-]+", str(query or "")):
            for scope_ref in scope_refs:
                record = self.store.get_by_id(record_id, scope=scope_ref)
                if record is None or record.record_id in seen or not self._is_recallable_report_record(record):
                    continue
                seen.add(record.record_id)
                records.append(record)
        return records

    @staticmethod
    def _is_report_query(query: str, task_context: dict) -> bool:
        haystack = f"{query} " + " ".join(
            str(task_context.get(key) or "")
            for key in ("intent", "goal", "task_type", "report_type")
        )
        lowered = haystack.lower()
        return any(
            marker in lowered
            for marker in (
                "rule evolution",
                "rule_evolution",
                "evolve loop",
                "evolution report",
                "governance report",
                "进化报告",
                "治理报告",
            )
        )

    @staticmethod
    def _is_recallable_report_record(item: RecordEnvelope) -> bool:
        report_type = str(business_metadata(item.meta).get("report_type") or item.provenance.get("report_type") or "").strip()
        return item.kind == "reflection" and (
            report_type == "rule_evolution" or str(item.source or "") == "eimemory.rule_evolution_loop"
        )

    @staticmethod
    def _dedupe_records(items: list[RecordEnvelope]) -> list[RecordEnvelope]:
        seen: set[str] = set()
        deduped: list[RecordEnvelope] = []
        for item in items:
            if item.record_id in seen:
                continue
            seen.add(item.record_id)
            deduped.append(item)
        return deduped

    @staticmethod
    def _record_text(item: RecordEnvelope) -> str:
        content = item.content if isinstance(item.content, dict) else {}
        values = [
            item.title,
            item.summary,
            item.detail,
            content.get("text"),
            content.get("body"),
            content.get("raw_query"),
            content.get("query"),
        ]
        return "\n".join(str(value or "") for value in values if str(value or "").strip())

    @staticmethod
    def _looks_like_explicit_preference(text: str) -> bool:
        haystack = str(text or "")
        lowered = haystack.lower()
        if "沟通风格" in haystack and any(
            marker in haystack
            for marker in ("极简", "直接", "简洁", "废话", "少解释", "先给结论", "结论")
        ):
            return True
        if any(marker in haystack for marker in ("偏好", "喜欢", "讨厌", "不喜欢")) and any(
            marker in haystack
            for marker in ("极简", "直接", "简洁", "废话", "啰嗦", "长篇", "解释", "结论")
        ):
            return True
        if any(marker in haystack for marker in ("鸿哥", "用户", "我", "operator")) and any(
            marker in haystack for marker in ("不要废话", "别废话", "少废话", "讨厌废话", "先给结论", "少解释", "极简")
        ):
            return True
        return any(marker in lowered for marker in ("prefer concise", "reply style", "communication style"))

    @staticmethod
    def _looks_like_recall_diagnostic(text: str, query: str) -> bool:
        haystack = str(text or "")
        lowered = haystack.lower()
        query_text = str(query or "").strip()
        contains_query = bool(query_text and query_text in haystack)
        if not contains_query and "沟通风格" not in haystack:
            return False
        diagnostic_markers = (
            "recall",
            "诊断",
            "问题",
            "不对",
            "不合格",
            "失败",
            "返回了",
            "ranking",
            "filter",
            "news_digest",
            "新闻简报",
            "部署",
            "health",
        )
        return any(marker in lowered or marker in haystack for marker in diagnostic_markers)

    @staticmethod
    def _include_digest_pages(task_context: dict) -> bool:
        if bool(task_context.get("include_digest_pages")):
            return True
        explicit_view = str(task_context.get("recall_view") or task_context.get("memory_view") or "").strip()
        if explicit_view in {"page_centered", "freshness"}:
            return True
        haystack = " ".join(str(task_context.get(key) or "") for key in ("intent", "task_type", "goal")).lower()
        return any(marker in haystack for marker in ("research", "synthesis", "digest", "brief"))

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _living_task_context_terms(task_context: dict) -> list[str]:
        terms: list[str] = []
        for key in (
            "intent",
            "goal",
            "task_type",
            "motive",
            "desire",
            "boundary",
            "repair_needed",
        ):
            value = task_context.get(key)
            if isinstance(value, list):
                terms.extend(str(item).strip() for item in value if str(item).strip())
            elif value is not None and str(value).strip():
                terms.append(str(value).strip())
        return terms

    @staticmethod
    def _source_weights(value: object) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        weights: dict[str, float] = {}
        for key, raw_weight in value.items():
            source = str(key).strip()
            if not source:
                continue
            try:
                weights[source] = max(0.0, float(raw_weight))
            except (TypeError, ValueError):
                continue
        return weights

    def _resolve_recall_profile(self, *, task_context: dict, retrieval_policy: dict) -> tuple[str, str]:
        candidates = [
            ("task_context", task_context.get("recall_profile")),
            ("task_context.retrieval_policy", (task_context.get("retrieval_policy") or {}).get("recall_profile") if isinstance(task_context.get("retrieval_policy"), dict) else None),
            ("retrieval_policy", retrieval_policy.get("recall_profile")),
        ]
        for source, value in candidates:
            profile = self._normalize_recall_profile(value)
            if profile:
                return profile, source
        return "balanced", "default"

    def _normalize_recall_profile(self, value: object) -> str:
        profile = str(value or "").strip().lower()
        if profile in {"precision", "balanced", "exploratory"}:
            return profile
        return ""

    def _recall_profile_config(self, recall_profile: str) -> dict:
        if recall_profile == "precision":
            return {
                "search_multiplier": 2,
                "graph_depth": 0,
                "graph_policy": "disabled",
                "candidate_bias": "strict",
            }
        if recall_profile == "exploratory":
            return {
                "search_multiplier": 5,
                "graph_depth": 2,
                "graph_policy": "two_hop",
                "candidate_bias": "broad",
            }
        return {
            "search_multiplier": 3,
            "graph_depth": 1,
            "graph_policy": "one_hop",
            "candidate_bias": "balanced",
        }

    def _expand_graph_items(
        self,
        *,
        base_items: list[RecordEnvelope],
        scope: ScopeRef,
        graph_depth: int,
    ) -> list[RecordEnvelope]:
        existing_ids: set[str] = set()
        expanded_items: list[RecordEnvelope] = []
        frontier = list(base_items)
        depth = 0
        while frontier and depth < graph_depth:
            related_ids: list[str] = []
            for item in frontier:
                if item.record_id not in existing_ids:
                    expanded_items.append(item)
                    existing_ids.add(item.record_id)
                for link in item.links:
                    if link.target_kind in {"memory", "multimodal_memory"}:
                        related_ids.append(link.target_id)
            if not related_ids:
                break
            related_records = self.store.get_many_by_ids(related_ids, scope=scope)
            next_frontier: list[RecordEnvelope] = []
            for record in related_records:
                if record.record_id in existing_ids:
                    continue
                if not self._is_returnable_memory_record(record):
                    continue
                if not self._record_matches_scope(record, scope):
                    continue
                expanded_items.append(record)
                existing_ids.add(record.record_id)
                next_frontier.append(record)
            frontier = next_frontier
            depth += 1
        for item in base_items:
            if item.record_id not in existing_ids:
                expanded_items.append(item)
                existing_ids.add(item.record_id)
        return expanded_items

    def _quality_summary(self, items: list[RecordEnvelope]) -> dict:
        tiers: dict[str, int] = {}
        rejected = 0
        for item in items:
            quality = business_metadata(item.meta).get("quality") if isinstance(item.meta, dict) else {}
            if not isinstance(quality, dict):
                quality = {}
            tier = str(quality.get("quality_tier") or "unscored")
            tiers[tier] = tiers.get(tier, 0) + 1
            if quality.get("capture_decision") == "reject" or item.status == "rejected":
                rejected += 1
        return {
            "tiers": tiers,
            "rejected_returned": rejected,
        }

    def _source_composition(self, items: list[RecordEnvelope]) -> dict:
        by_kind: dict[str, int] = {}
        projected_count = 0
        projected_source_ids: list[str] = []
        for item in items:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
            meta = business_metadata(item.meta)
            if meta.get("projection_type") == "operational_knowledge":
                projected_count += 1
                source_id = str(
                    meta.get("source_record_id")
                    or item.provenance.get("source_record_id")
                    or item.content.get("source_record_id")
                    or ""
                )
                if source_id and source_id not in projected_source_ids:
                    projected_source_ids.append(source_id)
        return {
            "by_kind": by_kind,
            "projected_count": projected_count,
            "projected_source_ids": projected_source_ids,
            "knowledge_count": by_kind.get("claim_card", 0) + by_kind.get("knowledge_page", 0),
            "memory_count": by_kind.get("memory", 0),
        }

    def _selected_record_summaries(self, items: list[RecordEnvelope]) -> list[dict]:
        selected: list[dict] = []
        for item in items:
            selected.append(
                {
                    "record_id": item.record_id,
                    "kind": item.kind,
                    "status": item.status,
                    "title": item.title,
                    "source": item.source,
                    "projection_type": str(business_metadata(item.meta).get("projection_type") or ""),
                    "source_record_id": str(
                        business_metadata(item.meta).get("source_record_id")
                        or item.provenance.get("source_record_id")
                        or item.content.get("source_record_id")
                        or ""
                    ),
                }
            )
        return selected

    @staticmethod
    def _scope_dict(scope: ScopeRef) -> dict[str, str]:
        return {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        }

    def _merge_search_reports(self, reports: list[dict]) -> dict:
        merged: dict = {
            "retrieval_mode": "hybrid",
            "vector_hits": 0,
            "scored_items": [],
        }
        seen_scores: set[str] = set()
        for report in reports:
            if not isinstance(report, dict):
                continue
            if report.get("retrieval_mode"):
                merged["retrieval_mode"] = report.get("retrieval_mode")
            merged["vector_hits"] += int(report.get("vector_hits") or 0)
            for entry in report.get("scored_items") or []:
                if not isinstance(entry, dict):
                    continue
                record_id = str(entry.get("record_id") or "")
                if record_id and record_id in seen_scores:
                    continue
                if record_id:
                    seen_scores.add(record_id)
                merged["scored_items"].append(dict(entry))
        return merged

    def _is_returnable_memory_record(self, record: RecordEnvelope) -> bool:
        if record.status == "rejected":
            return False
        quality = business_metadata(record.meta).get("quality") if isinstance(record.meta, dict) else {}
        return not isinstance(quality, dict) or quality.get("capture_decision") != "reject"

    def _record_matches_scope(self, record: RecordEnvelope, scope: ScopeRef) -> bool:
        if scope.tenant_id and record.scope.tenant_id != scope.tenant_id:
            return False
        if scope.agent_id and record.scope.agent_id != scope.agent_id:
            return False
        if scope.workspace_id and record.scope.workspace_id != scope.workspace_id:
            return False
        if scope.user_id and record.scope.user_id != scope.user_id:
            return record.scope.user_id == ""
        if not scope.user_id and record.scope.user_id:
            return False
        return True

    def _scoring_for_items(self, items: list[RecordEnvelope], search_report: dict) -> list[dict]:
        scored_by_id = {
            str(entry.get("record_id")): dict(entry)
            for entry in (search_report.get("scored_items") or [])
            if isinstance(entry, dict)
        }
        scoring: list[dict] = []
        for item in items:
            entry = dict(scored_by_id.get(item.record_id) or {})
            quality = business_metadata(item.meta).get("quality") if isinstance(item.meta, dict) else {}
            if not isinstance(quality, dict):
                quality = {}
            scoring.append(
                {
                    "record_id": item.record_id,
                    "kind": item.kind,
                    "title": item.title,
                    "lexical_score": entry.get("lexical_score", 0),
                    "semantic_score": entry.get("semantic_score", 0.0),
                    "vector_score": entry.get("vector_score", 0.0),
                    "quality_score": entry.get("quality_score", quality.get("salience_score", 0.0)),
                    "quality_tier": str(quality.get("quality_tier") or "unscored"),
                    "modality_boost": entry.get("modality_boost", 0.0),
                    "final_score": entry.get("final_score", 0.0),
                    "scoring_version": entry.get("scoring_version", "memory_score.v1"),
                    "memory_score": entry.get(
                        "memory_score",
                        (
                            extract_memory_score(item.meta).to_dict()
                            if extract_memory_score(item.meta) is not None
                            else {}
                        ),
                    ),
                    "components": entry.get(
                        "components",
                        (
                            extract_memory_score(item.meta).to_dict().get("components", {})
                            if extract_memory_score(item.meta) is not None
                            else {}
                        ),
                    ),
                    "labels": entry.get(
                        "labels",
                        (
                            extract_memory_score(item.meta).to_dict().get("labels", [])
                            if extract_memory_score(item.meta) is not None
                            else []
                        ),
                    ),
                    "provenance": entry.get(
                        "provenance",
                        (
                            extract_memory_score(item.meta).to_dict().get("provenance", {})
                            if extract_memory_score(item.meta) is not None
                            else {}
                        ),
                    ),
                    "source": "search" if entry else "expanded_or_view",
                }
            )
        return scoring
