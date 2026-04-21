from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _deep_freeze(value: Any) -> Any:
    value = _json_safe(value)
    if isinstance(value, dict):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


@dataclass(slots=True, frozen=True)
class PaperSource:
    paper_source_id: str
    source_kind: str
    title: str = ""
    authors: tuple[str, ...] = field(default_factory=tuple)
    abstract: str = ""
    venue: str = ""
    published_at: str = ""
    doi: str = ""
    arxiv_id: str = ""
    canonical_url: str = ""
    pdf_blob_ref: str = ""
    normalized_text_ref: str = ""
    source_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authors", tuple(str(item) for item in self.authors))
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        object.__setattr__(self, "provenance", _deep_freeze(dict(self.provenance or {})))

    def to_payload(self) -> dict[str, Any]:
        return {
            "paper_source_id": self.paper_source_id,
            "source_kind": self.source_kind,
            "title": self.title,
            "authors": list(self.authors),
            "abstract": self.abstract,
            "venue": self.venue,
            "published_at": self.published_at,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "canonical_url": self.canonical_url,
            "pdf_blob_ref": self.pdf_blob_ref,
            "normalized_text_ref": self.normalized_text_ref,
            "source_hash": self.source_hash,
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.paper_intake") -> RecordEnvelope:
        ts = now_iso()
        return RecordEnvelope(
            record_id=self.paper_source_id,
            kind="paper_source",
            status="active",
            title=self.title or self.paper_source_id,
            summary=self.abstract,
            detail="Immutable canonical paper source intake",
            content=self.to_payload(),
            tags=[],
            links=[],
            evidence=[],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "paper_source_id": self.paper_source_id,
                "source_kind": self.source_kind,
                "source_hash": self.source_hash,
                "provenance": _deep_thaw(self.provenance),
            },
        )
