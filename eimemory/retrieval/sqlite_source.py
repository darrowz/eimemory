from __future__ import annotations

from time import perf_counter
from typing import Any

from eimemory.storage.runtime_store import RuntimeStore

from .contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, ExactScope, freeze_value


class SQLiteCandidateSource:
    """Adapter over the existing SQLite scorer that exposes ID-only hits."""

    name = "sqlite"
    policy_version = "sqlite-recall.v1"

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def search(self, request: CandidateRequest) -> CandidateBatch:
        started = perf_counter()
        if not request.query or request.limit <= 0 or request.budget <= 0 or request.source_ids == ():
            return self._batch(request=request, hits=(), elapsed_ms=(perf_counter() - started) * 1000.0)
        recall_filters = request.recall_filter_dict()
        configured_budget = _positive_int(recall_filters.get("candidate_limit"))
        recall_filters["candidate_limit"] = min(
            request.budget,
            configured_budget if configured_budget else request.budget,
        )
        records, report = self.store.search_with_diagnostics(
            query=request.query,
            kinds=list(request.kinds) or None,
            scope=request.scope.to_scope_ref(),
            limit=request.limit,
            recall_filters=recall_filters,
            source_ids=request.source_ids,
        )
        scores = {
            str(entry.get("record_id") or ""): entry
            for entry in list(report.get("scored_items") or [])
            if isinstance(entry, dict) and str(entry.get("record_id") or "")
        }
        hits: list[CandidateHit] = []
        for rank, record in enumerate(records[: request.limit], start=1):
            score_entry = dict(scores.get(record.record_id) or {})
            score_entry.pop("record_id", None)
            score_entry.pop("kind", None)
            score_entry.pop("title", None)
            hits.append(
                CandidateHit(
                    ref=CandidateRef(
                        record_id=record.record_id,
                        scope=ExactScope.from_scope(record.scope),
                        source_id=record.source_id,
                    ),
                    source_rank=rank,
                    source_score=_float_score(score_entry.get("final_score")),
                    component_hints=freeze_value(score_entry),
                    evidence_hints=("sqlite_hybrid",),
                )
            )
        return self._batch(
            request=request,
            hits=tuple(hits),
            elapsed_ms=(perf_counter() - started) * 1000.0,
            report=report,
        )

    def _batch(
        self,
        *,
        request: CandidateRequest,
        hits: tuple[CandidateHit, ...],
        elapsed_ms: float,
        report: dict[str, Any] | None = None,
    ) -> CandidateBatch:
        report = dict(report or {})
        blocked = dict(report.get("blocked_counts") or (report.get("recall_filters") or {}).get("blocked_counts") or {})
        drops = {str(key): int(value or 0) for key, value in list(sorted(blocked.items()))[:8]}
        fallback_reason = str(report.get("candidate_fallback") or "")
        return CandidateBatch(
            hits=hits,
            diagnostics={
                "source_name": self.name,
                "candidate_count": int(report.get("candidate_count") or len(hits)),
                "candidate_limit": int(report.get("candidate_limit") or request.limit),
                "returned_count": len(hits),
                "elapsed_ms": round(max(0.0, elapsed_ms), 3),
                "drops": drops,
                "fallback": bool(fallback_reason),
                "fallback_reason": fallback_reason,
                "policy_version": self.policy_version,
                "retrieval_mode": str(report.get("retrieval_mode") or "recall_index_hybrid"),
                "vector_hits": int(report.get("vector_hits") or 0),
            },
        )


def _float_score(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
