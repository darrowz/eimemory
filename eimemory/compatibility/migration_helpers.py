from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from eimemory.core.clock import now_iso
from eimemory.api.runtime import Runtime
from eimemory.security_screening import (
    looks_like_prompt_injection as _looks_like_prompt_injection,
    looks_like_secret as _looks_like_secret,
    mark_external_origin,
    screen_record_payload,
)
from eimemory.models.records import RecordEnvelope, ScopeRef, evaluate_memory_quality
from eimemory.storage.atomic_file import _existing_stat, _fsync_directory, atomic_write_json, interprocess_lock
from eimemory.storage.jsonl import JsonlLog, scan_jsonl_strict
from eimemory.storage.private_file import private_temporary_file
from eimemory.storage.record_replay import retired_record_ref


SUPPORTED_IMPORT_KINDS = {"memory", "multimodal_memory"}
BACKUP_FORMAT_VERSION = 1


def export_records(runtime: Runtime, path: str | Path) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_stat(target)
    with _staged_record_export(runtime, target.parent, owner=existing) as (temporary, total, _digest):
        _existing_stat(target)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    return total


@contextmanager
def _staged_record_export(runtime: Runtime, directory: Path, *, owner: os.stat_result | None = None):
    """Stream and sync a private complete snapshot before publishing its name."""
    descriptor, name = private_temporary_file(prefix=".eimemory-export-", directory=directory)
    temporary = Path(name)
    count = 0
    digest = hashlib.sha256()
    try:
        try:
            handle = os.fdopen(descriptor, "wb")
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        with handle:
            if os.name == "posix" and owner is not None:
                opened = os.fstat(handle.fileno())
                if (opened.st_uid, opened.st_gid) != (owner.st_uid, owner.st_gid):
                    os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            for record in _iter_runtime_records(runtime, scratch_directory=directory):
                line = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"
                handle.write(line)
                digest.update(line)
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary, count, digest.hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def import_records(runtime: Runtime, path: str | Path) -> int:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    count = 0
    for entry in scan_jsonl_strict(source):
        record = RecordEnvelope.from_dict(entry.payload)
        screening = screen_record_payload(record)
        if not screening.get("ok"):
            raise ValueError(
                f"import_screened_out:{record.record_id}:{','.join(screening.get('reasons') or [])}"
            )
        mark_external_origin(record)
        runtime.store.append(record)
        count += 1
    return count


def backup_create(runtime: Runtime, path: str | Path) -> dict:
    target = Path(path)
    data_path, manifest_path, mode = _resolve_backup_paths(target)
    if mode == "directory":
        target.mkdir(parents=True, exist_ok=True)
    else:
        data_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    with interprocess_lock(lock_path):
        previous_data = _existing_stat(data_path)
        previous_manifest = _existing_stat(manifest_path)
        # The manifest is the commit point. Never replace the data file that a
        # previous manifest (or an in-flight reader) still references. The first
        # generation retains the historical name; replacements use unique names
        # already supported by the v1 relative data_file contract. Retain older
        # generations for readers and let backup-directory retention own cleanup.
        if data_path.exists() or manifest_path.exists():
            data_path = data_path.with_name(f"{data_path.stem}.{uuid4().hex}.jsonl")
        with _staged_record_export(runtime, data_path.parent, owner=previous_manifest or previous_data) as (temporary, record_count, digest):
            manifest = {
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": now_iso(),
                "record_count": record_count,
                "sha256": digest,
                "data_file": _manifest_data_file(data_path, manifest_path),
            }
            os.replace(temporary, data_path)
            _fsync_directory(data_path.parent)
            # A directory-fsync failure can follow the atomic manifest replace.
            # Keep the complete new data generation even on that exception.
            atomic_write_json(manifest_path, manifest)
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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        text = str(candidate.get("text") or "")
        cleaned_text = text.strip()
        if candidate.get("source_type") != "markdown":
            text = cleaned_text
        title = str(candidate.get("title") or "Migrated memory").strip() or "Migrated memory"
        if not cleaned_text:
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
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        title, body = _extract_markdown_title_and_body(file_path, text, normalize_title_input=True)
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
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
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
        "text": text if source_type == "markdown" else cleaned_text,
        "memory_type": "fact",
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
    }


