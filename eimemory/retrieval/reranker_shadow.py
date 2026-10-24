"""Explicit offline reranker comparison, isolated from recall delivery.

The caller supplies native authorized references, a fresh authority validator,
and an explicit scorer implementing ``score(query, texts, timeout_seconds=...,
deadline_at=...)``. No client is constructed and no environment flag enables
this utility. The scorer must honor the supplied transport/deadline budget;
the existing TEIReranker does so. A late result never becomes quality evidence.
An arbitrary in-process callback cannot be forcibly cancelled by this utility.

Labels are used only after scoring, never as model inputs or selection signals.
Reports contain opaque reference hashes, counters, ranking quality and elapsed
time; they contain no query, evidence text, endpoint, token or exception body.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from time import perf_counter

from eimemory.models.records import RecordEnvelope
from .contracts import CandidateRef, ExactScope
from .relevance import RelevanceConfig, RelevanceUnavailable, authoritative_text, record_digest, validate_scores


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool = False
    max_candidates: int = 24
    max_input_candidates: int = 256
    max_cases: int = 64
    max_text_chars: int = 700
    timeout_seconds: float = 2.0
    max_batch_seconds: float = 30.0

    def __post_init__(self):
        if type(self.max_candidates) is not int or not 20 <= self.max_candidates <= 32:
            raise ValueError("shadow_candidate_bounds")
        if (type(self.max_input_candidates) is not int
                or not self.max_candidates <= self.max_input_candidates <= 512
                or type(self.max_cases) is not int or not 1 <= self.max_cases <= 128
                or type(self.max_text_chars) is not int or not 128 <= self.max_text_chars <= 4000):
            raise ValueError("shadow_input_bounds")
        if (type(self.enabled) is not bool
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not .05 <= self.timeout_seconds <= 10
                or type(self.max_batch_seconds) not in (int, float)
                or not math.isfinite(self.max_batch_seconds) or not .05 <= self.max_batch_seconds <= 300):
            raise ValueError("shadow_budget_bounds")


@dataclass(frozen=True)
class ShadowCase:
    case_id: str
    query: str
    candidates: tuple[RecordEnvelope, ...]
    authorized_refs: frozenset[CandidateRef]
    # None is unlabelled; an explicit empty set is a labelled negative query.
    relevant_refs: frozenset[CandidateRef] | None = None

    def __post_init__(self):
        if not isinstance(self.case_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", self.case_id) is None:
            raise ValueError("shadow_case_id_invalid")
        if (not isinstance(self.candidates, tuple)
                or not isinstance(self.authorized_refs, frozenset)
                or len(self.authorized_refs) > 4096
                or any(not isinstance(ref, CandidateRef) for ref in self.authorized_refs)):
            raise ValueError("shadow_authority_input_invalid")
        if self.relevant_refs is not None and (
            not isinstance(self.relevant_refs, frozenset)
            or not self.relevant_refs.issubset(self.authorized_refs)
        ):
            raise ValueError("shadow_label_authority_mismatch")


def _ref(record: RecordEnvelope) -> CandidateRef:
    return CandidateRef(record.record_id, ExactScope.from_scope(record.scope), record.source_id)


def _token(ref: CandidateRef) -> str:
    return sha256(json.dumps(asdict(ref), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _quality(order: list[CandidateRef], relevant: frozenset[CandidateRef] | None):
    if relevant is None:
        return None
    hits = [int(ref in relevant) for ref in order[:5]]
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(5, len(relevant)) + 1))
    return {
        "precision_at_1": float(bool(hits and hits[0])),
        "precision_at_5": sum(hits) / 5,
        # MRR is measured at the same five-result delivery cutoff, not on the
        # larger model input pool where a result may never have been delivered.
        "mrr": next((1 / rank for rank, hit in enumerate(hits, 1) if hit), 0.0),
        "recall_at_5": sum(hits) / len(relevant) if relevant else None,
        "ndcg_at_5": sum(hit / math.log2(rank + 1) for rank, hit in enumerate(hits, 1)) / ideal if ideal else 0.0,
        "false_positive_rate": float(bool(order)) if not relevant else None,
    }


def _mean_quality(rows):
    if not rows:
        return None
    result = {}
    for key in rows[0]:
        observed = [row[key] for row in rows if row[key] is not None]
        result[key] = sum(observed) / len(observed) if observed else None
    return result


def _latencies(values):
    ordered = sorted(values)
    return {
        "sample_count": len(ordered),
        "p50": ordered[math.ceil(.5 * len(ordered)) - 1] if ordered else None,
        "p95": ordered[math.ceil(.95 * len(ordered)) - 1] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


_SAFE_SCORER_ERRORS = frozenset({
    "reranker_busy", "reranker_input_bound", "reranker_deadline_exceeded",
    "reranker_response_invalid", "reranker_response_bound", "reranker_identity_invalid",
    "reranker_identity_mismatch", "reranker_redirect_rejected", "reranker_request_failed",
})


def _compare_case(case, *, config, scorer, validate, batch_deadline):
    started = perf_counter()
    deadline = min(batch_deadline, started + config.timeout_seconds)
    result = {
        "case_id": case.case_id, "status": "unavailable", "reason": "",
        "input_count": len(case.candidates), "authorized_count": 0, "candidate_count": 0,
        "model_calls": 0, "baseline_order": [], "shadow_order": [],
        "candidate_recall_at_k": None, "baseline": None, "shadow": None,
        "dropped_reasons": {},
    }

    def drop(reason, count=1):
        result["dropped_reasons"][reason] = result["dropped_reasons"].get(reason, 0) + count

    def expired():
        return perf_counter() >= deadline

    def finish(reason="", *, complete=False):
        finished = perf_counter()
        if complete and finished >= deadline:
            complete, reason = False, "shadow_deadline_exceeded"
            result["shadow_order"], result["shadow"] = [], None
        result.update(status="complete" if complete else "unavailable", reason=reason,
                      elapsed_ms=round(max(0, finished - started) * 1000, 3))
        return result

    if expired():
        return finish("shadow_deadline_exceeded")
    if not isinstance(case.query, str) or not case.query.strip() or len(case.query) > 16000:
        return finish("shadow_query_bound")
    scanned = case.candidates[:config.max_input_candidates]
    if len(scanned) < len(case.candidates):
        drop("input_candidate_budget", len(case.candidates) - len(scanned))
    authorized, seen, initial_digests = [], set(), {}
    try:
        for record in scanned:
            if expired():
                return finish("shadow_deadline_exceeded")
            ref = _ref(record)
            if ref not in case.authorized_refs:
                drop("unauthorized_reference")
            elif record.status != "active":
                drop("inactive_record")
            elif not validate(record) or _ref(record) != ref or record.status != "active":
                drop("authority_changed_or_forbidden")
            elif ref in seen:
                drop("duplicate_reference")
            else:
                authorized.append(record)
                seen.add(ref)
                initial_digests[ref] = record_digest(record)
    except Exception:
        return finish("authority_check_failed")
    if expired():
        return finish("shadow_deadline_exceeded")
    result["authorized_count"] = len(authorized)
    bounded = authorized[:config.max_candidates]
    if len(bounded) < len(authorized):
        drop("candidate_budget", len(authorized) - len(bounded))
    refs = [_ref(record) for record in bounded]
    result["candidate_count"] = len(refs)
    result["baseline_order"] = [_token(ref) for ref in refs]
    result["baseline"] = _quality(refs, case.relevant_refs)
    if case.relevant_refs:
        result["candidate_recall_at_k"] = len(set(refs) & case.relevant_refs) / len(case.relevant_refs)
    if not bounded:
        result["shadow"] = _quality([], case.relevant_refs)
        return finish("no_authorized_candidates", complete=True)
    if scorer is None or not callable(getattr(scorer, "score", None)):
        return finish("shadow_scorer_not_supplied")
    # Initial filtering may have consumed time or observed other records. Check
    # the bounded batch again before projecting any model-visible text.
    try:
        for record, ref in zip(bounded, refs, strict=True):
            if expired():
                return finish("shadow_deadline_exceeded")
            if (record.status != "active" or _ref(record) != ref or not validate(record)
                    or record_digest(record) != initial_digests[ref]):
                return finish("authority_changed")
    except Exception:
        return finish("authority_check_failed")
    try:
        snapshots, texts = [], []
        for record in bounded:
            if expired():
                return finish("shadow_deadline_exceeded")
            snapshots.append(record_digest(record))
            texts.append(authoritative_text(record, max_chars=config.max_text_chars))
    except Exception:
        return finish("shadow_projection_invalid")
    remaining = deadline - perf_counter()
    if remaining < .05:
        return finish("shadow_deadline_exceeded")
    try:
        result["model_calls"] = 1
        scores = scorer.score(case.query, texts, timeout_seconds=remaining, deadline_at=deadline)
        if expired():
            return finish("shadow_deadline_exceeded")
        if not isinstance(scores, (list, tuple)) or len(scores) != len(bounded):
            raise RelevanceUnavailable("reranker_response_invalid")
        scores = validate_scores([{"index": index, "score": score} for index, score in enumerate(scores)], len(bounded))
    except RelevanceUnavailable as exc:
        return finish(str(exc) if str(exc) in _SAFE_SCORER_ERRORS else "shadow_scorer_failed")
    except Exception:
        return finish("shadow_scorer_failed")
    try:
        for record, ref, digest in zip(bounded, refs, snapshots, strict=True):
            if expired():
                return finish("shadow_deadline_exceeded")
            if (record.status != "active" or _ref(record) != ref or not validate(record)
                    or record_digest(record) != digest):
                return finish("authority_changed")
    except Exception:
        return finish("authority_check_failed")
    if expired():
        return finish("shadow_deadline_exceeded")
    # Equal raw scores retain the original candidate order deterministically.
    shadow_refs = [refs[index] for index in sorted(range(len(refs)), key=lambda i: (-scores[i], i))]
    result["shadow_order"] = [_token(ref) for ref in shadow_refs]
    result["shadow"] = _quality(shadow_refs, case.relevant_refs)
    return finish(complete=True)


def evaluate_reranker_shadow(
    cases: Sequence[ShadowCase], *, validate: Callable[[RecordEnvelope], bool],
    scorer=None, config: ShadowConfig = ShadowConfig(), deadline_at: float = 0.0,
) -> dict:
    """Compare an explicitly supplied scorer with frozen fused-order candidates.

    This function never enters the recall engine or changes a delivered order.
    A caller must first obtain exact authorized references from its controlled
    retrieval, then validate the native snapshot before and after model scoring.
    P95 is nearest-rank over every attempted case, including failures. Quality
    averages use labelled *paired* cases only; failure counts stay visible.
    """
    report = {
        "schema_version": "reranker_shadow.v1", "mode": "offline_shadow",
        "status": "disabled", "affects_delivery": False,
        "evidence_origin": "caller_supplied_offline_cases", "config": asdict(config),
        "candidate_k": config.max_candidates, "quality_k": 5, "model_calls": 0,
        "case_count": 0, "paired_case_count": 0, "unavailable_case_count": 0,
        "cases": [], "baseline": None, "shadow": None, "candidate_recall_at_k": None,
        "latency_ms": _latencies([]),
    }
    if not config.enabled:
        return report
    if not isinstance(cases, (list, tuple)) or any(not isinstance(case, ShadowCase) for case in cases[:config.max_cases]):
        raise ValueError("shadow_cases_invalid")
    if (not callable(validate) or type(deadline_at) not in (int, float)
            or not math.isfinite(deadline_at) or deadline_at < 0):
        raise ValueError("shadow_authority_or_deadline_invalid")
    started = perf_counter()
    batch_deadline = started + config.max_batch_seconds
    if deadline_at > 0:
        batch_deadline = min(batch_deadline, deadline_at)
    chosen_cases = cases[:config.max_cases]
    if len({case.case_id for case in chosen_cases}) != len(chosen_cases):
        raise ValueError("shadow_duplicate_case_id")
    scorer_config = getattr(scorer, "config", None)
    report["configured_scorer"] = (
        scorer_config.identity() if isinstance(scorer_config, RelevanceConfig)
        else {"kind": "explicit_injected_scorer", "model_identity": "not_supplied"}
    )
    rows = [_compare_case(case, config=config, scorer=scorer, validate=validate, batch_deadline=batch_deadline)
            for case in chosen_cases]
    paired = [row for row in rows if row["status"] == "complete" and row["baseline"] is not None]
    unavailable = sum(row["status"] == "unavailable" for row in rows)
    recalls = [row["candidate_recall_at_k"] for row in rows if row["candidate_recall_at_k"] is not None]
    report.update(
        status="unavailable" if rows and unavailable == len(rows) else "partial" if unavailable else "complete",
        model_calls=sum(row["model_calls"] for row in rows), case_count=len(rows),
        case_budget_dropped_count=len(cases) - len(chosen_cases), paired_case_count=len(paired),
        unavailable_case_count=unavailable, cases=rows,
        baseline=_mean_quality([row["baseline"] for row in paired]),
        shadow=_mean_quality([row["shadow"] for row in paired]),
        candidate_recall_at_k=sum(recalls) / len(recalls) if recalls else None,
        latency_ms=_latencies([row["elapsed_ms"] for row in rows]),
        elapsed_ms=round(max(0, perf_counter() - started) * 1000, 3),
    )
    return report
