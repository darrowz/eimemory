from __future__ import annotations

from typing import Any

from eimemory.identity import (
    build_identity_report,
    needs_hongtu_identity_repair,
    normalize_hongtu_record,
)
from eimemory.models.records import RecordEnvelope


def identity_report(runtime, *, limit: int | None = None) -> dict[str, Any]:
    records = _list_records(runtime, limit=limit)
    return build_identity_report(records)


def repair_hongtu_identity(runtime, *, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    records = _list_records(runtime, limit=limit)
    candidates = [record for record in records if needs_hongtu_identity_repair(record)]
    repaired_ids: list[str] = []
    for record in candidates:
        if not apply:
            continue
        normalized = normalize_hongtu_record(record)
        runtime.store.rewrite(normalized, previous_scope=record.scope)
        repaired_ids.append(normalized.record_id)
    report = build_identity_report(_list_records(runtime, limit=limit) if apply else records)
    report.update(
        {
            "ok": True,
            "apply": bool(apply),
            "candidate_count": len(candidates),
            "repaired_count": len(repaired_ids),
            "repaired_record_ids": repaired_ids[:100],
        }
    )
    return report


def _list_records(runtime, *, limit: int | None = None) -> list[RecordEnvelope]:
    page_size = 500
    offset = 0
    records: list[RecordEnvelope] = []
    target_limit = None if limit is None or limit <= 0 else int(limit)
    while True:
        remaining = page_size if target_limit is None else max(0, min(page_size, target_limit - len(records)))
        if remaining <= 0:
            break
        page = runtime.store.list_records(limit=remaining, offset=offset)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records
