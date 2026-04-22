from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef

FORMAT_VERSION = "eimemory-pack-v1"
PACK_RECORDS_NAME = "records.jsonl"
PACK_MANIFEST_NAME = "manifest.json"
STABLE_KINDS = ("memory", "claim_card", "knowledge_page", "paper_source")
CANDIDATE_STATUSES = ("candidate", "reviewed", "promoted")


def export_knowledge_pack(
    runtime: Any,
    path: str | Path,
    scope: ScopeRef | dict[str, Any] | None,
    include_candidates: bool = False,
) -> dict[str, Any]:
    pack_dir = _pack_dir(path)
    pack_dir.mkdir(parents=True, exist_ok=True)
    scope_ref = _scope_ref(scope)
    records = _export_records(runtime, scope_ref=scope_ref, include_candidates=include_candidates)
    records_path = pack_dir / PACK_RECORDS_NAME
    manifest_path = pack_dir / PACK_MANIFEST_NAME

    with records_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    digest = _file_sha256(records_path)
    kind_counts = dict(sorted(Counter(record.kind for record in records).items()))
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": now_iso(),
        "scope": asdict(scope_ref),
        "record_count": len(records),
        "kind_counts": kind_counts,
        "sha256": digest,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "path": str(pack_dir),
        "records_path": str(records_path),
        "manifest_path": str(manifest_path),
        "record_count": len(records),
        "kind_counts": kind_counts,
        "sha256": digest,
        "format_version": FORMAT_VERSION,
    }


def import_knowledge_pack(
    runtime: Any,
    path: str | Path,
    scope: ScopeRef | dict[str, Any] | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    pack_dir = _pack_dir(path)
    records_path = pack_dir / PACK_RECORDS_NAME
    manifest = _load_manifest(pack_dir / PACK_MANIFEST_NAME)
    actual_hash = _file_sha256(records_path)
    if actual_hash != manifest["sha256"]:
        raise ValueError("hash mismatch")

    records = _read_records(records_path)
    kind_counts = dict(sorted(Counter(record.kind for record in records).items()))
    if len(records) != manifest["record_count"] or kind_counts != manifest["kind_counts"]:
        raise ValueError("invalid manifest")
    collisions = [record.record_id for record in records if runtime.store.get_by_id(record.record_id) is not None]
    if collisions and not dry_run:
        raise ValueError("record id collision")

    target_scope = _scope_ref(scope)
    if not dry_run:
        for record in records:
            record.scope = target_scope
            runtime.store.append(record)

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "record_count": len(records),
        "written_count": 0 if dry_run else len(records),
        "collision_count": len(collisions),
        "collisions": collisions[:20],
        "kind_counts": kind_counts,
        "source_scope": dict(manifest["scope"]),
        "target_scope": asdict(target_scope),
        "sha256": actual_hash,
        "format_version": FORMAT_VERSION,
    }


def _export_records(runtime: Any, *, scope_ref: ScopeRef, include_candidates: bool) -> list[RecordEnvelope]:
    records = _list_all_records(runtime, kinds=list(STABLE_KINDS), scope=scope_ref)
    if include_candidates:
        for status in CANDIDATE_STATUSES:
            records.extend(
                _list_all_records(
                    runtime,
                    kinds=["knowledge_candidate"],
                    scope=scope_ref,
                    status=status,
                )
            )
    records.sort(key=lambda record: (record.kind, record.record_id))
    return records


def _list_all_records(
    runtime: Any,
    *,
    kinds: list[str],
    scope: ScopeRef,
    status: str | None = None,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    page_size = 100
    while True:
        page = runtime.store.list_records(kinds=kinds, scope=scope, status=status, limit=page_size, offset=offset)
        records.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return records


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid manifest") from exc
    if not isinstance(raw, dict):
        raise ValueError("invalid manifest")
    required = {
        "format_version": str,
        "created_at": str,
        "scope": dict,
        "record_count": int,
        "kind_counts": dict,
        "sha256": str,
    }
    for key, expected_type in required.items():
        if not isinstance(raw.get(key), expected_type):
            raise ValueError("invalid manifest")
    if raw["format_version"] != FORMAT_VERSION:
        raise ValueError("invalid manifest")
    if len(raw["sha256"]) != 64:
        raise ValueError("invalid manifest")
    raw["kind_counts"] = {str(key): int(value) for key, value in raw["kind_counts"].items()}
    return raw


def _read_records(path: Path) -> list[RecordEnvelope]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("invalid pack records") from exc
    records: list[RecordEnvelope] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid pack records") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid pack records")
        records.append(RecordEnvelope.from_dict(payload))
    return records


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("invalid pack records") from exc
    return digest.hexdigest()


def _scope_ref(scope: ScopeRef | dict[str, Any] | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _pack_dir(path: str | Path) -> Path:
    return Path(path)
