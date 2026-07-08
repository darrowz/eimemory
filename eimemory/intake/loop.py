from __future__ import annotations

import codecs
import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id
from eimemory.intake.registry import SourceEntry, SourceRegistry, VALID_SOURCE_KINDS
from eimemory.intake.title_normalization import strip_candidate_title_prefixes
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef

KIND_NAME = "knowledge_candidate"

DECISION_CANDIDATE = "candidate"
DECISION_REJECTED = "rejected"
DECISION_QUARANTINED = "quarantined"

LOCAL_TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".jsonl"})
MIN_ACTIVE_CONTENT_CHARS = 32
EXCERPT_CHARS = 1200
MAX_LOCAL_READ_BYTES = 1_000_000
LOCAL_READ_CHUNK_BYTES = 64 * 1024

_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "reveal the system prompt",
    "show the system prompt",
    "developer message",
    "system message",
    "prompt injection",
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", re.IGNORECASE),
    re.compile(r"\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
)


@dataclass(slots=True)
class _SourceMaterial:
    title: str
    summary: str
    content_excerpt: str
    screening_text: str
    provenance: dict[str, Any]
    reason: str


class KnowledgeIntakeLoop:
    """Build safe, deterministic knowledge intake candidates from source entries."""

    def __init__(
        self,
        sources: SourceRegistry | None = None,
        store: Any | None = None,
        *,
        excerpt_chars: int = EXCERPT_CHARS,
        min_content_chars: int = MIN_ACTIVE_CONTENT_CHARS,
    ) -> None:
        self.sources = sources
        self.store = store
        self.excerpt_chars = max(80, int(excerpt_chars))
        self.min_content_chars = max(1, int(min_content_chars))

    def run(
        self,
        scope: dict[str, Any] | ScopeRef | None = None,
        *,
        persist: bool = False,
        source_kind: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if self.sources is None:
            raise ValueError("source registry is required")
        if persist and self.store is None:
            raise ValueError("runtime store is required when persist=True")
        sources = self.sources.list_sources(source_kind=source_kind or None)
        if limit is not None:
            sources = sources[: max(0, int(limit))]
        scanned_at = now_iso()
        candidates = self.build_candidates(sources, scanned_at=scanned_at)
        written = 0
        skipped_existing = 0
        written_by_source: dict[str, int] = {}
        skipped_by_source: dict[str, int] = {}
        if persist:
            for record in candidates_to_records(candidates, scope):
                existing = self.store.get_by_id(record.record_id, scope=record.scope)
                if existing is not None and existing.status != "candidate":
                    skipped_existing += 1
                    source_id = str(record.meta.get("source_id") or "")
                    skipped_by_source[source_id] = skipped_by_source.get(source_id, 0) + 1
                    continue
                self.store.append(record)
                written += 1
                source_id = str(record.meta.get("source_id") or "")
                written_by_source[source_id] = written_by_source.get(source_id, 0) + 1
        self._mark_scanned_sources(
            sources,
            candidates,
            scanned_at=scanned_at,
            written_by_source=written_by_source,
            skipped_by_source=skipped_by_source,
        )
        return {
            "ok": True,
            "persist": bool(persist),
            "source_kind": source_kind or "",
            "limit": limit,
            "scanned_count": len(sources),
            "candidate_count": sum(1 for item in candidates if item.get("decision") == DECISION_CANDIDATE),
            "rejected_count": sum(1 for item in candidates if item.get("decision") == DECISION_REJECTED),
            "quarantined_count": sum(1 for item in candidates if item.get("decision") == DECISION_QUARANTINED),
            "written_count": written,
            "skipped_existing_count": skipped_existing,
            "candidates": candidates,
        }

    def build_candidates(self, sources: Iterable[SourceEntry], *, scanned_at: str | None = None) -> list[dict[str, Any]]:
        seen_fingerprints: set[str] = set()
        candidates: list[dict[str, Any]] = []
        final_scanned_at = scanned_at or now_iso()
        for source in sources:
            candidate = self._candidate_for_source(source, scanned_at=final_scanned_at)
            fingerprint = str(candidate["fingerprint"])
            if candidate["decision"] == DECISION_CANDIDATE and fingerprint in seen_fingerprints:
                candidate = {
                    **candidate,
                    "decision": DECISION_REJECTED,
                    "reason": "duplicate_fingerprint",
                    "quality": {
                        **dict(candidate.get("quality") or {}),
                        "duplicate": True,
                    },
                }
            if candidate["decision"] == DECISION_CANDIDATE:
                seen_fingerprints.add(fingerprint)
            candidates.append(candidate)
        return candidates

    def _mark_scanned_sources(
        self,
        sources: Iterable[SourceEntry],
        candidates: Iterable[dict[str, Any]],
        *,
        scanned_at: str,
        written_by_source: dict[str, int],
        skipped_by_source: dict[str, int],
    ) -> None:
        if self.sources is None:
            return
        candidates_by_source = {str(item.get("source_id") or ""): item for item in candidates}
        for source in sources:
            candidate = candidates_by_source.get(source.source_id, {})
            status = str(candidate.get("decision") or ("skipped" if not source.enabled else "unknown"))
            self.sources.mark_source_scanned(
                source.source_id,
                scanned_at=scanned_at,
                status=status,
                item_count=1 if candidate else 0,
                written_count=written_by_source.get(source.source_id, 0),
                skipped_existing_count=skipped_by_source.get(source.source_id, 0),
                error="" if status == DECISION_CANDIDATE else str(candidate.get("reason") or ""),
            )

    def _candidate_for_source(self, source: SourceEntry, *, scanned_at: str) -> dict[str, Any]:
        material = self._source_material(source, scanned_at=scanned_at)
        decision, reason = self._screen(source, material)
        fingerprint = self._fingerprint(source, material)
        material = self._redacted_material(source, material, decision=decision, reason=reason)
        quality = self._quality(source, material, decision=decision, reason=reason)
        return {
            "source_id": source.source_id,
            "source_kind": source.source_kind,
            "title": material.title,
            "uri": _safe_source_uri(source.uri, decision=decision),
            "summary": material.summary,
            "content_excerpt": material.content_excerpt,
            "decision": decision,
            "reason": reason,
            "fingerprint": fingerprint,
            "provenance": material.provenance,
            "quality": quality,
        }

    def _redacted_material(
        self,
        source: SourceEntry,
        material: _SourceMaterial,
        *,
        decision: str,
        reason: str,
    ) -> _SourceMaterial:
        if decision != DECISION_QUARANTINED:
            return material
        provenance = dict(material.provenance)
        provenance["redacted"] = True
        provenance["source_uri"] = "[redacted]"
        provenance["source_tags"] = []
        provenance.pop("file_path", None)
        return _SourceMaterial(
            title=f"Quarantined source: {source.source_id or source.source_kind}",
            summary=f"Quarantined during intake: {reason}",
            content_excerpt=f"[redacted:{reason}]",
            screening_text="",
            provenance=provenance,
            reason=material.reason,
        )

    def _source_material(self, source: SourceEntry, *, scanned_at: str) -> _SourceMaterial:
        base_title = source.title or source.source_id or source.uri
        provenance: dict[str, Any] = {
            "source_id": source.source_id,
            "source_kind": source.source_kind,
            "source_uri": source.uri,
            "source_tags": list(source.tags),
            "scan_kind": "knowledge_intake_loop",
            "scanned_at": scanned_at,
        }
        local_path = _local_path_from_uri(source.uri)
        if local_path is not None:
            provenance["read_mode"] = "local_file"
            provenance["file_path"] = str(local_path)
            provenance["file_format"] = local_path.suffix.lower()
            excerpt, screening_text, read_reason = self._read_local_excerpt(local_path)
            summary = _summary_from_text(excerpt) if excerpt else self._metadata_summary(source)
            return _SourceMaterial(
                title=base_title,
                summary=summary,
                content_excerpt=excerpt,
                screening_text=screening_text,
                provenance=provenance,
                reason=read_reason,
            )
        provenance["read_mode"] = "metadata_dry_run"
        summary = self._metadata_summary(source)
        excerpt = _metadata_excerpt(source)
        return _SourceMaterial(
            title=base_title,
            summary=summary,
            content_excerpt=excerpt,
            screening_text=excerpt,
            provenance=provenance,
            reason="metadata_dry_run",
        )

    def _read_local_excerpt(self, path: Path) -> tuple[str, str, str]:
        suffix = path.suffix.lower()
        if suffix not in LOCAL_TEXT_SUFFIXES:
            return "", "", "unsupported_local_file"
        if not path.exists() or not path.is_file():
            return "", "", "local_file_missing"
        raw = _read_local_text_window(path, max_bytes=MAX_LOCAL_READ_BYTES)
        screening_text = _clean_excerpt(raw, MAX_LOCAL_READ_BYTES)
        if suffix == ".json":
            return _excerpt_from_json(raw, self.excerpt_chars), screening_text, "local_file_read"
        if suffix == ".jsonl":
            return _excerpt_from_jsonl(raw, self.excerpt_chars), screening_text, "local_file_read"
        return _clean_excerpt(raw, self.excerpt_chars), screening_text, "local_file_read"

    def _metadata_summary(self, source: SourceEntry) -> str:
        title = source.title or source.source_id or "untitled source"
        if source.uri:
            return f"{source.source_kind} source '{title}' at {source.uri}"
        return f"{source.source_kind} source '{title}'"

    def _screen(self, source: SourceEntry, material: _SourceMaterial) -> tuple[str, str]:
        if not source.enabled:
            return DECISION_REJECTED, "disabled_source"
        if source.source_kind not in VALID_SOURCE_KINDS:
            return DECISION_REJECTED, "unsupported_source_kind"
        has_metadata = bool(source.title or source.uri or source.tags or _meaningful_source_metadata(source.metadata))
        if not has_metadata:
            return DECISION_REJECTED, "empty_source"
        combined = " ".join(
            [material.title, source.uri, material.summary, material.content_excerpt, material.screening_text]
        ).strip()
        if _looks_like_prompt_injection(combined):
            return DECISION_QUARANTINED, "prompt_injection_detected"
        if _looks_like_secret(combined):
            return DECISION_QUARANTINED, "secret_detected"
        if material.reason in {"local_file_missing", "unsupported_local_file"}:
            return DECISION_REJECTED, material.reason
        if _local_path_from_uri(source.uri) is not None and len(_alnum_text(material.content_excerpt)) < self.min_content_chars:
            return DECISION_REJECTED, "content_too_short"
        if not combined:
            return DECISION_REJECTED, "empty_source"
        return DECISION_CANDIDATE, material.reason if material.reason != "local_file_read" else "accepted"

    def _fingerprint(self, source: SourceEntry, material: _SourceMaterial) -> str:
        uri = str(material.provenance.get("file_path") or source.uri).strip().lower()
        normalized = "\n".join(
            [
                source.source_kind.strip().lower(),
                uri,
                _normalize_for_fingerprint(material.content_excerpt or material.summary),
            ]
        )
        return sha256(normalized.encode("utf-8")).hexdigest()

    def _quality(self, source: SourceEntry, material: _SourceMaterial, *, decision: str, reason: str) -> dict[str, Any]:
        content_length = len(_alnum_text(material.content_excerpt))
        metadata_length = len(_alnum_text(material.summary))
        score = 0.0
        if decision == DECISION_CANDIDATE:
            score = min(1.0, 0.45 + min(0.4, content_length / 500) + min(0.15, metadata_length / 300))
        return {
            "score": round(score, 3),
            "content_length": content_length,
            "metadata_length": metadata_length,
            "has_excerpt": bool(material.content_excerpt),
            "local_content_read": material.provenance.get("read_mode") == "local_file" and bool(material.content_excerpt),
            "source_enabled": source.enabled,
            "decision": decision,
            "reason": reason,
        }


def _read_local_text_window(
    path: Path,
    *,
    max_bytes: int = MAX_LOCAL_READ_BYTES,
    chunk_bytes: int = LOCAL_READ_CHUNK_BYTES,
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunks: list[str] = []
    read_bytes = 0
    max_bytes = max(0, int(max_bytes))
    chunk_bytes = max(1, min(int(chunk_bytes), max(1, max_bytes or 1)))
    with path.open("rb") as handle:
        while read_bytes < max_bytes:
            chunk = handle.read(min(chunk_bytes, max_bytes - read_bytes))
            if not chunk:
                break
            read_bytes += len(chunk)
            chunks.append(decoder.decode(chunk, final=False))
    tail = decoder.decode(b"", final=True)
    if tail:
        chunks.append(tail)
    return "".join(chunks)


def candidates_to_records(candidates: Iterable[dict[str, Any]], scope: dict[str, Any] | ScopeRef | None) -> list[RecordEnvelope]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_hash = _scope_hash(scope_ref)
    records: list[RecordEnvelope] = []
    for candidate in candidates:
        if candidate.get("decision") != DECISION_CANDIDATE:
            continue
        fingerprint = str(candidate.get("fingerprint") or "")
        raw_title = str(candidate.get("title") or candidate.get("source_id") or "Knowledge candidate")
        title = strip_candidate_title_prefixes(raw_title, default="Knowledge candidate")
        content = {
            "source_id": str(candidate.get("source_id") or ""),
            "source_kind": str(candidate.get("source_kind") or ""),
            "title": title,
            "original_title": raw_title,
            "uri": str(candidate.get("uri") or ""),
            "summary": str(candidate.get("summary") or ""),
            "content_excerpt": str(candidate.get("content_excerpt") or ""),
            "decision": str(candidate.get("decision") or ""),
            "reason": str(candidate.get("reason") or ""),
            "fingerprint": fingerprint,
            "provenance": dict(candidate.get("provenance") or {}),
            "quality": dict(candidate.get("quality") or {}),
        }
        records.append(
            RecordEnvelope(
                record_id=f"kc_{fingerprint[:12]}_{scope_hash}" if fingerprint else generate_record_id(KIND_NAME),
                kind=KIND_NAME,
                status="candidate",
                title=f"Knowledge candidate: {title}",
                summary=str(candidate.get("summary") or ""),
                detail=str(candidate.get("content_excerpt") or ""),
                content=content,
                tags=[],
                links=[],
                evidence=[],
                source="eimemory.knowledge_intake_loop",
                scope=scope_ref,
                time=TimeRef.now(),
                provenance=dict(candidate.get("provenance") or {}),
                meta={
                    "intake_decision": str(candidate.get("decision") or ""),
                    "source_id": str(candidate.get("source_id") or ""),
                    "source_kind": str(candidate.get("source_kind") or ""),
                    "source_uri": str(candidate.get("uri") or ""),
                    "fingerprint": fingerprint,
                    "quality": dict(candidate.get("quality") or {}),
                },
            )
        )
    return records


def _scope_hash(scope: ScopeRef) -> str:
    payload = {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]


def _local_path_from_uri(uri: str) -> Path | None:
    raw = str(uri or "").strip()
    if not raw:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        return Path(raw)
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        path = unquote(parsed.path or "")
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return Path(path)
    if parsed.scheme:
        return None
    candidate = Path(raw)
    if candidate.suffix.lower() in LOCAL_TEXT_SUFFIXES or candidate.exists():
        return candidate
    return None


def _safe_source_uri(uri: str, *, decision: str) -> str:
    if decision == DECISION_QUARANTINED:
        return "[redacted]"
    return str(uri or "")


def _excerpt_from_json(raw: str, limit: int) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _clean_excerpt(raw, limit)
    return _clean_excerpt(_text_from_json_value(data), limit)


def _excerpt_from_jsonl(raw: str, limit: int) -> str:
    chunks: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            chunks.append(stripped)
        else:
            chunks.append(_text_from_json_value(value))
        if len(" ".join(chunks)) >= limit:
            break
    return _clean_excerpt("\n".join(chunk for chunk in chunks if chunk), limit)


def _text_from_json_value(value: Any) -> str:
    if isinstance(value, dict):
        preferred: list[str] = []
        for key in ("title", "summary", "abstract", "description", "text", "content", "body"):
            if key in value:
                preferred.append(_text_from_json_value(value[key]))
        if preferred:
            return "\n".join(item for item in preferred if item)
        return "\n".join(f"{key}: {_text_from_json_value(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_text_from_json_value(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _metadata_excerpt(source: SourceEntry) -> str:
    payload = {
        "title": source.title,
        "uri": source.uri,
        "tags": list(source.tags),
        "metadata": dict(source.metadata),
    }
    return _clean_excerpt(json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True), EXCERPT_CHARS)


def _meaningful_source_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Ignore source strategy defaults when deciding whether a source is empty."""
    defaults = {"frequency": "daily", "max_items": 10}
    return {key: value for key, value in metadata.items() if defaults.get(key) != value}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return asdict(value)
    except TypeError:
        return str(value)


def _summary_from_text(text: str) -> str:
    cleaned = _clean_excerpt(text, 240)
    if not cleaned:
        return ""
    first_line = next((line.strip("# ").strip() for line in cleaned.splitlines() if line.strip()), "")
    return first_line[:240]


def _clean_excerpt(text: str, limit: int) -> str:
    normalized = str(text or "").replace("\ufeff", "").replace("\x00", "")
    normalized = "\n".join(line.strip() for line in normalized.splitlines())
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rsplit(" ", 1)[0].strip()


def _normalize_for_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _alnum_text(text: str) -> str:
    return "".join(char for char in str(text or "") if char.isalnum())


def _looks_like_prompt_injection(text: str) -> bool:
    lowered = str(text or "").lower()
    normalized = re.sub(r"\s+", " ", lowered)
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    for pattern in _INJECTION_PATTERNS:
        normalized_pattern = re.sub(r"\s+", " ", pattern.lower())
        compact_pattern = re.sub(r"[^a-z0-9]+", "", pattern.lower())
        if normalized_pattern in normalized or compact_pattern in compact:
            return True
    return False


def _looks_like_secret(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern in _SECRET_PATTERNS)
