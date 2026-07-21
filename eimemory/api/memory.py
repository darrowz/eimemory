from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import re

from eimemory.intake.registry import SourceRegistry
from eimemory.knowledge.safety import evaluate_knowledge_safety
from eimemory.knowledge.views import build_recall_view, choose_view_type, records_from_view
from eimemory.identity import (
    HONGTU_AGENT_ID,
    HONGTU_WORKSPACE_ID,
    extract_user_aliases,
    hongtu_query_scopes_with_aliases,
)
from eimemory.living import LIVING_MEMORY_META_KEY, enrich_living_memory, refresh_living_quality_snapshot
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecallBundle, RecordEnvelope, ScopeRef
from eimemory.recall import RecallIntent
from eimemory.scoring import ScoreContext, evaluate_memory_score, extract_memory_score, with_score_metadata
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.retrieval.contracts import CandidateRequest, CandidateSource, RecallEngine, RecallPipelineSnapshot
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource


_KNOWLEDGE_CONTENT_DEDUPE_KINDS = {"knowledge_page", "claim_card", "paper_source", "paper_extract"}
_MAX_RECORDS_PER_KNOWLEDGE_SOURCE = 2
_DEFAULT_BLOCKED_RECALL_LANES = ("run_log", "audit_record", "incident_report", "evolution_artifact")
_MEMORY_USAGE_TELEMETRY_REPORT_TYPE = "memory_usage_telemetry"
_MEMORY_USAGE_TELEMETRY_SCHEMA = "memory_usage_telemetry.v1"
_PROACTIVE_USAGE_STATES = frozenset(
    {"volunteered", "injected", "used", "not_used", "suppressed", "rejected"}
)
_PROACTIVE_CITATION = re.compile(r"pm:[0-9a-f]{20}")
_MEMORY_USAGE_PROMOTION_WEIGHT = 0.08
_MEMORY_USAGE_REJECTION_WEIGHT = -0.12
_MEMORY_USAGE_MAX_ADJUSTMENT = 0.30
_RECALL_PIPELINE_SCHEMA = "recall_pipeline.v1"
_RECALL_PIPELINE_PHASES = ("prepare", "retrieve", "graph_expand", "score_filter", "package")
_DEFAULT_PREFERENCE_QUERY_MARKERS = (
    "preference",
    "reply style",
    "communication style",
    "operator preference",
    "user preference",
    "偏好",
    "喜欢",
    "讨厌",
    "沟通风格",
    "回复风格",
    "废话",
    "简洁",
    "极简",
    "直接",
)
_DEFAULT_PREFERENCE_QUERY_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in sorted(_DEFAULT_PREFERENCE_QUERY_MARKERS, key=len, reverse=True)),
    re.IGNORECASE,
)
_RECALL_LANE_MEMORY_TYPE_ALIASES = {
    "audit": "audit_record",
    "audit_record": "audit_record",
    "incident": "incident_report",
    "incident_report": "incident_report",
    "log": "run_log",
    "run_log": "run_log",
    "runtime_log": "run_log",
    "evolution": "evolution_artifact",
    "evolution_artifact": "evolution_artifact",
    "preference": "user_preference",
    "user_preference": "user_preference",
    "rule": "system_rule",
    "system_rule": "system_rule",
    "fact": "durable_fact",
    "durable_fact": "durable_fact",
    "knowledge": "external_knowledge",
    "external_knowledge": "external_knowledge",
    "conversation": "task_context",
    "context": "task_context",
    "task_context": "task_context",
}


def _capture_warnings(score) -> list[dict[str, object]]:
    payload = score.to_dict() if hasattr(score, "to_dict") else {}
    explanation = payload.get("explanation", {}) if isinstance(payload, dict) else {}
    risk_labels = explanation.get("risk_labels") if isinstance(explanation, dict) else []
    warnings: list[dict[str, object]] = []
    components = payload.get("components", {}) if isinstance(payload, dict) else {}
    risk_penalty = components.get("risk_penalty", {}) if isinstance(components, dict) else {}
    risk_evidence = risk_penalty.get("evidence", {}) if isinstance(risk_penalty, dict) else {}
    thin_or_noisy = bool(isinstance(risk_evidence, dict) and risk_evidence.get("thin_or_noisy"))
    if thin_or_noisy or (isinstance(risk_labels, list) and "thin_or_noisy" in risk_labels):
        warnings.append(
            {
                "code": "thin_or_noisy_risk",
                "message": "memory candidate was rejected by the capture quality gate; pass force_capture to persist deliberate short facts",
            }
        )
    if not warnings:
        warnings.append(
            {
                "code": "capture_rejected",
                "message": "memory candidate was rejected by the capture quality gate",
            }
        )
    return warnings


