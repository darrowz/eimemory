from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from eimemory.core.clock import now_iso
from eimemory.intake.title_normalization import strip_candidate_title_prefixes
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.atomic_file import locked_json_update, read_json_strict

VALID_SOURCE_KINDS: frozenset[str] = frozenset({"paper", "news", "rss", "url", "manual"})
VALID_SOURCE_FREQUENCIES: frozenset[str] = frozenset({"daily", "weekly", "paused"})
DEFAULT_SOURCE_FREQUENCY = "daily"
DEFAULT_SOURCE_MAX_ITEMS = 10


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
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


def _normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    return sorted({str(item).strip() for item in tags if str(item).strip()})


def _normalize_ordered_text_list(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def normalize_source_strategy_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize source collection hints without turning them into a scheduler."""
    source_metadata = dict(metadata or {})
    frequency = str(source_metadata.get("frequency") or DEFAULT_SOURCE_FREQUENCY).strip().lower()
    if frequency not in VALID_SOURCE_FREQUENCIES:
        raise ValueError(f"invalid source strategy frequency: {frequency}")

    try:
        max_items = int(source_metadata.get("max_items", DEFAULT_SOURCE_MAX_ITEMS))
    except (TypeError, ValueError) as exc:
        raise ValueError("source strategy max_items must be a positive integer") from exc
    if max_items <= 0:
        raise ValueError("source strategy max_items must be a positive integer")

    normalized = dict(source_metadata)
    normalized["frequency"] = frequency
    normalized["max_items"] = max_items
    categories = _normalize_ordered_text_list(source_metadata.get("categories"))
    if categories:
        normalized["categories"] = categories
    else:
        normalized.pop("categories", None)

    for key in ("priority", "trust"):
        if key in normalized and normalized[key] is None:
            normalized.pop(key)
    return normalized


def _default_source_id(source_kind: str, uri: str, title: str) -> str:
    fingerprint = "|".join(
        [
            source_kind.strip().lower(),
            uri.strip(),
            title.strip(),
        ]
    )
    digest = sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"src_{digest[:12]}"


@dataclass(slots=True)
class SourceEntry:
    source_id: str
    source_kind: str
    title: str = ""
    uri: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    last_scanned_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_id = str(self.source_id).strip()
        self.source_kind = str(self.source_kind).strip().lower()
        self.title = str(self.title).strip()
        self.uri = str(self.uri).strip()
        self.tags = _normalize_tags(self.tags)
        self.enabled = bool(self.enabled)
        self.last_scanned_at = str(self.last_scanned_at or "")
        self.metadata = normalize_source_strategy_metadata(dict(self.metadata or {}))
        if self.source_kind not in VALID_SOURCE_KINDS:
            raise ValueError(f"invalid source_kind: {self.source_kind}")
        if not self.source_id:
            self.source_id = _default_source_id(self.source_kind, self.uri, self.title)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "metadata": _json_safe(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceEntry":
        return cls(
            source_id=str(data.get("source_id") or ""),
            source_kind=str(data.get("source_kind") or ""),
            title=str(data.get("title") or ""),
            uri=str(data.get("uri") or ""),
            tags=[str(item) for item in (data.get("tags") or [])],
            enabled=bool(data.get("enabled", True)),
            last_scanned_at=str(data.get("last_scanned_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


class SourceRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sources: list[SourceEntry] = []
        self._load()

    def add_source(self, payload: dict[str, Any]) -> SourceEntry:
        entry = SourceEntry(
            source_id=str(payload.get("source_id") or ""),
            source_kind=str(payload.get("source_kind") or ""),
            title=str(payload.get("title") or ""),
            uri=str(payload.get("uri") or ""),
            tags=payload.get("tags") or [],
            enabled=bool(payload.get("enabled", True)),
            last_scanned_at=str(payload.get("last_scanned_at") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )
        def update(sources: list[SourceEntry]) -> list[SourceEntry]:
            self._upsert(sources, entry)
            return sources

        self._locked_update(update)
        return entry

    def list_sources(
        self,
        *,
        enabled: bool | None = None,
        source_kind: str | None = None,
    ) -> list[SourceEntry]:
        self._load()
        sources = list(self._sources)
        if enabled is not None:
            sources = [item for item in sources if item.enabled is enabled]
        if source_kind:
            normalized_kind = str(source_kind).strip().lower()
            sources = [item for item in sources if item.source_kind == normalized_kind]
        return sources

    def mark_source_scanned(
        self,
        source_id: str,
        *,
        scanned_at: str | None = None,
        status: str = "ok",
        item_count: int = 0,
        written_count: int = 0,
        skipped_existing_count: int = 0,
        error: str = "",
    ) -> SourceEntry | None:
        target_id = str(source_id or "").strip()
        if not target_id:
            return None
        final_scanned_at = str(scanned_at or now_iso())
        updated_entry: SourceEntry | None = None

        def update(sources: list[SourceEntry]) -> list[SourceEntry]:
            nonlocal updated_entry
            updated_sources: list[SourceEntry] = []
            for entry in sources:
                if entry.source_id != target_id:
                    updated_sources.append(entry)
                    continue
                metadata = dict(entry.metadata or {})
                metadata["last_scan"] = _json_safe(
                    {
                        "scanned_at": final_scanned_at,
                        "status": str(status or "ok"),
                        "item_count": max(0, int(item_count)),
                        "written_count": max(0, int(written_count)),
                        "skipped_existing_count": max(0, int(skipped_existing_count)),
                        "error": str(error or ""),
                    }
                )
                updated_entry = SourceEntry.from_dict(
                    {
                        **entry.to_dict(),
                        "last_scanned_at": final_scanned_at,
                        "metadata": metadata,
                    }
                )
                updated_sources.append(updated_entry)
            return updated_sources

        self._locked_update(update)
        return updated_entry

    def scan_sources(
        self,
        *,
        store: RuntimeStore | None = None,
        scope: dict[str, Any] | ScopeRef | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        scanned_at = now_iso()
        candidates: list[dict[str, Any]] = []
        written = 0

        def update(sources: list[SourceEntry]) -> list[SourceEntry]:
            updated_sources: list[SourceEntry] = []
            for entry in sources:
                if not entry.enabled:
                    updated_sources.append(entry)
                    continue
                updated = SourceEntry.from_dict({**entry.to_dict(), "last_scanned_at": scanned_at})
                updated_sources.append(updated)
                candidates.append(self._build_candidate(updated, scanned_at=scanned_at))
            return updated_sources

        self._locked_update(update)
        for candidate in candidates:
            if persist and store is not None:
                store.append(self._candidate_record(candidate, scope=scope_ref))
                written += 1
        return {
            "ok": True,
            "scanned_at": scanned_at,
            "source_count": len(self._sources),
            "scanned_count": len(candidates),
            "skipped_count": sum(1 for item in self._sources if not item.enabled),
            "candidate_count": len(candidates),
            "written_count": written,
            "candidates": candidates,
            "sources": [entry.to_dict() for entry in self._sources],
        }

    def _build_candidate(self, entry: SourceEntry, *, scanned_at: str) -> dict[str, Any]:
        summary = self._candidate_summary(entry)
        return {
            "candidate_id": f"srcscan-{entry.source_id}",
            "source_id": entry.source_id,
            "source_kind": entry.source_kind,
            "title": entry.title,
            "uri": entry.uri,
            "tags": list(entry.tags),
            "enabled": entry.enabled,
            "last_scanned_at": entry.last_scanned_at,
            "summary": summary,
            "scan_notes": self._scan_notes(entry),
            "provenance": {
                "source_id": entry.source_id,
                "source_kind": entry.source_kind,
                "source_uri": entry.uri,
                "source_tags": list(entry.tags),
                "registry_path": str(self.path),
                "scan_kind": "source_registry",
                "scanned_at": scanned_at,
            },
        }

    def _candidate_record(self, candidate: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
        raw_title = str(candidate.get("title") or candidate["source_id"])
        title = strip_candidate_title_prefixes(raw_title, default=str(candidate["source_id"]))
        summary = str(candidate.get("summary") or "")
        detail = "\n".join(
            [
                f"Registry source: {candidate['source_id']}",
                f"Kind: {candidate['source_kind']}",
                f"URI: {candidate['uri']}",
                f"Tags: {', '.join(candidate.get('tags') or [])}",
                f"Scanned at: {candidate['provenance']['scanned_at']}",
                f"Registry path: {candidate['provenance']['registry_path']}",
            ]
        )
        return RecordEnvelope(
            record_id=f"srcscan_{candidate['source_id']}",
            kind="source_candidate",
            status="candidate",
            title=f"Source candidate: {title}",
            summary=summary,
            detail=detail,
            content={
                "source_id": candidate["source_id"],
                "source_kind": candidate["source_kind"],
                "title": title,
                "original_title": raw_title,
                "uri": candidate["uri"],
                "tags": list(candidate.get("tags") or []),
                "summary": summary,
                "scan_notes": candidate["scan_notes"],
            },
            tags=list(candidate.get("tags") or []),
            links=[],
            evidence=[],
            source="eimemory.source_registry.scan",
            scope=scope,
            time=TimeRef.now(),
            provenance=dict(candidate["provenance"]),
            meta={
                "source_id": candidate["source_id"],
                "source_kind": candidate["source_kind"],
                "source_uri": candidate["uri"],
                "scan_kind": "source_registry",
                "provenance": dict(candidate["provenance"]),
                "force_capture": True,
            },
        )

    def _candidate_summary(self, entry: SourceEntry) -> str:
        title = entry.title or entry.source_id
        if entry.uri:
            return f"{entry.source_kind} source '{title}' at {entry.uri}"
        return f"{entry.source_kind} source '{title}'"

    def _scan_notes(self, entry: SourceEntry) -> str:
        pieces = [f"enabled={str(entry.enabled).lower()}"]
        if entry.tags:
            pieces.append(f"tags={', '.join(entry.tags)}")
        if entry.metadata:
            pieces.append(f"metadata_keys={', '.join(sorted(entry.metadata.keys()))}")
        return "; ".join(pieces)

    def _load(self) -> None:
        if not self.path.exists():
            self._sources = []
            return
        try:
            self._sources = self._decode_sources(read_json_strict(self.path, list))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid source registry at {self.path}") from exc

    def _locked_update(
        self,
        mutate: Callable[[list[SourceEntry]], list[SourceEntry]],
    ) -> None:
        def update(raw: list[Any]) -> list[dict[str, Any]]:
            sources = self._decode_sources(raw)
            updated = mutate(sources)
            if not isinstance(updated, list) or any(not isinstance(item, SourceEntry) for item in updated):
                raise TypeError("source registry mutation must return SourceEntry items")
            return [entry.to_dict() for entry in updated]

        try:
            payload = locked_json_update(
                self.path,
                update,
                default=[],
                expected_type=list,
            )
            self._sources = self._decode_sources(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid source registry at {self.path}") from exc

    @staticmethod
    def _decode_sources(raw: list[Any]) -> list[SourceEntry]:
        if any(not isinstance(item, dict) for item in raw):
            raise ValueError("source registry entries must be objects")
        return [SourceEntry.from_dict(item) for item in raw]

    @staticmethod
    def _upsert(sources: list[SourceEntry], entry: SourceEntry) -> None:
        for index, existing in enumerate(sources):
            if existing.source_id == entry.source_id:
                sources[index] = entry
                return
        sources.append(entry)
