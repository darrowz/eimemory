from __future__ import annotations

import os
from statistics import mean
from typing import Any

from eimemory.governance.harness_patch import HarnessSurface
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef

SEEDED_LEDGER_CAPABILITIES = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "code.implementation",
    "operations.uumit",
    "office.daily_task",
    "device.control",
    "research.synthesis",
    "safety.boundary",
]


def record_capability_score(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    capability: str,
    score: float,
    evidence_record_ids: list[str] | None = None,
    evidence_items: list[dict[str, Any]] | None = None,
    evidence_tiers: list[str] | None = None,
    evidence_sources: list[str] | None = None,
    regression_count: int = 0,
    meta: dict[str, Any] | None = None,
) -> str:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    sequence = _next_capability_score_sequence(runtime, scope=scope_ref, capability=capability)
    semantic_key = stable_semantic_key("capability_score", capability, loop_id, score, evidence_record_ids or [])
    content = {
        "capability": capability,
        "score": round(float(score), 3),
        "evidence_record_ids": list(evidence_record_ids or []),
        "evidence_items": list(evidence_items or []),
        "evidence_tiers": list(evidence_tiers or []),
        "evidence_sources": list(evidence_sources or []),
        "regression_count": regression_count,
        "score_sequence": sequence,
    }
    merged_meta: dict[str, Any] = {
        "capability": capability,
        "score": round(float(score), 3),
        "score_sequence": sequence,
        "evidence_count": len(evidence_record_ids or []),
        "evidence_tiers": list(evidence_tiers or []),
        "evidence_sources": list(evidence_sources or []),
        "regression_count": regression_count,
    }
    if meta:
        merged_meta.update(meta)
        if isinstance(meta.get("proposal_card"), dict):
            content["proposal_card"] = dict(meta["proposal_card"])
    tier = str(merged_meta.get("authority_tier") or "L0")
    # Re-read HARNESS_PATCH_V2 at call time so monkeypatch.setenv in tests takes
    # effect (the module-level constant in harness_patch is captured at import).
    if os.environ.get("HARNESS_PATCH_V2") == "1" and meta and str(meta.get("kind") or "") == "candidate_promotion":
        card = content.get("proposal_card") if isinstance(content, dict) else None
        if not card or not isinstance(card, dict):
            raise ValueError(
                "proposal_card is required for candidate_promotion under HARNESS_PATCH_V2"
            )
        surface = str(card.get("target_surface") or "")
        if surface not in {s.value for s in HarnessSurface}:
            raise ValueError(f"proposal_card.target_surface invalid: {surface!r}")
    record = append_learning_record_once(
        runtime,
        kind="capability_score",
        title=f"Capability score: {capability}",
        summary=f"{capability} score {round(float(score), 3)}",
        scope=scope_ref,
        loop_id=loop_id,
        step_name="ledger",
        semantic_key=semantic_key,
        authority_tier=tier,
        status="active",
        content=content,
        meta=merged_meta,
    )
    return record.record_id


def _next_capability_score_sequence(runtime: Any, *, scope: ScopeRef, capability: str) -> int:
    counter = getattr(runtime.store, "count_records_by_meta_value", None)
    if callable(counter):
        count = counter(
            kinds=["capability_score"],
            scope=scope,
            meta_key="capability",
            meta_value=capability,
        )
        if count is not None:
            return int(count) + 1
    existing = [
        record
        for record in runtime.store.list_records(kinds=["capability_score"], scope=scope, limit=500)
        if str(record.meta.get("capability") or "") == capability
    ]
    return len(existing) + 1