class MemoryAPI:
    def __init__(
        self,
        store: RuntimeStore,
        *,
        source_registry: SourceRegistry | None = None,
        candidate_source: CandidateSource | None = None,
        recall_engine: RecallEngine | None = None,
    ) -> None:
        self.store = store
        self.source_registry = source_registry
        if candidate_source is not None and recall_engine is not None:
            raise ValueError("candidate_source and recall_engine cannot both be supplied")
        engine_store = getattr(recall_engine, "store", store)
        if recall_engine is not None and engine_store is not store:
            raise ValueError("recall engine and MemoryAPI must use the same store")
        source = candidate_source or SQLiteCandidateSource(store)
        self.recall_engine = recall_engine or GovernedRecallEngine(
            store=store,
            candidate_source=source,
            callbacks=self,
        )
        bind = getattr(self.recall_engine, "bind", None)
        if callable(bind):
            bind(self)

    def ingest(
        self,
        *,
        text: str,
        memory_type: str,
        title: str,
        scope: dict,
        tags: list[str] | None = None,
        source: str = "runtime",
        source_id: str = "default",
        force_capture: bool = False,
        meta: dict | None = None,
        content: dict | None = None,
        evidence: list[str] | None = None,
        links: list[LinkRef] | None = None,
        record_id: str = "",
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
        provided_living = isinstance(meta_payload.get(LIVING_MEMORY_META_KEY), dict)
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
            source_id=source_id,
            meta=meta_payload,
        )
        if str(record_id or "").strip():
            record.record_id = str(record_id).strip()
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
        existing_living = record.meta.get(LIVING_MEMORY_META_KEY)
        if provided_living and isinstance(existing_living, dict):
            record.meta[LIVING_MEMORY_META_KEY] = refresh_living_quality_snapshot(existing_living, meta=record.meta)
        else:
            record.meta[LIVING_MEMORY_META_KEY] = enrich_living_memory(record, meta=record.meta)
        if business_metadata(record.meta).get("quality", {}).get("capture_decision") == "reject":
            record.status = "rejected"
            record.meta["capture_warnings"] = _capture_warnings(score)
            return record
        return self.store.append(record)

    def record_memory_usage(
        self,
        *,
        query_id: str,
        scope: dict,
        used_record_ids: list[str] | None = None,
        rejected_record_ids: list[str] | None = None,
        query: str = "",
        source: str = "openclaw.gateway",
        source_id: str = "default",
        proactive_state: str = "",
        session_id: str = "",
        transition_id: str = "",
        policy_version: str = "",
        release_identity: dict | None = None,
        control_cohort: bool = False,
        control_suppressed: bool = False,
        citation: str = "",
        decision_id: str = "",
        record_id: str = "",
        pair_id: str = "",
        meta: dict | None = None,
        persist: bool = True,
    ) -> RecordEnvelope:
        normalized_query_id = str(query_id or "").strip()
        if not normalized_query_id:
            raise ValueError("query_id is required for memory usage telemetry")

        scope_ref = ScopeRef.from_dict(scope)
        used_ids = self._unique_record_ids(used_record_ids)
        rejected_ids = self._unique_record_ids(rejected_record_ids)
        normalized_state = str(proactive_state or "").strip().lower()
        normalized_transition_id = str(transition_id or "").strip()
        normalized_decision_id = str(decision_id or "").strip()
        normalized_record_id = str(record_id or "").strip()
        normalized_citation = str(citation or "").strip()
        normalized_policy = str(policy_version or "").strip()
        release_payload = {
            key: str((release_identity or {}).get(key) or "")
            for key in (
                "release_commit",
                "release_version",
                "deployment_receipt_id",
                "release_session_id",
            )
        }
        if normalized_state and normalized_state not in _PROACTIVE_USAGE_STATES:
            raise ValueError("unsupported proactive usage transition state")
        if normalized_state and not all(
            (normalized_transition_id, normalized_decision_id, normalized_record_id, normalized_citation, normalized_policy)
        ):
            raise ValueError("proactive usage transitions require server-bound decision, record, citation, policy, and transition IDs")
        if normalized_state and (
            _PROACTIVE_CITATION.fullmatch(normalized_citation) is None
            or not all(release_payload.values())
        ):
            raise ValueError("proactive usage transitions require an opaque citation and complete release identity")
        schema_version = "memory_usage_telemetry.v2" if normalized_state else _MEMORY_USAGE_TELEMETRY_SCHEMA
        idempotency_key = sha256(
            "|".join(
                [
                    schema_version,
                    scope_ref.tenant_id,
                    scope_ref.agent_id,
                    scope_ref.workspace_id,
                    scope_ref.user_id,
                    normalized_query_id,
                    normalized_state,
                    normalized_transition_id,
                    normalized_decision_id,
                    normalized_record_id,
                    normalized_citation,
                    str(source_id or "default"),
                    normalized_policy,
                    release_payload["release_commit"],
                    release_payload["deployment_receipt_id"],
                ]
            ).encode("utf-8")
        ).hexdigest()
        if persist:
            existing = self.store.get_by_idempotency_key(
                kinds=["feedback"],
                scope=scope_ref,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return existing

        meta_payload = {
            "report_type": _MEMORY_USAGE_TELEMETRY_REPORT_TYPE,
            "schema_version": _MEMORY_USAGE_TELEMETRY_SCHEMA,
            "query_id": normalized_query_id,
            "idempotency_key": idempotency_key,
            "used_count": len(used_ids),
            "rejected_count": len(rejected_ids),
        }
        if normalized_state:
            meta_payload.update(
                {
                    "schema_version": "memory_usage_telemetry.v2",
                    "proactive_query_id": normalized_query_id,
                    "proactive_state": normalized_state,
                    "session_id": str(session_id or ""),
                    "transition_id": normalized_transition_id,
                    "policy_version": normalized_policy,
                    "control_cohort": bool(control_cohort),
                    "control_suppressed": bool(control_suppressed),
                    "citation": normalized_citation,
                    "decision_id": normalized_decision_id,
                    "record_id": normalized_record_id,
                    "pair_id": str(pair_id or ""),
                    **release_payload,
                }
            )
        if meta:
            meta_payload.update(dict(meta))
        meta_payload.update(
            {
                "report_type": _MEMORY_USAGE_TELEMETRY_REPORT_TYPE,
                "schema_version": schema_version,
                "query_id": normalized_query_id,
                "idempotency_key": idempotency_key,
                "used_count": len(used_ids),
                "rejected_count": len(rejected_ids),
            }
        )
        if normalized_state:
            meta_payload.update(
                {
                    "proactive_query_id": normalized_query_id,
                    "proactive_state": normalized_state,
                    "session_id": str(session_id or ""),
                    "transition_id": normalized_transition_id,
                    "policy_version": normalized_policy,
                    "control_cohort": bool(control_cohort),
                    "control_suppressed": bool(control_suppressed),
                    "citation": normalized_citation,
                    "decision_id": normalized_decision_id,
                    "record_id": normalized_record_id,
                    "pair_id": str(pair_id or ""),
                    **release_payload,
                }
            )
        content = {
            "report_type": _MEMORY_USAGE_TELEMETRY_REPORT_TYPE,
            "schema_version": _MEMORY_USAGE_TELEMETRY_SCHEMA,
            "query_id": normalized_query_id,
            "query": str(query or ""),
            "used_record_ids": used_ids,
            "rejected_record_ids": rejected_ids,
            "used_count": len(used_ids),
            "rejected_count": len(rejected_ids),
        }
        if normalized_state:
            content.update(
                {
                    "schema_version": "memory_usage_telemetry.v2",
                    "proactive_query_id": normalized_query_id,
                    "proactive_state": normalized_state,
                    "session_id": str(session_id or ""),
                    "transition_id": normalized_transition_id,
                    "policy_version": normalized_policy,
                    "control_cohort": bool(control_cohort),
                    "control_suppressed": bool(control_suppressed),
                    "citation": normalized_citation,
                    "decision_id": normalized_decision_id,
                    "record_id": normalized_record_id,
                    "pair_id": str(pair_id or ""),
                    **release_payload,
                }
            )
        links = [
            *(LinkRef(relation="used_memory", target_kind="record", target_id=record_id) for record_id in used_ids),
            *(
                LinkRef(relation="rejected_memory", target_kind="record", target_id=record_id)
                for record_id in rejected_ids
            ),
        ]
        record = RecordEnvelope.create(
            kind="feedback",
            title=f"Memory usage telemetry {normalized_query_id}",
            summary=f"Memory usage telemetry: used={len(used_ids)} rejected={len(rejected_ids)}",
            content=content,
            scope=scope_ref,
            source=source,
            source_id=source_id,
            tags=["memory_usage_telemetry"],
            links=links,
            meta=meta_payload,
        )
        if not persist:
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
        """Build the immutable public request and delegate all recall control flow."""

        context = dict(task_context or {})
        raw_source_ids = context.get("source_ids") if "source_ids" in context else None
        if raw_source_ids is not None and not isinstance(raw_source_ids, (list, tuple)):
            raise ValueError("source_ids must be an allowlist")
        request = CandidateRequest.create(
            query=str(query or "").strip(),
            scope=ScopeRef.from_dict(scope),
            source_ids=raw_source_ids,
            target_source_id=context.get("target_source_id"),
            limit=max(0, min(1000, int(limit))),
            task_context=context,
        )
        return self.recall_engine.recall(request)

    @staticmethod
    def _pipeline_snapshot(
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
    ) -> RecallPipelineSnapshot:
        return RecallPipelineSnapshot(
            search_limit=search_limit,
            raw_hybrid=raw_hybrid,
            recall_profile=recall_profile,
            recall_profile_source=recall_profile_source,
            recall_intent_name=recall_intent_name,
            graph_depth=graph_depth,
            query_scope_count=query_scope_count,
            report_query=report_query,
            operational_recall_allowed=operational_recall_allowed,
        )

    @staticmethod
    def _default_blocked_recall_lanes() -> tuple[str, ...]:
        return _DEFAULT_BLOCKED_RECALL_LANES

    @staticmethod
    def _unique_record_ids(record_ids: list[str] | None) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for record_id in record_ids or []:
            normalized = str(record_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    @staticmethod
    def _bounded_score(value: object, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0.0, min(1.0, parsed))

    @staticmethod
    def _bounded_adjustment(value: object) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(-_MEMORY_USAGE_MAX_ADJUSTMENT, min(_MEMORY_USAGE_MAX_ADJUSTMENT, parsed))

    def _memory_usage_adjustments(
        self,
        scope: ScopeRef,
        *,
        source_ids: tuple[str, ...] | None = None,
    ) -> dict[tuple[str, str, str, str, str, str], dict[str, object]]:
        feedback_records = self.store.list_records_by_meta_value(
            kinds=["feedback"],
            scope=scope,
            meta_key="report_type",
            meta_value=_MEMORY_USAGE_TELEMETRY_REPORT_TYPE,
            status="active",
            limit=500,
            source_ids=source_ids,
        )
        if feedback_records is None:
            feedback_records = self.store.list_records(
                kinds=["feedback"],
                scope=scope,
                status="active",
                limit=500,
                source_ids=source_ids,
            )

        adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]] = {}
        for feedback in feedback_records:
            meta = business_metadata(feedback.meta)
            content = feedback.content if isinstance(feedback.content, dict) else {}
            report_type = str(meta.get("report_type") or content.get("report_type") or "")
            if report_type != _MEMORY_USAGE_TELEMETRY_REPORT_TYPE:
                continue
            for record_id in self._unique_record_ids(self._string_list(content.get("used_record_ids"))):
                usage_key = self._memory_usage_key_for_namespace(
                    record_id,
                    scope=feedback.scope,
                    source_id=feedback.source_id,
                )
                entry = adjustments.setdefault(
                    usage_key,
                    {"adjustment": 0.0, "used_count": 0, "rejected_count": 0, "latest_feedback_at": ""},
                )
                entry["adjustment"] = float(entry["adjustment"]) + _MEMORY_USAGE_PROMOTION_WEIGHT
                entry["used_count"] = int(entry["used_count"]) + 1
                entry["latest_feedback_at"] = max(str(entry["latest_feedback_at"]), str(feedback.time.updated_at or ""))
            for record_id in self._unique_record_ids(self._string_list(content.get("rejected_record_ids"))):
                usage_key = self._memory_usage_key_for_namespace(
                    record_id,
                    scope=feedback.scope,
                    source_id=feedback.source_id,
                )
                entry = adjustments.setdefault(
                    usage_key,
                    {"adjustment": 0.0, "used_count": 0, "rejected_count": 0, "latest_feedback_at": ""},
                )
                entry["adjustment"] = float(entry["adjustment"]) + _MEMORY_USAGE_REJECTION_WEIGHT
                entry["rejected_count"] = int(entry["rejected_count"]) + 1
                entry["latest_feedback_at"] = max(str(entry["latest_feedback_at"]), str(feedback.time.updated_at or ""))

        for entry in adjustments.values():
            entry["adjustment"] = round(self._bounded_adjustment(entry.get("adjustment")), 3)
        return adjustments

    def _apply_memory_usage_feedback(
        self,
        items: list[RecordEnvelope],
        adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]],
    ) -> list[RecordEnvelope]:
        if not items or not adjustments:
            return items
        indexed = list(enumerate(items))
        indexed.sort(
            key=lambda pair: (
                float(adjustments.get(self._memory_usage_key(pair[1]), {}).get("adjustment") or 0.0),
                -pair[0],
            ),
            reverse=True,
        )
        return [item for _index, item in indexed]

    def _memory_usage_summary(
        self,
        items: list[RecordEnvelope],
        adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]],
    ) -> dict[str, object]:
        selected_adjustments = {
            self._memory_usage_output_key(item): adjustments[self._memory_usage_key(item)]
            for item in items
            if self._memory_usage_key(item) in adjustments
        }
        return {
            "schema_version": _MEMORY_USAGE_TELEMETRY_SCHEMA,
            "applied": bool(adjustments),
            "known_adjusted_count": len(adjustments),
            "selected_adjusted_count": len(selected_adjustments),
            "positive_selected_count": sum(
                1 for entry in selected_adjustments.values() if float(entry.get("adjustment") or 0.0) > 0.0
            ),
            "negative_selected_count": sum(
                1 for entry in selected_adjustments.values() if float(entry.get("adjustment") or 0.0) < 0.0
            ),
            "selected_adjustments": selected_adjustments,
        }

    @staticmethod
    def _memory_usage_key_for_namespace(
        record_id: str,
        *,
        scope: ScopeRef,
        source_id: str,
    ) -> tuple[str, str, str, str, str, str]:
        return (
            str(record_id or ""),
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            str(source_id or "default"),
        )

    @classmethod
    def _memory_usage_key(cls, item: RecordEnvelope) -> tuple[str, str, str, str, str, str]:
        return cls._memory_usage_key_for_namespace(
            item.record_id,
            scope=item.scope,
            source_id=item.source_id,
        )

    @classmethod
    def _memory_usage_output_key(cls, item: RecordEnvelope) -> str:
        exact_key = cls._memory_usage_key(item)
        namespace_digest = sha256("\x1f".join(exact_key[1:]).encode("utf-8")).hexdigest()[:16]
        return f"{item.record_id}@{namespace_digest}"

    @staticmethod
    def _recall_pipeline_summary(
        snapshot: RecallPipelineSnapshot,
        *,
        retrieved_count: int,
        candidate_count: int,
        selected_count: int,
        blocked_counts: Counter[str],
    ) -> dict[str, object]:
        return {
            "schema_version": _RECALL_PIPELINE_SCHEMA,
            "phase_names": list(_RECALL_PIPELINE_PHASES),
            "phases": [
                {
                    "name": "prepare",
                    "recall_profile": snapshot.recall_profile,
                    "recall_profile_source": snapshot.recall_profile_source,
                    "recall_intent": snapshot.recall_intent_name,
                    "query_scope_count": snapshot.query_scope_count,
                    "raw_hybrid": snapshot.raw_hybrid,
                },
                {
                    "name": "retrieve",
                    "search_limit": snapshot.search_limit,
                    "retrieved_count": int(retrieved_count),
                    "report_query": snapshot.report_query,
                    "operational_recall_allowed": snapshot.operational_recall_allowed,
                },
                {
                    "name": "graph_expand",
                    "graph_depth": snapshot.graph_depth,
                    "candidate_count": int(candidate_count),
                },
                {
                    "name": "score_filter",
                    "blocked_counts": dict(sorted(blocked_counts.items())),
                },
                {
                    "name": "package",
                    "selected_count": int(selected_count),
                },
            ],
        }

    def _recall_filters_from_task_context(self, task_context: dict) -> dict:
        filters = {
            "allowed_sources": self._string_list(task_context.get("allowed_sources")),
            "blocked_sources": self._string_list(task_context.get("blocked_sources")),
            "allowed_memory_types": self._string_list(task_context.get("allowed_memory_types")),
            "preferred_modalities": self._string_list(task_context.get("preferred_modalities")),
            "organs": self._string_list(task_context.get("organs")),
            "allowed_recall_lanes": self._string_list(task_context.get("allowed_recall_lanes")),
            "blocked_recall_lanes": self._string_list(task_context.get("blocked_recall_lanes")),
            "source_weights": self._source_weights(task_context.get("source_weights")),
            "living_task_context_terms": self._living_task_context_terms(task_context),
        }
        candidate_limit = self._positive_int(task_context.get("candidate_limit"))
        if candidate_limit:
            filters["candidate_limit"] = candidate_limit
        return {key: value for key, value in filters.items() if value}

    def _merge_recall_intent_filters(self, recall_filters: dict, recall_intent: RecallIntent) -> None:
        recall_filters["intent_name"] = recall_intent.name
        recall_filters["memory_cube"] = recall_intent.memory_cube
        if recall_intent.preferred_kinds:
            recall_filters["preferred_kinds"] = list(recall_intent.preferred_kinds)
        if recall_intent.suppressed_kinds:
            recall_filters["suppressed_kinds"] = list(recall_intent.suppressed_kinds)
        if recall_intent.name in {"project_delivery", "operator_preference", "living_posture"} and recall_intent.confidence >= 0.45:
            recall_filters["blocked_projection_types"] = list(
                dict.fromkeys([*list(recall_filters.get("blocked_projection_types") or []), "operational_knowledge"])
            )
        if recall_intent.query_terms:
            terms = [*list(recall_filters.get("living_task_context_terms") or []), *list(recall_intent.query_terms)]
            recall_filters["living_query_terms"] = list(dict.fromkeys(str(term) for term in terms if str(term).strip()))
        source_weights = dict(recall_intent.source_weights)
        source_weights.update(dict(recall_filters.get("source_weights") or {}))
        if source_weights:
            recall_filters["source_weights"] = source_weights

    def _search_kinds_for_recall_intent(
        self,
        *,
        recall_intent: RecallIntent,
        report_query: bool,
        operational_recall_allowed: bool = False,
    ) -> list[str]:
        if operational_recall_allowed:
            return [
                "reflection",
                "memory",
                "claim_card",
                "incident",
                "replay_result",
                "learning_eval",
                "recall_view",
                "capability_candidate",
                "skill_candidate",
                "promotion_request",
            ]
        if report_query or recall_intent.name == "report":
            return ["reflection", "memory", "claim_card"]
        if recall_intent.name in {"project_delivery", "operator_preference", "living_posture"} and recall_intent.confidence >= 0.45:
            return ["memory", "claim_card"]
        return ["memory", "claim_card", "knowledge_page"]

    @staticmethod
    def _recall_intent_summary(recall_intent: RecallIntent) -> dict:
        return {
            "name": recall_intent.name,
            "confidence": recall_intent.confidence,
            "reasons": list(recall_intent.reasons),
            "preferred_kinds": list(recall_intent.preferred_kinds),
            "suppressed_kinds": list(recall_intent.suppressed_kinds),
            "source_weights": dict(recall_intent.source_weights),
            "memory_cube": recall_intent.memory_cube,
            "query_terms": list(recall_intent.query_terms),
        }

    def _filter_default_suppressed_items(
        self,
        items: list[RecordEnvelope],
        task_context: dict,
        *,
        allow_operational_recall: bool,
    ) -> tuple[list[RecordEnvelope], Counter[str]]:
        filtered: list[RecordEnvelope] = []
        blocked_counts: Counter[str] = Counter()
        for item in items:
            if not allow_operational_recall and self._is_internal_audit_record(item):
                blocked_counts[self._record_recall_lane(item) or "audit_record"] += 1
                continue
            if self._is_default_recall_suppressed_record(
                item,
                task_context,
                allow_operational_recall=allow_operational_recall,
            ):
                blocked_counts[self._record_recall_lane(item) or str(item.kind or "suppressed")] += 1
                continue
            filtered.append(item)
        return filtered, blocked_counts

    def _diagnostic_blocked_operational_counts(
        self,
        *,
        query: str,
        query_scope_refs: list[ScopeRef],
        recall_filters: dict,
        operational_recall_allowed: bool,
        source_ids: tuple[str, ...] | None = None,
    ) -> Counter[str]:
        if operational_recall_allowed:
            return Counter()
        blocked_lanes = set(recall_filters.get("blocked_recall_lanes") or [])
        if not blocked_lanes:
            return Counter()
        diagnostic_filters = {
            key: value
            for key, value in dict(recall_filters or {}).items()
            if key not in {"blocked_recall_lanes", "allowed_recall_lanes", "blocked_counts"}
        }
        diagnostic_filters["include_report_records"] = True
        diagnostic_filters["include_evidence_only"] = True
        counts: Counter[str] = Counter()
        diagnostic_kinds = [
            "reflection",
            "incident",
            "replay_result",
            "learning_eval",
            "recall_view",
            "feedback",
            "capability_candidate",
            "skill_candidate",
            "promotion_request",
        ]
        for scope_ref in query_scope_refs[:3]:
            try:
                candidates, _report = self.store.search_with_diagnostics(
                    query=query,
                    kinds=diagnostic_kinds,
                    scope=scope_ref,
                    limit=24,
                    recall_filters=diagnostic_filters,
                    source_ids=source_ids,
                )
            except Exception:
                continue
            for item in candidates:
                lane = self._record_recall_lane(item)
                if lane in blocked_lanes:
                    counts[lane] += 1
        return counts

    def _apply_hard_recall_filters(self, items: list[RecordEnvelope], recall_filters: dict) -> list[RecordEnvelope]:
        filtered, _counts = self._apply_hard_recall_filters_with_counts(items, recall_filters)
        return filtered

    def _apply_hard_recall_filters_with_counts(
        self,
        items: list[RecordEnvelope],
        recall_filters: dict,
    ) -> tuple[list[RecordEnvelope], Counter[str]]:
        if not recall_filters:
            return items, Counter()
        filtered: list[RecordEnvelope] = []
        blocked_counts: Counter[str] = Counter()
        for item in items:
            reason = self._record_recall_filter_block_reason(item, recall_filters)
            if reason:
                blocked_counts[reason] += 1
                continue
            filtered.append(item)
        return filtered, blocked_counts

    def _apply_online_recall_pollution_gate(
        self,
        items: list[RecordEnvelope],
        *,
        allow_operational_recall: bool,
    ) -> tuple[list[RecordEnvelope], Counter[str]]:
        if allow_operational_recall:
            return items, Counter()
        filtered: list[RecordEnvelope] = []
        blocked_counts: Counter[str] = Counter()
        for item in items:
            reason = self._online_recall_pollution_reason(item)
            if reason:
                blocked_counts[reason] += 1
                continue
            filtered.append(item)
        return filtered, blocked_counts

    def _online_recall_pollution_reason(self, item: RecordEnvelope) -> str:
        if self._is_stale_rule_record(item):
            return "stale_rule"
        if self._is_temporally_stale_memory(item):
            return "stale_memory"
        lane = self._record_recall_lane(item)
        if lane in _DEFAULT_BLOCKED_RECALL_LANES:
            return lane
        if lane == "external_knowledge":
            safety = evaluate_knowledge_safety(
                item,
                task="recall",
                registry=self.source_registry,
            )
            if safety["recall_allowed"]:
                return ""
            reasons = set(safety.get("reasons") or [])
            if any(str(reason).startswith("status_") for reason in reasons):
                return "external_knowledge_quarantined"
            return "external_knowledge_untrusted"
        return ""

    def _record_allowed_by_recall_filters(self, item: RecordEnvelope, recall_filters: dict) -> bool:
        return not bool(self._record_recall_filter_block_reason(item, recall_filters))

    def _record_recall_filter_block_reason(self, item: RecordEnvelope, recall_filters: dict) -> str:
        labels = self._record_filter_labels(item)
        blocked_sources = set(recall_filters.get("blocked_sources") or [])
        if blocked_sources and labels["sources"] & blocked_sources:
            return "source:blocked"
        allowed_sources = set(recall_filters.get("allowed_sources") or [])
        if allowed_sources and not labels["sources"] & allowed_sources:
            return "source:not_allowed"
        allowed_memory_types = set(recall_filters.get("allowed_memory_types") or [])
        if allowed_memory_types and item.kind == "memory" and labels["memory_types"] and not labels["memory_types"] & allowed_memory_types:
            return "memory_type:not_allowed"
        organs = set(recall_filters.get("organs") or [])
        if organs and labels["organs"] and not labels["organs"] & organs:
            return "organ:not_allowed"
        recall_lane = self._record_recall_lane(item)
        blocked_recall_lanes = set(recall_filters.get("blocked_recall_lanes") or [])
        if blocked_recall_lanes and recall_lane in blocked_recall_lanes:
            return recall_lane
        allowed_recall_lanes = set(recall_filters.get("allowed_recall_lanes") or [])
        if allowed_recall_lanes and recall_lane not in allowed_recall_lanes:
            return "recall_lane:not_allowed"
        return ""

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

    def _is_default_recall_suppressed_record(
        self,
        item: RecordEnvelope,
        task_context: dict,
        *,
        allow_operational_recall: bool = False,
    ) -> bool:
        if self._include_digest_pages(task_context):
            return False
        page_type = str(business_metadata(item.meta).get("page_type") or item.content.get("page_type") or "").strip().lower()
        if item.kind == "knowledge_page" and page_type in {"digest", "synthesis"}:
            return True
        if item.kind == "knowledge_page" and str(item.source or "") == "eimemory.knowledge.synthesis":
            return True
        projection_type = str(
            business_metadata(item.meta).get("projection_type")
            or item.provenance.get("projection_type")
            or item.content.get("projection_type")
            or ""
        ).strip().lower()
        if (
            item.kind == "memory"
            and projection_type == "operational_knowledge"
            and str(item.source or "") == "eimemory.knowledge.projectors"
        ):
            return True
        if not allow_operational_recall and self._record_recall_lane(item) in _DEFAULT_BLOCKED_RECALL_LANES:
            return True
        return False

    @staticmethod
    def _is_stale_rule_record(item: RecordEnvelope) -> bool:
        if item.kind != "rule":
            return False
        if str(item.status or "").strip().lower() not in {"active", "accepted"}:
            return True
        meta = business_metadata(item.meta)
        watch = meta.get("post_promotion_watch") if isinstance(meta, dict) else {}
        if isinstance(watch, dict) and str(watch.get("status") or "").strip().lower() in {
            "rolled_back",
            "quarantined",
            "rejected",
        }:
            return True
        return False

    @staticmethod
    def _is_temporally_stale_memory(item: RecordEnvelope) -> bool:
        if item.kind != "memory":
            return False
        meta = business_metadata(item.meta)
        living = meta.get(LIVING_MEMORY_META_KEY)
        if not isinstance(living, dict):
            return False
        temporal = living.get("temporal")
        if not isinstance(temporal, dict):
            return False
        temporal_status = str(temporal.get("status") or temporal.get("state") or "").strip().lower().replace("_", "-")
        temporal_distance = str(temporal.get("temporal_distance") or "").strip().lower().replace("_", "-")
        if bool(temporal.get("superseded")) or temporal_status in {"superseded", "expired", "stale"}:
            return True
        if temporal_distance == "stale":
            return True
        return MemoryAPI._valid_until_is_past(temporal.get("valid_until"))

    @staticmethod
    def _valid_until_is_past(value: object) -> bool:
        if not value:
            return False
        text = str(value).strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed < datetime.now(timezone.utc)

    def _record_recall_lane(self, item: RecordEnvelope) -> str:
        labels = self._record_filter_labels(item)
        for memory_type in labels["memory_types"]:
            normalized = _RECALL_LANE_MEMORY_TYPE_ALIASES.get(memory_type, memory_type)
            if normalized:
                return normalized
        if item.kind == "rule":
            return "system_rule"
        if item.kind == "reflection":
            return self._reflection_recall_lane(item)
        if item.kind in {"recall_view", "feedback"}:
            return "audit_record"
        if item.kind == "incident":
            return "incident_report"
        if item.kind in {"replay_result", "learning_eval", "capability_candidate", "promotion_request", "skill_candidate"}:
            return "evolution_artifact"
        if item.kind in _KNOWLEDGE_CONTENT_DEDUPE_KINDS or item.kind == "knowledge_unit":
            return "external_knowledge"
        if item.kind == "memory":
            return "durable_fact"
        return ""

    @staticmethod
    def _reflection_recall_lane(item: RecordEnvelope) -> str:
        meta = business_metadata(item.meta)
        content = item.content if isinstance(item.content, dict) else {}
        report_type = str(meta.get("report_type") or item.provenance.get("report_type") or content.get("report_type") or "").strip().lower()
        haystack = " ".join([report_type, str(item.source or ""), str(item.title or "")]).lower()
        if any(marker in haystack for marker in ("audit", "before_prompt_build", "injection")):
            return "audit_record"
        if "incident" in haystack:
            return "incident_report"
        if "outcome_trace" in haystack or "run_log" in haystack:
            return "run_log"
        if report_type:
            return "evolution_artifact"
        return "audit_record"

    @staticmethod
    def _allows_operational_recall(query: str, task_context: dict) -> bool:
        if bool(task_context.get("include_operational_recall")) or bool(task_context.get("include_recall_pollution")):
            return True
        if bool(task_context.get("include_report_records")) or bool(task_context.get("include_evidence_only")):
            return True
        haystack = " ".join(
            str(value or "")
            for value in (
                query,
                task_context.get("intent"),
                task_context.get("task_intent"),
                task_context.get("task_type"),
                task_context.get("query_type"),
                task_context.get("goal"),
                task_context.get("report_type"),
            )
        ).lower()
        return any(
            marker in haystack
            for marker in (
                "diagnostic",
                "diagnostics",
                "debug",
                "debugging",
                "evidence report",
                "governance report",
                "audit",
                "incident",
                "run log",
                "run_log",
                "postmortem",
                "root cause",
                "trace",
            )
        )

    def _normalize_ingest_memory_type(self, *, memory_type: str, text: str, title: str) -> str:
        normalized = str(memory_type or "").strip()
        if normalized and normalized != "conversation":
            return normalized
        if self._looks_like_explicit_preference(f"{title}\n{text}"):
            return "preference"
        return normalized or "fact"

    def _is_preference_query(
        self,
        query: str,
        task_context: dict,
        *,
        recall_intent: RecallIntent | None = None,
    ) -> bool:
        haystack = f"{query} " + " ".join(str(task_context.get(key) or "") for key in ("intent", "goal", "task_type"))
        lowered = haystack.lower()
        if _DEFAULT_PREFERENCE_QUERY_MARKER_RE.search(haystack):
            return True
        custom_markers = tuple(dict.fromkeys(self._string_list(task_context.get("preference_query_markers"))))
        if custom_markers and any(marker.lower() in lowered or marker in haystack for marker in custom_markers):
            return True
        if recall_intent is None or recall_intent.name not in {"operator_preference", "living_posture"}:
            return False
        intent_terms = " ".join(str(term or "") for term in recall_intent.query_terms).lower()
        intent_marker_match = any(marker in intent_terms for marker in ("preference", "reply style", "communication style"))
        return recall_intent.confidence >= 0.65 and intent_marker_match

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

    def _matching_active_rule_recall_items(
        self,
        *,
        active_rules: list[RecordEnvelope],
        query: str,
        recall_intent: RecallIntent,
        limit: int,
    ) -> list[RecordEnvelope]:
        if not active_rules or limit <= 0:
            return []
        if recall_intent.name not in {"operator_preference", "living_posture"}:
            return []
        if recall_intent.confidence < 0.45:
            return []
        scored: list[tuple[float, str, RecordEnvelope]] = []
        for rule in active_rules:
            if str(rule.status or "").strip().lower() != "active":
                continue
            score = self._active_rule_query_score(rule, query=query, recall_intent=recall_intent)
            if score <= 0.0:
                continue
            scored.append((score, str(rule.time.updated_at or ""), rule))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [rule for _score, _updated_at, rule in scored[:limit]]

    def _active_rule_query_score(self, rule: RecordEnvelope, *, query: str, recall_intent: RecallIntent) -> float:
        rule_text = self._record_text(rule).casefold()
        compact_rule_text = re.sub(r"\s+", "", rule_text)
        normalized_query = str(query or "").strip().casefold()
        compact_query = re.sub(r"\s+", "", normalized_query)
        if not rule_text or not compact_query:
            return 0.0

        terms = self._rule_recall_query_terms(query=normalized_query, recall_intent=recall_intent)
        matched_terms = [
            term
            for term in terms
            if term and (term in rule_text or re.sub(r"\s+", "", term) in compact_rule_text)
        ]
        exact_match = normalized_query in rule_text or compact_query in compact_rule_text
        if not matched_terms and not exact_match:
            return 0.0

        score = len(matched_terms) / max(1, len(terms))
        if exact_match:
            score += 1.0
        if self._looks_like_explicit_preference(rule_text):
            score += 0.25
        if recall_intent.name == "operator_preference" and any(
            marker in rule_text for marker in ("沟通风格", "communication style", "reply style")
        ):
            score += 0.2
        return score

    @staticmethod
    def _rule_recall_query_terms(*, query: str, recall_intent: RecallIntent) -> list[str]:
        terms = [str(term).strip().casefold() for term in recall_intent.query_terms if str(term).strip()]
        compact_query = re.sub(r"\s+", "", str(query or "").strip().casefold())
        if compact_query:
            terms.append(compact_query)
        if compact_query and re.search(r"[\u4e00-\u9fff]", compact_query):
            terms.extend(compact_query[index : index + 2] for index in range(max(0, len(compact_query) - 1)))
        return list(dict.fromkeys(term for term in terms if len(term) >= 2))

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
        seen_refs: set[tuple[str, str, str, str, str, str]] = set()
        seen_content_positions: dict[tuple[tuple[str, str, str, str, str], str], int] = {}
        deduped: list[RecordEnvelope] = []
        for item in items:
            scope = item.scope
            namespace = (
                scope.tenant_id or "default",
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                item.source_id,
            )
            exact_ref = (item.record_id, *namespace)
            if exact_ref in seen_refs:
                continue
            seen_refs.add(exact_ref)
            content_key = MemoryAPI._record_content_key(item)
            if content_key:
                namespaced_content_key = (namespace, content_key)
                existing_index = seen_content_positions.get(namespaced_content_key)
                if existing_index is not None:
                    if MemoryAPI._prefer_dedupe_replacement(item, deduped[existing_index]):
                        deduped[existing_index] = item
                    continue
                seen_content_positions[namespaced_content_key] = len(deduped)
            deduped.append(item)
        return MemoryAPI._cap_knowledge_source_groups(deduped)

    @staticmethod
    def _record_content_key(item: RecordEnvelope) -> str:
        title = str(item.title or "").strip().lower()
        summary = str(item.summary or "").strip().lower()
        if not title and not summary:
            return ""
        if item.kind == "memory":
            memory_type = (
                str(business_metadata(item.meta).get("memory_type") or item.content.get("memory_type") or "")
                .strip()
                .lower()
            )
            text = f"memory::{memory_type}::{title}::{summary}"
            return sha256(text.encode("utf-8")).hexdigest()[:24]

        title_summary = f"{item.kind}::{title}::{summary[:100]}"
        summary_key = f"knowledge::{summary[:220]}" if item.kind in _KNOWLEDGE_CONTENT_DEDUPE_KINDS and len(summary) >= 80 else ""
        text = summary_key or title_summary
        if not text.strip(":"):
            return ""
        return sha256(text.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _prefer_dedupe_replacement(candidate: RecordEnvelope, incumbent: RecordEnvelope) -> bool:
        return MemoryAPI._dedupe_rank(candidate) > MemoryAPI._dedupe_rank(incumbent)

    @staticmethod
    def _dedupe_rank(item: RecordEnvelope) -> tuple[float, str, str, str]:
        score = extract_memory_score(item.meta)
        quality = business_metadata(item.meta).get("quality")
        quality_score = 0.0
        if isinstance(quality, dict):
            try:
                quality_score = float(quality.get("salience_score") or quality.get("importance") or 0.0)
            except (TypeError, ValueError):
                quality_score = 0.0
        final_score = float(score.final_score) if score is not None else quality_score
        return (final_score, str(item.time.updated_at or ""), str(item.time.created_at or ""), str(item.record_id or ""))

    @staticmethod
    def _cap_knowledge_source_groups(items: list[RecordEnvelope]) -> list[RecordEnvelope]:
        source_counts: dict[str, int] = {}
        capped: list[RecordEnvelope] = []
        for item in items:
            source_key = MemoryAPI._record_source_group_key(item)
            if source_key:
                count = source_counts.get(source_key, 0)
                if count >= _MAX_RECORDS_PER_KNOWLEDGE_SOURCE:
                    continue
                source_counts[source_key] = count + 1
            capped.append(item)
        return capped

    @staticmethod
    def _record_source_group_key(item: RecordEnvelope) -> str:
        if item.kind not in _KNOWLEDGE_CONTENT_DEDUPE_KINDS:
            return ""
        values: list[str] = []
        containers = [item.provenance, business_metadata(item.meta), item.content if isinstance(item.content, dict) else {}]
        for container in containers:
            for key in ("paper_source_id", "source_id"):
                value = container.get(key)
                if value:
                    values.append(str(value).strip().lower())
            source_ids = container.get("source_ids")
            if isinstance(source_ids, (list, tuple)):
                values.extend(str(value).strip().lower() for value in source_ids if str(value).strip())
        values.extend(
            str(link.target_id).strip().lower()
            for link in item.links
            if str(link.target_kind or "").strip().lower() == "paper_source" and str(link.target_id or "").strip()
        )
        normalized = sorted({value for value in values if value})
        if not normalized:
            return ""
        scope = item.scope
        namespace = "\x1f".join(
            (
                scope.tenant_id or "default",
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                item.source_id,
            )
        )
        return f"paper::{sha256(namespace.encode('utf-8')).hexdigest()}::{normalized[0]}"

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

    @staticmethod
    def _positive_int(value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

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
        scopes: list[ScopeRef],
        graph_depth: int,
        source_ids: tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        query_scopes = scopes or []
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
            related_records = self._get_many_by_ids_across_scopes(
                related_ids,
                query_scopes,
                source_ids=source_ids,
            )
            next_frontier: list[RecordEnvelope] = []
            for record in related_records:
                if record.record_id in existing_ids:
                    continue
                if not self._is_returnable_memory_record(record):
                    continue
                if not self._record_matches_any_scope(record, query_scopes):
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

    def _expand_memory_edge_items(
        self,
        *,
        base_items: list[RecordEnvelope],
        scopes: list[ScopeRef],
        edge_types: list[str],
        limit: int,
        source_ids: tuple[str, ...] | None = None,
    ) -> tuple[list[RecordEnvelope], list]:
        base_ids = [item.record_id for item in base_items]
        if not base_ids:
            return [], []
        query_scopes = scopes or []
        edges = []
        seen_edge_ids: set[str] = set()
        for scope in query_scopes:
            for edge in self.store.list_memory_edges(
                scope=scope,
                edge_types=edge_types,
                record_ids=base_ids,
                limit=max(1, int(limit)) * 4,
            ):
                edge_id = str(getattr(edge, "edge_id", "") or "")
                if edge_id and edge_id in seen_edge_ids:
                    continue
                if edge_id:
                    seen_edge_ids.add(edge_id)
                edges.append(edge)
        related_ids: list[str] = []
        for edge in edges:
            if edge.from_id in base_ids and edge.to_id not in base_ids:
                related_ids.append(edge.to_id)
            if edge.to_id in base_ids and edge.from_id not in base_ids:
                related_ids.append(edge.from_id)
        related = self._get_many_by_ids_across_scopes(
            list(dict.fromkeys(related_ids)),
            query_scopes,
            source_ids=source_ids,
        )
        expanded = [
            record
            for record in related
            if self._is_returnable_memory_record(record) and self._record_matches_any_scope(record, query_scopes)
        ]
        return expanded[: max(0, int(limit))], edges

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
        by_recall_lane: dict[str, int] = {}
        projected_count = 0
        projected_source_ids: list[str] = []
        for item in items:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
            recall_lane = self._record_recall_lane(item) or "unknown"
            by_recall_lane[recall_lane] = by_recall_lane.get(recall_lane, 0) + 1
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
            "by_recall_lane": by_recall_lane,
            "projected_count": projected_count,
            "projected_source_ids": projected_source_ids,
            "knowledge_count": by_kind.get("claim_card", 0) + by_kind.get("knowledge_page", 0),
            "memory_count": by_kind.get("memory", 0),
        }

    def _event_graph_summary(self, items: list[RecordEnvelope], edges: list) -> dict:
        event_items = [
            item
            for item in items
            if str(
                business_metadata(item.meta).get("projection_type")
                or item.provenance.get("projection_type")
                or item.content.get("projection_type")
                or ""
            ).strip().lower()
            == "event_memory"
        ]
        return {
            "ok": True,
            "selected_event_count": len(event_items),
            "event_record_ids": [item.record_id for item in event_items],
            "event_ids": [
                str(
                    business_metadata(item.meta).get("event_id")
                    or item.provenance.get("event_id")
                    or item.content.get("event_id")
                    or ""
                )
                for item in event_items
                if str(
                    business_metadata(item.meta).get("event_id")
                    or item.provenance.get("event_id")
                    or item.content.get("event_id")
                    or ""
                )
            ],
            "edge_ids": [
                str(getattr(edge, "edge_id", ""))
                for edge in list(edges or [])
                if str(getattr(edge, "edge_id", ""))
            ],
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
                    "recall_lane": self._record_recall_lane(item),
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

    @staticmethod
    def _prioritize_fast_query_scopes(
        scopes: list[ScopeRef],
        *,
        primary_scope: ScopeRef,
    ) -> list[ScopeRef]:
        primary_key = (
            primary_scope.tenant_id,
            primary_scope.agent_id,
            primary_scope.workspace_id,
            primary_scope.user_id,
        )
        primary = [
            scope
            for scope in scopes
            if (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id) == primary_key
        ]
        canonical = [
            scope
            for scope in scopes
            if scope.agent_id == HONGTU_AGENT_ID and scope.workspace_id == HONGTU_WORKSPACE_ID
        ]
        same_user = [scope for scope in scopes if scope.user_id == primary_scope.user_id]
        ordered: list[ScopeRef] = []
        seen: set[tuple[str, str, str, str]] = set()
        for scope in [*primary, *canonical, *same_user, *scopes]:
            key = (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(scope)
        return ordered

    def _merge_search_reports(self, reports: list[dict]) -> dict:
        merged: dict = {
            "retrieval_mode": "hybrid",
            "vector_hits": 0,
            "scored_items": [],
            "blocked_counts": {},
        }
        seen_scores: set[str] = set()
        for report in reports:
            if not isinstance(report, dict):
                continue
            if report.get("retrieval_mode"):
                merged["retrieval_mode"] = report.get("retrieval_mode")
            merged["vector_hits"] += int(report.get("vector_hits") or 0)
            blocked_counts = dict(report.get("blocked_counts") or (report.get("recall_filters") or {}).get("blocked_counts") or {})
            for key, value in blocked_counts.items():
                merged["blocked_counts"][str(key)] = int(merged["blocked_counts"].get(str(key), 0)) + int(value or 0)
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
        if record.status != "active":
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

    def _record_matches_any_scope(self, record: RecordEnvelope, scopes: list[ScopeRef]) -> bool:
        if not scopes:
            return True
        return any(self._record_matches_scope(record, scope) for scope in scopes)

    def _get_many_by_ids_across_scopes(
        self,
        record_ids: list[str],
        scopes: list[ScopeRef],
        *,
        source_ids: tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        resolved: list[RecordEnvelope] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        exact_scopes: list[ScopeRef] = []
        exact_scope_keys: set[tuple[str, str, str, str]] = set()
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
                scope_key = (
                    candidate.tenant_id,
                    candidate.agent_id,
                    candidate.workspace_id,
                    candidate.user_id,
                )
                if scope_key in exact_scope_keys:
                    continue
                exact_scope_keys.add(scope_key)
                exact_scopes.append(candidate)
        for record_id in dict.fromkeys(record_ids):
            for scope in exact_scopes:
                for record in self.store.list_by_record_id_exact_scope(
                    record_id,
                    scope=scope,
                    source_ids=source_ids,
                ):
                    key = (
                        record.record_id,
                        record.scope.tenant_id,
                        record.scope.agent_id,
                        record.scope.workspace_id,
                        record.scope.user_id,
                        record.source_id,
                    )
                    if key in seen or record.status != "active":
                        continue
                    hydrated = self.store.get_by_exact_ref(
                        record.record_id,
                        scope=record.scope,
                        source_id=record.source_id,
                    )
                    if hydrated is None or hydrated.status != "active":
                        continue
                    seen.add(key)
                    resolved.append(hydrated)
        return resolved

    def _scoring_for_items(
        self,
        items: list[RecordEnvelope],
        search_report: dict,
        *,
        memory_usage_adjustments: dict[tuple[str, str, str, str, str, str], dict[str, object]] | None = None,
    ) -> list[dict]:
        scored_by_ref = {
            self._scored_entry_key(entry): dict(entry)
            for entry in (search_report.get("scored_items") or [])
            if isinstance(entry, dict) and self._scored_entry_key(entry) is not None
        }
        memory_usage_adjustments = memory_usage_adjustments or {}
        scoring: list[dict] = []
        for item in items:
            entry = dict(scored_by_ref.get(self._memory_usage_key(item)) or {})
            quality = business_metadata(item.meta).get("quality") if isinstance(item.meta, dict) else {}
            if not isinstance(quality, dict):
                quality = {}
            telemetry = dict(memory_usage_adjustments.get(self._memory_usage_key(item)) or {})
            telemetry_adjustment = self._bounded_adjustment(telemetry.get("adjustment"))
            base_quality_score = self._bounded_score(
                entry.get("quality_score", quality.get("salience_score", 0.0)),
                default=0.0,
            )
            adjusted_quality_score = self._bounded_score(base_quality_score + telemetry_adjustment, default=base_quality_score)
            raw_final_score = entry.get("final_score", 0.0)
            try:
                base_final_score = float(raw_final_score)
            except (TypeError, ValueError):
                base_final_score = 0.0
            final_score = (
                round(base_final_score + telemetry_adjustment, 3)
                if telemetry_adjustment
                else raw_final_score
            )
            scoring.append(
                {
                    "record_id": item.record_id,
                    "source_id": item.source_id,
                    "scope": self._scope_dict(item.scope),
                    "kind": item.kind,
                    "title": item.title,
                    "lexical_score": entry.get("lexical_score", 0),
                    "semantic_score": entry.get("semantic_score", 0.0),
                    "vector_score": entry.get("vector_score", 0.0),
                    "quality_score": adjusted_quality_score,
                    "base_quality_score": base_quality_score,
                    "telemetry_adjustment": telemetry_adjustment,
                    "telemetry_used_count": int(telemetry.get("used_count") or 0),
                    "telemetry_rejected_count": int(telemetry.get("rejected_count") or 0),
                    "quality_tier": str(quality.get("quality_tier") or "unscored"),
                    "modality_boost": entry.get("modality_boost", 0.0),
                    "final_score": final_score,
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

    @staticmethod
    def _scored_entry_key(entry: dict) -> tuple[str, str, str, str, str, str] | None:
        scope = entry.get("scope")
        record_id = str(entry.get("record_id") or "")
        source_id = str(entry.get("source_id") or "")
        if not record_id or not source_id or not isinstance(scope, dict):
            return None
        return (
            record_id,
            str(scope.get("tenant_id") or "default"),
            str(scope.get("agent_id") or ""),
            str(scope.get("workspace_id") or ""),
            str(scope.get("user_id") or ""),
            source_id,
        )
