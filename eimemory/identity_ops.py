from __future__ import annotations

from typing import Any

from eimemory.identity import (
    build_identity_report,
    needs_hongtu_identity_repair,
    normalize_hongtu_record,
)
from eimemory.models.records import RecordEnvelope


def identity_report(runtime, *, limit: int | None = None) -> dict[str, Any]:
    return build_identity_report(_iter_records(runtime, limit=limit))


def repair_hongtu_identity(runtime, *, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    if not apply:
        report = build_identity_report(_iter_records(runtime, limit=limit))
        report.update(
            {
                "ok": True,
                "apply": False,
                "candidate_count": report["repair_candidate_count"],
                "repaired_count": 0,
                "repaired_record_ids": [],
            }
        )
        return report

    candidate_ids = [
        record.record_id
        for record in _iter_records(runtime, limit=limit)
        if needs_hongtu_identity_repair(record)
    ]
    repaired_ids: list[str] = []
    for record_id in candidate_ids:
        record = runtime.store.get_by_id(record_id)
        if record is None or not needs_hongtu_identity_repair(record):
            continue
        normalized = normalize_hongtu_record(record)
        runtime.store.rewrite(normalized, previous_scope=record.scope)
        repaired_ids.append(normalized.record_id)
    report = build_identity_report(_iter_records(runtime, limit=limit))
    report.update(
        {
            "ok": True,
            "apply": True,
            "candidate_count": len(candidate_ids),
            "repaired_count": len(repaired_ids),
            "repaired_record_ids": repaired_ids[:100],
        }
    )
    return report


def _iter_records(runtime, *, limit: int | None = None):
    page_size = 500
    offset = 0
    yielded_count = 0
    target_limit = None if limit is None or limit <= 0 else int(limit)
    while True:
        remaining = page_size if target_limit is None else max(0, min(page_size, target_limit - yielded_count))
        if remaining <= 0:
            break
        page = runtime.store.list_records(limit=remaining, offset=offset)
        if not page:
            break
        page_count = len(page)
        for record in page:
            yield record
            yielded_count += 1
        del record
        page.clear()
        offset += page_count
