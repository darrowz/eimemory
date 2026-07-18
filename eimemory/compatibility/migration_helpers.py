from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

from eimemory.core.clock import now_iso
from eimemory.api.runtime import Runtime
from eimemory.intake.loop import _looks_like_prompt_injection, _looks_like_secret
from eimemory.models.records import RecordEnvelope, ScopeRef, evaluate_memory_quality
from eimemory.storage.jsonl import JsonlLog, scan_jsonl_strict


SUPPORTED_IMPORT_KINDS = {"memory", "multimodal_memory"}
BACKUP_FORMAT_VERSION = 1


def export_records(runtime: Runtime, path: str | Path) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with target.open("w", encoding="utf-8") as handle:
        for record in _iter_runtime_records(runtime):
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            total += 1
    return total


def import_records(runtime: Runtime, path: str | Path) -> int:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    count = 0
    for entry in scan_jsonl_strict(source):
        runtime.store.append(RecordEnvelope.from_dict(entry.payload))
        count += 1
    return count


def backup_create(runtime: Runtime, path: str | Path) -> dict:
    target = Path(path)
    data_path, manifest_path, mode = _resolve_backup_paths(target)
    if mode == "directory":
        target.mkdir(parents=True, exist_ok=True)
    else:
        data_path.parent.mkdir(parents=True, exist_ok=True)
    record_count = 0
    sha256 = hashlib.sha256()
    with data_path.open("wb") as handle:
        for record in _iter_runtime_records(runtime):
            line = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"
            handle.write(line)
            sha256.update(line)
            record_count += 1
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": now_iso(),
        "record_count": record_count,
        "sha256": sha256.hexdigest(),
        "data_file": _manifest_data_file(data_path, manifest_path),
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {
        "ok": True,
        "path": str(target),
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "record_count": record_count,
    }


