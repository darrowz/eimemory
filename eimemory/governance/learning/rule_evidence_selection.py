"""Pure ordering for already scope/target/type-filtered rule evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import math

from eimemory.models.records import RecordEnvelope


def latest_rule_evidence(records: Sequence[RecordEnvelope], *, positive_field: str,
                         positive_value: str) -> RecordEnvelope | None:
    """Select the latest event; unresolved same-time conflicts fail closed.

    Callers retain their own exact scope, target and evidence-kind filters.
    Opaque IDs break otherwise equivalent ties, never positive/negative ties.
    """
    if not records:
        return None
    dated: list[tuple[datetime, RecordEnvelope]] = []
    for record in records:
        try:
            instant = datetime.fromisoformat(record.time.updated_at.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
            dated.append((instant.astimezone(timezone.utc), record))
        except (AttributeError, TypeError, ValueError):
            return None
    latest_time = max(instant for instant, _record in dated)
    tied = [record for instant, record in dated if instant == latest_time]

    def key(record: RecordEnvelope) -> tuple[bool, float, str]:
        positive = str(record.meta.get(positive_field) or "") == positive_value
        # Two declared passes at the same instant must not select the higher
        # metric by random ID and bypass the caller's pass-rate threshold.
        rate = 0.0
        if positive_field == "verdict":
            try:
                rate = float(record.meta.get("pass_rate", 0.0))
                if not math.isfinite(rate):
                    rate = -math.inf
            except (TypeError, ValueError):
                rate = -math.inf
        return positive, rate, record.record_id

    return min(tied, key=key)
