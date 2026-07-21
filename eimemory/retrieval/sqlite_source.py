from __future__ import annotations

from time import perf_counter
from typing import Any

from eimemory.storage.runtime_store import RuntimeStore
from eimemory.models.identity_aliases import normalize_identity_text
from eimemory.models.source_partitions import normalize_source_id
from eimemory.metadata import business_metadata

from .contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, ExactScope, freeze_value


class SQLiteCandidateSource:
    """Adapter over the existing SQLite scorer that exposes ID-only hits."""

    name = "sqlite"
    policy_version = "sqlite-recall.v1"

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def authority_head(self) -> tuple[str, str]:
        """Return the exact keyset head used by the optional projection sync."""
        with self.store._lock:
            row = self.store.sqlite.conn.execute(
                "SELECT updated_at, storage_key FROM records "
                "ORDER BY updated_at DESC, storage_key DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return ("", "")
        return (str(row["updated_at"] or "")[:64], str(row["storage_key"] or "")[:512])

    def authority_revision(self) -> str:
        with self.store._lock:
            exists = self.store.sqlite.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_sync_revision'"
            ).fetchone()
            if exists is None:
                return ""
            row = self.store.sqlite.conn.execute(
                "SELECT revision FROM vector_sync_revision WHERE singleton = 1"
            ).fetchone()
        return "" if row is None else str(int(row["revision"]))

    def search(self, request: CandidateRequest) -> CandidateBatch:
        started = perf_counter()
        if not request.query or request.limit <= 0 or request.budget <= 0 or request.source_ids == ():
            return self._batch(request=request, hits=(), elapsed_ms=(perf_counter() - started) * 1000.0)
        recall_filters = request.recall_filter_dict()
        # CandidateRequest.scope is an exact physical partition contract.  The
        # governed engine performs any legacy/Hongtu alias fan-out explicitly;
        # the SQLite adapter must not silently broaden an individual request.
        recall_filters["_exact_scope"] = True
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
        identity_rows = self.store.sqlite.search_identity_candidates(
            query=request.query,
            kinds=list(request.kinds) or None,
            scope=request.scope.to_scope_ref(),
            limit=min(request.budget, max(request.limit, 1)),
            recall_filters=recall_filters,
            source_ids=request.source_ids,
        )
        verified_identity_rows: list[dict[str, object]] = []
        identity_drop_count = 0
        for row in identity_rows:
            row_scope = ExactScope.from_scope(row.get("scope") if isinstance(row.get("scope"), dict) else {})
            row_record_id = str(row.get("record_id") or "").strip()
            try:
                row_source_id = normalize_source_id(row.get("source_id"))
            except (TypeError, ValueError):
                identity_drop_count += 1
                continue
            if not row_record_id or row_scope != request.scope:
                identity_drop_count += 1
                continue
            try:
                authoritative = self.store.get_by_exact_ref(
                    row_record_id,
                    scope=row_scope.to_scope_ref(),
                    source_id=row_source_id,
                )
            except (TypeError, ValueError):
                identity_drop_count += 1
                continue
            quality = business_metadata(authoritative.meta).get("quality") if authoritative is not None else {}
            if (
                authoritative is None
                or authoritative.status != "active"
                or ExactScope.from_scope(authoritative.scope) != row_scope
                or authoritative.source_id != row_source_id
                or (request.kinds and authoritative.kind not in request.kinds)
                or (isinstance(quality, dict) and quality.get("capture_decision") == "reject")
            ):
                continue
            normalized_query = normalize_identity_text(request.query)
            evidence = set(str(item) for item in (row.get("evidence") or ()))
            verified_evidence: list[str] = []
            if "exact_title" in evidence and normalize_identity_text(authoritative.title) == normalized_query:
                verified_evidence.append("exact_title")
            if "alias_hit" in evidence and normalized_query in authoritative.aliases:
                verified_evidence.append("alias_hit")
            if not verified_evidence:
                continue
            verified_identity_rows.append({**row, "evidence": verified_evidence})
        identity_rows = verified_identity_rows
        if identity_drop_count:
            blocked = dict(report.get("blocked_counts") or {})
            blocked["identity_invalid_ref"] = min(1000, int(blocked.get("identity_invalid_ref") or 0) + identity_drop_count)
            report["blocked_counts"] = blocked
        identity_by_ref = {
            (
                str(row.get("record_id") or ""),
                ExactScope.from_scope(row.get("scope") if isinstance(row.get("scope"), dict) else {}),
                str(row.get("source_id") or ""),
            ): tuple(str(item) for item in (row.get("evidence") or ()))
            for row in identity_rows
        }
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
                    evidence_hints=identity_by_ref.get(
                        (record.record_id, ExactScope.from_scope(record.scope), record.source_id),
                        (),
                    ),
                )
            )
        seen_refs = {(hit.ref.record_id, hit.ref.scope, hit.ref.source_id) for hit in hits}
        for row in identity_rows:
            ref = CandidateRef(
                record_id=str(row.get("record_id") or ""),
                scope=ExactScope.from_scope(row.get("scope") if isinstance(row.get("scope"), dict) else {}),
                source_id=str(row.get("source_id") or ""),
            )
            ref_key = (ref.record_id, ref.scope, ref.source_id)
            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)
            hits.append(
                CandidateHit(
                    ref=ref,
                    source_rank=len(hits) + 1,
                    source_score=1.0,
                    component_hints={"final_score": 1.0, "identity_indexed": True},
                    evidence_hints=tuple(str(item) for item in (row.get("evidence") or ())),
                )
            )
        hits.sort(key=lambda hit: (0 if hit.evidence_hints else 1, hit.source_rank, hit.ref.record_id))
        return self._batch(
            request=request,
            hits=tuple(hits[: request.limit]),
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
