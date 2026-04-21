from __future__ import annotations

from typing import Iterable

from eimemory.models.recall_views import RecallView
from eimemory.models.records import RecordEnvelope


def build_claim_centered_view(
    *,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    items = [
        *_view_items(claims),
        *_view_items(pages),
        *_view_items(memories),
    ]
    return RecallView(view_type="claim_centered", items=tuple(items), query=query)


def build_page_centered_view(
    *,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    items = [
        *_view_items(pages),
        *_view_items(claims),
        *_view_items(memories),
    ]
    return RecallView(view_type="page_centered", items=tuple(items), query=query)


def build_mixed_view(
    *,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    items = _interleave([list(_view_items(memories)), list(_view_items(claims)), list(_view_items(pages))])
    return RecallView(view_type="mixed", items=tuple(items), query=query)


def build_contradiction_view(
    *,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    items = [
        item
        for item in _view_items([*claims, *pages, *memories])
        if item.get("contradiction_ids") or item.get("status") == "conflicted"
    ]
    return RecallView(view_type="contradiction", items=tuple(items), query=query)


def build_freshness_view(
    *,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    items = sorted(
        _view_items([*claims, *pages, *memories]),
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return RecallView(view_type="freshness", items=tuple(items), query=query)


def build_recall_view(
    *,
    view_type: str,
    claims: Iterable[RecordEnvelope],
    pages: Iterable[RecordEnvelope],
    memories: Iterable[RecordEnvelope] = (),
    query: str = "",
) -> RecallView:
    if view_type == "claim_centered":
        return build_claim_centered_view(claims=claims, pages=pages, memories=memories, query=query)
    if view_type == "page_centered":
        return build_page_centered_view(claims=claims, pages=pages, memories=memories, query=query)
    if view_type == "contradiction":
        return build_contradiction_view(claims=claims, pages=pages, memories=memories, query=query)
    if view_type == "freshness":
        return build_freshness_view(claims=claims, pages=pages, memories=memories, query=query)
    return build_mixed_view(claims=claims, pages=pages, memories=memories, query=query)


def choose_view_type(task_context: dict) -> str:
    explicit = str(task_context.get("recall_view") or task_context.get("memory_view") or "").strip()
    if explicit in {"claim_centered", "page_centered", "mixed", "contradiction", "freshness"}:
        return explicit
    haystack = " ".join(str(task_context.get(key) or "") for key in ("intent", "task_type", "goal")).lower()
    if any(marker in haystack for marker in ["research", "explain", "summarize", "synthesis"]):
        return "page_centered"
    if haystack.strip():
        return "claim_centered"
    return "mixed"


def records_from_view(view: RecallView, source_records: list[RecordEnvelope], *, limit: int) -> list[RecordEnvelope]:
    by_id = {record.record_id: record for record in source_records}
    ordered: list[RecordEnvelope] = []
    for item in view.items:
        record = by_id.get(str(item.get("record_id") or ""))
        if record is not None:
            ordered.append(record)
        if len(ordered) >= limit:
            break
    return ordered


def _view_items(records: Iterable[RecordEnvelope]) -> list[dict]:
    result: list[dict] = []
    for record in records:
        result.append(
            {
                "record_id": record.record_id,
                "kind": record.kind,
                "title": record.title,
                "summary": record.summary,
                "status": record.status,
                "updated_at": record.time.updated_at,
                "supporting_claim_ids": list(record.content.get("supporting_claim_ids") or []),
                "contradiction_ids": list(record.content.get("contradiction_ids") or []),
                "source_ids": list(record.content.get("source_ids") or []),
            }
        )
    return result


def _interleave(groups: list[list[dict]]) -> list[dict]:
    result: list[dict] = []
    max_len = max((len(group) for group in groups), default=0)
    seen: set[str] = set()
    for index in range(max_len):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            record_id = str(item.get("record_id") or "")
            if record_id in seen:
                continue
            seen.add(record_id)
            result.append(item)
    return result
