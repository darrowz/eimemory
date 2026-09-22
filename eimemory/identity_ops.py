from __future__ import annotations
# EXT-08 FIXED: identity repair/report accept scope filter

from collections.abc import Iterator
from typing import Any

from eimemory.identity import (
    build_identity_report,
    needs_hongtu_identity_repair,
    normalize_hongtu_record,
)
from eimemory.models.records import RecordEnvelope


def identity_report(runtime, *, limit: int | None = None, scope=None) -> dict[str, Any]:
    return build_identity_report(_iter_records(runtime, limit=limit, scope=scope))


def repair_hongtu_identity(
    runtime,
    *,
    apply: bool = False,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> dict[str, Any]:
    if not apply:
        report = build_identity_report(
            _iter_records(
                runtime,
                limit=limit,
                scope=scope,
                skip_created_at_or_after=skip_created_at_or_after,
            )
        )
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

    candidate_ids: list[str] = []
    report = build_identity_report(
        _capture_repair_candidates(
            _iter_records(
                runtime,
                limit=limit,
                scope=scope,
                skip_created_at_or_after=skip_created_at_or_after,
            ),
            candidate_ids=candidate_ids,
        )
    )
    repaired_ids: list[str] = []
    skipped_fresh = 0
    for record_id in candidate_ids:
        record = runtime.store.get_by_id(record_id)
        if record is None or not needs_hongtu_identity_repair(record):
            continue
        if skip_created_at_or_after and _record_at_or_after(record, skip_created_at_or_after):
            skipped_fresh += 1
            continue
        if bool((record.meta or {}).get("identity_stamped_on_ingest")):
            skipped_fresh += 1
            continue
        normalized = normalize_hongtu_record(record)
        runtime.store.rewrite(normalized, previous_scope=record.scope)
        repaired_ids.append(normalized.record_id)
    if candidate_ids:
        report = build_identity_report(
            _iter_records(
                runtime,
                limit=limit,
                scope=scope,
                skip_created_at_or_after=skip_created_at_or_after,
            )
        )
    report.update(
        {
            "ok": True,
            "apply": True,
            "candidate_count": len(candidate_ids),
            "repaired_count": len(repaired_ids),
            "repaired_record_ids": repaired_ids[:100],
            "skipped_fresh_count": skipped_fresh,
            "scoped": scope is not None,
        }
    )
    return report


def _record_at_or_after(record: RecordEnvelope, bound: str) -> bool:
    stamp = str(getattr(record, "created_at", "") or getattr(record, "updated_at", "") or "")
    return bool(stamp and bound and stamp >= bound)


def _capture_repair_candidates(
    records: Iterator[RecordEnvelope],
    *,
    candidate_ids: list[str],
) -> Iterator[RecordEnvelope]:
    for record in records:
        if needs_hongtu_identity_repair(record):
            candidate_ids.append(record.record_id)
        yield record


def _iter_records(
    runtime,
    *,
    limit: int | None = None,
    scope=None,
    skip_created_at_or_after: str | None = None,
) -> Iterator[RecordEnvelope]:
    page_size = 500
    offset = 0
    yielded_count = 0
    target_limit = None if limit is None or limit <= 0 else int(limit)
    while True:
        remaining = page_size if target_limit is None else max(0, min(page_size, target_limit - yielded_count))
        if remaining <= 0:
            break
        page = runtime.store.list_records(limit=remaining, offset=offset, scope=scope)
        if not page:
            break
        page_count = len(page)
        for record in page:
            if skip_created_at_or_after and _record_at_or_after(record, skip_created_at_or_after):
                continue
            if bool((getattr(record, "meta", {}) or {}).get("identity_stamped_on_ingest")):
                # Already stamped at ingest — never a repair candidate.
                if not needs_hongtu_identity_repair(record):
                    continue
            yield record
            yielded_count += 1
        del record
        page.clear()
        offset += page_count
