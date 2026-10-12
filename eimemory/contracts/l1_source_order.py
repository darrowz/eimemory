"""Source observation order for explicit L1 supersession, without model authority."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any, Callable

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


class L1SourceOrderBlocked(ValueError):
    """The actual source observations do not authorize this supersession."""

    def __init__(self, code: str, *, target: RecordEnvelope | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.target = target


def l1_source_material_digest(parent: RecordEnvelope) -> str:
    meta = business_metadata(parent.meta)
    material = {
        "record_id": parent.record_id,
        "source_id": parent.source_id,
        "scope": asdict(parent.scope),
        "kind": parent.kind,
        "title": parent.title,
        "source": parent.source,
        "content": parent.content,
        "summary": parent.summary,
        "detail": parent.detail,
        "occurred_at": parent.time.occurred_at,
        "extraction_metadata": {key: meta.get(key) for key in (
            "session_id", "turn_id", "memory_type", "memory_layer", "capture_origin")},
        "provenance_capture_origin": parent.provenance.get("capture_origin"),
    }
    return sha256(json.dumps(material, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def l1_source_event_binding(parent: RecordEnvelope) -> dict[str, Any]:
    return {
        "schema": "l1_source_event.v1",
        "record_id": parent.record_id,
        "input_digest": l1_source_material_digest(parent),
        "occurred_at": parent.time.occurred_at,
    }


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def validate_l1_source_event(
    record: RecordEnvelope,
    binding: dict[str, Any],
    *,
    get_exact: Callable[..., RecordEnvelope | None],
) -> RecordEnvelope:
    """Re-read the exact source inside the caller's write transaction."""
    if (record.kind != "memory" or not isinstance(binding, dict) or binding.get("schema") != "l1_source_event.v1"
            or not isinstance(binding.get("record_id"), str)
            or not binding["record_id"] or not isinstance(binding.get("input_digest"), str)
            or len(binding["input_digest"]) != 64):
        raise L1SourceOrderBlocked("l1_source_binding_invalid")
    parent_id = binding["record_id"]
    if not any(link.relation == "derived_from" and link.target_id == parent_id
               and link.target_kind in {"memory", "record"}
               for link in record.links):
        raise L1SourceOrderBlocked("l1_source_binding_invalid")
    parent = get_exact(parent_id, scope=record.scope, source_id=record.source_id,
                       preserve_unknown_occurred_at=True)
    if parent is None or parent.scope != record.scope or parent.source_id != record.source_id:
        raise L1SourceOrderBlocked("l1_source_parent_unavailable")
    require_l1_source_parent_current(parent, binding)
    if _instant(parent.time.occurred_at) is None:
        raise L1SourceOrderBlocked("l1_source_order_unknown")
    return parent


def require_l1_source_parent_current(parent: RecordEnvelope, binding: dict[str, Any]) -> None:
    """Validate an empty/skip completion as well as a child's source write."""
    from eimemory.recall.indexing import is_episode_evidence_record, is_inactive_or_superseded_record
    if parent.status != "active" or is_inactive_or_superseded_record(parent):
        raise L1SourceOrderBlocked("l1_source_parent_unavailable")
    if not is_episode_evidence_record(parent):
        raise L1SourceOrderBlocked("l1_source_binding_invalid")
    if (parent.record_id != binding.get("record_id")
            or l1_source_material_digest(parent) != binding["input_digest"]
            or parent.time.occurred_at != binding.get("occurred_at")):
        raise L1SourceOrderBlocked("l1_source_parent_changed")


def _prior_observation(
    prior: RecordEnvelope, *, get_exact: Callable[..., RecordEnvelope | None],
) -> tuple[datetime | None, str]:
    prior = get_exact(prior.record_id, scope=prior.scope, source_id=prior.source_id,
                      preserve_unknown_occurred_at=True)
    if prior is None:
        return None, ""
    meta = business_metadata(prior.meta)
    if meta.get("capture_origin") != "l1_extract":
        # A directly captured/manual correction represents its own observation.
        return _instant(prior.time.occurred_at), ""
    ids = {link.target_id for link in prior.links if link.relation == "derived_from"
           and link.target_kind in {"memory", "record"}}
    if len(ids) != 1:
        return None, ""
    parent_id = next(iter(ids))
    parent = get_exact(parent_id, scope=prior.scope, source_id=prior.source_id,
                       preserve_unknown_occurred_at=True)
    from eimemory.recall.indexing import is_inactive_or_superseded_record
    if (parent is None or parent.scope != prior.scope or parent.source_id != prior.source_id
            or parent.status != "active" or is_inactive_or_superseded_record(parent)):
        return None, parent_id
    binding = meta.get("source_event_binding")
    if binding is not None and (
        not isinstance(binding, dict) or binding.get("schema") != "l1_source_event.v1"
        or binding.get("record_id") != parent_id
        or binding.get("input_digest") != l1_source_material_digest(parent)
        or binding.get("occurred_at") != parent.time.occurred_at
    ):
        return None, parent_id
    return _instant(parent.time.occurred_at), parent_id


def require_l1_supersession_order(
    incoming: RecordEnvelope,
    prior: RecordEnvelope,
    binding: dict[str, Any],
    *,
    get_exact: Callable[..., RecordEnvelope | None],
) -> None:
    """Guard only already-related targets; this never chooses semantic targets."""
    incoming_time = _instant(binding.get("occurred_at"))
    prior_time, prior_parent_id = _prior_observation(prior, get_exact=get_exact)
    if incoming_time is None or prior_time is None:
        raise L1SourceOrderBlocked("l1_source_order_unknown", target=prior)
    if incoming_time < prior_time:
        raise L1SourceOrderBlocked("l1_source_order_stale", target=prior)
    if incoming_time == prior_time:
        same_observation = prior_parent_id == binding["record_id"]
        identical = (incoming.summary == prior.summary and incoming.content == prior.content)
        if not same_observation and not identical:
            # Equal coarse source clocks are not a chronology. Record IDs and
            # extraction time must not manufacture a later assertion.
            raise L1SourceOrderBlocked("l1_source_order_unknown", target=prior)
