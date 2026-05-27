from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Mapping

from eimemory.models.relation_records import RelationRecord
from eimemory.models.records import LinkRef

SUPERSEDES = "supersedes"
REPEATS = "repeats"
BELONGS_TO_PHASE = "belongs_to_phase"
ANTICIPATES = "anticipates"
REPAIRS = "repairs"
REINFORCES = "reinforces"

TEMPORAL_RELATIONS = (
    SUPERSEDES,
    REPEATS,
    BELONGS_TO_PHASE,
    ANTICIPATES,
    REPAIRS,
    REINFORCES,
)

LIVING_META_KEY = "living_memory_v1"
TEMPORAL_META_KEY = "temporal"
DEFAULT_TEMPORAL_SOURCE_ID = "living_memory_v1"


def build_temporal_link(relation: str, target_id: str, target_kind: str = "record") -> LinkRef:
    """Return a validated temporal LinkRef."""
    _validate_temporal_relation(relation)
    return LinkRef(relation=relation, target_kind=target_kind, target_id=str(target_id))


def build_temporal_relation_payload(
    relation: str,
    *,
    subject_id: str,
    object_id: str,
    evidence_text: str = "",
    confidence: float = 0.5,
    metadata: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    relation_record_id: str | None = None,
    paper_source_id: str = DEFAULT_TEMPORAL_SOURCE_ID,
) -> dict[str, Any]:
    """Return a RelationRecord payload for a temporal relation without persisting it."""
    _validate_temporal_relation(relation)
    payload_id = relation_record_id or f"temporal_relation:{subject_id}:{relation}:{object_id}"
    return RelationRecord(
        relation_record_id=payload_id,
        paper_source_id=paper_source_id,
        subject_id=str(subject_id),
        object_id=str(object_id),
        relation_type=relation,
        evidence_text=evidence_text,
        confidence=confidence,
        metadata=dict(metadata or {}),
        provenance=dict(provenance or {}),
    ).to_payload()


def summarize_timeline(records: Iterable[Any]) -> dict[str, Any]:
    """Return compact temporal counts and maps for living memory records."""
    phase_counts: Counter[str] = Counter()
    recurrence_counts: Counter[str] = Counter()
    open_future_intents: list[dict[str, str]] = []
    supersession_map: dict[str, str] = {}
    unresolved_repair_count = 0

    for record in records:
        record_id = _record_value(record, "record_id")
        temporal = _temporal_meta(record)

        life_phase = _clean_string(temporal.get("life_phase"))
        if life_phase:
            phase_counts[life_phase] += 1

        recurrence = _clean_string(temporal.get("recurrence"))
        if recurrence:
            recurrence_counts[recurrence] += 1

        future_intent = _open_future_intent(record, temporal)
        if future_intent:
            open_future_intents.append(future_intent)

        if _is_unresolved_repair(temporal):
            unresolved_repair_count += 1

        for link in _record_links(record):
            relation = _link_value(link, "relation")
            target_id = _link_value(link, "target_id")
            if relation == SUPERSEDES and record_id and target_id:
                supersession_map[str(target_id)] = str(record_id)

    return {
        "phase_counts": dict(phase_counts),
        "recurrence_counts": dict(recurrence_counts),
        "open_future_intents": open_future_intents,
        "unresolved_repair_count": unresolved_repair_count,
        "supersession_map": supersession_map,
    }


def _validate_temporal_relation(relation: str) -> None:
    if relation not in TEMPORAL_RELATIONS:
        raise ValueError(f"unknown temporal relation: {relation}")


def _temporal_meta(record: Any) -> dict[str, Any]:
    meta = _record_value(record, "meta")
    if not isinstance(meta, Mapping):
        return {}
    living = meta.get(LIVING_META_KEY)
    if not isinstance(living, Mapping):
        return {}
    temporal = living.get(TEMPORAL_META_KEY)
    if not isinstance(temporal, Mapping):
        return {}
    return dict(temporal)


def _record_links(record: Any) -> list[Any]:
    links = _record_value(record, "links")
    if not isinstance(links, list):
        return []
    return links


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _link_value(link: Any, key: str) -> Any:
    if isinstance(link, Mapping):
        return link.get(key)
    if is_dataclass(link):
        return asdict(link).get(key)
    return getattr(link, key, None)


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _open_future_intent(record: Any, temporal: Mapping[str, Any]) -> dict[str, str]:
    intent = temporal.get("future_intent")
    if not intent:
        return {}

    status = ""
    text = ""
    if isinstance(intent, Mapping):
        status = _clean_string(intent.get("status") or "open").lower()
        text = _clean_string(intent.get("intent") or intent.get("text") or intent.get("summary"))
    else:
        status = "open"
        text = _clean_string(intent)

    if status not in {"open", "pending", "active"}:
        return {}

    record_id = _clean_string(_record_value(record, "record_id"))
    title = _clean_string(_record_value(record, "title"))
    return {
        "record_id": record_id,
        "title": title,
        "life_phase": _clean_string(temporal.get("life_phase")),
        "recurrence": _clean_string(temporal.get("recurrence")),
        "intent": text,
    }


def _is_unresolved_repair(temporal: Mapping[str, Any]) -> bool:
    repair = temporal.get("repair")
    if isinstance(repair, Mapping):
        status = _clean_string(repair.get("status")).lower()
    else:
        status = _clean_string(temporal.get("repair_status")).lower()
    return status in {"unresolved", "open", "pending", "active"}