def backup_verify(path: str | Path) -> dict:
    target = Path(path)
    fallback_data_path, manifest_path, _ = _resolve_backup_paths(target)
    data_path = fallback_data_path
    report = {
        "ok": False,
        "path": str(target),
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "manifest": {},
        "record_count": 0,
        "expected_record_count": None,
        "sha256": None,
        "expected_sha256": None,
        "format_version": None,
        "errors": [],
    }
    if not manifest_path.exists():
        report["errors"].append({"code": "manifest_missing", "path": str(manifest_path)})
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report["errors"].append({"code": "manifest_unreadable", "error": str(exc)})
        return report
    if not isinstance(manifest, dict):
        report["errors"].append({"code": "manifest_invalid", "error": "manifest must be an object"})
        return report
    report["manifest"] = manifest
    data_path = _resolve_manifest_data_path(
        manifest.get("data_file"),
        manifest_path=manifest_path,
        fallback_data_path=fallback_data_path,
    )
    report["data_path"] = str(data_path)
    if not data_path.exists():
        report["errors"].append({"code": "data_missing", "path": str(data_path)})
        return report
    format_version = manifest.get("format_version")
    created_at = manifest.get("created_at")
    expected_record_count = manifest.get("record_count")
    expected_sha256 = manifest.get("sha256")
    report["format_version"] = format_version
    report["expected_record_count"] = expected_record_count
    report["expected_sha256"] = expected_sha256
    if not isinstance(created_at, str) or not created_at.strip():
        report["errors"].append({"code": "created_at_invalid", "value": created_at})
        return report
    if format_version != BACKUP_FORMAT_VERSION:
        report["errors"].append(
            {
                "code": "format_version_mismatch",
                "expected": BACKUP_FORMAT_VERSION,
                "actual": format_version,
            }
        )
        return report
    if not isinstance(expected_record_count, int) or expected_record_count < 0:
        report["errors"].append({"code": "record_count_invalid", "value": expected_record_count})
        return report
    if not isinstance(expected_sha256, str) or not expected_sha256:
        report["errors"].append({"code": "sha256_invalid", "value": expected_sha256})
        return report
    record_count = 0
    sha256 = hashlib.sha256()
    try:
        with data_path.open("rb") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                sha256.update(raw_line)
                try:
                    payload = json.loads(raw_line.decode("utf-8"))
                    RecordEnvelope.from_dict(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    report["errors"].append(
                        {
                            "code": "record_invalid",
                            "line": line_no,
                            "error": str(exc),
                        }
                    )
                    break
                record_count += 1
    except OSError as exc:
        report["errors"].append({"code": "data_unreadable", "error": str(exc)})
        return report
    actual_sha256 = sha256.hexdigest()
    report["record_count"] = record_count
    report["sha256"] = actual_sha256
    if not report["errors"] and record_count != expected_record_count:
        report["errors"].append(
            {
                "code": "record_count_mismatch",
                "expected": expected_record_count,
                "actual": record_count,
            }
        )
    if not report["errors"] and actual_sha256 != expected_sha256:
        report["errors"].append(
            {
                "code": "sha256_mismatch",
                "expected": expected_sha256,
                "actual": actual_sha256,
            }
        )
    report["ok"] = not report["errors"]
    return report


def scan_migration_source(path: str | Path) -> dict:
    source = Path(path)
    source_type = _detect_source_type(source)
    candidates = list(_scan_candidates(source, source_type))
    accepted = sum(1 for item in candidates if item["decision"] == "accept")
    return {
        "path": str(source),
        "source_type": source_type,
        "candidate_count": len(candidates),
        "accepted_count": accepted,
        "candidates": candidates,
    }


def import_candidates(
    runtime: Runtime,
    candidates: list[dict],
    *,
    scope: dict,
    candidate_ids: list[str] | None = None,
) -> int:
    allowed = set(candidate_ids or [])
    imported = 0
    for candidate in candidates:
        if candidate.get("decision") != "accept":
            continue
        if allowed and candidate.get("candidate_id") not in allowed:
            continue
        text = str(candidate.get("text") or "").strip()
        title = str(candidate.get("title") or "Migrated memory").strip() or "Migrated memory"
        if not text:
            continue
        runtime.memory.ingest(
            text=text,
            memory_type=str(candidate.get("memory_type") or "fact"),
            title=title,
            scope=scope,
            source=f"migration.{candidate.get('source_type') or 'unknown'}",
            tags=["migrated"],
        )
        imported += 1
    return imported


def build_review_report(report: dict) -> str:
    lines = [
        "# Migration Review Report",
        "",
        f"- Source: `{report['path']}`",
        f"- Source Type: `{report['source_type']}`",
        f"- Candidate Count: `{report['candidate_count']}`",
        f"- Accepted by Screen: `{report['accepted_count']}`",
        "",
        "## Review Checklist",
        "",
        "- Verify every accepted candidate is a stable long-term memory, not transient conversation noise.",
        "- Keep anything ambiguous out of the import set until it is rewritten or confirmed.",
        "- Import only by explicit `candidate_id` when reviewing a mixed source.",
        "",
        "## Candidates",
        "",
    ]
    for candidate in report["candidates"]:
        marker = "[ ]" if candidate["decision"] == "accept" else "[x]"
        lines.extend(
            [
                f"### {candidate['candidate_id']}",
                "",
                f"- {marker} {'Import' if candidate['decision'] == 'accept' else 'Reject'} `{candidate['candidate_id']}`",
                f"- Title: `{candidate['title']}`",
                f"- Source: `{candidate['source_ref']}`",
                f"- Decision: `{candidate['decision']}`",
                f"- Reason: `{candidate['reason']}`",
                f"- Confidence: `{candidate['confidence']}`",
                "",
                "Excerpt:",
                "",
                "```text",
                candidate["text"][:400].strip(),
                "```",
                "",
            ]
        )
    accepted_ids = [item["candidate_id"] for item in report["candidates"] if item["decision"] == "accept"]
    if accepted_ids:
        lines.extend(
            [
                "## Import Command",
                "",
                "```bash",
                "eimemory migrate import "
                + str(report["path"])
                + " "
                + " ".join(f"--candidate-id {candidate_id}" for candidate_id in accepted_ids),
                "```",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _detect_source_type(path: Path) -> str:
    if path.is_dir():
        return "markdown"
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return "markdown"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".sqlite":
        return "sqlite"
    raise ValueError(f"unsupported migration source: {path}")


def _scan_candidates(path: Path, source_type: str) -> Iterable[dict]:
    if source_type == "markdown":
        yield from _scan_markdown(path)
        return
    if source_type == "jsonl":
        yield from _scan_jsonl(path)
        return
    if source_type == "sqlite":
        yield from _scan_sqlite(path)
        return
    raise ValueError(f"unsupported migration source type: {source_type}")


def _scan_markdown(path: Path) -> Iterable[dict]:
    files = [path] if path.is_file() else sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    for index, file_path in enumerate(files, start=1):
        if file_path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        title, body = _extract_markdown_title_and_body(file_path, text)
        yield _candidate_payload(
            candidate_id=f"md-{index}",
            source_type="markdown",
            source_ref=str(file_path),
            title=title,
            text=body,
        )


def _scan_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            payload = json.loads(line)
            kind = str(payload.get("kind") or "")
            title = str(payload.get("title") or f"JSONL record {index}")
            text = str((payload.get("content") or {}).get("text") or payload.get("summary") or "").strip()
            candidate = _candidate_payload(
                candidate_id=f"jsonl-{index}",
                source_type="jsonl",
                source_ref=f"{path}:{index}",
                title=title,
                text=text,
            )
            if kind and kind not in SUPPORTED_IMPORT_KINDS:
                candidate["decision"] = "reject"
                candidate["reason"] = "unsupported_kind"
            yield candidate


def _scan_sqlite(path: Path) -> Iterable[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"files", "chunks"} <= tables:
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
            file_columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
            if {"file_id", "chunk_text"} <= chunk_columns and "id" in file_columns:
                rows = conn.execute(
                    """
                    SELECT files.path AS file_path, GROUP_CONCAT(chunks.chunk_text, '\n') AS text
                    FROM files
                    JOIN chunks ON chunks.file_id = files.id
                    GROUP BY files.id, files.path
                    ORDER BY files.id
                    """
                ).fetchall()
            elif {"path", "text"} <= chunk_columns:
                rows = conn.execute(
                    """
                    SELECT chunks.path AS file_path, GROUP_CONCAT(chunks.text, '\n') AS text
                    FROM chunks
                    GROUP BY chunks.path
                    ORDER BY chunks.path
                    """
                ).fetchall()
            else:
                raise ValueError(f"unsupported openclaw chunks schema: {path}")
            for index, row in enumerate(rows, start=1):
                yield _candidate_payload(
                    candidate_id=f"sqlite-openclaw-{index}",
                    source_type="sqlite",
                    source_ref=f"{path}:{row['file_path']}",
                    title=Path(str(row["file_path"] or "")).stem or f"SQLite memory {index}",
                    text=str(row["text"] or "").strip(),
                )
            return
        if "records" in tables:
            rows = conn.execute("SELECT payload_json FROM records ORDER BY updated_at DESC").fetchall()
            for index, row in enumerate(rows, start=1):
                payload = json.loads(row["payload_json"])
                kind = str(payload.get("kind") or "")
                text = str((payload.get("content") or {}).get("text") or payload.get("summary") or "").strip()
                candidate = _candidate_payload(
                    candidate_id=f"sqlite-records-{index}",
                    source_type="sqlite",
                    source_ref=f"{path}:records:{index}",
                    title=str(payload.get("title") or f"SQLite record {index}"),
                    text=text,
                )
                if kind and kind not in SUPPORTED_IMPORT_KINDS:
                    candidate["decision"] = "reject"
                    candidate["reason"] = "unsupported_kind"
                yield candidate
            return
        raise ValueError(f"unsupported sqlite schema: {path}")
    finally:
        conn.close()


def _candidate_payload(
    *,
    candidate_id: str,
    source_type: str,
    source_ref: str,
    title: str,
    text: str,
) -> dict:
    cleaned_text = text.strip()
    cleaned_title = title.strip() or "Migrated memory"
    decision = "accept"
    reason = "accepted"
    confidence = 0.92
    combined = " ".join(part for part in (cleaned_title, cleaned_text) if part).strip()
    if _looks_like_prompt_injection(combined):
        decision = "reject"
        reason = "prompt_injection_detected"
        confidence = 0.02
    elif _looks_like_secret(combined):
        decision = "reject"
        reason = "secret_detected"
        confidence = 0.02
    elif len(cleaned_text) < 24 or len(cleaned_text.split()) < 4:
        decision = "reject"
        reason = "content_too_thin"
        confidence = 0.2
    else:
        quality = evaluate_memory_quality(
            text=cleaned_text,
            title=cleaned_title,
            memory_type="fact",
            source=f"migration.{source_type}",
        )
        confidence = float(quality.get("confidence") or confidence)
        if quality.get("capture_decision") != "accept":
            decision = "reject"
            reason = str(quality.get("capture_decision") or "low_salience")
    return {
        "candidate_id": candidate_id,
        "source_type": source_type,
        "source_ref": source_ref,
        "title": cleaned_title,
        "text": cleaned_text,
        "memory_type": "fact",
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
    }


def _iter_runtime_records(runtime: Runtime) -> Iterable[RecordEnvelope]:
    log_path = getattr(getattr(runtime.store, "log", None), "path", None)
    records = _records_from_jsonl_log(log_path)
    if records:
        return records
    return _records_from_sqlite(runtime)


def _records_from_sqlite(runtime: Runtime) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    batch_size = 1000
    offset = 0
    while True:
        page = runtime.store.list_records(limit=batch_size, offset=offset)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records


def _records_from_jsonl_log(path: Path | None) -> list[RecordEnvelope]:
    if path is None:
        return []
    latest_by_key: "OrderedDict[str, RecordEnvelope]" = OrderedDict()
    log = JsonlLog(path, create_parent=False)
    for entry in log.scan_strict():
        record = RecordEnvelope.from_dict(entry.payload)
        key = _record_storage_key(record)
        if key in latest_by_key:
            del latest_by_key[key]
        latest_by_key[key] = record
    return list(latest_by_key.values())


def _record_storage_key(record: RecordEnvelope) -> str:
    scope = record.scope
    return "|".join(
        [
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            record.record_id,
        ]
    )


def _extract_markdown_title_and_body(path: Path, text: str) -> tuple[str, str]:
    lines = [line.rstrip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line.startswith("# "):
            body = "\n".join(lines[index + 1:]).strip()
            return line[2:].strip(), body
    return path.stem, text.strip()


def _resolve_backup_paths(path: Path) -> tuple[Path, Path, str]:
    raw = str(path)
    if path.exists() and path.is_dir():
        return path / "backup.jsonl", path / "backup.manifest.json", "directory"
    if raw.endswith(("/", "\\")):
        return path / "backup.jsonl", path / "backup.manifest.json", "directory"
    return path.with_suffix(".jsonl"), path.with_suffix(".manifest.json"), "base"


def _manifest_data_file(data_path: Path, manifest_path: Path) -> str:
    try:
        return os.path.relpath(data_path, start=manifest_path.parent)
    except ValueError:
        return data_path.name


def _resolve_manifest_data_path(value: object, *, manifest_path: Path, fallback_data_path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return fallback_data_path
    declared = Path(value)
    if not declared.is_absolute():
        return manifest_path.parent / declared
    if declared.exists():
        return declared
    if declared.name == fallback_data_path.name and fallback_data_path.exists():
        return fallback_data_path
    return declared