def _iter_runtime_records(runtime: Runtime, *, scratch_directory: Path | None = None) -> Iterable[RecordEnvelope]:
    log_path = getattr(getattr(runtime.store, "log", None), "path", None)
    # A compacted/rotated journal can contain only the recent tail. Enumerate
    # current authority first; retain log-only recovery entries without letting
    # an older journal payload replace a hydrated authoritative record.
    # Preserve authority precedence and insertion order without retaining every
    # hydrated SQL/log envelope in RAM. This private scratch index is not an
    # authority and is removed before the completed backup is published.
    with tempfile.TemporaryDirectory(prefix='.eimemory-export-',dir=scratch_directory) as folder:
        path=Path(folder)/'records.sqlite'
        connection=sqlite3.connect(path)
        try:
            path.chmod(0o600)
            connection.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                PRAGMA cache_size=-4096; PRAGMA temp_store=FILE;
                CREATE TABLE export_records(position INTEGER PRIMARY KEY,key TEXT UNIQUE,payload TEXT NOT NULL);''')
            with _export_store_snapshot(runtime.store):
                for record in _records_from_sqlite(runtime):
                    connection.execute('''INSERT INTO export_records(key,payload) VALUES(?,?)
                        ON CONFLICT(key) DO UPDATE SET payload=excluded.payload''',
                        (_record_storage_key(record),json.dumps(record.to_dict(),ensure_ascii=False)))
                for record in _records_from_jsonl_log(log_path):
                    connection.execute('INSERT OR IGNORE INTO export_records(key,payload) VALUES(?,?)',
                        (_record_storage_key(record),json.dumps(record.to_dict(),ensure_ascii=False)))
            connection.commit()
            for row in connection.execute('SELECT payload FROM export_records ORDER BY position'):
                yield RecordEnvelope.from_dict(json.loads(row[0]))
        finally:
            connection.close()


@contextmanager
def _export_store_snapshot(store):
    """Pin SQL and its drained journal until the private export index is ready.

    Native writers enqueue their journal in the same SQL transaction. A write
    reservation closes the race between draining that outbox and reading both
    authorities; it is released before streaming the completed export to disk.
    Minimal legacy readers without this native facade retain their read API.
    """
    locked = getattr(store, "locked", None)
    flush = getattr(store, "flush_exports", None)
    if not callable(locked) or not callable(flush):
        yield
        return
    with locked() as sqlite:
        if sqlite.in_transaction:
            raise RuntimeError("backup_requires_own_transaction")
        report = flush(limit=1000)
        # Bound work by the backlog observed at entry; a continuously growing
        # outbox must return a retryable failure instead of hanging a backup.
        batches = (int(report["remaining"]) + 999) // 1000
        for _ in range(batches):
            if not report["remaining"]:
                break
            if not report["exported"]:
                raise RuntimeError("backup_journal_exports_pending")
            report = flush(limit=1000)
        if report["remaining"]:
            raise RuntimeError("backup_journal_exports_pending")
        sqlite.execute("BEGIN IMMEDIATE")
        try:
            if sqlite.pending_exports(limit=1):
                raise RuntimeError("backup_journal_exports_pending")
            yield
        finally:
            sqlite.rollback()


def _records_from_sqlite(runtime: Runtime) -> Iterable[RecordEnvelope]:
    batch_size = 100
    offset = 0
    while True:
        page = runtime.store.list_records(limit=batch_size, offset=offset)
        if not page:
            break
        yield from page
        offset += len(page)
        del page


def _records_from_jsonl_log(path: Path | None) -> Iterable[RecordEnvelope]:
    if path is None:
        return
    latest_by_key = {}
    operations = {}
    first_pass = hashlib.sha256()
    log = JsonlLog(path, create_parent=False)
    for entry in log.scan_strict():
        first_pass.update(_journal_entry_fingerprint(entry))
        if entry.operation_id:
            previous_digest = operations.get(entry.operation_id)
            if previous_digest is not None:
                if previous_digest != entry.payload_digest:
                    raise ValueError("backup_journal_operation_id_collision")
                continue
            operations[entry.operation_id] = entry.payload_digest
        record = RecordEnvelope.from_dict(entry.payload)
        retired = retired_record_ref(entry.payload)
        if retired is not None:
            old_key = _scope_record_key(ScopeRef.from_dict(retired["scope"]), retired["record_id"])
            old = latest_by_key.get(old_key)
            if old is not None and old[4] != retired["source_id"]:
                raise ValueError("backup_journal_retired_source_mismatch")
            latest_by_key.pop(old_key, None)
        key = _record_storage_key(record)
        latest_by_key[key]=(str(entry.path),entry.line,entry.offset,entry.payload_digest,record.source_id)
    wanted={value[:3]:(key,value[3]) for key,value in latest_by_key.items()}
    found=set()
    second_pass = hashlib.sha256()
    for entry in log.scan_strict():
        second_pass.update(_journal_entry_fingerprint(entry))
        position=(str(entry.path),entry.line,entry.offset)
        expected=wanted.get(position)
        if expected is None:continue
        record=RecordEnvelope.from_dict(entry.payload)
        if entry.payload_digest!=expected[1] or _record_storage_key(record)!=expected[0]:
            raise ValueError('backup_journal_changed_during_export')
        found.add(position)
        yield record
    if len(found)!=len(wanted) or second_pass.digest()!=first_pass.digest():
        raise ValueError('backup_journal_changed_during_export')


def _journal_entry_fingerprint(entry) -> bytes:
    return json.dumps([str(entry.path), entry.line, entry.offset, entry.operation_id,
                       entry.payload_digest], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _record_storage_key(record: RecordEnvelope) -> str:
    return _scope_record_key(record.scope, record.record_id)


def _scope_record_key(scope: ScopeRef, record_id: str) -> str:
    return json.dumps(
        [
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            record_id,
        ], ensure_ascii=False, separators=(",", ":")
    )


def _extract_markdown_title_and_body(
    path: Path, text: str, *, normalize_title_input: bool = False,
) -> tuple[str, str]:
    # The scanner historically stripped the source before selecting a title.
    # Keep that selection view separate from the body's boundary whitespace.
    lines = (text.strip() if normalize_title_input else text).splitlines()
    fence_char = ""
    fence_length = 0
    for index, line in enumerate(lines):
        # Markdown fences allow up to three leading spaces. A closing fence
        # must use the same character, be at least as long, and have no info.
        fence = line.lstrip(" ")
        if len(line) - len(fence) <= 3 and fence[:1] in {"`", "~"}:
            char = fence[0]
            length = len(fence) - len(fence.lstrip(char))
            suffix = fence[length:]
            if fence_char:
                if char == fence_char and length >= fence_length and not suffix.strip(" \t"):
                    fence_char = ""
            elif length >= 3 and (char == "~" or "`" not in suffix):
                fence_char = char
                fence_length = length
            continue
        if fence_char:
            continue
        if line.rstrip().startswith("# "):
            # Trim blank boundary newlines, not indentation or hard-break spaces.
            body_lines = text.lstrip().splitlines() if normalize_title_input else lines
            body = "\n".join(body_lines[index + 1:]).strip("\r\n")
            return line[2:].strip(), body
    return path.stem, text.strip("\r\n")


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
