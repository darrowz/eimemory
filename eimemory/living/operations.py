from __future__ import annotations
# RSC-23 FIXED: persist before/after digests on enrich rewrite
# EXT-22 FIXED: summarize_living_memory can persist report when store given
# EXT-07 FIXED: multi-page enrich with CAS rewrite + incomplete flag

from dataclasses import asdict
from typing import Any

from eimemory.living.schema import get_living_memory_meta, has_living_memory_meta, with_living_memory_meta
from eimemory.living.posture import compile_living_posture_report
from eimemory.models.records import RecordEnvelope, ScopeRef


def enrich_memory_records(
    runtime,
    *,
    scope: dict | ScopeRef | None = None,
    limit: int = 100,
    max_pages: int = 20,
    page_size: int | None = None,
) -> dict[str, Any]:
    """EXT-07/RSC-23: page through records, CAS rewrite, persist before/after digests."""
    if limit <= 0:
        return {"ok": False, "error": "invalid_limit"}
    scope_ref = _scope_ref(scope)
    page = max(1, min(int(page_size or limit), 500))
    pages = max(1, min(int(max_pages), 100))
    enriched_ids: list[str] = []
    skipped_count = 0
    scanned_count = 0
    digest_log: list[dict[str, str]] = []
    incomplete = False
    offset = 0
    remaining = int(limit)
    for _ in range(pages):
        if remaining <= 0:
            break
        batch = runtime.store.list_records(
            kinds=["memory"], scope=scope_ref, limit=min(page, remaining), offset=offset
        )
        if not batch:
            break
        scanned_count += len(batch)
        for record in batch:
            if has_living_memory_meta(record):
                skipped_count += 1
                continue
            before = _record_digest(record)
            record.meta = with_living_memory_meta(record)
            record.touch()
            after = _record_digest(record)
            # CAS-style rewrite: require current exact ref still matches before write.
            current = runtime.store.get_by_id(record.record_id, scope=record.scope)
            if current is None or _record_digest(current) != before:
                skipped_count += 1
                continue
            runtime.store.rewrite(record, previous_scope=record.scope)
            enriched_ids.append(record.record_id)
            digest_log.append(
                {
                    "record_id": record.record_id,
                    "before_digest": before,
                    "after_digest": after,
                }
            )
        offset += len(batch)
        remaining -= len(batch)
        if len(batch) < min(page, remaining + len(batch)):
            break
    else:
        if remaining > 0:
            incomplete = True
    return {
        "ok": True,
        "scope": asdict(scope_ref),
        "scanned_count": scanned_count,
        "enriched_count": len(enriched_ids),
        "skipped_count": skipped_count,
        "record_ids": enriched_ids,
        "digests": digest_log[:100],
        "incomplete": incomplete,
    }


def build_living_timeline(runtime, *, scope: dict | ScopeRef | None = None, limit: int = 100) -> dict[str, Any]:
    if limit <= 0:
        return {"ok": False, "error": "invalid_limit"}
    scope_ref = _scope_ref(scope)
    records = runtime.store.list_records(kinds=["memory"], scope=scope_ref, limit=limit)
    phase_counts: dict[str, int] = {}
    recurrence_counts: dict[str, int] = {}
    future_intents: list[dict[str, str]] = []
    repair_needed_count = 0
    for record in records:
        living = get_living_memory_meta(record)
        temporal = living.get("temporal") if isinstance(living.get("temporal"), dict) else {}
        affective = living.get("affective") if isinstance(living.get("affective"), dict) else {}
        phase = _clean_text(temporal.get("life_phase"))
        recurrence = _clean_text(temporal.get("recurrence"))
        if phase and phase != "unspecified":
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if recurrence and recurrence != "none":
            recurrence_counts[recurrence] = recurrence_counts.get(recurrence, 0) + 1
        if bool(affective.get("repair_needed")):
            repair_needed_count += 1
        future_intent = temporal.get("future_intent") if isinstance(temporal.get("future_intent"), dict) else {}
        if future_intent and _clean_text(future_intent.get("status") or "open").lower() not in {"closed", "done", "resolved"}:
            future_intents.append(
                {
                    "record_id": record.record_id,
                    "title": record.title,
                    "life_phase": phase,
                    "recurrence": recurrence,
                    "intent": _clean_text(future_intent.get("intent") or future_intent.get("text")),
                }
            )
    return {
        "ok": True,
        "scope": asdict(scope_ref),
        "record_count": len(records),
        "by_life_phase": dict(sorted(phase_counts.items())),
        "by_recurrence": dict(sorted(recurrence_counts.items())),
        "repair_needed_count": repair_needed_count,
        "future_intent_count": len(future_intents),
        "future_intents": future_intents[:20],
    }


def recommend_action_posture(
    runtime,
    query: str,
    *,
    scope: dict | ScopeRef | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    return compile_living_posture_report(runtime, query=query, scope=scope, limit=limit)


def summarize_living_memory(records: list[RecordEnvelope], *, store=None, scope: dict | ScopeRef | None = None, persist: bool = False) -> dict[str, Any]:
    enriched = [record for record in records if has_living_memory_meta(record)]
    phase_counts: dict[str, int] = {}
    future_intent_count = 0
    repair_needed_count = 0
    ripeness_scores = [
        score
        for record in enriched
        for score in [_ripeness_score(get_living_memory_meta(record).get("action_posture", {}).get("ripeness"))]
        if score is not None
    ]
    for record in enriched:
        living = get_living_memory_meta(record)
        temporal = living.get("temporal") if isinstance(living.get("temporal"), dict) else {}
        affective = living.get("affective") if isinstance(living.get("affective"), dict) else {}
        phase = _clean_text(temporal.get("life_phase"))
        if phase and phase != "unspecified":
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if bool(affective.get("repair_needed")):
            repair_needed_count += 1
        future_intent = temporal.get("future_intent") if isinstance(temporal.get("future_intent"), dict) else {}
        if future_intent and _clean_text(future_intent.get("status") or "open").lower() not in {"closed", "done", "resolved"}:
            future_intent_count += 1
    summary = {
        "record_count": len(records),
        "enriched_count": len(enriched),
        "repair_needed_count": repair_needed_count,
        "future_intent_count": future_intent_count,
        "by_life_phase": dict(sorted(phase_counts.items())),
        "average_ripeness": round(sum(ripeness_scores) / len(ripeness_scores), 3) if ripeness_scores else 0.0,
    }
    if persist and store is not None:
        from eimemory.models.records import RecordEnvelope as _RE
        from eimemory.core.clock import now_iso
        report = _RE.create(
            kind="reflection",
            title="living memory summary",
            summary=f"enriched={summary['enriched_count']} repair={summary['repair_needed_count']}",
            content={"living_summary": summary, "schema": "living_summary.v1"},
            scope=_scope_ref(scope),
            source="eimemory.living.summarize",
            meta={"report_type": "living_summary"},
        )
        store.append(report)
        summary["persisted_record_id"] = report.record_id
        summary["persisted_at"] = now_iso()
    return summary


def _scope_ref(scope: dict | ScopeRef | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _ripeness_score(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    normalized = _clean_text(value).lower()
    if not normalized:
        return None
    return {
        "low": 0.25,
        "medium": 0.5,
        "normal": 0.5,
        "high": 1.0,
    }.get(normalized, 0.0)


def _record_digest(record: RecordEnvelope) -> str:
    from hashlib import sha256
    import json
    payload = {
        "record_id": record.record_id,
        "updated_at": record.time.updated_at,
        "status": record.status,
        "meta": dict(record.meta or {}),
    }
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
