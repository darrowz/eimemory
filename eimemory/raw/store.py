from __future__ import annotations

from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.chunks import chunk_text, raw_text_hash
from eimemory.storage.runtime_store import RuntimeStore


class RawEvidenceAPI:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def ingest_text(
        self,
        *,
        text: str,
        scope: ScopeRef | dict | None = None,
        source_event_id: str,
        session_id: str,
        source_type: str = "conversation",
        turn_id: str = "",
        role: str = "",
        speaker: str = "",
        occurred_at: str = "",
        max_chars: int = 1200,
        overlap_chars: int = 160,
        meta: dict[str, Any] | None = None,
    ) -> list[RecordEnvelope]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        payloads = chunk_text(
            text,
            session_id=session_id,
            source_event_id=source_event_id,
            source_type=source_type,
            turn_id=turn_id,
            role=role,
            speaker=speaker,
            occurred_at=occurred_at,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        records = [
            RecordEnvelope.create(
                kind="raw_chunk",
                title=f"Raw chunk {session_id}#{payload['chunk_index']}",
                summary=str(payload["text"])[:240],
                detail=str(payload["text"]),
                content=payload,
                tags=["raw-evidence", source_type],
                source="eimemory.raw.ingest",
                scope=scope_ref,
                meta={
                    "evidence_layer": "raw",
                    "granularity": "chunk",
                    "token_estimate": _token_estimate(str(payload["text"])),
                    **dict(meta or {}),
                },
            )
            for payload in payloads
        ]

        for index, record in enumerate(records):
            if index > 0:
                record.content["prev_chunk_id"] = records[index - 1].record_id
            if index + 1 < len(records):
                record.content["next_chunk_id"] = records[index + 1].record_id

        return [self.store.append(record) for record in records]

    def ingest_chunk(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopeRef | dict | None = None,
    ) -> RecordEnvelope:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        content = dict(payload or {})
        text = str(content.get("text") or content.get("raw_text") or "")
        source_type = str(content.get("source_type") or content.get("source") or "conversation")
        session_id = str(content.get("session_id") or "")
        chunk_index = int(content.get("chunk_index") or 0)
        content.setdefault("text", text)
        content.setdefault("raw_text", text)
        content.setdefault("source_type", source_type)
        content.setdefault("session_id", session_id)
        content.setdefault("chunk_index", chunk_index)
        content.setdefault("raw_text_hash", raw_text_hash(text) if text else "")
        record = RecordEnvelope.create(
            kind="raw_chunk",
            title=str(content.get("title") or f"Raw chunk {session_id}#{chunk_index}"),
            summary=str(content.get("summary") or text[:240]),
            detail=str(content.get("detail") or text),
            content=content,
            tags=["raw-evidence", source_type],
            source=str(content.get("record_source") or "eimemory.raw.ingest"),
            scope=scope_ref,
            meta={
                "evidence_layer": "raw",
                "granularity": str(content.get("granularity") or "chunk"),
                "token_estimate": _token_estimate(text),
                **dict(content.get("meta") or {}),
            },
        )
        return self.store.append(record)

    def search_raw_chunks(
        self,
        *,
        query: str,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        records, report = self.store.search_with_diagnostics(
            query=str(query or ""),
            kinds=["raw_chunk"],
            scope=scope_ref,
            limit=limit,
        )
        scores = {
            str(item.get("record_id") or ""): float(item.get("final_score") or 0.0)
            for item in list((report or {}).get("scored_items") or [])
            if isinstance(item, dict)
        }
        return [
            {
                "record": record,
                "base_score": scores.get(record.record_id, 0.0),
            }
            for record in records
        ]

    def search(
        self,
        *,
        query: str,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return self.search_raw_chunks(query=query, scope=scope, limit=limit)

    def context_window(
        self,
        record_id: str,
        *,
        scope: ScopeRef | dict | None = None,
        radius: int = 1,
    ) -> list[RecordEnvelope]:
        center = self.store.get_by_id(record_id, scope=scope)
        if center is None or center.kind != "raw_chunk":
            return []
        session_id = str(center.content.get("session_id") or "")
        center_index = _chunk_index(center)
        window_radius = max(0, int(radius))
        lower = center_index - window_radius
        upper = center_index + window_radius
        records = [
            record
            for record in self.store.list_records(kinds=["raw_chunk"], scope=scope, limit=1000)
            if str(record.content.get("session_id") or "") == session_id
            and lower <= _chunk_index(record) <= upper
        ]
        return sorted(records, key=_chunk_index)


def _chunk_index(record: RecordEnvelope) -> int:
    try:
        return int(record.content.get("chunk_index") or 0)
    except (TypeError, ValueError):
        return 0


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()))