def build_capability_ledger(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
    since: str | None = None,
    until: str | None = None,
    ensure_seeded: bool = False,
    attribute_outcomes: bool = True,
) -> dict[str, Any]:
    if ensure_seeded:
        from eimemory.governance.capability_seeding import ensure_all_seeded

        ensure_all_seeded(runtime, scope=scope, loop_id="seed")

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if attribute_outcomes:
        try:
            from eimemory.governance.capability_attribution import attribute_capability_outcomes

            attribute_capability_outcomes(runtime, scope=scope_ref, loop_id="outcome_attribution", limit=limit)
        except Exception:
            pass
    normalized_since = _normalize_date_bound(since, end_of_day=False)
    normalized_until = _normalize_date_bound(until, end_of_day=True)
    records = runtime.store.list_records(
        kinds=["capability_score"],
        scope=scope_ref,
        limit=limit,
        since=normalized_since,
        until=normalized_until,
    )
    records = [
        record
        for record in records
        if not _is_legacy_unexecuted_replay_score(runtime, record=record, scope=scope_ref)
        and not _is_candidate_gate_failure_score(record)
    ]
    by_capability: dict[str, list[RecordEnvelope]] = {}
    for record in records:
        by_capability.setdefault(str(record.meta.get("capability") or "general"), []).append(record)
    capabilities = {}
    for capability, items in by_capability.items():
        ordered = sorted(
            items,
            key=lambda item: (
                int(item.meta.get("score_sequence") or item.content.get("score_sequence") or 0),
                item.time.updated_at,
                item.time.created_at,
            ),
            reverse=True,
        )
        scores = [float(item.meta.get("score") or 0.0) for item in ordered]
        evidence_record_ids = sorted({value for item in ordered for value in _record_list(item, "evidence_record_ids")})
        evidence_count = len(evidence_record_ids) if evidence_record_ids else sum(int(item.meta.get("evidence_count") or 0) for item in ordered)
        regression_count = sum(int(item.meta.get("regression_count") or 0) for item in ordered)
        evidence_tiers = sorted({value for item in ordered for value in _record_list(item, "evidence_tiers")})
        evidence_sources = sorted({value for item in ordered for value in _record_list(item, "evidence_sources")})
        evidence_source_counts = _evidence_source_counts(ordered)
        latest_score = scores[0] if scores else 0.0
        confidence = _ledger_confidence(score=latest_score, evidence_count=evidence_count)
        capabilities[capability] = {
            "score": latest_score,
            "average": round(mean(scores), 3) if scores else 0.0,
            "trend": round(scores[0] - scores[-1], 3) if len(scores) >= 2 else 0.0,
            "evidence_count": evidence_count,
            "evidence_record_ids": evidence_record_ids,
            "regression_count": regression_count,
            "evidence_tiers": evidence_tiers,
            "evidence_sources": evidence_sources,
            "evidence_source_counts": evidence_source_counts,
            "confidence": confidence,
            "status": _ledger_status(score=latest_score, evidence_count=evidence_count),
            "needs_outcome_recalculation": bool(latest_score < 0.5 or evidence_count < 3),
            "goal_gap_reason": _goal_gap_reason(score=latest_score, evidence_count=evidence_count),
            "last_record_id": ordered[0].record_id if ordered else "",
        }
    for capability in SEEDED_LEDGER_CAPABILITIES:
        capabilities.setdefault(
            capability,
            {
                "score": 0.0,
                "average": 0.0,
                "trend": 0.0,
                "evidence_count": 0,
                "evidence_record_ids": [],
                "regression_count": 0,
                "evidence_tiers": [],
                "evidence_sources": [],
                "evidence_source_counts": {},
                "confidence": "none",
                "status": "stale_unverified",
                "needs_outcome_recalculation": True,
                "goal_gap_reason": "no_outcome_evidence",
                "last_record_id": "",
            },
        )
    return {
        "ok": True,
        "capabilities": capabilities,
        "record_count": len(records),
        "query": {
            "limit": max(0, int(limit)),
            "since": normalized_since,
            "until": normalized_until,
        },
    }


def _ledger_confidence(*, score: float, evidence_count: int) -> str:
    if evidence_count <= 0:
        return "none"
    if evidence_count < 3:
        return "low"
    if score < 0.5:
        return "low"
    return "medium" if evidence_count < 10 else "high"


def _ledger_status(*, score: float, evidence_count: int) -> str:
    if evidence_count <= 0:
        return "stale_unverified"
    if evidence_count < 3:
        return "needs_outcome_recalculation"
    if score < 0.5:
        return "needs_outcome_recalculation"
    return "active"


def _goal_gap_reason(*, score: float, evidence_count: int) -> str:
    if evidence_count <= 0:
        return "no_outcome_evidence"
    if evidence_count < 3:
        return "insufficient_outcome_evidence"
    if score < 0.5:
        return "low_outcome_score"
    return ""


def _record_list(record: RecordEnvelope, key: str) -> list[str]:
    value = record.meta.get(key)
    if not isinstance(value, list):
        value = record.content.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _is_legacy_unexecuted_replay_score(runtime: Any, *, record: RecordEnvelope, scope: ScopeRef) -> bool:
    if str(record.meta.get("kind") or "") != "capability_replay_pack":
        return False
    if float(record.meta.get("score") or record.content.get("score") or 0.0) != 0.0:
        return False
    evidence_ids = _record_list(record, "evidence_record_ids")
    if not evidence_ids:
        return False
    for evidence_id in evidence_ids:
        evidence = runtime.store.get_by_id(evidence_id, scope=scope)
        if evidence is None:
            return False
        if str(evidence.meta.get("report_type") or "") != "capability_replay_pack":
            return False
        if str(evidence.meta.get("verdict") or evidence.content.get("verdict") or "") != "not_run":
            return False
    return True


def _is_candidate_gate_failure_score(record: RecordEnvelope) -> bool:
    if str(record.meta.get("kind") or "") != "autonomous_learning_measured":
        return False
    if float(record.meta.get("score") or record.content.get("score") or 0.0) != 0.0:
        return False
    return (
        str(record.meta.get("eval_verdict") or "").strip().lower() in {"fail", "failed", "blocked"}
        or record.meta.get("replay_gate_passed") is False
        or record.meta.get("safety_gate_passed") is False
        or record.meta.get("isolation_gate_passed") is False
    )


def _evidence_source_counts(records: list[RecordEnvelope]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        items = record.content.get("evidence_items") if isinstance(record.content, dict) else None
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source_kind") or "").strip()
                if source:
                    counts[source] = counts.get(source, 0) + 1
            continue
        evidence_count = int(record.meta.get("evidence_count") or len(_record_list(record, "evidence_record_ids")) or 0)
        for source in _record_list(record, "evidence_sources"):
            counts[source] = counts.get(source, 0) + max(1, evidence_count)
    return dict(sorted(counts.items()))


def _normalize_date_bound(value: str | None, *, end_of_day: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw}T23:59:59.999999+00:00" if end_of_day else f"{raw}T00:00:00+00:00"
    return raw
